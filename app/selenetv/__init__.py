"""
SeleneTV 专用 API 包（Helios 风格，独立、只增不改）。

对 vodsync 现有功能的保证：
  - 现有文件只改 main.py（挂载/鉴权放行/建表+直播首拉）、scheduler.py（每日拉取 job）；
  - 不修改任何现有表、字段、handler、鉴权逻辑；
  - 全部 API（含直播源融合的 /live/playlist/{key}、用户管理 /api/v1/admin/selenetv/users）
    在本包内实现，可整包删除即完全回退；
  - 用户表 selenetv_users 只增，鉴权「用户表优先、表外回退面板密码」保证存量 zero-impact。

对外入口：
  from .selenetv import selenetv_router, admin_router, mount  # mount(app) 一步挂载
"""
import logging

from .admin_routes import admin_router
from .router import PROTECTED_PATHS, PUBLIC_PATHS, is_selenetv_path, selenetv_router

log = logging.getLogger("vodsync.selenetv")

__all__ = [
    "selenetv_router",
    "admin_router",
    "mount",
    "is_selenetv_path",
    "PROTECTED_PATHS",
    "PUBLIC_PATHS",
]


def mount(app) -> None:
    """
    把 SeleneTV API 挂到 vodsync 的 FastAPI app 上（幂等，可重复调用不重复注册）。
    仅供 main.py 调用；细节见 main.py 两处增量改动注释。
    """
    # 新版 Starlette 的 app.routes 里可能混有 _IncludedRouter 等无 path 属性的对象，
    # 用 getattr 兜底，避免 mounts 检测自身抛异常。
    existing = {getattr(r, "path", None) for r in app.routes}
    if "/api/login" not in existing:
        app.include_router(selenetv_router)
        log.info("SeleneTV API 已挂载（%d 条路由）", len(PROTECTED_PATHS) + len(PUBLIC_PATHS))
    if "/api/v1/admin/selenetv/users" not in existing:
        # 面板用户管理 API（走 vodsync 会话/Token 鉴权，不属 Helios 放行清单）
        app.include_router(admin_router)
        log.info("SeleneTV 用户管理 API 已挂载（面板 CRUD）")
