"""Модель данных: слои, фигуры, документ проекта.

Все координаты хранятся в миллиметрах станка: начало координат в левом
нижнем углу рабочего поля, ось Y направлена вверх (как в GRBL).
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass

Point = tuple[float, float]
Polyline = list[Point]

MODE_CUT = "cut"
MODE_FILL = "fill"
MODE_NAMES = {MODE_CUT: "Срез", MODE_FILL: "Гравировка"}

# Палитра цветов слоёв (как в LightBurn: каждый слой — свой цвет)
PALETTE = [
    "#000000", "#0000ff", "#ff0000", "#00b000", "#c0a000", "#ff8000",
    "#00b0b0", "#ff00ff", "#808080", "#0000a0", "#a00000", "#006000",
    "#606000", "#a05000", "#006060", "#a000a0",
]

PROJECT_VERSION = 1


@dataclass
class Layer:
    """Слой: набор параметров резки/гравировки для всех фигур этого цвета."""
    id: int
    name: str
    color: str
    mode: str = MODE_CUT
    speed: float = 1000.0         # мм/мин
    power: float = 50.0           # % от максимальной мощности
    passes: int = 1
    interval: float = 0.1         # мм, шаг строк гравировки
    output: bool = True           # включать ли слой в задание
    constant_power: bool = False  # True — M3 (постоянная), False — M4 (динамическая)


@dataclass
class Shape:
    """Векторная фигура. Геометрия — набор полилиний в локальных координатах (мм),
    смещение (dx, dy) применяется при выводе."""
    layer_id: int
    paths: list[Polyline]
    kind: str = "path"
    dx: float = 0.0
    dy: float = 0.0

    def world_paths(self) -> list[Polyline]:
        return [[(x + self.dx, y + self.dy) for x, y in p] for p in self.paths]

    def bounds(self) -> tuple[float, float, float, float] | None:
        return paths_bounds(self.world_paths())


def is_closed(path: Polyline, eps: float = 1e-6) -> bool:
    return (len(path) > 2 and abs(path[0][0] - path[-1][0]) < eps
            and abs(path[0][1] - path[-1][1]) < eps)


def paths_bounds(paths: list[Polyline]) -> tuple[float, float, float, float] | None:
    xs = [x for p in paths for x, _ in p]
    ys = [y for p in paths for _, y in p]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def make_line(layer_id: int, p1: Point, p2: Point) -> Shape:
    return Shape(layer_id, [[p1, p2]], kind="line")


def make_rect(layer_id: int, x0: float, y0: float, x1: float, y1: float) -> Shape:
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return Shape(layer_id, [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]], kind="rect")


def make_ellipse(layer_id: int, cx: float, cy: float, rx: float, ry: float) -> Shape:
    rx, ry = abs(rx), abs(ry)
    # Шаг аппроксимации ~0.5 мм по дуге, но не меньше 24 сегментов
    perimeter = 2 * math.pi * math.sqrt((rx * rx + ry * ry) / 2)
    n = max(24, min(720, int(perimeter / 0.5)))
    pts = [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n))
           for i in range(n)]
    pts.append(pts[0])
    return Shape(layer_id, [pts], kind="ellipse")


class Document:
    """Проект: размеры рабочего поля, упорядоченный список слоёв (порядок = приоритет) и фигуры."""

    def __init__(self, bed_width: float = 400.0, bed_height: float = 400.0):
        self.bed_width = bed_width
        self.bed_height = bed_height
        self.layers: list[Layer] = []
        self.shapes: list[Shape] = []
        self.add_layer()

    # --- слои ---
    def next_layer_id(self) -> int:
        return max((layer.id for layer in self.layers), default=-1) + 1

    def add_layer(self, color: str | None = None, **params) -> Layer:
        lid = self.next_layer_id()
        if color is None:
            used = {layer.color.lower() for layer in self.layers}
            color = next((c for c in PALETTE if c not in used), PALETTE[lid % len(PALETTE)])
        layer = Layer(id=lid, name=f"C{lid:02d}", color=color, **params)
        self.layers.append(layer)
        return layer

    def layer(self, layer_id: int) -> Layer | None:
        return next((layer for layer in self.layers if layer.id == layer_id), None)

    def layer_by_color(self, color: str) -> Layer | None:
        return next((layer for layer in self.layers if layer.color.lower() == color.lower()), None)

    def remove_layer(self, layer_id: int) -> None:
        self.layers = [layer for layer in self.layers if layer.id != layer_id]
        self.shapes = [s for s in self.shapes if s.layer_id != layer_id]

    def move_layer(self, layer_id: int, delta: int) -> None:
        idx = next(i for i, layer in enumerate(self.layers) if layer.id == layer_id)
        new = max(0, min(len(self.layers) - 1, idx + delta))
        self.layers.insert(new, self.layers.pop(idx))

    def shapes_of(self, layer_id: int) -> list[Shape]:
        return [s for s in self.shapes if s.layer_id == layer_id]

    def bounds(self, shapes: list[Shape] | None = None) -> tuple[float, float, float, float] | None:
        shapes = self.shapes if shapes is None else shapes
        return paths_bounds([p for s in shapes for p in s.world_paths()])

    # --- снимки для отмены действий ---
    def snapshot(self) -> dict:
        """Лёгкий снимок слоёв и фигур. Списки точек не копируются: геометрия фигуры
        после создания не меняется, меняются только слой и смещение."""
        return {
            "layers": [asdict(layer) for layer in self.layers],
            "shapes": [(s.layer_id, s.kind, s.dx, s.dy, s.paths) for s in self.shapes],
        }

    def restore(self, snap: dict) -> None:
        self.layers = [Layer(**d) for d in snap["layers"]]
        self.shapes = [Shape(layer_id=lid, paths=paths, kind=kind, dx=dx, dy=dy)
                       for lid, kind, dx, dy, paths in snap["shapes"]]

    # --- сохранение ---
    def to_dict(self) -> dict:
        return {
            "version": PROJECT_VERSION,
            "bed": [self.bed_width, self.bed_height],
            "layers": [asdict(layer) for layer in self.layers],
            "shapes": [asdict(s) for s in self.shapes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Document:
        doc = cls(*data.get("bed", (400.0, 400.0)))
        doc.layers = [Layer(**d) for d in data.get("layers", [])]
        doc.shapes = []
        for d in data.get("shapes", []):
            d = dict(d)
            d["paths"] = [[tuple(pt) for pt in p] for p in d["paths"]]
            doc.shapes.append(Shape(**d))
        if not doc.layers:
            doc.add_layer()
        return doc

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path: str) -> Document:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


class UndoHistory:
    """История действий для Ctrl+Z / Ctrl+Shift+Z.

    Хранит снимки состояния после каждого действия. Подряд идущие изменения одного
    поля (например, ввод мощности с клавиатуры) с одинаковым merge_key в пределах
    MERGE_WINDOW секунд склеиваются в один шаг."""
    MERGE_WINDOW = 2.0

    def __init__(self, limit: int = 200):
        self.limit = limit
        self._undo: list[tuple[dict, str]] = []   # (предыдущее состояние, название действия)
        self._redo: list[tuple[dict, str]] = []   # (следующее состояние, название действия)
        self.current: dict | None = None
        self._merge_key: str | None = None
        self._merge_time = 0.0

    def reset(self, state: dict) -> None:
        self._undo.clear()
        self._redo.clear()
        self.current = state
        self._merge_key = None

    def commit(self, state: dict, label: str, merge_key: str | None = None) -> bool:
        """Записать новое состояние. Возвращает False, если ничего не изменилось."""
        if state == self.current:
            return False
        now = time.monotonic()
        merge = (merge_key is not None and merge_key == self._merge_key and self._undo
                 and now - self._merge_time < self.MERGE_WINDOW)
        if not merge:
            self._undo.append((self.current, label))
            del self._undo[:-self.limit]
        self._redo.clear()
        self.current = state
        self._merge_key = merge_key
        self._merge_time = now
        return True

    def undo(self) -> dict | None:
        if not self._undo:
            return None
        prev, label = self._undo.pop()
        self._redo.append((self.current, label))
        self.current = prev
        self._merge_key = None
        return prev

    def redo(self) -> dict | None:
        if not self._redo:
            return None
        nxt, label = self._redo.pop()
        self._undo.append((self.current, label))
        self.current = nxt
        self._merge_key = None
        return nxt

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1][1] if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1][1] if self._redo else None


@dataclass
class MachineSettings:
    """Параметры станка (TwoTrees на GRBL 1.1)."""
    bed_width: float = 400.0
    bed_height: float = 400.0
    s_max: int = 1000              # $30 в GRBL
    travel_speed: float = 6000.0   # мм/мин для холостых G0 (информативно) и рамки
    return_home: bool = True       # вернуться в 0,0 после задания
