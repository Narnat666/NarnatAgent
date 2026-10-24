/**
 * 验收：浅色主题下代码块的语法高亮是否可读（用户报障「看不清代码」）。
 *
 * 走**真实管线**：真实 theme 快照 → initialize → renderHistory 渲染出代码块
 * → 量每个词法色对它实际所在底色的对比度。
 *
 * 判据（用户明确要求：One Dark Pro 下不要动，白色主题下要能看清）：
 *   C1 深色主题：关键字/字符串/数字/函数名 对代码底 ≥ 4.5，且与改动前的
 *      原值一致（不能把用户满意的深色配色改掉）
 *   C2 浅色主题：同上 ≥ 4.5（修复前是 2.43 / 2.31 / 1.48 / 2.64）
 *   C3 浅色主题：保留色相（不是一律变黑灰）—— 用色相角差 ≤ 25° 判
 *   C4 代码块底色在浅色下仍是「比页面底暗一点点」，不是黑窟窿
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

const py = [
  'import sys, io, json',
  'sys.path.insert(0, r"' + REPO + '")',
  'from narnat_agent.ui.theme import snapshot',
  'from narnat_agent.config.loader import load_config',
  'c = load_config()',
  'print(json.dumps(snapshot(getattr(c.ui, "raw", None) or {}), ensure_ascii=False))'
].join('\n');
const theme = JSON.parse(execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim());

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
  --vscode-debugTokenExpression-string:#ce9178;
  --vscode-debugTokenExpression-number:#b5cea8;
  --vscode-debugTokenExpression-name:#c586c0;
  --vscode-symbolIcon-methodForeground:#b180d7;
  --vscode-editorLineNumber-foreground:#495162;
  --vscode-gitDecoration-addedResourceForeground:#81b88b;
  --vscode-gitDecoration-deletedResourceForeground:#c74e39;
}`;

// Light Modern 的真实值：注意它**没有** debugTokenExpression-*（这正是 bug 成因）
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
  --vscode-editorLineNumber-foreground:#6e7681;
  --vscode-gitDecoration-addedResourceForeground:#587c0c;
  --vscode-gitDecoration-deletedResourceForeground:#ad0707;
}`;

// 要渲染的 markdown（含一个 cpp 代码块）。用数组拼，避免在模板字符串里
// 出现三反引号（会提前闭合外层模板）。围栏用 [0] 拼出来。
const FENCE = String.fromCharCode(96, 96, 96);
const MD_SRC = [
  '一段代码：', '',
  FENCE + 'cpp',
  '#include <thread>',
  'class Pool {',
  '  // 注释',
  '  int n = 42;',
  '  string s = "hi";',
  '};',
  FENCE
].join('\n');

const TEST = `
(function(){
  var MD = ${JSON.stringify(MD_SRC)};
  var out = [], fails = 0;
  function ck(n, ok, d){ if(!ok) fails++; out.push((ok?'PASS ':'FAIL ')+n+(d===undefined?'':'  :: '+String(d))); }
  function parse(c){
    if(!c) return null;
    var m=/^rgba?\\(\\s*([\\d.]+)[,\\s]+([\\d.]+)[,\\s]+([\\d.]+)(?:[,\\s/]+([\\d.]+))?/.exec(c);
    if(m){var a=m[4]===undefined?1:parseFloat(m[4]);return a===0?null:[+m[1],+m[2],+m[3]];}
    m=/^color\\(srgb\\s+([\\d.]+)\\s+([\\d.]+)\\s+([\\d.]+)/.exec(c);
    if(m) return [m[1]*255,m[2]*255,m[3]*255];
    return null;
  }
  function lum(c){var a=c.map(function(v){v=v/255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);});return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2];}
  function ratio(f,b){var l1=lum(f),l2=lum(b);if(l1<l2){var t=l1;l1=l2;l2=t;}return (l1+0.05)/(l2+0.05);}
  function hex(c){return '#'+c.map(function(v){return ('0'+Math.round(v).toString(16)).slice(-2);}).join('');}
  function hue(c){
    var r=c[0]/255,g=c[1]/255,b=c[2]/255;
    var mx=Math.max(r,g,b),mn=Math.min(r,g,b),d=mx-mn;
    if(d===0) return -1;
    var h;
    if(mx===r) h=((g-b)/d)%6; else if(mx===g) h=(b-r)/d+2; else h=(r-g)/d+4;
    h*=60; if(h<0) h+=360; return h;
  }
  function hueDiff(a,b){
    if(a<0||b<0) return 999;   // 无彩色（灰）与有彩色不能比色相
    var d=Math.abs(a-b); return d>180?360-d:d;
  }
  function realBg(el){
    var n=el;
    while(n&&n.nodeType===1){var c=parse(getComputedStyle(n).backgroundColor);if(c)return c;n=n.parentElement;}
    return [255,255,255];
  }

  window.addEventListener('load', function(){
    emitInit();
    function emitInit(){
      window.dispatchEvent(new MessageEvent('message',{data:{type:'initialize',params:{
        version:'t',protocol:1,model:{current:'m',options:['m']},
        thinking:{effort:'high',options:{high:'高'}},goal:{enabled:false,maxRounds:10},
        plugins:[],session:{current:null},workdir:'E:\\\\x',theme:THEME}}}));
    }
    setTimeout(function(){
      window.dispatchEvent(new MessageEvent('message',{data:{type:'session/result',params:{
        action:'sessionSwitch',current:'代码',isChild:false,
        messages:[{role:'user',text:'写段代码'},{role:'assistant',text:MD}]}}}));
      setTimeout(measure, 900);
    }, 250);

    function measure(){
      var msg = document.querySelector('#convo .msg.assistant');
      if(!msg){ ck('渲染出消息', false, '找不到 .msg.assistant'); finish(); return; }
      var codeEl = msg.querySelector('.code');
      if(!codeEl){ ck('渲染出代码块', false, '找不到 .code'); finish(); return; }
      var preEl = msg.querySelector('pre');
      var bodyBg = realBg(document.body);
      var codeBg = realBg(codeEl);

      out.push('BODY '+hex(bodyBg)+'  CODE '+hex(codeBg)+
        '  亮度差 '+((lum(codeBg)-lum(bodyBg))*100).toFixed(1)+'%');
      ck('C4 代码块底不是黑窟窿（|亮度差| ≤ 25%）',
        Math.abs((lum(codeBg)-lum(bodyBg))*100) <= 25,
        hex(codeBg));

      var toks=[['.tk-kw','关键字',4.5],['.tk-st','字符串',4.5],['.tk-nu','数字',4.5],
                ['.tk-fn','函数名',4.5],['.tk-cm','注释',3.0]];
      window.__RATIOS = {};
      // 诊断：--tk-* 变量到底有没有被写进去（区分「没算」与「算了没生效」）
      var rs = getComputedStyle(document.documentElement);
      out.push('DIAG --bg-code = ' + JSON.stringify(rs.getPropertyValue('--bg-code').trim()));
      ['--tk-keyword','--tk-string','--tk-number','--tk-function','--tk-comment','--tk-type']
        .forEach(function(v){
          out.push('DIAG ' + v + ' = ' + JSON.stringify(rs.getPropertyValue(v).trim()));
        });
      for(var i=0;i<toks.length;i++){
        var el=msg.querySelector(toks[i][0]);
        if(!el){ out.push('SKIP '+toks[i][1]+' 不存在'); continue; }
        var fg=parse(getComputedStyle(el).color);
        if(!fg){ out.push('SKIP '+toks[i][1]+' 无前景'); continue; }
        var r=ratio(fg,codeBg);
        window.__RATIOS[toks[i][1]]={hex:hex(fg),r:r,hue:hue(fg)};
        ck('C2 '+toks[i][1]+' 对代码底 ≥ '+toks[i][2], r>=toks[i][2],
          hex(fg)+' '+r.toFixed(2)+':1');
      }
      finish();
    }
    function finish(){
      var p=document.createElement('pre'); p.id='RESULT';
      p.textContent=out.join('\\n')+'\\nRATIOS '+JSON.stringify(window.__RATIOS||{})+
        '\\nFAILCOUNT '+fails;
      document.body.appendChild(p);
    }
  });
})();
`;

function build(vars) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>t</title>
<style>${vars}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>var THEME=${JSON.stringify(theme)};window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;
}

const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

const results = {};
function run(vars, tag) {
  const dir = path.join(os.tmpdir(), 'vcode-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, build(vars), 'utf8');
  const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=14000', '--dump-dom',
    'file:///' + file.replace(/\\/g, '/')],
  { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
  const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&') : '(无结果)';
  console.log('\n===== ' + tag + ' =====');
  for (const ln of text.split('\n')) {
    if (/^RATIOS /.test(ln)) {
      try { results[tag] = JSON.parse(ln.slice(7)); } catch (e) { /* 忽略 */ }
      continue;
    }
    if (/^FAILCOUNT/.test(ln)) continue;
    if (ln.trim()) console.log(ln);
  }
  fs.rmSync(dir, { recursive: true, force: true });
}

run(DARK_VARS, 'dark');
run(LIGHT_VARS, 'light');

// 深色下不许变（用户明确要求）：与改动前的 Dark+ 原值对比
const DARK_ORIG = { '#c586c0': '关键字', '#ce9178': '字符串', '#b5cea8': '数字', '#b180d7': '函数名' };
console.log('\n===== 深色是否被改动（要求：一个像素都不变）=====');
let changed = 0;
if (results.dark) {
  for (const [k, v] of Object.entries(results.dark)) {
    const orig = Object.keys(DARK_ORIG).find((h) =>
      DARK_ORIG[h] === k);
    const same = orig ? (v.hex.toLowerCase() === orig.toLowerCase()) : true;
    console.log((same ? 'SAME ' : 'CHANGED ') + k + ' 现=' + v.hex + ' 原=' + (orig || '(无基线)'));
    if (!same) changed++;
  }
} else {
  console.log('（深色结果没取到）');
}
console.log('\n深色变更项 = ' + changed + (changed === 0 ? '  ← 符合要求' : '  ← 需要检查'));
