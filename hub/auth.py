# hub/auth.py
import hashlib
import os
import secrets
from aiohttp import web

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")
SESSION_COOKIE = "agent_session"

# In-memory session store
_sessions: set[str] = set()


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def is_auth_enabled() -> bool:
    return bool(DASHBOARD_PASS)


def verify(username: str, password: str) -> bool:
    return username == DASHBOARD_USER and password == DASHBOARD_PASS


def create_session() -> str:
    token = secrets.token_hex(32)
    _sessions.add(token)
    return token


def check_session(request: web.Request) -> bool:
    if not is_auth_enabled():
        return True
    token = request.cookies.get(SESSION_COOKIE)
    return token in _sessions if token else False


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Platform - 登入</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; background: #0d1117; color: #c9d1d9; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
        .login-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 32px; width: 360px; }
        .login-card h1 { color: #58a6ff; font-size: 1.3em; margin-bottom: 24px; text-align: center; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; color: #8b949e; font-size: 0.85em; margin-bottom: 6px; }
        .form-group input { width: 100%; padding: 10px 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 0.95em; outline: none; }
        .form-group input:focus { border-color: #58a6ff; }
        .btn-login { width: 100%; padding: 10px; background: #238636; border: none; border-radius: 6px; color: #fff; font-size: 0.95em; font-weight: 600; cursor: pointer; margin-top: 8px; }
        .btn-login:hover { background: #2ea043; }
        .error { color: #f85149; font-size: 0.85em; margin-top: 12px; text-align: center; display: none; }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>Agent Platform</h1>
        <form id="loginForm">
            <div class="form-group">
                <label>帳號</label>
                <input type="text" id="username" autocomplete="username" autofocus>
            </div>
            <div class="form-group">
                <label>密碼</label>
                <input type="password" id="password" autocomplete="current-password">
            </div>
            <button type="submit" class="btn-login">登入</button>
            <div class="error" id="error">帳號或密碼錯誤</div>
        </form>
    </div>
    <script>
        document.getElementById('loginForm').onsubmit = async (e) => {
            e.preventDefault();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const res = await fetch('/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username, password}),
            });
            if (res.ok) {
                window.location.href = '/';
            } else {
                document.getElementById('error').style.display = 'block';
            }
        };
    </script>
</body>
</html>"""


async def handle_login_page(request: web.Request) -> web.Response:
    if check_session(request):
        raise web.HTTPFound("/")
    return web.Response(text=LOGIN_HTML, content_type="text/html")


async def handle_login(request: web.Request) -> web.Response:
    data = await request.json()
    username = data.get("username", "")
    password = data.get("password", "")

    if verify(username, password):
        token = create_session()
        resp = web.json_response({"status": "ok"})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, max_age=86400 * 7)
        return resp

    return web.json_response({"status": "error"}, status=401)


async def handle_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.discard(token)
    resp = web.HTTPFound("/auth/login")
    resp.del_cookie(SESSION_COOKIE)
    return resp
