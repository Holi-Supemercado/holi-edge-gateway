"""
The cart belongs to the customer when a token comes along and to the device
when none does, so the app and the agent see the same cart.
"""
import json

import httpx
import pytest

from app.core import deps as deps_module
from app.core.security import create_access_token
from app.main import app
from app.routers import cart as cart_router
from tests.test_auth_otp import FakeRedis

SEDE = {"X-Holi-Sede": "CHACH"}


def _sesion(partner_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(partner_id)}"}


@pytest.fixture
def entorno(monkeypatch):
    estado, cache = FakeRedis(), FakeRedis()
    cache.values["v:price:CHACH"] = "3"
    cache.hashes["price:3:CHACH:LECHE"] = {"final_price": "4.5"}
    cache.hashes["prod:LECHE"] = {"name": "Leche"}
    cache.hashes["price:3:CHACH:PAN"] = {"final_price": "0.5"}
    cache.hashes["prod:PAN"] = {"name": "Pan"}
    monkeypatch.setattr(cart_router, "get_state_redis", lambda: estado)
    monkeypatch.setattr(cart_router, "get_cache_redis", lambda: cache)
    monkeypatch.setattr(deps_module, "get_state_redis", lambda: estado)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), estado


async def _poner(cliente, sku, cantidad, **cabeceras):
    res = await cliente.put("/v1/cart/lines", json={"sku": sku, "cantidad": cantidad}, headers={**SEDE, **cabeceras})
    assert res.status_code == 200, res.text
    return res.json()


def _cantidades(carrito: dict) -> dict:
    return {l["sku"]: l["cantidad"] for l in carrito["lineas"]}


@pytest.mark.asyncio
async def test_invitado_usa_el_carrito_del_dispositivo(entorno):
    cliente, _ = entorno
    await _poner(cliente, "LECHE", 2, **{"X-Device-Id": "cel-1"})
    res = await cliente.get("/v1/cart", headers={**SEDE, "X-Device-Id": "cel-1"})
    assert _cantidades(res.json()) == {"LECHE": 2}
    assert res.json()["total"] == 9.0


@pytest.mark.asyncio
async def test_con_sesion_el_carrito_es_de_la_cuenta_y_no_del_dispositivo(entorno):
    cliente, _ = entorno
    await _poner(cliente, "LECHE", 1, **_sesion(7), **{"X-Device-Id": "cel-1"})

    # The agent calls with the same token and no device: same cart.
    res = await cliente.get("/v1/cart", headers={**SEDE, **_sesion(7)})
    assert _cantidades(res.json()) == {"LECHE": 1}

    # Another customer on another device does not see it.
    otro = await cliente.get("/v1/cart", headers={**SEDE, **_sesion(8), "X-Device-Id": "cel-2"})
    assert otro.json()["lineas"] == []


@pytest.mark.asyncio
async def test_al_iniciar_sesion_el_carrito_del_invitado_pasa_a_la_cuenta(entorno):
    cliente, estado = entorno
    await _poner(cliente, "PAN", 3, **{"X-Device-Id": "cel-1"})

    res = await cliente.get("/v1/cart", headers={**SEDE, **_sesion(7), "X-Device-Id": "cel-1"})
    assert _cantidades(res.json()) == {"PAN": 3}
    assert await estado.get("cart:device:cel-1") is None

    # After logout the shared phone starts with an empty cart.
    tras_salir = await cliente.get("/v1/cart", headers={**SEDE, "X-Device-Id": "cel-1"})
    assert tras_salir.json()["lineas"] == []


@pytest.mark.asyncio
async def test_la_fusion_conserva_la_cantidad_mayor_y_no_suma(entorno):
    cliente, estado = entorno
    await _poner(cliente, "LECHE", 2, **_sesion(7))
    await _poner(cliente, "PAN", 1, **_sesion(7))
    await _poner(cliente, "LECHE", 1, **{"X-Device-Id": "cel-1"})
    await _poner(cliente, "PAN", 4, **{"X-Device-Id": "cel-1"})
    carrito_invitado = await estado.get("cart:device:cel-1")

    res = await cliente.get("/v1/cart", headers={**SEDE, **_sesion(7), "X-Device-Id": "cel-1"})
    assert _cantidades(res.json()) == {"LECHE": 2, "PAN": 4}
    assert await estado.hgetall(f"cart:{carrito_invitado}:lines") == {}


@pytest.mark.asyncio
async def test_token_invalido_no_cae_al_carrito_del_dispositivo(entorno):
    cliente, _ = entorno
    await _poner(cliente, "PAN", 1, **{"X-Device-Id": "cel-1"})
    res = await cliente.get("/v1/cart", headers={**SEDE, "Authorization": "Bearer no-es-un-token", "X-Device-Id": "cel-1"})
    assert res.status_code == 401
    assert res.json()["type"].endswith("/sesion-expirada")


@pytest.mark.asyncio
async def test_cantidad_cero_quita_la_linea(entorno):
    cliente, _ = entorno
    await _poner(cliente, "PAN", 2, **_sesion(7))
    carrito = await _poner(cliente, "PAN", 0, **_sesion(7))
    assert carrito["lineas"] == []


@pytest.mark.asyncio
async def test_el_precio_sale_del_cache_de_la_sede(entorno):
    cliente, estado = entorno
    carrito = await _poner(cliente, "LECHE", 1, **_sesion(7))
    assert carrito["lineas"][0]["precio_unitario"] == 4.5
    guardado = json.loads((await estado.hgetall(f"cart:{carrito['carrito_id']}:lines"))["LECHE"])
    assert guardado == {"qty": 1.0, "price_unit": 4.5, "name": "Leche"}
