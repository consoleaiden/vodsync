"""
SeleneTV 专用 API 包（Helios 风格，独立、只增不改）。

对 vodsync 现有功能的保证：
  - 现有文件只改 main.py 两处：include_router + startup 里调 ensure_tables()；
  - 不修改任何现有表、字段、handler、鉴权逻辑；
  - 13 个 API（含 3 个多方法）全部在本包内实现，可整包删除即完全回退。

对外入口：
  from .selenetv import selenetv_router, mount  # mount(app) 一步挂载
"""
import logging

from .router import PROTECTED_PATHS, PUBLIC_PATHS, is_selenetv_path, selenetv_router

log = logging.getLogger("vodsync.selenetv")

__all__ = [
    "selenetv_router",
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
