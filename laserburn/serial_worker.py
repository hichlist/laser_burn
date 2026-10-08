"""Поток обмена со станком (QThread). Вся работа с портом идёт здесь,
интерфейс получает данные только через сигналы."""
from __future__ import annotations

import time

import serial
from PyQt6.QtCore import QThread, pyqtSignal
from serial.tools import list_ports

from .grbl import RT_HOLD, RT_RESET, Callbacks, GrblController, MachineStatus
from .grbl_sim import GrblSimulator

SIMULATOR_PORT = "SIMULATOR"
BAUD_RATES = [115200, 250000, 230400, 57600, 38400, 19200, 9600]


def available_ports() -> list[str]:
    ports = sorted(p.device for p in list_ports.comports())
    return ports + [SIMULATOR_PORT]


class SerialWorker(QThread):
    line_received = pyqtSignal(str)
    line_sent = pyqtSignal(str)
    status_changed = pyqtSignal(object)        # MachineStatus
    progress = pyqtSignal(int, int)
    job_finished = pyqtSignal(bool, str)
    connection_changed = pyqtSignal(bool, str)

    def __init__(self, port: str, baud: int, parent=None):
        super().__init__(parent)
        self.port_name = port
        self.baud = baud
        self.controller: GrblController | None = None
        self._running = False

    def run(self) -> None:
        try:
            if self.port_name == SIMULATOR_PORT:
                port = GrblSimulator(line_delay=0.002)
            else:
                port = serial.Serial(self.port_name, self.baud, timeout=0.05, write_timeout=1)
                # Многие платы (CH340 + Arduino) перезагружаются при открытии порта
                port.reset_input_buffer()
        except (serial.SerialException, OSError) as e:
            self.connection_changed.emit(False, f"Не удалось открыть {self.port_name}: {e}")
            return

        cb = Callbacks(
            on_line=self.line_received.emit,
            on_sent=self.line_sent.emit,
            on_status=self._emit_status,
            on_progress=self.progress.emit,
            on_job_done=self.job_finished.emit,
        )
        self.controller = GrblController(port, cb)
        self._running = True
        self.connection_changed.emit(True, f"Подключено: {self.port_name} @ {self.baud}")
        error = ""
        try:
            while self._running:
                self.controller.step()
        except (serial.SerialException, OSError) as e:
            error = f"Связь потеряна: {e}"
        finally:
            if self.controller.job_running:
                # Закрытие порта во время задания: остановить движение и лазер
                try:
                    port.write(RT_HOLD)
                    time.sleep(GrblController.STOP_RESET_DELAY)
                    port.write(RT_RESET)
                except (serial.SerialException, OSError):
                    pass
                self.job_finished.emit(False, "Соединение закрыто во время задания")
            try:
                port.close()
            except Exception:
                pass
            self.controller = None
            self.connection_changed.emit(False, error or "Отключено")

    def _emit_status(self, st: MachineStatus) -> None:
        self.status_changed.emit(st)

    def shutdown(self) -> None:
        self._running = False
        self.wait(2000)

    # Делегирование потокобезопасным методам контроллера
    def send(self, line: str) -> None:
        if self.controller:
            self.controller.send(line)

    def realtime(self, cmd: bytes) -> None:
        if self.controller:
            self.controller.realtime(cmd)

    def start_job(self, lines: list[str]) -> None:
        if self.controller:
            self.controller.start_job(lines)

    def pause(self) -> None:
        if self.controller:
            self.controller.pause()

    def resume(self) -> None:
        if self.controller:
            self.controller.resume()

    def stop_job(self) -> None:
        if self.controller:
            self.controller.stop()
