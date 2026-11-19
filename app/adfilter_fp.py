"""
广告指纹库（adfilter fingerprint，v1.17.0 / Phase 2）。

它是什么
--------
把客户端「删了哪些片段」的**结构特征**跨用户、跨 URL 聚合，形成共享的
「哪些片段是广告」知识库。老架构下每个客户端各自跑本地启发式；指纹库让
新用户 / 新站点拉到 confirmed 指纹后可直接命中，不必等本地算法积累。

边界（与 adfilter.py 完全一致，别越界）
--------------------------------------
1. 不算分片内容哈希：服务端拿不到 ts 字节，只处理上报里的时长/大小/位置数字。
2. 不碰播放数据面：过滤仍在客户端，服务端只做「聚合 + 下发 + 收反馈」。
3. 不改任何既有接口语义：segments / fp_* 都是 adfilter.report 的可选追加字段，
   老客户端不带就完全走 v1.16.x 的老路径。

数据流（2a→2d 四阶段）
--------------------
2a  ingest_segments()    上报时把逐段特征落库（明细，只增不改）
2b  rebuild()            按 fp_hash 聚合，算置信度，样本够则 candidate→confirmed
2c  downloadable()       confirmed 指纹注入规则包下发（见 adfilter.publish）
2d  feedback()           客户端回报「命中/误杀」，run_hits++ / miss_count++，
                         误杀达阈值自动 disabled 并停止下发

指纹签名（fp_hash）
-------------------
fp_hash = sha256(f"{host}|{dur_bucket}|{size_bucket}")
    - host       ：站点级标识（小写、去端口），与统计口径一致
    - dur_bucket ：时长按 config.ADFILTER_FP_DUR_BUCKET_MS 取整
    - size_bucket：大小按 config.ADFILTER_FP_SIZE_BUCKET_B 取整（0=不参与指纹）
size_b = 0（客户端没提供）时 size_bucket 固定为 0，避免「提供了大小」和
「没提供大小」被拆成两个指纹。
"""
import hashlib
import ipaddress
import json
import logging
import re
import time as time_module
from datetime import datetime
from typing import Optional

from sqlalchemy import case, func, select, text

from . import audit as audit_mod
from . import config, database
from .models import adfilter_admin_audit, adfilter_fingerprints, adfilter_segments

log = logging.getLogger("vodsync.adfilter_fp")

# 状态机：candidate → confirmed（样本达标 / 人工审核通过）→ disabled（人工停用或误杀降权）
STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_DISABLED = "disabled"
STATUSES = (STATUS_CANDIDATE, STATUS_CONFIRMED, STATUS_DISABLED)

# 反馈类型
FEEDBACK_HIT = "hit"      # 客户端按指纹命中并成功过滤
FEEDBACK_MISS = "miss"    # 客户端认为该指纹误杀（不该删）

# v1.19.0 §4.6：miss 反馈原因口径（服务端先实现，待客户端协同传 reason）
#   over_deleted  ：确实删多了 → 计 miss_count，达阈值自动停用
#   still_has_ads ：还有别的广告（默认）→ 不计 miss，只做漏判统计
FEEDBACK_REASON_OVER_DELETED = "over_deleted"
FEEDBACK_REASON_STILL_ADS = "still_has_ads"
FEEDBACK_REASONS = (FEEDBACK_REASON_OVER_DELETED, FEEDBACK_REASON_STILL_ADS)

MAX_PATTERN_JSON = 512
MAX_NOTE = 255


class FPError(Exception):
    """指纹操作校验失败。field 指出字段，供路由层定位。"""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------------------
# 归一化：上报的片段特征 → 签名
# ---------------------------------------------------------------------------
def _as_int(value, field: str, minimum: int = 0) -> Optional[int]:
    """接受整数或整数值的浮点（JSON 客户端常见 1.0）；否则 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and abs(value - round(value)) > 1e-9:
        return None
    ivalue = int(round(value))
    return ivalue if ivalue >= minimum else None


def signature(host: str, dur_ms: int, size_b: int = 0) -> dict:
    """
    计算片段特征签名。返回 {host, dur_bucket, size_bucket, fp_hash, pattern}。

    纯函数、不查库，便于测试与客户端契约对齐（客户端可用同算法预判，
    但**不需要**——指纹由服务端下发，客户端只按 fp_hash 匹配）。
    """
    host = (host or "").strip().lower()[:253]
    dur_bucket = int(round(dur_ms / float(config.ADFILTER_FP_DUR_BUCKET_MS)))
    size_bucket = 0
    if config.ADFILTER_FP_SIZE_BUCKET_B > 0 and size_b > 0:
        size_bucket = int(round(size_b / float(config.ADFILTER_FP_SIZE_BUCKET_B)))
    raw = f"{host}|{dur_bucket}|{size_bucket}"
    fp_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    pattern = {
        "host": host,
        "dur_bucket": dur_bucket,
        "size_bucket": size_bucket,
        "dur_bucket_ms": config.ADFILTER_FP_DUR_BUCKET_MS,
        "size_bucket_b": config.ADFILTER_FP_SIZE_BUCKET_B,
    }
    return {
        "host": host,
        "dur_bucket": dur_bucket,
        "size_bucket": size_bucket,
        "fp_hash": fp_hash,
        "pattern": pattern,
    }


def validate_segments(segments) -> tuple[list, dict]:
    """
    校验单条上报里的 segments[]，返回 (合法片段列表, 丢弃原因统计)。

    每段必需：index(int ≥0) / dur_ms(int >0)。
    可选：size_b(int ≥0，默认 0)。
    单条上报最多 ADFILTER_FP_MAX_SEGMENTS 段，超出按「只取前 N 段」处理并记
    segments_too_many（不整批丢弃，避免一条脏 segment 毁掉整条上报的入库）。
    """
    reasons: dict = {}
    if not isinstance(segments, list):
        return [], {"segments_not_array": 1}
    limit = config.ADFILTER_FP_MAX_SEGMENTS
    if len(segments) > limit:
        reasons["segments_too_many"] = len(segments) - limit
        segments = segments[:limit]
    clean = []
    for raw in segments:
        if not isinstance(raw, dict):
            reasons["segment_not_object"] = reasons.get("segment_not_object", 0) + 1
            continue
        idx = _as_int(raw.get("index"), "index", minimum=0)
        dur = _as_int(raw.get("dur_ms"), "dur_ms", minimum=1)
        size = _as_int(raw.get("size_b", 0), "size_b", minimum=0)
        if idx is None or dur is None or size is None:
            reasons["segment_field_invalid"] = reasons.get("segment_field_invalid", 0) + 1
            continue
        clean.append({"index": idx, "dur_ms": dur, "size_b": size})
    return clean, reasons


# ---------------------------------------------------------------------------
# 2a：落库
# ---------------------------------------------------------------------------
def ingest_segments(
    segments: list,
    *,
    user_id: int,
    url_hash: str,
    host: str,
    at: int,
) -> int:
    """
    把已校验的片段列表落库，返回真正插入的条数（去重后）。

    调用方（adfilter.ingest）只在**本条上报成功插入 adfilter_reports** 后才调，
    否则重复上报会产生「有 segments 但没有对应 report」的孤儿明细。
    """
    if not segments:
        return 0
    rows = []
    for seg in segments:
        sig = signature(host, seg["dur_ms"], seg["size_b"])
        rows.append({
            "user_id": int(user_id),
            "url_hash": url_hash,
            "host": sig["host"],
            "at": int(at),
            "seg_index": seg["index"],
            "dur_ms": seg["dur_ms"],
            "size_b": seg["size_b"],
            "fp_hash": sig["fp_hash"],
        })
    stmt = database.dialect_insert(adfilter_segments).on_conflict_do_nothing(
        index_elements=[
            adfilter_segments.c.user_id,
            adfilter_segments.c.url_hash,
            adfilter_segments.c.at,
            adfilter_segments.c.seg_index,
        ]
    )
    inserted = 0
    with database.write_txn() as conn:
        for row in rows:
            rc = conn.execute(stmt.values(**row)).rowcount
            inserted += 1 if (rc is None or rc > 0) else 0
    return inserted


# ---------------------------------------------------------------------------
# 2b：聚类 + 置信度
# ---------------------------------------------------------------------------
def _confidence(distinct_users: int, distinct_urls: int, hit_count: int) -> float:
    """
    置信度：三个维度都达标才接近 1，任一侧不足就被木桶短板压住。

    不用简单平均——一个用户刷 1000 次不该被视为高置信。取三项归一化的最小值，
    再乘一个命中量的饱和项（log 曲线，避免超长尾把置信度顶满）。
    """
    import math

    u = min(1.0, distinct_users / float(config.ADFILTER_FP_MIN_USERS))
    r = min(1.0, distinct_urls / float(config.ADFILTER_FP_MIN_URLS))
    h = min(1.0, hit_count / float(config.ADFILTER_FP_MIN_HITS))
    saturation = 0.5 + 0.5 * min(1.0, math.log1p(hit_count) / math.log1p(20))
    return round(min(u, r, h) * saturation, 4)


def rebuild() -> dict:
    """
    从 adfilter_segments 全量重算指纹聚合（2b）。幂等，可反复调用。

    规则：
      - 每个 fp_hash 聚合 distinct_users / distinct_urls / hit_count / 时间窗；
      - 样本三项都达标 → candidate 自动晋升 confirmed；
      - **已 disabled 的指纹永不自动改回**（人工/误杀降权的决定必须人工撤销）；
      - **已 locked=1 的指纹（人工审核锁定）照常更新证据字段，但跳过 status 与
        reviewed_at** —— 机器不能推翻人的决定（v1.18.0 §2.1）；
      - 已 confirmed 的若样本回落到门槛下（例如清理了历史数据）→ 降回 candidate，
        避免下发一个证据已经消失的指纹；locked 行除外。

    v1.17.1 修复：pattern_json 必须携带 dur_bucket / size_bucket / dur_bucket_ms /
    size_bucket_b——downloadable() 从这里读桶字段下发给客户端，缺了会全部回落
    默认 0，客户端永远匹配不上（阻断性 bug）。同一 fp_hash 组内所有片段的桶序号
    必然相同（fp_hash 就是桶字段算出来的），所以聚合时取 min(dur_ms)/min(size_b)
    样本即可无损还原桶字段；更新分支同样回写 pattern_json，保证存量坏数据在
    重跑 rebuild 后自动修复。
    返回 {scanned, created, confirmed, demoted, disabled_kept, locked_kept}。
    """
    with database.engine.connect() as conn:
        grouped = conn.execute(
            select(
                adfilter_segments.c.fp_hash,
                adfilter_segments.c.host,
                func.count().label("hit_count"),
                func.count(func.distinct(adfilter_segments.c.user_id)).label("distinct_users"),
                func.count(func.distinct(adfilter_segments.c.url_hash)).label("distinct_urls"),
                func.min(adfilter_segments.c.at).label("first_seen"),
                func.max(adfilter_segments.c.at).label("last_seen"),
                # 桶字段还原样本：组内同 fp_hash ⇒ 同桶，任取一条即可（用 min 保持确定性）
                func.min(adfilter_segments.c.dur_ms).label("sample_dur_ms"),
                func.min(adfilter_segments.c.size_b).label("sample_size_b"),
            )
            .group_by(adfilter_segments.c.fp_hash, adfilter_segments.c.host)
        ).mappings().all()

    created = confirmed = demoted = disabled_kept = locked_kept = 0
    now = datetime.now()
    # v1.19.0 §4.3：逐条收集状态跃迁事件，末尾单事务写审计（供事件流/今日提示）
    events: list = []
    actor = "system:rebuild"

    def _pattern(m: dict, hits: int, users: int, urls: int) -> dict:
        """从聚合样本还原完整 pattern（含下发必需的桶字段）。"""
        sig = signature(
            m["host"],
            int(m["sample_dur_ms"] or 0),
            int(m["sample_size_b"] or 0),
        )
        return {
            "host": m["host"],
            "dur_bucket": sig["dur_bucket"],
            "size_bucket": sig["size_bucket"],
            "dur_bucket_ms": config.ADFILTER_FP_DUR_BUCKET_MS,
            "size_bucket_b": config.ADFILTER_FP_SIZE_BUCKET_B,
            "hit_count": hits,
            "distinct_users": users,
            "distinct_urls": urls,
        }

    with database.write_txn() as conn:
        seen: set = set()
        for row in grouped:
            m = row
            fp_hash = m["fp_hash"]
            seen.add(fp_hash)
            hits = int(m["hit_count"] or 0)
            users = int(m["distinct_users"] or 0)
            urls = int(m["distinct_urls"] or 0)
            confidence = _confidence(users, urls, hits)
            qualified = (
                users >= config.ADFILTER_FP_MIN_USERS
                and urls >= config.ADFILTER_FP_MIN_URLS
                and hits >= config.ADFILTER_FP_MIN_HITS
            )

            existing = conn.execute(
                select(
                    adfilter_fingerprints.c.id,
                    adfilter_fingerprints.c.status,
                    adfilter_fingerprints.c.pattern_json,
                    adfilter_fingerprints.c.reviewed_at,
                    adfilter_fingerprints.c.locked,
                ).where(adfilter_fingerprints.c.fp_hash == fp_hash)
            ).mappings().first()

            first_seen = int(m["first_seen"] or 0)
            last_seen = int(m["last_seen"] or 0)
            # 每次 rebuild 都重算完整 pattern（含桶字段），存量缺桶字段的数据在此自动修复
            pattern_json = json.dumps(
                _pattern(m, hits, users, urls), ensure_ascii=False
            )[:MAX_PATTERN_JSON]
            if existing is None:
                status = STATUS_CONFIRMED if qualified else STATUS_CANDIDATE
                conn.execute(adfilter_fingerprints.insert().values(
                    fp_hash=fp_hash, host=m["host"],
                    pattern_json=pattern_json,
                    status=status, confidence=confidence,
                    hit_count=hits, distinct_users=users, distinct_urls=urls,
                    first_seen_at=first_seen, last_seen_at=last_seen,
                    reviewed_at=now if status == STATUS_CONFIRMED else None,
                    source="auto", locked=0,
                    created_at=now, updated_at=now,
                ))
                created += 1
                if status == STATUS_CONFIRMED:
                    confirmed += 1
                    events.append({"action": audit_mod.EVENT_PROMOTED, "target": fp_hash,
                                   "detail": {"reason": "auto_qualified", "source": "auto",
                                              "users": users, "urls": urls, "hits": hits},
                                   "actor": actor})
            else:
                old_status = existing["status"]
                is_locked = bool(existing["locked"])
                if old_status == STATUS_DISABLED or is_locked:
                    # disabled 与 locked 都只更新证据字段，不动 status/reviewed_at：
                    #   - disabled：误杀降权是人工/自动的止血决定，永不自动恢复；
                    #   - locked：人工审核锁定，机器不推翻人的决定（§2.1）。
                    # 注意即使 locked 的当前状态是 confirmed，也照常更新 confidence，
                    # 让「人工放行但样本在涨」的指纹置信度保持真实。
                    conn.execute(
                        adfilter_fingerprints.update()
                        .where(adfilter_fingerprints.c.id == existing["id"])
                        .values(
                            confidence=confidence,
                            hit_count=hits, distinct_users=users, distinct_urls=urls,
                            pattern_json=pattern_json,
                            last_seen_at=last_seen, updated_at=now,
                        )
                    )
                    if is_locked:
                        locked_kept += 1
                    else:
                        disabled_kept += 1
                    continue
                new_status = STATUS_CONFIRMED if qualified else STATUS_CANDIDATE
                conn.execute(
                    adfilter_fingerprints.update()
                    .where(adfilter_fingerprints.c.id == existing["id"])
                    .values(
                        confidence=confidence, hit_count=hits,
                        distinct_users=users, distinct_urls=urls,
                        pattern_json=pattern_json,
                        last_seen_at=last_seen,
                        status=new_status,
                        reviewed_at=now if new_status == STATUS_CONFIRMED else existing["reviewed_at"],
                        updated_at=now,
                    )
                )
                if old_status != STATUS_CONFIRMED and new_status == STATUS_CONFIRMED:
                    confirmed += 1
                    events.append({"action": audit_mod.EVENT_PROMOTED, "target": fp_hash,
                                   "detail": {"reason": "auto_qualified", "from": old_status,
                                              "users": users, "urls": urls, "hits": hits},
                                   "actor": actor})
                if old_status == STATUS_CONFIRMED and new_status == STATUS_CANDIDATE:
                    demoted += 1
                    events.append({"action": audit_mod.EVENT_DEMOTED, "target": fp_hash,
                                   "detail": {"reason": "sample_fell_below_threshold",
                                              "users": users, "urls": urls, "hits": hits},
                                   "actor": actor})

    # 明细被清空的指纹：保留实体（历史发布还可能引用），但状态降为 candidate。
    # locked / disabled 行豁免——人工锁定的 confirmed 不因明细清空而被机器降级。
    if seen:
        with database.write_txn() as conn:
            stale = conn.execute(
                select(adfilter_fingerprints.c.id).where(
                    adfilter_fingerprints.c.fp_hash.notin_(list(seen)),
                    adfilter_fingerprints.c.status == STATUS_CONFIRMED,
                    adfilter_fingerprints.c.locked == 0,
                )
            ).scalars().all()
            for fid in stale:
                conn.execute(
                    adfilter_fingerprints.update()
                    .where(adfilter_fingerprints.c.id == fid)
                    .values(status=STATUS_CANDIDATE, updated_at=now)
                )
                events.append({"action": audit_mod.EVENT_DEMOTED,
                               "target": "", "detail": {"reason": "segments_cleared", "id": fid},
                               "actor": actor})
            demoted += len(stale)

    # 事件落库（尽力而为，不阻断 rebuild 主流程）
    if events:
        audit_mod.record_many(events)

    result = {
        "scanned": len(grouped),
        "created": created,
        "confirmed": confirmed,
        "demoted": demoted,
        "disabled_kept": disabled_kept,
        "locked_kept": locked_kept,
    }
    log.info("[adfilter_fp] rebuild 完成: %s", result)
    return result


# ---------------------------------------------------------------------------
# 2c：下发
# ---------------------------------------------------------------------------
def downloadable(limit: Optional[int] = None) -> list:
    """
    返回可下发到规则包的指纹（confirmed 且未被误杀 disabled），按置信度降序。

    每条只带客户端所需的最小字段：fp_hash / host / dur_bucket / size_bucket，
    不含任何 url 原文或用户身份。
    """
    if not config.ADFILTER_FP_ENABLED:
        return []
    cap = config.ADFILTER_FP_MAX_DOWNLOAD if limit is None else max(0, min(int(limit), 256))
    if cap <= 0:
        return []
    try:
        with database.engine.connect() as conn:
            rows = conn.execute(
                select(
                    adfilter_fingerprints.c.fp_hash,
                    adfilter_fingerprints.c.host,
                    adfilter_fingerprints.c.pattern_json,
                    adfilter_fingerprints.c.confidence,
                )
                .where(
                    adfilter_fingerprints.c.status == STATUS_CONFIRMED,
                    adfilter_fingerprints.c.miss_count < config.ADFILTER_FP_MISS_DISABLE,
                )
                .order_by(
                    adfilter_fingerprints.c.confidence.desc(),
                    adfilter_fingerprints.c.hit_count.desc(),
                )
                .limit(cap)
            ).mappings().all()
    except Exception as e:  # noqa: BLE001 —— 控制面读失败不能拖垮规则下发
        log.warning("[adfilter_fp] 读取可下发指纹失败: %s", str(e)[:160])
        return []
    out = []
    for r in rows:
        try:
            pat = json.loads(r["pattern_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        out.append({
            "fp_hash": r["fp_hash"],
            "host": r["host"],
            "dur_bucket": int(pat.get("dur_bucket", 0)),
            "size_bucket": int(pat.get("size_bucket", 0)),
            "dur_bucket_ms": int(pat.get("dur_bucket_ms", config.ADFILTER_FP_DUR_BUCKET_MS)),
            "size_bucket_b": int(pat.get("size_bucket_b", config.ADFILTER_FP_SIZE_BUCKET_B)),
            "confidence": round(float(r["confidence"] or 0), 4),
        })
    return out


def _downloadable_hashes() -> set:
    """实际可下发指纹的 hash 集合（按 config cap 截断），用于列表 capped 判定。"""
    try:
        return {f["fp_hash"] for f in downloadable()}  # 不传 limit → 用 config cap
    except Exception:  # noqa: BLE001
        return set()


# ---------------------------------------------------------------------------
# 2d：反馈闭环
# ---------------------------------------------------------------------------
def feedback(fp_hash: str, kind: str, reason: str = "") -> dict:
    """
    客户端回报某指纹命中 / 误杀。返回该指纹的最新状态。

    - hit ：run_hits++（正向证据，不影响 status）
    - miss：按 reason 决定是否计误杀（v1.19.0 §4.6）：
        * over_deleted  ：确实删多了 → miss_count++，达阈值自动 disabled；
        * still_has_ads ：只是还有别的广告 → 不计 miss，仅写
                          feedback_still_ads 事件喂给漏判统计（§3.3）；
        * 不传 reason   ：按 ADFILTER_FP_MISS_REASON_REQUIRED 决定——
                          false（默认过渡期）当 over_deleted 计 miss，保证老客户端
                          止血能力不中断；true（客户端升级后）当 still_has_ads 不计。
    未知 fp_hash 直接返回 {found:false}，不报错——客户端缓存了旧指纹是正常的。
    """
    if kind not in (FEEDBACK_HIT, FEEDBACK_MISS):
        raise FPError(f"feedback kind 必须是 {FEEDBACK_HIT}/{FEEDBACK_MISS}", "kind")
    reason = (reason or "").strip().lower()
    if reason and reason not in FEEDBACK_REASONS:
        raise FPError(
            f"reason 必须是 {'/'.join(FEEDBACK_REASONS)} 或留空", "reason"
        )
    fp_hash = (fp_hash or "").strip().lower()
    if not fp_hash:
        raise FPError("fp_hash 不能为空", "fp_hash")

    event_to_record = None  # 事务外再写审计（write_txn 持锁不可重入）
    with database.write_txn() as conn:
        row = conn.execute(
            select(
                adfilter_fingerprints.c.id,
                adfilter_fingerprints.c.miss_count,
                adfilter_fingerprints.c.status,
                adfilter_fingerprints.c.host,
            ).where(adfilter_fingerprints.c.fp_hash == fp_hash)
        ).mappings().first()
        if row is None:
            return {"found": False, "fp_hash": fp_hash}

        now = datetime.now()
        # 判定本次 miss 是否计入误杀计数
        effective_reason = reason or (
            FEEDBACK_REASON_STILL_ADS if config.ADFILTER_FP_MISS_REASON_REQUIRED
            else FEEDBACK_REASON_OVER_DELETED
        )
        count_as_miss = effective_reason == FEEDBACK_REASON_OVER_DELETED
        if kind == FEEDBACK_HIT:
            conn.execute(
                adfilter_fingerprints.update()
                .where(adfilter_fingerprints.c.id == row["id"])
                .values(
                    run_hits=adfilter_fingerprints.c.run_hits + 1,
                    last_hit_at=now, updated_at=now,
                )
            )
            new_miss = int(row["miss_count"] or 0)
            status = row["status"]
        elif count_as_miss:
            new_miss = int(row["miss_count"] or 0) + 1
            disable = new_miss >= config.ADFILTER_FP_MISS_DISABLE
            new_status = STATUS_DISABLED if disable else row["status"]
            conn.execute(
                adfilter_fingerprints.update()
                .where(adfilter_fingerprints.c.id == row["id"])
                .values(
                    miss_count=new_miss,
                    status=new_status,
                    reviewed_at=now if disable else None,
                    note="误杀举报达阈值，自动停用" if disable else None,
                    last_miss_at=now,
                    updated_at=now,
                )
            )
            status = new_status
            if disable:
                log.info("[adfilter_fp] 指纹 %s… 误杀 %d 次，自动停用", fp_hash[:12], new_miss)
                event_to_record = (
                    audit_mod.EVENT_AUTO_DISABLED,
                    {"miss_count": new_miss, "reason": effective_reason,
                     "threshold": config.ADFILTER_FP_MISS_DISABLE, "host": row["host"]},
                )
        else:
            # still_has_ads：不计 miss，只更新 last_miss_at + 写漏判事件（§3.3 喂数据）
            new_miss = int(row["miss_count"] or 0)
            status = row["status"]
            conn.execute(
                adfilter_fingerprints.update()
                .where(adfilter_fingerprints.c.id == row["id"])
                .values(last_miss_at=now, updated_at=now)
            )
            event_to_record = (
                audit_mod.EVENT_FEEDBACK_STILL_ADS,
                {"reason": effective_reason, "host": row["host"],
                 "status": status, "miss_count": new_miss},
            )
    # 事务外写事件，避免 write_txn 嵌套锁
    if event_to_record:
        audit_mod.record(event_to_record[0], fp_hash, event_to_record[1],
                         actor="system:feedback")
    return {
        "found": True,
        "fp_hash": fp_hash,
        "status": status,
        "miss_count": new_miss,
        "counted_as_miss": count_as_miss if kind == FEEDBACK_MISS else False,
        "reason": effective_reason if kind == FEEDBACK_MISS else "",
        "downloadable": status == STATUS_CONFIRMED and new_miss < config.ADFILTER_FP_MISS_DISABLE,
    }


# ---------------------------------------------------------------------------
# 管理端查询 / 审核
# ---------------------------------------------------------------------------
# 列表排序白名单：order 参数必须是这里的 key，避免任意列注入
_LIST_ORDERS = {
    "confidence_desc": (adfilter_fingerprints.c.confidence.desc(), adfilter_fingerprints.c.hit_count.desc()),
    "hit_desc": (adfilter_fingerprints.c.hit_count.desc(), adfilter_fingerprints.c.confidence.desc()),
    "miss_desc": (adfilter_fingerprints.c.miss_count.desc(), adfilter_fingerprints.c.hit_count.desc()),
    "updated_desc": (adfilter_fingerprints.c.updated_at.desc(), adfilter_fingerprints.c.fp_hash.asc()),
}


def list_fingerprints(
    status: str = "",
    host: str = "",
    limit: int = 50,
    offset: int = 0,
    source: str = "",
    downloadable: Optional[bool] = None,
    miss_gt: Optional[int] = None,
    near_miss: bool = False,
    order: str = "",
) -> dict:
    """
    指纹分页列表（管理端，v1.18.0 §3.1）。status/host/source/downloadable/
    miss_gt/near_miss 可筛选，order 排序，末尾始终按 fp_hash 兜底保证分页稳定。

    near_miss=1：仅「恰好差一项达标」的 candidate（批量审核目标）。
    downloadable 筛选在 SQL 层表达，保证 total 与分页一致。
    """
    fp = adfilter_fingerprints
    where = []
    if status in STATUSES:
        where.append(fp.c.status == status)
    if host:
        where.append(fp.c.host == host.strip().lower()[:253])
    if source in ("auto", "manual"):
        where.append(fp.c.source == source)
    if miss_gt is not None:
        where.append(fp.c.miss_count > max(0, int(miss_gt)))
    if downloadable is True:
        where.append(fp.c.status == STATUS_CONFIRMED)
        where.append(fp.c.miss_count < config.ADFILTER_FP_MISS_DISABLE)
    elif downloadable is False:
        where.append(
            (fp.c.status != STATUS_CONFIRMED)
            | (fp.c.miss_count >= config.ADFILTER_FP_MISS_DISABLE)
        )
    if near_miss:
        # 只差一项：candidate 且三项门槛中恰好两项达标
        mu, mr, mh = (fp.c.distinct_users >= config.ADFILTER_FP_MIN_USERS,
                      fp.c.distinct_urls >= config.ADFILTER_FP_MIN_URLS,
                      fp.c.hit_count >= config.ADFILTER_FP_MIN_HITS)
        where.append(fp.c.status == STATUS_CANDIDATE)
        # SQLAlchemy 布尔表达式不能相加，用 case 转成 0/1 再求和
        where.append(
            (case((mu, 1), else_=0) + case((mr, 1), else_=0) + case((mh, 1), else_=0)) == 2
        )

    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    order_cols = _LIST_ORDERS.get(order or "", _LIST_ORDERS["confidence_desc"])
    order_by = tuple(order_cols) + (fp.c.fp_hash.asc(),)

    with database.engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(fp).where(*where)
        ).scalar() or 0
        rows = conn.execute(
            select(fp).where(*where).order_by(*order_by).limit(limit).offset(offset)
        ).mappings().all()

    # v1.19.0 §4.1：单次取全量可下发 hash，用于把「confirmed 但被上限截断」的行
    # 标成 block_reason=capped（否则面板看不出是 128 条截断还是只有 128 条）。
    eligible = set()
    if config.ADFILTER_FP_ENABLED:
        eligible = _downloadable_hashes()

    items = []
    for r in rows:
        item = _row_to_dict(r)
        # 只有「本应下发（confirmed 且未达误杀阈值）却被 cap 截断」才标 capped
        if (item["status"] == STATUS_CONFIRMED
                and item["block_reason"] == ""
                and r["fp_hash"] not in eligible):
            item["block_reason"] = "capped"
            item["downloadable"] = False
        item["blocked_by"] = blocked_by_flags(
            r["status"], int(r["distinct_users"] or 0),
            int(r["distinct_urls"] or 0), int(r["hit_count"] or 0),
        )
        items.append(item)
    return {
        "total": int(total),
        "items": items,
        "limit": limit,
        "offset": offset,
    }


def get_fingerprint(fp_hash: str) -> Optional[dict]:
    """单条指纹详情（管理端）。"""
    with database.engine.connect() as conn:
        row = conn.execute(
            select(adfilter_fingerprints)
            .where(adfilter_fingerprints.c.fp_hash == (fp_hash or "").strip().lower())
        ).mappings().first()
    return _row_to_dict(row) if row else None


def _block_reason(status: str, miss_count: int, fp_hash: str = "") -> str:
    """
    解释「为什么没下发」："" | miss_threshold | not_confirmed | capped。
    capped 需要结合下发排名，由调用方在列表场景额外判定（此处只判前两种）。

    误杀达阈值优先于状态判断：disabled 若因误杀触发（miss_count 达阈值），
    报 miss_threshold 才能向运营解释「止血的原因」，而不是笼统的 not_confirmed。
    """
    if miss_count >= config.ADFILTER_FP_MISS_DISABLE:
        return "miss_threshold"
    if status != STATUS_CONFIRMED:
        return "not_confirmed"
    return ""


def _row_to_dict(m) -> dict:
    status = m["status"]
    miss_count = int(m["miss_count"] or 0)
    run_hits = int(m["run_hits"] or 0)
    reason = _block_reason(status, miss_count)
    return {
        "id": int(m["id"]),
        "fp_hash": m["fp_hash"],
        "host": m["host"],
        "pattern": _safe_json(m["pattern_json"]),
        "status": status,
        "confidence": round(float(m["confidence"] or 0), 4),
        "hit_count": int(m["hit_count"] or 0),
        "distinct_users": int(m["distinct_users"] or 0),
        "distinct_urls": int(m["distinct_urls"] or 0),
        "run_hits": run_hits,
        "miss_count": miss_count,
        "downloadable": reason == "",
        "block_reason": reason,
        # 命中率：run_hits / (run_hits + miss_count)，无反馈时为 0
        "hit_rate": round(run_hits / (run_hits + miss_count), 4) if (run_hits + miss_count) else 0,
        # v1.18.0 运营字段
        "locked": bool(m["locked"]) if "locked" in m.keys() else False,
        "source": m["source"] if "source" in m.keys() else "auto",
        "last_reset_at": (m["last_reset_at"].isoformat()
                          if "last_reset_at" in m.keys() and m["last_reset_at"] else None),
        "last_hit_at": (m["last_hit_at"].isoformat()
                        if "last_hit_at" in m.keys() and m["last_hit_at"] else None),
        "last_miss_at": (m["last_miss_at"].isoformat()
                         if "last_miss_at" in m.keys() and m["last_miss_at"] else None),
        "first_seen_at": int(m["first_seen_at"] or 0) or None,
        "last_seen_at": int(m["last_seen_at"] or 0) or None,
        "reviewed_at": m["reviewed_at"].isoformat() if m["reviewed_at"] else None,
        "note": m["note"] or "",
        "created_at": m["created_at"].isoformat() if m["created_at"] else None,
    }


def _safe_json(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# v1.19.0 §2.3：host 可解析校验（域名/IP/localhost），防止人工补录填乱码把库污染。
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def valid_host(host: str) -> bool:
    """域名 / IPv4 / IPv6 / localhost 视为合法；大小写不敏感。空或带路径/端口判非法。"""
    h = (host or "").strip().lower()
    if not h or len(h) > 253 or any(c in h for c in " /\\:?#@"):
        return False
    if h == "localhost":
        return True
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    labels = h.rstrip(".").split(".")
    if len(labels) < 2:
        return False
    return all(_HOST_LABEL.match(label) for label in labels)


def _apply_review(conn, fp_hash: str, status: str, note: str, lock: bool, now) -> bool:
    """
    单条审核的实际写库（在调用方的事务里执行）。返回是否命中行。

    lock=True（默认）：认为这是人工决定，写 locked=1 + status + reviewed_at。
    lock=False：「交还自动」，写 locked=0、状态保持当前值由下次 rebuild 接管；
               reviewed_at/note 仍写，保留操作留痕。
    status=confirmed 时复位 miss_count + 写 last_reset_at（§2.2 止血后可解除）。
    """
    row = conn.execute(
        select(adfilter_fingerprints.c.id).where(adfilter_fingerprints.c.fp_hash == fp_hash)
    ).first()
    if row is None:
        return False
    values = {
        "note": (note or "")[:MAX_NOTE],
        "reviewed_at": now,
        "updated_at": now,
    }
    if lock:
        values["status"] = status
        values["locked"] = 1
    else:
        values["locked"] = 0
        # 交还自动：不动 status，让 rebuild 依据样本重新判定
    if status == STATUS_CONFIRMED:
        # 人工确认 → 误杀计数归零，避免「disabled 后确认但仍不下发」的死结
        values["miss_count"] = 0
        values["last_reset_at"] = now
    conn.execute(
        adfilter_fingerprints.update()
        .where(adfilter_fingerprints.c.fp_hash == fp_hash)
        .values(**values)
    )
    return True


def review(fp_hash: str, status: str, note: str = "", lock: bool = True) -> dict:
    """
    人工审核指纹（管理端）：把状态设为 confirmed / candidate / disabled 之一。

    人工 confirmed 不受样本门槛限制（运营判断优先），但要写 reviewed_at。
    disabled 是「止血」，永不自动恢复。
    lock 缺省 True（向后兼容：老调用方语义等同「审核即锁定」，rebuild 不再覆盖）；
    lock=False 表示交还自动判定。status=confirmed 时复位误杀计数（§2.2）。
    """
    if status not in STATUSES:
        raise FPError(f"status 必须是 {'/'.join(STATUSES)}", "status")
    fp_hash = (fp_hash or "").strip().lower()
    now = datetime.now()
    with database.write_txn() as conn:
        if not _apply_review(conn, fp_hash, status, note, bool(lock), now):
            raise LookupError(f"不存在指纹 {fp_hash}")
    return get_fingerprint(fp_hash)


def batch_review(fp_hashes: list, status: str, note: str = "", lock: bool = True) -> dict:
    """
    批量人工审核（§3.2）。单事务处理，返回 {updated, not_found, items}。
    不存在的 hash 收集到 not_found 而不中断整批；status 非法抛 FPError。
    """
    if status not in STATUSES:
        raise FPError(f"status 必须是 {'/'.join(STATUSES)}", "status")
    hashes = []
    for h in (fp_hashes or []):
        hv = (h or "").strip().lower()
        if hv and hv not in hashes:
            hashes.append(hv)
    if not hashes:
        raise FPError("fp_hashes 不能为空", "fp_hashes")
    now = datetime.now()
    not_found = []
    with database.write_txn() as conn:
        for fp_hash in hashes:
            if not _apply_review(conn, fp_hash, status, note, bool(lock), now):
                not_found.append(fp_hash)
    return {
        "updated": len(hashes) - len(not_found),
        "not_found": not_found,
        "items": [get_fingerprint(h) for h in hashes if h not in not_found],
    }


def manual_create(host: str, dur_ms: int, size_b: int = 0, note: str = "") -> dict:
    """
    人工补录指纹（§2.3）：运营确认某站广告切片特征后直接录入 confirmed。

    不接受调用方传入 fp_hash —— 必须由 signature() 按服务端口径计算，否则无法校验真伪。
    幂等：fp_hash 已存在时只补确认（status=confirmed / locked=1 / source=manual /
    confidence=1.0 / note），不返回 409，运营重复提交也安全；created=False 表示合并。

    校验：host 非空、dur_ms ∈ [1, 3600000]、size_b ∈ [0, 2^31)，越界抛 FPError。
    """
    host = (host or "").strip().lower()
    if not host:
        raise FPError("host 不能为空", "host")
    if not valid_host(host):
        raise FPError("host 必须是合法域名 / IP / localhost", "host")
    try:
        dur = int(dur_ms)
        size = int(size_b or 0)
    except (TypeError, ValueError):
        raise FPError("dur_ms / size_b 必须是整数", "dur_ms")
    if not (1 <= dur <= 3_600_000):
        raise FPError("dur_ms 必须在 1 ~ 3600000 之间", "dur_ms")
    if not (0 <= size < 2 ** 31):
        raise FPError("size_b 必须在 0 ~ 2147483647 之间", "size_b")

    sig = signature(host, dur, size)
    fp_hash = sig["fp_hash"]
    pattern_json = json.dumps(sig["pattern"], ensure_ascii=False)[:MAX_PATTERN_JSON]
    now = datetime.now()
    with database.write_txn() as conn:
        existing = conn.execute(
            select(adfilter_fingerprints.c.id).where(adfilter_fingerprints.c.fp_hash == fp_hash)
        ).first()
        if existing is None:
            conn.execute(adfilter_fingerprints.insert().values(
                fp_hash=fp_hash, host=host, pattern_json=pattern_json,
                status=STATUS_CONFIRMED, confidence=1.0,
                hit_count=0, distinct_users=0, distinct_urls=0,
                first_seen_at=0, last_seen_at=0,
                source="manual", locked=1,
                reviewed_at=now, note=(note or "")[:MAX_NOTE],
                created_at=now, updated_at=now,
            ))
            created = True
        else:
            # 幂等合并：只补确认与来源，保留已有样本计数/命中时间。
            conn.execute(
                adfilter_fingerprints.update()
                .where(adfilter_fingerprints.c.fp_hash == fp_hash)
                .values(
                    pattern_json=pattern_json, status=STATUS_CONFIRMED,
                    confidence=1.0, source="manual", locked=1,
                    miss_count=0, last_reset_at=now,
                    reviewed_at=now, note=(note or "")[:MAX_NOTE],
                    updated_at=now,
                )
            )
            created = False
    row = get_fingerprint(fp_hash)
    # v1.19.0 §4.3：人工补录写事件流（新建/合并都算人工创建并锁定）
    audit_mod.record(audit_mod.EVENT_MANUAL_CREATED, fp_hash, {
        "host": host, "created": created, "dur_ms": dur, "size_b": size,
        "note": (note or "")[:120], "locked": True,
    }, actor="admin:manual")
    return {"created": created, "fingerprint": row}


def blocked_by_flags(status: str, users: int, urls: int, hits: int) -> dict:
    """一条 candidate 卡在哪些门槛上（供列表/near_miss 用）。"""
    return {
        "users": bool(status == STATUS_CANDIDATE and users < config.ADFILTER_FP_MIN_USERS),
        "urls": bool(status == STATUS_CANDIDATE and urls < config.ADFILTER_FP_MIN_URLS),
        "hits": bool(status == STATUS_CANDIDATE and hits < config.ADFILTER_FP_MIN_HITS),
    }


def delete_fingerprint(fp_hash: str) -> dict:
    """
    彻底删除一条指纹（管理端，v1.17.5）。

    与 review(disabled) 的区别：disabled 只是「止血、停止下发」，记录仍在库中、
    面板仍可见、还占着统计口径；delete 是物理删除，记录从面板消失。

    **必须同时删两张表**：
      - adfilter_fingerprints：聚合实体（面板列表读它）；
      - adfilter_segments：上报明细（rebuild 的数据源）。
    只删聚合表的话，下次 rebuild() 会从 segments 明细把这条指纹重新生成出来，
    用户看到「删了又回来」。两表放在同一个 write_txn 事务里，保证原子性。

    不存在的 fp_hash 抛 LookupError（路由层映射 404）。
    返回 {"fp_hash": ..., "deleted_fingerprints": n, "deleted_segments": m}。
    注意：规则包是快照，删除后需另行 publish 才会从下发包里移除（面板有提示）。
    """
    fp_hash = (fp_hash or "").strip().lower()
    if not fp_hash:
        raise FPError("fp_hash 不能为空", "fp_hash")
    with database.write_txn() as conn:
        row = conn.execute(
            select(adfilter_fingerprints.c.id)
            .where(adfilter_fingerprints.c.fp_hash == fp_hash)
        ).first()
        if row is None:
            raise LookupError(f"不存在指纹 {fp_hash}")
        deleted_segments = conn.execute(
            adfilter_segments.delete().where(adfilter_segments.c.fp_hash == fp_hash)
        ).rowcount
        deleted_fingerprints = conn.execute(
            adfilter_fingerprints.delete().where(adfilter_fingerprints.c.fp_hash == fp_hash)
        ).rowcount
    log.info("[adfilter_fp] 删除指纹 %s…: 聚合 %d 行 / 明细 %d 行",
             fp_hash[:12], deleted_fingerprints, deleted_segments)
    return {
        "fp_hash": fp_hash,
        "deleted_fingerprints": int(deleted_fingerprints or 0),
        "deleted_segments": int(deleted_segments or 0),
    }


def batch_delete_preview(fp_hashes: list) -> dict:
    """
    批量删除预览（§4.4）：不写库，返回每条指纹将删除的明细行数与合计，
    供面板二次确认框如实展示「将删除 N 条指纹 / M 行明细」。
    """
    hashes = []
    for h in (fp_hashes or []):
        hv = (h or "").strip().lower()
        if hv and hv not in hashes:
            hashes.append(hv)
    if not hashes:
        raise FPError("fp_hashes 不能为空", "fp_hashes")
    items = []
    with database.engine.connect() as conn:
        for hv in hashes:
            fp_row = conn.execute(
                select(adfilter_fingerprints.c.id, adfilter_fingerprints.c.host)
                .where(adfilter_fingerprints.c.fp_hash == hv)
            ).mappings().first()
            seg_n = conn.execute(
                select(func.count()).select_from(adfilter_segments)
                .where(adfilter_segments.c.fp_hash == hv)
            ).scalar() or 0
            items.append({
                "fp_hash": hv,
                "exists": fp_row is not None,
                "host": fp_row["host"] if fp_row else None,
                "segments": int(seg_n),
            })
    return {
        "items": items,
        "found": sum(1 for i in items if i["exists"]),
        "not_found": [i["fp_hash"] for i in items if not i["exists"]],
        "total_fingerprints": sum(1 for i in items if i["exists"]),
        "total_segments": sum(i["segments"] for i in items if i["exists"]),
    }


def batch_delete(fp_hashes: list) -> dict:
    """
    批量物理删除（§4.4）：单事务删多条指纹 + 对应明细，返回汇总。
    不存在的 hash 收集到 not_found 不中断整批；尽力写审计事件。
    """
    hashes = []
    for h in (fp_hashes or []):
        hv = (h or "").strip().lower()
        if hv and hv not in hashes:
            hashes.append(hv)
    if not hashes:
        raise FPError("fp_hashes 不能为空", "fp_hashes")
    deleted_fp = deleted_seg = 0
    not_found = []
    deleted_hosts = []
    with database.write_txn() as conn:
        for hv in hashes:
            row = conn.execute(
                select(adfilter_fingerprints.c.id, adfilter_fingerprints.c.host)
                .where(adfilter_fingerprints.c.fp_hash == hv)
            ).mappings().first()
            if row is None:
                not_found.append(hv)
                continue
            deleted_hosts.append(row["host"])
            deleted_seg += int(conn.execute(
                adfilter_segments.delete().where(adfilter_segments.c.fp_hash == hv)
            ).rowcount or 0)
            deleted_fp += int(conn.execute(
                adfilter_fingerprints.delete().where(adfilter_fingerprints.c.fp_hash == hv)
            ).rowcount or 0)
    if deleted_fp:
        audit_mod.record("delete", f"batch:{deleted_fp}", {
            "deleted_fingerprints": deleted_fp, "deleted_segments": deleted_seg,
            "not_found": not_found, "hosts": deleted_hosts[:20], "by": "batch_delete",
        }, actor="admin:batch_delete")
    return {
        "deleted_fingerprints": deleted_fp,
        "deleted_segments": deleted_seg,
        "not_found": not_found,
    }


def stats() -> dict:
    """
    指纹库概览（管理端面板用，v1.18.0 扩展）：
    各状态数量 + 累计命中/误杀 + 人工锁定/来源 + 达标阻断分桶 + 下发上限可见。
    """
    fp = adfilter_fingerprints
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                fp.c.status,
                func.count().label("n"),
                func.coalesce(func.sum(fp.c.hit_count), 0).label("hits"),
                func.coalesce(func.sum(fp.c.run_hits), 0).label("runs"),
                func.coalesce(func.sum(fp.c.miss_count), 0).label("miss"),
            ).group_by(fp.c.status)
        ).mappings().all()
        extras = conn.execute(
            select(
                func.count().label("total"),
                func.coalesce(func.sum(case((fp.c.locked == 1, 1), else_=0)), 0).label("locked"),
                func.coalesce(func.sum(case((fp.c.source == "manual", 1), else_=0)), 0).label("manual"),
                # candidate 中各项未达标的数量（blocked_by 分桶）
                func.coalesce(func.sum(case((
                    (fp.c.status == STATUS_CANDIDATE) & (fp.c.distinct_users < config.ADFILTER_FP_MIN_USERS), 1), else_=0)), 0).label("b_users"),
                func.coalesce(func.sum(case((
                    (fp.c.status == STATUS_CANDIDATE) & (fp.c.distinct_urls < config.ADFILTER_FP_MIN_URLS), 1), else_=0)), 0).label("b_urls"),
                func.coalesce(func.sum(case((
                    (fp.c.status == STATUS_CANDIDATE) & (fp.c.hit_count < config.ADFILTER_FP_MIN_HITS), 1), else_=0)), 0).label("b_hits"),
                # confirmed 但被误杀阈值挡住（不下发）的数量
                func.coalesce(func.sum(case((
                    (fp.c.status == STATUS_CONFIRMED) & (fp.c.miss_count >= config.ADFILTER_FP_MISS_DISABLE), 1), else_=0)), 0).label("c_blocked"),
            )
        ).mappings().first()
        # 明细体量与最早时间（§3.4 面板概览）
        detail = conn.execute(
            select(
                func.count().label("segments"),
                func.min(adfilter_segments.c.at).label("oldest"),
            ).select_from(adfilter_segments)
        ).mappings().first()

    by_status = {r["status"]: int(r["n"]) for r in rows}
    e = extras or {}
    confirmed_total = by_status.get(STATUS_CONFIRMED, 0)
    dl_count = len(downloadable())  # 实际下发数（已按 cap 截断）
    not_delivered_by_cap = max(0, confirmed_total - dl_count)
    return {
        "total": sum(by_status.values()),
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
        "downloadable": dl_count,
        "total_hits": sum(int(r["hits"] or 0) for r in rows),
        "total_run_hits": sum(int(r["runs"] or 0) for r in rows),
        "total_miss": sum(int(r["miss"] or 0) for r in rows),
        # v1.18.0 新增
        "manual_count": int(e.get("manual") or 0),
        "locked_count": int(e.get("locked") or 0),
        "confirmed_total": confirmed_total,
        "not_delivered_by_cap": not_delivered_by_cap,
        "capped": not_delivered_by_cap > 0,
        "confirmed_but_blocked": int(e.get("c_blocked") or 0),
        "blocked_by": {
            "users": int(e.get("b_users") or 0),
            "urls": int(e.get("b_urls") or 0),
            "hits": int(e.get("b_hits") or 0),
        },
        "detail": {
            "segments": int((detail or {}).get("segments") or 0),
            "oldest_at": int((detail or {}).get("oldest") or 0) or None,
            "segment_retention_days": config.ADFILTER_SEGMENT_RETENTION_DAYS,
            "report_retention_days": config.ADFILTER_REPORT_RETENTION_DAYS,
        },
        "config": {
            "dur_bucket_ms": config.ADFILTER_FP_DUR_BUCKET_MS,
            "size_bucket_b": config.ADFILTER_FP_SIZE_BUCKET_B,
            "min_users": config.ADFILTER_FP_MIN_USERS,
            "min_urls": config.ADFILTER_FP_MIN_URLS,
            "min_hits": config.ADFILTER_FP_MIN_HITS,
            "miss_disable": config.ADFILTER_FP_MISS_DISABLE,
            "max_download": config.ADFILTER_FP_MAX_DOWNLOAD,
            "enabled": config.ADFILTER_FP_ENABLED,
        },
    }


def miss_samples(days: int = 7, limit: int = 50) -> dict:
    """
    漏判样本明细（§3.3）：按 host 聚合「上报了但没过滤掉」（removed_runs=0）的举报，
    left join 指纹库给出覆盖度与判定，帮助运营回答「这个站为什么还在漏」。

    verdict:
      - uncovered      该 host 无任何指纹
      - candidate_only 有指纹但无可下发（未达标/未发布）
      - covered        有下发中指纹却仍被举报 → 指纹不准或广告换了切片
    **不返回 url 原文**，沿用隐私口径（host 级聚合）。
    """
    from .models import adfilter_reports  # 局部导入避免顶部循环依赖噪音

    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 200))
    cutoff = int(time_module.time()) - days * 86400
    with database.engine.connect() as conn:
        hosts = conn.execute(
            select(
                adfilter_reports.c.host,
                func.count().label("reports"),
                func.count(func.distinct(adfilter_reports.c.url_hash)).label("urls"),
                func.max(adfilter_reports.c.at).label("last_at"),
            )
            .where(
                adfilter_reports.c.removed_runs == 0,
                adfilter_reports.c.at >= cutoff,
                adfilter_reports.c.host != "",
            )
            .group_by(adfilter_reports.c.host)
            .order_by(func.count().desc())
            .limit(limit)
        ).mappings().all()
        host_names = [h["host"] for h in hosts]
        fps = []
        if host_names:
            fps = conn.execute(
                select(
                    adfilter_fingerprints.c.host,
                    func.count().label("n"),
                    func.sum(case((adfilter_fingerprints.c.status == STATUS_CONFIRMED, 1), else_=0)).label("confirmed"),
                    func.sum(case((
                        (adfilter_fingerprints.c.status == STATUS_CONFIRMED)
                        & (adfilter_fingerprints.c.miss_count < config.ADFILTER_FP_MISS_DISABLE), 1), else_=0)).label("dl"),
                )
                .where(adfilter_fingerprints.c.host.in_(host_names))
                .group_by(adfilter_fingerprints.c.host)
            ).mappings().all()
    fp_by_host = {f["host"]: f for f in fps}
    items = []
    for h in hosts:
        f = fp_by_host.get(h["host"])
        n = int(f["n"] or 0) if f else 0
        confirmed = int(f["confirmed"] or 0) if f else 0
        dl = int(f["dl"] or 0) if f else 0
        if dl > 0:
            verdict = "covered"
        elif n > 0:
            verdict = "candidate_only"
        else:
            verdict = "uncovered"
        items.append({
            "host": h["host"],
            "reports": int(h["reports"] or 0),
            "distinct_urls": int(h["urls"] or 0),
            "last_at": int(h["last_at"] or 0) or None,
            "fingerprints": {"total": n, "confirmed": confirmed, "downloadable": dl},
            "verdict": verdict,
        })
    return {"days": days, "total": len(items), "items": items}
