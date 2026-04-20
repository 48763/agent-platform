from core.agent_dashboard import create_dashboard_handler
from agents.tg_transfer.media_db import MediaDB

# Friendly labels for file_type values that may appear in media.file_type.
# Unknown types are still shown using their raw name so nothing silently
# disappears from the dashboard.
_TYPE_LABELS = {
    "photo": "📷 圖片",
    "video": "🎬 影片",
    "document": "📄 檔案",
    "audio": "🎵 音訊",
    "voice": "🎤 語音",
    "animation": "🎞 動圖",
}


def _type_counters(by_type: dict[str, int]) -> list[tuple[str, int]]:
    """Turn {'photo': 2, 'video': 1, ...} into ordered (label, count) pairs.
    Known kinds first (stable display order), then anything else."""
    counters: list[tuple[str, int]] = []
    for key, label in _TYPE_LABELS.items():
        counters.append((label, by_type.get(key, 0)))
    for key, count in by_type.items():
        if key not in _TYPE_LABELS:
            counters.append((key, count))
    return counters


def create_tg_dashboard_handler(media_db: MediaDB | None):
    """Create dashboard handler using shared framework."""
    async def get_stats():
        if not media_db:
            return {"title": "TG Transfer 統計", "counters": [("狀態", "未初始化")], "tables": []}
        stats = await media_db.get_stats()
        by_type = stats.get("by_type", {})

        counters = [("已轉存媒體", stats["total_media"])]
        counters.extend(_type_counters(by_type))
        counters.append(("標籤總數", stats["total_tags"]))

        tables = []
        if stats["tag_counts"]:
            tables.append({
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [(f"#{name}", count) for name, count in stats["tag_counts"]],
            })
        return {
            "title": "TG Transfer 統計",
            "counters": counters,
            "tables": tables,
        }
    return create_dashboard_handler(get_stats)
