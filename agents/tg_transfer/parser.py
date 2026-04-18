import re
from dataclasses import dataclass
from typing import Optional

# https://t.me/channel_name/123 (public)
_PUBLIC_MSG_RE = re.compile(r"https?://t\.me/([A-Za-z_]\w+)/(\d+)")
# https://t.me/c/1234567890/456 (private)
_PRIVATE_MSG_RE = re.compile(r"https?://t\.me/c/(\d+)/(\d+)")
# Config keywords
_CONFIG_RE = re.compile(r"(預設目標|設定目標|default.?target)", re.IGNORECASE)
_SEARCH_RE = re.compile(r"(搜尋|查詢|search|找)", re.IGNORECASE)
_STATS_RE = re.compile(r"(統計|stats)", re.IGNORECASE)
_PAGE_RE = re.compile(r"(下一頁|上一頁|next|prev)", re.IGNORECASE)


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
    Returns: 'single_transfer', 'config', 'search', 'stats', 'page', or 'batch'.
    """
    if parse_tg_link(text) is not None:
        return "single_transfer"
    if _CONFIG_RE.search(text):
        return "config"
    if _STATS_RE.search(text):
        return "stats"
    if _PAGE_RE.search(text):
        return "page"
    if _SEARCH_RE.search(text):
        return "search"
    return "batch"
