/**
 * 浅色主题核对（用户问：「到时候我 vscode 换成白色主题也行是吧，就是代码底色」）。
 *
 * 目的不是出好看的图，而是回答一个可判定的问题：把 VSCode 换成 Light Modern
 * 之后，代码块 / 行内代码 / 表格 / 配置色的那些**底色派生**还成不成立。
 * 需要盯三件事：
 *   1. 代码块底色相对页面底是「暗一档」还是「亮一档」——派生逻辑混的是前景色，
 *      深色主题下变亮、浅色主题下变暗，方向应当自动正确
 *   2. 标题栏（--bg-code-hd）比块体再深一档，会不会深成一条脏灰条
 *   3. narnat.json 里配的深色背景（如 #1E2630）会不会在浅底上变成一块深斑
 *      ——走的是 adaptSurface 的镜像逻辑，本图直接看结果
 *
 * 同时输出每个元素的**实测 RGB 与对比度**，够判断就不用肉眼猜。
 * 无断言，产出 PNG + 数字报告。
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
  '# 浅色主题下的一级标题',
  '',
  '普通正文一段，带 **粗体**、*斜体*、`行内代码` 和 [链接](https://x.y)。',
  '',
  '> 引用块：底色与左边线都要在浅底上看得见。',
  '',
  '| 参数 | 类型 | 说明 |',
  '| --- | --- | --- |',
  '| id | int | 用户 ID |',
  '| path | string | `E:\\桌面\\narnat` |',
  '',
  '```js',
  '// 注释',
  'function parse(input) {',
  '  const node = next(input);',
  '  return node && node.line;',
  '}',
  '```',
  '',
  '```diff',
  '- const a = 1;',
  '+ const a = 2;',
  '```',
  ''
].join('\n');

const SCENE = `
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function rgb(s){
    var m = /rgba?\\(([^)]+)\\)/.exec(s || '');
    if (m) {
      var p = m[1].split(',').map(function(x){ return parseFloat(x); });
      // 透明（alpha=0）当作「没有自己的底色」：否则 rgba(0,0,0,0) 会被当成纯黑，
      // 算出来的对比度全是假的（上一版正文/表格/引用块就是这么报成 1.87:1 的）
      if (p.length >= 4 && !p[3]) return null;
      return p.slice(0, 3);
    }
    // color-mix 的 computed 值是「color(srgb 0.779765 0.794196 0.815843)」，
    // 不是 rgb()。只认 rgb() 会把它当成「没有底色」，上一版就是这样把
    // 代码块标题栏错报成「与块体同色」的（其实它确实深了一档）。
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
    return ((Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)).toFixed(2);
  }
  function hex(c){ return '#' + c.map(function(v){ return ('0' + Math.round(v).toString(16)).slice(-2); }).join(''); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent',
      theme: window.__THEME } });
    await sleep(200);
    // 走实时渲染路径（turn 流），不走 session/result：
    // 上一版用历史回放没渲染出内容，这里用已验证可行的实时链路，
    // 要验的是配色（CSS 变量最终值），与走哪条渲染入口无关。
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '先确认配色链路：底色派生要跟着主题翻转。' } });
    await sleep(20);
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'c1', name: 'Read', label: '读取',
      summary: 'a.md', arguments: { path: 'a.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'c1', ok: true, durationMs: 5, output: 'ok' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: window.__MD } });
    await sleep(40);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(800);
    var row = document.querySelector('.turn-proc .tp-row');
    if (row) row.click();
    await sleep(500);

    // 实测：把关键元素的实际计算色 + 对比度打出来
    var out = [];
    function probe(name, sel, againstSel){
      var el = document.querySelector(sel);
      if (!el) { out.push(name + ' = (缺元素)'); return; }
      var cs = getComputedStyle(el);
      var fg = rgb(cs.color);
      var bgs = rgb(cs.backgroundColor);
      var base = againstSel ? rgb(getComputedStyle(document.querySelector(againstSel)).backgroundColor)
                            : rgb(getComputedStyle(document.body).backgroundColor);
      if (!fg || !base) { out.push(name + ' = (取不到色)'); return; }
      var own = bgs || base;
      out.push(name + ' 文字=' + hex(fg) + ' 底=' + hex(own) +
        ' 对底=' + ratio(fg, own) + ':1' +
        (againstSel ? ' | 该底对页面' + ratio(own, base) + ':1' : ''));
    }
    var bodyBg = rgb(getComputedStyle(document.body).backgroundColor);
    out.push('页面底=' + (bodyBg ? hex(bodyBg) : '?'));
    probe('正文', '.msg.assistant p', null);
    probe('一级标题', '.msg.assistant h1', null);
    probe('行内代码', '.msg.assistant code:not(.code code)', null);
    probe('代码块体', '.code', null);
    probe('代码块标题栏', '.code-hd', '.code');
    probe('代码块文字', '.code pre', '.code');
    probe('关键字高亮', '.code .tk-kw', '.code');
    probe('表格表头', '.msg.assistant th', null);
    probe('表格正文', '.msg.assistant td', null);
    probe('引用块', '.msg.assistant blockquote', null);
    // 变量本身也要打出来：派生链断了（比如 color-mix 嵌套 var 无效）时，
    // 光看元素色只会看到「和块体同色」，不知道是哪一环没生效
    var rs = getComputedStyle(document.documentElement);
    out.push('--bg=' + rs.getPropertyValue('--bg').trim() +
      ' --bg-code=' + rs.getPropertyValue('--bg-code').trim() +
      ' --bg-code-hd=' + rs.getPropertyValue('--bg-code-hd').trim() +
      ' --tb-codeblock-background=' + rs.getPropertyValue('--tb-codeblock-background').trim() +
      ' --tb-code-bg=' + rs.getPropertyValue('--tb-code-bg').trim() +
      ' --code-border=' + rs.getPropertyValue('--code-border').trim());
    // 配置色推到什么程度，取决于 applyTheme 认定「底是暗还是亮」。
    // 这两个变量一起看就能判断方向对不对（引用块如果比配置色更亮，
    // 说明它按「深底要提亮」处理了，在浅底上就会失读）。
    out.push('--t-markdown-blockquote=' + rs.getPropertyValue('--t-markdown-blockquote').trim() +
      ' --t-markdown-table-content=' + rs.getPropertyValue('--t-markdown-table-content').trim() +
      ' --t-markdown-heading-h1=' + rs.getPropertyValue('--t-markdown-heading-h1').trim());
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\\n');
    document.body.appendChild(pre);
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

// VSCode Light Modern 的真实变量值（不是随手造的白）
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
  --vscode-symbolIcon-classForeground:#267f99;
  --vscode-descriptionForeground:#717171;
}
#RESULT{position:fixed;left:0;bottom:0;margin:0;padding:6px 8px;font:10px/1.5 monospace;
  background:rgba(255,255,255,.94);color:#111;white-space:pre;z-index:99;border-top:1px solid #ccc}
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-light</title>
<style>${CSS}${LIGHT_VARS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};window.__MD=${JSON.stringify(MD)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shot-light');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'light-theme.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
// dump-dom 与 screenshot 不能同给（实测），分两次
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p2'), '--virtual-time-budget=15000',
  '--window-size=760,1050', '--hide-scrollbars', '--screenshot=' + out,
  'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
console.log(out);
