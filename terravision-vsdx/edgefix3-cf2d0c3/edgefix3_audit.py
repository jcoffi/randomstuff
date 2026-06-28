import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "http://schemas.microsoft.com/office/visio/2012/main"

def q(tag):
    return f"{{{NS}}}{tag}"

def cell(shape, name):
    for c in shape.findall(q("Cell")):
        if c.get("N") == name:
            return c.get("V"), c.get("F")
    return None, None

def own_has_geometry(shape):
    return any(sec.get("N") == "Geometry" for sec in shape.findall(q("Section")))

def iter_shapes(shape):
    yield shape
    nested = shape.find(q("Shapes"))
    if nested is not None:
        for child in nested.findall(q("Shape")):
            yield from iter_shapes(child)

def audit(path: Path):
    errors = []
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad is not None:
            errors.append(f"zip CRC failed at {bad}")
        page = ET.fromstring(zf.read("visio/pages/page1.xml"))
    top = page.find(q("Shapes"))
    top_shapes = top.findall(q("Shape")) if top is not None else []
    connectors = [s for s in top_shapes if "->" in (s.get("NameU") or "")]
    content = [s for s in top_shapes if "->" not in (s.get("NameU") or "")]
    if connectors and content:
        conn_ix = [top_shapes.index(s) for s in connectors]
        content_ix = [top_shapes.index(s) for s in content]
        if not (max(conn_ix) < min(content_ix)):
            errors.append(f"connectors not behind content: conn_ix={conn_ix} content_ix={content_ix}")
    else:
        errors.append("missing top-level connectors or content")

    all_shapes = []
    for s in top_shapes:
        all_shapes.extend(list(iter_shapes(s)))
    by_id = {s.get("ID"): s for s in all_shapes if s.get("ID")}
    connects = page.findall(f"{q('Connects')}/{q('Connect')}")
    begin = end = 0
    for conn in connectors:
        cid = conn.get("ID")
        if conn.get("Master") != "2":
            errors.append(f"connector {cid} missing Master=2")
    for cn in connects:
        if cn.get("FromCell") == "BeginX":
            begin += 1
        if cn.get("FromCell") == "EndX":
            end += 1
        if cn.get("ToCell") == "PinX" and cn.get("ToPart") == "3":
            errors.append(f"center glue reintroduced on connector {cn.get('FromSheet')}")
        if not (cn.get("ToCell") or "").startswith("Connections.X"):
            errors.append(f"non-perimeter ToCell {cn.get('ToCell')} on connector {cn.get('FromSheet')}")
        if cn.get("ToPart") not in {"100", "101", "102", "103"}:
            errors.append(f"bad ToPart {cn.get('ToPart')} on connector {cn.get('FromSheet')}")
        target = by_id.get(cn.get("ToSheet"))
        if target is None:
            errors.append(f"dangling ToSheet {cn.get('ToSheet')}")
        elif (target.get("Type") or "").lower() != "group":
            errors.append(f"connector targets non-group {target.get('Type')} {target.get('NameU')}")
        elif target.find(q("Section")) is None:
            errors.append(f"target group {target.get('ID')} lacks sections")
    boxes = [s for s in all_shapes if (s.get("NameU") or "").endswith(".box")]
    labels = [s for s in all_shapes if (s.get("NameU") or "").endswith(".label")]
    for box in boxes:
        fp, _ = cell(box, "FillPattern")
        lp, _ = cell(box, "LinePattern")
        lw, _ = cell(box, "LineWeight")
        lc, lcf = cell(box, "LineColor")
        if fp != "0":
            errors.append(f"box {box.get('NameU')} FillPattern={fp}")
        if lp in (None, "0"):
            errors.append(f"box {box.get('NameU')} LinePattern={lp}")
        try:
            if float(lw or "0") <= 0:
                errors.append(f"box {box.get('NameU')} LineWeight={lw}")
        except ValueError:
            errors.append(f"box {box.get('NameU')} invalid LineWeight={lw}")
        if not (lc or lcf):
            errors.append(f"box {box.get('NameU')} missing LineColor")
    for lbl in labels:
        fp, _ = cell(lbl, "FillPattern")
        fg, _ = cell(lbl, "FillForegnd")
        nl, _ = cell(lbl, "NoLine")
        nf, _ = cell(lbl, "NoFill")
        if fp != "1" or (fg or "").lower() != "#ffffff" or nl != "1" or nf == "1" or not own_has_geometry(lbl):
            errors.append(
                f"label {lbl.get('NameU')} mask bad FillPattern={fp} FillForegnd={fg} NoLine={nl} NoFill={nf} geom={own_has_geometry(lbl)}"
            )
    return {
        "path": str(path),
        "top_shapes": len(top_shapes),
        "connectors": len(connectors),
        "connect_rows": len(connects),
        "begin_rows": begin,
        "end_rows": end,
        "boxes": len(boxes),
        "labels": len(labels),
        "ok": not errors,
        "errors": errors,
    }

paths = [Path(p) for p in sys.argv[1:]]
report = [audit(p) for p in paths]
print(json.dumps(report, indent=2, sort_keys=True))
if not all(item["ok"] for item in report):
    sys.exit(1)
