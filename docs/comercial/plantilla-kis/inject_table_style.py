"""Inyecta el estilo de tabla KISTabla en word/styles.xml de un .docx generado con docx-js."""
import sys, zipfile, shutil, os

src = sys.argv[1]
TABLE_STYLE = (
    '<w:style w:type="table" w:customStyle="1" w:styleId="KISTabla">'
    '<w:name w:val="KIS Tabla"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="59"/><w:qFormat/>'
    '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr>'
    '<w:rPr><w:rFonts w:ascii="Trebuchet MS" w:hAnsi="Trebuchet MS" w:cs="Trebuchet MS"/><w:color w:val="000000"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
    '<w:tblPr><w:tblBorders>'
    '<w:top w:val="single" w:sz="6" w:space="0" w:color="ED7D31"/><w:left w:val="single" w:sz="6" w:space="0" w:color="ED7D31"/>'
    '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="ED7D31"/><w:right w:val="single" w:sz="6" w:space="0" w:color="ED7D31"/>'
    '<w:insideH w:val="single" w:sz="6" w:space="0" w:color="ED7D31"/><w:insideV w:val="single" w:sz="6" w:space="0" w:color="ED7D31"/>'
    '</w:tblBorders><w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:left w:w="100" w:type="dxa"/><w:bottom w:w="60" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr>'
    '<w:tblStylePr w:type="firstRow"><w:pPr><w:jc w:val="left"/></w:pPr><w:rPr><w:b/><w:bCs/><w:color w:val="FFFFFF"/></w:rPr>'
    '<w:tblPr/><w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="ED7D31"/></w:tcPr></w:tblStylePr>'
    '<w:tblStylePr w:type="band2Horz"><w:tblPr/><w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="FCE4D6"/></w:tcPr></w:tblStylePr>'
    '</w:style>'
)
TABLE_NORMAL = (
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/>'
    '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
)

THEME = open(os.path.join(os.path.dirname(__file__), "theme1.xml"), encoding="utf-8").read()
THEME = THEME.replace('<a:accent1><a:srgbClr val="5B9BD5"/></a:accent1>', '<a:accent1><a:srgbClr val="FF9900"/></a:accent1>')
THEME = THEME.replace('<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>', '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>')
THEME = THEME.replace('<a:latin typeface="Calibri Light"', '<a:latin typeface="Trebuchet MS"').replace('<a:latin typeface="Calibri"', '<a:latin typeface="Trebuchet MS"')
THEME = THEME.replace('<a:dk2><a:srgbClr val="44546A"/></a:dk2>', '<a:dk2><a:srgbClr val="595959"/></a:dk2>')
tmp = src + ".tmp"
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "word/styles.xml":
            s = data.decode("utf-8")
            if 'w:styleId="TableNormal"' not in s:
                s = s.replace("</w:styles>", TABLE_NORMAL + "</w:styles>")
            if 'w:styleId="TableGrid"' not in s:
                s = s.replace("</w:styles>", TABLE_STYLE + "</w:styles>")
            data = s.encode("utf-8")
        if item.filename == "word/document.xml":
            s = data.decode("utf-8")
            # tblLook: primera fila + bandas horizontales, sin primera columna
            s = s.replace('<w:tblStyle w:val="KISTabla"/>', '<w:tblStyle w:val="KISTabla"/><w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/>')
            data = s.encode("utf-8")
        if item.filename == "word/_rels/document.xml.rels":
            s = data.decode("utf-8")
            if "relationships/theme" not in s:
                s = s.replace("</Relationships>", '<Relationship Id="rIdTheme1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/></Relationships>')
            data = s.encode("utf-8")
        if item.filename == "[Content_Types].xml":
            s = data.decode("utf-8")
            if "theme+xml" not in s:
                s = s.replace("</Types>", '<Override PartName="/word/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/></Types>')
            data = s.encode("utf-8")
        zout.writestr(item, data)
    zout.writestr("word/theme/theme1.xml", THEME.encode("utf-8"))
shutil.move(tmp, src)
print("table style injected into", src)
