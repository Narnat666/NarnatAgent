/**
 * 目视核对这一轮的两处修复：
 *   1. 折叠行的耗时（「3 秒」）应在行右侧，不夹在中间
 *   2. Markdown 表格的几何照 dsh（0.5px 下边线、表头 500 字重、无底色），
 *      文字色走 narnat.json 的「标注.表格边框 / 表格内容」
 * 无断言，产出 PNG 给人看。
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

const MD = [
  '表格渲染照 dsh：无竖线、无外框、无底色，只留每行一条 0.5px 下边线。',
  '',
  '| 参数 | 类型 | 必填 | 说明 |',
  '| --- | --- | --- | --- |',
  '| id | int | 是 | 用户 ID |',
  '| name | string | 否 | 用户名 |',
  '| 很长的路径 | string | 否 | `E:\\桌面\\narnat\\NarnatAgent\\vscode-extension` |',
  '',
  '行内代码 `session/switch` 与**粗体**、*斜体*、[链接](https://x.y)。',
  ''
].join('\n');

const SCENE = `
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent',
      theme: window.__THEME } });
    await sleep(150);
    // 一轮：思考 + 工具 + 正文，turn/end 后才建折叠组（带耗时）
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '先确认表格渲染参数。' } });
    await sleep(20);
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'c1', name: 'Read', label: '读取',
      summary: 'ui-full.html', arguments: { path: 'ui-full.html' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'c1', ok: true, durationMs: 7, output: 'ok' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: window.__MD } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(900);
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-table</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};window.__MD=${JSON.stringify(MD)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shot-table');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'table-and-duration.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=12000',
  '--window-size=760,760', '--hide-scrollbars', '--screenshot=' + out,
  'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
console.log(out);
