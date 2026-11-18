"""
中转代理配置的读取与持久化（v1.16.7）。

历史：代理相关配置全部来自环境变量（docker-compose），改一次要重启容器：

    PROXY_ENABLED / PROXY_URLS / PROXY_PREF_TTL_MINUTES
    PROXY_CONCURRENCY / PROXY_TIMEOUT

现在管理面板可以直接改，并**立即生效**（无需重启）。配置持久化在
`settings` 表（key = `panel_proxy_config`），刷新 / 重启都不丢。

优先级：**面板配置 > 环境变量**。
面板里「恢复环境变量默认」会删掉 settings 里的记录，于是回到环境变量取值 ——
升级路径因此是平滑的：老部署升级后行为完全不变，直到有人在面板里改。

所有读取都带 5 秒内存快照：`maccms` 每次 HTTP 请求都要拿并发上限 / URL 列表，
不缓存的话等于给每个请求加一次数据库往返。写操作会主动失效快照。
"""
import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import delete, select

from . import config, database
from .models import settings as settings_tbl

log = logging.getLogger("vodsync.proxyconf")

PROXY_KEY = "panel_proxy_config"

# 读快照的有效期（秒）。代理配置改动极少，5 秒足够掩盖并发请求的重复查询，
# 同时让面板保存后最多 5 秒内生效（保存动作会主动失效，实际立即生效）。
CACHE_TTL = 5.0

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "value": None}

# 校验边界（与 config.py 的默认值同一量级）
MAX_URLS = 8
CONCURRENCY_RANGE = (1, 32)
TTL_RANGE = (0, 10080)        # 0 = 不记忆，每次都先试直连
TIMEOUT_RANGE = (1, 120)


def env_default() -> dict:
    """环境变量里的默认值（也是面板的初始值 / 回退值）。

    URL 在这里就归一化（去尾斜杠）：面板表单展示的是归一化后的形态，
    若默认值保留原始尾斜杠，「恢复默认」前后会出现两种写法。
    """
    return {
        "enabled": bool(config.PROXY_ENABLED),
        "urls": [u.strip().rstrip("/") for u in (config.PROXY_URLS or []) if u.strip()],
        "pref_ttl_minutes": int(config.PROXY_PREF_TTL_MINUTES),
        "concurrency": int(config.PROXY_CONCURRENCY),
        "timeout": int(config.PROXY_TIMEOUT),
    }


def _read_raw() -> dict:
    """从 settings 读原始 JSON；没有记录 / 解析失败都返回 {}。"""
    try:
        with database.engine.connect() as conn:
            raw = conn.execute(
                select(settings_tbl.c.value).where(settings_tbl.c.key == PROXY_KEY)
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - 表未建好时按"无面板配置"处理
        log.debug("读取代理配置失败（按环境变量处理）: %s", exc)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalise(data: dict) -> dict:
    """把任意输入收敛成合法配置；非法字段回落到环境变量默认值。"""
    d = env_default()

    if isinstance(data.get("enabled"), bool):
        d["enabled"] = data["enabled"]

    urls = data.get("urls")
    if isinstance(urls, str):                     # 面板表单可能直接给逗号串
        urls = urls.split(",")
    if isinstance(urls, list):
        clean = []
        for u in urls:
            u = str(u or "").strip().rstrip("/") or ""
            if not u:
                continue
            if not u.startswith(("http://", "https://")):
                continue                      # 明显不是 URL 的直接丢弃
            if u not in clean:
                clean.append(u)
        d["urls"] = clean[:MAX_URLS]

    for key, (lo, hi) in (("pref_ttl_minutes", TTL_RANGE),
                          ("concurrency", CONCURRENCY_RANGE),
                          ("timeout", TIMEOUT_RANGE)):
        v = data.get(key)
        if isinstance(v, bool):                # bool 是 int 子类，先排掉
            continue
        if isinstance(v, (int, float)):
            v = int(v)
            if lo <= v <= hi:
                d[key] = v
    return d


def get(refresh: bool = False) -> dict:
    """
    返回当前生效配置：`{enabled, urls, pref_ttl_minutes, concurrency, timeout}`。

    面板有配置就用面板的（逐字段合并：面板只存了部分字段也不会丢默认值）；
    没有则完全用环境变量。
    """
    with _CACHE_LOCK:
        hit = _CACHE["value"]
        if not refresh and hit is not None and (time.monotonic() - _CACHE["at"]) < CACHE_TTL:
            return dict(hit)
    merged = _normalise(_read_raw())
    with _CACHE_LOCK:
        _CACHE["at"] = time.monotonic()
        _CACHE["value"] = merged
    return dict(merged)


def source() -> str:
    """当前生效配置的来源：`panel` 或 `env`（供面板展示）。"""
    return "panel" if _read_raw() else "env"


def invalidate() -> None:
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0
        _CACHE["value"] = None


def set_config(data: dict) -> dict:
    """
    写入面板配置（整体替换），返回生效后的完整配置。

    这里存的是**完整配置**而不是差量：面板表单总是提交全部字段，
    存全量可以避免「面板只存一半、另一半悄悄用环境变量」的迷惑行为。
    """
    clean = _normalise(data or {})
    now = datetime.now()
    value = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    stmt = (
        database.dialect_insert(settings_tbl)
        .values(key=PROXY_KEY, value=value, updated_at=now)
        .on_conflict_do_update(
            index_elements=[settings_tbl.c.key],
            set_={"value": value, "updated_at": now},
        )
    )
    with database.write_txn() as conn:
        conn.execute(stmt)
    invalidate()
    log.info("[proxy] 面板已更新代理配置: enabled=%s urls=%s ttl=%s concurrency=%s timeout=%s",
             clean["enabled"], clean["urls"], clean["pref_ttl_minutes"],
             clean["concurrency"], clean["timeout"])
    return clean


def reset() -> dict:
    """删除面板配置，回到环境变量默认值。"""
    try:
        with database.write_txn() as conn:
            conn.execute(
                delete(settings_tbl).where(settings_tbl.c.key == PROXY_KEY)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[proxy] 清除面板代理配置失败: %s", exc)
    invalidate()
    log.info("[proxy] 已恢复环境变量默认代理配置")
    return env_default()


def describe() -> dict:
    """给面板用：生效值 + 来源 + 环境变量默认值。"""
    return {
        **get(refresh=True),
        "source": source(),
        "env_default": env_default(),
    }
