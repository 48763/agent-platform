# hub/dashboard.py
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
        .card-title { font-weight: 600; color: #f0f6fc; font-size: 1em; }
        .card-desc { color: #8b949e; font-size: 0.85em; margin-bottom: 8px; }

        .badge { padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 600; display: inline-block; }
        .badge-online { background: #238636; color: #fff; }
        .badge-offline { background: #f85149; color: #fff; }
        .badge-error { background: #f85149; color: #fff; }
        .badge-unauthenticated { background: #da3633; color: #fff; }
        .badge-disabled { background: #d29922; color: #000; }

        .error-msg { background: #2d1b1b; border: 1px solid #f8514930; border-radius: 4px; padding: 8px 12px; margin-top: 8px; color: #f85149; font-size: 0.8em; font-family: monospace; }
        .btn-link { background: none; border: 1px solid #58a6ff; color: #58a6ff; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 0.8em; text-decoration: none; }
        .btn-link:hover { background: #58a6ff; color: #fff; }
        .badge-working { background: #1f6feb; color: #fff; }
        .badge-waiting { background: #d29922; color: #000; }
        .badge-done { background: #8b949e; color: #fff; }
        .badge-closed { background: #484f58; color: #8b949e; }

        .meta { color: #8b949e; font-size: 0.85em; margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
        .meta-item { display: flex; align-items: center; gap: 4px; }

        .agent-stats { display: flex; gap: 16px; margin-top: 8px; flex-wrap: wrap; }
        .agent-stat { background: #0d1117; border-radius: 4px; padding: 6px 12px; font-size: 0.8em; }
        .agent-stat-value { color: #58a6ff; font-weight: 600; }
        .agent-stat-label { color: #8b949e; }
        .agent-patterns { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
        .agent-pattern { background: #0d1117; color: #7ee787; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; font-family: monospace; }

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

        .btn { border: none; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 0.8em; font-weight: 500; }
        .btn-danger { background: none; border: 1px solid #f85149; color: #f85149; }
        .btn-danger:hover { background: #f85149; color: #fff; }
        .btn-warn { background: none; border: 1px solid #d29922; color: #d29922; }
        .btn-warn:hover { background: #d29922; color: #000; }
        .btn-success { background: none; border: 1px solid #238636; color: #238636; }
        .btn-success:hover { background: #238636; color: #fff; }
        .actions { display: flex; gap: 6px; align-items: center; }

        .section { margin-bottom: 24px; }
        .section-header { display: flex; justify-content: space-between; align-items: center; cursor: pointer; user-select: none; color: #8b949e; margin: 20px 0 10px; font-size: 1.1em; border-bottom: 1px solid #21262d; padding-bottom: 8px; }
        .section-header:hover { color: #58a6ff; }
        .section-toggle { font-size: 0.8em; color: #484f58; }
        .section-body { transition: max-height 0.3s ease; overflow: hidden; }
        .section-body.collapsed { max-height: 0 !important; }

        .search-box { width: 100%; padding: 8px 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 0.9em; margin-bottom: 12px; outline: none; }
        .search-box:focus { border-color: #58a6ff; }
        .search-box::placeholder { color: #484f58; }
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <h1>Agent Platform</h1>
            <div style="display:flex;gap:16px;align-items:center">
                <span class="refresh" onclick="loadAll()">&#x21bb; 重新整理</span>
                <a href="/auth/logout" style="color:#8b949e;font-size:0.8em;text-decoration:none">登出</a>
            </div>
        </div>
        <div class="stats" id="stats"></div>

        <div class="section">
            <div class="section-header" onclick="toggleSection('gateways')">
                <span>Gateway 連線 <span class="section-toggle" id="gateways-toggle">&#x25BC;</span></span>
                <span style="font-size:0.8em;font-weight:normal" id="gateways-count"></span>
            </div>
            <div class="section-body" id="gateways-body">
                <div id="gateways"></div>
            </div>
        </div>

        <div class="section">
            <div class="section-header" onclick="toggleSection('agents')">
                <span>Agents <span class="section-toggle" id="agents-toggle">&#x25BC;</span></span>
                <span style="font-size:0.8em;font-weight:normal" id="agents-count"></span>
            </div>
            <div class="section-body" id="agents-body">
                <input class="search-box" id="agents-search" placeholder="搜尋 agent 名稱、描述、關鍵字..." oninput="filterAgents()">
                <div id="agents"></div>
            </div>
        </div>

        <div class="section">
            <div class="section-header" onclick="toggleSection('tasks')">
                <span>對話紀錄 <span class="section-toggle" id="tasks-toggle">&#x25BC;</span></span>
                <span style="font-size:0.8em;font-weight:normal" id="tasks-count"></span>
            </div>
            <div class="section-body" id="tasks-body">
                <input class="search-box" id="tasks-search" placeholder="搜尋對話內容、agent 名稱..." oninput="renderTasks()">
                <div class="tabs">
                    <div class="tab active" onclick="filterTasks('all', this)">全部</div>
                    <div class="tab" onclick="filterTasks('active', this)">處理中</div>
                    <div class="tab" onclick="filterTasks('done', this)">已完成</div>
                    <div class="tab" onclick="filterTasks('archived', this)">已封存</div>
                    <div class="tab" onclick="filterTasks('closed', this)">已關閉</div>
                </div>
                <div id="tasks"></div>
            </div>
        </div>
    </div>
    <script>
        let allTasks = [];
        let currentFilter = 'all';

        function timeAgo(ts) {
            if (!ts) return '-';
            const s = Math.floor(Date.now()/1000 - ts);
            if (s < 0) return '剛剛';
            if (s < 60) return s + ' 秒前';
            if (s < 3600) return Math.floor(s/60) + ' 分鐘前';
            if (s < 86400) return Math.floor(s/3600) + ' 小時前';
            return Math.floor(s/86400) + ' 天前';
        }

        function duration(secs) {
            if (secs < 60) return secs + ' 秒';
            if (secs < 3600) return Math.floor(secs/60) + ' 分鐘';
            if (secs < 86400) return Math.floor(secs/3600) + ' 小時';
            return Math.floor(secs/86400) + ' 天';
        }

        function agentDisplayName(name) {
            if (name === '_hub') return 'Hub 閒聊';
            return name;
        }

        function statusLabel(status) {
            const map = {
                'working': '處理中', 'waiting_input': '等待回覆', 'waiting_approval': '等待授權',
                'done': '已完成', 'archived': '已封存', 'closed': '已關閉'
            };
            return map[status] || status;
        }

        function badgeClass(status) {
            if (status === 'working') return 'badge-working';
            if (status.startsWith('waiting')) return 'badge-waiting';
            if (status === 'done') return 'badge-done';
            if (status === 'online') return 'badge-online';
            if (status === 'offline') return 'badge-offline';
            if (status === 'disabled') return 'badge-disabled';
            return 'badge-closed';
        }

        function agentStatusLabel(status) {
            const map = { 'online': '在線', 'offline': '離線', 'disabled': '已停用', 'error': '錯誤', 'unauthenticated': '未認證' };
            return map[status] || status;
        }

        function sourceLabel(source) {
            const map = { 'telegram': 'Telegram', 'discord': 'Discord', 'line': 'Line' };
            return map[source] || source || 'Telegram';
        }

        async function loadAgents() {
            const res = await fetch('/dashboard/agents');
            const data = await res.json();
            allAgents = data.agents;
            const onlineCount = data.agents.filter(a => a.status === 'online').length;
            document.getElementById('agents-count').textContent = `${onlineCount} 在線 / ${data.agents.length} 總計`;
            filterAgents();
            return onlineCount;
        }

        function renderAgents(agents) {
            const el = document.getElementById('agents');
            if (!agents.length) {
                el.innerHTML = '<div class="empty">沒有匹配的 Agent</div>';
                return;
            }
            el.innerHTML = agents.map(a => {
                const s = a.stats;
                const successRate = s.total_tasks > 0 ? Math.round(s.success / s.total_tasks * 100) : '-';

                const toggleBtn = a.status === 'disabled'
                    ? `<button class="btn btn-success" onclick="enableAgent('${a.name}')">啟用</button>`
                    : (a.status !== 'error' && a.status !== 'unauthenticated')
                        ? `<button class="btn btn-warn" onclick="disableAgent('${a.name}')">停用</button>`
                        : '';

                const dashboardBtn = a.has_dashboard
                    ? `<a class="btn-link" href="/dashboard/agent/${a.name}/proxy">Dashboard</a>`
                    : '';

                const errorMsg = a.error
                    ? `<div class="error-msg">${a.error}</div>`
                    : '';

                const patterns = a.route_patterns.map(p =>
                    p.split('|').map(kw => `<span class="agent-pattern">${kw}</span>`).join('')
                ).join('');

                return `
                    <div class="card">
                        <div class="card-header">
                            <span class="card-title">${a.name}</span>
                            <div class="actions">
                                ${dashboardBtn}
                                <span class="badge ${badgeClass(a.status)}">${agentStatusLabel(a.status)}</span>
                                ${toggleBtn}
                            </div>
                        </div>
                        <div class="card-desc">${a.description}</div>${errorMsg}
                        <div class="agent-stats">
                            <div class="agent-stat"><span class="agent-stat-label">優先級:</span> <span class="agent-stat-value">${a.priority}</span></div>
                            <div class="agent-stat"><span class="agent-stat-label">處理任務:</span> <span class="agent-stat-value">${s.total_tasks}</span></div>
                            <div class="agent-stat"><span class="agent-stat-label">成功率:</span> <span class="agent-stat-value">${successRate}%</span></div>
                            <div class="agent-stat"><span class="agent-stat-label">平均回應:</span> <span class="agent-stat-value">${s.avg_response_ms}ms</span></div>
                            <div class="agent-stat"><span class="agent-stat-label">在線時長:</span> <span class="agent-stat-value">${duration(a.uptime_seconds)}</span></div>
                            <div class="agent-stat"><span class="agent-stat-label">WS:</span> <span class="agent-stat-value">${a.ws_connected ? '已連線' : '未連線'}</span></div>
                        </div>
                        <div class="agent-patterns"><span class="agent-stat-label" style="font-size:0.8em;margin-right:4px">關鍵字:</span>${patterns}</div>
                    </div>
                `;
            }).join('');
        }

        async function disableAgent(name) {
            await fetch('/dashboard/agent/' + name + '/disable', {method: 'POST'});
            loadAll();
        }

        async function enableAgent(name) {
            await fetch('/dashboard/agent/' + name + '/enable', {method: 'POST'});
            loadAll();
        }

        async function loadTasks() {
            const res = await fetch('/dashboard/tasks');
            const data = await res.json();
            allTasks = data.tasks;
            const active = data.tasks.filter(t => ['working','waiting_input','waiting_approval'].includes(t.status)).length;
            document.getElementById('tasks-count').textContent = `${active} 處理中 / ${data.tasks.length} 總計`;
            renderTasks();
            return {active, total: data.tasks.length};
        }

        function renderTasks() {
            const el = document.getElementById('tasks');
            let tasks = allTasks;
            if (currentFilter === 'active') tasks = tasks.filter(t => ['working','waiting_input','waiting_approval'].includes(t.status));
            if (currentFilter === 'done') tasks = tasks.filter(t => t.status === 'done');
            if (currentFilter === 'archived') tasks = tasks.filter(t => t.status === 'archived');
            if (currentFilter === 'closed') tasks = tasks.filter(t => t.status === 'closed');

            const q = document.getElementById('tasks-search')?.value?.toLowerCase();
            if (q) {
                tasks = tasks.filter(t =>
                    t.agent_name.toLowerCase().includes(q) ||
                    t.conversation_history.some(m => m.content.toLowerCase().includes(q))
                );
            }

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

                const isActive = ['working','waiting_input','waiting_approval'].includes(t.status);
                let actions = '';
                if (isActive) {
                    actions = `<button class="btn btn-warn" onclick="closeTask('${t.task_id}')">關閉</button>`;
                } else if (t.status === 'closed') {
                    actions = `<button class="btn btn-success" onclick="reopenTask('${t.task_id}')">重新完成</button>`;
                }
                const deleteBtn = `<button class="btn btn-danger" onclick="deleteTask('${t.task_id}')">刪除</button>`;

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

        async function reopenTask(taskId) {
            await fetch('/dashboard/task/' + taskId + '/reopen', {method: 'POST'});
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

        async function loadGateways() {
            const res = await fetch('/dashboard/gateways');
            const data = await res.json();
            document.getElementById('gateways-count').textContent = data.gateways.length + ' 個連線';
            const el = document.getElementById('gateways');
            if (!data.gateways.length) {
                el.innerHTML = '<div class="empty">沒有 Gateway 連線</div>';
                return data.gateways.length;
            }
            el.innerHTML = data.gateways.map(g => `
                <div class="card">
                    <div class="card-header">
                        <span class="card-title">Gateway (${g.mode || 'unknown'})</span>
                        <span class="badge badge-online">已連線</span>
                    </div>
                    <div class="meta">
                        ${g.phone ? '<span class="meta-item">📱 ' + g.phone + '</span>' : ''}
                        ${g.allowed_chats ? '<span class="meta-item">💬 ' + g.allowed_chats.length + ' 個群組</span>' : ''}
                    </div>
                </div>
            `).join('');
            return data.gateways.length;
        }

        async function loadAll() {
            const agentCount = await loadAgents();
            const taskInfo = await loadTasks();
            const gwCount = await loadGateways();
            document.getElementById('stats').innerHTML = `
                <div class="stat"><div class="stat-value">${agentCount}</div><div class="stat-label">在線 Agent</div></div>
                <div class="stat"><div class="stat-value">${gwCount}</div><div class="stat-label">Gateway 連線</div></div>
                <div class="stat"><div class="stat-value">${taskInfo.active}</div><div class="stat-label">處理中</div></div>
                <div class="stat"><div class="stat-value">${taskInfo.total}</div><div class="stat-label">全部對話</div></div>
            `;
        }

        function toggleSection(name) {
            const body = document.getElementById(name + '-body');
            const toggle = document.getElementById(name + '-toggle');
            body.classList.toggle('collapsed');
            toggle.innerHTML = body.classList.contains('collapsed') ? '&#x25B6;' : '&#x25BC;';
        }

        let allAgents = [];

        function filterAgents() {
            const q = document.getElementById('agents-search').value.toLowerCase();
            const el = document.getElementById('agents');
            let agents = allAgents;
            if (q) {
                agents = agents.filter(a =>
                    a.name.toLowerCase().includes(q) ||
                    a.description.toLowerCase().includes(q) ||
                    a.route_patterns.join(' ').toLowerCase().includes(q)
                );
            }
            renderAgents(agents);
        }

        loadAll();
        setInterval(loadAll, 10000);
    </script>
</body>
</html>"""


async def handle_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_dashboard_agents(request: web.Request) -> web.Response:
    agents = request.app["registry"].list_all()
    return web.json_response({"agents": agents})


async def handle_agent_disable(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    request.app["registry"].disable(name)
    return web.json_response({"status": "ok"})


async def handle_agent_enable(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    request.app["registry"].enable(name)
    return web.json_response({"status": "ok"})


async def handle_dashboard_tasks(request: web.Request) -> web.Response:
    tm = request.app["task_manager"]
    rows = tm._conn.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    tasks = [tm._row_to_dict(r) for r in rows]
    return web.json_response({"tasks": tasks})


async def handle_task_close(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    task_manager = request.app["task_manager"]
    task = task_manager.get_task(task_id)

    if task and task["status"] in ("working", "waiting_input", "waiting_approval"):
        # Send cancel to agent via WS
        registry = request.app["registry"]
        agent_ws = registry.get_ws(task["agent_name"])
        if agent_ws:
            from core.ws import ws_msg, MsgType
            await agent_ws.send_str(ws_msg(MsgType.CANCEL, task_id=task_id))

    task_manager.close_task(task_id)
    return web.json_response({"status": "ok"})


async def handle_task_reopen(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    request.app["task_manager"].reopen_task(task_id)
    return web.json_response({"status": "ok"})


async def handle_task_delete(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    tm = request.app["task_manager"]
    tm._conn.execute("DELETE FROM task_messages WHERE task_id = ?", (task_id,))
    tm._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    tm._conn.commit()
    return web.json_response({"status": "ok"})
