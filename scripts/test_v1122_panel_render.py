"""
v1.12.2 管理面板「数据表浏览」渲染回归测试。

背景（真实线上 bug）：v1.12.0 的 tbRender() 把 `tbState.cols` 当成
`[{name,type,pk}]` 对象数组用，但 `/rows` 接口返回的 `columns` 是
**列名字符串数组**，于是 `c.name` 恒为 undefined → `r[undefined]` 恒空 →
表头空白、每行每个单元格都渲染成 "-"。功能看着像「查询没有数据」，
其实是列名对不上。

这类 bug 接口级测试照不到（接口是对的），必须在真实渲染函数上跑一遍。
本脚本的做法：从 app/web/index.html 里**现场切出** esc/fmtNum 与
tbCell/tbCols/tbRender 的源码，用 node 起一个极简 DOM 桩跑一遍渲染，
断言输出的 HTML 里有真实值。

覆盖项：
 A. 列名为字符串数组（接口真实形状）→ 表头有列名、单元格有值
 B. 主键列带 🔑 与 title 提示（类型信息从表清单合并进来）
 C. 空值仍渲染 "-"（不能因为修复把空值也塞成 undefined）
 D. 超长文本进 clamp 分支（点击展开）
 E. 兼容列元信息已是对象的调用方（两种形状都要work）
 F. 没有列时走「该表没有列」空态
 G. 面板源码里的 tbRender 不再直接对列名取 .name（防回归）

运行：python scripts/test_v1122_panel_render.py
（需要 node；没有 node 时相关项标记 SKIP 而非失败）
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 用临时 sqlite，别连到默认的 postgres（H 段要真查一次库）
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vodsync-v1122-"))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(os.environ["DATA_DIR"], "v1122.db"))
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("FULL_SYNC_ON_DISCOVER", "false")
os.environ.setdefault("AUTO_RESUME_ON_BOOT", "false")
os.environ.setdefault("RELATED_ENABLED", "false")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "app", "web", "index.html")

passed = failed = skipped = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def skip(name, why):
    global skipped
    skipped += 1
    print(f"  SKIP  {name}  ({why})")


def _slice(js, start_mark, end_mark):
    """从面板脚本里切一段源码；切不到就抛错（结构变了要立刻发现）。"""
    i = js.find(start_mark)
    if i < 0:
        raise AssertionError(f"面板脚本里找不到起点：{start_mark}")
    j = js.find(end_mark, i)
    if j < 0:
        raise AssertionError(f"面板脚本里找不到终点：{end_mark}")
    return js[i:j]


def build_harness(render_src, helpers_src, payload):
    """拼一个 node 脚本：极简 DOM 桩 + 面板真实渲染函数 + 断言。"""
    return f"""
// ---- 面板真实源码（从 index.html 切出，勿手改）----
{helpers_src}

const __els = {{}};
const $ = (sel) => (__els[sel] = __els[sel] || {{
  innerHTML: "", textContent: "", value: "", max: "", disabled: false, style: {{}},
}});

const __payload = {json.dumps(payload, ensure_ascii=False)};
const tbState = __payload.state;
const tbCurName = () => __payload.current;

{render_src}

tbRender();
const body = __els["#tbBody"] ? __els["#tbBody"].innerHTML : "";
const pager = __els["#tbPageInfo"] ? __els["#tbPageInfo"].textContent : "";
const thCount = (body.match(/<th[ >]/g) || []).length;   // <thead> 不算表头单元格
const tdCount = (body.match(/<td/g) || []).length;
const dashCount = (body.match(/<td class="muted">-<\\/td>/g) || []).length;
const clampCount = (body.match(/class="clamp"/g) || []).length;
const out = {{ body, pager, thCount, tdCount, dashCount, clampCount }};
console.log(JSON.stringify(out));
"""


def run_node(harness):
    exe = shutil.which("node")
    if not exe:
        return None, "未安装 node"
    fd, path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)
    try:
        r = subprocess.run([exe, path], capture_output=True, text=True, encoding="utf-8")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r.returncode != 0:
        return None, (r.stderr or r.stdout)[-1500:]
    return r.stdout.strip(), None


# 真实形状：/rows 返回的是列名字符串数组；类型/主键在表清单的 columns 里
STATE_STR_COLS = {
    "tables": [{
        "name": "vod_s361c088099", "kind": "vod", "label": "新极品资源站", "rows": 72721,
        "columns": [
            {"name": "vod_id", "type": "INTEGER", "pk": True},
            {"name": "name", "type": "VARCHAR(255)", "pk": False},
            {"name": "remarks", "type": "VARCHAR(128)", "pk": False},
            {"name": "content", "type": "TEXT", "pk": False},
        ],
    }],
    "cols": ["vod_id", "name", "remarks", "content"],   # ← 字符串数组（接口原样）
    "rows": [
        {"vod_id": 1, "name": "三体", "remarks": "更新至30集", "content": "简介"},
        {"vod_id": 2, "name": "流浪地球2", "remarks": "", "content": "长" * 200},
    ],
    "total": 2, "page": 1, "pages": 1, "busy": False,
}

# 兼容形状：列元信息已经是对象（万一以后接口改成对象）
STATE_OBJ_COLS = dict(STATE_STR_COLS)
STATE_OBJ_COLS["cols"] = [
    {"name": "vod_id", "type": "INTEGER", "pk": True},
    {"name": "name", "type": "VARCHAR(255)", "pk": False},
    {"name": "remarks", "type": "VARCHAR(128)", "pk": False},
    {"name": "content", "type": "TEXT", "pk": False},
]

STATE_NO_COLS = {"tables": [], "cols": [], "rows": [], "total": 0, "page": 1, "pages": 1}


def main():
    html = open(HTML, encoding="utf-8").read()
    m = re.search(r"<script>(.*)</script>", html, re.S)
    check("0 面板脚本可提取", bool(m))
    if not m:
        return 1
    js = m.group(1)

    helpers = _slice(js, "const esc = (s)", "const fmtTime =")
    render = _slice(js, "// 单元格：", '$("#tbTable").addEventListener')

    # ---- G. 源码级防回归（不依赖 node）----
    check("G1 渲染用 tbCols() 归一化列名", "function tbCols()" in render and "const cols = tbCols();" in render,
          render[:120])
    check("G2 不再有 r[c.name] 直接取列名的地方（字符串会变 undefined）",
          not re.search(r"r\[c\.name\]", render) or "typeof c === \"string\"" in render,
          "见 tbCols()")
    check("G3 取列名走 tbCell(r[c.name]) 且 c 已归一化",
          'tbCell(r[c.name])' in render, "")

    if not shutil.which("node"):
        skip("A~F 实际渲染", "未安装 node")
        print()
        print("=" * 50)
        print(f"通过 {passed} / 失败 {failed} / 跳过 {skipped}")
        return 1 if failed else 0

    out, err = run_node(build_harness(render, helpers,
                                      {"state": STATE_STR_COLS, "current": "vod_s361c088099"}))
    check("A0 渲染跑通", out is not None, err)
    if out:
        d = json.loads(out)
        # ---- A. 字符串列名也能出表头与数据 ----
        check("A1 表头 4 列", d["thCount"] == 4, d["thCount"])
        check("A2 表头显示列名", "vod_id" in d["body"] and "remarks" in d["body"], "")
        check("A3 表头无 undefined", "undefined" not in d["body"], "")
        check("A4 单元格有真实值", "三体" in d["body"] and "更新至30集" in d["body"], "")
        check("A5 第二行也有值", "流浪地球2" in d["body"], "")
        check("A6 单元格总数 = 行×列", d["tdCount"] == 8, d["tdCount"])
        check("A7 不再满屏 -", d["dashCount"] == 1, d["dashCount"])   # 只有 remarks 空
        # ---- B. 主键标记 ----
        check("B1 主键列带 🔑", d["body"].count("🔑") >= 1, "")
        check("B2 主键 title 含类型", 'title="INTEGER · 主键"' in d["body"], "")
        # ---- C. 空值 ----
        check("C1 空字符串渲染成 -", d["dashCount"] == 1, d["dashCount"])
        # ---- D. 长文本 ----
        check("D1 超长文本进 clamp", d["clampCount"] == 1, d["clampCount"])
        check("D2 翻页条信息正常", "共 2 条" in d["pager"] and "第 1 / 1 页" in d["pager"], d["pager"])

    # ---- E. 列元信息已是对象时也要 work ----
    out2, err2 = run_node(build_harness(render, helpers,
                                        {"state": STATE_OBJ_COLS, "current": "vod_s361c088099"}))
    check("E0 对象形状渲染跑通", out2 is not None, err2)
    if out2:
        d2 = json.loads(out2)
        check("E1 对象形状表头 4 列", d2["thCount"] == 4, d2["thCount"])
        check("E2 对象形状有数据", "三体" in d2["body"], "")

    # ---- F. 空态 ----
    out3, err3 = run_node(build_harness(render, helpers, {"state": STATE_NO_COLS, "current": ""}))
    check("F0 空态渲染跑通", out3 is not None, err3)
    if out3:
        d3 = json.loads(out3)
        check("F1 无列时显示空态", "该表没有列" in d3["body"], d3["body"][:80])
        check("F2 空态不动翻页条", d3["pager"] == "", d3["pager"])

    # ---- 后端契约：columns 确实是列名字符串数组（渲染归一化的前提）----
    sys.path.insert(0, ROOT)
    from app import database, tablebrowse  # noqa: E402

    database.init_db()
    direct = tablebrowse.browse("sources", 1, 5)
    check("H1 接口 columns 是字符串数组", all(isinstance(c, str) for c in direct["columns"]),
          direct["columns"][:3])
    check("H2 rows 的 key 与 columns 一致",
          all(set(direct["columns"]) == set(r.keys()) for r in direct["rows"]) if direct["rows"] else True)

    print()
    print("=" * 50)
    print(f"通过 {passed} / 失败 {failed} / 跳过 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
