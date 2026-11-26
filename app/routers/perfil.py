"""
E-mail verification from the profile (ADR-017).

The phone is proven at sign-up; the e-mail is proven here, whenever the
customer chooses to, and a purchase needs both. Same division of labour as
sign-up (ADR-013): Twilio Verify sends and judges the code, the Edge counts
and throttles, and Odoo only receives the proven fact.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request

from ..clients.odoo_client import OdooClient, OdooClientError
from ..clients.otp_provider import get_otp_provider
from ..clients.redis_client import get_state_redis
from ..core.config import settings
from ..core.deps import ClienteId, RequestId, ip_del_cliente
from ..core.errors import ApiError
from ..core.otp_guard import check_send_allowed, register_check_attempt, send_refusal, start_cooldown
from ..core.passwords import mask_destination
from ..schemas.auth import OtpEnviarOut
from ..schemas.common import MensajeOut
from ..schemas.perfil import (
    CoberturaDireccion, CorreoVerificadoOut, CorreoVerificarIn, Direccion, DireccionCambios, DireccionesOut,
    DireccionIn,
)
from .auth import primer_nombre
from .master_data import cobertura_de

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/me", tags=["Perfil"])
odoo_client = OdooClient()

CANAL = "correo"


def _clave(cliente_id: int) -> str:
    # Plays the role of registro_id for the OTP guard: cooldown and attempts.
    return f"correo-{cliente_id}"


def _sesion_otp(cliente_id: int) -> str:
    return f"otp:correo:{cliente_id}"


@router.post("/email/otp/send", response_model=OtpEnviarOut)
async def enviar_codigo_correo(cliente_id: ClienteId, request: Request, request_id: RequestId):
    """Sends a code to the customer's current e-mail, as Odoo has it right now."""
    state_redis = get_state_redis()
    clave = _clave(cliente_id)

    estado = await odoo_client.auth_email_status(cliente_id, request_id)
    correo = (estado.get("email") or "").strip().lower()
    if not correo:
        raise ApiError("destino-invalido", "Tu cuenta no tiene un correo registrado.")
    if estado.get("email_verified"):
        raise ApiError("correo-ya-verificado")

    veredicto = await check_send_allowed(state_redis, correo, ip_del_cliente(request), clave)
    if not veredicto.allowed:
        raise send_refusal(veredicto)

    reintento = await start_cooldown(state_redis, clave)
    resultado = await get_otp_provider().send(
        correo, CANAL, settings.TWILIO_LOCALE,
        email_template=settings.TWILIO_EMAIL_TEMPLATE_EMAIL_VERIFICATION,
        substitutions={"first_name": primer_nombre(estado.get("name", ""))},
    )

    # The address is pinned here: the check goes to the address that received
    # the code, and Odoo refuses it if the account moved to another one since.
    await state_redis.hset(_sesion_otp(cliente_id), mapping={"sid": resultado.sid, "destino": correo})
    await state_redis.expire(_sesion_otp(cliente_id), settings.OTP_SESSION_TTL_SECONDS)
    await state_redis.delete(f"otp:try:{clave}")

    return OtpEnviarOut(
        enviado=True,
        canal=CANAL,
        destino_enmascarado=mask_destination(correo, CANAL),
        reintento_en=reintento,
        expira_en=settings.OTP_SESSION_TTL_SECONDS,
    )


@router.post("/email/otp/verify", response_model=CorreoVerificadoOut)
async def verificar_codigo_correo(payload: CorreoVerificarIn, cliente_id: ClienteId, request_id: RequestId):
    """Spends one attempt against Twilio's verdict and records the proven address in Odoo."""
    state_redis = get_state_redis()
    clave = _clave(cliente_id)

    intento = await register_check_attempt(state_redis, clave)
    if not intento.allowed:
        if intento.reason == "estado_no_disponible":
            raise ApiError("servicio-no-disponible", "No podemos verificar el código en este momento.")
        await state_redis.delete(_sesion_otp(cliente_id))
        raise ApiError("intentos-agotados", "Demasiados intentos fallidos. Solicita un código nuevo.")

    sesion = await state_redis.hgetall(_sesion_otp(cliente_id))
    destino = sesion.get("destino") if sesion else None
    if not destino:
        raise ApiError("codigo-expirado")

    resultado = await get_otp_provider().check(destino, payload.codigo)
    if not resultado.approved:
        raise ApiError("codigo-expirado" if resultado.status == "expired" else "codigo-incorrecto")

    try:
        await odoo_client.auth_mark_email_verified(cliente_id, destino, request_id)
    except OdooClientError as e:
        if e.status_code != 409:
            raise
        await state_redis.delete(_sesion_otp(cliente_id))
        raise ApiError("conflicto", "Tu correo cambió mientras lo verificabas. Solicita un código nuevo.")

    await state_redis.delete(_sesion_otp(cliente_id), f"otp:try:{clave}")
    _logger.info("E-mail verified for partner %s (%s)", cliente_id, request_id)
    return CorreoVerificadoOut(correo_verificado=True)


# --------------------------------------------------------------------------- #
# Delivery addresses. Odoo keeps them as the customer's delivery contacts;
# the Edge adds which store delivers there, from its coverage polygons.
# --------------------------------------------------------------------------- #

# Contract field -> Odoo field (ADR-012: Spanish outside, English inside).
_A_ODOO = {
    "etiqueta": "label", "calle": "street", "referencia": "reference", "distrito_id": "district_id",
    "latitud": "latitude", "longitud": "longitude", "por_defecto": "is_default",
}


async def _cobertura(direccion: Dict[str, Any], request_id: str) -> Optional[CoberturaDireccion]:
    lat, lng = direccion.get("latitude"), direccion.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        respuesta = await cobertura_de(lat, lng, request_id)
    except Exception as e:  # noqa: BLE001 — an address list must not fail for the coverage lookup
        _logger.warning("Coverage lookup failed for an address: %s", e)
        return None
    return CoberturaDireccion(cobertura=respuesta.cobertura, sede=respuesta.tienda.sede if respuesta.tienda else None)


async def _direcciones(crudas: List[Dict[str, Any]], request_id: str) -> List[Direccion]:
    coberturas = await asyncio.gather(*(_cobertura(d, request_id) for d in crudas))
    return [
        Direccion(
            id=d["id"], etiqueta=d.get("label", ""), calle=d.get("street", ""), referencia=d.get("reference", ""),
            distrito_id=d.get("district_id"), distrito=d.get("district_name", ""),
            provincia_id=d.get("province_id"), departamento_id=d.get("department_id"),
            latitud=d.get("latitude"), longitud=d.get("longitude"), por_defecto=bool(d.get("is_default")),
            cobertura=cobertura,
        )
        for d, cobertura in zip(crudas, coberturas)
    ]


def _para_odoo(campos: Dict[str, Any]) -> Dict[str, Any]:
    return {_A_ODOO[k]: v for k, v in campos.items() if k in _A_ODOO}


@router.get("/addresses", response_model=DireccionesOut)
async def listar_direcciones(cliente_id: ClienteId, request_id: RequestId):
    """The customer's delivery addresses, the default one first."""
    data = await odoo_client.customer_addresses(cliente_id, request_id)
    return DireccionesOut(items=await _direcciones(data.get("addresses", []), request_id))


@router.post("/addresses", response_model=Direccion)
async def crear_direccion(direccion: DireccionIn, cliente_id: ClienteId, request_id: RequestId):
    """A new delivery address. The first one becomes the default."""
    data = await odoo_client.customer_save_address(cliente_id, _para_odoo(direccion.model_dump()), None, request_id)
    return (await _direcciones([data["address"]], request_id))[0]


@router.patch("/addresses/{direccion_id}", response_model=Direccion)
async def actualizar_direccion(direccion_id: int, cambios: DireccionCambios, cliente_id: ClienteId,
                               request_id: RequestId):
    """Changes only the fields sent. An address of another customer is 404."""
    campos = _para_odoo(cambios.model_dump(exclude_unset=True))
    data = await odoo_client.customer_save_address(cliente_id, campos, direccion_id, request_id)
    return (await _direcciones([data["address"]], request_id))[0]


@router.delete("/addresses/{direccion_id}", response_model=MensajeOut)
async def eliminar_direccion(direccion_id: int, cliente_id: ClienteId, request_id: RequestId):
    """Removes the address from the list; past orders keep it. If it was the default, another takes its place."""
    await odoo_client.customer_delete_address(cliente_id, direccion_id, request_id)
    return MensajeOut(mensaje="Dirección eliminada.")
