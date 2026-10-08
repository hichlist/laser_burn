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


def test_undo_history_basic():
    from laserburn.core import UndoHistory
    h = UndoHistory()
    h.reset({"v": 0})
    assert h.commit({"v": 1}, "раз")
    assert not h.commit({"v": 1}, "без изменений")
    assert h.commit({"v": 2}, "два")
    assert h.undo_label == "два"
    assert h.undo() == {"v": 1} and h.redo_label == "два"
    assert h.undo() == {"v": 0} and h.undo() is None
    assert h.redo() == {"v": 1}
    h.commit({"v": 5}, "новое")  # новое действие сбрасывает повтор
    assert h.redo() is None and h.undo() == {"v": 1}


def test_undo_history_merge_and_limit():
    from laserburn.core import UndoHistory
    h = UndoHistory(limit=3)
    h.reset({"v": 0})
    for v in (10, 20, 30):
        h.commit({"v": v}, "мощность", merge_key="layer:0:power")
    assert h.undo() == {"v": 0} and h.undo() is None
    h.reset({"v": 0})
    for v in range(1, 10):
        h.commit({"v": v}, f"шаг {v}")
    assert [h.undo()["v"] for _ in range(3)] == [8, 7, 6]
    assert h.undo() is None


def test_snapshot_restore_shares_geometry():
    doc = Document()
    s = make_rect(0, 0, 0, 10, 10)
    doc.shapes.append(s)
    snap = doc.snapshot()
    s.dx = 50
    doc.layers[0].power = 99
    doc.restore(snap)
    assert doc.shapes[0].dx == 0 and doc.layers[0].power == 50
    assert doc.shapes[0].paths is s.paths
