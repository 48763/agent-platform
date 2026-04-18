from aiohttp import web
from typing import Callable


def render_dashboard_html(stats: dict) -> str:
    """Render a stats dict into an HTML page."""
    title = stats.get("title", "Agent Dashboard")
    counters = stats.get("counters", [])
    tables = stats.get("tables", [])

    counter_html = ""
    for label, value in counters:
        counter_html += (
            f'<div class="stat-card">'
            f'<div class="stat-value">{value}</div>'
            f'<div class="stat-label">{label}</div>'
            f'</div>\n'
        )

    tables_html = ""
    for table in tables:
        headers = "".join(f"<th>{h}</th>" for h in table.get("headers", []))
        rows = ""
        for row in table.get("rows", []):
            cells = "".join(f"<td>{c}</td>" for c in row)
            rows += f"<tr>{cells}</tr>\n"
        tables_html += (
            f'<h2>{table.get("title", "")}</h2>\n'
            f'<table><tr>{headers}</tr>\n{rows}</table>\n'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
       background: #0d1117; color: #c9d1d9; max-width: 700px; margin: 40px auto; padding: 0 20px; }}
h1 {{ color: #58a6ff; margin-bottom: 20px; }}
h2 {{ color: #8b949e; margin: 24px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }}
.stats-row {{ display: flex; gap: 24px; margin: 20px 0; flex-wrap: wrap; }}
.stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px 24px; }}
.stat-value {{ font-size: 2em; font-weight: bold; color: #f0f6fc; }}
.stat-label {{ color: #8b949e; font-size: 0.9em; margin-top: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
th, td {{ border: 1px solid #30363d; padding: 8px 12px; text-align: left; }}
th {{ background: #161b22; color: #8b949e; }}
td {{ color: #c9d1d9; }}
tr:hover {{ background: #161b22; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="stats-row">
{counter_html if counter_html else '<div class="stat-card"><div class="stat-label">尚無資料</div></div>'}
</div>
{tables_html}
</body></html>"""


def create_dashboard_handler(stats_fn: Callable) -> Callable:
    """Create an aiohttp handler that renders stats from the given async function."""
    async def handler(request: web.Request) -> web.Response:
        stats = await stats_fn()
        html = render_dashboard_html(stats)
        return web.Response(text=html, content_type="text/html")
    return handler
