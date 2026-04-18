import pytest
from agents.tg_transfer.tag_extractor import extract_tags


class TestExtractTags:
    def test_single_tag(self):
        assert extract_tags("這是 #教學 影片") == ["教學"]

    def test_multiple_tags(self):
        assert extract_tags("#影片 #教學 #python") == ["影片", "教學", "python"]

    def test_no_tags(self):
        assert extract_tags("這是一段普通文字") == []

    def test_none_input(self):
        assert extract_tags(None) == []

    def test_empty_string(self):
        assert extract_tags("") == []

    def test_tag_with_chinese(self):
        assert extract_tags("#測試 #資料備份") == ["測試", "資料備份"]

    def test_dedup_tags(self):
        assert extract_tags("#aaa #bbb #aaa") == ["aaa", "bbb"]

    def test_tag_at_start(self):
        assert extract_tags("#開頭標籤 一些文字") == ["開頭標籤"]
