"""Дымовой тест интерфейса в offscreen-режиме, включая работу через симулятор."""
import time

import pytest
from PyQt6.QtCore import QCoreApplication, QSettings
from PyQt6.QtWidgets import QApplication

from laserburn.core import make_rect
from laserburn.gui import MainWindow
from laserburn.serial_worker import SIMULATOR_PORT


@pytest.fixture
def win(tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("LaserBurnTest")
    w = MainWindow()
    w.show()
    yield w
    w.modified = False
    w.close()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_draw_and_recolor(win):
    shape = make_rect(0, 10, 10, 50, 40)
    win._on_shape_created(shape)
    assert win.doc.shapes == [shape] and win.modified
    layer = win.doc.add_layer("#00ff00")
    win.layers.rebuild(layer.id)
    win._assign_layer(layer.id)  # ничего не выделено — без изменений
    assert shape.layer_id == 0
    win.canvas.select_all()
    win._assign_layer(layer.id)
    assert shape.layer_id == layer.id
    item = win.canvas._items[0]
    assert item.pen().color().name() == "#00ff00"
    win.delete_selected()
    assert win.doc.shapes == []


def test_job_on_simulator(win):
    win._on_shape_created(make_rect(0, 10, 10, 30, 30))
    win.connect_machine(SIMULATOR_PORT, 115200)
    assert wait_for(lambda: win.machine.connected)
    win.start_job()
    assert win.job_running
    assert wait_for(lambda: not win.job_running)
    assert "выполнено" in win.machine.job_label.text()
    assert wait_for(lambda: win.machine.pos_label.text().startswith("X: 0.00"))
    win.disconnect_machine()
    assert wait_for(lambda: win.worker is None)
