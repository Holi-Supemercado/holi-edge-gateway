from typing import List, Optional

from pydantic import BaseModel, Field

from .common import ListaPaginada


class PedidoResumen(BaseModel):
    id: int
    numero: str
    fecha: Optional[str] = Field(None, description="ISO 8601 en UTC")
    estado: str = Field(description="pendiente · confirmado · entregado · cancelado")
    sede: Optional[str] = None
    direccion: str = Field(description="Calle y distrito de entrega; vacío si no se eligió dirección")
    direccion_id: Optional[int] = None
    subtotal: float = Field(description="Sin impuestos")
    impuestos: float
    total: float
    cantidad_items: int


class LineaPedido(BaseModel):
    sku: str
    nombre: str
    cantidad: float
    unidad: str
    precio_unitario: float
    subtotal: float = Field(description="Con impuestos")


class PedidoDetalle(PedidoResumen):
    lineas: List[LineaPedido]


class PedidosOut(ListaPaginada[PedidoDetalle]):
    """Each order comes with its lines: the app shows what it had without a call per order."""
