"""
SeleneTV 专用 API —— 用户管理与鉴权（只增模块）。

设计要点
--------
1. **独立用户表** `selenetv_users`：vodsync 本身是单租户（无多用户表），
   SeleneTV 需要「多用户 + 每用户 18+ 权限」，故单独建表，独立 MetaData，
   不碰 vodsync 任何现有表/字段。
2. **向后兼容（零影响）**：鉴权「用户表优先、表外回退面板密码」。
   - 表里有该用户 → 按用户表密码校验，并携带该用户 allow_adult 权限；
   - 表里没有该用户名（存量场景，如 aiden）→ 回退到 vodsync 面板密码校验，
     视为 owner、allow_adult=1 —— 现有 mumu cookie 完全不失效、权限不变。
3. **密码安全**：复用 vodsync.auth 的 PBKDF2-SHA256（20 万轮）格式，不存明文；
   API 一律不返回 password_hash。
4. **18+ 权限真源**：allow_adult 只表达「用户是否允许」，18+ 源站的判定统一
   复用 vodsync.minor_mode.blocked_slugs()（与面板 18+ 标记同源），不重复造轮子。
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from .. import auth as vodsync_auth
from .. import database
from .schema import users as users_table

log = logging.getLogger("vodsync.selenetv.users")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_dict(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def get_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with database.engine.connect() as conn:
        return _row_dict(
            conn.execute(
                select(users_table).where(users_table.c.id == user_id)
            ).mappings().first()
        )


def get_by_username(username: str) -> Optional[Dict[str, Any]]:
    if not username:
        return None
    with database.engine.connect() as conn:
        return _row_dict(
            conn.execute(
                select(users_table).where(users_table.c.username == username)
            ).mappings().first()
        )


def count() -> int:
    with database.engine.connect() as conn:
        return int(conn.execute(select(func.count(users_table.c.id)).scalar() or 0))


def list_all() -> List[Dict[str, Any]]:
    """返回用户列表（不含 password_hash）。"""
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                users_table.c.id,
                users_table.c.username,
                users_table.c.role,
                users_table.c.allow_adult,
                users_table.c.enabled,
                users_table.c.created_at,
                users_table.c.updated_at,
            ).order_by(users_table.c.id)
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def create(username: str, password: str, *, role: str = "user",
           allow_adult: bool = False, enabled: bool = True) -> Dict[str, Any]:
    username = str(username or "").strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    now = _now_ms()
    stmt = (
        database.dialect_insert(users_table)
        .values(
            username=username,
            password_hash=vodsync_auth.hash_password(password),
            role=role or "user",
            allow_adult=1 if allow_adult else 0,
            enabled=1 if enabled else 0,
            created_at=now,
            updated_at=now,
        )
    )
    with database.write_txn() as conn:
        try:
            result = conn.execute(stmt)
            new_id = int(result.inserted_primary_key[0]) if result.inserted_primary_key else 0
        except Exception as exc:  # noqa: BLE001 - 唯一约束冲突转业务错误
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ValueError("用户名已存在") from exc
            raise
    return get_by_id(new_id) if new_id else {}


def update(user_id: int, *, password: Optional[str] = None,
           role: Optional[str] = None, allow_adult: Optional[bool] = None,
           enabled: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    values: Dict[str, Any] = {"updated_at": _now_ms()}
    if password:
        values["password_hash"] = vodsync_auth.hash_password(password)
    if role is not None:
        values["role"] = role
    if allow_adult is not None:
        values["allow_adult"] = 1 if allow_adult else 0
    if enabled is not None:
        values["enabled"] = 1 if enabled else 0
    with database.write_txn() as conn:
        result = conn.execute(
            users_table.update().where(users_table.c.id == user_id).values(**values)
        )
        if not result.rowcount:
            return None
    return get_by_id(user_id)


def delete(user_id: int) -> bool:
    with database.write_txn() as conn:
        result = conn.execute(
            users_table.delete().where(users_table.c.id == user_id)
        )
        return bool(result.rowcount)


# ---------------------------------------------------------------------------
# 鉴权（含兼容回退）
# ---------------------------------------------------------------------------
def verify(username: str, password: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    校验登录凭据。返回 (ok, user_or_None, reason)。
      - user 不为 None：命中用户表，携带 allow_adult / enabled 等权限；
      - user 为 None：兼容 owner（表外回退面板密码），视为 allow_adult=1。
    """
    if not username or not password:
        return False, None, "empty"
    row = get_by_username(username)
    if row is not None:
        if not row.get("enabled"):
            return False, None, "disabled"
        if not vodsync_auth.verify_password(password, row.get("password_hash", "")):
            return False, None, "bad_password"
        return True, row, "table"
    # 表外回退：存量单租户场景（面板密码）
    if vodsync_auth.verify_password(password):
        return True, None, "compat_owner"
    return False, None, "bad_password"


def allows_adult(user: Optional[Dict[str, Any]]) -> bool:
    """
    用户是否允许访问 18+ 源。
    - None（兼容 owner）：默认全开，保持现状；
    - 表内用户：按 allow_adult 字段。
    """
    if user is None:
        return True
    return int(user.get("allow_adult") or 0) == 1
