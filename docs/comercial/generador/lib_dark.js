// Tema "propuesta" de Keep IT Simple: tarjetas negro-verdosas, acento verde menta,
// fondos crudos y etiquetas en monoespaciada (tomado de la propuesta KIS-2026-131).
// Misma API que lib.js para que los scripts de documentos no cambien.
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, AlignmentType, BorderStyle, ShadingType, LevelFormat, PageBreak,
  Header, Footer, PageNumber, TableOfContents, VerticalAlign, TabStopType, TabStopPosition,
} = require("docx");

// ── Paleta (muestreada del PDF) ─────────────────────────────────────────────
const INK = "0D1613";        // tarjetas y títulos
const MINT = "31D6A0";       // acento sobre fondo oscuro
const GREEN = "12A37C";      // acento sobre fondo claro
const GREEN_DK = "1D6C52";   // borde de etiqueta sobre oscuro
const BODY = "5C6663";       // texto corriente
const LABEL = "8D968F";      // etiquetas grises
const CARD = "EEEDE8";       // tarjeta clara
const LINE = "E3E2DC";       // bordes
const MINT_LIGHT = "D9F3E8"; // fila destacada
const CARD_TEXT = "C6D0CC";  // texto secundario sobre oscuro
const WHITE = "FFFFFF";

const SANS = "Segoe UI";
const MONO = "Consolas";

const PAGE_W = 11906, PAGE_H = 16838;
const M_LEFT = 1250, M_RIGHT = 1250, M_TOP = 1350, M_BOTTOM = 1250;
const CONTENT_W = PAGE_W - M_LEFT - M_RIGHT; // 9406
const LEGACY_W = 9026;

const EMPRESA = "Keep IT Simple";
function brand(text) {
  if (typeof text !== "string") return text;
  return text
    .replace(/\[PROVEEDOR\]/g, EMPRESA)
    .replace(/\b[Dd]el Proveedor\b/g, "de " + EMPRESA)
    .replace(/\b[Aa]l Proveedor\b/g, "a " + EMPRESA)
    .replace(/\b[Ee]l Proveedor\b/g, EMPRESA)
    .replace(/\bProveedor\b(?! de (nube|conectividad|IA|modelos|servicios))/g, EMPRESA);
}

const none = { style: BorderStyle.NONE, size: 0, color: WHITE };
const noBorders = { top: none, bottom: none, left: none, right: none, insideHorizontal: none, insideVertical: none };

function run(text, opts = {}) {
  return new TextRun({ text: brand(text), font: SANS, size: 20, color: BODY, ...opts });
}
function mono(text, opts = {}) {
  return new TextRun({ text: brand(text), font: MONO, size: 14, color: LABEL, ...opts });
}
function runs(text, base = {}) {
  const parts = brand(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return parts.map((p) => {
    if (p.startsWith("**")) return run(p.slice(2, -2), { ...base, bold: true, color: base.color || INK });
    if (p.startsWith("`")) { const { font, size, ...rest } = base; return new TextRun({ text: p.slice(1, -1), font: MONO, size: (size || 20) - 2, color: GREEN, ...rest }); }
    return run(p, base);
  });
}

function p(text, opts = {}) {
  return new Paragraph({ children: typeof text === "string" ? runs(text) : text, spacing: { after: 140, line: 288 }, ...opts });
}
function small(text) {
  return new Paragraph({ children: runs(text, { size: 16, color: LABEL }), spacing: { after: 120, line: 264 } });
}

// "1. Declaración" -> etiqueta "SECCIÓN 01" + título sin número
function h1(text) {
  const m = /^(\d+)\.\s+(.*)$/.exec(text);
  const eyebrow = m ? `SECCIÓN ${m[1].padStart(2, "0")}` : "SECCIÓN";
  const title = m ? m[2] : text;
  return [
    new Paragraph({ children: [mono(eyebrow, { color: GREEN, bold: true, characterSpacing: 30 })], spacing: { before: 420, after: 40 }, keepNext: true }),
    new Paragraph({ children: [run(title, { bold: true, size: 40, color: INK })], heading: HeadingLevel.HEADING_1, spacing: { after: 160 }, keepNext: true }),
  ];
}
function h2(text) {
  return new Paragraph({ children: [run(text, { bold: true, size: 26, color: INK })], heading: HeadingLevel.HEADING_2, spacing: { before: 260, after: 100 }, keepNext: true });
}
function h3(text) {
  return new Paragraph({ children: [run(text, { bold: true, size: 21, color: INK })], heading: HeadingLevel.HEADING_3, spacing: { before: 200, after: 80 }, keepNext: true });
}

function bullets(items, ref = "bullets") {
  return items.map((t) => new Paragraph({ children: runs(t), numbering: { reference: ref, level: 0 }, spacing: { after: 90, line: 276 } }));
}
let numberedInstance = 0;
function numbered(items) {
  numberedInstance += 1;
  return items.map((t) => new Paragraph({ children: runs(t), numbering: { reference: "numbers", level: 0, instance: numberedInstance }, spacing: { after: 90, line: 276 } }));
}

function cell(text, opts = {}) {
  const { width, header = false, first = false } = opts;
  const lines = Array.isArray(text) ? text : [text];
  const fill = header ? INK : first ? CARD : undefined;
  const side = { style: BorderStyle.SINGLE, size: 4, color: LINE };
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: fill ? { type: ShadingType.CLEAR, fill, color: "auto" } : undefined,
    borders: { top: side, bottom: side, left: none, right: none },
    verticalAlign: VerticalAlign.TOP,
    margins: { top: 110, bottom: 110, left: 140, right: 140 },
    children: lines.map((l) => new Paragraph({
      children: header
        ? [mono(brand(l.replace(/\*\*/g, "")).toUpperCase(), { color: CARD_TEXT, bold: true, characterSpacing: 20 })]
        : runs(l, { size: 17, color: first ? INK : BODY, bold: first || undefined }),
      spacing: { after: 30, line: 252 },
    })),
  });
}

function table(headers, rows, widths, opts = {}) {
  const scaled = widths.map((w) => Math.round((w * CONTENT_W) / LEGACY_W));
  const total = scaled.reduce((a, b) => a + b, 0);
  const side = { style: BorderStyle.SINGLE, size: 4, color: LINE };
  return new Table({
    width: { size: total, type: WidthType.DXA }, columnWidths: scaled,
    borders: { top: side, bottom: side, left: side, right: side, insideHorizontal: side, insideVertical: none },
    rows: [
      new TableRow({ tableHeader: true, cantSplit: true, children: headers.map((h, i) => cell(h, { width: scaled[i], header: true })) }),
      ...rows.map((r) => new TableRow({ cantSplit: true, children: r.map((c, i) => cell(c, { width: scaled[i], first: !opts.plain && i === 0 })) })),
    ],
  });
}

// Tarjeta oscura con etiqueta menta (como "POR QUÉ NO ES UN BOT GENÉRICO")
function darkCard(children) {
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [CONTENT_W], borders: noBorders,
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: INK, color: "auto" },
      margins: { top: 260, bottom: 260, left: 340, right: 340 },
      children,
    })] })],
  });
}
function callout(title, lines) {
  return darkCard([
    new Paragraph({ children: [mono(title.toUpperCase(), { color: MINT, bold: true, characterSpacing: 30 })], spacing: { after: 120 } }),
    ...lines.map((l) => new Paragraph({ children: runs(l, { size: 19, color: CARD }).map((r) => r), spacing: { after: 80, line: 288 } })),
  ]);
}

function spacer() { return new Paragraph({ children: [run("")], spacing: { after: 80 } }); }
function pageBreak() { return new Paragraph({ children: [new PageBreak()] }); }

function codeBlock(text) {
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [CONTENT_W], borders: noBorders,
    rows: [new TableRow({ children: [new TableCell({
      width: { size: CONTENT_W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: CARD, color: "auto" },
      margins: { top: 160, bottom: 160, left: 240, right: 240 },
      children: brand(text).split("\n").map((l) => new Paragraph({ children: [new TextRun({ text: l, font: MONO, size: 15, color: INK })], spacing: { after: 0 } })),
    })] })],
  });
}

// ── Apertura: tarjeta oscura con título + tarjeta de datos (sin página de portada)
function shortLabel(k) {
  if (/^Responsable/.test(k)) return "CLIENTE";
  if (/^Encargado/.test(k)) return "ENCARGADO";
  return k.toUpperCase();
}
function cover({ title, subtitle, meta }) {
  const pill = new Table({
    width: { size: 5600, type: WidthType.DXA }, columnWidths: [5600],
    borders: { top: { style: BorderStyle.SINGLE, size: 6, color: GREEN_DK }, bottom: { style: BorderStyle.SINGLE, size: 6, color: GREEN_DK }, left: { style: BorderStyle.SINGLE, size: 6, color: GREEN_DK }, right: { style: BorderStyle.SINGLE, size: 6, color: GREEN_DK }, insideHorizontal: none, insideVertical: none },
    rows: [new TableRow({ children: [new TableCell({
      width: { size: 5600, type: WidthType.DXA }, margins: { top: 40, bottom: 40, left: 120, right: 120 },
      shading: { type: ShadingType.CLEAR, fill: INK, color: "auto" },
      children: [new Paragraph({ children: [mono("LEY 25.326 — DECLARACIÓN DEL ENCARGADO", { color: MINT, characterSpacing: 20 })], spacing: { after: 0 } })],
    })] })],
  });
  const hero = darkCard([
    new Paragraph({ children: [run(title, { bold: true, size: 46, color: WHITE }), run(".", { bold: true, size: 46, color: MINT })], spacing: { after: 160, line: 264 } }),
    pill,
    new Paragraph({ children: [run("")], spacing: { after: 60 } }),
    new Paragraph({ children: [run(subtitle, { size: 20, color: CARD })], spacing: { after: 0, line: 300 } }),
  ]);
  const prefer = ["Responsable", "Cliente", "Encargado", "Versión", "Servicio", "Producto", "Fecha"];
  const picked = [];
  for (const k of prefer) { const m = meta.find(([kk]) => kk.startsWith(k) && !picked.includes(m0 => m0)); const hit = meta.find(([kk]) => kk.startsWith(k)); if (hit && !picked.includes(hit) && picked.length < 4) picked.push(hit); }
  const items = picked.length ? picked : meta.slice(0, 4);
  const w = Math.floor(CONTENT_W / items.length);
  const side = { style: BorderStyle.SINGLE, size: 4, color: LINE };
  const metaCard = new Table({
    width: { size: w * items.length, type: WidthType.DXA }, columnWidths: items.map(() => w),
    borders: { top: side, bottom: side, left: side, right: side, insideHorizontal: none, insideVertical: side },
    rows: [new TableRow({ children: items.map(([k, v]) => new TableCell({
      width: { size: w, type: WidthType.DXA }, margins: { top: 160, bottom: 160, left: 200, right: 160 },
      children: [
        new Paragraph({ children: [mono(shortLabel(k), { characterSpacing: 20 })], spacing: { after: 50 } }),
        new Paragraph({ children: [run(v, { bold: true, size: 19, color: INK })], spacing: { after: 0 } }),
      ],
    })) })],
  });
  return [hero, new Paragraph({ children: [run("")], spacing: { after: 120 } }), metaCard, new Paragraph({ children: [run("")], spacing: { after: 160 } })];
}

function makeHeader(shortTitle) {
  return new Header({ children: [new Paragraph({
    tabStops: [{ type: TabStopType.RIGHT, position: CONTENT_W }],
    children: [mono("KEEP IT SIMPLE", { bold: true, color: INK, characterSpacing: 40 }), new TextRun({ text: "\t", font: MONO, size: 14 }), mono(brand(shortTitle).toUpperCase(), { characterSpacing: 20 })],
    spacing: { after: 0 },
  })] });
}
function makeFooter() {
  return new Footer({ children: [new Paragraph({
    alignment: AlignmentType.CENTER,
    border: { top: { style: BorderStyle.SINGLE, size: 4, color: LINE, space: 10 } },
    children: [mono("Documento confidencial — declaración de cumplimiento  ·  página "), new TextRun({ children: [PageNumber.CURRENT], font: MONO, size: 14, color: LABEL }), mono(" de "), new TextRun({ children: [PageNumber.TOTAL_PAGES], font: MONO, size: 14, color: LABEL })],
    spacing: { after: 0 },
  })] });
}

function buildDoc({ title, shortTitle, children }) {
  const flat = children.flat();
  return new Document({
    creator: EMPRESA, title: brand(title),
    styles: {
      default: { document: { run: { font: SANS, size: 20, color: BODY } } },
      paragraphStyles: [
        { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 40, bold: true, color: INK, font: SANS }, paragraph: { spacing: { after: 160 }, outlineLevel: 0 } },
        { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 26, bold: true, color: INK, font: SANS }, paragraph: { spacing: { before: 260, after: 100 }, outlineLevel: 1 } },
        { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true, run: { size: 21, bold: true, color: INK, font: SANS }, paragraph: { spacing: { before: 200, after: 80 }, outlineLevel: 2 } },
        { id: "TOC1", name: "toc 1", basedOn: "Normal", next: "Normal", run: { size: 19, font: SANS, color: INK }, paragraph: { spacing: { after: 60 } } },
        { id: "TOC2", name: "toc 2", basedOn: "Normal", next: "Normal", run: { size: 17, font: SANS, color: BODY }, paragraph: { spacing: { after: 40 }, indent: { left: 360 } } },
      ],
    },
    numbering: { config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 270 } }, run: { font: SANS, color: GREEN, bold: true } } }] },
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL_ZERO, text: "%1", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 640, hanging: 400 } }, run: { font: MONO, color: GREEN, bold: true, size: 16 } } }] },
    ] },
    sections: [{
      properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: M_TOP, bottom: M_BOTTOM, left: M_LEFT, right: M_RIGHT, header: 620, footer: 560 } } },
      headers: { default: makeHeader(shortTitle) }, footers: { default: makeFooter() },
      children: flat,
    }],
  });
}

function toc() {
  return [
    new Paragraph({ children: [mono("CONTENIDO", { color: GREEN, bold: true, characterSpacing: 30 })], spacing: { after: 120 } }),
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
