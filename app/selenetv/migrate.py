"""
SeleneTV 专用 API —— 建表入口（幂等，只增不改）。

保证：
  - 只调用 selenetv_meta.create_all(checkfirst=True)：已存在的表一律跳过，
    不执行任何 ALTER / DROP / UPDATE，因此 vodsync 现有数据零影响。
  - 与 vodsync.database.init_db() 完全解耦：本函数由 selenetv.router 在首次
    挂载时按需调用，或运维手动 `python -m app.selenetv.migrate` 执行。
  - 失败不致命：捕获异常记日志，主服务照常启动。
"""
import logging

from .. import database
from .schema import TABLE_NAMES, selenetv_meta

log = logging.getLogger("vodsync.selenetv.migrate")


def ensure_tables(verbose: bool = False) -> dict:
    """
    幂等创建 SeleneTV 专用表。

    Returns:
        {"created": [...], "existing": [...], "ok": bool}
        created = 本次新建的表名；existing = 调用前已存在的表名。
    """
    before = set(database.list_tables())
    existing = sorted(name for name in TABLE_NAMES if name in before)
    try:
        # checkfirst=True 是"绝不碰现有表"的关键：只建缺失的表。
        selenetv_meta.create_all(database.engine, checkfirst=True)
    except Exception as exc:  # noqa: BLE001 - 建表失败不能拖垮主服务
        log.exception("SeleneTV 专用表创建失败（不影响 vodsync 现有功能）: %s", exc)
        return {"created": [], "existing": existing, "ok": False, "error": str(exc)}

    after = set(database.list_tables())
    created = sorted(name for name in TABLE_NAMES if name in after and name not in before)
    if verbose or created:
        log.info(
            "SeleneTV 专用表就绪：新建=%s，已存在跳过=%s",
            created or "无", existing or "无",
        )
    return {"created": created, "existing": existing, "ok": True}


if __name__ == "__main__":
    # 手动迁移入口：python -m app.selenetv.migrate
    result = ensure_tables(verbose=True)
    print(f"selenetv tables: ok={result['ok']} created={result['created']} "
          f"existing={result['existing']}")
