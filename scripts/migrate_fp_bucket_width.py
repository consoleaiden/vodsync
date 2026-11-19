"""
桶宽变更迁移脚本（v1.19.0 §4.5）。

背景：fp_hash = sha256(host|dur_bucket|size_bucket)，桶宽一改，全库指纹失效。
本脚本按当前 config 的 ADFILTER_FP_DUR_BUCKET_MS / ADFILTER_FP_SIZE_BUCKET_B
重新计算 segments 的 fp_hash 与桶字段，重建 fingerprints 聚合，删除不再可用的旧指纹。

用法（在生产库上执行前务必先备份数据库）：
    # 预览，不写库（强烈建议先看一遍影响面）
    PYTHONPATH=. python scripts/migrate_fp_bucket_width.py --dry-run

    # 实际执行
    PYTHONPATH=. python scripts/migrate_fp_bucket_width.py --apply

    # 执行后按常规流程重算 + 发布（本脚本已重建聚合，rebuild 可再跑一次校准）
    # 面板点「重算聚合」→「发布指纹到规则包」

设计约束：
  - 只处理 adfilter_segments / adfilter_fingerprints；不改规则包历史（快照保持原样）。
  - 迁移后 published 包仍是旧 hash；务必发布新包后客户端才会用新指纹（会有短暂
    新旧 hash 并存窗口，rebuild + publish 后一致）。
  - 本脚本是显式运维动作，面板不提供按钮（§4.5 明确要求）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app import config, database  # noqa: E402
from app.adfilter_fp import signature  # noqa: E402
from app.models import adfilter_fingerprints, adfilter_segments  # noqa: E402


def _bucket_signature_counts():
    """统计当前库里使用旧桶宽的指纹数（pattern_json 里的桶宽与 config 不一致）。"""
    outdated = 0
    total = 0
    with database.engine.connect() as conn:
        rows = conn.execute(select(adfilter_fingerprints.c.pattern_json)).scalars().all()
    for raw in rows:
        total += 1
        try:
            pat = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not pat:
            continue
        if (int(pat.get("dur_bucket_ms", config.ADFILTER_FP_DUR_BUCKET_MS))
                != config.ADFILTER_FP_DUR_BUCKET_MS
                or int(pat.get("size_bucket_b", config.ADFILTER_FP_SIZE_BUCKET_B))
                != config.ADFILTER_FP_SIZE_BUCKET_B):
            outdated += 1
    return total, outdated


def migrate(apply: bool = False) -> dict:
    total_before, outdated_before = _bucket_signature_counts()
    with database.engine.connect() as conn:
        seg_rows = conn.execute(
            select(
                adfilter_segments.c.id,
                adfilter_segments.c.host,
                adfilter_segments.c.dur_ms,
                adfilter_segments.c.size_b,
                adfilter_segments.c.fp_hash,
            )
        ).mappings().all()

    # 逐条按当前桶宽重算 fp_hash
    remap = {}          # old_hash -> new_hash
    new_hash_buckets = set()
    changed_segments = 0
    for r in seg_rows:
        sig = signature(r["host"] or "", int(r["dur_ms"] or 0), int(r["size_b"] or 0))
        new_hash = sig["fp_hash"]
        old_hash = r["fp_hash"]
        if new_hash != old_hash:
            changed_segments += 1
        remap.setdefault(old_hash, new_hash)
        new_hash_buckets.add(new_hash)

    old_hashes = set(remap)
    # 迁移后仍需保留的旧 hash（新旧恰好相同的，不动）
    stale_hashes = {h for h in old_hashes if remap[h] != h}

    result = {
        "dry_run": not apply,
        "bucket_ms": config.ADFILTER_FP_DUR_BUCKET_MS,
        "bucket_b": config.ADFILTER_FP_SIZE_BUCKET_B,
        "segments_total": len(seg_rows),
        "segments_hash_changed": changed_segments,
        "fingerprints_outdated_before": outdated_before,
        "fingerprints_total_before": total_before,
        "old_hashes_to_migrate": len(stale_hashes),
        "new_hashes": len(new_hash_buckets),
    }

    if not apply:
        result["note"] = "预览模式，未写库。确认影响面后加 --apply 执行。"
        return result

    # 实际迁移：单事务。先删旧聚合（避免 unique 冲突），再改 segments hash，
    # 最后让调用方跑 rebuild 重建聚合（脚本内直接调 rebuild 更省事）。
    with database.write_txn() as conn:
        if stale_hashes:
            conn.execute(
                adfilter_fingerprints.delete().where(
                    adfilter_fingerprints.c.fp_hash.in_(list(stale_hashes))
                )
            )
        for old_h, new_h in remap.items():
            if old_h != new_h:
                conn.execute(
                    adfilter_segments.update()
                    .where(adfilter_segments.c.fp_hash == old_h)
                    .values(fp_hash=new_h)
                )

    # 重建聚合（新桶宽下重新分组 + 晋升），复用既有 rebuild 逻辑
    from app import adfilter_fp
    rebuild_result = adfilter_fp.rebuild()
    result["rebuild"] = rebuild_result
    total_after, outdated_after = _bucket_signature_counts()
    result["fingerprints_total_after"] = total_after
    result["fingerprints_outdated_after"] = outdated_after
    result["note"] = ("迁移完成。请到面板确认聚合结果并发布新规则包；"
                      "发布后客户端才会使用新桶宽的指纹。")
    return result


def main():
    ap = argparse.ArgumentParser(description="桶宽变更迁移（§4.5）")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认仅预览）")
    ap.add_argument("--dry-run", action="store_true", help="显式预览（默认行为）")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run
    result = migrate(apply=apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
