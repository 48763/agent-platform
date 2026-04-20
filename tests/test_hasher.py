import io
from unittest.mock import AsyncMock, MagicMock
import pytest
from PIL import Image
from agents.tg_transfer.hasher import compute_sha256, compute_phash, hamming_distance


class TestSHA256:
    def test_compute_sha256(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        result = compute_sha256(str(f))
        assert len(result) == 64
        assert result == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"identical")
        f2.write_bytes(b"identical")
        assert compute_sha256(str(f1)) == compute_sha256(str(f2))

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"aaa")
        f2.write_bytes(b"bbb")
        assert compute_sha256(str(f1)) != compute_sha256(str(f2))


class TestPHash:
    def test_compute_phash_returns_hex_string(self, tmp_path):
        img = Image.new("RGB", (100, 100), color="red")
        path = str(tmp_path / "red.png")
        img.save(path)
        result = compute_phash(path)
        assert result is not None
        assert len(result) == 16

    def test_same_image_same_phash(self, tmp_path):
        img = Image.new("RGB", (100, 100), color="blue")
        p1 = str(tmp_path / "a.png")
        p2 = str(tmp_path / "b.png")
        img.save(p1)
        img.save(p2)
        assert compute_phash(p1) == compute_phash(p2)

    def test_non_image_returns_none(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("not an image")
        result = compute_phash(str(f))
        assert result is None


class TestDownloadThumbAndPhash:
    """Phase 1 — dedicated helper for computing thumb_phash directly from a
    Telegram message without touching the full media file. Used by the
    /index_target scan (Phase 2) and the source-side dedup path (Phase 4)."""

    @pytest.mark.asyncio
    async def test_returns_none_for_text_only_message(self):
        from agents.tg_transfer.hasher import download_thumb_and_phash
        client = AsyncMock()
        msg = MagicMock()
        msg.media = None
        result = await download_thumb_and_phash(client, msg)
        assert result is None
        client.download_media.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_thumb_and_returns_phash(self):
        from agents.tg_transfer.hasher import download_thumb_and_phash
        # Produce real PNG bytes so Pillow can decode them.
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), color="green").save(buf, format="PNG")
        thumb_bytes = buf.getvalue()

        client = AsyncMock()
        client.download_media = AsyncMock(return_value=thumb_bytes)
        msg = MagicMock()
        msg.media = MagicMock()  # has media

        result = await download_thumb_and_phash(client, msg)
        assert result is not None
        assert len(result) == 16  # 16-char hex phash

        # Must request the thumb, not the full file.
        _, kwargs = client.download_media.call_args
        assert "thumb" in kwargs

    @pytest.mark.asyncio
    async def test_returns_none_when_download_returns_empty(self):
        from agents.tg_transfer.hasher import download_thumb_and_phash
        client = AsyncMock()
        client.download_media = AsyncMock(return_value=None)
        msg = MagicMock()
        msg.media = MagicMock()
        result = await download_thumb_and_phash(client, msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_download_raises(self):
        from agents.tg_transfer.hasher import download_thumb_and_phash
        client = AsyncMock()
        client.download_media = AsyncMock(side_effect=RuntimeError("boom"))
        msg = MagicMock()
        msg.media = MagicMock()
        result = await download_thumb_and_phash(client, msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_bytes_are_not_image(self):
        from agents.tg_transfer.hasher import download_thumb_and_phash
        client = AsyncMock()
        client.download_media = AsyncMock(return_value=b"not-an-image")
        msg = MagicMock()
        msg.media = MagicMock()
        result = await download_thumb_and_phash(client, msg)
        assert result is None


class TestHammingDistance:
    def test_identical(self):
        assert hamming_distance("abcdef0123456789", "abcdef0123456789") == 0

    def test_one_bit_diff(self):
        assert hamming_distance("0000000000000000", "0000000000000001") == 1

    def test_all_bits_diff(self):
        assert hamming_distance("0000000000000000", "ffffffffffffffff") == 64
