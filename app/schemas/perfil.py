import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import Lista


class CorreoVerificarIn(BaseModel):
    codigo: str

    @field_validator("codigo")
    @classmethod
    def _codigo_valido(cls, v: str) -> str:
        v = re.sub(r"\D", "", v or "")
        if not re.fullmatch(r"\d{4,10}", v):
            raise ValueError("El código debe ser numérico.")
        return v


class CorreoVerificadoOut(BaseModel):
    correo_verificado: bool


# --------------------------------------------------------------------------- #
# Delivery addresses
# --------------------------------------------------------------------------- #

# Peru's bounds, the same as /coverage: a point outside is almost always
# latitude and longitude swapped.
_LAT = {"ge": -18.5, "le": 0.0}
_LNG = {"ge": -81.5, "le": -68.5}


class _CoordenadasJuntas(BaseModel):
    @model_validator(mode="after")
    def _ambas_o_ninguna(self):
        lat, lng = getattr(self, "latitud", None), getattr(self, "longitud", None)
        if (lat is None) != (lng is None):
            raise ValueError("Envía latitud y longitud juntas.")
        return self


class DireccionIn(_CoordenadasJuntas):
    etiqueta: Optional[str] = Field(None, max_length=40, description="Casa, Trabajo… Por defecto «Dirección»")
    calle: str = Field(min_length=1, max_length=200)
    referencia: Optional[str] = Field(None, max_length=200)
    distrito_id: int = Field(ge=1, description="Id de GET /locations/districts")
    latitud: Optional[float] = Field(None, **_LAT)
    longitud: Optional[float] = Field(None, **_LNG)
    por_defecto: bool = False


class DireccionCambios(_CoordenadasJuntas):
    """Only the fields that come are changed."""
    etiqueta: Optional[str] = Field(None, max_length=40)
    calle: Optional[str] = Field(None, min_length=1, max_length=200)
    referencia: Optional[str] = Field(None, max_length=200)
    distrito_id: Optional[int] = Field(None, ge=1)
    latitud: Optional[float] = Field(None, **_LAT)
    longitud: Optional[float] = Field(None, **_LNG)
    por_defecto: Optional[bool] = None


class CoberturaDireccion(BaseModel):
    cobertura: bool
    sede: Optional[str] = Field(None, description="Tienda que reparte ahí; fuera de cobertura, la más cercana")


class Direccion(BaseModel):
    id: int
    etiqueta: str
    calle: str
    referencia: str
    distrito_id: Optional[int] = None
    distrito: str
    provincia_id: Optional[int] = None
    departamento_id: Optional[int] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    por_defecto: bool
    cobertura: Optional[CoberturaDireccion] = Field(
        None, description="null si la dirección no tiene coordenadas o no se pudo consultar")


class DireccionesOut(Lista[Direccion]):
    pass


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #

class PerfilOut(BaseModel):
    nombre: str
    correo: str
    correo_verificado: bool
    telefono: str = Field(description="Nueve dígitos, sin +51")
    telefono_verificado: bool
    tipo_documento_id: Optional[int] = None
    tipo_documento: str
    numero_documento: str
    estado_validacion: str = Field(description="validado · pendiente · rechazado · vacío")
    nombre_bloqueado: bool = Field(description="true si RENIEC validó el nombre: no se puede cambiar")


class PerfilCambios(BaseModel):
    """Only the fields that come are changed. The phone changes with its own code: /me/phone/otp."""
    nombre: Optional[str] = Field(None, min_length=2, max_length=120)
    correo: Optional[str] = Field(None, max_length=254)

    @field_validator("correo")
    @classmethod
    def _correo_valido(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("Ingresa un correo válido.")
        return v


class TelefonoIn(BaseModel):
    telefono: str = Field(description="Celular de 9 dígitos que empieza con 9; acepta +51 y espacios")

    @field_validator("telefono")
    @classmethod
    def _telefono_valido(cls, v: str) -> str:
        v = re.sub(r"\D", "", v or "")
        if v.startswith("51") and len(v) == 11:
            v = v[2:]
        if not re.fullmatch(r"9\d{8}", v):
            raise ValueError("Ingresa un celular de 9 dígitos que empiece con 9.")
        return v
