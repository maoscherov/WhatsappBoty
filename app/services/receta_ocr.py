"""
Cruce de la receta leída por OCR contra el catálogo y el padrón.

Cuando una receta llega por foto y receta_ocr_enabled está activo, además de
derivar a una persona (esa regla no cambia: el bot nunca vende con receta),
se arma un paquete de información para que el operador tenga todo en el
backoffice sin pedirle nada al cliente: qué medicamento es, qué candidato del
catálogo matchea (con precio y stock) y si el paciente es socio.

Privacidad: este paquete viaja a la sesión y al backoffice. Nunca entra al
prompt del modelo conversacional — misma política que el DNI del padrón.
"""

import logging

logger = logging.getLogger(__name__)


def armar_receta_info(ocr: dict, sku_svc, socio_svc, phone: str) -> dict:
    """
    Devuelve el paquete completo para el operador:
      ocr                 → los campos leídos de la receta
      candidatos_catalogo → top 3 del catálogo (por producto sugerido, o por
                            droga si no hay marca)
      socio_por_dni       → socio del padrón con el DNI de la RECETA (puede
                            ser otra persona que quien escribe)
      socio_por_telefono  → socio del padrón con el teléfono que la mandó
      dni_coincide_padron → True si el DNI de la receta está en el padrón
    """
    candidatos: list[dict] = []
    for consulta in (ocr.get("producto_sugerido"), ocr.get("droga")):
        if not consulta:
            continue
        try:
            resultados = sku_svc.buscar(consulta, top_n=3)
        except Exception as e:
            logger.warning(f"receta_ocr: búsqueda de {consulta!r} falló: {e}")
            resultados = []
        if resultados:
            candidatos = [{
                "sku_id": r.get("sku_id"), "nombre": r.get("nombre"),
                "precio": r.get("precio"), "estado": r.get("estado"),
                "requiere_receta": r.get("requiere_receta"),
            } for r in resultados]
            break

    socio_dni = None
    socio_tel = None
    try:
        socio_dni = socio_svc.find_by_dni(ocr.get("dni") or "")
        socio_tel = socio_svc.find_by_phone(phone)
    except Exception as e:
        logger.warning(f"receta_ocr: cruce de padrón falló: {e}")

    def _publico(s):
        if not s:
            return None
        return {"nombre": f"{s.get('nombre', '')} {s.get('apellido', '')}".strip(),
                "nro_socio": s.get("nro_socio", "")}

    return {
        "ocr": ocr,
        "candidatos_catalogo": candidatos,
        "socio_por_dni": _publico(socio_dni),
        "socio_por_telefono": _publico(socio_tel),
        "dni_coincide_padron": socio_dni is not None,
    }
