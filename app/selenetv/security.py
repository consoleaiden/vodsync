"""
SeleneTV 专用 API —— 鉴权。

完全复刻 Helios 的 Cookie 契约，SeleneTV 客户端零改动：
  - Cookie 名 "auth"，值 = urlencode(urlencode(JSON{role,username,password}))
    （Go url.QueryEscape 把空格编码成 '+'，对应 Python quote_plus）
  - 登录返回 {"ok":true}，Set-Cookie: auth=<双编码JSON>; Path=/; SameSite=Lax;
    Expires=now+7d; HttpOnly=false; Secure=false
  - AuthMiddleware 失败一律 text/plain: "Authentication required" / "Invalid credentials"

与 Helios 的唯一差异（更强，不破坏契约）：
  - Helios 把明文口令放环境变量、明文比对；这里口令存在 vodsync settings 表
    （PBKDF2-SHA256 20 万轮），校验走 vodsync.auth.verify_password，不落明文到日志。
  - username 单租户语义：仅做非空校验，不做多用户隔离（与 Helios 单租户一致）。
"""
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, unquote_plus

from fastapi import Request, Response

from .. import auth as vodsync_auth
from .. import config as vodsync_config

COOKIE_NAME = "auth"            # Helios 固定 cookie 名
SESSION_DAYS = 7                # Helios 固定 7 天（对齐其 Expires）
ROLE_OWNER = "owner"            # Helios 登录固定写 role=owner（鉴权不校验 role）


def text_error(status: int, message: str) -> Response:
    """
    构造 Helios 风格纯文本错误响应。
    Go http.Error 行为：body 末尾追加 \n，Content-Type: text/plain; charset=utf-8，
    并带 X-Content-Type-Options: nosniff。
    """
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        content=message + "\n",
        status_code=status,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _encode_auth_cookie(username: str, password: str) -> str:
    """双重 urlencode 的 AuthInfo JSON —— 与 Helios login.go 完全一致。"""
    payload = json.dumps(
        {"role": ROLE_OWNER, "username": username, "password": password},
        ensure_ascii=False,
        separators=(",", ":"),   # sonic 紧凑输出
    )
    return quote_plus(quote_plus(payload))


def _decode_auth_cookie(value: str):
    """双重 urldecode + JSON 解析；失败返回 None（对应 Helios Invalid authentication data）。"""
    try:
        raw = unquote_plus(unquote_plus(value))
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if not all(k in data for k in ("username", "password")):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def check_credentials(username: str, password: str) -> bool:
    """
    校验登录凭据（用户表优先、表外回退面板密码，零影响存量用户）。

    注意：不检查 vodsync.auth.is_enabled() —— Helios 的 /api/login 永远校验口令，
    SeleneTV 也永远需要登录；vodsync 的"关闭鉴权"是它自己面板的开关，语义不同。
    """
    ok, _, _ = resolve_user(username, password)
    return ok


def resolve_user(username: str, password: str):
    """
    解析凭据 → (ok, user_row_or_None, reason)。

    统一走 users.verify：命中 selenetv_users 表则按用户密码校验并携带权限；
    表外用户名回退到 vodsync 面板密码（兼容 owner，allow_adult=1）。
    """
    from . import users as users_repo

    return users_repo.verify(username, password)


def current_user(request: Request):
    """
    从 auth cookie 解析当前用户（登录后的权限判定入口）。

    Returns:
        user_row(dict) 命中用户表；None 表示兼容 owner（面板密码/未建表用户）。
        未登录或凭据失效返回 False。
    """
    raw = request.cookies.get(COOKIE_NAME, "")
    if not raw:
        return False
    data = _decode_auth_cookie(raw)
    if data is None:
        return False
    ok, user, _ = resolve_user(data.get("username", ""), data.get("password", ""))
    if not ok:
        return False
    return user  # None = 兼容 owner，有权限判定函数按全开处理


def allows_adult(request: Request) -> bool:
    """当前请求用户是否允许访问 18+ 源站（未登录按禁止处理，安全侧收口）。"""
    user = current_user(request)
    if user is False:
        return False
    from . import users as users_repo

    return users_repo.allows_adult(user)


def issue_login_response(username: str, password: str) -> Response:
    """登录成功：{"ok":true} + 双重编码 auth cookie（Helios 风格）。"""
    from fastapi.responses import JSONResponse

    resp = JSONResponse(content={"ok": True})
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=_encode_auth_cookie(username, password),
        path="/",
        expires=expires,          # 只有 Expires，无 Max-Age（对齐 Helios）
        samesite="lax",
        httponly=False,
        secure=False,
    )
    return resp


def request_authenticated(request: Request) -> bool:
    """
    AuthMiddleware 核心：读 auth cookie → 双重解码 → 校验口令。
    """
    raw = request.cookies.get(COOKIE_NAME, "")
    if not raw:
        return False
    data = _decode_auth_cookie(raw)
    if data is None:
        return False
    return check_credentials(data.get("username", ""), data.get("password", ""))


def invalidate(request: Request) -> bool:
    """仅用于自检/测试：判断请求是否带了不合法 cookie。"""
    raw = request.cookies.get(COOKIE_NAME, "")
    return bool(raw) and _decode_auth_cookie(raw) is None
