from core.agent_dashboard import create_dashboard_handler
from agents.tg_transfer.media_db import MediaDB


def create_tg_dashboard_handler(media_db: MediaDB):
    """Create dashboard handler using shared framework."""
    async def get_stats():
        stats = await media_db.get_stats()
        return {
            "title": "TG Transfer 統計",
            "counters": [
                ("儲存媒體", stats["total_media"]),
                ("標籤總數", stats["total_tags"]),
            ],
            "tables": [{
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [(f"#{name}", count) for name, count in stats["tag_counts"]],
            }] if stats["tag_counts"] else [],
        }
    return create_dashboard_handler(get_stats)
