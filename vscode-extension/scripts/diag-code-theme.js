/**
 * 定位浅色主题下代码块「黑窟窿 + 刺眼语法色」的真实根因。
 *
 * 走**真实管线**：从后端拿 theme 快照（与 webview 收到的一致），
 * 注入真实 CSS 变量后再量 —— 上一次诊断手填的变量名不对，量出来的
 * pre 底色是透明，属于假数据。
 *
 * 要回答的问题：
 *   1. 代码块底色在白底上到底是什么（是不是一块深色斑）
 *   2. 语法色（tk-kw / tk-st / tk-nu）在白底/代码底上的对比度
 *   3. 这些值的来源：是配置里的固定值，还是经亮度适配过的
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const REPO = path.resolve(__dirname, '..', '..');
const EXT = path.join(REPO, 'vscode-extension');
const html = fs.readFileSync(path.join(EXT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(EXT, 'media', 'narnat.js'), 'utf8');

// 真后端产出的主题快照（结构与 initialize 下发的 theme 一致）
const py = [
  'import sys, io, json',
  'sys.path.insert(0, r"' + REPO + '")',
  'from narnat_agent.ui.theme import snapshot',
  'from narnat_agent.config.loader import load_config',
  'c = load_config()',
  'raw = getattr(c.ui, "raw", None) or {}',
  'print(json.dumps(snapshot(raw), ensure_ascii=False))'
].join('\n');
let theme = null;
try {
  theme = JSON.parse(execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim());
} catch (e) {
  console.log('取主题快照失败: ' + String(e.stdout || e.message).slice(0, 400));
  process.exit(2);
}
console.log('快照 bgs 键: ' + JSON.stringify(Object.keys(theme.bgs || {})));
console.log('快照 colors 里 codeblock 相关: ' +
  JSON.stringify(Object.keys(theme.colors || {}).filter((k) => k.indexOf('code') >= 0)));

const DARK_VARS = `:root{
  --vscode-editor-background:#1f1f1f; --vscode-editor-foreground:#cccccc;
  --vscode-sideBar-background:#181818; --vscode-editorWidget-background:#202020;
  --vscode-list-hoverBackground:#2a2d2e; --vscode-panel-border:#2b2b2b;
  --vscode-textLink-foreground:#4daafc; --vscode-charts-green:#89d185;
  --vscode-errorForeground:#f14c4c; --vscode-chat-requestBackground:#1f1f1f;
  --vscode-button-secondaryBackground:#313131; --vscode-button-secondaryForeground:#cccccc;
  --vscode-button-background:#0078d4; --vscode-button-foreground:#ffffff;
  --vscode-icon-foreground:#cccccc; --vscode-focusBorder:#0078d4;
  --vscode-descriptionForeground:#9d9d9d; --vscode-editorWidget-border:#3c3c3c;
  --vscode-input-background:#313131;
}`;
const LIGHT_VARS = `:root{
  --vscode-editor-background:#ffffff; --vscode-editor-foreground:#3b3b3b;
  --vscode-sideBar-background:#f8f8f8; --vscode-editorWidget-background:#f8f8f8;
  --vscode-list-hoverBackground:#e8e8e8; --vscode-panel-border:#e5e5e5;
  --vscode-textLink-foreground:#006ab1; --vscode-charts-green:#388a34;
  --vscode-errorForeground:#e51400; --vscode-chat-requestBackground:#f2f4f7;
  --vscode-button-secondaryBackground:#e5e5e5; --vscode-button-secondaryForeground:#3b3b3b;
  --vscode-button-background:#005fb8; --vscode-button-foreground:#ffffff;
  --vscode-icon-foreground:#3b3b3b; --vscode-focusBorder:#0090f1;
  --vscode-descriptionForeground:#717171; --vscode-editorWidget-border:#cecece;
  --vscode-input-background:#ffffff;
}`;

const TEST = `
(function(){
  var out = [];
  function parse(c){
    if (!c) return null;
    var m = /^rgba?\\(\\s*([\\d.]+)[,\\s]+([\\d.]+)[,\\s]+([\\d.]+)(?:[,\\s/]+([\\d.]+))?/.exec(c);
    if (m) { var a = m[4] === undefined ? 1 : parseFloat(m[4]); return a === 0 ? null : [+m[1],+m[2],+m[3]]; }
    m = /^color\\(srgb\\s+([\\d.]+)\\s+([\\d.]+)\\s+([\\d.]+)/.exec(c);
    if (m) return [m[1]*255, m[2]*255, m[3]*255];
    return null;
  }
  function lum(c){
    var a = c.map(function(v){ v=v/255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); });
    return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];
  }
  function ratio(f,b){ var l1=lum(f),l2=lum(b); if(l1<l2){var t=l1;l1=l2;l2=t;} return (l1+0.05)/(l2+0.05); }
  function hex(c){ return '#'+c.map(function(v){return ('0'+Math.round(v).toString(16)).slice(-2);}).join(''); }
  function realBg(el){
    var n = el;
    while (n && n.nodeType === 1) { var c = parse(getComputedStyle(n).backgroundColor); if (c) return c; n = n.parentElement; }
    return [255,255,255];
  }

  window.addEventListener('load', function(){
    var msg = document.createElement('div');
    msg.className = 'msg assistant';
    msg.innerHTML =
      '<div class="code" data-wrap="true">' +
        '<div class="code-hd" data-lang-group="purple">' +
          '<span class="lang">cpp</span>' +
          '<button class="code-wrap" type="button" data-on="true">换行</button>' +
          '<button class="code-copy" type="button">复制</button>' +
        '</div>' +
        '<pre data-lang="cpp" data-lines="6">' +
          '<span class="tk-kw">class</span> <span class="tk-fn">Pool</span> {\\n' +
          '  <span class="tk-cm">// c</span> <span class="tk-nu">42</span> <span class="tk-st">"s"</span>\\n' +
          '};' +
        '</pre>' +
      '</div>';
    document.getElementById('convo').appendChild(msg);

    var codeEl = msg.querySelector('.code');
    var preEl = msg.querySelector('pre');
    var bodyBg = realBg(document.body);
    var codeBg = realBg(codeEl);
    out.push('BODY  ' + hex(bodyBg));
    out.push('CODE  ' + hex(codeBg) + '  亮度差 ' + ((lum(codeBg)-lum(bodyBg))*100).toFixed(1) + '%');

    var hd = msg.querySelector('.code-hd');
    out.push('CODE头 ' + hex(realBg(hd)));

    var toks = [['.tk-kw','关键字'],['.tk-st','字符串'],['.tk-nu','数字'],['.tk-cm','注释'],['.tk-fn','函数名']];
    for (var i=0;i<toks.length;i++){
      var el = msg.querySelector(toks[i][0]);
      if (!el) { out.push(toks[i][1]+' 不存在'); continue; }
      var fg = parse(getComputedStyle(el).color);
      if (!fg) { out.push(toks[i][1]+' 无前景色'); continue; }
      out.push(toks[i][1]+' '+hex(fg)+
        '  对代码底 '+ratio(fg,codeBg).toFixed(2)+
        '  对页面底 '+ratio(fg,bodyBg).toFixed(2));
    }
    // CSS 变量的真实值（看是配置原值还是被适配过）
    var rs = getComputedStyle(document.documentElement);
    ['--bg','--bg-code','--bg-code-hd','--tb-card_background','--t-b-code_bg',
     '--t-codeblock-keyword','--t-codeblock-string','--t-codeblock-number',
     '--t-codeblock-comment','--t-codeblock-function'].forEach(function(v){
      var val = rs.getPropertyValue(v);
      if (val) out.push('VAR '+v+' = '+val.trim());
    });

    var p = document.createElement('pre');
    p.id = 'RESULT';
    p.textContent = out.join('\\n');
    document.body.appendChild(p);
  });
})();
`;

function build(vars, themeObj) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>diag</title>
<style>${vars}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>var THEME=${JSON.stringify(themeObj)};window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;
}

const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

function run(vars, tag) {
  const dir = path.join(os.tmpdir(), 'diag-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, build(vars, theme), 'utf8');
  const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=9000', '--dump-dom',
    'file:///' + file.replace(/\\/g, '/')],
  { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
  console.log('\n===== ' + tag + ' =====');
  console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&') : '(无结果)');
  fs.rmSync(dir, { recursive: true, force: true });
}

run(DARK_VARS, 'dark');
run(LIGHT_VARS, 'light');
