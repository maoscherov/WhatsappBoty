"""catalogo_diccionario: abreviaturas y sinónimos para reconocer los productos

El sistema de la farmacia abrevia los nombres ("ESTRELLA HIS POT x 125" son
hisopos) y el cliente pide con sus palabras ("cotonetes", "suero"). Hasta
ahora las equivalencias vivían en listas fijas del código; acá pasan a
Postgres, editables desde el backoffice sin deploy (2/10).

  tipo = abreviatura → termino es la sigla del catálogo ("his"), equivale la
         palabra que escribe el cliente ("hisopos"). Se agrega al índice.
  tipo = sinonimo    → termino es lo que escribe el cliente ("cotonetes"),
         equivale los términos del catálogo a buscar ("hisopos").
  estado: activa (se usa) | propuesta (a validar) | descartada (no se usa,
         aunque esté en la lista base del código).

Se siembra con lo que ya usaba el bot (activa) y con siglas encontradas en
los nombres de los productos con stock (propuesta, con ejemplos en la nota).

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# Lo que el bot ya usaba (catalogo_enriquecido.ABREVIATURAS / sku_service.SINONIMOS).
_BASE_ABREV = {
    "sha": "shampoo", "shamp": "shampoo", "aco": "acondicionador", "acond": "acondicionador",
    "jab": "jabon", "jli": "jabon liquido", "des": "desodorante", "deo": "desodorante",
    "tal": "talco", "cre": "crema", "cr": "crema", "loc": "locion", "gel": "gel",
    "spr": "spray", "aer": "aerosol", "toa": "toallitas", "emu": "emulsion", "esp": "espuma",
    "ser": "serum", "apo": "apositos", "pol": "polvo", "pmo": "pomo", "com": "comprimidos",
    "cap": "capsulas", "caps": "capsulas", "tab": "tabletas", "gts": "gotas", "got": "gotas",
    "jbe": "jarabe", "sol": "solucion", "sus": "suspension", "susp": "suspension",
    "sob": "sobres", "amp": "ampollas", "ovu": "ovulos", "fco": "frasco", "ung": "unguento",
    "past": "pastillas", "grag": "grageas", "iny": "inyectable",
}
_BASE_SINON = {
    "protector": "solaire, solar, fps",
    "ibuprofeno": "ibupirac, actron",
    "omeprazol": "aziatop",
    "escopolamina": "buscapina, sertal",
    "butilhioscina": "buscapina, sertal",
    "hioscina": "buscapina, sertal",
    "diclofenac": "voltaren",
    "aspirina": "bayaspirina, aspirineta",
    "acido acetilsalicilico": "aspirina, bayaspirina",
    "amoxicilina": "amoxidal",
    "paracetamol": "tafirol",
}
# Siglas encontradas en los nombres (productos con stock, 2/10): a validar.
_PROPUESTAS_ABREV = [
    ("his", "hisopos", "ESTRELLA HIS POT x 125; JOHNSON COTONETES HIS CAJ x 75"),
    ("edt", "perfume", "eau de toilette — NASA JEANS INDIGO EDT C-VAP (134 productos)"),
    ("edp", "perfume", "eau de parfum — CHER 20 EDP; SHAKIRA ROJO ELIXIR EDP (83)"),
    ("cep", "cepillo", "ORAL B CEP SUAVE; COLGATE CEP DENTAL KIDS (64)"),
    ("esm", "esmalte", "MYMB COLORAMA CREMOSO ROSA ESM; CUTEX QUI ESM (58)"),
    ("tin", "tintura", "NUTRISSE ULTRA 60 RUBIO OSCURO TIN (40)"),
    ("maq", "maquina", "GILLETTE VENUS BREEZE CART MAQ AFE (29)"),
    ("afe", "afeitar", "GILLETTE MACH3 HOJ AFE; MINORA MAQ AFE (20)"),
    ("sch", "sachet", "Sedal shampoo/acondicionador SCH x 10 (27)"),
    ("rep", "repuesto", "VIDOL TALCO FLORAL REP; PALMOLIVE 200ML REP JLI (26)"),
    ("liq", "liquido", "vaselina liq x 125 ml; COLGATE ENJ BUC PLAX LIQ (25)"),
    ("hig", "higienica", "KOTEX NORMAL C/ALAS HIG TOA (toallas higiénicas, 23)"),
    ("gde", "grande", "NONISEC PAÑAL ANAT ULTRA GDE (22)"),
    ("vap", "vaporizador", "DANIELLE GIRL EDT C-VAP (21)"),
    ("prot", "protector", "CAREFREE PROT PROTECCION S-PERF (protector diario, 21)"),
    ("den", "dental", "COLGATE TOTAL 12 DEN CRE (crema dental, 67)"),
    ("dent", "dental", "COLGATE DENT KIDS FRUTILLA GEL (7)"),
    ("tra", "tratamiento", "CAPILATIS ORTIGA TRA CAP SHA (tratamiento capilar, 21)"),
    ("amb", "ampollas bebibles", "ENTEROGERMINA PLUS 5 ml AMB x 5 (20)"),
    ("pda", "pomada", "HIPOGLOS PDA x 100; LENEMICINA PDA (14)"),
    ("pañ", "pañales", "AMBIGUA: PLENITUD PAÑ ADU son pañales, pero SNIFF TISSUE PAÑ DES "
                       "son pañuelos descartables — revisar"),
    ("pot", "pote", "ESTRELLA HIS POT x 125 (8)"),
    ("caj", "caja", "GASA HIDROGASA CAJ x 32 (15)"),
    ("toall", "toallas", "CALIPSO TOALL FEM NORMAL (3)"),
    ("apos", "apositos", "NONISEC APOS P-AD INCONT (2)"),
    ("bol", "bolsa", "vidol fecula bebe BOL x 200 (3)"),
    ("inf", "infantil", "SILFAB GEL INF MULT DINO (2)"),
    ("fem", "femenino", "IBUPIRAC FEM; REXONA ODORONO FEM (64)"),
    ("men", "hombre", "DOVE DEO MEN AER; NIVEA MEN (66)"),
    ("col", "colirio", "AMBIGUA: REFENAX COL / IRIX LAGRIMAS COL son colirios, pero "
                       "JOHNSON BABY COL ENV es colonia — revisar"),
    ("masc", "masculino", "AMBIGUA: PACO MASC LOC es perfume masculino, pero 'mymb great "
                          "lash masc pestañas' es máscara — revisar"),
]
_PROPUESTAS_SINON = [
    ("cotonetes", "hisopos", "como piden los hisopos"),
    ("palitos", "hisopos", "'palitos para los oídos'"),
    ("suero fisiologico", "solucion fisiologica", "caso 28/9 y 1/10: 'suero fisiológico' sin resultado"),
    ("forros", "preservativos", "coloquial"),
    ("condones", "preservativos", "coloquial"),
    ("pasta de dientes", "dental", "el catálogo dice DEN CRE"),
    ("pasta dental", "dental", "el catálogo dice DEN CRE"),
    ("toallitas femeninas", "higienica", "toallas higiénicas: HIG TOA"),
    ("toallas femeninas", "higienica", "toallas higiénicas: HIG TOA"),
    ("protector diario", "prot, protector", "CAREFREE PROT"),
    ("afeitadora", "afeitar, maquina", "MAQ AFE"),
    ("maquinita", "afeitar, maquina", "MAQ AFE"),
    ("dipirona", "novalgina, novacler", "también sale por el principio activo"),
    ("compuesta", "compositum", "BUSCAPINA COMPOSITUM, MIGRAL COMPOSITUM"),
    ("bebe", "baby", "JOHNSON BABY, ALGABO BABY"),
    ("spray para el pelo", "laca, fijador", "coloquial"),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS catalogo_diccionario (
            id          BIGSERIAL PRIMARY KEY,
            tipo        TEXT NOT NULL CHECK (tipo IN ('abreviatura', 'sinonimo')),
            termino     TEXT NOT NULL,
            equivale    TEXT NOT NULL,
            estado      TEXT NOT NULL DEFAULT 'activa'
                        CHECK (estado IN ('activa', 'propuesta', 'descartada')),
            origen      TEXT NOT NULL DEFAULT 'farmacia',
            nota        TEXT,
            autor       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tipo, termino)
        )
    """)
    tabla = sa.table(
        "catalogo_diccionario",
        sa.column("tipo", sa.Text), sa.column("termino", sa.Text),
        sa.column("equivale", sa.Text), sa.column("estado", sa.Text),
        sa.column("origen", sa.Text), sa.column("nota", sa.Text),
    )
    filas = (
        [{"tipo": "abreviatura", "termino": t, "equivale": e, "estado": "activa",
          "origen": "base", "nota": None} for t, e in _BASE_ABREV.items()]
        + [{"tipo": "sinonimo", "termino": t, "equivale": e, "estado": "activa",
            "origen": "base", "nota": None} for t, e in _BASE_SINON.items()]
        + [{"tipo": "abreviatura", "termino": t, "equivale": e, "estado": "propuesta",
            "origen": "minado", "nota": n} for t, e, n in _PROPUESTAS_ABREV]
        + [{"tipo": "sinonimo", "termino": t, "equivale": e, "estado": "propuesta",
            "origen": "minado", "nota": n} for t, e, n in _PROPUESTAS_SINON]
    )
    # Idempotente: si la tabla ya tenía filas (migración aplicada a mano), no duplica.
    conn = op.get_bind()
    existentes = {(r[0], r[1]) for r in conn.execute(
        sa.text("SELECT tipo, termino FROM catalogo_diccionario"))}
    nuevas = [f for f in filas if (f["tipo"], f["termino"]) not in existentes]
    if nuevas:
        op.bulk_insert(tabla, nuevas)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalogo_diccionario")
