import pytest
import asyncio
import os
import subprocess

from agents.tg_transfer.media_utils import ffprobe_metadata


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
