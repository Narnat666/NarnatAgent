/**
 * 目视核对：被打断后有标记（用户报障的两个场景）。
 *   1. 探索分支：回答中新建分支 → 分支会话里出现「■ 已停止上一轮」
 *   2. 另存为新会话：回答中点保存 → 保存后出现「■ 已停止上一轮」
 * 走真实点击路径（抽屉底部按钮 / 右键菜单 → 保存为…），不手造事件。
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
        { name: '接口文档模板', timestamp: 2 }
      ] }
    ] } });
    await sleep(250);
  }
  function msgs(tag){
    return [{ role: 'user', text: '帮我看下 ' + tag },
            { role: 'assistant', text: '这是 ' + tag + ' 的历史内容。' }];
  }
`;

const SCENES = {
  /* 场景 1：探索分支 —— 回答中建分支，进入后应有打断标记 */
  branch: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: '我正在改这几处 UI，稍等一下。' } });
    await sleep(150);
    /* 真实顺序：后端先 cancel（turn/end），再回 explore 回执 */
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(120);
    emit({ type: 'session/result', params: { action: 'sessionExplore',
      current: 'Narnat 插件/UI 精修', messages: msgs('分支') } });
    await sleep(600);
    document.title = 'shot-ok';
  });
})();`,
  /* 场景 2：另存为新会话 —— 回答中点保存，保存后应有打断标记 */
  save: `
(function(){
  ${BOOT}
  window.addEventListener('load', async function(){
    await boot();
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: '我正在写这段实现。' } });
    await sleep(150);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(120);
    emit({ type: 'session/saved', params: { name: '另存的名字', switched: true,
      current: '另存的名字' } });
    await sleep(600);
    document.title = 'shot-ok';
  });
})();`
};

const dir = path.join(os.tmpdir(), 'narnat-shot-cut');
fs.mkdirSync(dir, { recursive: true });
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

[['branch', 'cut-branch.png'], ['save', 'cut-save.png']].forEach(([key, name], i) => {
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-cut</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENES[key]}</script></body></html>`;
  const file = path.join(dir, 't' + i + '.html');
  fs.writeFileSync(file, page, 'utf8');
  const out = path.join(shots, name);
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p' + i), '--virtual-time-budget=12000',
    '--window-size=760,520', '--hide-scrollbars', '--screenshot=' + out,
    'file:///' + file.replace(/\\/g, '/')],
  { stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
  console.log(out);
});
