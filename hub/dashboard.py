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
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; margin-bottom: 12px; position: relative; }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .card-title { font-weight: 600; color: #f0f6fc; }
        .badge { padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }
        .badge-online { background: #238636; color: #fff; }
        .badge-working { background: #1f6feb; color: #fff; }
        .badge-waiting { background: #d29922; color: #000; }
        .badge-done { background: #8b949e; color: #fff; }
        .badge-closed { background: #484f58; color: #8b949e; }
        .meta { color: #8b949e; font-size: 0.85em; margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
        .meta-item { display: flex; align-items: center; gap: 4px; }
        .patterns { color: #7ee787; font-size: 0.85em; }
        .history { margin-top: 10px; padding: 10px; background: #0d1117; border-radius: 4px; max-height: 200px; overflow-y: auto; font-size: 0.85em; line-height: 1.6; }
        .history .msg { margin-bottom: 6px; }
        .history .msg-user { color: #58a6ff; }
        .history .msg-assistant { color: #7ee787; }
        .history .msg-label { font-weight: 600; }
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
        .btn-delete { background: none; border: 1px solid #f85149; color: #f85149; padding: 2px 10px; border-radius: 4px; cursor: pointer; font-size: 0.8em; }
        .btn-delete:hover { background: #f85149; color: #fff; }
        .btn-close { background: none; border: 1px solid #d29922; color: #d29922; padding: 2px 10px; border-radius: 4px; cursor: pointer; font-size: 0.8em; }
        .btn-close:hover { background: #d29922; color: #000; }
        .actions { display: flex; gap: 6px; }
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <h1>Agent Platform</h1>
            <span class="refresh" onclick="loadAll()">&#x21bb; 重新整理</span>
        </div>
        <div class="stats" id="stats"></div>
        <h2>Agents</h2>
        <div id="agents"></div>
        <h2>對話紀錄</h2>
        <div class="tabs">
            <div class="tab active" onclick="filterTasks('all', this)">全部</div>
        </div>
        <div id="tasks"></div>
    </div>
    <script>
        let allTasks = [];
        let currentFilter = 'all';

        function timeAgo(ts) {
            const s = Math.floor(Date.now()/1000 - ts);
            if (s < 60) return s + ' 秒前';
            if (s < 3600) return Math.floor(s/60) + ' 分鐘前';
            if (s < 86400) return Math.floor(s/3600) + ' 小時前';
            return Math.floor(s/86400) + ' 天前';
        }

        function agentDisplayName(name) {
            if (name === '_hub') return 'Hub 閒聊';
            return name;
        }

        function statusLabel(status) {
            const map = {
                'working': '處理中',
                'waiting_input': '等待回覆',
                'waiting_approval': '等待授權',
                'done': '已完成',
                'closed': '已完成'
            };
            return map[status] || status;
        }

        function badgeClass(status) {
            if (status === 'working') return 'badge-working';
            if (status.startsWith('waiting')) return 'badge-waiting';
            if (status === 'done') return 'badge-done';
            if (status === 'closed') return 'badge-closed';
            return 'badge-online';
        }

        function sourceLabel(source) {
            const map = { 'telegram': 'Telegram', 'discord': 'Discord', 'line': 'Line' };
            return map[source] || source || 'Telegram';
        }

        async function loadAgents() {
            const res = await fetch('/agents');
            const data = await res.json();
            const el = document.getElementById('agents');
            if (!data.agents.length) {
                el.innerHTML = '<div class="empty">沒有在線的 Agent</div>';
                return data.agents.length;
            }
            el.innerHTML = data.agents.map(a => `
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">${a.name}</span>
                        <span class="badge badge-online">在線</span>
                    </div>
                    <div class="meta">${a.description}</div>
                    <div class="patterns">優先級: ${a.priority} &nbsp; 關鍵字: ${a.route_patterns.join(', ')}</div>
                </div>
            `).join('');
            return data.agents.length;
        }

        async function loadTasks() {
            const res = await fetch('/dashboard/tasks');
            const data = await res.json();
            allTasks = data.tasks;
            renderTasks();
            return {active: data.tasks.filter(t => t.status !== 'done').length, total: data.tasks.length};
        }

        function renderTasks() {
            const el = document.getElementById('tasks');
            let tasks = allTasks;
            if (currentFilter === 'active') tasks = tasks.filter(t => t.status !== 'done');
            if (currentFilter === 'done') tasks = tasks.filter(t => t.status === 'done');

            if (!tasks.length) {
                el.innerHTML = '<div class="empty">沒有對話紀錄</div>';
                return;
            }
            el.innerHTML = tasks.map(t => {
                const history = t.conversation_history.slice(-6).map(m => {
                    const isUser = m.role === 'user';
                    const label = isUser ? '使用者' : agentDisplayName(t.agent_name);
                    const cls = isUser ? 'msg-user' : 'msg-assistant';
                    const content = m.content.substring(0, 300) + (m.content.length > 300 ? '...' : '');
                    return `<div class="msg ${cls}"><span class="msg-label">${label}:</span> ${content}</div>`;
                }).join('');

                const isActive = t.status !== 'done';
                const actions = isActive
                    ? `<button class="btn-close" onclick="closeTask('${t.task_id}')">關閉</button>`
                    : '';
                const deleteBtn = `<button class="btn-delete" onclick="deleteTask('${t.task_id}')">刪除</button>`;

                return `
                    <div class="card">
                        <div class="card-header">
                            <span class="card-title">${agentDisplayName(t.agent_name)}</span>
                            <div class="actions">
                                <span class="badge ${badgeClass(t.status)}">${statusLabel(t.status)}</span>
                                ${actions}
                                ${deleteBtn}
                            </div>
                        </div>
                        <div class="meta">
                            <span class="meta-item">&#x1f4ac; ${sourceLabel(t.source)}</span>
                            <span class="meta-item">&#x1f4dd; ${t.conversation_history.length} 則訊息</span>
                            <span class="meta-item">&#x1f552; ${timeAgo(t.updated_at)}</span>
                        </div>
                        <div class="history">${history}</div>
                    </div>
                `;
            }).join('');
        }

        async function closeTask(taskId) {
            await fetch('/dashboard/task/' + taskId + '/close', {method: 'POST'});
            loadAll();
        }

        async function deleteTask(taskId) {
            if (!confirm('確定要刪除這個對話？')) return;
            await fetch('/dashboard/task/' + taskId + '/delete', {method: 'POST'});
            loadAll();
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
                <div class="stat"><div class="stat-value">${agentCount}</div><div class="stat-label">在線 Agent</div></div>
                <div class="stat"><div class="stat-value">${taskInfo.active}</div><div class="stat-label">進行中</div></div>
                <div class="stat"><div class="stat-value">${taskInfo.total}</div><div class="stat-label">全部對話</div></div>
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


async def handle_task_close(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    request.app["task_manager"].close_task(task_id)
    return web.json_response({"status": "ok"})


async def handle_task_delete(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    tm = request.app["task_manager"]
    tm._conn.execute("DELETE FROM task_messages WHERE task_id = ?", (task_id,))
    tm._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    tm._conn.commit()
    return web.json_response({"status": "ok"})
