/**
 * 截图：被打断后的实际观感（对照 dsh 的显示）。
 * 场景还原用户报障那一轮：思考 → 正文「我来做个工具自检…」→ 工具1 → 工具2 → 打断
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const OUT = process.argv[2] || path.join(ROOT, '..', 'output', 'shot-interrupt.png');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const DRIVE = String.raw`
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    document.querySelector('.shell').style.width = '430px';
    document.querySelector('.shell').style.height = '720px';
    emit({ type: 'initialize', params: { version: 's', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);

    // 用户消息
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    var u = document.createElement('div');
    u.className = 'msg'; u.setAttribute('data-role', 'user');
    u.innerHTML = '<div class="text">简单的试试你的工具好不好用</div>';
    document.getElementById('convo').appendChild(u);
    await sleep(20);

    emit({ type: 'thinking/begin', params: {} });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '用户让我试试工具，我并行跑几个探测。' } });
    await sleep(20);
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我来做个工具自检，并行跑几组探测。' } });
    await sleep(40);
    emit({ type: 'tool/call', params: { id: 'p1', name: 'Glob', label: '搜索文件',
      summary: '*.md', arguments: { pattern: '*.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'p1', ok: true, durationMs: 68, output: 'a.md' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'p2', name: 'Shell', label: '执行命令',
      summary: 'echo === CWD ===', arguments: { command: 'echo === CWD ===' } } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(600);
    // 展开折叠组，让截图能看出组内顺序
    var row = document.querySelector('.turn-proc .tp-row');
    if (row) row.click();
    await sleep(400);
    var c = document.getElementById('convo');
    if (c) c.scrollTop = 0;
    await sleep(80);
    console.log('procs=' + document.querySelectorAll('.turn-proc').length +
                ' label=' + ((document.querySelector('.tp-label')||{}).textContent||''));
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot</title>
<style>${CSS}</style><style>html,body{margin:0;background:#1e1e1e}</style></head>
<body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${DRIVE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shotintr');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'shot.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
fs.mkdirSync(path.dirname(OUT), { recursive: true });
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000',
  '--window-size=430,720', '--screenshot=' + OUT,
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
console.log('PNG ' + OUT + ' ' + fs.statSync(OUT).size + ' bytes');
