/**
 * 用真实会话 payload 分别渲染两个会话并截图，比对「颜色是否一致」。
 * 目的：判断用户看到的两张图差异，是「渲染规则不同（bug）」还是
 *      「内容里可着色元素数量不同（正常）」。
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

const pyTheme = 'import json,sys;sys.path.insert(0,".");'
  + 'from narnat_agent.config.loader import normalize_ui_raw;'
  + 'from narnat_agent.ui.theme import snapshot;'
  + 'cfg=json.load(open(".narnat/config/narnat.json",encoding="utf-8-sig"));'
  + 'print(json.dumps(snapshot(normalize_ui_raw(cfg.get("界面") or {})),ensure_ascii=False))';
const THEME = execFileSync('python', ['-c', pyTheme], { cwd: REPO, encoding: 'utf8' }).trim();

function payload(name) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'cmp-'));
  const lines = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }) + '\n'
    + JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'session/switch',
      params: { name, historyLimit: 500, historyMaxItems: 2000 } }) + '\n';
  const res = execFileSync('python', ['-u', 'main.py', '--rpc'], {
    input: lines, encoding: 'utf8', timeout: 180000, cwd: REPO,
    env: { ...process.env, NARNAT_HOME: home }, maxBuffer: 128 * 1024 * 1024,
  });
  fs.rmSync(home, { recursive: true, force: true });
  let out = null;
  for (const raw of String(res).split('\n')) {
    const s = raw.trim();
    if (!s.startsWith('{')) continue;
    let m;
    try { m = JSON.parse(s); } catch (e) { continue; }
    if (m.id === 2 && m.result) out = m.result;
  }
  return out;
}

const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

function render(name, fileTag) {
  const p = payload(name);
  if (!p) { console.log('拿不到会话: ' + name); return; }
  const msgs = p.messages || [];
  const stat = {
    headings: 0, bold: 0, inlineCode: 0, cells: 0, fences: 0,
  };
  msgs.forEach((m) => {
    const t = String(m.text || '');
    stat.headings += (t.match(/^#{1,6}\s+/gm) || []).length;
    stat.bold += (t.match(/\*\*[^*\n]+\*\*/g) || []).length;
    stat.inlineCode += (t.match(/`[^`\n]+`/g) || []).length;
    stat.cells += (t.match(/^\|.*\|/gm) || []).length;
    stat.fences += (t.match(/```/g) || []).length;
  });
  console.log(name + ' → 条目=' + msgs.length + ' hasMore=' + p.historyHasMore
    + ' 标题=' + stat.headings + ' 粗体=' + stat.bold
    + ' 行内代码=' + stat.inlineCode + ' 表格行=' + stat.cells + ' 围栏=' + stat.fences);

  const SCENE = `
  (function(){
    function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
    function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
    var P = JSON.parse(decodeURIComponent(escape(atob('${Buffer.from(JSON.stringify({
      current: p.current, messages: msgs,
      historyHasMore: p.historyHasMore, historyBefore: p.historyBefore,
      historyTotal: p.historyTotal,
    })).toString('base64')}'))));
    async function run(){
      emit({ type: 'initialize', params: { version: 't', protocol: 1,
        model: { current: 'm', options: ['m'] },
        thinking: { effort: 'high', options: { high: '高' } },
        goal: { enabled: false, maxRounds: 10 }, plugins: [],
        session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent',
        theme: window.__THEME } });
      await sleep(150);
      emit({ type: 'session/result', params: { action: 'sessionSwitch',
        current: P.current, messages: P.messages,
        historyHasMore: P.historyHasMore, historyBefore: P.historyBefore,
        historyTotal: P.historyTotal } });
      await sleep(1500);
      var pre = document.createElement('pre');
      pre.id = 'DIAG';
      pre.textContent = JSON.stringify({
        headings: document.querySelectorAll('#convo h1,#convo h2,#convo h3,#convo h4').length,
        strong: document.querySelectorAll('#convo strong').length,
        code: document.querySelectorAll('#convo code').length,
        td: document.querySelectorAll('#convo td').length
      });
      document.body.appendChild(pre);
    }
    window.addEventListener('load', function(){ run(); });
  })();
  `;
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head>
<body data-run="false">${BODY}<style>${CSS}</style>
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;
  const dir = path.join(os.tmpdir(), 'cmp-' + fileTag);
  fs.mkdirSync(dir, { recursive: true });
  const f = path.join(dir, 'a.html');
  fs.writeFileSync(f, page, 'utf8');
  const out = path.join(shots, 'cmp-' + fileTag + '.png');
  const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
    '--window-size=1130,975', '--hide-scrollbars', '--screenshot=' + out,
    'file:///' + f.replace(/\\/g, '/')],
  { encoding: 'utf8', maxBuffer: 128 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
    timeout: 180000 });
  const m = /<pre id="DIAG">([\s\S]*?)<\/pre>/.exec(dom);
  console.log('   DOM: ' + (m ? m[1] : '(无)') + '  截图: ' + out);
}

render('Narnat Agent 25轮测试与70项修复', 'r25');
render('你好 (6)', 'nihao6');
