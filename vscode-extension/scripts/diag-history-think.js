/**
 * 最小诊断：真实会话 payload → renderHistory → 直接 dump DOM 数 .think。
 * 不做任何点击（避免动画/rAF 把 virtual-time 卡住）。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const REPO = path.resolve(ROOT, '..');
const NAME = process.argv[2] || 'Narnat插件重装与后端路径修正';

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'thinkdiag-'));
const lines = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }) + '\n' +
  JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'session/switch', params: { name: NAME } }) + '\n';
const res = execFileSync('python', ['-u', 'main.py', '--rpc'], {
  input: lines, encoding: 'utf8', timeout: 180000, cwd: REPO,
  env: { ...process.env, NARNAT_HOME: home }, maxBuffer: 128 * 1024 * 1024,
});
fs.rmSync(home, { recursive: true, force: true });

let payload = null;
for (const raw of String(res).split('\n')) {
  const s = raw.trim();
  if (!s.startsWith('{')) continue;
  let m;
  try { m = JSON.parse(s); } catch (err) { continue; }
  if (m.id === 2 && m.result) payload = m.result;
}
if (!payload) { console.log('拿不到回执'); process.exit(2); }

const msgs = payload.messages || [];
const withThink = msgs.filter((m) => typeof m.thinking === 'string' && m.thinking.trim()).length;
console.log('payload 条目=' + msgs.length + ' 带 thinking 键=' + withThink);

const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const SCENE = `
(function(){
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  // payload 必须 base64 内联：147 条消息里任意一条工具输出只要含有脚本闭合标签
  // 的字面量，直接 JSON 内联就会提前闭合 script 元素 —— 后半段脚本静默不执行、
  // 页面空白（实测踩过，诊断一直报「无」就是这个原因）。
  var P = JSON.parse(decodeURIComponent(escape(atob('${Buffer.from(JSON.stringify({
    current: payload.current, messages: msgs,
    historyHasMore: payload.historyHasMore, historyBefore: payload.historyBefore,
    historyTotal: payload.historyTotal,
  })).toString('base64')}'))));
  function go(){
    try {
      emit({ type: 'initialize', params: { version: 't', protocol: 1,
        model: { current: 'm', options: ['m'] },
        thinking: { effort: 'high', options: { high: '高' } },
        goal: { enabled: false, maxRounds: 10 }, plugins: [],
        session: { current: null }, workdir: 'E:\\\\x' } });
      emit({ type: 'session/result', params: {
        action: 'sessionSwitch', current: P.current, messages: P.messages,
        historyHasMore: P.historyHasMore, historyBefore: P.historyBefore,
        historyTotal: P.historyTotal } });
      var pre = document.createElement('pre');
      pre.id = 'DIAG';
      pre.setAttribute('data-diag', '1');
      var info = {
        procs: convo().querySelectorAll('.turn-proc').length,
        thinks: convo().querySelectorAll('.think').length,
        thinksInProc: convo().querySelectorAll('.turn-proc .think').length,
        tools: convo().querySelectorAll('.tools').length,
        msg: convo().querySelectorAll('.msg').length,
        firstNames: [].slice.call(convo().querySelectorAll('.think .name')).slice(0, 4)
          .map(function(n){ return n.textContent; })
      };
      pre.textContent = JSON.stringify(info);
      document.body.appendChild(pre);
      // title 一定在 dump 里，且不会被内容干扰
      document.title = 'DIAG:' + JSON.stringify(info);
    } catch (e) {
      var pre2 = document.createElement('pre');
      pre2.id = 'ERR';
      pre2.textContent = String(e && e.stack ? e.stack : e);
      document.body.appendChild(pre2);
    }
  }
  // 直接执行（不等 load：headless 的 load 时序在这里不可靠，实测等不到）
  go();
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head>
<body data-run="false">${BODY.replace('<style>', '<style>')}
<style>${CSS}</style>
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'thinkdiag');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=8000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 128 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'], timeout: 120000 });
const title = /<title>([\s\S]*?)<\/title>/.exec(dom);
const diag = title && title[1].indexOf('DIAG:') === 0 ? title[1].slice(5) : '(无)';
console.log('DIAG ' + diag.replace(/&quot;/g, '"'));
const pre = /<pre id="DIAG" data-diag="1">([\s\S]*?)<\/pre>/.exec(dom);
if (pre) console.log('PRE  ' + pre[1].replace(/&quot;/g, '"'));
const err = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (err) console.log('ERR ' + err[1].slice(0, 600));

