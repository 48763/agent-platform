import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Intents the LLM fallback is allowed to return. Anything else is rejected to
# avoid routing to code paths the LLM invented.
_AI_ALLOWED_INTENTS = frozenset({
    "threshold", "config", "stats", "search", "batch",
})

# https://t.me/channel_name/123 (public)
_PUBLIC_MSG_RE = re.compile(r"https?://t\.me/([A-Za-z_]\w+)/(\d+)")
# https://t.me/c/1234567890/456 (private)
_PRIVATE_MSG_RE = re.compile(r"https?://t\.me/c/(\d+)/(\d+)")
# Config keywords
_CONFIG_RE = re.compile(r"(預設目標|設定目標|default.?target)", re.IGNORECASE)
_SEARCH_RE = re.compile(r"(搜尋|查詢|search|找)", re.IGNORECASE)
_STATS_RE = re.compile(r"(統計|stats)", re.IGNORECASE)
_PAGE_RE = re.compile(r"(下一頁|上一頁|next|prev)", re.IGNORECASE)
_THRESHOLD_RE = re.compile(
    r"(門檻|大小限制|size\s*limit|threshold|限制)", re.IGNORECASE,
)
# Matches "300MB", "1.5GB", "2gb", "500" (MB implied), etc.
_THRESHOLD_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(gb|mb|kb)?", re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedLink:
    chat: int | str  # chat_id (int) or username (str)
    message_id: int
    is_private: bool


def parse_tg_link(text: str) -> Optional[ParsedLink]:
    """Extract a TG message link from text. Returns None if no message link found."""
    m = _PRIVATE_MSG_RE.search(text)
    if m:
        return ParsedLink(chat=int(m.group(1)), message_id=int(m.group(2)), is_private=True)
    m = _PUBLIC_MSG_RE.search(text)
    if m:
        return ParsedLink(chat=m.group(1), message_id=int(m.group(2)), is_private=False)
    return None


def detect_forward(content: str, metadata: dict) -> Optional[ParsedLink]:
    """Detect if a message is forwarded. Returns ParsedLink if forward info present."""
    chat_id = metadata.get("forward_chat_id")
    msg_id = metadata.get("forward_message_id")
    if chat_id is not None and msg_id is not None:
        return ParsedLink(chat=chat_id, message_id=msg_id, is_private=True)
    return None


def classify_intent(text: str) -> str:
    """Classify user message intent.
    Returns: 'single_transfer', 'config', 'threshold', 'search', 'stats',
    'page', or 'batch'.
    """
    if parse_tg_link(text) is not None:
        return "single_transfer"
    if _CONFIG_RE.search(text):
        return "config"
    if _THRESHOLD_RE.search(text):
        return "threshold"
    if _STATS_RE.search(text):
        return "stats"
    if _PAGE_RE.search(text):
        return "page"
    if _SEARCH_RE.search(text):
        return "search"
    return "batch"


def parse_threshold(text: str) -> Optional[int]:
    """Extract a size threshold from `text` and return it in MB (int).
    Returns None if no number is present or the unit is too small (KB).

    Conventions:
      - Bare number (no unit)  → MB.
      - "MB"                   → MB.
      - "GB"                   → multiplied by 1024.
      - "KB" (or smaller)      → None (not meaningful at this layer).
      - "0" explicitly         → 0 (disables the limit).
    """
    m = _THRESHOLD_VALUE_RE.search(text)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "kb":
        return None
    if unit == "gb":
        return int(value * 1024)
    # "mb" or empty (bare number treated as MB)
    return int(value)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _strip_json_fence(text: str) -> str:
    """LLMs often wrap JSON in ```json ... ``` fences. Pull out the inner
    object. If no fence is found, return the stripped text."""
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


async def ai_classify_command(content: str, llm: Any) -> Optional[dict]:
    """LLM-based fuzzy fallback for commands the regex classifier couldn't
    identify. Returns {'intent': str, 'params': dict} or None.

    None is returned when:
      - llm is None (no LLM configured)
      - the LLM raises
      - the reply isn't valid JSON
      - the intent isn't in the allow-list (defends against hallucinated
        actions like 'delete_everything')
    """
    if llm is None:
        return None
    prompt = (
        "你是一個 Telegram 轉存 bot 的指令分類器。\n"
        "請把使用者訊息分類為以下其中一個 intent，並抽出參數，回覆純 JSON：\n"
        '- threshold: 調整檔案大小上限。params: {"mb": <int, 0 表示取消>}\n'
        '- config: 修改設定（例如預設目標）。params: {}\n'
        '- stats: 查看統計。params: {}\n'
        '- search: 搜尋媒體。params: {}\n'
        '- batch: 搬移多則訊息。params: {}\n\n'
        f"使用者訊息：{content}\n\n只回覆 JSON，不要解釋。"
    )
    try:
        raw = await llm.prompt(prompt)
    except Exception as e:  # LLM outage shouldn't crash routing
        logger.warning(f"ai_classify_command LLM error: {e}")
        return None
    try:
        data = json.loads(_strip_json_fence(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in _AI_ALLOWED_INTENTS:
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        params = {}
    return {"intent": intent, "params": params}
