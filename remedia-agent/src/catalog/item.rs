//! `CatalogItem`: lo que viaja a Remedia. Normalización del DTO del ERP y hash blake3.

use crate::erp::model::ProductoDTO;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CatalogItem {
    /// `idProducto` del ERP. Clave primaria del catálogo.
    pub external_id: String,
    /// blake3 (hex) de todos los demás campos. No incluye ofertas.
    pub hash: String,
    /// Ordenados y sin duplicados, para que el hash sea estable.
    pub barcodes: Vec<String>,
    /// `None` si el ERP trae 0 (no-medicamentos).
    pub troquel: Option<i64>,
    /// `descripcion` con espacios colapsados y recortados.
    pub name: String,
    /// `laboratorio`.
    pub brand: Option<String>,
    /// `nombresDrogas` si no está vacío.
    pub drug: Option<String>,
    /// `formaFarmaceutica`.
    pub form: Option<String>,
    pub category: String,
    pub rubro: String,
    pub subrubro: String,
    pub therapeutic_actions: Vec<String>,
    /// `None` si el ERP trae 0: significa "sin precio", no gratis. 2 decimales.
    pub price: Option<Decimal>,
    pub stock: i32,
    /// `esVisibleEnVenta`.
    pub visible: bool,
    /// `!Baja`.
    pub active: bool,
}

/// Colapsa espacios múltiples a uno y recorta extremos.
pub fn normalize_name(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn non_empty(s: Option<&String>) -> Option<String> {
    let v = normalize_name(s.map(String::as_str).unwrap_or(""));
    if v.is_empty() {
        None
    } else {
        Some(v)
    }
}

/// Campos que participan del hash, en orden fijo. Excluye `hash` y ofertas.
#[derive(Serialize)]
struct HashInput<'a> {
    external_id: &'a str,
    barcodes: &'a [String],
    troquel: Option<i64>,
    name: &'a str,
    brand: Option<&'a str>,
    drug: Option<&'a str>,
    form: Option<&'a str>,
    category: &'a str,
    rubro: &'a str,
    subrubro: &'a str,
    therapeutic_actions: &'a [String],
    price: Option<String>,
    stock: i32,
    visible: bool,
    active: bool,
}

impl CatalogItem {
    pub fn from_dto(dto: &ProductoDTO) -> CatalogItem {
        let mut barcodes: Vec<String> = dto
            .codigo_barras
            .iter()
            .map(|b| b.trim().to_string())
            .filter(|b| !b.is_empty())
            .collect();
        barcodes.sort_unstable();
        barcodes.dedup();

        let price = if dto.precio.is_zero() || dto.precio.is_sign_negative() {
            None
        } else {
            Some(dto.precio.round_dp(2).normalize())
        };

        let mut item = CatalogItem {
            external_id: dto.id_producto.to_string(),
            hash: String::new(),
            barcodes,
            troquel: (dto.troquel != 0).then_some(dto.troquel),
            name: normalize_name(&dto.descripcion),
            brand: non_empty(dto.laboratorio.as_ref()),
            drug: non_empty(dto.nombres_drogas.as_ref()),
            form: non_empty(dto.forma_farmaceutica.as_ref()),
            category: normalize_name(&dto.categoria),
            rubro: normalize_name(&dto.rubro),
            subrubro: normalize_name(&dto.subrubro),
            therapeutic_actions: dto
                .acciones_terapeuticas
                .iter()
                .map(|a| normalize_name(a))
                .filter(|a| !a.is_empty())
                .collect(),
            price,
            stock: dto.stock_sucursal.round() as i32,
            visible: dto.es_visible_en_venta,
            active: !dto.baja,
        };
        item.hash = item.compute_hash();
        item
    }

    /// blake3 hex de la serialización JSON canónica de `HashInput`.
    pub fn compute_hash(&self) -> String {
        let input = HashInput {
            external_id: &self.external_id,
            barcodes: &self.barcodes,
            troquel: self.troquel,
            name: &self.name,
            brand: self.brand.as_deref(),
            drug: self.drug.as_deref(),
            form: self.form.as_deref(),
            category: &self.category,
            rubro: &self.rubro,
            subrubro: &self.subrubro,
            therapeutic_actions: &self.therapeutic_actions,
            price: self.price.map(|p| p.round_dp(2).to_string()),
            stock: self.stock,
            visible: self.visible,
            active: self.active,
        };
        let bytes = serde_json::to_vec(&input).expect("HashInput siempre serializa");
        blake3::hash(&bytes).to_hex().to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    fn dto() -> ProductoDTO {
        serde_json::from_str(
            r#"{
          "idProducto": 7454, "troquel": 4479051, "codigoBarras": ["7795336085205","1111"],
          "descripcion": "CLARITROMICINA RICHET 500 mg COM x    8", "stockSucursal": 0,
          "precio": 19641.2600, "categoria": "Medicamentos", "rubro": "Medicamentos",
          "subrubro": "Medicamentos", "formaFarmaceutica": "Comprimidos",
          "accionesTerapeuticas": ["Antibiótico"], "laboratorio": "Richet",
          "nombresDrogas": "Claritromicina",
          "ofertas": [{"his_IdCondicionComercial": 11, "porcentaje": -3.1, "descripcion": "-3,10%"}],
          "esVisibleEnVenta": true, "visiblesMismoCB": 1, "Baja": false }"#,
        )
        .unwrap()
    }

    #[test]
    fn collapses_spaces() {
        assert_eq!(normalize_name("  COM x    8 "), "COM x 8");
        assert_eq!(normalize_name("a\t\tb\n c"), "a b c");
    }

    #[test]
    fn hash_stable_on_barcode_reorder_and_duplicates() {
        let a = CatalogItem::from_dto(&dto());
        let mut d = dto();
        d.codigo_barras.reverse();
        d.codigo_barras.push("1111".into());
        let b = CatalogItem::from_dto(&d);
        assert_eq!(a.hash, b.hash);
        assert_eq!(a.barcodes, vec!["1111", "7795336085205"]);
        assert_eq!(a.hash.len(), 64);
    }

    #[test]
    fn price_zero_is_none_and_troquel_zero_is_none() {
        let mut d = dto();
        d.precio = Decimal::ZERO;
        d.troquel = 0;
        let it = CatalogItem::from_dto(&d);
        assert!(it.price.is_none());
        assert!(it.troquel.is_none());
    }

    #[test]
    fn ofertas_do_not_affect_hash() {
        let a = CatalogItem::from_dto(&dto());
        let mut d = dto();
        d.ofertas.clear();
        assert_eq!(a.hash, CatalogItem::from_dto(&d).hash);
    }

    #[test]
    fn stock_and_price_changes_change_hash() {
        let a = CatalogItem::from_dto(&dto());
        let mut d = dto();
        d.stock_sucursal = 3.0;
        let b = CatalogItem::from_dto(&d);
        assert_ne!(a.hash, b.hash);
        assert_eq!(b.stock, 3);
        let mut d = dto();
        d.precio = Decimal::from_str("19641.27").unwrap();
        assert_ne!(a.hash, CatalogItem::from_dto(&d).hash);
    }

    #[test]
    fn price_has_two_decimals_and_name_normalized() {
        let it = CatalogItem::from_dto(&dto());
        assert_eq!(it.price.unwrap().to_string(), "19641.26");
        assert_eq!(it.name, "CLARITROMICINA RICHET 500 mg COM x 8");
        assert_eq!(it.drug.as_deref(), Some("Claritromicina"));
        assert_eq!(it.brand.as_deref(), Some("Richet"));
        assert_eq!(it.external_id, "7454");
        assert_eq!(it.troquel, Some(4479051));
        assert!(it.active && it.visible);
    }

    #[test]
    fn empty_drug_and_brand_are_none() {
        let mut d = dto();
        d.nombres_drogas = Some("  ".into());
        d.laboratorio = None;
        let it = CatalogItem::from_dto(&d);
        assert!(it.drug.is_none());
        assert!(it.brand.is_none());
    }

    #[test]
    fn hash_recomputed_matches_stored() {
        let it = CatalogItem::from_dto(&dto());
        assert_eq!(it.hash, it.compute_hash());
    }

    #[test]
    fn serializes_price_as_string_and_omits_nothing() {
        let it = CatalogItem::from_dto(&dto());
        let v: serde_json::Value = serde_json::to_value(&it).unwrap();
        assert_eq!(v["price"], serde_json::json!("19641.26"));
        assert_eq!(v["external_id"], serde_json::json!("7454"));
        assert!(v.get("ofertas").is_none());
    }
}
