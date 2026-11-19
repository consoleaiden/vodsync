"""
v1.17.4 回归测试：面板页脚显示镜像版本 + GitHub 项目地址。

新增
----
- 面板底部 <footer id="appFooter">：静态渲染「镜像版本 + GitHub 链接」，
  JS 即使失败 GitHub 链接仍在（吸取 v1.17.2 顶层抛错教训）。
- loadFooterVersion() 从公开 /healthz 读 version 字段渲染「vX.Y.Z」，
  失败时降级为 '-' 且不抛错、不阻塞 boot。

测试
----
1. footer HTML 结构：id、版本占位、GitHub 链接（含 target=_blank + noopener）
2. 版本脚本：函数存在、读 /healthz、有 try/catch、boot 中非阻塞调用
3. 模拟 DOM + mock fetch 真实执行 loadFooterVersion：
   - healthz 正常返回 version -> 渲染 vX.Y.Z
   - healthz 抛错 -> 降级 '-' 且不抛异常

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1174_footer.py
"""
import os
import re
import sys
import shutil
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


with open(os.path.join(ROOT, "app", "web", "index.html"), encoding="utf-8") as f:
    html = f.read()
script = re.findall(r"<script>([\s\S]*?)</script>", html)[0]

# ---------------- 1. footer HTML 结构 ----------------
check("footer#appFooter 存在", '<footer id="appFooter">' in html)
check("版本占位元素 #footerVersion 存在", 'id="footerVersion"' in html)
check("GitHub 项目地址链接存在",
      'href="https://github.com/consoleaiden/vodsync"' in html)
check("外链带 target=_blank", 'target="_blank"' in html)
check("外链带 rel=noopener（安全）", 'rel="noopener noreferrer"' in html)
# footer 位于 </main> 之后 —— JS 失败也能看到静态链接
check("footer 在 </main> 之后（静态可见）",
      html.index("</main>") < html.index("<footer") < html.index("<script"))

# ---------------- 2. 版本脚本结构 ----------------
check("loadFooterVersion 函数存在", "async function loadFooterVersion" in script)
check("从 /healthz 读版本", 'fetch("/healthz"' in script)
check("有 try/catch 兜底（失败不抛错）",
      "loadFooterVersion" in script and "catch (e)" in script)
check("boot 中调用（非阻塞，无 await）",
      re.search(r"\n\s*loadFooterVersion\(\);", script) is not None)
check("boot 调用在鉴权跳转之后",
      script.index("location.replace") < script.index("loadFooterVersion();"))

# ---------------- 3. 模拟 DOM 真实执行 loadFooterVersion ----------------
if shutil.which("node"):
    harness = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
// 抽出 loadFooterVersion 函数体单独执行
const m = script.match(/async function loadFooterVersion\(\) \{[\s\S]*?\n\}/);
if (!m) { console.log(JSON.stringify({error: 'function not found'})); process.exit(0); }

function makeEl() { return { textContent:'', title:'' }; }
const el = makeEl();
const document = { querySelector(sel){ return sel === '#footerVersion' ? el : null; } };

async function run(mode, version) {
  el.textContent = ''; el.title = '';
  const fetch = async (path, opts) => {
    if (mode === 'throw') throw new Error('network down');
    if (mode === 'notok') return { ok:false, status:500 };
    return { ok:true, json: async () => ({ version }) };
  };
  let threw = false;
  const $ = (sel) => document.querySelector(sel);
  try {
    const fn = new Function('document','fetch','$', m[0] + '\nreturn loadFooterVersion();');
    await fn(document, fetch, $);
  } catch(e) { threw = true; }
  return { text: el.textContent, title: el.title, threw };
}

(async () => {
  const ok = await run('ok', '1.17.4');
  const bad = await run('throw');
  const notok = await run('notok');
  console.log(JSON.stringify({ ok, bad, notok }));
})();
"""
    tmp = os.path.join(tempfile.gettempdir(), "_v1174_footer_harness.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(harness)
    try:
        p = subprocess.run(["node", tmp, os.path.join(ROOT, "app", "web", "index.html")],
                           capture_output=True, text=True, timeout=30)
        import json
        result = json.loads(p.stdout.strip().splitlines()[-1])
        ok = result.get("ok", {})
        bad = result.get("bad", {})
        notok = result.get("notok", {})
        check("healthz 正常 -> 渲染 v1.17.4", ok.get("text") == "v1.17.4", ok)
        check("版本 title 含版本号", "1.17.4" in ok.get("title", ""), ok.get("title"))
        check("渲染成功不抛错", ok.get("threw") is False)
        check("fetch 抛错 -> 降级 '-' 且不抛异常",
              bad.get("text") == "-" and bad.get("threw") is False, bad)
        check("healthz 非 200 -> 降级 '-' 且不抛异常",
              notok.get("text") == "-" and notok.get("threw") is False, notok)
    except Exception as e:
        check("模拟 DOM 执行版本函数可运行", False, repr(e))
else:
    print("SKIP  node 不可用")

print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
