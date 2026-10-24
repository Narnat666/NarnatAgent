/**
 * 目视核对：把真实渲染的一屏对话截成 PNG，人工看图确认「每行有语义图标、
 * hover 才出 chevron」「折叠组 chevron 在右」。
 *
 * 这个脚本不做断言，只产出图片路径（视觉类改动最终要人眼过一遍）。
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

const SCENE = String.raw`
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\桌面\\narnat\\NarnatAgent' } });
    await sleep(60);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '先确认目录结构，再定位渲染入口。' } });
    await sleep(20);
    emit({ type: 'thinking/end', params: {} });
    var seq = [
      ['Read', '读取', 'E:\\桌面\\narnat\\NarnatAgent\\vscode-extension\\design\\ui-full.html', '第一行\n第二行'],
      ['Edit', '编辑', 'E:\\桌面\\narnat\\NarnatAgent\\vscode-extension\\design\\ui-full.html', '[已编辑: ui-full.html]'],
      ['Grep', '搜索内容', 'tpHasError|tpTitle|tpErrorCount', 'a:1:foo'],
      ['Shell', '执行命令', 'cd /d E:\\桌面\\narnat\\NarnatAgent\\vscode-extension && node scripts/sync-ui.js', 'ok'],
      ['Write', '写入', 'E:\\桌面\\narnat\\NarnatAgent\\narnat_agent\\verify.js', '[已写入: verify.js (912字节)]'],
      ['WebSearch', '联网搜索', 'narnat agent ui', '结果一\n结果二']
    ];
    for (var i = 0; i < seq.length; i++) {
      var r = seq[i];
      emit({ type: 'tool/call', params: { id: 'c' + i, name: r[0], label: r[1],
        summary: r[2], arguments: { path: r[2], command: r[2] } } });
      await sleep(15);
      emit({ type: 'tool/result', params: { id: 'c' + i, ok: true, durationMs: 7, output: r[3] } });
      await sleep(15);
    }
    emit({ type: 'stream/delta', params: { text: '六类工具都跑过了，结论如下。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(700);
    // 展开折叠组：截图要看的是里面那一列工具行的语义图标
    var tp = convo().querySelector('.turn-proc .tp-row');
    if (tp) { tp.click(); }
    await sleep(600);
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-visual</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shot');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'shot-visual.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'visual-icons.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=12000',
  '--window-size=760,900', '--hide-scrollbars',
  '--screenshot=' + out, 'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 120000 });
console.log(fs.existsSync(out) ? out : '(截图失败)');
