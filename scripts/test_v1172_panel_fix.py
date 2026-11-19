"""
v1.17.2 回归测试：修复指纹库刷新按钮 id 拼写错误导致的面板整体失效。

根因
----
app/web/index.html 顶层 JS 写 $("#fpReload").onclick = ...，
但 HTML 里按钮 id 是 btnFpReload。$("#fpReload") 返回 null，
对 null 赋 .onclick 抛 TypeError，中断整段 <script>：
  - 之后所有区块的展开事件绑定（SEC_DEFS 循环）没执行 -> 功能块展开不了
  - 末尾 boot() 没执行 -> 统计/列表数据全部不加载
表现为"能登录、面板能显示，但展开不了功能块、加载不了数据"。

本测试把两类保护固化：
  1. 全量静态接线检查：JS 中每个 $("#id") 引用都必须在 HTML 有对应 id 定义
  2. 模拟 DOM 真实执行整段脚本：顶层不得抛错，且 boot() 必须被触发

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1172_panel_fix.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


with open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8") as f:
    html = f.read()

# ---------------- 1. 静态接线检查 ----------------
ids = re.findall(r'\bid="([^"]+)"', html)
check("HTML 无重复 id", len(ids) == len(set(ids)),
      sorted(i for i in set(ids) if ids.count(i) > 1))

script_blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
check("面板只有一个 script 块（顶层抛错会全量中断）", len(script_blocks) == 1, len(script_blocks))
js = script_blocks[0] if script_blocks else ""

used = re.findall(r'\$\("#([\w-]+)"\)', js)
missing = sorted(set(used) - set(ids))
check("JS 引用的每个 #id 都在 HTML 有定义", not missing, missing)

# 针对本 bug 的精确断言（防止有人把按钮 id 与绑定又改岔）
check("指纹库刷新按钮 btnFpReload 有定义", 'id="btnFpReload"' in html)
check("绑定的是 btnFpReload 而非 fpReload",
      '$("#btnFpReload").onclick' in js and '$("#fpReload")' not in js)

# ---------------- 2. 模拟 DOM 执行脚本 ----------------
# 只有 HTML 中真实存在的 id 才会返回元素，其余返回 null，
# 与浏览器行为一致：任何顶层 null 解引用都会被捕获。
node = None
import shutil
if shutil.which("node"):
    import subprocess
    import tempfile

    harness = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
const defined = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
function mkEl() {
  return { style:{}, classList:{add(){},remove(){},toggle(){}},
    addEventListener(){}, removeEventListener(){}, appendChild(){},
    querySelector(){return null}, querySelectorAll(){return []},
    setAttribute(){}, getAttribute(){return null}, focus(){}, click(){},
    value:'', textContent:'', innerHTML:'', checked:false, disabled:false,
    dataset:{}, options:[] };
}
const els = {}; defined.forEach(id => els[id] = mkEl());
const document = {
  querySelector(sel){ return sel.startsWith('#') ? (els[sel.slice(1)] || null) : mkEl(); },
  querySelectorAll(){return []}, getElementById(id){return els[id] || null},
  addEventListener(){}, createElement(){return mkEl()}, hidden:false,
  body:mkEl(), documentElement:mkEl()
};
const localStorage = {getItem(){return null},setItem(){},removeItem(){}};
const location = {replace(){},pathname:'/',search:'',href:'/'};
const window = {open(){},location};
let fetchCalls = 0;
const fetch = async () => { fetchCalls++; return {ok:true,status:200,json:async()=>({}),text:async()=>''}; };
let timerCalls = 0;
const setInterval = () => {timerCalls++; return 1};
try {
  const fn = new Function('document','localStorage','location','window','navigator',
    'fetch','setInterval','clearInterval','setTimeout','console', script);
  fn(document,localStorage,location,window,{},fetch,setInterval,()=>{},()=>{},{log(){},warn(){},error(){}});
  console.log(JSON.stringify({ok:true, fetchCalls, timerCalls}));
} catch (e) {
  console.log(JSON.stringify({ok:false, error:String(e && e.message || e)}));
}
"""
    tmp = os.path.join(tempfile.gettempdir(), "_v1172_panel_harness.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(harness)
    try:
        p = subprocess.run(["node", tmp, os.path.join(ROOT, "app", "web", "index.html")],
                           capture_output=True, text=True, timeout=30)
        import json
        result = json.loads(p.stdout.strip().splitlines()[-1])
        check("脚本顶层执行无抛错（不中断展开绑定与 boot）", result.get("ok") is True,
              result.get("error", ""))
        check("boot() 已触发数据加载", result.get("fetchCalls", 0) >= 1, result)
    except Exception as e:
        check("模拟 DOM 执行脚本可运行", False, repr(e))
else:
    print("SKIP  node 不可用，跳过模拟 DOM 执行")

# ---------------- 汇总 ----------------
print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
