"""Симулятор станка GRBL 1.1: объект с интерфейсом последовательного порта.
Используется для тестов и для режима «SIMULATOR» в списке портов."""
from __future__ import annotations

import re
import threading
import time
from collections import deque

WELCOME = "Grbl 1.1h ['$' for help]"
_WORD_RE = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")


class GrblSimulator:
    def __init__(self, line_delay: float = 0.0, read_timeout: float = 0.02):
        self.line_delay = line_delay      # имитация времени выполнения строки
        self.read_timeout = read_timeout
        self.is_open = True
        self.pos = [0.0, 0.0, 0.0]
        self.wco = [0.0, 0.0, 0.0]
        self.absolute = True
        self.state = "Idle"
        self.feed = 0.0
        self.spindle = 0.0
        self.laser_on = False
        self.hold = False
        self.received: list[str] = []     # все принятые строки (для тестов)
        self._rx = b""
        self._out: deque[str] = deque([WELCOME])
        self._pending: deque[str] = deque()   # строки, ожидающие исполнения
        self._busy_until = 0.0
        self._lock = threading.Lock()

    # --- интерфейс serial.Serial ---
    def write(self, data: bytes) -> int:
        with self._lock:
            for b in data:
                ch = bytes([b])
                if ch == b"?":
                    self._out.append(self._status())
                elif ch == b"!":
                    self.hold = True
                    self.state = "Hold:0"
                elif ch == b"~":
                    self.hold = False
                    self.state = "Idle"
                elif ch == b"\x18":
                    self._reset()
                elif ch == b"\x85":
                    pass
                elif ch == b"\n":
                    line = self._rx.decode("ascii", errors="replace").strip()
                    self._rx = b""
                    if line:
                        self._pending.append(line)
                elif ch != b"\r":
                    self._rx += ch
        return len(data)

    def readline(self) -> bytes:
        deadline = time.monotonic() + self.read_timeout
        while True:
            with self._lock:
                self._execute()
                if self._out:
                    return (self._out.popleft() + "\r\n").encode()
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.002)

    def close(self) -> None:
        self.is_open = False

    # --- исполнение ---
    def _reset(self) -> None:
        self._pending.clear()
        self._rx = b""
        self.hold = False
        self.laser_on = False
        self.state = "Idle"
        self._out.append(WELCOME)

    def _status(self) -> str:
        p = ",".join(f"{v:.3f}" for v in self.pos)
        return f"<{self.state}|MPos:{p}|FS:{self.feed:.0f},{self.spindle:.0f}|WCO:" + \
            ",".join(f"{v:.3f}" for v in self.wco) + ">"

    def _execute(self) -> None:
        now = time.monotonic()
        if self.hold or not self._pending or now < self._busy_until:
            if not self.hold and not self._pending and now >= self._busy_until and self.state == "Run":
                self.state = "Idle"
            return
        line = self._pending.popleft()
        self.received.append(line)
        self._out.append(self._run_line(line))
        self._busy_until = now + self.line_delay
        if self.line_delay:
            self.state = "Run"

    def _run_line(self, line: str) -> str:
        up = line.upper()
        if up.startswith("$"):
            if up == "$H":
                self.pos = [0.0, 0.0, 0.0]
            elif up.startswith("$J="):
                self._motion(up)
            elif up == "$$":
                self._out.extend(["$30=1000", "$31=0", "$32=1", "$130=400.000", "$131=400.000"])
            elif up not in ("$X", "$#", "$G", "$I") and not re.match(r"^\$\d+=", up):
                return "error:3"
            return "ok"
        if "ERR" in up:
            return "error:20"
        self._motion(up)
        return "ok"

    def _motion(self, cmd: str) -> None:
        jog = cmd.startswith("$J=")
        words = dict(_WORD_RE.findall(cmd.replace(" ", "")))
        codes = re.findall(r"([GM])(\d+)", cmd)
        relative = not self.absolute
        for letter, num in codes:
            if (letter, num) in (("G", "90"), ("G", "91")):
                relative = num == "91"
                if not jog:  # jog не меняет модальный режим координат
                    self.absolute = num == "90"
            elif letter == "M" and num in ("3", "4"):
                self.laser_on = True
            elif letter == "M" and num == "5":
                self.laser_on = False
                self.spindle = 0.0
        if ("G", "10") in codes:
            # G10 L20 P1 X0 Y0 — текущая позиция становится рабочим нулём
            for i, axis in enumerate("XYZ"):
                if axis in words:
                    self.wco[i] = self.pos[i] - float(words[axis])
            return
        if "F" in words:
            self.feed = float(words["F"])
        if "S" in words:
            self.spindle = float(words["S"])
        for i, axis in enumerate("XYZ"):
            if axis in words:
                v = float(words[axis])
                self.pos[i] = self.pos[i] + v if relative else v + self.wco[i]
