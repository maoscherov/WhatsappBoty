"""
Carga la base de conocimiento de Mutual AMI (vertical "mutual").

Contenido tomado de `Modelo_Conversacional_CERCA_Especificacion_Developer.docx`,
sección 2. Cada bloque entra como un documento independiente para que la
búsqueda semántica devuelva sólo lo relevante a cada consulta.

Uso (con DATABASE_URL y OPENAI_API_KEY configuradas):
    python -m scripts.cargar_kb_mutual              # carga los que falten
    python -m scripts.cargar_kb_mutual --reemplazar # borra los existentes y recarga
"""

import asyncio
import sys

from app.config import get_settings
from app.services.db import get_db
from app.services.embeddings import get_embedding_service
from app.services.rag_service import get_rag_service

# Los importes y tasas cambian: revisar contra la fuente antes de cada carga.
DOCUMENTOS: list[tuple[str, str]] = [
    ("Horarios de atención",
     "Caja: lunes a viernes de 7:30 a 13:00. "
     "Administración: lunes a viernes de 7:30 a 15:00, sábados de 8 a 12. "
     "Farmacia: lunes a viernes de 7:30 a 15:00, sábados de 8 a 12."),

    ("Datos generales y alta de socio",
     "Alias para transferencias: AMICORREA. "
     "Cuota social: $4.000. "
     "Las cuotas de préstamos vencen hasta el día 10 inclusive de cada mes. "
     "Para asociarse hace falta fotocopia de DNI."),

    ("Requisitos para préstamos personales",
     "Se necesita: DNI; los 3 últimos recibos de sueldo, pagos de monotributo o DDJJ de IVA; "
     "un impuesto o servicio a nombre del titular; situación Normal en el sistema financiero (BCRA); "
     "un año de antigüedad laboral o de aportes como mínimo; y 1 o 2 garantías que cumplan los mismos "
     "requisitos que el titular. "
     "Los préstamos son personales: se otorgan a solicitante y garante, y ambos deben acreditar "
     "actividad e ingresos. El monto se evalúa según el ingreso y la cuota resultante."),

    ("Condiciones de préstamos: tasas, montos y plazos",
     "Tasa preferencial: 55% TNA, montos de $1.500.000 a $6.000.000, hasta 12 cuotas. "
     "Tasa público general: 75% TNA, hasta 36 cuotas. "
     "La primera cuota puede variar según la fecha de liquidación: a partir del día 18, el vencimiento "
     "de la primera cuota pasa al mes subsiguiente, por lo que su importe es más alto que el resto. "
     "El importe final de la cuota lo calcula el equipo al evaluar la solicitud."),

    ("Ahorro Mutual a Término (AMT)",
     "Tasa presencial: 23,5% TNA. Tasa online: 26% TNA. "
     "Plazo: de 29 a 60 días, siempre finalizando en día hábil. "
     "A 29 días se reduce a la mitad el gasto de sellado."),

    ("Beneficios para socios",
     "Caja de ahorro en pesos y en dólares sin costo de mantenimiento. "
     "Ahorro a término (AMT). Ayudas económicas para proyectos, hasta 36 cuotas fijas en pesos. "
     "Gestión de valores. 15% de descuento en la Farmacia Mutual. Cobro de impuestos y servicios. "
     "Alojamiento en Apart Hotel en Buenos Aires, en zona céntrica, para hasta 5 personas, con "
     "estacionamiento, wifi, limpieza y desayunador. Préstamo de elementos ortopédicos. "
     "Venta de artículos de hogar y electro con financiación propia. "
     "10% de descuento en La Esquina de Tu Mutual (Carcarañá) en librería y bazar. "
     "Turismo con destinos nacionales e internacionales."),

    ("Qué resuelve una persona del equipo",
     "Las consultas sobre datos de cuentas las atiende una persona, no el asistente: "
     "envío de comprobantes de transferencia, solicitudes de transferencia, renovación de plazos fijos, "
     "valor de cuotas de préstamos, saldo de caja de ahorro y vencimientos de plazos fijos."),
]


async def main(reemplazar: bool = False) -> int:
    settings = get_settings()
    db = get_db(settings.database_url)
    if not await db.connect():
        print("✗ No se pudo conectar a Postgres. Configurá DATABASE_URL.")
        return 1

    rag = get_rag_service(db, get_embedding_service(settings.openai_api_key))
    if not rag.enabled():
        print("✗ RAG deshabilitado: falta OPENAI_API_KEY para generar los embeddings.")
        return 1

    existentes = {d["titulo"] for d in await rag.kb_list()}
    if reemplazar and existentes:
        for doc in await rag.kb_list():
            await rag.kb_delete(doc["id"])
        print(f"• {len(existentes)} documentos anteriores eliminados")
        existentes = set()

    nuevos = 0
    for titulo, contenido in DOCUMENTOS:
        if titulo in existentes:
            print(f"– ya estaba: {titulo}")
            continue
        if await rag.kb_add(titulo, contenido):
            nuevos += 1
            print(f"✓ {titulo}")
        else:
            print(f"✗ falló: {titulo}")

    print(f"\nListo: {nuevos} documentos nuevos, {len(await rag.kb_list())} en total.")
    await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--reemplazar" in sys.argv)))
