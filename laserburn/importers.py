"""Импорт векторной графики (SVG, DXF) в полилинии в мм.

Каждый импортёр возвращает список (цвет '#rrggbb' или None, [полилинии]).
Ось Y приводится к системе станка (вверх)."""
from __future__ import annotations

import math
import os

from .core import Polyline

ImportedGroup = tuple[str | None, list[Polyline]]

SVG_PPI = 96.0
FLATTEN_TOLERANCE = 0.05  # мм — максимальное отклонение хорды от кривой


def import_file(path: str) -> list[ImportedGroup]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        return import_svg(path)
    if ext == ".dxf":
        return import_dxf(path)
    raise ValueError(f"Неподдерживаемый формат: {ext}")


def _dedupe(poly: Polyline) -> Polyline:
    out: Polyline = []
    for pt in poly:
        if not out or math.dist(out[-1], pt) > 1e-6:
            out.append(pt)
    return out


def import_svg(path: str) -> list[ImportedGroup]:
    try:
        import svgelements as se
    except ImportError as e:
        raise RuntimeError("Для импорта SVG установите пакет svgelements") from e

    k = 25.4 / SVG_PPI  # px -> мм
    svg = se.SVG.parse(path, reify=True, ppi=SVG_PPI)
    groups: dict[str | None, list[Polyline]] = {}
    for el in svg.elements():
        if not isinstance(el, se.Shape) or isinstance(el, se.Group):
            continue
        color = None
        for c in (el.stroke, el.fill):
            if c is not None and c.value is not None and c.alpha != 0:
                color = c.hexrgb
                break
        polys: list[Polyline] = []
        cur: Polyline = []
        for seg in se.Path(el).segments():
            if isinstance(seg, se.Move):
                if len(cur) > 1:
                    polys.append(cur)
                cur = [(seg.end.x * k, -seg.end.y * k)]
            elif isinstance(seg, (se.Line, se.Close)):
                if seg.start is None or seg.end is None:
                    continue
                if not cur:
                    cur = [(seg.start.x * k, -seg.start.y * k)]
                cur.append((seg.end.x * k, -seg.end.y * k))
            else:
                if not cur:
                    cur = [(seg.start.x * k, -seg.start.y * k)]
                length = seg.length(error=1e-3) * k
                n = max(4, min(500, int(length / 0.3)))
                for i in range(1, n + 1):
                    p = seg.point(i / n)
                    cur.append((p.x * k, -p.y * k))
        if len(cur) > 1:
            polys.append(cur)
        polys = [p for p in (_dedupe(p) for p in polys) if len(p) > 1]
        if polys:
            groups.setdefault(color, []).extend(polys)
    return list(groups.items())


def import_dxf(path: str) -> list[ImportedGroup]:
    try:
        import ezdxf
        from ezdxf import colors as dxf_colors
        from ezdxf import path as dxf_path
    except ImportError as e:
        raise RuntimeError("Для импорта DXF установите пакет ezdxf") from e

    doc = ezdxf.readfile(path)
    units = doc.header.get("$INSUNITS", 4)
    scale = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}.get(units, 1.0)
    groups: dict[str | None, list[Polyline]] = {}
    supported = {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE", "HATCH", "SOLID"}
    for e in doc.modelspace():
        if e.dxftype() not in supported:
            continue
        try:
            p = dxf_path.make_path(e)
        except (TypeError, ValueError):
            continue
        color = None
        aci = e.dxf.get("color", 256)
        if e.rgb is not None:
            color = "#%02x%02x%02x" % tuple(e.rgb)
        elif aci == 7:
            color = "#000000"  # ACI 7 — «чёрный/белый» в зависимости от фона
        elif 1 <= aci <= 255:
            color = "#%02x%02x%02x" % tuple(dxf_colors.aci2rgb(aci))
        for sub in p.sub_paths():
            pts = _dedupe([(v.x * scale, v.y * scale) for v in sub.flattening(FLATTEN_TOLERANCE / scale)])
            if len(pts) > 1:
                groups.setdefault(color, []).append(pts)
    return list(groups.items())
