/**
 * 代码块底色方案对比图（用户报障：「mkdown 文字所在的背景颜色不好看太老土」）。
 *
 * 一屏放 4 段完全相同的 markdown，只有代码块的三个变量不同：
 *   --bg-code      块体底
 *   --bg-code-hd   标题栏底（比块体再亮一档）
 *   --code-border  外框（dsh 无边框）
 *
 * 方案都取自真实主题实测值，不是随手调：
 *   A 面板提亮（新默认）  从 --bg 派生，≈ One Dark 自己的 #2c313a 量级
 *   B 主题 editorWidget   旧行为：#21252b（比面板暗一档 + 1px 灰框）
 *   C Tokyo Night 夜色    #1f2335 / #24283b（偏蓝紫，柔和）
 *   D 中性灰（dsh 口径）  #232327 / #2e2e32（去掉彩度，最干净）
 *
 * 无断言，产出 PNG 人工过一眼。
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

const BODY_1 = [
  '**A · 面板提亮（新默认）**',
  '',
  '```js',
  'function parse(input) {',
  '  const node = next(input);',
  '  return node && node.line;',
  '}',
  '```',
  ''
].join('\n');
const BODY_2 = BODY_1.replace('A · 面板提亮（新默认）', 'B · 主题 editorWidget（旧行为）');
const BODY_3 = BODY_1.replace('A · 面板提亮（新默认）', 'C · Tokyo Night 夜色');
const BODY_4 = BODY_1.replace('A · 面板提亮（新默认）', 'D · 中性灰（dsh 口径）');

const SCENE = `
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function one(i, text){
    emit({ type: 'turn/begin', params: { round: i } });
    await sleep(10);
    emit({ type: 'stream/delta', params: { text: text } });
    await sleep(25);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(60);
  }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent',
      theme: window.__THEME } });
    await sleep(150);
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h',
      messages: [{ role: 'user', text: '同四段 markdown，只有代码块底色不同' }] } });
    await sleep(300);
    await one(1, window.__B1);
    await one(2, window.__B2);
    await one(3, window.__B3);
    await one(4, window.__B4);
    var msgs = [].slice.call(document.querySelectorAll('.msg.assistant'));
    ['vA', 'vB', 'vC', 'vD'].forEach(function(cls, i){
      if (msgs[i]) msgs[i].className += ' ' + cls;
    });
    await sleep(400);
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

// 方案覆盖（CSS 变量在 .msg 上重设，后代 .code 继承）
const VARIANTS = `
.vB{--bg-code:#21252b;--bg-code-hd:#21252b;--code-border:#3e4452}
.vC{--bg-code:#1f2335;--bg-code-hd:#24283b;--code-border:transparent}
.vD{--bg-code:#232327;--bg-code-hd:#2e2e32;--code-border:transparent}
body{padding-bottom:24px}
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-code-bg</title>
<style>${CSS}${VARIANTS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.__B1=${JSON.stringify(BODY_1)};window.__B2=${JSON.stringify(BODY_2)};
window.__B3=${JSON.stringify(BODY_3)};window.__B4=${JSON.stringify(BODY_4)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shot-codebg');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'code-bg-variants.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000',
  '--window-size=760,1150', '--hide-scrollbars', '--screenshot=' + out,
  'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
console.log(out);
