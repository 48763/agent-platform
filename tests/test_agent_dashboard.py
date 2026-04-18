import pytest
from aiohttp import web
from core.agent_dashboard import create_dashboard_handler, render_dashboard_html


class TestRenderDashboardHTML:
    def test_renders_title(self):
        stats = {"title": "My Stats", "counters": [], "tables": []}
        html = render_dashboard_html(stats)
        assert "My Stats" in html

    def test_renders_counters(self):
        stats = {
            "title": "Test",
            "counters": [("媒體數", 42), ("標籤", 5)],
            "tables": [],
        }
        html = render_dashboard_html(stats)
        assert "42" in html
        assert "媒體數" in html
        assert "5" in html

    def test_renders_table(self):
        stats = {
            "title": "Test",
            "counters": [],
            "tables": [{
                "title": "標籤統計",
                "headers": ["標籤", "數量"],
                "rows": [("#教學", 10), ("#python", 5)],
            }],
        }
        html = render_dashboard_html(stats)
        assert "標籤統計" in html
        assert "#教學" in html
        assert "10" in html

    def test_empty_tables(self):
        stats = {"title": "Empty", "counters": [], "tables": []}
        html = render_dashboard_html(stats)
        assert "Empty" in html

    def test_multiple_tables(self):
        stats = {
            "title": "Multi",
            "counters": [],
            "tables": [
                {"title": "T1", "headers": ["A"], "rows": [("x",)]},
                {"title": "T2", "headers": ["B"], "rows": [("y",)]},
            ],
        }
        html = render_dashboard_html(stats)
        assert "T1" in html
        assert "T2" in html


class TestCreateDashboardHandler:
    @pytest.mark.asyncio
    async def test_handler_returns_html(self, aiohttp_client):
        async def my_stats():
            return {"title": "Test", "counters": [("Count", 1)], "tables": []}

        app = web.Application()
        app.router.add_get("/dashboard", create_dashboard_handler(my_stats))
        client = await aiohttp_client(app)
        resp = await client.get("/dashboard")
        assert resp.status == 200
        text = await resp.text()
        assert "Test" in text
        assert "text/html" in resp.headers["Content-Type"]
