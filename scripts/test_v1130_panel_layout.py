"""
v1.13.0 管理面板布局调整测试。

两个需求：
 1) 「明星词库」从设置弹层移出来，成为主面板的独立区块
 2) 页面自适应全屏（原来 main 被 max-width:1280px 锁死，宽屏只占中间一条）

这类改动最容易回归的地方：把词库搬回弹层、把 max-width 加回去、搬代码时
漏掉 boot 里的初始化（面板上是个空白区块）。所以既查结构也跑一遍真实渲染。

覆盖项：
 A. 全屏：main 规则不含 max-width；有窄屏 media query；表格容器仍可横向滚动
 B. 词库区块在 <main> 内、位于「数据表浏览」之前，且带标题/输入框/按钮/列表容器
 C. 设置弹层里不再有词库控件（只留一句指路文案）
 D. JS 接线：loadStarTerms 定义在顶层、boot 里调用、4 个按钮都有绑定
 E. 结构健康：无重复 id、JS 里 $("#x") 都能在 HTML 找到、node --check 通过
 F. 功能：用 node 跑真实 loadStarTerms（桩 api），断言行内容/统计角标/空态/错误态

运行：python scripts/test_v1130_panel_layout.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "app", "web", "index.html")
sys.path.insert(0, ROOT)

passed = failed = skipped = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    global passed, failed, skipped
    html = open(HTML, encoding="utf-8").read()
    m = re.search(r"<script>(.*)</script>", html, re.S)
    check("0 面板脚本可提取", bool(m))
    if not m:
        return 1
    js = m.group(1)

    # ---------------- A. 全屏自适应 ----------------
    main_rule = re.search(r"\bmain \{([^}]*)\}", html)
    check("A1 存在 main 规则", bool(main_rule))
    rule = main_rule.group(1) if main_rule else ""
    check("A2 main 不再限宽", "max-width" not in rule, rule.strip())
    check("A3 main 仍保留内边距", "padding" in rule, rule.strip())
    check("A4 内容区没有被别的选择器限宽",
          not re.search(r"#app[^{]*\{[^}]*max-width", html), "")
    check("A5 有窄屏 media query", "@media (max-width: 960px)" in html, "")
    check("A6 窄屏下收窄 main 内边距", "main { padding: 14px 14px 40px; }" in html, "")
    check("A7 表格容器仍可横向滚动（列多时不挤爆）",
          ".panel > .body { padding: 0; overflow-x: auto; }" in html, "")
    check("A8 卡片栅格自适应", "repeat(auto-fit, minmax(190px, 1fr))" in html, "")

    # ---------------- B. 词库区块位置 ----------------
    main_html = html[html.index("<main>"):html.index("</main>")]
    check("B1 starPanel 在主面板内", 'id="starPanel"' in main_html, "")
    check("B2 不在设置弹层里（弹层是 JS 模板，不在 main 内）",
          'id="starPanel"' not in html[html.index("function openSettings()"):], "")
    idx_star = main_html.index('id="starPanel"')
    idx_tb = main_html.index("数据表浏览")
    check("B3 词库排在数据表浏览之前（低频工具靠后）", idx_star < idx_tb, (idx_star, idx_tb))
    check("B4 区块有标题与说明", ">明星词库<" in main_html and "相关推荐增强信号" in main_html, "")
    for el in ('id="starInput"', 'id="btnStarAdd"', 'id="btnStarRecompute"',
               'id="starList"', 'id="starCount"', 'id="btnStarReload"', 'id="btnStarPaste"'):
        check(f"B5 区块含 {el}", el in main_html, "")
    check("B6 说明文案保留强/弱信号解释", "强信号" in main_html and "弱信号" in main_html, "")
    check("B7 批量粘贴格式提示仍在", "JSON 数组" in main_html, "")

    # ---------------- C. 设置弹层已清空 ----------------
    modal = html[html.index("function openSettings()"):html.index('$("#btnSettings").addEventListener')]
    for el in ('id="starInput"', 'id="btnStarAdd"', 'id="starList"', 'id="starCount"'):
        check(f"C 弹层内不再有 {el}", el not in modal, "")
    check("C5 弹层保留一句指路文案", "已移到主面板" in modal, "")
    check("C6 弹层仍有访问控制/Token/密码", all(k in modal for k in ("访问控制", "API Token", "修改管理密码")), "")

    # ---------------- D. JS 接线 ----------------
    check("D1 loadStarTerms 在顶层定义", re.search(r"\nasync function loadStarTerms\(", js) is not None, "")
    # v1.14.1：词库改为折叠区块「首次展开才加载」，boot 里不再直接调 loadStarTerms，
    # 改由 SEC_DEFS.stars.load 挂钩 + boot 的 secLoadOpen() 统一补载
    check("D2 折叠表把词库挂在 stars.load 上",
          re.search(r"stars:\s*\{[^}]*load:\s*\(\)\s*=>\s*loadStarTerms\(\)", js) is not None, "")
    check("D2b boot 调用 secLoadOpen 补载已展开区块",
          re.search(r"boot\(\)[\s\S]{0,600}secLoadOpen\(\);", js) is not None, "")
    check("D3 词库不进 5 秒轮询", "loadStarTerms" not in
          re.search(r"async function refreshAll\(\)[^}]*\}", js).group(0), "")
    for btn in ("btnStarAdd", "btnStarRecompute", "btnStarReload", "btnStarPaste"):
        check(f"D4 {btn} 有绑定", f'$("#{btn}").addEventListener' in js, "")
    check("D5 列表用事件委托（渲染后仍可点）", 'starListEl.addEventListener("click"' in js, "")
    check("D6 删除前有确认", 'confirm(`确认删除词条' in js, "")
    check("D7 starRowHtml 输出别名/备注/按钮",
          all(k in js for k in ("data-star-alias", "data-star-edit", "data-star-del", "data-star-toggle")), "")

    # ---------------- G. 可折叠区块（v1.14.1：首展才加载，缩短首屏） ----------------
    for key, btn, wrap in (("tables", "tablesToggle", "tablesWrap"),
                           ("stars", "starsToggle", "starsWrap"),
                           ("sources", "sourcesToggle", "sourcesWrap"),
                           ("logs", "logsToggle", "logsWrap")):
        check(f"G1 {btn} 在面板头部", f'id="{btn}"' in html, "")
        check(f"G2 {wrap} 默认隐藏", f'id="{wrap}" style="display:none"' in html, "")
    check("G3 SEC_DEFS 覆盖 4 个区块",
          all(k in js for k in ("tables:", "stars:", "sources:", "logs:")) and "SEC_DEFS" in js, "")
    check("G4 展开状态持久化到 localStorage", "localStorage.setItem(SEC_KEY" in js, "")
    check("G5 首展才加载（_loaded 标记防重复）", "_loaded" in js, "")
    check("G6 轮询只刷展开中的区块",
          "if (secOpen.sources) jobs.push(loadSources())" in js
          and "if (secOpen.logs) jobs.push(loadLogs())" in js, "")
    check("G7 收起文案与展开文案齐全",
          '"收起 \u25b4"' in js or "收起 ▴" in js, "")
    check("G8 加载失败允许重试（_loaded 回滚）",
          "def._loaded = false" in js, "")
    check("G9 boot 不再无条件加载 tables/stars",
          "tbLoadTables();   //" not in js and "loadStarTerms();  //" not in js, "")

    # ---------------- E. 结构健康 ----------------
    ids = re.findall(r'\bid="([^"]+)"', html)
    check("E1 无重复 id", len(ids) == len(set(ids)), [i for i in set(ids) if ids.count(i) > 1])
    used = set(re.findall(r'\$\("#([\w-]+)"\)', js))
    check("E2 JS 引用的 id 都存在", not (used - set(ids)), sorted(used - set(ids)))
    if shutil.which("node"):
        p = os.path.join(tempfile.gettempdir(), "v1130_syntax.js")
        open(p, "w", encoding="utf-8").write(js)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True, encoding="utf-8")
        check("E3 node --check 通过", r.returncode == 0, (r.stderr or "")[:600])
    else:
        skipped += 1
        print("  SKIP  E3 node --check（未安装 node）")

    # ---------------- F. 功能：真跑 loadStarTerms ----------------
    if not shutil.which("node"):
        skipped += 1
        print("  SKIP  F 功能渲染（未安装 node）")
        print()
        print("=" * 50)
        print(f"通过 {passed} / 失败 {failed} / 跳过 {skipped}")
        return 1 if failed else 0

    helpers = js[js.index("const esc = (s)"):js.index("const fmtTime =")]
    star = js[js.index("// ---------------- 明星词库"):]
    star = star[:star.index('$("#btnStarReload").addEventListener("click", () => loadStarTerms());')
               + len('$("#btnStarReload").addEventListener("click", () => loadStarTerms());')]

    harness = f"""
{helpers}
const __els = {{}};
const $ = (sel) => (__els[sel] = __els[sel] || {{
  innerHTML: "", textContent: "", value: "", max: "", disabled: false, style: {{}},
  addEventListener() {{}},
}});
let __reply = null, __throw = null;
const api = async () => {{ if (__throw) throw new Error(__throw); return __reply; }};
const toast = () => {{}};
const _jsonPost = async () => ({{}});

{star}

const out = {{}};
(async () => {{
  __reply = {{
    active: 2, infer_mode: "strict", factor: 0.35,
    list: [
      {{ id: 3, term: "最美宝儿", aliases: "宝儿,宝", note: "词库样本", enabled: true }},
      {{ id: 2, term: "困困狗", aliases: "", note: "", enabled: false }},
      {{ id: 1, term: "魔鬼爱", aliases: "", note: "高置信", enabled: true }},
    ],
  }};
  await loadStarTerms();
  out.rows = __els["#starList"].innerHTML;
  out.count = __els["#starCount"].textContent;
  out.hint = __els["#starHint"].textContent;

  __reply = {{ active: 0, infer_mode: "loose", factor: 0.5, list: [] }};
  await loadStarTerms();
  out.empty = __els["#starList"].innerHTML;

  __throw = "boom";
  await loadStarTerms();
  out.err = __els["#starList"].innerHTML;
  out.errCount = __els["#starCount"].textContent;
  console.log(JSON.stringify(out));
}})();
"""
    p = os.path.join(tempfile.gettempdir(), "v1130_star.js")
    open(p, "w", encoding="utf-8").write(harness)
    r = subprocess.run(["node", p], capture_output=True, text=True, encoding="utf-8")
    check("F0 功能脚本跑通", r.returncode == 0, (r.stderr or "")[-800:])
    if r.returncode == 0:
        d = json.loads(r.stdout.strip().splitlines()[-1])
        rows = d["rows"]
        check("F1 三个词条都渲染", all(t in rows for t in ("最美宝儿", "困困狗", "魔鬼爱")), rows[:120])
        check("F2 行数正确", rows.count('class="star-row"') == 3, rows.count('class="star-row"'))
        check("F3 别名展示", "＝ 宝儿,宝" in rows, "")
        check("F4 启用/停用徽标", rows.count(">启用<") == 2 and rows.count(">停用<") == 2 or
              (rows.count(">启用<") >= 2 and ">停用<" in rows), (rows.count(">启用<"), rows.count(">停用<")))
        check("F5 每行四个操作按钮", rows.count("data-star-toggle") == 3 and
              rows.count("data-star-alias") == 3 and rows.count("data-star-edit") == 3 and
              rows.count("data-star-del") == 3, "")
        check("F6 统计角标含总数/启用/生效",
              "词条 3 条" in d["count"] and "启用 2 条" in d["count"] and "生效 2 条" in d["count"], d["count"])
        check("F7 提示含推断模式与系数", "strict" in d["hint"] and "0.35" in d["hint"], d["hint"])
        check("F8 空词库有空态文案", "词库为空" in d["empty"], d["empty"][:80])
        check("F9 接口报错有错误态", "加载失败" in d["err"] and "boom" in d["err"], d["err"][:100])
        check("F10 报错时角标复位", d["errCount"] == "-", d["errCount"])

    # ---------------- H. 面板功能覆盖审计（对照 README/接口总表逐项核对） ----------------
    admin_eps = [
        "/api/v1/admin/tables",                    # 数据表浏览
        "/api/v1/maintenance/recount",             # 条数校准
        "/api/v1/pause",                           # 同步暂停
        "/api/v1/sources/custom",                  # 自定义源站登记
        "/api/v1/sources/probe",                   # 源站连通性测试
        "play-line-policy",                        # 播放线路策略（v1.11）
        "/api/v1/star/tags/status",                # 明星 Tag 覆盖统计（本次补）
        "/api/v1/star/tags/untracked",             # 高频词建议（本次补）
        "/api/v1/maintenance/star-tags",           # 重建明星 Tag（本次补）
        "/api/v1/related/status",                  # 推荐覆盖率（本次补）
        "/api/v1/maintenance/related-recompute",   # 按源站重算推荐（本次补）
        "backfill_play_from",                      # 线路来源回填（本次补）
    ]
    for ep in admin_eps:
        check(f"H 面板已接入 {ep}", ep in js, "面板里找不到该能力的调用")

    for btn in ("btnStarTagsStatus", "btnRelatedStatus", "btnUntracked",
                "btnStarTagsRebuild", "btnStarStatsClose"):
        check(f"H {btn} 有绑定", f'$("#{btn}").addEventListener' in js, "")
    check("H loadStarStats 定义", re.search(r"\nasync function loadStarStats\(", js) is not None, "")
    check("H 高频词一键加入有入口", "btnUntrackedAddTop" in js and "data-untracked=" in js, "")
    check("H 行内回填入口", 'data-act="backfill"' in js, "")
    check("H 回填前有确认", "确认对 ${slug} 回填线路来源" in js, "")

    if shutil.which("node"):
        stats = js[js.index("function starStatsTable("):]
        stats = stats[:stats.index('$("#btnStarTagsStatus").addEventListener')]
        sh = f"""
{helpers}
const __els = {{}};
const $ = (sel) => (__els[sel] = __els[sel] || {{
  innerHTML: "", textContent: "", value: "", style: {{}}, display: "",
  addEventListener() {{}}, querySelectorAll: () => [], dataset: {{}},
}});
let __routes = {{}};
const api = async (url) => {{
  for (k, fn] of Object.entries(__routes)) if (url.includes(k)) return fn(url);
  throw new Error("no route: " + url);
}};
const toast = () => {{}};
const _jsonPost = async () => ({{}});

{stats}

const out = {{}};
(async () => {{
  __routes = {{ "/star/tags/status": () => ({{ code: 1, list: [
      {{ slug: "s361c088099", videos: 72721, no_actor_videos: 52000, star_mapped: 312 }},
  ]}}) }};
  await loadStarStats("tags");
  out.tags = __els["#starStatsBody"].innerHTML;
  out.tagsHint = __els["#starStatsHint"].textContent;

  __routes = {{ "/related/status": () => ({{ code: 1, algo_version: 2, list: [
      {{ slug: "s361c088099", videos: 72721, computed: 65000, coverage: 89.4 }},
      {{ slug: "sd834aeddd0", videos: 112000, computed: 9000, coverage: 8.0 }},
  ]}}) }};
  await loadStarStats("related");
  out.related = __els["#starStatsBody"].innerHTML;

  __routes = {{ "/star/tags/untracked": () => ({{ code: 1, sample_titles: 3000, candidates: [
      {{ term: "某明星A", count: 88 }}, {{ term: "某明星B", count: 41 }},
  ]}}) }};
  await loadStarStats("untracked");
  out.untracked = __els["#starStatsBody"].innerHTML;
  out.untrackedHint = __els["#starStatsHint"].textContent;
  console.log(JSON.stringify(out));
}})();
"""
        # 上面 harness 里 for (k, fn] 是故意的笔误吗？不是 —— 修正为标准语法
        sh = sh.replace("for (k, fn] of Object.entries", "for (const [k, fn] of Object.entries")
        p2 = os.path.join(tempfile.gettempdir(), "v1130_stats.js")
        open(p2, "w", encoding="utf-8").write(sh)
        r = subprocess.run(["node", p2], capture_output=True, text=True, encoding="utf-8")
        check("H0 维护功能渲染跑通", r.returncode == 0, (r.stderr or "")[-700:])
        if r.returncode == 0:
            d = json.loads(r.stdout.strip().splitlines()[-1])
            check("H1 覆盖统计表渲染", "s361c088099" in d["tags"] and "72,721" in d["tags"]
                  and "已映射明星" in d["tags"], d["tags"][:150])
            check("H2 覆盖统计提示", "实时 count" in d["tagsHint"], d["tagsHint"])
            check("H3 覆盖率表 + 按源站重算按钮",
                  "89.4%" in d["related"] and d["related"].count("data-recompute") == 2
                  and "覆盖率" in d["related"], d["related"][:150])
            check("H4 覆盖率颜色分级", 'class="badge ok"' in d["related"] and 'class="badge bad"' in d["related"], "")
            check("H5 高频词 chip 可点", d["untracked"].count("data-untracked") == 2
                  and "某明星A (88)" in d["untracked"] and "把前 20 个加入词库" in d["untracked"], d["untracked"][:150])
            check("H6 高频词提示含抽样数", "3,000" in d["untrackedHint"], d["untrackedHint"])

    # ---------------- G. 版本 ----------------
    from app import __version__

    try:
        ver = tuple(int(x) for x in __version__.split(".")[:3])
    except ValueError:
        ver = (0, 0, 0)
    check("G1 版本号不低于 1.13.0", ver >= (1, 13, 0), __version__)

    # G2~G4 Token 清单吊销入口（v1.14.1 审查补齐：DELETE 接口的 UI 入口）
    check("G2 Token 清单有吊销按钮", 'data-revoke="${it.id}"' in js and "吊销" in js, "")
    check("G3 Token 清单有操作列", "<th>操作</th>" in js, "")
    check("G4 吊销走 DELETE 且有确认",
          '{ method: "DELETE" }' in js and "正在使用它的客户端会 401" in js, "")

    print()
    print("=" * 50)
    print(f"通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
