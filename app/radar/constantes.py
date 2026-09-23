"""Constantes de Radar compartidas por la app, las migraciones y los tests."""

import uuid

# Tenant fijo de Keep IT Simple: ahí viven los admins de KIS (rol `admin`).
# Lo siembra la migración r0001 antes de activar RLS sobre `tenants`.
TENANT_KIS_STR = "00000000-0000-0000-0000-000000000001"
TENANT_KIS = uuid.UUID(TENANT_KIS_STR)
