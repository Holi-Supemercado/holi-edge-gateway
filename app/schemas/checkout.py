from typing import List, Optional

from pydantic import BaseModel, Field

from .common import Lista


class LineaCompraIn(BaseModel):
    """The price is never taken from here: the Edge uses the one the store published."""
    sku: str
    cantidad: float = Field(gt=0)


class CompraIn(BaseModel):
    lineas: List[LineaCompraIn]
    cupon_codigo: Optional[str] = None
    franja_id: Optional[str] = None
    direccion_id: Optional[int] = None
    notas: Optional[str] = None


class CotizacionIn(BaseModel):
    lineas: List[LineaCompraIn] = Field(min_length=1)


class CotizacionOut(BaseModel):
    """
    What the order costs, computed by Odoo with the same rules as the checkout:
    the app shows these amounts and the customer pays exactly `total`.
    """
    subtotal: float = Field(description="Suma de los productos a precio de la sede")
    descuento_app: float
    descuento_app_pct: float
    envio: float = Field(description="0 si llega al envío gratis")
    envio_gratis_desde: float
    faltante_envio_gratis: float
    pedido_minimo: float
    impuestos: float
    total: float


class CompraOut(BaseModel):
    pedido_id: int
    numero: str
    importe_total: float
    estado: str
    idempotente: bool = Field(default=False, description="true si es la respuesta guardada de un intento anterior")


class Franja(BaseModel):
    id: str
    nombre: str
    disponible: bool = True


class FranjasOut(Lista[Franja]):
    sede: str
