"""Боковые панели: слои, управление станком, консоль GRBL."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox,
                             QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                             QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from .core import MODE_CUT, MODE_FILL, MODE_NAMES, Document, Layer
from .grbl import RT_JOG_CANCEL, MachineStatus
from .serial_worker import BAUD_RATES, available_ports


def _centered(w: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(w)
    return box


class _NoWheelMixin:
    """Спинбоксы и списки в таблице не должны менять значение при прокрутке колесом."""
    def wheelEvent(self, e):
        e.ignore()


class _Combo(_NoWheelMixin, QComboBox):
    pass


class _Spin(_NoWheelMixin, QSpinBox):
    pass


class _DSpin(_NoWheelMixin, QDoubleSpinBox):
    pass


class LayerPanel(QWidget):
    COLUMNS = ["Вывод", "Цвет", "Слой", "Режим", "Скорость,\nмм/мин", "Мощность,\n%",
               "Проходы", "Интервал,\nмм", "M3"]

    layers_changed = pyqtSignal()
    current_layer_changed = pyqtSignal(int)
    assign_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeaderItem(8).setToolTip("Постоянная мощность (M3) вместо динамической (M4)")
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemChanged.connect(self._on_item_changed)

        add_btn = QPushButton("+ Слой")
        del_btn = QPushButton("− Слой")
        up_btn = QPushButton("▲")
        down_btn = QPushButton("▼")
        assign_btn = QPushButton("Назначить выделенным")
        up_btn.setToolTip("Поднять приоритет (слой выполняется раньше)")
        down_btn.setToolTip("Опустить приоритет")
        assign_btn.setToolTip("Перенести выделенные объекты на текущий слой")
        add_btn.clicked.connect(self._add)
        del_btn.clicked.connect(self._remove)
        up_btn.clicked.connect(lambda: self._move(-1))
        down_btn.clicked.connect(lambda: self._move(1))
        assign_btn.clicked.connect(lambda: self.assign_requested.emit(self.current_layer_id()))

        buttons = QHBoxLayout()
        for b in (add_btn, del_btn, up_btn, down_btn):
            buttons.addWidget(b)
        buttons.addStretch()
        buttons.addWidget(assign_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(buttons)

    def set_document(self, doc: Document) -> None:
        self.doc = doc
        self.rebuild(doc.layers[0].id if doc.layers else None)

    def current_layer_id(self) -> int:
        row = self.table.currentRow()
        if self.doc and 0 <= row < len(self.doc.layers):
            return self.doc.layers[row].id
        return self.doc.layers[0].id if self.doc and self.doc.layers else 0

    def select_layer(self, layer_id: int) -> None:
        for row, layer in enumerate(self.doc.layers):
            if layer.id == layer_id:
                self.table.selectRow(row)
                return

    def rebuild(self, select_id: int | None = None) -> None:
        if select_id is None:
            select_id = self.current_layer_id()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row, layer in enumerate(self.doc.layers):
            self.table.insertRow(row)
            self._fill_row(row, layer)
        self.table.blockSignals(False)
        self.select_layer(select_id)

    def _fill_row(self, row: int, layer: Layer) -> None:
        def changed(attr, conv=lambda v: v):
            def handler(value):
                setattr(layer, attr, conv(value))
                self.layers_changed.emit()
            return handler

        out = QCheckBox()
        out.setChecked(layer.output)
        out.toggled.connect(changed("output"))
        self.table.setCellWidget(row, 0, _centered(out))

        color_btn = QPushButton()
        color_btn.setFixedSize(36, 20)
        color_btn.setStyleSheet(f"background:{layer.color}; border:1px solid #444;")
        color_btn.clicked.connect(lambda _, l=layer: self._pick_color(l))
        self.table.setCellWidget(row, 1, _centered(color_btn))

        name = QTableWidgetItem(layer.name)
        self.table.setItem(row, 2, name)

        mode = _Combo()
        for key in (MODE_CUT, MODE_FILL):
            mode.addItem(MODE_NAMES[key], key)
        mode.setCurrentIndex(0 if layer.mode == MODE_CUT else 1)
        mode.currentIndexChanged.connect(lambda i, m=mode: changed("mode")(m.itemData(i)))
        self.table.setCellWidget(row, 3, mode)

        speed = _DSpin()
        speed.setRange(1, 100000)
        speed.setDecimals(0)
        speed.setValue(layer.speed)
        speed.valueChanged.connect(changed("speed"))
        self.table.setCellWidget(row, 4, speed)

        power = _DSpin()
        power.setRange(0, 100)
        power.setDecimals(1)
        power.setValue(layer.power)
        power.valueChanged.connect(changed("power"))
        self.table.setCellWidget(row, 5, power)

        passes = _Spin()
        passes.setRange(1, 100)
        passes.setValue(layer.passes)
        passes.valueChanged.connect(changed("passes"))
        self.table.setCellWidget(row, 6, passes)

        interval = _DSpin()
        interval.setRange(0.01, 5)
        interval.setDecimals(3)
        interval.setSingleStep(0.01)
        interval.setValue(layer.interval)
        interval.valueChanged.connect(changed("interval"))
        self.table.setCellWidget(row, 7, interval)

        const = QCheckBox()
        const.setChecked(layer.constant_power)
        const.toggled.connect(changed("constant_power"))
        self.table.setCellWidget(row, 8, _centered(const))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 2 and 0 <= item.row() < len(self.doc.layers):
            self.doc.layers[item.row()].name = item.text().strip() or self.doc.layers[item.row()].name
            self.layers_changed.emit()

    def _on_selection(self) -> None:
        self.current_layer_changed.emit(self.current_layer_id())

    def _pick_color(self, layer: Layer) -> None:
        c = QColorDialog.getColor(QColor(layer.color), self, f"Цвет слоя {layer.name}")
        if c.isValid():
            layer.color = c.name()
            self.rebuild(layer.id)
            self.layers_changed.emit()

    def _add(self) -> None:
        layer = self.doc.add_layer()
        self.rebuild(layer.id)
        self.layers_changed.emit()

    def _remove(self) -> None:
        if len(self.doc.layers) <= 1:
            QMessageBox.information(self, "Слои", "Нельзя удалить последний слой.")
            return
        lid = self.current_layer_id()
        layer = self.doc.layer(lid)
        count = len(self.doc.shapes_of(lid))
        if count and QMessageBox.question(
                self, "Удаление слоя",
                f"На слое «{layer.name}» {count} объект(ов). Удалить слой вместе с ними?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.doc.remove_layer(lid)
        self.rebuild(self.doc.layers[0].id)
        self.layers_changed.emit()

    def _move(self, delta: int) -> None:
        lid = self.current_layer_id()
        self.doc.move_layer(lid, delta)
        self.rebuild(lid)
        self.layers_changed.emit()


class MachinePanel(QWidget):
    connect_requested = pyqtSignal(str, int)
    disconnect_requested = pyqtSignal()
    command = pyqtSignal(str)
    realtime = pyqtSignal(bytes)
    frame_requested = pyqtSignal()
    start_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    stop_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connected = False
        self.job_running = False
        self.paused = False

        # Подключение
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(140)
        refresh = QPushButton("⟳")
        refresh.setToolTip("Обновить список портов")
        refresh.setFixedWidth(30)
        refresh.clicked.connect(self.refresh_ports)
        self.baud_combo = QComboBox()
        for b in BAUD_RATES:
            self.baud_combo.addItem(str(b), b)
        self.connect_btn = QPushButton("Подключить")
        self.connect_btn.clicked.connect(self._toggle_connect)
        conn = QHBoxLayout()
        conn.addWidget(self.port_combo, 1)
        conn.addWidget(refresh)
        conn.addWidget(self.baud_combo)
        conn.addWidget(self.connect_btn)

        self.status_label = QLabel("Не подключено")
        self.status_label.setStyleSheet("font-weight:bold;")
        self.pos_label = QLabel("X: —  Y: —")

        # Перемещение (jog)
        jog_box = QGroupBox("Перемещение")
        grid = QGridLayout(jog_box)
        dirs = {(0, 0): (-1, 1, "↖"), (0, 1): (0, 1, "↑"), (0, 2): (1, 1, "↗"),
                (1, 0): (-1, 0, "←"), (1, 2): (1, 0, "→"),
                (2, 0): (-1, -1, "↙"), (2, 1): (0, -1, "↓"), (2, 2): (1, -1, "↘")}
        self._machine_buttons: list[QWidget] = []
        for (r, c), (dx, dy, text) in dirs.items():
            b = QPushButton(text)
            b.setFixedSize(40, 32)
            b.clicked.connect(lambda _, dx=dx, dy=dy: self._jog(dx, dy))
            grid.addWidget(b, r, c)
            self._machine_buttons.append(b)
        home_btn = QPushButton("⌂")
        home_btn.setFixedSize(40, 32)
        home_btn.setToolTip("Хоминг ($H)")
        home_btn.clicked.connect(lambda: self.command.emit("$H"))
        grid.addWidget(home_btn, 1, 1)
        self._machine_buttons.append(home_btn)

        self.step_combo = QComboBox()
        for s in ("0.1", "1", "5", "10", "50", "100"):
            self.step_combo.addItem(s + " мм", float(s))
        self.step_combo.setCurrentIndex(3)
        self.jog_feed = QSpinBox()
        self.jog_feed.setRange(10, 20000)
        self.jog_feed.setValue(3000)
        self.jog_feed.setSuffix(" мм/мин")
        grid.addWidget(QLabel("Шаг:"), 0, 3)
        grid.addWidget(self.step_combo, 0, 4)
        grid.addWidget(QLabel("Скорость:"), 1, 3)
        grid.addWidget(self.jog_feed, 1, 4)
        cancel_jog = QPushButton("Отмена jog")
        cancel_jog.clicked.connect(lambda: self.realtime.emit(RT_JOG_CANCEL))
        grid.addWidget(cancel_jog, 2, 3, 1, 2)
        self._machine_buttons.append(cancel_jog)

        # Сервисные команды
        svc = QGridLayout()
        actions = [
            ("Разблокировать ($X)", lambda: self.command.emit("$X")),
            ("Задать ноль здесь", lambda: self.command.emit("G10 L20 P1 X0 Y0")),
            ("В ноль", lambda: self.command.emit("G90 G0 X0 Y0")),
            ("Рамка", self.frame_requested.emit),
        ]
        tips = ["Снять блокировку после аварии", "Текущая позиция станет началом координат задания",
                "Переместить голову в рабочий ноль", "Обвести габарит задания с выключенным лазером"]
        for i, ((text, slot), tip) in enumerate(zip(actions, tips)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            svc.addWidget(b, i // 2, i % 2)
            self._machine_buttons.append(b)

        # Задание
        job_box = QGroupBox("Задание")
        job = QVBoxLayout(job_box)
        row = QHBoxLayout()
        self.start_btn = QPushButton("▶ Старт")
        self.pause_btn = QPushButton("⏸ Пауза")
        self.stop_btn = QPushButton("■ Стоп")
        self.start_btn.setStyleSheet("font-weight:bold;")
        self.stop_btn.setStyleSheet("QPushButton:enabled { color:#b00000; } QPushButton { font-weight:bold; }")
        self.start_btn.clicked.connect(self.start_requested.emit)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            row.addWidget(b)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m  (%p%)")
        self.job_label = QLabel("")
        self.job_label.setWordWrap(True)
        job.addLayout(row)
        job.addWidget(self.progress)
        job.addWidget(self.job_label)

        lay = QVBoxLayout(self)
        lay.addLayout(conn)
        lay.addWidget(self.status_label)
        lay.addWidget(self.pos_label)
        lay.addWidget(jog_box)
        lay.addLayout(svc)
        lay.addWidget(job_box)
        lay.addStretch()

        self.refresh_ports()
        self._update_enabled()

    def refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(available_ports())
        if current:
            self.port_combo.setCurrentText(current)

    def set_port(self, port: str, baud: int) -> None:
        if port:
            self.port_combo.setCurrentText(port)
        i = self.baud_combo.findData(baud)
        if i >= 0:
            self.baud_combo.setCurrentIndex(i)

    def _toggle_connect(self) -> None:
        if self.connected:
            self.disconnect_requested.emit()
        else:
            self.connect_btn.setEnabled(False)
            self.connect_requested.emit(self.port_combo.currentText().strip(), self.baud_combo.currentData())

    def _toggle_pause(self) -> None:
        if self.paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()
        self.set_paused(not self.paused)

    def _jog(self, dx: int, dy: int) -> None:
        step = self.step_combo.currentData()
        parts = ["$J=G91 G21"]
        if dx:
            parts.append(f"X{dx * step:g}")
        if dy:
            parts.append(f"Y{dy * step:g}")
        parts.append(f"F{self.jog_feed.value()}")
        self.command.emit(" ".join(parts))

    def set_connected(self, connected: bool, message: str) -> None:
        self.connected = connected
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Отключить" if connected else "Подключить")
        self.status_label.setText(message)
        if not connected:
            self.pos_label.setText("X: —  Y: —")
        self._update_enabled()

    def set_job_running(self, running: bool) -> None:
        self.job_running = running
        if not running:
            self.set_paused(False)
        self._update_enabled()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.pause_btn.setText("▶ Продолжить" if paused else "⏸ Пауза")

    def set_status(self, st: MachineStatus) -> None:
        colors = {"Idle": "#007000", "Run": "#0050c0", "Hold": "#c07000", "Jog": "#0050c0",
                  "Alarm": "#c00000", "Door": "#c00000", "Home": "#0050c0"}
        self.status_label.setText(f"Состояние: {st.state}")
        self.status_label.setStyleSheet(f"font-weight:bold; color:{colors.get(st.state, '#000')};")
        self.pos_label.setText(f"X: {st.wpos[0]:.2f}  Y: {st.wpos[1]:.2f}   "
                               f"(маш. {st.mpos[0]:.2f}, {st.mpos[1]:.2f})")

    def _update_enabled(self) -> None:
        idle = self.connected and not self.job_running
        for b in self._machine_buttons:
            b.setEnabled(idle)
        self.port_combo.setEnabled(not self.connected)
        self.baud_combo.setEnabled(not self.connected)
        self.start_btn.setEnabled(idle)
        self.pause_btn.setEnabled(self.connected and self.job_running)
        self.stop_btn.setEnabled(self.connected)


class ConsolePanel(QWidget):
    command = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setStyleSheet("font-family: monospace;")
        self.input = QLineEdit()
        self.input.setPlaceholderText("Команда GRBL ($$, $I, G0 X10 ...), Enter — отправить")
        self.input.returnPressed.connect(self._send)
        self.input.installEventFilter(self)
        send = QPushButton("Отправить")
        send.clicked.connect(self._send)
        clear = QPushButton("Очистить")
        clear.clicked.connect(self.log.clear)
        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(send)
        row.addWidget(clear)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.log)
        lay.addLayout(row)
        self._history: list[str] = []
        self._hist_pos = 0

    def eventFilter(self, obj, event) -> bool:
        if obj is self.input and event.type() == event.Type.KeyPress and self._history:
            if event.key() == Qt.Key.Key_Up:
                self._hist_pos = max(0, self._hist_pos - 1)
                self.input.setText(self._history[self._hist_pos])
                return True
            if event.key() == Qt.Key.Key_Down:
                self._hist_pos = min(len(self._history), self._hist_pos + 1)
                self.input.setText(self._history[self._hist_pos] if self._hist_pos < len(self._history) else "")
                return True
        return super().eventFilter(obj, event)

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self._history.append(text)
        self._hist_pos = len(self._history)
        self.input.clear()
        self.command.emit(text)

    def append(self, text: str) -> None:
        self.log.appendPlainText(text)
