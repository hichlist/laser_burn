import math

from laserburn.core import (MODE_FILL, Document, is_closed, make_ellipse, make_line, make_rect)


def test_rect_is_closed_and_normalized():
    s = make_rect(0, 30, 20, 10, 5)
    p = s.paths[0]
    assert is_closed(p)
    assert s.bounds() == (10, 5, 30, 20)


def test_ellipse_points_on_curve():
    s = make_ellipse(0, 50, 50, 20, 10)
    p = s.paths[0]
    assert is_closed(p)
    for x, y in p:
        assert math.isclose(((x - 50) / 20) ** 2 + ((y - 50) / 10) ** 2, 1, rel_tol=1e-9)


def test_offset_applied_to_world_paths():
    s = make_line(0, (0, 0), (10, 0))
    s.dx, s.dy = 5, 7
    assert s.world_paths() == [[(5, 7), (15, 7)]]


def test_layers_get_distinct_colors_and_order():
    doc = Document()
    a = doc.layers[0]
    b = doc.add_layer()
    c = doc.add_layer()
    assert len({a.color, b.color, c.color}) == 3
    doc.move_layer(c.id, -2)
    assert [lyr.id for lyr in doc.layers] == [c.id, a.id, b.id]


def test_remove_layer_removes_its_shapes():
    doc = Document()
    b = doc.add_layer()
    doc.shapes = [make_line(0, (0, 0), (1, 1)), make_line(b.id, (0, 0), (2, 2))]
    doc.remove_layer(b.id)
    assert len(doc.shapes) == 1 and doc.shapes[0].layer_id == 0


def test_save_load_roundtrip(tmp_path):
    doc = Document(300, 200)
    lyr = doc.add_layer("#123456", mode=MODE_FILL, speed=2500, power=33)
    s = make_rect(lyr.id, 0, 0, 10, 10)
    s.dx = 4
    doc.shapes.append(s)
    path = tmp_path / "p.lbrn.json"
    doc.save(str(path))
    loaded = Document.load(str(path))
    assert (loaded.bed_width, loaded.bed_height) == (300, 200)
    assert loaded.layer(lyr.id) == lyr
    assert loaded.shapes[0].world_paths() == s.world_paths()
