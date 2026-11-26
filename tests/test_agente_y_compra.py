"""
agent-api calls with its own Google token plus the customer's; the checkout
takes the customer from the session and the prices from the store.
"""
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.clients.odoo_client import OdooClientError
from app.core import agente as agente_module
from app.core import deps as deps_module
from app.core.config import settings
from app.core.security import create_access_token
from app.main import app
from app.routers import cart as cart_router
from app.routers import checkout as checkout_router
from tests.test_auth_otp import FakeRedis

SEDE = {"X-Holi-Sede": "CHACH"}
AGENTE = "agent-api@holi-ecommerce.iam.gserviceaccount.com"
AUDIENCIA = "https://api.holi.com.pe"

_clave_privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token_google(correo=AGENTE, audiencia=AUDIENCIA, verificado=True, expira_en=600):
    ahora = int(time.time())
    return jwt.encode(
        {"iss": "https://accounts.google.com", "aud": audiencia, "email": correo,
         "email_verified": verificado, "iat": ahora, "exp": ahora + expira_en},
        _clave_privada, algorithm="RS256", headers={"kid": "prueba"},
    )


def _sesion(partner_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(partner_id)}"}


class FakeOdooCompra:
    def __init__(self):
        self.pedidos = []

    async def execute_checkout(self, sede, vals, idempotency_key, request_id):
        self.pedidos.append(vals)
        return {"order_id": len(self.pedidos), "order_name": f"S{len(self.pedidos):05d}",
                "amount_total": 10.0, "state": "sale"}


@pytest.fixture
def entorno(monkeypatch):
    estado, cache = FakeRedis(), FakeRedis()
    cache.values["v:price:CHACH"] = "3"
    cache.hashes["price:3:CHACH:LECHE"] = {"final_price": "4.5"}
    cache.hashes["prod:LECHE"] = {"name": "Leche"}
    odoo = FakeOdooCompra()
    for modulo in (cart_router, checkout_router):
        monkeypatch.setattr(modulo, "get_state_redis", lambda: estado)
        monkeypatch.setattr(modulo, "get_cache_redis", lambda: cache)
    monkeypatch.setattr(deps_module, "get_state_redis", lambda: estado)
    monkeypatch.setattr(checkout_router, "odoo_client", odoo)
    monkeypatch.setattr(settings, "AGENT_SERVICE_ACCOUNTS", AGENTE)
    monkeypatch.setattr(settings, "AGENT_TOKEN_AUDIENCE", AUDIENCIA)
    monkeypatch.setattr(agente_module._claves_google, "get_signing_key_from_jwt",
                        lambda token: SimpleNamespace(key=_clave_privada.public_key()))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), estado, odoo


# --------------------------------------------------------------------------- #
# Agent identity
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_el_agente_opera_el_mismo_carrito_que_la_app(entorno):
    cliente, _, _ = entorno
    agente = {"X-Holi-Agent-Token": _token_google(), **_sesion(7)}
    res = await cliente.put("/v1/cart/lines", json={"sku": "LECHE", "cantidad": 2}, headers={**SEDE, **agente})
    assert res.status_code == 200, res.text

    en_la_app = await cliente.get("/v1/cart", headers={**SEDE, **_sesion(7), "X-Device-Id": "cel-1"})
    assert [(l["sku"], l["cantidad"]) for l in en_la_app.json()["lineas"]] == [("LECHE", 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
    _token_google(audiencia="https://otro-servicio"),
    _token_google(correo="intruso@otro-proyecto.iam.gserviceaccount.com"),
    _token_google(verificado=False),
    _token_google(expira_en=-60),
    "no-es-un-jwt",
])
async def test_token_de_agente_invalido_se_rechaza(entorno, token):
    cliente, _, _ = entorno
    res = await cliente.put("/v1/cart/lines", json={"sku": "LECHE", "cantidad": 1},
                            headers={**SEDE, "X-Holi-Agent-Token": token, "X-Device-Id": "wa-1"})
    assert res.status_code == 401
    assert res.json()["type"].endswith("/servicio-no-autorizado")


@pytest.mark.asyncio
async def test_sin_agente_configurado_ningun_token_entra(entorno, monkeypatch):
    cliente, _, _ = entorno
    monkeypatch.setattr(settings, "AGENT_SERVICE_ACCOUNTS", "")
    res = await cliente.put("/v1/cart/lines", json={"sku": "LECHE", "cantidad": 1},
                            headers={**SEDE, "X-Holi-Agent-Token": _token_google(), "X-Device-Id": "wa-1"})
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# Per-minute limits
# --------------------------------------------------------------------------- #

async def _llenar_carrito(cliente, veces, **cabeceras):
    codigos = []
    for _ in range(veces):
        res = await cliente.put("/v1/cart/lines", json={"sku": "LECHE", "cantidad": 1}, headers={**SEDE, **cabeceras})
        codigos.append(res.status_code)
    return codigos


@pytest.mark.asyncio
async def test_el_limite_del_agente_es_por_cliente_atendido(entorno):
    cliente, _, _ = entorno
    maximo = settings.RATE_LIMIT_CART_PER_MINUTE
    token = _token_google()

    codigos = await _llenar_carrito(cliente, maximo + 1, **{"X-Holi-Agent-Token": token}, **_sesion(7))
    assert codigos[:maximo] == [200] * maximo
    assert codigos[-1] == 429

    # Same agent, same IP, another customer: its own allowance.
    otro = await _llenar_carrito(cliente, 1, **{"X-Holi-Agent-Token": token}, **_sesion(8))
    assert otro == [200]


@pytest.mark.asyncio
async def test_sin_sesion_el_limite_es_por_ip(entorno):
    cliente, _, _ = entorno
    maximo = settings.RATE_LIMIT_CART_PER_MINUTE
    codigos = []
    for i in range(maximo + 1):
        codigos += await _llenar_carrito(cliente, 1, **{"X-Device-Id": f"cel-{i}"})
    assert codigos[-1] == 429, "changing the device must not reset a guest's allowance"


# --------------------------------------------------------------------------- #
# Checkout
# --------------------------------------------------------------------------- #

async def _comprar(cliente, lineas, **cabeceras):
    return await cliente.post("/v1/checkout", json={"lineas": lineas},
                              headers={**SEDE, "Idempotency-Key": f"k-{time.time_ns()}", **cabeceras})


@pytest.mark.asyncio
async def test_la_compra_va_a_nombre_del_cliente_de_la_sesion(entorno):
    cliente, _, odoo = entorno
    res = await _comprar(cliente, [{"sku": "LECHE", "cantidad": 1}], **_sesion(7))
    assert res.status_code == 200, res.text
    assert odoo.pedidos[0]["partner_id"] == 7


@pytest.mark.asyncio
async def test_la_compra_ignora_el_precio_enviado_y_usa_el_publicado(entorno):
    cliente, _, odoo = entorno
    res = await _comprar(cliente, [{"sku": "LECHE", "cantidad": 2, "precio_unitario": 0.01},
                                   {"sku": "SIN-PRECIO", "cantidad": 1, "precio_unitario": 0.01}])
    assert res.status_code == 200, res.text
    assert odoo.pedidos[0]["lines"] == [
        {"sku": "LECHE", "qty": 2.0, "price_unit": 4.5},
        {"sku": "SIN-PRECIO", "qty": 1.0},
    ]
    assert odoo.pedidos[0]["partner_id"] is None


# --------------------------------------------------------------------------- #
# Quote: what the order costs, computed by Odoo
# --------------------------------------------------------------------------- #

COTIZACION_ODOO = {
    "subtotal": 9.0, "app_discount": 0.9, "app_discount_percent": 10.0, "delivery_fee": 9.9,
    "free_delivery_threshold": 300.0, "missing_for_free_delivery": 291.0, "min_order_amount": 5.0,
    "untaxed": 15.26, "tax": 2.74, "total": 18.0,
}


@pytest.mark.asyncio
async def test_la_cotizacion_usa_el_precio_publicado_y_traduce_los_montos(entorno, monkeypatch):
    cliente, _, odoo = entorno
    enviado = {}

    async def checkout_quote(sede, partner_id, lines, request_id):
        enviado.update(sede=sede, partner_id=partner_id, lines=lines)
        return COTIZACION_ODOO

    monkeypatch.setattr(odoo, "checkout_quote", checkout_quote, raising=False)
    res = await cliente.post("/v1/checkout/quote", json={"lineas": [{"sku": "LECHE", "cantidad": 2}]},
                             headers={**SEDE, **_sesion(7)})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "subtotal": 9.0, "descuento_app": 0.9, "descuento_app_pct": 10.0, "envio": 9.9, "envio_gratis_desde": 300.0,
        "faltante_envio_gratis": 291.0, "pedido_minimo": 5.0, "impuestos": 2.74, "total": 18.0,
    }
    assert enviado == {"sede": "CHACH", "partner_id": 7, "lines": [{"sku": "LECHE", "qty": 2.0, "price_unit": 4.5}]}


@pytest.mark.asyncio
async def test_por_debajo_del_minimo_es_422(entorno, monkeypatch):
    cliente, _, odoo = entorno

    async def checkout_quote(sede, partner_id, lines, request_id):
        raise OdooClientError(422, "Minimum Order Not Reached", "The minimum order is 20.00.",
                              {"codigo": "pedido-minimo", "minimo": 20.0, "faltante": 11.0})

    monkeypatch.setattr(odoo, "checkout_quote", checkout_quote, raising=False)
    res = await cliente.post("/v1/checkout/quote", json={"lineas": [{"sku": "LECHE", "cantidad": 2}]}, headers=SEDE)
    assert res.status_code == 422
    assert res.json()["type"].endswith("/pedido-minimo")
    assert res.json()["faltante"] == 11.0
