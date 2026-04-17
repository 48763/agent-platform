import pytest
from agents.tg_transfer.parser import (
    parse_tg_link,
    detect_forward,
    classify_intent,
    ParsedLink,
)


class TestParseTgLink:
    def test_public_message_link(self):
        result = parse_tg_link("https://t.me/channel_name/123")
        assert result == ParsedLink(chat="channel_name", message_id=123, is_private=False)

    def test_private_message_link(self):
        result = parse_tg_link("https://t.me/c/1234567890/456")
        assert result == ParsedLink(chat=1234567890, message_id=456, is_private=True)

    def test_message_link_in_text(self):
        text = "幫我搬這個 https://t.me/channel_name/789 到備份群"
        result = parse_tg_link(text)
        assert result == ParsedLink(chat="channel_name", message_id=789, is_private=False)

    def test_no_link(self):
        result = parse_tg_link("這只是一段普通文字")
        assert result is None

    def test_invite_link_not_message(self):
        result = parse_tg_link("https://t.me/+AbCdEfG")
        assert result is None


class TestDetectForward:
    def test_forwarded_message(self):
        content = "這是一段訊息"
        metadata = {"forward_chat_id": -1001234567890, "forward_message_id": 42}
        result = detect_forward(content, metadata)
        assert result == ParsedLink(chat=-1001234567890, message_id=42, is_private=True)

    def test_not_forwarded(self):
        result = detect_forward("普通訊息", {})
        assert result is None


class TestClassifyIntent:
    def test_link_intent(self):
        assert classify_intent("https://t.me/ch/123") == "single_transfer"

    def test_config_intent_default_target(self):
        assert classify_intent("預設目標改成 @my_backup") == "config"

    def test_config_intent_set_target(self):
        assert classify_intent("設定目標群組 @new_channel") == "config"

    def test_batch_intent(self):
        assert classify_intent("把 @old_channel 的東西搬到 @new_channel") == "batch"

    def test_link_with_surrounding_text(self):
        assert classify_intent("幫我搬 https://t.me/c/123/456") == "single_transfer"
