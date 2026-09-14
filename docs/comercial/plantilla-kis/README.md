# Plantilla maestra Keep IT Simple para brand-docs

Plantilla Word con estilos nombrados (Title, Subtitle, Heading 1-3, Normal, Caption,
KIS Nota, List Bullet/Number, tabla "KIS Tabla") que el plugin `brand-docs` aprende
como perfil de marca `keep-it-simple` (guardado en `~/.claude/brand-kit/keep-it-simple`).

## Regenerar la plantilla

```bash
node kis_master_template.js "KIS - Plantilla maestra.docx"
python inject_table_style.py "KIS - Plantilla maestra.docx"   # agrega estilo de tabla y tema
```

Requiere `docx` (npm) y los PNG `kis-logo.png` / `kis-iso.png` en la misma carpeta.

## Volver a aprender el perfil (sólo si cambia la plantilla)

```bash
cd ~/.claude/plugins/cache/brand-docs/brand-docs/<version>/skills/brand-docx
python scripts/cli.py extract --name keep-it-simple --template "<ruta>/KIS - Plantilla maestra.docx" --scope global
python scripts/cli.py comprehend --name keep-it-simple --input comprehension.json
```

Si cambian las anclas de portada o el campo TOC, actualizar los ids en `comprehension.json`
(`comprehend-input --name keep-it-simple` los lista).

## Generar un documento

```bash
python scripts/cli.py generate --name keep-it-simple --input idoc-compromisos.json --output salida.docx --scope auto --qa auto
```

`idoc-compromisos.json` es el documento de compromisos de datos personales en formato
IntermediateDocument; sirve de ejemplo para otros documentos.

## Limitaciones conocidas del plugin (v0.10.1)

- Los campos extra de portada se re-estampan con el estilo Título: usar sólo título y subtítulo.
- El texto nuevo de un slot de portada no puede contener el texto de muestra como subcadena.
- El estilo de tabla debe ser personalizado (`customStyle="1"`); si no, elige "Normal Table".
- El alias de color en `palette_annotations` rompe la siguiente generación (colisión consigo mismo); no usarlo.
- Sin LibreOffice/pdftoppm no hay auditoría visual; exportar con Word para revisar.
