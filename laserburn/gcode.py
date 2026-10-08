"""Компилятор G-code для GRBL 1.1 в лазерном режиме ($32=1).

Слои обходятся строго в порядке их приоритета (порядок в документе).
Срез — обход контуров, гравировка — построчная заливка замкнутых контуров
(правило чёт-нечет, двунаправленные проходы).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .core import MODE_FILL, Document, Layer, MachineSettings, Point, Polyline, is_closed


def fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def power_to_s(power_percent: float, s_max: int) -> int:
    return round(max(0.0, min(100.0, power_percent)) * s_max / 100.0)


def fill_segments(paths: list[Polyline], interval: float) -> list[tuple[Point, Point]]:
    """Горизонтальные строки заливки замкнутых контуров (правило чёт-нечет).
    Нечётные строки идут в обратном направлении, чтобы сократить холостые проходы."""
    polys = [p for p in paths if is_closed(p)]
    if not polys or interval <= 0:
        return []
    ys = [y for p in polys for _, y in p]
    y_min, y_max = min(ys), max(ys)
    rows = int(math.floor((y_max - y_min) / interval))
    segments: list[tuple[Point, Point]] = []
    reverse = False
    for k in range(rows + 1):
        y = y_min + interval / 2 + k * interval
        if y >= y_max:
            break
        xs: list[float] = []
        for p in polys:
            for (ax, ay), (bx, by) in zip(p, p[1:]):
                if (ay <= y < by) or (by <= y < ay):
                    xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        row = [((xs[i], y), (xs[i + 1], y)) for i in range(0, len(xs) - 1, 2) if xs[i + 1] - xs[i] > 1e-9]
        if not row:
            continue
        if reverse:
            row = [(b, a) for a, b in reversed(row)]
        segments.extend(row)
        reverse = not reverse
    return segments


def order_paths(paths: list[Polyline], start: Point) -> list[Polyline]:
    """Жадная сортировка контуров «ближайший следующий» для сокращения холостых ходов.
    Открытые линии могут быть пройдены в обратном направлении."""
    rest = [p for p in paths if len(p) >= 2]
    result: list[Polyline] = []
    cur = start
    while rest:
        best_i, best_d, best_rev = 0, math.inf, False
        for i, p in enumerate(rest):
            d = math.dist(cur, p[0])
            if d < best_d:
                best_i, best_d, best_rev = i, d, False
            if not is_closed(p):
                d = math.dist(cur, p[-1])
                if d < best_d:
                    best_i, best_d, best_rev = i, d, True
        p = rest.pop(best_i)
        if best_rev:
            p = list(reversed(p))
        result.append(p)
        cur = p[-1]
    return result


@dataclass
class JobStats:
    cut_length: float = 0.0     # мм
    travel_length: float = 0.0  # мм
    est_seconds: float = 0.0


class GcodeCompiler:
    def __init__(self, doc: Document, settings: MachineSettings):
        self.doc = doc
        self.settings = settings
        self.lines: list[str] = []
        self.stats = JobStats()
        self._pos: Point = (0.0, 0.0)
        self._s: int | None = None
        self._f: float | None = None

    def compile(self) -> list[str]:
        self.lines = ["; LaserBurn Analogue", "G21 ; миллиметры", "G90 ; абсолютные координаты", "M5"]
        for layer in self.doc.layers:
            if layer.output:
                self._layer(layer)
        self.lines.append("M5")
        if self.settings.return_home:
            self._travel((0.0, 0.0))
        self.lines.append("M2")
        return self.lines

    def _layer(self, layer: Layer) -> None:
        paths = [p for s in self.doc.shapes_of(layer.id) for p in s.world_paths()]
        if not paths:
            return
        s_value = power_to_s(layer.power, self.settings.s_max)
        self.lines.append(f"; Слой {layer.name}: {'гравировка' if layer.mode == MODE_FILL else 'срез'}, "
                          f"{fmt(layer.speed)} мм/мин, {fmt(layer.power)}%, проходов {layer.passes}")
        self.lines.append(("M3" if layer.constant_power else "M4") + " S0")
        self._s = 0
        for _ in range(max(1, layer.passes)):
            if layer.mode == MODE_FILL:
                for a, b in fill_segments(paths, layer.interval):
                    self._travel(a)
                    self._cut(b, s_value, layer.speed)
            else:
                for p in order_paths(paths, self._pos):
                    self._travel(p[0])
                    for pt in p[1:]:
                        self._cut(pt, s_value, layer.speed)
        self.lines.append("M5")

    def _travel(self, pt: Point) -> None:
        if math.dist(self._pos, pt) < 1e-6:
            return
        self.lines.append(f"G0 X{fmt(pt[0])} Y{fmt(pt[1])}")
        d = math.dist(self._pos, pt)
        self.stats.travel_length += d
        self.stats.est_seconds += d / self.settings.travel_speed * 60
        self._pos = pt

    def _cut(self, pt: Point, s_value: int, speed: float) -> None:
        d = math.dist(self._pos, pt)
        if d < 1e-6:
            return
        cmd = f"G1 X{fmt(pt[0])} Y{fmt(pt[1])}"
        if s_value != self._s:
            cmd += f" S{s_value}"
            self._s = s_value
        if speed != self._f:
            cmd += f" F{fmt(speed)}"
            self._f = speed
        self.lines.append(cmd)
        self.stats.cut_length += d
        self.stats.est_seconds += d / max(speed, 1e-6) * 60
        self._pos = pt


def compile_document(doc: Document, settings: MachineSettings) -> tuple[list[str], JobStats]:
    c = GcodeCompiler(doc, settings)
    lines = c.compile()
    return lines, c.stats


def frame_gcode(bounds: tuple[float, float, float, float], speed: float) -> list[str]:
    """Обвод габаритов задания с выключенным лазером (проверка положения заготовки)."""
    x0, y0, x1, y1 = bounds
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return ["G90", "M5"] + [f"G1 X{fmt(x)} Y{fmt(y)} F{fmt(speed)}" for x, y in pts]
