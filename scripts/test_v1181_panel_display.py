"""
v1.18.1 回归测试：管理面板两处展示修复。

问题复现（用户截图）
------------------
1. 指纹库状态列把 `title="未下发原因：未确认"` 当**普通文本**渲染在单元格里：
   fpStatusBadge() 把 title 属性字符串拼在 `<span class="badge">…</span>` 之后，
   innerHTML 自然把它显示出来。且 candidate 笼统报「未确认」没有可操作信息。
2. 「最近操作 / 事件」详情列直接 `JSON.stringify(detail).slice(0, 90)`，
   运营看到的是半截 JSON 代码，不是人话。

修复
----
1. title 写进 <span> 内部；candidate 改为按 blocked_by + 阈值显示具体差额。
2. 详情列新增 fmtAuditDetail() 中文化摘要，原始 JSON 移到 title 悬浮提示；
   操作者数字身份映射为可读名称。

运行：PYTHONPATH=. .venv/Scripts/python.exe scripts/test_v1181_panel_display.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
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

# ---------------- 1. 状态列静态检查 ----------------
check("旧的报错拼接已移除（badge + lock + why）",
      "return badge + lock + (why || '')" not in script)
check("旧的字面量「未下发原因：未确认」不再出现",
      "未下发原因：未确认" not in script)
check("fpStatusBadge 的 title 写在 span 内部",
      '<span class="badge ${cls}"${tip ? ` title="${esc(tip)}"` : ""}>' in script)
check("candidate 使用具体门槛差额 shortfall",
      "function fpStatusBadge" in script and "shortfall" in script
      and "未达标：" in script)
check("短差额计算覆盖 用户/地址/命中",
      all(k in script for k in ("bb.users", "bb.urls", "bb.hits")))
check("阈值从 overview 注入（min_users/min_urls/min_hits）",
      all(k in script for k in ("window._fpMinUsers", "window._fpMinUrls", "window._fpMinHits")))
check("下发列也带未下发原因的 title", 'title="${esc(why)}"' in script)

# ---------------- 2. 审计详情静态检查 ----------------
check("审计详情不再直接输出 JSON.stringify 片段",
      'JSON.stringify(r.detail || {}).slice(0, 90)' not in script)
check("fmtAuditDetail/fmtAuditActor 已定义",
      "function fmtAuditDetail" in script and "function fmtAuditActor" in script)
check("审计表格使用格式化函数",
      "${fmtAuditDetail(r.detail)}" in script and "${fmtAuditActor(r.actor)}" in script)
check("原始 JSON 仍可通过 title 悬浮查看",
      'title="${esc(JSON.stringify(r.detail || {}).slice(0, 600))}"' in script)
label_block = re.search(r"const FP_AUDIT_LABELS = \{[\s\S]*?\n\};", script)
check("审计标签表存在且无重复 created 键",
      label_block is not None and label_block.group(0).count("created:") == 1)

# ---------------- 3. 模拟 DOM 真实执行渲染函数 ----------------
if shutil.which("node"):
    harness = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
function grab(re) { const m = script.match(re); return m ? m[0] : null; }
globalThis.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
const parts = [
  grab(/const fmtNum = [^\n]+/),
  grab(/function fpStatusBadge\([\s\S]*?\n\}/),
  grab(/const FP_AUDIT_LABELS = \{[\s\S]*?\n\};/),
  grab(/function fmtAuditDetail\([\s\S]*?\n\}/),
  grab(/function fmtAuditActor\([\s\S]*?\n\}/),
];
if (parts.some(x => !x)) {
  console.log(JSON.stringify({error: 'extract failed', got: parts.map(Boolean)}));
  process.exit(0);
}
const api = new Function(parts.join('\n') + '\nreturn {fpStatusBadge, fmtAuditDetail, fmtAuditActor};')();
globalThis.window = {};
window._fpMinUsers = 1; window._fpMinUrls = 3; window._fpMinHits = 10;
console.log(JSON.stringify({
  candidate: api.fpStatusBadge('candidate', false, false, 'not_confirmed', '地址 2/3'),
  confirmed: api.fpStatusBadge('confirmed', true, true, '', ''),
  blocked: api.fpStatusBadge('confirmed', false, false, 'miss_threshold', ''),
  locked: api.fpStatusBadge('candidate', true, false, 'not_confirmed', '地址 2/3'),
  detail1: api.fmtAuditDetail({fp_hash: 'f2e16bad411bed239166aae69e877fee6a36a688b8947c127213318047707',
                               deleted_fingerprints: 1, deleted_segments: 1}),
  detail2: api.fmtAuditDetail({scanned: 10, created: 0, confirmed: 0, demoted: 0,
                               disabled: 0, kept: 10, locked_kept: 0, duration_ms: 1234}),
  detail3: api.fmtAuditDetail({created: true, host: 'e2e-verify.example.com',
                               pattern: '{"host":"e2e-verify.example.com"}'}),
  actor0: api.fmtAuditActor(0),
  actorPanel: api.fmtAuditActor(-2),
  actorToken: api.fmtAuditActor(7),
}));
"""
    tmp = os.path.join(tempfile.gettempdir(), "_v1181_panel_harness.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(harness)
    try:
        p = subprocess.run(["node", tmp, os.path.join(ROOT, "app", "web", "index.html")],
                           capture_output=True, text=True, timeout=30)
        result = json.loads(p.stdout.strip().splitlines()[-1])
        check("函数抽取并执行成功", "error" not in result, result.get("error", ""))

        cand = result.get("candidate", "")
        check("candidate 显示具体差额，不再是「未确认」",
              "未达标：地址 2/3" in cand and "未确认" not in cand, cand)
        check("candidate title 在 span 内部，不在 </span> 之后",
              "</span> title=" not in cand and '<span class="badge mute" title=' in cand, cand)
        check("confirmed 锁定且可下发时显示 下发中",
              '<span class="badge ok">下发中</span>' in result.get("confirmed", ""),
              result.get("confirmed"))
        check("误杀达阈值有明确提示",
              "误杀达阈值，未下发" in result.get("blocked", ""), result.get("blocked"))
        check("锁定标记仍保留", "🔒" in result.get("locked", ""))

        d1 = result.get("detail1", "")
        check("删除审计详情中文化且 fp_hash 截断",
              "指纹=f2e16bad41…" in d1 and "删聚合=1" in d1 and "{" not in d1, d1)
        d2 = result.get("detail2", "")
        check("rebuild 审计详情中文化且毫秒单位正确",
              "扫描=10" in d2 and "耗时=1234ms" in d2 and "{" not in d2, d2)
        d3 = result.get("detail3", "")
        check("manual 审计详情中文化（含内嵌 pattern）",
              "新建=是" in d3 and "站点=e2e-verify.example.com" in d3
              and "模式=host=e2e-verify.example.com" in d3 and "{" not in d3, d3)
        check("操作者身份可读化",
              result.get("actor0") == "主 Token"
              and result.get("actorPanel") == "管理面板"
              and result.get("actorToken") == "Token #7",
              [result.get("actor0"), result.get("actorPanel"), result.get("actorToken")])
    except Exception as e:
        check("模拟 DOM 执行渲染函数可运行", False, repr(e))
else:
    print("SKIP  node 不可用")

print("\n==== SUMMARY ====")
if fails:
    print(f"FAILED {len(fails)}: " + ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
