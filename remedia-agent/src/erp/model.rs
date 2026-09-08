//! DTOs de ObServer Gestión (`ServiciosRestGestion.dll`), tal como vienen del ERP.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ProductoDTO {
    pub id_producto: i64,
    #[serde(default)]
    pub troquel: i64,
    #[serde(default)]
    pub codigo_barras: Vec<String>,
    #[serde(default)]
    pub descripcion: String,
    #[serde(default)]
    pub stock_sucursal: f64,
    #[serde(default)]
    pub precio: Decimal,
    #[serde(default)]
    pub categoria: String,
    #[serde(default)]
    pub rubro: String,
    #[serde(default)]
    pub subrubro: String,
    #[serde(default)]
    pub forma_farmaceutica: Option<String>,
    #[serde(default)]
    pub acciones_terapeuticas: Vec<String>,
    #[serde(default)]
    pub laboratorio: Option<String>,
    #[serde(default)]
    pub nombres_drogas: Option<String>,
    /// Condiciones de pago. Se parsean pero no se hashean ni se envían (spec §1).
    #[serde(default)]
    pub ofertas: Vec<Oferta>,
    #[serde(default = "default_true")]
    pub es_visible_en_venta: bool,
    /// Cantidad de productos que comparten el CB. `null` en consultas por ID.
    #[serde(default, rename = "visiblesMismoCB")]
    pub visibles_mismo_cb: Option<i32>,
    #[serde(default, rename = "Baja")]
    pub baja: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Oferta {
    #[serde(default, rename = "his_IdCondicionComercial")]
    pub his_id_condicion_comercial: i64,
    #[serde(default)]
    pub porcentaje: Decimal,
    #[serde(default)]
    pub descripcion: String,
}

/// Respuesta de `GET /api/productos/lote/{n}`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LoteResponse {
    pub cantidad_lotes: u32,
    #[serde(default)]
    pub productos: Vec<ProductoDTO>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_spec_sample() {
        let json = r#"{
          "idProducto": 7454, "troquel": 4479051, "codigoBarras": ["7795336085205"],
          "descripcion": "CLARITROMICINA RICHET 500 mg COM x    8", "stockSucursal": 0,
          "precio": 19641.2600, "categoria": "Medicamentos", "rubro": "Medicamentos",
          "subrubro": "Medicamentos", "formaFarmaceutica": "Comprimidos",
          "accionesTerapeuticas": ["Antibiótico"], "laboratorio": "Richet",
          "nombresDrogas": "Claritromicina",
          "ofertas": [{ "his_IdCondicionComercial": 11, "porcentaje": -3.1000,
                        "descripcion": "-3,10% con crédito 3 cuotas LaPos.." }],
          "esVisibleEnVenta": true, "visiblesMismoCB": 1, "Baja": false }"#;
        let p: ProductoDTO = serde_json::from_str(json).unwrap();
        assert_eq!(p.id_producto, 7454);
        assert_eq!(p.codigo_barras, vec!["7795336085205"]);
        assert_eq!(p.precio, Decimal::new(1964126, 2));
        assert_eq!(p.ofertas.len(), 1);
        assert_eq!(p.ofertas[0].his_id_condicion_comercial, 11);
        assert_eq!(p.visibles_mismo_cb, Some(1));
        assert!(!p.baja);
    }

    #[test]
    fn tolerates_missing_and_null_fields() {
        let json = r#"{"idProducto": 1, "descripcion": "X", "visiblesMismoCB": null,
                       "laboratorio": null, "nombresDrogas": null, "codigoBarras": []}"#;
        let p: ProductoDTO = serde_json::from_str(json).unwrap();
        assert_eq!(p.troquel, 0);
        assert!(p.codigo_barras.is_empty());
        assert!(p.es_visible_en_venta);
        assert_eq!(p.visibles_mismo_cb, None);
        assert_eq!(p.laboratorio, None);
    }
}
