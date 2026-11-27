"""
The customer's profile: what the app shows and what it may change. The phone
changes only after a code sent to the new number proves it.
"""
import httpx
import pytest

from app.clients.odoo_client import OdooClientError
from app.clients.otp_provider import set_otp_provider
from app.core.security import create_access_token
from app.main import app
from app.routers import perfil as perfil_router
from tests.test_auth_otp import FakeOtpProvider, FakeRedis

PERFIL_ODOO = {
    "name": "ANA TORRES", "email": "ana@example.com", "email_verified": True, "phone": "987650010",
    "phone_verified": True, "document_type_id": 4, "document_type": "DNI", "document_number": "70123456",
    "validation_state": "validado", "name_locked": True,
}


class FakeOdooPerfil:
    def __init__(self):
        self.perfil = dict(PERFIL_ODOO)
        self.llamadas = []

    async def customer_profile(self, partner_id, request_id):
        return {"profile": self.perfil}

    async def customer_update_profile(self, partner_id, profile, request_id):
        self.llamadas.append(("actualizar", partner_id, profile))
        if "name" in profile and self.perfil["name_locked"]:
            raise OdooClientError(409, "Profile Conflict", "Locked.", {"codigo": "nombre-validado"})
        if profile.get("email") == "otra@example.com":
            raise OdooClientError(409, "Profile Conflict", "Taken.", {"codigo": "cuenta-ya-existe"})
        if "email" in profile:
            self.perfil.update(email=profile["email"], email_verified=False)
        return {"profile": self.perfil}

    async def customer_check_phone(self, partner_id, phone, request_id):
        self.llamadas.append(("comprobar", partner_id, phone))
        if phone == "987650020":
            raise OdooClientError(409, "Profile Conflict", "Taken.", {"codigo": "cuenta-ya-existe"})
        return {"phone": phone}

    async def customer_set_phone(self, partner_id, phone, channel, request_id):
        self.llamadas.append(("guardar", partner_id, phone, channel))
        self.perfil.update(phone=phone, phone_verified=True)
        return {"profile": self.perfil}


@pytest.fixture
def entorno(monkeypatch):
    redis, provider, odoo = FakeRedis(), FakeOtpProvider(), FakeOdooPerfil()
    monkeypatch.setattr(perfil_router, "get_state_redis", lambda: redis)
    monkeypatch.setattr(perfil_router, "odoo_client", odoo)
    set_otp_provider(provider)
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test"), odoo, provider
    set_otp_provider(None)


def _sesion(partner_id: int = 501) -> dict:
    return {"Authorization": f"Bearer {create_access_token(partner_id)}"}


@pytest.mark.asyncio
async def test_el_perfil_trae_lo_verificado_y_lo_bloqueado(entorno):
    cliente, _, _ = entorno
    res = await cliente.get("/v1/me", headers=_sesion())
    assert res.status_code == 200, res.text
    assert res.json() == {
        "nombre": "ANA TORRES", "correo": "ana@example.com", "correo_verificado": True, "telefono": "987650010",
        "telefono_verificado": True, "tipo_documento_id": 4, "tipo_documento": "DNI", "numero_documento": "70123456",
        "estado_validacion": "validado", "nombre_bloqueado": True,
    }


@pytest.mark.asyncio
async def test_sin_sesion_no_hay_perfil(entorno):
    cliente, odoo, _ = entorno
    for metodo, ruta in (("GET", "/v1/me"), ("PATCH", "/v1/me"), ("POST", "/v1/me/phone/otp/send")):
        res = await cliente.request(metodo, ruta, json={"telefono": "912345678"} if metodo == "POST" else {})
        assert res.status_code == 401, (metodo, ruta)
    assert odoo.llamadas == []


@pytest.mark.asyncio
async def test_cambiar_el_correo_lo_deja_sin_verificar(entorno):
    cliente, odoo, _ = entorno
    res = await cliente.patch("/v1/me", json={"correo": " Ana.Nueva@Example.com "}, headers=_sesion())
    assert res.status_code == 200, res.text
    assert (res.json()["correo"], res.json()["correo_verificado"]) == ("ana.nueva@example.com", False)
    assert odoo.llamadas == [("actualizar", 501, {"email": "ana.nueva@example.com"})]


@pytest.mark.asyncio
async def test_nombre_validado_y_correo_ajeno_son_409_con_su_codigo(entorno):
    cliente, _, _ = entorno
    res = await cliente.patch("/v1/me", json={"nombre": "Otro Nombre"}, headers=_sesion())
    assert res.status_code == 409 and res.json()["type"].endswith("/nombre-validado")
    res = await cliente.patch("/v1/me", json={"correo": "otra@example.com"}, headers=_sesion())
    assert res.status_code == 409 and res.json()["type"].endswith("/cuenta-ya-existe")


@pytest.mark.asyncio
@pytest.mark.parametrize("cuerpo", [{"correo": "no-es-correo"}, {"nombre": "A"}])
async def test_valores_invalidos_son_422(entorno, cuerpo):
    cliente, odoo, _ = entorno
    res = await cliente.patch("/v1/me", json=cuerpo, headers=_sesion())
    assert res.status_code == 422
    assert odoo.llamadas == []


@pytest.mark.asyncio
async def test_cambiar_el_telefono_con_codigo_al_numero_nuevo(entorno):
    cliente, odoo, provider = entorno
    res = await cliente.post("/v1/me/phone/otp/send", json={"telefono": "+51 912 345 678"}, headers=_sesion())
    assert res.status_code == 200, res.text
    assert res.json()["canal"] == "sms"
    assert provider.sent[-1][:2] == ("+51912345678", "sms")
    res = await cliente.post("/v1/me/phone/otp/verify", json={"codigo": FakeOtpProvider.CODE}, headers=_sesion())
    assert res.status_code == 200, res.text
    assert (res.json()["telefono"], res.json()["telefono_verificado"]) == ("912345678", True)
    assert odoo.llamadas[-1] == ("guardar", 501, "912345678", "sms")
    # El código ya se usó: no sirve otra vez.
    res = await cliente.post("/v1/me/phone/otp/verify", json={"codigo": FakeOtpProvider.CODE}, headers=_sesion())
    assert res.status_code == 410


@pytest.mark.asyncio
async def test_telefono_de_otra_cuenta_no_gasta_un_envio(entorno):
    cliente, _, provider = entorno
    res = await cliente.post("/v1/me/phone/otp/send", json={"telefono": "987650020"}, headers=_sesion())
    assert res.status_code == 409 and res.json()["type"].endswith("/cuenta-ya-existe")
    assert provider.sent == []


@pytest.mark.asyncio
async def test_codigo_incorrecto_no_cambia_el_telefono(entorno):
    cliente, odoo, _ = entorno
    await cliente.post("/v1/me/phone/otp/send", json={"telefono": "912345678"}, headers=_sesion())
    res = await cliente.post("/v1/me/phone/otp/verify", json={"codigo": "000000"}, headers=_sesion())
    assert res.status_code == 400 and res.json()["type"].endswith("/codigo-incorrecto")
    assert not any(llamada[0] == "guardar" for llamada in odoo.llamadas)
