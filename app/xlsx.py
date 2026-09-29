"""Минимальное чтение .xlsx без сторонних библиотек (zip + XML).

Возвращает {имя_листа: [[значения строки], ...]} — достаточно для импорта таблиц.
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el) -> str:
    # <si>/<is> может содержать <t> или набор <r><t>…</t></r> (rich text)
    return "".join(t.text or "" for t in el.iter(f"{{{NS['m']}}}t"))


def read_xlsx(data: bytes) -> dict[str, list[list]]:
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = [_text(si) for si in root.findall("m:si", NS)]

    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS)}

    result = {}
    for sh in wb.findall("m:sheets/m:sheet", NS):
        rid = sh.get(f"{{{NS['r']}}}id")
        target = targets[rid].lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        root = ET.fromstring(z.read(path))
        rows = []
        for row in root.findall("m:sheetData/m:row", NS):
            vals = {}
            col = -1
            for c in row.findall("m:c", NS):
                # атрибут r («B7») по стандарту необязателен — без него ячейка идёт следующей по порядку
                col = _col_index(c.get("r")) if c.get("r") else col + 1
                t = c.get("t")
                v = c.find("m:v", NS)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    is_ = c.find("m:is", NS)
                    val = _text(is_) if is_ is not None else ""
                elif v is not None:
                    val = v.text
                    if t not in ("str", "e", "b"):
                        try:
                            f = float(val)
                            val = int(f) if f.is_integer() else f
                        except ValueError:
                            pass
                else:
                    val = None
                vals[col] = val
            rnum = int(row.get("r")) - 1 if row.get("r") else len(rows)
            while len(rows) < rnum:
                rows.append([])
            width = max(vals) + 1 if vals else 0
            rows.append([vals.get(i) for i in range(width)])
        result[sh.get("name")] = rows
    return result
