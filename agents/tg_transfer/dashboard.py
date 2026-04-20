from core.agent_dashboard import create_dashboard_handler
from agents.tg_transfer.media_db import MediaDB


def create_tg_dashboard_handler(media_db: MediaDB | None):
    """Create dashboard handler using shared framework."""
    async def get_stats():
        if not media_db:
            return {"title": "TG Transfer 統計", "counters": [("狀態", "未初始化")], "tables": []}
        stats = await media_db.get_stats()
        by = stats.get("by_status", {})
        tables = [{
            "title": "媒體狀態",
            "headers": ["狀態", "數量"],
            "rows": [
                ("✅ 已上傳", by.get("uploaded", 0)),
                ("⏳ 待上傳", by.get("pending", 0)),
                ("❌ 失敗", by.get("failed", 0)),
                ("⏭️ 跳過", by.get("skipped", 0)),
            ],
        }]
        if stats["tag_counts"]:
            tables.append({
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [(f"#{name}", count) for name, count in stats["tag_counts"]],
            })
        return {
            "title": "TG Transfer 統計",
            "counters": [
                ("已轉存媒體", stats["total_media"]),
                ("標籤總數", stats["total_tags"]),
            ],
            "tables": tables,
        }
    return create_dashboard_handler(get_stats)
