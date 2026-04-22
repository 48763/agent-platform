import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)


async def ffprobe_metadata(file_path: str) -> dict | None:
    """Extract video metadata using ffprobe.

    Returns {"duration": int, "width": int, "height": int} or None on failure.

    Hardened against weird sources:
      - falls back to coded_width/coded_height when the stream omits
        display dimensions (some HEVC / mobile-recorded MOVs do this).
      - duration is searched in the v:0 stream first, then format level,
        then the entire stream list — `-select_streams v:0` can produce a
        stream with no duration set even when format.duration has it.
      - logs at WARNING when a video file fails probing so production logs
        surface "video uploaded as document" cases instead of silently
        dropping the attribute set.
    """
    try:
        # Probe the first video stream first. We use stderr=PIPE in case
        # we need to inspect failures.
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "v:0", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            logger.warning(
                f"ffprobe v:0 failed for {file_path} (rc={proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[:200]}"
            )
            return None

        data = json.loads(stdout)
        streams = data.get("streams", [])
        if not streams:
            logger.warning(f"ffprobe found no v:0 stream in {file_path}")
            return None

        stream = streams[0]
        width = int(stream.get("width") or stream.get("coded_width") or 0)
        height = int(stream.get("height") or stream.get("coded_height") or 0)

        # Apply rotation from side_data if present. ffprobe reports coded
        # width/height; TG does not auto-rotate by metadata, so we must swap.
        rotation = 0
        for sd in stream.get("side_data_list", []) or []:
            if "rotation" in sd:
                try:
                    rotation = int(sd["rotation"])
                except (TypeError, ValueError):
                    rotation = 0
                break
        if rotation % 180 != 0:
            width, height = height, width

        # Duration can live in v:0, the format header, or another stream.
        duration_str = stream.get("duration")
        if not duration_str:
            # Try format level (covers most containers)
            proc2 = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=30)
            if proc2.returncode == 0:
                fmt = json.loads(stdout2).get("format", {})
                duration_str = fmt.get("duration")

        duration = int(float(duration_str)) if duration_str else 0

        if width == 0 or height == 0:
            logger.warning(
                f"ffprobe parsed {file_path} but width/height still 0 "
                f"(stream keys: {list(stream.keys())})"
            )
            return None

        return {"duration": duration, "width": width, "height": height}
    except Exception as e:
        logger.warning(f"ffprobe exception for {file_path}: {e}")
        return None


async def extract_video_thumb(
    file_path: str, out_path: str, at_seconds: float = 1.0,
    max_dim: int = 320,
) -> str | None:
    """Pull a single JPEG frame from `file_path` and write it to `out_path`,
    scaled so neither side exceeds `max_dim` (TG rejects oversized thumbs).
    Used as a fallback when the source message has no TG-attached thumb —
    common for `Send as file` videos. Returns `out_path` on success or None
    if ffmpeg failed.

    `at_seconds` defaults to 1s in. Capturing the very first frame often
    yields a black/keyframe-padding frame that makes the in-feed preview
    look broken; 1s is far enough in to land on real content for most
    short clips while still working for sub-1s clips (ffmpeg clamps to
    end-of-stream).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", str(at_seconds), "-i", file_path,
            "-frames:v", "1",
            # Downscale ONLY when the source is larger than max_dim. The
            # `-2` makes the other dimension auto-compute while keeping
            # aspect ratio and even-numbered (JPEG encoder requirement).
            "-vf", (
                f"scale='if(gt(iw,ih),min({max_dim},iw),-2)':"
                f"'if(gt(ih,iw),min({max_dim},ih),-2)'"
            ),
            "-q:v", "5",
            out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        logger.warning(
            f"extract_video_thumb failed for {file_path} (rc={proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace')[:200]}"
        )
    except Exception as e:
        logger.warning(f"extract_video_thumb exception for {file_path}: {e}")
    return None
