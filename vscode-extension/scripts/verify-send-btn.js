/**
 * 验收输入区两个圆形按钮在**两套主题**下的符号可辨性。
 *
 * 背景（用户报障的真实形态）：浅色主题下「＋附件」和「↑发送」变成两个实心灰圆，
 * 里面的符号看不见 —— 放大截图后圆里只有 ~1.25:1 的脏点。
 * 根因是 `.send[data-empty="true"]` 用 `background:--pill; color:--fg-faint`：
 * `--fg-faint` 是「对页面底可读的暗灰」，配上深色圆底就成了暗叠暗。
 * 两个各自正确的颜色组合起来互相抵消 —— 又一次「派生式在另一主题失效」。
 *
 * 判据（可计算）：
 *   S1 深色：附件符号 / 其实际底色 ≥ 3:1
 *   S2 深色：空输入发送符号 / 其实际底色 ≥ 3:1
 *   S3 浅色：附件符号 / 其实际底色 ≥ 3:1
 *   S4 浅色：空输入发送符号 / 其实际底色 ≥ 3:1
 *   S5 两套主题：可发送态（有字）符号 / 按钮底 ≥ 4.5:1
 *   S6 空输入与可发送态必须有可见差异（底或环不同），否则用户看不出「按了没用」
 *
 * 注意「透明底」要向上取到真有底色的祖先，直接拿 rgba(0,0,0,0) 算会得到假数字。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const DARK = `
  --vscode-editor-background:#1f1f1f;
  --vscode-editor-foreground:#cccccc;
  --vscode-sideBar-background:#181818;
  --vscode-editorWidget-background:#202020;
  --vscode-list-hoverBackground:#2a2d2e;
  --vscode-panel-border:#2b2b2b;
  --vscode-textLink-foreground:#4daafc;
  --vscode-descriptionForeground:#9d9d9d;
  --vscode-button-background:#0078d4;
  --vscode-button-foreground:#ffffff;
  --vscode-button-hoverBackground:#026ec1;
  --vscode-button-secondaryBackground:#313131;
  --vscode-button-secondaryForeground:#cccccc;
  --vscode-icon-foreground:#cccccc;
  --vscode-focusBorder:#0078d4;
`;
const LIGHT = `
  --vscode-editor-background:#ffffff;
  --vscode-editor-foreground:#3b3b3b;
  --vscode-sideBar-background:#f8f8f8;
  --vscode-editorWidget-background:#f8f8f8;
  --vscode-list-hoverBackground:#e8e8e8;
  --vscode-panel-border:#e5e5e5;
  --vscode-textLink-foreground:#006ab1;
  --vscode-descriptionForeground:#717171;
  --vscode-button-background:#005fb8;
  --vscode-button-foreground:#ffffff;
  --vscode-button-hoverBackground:#0067c8;
  --vscode-button-secondaryBackground:#e5e5e5;
  --vscode-button-secondaryForeground:#3b3b3b;
  --vscode-icon-foreground:#3b3b3b;
  --vscode-focusBorder:#0090f1;
`;

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function parse(s){
    if (!s) return null;
    var m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?/.exec(s);
    if (m) {
      var a = m[4] === undefined ? 1 : parseFloat(m[4]);
      if (a === 0) return null;
      return [+m[1], +m[2], +m[3]];
    }
    m = /^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(s);
    if (m) return [m[1]*255, m[2]*255, m[3]*255];
    return null;
  }
  function lum(c){
    var a = c.map(function(v){
      v = v/255;
      return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4);
    });
    return 0.2126*a[0] + 0.7152*a[1] + 0.0722*a[2];
  }
  function ratio(f, b){
    var l1 = lum(f), l2 = lum(b);
    if (l1 < l2) { var t = l1; l1 = l2; l2 = t; }
    return (l1 + 0.05) / (l2 + 0.05);
  }
  /* 从元素自身往上找第一个真有底色的祖先（透明底直接算会得出假数字） */
  function realBg(el){
    var n = el;
    while (n && n.nodeType === 1) {
      var c = parse(getComputedStyle(n).backgroundColor);
      if (c) return c;
      n = n.parentElement;
    }
    return [31, 31, 31];
  }
  function probe(btnSel, childSel){
    var btn = document.querySelector(btnSel);
    if (!btn) return null;
    var sym = childSel ? (btn.querySelector(childSel) || btn) : btn;
    var fg = parse(getComputedStyle(sym).color);
    var bg = realBg(btn);
    return { fg: fg, bg: bg, r: fg ? ratio(fg, bg) : 0,
             bgStr: getComputedStyle(btn).backgroundColor,
             ring: getComputedStyle(btn).boxShadow };
  }
  window.addEventListener('load', function(){
    var send = document.getElementById('sendBtn');
    if (!send) { out.push('FAIL S0 找不到 #sendBtn'); }
    else {
      /* 空输入态 */
      send.setAttribute('data-empty', 'true');
      var attE = probe('#attachBtn', null);
      var sendE = probe('#sendBtn', '.ar');
      out.push('DIAG attach(empty) fg=' + (attE && attE.fg) + ' bg=' + (attE && attE.bg) +
        ' ratio=' + (attE ? attE.r.toFixed(2) : 'n/a'));
      out.push('DIAG send(empty) fg=' + (sendE && sendE.fg) + ' bg=' + (sendE && sendE.bg) +
        ' ratio=' + (sendE ? sendE.r.toFixed(2) : 'n/a') + ' ring=' + (sendE && sendE.ring));
      ck('S1 附件按钮符号 / 实际底色 ≥ 3', attE && attE.r >= 3,
        attE ? attE.r.toFixed(2) + ':1' : '取不到');
      ck('S2 空输入发送按钮符号 / 实际底色 ≥ 3', sendE && sendE.r >= 3,
        sendE ? sendE.r.toFixed(2) + ':1' : '取不到');

      /* 可发送态 */
      send.setAttribute('data-empty', 'false');
      var sendR = probe('#sendBtn', '.ar');
      out.push('DIAG send(ready) fg=' + (sendR && sendR.fg) + ' bg=' + (sendR && sendR.bg) +
        ' ratio=' + (sendR ? sendR.r.toFixed(2) : 'n/a'));
      ck('S5 可发送态符号 / 按钮底 ≥ 4.5', sendR && sendR.r >= 4.5,
        sendR ? sendR.r.toFixed(2) + ':1' : '取不到');

      /* 两种状态必须有可见差异：空输入时透明底+环，或至少背景不同 */
      var bgEmpty = sendE && String(sendE.bgStr);
      var bgReady = sendR && String(sendR.bgStr);
      var ringEmpty = sendE && /inset/.test(String(sendE.ring));
      ck('S6 空输入态与可发送态外观不同（底不同或有空心环）',
        bgEmpty !== bgReady || ringEmpty,
        'empty=' + bgEmpty + ' ready=' + bgReady + ' ring=' + ringEmpty);
    }
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\n') + '\nFAILCOUNT ' + fails;
    document.body.appendChild(pre);
  });
})();
`;

let totalFail = 0;
let totalAssert = 0;

function run(vars, tag, s1, s2) {
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>vsb-${tag}</title>
<style>:root{${vars}}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;
  const dir = path.join(os.tmpdir(), 'narnat-vsb-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, page, 'utf8');
  const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
  const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=9000', '--dump-dom',
    'file:///' + file.replace(/\\/g, '/')],
  { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
  if (!m) { console.log('FAIL ' + tag + ' 没有拿到结果'); totalFail++; return; }
  const text = m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  const lines = text.split('\n');
  for (const ln of lines) {
    if (!ln.trim() || /^FAILCOUNT/.test(ln)) continue;
    let show = ln;
    if (tag === 'light') {
      // 同一组断言在浅色下换个编号，避免与深色重复
      show = ln.replace(/^PASS S1 /, 'PASS S3 ').replace(/^PASS S2 /, 'PASS S4 ')
        .replace(/^FAIL S1 /, 'FAIL S3 ').replace(/^FAIL S2 /, 'FAIL S4 ');
    }
    console.log(show);
    if (/^PASS /.test(ln)) totalAssert++;
    if (/^FAIL /.test(ln)) { totalAssert++; totalFail++; }
  }
  const fc = /FAILCOUNT (\d+)/.exec(text);
  if (fc && Number(fc[1]) > 0) {
    // 计过数了
  }
}

run(DARK, 'dark');
run(LIGHT, 'light');
console.log('\nSUMMARY total=' + totalAssert + ' fail=' + totalFail);
process.exit(totalFail ? 1 : 0);
