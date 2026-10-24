/**
 * 目视核对：/skill、/ls、/plugin 三处的候选面板（用户报障的场景）。
 * 一次出三张 PNG，各自对应一条命令的参数补全。
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

function scene(text) {
  return `
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 },
      plugins: [
        { name: 'Terminal', label: '终端', enabled: true },
        { name: 'WebSearch', label: '联网搜索', enabled: false },
        { name: 'Serial', label: '串口', enabled: true },
        { name: 'MCP', label: 'MCP', enabled: true }
      ],
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
    await sleep(200);
    emit({ type: 'skill/result', params: { items: [
      { v: '代码审查', d: '系统技能', dir: false },
      { v: '写作规范', d: '系统技能', dir: false },
      { v: '工具-画图.md', d: '项目技能', dir: false },
      { v: '工具-识图.md', d: '项目技能', dir: false }
    ] } });
    await sleep(150);
    var el = document.getElementById('input');
    el.value = ${JSON.stringify(text)};
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(400);
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;
}

const dir = path.join(os.tmpdir(), 'narnat-shot-pal');
fs.mkdirSync(dir, { recursive: true });
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

[['/skill ', 'palette-skill.png'],
 ['/ls ', 'palette-ls.png'],
 ['/plugin ', 'palette-plugin.png']].forEach(([text, name], i) => {
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-pal</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${scene(text)}</script></body></html>`;
  const file = path.join(dir, 't' + i + '.html');
  fs.writeFileSync(file, page, 'utf8');
  const out = path.join(shots, name);
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p' + i), '--virtual-time-budget=12000',
    '--window-size=760,560', '--hide-scrollbars', '--screenshot=' + out,
    'file:///' + file.replace(/\\/g, '/')],
  { stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
  console.log(out);
});
