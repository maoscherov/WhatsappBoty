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


def cotizar_receta(precio_base: float, pct_os: float = 0,
                   es_socio: bool = False, pct_socio: float = 0) -> dict:
    """
    Cotización de una receta desde el backoffice: precio que carga el
    operador, menos el % que reconoce la obra social, menos el % de socio si
    el teléfono está en el padrón (en ese orden — decisión 4/9).

    Devuelve precio_lista/precio_final y el `desglose` ya redactado: el texto
    con los números lo arma el código, nunca una persona ni el modelo, para
    que el importe del mensaje sea siempre el que se cobra.
    """
    pct_os = max(0.0, float(pct_os or 0))
    pct_socio_aplicado = max(0.0, float(pct_socio or 0)) if es_socio else 0.0

    precio = float(precio_base)
    if pct_os:
        precio = precio * (1 - pct_os / 100)
    if pct_socio_aplicado:
        precio = precio * (1 - pct_socio_aplicado / 100)
    precio_final = round(precio, 2)

    lista = f"${precio_base:,.2f}"
    final = f"${precio_final:,.2f}"
    if pct_os and pct_socio_aplicado:
        desglose = (f"Sale {lista}, tu obra social te reconoce el {pct_os:g}% y por "
                    f"ser socio tenés un {pct_socio_aplicado:g}% adicional: te queda "
                    f"en {final}.")
    elif pct_os:
        desglose = (f"Sale {lista} y tu obra social te reconoce el {pct_os:g}%: "
                    f"te queda en {final}.")
    elif pct_socio_aplicado:
        # Sin % de OS cargado, el precio que puso el operador ya se presume
        # con la cobertura aplicada (pedido 5/9): se dice explícitamente.
        desglose = (f"Sale por obra social {lista} y por ser socio tenés un "
                    f"{pct_socio_aplicado:g}% de descuento: te queda en {final}.")
    else:
        desglose = f"Sale por obra social {final}."

    return {"precio_lista": round(float(precio_base), 2), "pct_os": pct_os,
            "pct_socio_aplicado": pct_socio_aplicado,
            "precio_final": precio_final, "desglose": desglose}


def _partir(valor: str) -> list[str]:
    """'Ibuprofeno 600, Omeprazol 20' → ['Ibuprofeno 600', 'Omeprazol 20']."""
    return [p.strip() for p in (valor or "").split(",") if p.strip()]


def armar_receta_info(ocr: dict, sku_svc, socio_svc, phone: str) -> dict:
    """
    Devuelve el paquete completo para el operador:
      ocr                 → los campos leídos de la receta
      candidatos_catalogo → top 3 del catálogo POR CADA medicamento de la
                            receta (por marca sugerida, o por droga si no hay
                            marca); cada candidato trae en `consulta` el
                            medicamento que lo trajo
      socio_por_dni       → socio del padrón con el DNI de la RECETA (puede
                            ser otra persona que quien escribe)
      socio_por_telefono  → socio del padrón con el teléfono que la mandó
      dni_coincide_padron → True si el DNI de la receta está en el padrón

    Una receta puede traer varios medicamentos separados por coma: buscar la
    cadena entera junta devolvía candidatos malos, así que se busca cada uno
    por separado (marca y droga apareadas por posición).
    """
    productos = _partir(ocr.get("producto_sugerido"))
    drogas = _partir(ocr.get("droga"))
    candidatos: list[dict] = []
    for i in range(max(len(productos), len(drogas))):
        consultas = (productos[i] if i < len(productos) else "",
                     drogas[i] if i < len(drogas) else "")
        for consulta in consultas:
            if not consulta:
                continue
            try:
                resultados = sku_svc.buscar(consulta, top_n=3)
            except Exception as e:
                logger.warning(f"receta_ocr: búsqueda de {consulta!r} falló: {e}")
                resultados = []
            if resultados:
                candidatos.extend({
                    "sku_id": r.get("sku_id"), "nombre": r.get("nombre"),
                    "precio": r.get("precio"), "estado": r.get("estado"),
                    "requiere_receta": r.get("requiere_receta"),
                    "consulta": consulta,
                } for r in resultados)
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
