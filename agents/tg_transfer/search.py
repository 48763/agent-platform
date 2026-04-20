import math


def _make_link(target_chat: str, target_msg_id: int) -> str:
    """Generate a TG message link.

    - Public chat (@name / name): https://t.me/<name>/<msg_id>
    - Private supergroup (-100XXXXXXXXXX): https://t.me/c/<XXXXXXXXXX>/<msg_id>
    """
    chat = target_chat.lstrip("@")
    if chat.startswith("-100") and chat[4:].isdigit():
        return f"https://t.me/c/{chat[4:]}/{target_msg_id}"
    return f"https://t.me/{chat}/{target_msg_id}"


def _truncate(text: str | None, max_len: int = 50) -> str:
    if not text:
        return "（無文字）"
    return text[:max_len] + "..." if len(text) > max_len else text


def format_search_results(results: list[dict], total: int, page: int, page_size: int) -> str:
    """Format search results with pagination info."""
    if not results:
        return "找不到符合條件的媒體"

    total_pages = math.ceil(total / page_size)
    lines = [f"搜尋結果（{page}/{total_pages} 頁，共 {total} 筆）\n"]

    for i, r in enumerate(results, start=(page - 1) * page_size + 1):
        preview = _truncate(r["caption"])
        link = _make_link(r["target_chat"], r["target_msg_id"])
        lines.append(f"{i}. {preview}\n   {link}")

    if page < total_pages:
        lines.append(f"\n輸入「下一頁」查看更多")

    return "\n".join(lines)


def format_similar_results(results: list[dict]) -> str:
    """Format pHash similar search results."""
    if not results:
        return "沒有找到相似的媒體"

    lines = ["找到以下相似媒體：\n"]
    for i, r in enumerate(results, 1):
        preview = _truncate(r["caption"])
        link = _make_link(r["target_chat"], r["target_msg_id"])
        distance = r.get("distance", "?")
        lines.append(f"{i}. {preview}（相似度差異：{distance}）\n   {link}")

    lines.append("\n是否仍要上傳？（是/否）")
    return "\n".join(lines)
