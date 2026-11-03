/**
 * 验收 T8 缺陷 2：「插话发送」的 busy 状态（对齐 dsh QueueDock 的 setBusy）。
 *
 * 断言（对应 nn-tasks/dsh-insert-align/T8-steer-busy.md ④ 缺陷 2 的 b1-b6）：
 *   b1 点排队行「插话发送」→ 立刻发 1 条 queueSteer
 *   b2 同一帧（回执到达前）该行三颗按钮全部 disabled === true
 *   b3 回执 queueSteer/result 到达后按钮恢复 disabled === false（运行中前提）
 *   b4 回执是失败时按钮也要恢复，且屏幕上有可见提示文字
 *   b5 busy 期间排队条强制展开（data-fold === 'false'），即使之前是收起状态
 *   b6 连点两次只发 1 条 queueSteer（第二次被 disabled 拦住）
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
  function sent(re){
    return (window.__sent || []).filter(function(m){ return m && re.test(String(m.type)); });
  }
  function rows(){ return el('queueBar').querySelectorAll('.row'); }
  function rowById(id){
    var all = rows();
    for (var i = 0; i < all.length; i++) {
      if (String(all[i].getAttribute('data-id')) === String(id)) return all[i];
    }
    return null;
  }
  function btns(id){
    var r = rowById(id);
    return r ? [].slice.call(r.querySelectorAll('button')) : [];
  }
  function allDisabled(id){
    var bs = btns(id);
    return bs.length > 0 && bs.every(function(b){ return b.disabled === true; });
  }
  function noneDisabled(id){
    var bs = btns(id);
    return bs.length > 0 && bs.every(function(b){ return b.disabled === false; });
  }
  function steerBtn(id){
    var r = rowById(id);
    return r ? r.querySelector('.b.steer') : null;
  }
  /**
   * 点按钮**里面的图标**，而不是按钮本身。
   *
   * 这是用户真实点的地方：按钮装的是 SVG 图标，鼠标落点是里面的 path，
   * 事件 target 不是 button。曾经的 bug 就藏在这条路径上：点击处理直接读
   * e.target 上的 data-act，点图标时读不到 -> 静默 return，表现是
   * 「点了没反应、按钮也不禁用、能一直连点」。
   * 原来的断言全是 button.click()（target 就是 button），恰好绕过了它。
   */
  function clickIcon(btn){
    if (!btn) return false;
    var ic = btn.querySelector('svg') || btn.firstElementChild;
    (ic || btn).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return true;
  }
  function fold(){ return el('queueBar').getAttribute('data-fold'); }
  function fixed(){
    // 两行排队：101 / 102
    emit({ type: 'queue/updated', params: { items: [
      { id: 101, text: '排队一' }, { id: 102, text: '排队二' }
    ] } });
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm1', options: ['m1'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [], session: {},
      busySend: 'queue' } });
    await sleep(260);

    /* 运行中 + 两条排队消息 → 排队条默认**收起**（n>1 且 queueFold 初值 true） */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(60);
    fixed();
    await sleep(80);
    var foldBefore = fold();
    var runNow = document.body.getAttribute('data-run');

    /* ========== b5 前置：折叠中的一行点「插话发送」 ========== */
    var st0 = sent(/^queueSteer$/).length;
    var sb = steerBtn(101);
    var hadBtn = !!sb && sb.disabled === false;
    /* 点的是按钮里的 <svg>/<path>（用户真实落点），不是 button 本身 */
    clickIcon(sb);
    /* b2：同一帧读 DOM（renderQueueBar 是同步的，点击后立刻重画） */
    var dis101 = allDisabled(101);
    var tip101 = (steerBtn(101) || {}).getAttribute
      ? steerBtn(101).getAttribute('data-tip') : null;
    await sleep(0);
    var st1 = sent(/^queueSteer$/).length;
    var posted = sent(/^queueSteer$/)[st0] || {};
    ck('b1 点排队行「插话发送」→ 立刻发 1 条 queueSteer 且带该行 id',
      runNow === 'true' && hadBtn && (st1 - st0) === 1 &&
      posted.params && posted.params.id === 101,
      'data-run=' + runNow + ' 按钮可点=' + hadBtn +
      ' queueSteer增量=' + (st1 - st0) +
      ' params=' + JSON.stringify(posted.params));
    ck('b2 同一帧（回执到达前）该行三颗按钮全部 disabled',
      dis101, '第101行按钮数=' + btns(101).length +
      ' 全部disabled=' + dis101 + ' 各按钮=' +
      JSON.stringify(btns(101).map(function(b){ return b.disabled; })) +
      ' steer的data-tip=' + JSON.stringify(tip101));
    ck('b5 busy 期间排队条强制展开（data-fold === false），此前是收起',
      foldBefore === 'true' && fold() === 'false',
      '点前 data-fold=' + JSON.stringify(foldBefore) +
      ' 点后 data-fold=' + JSON.stringify(fold()));

    /* ========== b6 连点第二次：被 disabled 拦住 ========== */
    var sb2 = steerBtn(101);
    clickIcon(sb2);
    await sleep(30);
    ck('b6 连点两次只发 1 条 queueSteer（第二次被 disabled 拦住）',
      sent(/^queueSteer$/).length === st1,
      'queueSteer总数=' + sent(/^queueSteer$/).length + '（期望 ' + st1 + '）' +
      ' 第二次按钮disabled=' + (sb2 ? sb2.disabled : '?') +
      ' 另一行102仍可点=' + (steerBtn(102) ? !steerBtn(102).disabled : '?'));

    /* ========== b3 成功回执 → 按钮恢复 ========== */
    emit({ type: 'queueSteer/result', params: { steered: true } });
    await sleep(60);
    ck('b3 回执到达后按钮恢复 disabled === false（运行中前提）',
      noneDisabled(101) && document.body.getAttribute('data-run') === 'true',
      '第101行全部可点=' + noneDisabled(101) +
      ' 各按钮=' + JSON.stringify(btns(101).map(function(b){ return b.disabled; })) +
      ' data-run=' + document.body.getAttribute('data-run') +
      ' data-fold=' + JSON.stringify(fold()));

    /* ========== b4 失败回执 → 按钮恢复 + 可见提示 ========== */
    fixed();
    await sleep(60);
    var st2 = sent(/^queueSteer$/).length;
    var sb3 = steerBtn(102);
    clickIcon(sb3);
    var dis102 = allDisabled(102);
    await sleep(20);
    emit({ type: 'queueSteer/result', params: {
      steered: false, error: 'queue/steer: 当前轮次已结束，不再接受插话'
    } });
    await sleep(90);
    var notice = (convo().textContent || '');
    var hasTip = notice.indexOf('插话发送失败') >= 0 &&
      notice.indexOf('当前轮次已结束') >= 0;
    ck('b4 失败回执到达后按钮也恢复，且屏幕上有可见提示文字',
      (st2 === sent(/^queueSteer$/).length - 1) && dis102 &&
      noneDisabled(102) && hasTip,
      '点击后先禁用=' + dis102 +
      ' 回执后全部可点=' + noneDisabled(102) +
      ' queueSteer增量=' + (sent(/^queueSteer$/).length - st2) +
      ' 提示可见=' + hasTip +
      ' 对话区含=' + JSON.stringify(notice.slice(-120)));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-queue-busy</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-queue-busy');
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
