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


class TestHammingDistance:
    def test_identical(self):
        assert hamming_distance("abcdef0123456789", "abcdef0123456789") == 0

    def test_one_bit_diff(self):
        assert hamming_distance("0000000000000000", "0000000000000001") == 1

    def test_all_bits_diff(self):
        assert hamming_distance("0000000000000000", "ffffffffffffffff") == 64
