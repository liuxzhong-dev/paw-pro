// v21_debounce_node_test.js —— 对发布版 ui/dashboard.html 里的**真实**
// togglePause 代码做功能级防抖验证（Node + vm，最小 DOM 桩）。
//
// 【为什么补这个】截图自检（_make_shot.py 的 MKSHOT_SELFTEST）在无头
// Edge 里没能把结果横幅渲染出来（看不到 PASS/FAIL），而静态断言只能
// 证明「串在不在」，证明不了「行为对」。这里直接把 HTML 里的函数体
// 抠出来，在全新 vm 上下文里跑：双击只算一次、越过 600ms 后双击又算
// 一次；再把防抖守卫打掉（负向对照），双击必须算两次 —— 旧 bug 复现
// 才说明断言有效（硬规则 42）。
//
// 做法：从 ui/dashboard.html 抽出 `$` / apiFail / togglePause 与防抖
// 变量声明（花括号配平，不整文件 eval —— 整文件里有 pywebview 专用代码），
// 每个变体一个独立 vm 上下文，互不污染。

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(
  path.join(__dirname, "ui", "dashboard.html"), "utf-8");

// ---- 按花括号配平从源码里抠函数 ----
function extractFn(src, signature) {
  const i = src.indexOf(signature);
  if (i < 0) throw new Error("找不到函数：" + signature);
  let depth = 0, started = false, j = i;
  for (; j < src.length; j++) {
    const c = src[j];
    if (c === "{") { depth++; started = true; }
    else if (c === "}") { depth--; if (started && depth === 0) { j++; break; } }
  }
  return src.slice(i, j);
}

// 防抖变量声明必须来自发布版 HTML 本身（不能在此伪造，否则测的是空气）
const declLine = html.split("\n").find(l => /let _pauseBtnTs\s*=\s*0/.test(l));
if (!declLine) {
  console.error("[FAIL] 发布版 HTML 里找不到 let _pauseBtnTs = 0 声明");
  process.exit(1);
}

const fnSrc = [
  "const $ = id => document.getElementById(id);",
  declLine.trim(),
  extractFn(html, "function apiFail("),
  extractFn(html, "async function togglePause("),
].join("\n\n");

if (!/_pauseBtnTs/.test(fnSrc)) {
  console.error("[FAIL] 抠出来的 togglePause 里没有防抖变量 _pauseBtnTs");
  process.exit(1);
}

// ---- 每个变量一个全新 vm 上下文（正/负向对照互不污染）----
function makeRun(src) {
  const box = { calls: 0, paused: false, label: "暂停" };
  let nowMs = 1000000;
  const pauseBtn = {
    click() { vmCtx.__toggle(); },        // 与 onclick="togglePause()" 等价
    get textContent() { return box.label; },
    set textContent(v) { box.label = v; },
    classList: { toggle() {} },
  };
  const hkNote = { innerHTML: "", className: "", style: {} };
  const sandbox = {
    document: { getElementById: (id) => (id === "pauseBtn" ? pauseBtn : hkNote) },
    window: {
      pywebview: {
        api: {
          toggle_pause: async () => {
            box.calls += 1;
            box.paused = !box.paused;
            return { ok: true, paused: box.paused,
                     msg: box.paused ? "已暂停" : "已恢复运行" };
          },
        },
      },
    },
    Date: { now: () => nowMs },
    console: { error() {} },              // apiFail 的 console.error 吞掉
    setTimeout: (fn) => fn(),             // 同步化，测试不等真实时间
    advance: (ms) => { nowMs += ms; },
  };
  const vmCtx = vm.createContext(sandbox);
  vm.runInContext(src + "\n;globalThis.__toggle = togglePause;", vmCtx);
  return {
    click() { pauseBtn.click(); },
    advance: sandbox.advance,
    get calls() { return box.calls; },
    get label() { return box.label.trim(); },
  };
}

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail });
  console.log((cond ? "  [PASS] " : "  [FAIL] ") + name +
              (cond ? "" : "   " + detail));
}

(async () => {
  console.log("v21 防抖功能验证（Node vm · 真实发布版代码）");

  // ---- 正向：真实防抖 ----
  const r = makeRun(fnSrc);
  r.click(); r.advance(120); r.click();          // 第 1 组双击（<600ms）
  await new Promise(res => setTimeout(res, 20));
  const n1 = r.calls, label1 = r.label;
  r.advance(800);                                 // 越过 600ms 防抖窗口
  r.click(); r.advance(120); r.click();          // 第 2 组双击
  await new Promise(res => setTimeout(res, 20));
  const n2 = r.calls, label2 = r.label;

  check("第 1 组双击只产生 1 次调用（防抖生效）", n1 === 1, "calls=" + n1);
  check("第 1 组后文案 = 恢复运行", label1 === "恢复运行", "label=" + label1);
  check("越过 600ms 后第 2 组双击又计 1 次（累计 2）", n2 === 2,
        "calls=" + n2);
  check("第 2 组后文案 = 暂停（切换语义保持）", label2 === "暂停",
        "label=" + label2);

  // ---- 负向对照：打掉防抖守卫，旧 bug 必须复现 ----
  const noGuard = fnSrc.replace(
    "if (nowTs && nowTs - _pauseBtnTs < 600) return;", "if (false) return;");
  if (noGuard === fnSrc) {
    check("负向对照：找到并打掉了防抖守卫", false, "替换未命中");
  } else {
    const r2 = makeRun(noGuard);
    r2.click(); r2.advance(120); r2.click();      // 同样一组双击
    await new Promise(res => setTimeout(res, 20));
    check("负向对照：无防抖时双击算 2 次（旧 bug 复现，断言有效）",
          r2.calls === 2, "calls=" + r2.calls);
  }

  const bad = results.filter(x => !x.ok);
  console.log("汇总：%d 项通过，%d 项失败", results.length - bad.length,
              bad.length);
  process.exit(bad.length ? 1 : 0);
})();
