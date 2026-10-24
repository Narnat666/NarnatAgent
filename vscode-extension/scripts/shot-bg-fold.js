/**
 * 目视核对：后台任务提交行是否收进折叠组（用户报障截图同一场景）。
 * 产出两张 PNG：收起态（一眼看该行还在不在）+ 展开态（确认它在组内、顺序正确）。
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

const SCENE_EXPAND = `
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent',
      theme: window.__THEME } });
    await sleep(150);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '跑一遍工具自检，先用后台跑长命令。' } });
    await sleep(20);
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 't1', name: 'Shell', label: '执行命令',
      summary: 'npm test', arguments: { command: 'npm test' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 't1', ok: true, durationMs: 900, output: 'bg1' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 't2', name: 'Read', label: '读取',
      summary: 'a.md', arguments: { path: 'a.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 't2', ok: true, durationMs: 3, output: 'ok' } });
    await sleep(20);
    emit({ type: 'bg/submit', params: { slot: 'bg1',
      logPath: 'C:\\\\Users\\\\86153\\\\AppData\\\\Local\\\\Temp\\\\narnat_bg_16508_hbo6mfq1\\\\bg1.log' } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: 'Shell、Glob 工具都正常：命令执行、文件检索都能用。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(900);
    if (window.__EXPAND) {
      var row = document.querySelector('.turn-proc .tp-row');
      if (row) row.click();
      await sleep(500);
    }
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

function buildPage(expand) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-bg-fold</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};window.__EXPAND=${expand ? 'true' : 'false'};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE_EXPAND}</script></body></html>`;
}

const dir = path.join(os.tmpdir(), 'narnat-shot-bgfold');
fs.mkdirSync(dir, { recursive: true });
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

[['bg-fold-collapsed.png', false], ['bg-fold-open.png', true]].forEach(([name, exp], i) => {
  const file = path.join(dir, 't' + i + '.html');
  fs.writeFileSync(file, buildPage(exp), 'utf8');
  const out = path.join(shots, name);
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p' + i), '--virtual-time-budget=15000',
    '--window-size=760,620', '--hide-scrollbars', '--screenshot=' + out,
    'file:///' + file.replace(/\\/g, '/')],
  { stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
  console.log(out);
});
