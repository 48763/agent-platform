import pytest
from agents.tg_transfer.parser import (
    parse_tg_link,
    detect_forward,
    classify_intent,
    parse_threshold,
    ai_classify_command,
    ParsedLink,
)


class _FakeLLM:
    """Minimal stub matching the LLM interface (.prompt(str) -> str)."""
    def __init__(self, reply: str):
        self._reply = reply
        self.calls = []

    async def prompt(self, text: str) -> str:
        self.calls.append(text)
        return self._reply


class _RaisingLLM:
    async def prompt(self, text: str) -> str:
        raise RuntimeError("LLM down")


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

    def test_threshold_intent_chinese(self):
        assert classify_intent("門檻改成 200MB") == "threshold"

    def test_threshold_intent_chinese_simple(self):
        assert classify_intent("大小限制 500MB") == "threshold"

    def test_threshold_intent_english(self):
        assert classify_intent("size limit 1GB") == "threshold"

    def test_index_target_explicit_slash_command(self):
        assert classify_intent("/index_target") == "index_target"

    def test_index_target_with_chat_arg(self):
        assert classify_intent("/index_target @my_archive") == "index_target"

    def test_index_target_chinese_phrase(self):
        assert classify_intent("索引目標") == "index_target"


class TestParseIndexTargetChat:
    """Extracts the chat argument from '/index_target [chat]' input. Returns
    None when the user gave no explicit chat — caller should fall back to
    default_target_chat config."""

    def test_extracts_username_arg(self):
        from agents.tg_transfer.parser import parse_index_target_chat
        assert parse_index_target_chat("/index_target @my_backup") == "@my_backup"

    def test_returns_none_when_no_arg(self):
        from agents.tg_transfer.parser import parse_index_target_chat
        assert parse_index_target_chat("/index_target") is None

    def test_returns_none_for_chinese_phrase(self):
        from agents.tg_transfer.parser import parse_index_target_chat
        assert parse_index_target_chat("索引目標") is None

    def test_extracts_numeric_chat_id(self):
        """Private chats are identified by numeric IDs like -1001234567890."""
        from agents.tg_transfer.parser import parse_index_target_chat
        assert parse_index_target_chat("/index_target -1001234567890") \
            == "-1001234567890"


class TestParseThreshold:
    """parse_threshold returns the threshold in MB (int), or None if it can't
    parse one. Used to let users change size_limit_mb mid-batch via chat."""

    def test_mb_explicit(self):
        assert parse_threshold("門檻改成 200MB") == 200

    def test_gb_uppercase(self):
        assert parse_threshold("size limit 1GB") == 1024

    def test_gb_lowercase(self):
        assert parse_threshold("限制 2gb") == 2048

    def test_decimal_gb(self):
        # 1.5 GB = 1536 MB
        assert parse_threshold("threshold 1.5GB") == 1536

    def test_bare_number_treated_as_mb(self):
        """Bare number with no unit — assume MB (convention)."""
        assert parse_threshold("門檻 500") == 500

    def test_zero_means_disable(self):
        """Explicit 0 is a valid threshold — means 'no limit'."""
        assert parse_threshold("門檻改成 0") == 0

    def test_no_number(self):
        assert parse_threshold("門檻改成") is None

    def test_kb_unsupported_returns_none(self):
        """Too small to be meaningful; don't guess."""
        assert parse_threshold("門檻 500KB") is None

    def test_surrounding_text_ok(self):
        assert parse_threshold("你好，請幫我把大小限制改成 300MB 謝謝") == 300


class TestAIClassifyCommand:
    """LLM-based fuzzy command classifier used as fallback when regex
    classify_intent returns 'batch' but the message isn't actually a batch
    request. Returns {'intent': str, 'params': dict} or None."""

    @pytest.mark.asyncio
    async def test_none_when_no_llm(self):
        result = await ai_classify_command("anything", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_threshold_fuzzy_phrasing(self):
        llm = _FakeLLM('{"intent": "threshold", "params": {"mb": 500}}')
        result = await ai_classify_command("我不想要超過 500 的檔案了", llm)
        assert result == {"intent": "threshold", "params": {"mb": 500}}

    @pytest.mark.asyncio
    async def test_stats_fuzzy_phrasing(self):
        llm = _FakeLLM('{"intent": "stats", "params": {}}')
        result = await ai_classify_command("看一下目前儲存了多少", llm)
        assert result == {"intent": "stats", "params": {}}

    @pytest.mark.asyncio
    async def test_strips_markdown_fence(self):
        llm = _FakeLLM('```json\n{"intent": "stats", "params": {}}\n```')
        result = await ai_classify_command("狀態", llm)
        assert result == {"intent": "stats", "params": {}}

    @pytest.mark.asyncio
    async def test_non_json_returns_none(self):
        llm = _FakeLLM("不知道你在說什麼")
        result = await ai_classify_command("???", llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_error_returns_none(self):
        result = await ai_classify_command("anything", _RaisingLLM())
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_intent_returns_none(self):
        """LLM returns a nonsense intent — we reject it rather than trusting."""
        llm = _FakeLLM('{"intent": "delete_everything", "params": {}}')
        result = await ai_classify_command("清掉所有東西", llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_params_defaults_to_empty_dict(self):
        llm = _FakeLLM('{"intent": "stats"}')
        result = await ai_classify_command("狀態", llm)
        assert result == {"intent": "stats", "params": {}}
