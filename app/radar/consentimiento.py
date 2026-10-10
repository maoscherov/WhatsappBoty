"""
Consentimiento por línea (§3 P2): versión del texto, hash, fecha, IP y
opciones elegidas. Los textos viven acá, versionados; se guarda sha256 del
texto para poder probar después exactamente qué se aceptó. Ningún texto de
consentimiento lleva datos de conversación.
"""

import hashlib
import json
import uuid
from typing import Optional

import asyncpg

TEXTO_V1 = """Qué hacemos: leemos los mensajes de tus chats individuales para calcular métricas y detectar patrones.

Qué no hacemos: no enviamos mensajes, no marcamos chats como leídos, no aparecemos "en línea", no leemos grupos, estados ni canales, no descargamos fotos, audios ni documentos.

Importante: mientras tu línea esté conectada, WhatsApp le entrega a nuestro servidor de conexión una copia técnica de todos tus chats individuales, incluidos los personales, y la sigue actualizando con cada mensaje nuevo. No podemos filtrarla chat por chat. Un proceso automático la recorre una vez, al conectar, para contar mensajes y sugerirte qué excluir; no guarda el texto de los chats que excluyas, y ninguna persona los ve. Después de eso solo leemos los chats que elegiste. La copia se borra completa cuando desconectás desde Radar. Si quitás el dispositivo desde tu teléfono, se borra dentro de las 72 horas.

Cuánto dura: la conexión no vence sola: queda activa hasta que la desconectes. Conservamos el texto de los chats que elegiste mientras tu línea siga dada de alta en Keep IT Simple, aunque la desconectes de WhatsApp. Para borrarlo usá "Desconectar y borrar todo".

Quién procesa y dónde: Keep IT Simple. Los datos se alojan en Railway (EE. UU.). Para el análisis se transfieren a Anthropic y, para agrupar preguntas, a OpenAI (ambos en EE. UU.). Antes de enviar nada reemplazamos teléfonos y nombres de contacto por códigos y tachamos DNI, tarjetas, CBU y direcciones que detectamos. El texto de los mensajes sí se envía: puede incluir datos que tus clientes escribieron y no garantizamos detectarlos todos. Anthropic y OpenAI pueden conservar hasta 30 días lo que les enviamos, por seguridad y prevención de abuso. No lo usan para entrenar sus modelos.

Para qué usamos el resultado: para mostrarte este diagnóstico y configurar el servicio que contrataste. No usamos tus conversaciones para ningún otro fin ni las cruzamos con otros clientes.

Riesgo que tenés que conocer: la conexión usa "dispositivos vinculados" mediante un cliente no oficial, que WhatsApp no avala. No enviamos mensajes ni hacemos las acciones de envío que se conocen como causa de bloqueo. Aun así, nadie garantiza que un vínculo de solo lectura sea seguro: el riesgo existe y no lo podemos cuantificar. Mientras dure la conexión el riesgo es continuo, no de una sola vez. La conexión ocupa uno de tus dispositivos vinculados, y si el teléfono pasa unos 14 días sin internet, WhatsApp la corta.

Soy titular o responsable de esta línea y confirmo el acuerdo de tratamiento de datos de mi contrato.
"""

VERSIONES: dict[str, str] = {"v1": TEXTO_V1}


def hash_texto(version: str) -> str:
    return hashlib.sha256(VERSIONES[version].encode("utf-8")).hexdigest()


async def registrar_consentimiento(con: asyncpg.Connection, *, tenant_id: uuid.UUID, line_id: uuid.UUID,
                                   user_id: uuid.UUID, version: str, opciones: dict, ip: Optional[str]) -> uuid.UUID:
    return await con.fetchval(
        "INSERT INTO consents (tenant_id, line_id, user_id, version_texto, hash_texto, opciones, ip) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) RETURNING id",
        tenant_id, line_id, user_id, version, hash_texto(version), json.dumps(opciones, default=str), ip)


async def listar_consentimientos(con: asyncpg.Connection, line_id: uuid.UUID) -> list[dict]:
    filas = await con.fetch(
        "SELECT id, version_texto, hash_texto, user_id, created_at, opciones FROM consents "
        "WHERE line_id = $1 ORDER BY created_at DESC", line_id)
    return [{"id": str(f["id"]), "version_texto": f["version_texto"], "hash_texto": f["hash_texto"],
             "user_id": str(f["user_id"]), "created_at": f["created_at"].isoformat(),
             "opciones": json.loads(f["opciones"])} for f in filas]
