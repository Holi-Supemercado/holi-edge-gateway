import json
import uuid
from typing import Optional

from fastapi import APIRouter, Header

from ..clients.redis_client import get_cache_redis, get_state_redis
from ..core.config import settings
from ..core.deps import ClienteOpcional, SedeRequerida, limite_por_minuto
from ..schemas.cart import CarritoOut, LineaCarrito, LineaCarritoIn

router = APIRouter(prefix="/v1/cart", tags=["Cart"])

CARRITO_TTL_SECONDS = 604800


async def precio_publicado(cache_redis, sede: str, sku: str) -> Optional[float]:
    """The price Odoo published for the store, or None when it has none."""
    v_price = await cache_redis.get(f"v:price:{sede}") or "1"
    price_data = await cache_redis.hgetall(f"price:{v_price}:{sede}:{sku}")
    if not price_data or price_data.get("final_price") in (None, ""):
        return None
    return float(price_data["final_price"])


def _cantidad(linea_json: str) -> float:
    try:
        return float(json.loads(linea_json).get("qty", 0.0))
    except (ValueError, TypeError, AttributeError):
        return 0.0


async def _fusionar_lineas(state_redis, origen: str, destino: str) -> None:
    """
    Moves the guest lines into the customer's cart. A SKU in both keeps the
    larger quantity, never the sum: the same product added before and after
    logging in is one product, not two.
    """
    clave_origen, clave_destino = f"cart:{origen}:lines", f"cart:{destino}:lines"
    lineas_origen = await state_redis.hgetall(clave_origen)
    if lineas_origen:
        lineas_destino = await state_redis.hgetall(clave_destino)
        for sku, linea in lineas_origen.items():
            actual = lineas_destino.get(sku)
            if actual is None or _cantidad(linea) > _cantidad(actual):
                await state_redis.hset(clave_destino, sku, linea)
        await state_redis.expire(clave_destino, CARRITO_TTL_SECONDS)
    await state_redis.delete(clave_origen)


async def _resolve_cart_id(state_redis, partner_id: Optional[int], device_id: Optional[str]) -> str:
    """
    The cart behind the request. A customer's cart lives under the account, so
    the app and the agent see the same one; a guest's lives under the device.

    The first request carrying both moves the guest cart into the account and
    unlinks the device, so a shared phone stops showing it after logout. Every
    use renews the index: the lines outlive their index otherwise.
    """
    clave_dispositivo = f"cart:device:{device_id}" if device_id else None
    carrito_dispositivo = await state_redis.get(clave_dispositivo) if clave_dispositivo else None

    if not partner_id:
        if not clave_dispositivo:
            return str(uuid.uuid4())
        carrito = carrito_dispositivo or str(uuid.uuid4())
        await state_redis.set(clave_dispositivo, carrito, ex=CARRITO_TTL_SECONDS)
        return carrito

    clave_cliente = f"cart:user:{partner_id}"
    carrito = await state_redis.get(clave_cliente)
    if carrito_dispositivo and carrito_dispositivo != carrito:
        if carrito:
            await _fusionar_lineas(state_redis, carrito_dispositivo, carrito)
        else:
            carrito = carrito_dispositivo
        await state_redis.delete(clave_dispositivo)
    carrito = carrito or str(uuid.uuid4())
    await state_redis.set(clave_cliente, carrito, ex=CARRITO_TTL_SECONDS)
    return carrito


async def _leer_carrito(state_redis, cart_id: str, sede: str) -> CarritoOut:
    # Redis keeps the English line fields (qty, price_unit, name); ADR-012.
    raw_lines = await state_redis.hgetall(f"cart:{cart_id}:lines")
    lineas = []
    total = 0.0

    for sku, line_json in raw_lines.items():
        try:
            data = json.loads(line_json)
            cantidad = float(data.get("qty", 0.0))
            precio_unitario = float(data.get("price_unit", 0.0))
            total += cantidad * precio_unitario
            lineas.append(LineaCarrito(
                sku=sku,
                nombre=data.get("name", sku),
                cantidad=cantidad,
                precio_unitario=precio_unitario,
            ))
        except Exception:
            continue

    return CarritoOut(
        carrito_id=cart_id,
        sede=sede,
        lineas=lineas,
        total=round(total, 2),
        cantidad_lineas=len(lineas),
    )


@router.get("", response_model=CarritoOut)
async def ver_carrito(
    sede: SedeRequerida,
    cliente: ClienteOpcional,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
):
    """Active cart from Redis holi-estado: the customer's with a token, the device's without one."""
    state_redis = get_state_redis()
    cart_id = await _resolve_cart_id(state_redis, cliente, x_device_id)
    return await _leer_carrito(state_redis, cart_id, sede)


@router.put("/lines", response_model=CarritoOut,
            dependencies=[limite_por_minuto("carrito", settings.RATE_LIMIT_CART_PER_MINUTE)])
async def actualizar_linea(
    linea: LineaCarritoIn,
    sede: SedeRequerida,
    cliente: ClienteOpcional,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
):
    """Sets the quantity of a SKU in the cart; 0 removes the line."""
    state_redis = get_state_redis()
    cache_redis = get_cache_redis()
    cart_id = await _resolve_cart_id(state_redis, cliente, x_device_id)

    lines_key = f"cart:{cart_id}:lines"

    if linea.cantidad <= 0:
        await state_redis.hdel(lines_key, linea.sku)
    else:
        final_price = await precio_publicado(cache_redis, sede, linea.sku) or 0.0
        prod_data = await cache_redis.hgetall(f"prod:{linea.sku}")
        name = prod_data.get("name", linea.sku) if prod_data else linea.sku

        payload = json.dumps({"qty": linea.cantidad, "price_unit": final_price, "name": name})
        await state_redis.hset(lines_key, linea.sku, payload)
        await state_redis.expire(lines_key, CARRITO_TTL_SECONDS)

    return await _leer_carrito(state_redis, cart_id, sede)
