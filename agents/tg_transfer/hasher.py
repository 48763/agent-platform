import hashlib
import io
import logging
import asyncio
import os

logger = logging.getLogger(__name__)

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


async def compute_phash_video(file_path: str, frame_path: str) -> str | None:
    """Extract first frame from video with ffmpeg, then compute pHash.

    frame_path: exact file path where the extracted frame will be written
    (and deleted after hashing). Caller chooses a unique path.
    """
    if not PHASH_AVAILABLE:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", file_path, "-ss", "1", "-frames:v", "1",
            "-y", frame_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and os.path.exists(frame_path):
            result = compute_phash(frame_path)
            os.remove(frame_path)
            return result
    except Exception as e:
        logger.debug(f"Video pHash failed for {file_path}: {e}")
    return None


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two hex hash strings."""
    return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")


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
