"""
标签倒排索引回填 CLI（v1.14.0 / B-01）。

用途
----
`video_tags` 是「标签精确检索（?tag=）」的数据源。正常运行时同步入库就会
增量维护，升级后启动也会自动补齐 —— 这个脚本是给**运维手动兜底**用的：

  - 容器里想看着进度跑（不依赖面板）
  - 只补某几个源站
  - 怀疑索引与 vod_tag 不一致，强制重建

用法
----
    # 全部启用源站（断点续跑，已有的站会跳过已处理部分）
    docker compose exec vodsync python scripts/backfill_tags.py

    # 只补一个站
    docker compose exec vodsync python scripts/backfill_tags.py --slug ffzy

    # 清空该源站标签行后从头重建
    docker compose exec vodsync python scripts/backfill_tags.py --slug ffzy --force

    # 看进度（不写数据）
    docker compose exec vodsync python scripts/backfill_tags.py --status

注意
----
- 幂等：重复执行不会产生重复行（主键去重 + 先删后插）；
- 可中断：游标落 `tag_index_state`，Ctrl+C 后重跑会从上次的 vod_id 继续；
- 限速：批间休眠 TAG_INDEX_SLEEP_MS（默认 50ms），避免和同步任务抢 IO；
- 不改变任何 API 行为；建完之前 `/healthz` 不会声明 `tag_search`。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description="标签倒排索引回填")
    parser.add_argument("--slug", default="", help="只处理指定源站（默认全部启用源站）")
    parser.add_argument("--force", action="store_true", help="清空该源站标签行后从头重建")
    parser.add_argument("--status", action="store_true", help="只打印当前状态，不写数据")
    args = parser.parse_args()

    from app import database, tags

    database.init_db()

    if args.status:
        st = tags.status()
        print(f"就绪: {st['ready']}   总行数: {st['rows']}   正在回填: {st['building'] or '-'}")
        print(f"{'源站':<20}{'状态':<10}{'游标':>12}{'总数':>12}{'已写行':>12}  进度")
        for s in st["sources"]:
            print(f"{s['slug']:<20}{s['status']:<10}{s['cursor']:>12}{s['total']:>12}"
                  f"{s['indexed']:>12}  {s['progress']}%{('  ' + s['error']) if s['error'] else ''}")
        return 0

    started = time.time()
    results = tags.rebuild_all(slug=args.slug, force=args.force)
    ok = sum(1 for r in results if r.get("status") == "done")
    fail = [r for r in results if r.get("status") != "done"]
    print(f"\n完成: {ok} 个源站成功, {len(fail)} 个失败, 耗时 {time.time() - started:.1f}s")
    for r in fail:
        print(f"  FAIL {r.get('slug')}: {r.get('error')}")
    st = tags.status()
    print(f"索引总行数: {st['rows']}   就绪: {st['ready']}")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
