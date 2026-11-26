"""
The customer's delivery addresses and orders: the Edge translates them,
adds which store delivers to each address, and never serves them without a
session.
"""
import httpx
import pytest

from app.clients.odoo_client import OdooClientError
from app.core import deps as deps_module
from app.core.security import create_access_token
from app.main import app
from app.routers import cart as cart_router
from app.routers import master_data as master_data_router
from app.routers import pedidos as pedidos_router
from app.routers import perfil as perfil_router
from tests.test_auth_otp import FakeRedis

SEDE = {"X-Holi-Sede": "CHACH"}

DIRECCION_ODOO = {
    "id": 41, "label": "Casa", "street": "Av. Larco 1234", "reference": "Frente al parque",
    "district_id": 150122, "district_name": "Miraflores", "province_id": 1501, "department_id": 15,
    "latitude": -12.12, "longitude": -77.03, "is_default": True,
}


def _sesion(partner_id: int = 7) -> dict:
    return {"Authorization": f"Bearer {create_access_token(partner_id)}"}


class FakeOdooCliente:
    def __init__(self):
        self.llamadas = []

    async def customer_addresses(self, partner_id, request_id):
        self.llamadas.append(("listar", partner_id))
        sin_coordenadas = dict(DIRECCION_ODOO, id=42, latitude=None, longitude=None, is_default=False)
        return {"addresses": [DIRECCION_ODOO, sin_coordenadas]}

    async def customer_save_address(self, partner_id, address, address_id, request_id):
        self.llamadas.append(("guardar", partner_id, address, address_id))
        if address_id == 999:
            raise OdooClientError(404, "Not Found", "The delivery address does not exist.", {"codigo": "no-encontrado"})
        return {"address": dict(DIRECCION_ODOO, id=address_id or 43)}

    async def customer_delete_address(self, partner_id, address_id, request_id):
        self.llamadas.append(("eliminar", partner_id, address_id))
        return {"deleted": True}

    async def customer_orders(self, partner_id, page, page_size, request_id):
        self.llamadas.append(("pedidos", partner_id, page, page_size))
        return {"total": 1, "orders": [{
            "id": 10, "number": "S00010", "date": "2026-11-26T15:00:00Z", "status": "confirmado",
            "store_code": "CHACH", "address": "Av. Larco 1234, Miraflores", "address_id": 41,
            "untaxed": 21.19, "tax": 3.81, "total": 25.0, "item_count": 2,
            "lines": [{"sku": "LECHE", "name": "Leche", "quantity": 2.0, "uom": "Unidades",
                       "price_unit": 5.4, "subtotal": 10.8}],
        }]}

    async def customer_order(self, partner_id, order_id, request_id):
        if order_id != 10:
            raise OdooClientError(404, "Not Found", "The order does not exist.", {"codigo": "no-encontrado"})
        return {"order": (await self.customer_orders(partner_id, 1, 1, request_id))["orders"][0]}

    async def fetch_coverage_zones(self, request_id):
        # A square around Miraflores, delivered by CHACH.
        return {"zonas": [{
            "sede": "CHACH", "tienda": "Holi Chacarilla", "tienda_lat": -12.11, "tienda_lng": -76.99,
            "poligono": [{"lat": -12.0, "lng": -77.1}, {"lat": -12.0, "lng": -76.9},
                         {"lat": -12.2, "lng": -76.9}, {"lat": -12.2, "lng": -77.1}],
        }]}


@pytest.fixture
def entorno(monkeypatch):
    odoo = FakeOdooCliente()
    cache, estado = FakeRedis(), FakeRedis()
    for modulo in (perfil_router, pedidos_router, master_data_router):
        monkeypatch.setattr(modulo, "odoo_client", odoo)
    monkeypatch.setattr(master_data_router, "get_cache_redis", lambda: cache)
    monkeypatch.setattr(cart_router, "get_state_redis", lambda: estado)
    monkeypatch.setattr(cart_router, "get_cache_redis", lambda: cache)
    monkeypatch.setattr(deps_module, "get_state_redis", lambda: estado)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), odoo, estado


@pytest.mark.asyncio
async def test_las_direcciones_traen_la_sede_que_reparte(entorno):
    cliente, odoo, _ = entorno
    res = await cliente.get("/v1/me/addresses", headers=_sesion(7))
    assert res.status_code == 200, res.text
    casa, sin_coordenadas = res.json()["items"]
    assert casa["calle"] == "Av. Larco 1234" and casa["distrito"] == "Miraflores" and casa["por_defecto"] is True
    assert casa["cobertura"] == {"cobertura": True, "sede": "CHACH"}
    assert sin_coordenadas["cobertura"] is None
    assert odoo.llamadas[0] == ("listar", 7)


@pytest.mark.asyncio
async def test_crear_direccion_traduce_los_campos(entorno):
    cliente, odoo, _ = entorno
    res = await cliente.post("/v1/me/addresses", headers=_sesion(7), json={
        "etiqueta": "Casa", "calle": "Av. Larco 1234", "distrito_id": 150122,
        "latitud": -12.12, "longitud": -77.03, "por_defecto": True,
    })
    assert res.status_code == 200, res.text
    _, partner, enviado, address_id = odoo.llamadas[0]
    assert (partner, address_id) == (7, None)
    assert enviado == {"label": "Casa", "street": "Av. Larco 1234", "reference": None, "district_id": 150122,
                       "latitude": -12.12, "longitude": -77.03, "is_default": True}


@pytest.mark.asyncio
async def test_actualizar_envia_solo_lo_que_cambia(entorno):
    cliente, odoo, _ = entorno
    res = await cliente.patch("/v1/me/addresses/41", headers=_sesion(7), json={"referencia": "Puerta verde"})
    assert res.status_code == 200, res.text
    assert odoo.llamadas[0] == ("guardar", 7, {"reference": "Puerta verde"}, 41)


@pytest.mark.asyncio
async def test_direccion_ajena_es_404(entorno):
    cliente, _, _ = entorno
    res = await cliente.patch("/v1/me/addresses/999", headers=_sesion(7), json={"calle": "X"})
    assert res.status_code == 404
    assert res.json()["type"].endswith("/no-encontrado")


@pytest.mark.asyncio
@pytest.mark.parametrize("cuerpo", [
    {"calle": "Av. Larco", "distrito_id": 150122, "latitud": -12.1},          # latitude without longitude
    {"calle": "Av. Larco", "distrito_id": 150122, "latitud": -77.0, "longitud": -12.1},  # swapped
    {"calle": "", "distrito_id": 150122},
    {"calle": "Av. Larco"},
])
async def test_direccion_invalida_es_422(entorno, cuerpo):
    cliente, odoo, _ = entorno
    res = await cliente.post("/v1/me/addresses", headers=_sesion(7), json=cuerpo)
    assert res.status_code == 422, res.text
    assert odoo.llamadas == []


@pytest.mark.asyncio
async def test_eliminar_direccion(entorno):
    cliente, odoo, _ = entorno
    res = await cliente.delete("/v1/me/addresses/41", headers=_sesion(7))
    assert res.status_code == 200
    assert res.json() == {"mensaje": "Dirección eliminada."}
    assert odoo.llamadas[0] == ("eliminar", 7, 41)


@pytest.mark.asyncio
@pytest.mark.parametrize("metodo,ruta", [
    ("GET", "/v1/me/addresses"), ("POST", "/v1/me/addresses"), ("DELETE", "/v1/me/addresses/41"),
    ("GET", "/v1/orders"), ("GET", "/v1/orders/10"),
])
async def test_sin_sesion_no_hay_direcciones_ni_pedidos(entorno, metodo, ruta):
    cliente, odoo, _ = entorno
    res = await cliente.request(metodo, ruta, json={"calle": "X", "distrito_id": 1} if metodo == "POST" else None)
    assert res.status_code == 401
    assert res.json()["type"].endswith("/sesion-requerida")
    assert odoo.llamadas == []


@pytest.mark.asyncio
async def test_pedidos_del_cliente_paginados(entorno):
    cliente, odoo, _ = entorno
    res = await cliente.get("/v1/orders?pagina=2&por_pagina=5", headers=_sesion(7))
    assert res.status_code == 200, res.text
    cuerpo = res.json()
    assert cuerpo["paginacion"] == {"pagina": 2, "por_pagina": 5, "total": 1}
    pedido = cuerpo["items"][0]
    assert (pedido["numero"], pedido["estado"], pedido["sede"], pedido["total"]) == ("S00010", "confirmado", "CHACH", 25.0)
    assert [linea["sku"] for linea in pedido["lineas"]] == ["LECHE"]
    assert odoo.llamadas[0] == ("pedidos", 7, 2, 5)


@pytest.mark.asyncio
async def test_detalle_del_pedido_con_lineas_y_ajeno_404(entorno):
    cliente, _, _ = entorno
    res = await cliente.get("/v1/orders/10", headers=_sesion(7))
    assert res.status_code == 200, res.text
    assert res.json()["lineas"] == [{"sku": "LECHE", "nombre": "Leche", "cantidad": 2.0, "unidad": "Unidades",
                                     "precio_unitario": 5.4, "subtotal": 10.8}]
    res = await cliente.get("/v1/orders/11", headers=_sesion(7))
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_quitar_una_linea_del_carrito(entorno):
    cliente, _, _ = entorno
    cabeceras = {**SEDE, "X-Device-Id": "cel-1"}
    await cliente.put("/v1/cart/lines", json={"sku": "LECHE", "cantidad": 2}, headers=cabeceras)
    await cliente.put("/v1/cart/lines", json={"sku": "PAN", "cantidad": 1}, headers=cabeceras)
    res = await cliente.delete("/v1/cart/lines/LECHE", headers=cabeceras)
    assert res.status_code == 200, res.text
    assert [l["sku"] for l in res.json()["lineas"]] == ["PAN"]
    res = await cliente.delete("/v1/cart/lines/NO-ESTA", headers=cabeceras)
    assert res.status_code == 200
