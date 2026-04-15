# hub/dashboard.py
import json
import time
from aiohttp import web


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Platform Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
        h1 { color: #58a6ff; margin-bottom: 20px; font-size: 1.5em; }
        h2 { color: #8b949e; margin: 20px 0 10px; font-size: 1.1em; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
        .container { max-width: 1000px; margin: 0 auto; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; margin-bottom: 12px; }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .card-title { font-weight: 600; color: #f0f6fc; }
        .badge { padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
        .badge-online { background: #238636; color: #fff; }
        .badge-working { background: #1f6feb; color: #fff; }
        .badge-waiting { background: #d29922; color: #000; }
        .badge-done { background: #8b949e; color: #fff; }
        .badge-closed { background: #484f58; color: #8b949e; }
        .meta { color: #8b949e; font-size: 0.85em; margin-top: 4px; }
        .patterns { color: #7ee787; font-size: 0.85em; }
        .history { margin-top: 10px; padding: 10px; background: #0d1117; border-radius: 4px; max-height: 200px; overflow-y: auto; font-size: 0.85em; }
        .history .user { color: #58a6ff; }
        .history .assistant { color: #7ee787; }
        .stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; flex: 1; text-align: center; }
        .stat-value { font-size: 2em; font-weight: 700; color: #58a6ff; }
        .stat-label { color: #8b949e; font-size: 0.85em; margin-top: 4px; }
        .refresh { color: #8b949e; font-size: 0.8em; cursor: pointer; }
        .refresh:hover { color: #58a6ff; }
        .empty { color: #484f58; font-style: italic; padding: 20px; text-align: center; }
        .tabs { display: flex; gap: 4px; margin-bottom: 16px; }
        .tab { padding: 6px 16px; background: #21262d; border: 1px solid #30363d; border-radius: 6px; cursor: pointer; color: #8b949e; font-size: 0.9em; }
        .tab.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <h1>Agent Platform</h1>
            <span class="refresh" onclick="loadAll()">Refresh</span>
        </div>
        <div class="stats" id="stats"></div>
        <h2>Agents</h2>
        <div id="agents"></div>
        <h2>Tasks</h2>
        <div class="tabs">
            <div class="tab active" onclick="filterTasks('active', this)">Active</div>
            <div class="tab" onclick="filterTasks('done', this)">Done</div>
            <div class="tab" onclick="filterTasks('all', this)">All</div>
        </div>
        <div id="tasks"></div>
    </div>
    <script>
        let allTasks = [];
        let currentFilter = 'active';

        function timeAgo(ts) {
            const s = Math.floor(Date.now()/1000 - ts);
            if (s < 60) return s + 's ago';
            if (s < 3600) return Math.floor(s/60) + 'm ago';
            if (s < 86400) return Math.floor(s/3600) + 'h ago';
            return Math.floor(s/86400) + 'd ago';
        }

        function badgeClass(status) {
            if (status === 'working') return 'badge-working';
            if (status.startsWith('waiting')) return 'badge-waiting';
            if (status === 'done') return 'badge-done';
            if (status === 'closed') return 'badge-closed';
            return 'badge-online';
        }

        async function loadAgents() {
            const res = await fetch('/agents');
            const data = await res.json();
            const el = document.getElementById('agents');
            if (!data.agents.length) {
                el.innerHTML = '<div class="empty">No agents online</div>';
                return data.agents.length;
            }
            el.innerHTML = data.agents.map(a => `
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">${a.name}</span>
                        <span class="badge badge-online">online</span>
                    </div>
                    <div class="meta">${a.description}</div>
                    <div class="patterns">priority: ${a.priority} &nbsp; patterns: ${a.route_patterns.join(', ')}</div>
                </div>
            `).join('');
            return data.agents.length;
        }

        async function loadTasks() {
            const res = await fetch('/dashboard/tasks');
            const data = await res.json();
            allTasks = data.tasks;
            renderTasks();
            return {active: data.tasks.filter(t => !['done','closed'].includes(t.status)).length, total: data.tasks.length};
        }

        function renderTasks() {
            const el = document.getElementById('tasks');
            let tasks = allTasks;
            if (currentFilter === 'active') tasks = tasks.filter(t => !['done','closed'].includes(t.status));
            if (currentFilter === 'done') tasks = tasks.filter(t => ['done','closed'].includes(t.status));

            if (!tasks.length) {
                el.innerHTML = '<div class="empty">No tasks</div>';
                return;
            }
            el.innerHTML = tasks.map(t => {
                const history = t.conversation_history.slice(-4).map(m =>
                    `<div class="${m.role}"><b>${m.role}:</b> ${m.content.substring(0, 200)}${m.content.length > 200 ? '...' : ''}</div>`
                ).join('');
                return `
                    <div class="card">
                        <div class="card-header">
                            <span class="card-title">${t.agent_name}</span>
                            <span class="badge ${badgeClass(t.status)}">${t.status}</span>
                        </div>
                        <div class="meta">task: ${t.task_id.substring(0,8)}... &nbsp; chat: ${t.chat_id} &nbsp; updated: ${timeAgo(t.updated_at)}</div>
                        <div class="history">${history}</div>
                    </div>
                `;
            }).join('');
        }

        function filterTasks(filter, tab) {
            currentFilter = filter;
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            renderTasks();
        }

        async function loadAll() {
            const agentCount = await loadAgents();
            const taskInfo = await loadTasks();
            document.getElementById('stats').innerHTML = `
                <div class="stat"><div class="stat-value">${agentCount}</div><div class="stat-label">Agents Online</div></div>
                <div class="stat"><div class="stat-value">${taskInfo.active}</div><div class="stat-label">Active Tasks</div></div>
                <div class="stat"><div class="stat-value">${taskInfo.total}</div><div class="stat-label">Total Tasks</div></div>
            `;
        }

        loadAll();
        setInterval(loadAll, 10000);
    </script>
</body>
</html>"""


async def handle_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_dashboard_tasks(request: web.Request) -> web.Response:
    tm = request.app["task_manager"]
    rows = tm._conn.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    tasks = [tm._row_to_dict(r) for r in rows]
    return web.json_response({"tasks": tasks})
