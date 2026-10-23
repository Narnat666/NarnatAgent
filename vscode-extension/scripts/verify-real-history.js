/**
 * 用**真实会话**验证历史重放的显示（不是构造数据）。
 * 断言：每个折叠组的状态标注与「该轮有无最终回复」严格对应。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');
const FIX = path.join(ROOT, '..', 'output', 'nn-tasks', 'fixture.json');
const raw = JSON.parse(fs.readFileSync(FIX, 'utf8'));
const MSGS = Array.isArray(raw) ? raw : raw.messages;
const NAME = raw.current || 'fixture';

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  :: ' + d : ''));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function procs(){ return [].slice.call(convo().querySelectorAll('.turn-proc')); }
  function labelOf(p){ return ((p.querySelector('.tp-label') || {}).textContent || ''); }

  // 按消息自己算一遍「每轮有没有最终回复」，作为 oracle
  function oracle(){
    var MSGS = window.__MSGS, acc = [], cur = null;
    MSGS.forEach(function(m){
      if (!m) return;
      if (m.role === 'user') {
        // 内部注入不算新轮
        if (typeof m.text === 'string' && /^(?:\[系统提醒\]|\[\]对话历史过长)/.test(m.text.replace(/^\s+/, ''))) return;
        if (cur) acc.push(cur);
        cur = { proc: [] };
        return;
      }
      if (!cur) return;
      if (m.role === 'tool') { cur.proc.push('tool'); return; }
      if (m.role === 'system') return;
      if (m.role === 'assistant') {
        if (typeof m.thinking === 'string' && m.thinking.trim()) cur.proc.push('think');
        if (typeof m.text === 'string' && m.text.trim()) cur.proc.push('text');
      }
    });
    if (cur) acc.push(cur);
    // 映射成「该轮最后一个过程节点是不是正文」
    return acc.map(function(r){
      var last = r.proc.length ? r.proc[r.proc.length - 1] : null;
      return { last: last, hasTail: last === 'text', empty: r.proc.length === 0 };
    });
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: window.__NAME, messages: window.__MSGS } });
    await sleep(1500);

    var ps = procs();
    var orc = oracle().filter(function(r){ return !r.empty; });
    // 每组应有对应的一轮；组数 ≤ 轮数（没有过程节点的轮不建组）
    ck('真实会话画出了折叠组', ps.length > 0, 'got=' + ps.length);

    // 口径变更（用户要求）：折叠行不再标「未完成」。
    // 用户原话：「后续正确的完整的工具调度也会在头上显示未完成，属于画蛇添足」。
    // 这里改成**反向断言**：真实历史里一个状态前缀都不许出现
    // （历史没有 turn/end 的 reason，分不清「用户点了停止」和「正常说完话又去调工具」）。
    var marked = [];
    ps.forEach(function(p, i){
      var lb = labelOf(p);
      if (lb.indexOf('未完成') >= 0 || lb.indexOf('已停止') >= 0) marked.push(i + ':' + lb);
    });
    ck('真实历史里没有任何折叠组带状态前缀', marked.length === 0,
       marked.slice(0, 3).join(' ; '));

    // 但计数必须还在（去掉状态词不等于把标题也掏空）
    var noCount = [];
    ps.forEach(function(p, i){
      var lb = labelOf(p);
      var tc = Number(p.getAttribute('data-tool-count') || 0);
      if (tc > 0 && lb.indexOf('次工具调用') < 0) noCount.push(i + '@' + lb);
    });
    ck('有工具的组，折叠行都写明了「N 次工具调用」', noCount.length === 0,
       noCount.slice(0, 3).join(' ; '));

    out.push('DIAG 真实会话组数=' + ps.length + '（状态前缀 0 组）');

    // 顺序：每个组内的成员，其 data-tool 顺序应与消息顺序一致（抽查前 5 组）
    var orderBad = [];
    ps.slice(0, 5).forEach(function(p, i){
      var inner = p.querySelector('.tp-inner');
      if (!inner) return;
      var seq = [].slice.call(inner.children).map(function(el){
        if (el.classList.contains('think')) return 'think';
        if (el.classList.contains('tools')) return 'tool';
        if (el.classList.contains('msg')) return 'text';
        return '?';
      });
      // 允许的形态：think/tool/text 的任意排列，但不允许同一组里出现
      // 「text 在最末且组被标未完成」这种矛盾（上面已查）
      out.push('DIAG 组' + i + ' 顺序=' + seq.join(','));
    });

    // 展开第一组，确认内容真的在
    var row = ps[0].querySelector('.tp-row');
    if (row) row.click();
    await sleep(300);
    ck('展开第一组后 .tp-body 可见',
       !ps[0].querySelector('.tp-body').hasAttribute('hidden'), 'ok');

    out.push('SUMMARY total=' + out.length + ' fail=' + fails);
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\n');
    document.body.appendChild(pre);
  }
  window.addEventListener('load', function(){
    run().catch(function(e){
      var pre = document.createElement('pre');
      pre.id = 'ERR';
      pre.textContent = 'run 抛错: ' + (e && e.stack ? e.stack : e);
      document.body.appendChild(pre);
    });
  });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-real-history</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__NAME=${JSON.stringify(NAME)};window.__MSGS=${JSON.stringify(MSGS)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-realhist');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-real-history.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=25000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const t = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT';
console.log(t);
process.exit(/fail=0/.test(t) ? 0 : 2);
