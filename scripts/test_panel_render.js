/**
 * 面板渲染校验：在 Node 里用 DOM 桩执行 app/web/index.html 的内联 JS，
 * 对着真实运行中的服务渲染源站列表，断言「暂停」按钮只在 running=true 的行出现。
 *
 * 用法： node scripts/test_panel_render.js [baseUrl]
 * 默认 baseUrl=http://127.0.0.1:8000（需服务已启动）
 */
const fs = require("fs");
const path = require("path");

const BASE = process.argv[2] || "http://127.0.0.1:8000";
const HTML_PATH = path.join(__dirname, "..", "app", "web", "index.html");
const html = fs.readFileSync(HTML_PATH, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// ---- 极简 DOM 桩 ----
const store = {};
function el(id) {
  if (!store[id]) {
    store[id] = {
      id, innerHTML: "", textContent: "", value: "", checked: true, className: "",
      style: {}, dataset: {}, children: [],
      classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
      addEventListener() {}, appendChild() {}, remove() {}, setAttribute() {},
      hasAttribute: () => false, querySelector: () => el(id + ">q"),
      closest: () => null,
    };
  }
  return store[id];
}
global.document = {
  hidden: false, body: el("body"),
  addEventListener() {}, createElement: () => el("tmp" + Math.random()),
  querySelector: (sel) => el(sel), querySelectorAll: () => [],
};
global.window = { confirm: () => false, prompt: () => "", location: {}, addEventListener() {} };
global.localStorage = { getItem: () => "", setItem: () => {} };
const realFetch = global.fetch;
global.fetch = (url, opts) => realFetch(BASE + url, opts);
global.setInterval = () => 0;      // 禁用自动刷新，避免进程不退

// ---- 执行面板脚本 ----
new Function(script)();
// refreshAll() 是异步的，等它落地
setTimeout(() => {}, 0);

async function main() {
  await new Promise((r) => setTimeout(r, 1200));
  const tbody = store["#sourcesBody"];
  if (!tbody || !tbody.innerHTML) throw new Error("源站表格未渲染");
  const rows = tbody.innerHTML.split("<tr>").slice(1);      // 第一段是 <table><thead>
  const bodyRows = rows.slice(1);                            // 跳过表头

  const res = await realFetch(BASE + "/api/v1/sources");
  const list = (await res.json()).list;
  const running = list.filter((s) => s.running);

  const pauseRows = bodyRows.filter((r) => r.includes('data-act="pause"'));
  const resumeRows = bodyRows.filter((r) => r.includes('data-act="resume"'));
  const modeTags = bodyRows.filter((r) => r.includes("正在增量同步") || r.includes("badge run"));

  console.log(`服务: ${BASE}`);
  console.log(`源站数: ${list.length}  运行中: ${running.length}  有断点未跑: ${list.filter((s) => s.syncing && !s.running).length}`);
  console.log(`渲染行数: ${bodyRows.length}  含「暂停」按钮: ${pauseRows.length}  含「继续」按钮: ${resumeRows.length}  含运行中徽标: ${modeTags.length}`);

  const errs = [];
  if (pauseRows.length !== running.length) {
    errs.push(`暂停按钮数(${pauseRows.length}) != 运行中源站数(${running.length})`);
  }
  // 逐行交叉校验：非运行中的行不得出现暂停按钮
  bodyRows.forEach((row, i) => {
    const s = list[i];
    const hasPause = row.includes('data-act="pause"');
    const shouldPause = !!s.running && !s.paused;
    if (hasPause !== shouldPause) {
      errs.push(`${s.slug}: running=${s.running} paused=${s.paused} 但 pause按钮=${hasPause}`);
    }
  });
  // 未运行且有断点、且未被暂停的行应显示「未在同步」（暂停的行显示"已暂停于第 N 页"）
  const idleBreak = list.findIndex((s) => s.syncing && !s.running && !s.paused);
  if (idleBreak >= 0 && !bodyRows[idleBreak].includes("未在同步")) {
    errs.push(`有断点但未运行的站(${list[idleBreak].slug})未标注「未在同步」`);
  }
  // 已暂停的站：应显示「继续」而不是「暂停」
  list.forEach((s, i) => {
    if (s.paused && !bodyRows[i].includes('data-act="resume"')) {
      errs.push(`${s.slug} 已暂停但未渲染「继续」按钮`);
    }
  });
  // 从未同步的站（synced=false 且不在跑）：不得显示「已完成」，应显示「从未同步」且进度 0%
  list.forEach((s, i) => {
    if (!s.synced && !s.running) {
      const row = bodyRows[i];
      if (row.includes("已完成")) errs.push(`${s.slug} 未同步却渲染了「已完成」`);
      if (!row.includes("从未同步")) errs.push(`${s.slug} 未同步但未标注「从未同步」`);
      if (!row.includes("0%")) errs.push(`${s.slug} 未同步但进度不是 0%`);
      if (s.progress !== 0) errs.push(`${s.slug} 接口 progress=${s.progress}，应为 0`);
    }
  });
  // 已同步完成的站：应显示「已完成」或其断点进度，不得显示「从未同步」
  list.forEach((s, i) => {
    if (s.synced && !s.running && !s.syncing && !s.paused && !bodyRows[i].includes("已完成")) {
      errs.push(`${s.slug} 已同步完成但未渲染「已完成」`);
    }
  });

  if (errs.length) {
    console.log("\nFAIL:");
    errs.slice(0, 6).forEach((e) => console.log("  - " + e));
    process.exit(1);
  }
  console.log("\nPANEL_RENDER_OK: 暂停按钮仅出现在运行中的源站行");
}
main().catch((e) => { console.error("ERROR", e); process.exit(1); });
