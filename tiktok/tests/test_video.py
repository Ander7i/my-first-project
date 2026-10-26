"""Тесты проверки файла и разбиения на части по правилам TikTok."""

from __future__ import annotations

from itertools import pairwise

import pytest
from fakes import box, mp4_bytes, mvhd, write_mp4

from tiktok_manager import video as video_module
from tiktok_manager.video import (
    CHUNK_SIZE,
    VideoError,
    inspect_video,
    mp4_duration,
    plan_chunks,
)

MB = 1_000_000


def assert_covers(plan):
    """Части идут подряд, без дыр и наложений, и покрывают весь файл."""
    ranges = list(plan.ranges())
    assert len(ranges) == plan.count
    assert ranges[0][0] == 0
    assert ranges[-1][1] == plan.size - 1
    for (_, last), (first, _) in pairwise(ranges):
        assert first == last + 1


@pytest.mark.parametrize("size", [1, 4 * MB, CHUNK_SIZE])
def test_small_file_goes_in_one_piece(size):
    plan = plan_chunks(size)
    assert (plan.chunk_size, plan.count) == (size, 1)
    assert list(plan.ranges()) == [(0, size - 1)]


def test_remainder_goes_to_last_chunk():
    plan = plan_chunks(25 * MB)
    assert (plan.chunk_size, plan.count) == (CHUNK_SIZE, 2)
    assert list(plan.ranges()) == [(0, CHUNK_SIZE - 1), (CHUNK_SIZE, 25 * MB - 1)]


def test_just_over_one_chunk():
    """count = size // chunk_size: 10 МБ + 1 байт — одна часть, а не две."""
    plan = plan_chunks(CHUNK_SIZE + 1)
    assert plan.count == 1
    assert_covers(plan)


@pytest.mark.parametrize("size", [CHUNK_SIZE * 3, CHUNK_SIZE * 3 + 7, 123_456_789])
def test_chunks_cover_file(size):
    assert_covers(plan_chunks(size))


def test_largest_file_fits_tiktok_limits():
    """Части 5–64 МБ, последняя до 128 МБ, всего не больше 1000."""
    size = video_module.MAX_SIZE
    plan = plan_chunks(size)
    assert plan.count <= 1000
    sizes = [last - first + 1 for first, last in plan.ranges()]
    assert all(5 * 1024**2 <= part <= 64 * MB for part in sizes[:-1])
    assert sizes[-1] <= 128 * MB
    assert_covers(plan)


def test_source_info_matches_plan():
    plan = plan_chunks(25 * MB)
    assert plan.source_info() == {
        "source": "FILE_UPLOAD",
        "video_size": 25 * MB,
        "chunk_size": CHUNK_SIZE,
        "total_chunk_count": 2,
    }


def test_duration_when_moov_first(tmp_path):
    assert mp4_duration(write_mp4(tmp_path / "a.mp4", 42.5)) == pytest.approx(42.5)


def test_duration_when_moov_last(tmp_path):
    """Без faststart moov лежит в конце, после данных."""
    path = write_mp4(tmp_path / "a.mp4", 12, moov_last=True, padding=200_000)
    assert mp4_duration(path) == pytest.approx(12)


def test_duration_version_1_header(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(box(b"ftyp", b"isom") + box(b"moov", mvhd(600, 600 * 90, version=1)))
    assert mp4_duration(path) == pytest.approx(90)


def test_duration_after_large_box(tmp_path):
    """Блок с 64-битным размером (size == 1) не должен сбивать разбор."""
    payload = bytes(100)
    large = (1).to_bytes(4, "big") + b"mdat" + (16 + len(payload)).to_bytes(8, "big") + payload
    path = tmp_path / "a.mp4"
    path.write_bytes(box(b"ftyp", b"isom") + large + box(b"moov", mvhd(1000, 30_000)))
    assert mp4_duration(path) == pytest.approx(30)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not an mp4 at all",
        box(b"ftyp", b"isom"),  # нет moov
        box(b"moov", mvhd(0, 1000)),  # нулевой timescale
        box(b"moov", mvhd(1000, 0xFFFFFFFF)),  # «длительность неизвестна»
        mp4_bytes(10)[:40],  # обрезанный файл
    ],
)
def test_duration_unknown(tmp_path, content):
    path = tmp_path / "a.mp4"
    path.write_bytes(content)
    assert mp4_duration(path) is None


def test_inspect_mp4(tmp_path):
    path = write_mp4(tmp_path / "Клип.MP4", 20)
    info = inspect_video(path)
    assert info.mime == "video/mp4"
    assert info.size == path.stat().st_size
    assert info.duration == pytest.approx(20)
    assert info.path.is_absolute()


def test_inspect_webm_has_no_duration(tmp_path):
    path = tmp_path / "clip.webm"
    path.write_bytes(b"\x1a\x45\xdf\xa3" + bytes(100))
    info = inspect_video(path)
    assert info.mime == "video/webm"
    assert info.duration is None


def test_inspect_rejects_missing_file(tmp_path):
    with pytest.raises(VideoError, match="не найден"):
        inspect_video(tmp_path / "nope.mp4")


def test_inspect_rejects_other_formats(tmp_path):
    path = tmp_path / "clip.avi"
    path.write_bytes(b"data")
    with pytest.raises(VideoError, match="MP4, MOV и WebM"):
        inspect_video(path)


def test_inspect_rejects_empty_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"")
    with pytest.raises(VideoError, match="пустой"):
        inspect_video(path)


def test_inspect_rejects_too_big_file(tmp_path, monkeypatch):
    monkeypatch.setattr(video_module, "MAX_SIZE", 100)
    path = write_mp4(tmp_path / "clip.mp4")
    with pytest.raises(VideoError, match="4 ГБ"):
        inspect_video(path)
