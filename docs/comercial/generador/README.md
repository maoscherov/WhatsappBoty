# Generador de documentos comerciales (docx-js)

Scripts que producen los .docx de `docs/comercial/`. Requieren Node y el paquete `docx` (`npm install docx`).

- `lib.js`: tema Keep IT Simple clásico (Trebuchet, naranja, logo y pie de Rosario, portada con banda).
- `lib_dark.js`: tema "propuesta" (tarjetas negro-verdosas, verde menta, monoespaciada), tomado de la propuesta KIS-2026-131.
- `doc1_ley25326.js`, `doc2_crm.js`, `doc3_compromisos.js`: contenido de cada documento.

```bash
node doc3_compromisos.js "../Remedia - Compromisos de cumplimiento Datos Personales.docx"
THEME=dark node doc3_compromisos.js "../Remedia - Compromisos de cumplimiento Datos Personales (verde).docx"
```

Hoy sólo `doc3_compromisos.js` elige el tema por la variable `THEME`; para los otros dos basta
cambiar el `require("./lib")` de la primera línea por el mismo selector.
