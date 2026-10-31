"""
访问控制（v1.5.0）：管理面板登录 + API Token。

设计取舍
--------
1. 状态全部放 `settings` 表（key/value），升级不用迁移，也不用引 alembic；
   进程内再加一层内存缓存，鉴权发生在每个请求上，不能每次都查库。
2. 密码用 PBKDF2-SHA256（20 万轮）+ 随机盐，校验用 compare_digest（防时序侧信道）。
3. 会话是**无状态签名 cookie**（HMAC-SHA256），签名里带密码指纹 ——
   改密码后旧 cookie 立刻失效，不用维护服务端会话表。
4. Token 只有一枚，可随时重置；同时兼容旧的 `API_KEY` 环境变量（多一枚有效值）。
5. 登录失败按 IP 计数，连续失败 N 次锁 M 分钟 —— 默认密码是六位数字，不防爆破等于没有。
"""
import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime

from fastapi import Request
from sqlalchemy import text

from . import config, database
from .models import settings as _settings_tbl

log = logging.getLogger("vodsync.auth")

COOKIE = "vodsync_session"

K_ENABLED = "auth_enabled"
K_PWD = "admin_password_hash"
K_TOKEN = "api_token"
K_TOKEN_AT = "api_token_created_at"
K_SECRET = "session_secret"
K_PWD_AT = "password_changed_at"

PBKDF2_ROUNDS = 200_000

_cache: dict = {}
_lock = threading.Lock()
_fails: dict = {}          # ip -> [失败次数, 解锁时间戳]


# ---------------------------------------------------------------------------
# settings 读写（带缓存）
# ---------------------------------------------------------------------------
def get(key: str, default: str = "") -> str:
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        with database.engine.connect() as conn:
            row = conn.execute(
                text('SELECT value FROM settings WHERE "key" = :k'), {"k": key}
            ).first()
    except Exception:  # noqa: BLE001 —— 表还没建好/库暂时不可用时不阻塞请求
        return default
    value = "" if row is None else (row[0] or "")
    with _lock:
        _cache[key] = value
    return value


def _put(key: str, value: str) -> None:
    now = datetime.now()
    stmt = database.dialect_insert(_settings_tbl).values(
        key=key, value=value, updated_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[_settings_tbl.c.key],
        set_={"value": value, "updated_at": now},
    )
    with database.write_txn() as conn:
        conn.execute(stmt)
    with _lock:
        _cache[key] = value


def reset_cache() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str = "") -> bool:
    stored = stored or get(K_PWD)
    if not password or not stored:
        return False
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _unb64(salt_b64), int(rounds)
        )
        return hmac.compare_digest(dk, _unb64(hash_b64))
    except Exception:  # noqa: BLE001
        return False


def set_password(password: str) -> None:
    _put(K_PWD, hash_password(password))
    _put(K_PWD_AT, datetime.now().isoformat(timespec="seconds"))


def password_changed_at() -> str:
    return get(K_PWD_AT)


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    value = get(K_ENABLED)
    if value == "":
        return config.AUTH_ENABLED
    return value == "1"


def set_enabled(flag: bool) -> None:
    _put(K_ENABLED, "1" if flag else "0")


# ---------------------------------------------------------------------------
# API Token
# ---------------------------------------------------------------------------
def get_token() -> str:
    return get(K_TOKEN)


def token_created_at() -> str:
    return get(K_TOKEN_AT)


def token_hint() -> str:
    tok = get_token()
    return f"{tok[:7]}…{tok[-4:]}" if len(tok) > 14 else ("已生成" if tok else "未生成")


def rotate_token() -> str:
    """生成新 Token（旧的立即失效）。"""
    token = "vs_" + secrets.token_urlsafe(32)
    _put(K_TOKEN, token)
    _put(K_TOKEN_AT, datetime.now().isoformat(timespec="seconds"))
    log.info("API Token 已更新（面板「设置」里可查看）")
    return token


def token_ok(supplied: str) -> bool:
    if not supplied:
        return False
    candidates = [get_token(), config.API_KEY]
    return any(c and hmac.compare_digest(supplied, c) for c in candidates)


# ---------------------------------------------------------------------------
# 会话 cookie（无状态签名）
# ---------------------------------------------------------------------------
def _secret() -> bytes:
    secret = get(K_SECRET)
    if not secret:
        secret = secrets.token_hex(32)
        _put(K_SECRET, secret)
    return secret.encode()


def _pwd_tag() -> str:
    """密码指纹：改密码 → 所有旧会话失效。"""
    return hashlib.sha256(get(K_PWD).encode()).hexdigest()[:10]


def _sign(payload: str) -> str:
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return _b64(mac)


def make_session(days: int = None) -> str:
    days = config.SESSION_TTL_DAYS if days is None else days
    exp = int(time.time()) + max(1, days) * 86400
    payload = f"{exp}:{_pwd_tag()}:{secrets.token_urlsafe(6)}"
    return f"{payload}:{_sign(payload)}"


def session_valid(value: str) -> bool:
    if not value:
        return False
    try:
        exp_s, tag, _nonce, sig = value.rsplit(":", 3)
        payload = f"{exp_s}:{tag}:{_nonce}"
        if int(exp_s) < time.time():
            return False
        if not hmac.compare_digest(tag, _pwd_tag()):
            return False
        return hmac.compare_digest(sig, _sign(payload))
    except Exception:  # noqa: BLE001
        return False


def has_session(request: Request) -> bool:
    return session_valid(request.cookies.get(COOKIE, ""))


# ---------------------------------------------------------------------------
# 请求级判定
# ---------------------------------------------------------------------------
def extract_token(request: Request) -> str:
    """支持 3 种带法：Authorization: Bearer、X-API-Key 头、?token= 查询参数。"""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    for header in ("x-api-key", "x-token"):
        value = request.headers.get(header, "")
        if value:
            return value.strip()
    params = request.query_params
    return (params.get("token") or params.get("apikey") or "").strip()


def request_authorized(request: Request) -> bool:
    """API 请求是否放行：Token（客户端）或会话 cookie（面板 JS）任一有效。"""
    if not is_enabled():
        return True
    if token_ok(extract_token(request)):
        return True
    return has_session(request)


PUBLIC_PATHS = {
    "/healthz",              # 存活探针必须放行，否则容器永远不健康
    "/login",
    "/api/v1/auth/login",
    "/api/v1/auth/state",
    "/favicon.ico",
}


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


# ---------------------------------------------------------------------------
# 登录失败限流（内存计数，进程级；重启即清空，够用）
# ---------------------------------------------------------------------------
def _prune(now: float) -> None:
    for ip, (fails, until) in list(_fails.items()):
        if until and until < now and fails == 0:
            _fails.pop(ip, None)


def login_lock_seconds(ip: str) -> int:
    now = time.time()
    _prune(now)
    fails, until = _fails.get(ip, (0, 0))
    return max(0, int(until - now) + 1) if until > now else 0


def note_login_fail(ip: str) -> int:
    """记一次失败，返回剩余可试次数；达到阈值则上锁。"""
    fails, until = _fails.get(ip, (0, 0))
    fails += 1
    if fails >= config.LOGIN_MAX_FAILS:
        until = time.time() + config.LOGIN_LOCK_MINUTES * 60
        fails = 0
        log.warning("登录失败次数过多，IP %s 锁定 %d 分钟", ip, config.LOGIN_LOCK_MINUTES)
        _fails[ip] = (fails, until)
        return 0
    _fails[ip] = (fails, until)
    return max(0, config.LOGIN_MAX_FAILS - fails)


def reset_login_fail(ip: str) -> None:
    _fails.pop(ip, None)


# ---------------------------------------------------------------------------
# 初始化（启动时调用一次）
# ---------------------------------------------------------------------------
def ensure_init() -> None:
    reset_cache()
    if not get(K_PWD):
        _put(K_PWD, hash_password(config.ADMIN_PASSWORD))
        _put(K_PWD_AT, datetime.now().isoformat(timespec="seconds"))
        log.info("已初始化面板密码（默认 666666，请登录后立即修改）")
    if not get(K_SECRET):
        _put(K_SECRET, secrets.token_hex(32))
    if not get(K_TOKEN):
        rotate_token()
    if get(K_ENABLED) == "":
        set_enabled(config.AUTH_ENABLED)
    log.info("访问控制：%s（Token %s）", "已启用" if is_enabled() else "未启用", token_hint())
