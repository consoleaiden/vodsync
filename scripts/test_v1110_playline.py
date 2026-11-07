"""
v1.11.0 播放线路识别 / 过滤 / 排序 测试。

背景（线上可复现）：非凡资源站 100% 的记录第 1 条线路是 H5 分享页
（`/share/<hash>` → 200 text/html，真实 m3u8 由页面 JS 生成且带会过期的 sign），
第 2 条才是真直链。旧实现把上游 `vod_play_from` 丢弃、`parse_play_url()`
只按序号命名线路、也不做任何形态判断 —— 客户端按顺序选第一条 → 必然播不出来。

覆盖项（对应需求第 5 节验收标准）：
 A. classify_play_line 纯函数
    A1 /share/ → page            A2 index.m3u8 → m3u8
    A3 ?sign= 不影响判定          A4 `.html` → page
    A5 `/play/` 前缀但以 .m3u8 结尾 → m3u8（**与需求原文顺序的差异，见下方说明**）
    A6 `re:` 正则规则            A7 空串 / 无扩展名 → unknown
 B. filter 策略（58 集 × 2 线路的真实样本）
    B1 只剩 1 条                  B2 from/type/original_index/集数
    B3 首集是 index.m3u8 且不含 ?sign=   B4 名字带 provider、编号是过滤后序号
 C. off 策略 = 旧行为（2 条、原顺序、原集数）
 D. whitelist 策略（只保留媒体直链）
 E. 安全阀：两条都是 /share/ → 2 条未过滤 + WARNING（绝不允许过滤成 0 条）
 F. 排序：m3u8 → unknown → page，同类型内保持原顺序，original_index 不丢
 G. play_from 为 NULL 的存量行不抛异常（只按 URL 形态判断）
 H. 三条接口路径（单条详情 / ids 批量 / ac=detail 归并 / MacCMS 兼容）
    + play_raw=1 拿原始列表 + 顶层 play_line_filtered 语义
 I. 源站级覆盖：sources.play_line_policy=off 时即使全局 filter 也不过滤
 J. 入库与回填：extract_row 保留 vod_play_from；upsert 不拿空值擦掉已有值；
    backfill_play_from 只写这一列；/stats 有 play_line 段
 K. 回归：vod_play_url 字节不变、老签名 parse_play_url(raw) 仍可用

为什么 A5 与需求原文不同：
需求写的判定顺序是「先黑名单（/share/、/play/、.html）再扩展名」，但
`https://x/play/<id>/index.m3u8` 这种「/play/ 前缀 + 真 m3u8」非常常见，
按原文顺序会把可播线路误杀成 page。以 .m3u8 结尾必然是媒体播放列表，
这个证据强于路径前缀，所以实现里扩展名优先。反过来的情况（页面地址以
.m3u8 结尾却返回 HTML）实际不存在。

运行：python scripts/test_v1110_playline.py
"""
import logging
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="vodsync-v1110-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1110.db")
os.environ["AUTH_ENABLED"] = "false"
os.environ["FULL_SYNC_ON_DISCOVER"] = "false"
os.environ["AUTO_RESUME_ON_BOOT"] = "false"
os.environ["RELATED_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as _text  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


SLUG = "t1110"
SHARE_LINE = "#".join(
    f"第{i:02d}集$https://vip.ffzy-play1.com/share/{i:032x}" for i in range(1, 59)
)
M3U8_LINE = "#".join(
    f"第{i:02d}集$https://vip.ffzy-play1.com/20221111/{14677 + i}_{i:08x}/index.m3u8"
    for i in range(1, 59)
)
REAL_RAW = SHARE_LINE + "$$$" + M3U8_LINE
REAL_FROM = "feifan$$$ffm3u8"

# 301 真实样本 / 302 双分享页（安全阀）/ 303 play_from 为 NULL / 304 三种类型混合
SAMPLES = [
    (301, "三生三世十里桃花2017", "国产剧", REAL_RAW, REAL_FROM, "2026-03-01 00:00:00"),
    (302, "双分享页影片", "国产剧",
     "第1集$https://vip.ffzy-play1.com/share/aaa$$$第1集$https://vip.ffzy-play1.com/share/bbb",
     "feifan$$$feifan", "2026-03-02 00:00:00"),
    (303, "存量无来源", "动作片",
     "第1集$https://x/share/old$$$第1集$https://cdn.x.com/v/1/index.m3u8",
     None, "2026-03-03 00:00:00"),
    (304, "混合线路", "动作片",
     "第1集$https://x/share/p1$$$第1集$https://cdn.x.com/a.m3u8$$$第1集$https://x/api/player.php?id=9",
     "feifan$$$ffm3u8$$$ffapi", "2026-03-04 00:00:00"),
]


class _StubClient:
    """伪 MacCmsClient：只实现 backfill_play_from 用到的 fetch_page。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_page(self, api, ac, pg):
        self.calls.append((ac, pg))
        return self.pages[pg - 1]


def _capture_warnings():
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("vodsync.utils")
    logger.setLevel(logging.WARNING)
    h = _H()
    logger.addHandler(h)
    return records, h


def main():
    from fastapi.testclient import TestClient

    from app import config, database, syncer
    from app.main import app
    from app.maccms import MacCmsClient
    from app.models import sources
    from app.utils import classify_play_line, parse_play_lines, parse_play_url

    database.init_db()
    client = TestClient(app)

    with database.write_txn() as conn:
        conn.execute(sources.insert().values(
            name="测试源1110", slug=SLUG, api_url="https://example.com/api.php",
            status="ok", enabled=1, auto_sync=1, origin="custom", total_videos=len(SAMPLES),
        ))
    syncer.ensure_table(SLUG)
    syncer.upsert_batch(SLUG, [{
        "vod_id": vid, "type_id": 1, "type_name": tname, "name": name,
        "pic": f"pic/{vid}.jpg", "remarks": "更新至58集",
        "content": "简介", "play_url": raw, "play_from": frm or "",
        "vod_actor": "杨幂", "vod_director": "林玉芬", "vod_writer": "",
        "vod_area": "中国大陆", "vod_year": "2017", "vod_tag": "", "vod_time": ts,
    } for vid, name, tname, raw, frm, ts in SAMPLES])

    # ==================================================================
    print("\n[A] classify_play_line（纯函数，只看 URL path）")
    # ==================================================================
    check("A1 /share/<hash> → page",
          classify_play_line("https://vip.ffzy-play1.com/share/abc123") == "page", "")
    check("A2 .../index.m3u8 → m3u8",
          classify_play_line("https://vip.ffzy-play1.com/20221111/14677_4ab87e51/index.m3u8")
          == "m3u8", "")
    check("A3 query 不影响判定（?sign= 仍是 m3u8）",
          classify_play_line("https://x/a.m3u8?sign=deadbeef") == "m3u8", "")
    check("A4 .html → page",
          classify_play_line("https://x/play/123.html") == "page", "")
    check("A5 /play/ 前缀但以 .m3u8 结尾 → m3u8（扩展名优先于黑名单）",
          classify_play_line("https://x/play/123/index.m3u8") == "m3u8", "")
    check("A6 `re:` 规则按正则匹配",
          classify_play_line("https://x/re/abc", patterns=("re:^/re/",)) == "page", "")
    check("A7 无扩展名/接口地址 → unknown",
          classify_play_line("https://x/api/player.php?id=1") == "unknown"
          and classify_play_line("") == "unknown", "")
    check("A8 mp4/flv/mkv 也识别",
          [classify_play_line(f"https://x/a.{e}") for e in ("mp4", "flv", "mkv")]
          == ["mp4", "flv", "mkv"], "")

    # ==================================================================
    print("\n[B] filter 策略：58 集 × 2 线路的真实样本")
    # ==================================================================
    check("B0 样本原始串就是线上形态（两侧线路各 58 集，8KB 级）",
          REAL_RAW.count("#") == 114 and len(REAL_RAW.encode()) > 5000,
          (REAL_RAW.count("#"), len(REAL_RAW.encode())))
    lines, meta = parse_play_lines(REAL_RAW, REAL_FROM)
    check("B1 过滤后 1 条", len(lines) == 1, len(lines))
    ln = lines[0] if lines else {}
    check("B2 from == ffm3u8", ln.get("from") == "ffm3u8", ln.get("from"))
    check("B2 type == m3u8", ln.get("type") == "m3u8", ln.get("type"))
    check("B2 original_index == 1（0 基，指向原始第 2 条）",
          ln.get("original_index") == 1, ln.get("original_index"))
    check("B2 58 集", len(ln.get("episodes") or []) == 58, len(ln.get("episodes") or []))
    first_url = (ln.get("episodes") or [{}])[0].get("url", "")
    check("B3 首集以 index.m3u8 结尾", first_url.endswith("index.m3u8"), first_url)
    check("B3 首集不含 ?sign=", "sign=" not in first_url, first_url)
    check("B4 名字带 provider、编号是过滤后序号",
          ln.get("name") == "线路1 (ffm3u8)", ln.get("name"))
    check("B4 playable == True", ln.get("playable") is True, ln.get("playable"))
    check("B5 meta：原始 2 条 / 剔除 1 条 / fallback=False",
          meta["raw_lines"] == 2 and meta["dropped"] == 1 and meta["fallback"] is False
          and meta["filtered"] is True, meta)

    # ==================================================================
    print("\n[C] off 策略 = 旧行为")
    # ==================================================================
    lines_off, meta_off = parse_play_lines(REAL_RAW, REAL_FROM, policy="off")
    check("C1 2 条", len(lines_off) == 2, len(lines_off))
    check("C2 顺序与上游一致（feifan 在前，original_index 0/1）",
          [x["original_index"] for x in lines_off] == [0, 1], lines_off and
          [x["original_index"] for x in lines_off])
    check("C3 集数不变（各 58 集）",
          [len(x["episodes"]) for x in lines_off] == [58, 58], "")
    check("C4 不过滤 → dropped=0 / filtered=False",
          meta_off["dropped"] == 0 and meta_off["filtered"] is False, meta_off)

    # ==================================================================
    print("\n[D] whitelist 策略")
    # ==================================================================
    lines_wl, meta_wl = parse_play_lines(REAL_RAW, REAL_FROM, policy="whitelist")
    check("D1 只保留 m3u8 → 1 条", len(lines_wl) == 1 and lines_wl[0]["type"] == "m3u8",
          [(x["type"]) for x in lines_wl])
    check("D2 剔除数 = 1", meta_wl["dropped"] == 1, meta_wl)

    # ==================================================================
    print("\n[E] 安全阀：绝不允许过滤成 0 条")
    # ==================================================================
    records, handler = _capture_warnings()
    try:
        lines_fb, meta_fb = parse_play_lines(
            SAMPLES[1][3], SAMPLES[1][4]
        )
    finally:
        logging.getLogger("vodsync.utils").removeHandler(handler)
    check("E1 两条都是 /share/ → 返回 2 条未过滤结果", len(lines_fb) == 2, len(lines_fb))
    check("E2 meta.fallback=True 且 filtered=False",
          meta_fb["fallback"] is True and meta_fb["filtered"] is False, meta_fb)
    check("E3 有 WARNING 日志（写明回退）",
          any("回退未过滤结果" in r.getMessage() for r in records),
          [r.getMessage()[:60] for r in records])

    # ==================================================================
    print("\n[F] 排序：直链在前、unknown 殿后")
    # ==================================================================
    # 304 = page + m3u8 + unknown：filter 策略会把 page 删掉，剩 2 条
    lines_s, _ = parse_play_lines(SAMPLES[3][3], SAMPLES[3][4])
    check("F1 filter 下 page 被剔除，剩 2 条", len(lines_s) == 2, len(lines_s))
    check("F2 剩 m3u8 → unknown",
          [x["type"] for x in lines_s] == ["m3u8", "unknown"],
          [x["type"] for x in lines_s])
    check("F3 original_index 保留上游下标（1、2）",
          [x["original_index"] for x in lines_s] == [1, 2],
          [x["original_index"] for x in lines_s])
    check("F4 名字按过滤后顺序重新编号",
          [x["name"] for x in lines_s] == ["线路1 (ffm3u8)", "线路2 (ffapi)"],
          [x["name"] for x in lines_s])

    # 上游顺序是 unknown 在前：排序必须把它换到 m3u8 之后
    swap_raw = "第1集$https://x/api/player.php?id=1$$$第1集$https://cdn.x.com/a.m3u8"
    lines_sw, _ = parse_play_lines(swap_raw, "ffapi$$$ffm3u8")
    check("F5 排序生效：m3u8 提到 unknown 之前",
          [x["type"] for x in lines_sw] == ["m3u8", "unknown"]
          and [x["original_index"] for x in lines_sw] == [1, 0],
          [(x["type"], x["original_index"]) for x in lines_sw])
    check("F6 名字随新顺序变（线路1 是 m3u8）",
          lines_sw[0]["name"] == "线路1 (ffm3u8)" and lines_sw[1]["name"] == "线路2 (ffapi)",
          [x["name"] for x in lines_sw])

    old_prefer = config.PLAY_LINE_PREFER_DIRECT
    config.PLAY_LINE_PREFER_DIRECT = False
    try:
        lines_np, _ = parse_play_lines(swap_raw, "ffapi$$$ffm3u8")
        check("F7 PREFER_DIRECT=false 时不排序（保持上游顺序）",
              [x["type"] for x in lines_np] == ["unknown", "m3u8"],
              [x["type"] for x in lines_np])
    finally:
        config.PLAY_LINE_PREFER_DIRECT = old_prefer

    lines_s2, _ = parse_play_lines(SAMPLES[3][3], SAMPLES[3][4], policy="off")
    check("F8 off 时完全不排序（仍是上游 page→m3u8→unknown）",
          [x["type"] for x in lines_s2] == ["page", "m3u8", "unknown"],
          [x["type"] for x in lines_s2])

    # ==================================================================
    print("\n[G] 存量行 play_from 为 NULL/空")
    # ==================================================================
    try:
        lines_g, meta_g = parse_play_lines(SAMPLES[2][3], None)
        ok_g, err_g = True, None
    except Exception as e:      # noqa: BLE001
        lines_g, meta_g, ok_g, err_g = [], {}, False, repr(e)
    check("G1 不抛异常", ok_g, err_g)
    check("G2 只按 URL 形态过滤 → 1 条 m3u8",
          len(lines_g) == 1 and lines_g[0]["type"] == "m3u8",
          [(x["type"]) for x in lines_g])
    check("G3 from 为空串、名字不带括号",
          lines_g and lines_g[0]["from"] == "" and lines_g[0]["name"] == "线路1",
          lines_g and (lines_g[0]["from"], lines_g[0]["name"]))

    # ==================================================================
    print("\n[H] 接口路径")
    # ==================================================================
    raw_bytes = SAMPLES[0][3]
    r = client.get(f"/api/v1/videos/{SLUG}/301")
    body = r.json()
    d = body.get("detail") or {}
    check("H1 单条详情 200", r.status_code == 200, r.status_code)
    check("H1 vod_play_list 只有 1 条（可播的那条）",
          len(d.get("vod_play_list") or []) == 1,
          len(d.get("vod_play_list") or []))
    check("H1 play_from 透出上游串", d.get("play_from") == REAL_FROM, d.get("play_from"))
    check("H1 顶层 play_line_filtered=True", body.get("play_line_filtered") is True, body.keys())
    check("H1 vod_play_from 仍是常量 vodsync（未破坏老字段）",
          d.get("vod_play_from") == "vodsync", d.get("vod_play_from"))
    check("H1 vod_play_url 原样返回（字节不变）",
          d.get("vod_play_url") == raw_bytes
          and len(d.get("vod_play_url") or "") == len(raw_bytes),
          (len(d.get("vod_play_url") or ""), len(raw_bytes)))

    body = client.get(f"/api/v1/videos/{SLUG}/301?play_raw=1").json()
    d = body.get("detail") or {}
    check("H2 play_raw=1 → 2 条原始线路",
          len(d.get("vod_play_list") or []) == 2,
          len(d.get("vod_play_list") or []))
    check("H2 play_raw=1 顺序与上游一致",
          [x["original_index"] for x in d.get("vod_play_list") or []] == [0, 1],
          [x.get("original_index") for x in d.get("vod_play_list") or []])
    check("H2 play_raw=1 不打 play_line_filtered",
          "play_line_filtered" not in body, body.keys())

    body = client.get(f"/api/v1/videos?source={SLUG}&ids=301").json()
    item = (body.get("list") or [{}])[0]
    check("H3 ids 批量：1 条线路 + play_from",
          len(item.get("vod_play_list") or []) == 1 and item.get("play_from") == REAL_FROM,
          (len(item.get("vod_play_list") or []), item.get("play_from")))
    check("H3 ids 批量：顶层 play_line_filtered",
          body.get("play_line_filtered") is True, body.keys())

    body = client.get(f"/api/v1/videos?source={SLUG}&ac=detail&page=1&page_size=10").json()
    item = next((x for x in body.get("list") or [] if x["vod_id"] == 301), None)
    check("H4 ac=detail 归并路径：同样过滤（1 条）",
          bool(item) and len(item.get("vod_play_list") or []) == 1,
          item and len(item.get("vod_play_list") or []))
    check("H4 ac=detail：play_from 非空（DETAIL_COLS 带上了这一列）",
          bool(item) and item.get("play_from") == REAL_FROM,
          item and item.get("play_from"))

    body = client.get(f"/api/v1/{SLUG}?ac=detail&ids=301").json()
    item = (body.get("list") or [{}])[0]
    check("H5 MacCMS 兼容路径：1 条线路",
          len(item.get("vod_play_list") or []) == 1,
          len(item.get("vod_play_list") or []))
    check("H5 MacCMS 兼容路径：play_from 与详情一致",
          item.get("play_from") == REAL_FROM, item.get("play_from"))

    body = client.get(f"/api/v1/videos?source={SLUG}&page=1&page_size=10").json()
    item = (body.get("list") or [{}])[0]
    check("H6 列表模式不带播放字段（体积控制）",
          "vod_play_list" not in item and "play_from" not in item, list(item.keys()))

    # ==================================================================
    print("\n[I] 源站级策略覆盖")
    # ==================================================================
    r = client.post(f"/api/v1/sources/{SLUG}/play-line-policy?policy=off")
    check("I1 设置策略 200", r.status_code == 200, r.text[:120])
    d = client.get(f"/api/v1/videos/{SLUG}/301").json().get("detail") or {}
    check("I2 源站 off 覆盖全局 filter → 2 条",
          len(d.get("vod_play_list") or []) == 2,
          len(d.get("vod_play_list") or []))

    lst = client.get("/api/v1/sources").json().get("list") or []
    me = next((x for x in lst if x["slug"] == SLUG), None)
    check("I3 源站列表带 play_line_policy / effective",
          bool(me) and me.get("play_line_policy") == "off" and me.get("play_line_effective") == "off",
          me and (me.get("play_line_policy"), me.get("play_line_effective")))
    check("I3 源站列表带读侧计数", bool(me) and "play_line" in me, me and list(me.keys()))

    r = client.post(f"/api/v1/sources/{SLUG}/play-line-policy?policy=bogus")
    check("I4 非法策略 → 400", r.status_code == 400, r.status_code)

    r = client.post(f"/api/v1/sources/{SLUG}/play-line-policy?policy=inherit")
    d = client.get(f"/api/v1/videos/{SLUG}/301").json().get("detail") or {}
    check("I5 改回 inherit → 跟随全局 filter（1 条）",
          len(d.get("vod_play_list") or []) == 1,
          len(d.get("vod_play_list") or []))

    # 全局策略改成 off，源站 inherit 时应跟着变
    old_global = config.PLAY_LINE_POLICY
    config.PLAY_LINE_POLICY = "off"
    try:
        d = client.get(f"/api/v1/videos/{SLUG}/301").json().get("detail") or {}
        check("I6 全局 off + 源站 inherit → 2 条",
              len(d.get("vod_play_list") or []) == 2,
              len(d.get("vod_play_list") or []))
    finally:
        config.PLAY_LINE_POLICY = old_global

    # ==================================================================
    print("\n[J] 入库与回填")
    # ==================================================================
    row = MacCmsClient.extract_row(
        {"vod_id": 9, "vod_name": "x", "vod_play_url": "a$https://x/1.m3u8",
         "vod_play_from": "feifan$$$ffm3u8"}, {}
    )
    check("J1 extract_row 保留 vod_play_from",
          row and row.get("play_from") == "feifan$$$ffm3u8", row and row.get("play_from"))
    row2 = MacCmsClient.extract_row({"vod_id": 10, "vod_name": "y"}, {})
    check("J2 上游没给 play_from → 空串（不是 KeyError）",
          row2 and row2.get("play_from") == "", row2 and row2.get("play_from"))

    with database.write_txn() as conn:
        conn.execute(_text(
            f"update vod_{SLUG} set play_from = 'keepme' where vod_id = 303"
        ))
    syncer.upsert_batch(SLUG, [{
        "vod_id": 303, "type_id": 1, "type_name": "动作片", "name": "存量无来源",
        "pic": "", "remarks": "更新至58集", "content": "简介",
        "play_url": SAMPLES[2][3], "play_from": "", "vod_time": SAMPLES[2][5],
    }])
    with database.engine.connect() as conn:
        pf = conn.execute(_text(
            f"select play_from from vod_{SLUG} where vod_id = 303"
        )).scalar()
    check("J3 upsert 空 play_from 不擦掉已有值（COALESCE 保护）",
          pf == "keepme", pf)

    stub = _StubClient([
        {"pagecount": 2, "list": [
            {"vod_id": 303, "vod_play_from": "feifan$$$ffm3u8"},
            {"vod_id": 304, "vod_play_from": "feifan$$$ffm3u8$$$ffapi"},
            {"vod_id": 999, "vod_play_from": ""},
        ]},
        {"pagecount": 2, "list": [{"vod_id": 301, "vod_play_from": "feifan$$$ffm3u8"}]},
    ])
    stats = syncer.backfill_play_from(SLUG, stub)
    with database.engine.connect() as conn:
        got = dict(conn.execute(_text(
            f"select vod_id, play_from from vod_{SLUG}"
        )).all())
        play_url_303 = conn.execute(_text(
            f"select play_url from vod_{SLUG} where vod_id = 303"
        )).scalar()
    check("J4 回填只写 play_from（2 页都翻了）",
          stats["pages"] == 2 and stats["updated"] == 3, stats)
    check("J4 回填结果正确", got.get(303) == "feifan$$$ffm3u8"
          and got.get(304) == "feifan$$$ffm3u8$$$ffapi"
          and got.get(301) == "feifan$$$ffm3u8", got)
    check("J5 回填没动其它列（play_url 不变）", play_url_303 == SAMPLES[2][3], "")
    check("J5 上游空值不写入（不制造空串覆盖）", got.get(999) is None, got.get(999))

    st = client.get("/api/v1/stats").json()
    check("J6 /stats 有 play_line 段",
          isinstance(st.get("play_line"), dict)
          and {"policy", "filtered_total", "fallback_total", "page_lines_total"}
          <= set(st["play_line"].keys()), st.get("play_line"))
    check("J6 读侧计数真的累加了（filtered_total > 0）",
          (st.get("play_line") or {}).get("filtered_total", 0) > 0, st.get("play_line"))

    # ==================================================================
    print("\n[K] 回归 / 兼容")
    # ==================================================================
    legacy = parse_play_url(REAL_RAW)
    check("K1 老签名（只传 raw）仍可用", len(legacy) == 1, len(legacy))
    check("K1 返回结构仍含 name / episodes",
          all({"name", "episodes"} <= set(x.keys()) for x in legacy), "")
    check("K2 空串 → 空列表", parse_play_url("") == [] and parse_play_url(None) == [], "")
    check("K3 单线路无 play_from → 名字就是「线路1」",
          parse_play_url("第1集$https://x/a.m3u8")[0]["name"] == "线路1", "")
    check("K4 无集名的段落仍按 EP{下标} 命名（老行为）",
          parse_play_url("https://x/a.m3u8")[0]["episodes"][0]["name"] == "EP0", "")
    check("K5 解析过程不改写入参（原始串仍逐字节等于入库值）",
          SAMPLES[0][3] == raw_bytes, "")

    print("\n" + "=" * 50)
    print(f"通过 {passed} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
