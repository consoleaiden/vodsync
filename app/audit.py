"""
去广告运营管理审计 / 事件流（v1.18.0，§3.5 / §4.3）。

一张表 adfilter_admin_audit 同时服务两个用途：
  - 操作审计：publish / rollback / review / delete / manual 等人工动作；
  - 事件流：promoted / demoted / auto_disabled 等系统事件。
用 action 前缀区分，查询时用 action 参数过滤即可，无需两张表。

设计约束：
  - 审计写入是「尽力而为」：失败只记 warning，绝不让主操作（发布/审核）失败；
  - 不记录 url 原文（隐私口径），detail_json 只有状态/数量等聚合字段；
  - actor 来自 auth.request_identity() 的整数身份，由路由层传入。
"""
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select

from . import database
from .models import adfilter_admin_audit

log = logging.getLogger("vodsync.audit")

# 事件类型（与人工操作审计共表，用 action 区分）
#   - 系统自动：promoted / demoted / auto_disabled
#   - 人工语义：manual_created / locked（v1.19.0 §4.3 补齐，便于事件流统一展示）
EVENT_PROMOTED = "promoted"
EVENT_DEMOTED = "demoted"
EVENT_AUTO_DISABLED = "auto_disabled"
EVENT_MANUAL_CREATED = "manual_created"
EVENT_LOCKED = "locked"
EVENT_FEEDBACK_STILL_ADS = "feedback_still_ads"
# 事件全集：events() 缺省返回全部类型（§4.3）
EVENT_TYPES = (
    EVENT_PROMOTED, EVENT_DEMOTED, EVENT_AUTO_DISABLED,
    EVENT_MANUAL_CREATED, EVENT_LOCKED, EVENT_FEEDBACK_STILL_ADS,
)


def record(
    action: str,
    target: str = "",
    detail: Optional[dict] = None,
    actor: str = "",
) -> bool:
    """
    写一条审计（尽力而为，失败返回 False 不抛异常）。
    detail 会被 JSON 序列化，含 datetime 等非 JSON 类型时转字符串。
    """
    try:
        safe_detail = json.dumps(detail or {}, ensure_ascii=False, default=str)[:2000]
        with database.write_txn() as conn:
            conn.execute(adfilter_admin_audit.insert().values(
                action=(action or "")[:32],
                target=(target or "")[:128],
                detail_json=safe_detail,
                actor=(actor or "")[:32],
                at=datetime.now(),
            ))
        return True
    except Exception as e:  # noqa: BLE001 —— 审计不能拖垮主操作
        log.warning("[audit] 写审计失败 action=%s: %s", action, str(e)[:160])
        return False


def record_many(entries: list) -> int:
    """
    批量写审计（v1.19.0 §4.3）：单事务写入多条事件，供 rebuild 一次提交
    promoted/demoted 等事件，避免逐条写事务。尽力而为，返回成功条数。
    每条 entry: {"action", "target", "detail", "actor"}。
    """
    if not entries:
        return 0
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            safe_detail = json.dumps(e.get("detail") or {}, ensure_ascii=False, default=str)[:2000]
        except Exception:  # noqa: BLE001
            safe_detail = "{}"
        rows.append({
            "action": str(e.get("action") or "")[:32],
            "target": str(e.get("target") or "")[:128],
            "detail_json": safe_detail,
            "actor": str(e.get("actor") or "")[:32],
            "at": datetime.now(),
        })
    if not rows:
        return 0
    try:
        with database.write_txn() as conn:
            conn.execute(adfilter_admin_audit.insert(), rows)
        return len(rows)
    except Exception as e:  # noqa: BLE001
        log.warning("[audit] 批量写审计失败 n=%d: %s", len(rows), str(e)[:160])
        return 0


def list_audit(
    limit: int = 50,
    offset: int = 0,
    action: str = "",
    target: str = "",
) -> dict:
    """
    分页查询审计/事件。action 支持精确匹配或逗号分隔的多个值；
    传 "event:" 前缀之外的普通值精确匹配。返回 {total, items, limit, offset}。
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    t = adfilter_admin_audit
    where = []
    if action:
        actions = [a.strip() for a in action.split(",") if a.strip()]
        if len(actions) == 1:
            where.append(t.c.action == actions[0])
        elif actions:
            where.append(t.c.action.in_(actions))
    if target:
        where.append(t.c.target == target.strip()[:128])
    with database.engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(t).where(*where)
        ).scalar() or 0
        rows = conn.execute(
            select(t).where(*where).order_by(t.c.at.desc(), t.c.id.desc())
            .limit(limit).offset(offset)
        ).mappings().all()
    items = []
    for r in rows:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except Exception:  # noqa: BLE001
            detail = {}
        items.append({
            "id": int(r["id"]),
            "action": r["action"],
            "target": r["target"],
            "detail": detail,
            "actor": r["actor"],
            "at": r["at"].isoformat() if r["at"] else None,
        })
    return {"total": int(total), "items": items, "limit": limit, "offset": offset}


def events(limit: int = 50, kind: str = "", since: str = "") -> dict:
    """
    事件流（§4.3）：返回系统事件 + 人工语义事件（v1.19.0 补齐写入侧）。
    kind 为空返回全部事件类型；since 为 ISO 时间字符串时只返回更晚的事件
    （供面板「今日 N 条」聚合），在 Python 侧过滤以便 SQLite/PG 兼容。
    """
    event_actions = list(EVENT_TYPES)
    if kind:
        event_actions = [a for a in event_actions if a == kind] or [kind]
    result = list_audit(limit=max(int(limit), 1), action=",".join(event_actions))
    if since:
        try:
            since_s = str(since).replace("Z", "+00:00")
            from datetime import datetime as _dt
            cutoff = _dt.fromisoformat(since_s)
            items = []
            for it in result["items"]:
                at = it.get("at")
                if not at:
                    continue
                try:
                    if _dt.fromisoformat(at.replace("Z", "+00:00")) >= cutoff:
                        items.append(it)
                except ValueError:
                    continue
            result["items"] = items
            result["total"] = len(items)
        except (ValueError, TypeError):
            pass  # since 非法则忽略过滤，不 500
    return result
