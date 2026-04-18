from aiohttp import web
from agents.tg_transfer.media_db import MediaDB


async def dashboard_handler(request: web.Request) -> web.Response:
    """Serve stats dashboard HTML page."""
    media_db: MediaDB = request.app["media_db"]
    stats = await media_db.get_stats()

    tag_rows = ""
    for name, count in stats["tag_counts"]:
        tag_rows += f"<tr><td>#{name}</td><td>{count}</td></tr>\n"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>TG Transfer Stats</title>
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f5f5f5; }}
.stat {{ font-size: 2em; font-weight: bold; color: #333; }}
.stat-label {{ color: #888; font-size: 0.9em; }}
.stats-row {{ display: flex; gap: 40px; margin: 20px 0; }}
</style></head>
<body>
<h1>TG Transfer 統計</h1>
<div class="stats-row">
  <div><div class="stat">{stats['total_media']}</div><div class="stat-label">儲存媒體數量</div></div>
  <div><div class="stat">{stats['total_tags']}</div><div class="stat-label">標籤總數</div></div>
</div>
<h2>標籤統計</h2>
<table>
<tr><th>標籤</th><th>媒體數量</th></tr>
{tag_rows if tag_rows else '<tr><td colspan="2">尚無標籤</td></tr>'}
</table>
</body></html>"""
    return web.Response(text=html, content_type="text/html")
