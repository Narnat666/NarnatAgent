/**
 * 全谱主题扫描：白 / 深 / 极深 / **中间灰** 各档底色下，插件关键元素
 * 的对比度是否达标。
 *
 * 为什么要专门扫中间色：`adaptColor()` 里有一个二值判断
 *   `var dark = relLum(bg) < 0.5;`
 * 底暗就往亮推、底亮就往暗推。中间灰（relLum 接近 0.5）恰好落在临界带上：
 *   · 底 relLum=0.4 判成 dark → 往白推，最多只能到 (1.05/0.45)=2.33:1 → 永远不达标
 *   · 而同一个底色往黑推能到 9:1
 * 所以中间色主题是这套派生算法最危险的区间，必须实测而不是推理。
 *
 * 判据（与项目既有口径一致）：
 *   正文级 4.5 / 辅助级 3.0 / 线条 2.2
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

// 主题矩阵：覆盖白 → 极深，中间灰多取几档（临界带）。
//
// 前景色必须与底色「方向相反且对比够」—— 这是真实主题的规律
// （VSCode 每个浅色主题都给深字、每个深色主题都给浅字）。
// 第一版把 #7a7a7a 配了 #f0f0f0（浅字配中灰底，2.77:1），
// 那是**输入本身就违规**，不是插件缺陷：任何 UI 拿这组值都读不清。
// 现在中间带的每一档都取「对该底色 ≥ 4.5 的字色」，再考插件的适配逻辑。
const THEMES = [
  ['纯白 #ffffff',      '#ffffff', '#3b3b3b', '#f8f8f8'],
  ['浅灰 #f3f3f3',      '#f3f3f3', '#383838', '#ffffff'],
  ['中间偏亮 #d4d4d4',  '#d4d4d4', '#1a1a1a', '#e8e8e8'],
  ['中间灰 #b4b4b4',    '#b4b4b4', '#1a1a1a', '#c8c8c8'],
  ['中间灰 #969696',    '#969696', '#111111', '#a8a8a8'],
  ['临界 #7a7a7a',      '#7a7a7a', '#0d0d0d', '#8a8a8a'],
  ['中间偏暗 #5a5a5a',  '#5a5a5a', '#f5f5f5', '#686868'],
  ['深灰 #333333',      '#333333', '#e8e8e8', '#3d3d3d'],
  ['深 #1f1f1f',        '#1f1f1f', '#cccccc', '#181818'],
  ['极深 #0d0d0d',      '#0d0d0d', '#e0e0e0', '#141414']
];

const TEST = `
(function(){
  var out = [];
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
  function realBg(el){
    var n=el;
    while(n&&n.nodeType===1){var c=parse(getComputedStyle(n).backgroundColor);if(c)return c;n=n.parentElement;}
    return [128,128,128];
  }
  var FENCE = String.fromCharCode(96,96,96);
  var TICK = String.fromCharCode(96);
  var MD = [
    '## 标题一', '',
    '正文段落，含 **粗体**、*斜体*、' + TICK + '行内代码' + TICK + ' 与 [链接](https://x.test)。', '',
    '- 列表项一', '- 列表项二', '',
    '> 引用块的一段话', '',
    '| 列A | 列B |', '| --- | --- |', '| 1 | 2 |', '',
    FENCE + 'cpp',
    'class Pool {', '  // 注释', '  int n = 42;', '  string s = "hi";', '};',
    FENCE
  ].join('\\n');

  window.addEventListener('load', function(){
    window.dispatchEvent(new MessageEvent('message',{data:{type:'initialize',params:{
      version:'t',protocol:1,model:{current:'m',options:['m']},
      thinking:{effort:'high',options:{high:'高'}},goal:{enabled:false,maxRounds:10},
      plugins:[],session:{current:null},workdir:'E:\\\\x',theme:THEME}}}));
    setTimeout(function(){
      window.dispatchEvent(new MessageEvent('message',{data:{type:'session/result',params:{
        action:'sessionSwitch',current:'全谱',isChild:false,
        messages:[{role:'user',text:'测试'},{role:'assistant',text:MD}]}}}));
      setTimeout(measure, 1100);
    }, 300);

    function measure(){
      var msg = document.querySelector('#convo .msg.assistant');
      var bodyBg = realBg(document.body);
      if(!msg){ out.push('FAIL 没渲染出消息'); finish(); return; }
      var codeEl = msg.querySelector('.code');
      var codeBg = codeEl ? realBg(codeEl) : bodyBg;

      function add(label, el, baseEl, min){
        if(!el){ out.push('MISS '+label); return; }
        var fg = parse(getComputedStyle(el).color);
        var bg = baseEl ? realBg(baseEl) : bodyBg;
        if(!fg){ out.push('MISS '+label+' 无前景'); return; }
        var r = ratio(fg,bg);
        out.push((r>=min?'OK   ':'BAD  ')+label+' '+hex(fg)+'/'+hex(bg)+
          ' = '+r.toFixed(2)+' (需 '+min+')');
      }

      out.push('BODY '+hex(bodyBg)+'  CODE '+hex(codeBg));
      add('正文',     msg.querySelector('p'), msg, 4.5);
      // 诊断：正文色是从哪来的、--fg 算出多少
      (function(){
        var rs = getComputedStyle(document.documentElement);
        out.push('DIAG --fg = ' + JSON.stringify(rs.getPropertyValue('--fg').trim()) +
          '  --t-primary = ' + JSON.stringify(rs.getPropertyValue('--t-primary').trim()) +
          '  --t-markdown-primary = ' + JSON.stringify(rs.getPropertyValue('--t-markdown-primary').trim()));
        var p = msg.querySelector('p');
        if (p) out.push('DIAG p.color = ' + getComputedStyle(p).color);
      })();
      add('H2标题',   msg.querySelector('h2'), msg, 4.5);
      add('粗体',     msg.querySelector('strong'), msg, 4.5);
      add('斜体',     msg.querySelector('em'), msg, 4.5);
      add('链接',     msg.querySelector('a'), msg, 4.5);
      add('行内代码', msg.querySelector('p code'), msg, 4.5);
      add('列表项',   msg.querySelector('li'), msg, 4.5);
      add('引用块',   msg.querySelector('blockquote'), msg, 3.0);
      add('表格正文', msg.querySelector('td'), msg, 4.5);
      // 「代码底文字」要量**没有语法着色的普通行**（比如结尾的「};」），
      // 直接量 pre 拿到的是 pre 自身的 color，而 pre 没设颜色 ——
      // 那是个假数字（实测会报 1.08:1 而实际文字是继承正文色的）。
      // 这里造一个不带 class 的纯文本行来量。
      if (codeEl) {
        var plain = codeEl.querySelector('pre');
        if (plain) {
          var probe = document.createElement('span');
          probe.textContent = 'plain';
          plain.appendChild(probe);
          add('代码内纯文本', probe, codeEl, 4.5);
          probe.remove();
        }
      }
      add('语法-关键字', msg.querySelector('.tk-kw'), codeEl, 4.5);
      add('语法-字符串', msg.querySelector('.tk-st'), codeEl, 4.5);
      add('语法-数字', msg.querySelector('.tk-nu'), codeEl, 4.5);
      add('语法-注释', msg.querySelector('.tk-cm'), codeEl, 3.0);
      // 代码块底色与页面底的关系（应是「一档差」，不是突变）
      if(codeEl){
        var d = (lum(codeBg)-lum(bodyBg))*100;
        out.push((Math.abs(d)<=25?'OK   ':'BAD  ')+'代码块亮度差 '+d.toFixed(1)+'%');
      }
      finish();
    }
    function finish(){
      var p=document.createElement('pre'); p.id='RESULT';
      p.textContent=out.join('\\n');
      document.body.appendChild(p);
    }
  });
})();
`;

function build(bg, fg, side) {
  const vars = `:root{
  --vscode-editor-background:${bg}; --vscode-editor-foreground:${fg};
  --vscode-sideBar-background:${side}; --vscode-editorWidget-background:${side};
  --vscode-list-hoverBackground:${side}; --vscode-panel-border:${side};
  --vscode-textLink-foreground:${fg}; --vscode-charts-green:${fg};
  --vscode-errorForeground:${fg}; --vscode-chat-requestBackground:${side};
  --vscode-button-secondaryBackground:${side}; --vscode-button-secondaryForeground:${fg};
  --vscode-button-background:${fg}; --vscode-button-foreground:${bg};
  --vscode-icon-foreground:${fg}; --vscode-focusBorder:${fg};
  --vscode-descriptionForeground:${fg}; --vscode-editorWidget-border:${side};
  --vscode-input-background:${side};
  --vscode-editorLineNumber-foreground:${fg};
  --vscode-gitDecoration-addedResourceForeground:${fg};
  --vscode-gitDecoration-deletedResourceForeground:${fg};
}`;
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>scan</title>
<style>${vars}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>var THEME=${JSON.stringify(theme)};window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;
}

const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

let totalBad = 0;
const summary = [];

for (const [name, bg, fg, side] of THEMES) {
  const tag = bg.slice(1);
  const dir = path.join(os.tmpdir(), 'scan-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, build(bg, fg, side), 'utf8');
  const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=14000', '--dump-dom',
    'file:///' + file.replace(/\\/g, '/')],
  { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
  const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&') : 'MISS 无结果';
  const bads = text.split('\n').filter((l) => /^(BAD|MISS)/.test(l));
  totalBad += bads.length;
  console.log('\n===== ' + name + ' =====');
  console.log(text);
  summary.push({ name, bg, bad: bads.length, bads });
  fs.rmSync(dir, { recursive: true, force: true });
}

console.log('\n\n################ 汇总 ################');
for (const s of summary) {
  console.log((s.bad === 0 ? '全部达标  ' : ('不达标 ' + s.bad + ' 项  ')) + s.name);
  for (const b of s.bads) console.log('        ' + b);
}
console.log('\n不达标总数 = ' + totalBad);
