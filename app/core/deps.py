"""
Request inputs that repeat across the public API, declared once.

A router asks for them by type (`sede: SedeRequerida`) instead of reading and
checking the headers itself, so a missing header answers the same way on
every route and /docs lists it on every route that needs it.
"""
import asyncio
import logging
from typing import Annotated, Optional

import jwt
from fastapi import Depends, Header, Request, Response

from ..clients.redis_client import get_state_redis
from .agente import verificar_token_agente
from .errors import ApiError
from .limits import check_rate_limit
from .sede import leer_sede_por_defecto
from .security import SecurityError, verify_access_token

_logger = logging.getLogger(__name__)


async def _sede_requerida(
    response: Response,
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede"),
) -> str:
    """
    The store a request is answered for: the header, or else the default store
    Odoo marks. The response echoes it in X-Holi-Sede so the app always knows
    whose prices it is showing.
    """
    sede = (x_holi_sede or "").strip().upper()
    if not sede:
        por_defecto = await leer_sede_por_defecto()
        if not por_defecto:
            raise ApiError("parametros-invalidos",
                           "Falta la cabecera X-Holi-Sede y no hay una sede por defecto.",
                           errores=[{"campo": "X-Holi-Sede", "mensaje": "Cabecera obligatoria"}])
        sede = por_defecto["sede"]
    response.headers["X-Holi-Sede"] = sede
    return sede


def _sede_opcional(x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")) -> Optional[str]:
    return (x_holi_sede or "").strip().upper() or None


def _idempotency_key(idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")) -> str:
    if not (idempotency_key or "").strip():
        raise ApiError("idempotency-key-requerida")
    return idempotency_key.strip()


def _cliente_id(authorization: Optional[str] = Header(None)) -> int:
    """The customer behind the access token, verified locally with no network hop."""
    esquema, _, token = (authorization or "").partition(" ")
    if esquema.lower() != "bearer" or not token.strip():
        raise ApiError("sesion-requerida")
    try:
        return int(verify_access_token(token.strip())["sub"])
    except (SecurityError, KeyError, ValueError):
        raise ApiError("sesion-expirada", "El token de acceso no es válido o expiró.")


def _cliente_opcional(authorization: Optional[str] = Header(None)) -> Optional[int]:
    """
    The customer when a token comes along, a guest when none does. A token that
    fails is an error, never a guest: answering with the device's cart would
    show the customer an empty cart instead of asking the app to refresh.
    """
    if not (authorization or "").strip():
        return None
    return _cliente_id(authorization)


async def _agente(x_holi_agent_token: Optional[str] = Header(None, alias="X-Holi-Agent-Token")) -> Optional[str]:
    """
    The agent's service account when agent-api calls, None for the app. A token
    that fails is refused, never read as the app: the agent would silently fall
    back to the per-IP limit it shares with every customer it serves.
    """
    token = (x_holi_agent_token or "").strip()
    if not token:
        return None
    try:
        return await asyncio.to_thread(verificar_token_agente, token)
    except SecurityError as e:
        _logger.warning("Agent token refused: %s", e)
        raise ApiError("servicio-no-autorizado")
    except jwt.PyJWKClientConnectionError as e:
        _logger.error("Could not fetch Google's keys to check the agent token: %s", e)
        raise ApiError("servicio-no-disponible", "No se pudo verificar la credencial del servicio.")


def ip_del_cliente(request: Request) -> str:
    """Behind Cloud Run the caller's address is the first hop of X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def limite_por_minuto(alcance: str, maximo: int):
    """
    A per-minute ceiling on a route, as a dependency. Counted per customer with
    a session and per IP without one. For the agent it counts per customer it
    serves (account, else device): it reaches us from a single IP.
    """
    async def _verificar(
        request: Request,
        cliente: Optional[int] = Depends(_cliente_opcional),
        agente: Optional[str] = Depends(_agente),
        x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
    ) -> None:
        if agente:
            quien = f"agente:c:{cliente}" if cliente else f"agente:d:{x_device_id or ip_del_cliente(request)}"
        elif cliente:
            quien = f"c:{cliente}"
        else:
            quien = f"ip:{ip_del_cliente(request)}"
        if not await check_rate_limit(get_state_redis(), alcance, quien, max_requests=maximo, window_seconds=60):
            raise ApiError("limite-excedido")

    return Depends(_verificar)


def _request_id(request: Request) -> str:
    # Set by the correlation middleware in main.py before any route runs.
    return request.state.request_id


SedeRequerida = Annotated[str, Depends(_sede_requerida)]
SedeOpcional = Annotated[Optional[str], Depends(_sede_opcional)]
IdempotencyKey = Annotated[str, Depends(_idempotency_key)]
RequestId = Annotated[str, Depends(_request_id)]
ClienteId = Annotated[int, Depends(_cliente_id)]
ClienteOpcional = Annotated[Optional[int], Depends(_cliente_opcional)]
