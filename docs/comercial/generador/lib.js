// Helpers compartidos para generar los documentos corporativos con docx-js.
// Look & feel Keep IT Simple (extraído del documento de referencia Fintouch 2022).
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, AlignmentType, BorderStyle, ShadingType, LevelFormat, PageBreak,
  Header, Footer, PageNumber, TableOfContents, VerticalAlign, ImageRun, HeightRule,
  HorizontalPositionRelativeFrom, VerticalPositionRelativeFrom, TextWrappingType,
} = require("docx");

// ── Identidad KIS ──────────────────────────────────────────────────────────
const FONT = "Trebuchet MS";
const ORANGE = "FF9900";        // títulos
const ORANGE_DK = "ED7D31";     // cabeceras y bordes de tabla
const PEACH = "FCE4D6";         // filas alternas
const GREY_TXT = "595959";      // título de portada / h3
const GREY_BAR = "7F7F7F";      // barra del pie y línea del encabezado
const GREY_BAND = "EDEDED";     // banda de portada
const GREY_LIGHT = "F2F2F2";    // recuadros
const BLACK = "000000";
const WHITE = "FFFFFF";

const LOGO = fs.readFileSync(path.join(__dirname, "kis-logo.png"));  // 2639x821
const ISO = fs.readFileSync(path.join(__dirname, "kis-iso.png"));    // 1560x1654

const EMU = 914400; // por pulgada
const PAGE_W = 11907, PAGE_H = 16839;
const M_LEFT = 1080, M_RIGHT = 992, M_TOP = 1440, M_BOTTOM = 1150;
const CONTENT_W = PAGE_W - M_LEFT - M_RIGHT; // 9835
const LEGACY_W = 9026; // ancho con el que se escribieron las tablas de los scripts

const EMPRESA = "Keep IT Simple";

// Reemplazo de la figura genérica "Proveedor" por la empresa.
function brand(text) {
  if (typeof text !== "string") return text;
  return text
    .replace(/\[PROVEEDOR\]/g, EMPRESA)
    .replace(/\b[Dd]el Proveedor\b/g, "de " + EMPRESA)
    .replace(/\b[Aa]l Proveedor\b/g, "a " + EMPRESA)
    .replace(/\b[Ee]l Proveedor\b/g, EMPRESA)
    .replace(/\bProveedor\b(?! de (nube|conectividad|IA|modelos|servicios))/g, EMPRESA);
}

function run(text, opts = {}) {
  return new TextRun({ text: brand(text), font: FONT, size: 22, color: BLACK, ...opts });
}

// Texto con **negrita** y `código` inline.
function runs(text, base = {}) {
  const parts = brand(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p) => {
    if (p.startsWith("**")) return run(p.slice(2, -2), { ...base, bold: true });
    if (p.startsWith("`")) {
      const { font, size, ...rest } = base;
      return new TextRun({ text: p.slice(1, -1), font: "Consolas", size: (size || 22) - 2, ...rest });
    }
    return run(p, base);
  });
}

function p(text, opts = {}) {
  return new Paragraph({
    children: typeof text === "string" ? runs(text) : text,
    spacing: { after: 140, line: 264 },
    alignment: AlignmentType.JUSTIFIED,
    ...opts,
  });
}

function small(text) {
  return new Paragraph({
    children: runs(text, { size: 18, color: GREY_TXT, italics: true }),
    spacing: { after: 120 },
  });
}

function h1(text) {
  return new Paragraph({
    children: [run(text, { bold: true, size: 40, color: ORANGE })],
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 200 },
    keepNext: true,
    border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ORANGE, space: 3 } },
  });
}
function h2(text) {
  return new Paragraph({
    children: [run(text, { bold: true, size: 28, color: ORANGE })],
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 260, after: 120 },
    keepNext: true,
  });
}
function h3(text) {
  return new Paragraph({
    children: [run(text, { bold: true, size: 23, color: GREY_TXT })],
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 200, after: 100 },
    keepNext: true,
  });
}

function bullets(items, ref = "bullets") {
  return items.map(
    (t) =>
      new Paragraph({
        children: runs(t),
        numbering: { reference: ref, level: 0 },
        spacing: { after: 80, line: 264 },
        alignment: AlignmentType.JUSTIFIED,
      })
  );
}

let numberedInstance = 0;
function numbered(items) {
  numberedInstance += 1;
  return items.map(
    (t) =>
      new Paragraph({
        children: runs(t),
        numbering: { reference: "numbers", level: 0, instance: numberedInstance },
        spacing: { after: 80, line: 264 },
        alignment: AlignmentType.JUSTIFIED,
      })
  );
}

function cell(text, opts = {}) {
  const { width, header = false, shade, align } = opts;
  const lines = Array.isArray(text) ? text : [text];
  const fill = header ? ORANGE_DK : shade;
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: fill ? { type: ShadingType.CLEAR, fill, color: "auto" } : undefined,
    verticalAlign: VerticalAlign.TOP,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: lines.map(
      (l) =>
        new Paragraph({
          children: runs(l, { size: 18, bold: header || undefined, color: header ? WHITE : BLACK }),
          alignment: align || AlignmentType.LEFT,
          spacing: { after: 40, line: 252 },
        })
    ),
  });
}

// table(headers, rows, widths) - widths escritos para 9026 DXA; se escalan al ancho útil actual.
function table(headers, rows, widths) {
  const scaled = widths.map((w) => Math.round((w * CONTENT_W) / LEGACY_W));
  const total = scaled.reduce((a, b) => a + b, 0);
  const border = { style: BorderStyle.SINGLE, size: 6, color: ORANGE_DK };
  return new Table({
    width: { size: total, type: WidthType.DXA },
    columnWidths: scaled,
    borders: { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border },
    rows: [
      new TableRow({
        tableHeader: true, cantSplit: true,
        children: headers.map((h, i) => cell(h, { width: scaled[i], header: true })),
      }),
      ...rows.map(
        (r, ri) =>
          new TableRow({
            cantSplit: true,
            children: r.map((c, i) => cell(c, { width: scaled[i], shade: ri % 2 === 1 ? PEACH : undefined })),
          })
      ),
    ],
  });
}

// Recuadro destacado: fondo gris claro con filete naranja a la izquierda.
function callout(title, lines) {
  const none = { style: BorderStyle.NONE, size: 0, color: WHITE };
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [CONTENT_W],
    borders: { top: none, bottom: none, right: none, insideHorizontal: none, insideVertical: none,
      left: { style: BorderStyle.SINGLE, size: 36, color: ORANGE } },
    rows: [
      new TableRow({
        children: [
          new TableCell({
            width: { size: CONTENT_W, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: GREY_LIGHT, color: "auto" },
            margins: { top: 140, bottom: 140, left: 220, right: 200 },
            children: [
              new Paragraph({ children: [run(title, { bold: true, color: ORANGE, size: 24 })], spacing: { after: 100 } }),
              ...lines.map((l) => new Paragraph({ children: runs(l, { size: 21 }), spacing: { after: 70, line: 264 }, alignment: AlignmentType.JUSTIFIED })),
            ],
          }),
        ],
      }),
    ],
  });
}

function spacer() {
  return new Paragraph({ children: [run("")], spacing: { after: 60 } });
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

function codeBlock(text) {
  const border = { style: BorderStyle.SINGLE, size: 4, color: "D9D9D9" };
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [CONTENT_W],
    borders: { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border },
    rows: [
      new TableRow({
        children: [
          new TableCell({
            width: { size: CONTENT_W, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: GREY_LIGHT, color: "auto" },
            margins: { top: 100, bottom: 100, left: 160, right: 160 },
            children: brand(text).split("\n").map(
              (l) => new Paragraph({ children: [new TextRun({ text: l, font: "Consolas", size: 16 })], spacing: { after: 0 } })
            ),
          }),
        ],
      }),
    ],
  });
}

// ── Portada (sección propia, sin márgenes) ─────────────────────────────────
function cover({ title, subtitle, meta }) {
  const none = { style: BorderStyle.NONE, size: 0, color: WHITE };
  const band = new Table({
    width: { size: PAGE_W, type: WidthType.DXA },
    columnWidths: [PAGE_W],
    borders: { top: none, bottom: none, left: none, right: none, insideHorizontal: none, insideVertical: none },
    rows: [
      new TableRow({
        height: { value: 4400, rule: HeightRule.EXACT },
        children: [
          new TableCell({
            width: { size: PAGE_W, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: GREY_BAND, color: "auto" },
            verticalAlign: VerticalAlign.CENTER,
            children: [
              new Paragraph({
                alignment: AlignmentType.CENTER,
                children: [new ImageRun({ type: "png", data: LOGO, transformation: { width: 336, height: 105 } })],
              }),
            ],
          }),
        ],
      }),
    ],
  });

  const iso = new Paragraph({
    children: [
      new ImageRun({
        type: "png", data: ISO,
        transformation: { width: 280, height: 297 },
        floating: {
          horizontalPosition: { relative: HorizontalPositionRelativeFrom.PAGE, offset: Math.round(-0.6 * EMU) },
          verticalPosition: { relative: VerticalPositionRelativeFrom.PAGE, offset: Math.round(8.85 * EMU) },
          wrap: { type: TextWrappingType.NONE },
          behindDocument: true, allowOverlap: true,
        },
      }),
    ],
  });

  const ind = { left: 1300, right: 1300 };
  const children = [
    band,
    new Paragraph({ children: [run("")], spacing: { before: 2600 } }),
    new Paragraph({
      alignment: AlignmentType.CENTER, indent: ind,
      children: [run(title, { size: 60, color: GREY_TXT })],
      spacing: { after: 240, line: 276 },
    }),
    new Paragraph({
      alignment: AlignmentType.CENTER, indent: ind,
      children: [run(subtitle, { size: 26, color: GREY_TXT, italics: true })],
      spacing: { after: 700 },
    }),
    ...meta.map(([k, v]) =>
      new Paragraph({
        alignment: AlignmentType.CENTER, indent: ind,
        children: [run(k + ": ", { bold: true, size: 20, color: GREY_TXT }), run(v, { size: 20, color: GREY_TXT })],
        spacing: { after: 80 },
      })
    ),
    iso,
  ];
  return [{ __cover: children }];
}

// ── Encabezado y pie de las páginas interiores ─────────────────────────────
function makeHeader() {
  return new Header({
    children: [
      new Paragraph({
        alignment: AlignmentType.RIGHT,
        children: [new ImageRun({ type: "png", data: LOGO, transformation: { width: 150, height: 47 } })],
        spacing: { after: 80 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 24, color: GREY_BAR, space: 6 } },
      }),
    ],
  });
}

function makeFooter(shortTitle) {
  const none = { style: BorderStyle.NONE, size: 0, color: WHITE };
  const iso = new Paragraph({
    children: [
      new ImageRun({
        type: "png", data: ISO,
        transformation: { width: 230, height: 244 },
        floating: {
          horizontalPosition: { relative: HorizontalPositionRelativeFrom.PAGE, offset: Math.round(-1.45 * EMU) },
          verticalPosition: { relative: VerticalPositionRelativeFrom.PAGE, offset: Math.round(9.6 * EMU) },
          wrap: { type: TextWrappingType.NONE },
          behindDocument: true, allowOverlap: true,
        },
      }),
    ],
    spacing: { after: 0 },
  });
  const w1 = 3000, w2 = PAGE_W - w1;
  const white = { size: 15, color: WHITE };
  const bar = new Table({
    width: { size: PAGE_W, type: WidthType.DXA },
    columnWidths: [w1, w2],
    indent: { size: -M_LEFT, type: WidthType.DXA },
    borders: { top: none, bottom: none, left: none, right: none, insideHorizontal: none, insideVertical: none },
    rows: [
      new TableRow({
        height: { value: 520, rule: HeightRule.EXACT },
        children: [
          new TableCell({
            width: { size: w1, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: GREY_BAR, color: "auto" },
            verticalAlign: VerticalAlign.CENTER,
            margins: { left: 1500, top: 40, bottom: 40 },
            children: [
              new Paragraph({
                children: [
                  run("Página ", white),
                  new TextRun({ children: [PageNumber.CURRENT], font: FONT, ...white }),
                  run(" de ", white),
                  new TextRun({ children: [PageNumber.TOTAL_PAGES], font: FONT, ...white }),
                ],
                spacing: { after: 0 },
              }),
            ],
          }),
          new TableCell({
            width: { size: w2, type: WidthType.DXA },
            shading: { type: ShadingType.CLEAR, fill: GREY_BAR, color: "auto" },
            verticalAlign: VerticalAlign.CENTER,
            margins: { right: 300, top: 40, bottom: 40 },
            children: [
              new Paragraph({ alignment: AlignmentType.RIGHT, children: [run("www.keepitsimple.com.ar  |  contacto@keepitsimple.com.ar", white)], spacing: { after: 0 } }),
              new Paragraph({ alignment: AlignmentType.RIGHT, children: [run("Av. Luis Cándido Carballo 183 Piso 3 Of. 3  |  Rosario  |  Santa Fe  |  Argentina", white)], spacing: { after: 0 } }),
            ],
          }),
        ],
      }),
    ],
  });
  return new Footer({ children: [iso, bar] });
}

function buildDoc({ title, shortTitle, children }) {
  const [first, ...rest] = children;
  const coverChildren = first && first.__cover ? first.__cover : null;
  const body = coverChildren ? rest : children;

  const sections = [];
  if (coverChildren) {
    sections.push({
      properties: {
        page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: 0, bottom: 0, left: 0, right: 0, header: 0, footer: 0 } },
      },
      headers: { default: new Header({ children: [] }) },
      footers: { default: new Footer({ children: [] }) },
      children: coverChildren,
    });
  }
  sections.push({
    properties: {
      page: {
        size: { width: PAGE_W, height: PAGE_H },
        margin: { top: M_TOP, bottom: M_BOTTOM, left: M_LEFT, right: M_RIGHT, header: 560, footer: 380 },
      },
    },
    headers: { default: makeHeader() },
    footers: { default: makeFooter(shortTitle) },
    children: body,
  });

  return new Document({
    creator: EMPRESA,
    title: brand(title),
    styles: {
      default: { document: { run: { font: FONT, size: 22, color: BLACK } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 40, bold: true, color: ORANGE, font: FONT }, paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 28, bold: true, color: ORANGE, font: FONT }, paragraph: { spacing: { before: 260, after: 120 }, outlineLevel: 1 } },
        { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 23, bold: true, color: GREY_TXT, font: FONT }, paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 } },
        { id: "TOC1", name: "toc 1", basedOn: "Normal", next: "Normal", run: { size: 21, font: FONT, color: BLACK }, paragraph: { spacing: { after: 70 } } },
        { id: "TOC2", name: "toc 2", basedOn: "Normal", next: "Normal", run: { size: 19, font: FONT, color: GREY_TXT }, paragraph: { spacing: { after: 50 }, indent: { left: 360 } } },
      ],
    },
    numbering: {
      config: [
        { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 540, hanging: 270 } }, run: { font: FONT } } }] },
        { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 540, hanging: 360 } }, run: { font: FONT } } }] },
      ],
    },
    sections,
  });
}

function toc() {
  return [
    new Paragraph({ children: [run("Contenido", { bold: true, size: 32, color: ORANGE })], spacing: { after: 200 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ORANGE, space: 3 } } }),
    new TableOfContents("Contenido", { hyperlink: true, headingStyleRange: "1-2" }),
    pageBreak(),
  ];
}

async function save(doc, outPath) {
  const buf = await Packer.toBuffer(doc);
  fs.writeFileSync(outPath, buf);
  console.log("OK", outPath, buf.length, "bytes");
}

module.exports = { p, small, h1, h2, h3, bullets, numbered, table, callout, spacer, pageBreak, codeBlock, cover, buildDoc, toc, save, run, brand, CONTENT_W, EMPRESA };
