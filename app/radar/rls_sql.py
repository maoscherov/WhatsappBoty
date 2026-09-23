"""
SQL de RLS efectiva (§6.4), compartido por las migraciones de Radar.

Los nombres de tabla y función vienen de las migraciones (constantes del
repo), nunca de entrada de usuario: por eso acá sí se interpolan.
"""


def politica_por_tenant(tabla: str, columna: str = "tenant_id") -> str:
    """ENABLE + FORCE RLS y la política de radar_app sobre `columna`.

    radar_tenant_actual() es NULL cuando app.tenant_id no está fijada o está
    vacía, y `columna = NULL` nunca es true: sin tenant hay cero filas, tanto
    para leer (USING) como para escribir (WITH CHECK).
    """
    return f"""
        ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {tabla}_por_tenant ON {tabla} FOR ALL TO radar_app
            USING ({columna} = radar_tenant_actual())
            WITH CHECK ({columna} = radar_tenant_actual());
    """


def grants_app(tabla: str, privilegios: str) -> str:
    return f"GRANT {privilegios} ON {tabla} TO radar_app;"


def definir_funcion_admin(firma: str, create_sql: str) -> str:
    """Crea una función SECURITY DEFINER propiedad de radar_admin, ejecutable
    solo por radar_app. Cambiar el dueño exige que radar_admin tenga CREATE en
    el esquema; se le da y se le quita en el mismo paso."""
    return f"""
        GRANT CREATE ON SCHEMA public TO radar_admin;
        {create_sql}
        ALTER FUNCTION {firma} OWNER TO radar_admin;
        REVOKE ALL ON FUNCTION {firma} FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION {firma} TO radar_app;
        REVOKE CREATE ON SCHEMA public FROM radar_admin;
    """
