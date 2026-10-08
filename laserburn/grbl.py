"""Протокол GRBL 1.1 без привязки к Qt: потоковая отправка G-code методом ping-pong
(строка -> ожидание ok/error -> следующая строка), realtime-команды и опрос статуса.

Методы управления (send, start_job, pause, ...) потокобезопасны: они кладут действие
в очередь, а вся работа с портом выполняется в step() в потоке обмена.
"""
from __future__ import annotations

import queue
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

# Realtime-команды GRBL (обрабатываются станком сразу, без ok)
RT_STATUS = b"?"
RT_HOLD = b"!"
RT_RESUME = b"~"
RT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"


class Port(Protocol):
    def write(self, data: bytes) -> int | None: ...
    def readline(self) -> bytes: ...
    def close(self) -> None: ...


@dataclass
class MachineStatus:
    state: str = "Unknown"
    mpos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    wpos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    feed: float = 0.0
    spindle: float = 0.0


_STATUS_RE = re.compile(r"^<(.*)>$")


def parse_status(line: str, wco: tuple[float, float, float]) -> tuple[MachineStatus, tuple[float, float, float]] | None:
    """Разбор отчёта вида <Idle|MPos:1.000,2.000,0.000|FS:0,0|WCO:0,0,0>.
    Возвращает статус и (возможно обновлённое) смещение рабочей СК."""
    m = _STATUS_RE.match(line.strip())
    if not m:
        return None
    parts = m.group(1).split("|")
    st = MachineStatus(state=parts[0].split(":")[0])
    mpos = wpos = None
    for part in parts[1:]:
        key, _, val = part.partition(":")
        nums = [float(v) for v in val.split(",") if v]
        if key == "MPos":
            mpos = tuple((nums + [0, 0, 0])[:3])
        elif key == "WPos":
            wpos = tuple((nums + [0, 0, 0])[:3])
        elif key == "WCO":
            wco = tuple((nums + [0, 0, 0])[:3])
        elif key == "FS" and nums:
            st.feed = nums[0]
            st.spindle = nums[1] if len(nums) > 1 else 0.0
        elif key == "F" and nums:
            st.feed = nums[0]
    if mpos is None and wpos is not None:
        mpos = tuple(w + o for w, o in zip(wpos, wco))
    if mpos is not None and wpos is None:
        wpos = tuple(m_ - o for m_, o in zip(mpos, wco))
    st.mpos = mpos or (0.0, 0.0, 0.0)
    st.wpos = wpos or (0.0, 0.0, 0.0)
    return st, wco


def clean_line(line: str) -> str:
    """Убирает комментарии (; и (...)) и пробелы по краям."""
    line = re.sub(r"\(.*?\)", "", line.split(";", 1)[0])
    return line.strip()


@dataclass
class Callbacks:
    on_line: Callable[[str], None] = lambda s: None            # строка от станка
    on_sent: Callable[[str], None] = lambda s: None            # строка, отправленная на станок
    on_status: Callable[[MachineStatus], None] = lambda s: None
    on_progress: Callable[[int, int], None] = lambda done, total: None
    on_job_done: Callable[[bool, str], None] = lambda ok, msg: None


@dataclass
class _Job:
    lines: list[str]
    sent: int = 0
    acked: int = 0
    paused: bool = False
    started: float = field(default_factory=time.monotonic)


class GrblController:
    STATUS_INTERVAL = 0.25   # с, период опроса '?'
    STOP_RESET_DELAY = 0.4   # с, пауза между feed hold и soft reset при остановке

    def __init__(self, port: Port, callbacks: Callbacks | None = None):
        self.port = port
        self.cb = callbacks or Callbacks()
        self._actions: queue.Queue = queue.Queue()
        self._manual: list[str] = []
        self._job: _Job | None = None
        self._waiting: str | None = None   # строка, на которую ждём ok/error
        self._waiting_is_job = False
        self._last_poll = 0.0
        self._stop_at: float | None = None
        self.wco = (0.0, 0.0, 0.0)
        self.status = MachineStatus()
        # Станок готов принимать строки после приветствия Grbl или первого отчёта о статусе.
        # Платы с автосбросом (CH340 + Arduino) перезагружаются при открытии порта
        # и теряют всё, что пришло до приветствия.
        self.ready = False

    # --- потокобезопасное API ---
    def send(self, line: str) -> None:
        self._actions.put(("send", line))

    def realtime(self, cmd: bytes) -> None:
        self._actions.put(("rt", cmd))

    def start_job(self, lines: list[str]) -> None:
        self._actions.put(("job", lines))

    def pause(self) -> None:
        self._actions.put(("pause", None))

    def resume(self) -> None:
        self._actions.put(("resume", None))

    def stop(self) -> None:
        self._actions.put(("stop", None))

    @property
    def job_running(self) -> bool:
        return self._job is not None

    # --- цикл обмена (вызывается в потоке порта) ---
    def step(self) -> None:
        self._process_actions()
        now = time.monotonic()
        if self._stop_at is not None and now >= self._stop_at:
            self._stop_at = None
            self._waiting = None
            self._write(RT_RESET)
        if now - self._last_poll >= self.STATUS_INTERVAL:
            self._last_poll = now
            self._write(RT_STATUS)
        self._feed()
        raw = self.port.readline()
        if raw:
            self._handle(raw.decode("ascii", errors="replace").strip())

    def _process_actions(self) -> None:
        while True:
            try:
                kind, arg = self._actions.get_nowait()
            except queue.Empty:
                return
            if kind == "send":
                if self._job is None:
                    self._manual.append(arg)
                else:
                    self.cb.on_line("[Задание выполняется: ручная команда отклонена]")
            elif kind == "rt":
                self._write(arg)
            elif kind == "job":
                if self._job is not None:
                    self.cb.on_line("[Задание уже выполняется]")
                    continue
                lines = [c for c in (clean_line(x) for x in arg) if c]
                self._job = _Job(lines)
                self.cb.on_progress(0, len(lines))
            elif kind == "pause" and self._job and not self._job.paused:
                self._job.paused = True
                self._write(RT_HOLD)
            elif kind == "resume" and self._job and self._job.paused:
                self._job.paused = False
                self._write(RT_RESUME)
            elif kind == "stop":
                self._abort("Задание остановлено пользователем")

    def _abort(self, msg: str) -> None:
        """Остановка: feed hold, затем soft reset (без потери позиции), очистка очередей."""
        had_job = self._job is not None
        self._job = None
        self._manual.clear()
        self._write(RT_HOLD)
        self._stop_at = time.monotonic() + self.STOP_RESET_DELAY
        if had_job:
            self.cb.on_job_done(False, msg)

    def _feed(self) -> None:
        if not self.ready or self._waiting is not None or self._stop_at is not None:
            return
        if self._manual:
            line = self._manual.pop(0)
            self._send_line(line, is_job=False)
            return
        job = self._job
        if job is None or job.paused:
            return
        if job.sent < len(job.lines):
            self._send_line(job.lines[job.sent], is_job=True)
            job.sent += 1

    def _send_line(self, line: str, is_job: bool) -> None:
        self._waiting = line
        self._waiting_is_job = is_job
        self._write(line.encode("ascii", errors="replace") + b"\n")
        self.cb.on_sent(line)

    def _write(self, data: bytes) -> None:
        self.port.write(data)

    def _handle(self, line: str) -> None:
        if not line:
            return
        if line.startswith("<"):
            parsed = parse_status(line, self.wco)
            if parsed:
                self.ready = True
                self.status, self.wco = parsed
                self.cb.on_status(self.status)
            return
        self.cb.on_line(line)
        if line == "ok" or line.startswith("error"):
            was_job = self._waiting_is_job
            self._waiting = None
            if was_job and self._job is not None:
                job = self._job
                job.acked += 1
                self.cb.on_progress(job.acked, len(job.lines))
                if line.startswith("error"):
                    self._abort(f"Ошибка станка на строке {job.acked}: {job.lines[job.acked - 1]} -> {line}")
                elif job.acked >= len(job.lines):
                    self._job = None
                    self.cb.on_job_done(True, f"Задание выполнено за {time.monotonic() - job.started:.0f} с")
        elif line.startswith("ALARM"):
            self._waiting = None
            if self._job is not None:
                self._abort(f"Авария станка: {line}")
        elif line.startswith("Grbl"):
            # Приветствие после сброса: всё, что было в буфере станка, потеряно
            self._waiting = None
            self._stop_at = None
            self.ready = True
            if self._job is not None and self._job.sent > 0:
                self._job = None
                self.cb.on_job_done(False, "Станок перезагрузился во время задания")
