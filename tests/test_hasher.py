import io
from unittest.mock import AsyncMock, MagicMock, patch
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


class TestHammingDistanceMulti:
    """3-frame comparison: each side carries a CSV of per-frame phashes
    (`"h1,h2,h3"`). Returns (matched_frames, total_frames_compared) where a
    frame counts as matched if its per-frame hamming distance is within the
    threshold. `total` is the min of the two sides' frame counts — so a
    legacy single-frame row on either side only compares frame 0."""

    def test_all_three_frames_match(self):
        from agents.tg_transfer.hasher import hamming_distance_multi
        a = "0000000000000000,1111111111111111,2222222222222222"
        b = "0000000000000000,1111111111111111,2222222222222222"
        assert hamming_distance_multi(a, b, per_frame_threshold=10) == (3, 3)

    def test_two_of_three_match(self):
        from agents.tg_transfer.hasher import hamming_distance_multi
        a = "0000000000000000,1111111111111111,2222222222222222"
        # Middle frame differs by all 64 bits, others identical.
        b = "0000000000000000,eeeeeeeeeeeeeeee,2222222222222222"
        assert hamming_distance_multi(a, b, per_frame_threshold=10) == (2, 3)

    def test_zero_match(self):
        from agents.tg_transfer.hasher import hamming_distance_multi
        a = "0000000000000000,0000000000000000,0000000000000000"
        b = "ffffffffffffffff,ffffffffffffffff,ffffffffffffffff"
        assert hamming_distance_multi(a, b, per_frame_threshold=10) == (0, 3)

    def test_threshold_boundary(self):
        from agents.tg_transfer.hasher import hamming_distance_multi
        # 10 bits differ — at threshold, should match.
        a = "0000000000000000,0000000000000000,0000000000000000"
        b = "00000000000003ff,0000000000000000,0000000000000000"  # 10 bits in frame 0
        assert hamming_distance_multi(a, b, per_frame_threshold=10) == (3, 3)
        # 11 bits differ in frame 0 — frame 0 misses.
        c = "00000000000007ff,0000000000000000,0000000000000000"  # 11 bits
        assert hamming_distance_multi(a, c, per_frame_threshold=10) == (2, 3)

    def test_legacy_single_frame_on_one_side(self):
        """Old rows stored a single 16-hex phash (no commas). Comparing a
        legacy row to a new 3-frame row compares frame 0 only and reports
        total=1 so callers can treat the result as lower-confidence."""
        from agents.tg_transfer.hasher import hamming_distance_multi
        legacy = "0000000000000000"  # no comma
        new = "0000000000000000,1111111111111111,2222222222222222"
        assert hamming_distance_multi(legacy, new, per_frame_threshold=10) == (1, 1)

    def test_legacy_on_both_sides(self):
        from agents.tg_transfer.hasher import hamming_distance_multi
        a = "0000000000000000"
        b = "0000000000000000"
        assert hamming_distance_multi(a, b, per_frame_threshold=10) == (1, 1)

    def test_mismatched_lengths_uses_min(self):
        """If a video was too short for 3 frames, it may have 1 or 2 frames.
        The compare should use min(len(a), len(b)) as the basis."""
        from agents.tg_transfer.hasher import hamming_distance_multi
        a = "0000000000000000,1111111111111111"           # 2 frames
        b = "0000000000000000,1111111111111111,2222222222222222"  # 3 frames
        assert hamming_distance_multi(a, b, per_frame_threshold=10) == (2, 2)


class TestComputePhashVideoMulti:
    """`compute_phash_video` returns a CSV `"h1,h2,h3"` of per-frame phashes
    extracted at 10% / 50% / 90% of the video duration. For very short
    videos it falls back to a single frame so the result is still usable
    as a (lower-confidence) dedup signal."""

    @pytest.mark.asyncio
    async def test_returns_three_frames_at_10_50_90_percent(self, tmp_path):
        from agents.tg_transfer import hasher
        # Pretend the video is 20s long.
        async def fake_ffprobe(_p):
            return {"duration": 20, "width": 1280, "height": 720}

        # Record the timestamps we were asked to extract frames at so we
        # can assert on them.
        calls: list[float] = []

        async def fake_single(file_path, at_seconds, frame_path):
            calls.append(at_seconds)
            # Return a distinct hex per call so we can see the CSV order.
            return f"{len(calls):016x}"

        with patch.object(hasher, "ffprobe_metadata", new=fake_ffprobe), \
             patch.object(hasher, "_phash_single_frame", new=fake_single):
            result = await hasher.compute_phash_video(
                "/tmp/fake.mp4", str(tmp_path),
            )

        assert result == "0000000000000001,0000000000000002,0000000000000003"
        # 10% / 50% / 90% of 20s
        assert calls == [2.0, 10.0, 18.0]

    @pytest.mark.asyncio
    async def test_short_video_falls_back_to_single_frame(self, tmp_path):
        """Videos under 3s don't have enough content for 3 independent
        samples; return a single-frame phash (no commas) and let callers
        treat it as lower-confidence via hamming_distance_multi."""
        from agents.tg_transfer import hasher

        async def fake_ffprobe(_p):
            return {"duration": 2, "width": 640, "height": 480}

        calls: list[float] = []

        async def fake_single(file_path, at_seconds, frame_path):
            calls.append(at_seconds)
            return "abcdef0123456789"

        with patch.object(hasher, "ffprobe_metadata", new=fake_ffprobe), \
             patch.object(hasher, "_phash_single_frame", new=fake_single):
            result = await hasher.compute_phash_video(
                "/tmp/short.mp4", str(tmp_path),
            )

        assert result == "abcdef0123456789"
        assert "," not in result
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_any_frame_fails(self, tmp_path):
        """If any of the three frames can't be extracted / hashed we bail
        out rather than return a partial CSV — a partial result would be
        indistinguishable from a short-video fallback to callers."""
        from agents.tg_transfer import hasher

        async def fake_ffprobe(_p):
            return {"duration": 30, "width": 1280, "height": 720}

        seq = iter(["aaaaaaaaaaaaaaaa", None, "cccccccccccccccc"])

        async def fake_single(file_path, at_seconds, frame_path):
            return next(seq)

        with patch.object(hasher, "ffprobe_metadata", new=fake_ffprobe), \
             patch.object(hasher, "_phash_single_frame", new=fake_single):
            result = await hasher.compute_phash_video(
                "/tmp/fake.mp4", str(tmp_path),
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_ffprobe_fails(self, tmp_path):
        """No duration → can't compute 10/50/90% timestamps. Return None
        rather than silently falling back to a single legacy frame."""
        from agents.tg_transfer import hasher

        async def fake_ffprobe(_p):
            return None

        with patch.object(hasher, "ffprobe_metadata", new=fake_ffprobe):
            result = await hasher.compute_phash_video(
                "/tmp/fake.mp4", str(tmp_path),
            )

        assert result is None
