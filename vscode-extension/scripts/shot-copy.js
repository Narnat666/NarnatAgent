/**
 * 目视核对：把改过文案的几个弹窗一次拍出来。
 *   1. 三个下拉的脚注（模型 / 思考 / 目标模式 / 插件）
 *   2. 「合并不可撤销」只剩按钮文案（不再多一行系统行）
 *   3. 二次确认框（已简化的描述）
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
      model: { current: 'deepseek-flash', options: ['deepseek-flash', 'deepseek-v4-pro'] },
      thinking: { effort: 'high', options: { high: '高', max: '全开' } },
      goal: { enabled: false, maxRounds: 10 },
      plugins: [
        { name: 'Terminal', label: '终端', enabled: true },
        { name: 'WebSearch', label: '联网搜索', enabled: false },
        { name: 'Serial', label: '串口', enabled: true },
        { name: 'MCP', label: 'MCP', enabled: true }
      ],
      session: { current: 'Narnat 插件', messages: [] },
      workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent', theme: window.__THEME } });
    await sleep(220);
    emit({ type: 'session/list', params: { current: 'Narnat 插件', groups: [
      { label: '今天', sessions: [
        { name: 'Narnat 插件', timestamp: 3, children: [{ name: 'UI 精修' }] },
        { name: '接口文档模板', timestamp: 2 }
      ] }
    ] } });
    await sleep(250);
  }
`;

const SCENES = {
  think: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    document.getElementById('btnThink').click();
    await sleep(400);
    document.title = 'shot-ok';
  });
})();`,
  plugin: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    document.getElementById('btnPlugin').click();
    await sleep(400);
    document.title = 'shot-ok';
  });
})();`,
  goal: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    document.getElementById('btnGoal').click();
    await sleep(400);
    document.title = 'shot-ok';
  });
})();`,
  /* 二次确认（简化后的描述） */
  ask: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(40);
    emit({ type: 'stream/delta', params: { text: '我正在查这几处代码。' } });
    await sleep(150);
    document.getElementById('histBtn').click();
    await sleep(300);
    var rows = [].slice.call(document.querySelectorAll('#sessList .dw-item'));
    var row = rows.filter(function(r){ return r.textContent.indexOf('接口文档模板') >= 0; })[0];
    if (row) row.click();
    await sleep(500);
    document.title = 'shot-ok';
  });
})();`
};

const dir = path.join(os.tmpdir(), 'narnat-shot-copy');
fs.mkdirSync(dir, { recursive: true });
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

[['think', 'pop-think.png', 760, 480],
 ['plugin', 'pop-plugin.png', 760, 480],
 ['goal', 'pop-goal.png', 760, 480],
 ['ask', 'interrupt-ask.png', 760, 560]].forEach(([key, name, w, h], i) => {
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-copy</title>
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
