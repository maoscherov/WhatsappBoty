from pydantic import BaseModel
from typing import Optional


class SKU(BaseModel):
    sku_id: str                      # SKU interno (ID numérico del catálogo)
    barcode: str                     # Codigo_Barras_1 (preferido para MP)
    sku_nombre: str                  # Nombre normalizado para búsqueda
    sku_nombre_original: str         # Nombre tal como viene del catálogo
    marca: str = ""
    laboratorio: str = ""
    categoria: str = ""
    es_medicamento: bool = False
    precio_venta: float = 0.0
    stock_actual: Optional[float] = None
    ventas_mes: Optional[float] = None
    prom_semanal: Optional[float] = None
    cantidad_visible: int = 0        # -1 = sin datos (mostrar "Consultar")
    tipo_producto: str = "regular"   # regular | estacional
    pausado: bool = False

    @property
    def disponible(self) -> bool:
        return not self.pausado and self.cantidad_visible > 0

    @property
    def estado(self) -> str:
        if self.pausado:
            return "pausado"
        if self.cantidad_visible > 0:
            return "disponible"
        return "consultar"
