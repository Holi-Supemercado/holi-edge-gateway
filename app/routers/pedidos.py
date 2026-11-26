"""
The customer's own orders. Odoo answers only with the orders of the partner
behind the access token: somebody else's order is 404, never 403, so the
answer does not confirm that it exists.
"""
from typing import Any, Dict

from fastapi import APIRouter, Query

from ..clients.odoo_client import OdooClient
from ..core.deps import ClienteId, RequestId
from ..schemas.common import Paginacion
from ..schemas.pedidos import LineaPedido, PedidoDetalle, PedidosOut

router = APIRouter(prefix="/v1/orders", tags=["Pedidos"])
odoo_client = OdooClient()


def _pedido(o: Dict[str, Any]) -> PedidoDetalle:
    """Odoo's order (English, ADR-012) as the contract shows it."""
    return PedidoDetalle(
        id=o["id"], numero=o.get("number", ""), fecha=o.get("date"), estado=o.get("status", ""),
        sede=o.get("store_code"), direccion=o.get("address", ""), direccion_id=o.get("address_id"),
        subtotal=o.get("untaxed", 0.0), impuestos=o.get("tax", 0.0), total=o.get("total", 0.0),
        envio=o.get("delivery_fee", 0.0), descuento=o.get("discount_total", 0.0),
        cantidad_items=o.get("item_count", 0),
        lineas=[
            LineaPedido(sku=linea.get("sku", ""), nombre=linea.get("name", ""), cantidad=linea.get("quantity", 0.0),
                        unidad=linea.get("uom", ""), precio_unitario=linea.get("price_unit", 0.0),
                        subtotal=linea.get("subtotal", 0.0))
            for linea in o.get("lines", [])
        ],
    )


@router.get("", response_model=PedidosOut)
async def listar_pedidos(
    cliente_id: ClienteId,
    request_id: RequestId,
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(20, ge=1, le=50),
):
    """The customer's app orders, newest first, each with its lines."""
    data = await odoo_client.customer_orders(cliente_id, pagina, por_pagina, request_id)
    return PedidosOut(
        items=[_pedido(o) for o in data.get("orders", [])],
        paginacion=Paginacion(pagina=pagina, por_pagina=por_pagina, total=data.get("total", 0)),
    )


@router.get("/{pedido_id}", response_model=PedidoDetalle)
async def ver_pedido(pedido_id: int, cliente_id: ClienteId, request_id: RequestId):
    """One order with its lines."""
    return _pedido((await odoo_client.customer_order(cliente_id, pedido_id, request_id))["order"])
