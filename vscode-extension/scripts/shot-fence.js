/**
 * 目视核对围栏解析：AI 输出的 3 包 3 markdown 文档整段收在一个块里，
 * 思考块只显示字数（耗时不入库，与 dsh 一致）。
 * 无断言，产出 PNG 给人看。真实文本取自会话 json。
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

const REAL = (() => {
  const dir = path.join(REPO, '.narnat/data/sessions');
  const f = fs.readdirSync(dir).find((n) => n.indexOf('mkdown') >= 0);
  const d = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8').replace(/^\uFEFF/, ''));
  const m = d.messages.filter((x) => x.role === 'assistant' && /```markdown/.test(x.content || ''))[0];
  return m ? m.content : '';
})();

const py = 'import json,sys;sys.path.insert(0,".");'
  + 'from narnat_agent.config.loader import normalize_ui_raw;'
  + 'from narnat_agent.ui.theme import snapshot;'
  + 'cfg=json.load(open(".narnat/config/narnat.json",encoding="utf-8-sig"));'
  + 'print(json.dumps(snapshot(normalize_ui_raw(cfg.get("界面") or {})),ensure_ascii=False))';
const THEME = execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim();

const HIST = [
  { role: 'user', text: '直接输出一个 markdown 格式的文字给我' },
  { role: 'assistant', text: '', thinking: '用户说直接输出。那就直接输出一个 markdown 文档，给一个接口文档模板吧。' },
  { role: 'tool', id: 'c1', name: 'Read', label: '读取', summary: 'ui-full.html', arguments: {}, output: 'ok' },
  { role: 'assistant', text: REAL, thinking: '给一份接口文档的 Markdown，不加解释。' },
];

const SCENE = `
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
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h',
      messages: window.__HIST } });
    await sleep(900);
    var row = document.querySelector('.turn-proc .tp-row');
    if (row) { row.click(); }
    await sleep(400);
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-fence</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};window.__HIST=${JSON.stringify(HIST)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-shot-fence');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'fence-render.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000',
  '--window-size=760,1000', '--hide-scrollbars', '--screenshot=' + out,
  'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
console.log(out);
