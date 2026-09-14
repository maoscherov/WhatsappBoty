// Plantilla maestra Keep IT Simple con estilos nombrados, pensada para que
// brand-docs la aprenda como perfil de marca. Todo el look vive en estilos
// (Title, Subtitle, Heading 1-3, Normal, Caption, KIS Nota, List Bullet, tabla KISTabla).
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, AlignmentType, BorderStyle, ShadingType, LevelFormat, Header, Footer,
  PageNumber, TableOfContents, VerticalAlign, ImageRun, HeightRule,
  HorizontalPositionRelativeFrom, VerticalPositionRelativeFrom, TextWrappingType,
} = require("docx");

const OUT = process.argv[2];
const FONT = "Trebuchet MS";
const ORANGE = "FF9900", ORANGE_DK = "ED7D31", PEACH = "FCE4D6", GREY_TXT = "595959", GREY_BAR = "7F7F7F", GREY_BAND = "EDEDED", GREY_LIGHT = "F2F2F2", BLACK = "000000", WHITE = "FFFFFF";
const LOGO = fs.readFileSync(path.join(__dirname, "kis-logo.png"));
const ISO = fs.readFileSync(path.join(__dirname, "kis-iso.png"));
const EMU = 914400;
const PAGE_W = 11907, PAGE_H = 16839;
const M_LEFT = 1080, M_RIGHT = 992, M_TOP = 1440, M_BOTTOM = 1150;
const CONTENT_W = PAGE_W - M_LEFT - M_RIGHT;
const none = { style: BorderStyle.NONE, size: 0, color: WHITE };
const noBorders = { top: none, bottom: none, left: none, right: none, insideHorizontal: none, insideVertical: none };

const t = (text, o = {}) => new TextRun({ text, ...o });
const P = (text, o = {}) => new Paragraph({ children: [t(text)], ...o });

// ── Portada: banda con logo en el encabezado de la primera sección ────────
const coverHeader = new Header({
  children: [
    new Table({
      width: { size: PAGE_W, type: WidthType.DXA }, columnWidths: [PAGE_W], borders: noBorders,
      rows: [new TableRow({ height: { value: 4400, rule: HeightRule.EXACT }, children: [
        new TableCell({ width: { size: PAGE_W, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: GREY_BAND, color: "auto" }, verticalAlign: VerticalAlign.CENTER,
          children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new ImageRun({ type: "png", data: LOGO, transformation: { width: 336, height: 105 } })] })] }),
      ] })],
    }),
  ],
});
const coverFooter = new Footer({
  children: [new Paragraph({ children: [new ImageRun({ type: "png", data: ISO, transformation: { width: 280, height: 297 },
    floating: { horizontalPosition: { relative: HorizontalPositionRelativeFrom.PAGE, offset: Math.round(-0.6 * EMU) }, verticalPosition: { relative: VerticalPositionRelativeFrom.PAGE, offset: Math.round(8.85 * EMU) }, wrap: { type: TextWrappingType.NONE }, behindDocument: true, allowOverlap: true } })] })],
});

// ── Encabezado y pie interiores ───────────────────────────────────────────
const bodyHeader = new Header({
  children: [new Paragraph({ alignment: AlignmentType.RIGHT, children: [new ImageRun({ type: "png", data: LOGO, transformation: { width: 150, height: 47 } })], spacing: { after: 80 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 24, color: GREY_BAR, space: 6 } } })],
});
const white = { size: 15, color: WHITE, font: FONT };
const w1 = 3000, w2 = PAGE_W - w1;
const bodyFooter = new Footer({
  children: [
    new Paragraph({ children: [new ImageRun({ type: "png", data: ISO, transformation: { width: 230, height: 244 },
      floating: { horizontalPosition: { relative: HorizontalPositionRelativeFrom.PAGE, offset: Math.round(-1.45 * EMU) }, verticalPosition: { relative: VerticalPositionRelativeFrom.PAGE, offset: Math.round(9.6 * EMU) }, wrap: { type: TextWrappingType.NONE }, behindDocument: true, allowOverlap: true } })], spacing: { after: 0 } }),
    new Table({ width: { size: PAGE_W, type: WidthType.DXA }, columnWidths: [w1, w2], indent: { size: -M_LEFT, type: WidthType.DXA }, borders: noBorders,
      rows: [new TableRow({ height: { value: 520, rule: HeightRule.EXACT }, children: [
        new TableCell({ width: { size: w1, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: GREY_BAR, color: "auto" }, verticalAlign: VerticalAlign.CENTER, margins: { left: 1500, top: 40, bottom: 40 },
          children: [new Paragraph({ children: [t("Página ", white), new TextRun({ children: [PageNumber.CURRENT], ...white }), t(" de ", white), new TextRun({ children: [PageNumber.TOTAL_PAGES], ...white })], spacing: { after: 0 } })] }),
        new TableCell({ width: { size: w2, type: WidthType.DXA }, shading: { type: ShadingType.CLEAR, fill: GREY_BAR, color: "auto" }, verticalAlign: VerticalAlign.CENTER, margins: { right: 300, top: 40, bottom: 40 },
          children: [
            new Paragraph({ alignment: AlignmentType.RIGHT, children: [t("www.keepitsimple.com.ar  |  contacto@keepitsimple.com.ar", white)], spacing: { after: 0 } }),
            new Paragraph({ alignment: AlignmentType.RIGHT, children: [t("Av. Luis Cándido Carballo 183 Piso 3 Of. 3  |  Rosario  |  Santa Fe  |  Argentina", white)], spacing: { after: 0 } }),
          ] }),
      ] })] }),
  ],
});

// ── Contenido de muestra (el plugin lo reemplaza) ─────────────────────────
const cell = (text, w, header = false) => new TableCell({ width: { size: w, type: WidthType.DXA }, margins: { top: 60, bottom: 60, left: 100, right: 100 },
  children: [new Paragraph({ children: [t(text, { size: 18, bold: header || undefined, color: header ? WHITE : undefined })], spacing: { after: 40 } })] });
const cw = [3000, 3835, 3000];
const demoTable = new Table({
  style: "KISTabla",
  width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: cw,
  rows: [
    new TableRow({ tableHeader: true, children: ["Columna uno", "Columna dos", "Columna tres"].map((h, i) => cell(h, cw[i], true)) }),
    new TableRow({ children: ["Fila uno", "Valor de ejemplo", "Detalle"].map((h, i) => cell(h, cw[i])) }),
    new TableRow({ children: ["Fila dos", "Valor de ejemplo", "Detalle"].map((h, i) => cell(h, cw[i])) }),
    new TableRow({ children: ["Fila tres", "Valor de ejemplo", "Detalle"].map((h, i) => cell(h, cw[i])) }),
  ],
});

const coverBody = [
  new Paragraph({ children: [t("")], spacing: { before: 2600 } }),
  new Paragraph({ text: "Título del documento", style: "Title" }),
  new Paragraph({ text: "Subtítulo o descripción breve del documento", style: "Subtitle" }),
];

const body = [
  new Paragraph({ text: "Contenido", style: "TOCHeading" }),
  new TableOfContents("Contenido", { hyperlink: true, headingStyleRange: "1-2" }),
  new Paragraph({ text: "Sección de ejemplo", heading: HeadingLevel.HEADING_1 }),
  P("Este es un párrafo de ejemplo del cuerpo del documento. Describe el contexto de la sección con dos o tres oraciones y se reemplaza por el contenido real de cada documento."),
  new Paragraph({ text: "Subsección de ejemplo", heading: HeadingLevel.HEADING_2 }),
  P("Segundo párrafo de ejemplo. El texto corriente va justificado, en negro y con la tipografía institucional."),
  new Paragraph({ text: "Detalle de ejemplo", heading: HeadingLevel.HEADING_3 }),
  new Paragraph({ text: "Primer ítem de una lista de ejemplo.", style: "ListBullet" }),
  new Paragraph({ text: "Segundo ítem de una lista de ejemplo.", style: "ListBullet" }),
  new Paragraph({ text: "Tercer ítem de una lista de ejemplo.", style: "ListBullet" }),
  new Paragraph({ text: "Nota destacada de ejemplo. Se usa para un aviso importante que el lector no debe pasar por alto.", style: "KISNota" }),
  demoTable,
  new Paragraph({ text: "Tabla 1. Leyenda de ejemplo de la tabla.", style: "Caption" }),
  P("Párrafo de cierre de ejemplo."),
];

const doc = new Document({
  creator: "Keep IT Simple",
  title: "Plantilla maestra Keep IT Simple",
  styles: {
    default: { document: { run: { font: FONT, size: 22, color: BLACK } } },
    paragraphStyles: [
      { id: "Normal", name: "Normal", run: { font: FONT, size: 22, color: BLACK }, paragraph: { spacing: { after: 140, line: 264 }, alignment: AlignmentType.LEFT } },
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 60, color: GREY_TXT }, paragraph: { alignment: AlignmentType.CENTER, spacing: { after: 240, line: 276 }, indent: { left: 1300, right: 1300 } } },
      { id: "Subtitle", name: "Subtitle", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 26, color: GREY_TXT, italics: true }, paragraph: { alignment: AlignmentType.CENTER, spacing: { after: 200 }, indent: { left: 1300, right: 1300 } } },
      { id: "KISPortadaDato", name: "KIS Portada Dato", basedOn: "Normal", next: "Normal", run: { font: FONT, size: 20, color: GREY_TXT }, paragraph: { alignment: AlignmentType.CENTER, spacing: { after: 80 } } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 40, bold: true, color: ORANGE }, paragraph: { spacing: { before: 360, after: 200 }, alignment: AlignmentType.LEFT, keepNext: true, outlineLevel: 0, border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ORANGE, space: 3 } } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 28, bold: true, color: ORANGE }, paragraph: { spacing: { before: 260, after: 120 }, alignment: AlignmentType.LEFT, keepNext: true, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 23, bold: true, color: GREY_TXT }, paragraph: { spacing: { before: 200, after: 100 }, alignment: AlignmentType.LEFT, keepNext: true, outlineLevel: 2 } },
      { id: "TOCHeading", name: "TOC Heading", basedOn: "Normal", next: "Normal", run: { font: FONT, size: 32, bold: true, color: ORANGE }, paragraph: { spacing: { after: 200 }, alignment: AlignmentType.LEFT, border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ORANGE, space: 3 } } } },
      { id: "TOC1", name: "toc 1", basedOn: "Normal", next: "Normal", run: { font: FONT, size: 21, color: BLACK }, paragraph: { spacing: { after: 70 }, alignment: AlignmentType.LEFT } },
      { id: "TOC2", name: "toc 2", basedOn: "Normal", next: "Normal", run: { font: FONT, size: 19, color: GREY_TXT }, paragraph: { spacing: { after: 50 }, indent: { left: 360 }, alignment: AlignmentType.LEFT } },
      { id: "Caption", name: "Caption", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 18, italics: true, color: GREY_TXT }, paragraph: { spacing: { before: 60, after: 200 }, alignment: AlignmentType.LEFT } },
      { id: "KISNota", name: "KIS Nota", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 21, color: BLACK }, paragraph: { alignment: AlignmentType.LEFT, spacing: { before: 120, after: 160, line: 264 }, indent: { left: 220, right: 200 }, shading: { type: ShadingType.CLEAR, fill: GREY_LIGHT, color: "auto" }, border: { left: { style: BorderStyle.SINGLE, size: 36, color: ORANGE, space: 8 }, top: { style: BorderStyle.SINGLE, size: 0, color: GREY_LIGHT, space: 6 }, bottom: { style: BorderStyle.SINGLE, size: 0, color: GREY_LIGHT, space: 6 } } } },
      { id: "ListBullet", name: "List Bullet", basedOn: "Normal", next: "ListBullet", quickFormat: true, run: { font: FONT, size: 22, color: BLACK }, paragraph: { numbering: { reference: "kis-bullets", level: 0 }, spacing: { after: 80, line: 264 } } },
      { id: "ListNumber", name: "List Number", basedOn: "Normal", next: "ListNumber", quickFormat: true, run: { font: FONT, size: 22, color: BLACK }, paragraph: { numbering: { reference: "kis-numbers", level: 0 }, spacing: { after: 80, line: 264 } } },
    ],
  },
  numbering: { config: [
    { reference: "kis-bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } }, run: { font: FONT } } }] },
    { reference: "kis-numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 360 } }, run: { font: FONT } } }] },
  ] },
  sections: [
    { properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: 0, bottom: 0, left: 0, right: 0, header: 0, footer: 0 } } },
      headers: { default: coverHeader }, footers: { default: coverFooter }, children: coverBody },
    { properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: M_TOP, bottom: M_BOTTOM, left: M_LEFT, right: M_RIGHT, header: 560, footer: 380 } } },
      headers: { default: bodyHeader }, footers: { default: bodyFooter }, children: body },
  ],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("OK", OUT, buf.length); });
