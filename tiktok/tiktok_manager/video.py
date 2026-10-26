"""Проверка файла ролика перед загрузкой и разбиение его на части."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# Форматы, которые принимает TikTok.
MIME_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
MAX_SIZE = 4 * 1024**3

# TikTok принимает части от 5 до 64 МБ (последнюю — до 128 МБ). 10 000 000 байт
# укладываются в эти рамки, как ни считай мегабайт — по 1000 или по 1024.
CHUNK_SIZE = 10_000_000

# Так в mvhd записывают «длительность неизвестна».
_UNKNOWN_DURATION = {0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}


class VideoError(RuntimeError):
    """Файл не годится для публикации."""


@dataclass(frozen=True)
class VideoFile:
    path: Path
    size: int
    mime: str
    duration: float | None  # секунды; None — определить не удалось


@dataclass(frozen=True)
class ChunkPlan:
    size: int
    chunk_size: int
    count: int

    def ranges(self) -> Iterator[tuple[int, int]]:
        """Границы частей: первый и последний байт, включительно."""
        for index in range(self.count):
            first = index * self.chunk_size
            last = self.size - 1 if index == self.count - 1 else first + self.chunk_size - 1
            yield first, last

    def source_info(self) -> dict[str, object]:
        return {
            "source": "FILE_UPLOAD",
            "video_size": self.size,
            "chunk_size": self.chunk_size,
            "total_chunk_count": self.count,
        }


def inspect_video(path: str | Path) -> VideoFile:
    path = Path(path).expanduser()
    if not path.is_file():
        raise VideoError(f"Файл не найден: {path}")
    mime = MIME_TYPES.get(path.suffix.lower())
    if mime is None:
        raise VideoError(f"{path.name}: TikTok принимает только MP4, MOV и WebM.")
    size = path.stat().st_size
    if size == 0:
        raise VideoError(f"{path.name}: файл пустой.")
    if size > MAX_SIZE:
        raise VideoError(f"{path.name}: файл больше 4 ГБ, TikTok такой не примет.")
    # У WebM другой контейнер; его длительность проверит сам TikTok.
    duration = None if mime == "video/webm" else mp4_duration(path)
    return VideoFile(path=path.resolve(), size=size, mime=mime, duration=duration)


def plan_chunks(size: int, chunk_size: int = CHUNK_SIZE) -> ChunkPlan:
    """Делит файл на части по правилам TikTok.

    Файл не больше одной части уходит целиком. Иначе частей size // chunk_size
    (с округлением вниз), а остаток дописывается в последнюю часть.
    """
    if size <= chunk_size:
        return ChunkPlan(size=size, chunk_size=size, count=1)
    return ChunkPlan(size=size, chunk_size=chunk_size, count=size // chunk_size)


def mp4_duration(path: str | Path) -> float | None:
    """Длительность MP4 или MOV по заголовку mvhd. None — если не нашлась.

    Свой разбор вместо ffprobe: ничего не нужно ставить, а заголовок простой.
    Блок moov бывает и в начале файла, и в конце — ищем по всему файлу.
    """
    try:
        with open(path, "rb") as file:
            end = os.fstat(file.fileno()).st_size
            moov = _find_box(file, 0, end, b"moov")
            if moov is None:
                return None
            mvhd = _find_box(file, moov[0], moov[1], b"mvhd")
            if mvhd is None:
                return None
            file.seek(mvhd[0])
            header = file.read(min(32, mvhd[1] - mvhd[0]))
    except OSError:
        return None

    if len(header) < 20:
        return None
    if header[0] == 1:
        # Версия 1: время создания и изменения по 8 байт, длительность — 8.
        if len(header) < 32:
            return None
        timescale = int.from_bytes(header[20:24], "big")
        duration = int.from_bytes(header[24:32], "big")
    else:
        timescale = int.from_bytes(header[12:16], "big")
        duration = int.from_bytes(header[16:20], "big")
    if not timescale or not duration or duration in _UNKNOWN_DURATION:
        return None
    return duration / timescale


def _find_box(file: BinaryIO, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    """Ищет блок нужного типа среди блоков одного уровня в [start, end).

    Возвращает границы его содержимого — без заголовка.
    """
    position = start
    while position + 8 <= end:
        file.seek(position)
        header = file.read(8)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[:4], "big")
        header_size = 8
        if size == 1:
            # Размер не влез в 4 байта и записан следом, в 8.
            extended = file.read(8)
            if len(extended) < 8:
                return None
            size = int.from_bytes(extended, "big")
            header_size = 16
        elif size == 0:
            # Блок до конца файла.
            size = end - position
        if size < header_size:
            return None  # битый файл
        if header[4:8] == kind:
            return position + header_size, min(position + size, end)
        position += size
    return None
