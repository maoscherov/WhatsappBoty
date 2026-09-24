"""Constantes de Radar compartidas por la app, las migraciones y los tests."""

import uuid

# Tenant fijo de Keep IT Simple: ahí viven los admins de KIS (rol `admin`).
# Lo siembra la migración r0001 antes de activar RLS sobre `tenants`.
TENANT_KIS_STR = "00000000-0000-0000-0000-000000000001"
TENANT_KIS = uuid.UUID(TENANT_KIS_STR)

# Estados de un vínculo (links.estado) y causas del job de fin de vínculo (§2.2, §6.3 punto 6).
ESTADOS_LINK = ("creando", "esperando_qr", "vinculado", "caido", "cerrando", "cerrado", "abortado")
CAUSAS_FIN = ("duracion", "pedido_dueno", "pedido_kis", "caida_72h", "migracion_api", "baja",
              "reemplazado", "qr_abandonado")
