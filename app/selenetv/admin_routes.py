"""
SeleneTV 用户管理 —— 面板 CRUD API（只增模块）。

路径挂在 `/api/v1/admin/selenetv/users*`，走 vodsync 全局 `_auth_guard`：
浏览器面板 session 或 API Token 均可访问，未登录 401。这些路径**不在**
SeleneTV Helios 放行清单里，因此不会被 `is_selenetv_path` 绕过面板鉴权。

响应遵循 vodsync 统一包络：成功 `{"code": 0, ...}`，失败 HTTP 4xx +
`{"code": ..., "detail": ...}`（由全局异常处理器构造）。

密码只写不读：任何端点都不返回 password_hash。
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import apiresp
from . import users as users_repo

log = logging.getLogger("vodsync.selenetv.admin")

admin_router = APIRouter()


class UserIn(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    allow_adult: Optional[bool] = None
    enabled: Optional[bool] = None


def _to_view(row: dict) -> dict:
    """对外视图：绝不包含 password_hash。"""
    return {
        "id": row.get("id"),
        "username": row.get("username"),
        "role": row.get("role") or "user",
        "allow_adult": bool(row.get("allow_adult")),
        "enabled": bool(row.get("enabled")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@admin_router.get("/api/v1/admin/selenetv/users", summary="SeleneTV 用户列表")
def list_users():
    rows = users_repo.list_all()
    return {"code": apiresp.SUCCESS_CODE, "list": [_to_view(r) for r in rows]}


@admin_router.post("/api/v1/admin/selenetv/users", summary="新增 SeleneTV 用户")
def create_user(payload: UserIn):
    username = (payload.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if not payload.password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    role = (payload.role or "user").strip().lower()
    if role not in ("owner", "user"):
        raise HTTPException(status_code=400, detail="role 只能是 owner 或 user")
    try:
        row = users_repo.create(
            username, payload.password, role=role,
            allow_adult=bool(payload.allow_adult),
            enabled=payload.enabled if payload.enabled is not None else True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("[SeleneTV用户] 新建用户 %s allow_adult=%s", username, bool(payload.allow_adult))
    return {"code": apiresp.SUCCESS_CODE, "msg": "用户已创建", "detail": _to_view(row)}


@admin_router.put("/api/v1/admin/selenetv/users/{user_id}", summary="修改 SeleneTV 用户")
def update_user(user_id: int, payload: UserIn):
    if not users_repo.get_by_id(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    role = None
    if payload.role is not None:
        role = payload.role.strip().lower()
        if role not in ("owner", "user"):
            raise HTTPException(status_code=400, detail="role 只能是 owner 或 user")
    row = users_repo.update(
        user_id,
        password=payload.password or None,
        role=role,
        allow_adult=payload.allow_adult,
        enabled=payload.enabled,
    )
    log.info("[SeleneTV用户] 更新用户 id=%s allow_adult=%s enabled=%s",
             user_id, payload.allow_adult, payload.enabled)
    return {"code": apiresp.SUCCESS_CODE, "msg": "用户已更新", "detail": _to_view(row)}


@admin_router.delete("/api/v1/admin/selenetv/users/{user_id}", summary="删除 SeleneTV 用户")
def delete_user(user_id: int):
    row = users_repo.get_by_id(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 安全护栏：不允许删掉唯一启用的 owner，避免把所有人都锁在门外（兼容 owner
    # 走面板密码不受此限，但表内 owner 是显式授权，需保留至少一个）。
    if row.get("role") == "owner" and row.get("enabled"):
        owners = [r for r in users_repo.list_all()
                  if r.get("role") == "owner" and r.get("enabled")]
        if len(owners) <= 1:
            raise HTTPException(
                status_code=409,
                detail="不能删除唯一启用的 owner 用户（请先停用或改角色）",
            )
    users_repo.delete(user_id)
    log.info("[SeleneTV用户] 删除用户 id=%s username=%s", user_id, row.get("username"))
    return {"code": apiresp.SUCCESS_CODE, "msg": "用户已删除", "deleted": user_id}
