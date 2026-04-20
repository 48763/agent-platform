import pytest
import asyncio
import json
import os
import subprocess
from unittest.mock import AsyncMock, patch

from agents.tg_transfer.media_utils import ffprobe_metadata


def _fake_ffprobe(stdout_json: dict, returncode: int = 0):
    """Build a mock for asyncio.create_subprocess_exec that returns the given stdout."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(json.dumps(stdout_json).encode(), b""))
    return AsyncMock(return_value=proc)


def _has_ffprobe():
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True)
        return True
    except FileNotFoundError:
        return False


@pytest.fixture
def sample_video(tmp_path):
    """Create a minimal MP4 with ffmpeg for testing."""
    path = str(tmp_path / "test.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        "color=c=black:s=320x240:d=2", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", path,
    ], capture_output=True)
    return path


@pytest.mark.skipif(not _has_ffprobe(), reason="ffprobe not installed")
@pytest.mark.asyncio
async def test_ffprobe_metadata(sample_video):
    meta = await ffprobe_metadata(sample_video)
    assert meta is not None
    assert meta["width"] == 320
    assert meta["height"] == 240
    assert meta["duration"] >= 1


@pytest.mark.asyncio
async def test_ffprobe_metadata_nonexistent():
    meta = await ffprobe_metadata("/tmp/does_not_exist.mp4")
    assert meta is None


@pytest.mark.skipif(not _has_ffprobe(), reason="ffprobe not installed")
@pytest.mark.asyncio
async def test_ffprobe_metadata_non_video(tmp_path):
    path = str(tmp_path / "text.txt")
    with open(path, "w") as f:
        f.write("not a video")
    meta = await ffprobe_metadata(path)
    assert meta is None


@pytest.mark.asyncio
async def test_ffprobe_metadata_rotation_90_swaps_wh(tmp_path):
    """豎拍橫存：原始 1920x1080 + rotate=90 → TG 顯示豎屏，應回傳 1080x1920。"""
    path = str(tmp_path / "vid.mp4")
    open(path, "w").close()
    fake = _fake_ffprobe({
        "streams": [{
            "width": 1920, "height": 1080, "duration": "10.0",
            "side_data_list": [{"rotation": 90}],
        }],
    })
    with patch("asyncio.create_subprocess_exec", fake):
        meta = await ffprobe_metadata(path)
    assert meta == {"duration": 10, "width": 1080, "height": 1920}


@pytest.mark.asyncio
async def test_ffprobe_metadata_rotation_neg90_swaps_wh(tmp_path):
    """橫拍豎存：原始 1080x1920 + rotate=-90 → TG 顯示橫屏，應回傳 1920x1080。"""
    path = str(tmp_path / "vid.mp4")
    open(path, "w").close()
    fake = _fake_ffprobe({
        "streams": [{
            "width": 1080, "height": 1920, "duration": "5.0",
            "side_data_list": [{"rotation": -90}],
        }],
    })
    with patch("asyncio.create_subprocess_exec", fake):
        meta = await ffprobe_metadata(path)
    assert meta == {"duration": 5, "width": 1920, "height": 1080}


@pytest.mark.asyncio
async def test_ffprobe_metadata_rotation_270_swaps_wh(tmp_path):
    """rotation=270 等同 -90，一樣要 swap。"""
    path = str(tmp_path / "vid.mp4")
    open(path, "w").close()
    fake = _fake_ffprobe({
        "streams": [{
            "width": 1080, "height": 1920, "duration": "3.0",
            "side_data_list": [{"rotation": 270}],
        }],
    })
    with patch("asyncio.create_subprocess_exec", fake):
        meta = await ffprobe_metadata(path)
    assert meta == {"duration": 3, "width": 1920, "height": 1080}


@pytest.mark.asyncio
async def test_ffprobe_metadata_no_rotation_keeps_wh(tmp_path):
    """沒 rotation metadata：w/h 原樣回傳。"""
    path = str(tmp_path / "vid.mp4")
    open(path, "w").close()
    fake = _fake_ffprobe({
        "streams": [{"width": 1280, "height": 720, "duration": "20.0"}],
    })
    with patch("asyncio.create_subprocess_exec", fake):
        meta = await ffprobe_metadata(path)
    assert meta == {"duration": 20, "width": 1280, "height": 720}


@pytest.mark.asyncio
async def test_ffprobe_metadata_rotation_180_no_swap(tmp_path):
    """rotation=180 不改變 aspect ratio，w/h 不 swap。"""
    path = str(tmp_path / "vid.mp4")
    open(path, "w").close()
    fake = _fake_ffprobe({
        "streams": [{
            "width": 1920, "height": 1080, "duration": "8.0",
            "side_data_list": [{"rotation": 180}],
        }],
    })
    with patch("asyncio.create_subprocess_exec", fake):
        meta = await ffprobe_metadata(path)
    assert meta == {"duration": 8, "width": 1920, "height": 1080}
