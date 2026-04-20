import pytest
from agents.tg_transfer.search import format_search_results, format_similar_results


class TestFormatSearchResults:
    def test_format_results(self):
        results = [
            {"caption": "Python 教學影片，這是一段很長的描述文字用來測試截斷功能", "target_chat": "@dst", "target_msg_id": 123},
            {"caption": "Rust 教學", "target_chat": "@dst", "target_msg_id": 456},
        ]
        text = format_search_results(results, total=2, page=1, page_size=10)
        assert "Python 教學影片" in text
        assert "t.me" in text
        assert "1/1" in text

    def test_private_chat_link_format(self):
        """Private supergroup ids like -1001234567890 must produce t.me/c/<id>/<msg>
        links (strip the '-100' prefix), not t.me/-1001234567890/..."""
        results = [
            {"caption": "p", "target_chat": "-1001234567890", "target_msg_id": 42},
        ]
        text = format_search_results(results, total=1, page=1, page_size=10)
        assert "https://t.me/c/1234567890/42" in text
        assert "-1001234567890" not in text

    def test_public_chat_link_format_unchanged(self):
        """Public @username links keep t.me/<username>/<msg> form."""
        results = [
            {"caption": "p", "target_chat": "@publicchan", "target_msg_id": 7},
        ]
        text = format_search_results(results, total=1, page=1, page_size=10)
        assert "https://t.me/publicchan/7" in text

    def test_format_no_results(self):
        text = format_search_results([], total=0, page=1, page_size=10)
        assert "找不到" in text

    def test_format_pagination_info(self):
        results = [{"caption": f"item {i}", "target_chat": "@dst", "target_msg_id": i} for i in range(10)]
        text = format_search_results(results, total=25, page=1, page_size=10)
        assert "1/3" in text

    def test_none_caption(self):
        results = [{"caption": None, "target_chat": "@dst", "target_msg_id": 789}]
        text = format_search_results(results, total=1, page=1, page_size=10)
        assert "（無文字）" in text


class TestFormatSimilarResults:
    def test_format_similar(self):
        results = [
            {"caption": "相似圖片", "target_chat": "@dst", "target_msg_id": 100, "distance": 3},
        ]
        text = format_similar_results(results)
        assert "相似" in text
        assert "t.me" in text

    def test_no_similar(self):
        text = format_similar_results([])
        assert "沒有找到" in text
