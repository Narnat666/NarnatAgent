/**
 * 目视核对：按真实 narnat.json 配色渲染一屏 markdown，导出 PNG。
 * 无断言，只产出图片路径（配色最终要人眼过一遍）。
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

// 真实主题：由后端 theme 快照生成（读真配置，不手抄）
const py = 'import json,sys;sys.path.insert(0,".");'
  + 'from narnat_agent.config.loader import normalize_ui_raw;'
  + 'from narnat_agent.ui.theme import snapshot;'
  + 'cfg=json.load(open(".narnat/config/narnat.json",encoding="utf-8-sig"));'
  + 'print(json.dumps(snapshot(normalize_ui_raw(cfg.get("界面") or {})),ensure_ascii=False))';
const THEME = execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8', timeout: 60000 }).trim();

const MD = [
  '# 一、结论先说',
  '',
  '插件现在按 `narnat.json` 的「界面」分组着色了：**标题走酒红**、*斜体走次文字*、~~删除线走红~~，',
  '行内代码像 `session/switch` 这样是琥珀色，链接 [dsh 源码](https://x.y) 走冷蓝。',
  '',
  '> 引用块用紫调，和正文拉开一档。',
  '',
  '---',
  '',
  '- 无序列表标记是绿',
  '- 第二条',
  '',
  '1. 有序列表标记是次文字',
  '2. 第二条',
  '',
  '### 三、代码块按语言上色',
  '',
  '```python',
  'def snapshot(raw_ui):',
  '    return _parse(raw_ui)',
  '```',
  '',
  '```bash',
  'node scripts/sync-ui.js && npm run check',
  '```',
  '',
  '```json',
  '{"界面": {"标注": {"标题1": "bold 红"}}}',
  '```',
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
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: window.__MD } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(800);
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-theme</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};window.__MD=${JSON.stringify(MD)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shot-theme');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'theme-colors.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=12000',
  '--window-size=760,1100', '--hide-scrollbars', '--screenshot=' + out,
  'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
console.log(out);
