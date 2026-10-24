/**
 * 目视核对这一轮的三处改动：
 *   1. 对话记录 / 设置的 × 加大（palette-close 两态）
 *   2. 正在回答中切会话时的二次确认框
 *   3. 用户气泡新底色（与改动前并排对比）
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

const BOOT = `
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: 'Narnat 插件', messages: [] },
      workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent', theme: window.__THEME } });
    await sleep(200);
    emit({ type: 'session/list', params: { current: 'Narnat 插件', groups: [
      { label: '今天', sessions: [
        { name: 'Narnat 插件', timestamp: 3, children: [{ name: 'UI 精修' }] },
        { name: '接口文档模板', timestamp: 2 },
        { name: '星露谷 AI 对话卡顿修复', timestamp: 1 }
      ] }
    ] } });
    await sleep(250);
  }
`;

const SCENES = {
  /* 对话记录抽屉：看右上角 × */
  drawer: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    document.getElementById('histBtn').click();
    await sleep(400);
    document.title = 'shot-ok';
  });
})();`,
  /* 设置面板：看右上角 × */
  settings: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    document.getElementById('btnSettings').click();
    await sleep(600);
    document.title = 'shot-ok';
  });
})();`,
  /* 二次确认框 */
  ask: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(40);
    emit({ type: 'stream/delta', params: { text: '我正在查这几处代码，稍等一下。' } });
    await sleep(150);
    document.getElementById('histBtn').click();
    await sleep(300);
    var rows = [].slice.call(document.querySelectorAll('#sessList .dw-item'));
    var row = rows.filter(function(r){ return r.textContent.indexOf('接口文档模板') >= 0; })[0];
    if (row) row.click();
    await sleep(500);
    document.title = 'shot-ok';
  });
})();`,
  /* 用户气泡：新旧并排 */
  bubble: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'Narnat 插件',
      messages: [
        { role: 'user', text: '现在（掺强调色）：这块是我说的话' },
        { role: 'assistant', text: '这是 AI 的回答，没有底色。' },
        { role: 'user', text: '改动前的样子（主题纯中性灰）：这块是我说的话' },
        { role: 'assistant', text: '对比上面那个气泡，看哪块更容易一眼认出。' }
      ] } });
    await sleep(700);
    var users = [].slice.call(document.querySelectorAll('.msg[data-role="user"] .text'));
    // 第二个气泡强制退回旧配方（One Dark 的 chat.requestBackground = #2c313c），
    // 做成同屏对比。不能用 var(--vscode-chat-requestBackground) —— headless
    // 里那个变量不存在，会退回页面底，看起来像「没有气泡」。
    if (users[1]) users[1].style.background = '#2c313c';
    await sleep(200);
    document.title = 'shot-ok';
  });
})();`
};

const dir = path.join(os.tmpdir(), 'narnat-shot-ask');
fs.mkdirSync(dir, { recursive: true });
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

const JOBS = [
  ['drawer', 'close-drawer.png', 760, 520],
  ['settings', 'close-settings.png', 760, 520],
  ['ask', 'interrupt-ask.png', 760, 560],
  ['bubble', 'user-bubble.png', 760, 520]
];

JOBS.forEach(([key, name, w, h], i) => {
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-ask</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENES[key]}</script></body></html>`;
  const file = path.join(dir, 't' + i + '.html');
  fs.writeFileSync(file, page, 'utf8');
  const out = path.join(shots, name);
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p' + i), '--virtual-time-budget=12000',
    '--window-size=' + w + ',' + h, '--hide-scrollbars', '--screenshot=' + out,
    'file:///' + file.replace(/\\/g, '/')],
  { stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
  console.log(out);
});
