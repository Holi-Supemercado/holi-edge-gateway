import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter

from ..clients.odoo_client import OdooClient, OdooClientError
from ..clients.redis_client import get_cache_redis, get_state_redis
from ..core.config import settings
from ..core.deps import ClienteOpcional, IdempotencyKey, RequestId, SedeRequerida, limite_por_minuto
from ..core.errors import ApiError, desde_odoo
from ..schemas.checkout import CompraIn, CompraOut, Franja, FranjasOut, LineaCompraIn
from .cart import precio_publicado

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/checkout", tags=["Checkout"])
odoo_client = OdooClient()


def _compra_out(resultado: Dict[str, Any], idempotente: bool) -> CompraOut:
    # holi-estado stores Odoo's English result (ADR-012); translate on the way out.
    return CompraOut(
        pedido_id=resultado["order_id"],
        numero=resultado["order_name"],
        importe_total=float(resultado.get("amount_total", 0.0)),
        estado=resultado.get("state", "sale"),
        idempotente=idempotente,
    )


@router.get("/slots", response_model=FranjasOut)
async def listar_franjas(sede: SedeRequerida, request_id: RequestId):
    """Available delivery windows of the store."""
    data = await odoo_client.fetch_delivery_slots(sede, request_id)
    franjas = [
        Franja(id=s["slot_id"], nombre=s["name"], disponible=s.get("available", True))
        for s in data.get("slots", [])
    ]
    return FranjasOut(sede=sede, items=franjas)


async def _lineas_para_odoo(sede: str, lineas: List[LineaCompraIn]) -> List[Dict[str, Any]]:
    """
    The order lines with the price the store published, never one the caller
    sent. A SKU with no published price goes without one and Odoo prices it
    with the store's pricelist.
    """
    cache_redis = get_cache_redis()
    resultado = []
    for linea in lineas:
        vals: Dict[str, Any] = {"sku": linea.sku, "qty": linea.cantidad}
        precio = await precio_publicado(cache_redis, sede, linea.sku)
        if precio:
            vals["price_unit"] = precio
        resultado.append(vals)
    return resultado


@router.post("", response_model=CompraOut,
             dependencies=[limite_por_minuto("compra", settings.RATE_LIMIT_CHECKOUT_PER_MINUTE)])
async def confirmar_compra(
    compra: CompraIn,
    sede: SedeRequerida,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
    cliente: ClienteOpcional,
):
    """
    Checkout Orchestration adhering to Rules 1, 2, 3 and 5:
    1. Idempotency reservation in holi-estado BEFORE touching Odoo.
    2. Calls Odoo under SEMAFORO_ODOO (max 20).
    3. Odoo decides stock via PostgreSQL SELECT ... FOR UPDATE.
    4. Caches final result in holi-estado.
    """
    state_redis = get_state_redis()

    # RULE 3: Atomic reservation before calling Odoo
    idem_key = f"idem:{idempotency_key}"
    reserved = await state_redis.set(idem_key, "en_curso", nx=True, ex=86400)

    if not reserved:
        stored_val = await state_redis.get(idem_key)
        if stored_val == "en_curso":
            # Concurrent retry in flight; wait briefly
            for _ in range(5):
                await asyncio.sleep(0.5)
                stored_val = await state_redis.get(idem_key)
                if stored_val and stored_val != "en_curso":
                    break

        if stored_val and stored_val != "en_curso":
            try:
                cached = json.loads(stored_val)
                _logger.info("Idempotent hit for key %s -> Returning cached order %s",
                             idempotency_key, cached.get("order_name"))
                return _compra_out(cached, idempotente=True)
            except Exception:
                pass

        raise ApiError("solicitud-en-curso")

    odoo_order_vals = {
        "sede_code": sede,
        "partner_id": cliente,
        "lines": await _lineas_para_odoo(sede, compra.lineas),
        "coupon_code": compra.cupon_codigo,
        "delivery_slot_id": compra.franja_id,
        "delivery_address_id": compra.direccion_id,
        "notes": compra.notas,
    }

    try:
        result = await odoo_client.execute_checkout(sede, odoo_order_vals, idempotency_key, request_id)

        resultado = {
            "order_id": result["order_id"],
            "order_name": result["order_name"],
            "amount_total": float(result.get("amount_total", 0.0)),
            "state": result.get("state", "sale"),
        }
        await state_redis.set(idem_key, json.dumps(resultado), ex=86400)
        return _compra_out(resultado, idempotente=False)

    except OdooClientError as e:
        # Release reservation if it was a client error (e.g. stock exhausted)
        if e.status_code == 409:
            await state_redis.delete(idem_key)
        else:
            await state_redis.set(idem_key, json.dumps({"error": e.detail}), ex=300)
        raise desde_odoo(e)

    except Exception:
        await state_redis.delete(idem_key)
        raise
