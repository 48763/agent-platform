import hashlib
import io
import logging
import asyncio
import os
import uuid

from agents.tg_transfer.media_utils import ffprobe_metadata

logger = logging.getLogger(__name__)

# Relative positions (as fractions of duration) at which the three frames
# used for video phash are sampled. 10/50/90% dodges the dead-zones at the
# very start (logos, black intro) and end (fade-out, end cards) while
# giving three reasonably independent samples across the body of the clip.
_VIDEO_FRAME_FRACTIONS = (0.1, 0.5, 0.9)

# Videos shorter than this many seconds don't give three independent
# samples, so we fall back to a single frame at the midpoint and let
# comparison treat the result as lower confidence via
# `hamming_distance_multi` (total=1).
_MULTI_FRAME_MIN_DURATION = 3

try:
    import imagehash
    from PIL import Image
    PHASH_AVAILABLE = True
except ImportError:
    PHASH_AVAILABLE = False
    logger.warning("imagehash/Pillow not available, pHash disabled")


def compute_sha256(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_phash(file_path: str) -> str | None:
    """Compute perceptual hash of an image file. Returns 16-char hex string or None."""
    if not PHASH_AVAILABLE:
        return None
    try:
        img = Image.open(file_path)
        return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"pHash failed for {file_path}: {e}")
        return None


async def _phash_single_frame(
    file_path: str, at_seconds: float, frame_path: str,
) -> str | None:
    """Extract exactly one frame at `at_seconds` and return its phash, or
    None if ffmpeg or hashing failed. Helper for both the multi-frame
    video flow and its short-video fallback."""
    if not PHASH_AVAILABLE:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", file_path, "-ss", str(at_seconds),
            "-frames:v", "1", "-y", frame_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and os.path.exists(frame_path):
            result = compute_phash(frame_path)
            try:
                os.remove(frame_path)
            except OSError:
                pass
            return result
    except Exception as e:
        logger.debug(f"Single-frame phash failed for {file_path}@{at_seconds}s: {e}")
    return None


async def compute_phash_video(file_path: str, tmp_dir: str) -> str | None:
    """Return a CSV of 3 per-frame phashes sampled at 10% / 50% / 90% of
    the video, e.g. `"h1,h2,h3"`. Used by the dedup path — callers store
    this string verbatim in `media.phash` and compare via
    `hamming_distance_multi`, which counts per-frame matches and reports
    `(matched, total)` so the caller can decide auto-skip (all three
    match) vs ambiguous (two of three match) vs different.

    Short videos (< `_MULTI_FRAME_MIN_DURATION`s) don't give three
    independent samples, so we fall back to a single mid-point frame —
    the returned string has no commas and comparison treats it as
    `total=1` (lower confidence, always surfaces to the ambiguous queue
    rather than auto-skipping).

    Returns None if duration can't be probed, ffmpeg fails on any
    frame, or phash dependencies are missing. A partial CSV would be
    indistinguishable from a short-video fallback, so we never return
    one."""
    if not PHASH_AVAILABLE:
        return None
    meta = await ffprobe_metadata(file_path)
    if not meta:
        return None
    duration = meta.get("duration") or 0
    if duration <= 0:
        return None

    if duration < _MULTI_FRAME_MIN_DURATION:
        # Single-frame fallback at the midpoint — most likely to avoid
        # black intro/outro frames for very short clips.
        frame_path = os.path.join(
            tmp_dir, f"{uuid.uuid4().hex[:8]}.frame.jpg",
        )
        return await _phash_single_frame(file_path, duration / 2, frame_path)

    parts: list[str] = []
    for fraction in _VIDEO_FRAME_FRACTIONS:
        at_seconds = duration * fraction
        frame_path = os.path.join(
            tmp_dir, f"{uuid.uuid4().hex[:8]}.frame.jpg",
        )
        h = await _phash_single_frame(file_path, at_seconds, frame_path)
        if h is None:
            return None
        parts.append(h)
    return ",".join(parts)


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")


def hamming_distance_multi(
    phash_a: str, phash_b: str, per_frame_threshold: int,
) -> tuple[int, int]:
    """Compare two multi-frame phashes stored as CSV of hex strings
    (e.g. `"h1,h2,h3"`). Returns `(matched_frames, total)` where a frame
    counts as matched if its per-frame hamming distance is within
    `per_frame_threshold`, and `total = min(len(a), len(b))` so a legacy
    single-frame entry on either side reports `total=1` and the caller
    can treat the result as lower-confidence."""
    a_frames = phash_a.split(",")
    b_frames = phash_b.split(",")
    total = min(len(a_frames), len(b_frames))
    matched = 0
    for i in range(total):
        if hamming_distance(a_frames[i], b_frames[i]) <= per_frame_threshold:
            matched += 1
    return matched, total


async def download_thumb_and_phash(client, message) -> str | None:
    """Download the smallest TG thumbnail for `message` and return its phash.

    Used by the cross-source dedup path: TG always attaches a small preview
    to photos/videos, so we can build a target-side index without ever
    fetching the full file. `None` is returned whenever a phash can't be
    produced (no media, no thumb, decode error, download error) — callers
    should treat that as "no thumb-level match possible" and fall through
    to the full-file path.
    """
    if not PHASH_AVAILABLE:
        return None
    if getattr(message, "media", None) is None:
        return None
    try:
        # thumb=0 = smallest available preview; bytes=True keeps it in-memory
        # so we avoid spilling temp files to disk during a scan.
        data = await client.download_media(message, file=bytes, thumb=0)
    except Exception as e:
        logger.debug(f"Thumb download failed: {e}")
        return None
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"Thumb pHash failed: {e}")
        return None
