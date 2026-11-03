/**
 * 验收 T3「dsh 双队列」的前端形态：排队待发（next-turn）与插话待发（next-step）。
 *
 * 断言（对应 nn-tasks/dsh-insert-align/T3-dual-queue.md ⑤ 前端 6 的 q1-q9）：
 *   q1 运行中发一条消息（默认 queue）→ 排队条出现 1 行，插话条不出现
 *   q2 运行中发一条 mode:'steer'    → 插话条出现 1 行，排队条不出现
 *   q3 非运行中：排队行的「插话发送」disabled === true 且 title === '仅运行中可插话发送'
 *   q4 运行中：该按钮 disabled === false
 *   q5 运行中 placeholder 提示「Enter 排队发送 / 插话发送」；偏好切 steer 后跟着变
 *   q6 运行中草稿为空 → 停止按钮可见、发送按钮隐藏
 *   q6b 运行中草稿非空 → 停止按钮**仍然**可见（用户硬要求：停止必须一直能点）
 *   q7 queue/consumed → 排队条那行消失，对话区多出一条**普通**用户气泡（无 data-inserted）
 *   q8 insert/consumed → 对话区那一条**带** data-inserted
 *   q9 两队列同时非空 → 两条都可见，且排队条在插话条上方
 *
 * 用假 RPC 事件驱动真实页面（design/ui-full.html 的 style + body + 生成的
 * media/narnat.js），无头 Edge 里跑，不需要后端。
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

const TEST = String.raw`
(function(){
  var out = [], fails = 0, total = 0;
  function ck(n, ok, d){
    total++;
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function el(id){ return document.getElementById(id); }
  function convo(){ return document.getElementById('convo'); }
  function setVal(v){
    el('input').value = v;
    el('input').dispatchEvent(new Event('input', { bubbles: true }));
  }
  function enter(mod){
    var o = { key: 'Enter', bubbles: true, cancelable: true };
    if (mod) { o.ctrlKey = true; }
    var e = new KeyboardEvent('keydown', o);
    el('input').dispatchEvent(e);
  }
  function sent(re){
    return (window.__sent || []).filter(function(m){ return m && re.test(String(m.type)); });
  }
  function rows(bar){
    return el(bar).querySelectorAll('.row');
  }
  function barOn(id){ return el(id).getAttribute('data-on') === 'true'; }
  function visible(id){
    var s = getComputedStyle(el(id));
    return s.display !== 'none' && s.visibility !== 'hidden';
  }
  function userBubbles(){
    return convo().querySelectorAll('.msg[data-role="user"]');
  }
  function insertedBubbles(){
    return convo().querySelectorAll('.msg[data-inserted]');
  }
  function lastUserText(){
    var l = userBubbles();
    return l.length ? (l[l.length - 1].textContent || '').trim() : '';
  }
  function top(id){ return el(id).getBoundingClientRect().top; }

  async function run(){
    /* 握手：busySend 缺省 queue（dsh 的 DEFAULT_BUSY_ENTER_BEHAVIOR） */
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm1', options: ['m1'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [], session: {},
      busySend: 'queue' } });
    await sleep(260);

    /* ================= q1 运行中默认走排队 ================= */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(60);
    var chat0 = sent(/^chat$/).length;
    setVal('排队的消息');
    await sleep(40);
    enter();
    await sleep(60);
    var posted = sent(/^chat$/)[sent(/^chat$/).length - 1] || {};
    // 后端：先广播入队事件，再回 chat/result（两条都带同一个 id → 界面按 id 去重）
    emit({ type: 'queue/queued', params: { id: 101, text: '排队的消息', position: 1 } });
    await sleep(40);
    emit({ type: 'chat/result', params: { accepted: true, mode: 'queue', id: 101 } });
    await sleep(80);
    ck('q1 运行中发一条消息走 queue → 排队条 1 行、插话条不出现',
      sent(/^chat$/).length === chat0 + 1 &&
      posted.params && posted.params.mode === 'queue' &&
      barOn('queueBar') && rows('queueBar').length === 1 && !barOn('insertBar'),
      'chat增量=' + (sent(/^chat$/).length - chat0) +
      ' mode=' + JSON.stringify(posted.params && posted.params.mode) +
      ' 排队行=' + rows('queueBar').length + ' 排队条=' + barOn('queueBar') +
      ' 插话条=' + barOn('insertBar'));

    /* 清空排队条，给 q2 一个干净起点 */
    emit({ type: 'queue/updated', params: { items: [] } });
    await sleep(50);

    /* ================= q2 运行中 mode:'steer' 走插话 ================= */
    emit({ type: 'insert/updated', params: { items: [] } });
    await sleep(40);
    var chat1 = sent(/^chat$/).length;
    setVal('插话的消息');
    await sleep(40);
    enter(true);            // Cmd/Ctrl+Enter：偏好 queue → 相反 = steer
    await sleep(60);
    var posted2 = sent(/^chat$/)[sent(/^chat$/).length - 1] || {};
    emit({ type: 'insert/queued', params: { id: 201, text: '插话的消息', position: 1 } });
    await sleep(40);
    emit({ type: 'chat/result', params: { accepted: true, mode: 'steer', id: 201 } });
    await sleep(80);
    ck('q2 运行中 mode:steer → 插话条 1 行、排队条不出现',
      sent(/^chat$/).length === chat1 + 1 &&
      posted2.params && posted2.params.mode === 'steer' &&
      barOn('insertBar') && rows('insertBar').length === 1 && !barOn('queueBar'),
      'chat增量=' + (sent(/^chat$/).length - chat1) +
      ' mode=' + JSON.stringify(posted2.params && posted2.params.mode) +
      ' 插话行=' + rows('insertBar').length + ' 插话条=' + barOn('insertBar') +
      ' 排队条=' + barOn('queueBar'));

    /* ================= q3 + q4 「插话发送」按钮的可用性 ================= */
    // 先让排队条里有 1 行，再让这一轮结束（非运行中）
    emit({ type: 'queue/queued', params: { id: 301, text: '待提升的一条', position: 1 } });
    await sleep(60);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(220);
    var sb3 = el('queueBar').querySelector('.row .b.steer');
    ck('q3 非运行中：排队行的「插话发送」disabled 且悬停提示=仅运行中可插话发送',
      !!sb3 && sb3.disabled === true &&
      (sb3.getAttribute('data-tip') || '') === '仅运行中可插话发送',
      '按钮=' + (sb3 ? '有' : '无') + ' disabled=' + (sb3 ? sb3.disabled : '?') +
      ' data-tip=' + JSON.stringify(sb3 ? sb3.getAttribute('data-tip') : null) +
      ' data-run=' + document.body.getAttribute('data-run'));

    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(80);
    var sb4 = el('queueBar').querySelector('.row .b.steer');
    ck('q4 运行中：该按钮 disabled === false',
      !!sb4 && sb4.disabled === false,
      '按钮=' + (sb4 ? '有' : '无') + ' disabled=' + (sb4 ? sb4.disabled : '?') +
      ' data-run=' + document.body.getAttribute('data-run'));

    /* 点它 = 把这条提升成插话（发 queueSteer，本地面板不动，等事件） */
    var st0 = sent(/^queueSteer$/).length;
    if (sb4) sb4.click();
    await sleep(80);
    ck('q4b 运行中点「插话发送」发 queueSteer 且带该行 id',
      sent(/^queueSteer$/).length === st0 + 1 &&
      sent(/^queueSteer$/)[st0].params.id === 301,
      'queueSteer增量=' + (sent(/^queueSteer$/).length - st0) +
      ' params=' + JSON.stringify(sent(/^queueSteer$/)[st0] && sent(/^queueSteer$/)[st0].params));
    // 后端把提升做完：排队少一条、插话多一条
    emit({ type: 'queue/updated', params: { items: [] } });
    emit({ type: 'insert/queued', params: { id: 302, text: '待提升的一条', position: 2 } });
    await sleep(70);

    /* ================= q5 运行中「Enter 去向」提示随偏好 ================= */
    setVal('草稿');
    await sleep(60);
    var ph1 = el('input').getAttribute('placeholder');
    // 偏好切到 steer：走设置面板那条 describe（用户刚改完就是这一条回来）
    emit({ type: 'config/describe', params: { items: [
      { id: 'api.busySend', group: 'api', label: '繁忙时的发送行为', kind: 'select',
        value: 'steer', writable: true,
        options: [{ value: 'queue', label: '排队发送' }, { value: 'steer', label: '插话发送' }] }
    ], groups: [{ id: 'api', label: '模型 API' }] } });
    await sleep(120);
    var ph2 = el('input').getAttribute('placeholder');
    ck('q5 运行中 placeholder 提示 Enter 去向；偏好切 steer 后跟着变',
      ph1 === 'Enter 排队发送，/ 调出命令' && ph2 === 'Enter 插话发送，/ 调出命令',
      '切前=' + JSON.stringify(ph1) + ' 切后=' + JSON.stringify(ph2));

    /* ================= q6 运行中停止按钮常显 ================= */
    // 用户要求：运行中停止按钮必须一直可点，不因草稿有无而隐藏
    setVal('');
    await sleep(60);
    ck('q6 运行中草稿为空 → 停止按钮可见、发送按钮隐藏',
      visible('stopBtn') && !visible('sendBtn'),
      'stopBtn.display=' + getComputedStyle(el('stopBtn')).display +
      ' sendBtn.display=' + getComputedStyle(el('sendBtn')).display);
    setVal('草稿');
    await sleep(60);
    ck('q6b 运行中草稿非空 → 停止按钮**仍然**可见（这是本任务的硬要求）',
      visible('stopBtn') && !visible('sendBtn'),
      'stopBtn.display=' + getComputedStyle(el('stopBtn')).display +
      ' sendBtn.display=' + getComputedStyle(el('sendBtn')).display);
    setVal('');
    await sleep(40);

    /* ================= q7 queue/consumed → 普通用户气泡 ================= */
    emit({ type: 'queue/queued', params: { id: 401, text: '被消费的排队消息', position: 1 } });
    await sleep(60);
    var ins0 = insertedBubbles().length;
    var ub0 = userBubbles().length;
    emit({ type: 'queue/consumed', params: { id: 401, text: '被消费的排队消息' } });
    await sleep(90);
    var born = userBubbles()[userBubbles().length - 1];
    ck('q7 queue/consumed → 排队那行消失，对话区多出一条普通用户气泡（无 data-inserted）',
      rows('queueBar').length === 0 && userBubbles().length === ub0 + 1 &&
      !!born && !born.hasAttribute('data-inserted') &&
      lastUserText() === '被消费的排队消息' &&
      insertedBubbles().length === ins0,
      '排队行=' + rows('queueBar').length + ' 用户气泡增量=' + (userBubbles().length - ub0) +
      ' data-inserted=' + (born ? born.hasAttribute('data-inserted') : '?') +
      ' 文本=' + JSON.stringify(lastUserText()) +
      ' 插话气泡增量=' + (insertedBubbles().length - ins0));

    /* ================= q8 insert/consumed → 插话气泡 ================= */
    var ins1 = insertedBubbles().length;
    emit({ type: 'insert/consumed', params: { id: 501, text: '被消费的插话' } });
    await sleep(90);
    var ib = insertedBubbles()[insertedBubbles().length - 1];
    ck('q8 insert/consumed → 那一条带 data-inserted',
      insertedBubbles().length === ins1 + 1 && !!ib &&
      (ib.textContent || '').indexOf('被消费的插话') >= 0,
      '插话气泡增量=' + (insertedBubbles().length - ins1) +
      ' 文本=' + JSON.stringify(ib ? (ib.textContent || '').trim() : null));

    /* ================= q9 两条同时非空：排队条在上 ================= */
    emit({ type: 'queue/queued', params: { id: 601, text: '排队一', position: 1 } });
    emit({ type: 'insert/queued', params: { id: 701, text: '插话一', position: 1 } });
    await sleep(120);
    var qTop = top('queueBar');
    var iTop = top('insertBar');
    ck('q9 两条都可见且排队条在插话条上方',
      visible('queueBar') && visible('insertBar') && qTop < iTop,
      'queueBar.top=' + Math.round(qTop) + ' insertBar.top=' + Math.round(iTop) +
      ' 排队条可见=' + visible('queueBar') + ' 插话条可见=' + visible('insertBar'));

    /* ================= q10 设置面板里的「繁忙时的发送行为」 ================= */
    el('btnSettings').click();
    await sleep(90);
    var tabApi = document.querySelectorAll('#setTabs button')[0];
    emit({ type: 'config/describe', params: { items: [
      { id: 'api.busySend', group: 'api', label: '繁忙时的发送行为', kind: 'select',
        value: 'queue', writable: true,
        options: [{ value: 'queue', label: '排队发送（等这一轮结束后再发）' },
                  { value: 'steer', label: '插话发送（本轮立即插进去）' }] }
    ], groups: [{ id: 'api', label: '模型 API' }] } });
    await sleep(160);
    if (tabApi) { tabApi.click(); await sleep(80); }
    var row = document.querySelector('[data-id="api.busySend"]');
    var sel = row ? row.querySelector('select') : null;
    var opts = sel ? [].slice.call(sel.options).map(function(o){ return o.value; }) : [];
    var lab = row ? (row.querySelector('.set-lab span') || {}).textContent : '';
    (window.__sent || []).length = 0;
    if (sel) {
      sel.value = 'steer';
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    await sleep(120);
    var sets = (window.__sent || []).filter(function(x){ return x.type === 'configSet'; });
    ck('q10 设置面板有「繁忙时的发送行为」select（queue/steer），改它发 configSet{key:busySend}',
      !!sel && lab === '繁忙时的发送行为' && opts.join(',') === 'queue,steer' &&
      sets.length === 1 && sets[0].params.key === 'busySend' &&
      sets[0].params.value === 'steer',
      'label=' + JSON.stringify(lab) + ' select=' + (!!sel) + ' options=' + opts.join(',') +
      ' configSet=' + JSON.stringify(sets.map(function(x){ return x.params; })));

    out.push('SUMMARY total=' + total + ' fail=' + fails);
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-dual-queue</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-dual-queue');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
if (!browser) {
  console.log('找不到 Edge，无法跑无头验证');
  process.exit(2);
}
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
