import asyncio
import json
import logging

logger = logging.getLogger(__name__)


async def ffprobe_metadata(file_path: str) -> dict | None:
    """Extract video metadata using ffprobe.

    Returns {"duration": int, "width": int, "height": int} or None on failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "v:0", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return None

        data = json.loads(stdout)
        streams = data.get("streams", [])
        if not streams:
            return None

        stream = streams[0]
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))

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

        # Duration can be in stream or format level
        duration_str = stream.get("duration")
        if not duration_str:
            # Try format level
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
            return None

        return {"duration": duration, "width": width, "height": height}
    except Exception as e:
        logger.debug(f"ffprobe failed for {file_path}: {e}")
        return None
