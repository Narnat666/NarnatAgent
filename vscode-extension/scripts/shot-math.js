/**
 * 目视核对：数学公式在真实渲染下长什么样（与用户给的 dsh 截图对照）。
 *
 * 场景刻意复刻截图里的内容：数学公式示例 / 基础微积分 / 泰勒展开与级数 /
 * 线性代数，块级 $$ 与行内 $ 都有。出图后人工看。
 *
 * 用法：node scripts/shot-math.js
 * 产物：scripts/shots/math-dark.png、math-light.png
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
const KCSS = fs.readFileSync(path.join(ROOT, 'media', 'katex', 'katex.min.css'), 'utf8');
const KJS = fs.readFileSync(path.join(ROOT, 'media', 'katex', 'katex.min.js'), 'utf8');

const DARK_VARS = `
:root{
  --vscode-editor-background:#1f1f1f;
  --vscode-editor-foreground:#cccccc;
  --vscode-sideBar-background:#181818;
  --vscode-editorWidget-background:#202020;
  --vscode-list-hoverBackground:#2a2d2e;
  --vscode-panel-border:#2b2b2b;
  --vscode-textLink-foreground:#4daafc;
  --vscode-charts-green:#89d185;
  --vscode-errorForeground:#f14c4c;
  --vscode-chat-requestBackground:#1f1f1f;
  --vscode-button-secondaryBackground:#313131;
  --vscode-focusBorder:#0078d4;
  --vscode-descriptionForeground:#9d9d9d;
}
`;
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
  --vscode-focusBorder:#0090f1;
  --vscode-descriptionForeground:#717171;
}
`;

// 复刻截图内容：标题 + 块级公式 + 行内公式
const MD = [
  '## 数学公式示例',
  '',
  '### 基础微积分',
  '',
  '$$',
  '\\frac{d}{dx}\\sin x = \\cos x, \\quad \\int_0^\\infty e^{-x^2}\\,dx = \\frac{\\sqrt{\\pi}}{2}',
  '$$',
  '',
  '$$',
  '\\lim_{x \\to 0}\\frac{\\sin x}{x} = 1, \\quad \\lim_{n\\to\\infty}\\left(1+\\frac{1}{n}\\right)^n = e',
  '$$',
  '',
  '### 泰勒展开与级数',
  '',
  '$$',
  'e^x = \\sum_{n=0}^{\\infty}\\frac{x^n}{n!}, \\quad \\frac{1}{1-x} = \\sum_{n=0}^{\\infty}x^n \\quad (|x|<1)',
  '$$',
  '',
  '$$',
  '\\zeta(s) = \\sum_{n=1}^{\\infty}\\frac{1}{n^s}, \\quad \\zeta(2) = \\frac{\\pi^2}{6}',
  '$$',
  '',
  '### 线性代数',
  '',
  '$$',
  '\\det(A - \\lambda I) = 0',
  '$$',
  '',
  '行内的写法也能用：$E = mc^2$ 与 $\\frac{1}{5}$，还有一段中文夹在中间不影响排版。',
  '',
  '对照不该变成公式的：价格 $5 到 $10 元。'
].join('\n');

const SCENE = `
(function(){
  var MD = ${JSON.stringify(MD)};
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\x' } });
    await sleep(200);
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '数学公式',
      isChild: false, messages: [
        { role: 'user', text: '输出一些数学公式' },
        { role: 'assistant', text: MD }
      ] } });
    await sleep(1600);
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

function build(vars) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-math</title>
<style>${KCSS}</style><style>${vars}</style><style>${CSS}</style></head>
<body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${KJS}</script><script>${JS}</script><script>${SCENE}</script></body></html>`;
}

const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

function shoot(vars, tag) {
  const dir = path.join(os.tmpdir(), 'shot-math-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  // KaTeX 字体相对路径：复制到页面旁边
  const fsrc = path.join(ROOT, 'media', 'katex', 'fonts');
  const fdst = path.join(dir, 'fonts');
  if (fs.existsSync(fsrc)) {
    fs.mkdirSync(fdst, { recursive: true });
    for (const f of fs.readdirSync(fsrc)) {
      if (f.endsWith('.woff2')) fs.copyFileSync(path.join(fsrc, f), path.join(fdst, f));
    }
  }
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, build(vars), 'utf8');
  const png = path.join(OUT, 'math-' + tag + '.png');
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=12000',
    '--window-size=760,1250', '--screenshot=' + png,
    'file:///' + file.replace(/\\/g, '/')], { stdio: 'ignore' });
  console.log('SHOT ' + png);
  fs.rmSync(dir, { recursive: true, force: true });
}

shoot(DARK_VARS, 'dark');
shoot(LIGHT_VARS, 'light');
