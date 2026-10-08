import math

import ezdxf

from laserburn.importers import import_dxf, import_svg


def test_import_svg_mm(tmp_path):
    svg = tmp_path / "a.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50">'
        '<rect x="10" y="10" width="20" height="10" stroke="#ff0000" fill="none"/>'
        '<circle cx="70" cy="25" r="10" stroke="blue" fill="none"/></svg>')
    groups = dict(import_svg(str(svg)))
    rect = groups["#ff0000"][0]
    xs, ys = [p[0] for p in rect], [p[1] for p in rect]
    assert math.isclose(max(xs) - min(xs), 20, abs_tol=1e-3)
    assert math.isclose(max(ys) - min(ys), 10, abs_tol=1e-3)
    assert math.isclose(min(xs), 10, abs_tol=1e-3)
    circle = groups["#0000ff"][0]
    for x, y in circle:
        assert math.isclose(math.hypot(x - 70, y + 25), 10, abs_tol=0.05)  # Y перевёрнута


def test_import_dxf(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4  # мм (ezdxf по умолчанию ставит метры)
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"color": 1})
    msp.add_circle((50, 50), 5, dxfattribs={"color": 7})
    path = tmp_path / "a.dxf"
    doc.saveas(path)
    groups = dict(import_dxf(str(path)))
    assert groups["#ff0000"][0] == [(0, 0), (10, 0)]
    circle = groups["#000000"][0]
    assert all(math.isclose(math.hypot(x - 50, y - 50), 5, abs_tol=0.06) for x, y in circle)
