"""
同步暂停控制（三层级：全量 / 增量 / 全局）。

语义：检查点式优雅停止——
  - 暂停不会杀死正在执行的页面批次（避免脏数据），而是在下一批次开始前生效，
    全量同步会把断点保存到 sources.last_page；
  - 继续 = 清除标记 + 自动按断点重新触发同步（全量续传 / 增量重跑）；
  - 状态持久化到 data/pause_state.json，服务重启后暂停状态保留
    （用户主动暂停的意图不应因重启而丢失）。
"""
import json
import logging
import os
import threading
from datetime import datetime

from . import config

log = logging.getLogger("vodsync.pause")

_lock = threading.Lock()
_state = {
    "global": False,
    "full": False,
    "incremental": False,
    "updated_at": {},   # scope -> iso 时间
}
_state_file = None


def _file_path() -> str:
    global _state_file
    if _state_file is None:
        _state_file = os.path.join(config.DATA_DIR, "pause_state.json")
    return _state_file


def load_state():
    """启动时恢复持久化的暂停状态。"""
    path = _file_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            with _lock:
                for scope in ("global", "full", "incremental"):
                    _state[scope] = bool(saved.get(scope))
                _state["updated_at"] = saved.get("updated_at") or {}
            if any(_state[s] for s in ("global", "full", "incremental")):
                log.warning("已恢复暂停状态: %s", {k: v for k, v in _state.items() if k != "updated_at" and v})
    except Exception as e:
        log.warning("读取暂停状态文件失败（按未暂停处理）: %s", e)


def _persist():
    path = _file_path()
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("暂停状态写入文件失败: %s", e)


def set_paused(scope: str, paused: bool):
    """scope: global / full / incremental。"""
    if scope not in ("global", "full", "incremental"):
        raise ValueError(f"未知暂停范围: {scope}")
    with _lock:
        _state[scope] = bool(paused)
        _state["updated_at"][scope] = datetime.now().isoformat(timespec="seconds")
        _persist()
    log.info("同步暂停控制 [%s] = %s", scope, "暂停" if paused else "继续")


def is_paused(mode: str) -> bool:
    """
    某类同步当前是否应暂停。
    mode: full / incremental。全局暂停对两类同时生效。
    """
    with _lock:
        if _state["global"]:
            return True
        return bool(_state.get(mode))


def snapshot() -> dict:
    with _lock:
        return {
            "global": _state["global"],
            "full": _state["full"],
            "incremental": _state["incremental"],
            "updated_at": dict(_state["updated_at"]),
        }


def any_paused() -> bool:
    s = snapshot()
    return s["global"] or s["full"] or s["incremental"]
