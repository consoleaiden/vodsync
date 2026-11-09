"""
v1.12.1 数据表浏览：下拉显示源站名称（而不是 vod_s361c088099 这种 slug）。

背景：v1.12.0 的下拉直接用表名 `vod_{slug}`，面板上看到一串 `s361c088099`
无法判断是哪个站，必须回「源站」区块对照。v1.12.1 起清单接口为每张表补
`label` 字段（vod_* 表取 sources.name），表名仍留在 `name` 里：

    name  = vod_s361c088099      ← 接口契约 / 实际表名（option 的 value 与 title）
    label = 新极品资源站          ← 面板显示名

覆盖项：
 A. 有对应源站：label = 源站名，name 仍是表名，rows 仍取 total_videos 缓存值
 B. 无对应源站（表在、sources 无此行）：label 带「无对应源站」，不显示空串
 C. 同名源站：label 追加「（表名）」区分，保证下拉里两项可分辨
 D. 系统表：label == name（没有更友好的名字，不硬造）
 E. 排序：vod 段按 label 升序（源站名比 slug 有意义的排序）
 F. 接口契约：清单每项都有非空 label；option 的 value 仍是表名（前端据此请求）
 G. 回归：browse() 仍按表名分页/搜索；源站列表接口不受影响
 H. 版本号 1.12.1

运行：python scripts/test_v1121_tablelabel.py
"""
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v1121-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1121.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# 真实形态的样本：(slug, 源站名, total_videos)
REAL = ("s361c088099", "新极品资源站", 72721)
ORPHAN = ("sorphan1234", None, 0)          # 表在、sources 里没有
DUP_A = ("sdup1aaaaaa", "同名资源站", 3)
DUP_B = ("sdup2bbbbbb", "同名资源站", 4)
DIGIT = ("sd834aeddd0", "360资源站", 112000)


def main():
    from fastapi.testclient import TestClient

    from app import database, syncer, tablebrowse
    from app.main import app
    from app.models import sources

    database.init_db()
    client = TestClient(app)

    with database.write_txn() as conn:
        for slug, name, total in (REAL, DUP_A, DUP_B, DIGIT):
            conn.execute(sources.insert().values(
                name=name, slug=slug, api_url="https://example.com/api.php",
                status="ok", total_videos=total,
            ))
    for slug, _, _ in (REAL, ORPHAN, DUP_A, DUP_B, DIGIT):
        syncer.ensure_table(slug)

    resp = client.get("/api/v1/admin/tables")
    check("0 清单接口 200", resp.status_code == 200, resp.status_code)
    items = resp.json()["tables"]
    by_name = {t["name"]: t for t in items}

    # ---- A. 有对应源站 ----
    real = by_name.get(f"vod_{REAL[0]}")
    check("A1 vod 表出现在清单里", real is not None, list(by_name))
    check("A2 label 是源站名称", real and real["label"] == REAL[1], real and real["label"])
    check("A3 name 仍是表名（契约不变）", real and real["name"] == f"vod_{REAL[0]}", real and real["name"])
    check("A4 rows 仍取 total_videos 缓存值", real and real["rows"] == REAL[2], real and real["rows"])
    check("A5 列元信息未受影响", real and any(c["name"] == "play_from" for c in real["columns"]),
          real and [c["name"] for c in real["columns"]][:3])

    # ---- B. 无对应源站的孤儿表 ----
    orphan = by_name.get(f"vod_{ORPHAN[0]}")
    check("B1 孤儿表仍在清单里", orphan is not None)
    check("B2 孤儿表 label 非空且带提示", orphan and orphan["label"] and "无对应源站" in orphan["label"],
          orphan and orphan["label"])
    check("B3 孤儿表 label 含 slug 便于定位", orphan and ORPHAN[0] in orphan["label"], orphan and orphan["label"])

    # ---- C. 同名源站去歧义 ----
    dup_a, dup_b = by_name.get(f"vod_{DUP_A[0]}"), by_name.get(f"vod_{DUP_B[0]}")
    check("C1 同名两条都在", dup_a and dup_b)
    check("C2 同名词都保留源站名", dup_a and dup_b and DUP_A[1] in dup_a["label"] and DUP_A[1] in dup_b["label"],
          (dup_a and dup_a["label"], dup_b and dup_b["label"]))
    check("C3 同名追加表名区分", dup_a and dup_b and dup_a["label"] != dup_b["label"],
          (dup_a and dup_a["label"], dup_b and dup_b["label"]))
    check("C4 区分后缀是各自表名",
          dup_a and dup_b and f"vod_{DUP_A[0]}" in dup_a["label"] and f"vod_{DUP_B[0]}" in dup_b["label"],
          (dup_a and dup_a["label"], dup_b and dup_b["label"]))

    # ---- D. 系统表 ----
    for nm in ("sources", "sync_logs", "star_terms", "vod_related", "star_map"):
        t = by_name.get(nm)
        check(f"D {nm} label == name", t and t["label"] == nm, t and t.get("label"))
    check("D6 settings 仍不可浏览", "settings" not in by_name, list(by_name))

    # ---- E. 排序 ----
    vod_labels = [t["label"] for t in items if t["kind"] == "vod"]
    check("E1 vod 段按 label 升序", vod_labels == sorted(vod_labels), vod_labels)
    check("E2 排序与表名顺序无关（360 在中文前）", vod_labels and vod_labels[0].startswith("360"),
          vod_labels[:2])

    # ---- F. 接口契约 ----
    check("F1 每项都有 label 且非空", all(t.get("label") for t in items),
          [t["name"] for t in items if not t.get("label")])
    check("F2 每项都有 name（option value 用）", all(t.get("name") for t in items))
    check("F3 未见 label 里混入 null 字符串", all(t["label"] != "None" for t in items),
          [t["label"] for t in items])

    # ---- G. 回归：仍按表名浏览 ----
    syncer.upsert_batch(REAL[0], [
        {"vod_id": 1, "type_id": 1, "type_name": "国产剧", "name": "三体", "pic": "",
         "remarks": "HD", "content": "c", "play_url": "第1集$https://x/1.m3u8",
         "vod_time": "2026-11-01 00:00:00"},
        {"vod_id": 2, "type_id": 2, "type_name": "电影", "name": "流浪地球2", "pic": "",
         "remarks": "TC", "content": "c", "play_url": "第1集$https://x/2.m3u8",
         "vod_time": "2026-11-02 00:00:00"},
    ])
    r = client.get(f"/api/v1/admin/tables/vod_{REAL[0]}/rows?page=1&page_size=1")
    check("G1 按表名分页 200", r.status_code == 200, r.status_code)
    d = r.json()
    check("G2 分页口径不变", d["total"] == 2 and d["pages"] == 2 and len(d["rows"]) == 1,
          (d["total"], d["pages"]))
    r = client.get(f"/api/v1/admin/tables/vod_{REAL[0]}/rows?field=name&q=地球").json()
    check("G3 按字段搜索结果不变", r["total"] == 1 and r["rows"][0]["name"] == "流浪地球2",
          (r["total"], [x["name"] for x in r["rows"]]))
    check("G4 源站列表接口不受影响", client.get("/api/v1/sources").status_code == 200)
    direct = tablebrowse.list_browsable()
    check("G5 模块与接口同构", {t["name"]: t["label"] for t in direct} ==
          {t["name"]: t["label"] for t in items})

    # ---- H. 版本 ----
    from app import __version__

    check("H1 版本号 1.12.1", __version__ == "1.12.1", __version__)

    print()
    print("=" * 50)
    print(f"通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
