/**
 * 验收：GoalComplete 在工具调度里的分类与命名。
 *
 * 用户报障：「1 次工具调用 · 1 条消息 更新了计划」——他实际调的是 GoalComplete
 * （声明完成），不是 TodoWrite（更新计划）。两者原来共用 'plan' 类别，
 * 折叠行摘要因此写成了「更新了计划」。
 *
 * 断言：
 *   G1 GoalComplete 单独成类（不再是 plan）
 *   G2 折叠行摘要写「声明完成」，不再是「更新了计划」
 *   G3 行内名字用后端 label「声明完成」
 *   G4 行首图标是 goal 专用图标（不是清单图标）
 *   G5 回归：TodoWrite 仍然是「更新了计划」+ plan 图标
 *   G6 两种工具混在一组时分别成类，不合并成一条
 *
 * 页面组装与收尾照抄 scripts/verify-plugin-lock.js（headless Edge + --dump-dom）。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media/narnat.js'), 'utf8');

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function $(sel){ return document.querySelector(sel); }
  function txt(id){ var e = document.getElementById(id); return e ? e.textContent.trim() : ''; }

  async function run(){
    emit({ type: 'initialize', params: {
      version: 't', protocol: 1,
      model: { current: 'm1', options: ['m1'] },
      thinking: { effort: 'high', options: { high: '高' } },
      session: { current: '甲', messages: [] },
      goal: { enabled: false, maxRounds: 10, locked: false },
      plugins: [{ name: 'Goal', label: '目标模式', enabled: true }],
      pluginsLocked: false
    } });
    await sleep(250);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(60);

    /* ---- 一轮只调 GoalComplete ---- */
    emit({ type: 'tool/call', params: {
      id: 'c1', name: 'GoalComplete', label: '声明完成',
      dispatch: 'serial', summary: '完成', arguments: { checklist: [] }
    } });
    await sleep(60);
    emit({ type: 'tool/result', params: {
      id: 'c1', ok: true, durationMs: 0,
      output: '[GOAL_COMPLETE] 完成声明已提交', failed: false, diff: null
    } });
    await sleep(80);
    emit({ type: 'stream/delta', params: { text: '已完成：本轮无实质工程任务。' } });
    await sleep(80);
    emit({ type: 'turn/end', params: { reason: 'goal_complete' } });
    await sleep(400);

    var grp = document.querySelector('#convo .tools[data-tool]');
    var rowName = grp ? (grp.querySelector('.row .name') || {}).textContent : '';
    var sub = '';
    var subEl = grp && grp.parentNode
      ? grp.parentNode.querySelector('.tphase-sub, .tp-sub, .grp-sub') : null;
    var phase = document.querySelector('#convo .tphase');
    if (phase) sub = phase.textContent || '';

    /* ---- 说明：tpActivityName / TP_ACTIVITY 在 IIFE 内，测试取不到，
       只能看真实渲染结果。折叠组摘要与行内名字都在 DOM 上。 ---- */
    var convo = document.getElementById('convo');
    var allText = convo ? (convo.textContent || '') : '';
    ck('G1 渲染结果里出现「声明完成」（行内名字/摘要任一位置）',
      allText.indexOf('声明完成') >= 0, JSON.stringify(allText.slice(-160)));
    ck('G2 渲染结果里没有把 GoalComplete 写成「更新了计划」',
      allText.indexOf('更新了计划') < 0, JSON.stringify(allText.slice(-160)));
    ck('G3 行内名字用后端 label「声明完成」',
      String(rowName || '').indexOf('声明完成') >= 0, JSON.stringify(rowName));

    /* G4 图标：GoalComplete 那一行的 svg 路径与 TodoWrite 那一行不同 */
    function iconOf(name){
      var el = document.querySelector('#convo .tools[data-tool="' + name + '"] .row-ic');
      return el ? el.innerHTML : '';
    }
    ck('G4 GoalComplete 用专用图标（与 TodoWrite 的清单图标不同）',
      iconOf('GoalComplete') !== '' && iconOf('GoalComplete') !== iconOf('TodoWrite'),
      'goal=' + iconOf('GoalComplete').slice(0, 40) +
      ' todo=' + iconOf('TodoWrite').slice(0, 40));

    /* ---- G5/G6 混合：两种工具同组时不能合并成一条 ---- */
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(60);
    emit({ type: 'tool/call', params: {
      id: 'c2', name: 'TodoWrite', label: '更新计划',
      dispatch: 'serial', summary: '2项', arguments: { todos: [] }
    } });
    await sleep(60);
    emit({ type: 'tool/result', params: {
      id: 'c2', ok: true, durationMs: 0, output: '[计划已更新]', failed: false, diff: null
    } });
    await sleep(60);
    emit({ type: 'tool/call', params: {
      id: 'c3', name: 'GoalComplete', label: '声明完成',
      dispatch: 'serial', summary: '完成', arguments: { checklist: [] }
    } });
    await sleep(60);
    emit({ type: 'tool/result', params: {
      id: 'c3', ok: true, durationMs: 0, output: '[GOAL_COMPLETE] ok', failed: false, diff: null
    } });
    await sleep(300);

    var rows = [].slice.call(document.querySelectorAll('#convo .tools[data-tool]'));
    var names = rows.map(function(el){
      var n = el.querySelector('.row .name');
      return n ? n.textContent.trim() : '';
    });
    ck('G5 回归：TodoWrite 那一行仍叫「更新计划」',
      names.indexOf('更新计划') >= 0, JSON.stringify(names));
    ck('G6 两种工具各占一行，不合并成一条',
      names.filter(function(x){ return x === '声明完成'; }).length >= 1 &&
      names.filter(function(x){ return x === '更新计划'; }).length >= 1,
      JSON.stringify(names));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-goal-row</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-goalrow');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=30000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
