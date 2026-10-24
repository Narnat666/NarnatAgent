/**
 * 诊断：用**真实会话**的历史 payload 渲染一屏并截图 + 断言思考块数量。
 * 目的：确认「历史里思考又没了」是数据问题、渲染问题还是折叠收起问题。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const REPO = path.resolve(ROOT, '..');
const NAME = process.argv[2] || 'Narnat插件重装与后端路径修正';

// 1. 起真实 --rpc 拿 session/switch 的 messages（与界面收到的一模一样）
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hist-shot-'));
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
  try { m = JSON.parse(s); } catch (e) { continue; }
  if (m.id === 2 && m.result) payload = m.result;
}
if (!payload) { console.log('拿不到 session/switch 回执'); process.exit(2); }

const msgs = payload.messages || [];
const thinkCount = msgs.filter((m) => typeof m.thinking === 'string' && m.thinking.trim()).length;
const thinkOnly = msgs.filter((m) => typeof m.thinking === 'string' && m.thinking.trim()
  && !String(m.text || '').trim()).length;
console.log('会话=' + NAME + ' 条目=' + msgs.length +
  ' hasMore=' + payload.historyHasMore + ' total=' + payload.historyTotal +
  ' 带思考=' + thinkCount + ' 仅思考=' + thinkOnly);

// 2. 渲染并截图
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const SCENE = `
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  // base64 内联：payload 里任何一条工具输出含脚本闭合标签字面量都会提前
  // 闭合 script 元素，后半段脚本静默不执行（诊断一直报「无」的根因）。
  var P = JSON.parse(decodeURIComponent(escape(atob('${Buffer.from(JSON.stringify({
    current: payload.current, messages: msgs,
    historyHasMore: payload.historyHasMore, historyBefore: payload.historyBefore,
    historyTotal: payload.historyTotal,
  })).toString('base64')}'))));
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\桌面\\\\narnat\\\\NarnatAgent' } });
    await sleep(80);
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: P.current, messages: P.messages,
      historyHasMore: P.historyHasMore, historyBefore: P.historyBefore,
      historyTotal: P.historyTotal } });
    await sleep(1500);
    // 把每个折叠组都展开，才能看到组里的思考块
    var rows = [].slice.call(convo().querySelectorAll('.turn-proc .tp-row'));
    rows.forEach(function(r){ r.click(); });
    await sleep(1200);
    var thinkEls = convo().querySelectorAll('.think');
    var opened = 0;
    [].slice.call(thinkEls).forEach(function(t){
      var r = t.querySelector('.row');
      if (r && r.getAttribute('data-open') !== 'true') { r.click(); opened++; }
    });
    await sleep(800);
    var pre = document.createElement('pre');
    pre.id = 'DIAG';
    pre.textContent = JSON.stringify({
      procs: convo().querySelectorAll('.turn-proc').length,
      thinks: thinkEls.length,
      thinkNames: [].slice.call(thinkEls).slice(0, 5).map(function(t){
        return (t.querySelector('.name') || {}).textContent || ''; }),
      msgCount: convo().querySelectorAll('.msg').length,
      toolRows: convo().querySelectorAll('.tools').length,
      convoTop: Math.round(convo().scrollTop)
    });
    document.body.appendChild(pre);
  }
  window.addEventListener('load', function(){
    run().catch(function(err){
      var pre = document.createElement('pre');
      pre.id = 'ERR';
      pre.textContent = 'run 抛错: ' + (err && err.stack ? err.stack : err);
      document.body.appendChild(pre);
    });
  });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>hist-shot</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-histshot');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'hist.html');
fs.writeFileSync(file, page, 'utf8');
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const out = path.join(shots, 'history-think.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
// 先只 dump-dom 拿诊断（--screenshot 与 --dump-dom 同时给时 dump 会拿不到，
// 实测输出为空），再单独跑一次截图。
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p1'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 128 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'], timeout: 180000 });
const m = /<pre id="DIAG">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
console.log('页面诊断 ' + (m ? m[1].replace(/&quot;/g, '"') : '(无)'));
if (e) console.log('页面抛错 ' + e[1].slice(0, 400));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p2'), '--virtual-time-budget=15000',
  '--window-size=760,1000', '--hide-scrollbars', '--screenshot=' + out,
  'file:///' + file.replace(/\\/g, '/')],
{ stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
console.log('截图 ' + out);
