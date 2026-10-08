import re

from laserburn.core import MODE_FILL, Document, MachineSettings, make_line, make_rect
from laserburn.gcode import compile_document, fill_segments, fmt, order_paths, power_to_s


def settings():
    return MachineSettings(s_max=1000, return_home=True)


def test_fmt():
    assert fmt(1.0) == "1"
    assert fmt(1.23456) == "1.235"
    assert fmt(-0.0001) == "0"


def test_power_scaling_and_clamping():
    assert power_to_s(50, 1000) == 500
    assert power_to_s(150, 255) == 255
    assert power_to_s(-5, 1000) == 0


def test_cut_layer_gcode():
    doc = Document()
    lyr = doc.layers[0]
    lyr.speed, lyr.power = 600, 40
    doc.shapes.append(make_rect(lyr.id, 10, 10, 20, 20))
    lines, stats = compile_document(doc, settings())
    assert lines[:4] == ["; LaserBurn Analogue", "G21 ; миллиметры", "G90 ; абсолютные координаты", "M5"]
    assert "M4 S0" in lines
    assert "G0 X10 Y10" in lines
    cuts = [x for x in lines if x.startswith("G1")]
    assert cuts[0] == "G1 X20 Y10 S400 F600"
    assert len(cuts) == 4 and all("S" not in c for c in cuts[1:])
    assert lines[-3:] == ["M5", "G0 X0 Y0", "M2"]
    assert abs(stats.cut_length - 40) < 1e-9


def test_constant_power_uses_m3():
    doc = Document()
    doc.layers[0].constant_power = True
    doc.shapes.append(make_line(0, (0, 0), (5, 0)))
    lines, _ = compile_document(doc, settings())
    assert "M3 S0" in lines and "M4 S0" not in lines


def test_layer_priority_passes_and_output_flag():
    doc = Document()
    a = doc.layers[0]
    b = doc.add_layer(power=80, passes=2)
    c = doc.add_layer(output=False)
    doc.shapes += [make_line(a.id, (0, 0), (1, 0)), make_line(b.id, (5, 5), (6, 5)),
                   make_line(c.id, (9, 9), (9, 8))]
    doc.move_layer(b.id, -1)  # слой b выполняется первым
    lines, _ = compile_document(doc, settings())
    layer_lines = [x for x in lines if x.startswith("; Слой")]
    assert [x.split(":")[0] for x in layer_lines] == [f"; Слой {b.name}", f"; Слой {a.name}"]
    b_block = lines[lines.index(layer_lines[0]):lines.index(layer_lines[1])]
    # два прохода; второй идёт в обратную сторону, т.к. голова уже в конце линии
    assert [x for x in b_block if x.startswith("G1")] == ["G1 X6 Y5 S800 F1000", "G1 X5 Y5"]
    assert not any("X9" in x for x in lines)  # слой c выключен


def test_order_paths_nearest_and_reverse_open():
    far = [(100, 0), (110, 0)]
    near_reversed = [(20, 0), (1, 0)]
    ordered = order_paths([far, near_reversed], (0, 0))
    assert ordered[0] == [(20, 0), (1, 0)][::-1]
    assert ordered[1] == far


def test_fill_square_rows():
    sq = make_rect(0, 0, 0, 10, 10).paths[0]
    segs = fill_segments([sq], 1.0)
    assert len(segs) == 10
    assert segs[0] == ((0, 0.5), (10, 0.5))
    assert segs[1] == ((10, 1.5), (0, 1.5))  # двунаправленно


def test_fill_hole_even_odd():
    outer = make_rect(0, 0, 0, 10, 10).paths[0]
    hole = make_rect(0, 3, 3, 7, 7).paths[0]
    segs = fill_segments([outer, hole], 1.0)
    row5 = [s for s in segs if s[0][1] == 5.5]
    assert len(row5) == 2
    xs = sorted(x for s in row5 for x in (s[0][0], s[1][0]))
    assert xs == [0, 3, 7, 10]


def test_fill_layer_gcode_ignores_open_paths():
    doc = Document()
    doc.layers[0].mode = MODE_FILL
    doc.layers[0].interval = 2
    doc.shapes += [make_rect(0, 0, 0, 10, 4), make_line(0, (50, 50), (60, 60))]
    lines, _ = compile_document(doc, settings())
    cuts = [x for x in lines if x.startswith("G1")]
    assert len(cuts) == 2
    assert not any(re.search(r"X60", x) for x in lines)
