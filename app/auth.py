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
K_TOKEN_SCOPE = "api_token_scope"
K_SECRET = "session_secret"
K_PWD_AT = "password_changed_at"

SCOPES = ("read", "admin")

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


def token_present() -> bool:
    """只回答「有没有 Token」，不回显任何字符。

    Token 是唯一凭据，哪怕只露前 7 位 + 后 4 位也会把搜索空间从 43 位降到十几位，
    写进日志/接口里都是没必要的攻击面。
    """
    return bool(get_token())


def rotate_token(scope: str = "admin") -> str:
    """
    生成新 Token（旧的立即失效）。

    v1.14.0：带 scope。默认 admin —— 存量调用方（面板「重置 Token」、旧脚本）
    不需要改一行代码，行为与 v1.13 完全一致；只有显式要 read 时才降权。
    """
    scope = scope if scope in SCOPES else "admin"
    token = "vs_" + secrets.token_urlsafe(32)
    _put(K_TOKEN, token)
    _put(K_TOKEN_AT, datetime.now().isoformat(timespec="seconds"))
    _put(K_TOKEN_SCOPE, scope)
    log.info("API Token 已更新（scope=%s，面板「设置」里可查看）", scope)
    return token


def token_scope() -> str:
    """存量单 Token 的权限。老库没有这个 key，按 admin 处理（宽权限过渡）。"""
    scope = (get(K_TOKEN_SCOPE) or "admin").strip().lower()
    return scope if scope in SCOPES else "admin"


def token_ok(supplied: str) -> bool:
    if not supplied:
        return False
    candidates = [get_token(), config.API_KEY]
    return any(c and hmac.compare_digest(supplied, c) for c in candidates)


# ---------------------------------------------------------------------------
# 多 Token 与权限粒度（v1.14.0 / B-08）
#
# 只存 sha256 摘要：库被读走不等于凭据泄露（面板密码同理）。
# 存量单 Token（settings.api_token）继续有效，权限取 K_TOKEN_SCOPE，
# 因此「升级 + 不新建 Token」的部署行为与 v1.13 完全一致 —— 这是 B-08
# 迁移四步里第 1 步（宽权限并行）的实现基础。
# ---------------------------------------------------------------------------
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_scoped_token(scope: str = "read", note: str = "") -> dict:
    """签发一枚具名 Token（明文只在这里返回一次）。"""
    from .models import api_tokens

    scope = scope if scope in SCOPES else "read"
    raw = "vs_" + secrets.token_urlsafe(32)
    now = datetime.now()
    with database.write_txn() as conn:
        rid = conn.execute(
            api_tokens.insert().values(
                token_hash=_hash(raw), scope=scope, note=(note or "")[:128],
                created_at=now, revoked=0,
            )
        ).inserted_primary_key[0]
    log.info("已签发 %s 权限 Token #%s（%s）", scope, rid, note or "无备注")
    return {"id": rid, "token": raw, "scope": scope, "note": note, "created_at": now.isoformat()}


def list_tokens() -> list:
    """
    已签发 Token 清单（**不返回明文**，只给 id/scope/时间/备注）。

    第 0 项是存量单 Token（settings 里那枚），id 固定为 0，方便客户端识别
    「这是我的老 Token」。只回答有没有，不回显任何片段 —— 理由同 token_present()。
    """
    from .models import api_tokens

    out = [{
        "id": 0,
        "kind": "legacy",
        "scope": token_scope(),
        "note": "存量单 Token（面板「设置」可查看/重置）",
        "created_at": token_created_at() or None,
        "last_used_at": None,
        "present": token_present(),
    }]
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                api_tokens.select().where(api_tokens.c.revoked == 0)
                .order_by(api_tokens.c.id)
            ).mappings().all()
    except Exception as e:  # noqa: BLE001
        log.debug("读取 Token 清单失败: %s", e)
        rows = []
    out.extend({
        "id": r["id"],
        "kind": "scoped",
        "scope": r["scope"],
        "note": r["note"] or "",
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
        "present": True,
    } for r in rows)
    return out


def revoke_scoped_token(token_id: int) -> bool:
    """吊销一枚具名 Token（幂等；已吊销返回 False）。"""
    from .models import api_tokens

    if int(token_id or 0) == 0:
        return False   # 存量 Token 只能通过「重置」轮换，不能单独吊销
    with database.write_txn() as conn:
        res = conn.execute(
            api_tokens.update()
            .where(api_tokens.c.id == int(token_id), api_tokens.c.revoked == 0)
            .values(revoked=1)
        )
    return bool(res.rowcount)


def resolve_scope(supplied: str) -> str:
    """
    解析一枚 Token 的权限：admin / read，无效 Token 返回空串。

    比对顺序：存量单 Token -> API_KEY 环境变量 -> api_tokens 表。
    命中具名 Token 时顺手更新 last_used_at（失败忽略，不能因为它挂了请求）。
    """
    if not supplied:
        return ""
    legacy = get_token()
    if legacy and hmac.compare_digest(supplied, legacy):
        return token_scope()
    if config.API_KEY and hmac.compare_digest(supplied, config.API_KEY):
        return config.API_KEY_SCOPE if config.API_KEY_SCOPE in SCOPES else "admin"
    from .models import api_tokens

    try:
        with database.engine.connect() as conn:
            row = conn.execute(
                api_tokens.select().where(
                    api_tokens.c.token_hash == _hash(supplied),
                    api_tokens.c.revoked == 0,
                )
            ).mappings().first()
        if not row:
            return ""
        try:
            with database.write_txn() as conn:
                conn.execute(
                    api_tokens.update().where(api_tokens.c.id == row["id"])
                    .values(last_used_at=datetime.now())
                )
        except Exception:  # noqa: BLE001
            pass
        return row["scope"] if row["scope"] in SCOPES else "read"
    except Exception:  # noqa: BLE001
        return ""


def request_scope(request: Request) -> str:
    """
    本请求的权限：会话 cookie（面板）恒为 admin；Token 按其 scope。

    返回空串表示未通过鉴权（由调用方决定是否 401）。
    """
    if not is_enabled():
        return "admin"
    if has_session(request):
        return "admin"
    return resolve_scope(extract_token(request))


# 需要 admin 的路径前缀（读接口不在此列：列表/详情/搜索/分类/统计/暂停状态）
_ADMIN_PREFIXES = ("/api/v1/auth/", "/api/v1/admin/", "/api/v1/maintenance/")


def requires_admin(method: str, path: str) -> bool:
    """
    该请求是否需要 admin 权限。

    规则刻意简单（可解释、可审计）：
      - 非 GET/HEAD（任何写操作、同步触发、词库 CRUD）-> admin
      - /auth/*  /admin/*  /maintenance/*  -> admin（即使 GET，也含敏感信息）
      - /healthz 永远免鉴权（在 PUBLIC_PATHS 里，走不到这里）
    单条例外：/api/v1/star/tags/status、/related/status 这类**只读统计**保留给
    read —— 它们只返回覆盖率数字，不含任何写入口。
    """
    if method in ("GET", "HEAD", "OPTIONS"):
        if path in ("/api/v1/related/status", "/api/v1/star/tags/status"):
            return False
        return any(path.startswith(p) for p in _ADMIN_PREFIXES)
    # 登录/登出本身就是未登录时的入口，不能要求 admin
    if path in ("/api/v1/auth/login", "/api/v1/auth/logout"):
        return False
    return True


def _ver_tuple(text: str) -> tuple:
    """把 "1.5.3" / "v1.5.3" 解析成 (1,5,3)；解析不了返回 ()。"""
    import re as _re

    m = _re.match(r"^[vV]?(\d+)\.(\d+)(?:\.(\d+))?", (text or "").strip())
    if not m:
        return ()
    return tuple(int(x or 0) for x in m.groups())


def legacy_client_exempt(request: Request) -> bool:
    """
    老客户端宽限（B-08 第 4 步「按客户端版本放行」）。

    强制 scope 后，只读 Token 的 App 调写接口会 401/403；但 v1.5.x 那种老版本
    根本不知道要拿 admin Token。所以：
      - 客户端用 X-Client-Version 头或 client_version 参数自报版本；
      - 低于 TOKEN_SCOPE_LEGACY_BELOW 的，按 admin 放行；
      - **没自报版本的也放行**（宽进严出）—— 宁可放过老客户端，也不能让它整站失效。
    想真正收紧时，把 TOKEN_SCOPE_LEGACY_BELOW 设为 "0.0.0"（视为无老版本）。
    """
    threshold = _ver_tuple(config.TOKEN_SCOPE_LEGACY_BELOW)
    if not threshold:
        return False   # 未配置阈值 = 不开启版本宽限（严格模式）
    raw = (request.headers.get("x-client-version")
           or request.query_params.get("client_version") or "").strip()
    if not raw:
        return True    # 没自报版本：当老客户端处理（宽进严出）
    ver = _ver_tuple(raw)
    if not ver:
        return True
    return ver < threshold


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
    """
    API 请求是否放行：Token（客户端）或会话 cookie（面板 JS）任一有效。

    v1.14.0 / B-08：判据从 token_ok（只认存量单 Token 与 API_KEY）换成
    resolve_scope —— 后者还会查 api_tokens 表，否则新签发的只读 Token
    拿着合法凭据却一路 401。权限够不够由 main._auth_guard 另行判断，
    这里只回答「这枚 Token 是否存在且未吊销」。
    """
    if not is_enabled():
        return True
    if has_session(request):
        return True
    return bool(resolve_scope(extract_token(request)))


PUBLIC_PATHS = {
    "/healthz",              # 存活探针必须放行，否则容器永远不健康
    "/login",
    "/api/v1/auth/login",
    "/api/v1/auth/state",
    "/favicon.ico",
}

# 返回 JSON 的接口路径。中间件对它们走「Token 或会话」判定。
# 注意 `/source`（源站清单）不在 /api/ 前缀下，但同样是对外 API ——
# 漏掉它会让只带 Token 的客户端收到 401「请先登录管理面板」。
API_PATHS = ("/source",)


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def is_api_path(path: str) -> bool:
    return path.startswith("/api/") or path in API_PATHS


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
    if not get(K_TOKEN_SCOPE):
        # v1.14.0：存量 Token 一律 admin（宽权限过渡第 1 步），
        # 不写这一条的话每次都走 get() 兜底，面板里也看不到它的权限
        _put(K_TOKEN_SCOPE, "admin")
    if get(K_ENABLED) == "":
        set_enabled(config.AUTH_ENABLED)
    log.info(
        "访问控制：%s（API Token %s）",
        "已启用" if is_enabled() else "未启用",
        "已生成" if token_present() else "未生成",
    )
