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
import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, text

from . import config, database
from .models import adfilter_fingerprints, adfilter_segments

log = logging.getLogger("vodsync.adfilter_fp")

# 状态机：candidate → confirmed（样本达标 / 人工审核通过）→ disabled（人工停用或误杀降权）
STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_DISABLED = "disabled"
STATUSES = (STATUS_CANDIDATE, STATUS_CONFIRMED, STATUS_DISABLED)

# 反馈类型
FEEDBACK_HIT = "hit"      # 客户端按指纹命中并成功过滤
FEEDBACK_MISS = "miss"    # 客户端认为该指纹误杀（不该删）

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
      - 已 confirmed 的若样本回落到门槛下（例如清理了历史数据）→ 降回 candidate，
        避免下发一个证据已经消失的指纹。

    v1.17.1 修复：pattern_json 必须携带 dur_bucket / size_bucket / dur_bucket_ms /
    size_bucket_b——downloadable() 从这里读桶字段下发给客户端，缺了会全部回落
    默认 0，客户端永远匹配不上（阻断性 bug）。同一 fp_hash 组内所有片段的桶序号
    必然相同（fp_hash 就是桶字段算出来的），所以聚合时取 min(dur_ms)/min(size_b)
    样本即可无损还原桶字段；更新分支同样回写 pattern_json，保证存量坏数据在
    重跑 rebuild 后自动修复。
    返回 {scanned, created, confirmed, demoted, disabled_kept}。
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

    created = confirmed = demoted = disabled_kept = 0
    now = datetime.now()

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
                    created_at=now, updated_at=now,
                ))
                created += 1
                if status == STATUS_CONFIRMED:
                    confirmed += 1
            else:
                old_status = existing["status"]
                if old_status == STATUS_DISABLED:
                    # 只更新时间证据与 pattern（桶字段修复），不动 status/confidence 的下发判定
                    conn.execute(
                        adfilter_fingerprints.update()
                        .where(adfilter_fingerprints.c.id == existing["id"])
                        .values(
                            hit_count=hits, distinct_users=users, distinct_urls=urls,
                            pattern_json=pattern_json,
                            last_seen_at=last_seen, updated_at=now,
                        )
                    )
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
                if old_status == STATUS_CONFIRMED and new_status == STATUS_CANDIDATE:
                    demoted += 1

    # 明细被清空的指纹：保留实体（历史发布还可能引用），但状态降为 candidate
    if seen:
        with database.write_txn() as conn:
            stale = conn.execute(
                select(adfilter_fingerprints.c.id).where(
                    adfilter_fingerprints.c.fp_hash.notin_(list(seen)),
                    adfilter_fingerprints.c.status == STATUS_CONFIRMED,
                )
            ).scalars().all()
            for fid in stale:
                conn.execute(
                    adfilter_fingerprints.update()
                    .where(adfilter_fingerprints.c.id == fid)
                    .values(status=STATUS_CANDIDATE, updated_at=now)
                )
            demoted += len(stale)

    result = {
        "scanned": len(grouped),
        "created": created,
        "confirmed": confirmed,
        "demoted": demoted,
        "disabled_kept": disabled_kept,
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


# ---------------------------------------------------------------------------
# 2d：反馈闭环
# ---------------------------------------------------------------------------
def feedback(fp_hash: str, kind: str) -> dict:
    """
    客户端回报某指纹命中 / 误杀。返回该指纹的最新状态。

    - hit ：run_hits++（正向证据，不影响 status）
    - miss：miss_count++，累计达到 ADFILTER_FP_MISS_DISABLE → 自动 disabled，
            并从下发列表中移除（下次发布规则包生效）。
    未知 fp_hash 直接返回 {found:false}，不报错——客户端缓存了旧指纹是正常的。
    """
    if kind not in (FEEDBACK_HIT, FEEDBACK_MISS):
        raise FPError(f"feedback kind 必须是 {FEEDBACK_HIT}/{FEEDBACK_MISS}", "kind")
    fp_hash = (fp_hash or "").strip().lower()
    if not fp_hash:
        raise FPError("fp_hash 不能为空", "fp_hash")

    with database.write_txn() as conn:
        row = conn.execute(
            select(
                adfilter_fingerprints.c.id,
                adfilter_fingerprints.c.miss_count,
                adfilter_fingerprints.c.status,
            ).where(adfilter_fingerprints.c.fp_hash == fp_hash)
        ).mappings().first()
        if row is None:
            return {"found": False, "fp_hash": fp_hash}

        now = datetime.now()
        if kind == FEEDBACK_HIT:
            conn.execute(
                adfilter_fingerprints.update()
                .where(adfilter_fingerprints.c.id == row["id"])
                .values(run_hits=adfilter_fingerprints.c.run_hits + 1, updated_at=now)
            )
            new_miss = int(row["miss_count"] or 0)
            status = row["status"]
        else:
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
                    updated_at=now,
                )
            )
            status = new_status
            if disable:
                log.info("[adfilter_fp] 指纹 %s… 误杀 %d 次，自动停用", fp_hash[:12], new_miss)
    return {
        "found": True,
        "fp_hash": fp_hash,
        "status": status,
        "miss_count": new_miss,
        "downloadable": status == STATUS_CONFIRMED and new_miss < config.ADFILTER_FP_MISS_DISABLE,
    }


# ---------------------------------------------------------------------------
# 管理端查询 / 审核
# ---------------------------------------------------------------------------
def list_fingerprints(
    status: str = "",
    host: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """指纹分页列表（管理端）。status/host 可筛选，按置信度降序。"""
    where = []
    if status in STATUSES:
        where.append(adfilter_fingerprints.c.status == status)
    if host:
        where.append(adfilter_fingerprints.c.host == host.strip().lower()[:253])
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    with database.engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(adfilter_fingerprints).where(*where)
        ).scalar() or 0
        rows = conn.execute(
            select(adfilter_fingerprints)
            .where(*where)
            .order_by(
                adfilter_fingerprints.c.confidence.desc(),
                adfilter_fingerprints.c.hit_count.desc(),
            )
            .limit(limit).offset(offset)
        ).mappings().all()
    return {
        "total": int(total),
        "items": [_row_to_dict(r) for r in rows],
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


def _row_to_dict(m) -> dict:
    return {
        "id": int(m["id"]),
        "fp_hash": m["fp_hash"],
        "host": m["host"],
        "pattern": _safe_json(m["pattern_json"]),
        "status": m["status"],
        "confidence": round(float(m["confidence"] or 0), 4),
        "hit_count": int(m["hit_count"] or 0),
        "distinct_users": int(m["distinct_users"] or 0),
        "distinct_urls": int(m["distinct_urls"] or 0),
        "run_hits": int(m["run_hits"] or 0),
        "miss_count": int(m["miss_count"] or 0),
        "downloadable": (
            m["status"] == STATUS_CONFIRMED
            and int(m["miss_count"] or 0) < config.ADFILTER_FP_MISS_DISABLE
        ),
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


def review(fp_hash: str, status: str, note: str = "") -> dict:
    """
    人工审核指纹（管理端）：把状态设为 confirmed / candidate / disabled 之一。

    人工 confirmed 不受样本门槛限制（运营判断优先），但要写 reviewed_at。
    disabled 是「止血」，永不自动恢复。
    """
    if status not in STATUSES:
        raise FPError(f"status 必须是 {'/'.join(STATUSES)}", "status")
    fp_hash = (fp_hash or "").strip().lower()
    with database.write_txn() as conn:
        row = conn.execute(
            select(adfilter_fingerprints.c.id)
            .where(adfilter_fingerprints.c.fp_hash == fp_hash)
        ).first()
        if row is None:
            raise LookupError(f"不存在指纹 {fp_hash}")
        conn.execute(
            adfilter_fingerprints.update()
            .where(adfilter_fingerprints.c.fp_hash == fp_hash)
            .values(
                status=status,
                note=(note or "")[:MAX_NOTE],
                reviewed_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
    return get_fingerprint(fp_hash)


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


def stats() -> dict:
    """指纹库概览（管理端面板用）：各状态数量 + 累计命中/误杀。"""
    with database.engine.connect() as conn:
        rows = conn.execute(
            select(
                adfilter_fingerprints.c.status,
                func.count().label("n"),
                func.coalesce(func.sum(adfilter_fingerprints.c.hit_count), 0).label("hits"),
                func.coalesce(func.sum(adfilter_fingerprints.c.run_hits), 0).label("runs"),
                func.coalesce(func.sum(adfilter_fingerprints.c.miss_count), 0).label("miss"),
            ).group_by(adfilter_fingerprints.c.status)
        ).mappings().all()
    by_status = {r["status"]: int(r["n"]) for r in rows}
    return {
        "total": sum(by_status.values()),
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
        "downloadable": len(downloadable()),
        "total_hits": sum(int(r["hits"] or 0) for r in rows),
        "total_run_hits": sum(int(r["runs"] or 0) for r in rows),
        "total_miss": sum(int(r["miss"] or 0) for r in rows),
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
