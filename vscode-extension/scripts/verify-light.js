/**
 * 验收浅色主题（用户问：「到时候我 vscode 换成白色主题也行是吧，就是代码底色」）。
 *
 * 做法：把 VSCode Light Modern 的真实变量值注入成 :root，再喂真实 narnat.json
 * 的配色快照，断言三件事：
 *   1. 代码块底色相对页面底**方向正确**（浅底上略暗、深底上略亮），且幅度不大
 *   2. 正文级文字（正文/标题/行内代码/表格正文/代码块文字）对**实际所在底色**
 *      的对比度 ≥ 4.5
 *   3. dim 修饰（「引用」是 italic dim）不会把颜色弱化到读不清
 *
 * 本轮就是这条链路实测出来的三个缺陷（改前 → 改后）：
 *   行内代码 3.28 → 4.50（底不该复用代码块那一档）
 *   引用块   2.31 → 3.09（dim 之后要再压一次对比度下限）
 *   代码块底 #d3d7dd → #ebedf0（adaptSurface 亮侧下限 0.82 → 0.93）
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const REPO = path.resolve(ROOT, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const py = 'import json,sys;sys.path.insert(0,".");'
  + 'from narnat_agent.config.loader import normalize_ui_raw;'
  + 'from narnat_agent.ui.theme import snapshot;'
  + 'cfg=json.load(open(".narnat/config/narnat.json",encoding="utf-8-sig"));'
  + 'print(json.dumps(snapshot(normalize_ui_raw(cfg.get("界面") or {})),ensure_ascii=False))';
const THEME = execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim();

const MD = [
  '# 一级标题',
  '',
  '正文带 **粗体**、`行内代码` 和 [链接](https://x.y)。',
  '',
  '> 引用块也要读得清。',
  '',
  '| 参数 | 类型 |',
  '| --- | --- |',
  '| id | int |',
  '',
  '```js',
  'const a = 1;',
  '```',
  ''
].join('\n');

const SCENE = `
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function rgb(s){
    var m = /rgba?\\(([^)]+)\\)/.exec(s || '');
    if (m) {
      var p = m[1].split(',').map(function(x){ return parseFloat(x); });
      if (p.length >= 4 && !p[3]) return null;   // 透明 = 没有自己的底色
      return p.slice(0, 3);
    }
    // color-mix 的 computed 值是 color(srgb 0..1 0..1 0..1)，不是 rgb()
    var c = /color\\(srgb ([\\d.]+) ([\\d.]+) ([\\d.]+)/.exec(s || '');
    if (c) return [parseFloat(c[1]) * 255, parseFloat(c[2]) * 255, parseFloat(c[3]) * 255];
    return null;
  }
  function lum(c){
    var a = c.map(function(v){ v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
  }
  function ratio(a, b){
    var la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  function hex(c){ return '#' + c.map(function(v){ return ('0' + Math.round(v).toString(16)).slice(-2); }).join(''); }
  /* 元素的「实际底色」：自己有背景色就用它，否则往上找第一个不透明的祖先 */
  function effBg(el, bodyBg){
    var n = el;
    while (n && n !== document.documentElement) {
      var c = rgb(getComputedStyle(n).backgroundColor);
      if (c) return c;
      n = n.parentElement;
    }
    return bodyBg;
  }
  function probe(sel, bodyBg){
    var el = document.querySelector(sel);
    if (!el) return null;
    return { fg: rgb(getComputedStyle(el).color), bg: effBg(el, bodyBg) };
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent',
      theme: window.__THEME } });
    await sleep(200);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: window.__MD } });
    await sleep(40);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(800);

    var bodyBg = rgb(getComputedStyle(document.body).backgroundColor);
    out.push('DIAG 页面底=' + (bodyBg ? hex(bodyBg) : '?'));

    // ── 1. 代码块底色的方向与幅度 ──
    var code = document.querySelector('.code');
    var codeBg = rgb(getComputedStyle(code).backgroundColor);
    var hd = document.querySelector('.code-hd');
    var hdBg = rgb(getComputedStyle(hd).backgroundColor);
    var pageL = lum(bodyBg), codeL = lum(codeBg);
    out.push('DIAG 块底=' + hex(codeBg) + ' 标题栏=' + hex(hdBg) + ' 页面=' + hex(bodyBg));
    ck('L1 代码块底色与页面底有区分（亮度差 ≥ 1%）',
       Math.abs(codeL - pageL) >= 0.01, 'ΔL=' + (codeL - pageL).toFixed(3));
    ck('L2 区别是「一档」而不是一道深沟（亮度差 ≤ 12%）',
       Math.abs(codeL - pageL) <= 0.12, 'ΔL=' + (codeL - pageL).toFixed(3));
    ck('L3 标题栏与块体分两档（亮度不同）',
       Math.abs(lum(hdBg) - codeL) > 0.004,
       'Δ=' + (lum(hdBg) - codeL).toFixed(4));

    // ── 2. 正文级文字对**实际底色**的对比度 ──
    var items = [
      ['L4 正文', '.msg.assistant p', 4.5],
      ['L5 一级标题', '.msg.assistant h1', 4.5],
      ['L6 行内代码（对它自己那层淡底）', '.msg.assistant code', 4.5],
      ['L7 代码块文字', '.code pre', 4.5],
      ['L8 表格正文', '.msg.assistant td', 4.5],
      ['L9 引用块', '.msg.assistant blockquote', 3.0]
    ];
    items.forEach(function(it){
      var p = probe(it[1], bodyBg);
      if (!p || !p.fg) { ck(it[0] + '：元素存在', false, '(找不到元素或取不到色)'); return; }
      var r = ratio(p.fg, p.bg);
      out.push('DIAG ' + it[0] + ' 文字=' + hex(p.fg) + ' 实底=' + hex(p.bg) + ' 对比=' + r.toFixed(2));
      ck(it[0] + ' 对比度 ≥ ' + it[2], r >= it[2], r.toFixed(2) + ':1');
    });

    // ── 3. dim 修饰不能弱化到读不清 ──
    // 「引用」在 narnat.json 里标了 italic dim；浅色下 dim 会往白色混、变得更浅，
    // 弱化本身是意图，但跌破该角色下限就是缺陷。
    var bq = probe('.msg.assistant blockquote', bodyBg);
    ck('L10 dim 修饰后仍 ≥ 3.0（弱化不等于看不清）',
       !!bq && ratio(bq.fg, bq.bg) >= 3.0,
       bq ? ratio(bq.fg, bq.bg).toFixed(2) + ':1' : '(无)');

    out.push('SUMMARY total=' + out.length + ' fail=' + fails);
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\\n');
    document.body.appendChild(pre);
  }
  window.addEventListener('load', function(){
    run().catch(function(e){
      var pre = document.createElement('pre');
      pre.id = 'ERR';
      pre.textContent = 'run 抛错: ' + (e && e.stack ? e.stack : e);
      document.body.appendChild(pre);
    });
  });
})();
`;

// VSCode Light Modern 的真实变量值
const LIGHT_VARS = `
:root{
  --vscode-editor-background:#ffffff;
  --vscode-editor-foreground:#3b3b3b;
  --vscode-sideBar-background:#f8f8f8;
  --vscode-editorWidget-background:#f8f8f8;
  --vscode-list-hoverBackground:#e8e8e8;
  --vscode-panel-border:#e5e5e5;
  --vscode-textLink-foreground:#006ab1;
  --vscode-charts-green:#388a34;
  --vscode-editorWarning-foreground:#bf8803;
  --vscode-errorForeground:#e51400;
  --vscode-chat-requestBackground:#f2f4f7;
  --vscode-button-secondaryBackground:#5f6a79;
  --vscode-badge-background:#cccccc;
  --vscode-focusBorder:#0090f1;
  --vscode-gitDecoration-addedResourceForeground:#587c0c;
  --vscode-gitDecoration-deletedResourceForeground:#ad0707;
  --vscode-debugTokenExpression-name:#af00db;
  --vscode-symbolIcon-methodForeground:#795e26;
  --vscode-descriptionForeground:#717171;
}
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-light</title>
<style>${CSS}${LIGHT_VARS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};window.__MD=${JSON.stringify(MD)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-light');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
if (!m) { console.log('(无 RESULT)'); process.exit(2); }
const text = m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&');
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
