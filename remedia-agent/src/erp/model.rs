//! DTOs de ObServer Gestión (`ServiciosRestGestion.dll`), tal como vienen del ERP.

use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize};

fn default_true() -> bool {
    true
}

/// `null` o campo ausente → valor por defecto. El ERP manda `null` en campos
/// que el DTO original declaraba no nulos (troquel, listas, precio...).
fn null_default<'de, D, T>(d: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Default + Deserialize<'de>,
{
    Ok(Option::<T>::deserialize(d)?.unwrap_or_default())
}

fn null_true<'de, D>(d: D) -> Result<bool, D::Error>
where
    D: Deserializer<'de>,
{
    Ok(Option::<bool>::deserialize(d)?.unwrap_or(true))
}

/// Números que pueden venir como string (`"19641.26"`) o `null`.
fn lenient_decimal<'de, D>(d: D) -> Result<Decimal, D::Error>
where
    D: Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Raw {
        Num(Decimal),
        Str(String),
    }
    match Option::<Raw>::deserialize(d)? {
        None => Ok(Decimal::ZERO),
        Some(Raw::Num(n)) => Ok(n),
        Some(Raw::Str(s)) => {
            // "1.234,50" (miles con punto, decimal con coma) o "1234.50".
            let s = s.trim();
            let s = if s.contains(',') { s.replace('.', "").replace(',', ".") } else { s.to_string() };
            if s.is_empty() {
                Ok(Decimal::ZERO)
            } else {
                s.parse().map_err(serde::de::Error::custom)
            }
        }
    }
}

fn lenient_f64<'de, D>(d: D) -> Result<f64, D::Error>
where
    D: Deserializer<'de>,
{
    let dec = lenient_decimal(d)?;
    Ok(dec.try_into().unwrap_or(0.0))
}

/// Un solo string o una lista; `null` → vacía.
fn string_list<'de, D>(d: D) -> Result<Vec<String>, D::Error>
where
    D: Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Raw {
        One(String),
        Many(Vec<Option<String>>),
    }
    Ok(match Option::<Raw>::deserialize(d)? {
        None => Vec::new(),
        Some(Raw::One(s)) => vec![s],
        Some(Raw::Many(v)) => v.into_iter().flatten().collect(),
    })
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ProductoDTO {
    pub id_producto: i64,
    #[serde(default, deserialize_with = "null_default")]
    pub troquel: i64,
    #[serde(default, deserialize_with = "string_list")]
    pub codigo_barras: Vec<String>,
    #[serde(default, deserialize_with = "null_default")]
    pub descripcion: String,
    #[serde(default, deserialize_with = "lenient_f64")]
    pub stock_sucursal: f64,
    #[serde(default, deserialize_with = "lenient_decimal")]
    pub precio: Decimal,
    #[serde(default, deserialize_with = "null_default")]
    pub categoria: String,
    #[serde(default, deserialize_with = "null_default")]
    pub rubro: String,
    #[serde(default, deserialize_with = "null_default")]
    pub subrubro: String,
    #[serde(default)]
    pub forma_farmaceutica: Option<String>,
    #[serde(default, deserialize_with = "string_list")]
    pub acciones_terapeuticas: Vec<String>,
    #[serde(default)]
    pub laboratorio: Option<String>,
    #[serde(default)]
    pub nombres_drogas: Option<String>,
    /// Condiciones de pago. Se parsean pero no se hashean ni se envían (spec §1).
    #[serde(default, deserialize_with = "null_default")]
    pub ofertas: Vec<Oferta>,
    #[serde(default = "default_true", deserialize_with = "null_true")]
    pub es_visible_en_venta: bool,
    /// Cantidad de productos que comparten el CB. `null` en consultas por ID.
    #[serde(default, rename = "visiblesMismoCB")]
    pub visibles_mismo_cb: Option<i32>,
    #[serde(default, rename = "Baja", deserialize_with = "null_default")]
    pub baja: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Oferta {
    #[serde(default, rename = "his_IdCondicionComercial", deserialize_with = "null_default")]
    pub his_id_condicion_comercial: i64,
    #[serde(default, deserialize_with = "lenient_decimal")]
    pub porcentaje: Decimal,
    #[serde(default, deserialize_with = "null_default")]
    pub descripcion: String,
}

/// Respuesta de `GET /api/productos/lote/{n}`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct LoteResponse {
    #[serde(default, deserialize_with = "null_default")]
    pub cantidad_lotes: u32,
    #[serde(default, deserialize_with = "null_default")]
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
    fn tolerates_null_in_every_field_and_string_numbers() {
        let json = r#"{
          "idProducto": 9, "troquel": null, "codigoBarras": null, "descripcion": null,
          "stockSucursal": "3,0", "precio": "1.234,50", "categoria": null, "rubro": null,
          "subrubro": null, "formaFarmaceutica": null, "accionesTerapeuticas": null,
          "laboratorio": null, "nombresDrogas": null, "ofertas": null,
          "esVisibleEnVenta": null, "visiblesMismoCB": null, "Baja": null }"#;
        let p: ProductoDTO = serde_json::from_str(json).unwrap();
        assert_eq!(p.troquel, 0);
        assert!(p.codigo_barras.is_empty());
        assert_eq!(p.descripcion, "");
        assert_eq!(p.stock_sucursal, 3.0);
        assert_eq!(p.precio.to_string(), "1234.50");
        assert!(p.es_visible_en_venta);
        assert!(!p.baja);
        assert!(p.ofertas.is_empty());

        // CB como string suelto y lista con nulls; ofertas con porcentaje null.
        let json = r#"{"idProducto": 10, "codigoBarras": "779", "accionesTerapeuticas": ["A", null],
                       "ofertas": [{"his_IdCondicionComercial": null, "porcentaje": null, "descripcion": null}]}"#;
        let p: ProductoDTO = serde_json::from_str(json).unwrap();
        assert_eq!(p.codigo_barras, vec!["779"]);
        assert_eq!(p.acciones_terapeuticas, vec!["A"]);
        assert_eq!(p.ofertas[0].porcentaje, Decimal::ZERO);

        let l: LoteResponse = serde_json::from_str(r#"{"cantidadLotes": null, "productos": null}"#).unwrap();
        assert_eq!(l.cantidad_lotes, 0);
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
