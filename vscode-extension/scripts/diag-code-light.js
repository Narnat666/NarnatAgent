/**
 * 量浅色主题下代码块的真实配色，定位「黑窟窿 + 刺眼语法色」。
 *
 * 症状（用户截图）：One Dark Pro 下一切正常；换 Light Modern 后
 * 代码块是接近纯黑的底，语法色是霓虹紫/霓虹绿，看不清。
 *
 * 怀疑：代码块底色与语法色取的是「固定配置值 / 深色兜底」，
 * 没有按浅色底重新适配。这里把每个角色的**实际计算值**打出来。
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
  --vscode-errorForeground:#e51400;
  --vscode-chat-requestBackground:#f2f4f7;
  --vscode-button-secondaryBackground:#e5e5e5;
  --vscode-button-secondaryForeground:#3b3b3b;
  --vscode-button-background:#005fb8;
  --vscode-button-foreground:#ffffff;
  --vscode-icon-foreground:#3b3b3b;
  --vscode-focusBorder:#0090f1;
  --vscode-descriptionForeground:#717171;
  --vscode-editorWidget-border:#cecece;
  --vscode-input-background:#ffffff;
}
`;

// 与 narnat.json 的界面配置一致（蓬莱仙酿）—— 这些是"内容色"
const EXTRA = `
:root{
  --tb-card-bg:#1E2630;
  --tb-accent:#C24D56;
  --tb-str:#5A9E82;
  --tb-num:#D4A853;
  --tb-kw:#9E8BB5;
  --tb-fn:#6A8FAF;
  --tb-cm:#6B7580;
}
`;

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
    var a = c.map(function(v){ v = v/255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); });
    return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];
  }
  function ratio(f,b){ var l1=lum(f),l2=lum(b); if(l1<l2){var t=l1;l1=l2;l2=t;} return (l1+0.05)/(l2+0.05); }
  function hex(c){ return '#' + c.map(function(v){ return ('0'+Math.round(v).toString(16)).slice(-2); }).join(''); }

  window.addEventListener('load', function(){
    var convo = document.getElementById('convo');

    // 造一段带代码块的回答
    document.body.setAttribute('data-run','false');
    var msg = document.createElement('div');
    msg.className = 'msg assistant';
    msg.innerHTML =
      '<p>下面是一段 C++ 代码：</p>' +
      '<div class="code" data-wrap="true">' +
        '<div class="code-hd" data-lang-group="purple">' +
          '<span class="lang">cpp</span>' +
          '<button class="code-wrap" type="button" data-on="true">换行</button>' +
          '<button class="code-copy" type="button">复制</button>' +
        '</div>' +
        '<pre data-lang="cpp" data-lines="12">' +
          '<span class="tk-kw">class</span> <span class="tk-fn">ThreadPool</span> {\\n' +
          '  <span class="tk-cm">// 注释</span>\\n' +
          '  <span class="tk-nu">42</span> <span class="tk-st">"字符串"</span>\\n' +
          '};' +
        '</pre>' +
      '</div>';
    convo.appendChild(msg);

    function probe(sel, label){
      var el = msg.querySelector(sel);
      if (!el) { out.push(label + ' => 元素不存在'); return; }
      var st = getComputedStyle(el);
      out.push(label + ' color=' + st.color + ' bg=' + st.backgroundColor);
    }

    // 代码块整体
    var codeEl = msg.querySelector('.code');
    var preEl = msg.querySelector('pre');
    var bodyBg = getComputedStyle(document.body).backgroundColor;
    var codeBg = getComputedStyle(preEl).backgroundColor;
    var cBg = parse(codeBg), bBg = parse(bodyBg);
    out.push('BODY 底色 = ' + bodyBg + (bBg ? ' ' + hex(bBg) : ''));
    out.push('CODE 区域底色 = ' + codeBg + (cBg ? ' ' + hex(cBg) : ''));
    if (cBg && bBg) {
      var dL = (lum(cBg) - lum(bBg)) * 100;
      out.push('代码块与页面底 亮度差 = ' + dL.toFixed(2) + '% (浅色主题下应当**更暗一点点或几乎同色**，差 60% 以上就是黑窟窿)');
    }

    // 语法色 vs 代码底
    var toks = [
      ['.tk-kw', '关键字'], ['.tk-st', '字符串'], ['.tk-nu', '数字'],
      ['.tk-cm', '注释'], ['.tk-fn', '函数名']
    ];
    for (var i = 0; i < toks.length; i++) {
      var el = msg.querySelector(toks[i][0]);
      if (!el) { out.push(toks[i][1] + ' 不存在'); continue; }
      var fg = parse(getComputedStyle(el).color);
      var r = (fg && cBg) ? ratio(fg, cBg) : 0;
      out.push(toks[i][1] + ' 前景=' + (fg ? hex(fg) : '?') +
        ' 对代码底对比度 = ' + r.toFixed(2) + (r < 4.5 ? '  ← 低于 4.5' : ''));
    }

    // 代码块头部
    var hd = msg.querySelector('.code-hd');
    if (hd) {
      var hst = getComputedStyle(hd);
      out.push('CODE 头 底=' + hst.backgroundColor + ' 字色=' + hst.color);
    }

    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\\n');
    document.body.appendChild(pre);
  });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>diag-code-light</title>
<style>${LIGHT_VARS}${EXTRA}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'diag-code-light');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=9000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&') : '(无结果)');
fs.rmSync(dir, { recursive: true, force: true });
