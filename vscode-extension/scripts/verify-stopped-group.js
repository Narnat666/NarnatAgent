/**
 * 验收「停止 / 失败轮照样建组，且**默认展开、组头可自由开合**」（T7 + R 轮两次修正）。
 *
 * dsh 有两层，必须分开看，别把上层的限制搬下来：
 *   · 组级层（步骤组）= 我们的 `.turn-proc`：header 是普通 button
 *     （`ChatGroupSeat.tsx:116` `ProcessGroupHeader` / `toggle`），**没有 disabled
 *     分支**，可自由开合。
 *   · 整轮层（`TurnProcessNodeView`）= 我们没实现的那一层：`turnProcessAlwaysOpen`
 *     为真时 `canCollapse=false` → 该控件 disabled，但内容仍留在屏幕上
 *     （`TurnProcessNodeView.tsx:19,45`；契约注释 `Keep live, stopped, and failed
 *     Turns open.`，`client/contract/turn-process.ts:48-53`；测试
 *     `chat-view.client.spec.tsx:3560`「keeps it open」断言 `disabled === true`）。
 *
 * ⚠ 本文件历史上两次写错方向，都是「把整轮层的 disabled 搬到组头上」：
 *   ① 「disabled + 默认收起 + 不绑点击」→ 收起且打不开，被终止的工具彻底消失
 *      （用户报障：「被终止的工具折叠起来了，无法展开」）；
 *   ② 改成「展开 + 不可收起」→ 仍不符合 dsh 的组级层。
 *   现在的口径：**默认展开 + 可自由开合**，由 A3 / A4 / A4b 三条守着。
 *
 * 本文件守着现在的实现：
 *   停止轮   → 1 个组、组头「已停止 · …」、**组正文默认展开且工具行可见**、
 *              **组头可自由开合**、组外正文末尾挂内联 .msg-stopped、
 *              没有整行「■ 已停止」
 *   失败轮   → 1 个组、组头「失败 · …」、同样默认展开且可开合、组外有「请求失败」
 *   插话轮   → 组照建、组头不带状态词、可以自由开合
 *   正常完成 → 与改动前一致（默认收起、可点开）
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
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function procs(){ return convo().querySelectorAll('.turn-proc'); }
  function tags(){ return convo().querySelectorAll('.msg-stopped'); }
  function labelOf(p){ return p ? ((p.querySelector('.tp-label') || {}).textContent || '') : ''; }
  function rowOf(p){ return p ? p.querySelector('.tp-row') : null; }
  function sysTexts(){
    return [].slice.call(convo().querySelectorAll('.sys'))
      .map(function(x){ return (x.textContent || '').replace(/\s+/g, ' ').trim(); });
  }
  /** 组头可自由开合（dsh 的组级 header 没有 disabled 分支）：
      有 chevron、未 disabled、点一次能收起、再点一次能展开。
      返回的 ok 要求「两次点击后回到原状」。 */
  async function toggles(p){
    var row = rowOf(p);
    if (!row) return { ok: false, why: '没有 .tp-row' };
    var caret = !!p.querySelector('.tp-caret, .chevron');
    var before = p.getAttribute('data-open');
    row.click();
    await sleep(260);
    var flipped = p.getAttribute('data-open');
    row.click();
    await sleep(260);
    var back = p.getAttribute('data-open');
    return {
      caret: caret,
      disabled: row.disabled === true,
      before: before, flipped: flipped, back: back,
      ok: caret && row.disabled !== true
        && flipped !== before && back === before
    };
  }
  function reset(){ convo().innerHTML = ''; }
  async function tool(id, name, label, summary){
    emit({ type: 'tool/call', params: { id: id, name: name, label: label,
      summary: summary, arguments: { q: summary } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: id, ok: true, durationMs: 5, output: 'x' } });
    await sleep(20);
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(80);

    /* ===== A：停止轮（有工具行 + 有正文）→ 组照建，组头不可点开 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    await tool('a1', 'Grep', '搜索代码', 'foo');
    emit({ type: 'stream/delta', params: { text: '我来实际跑一遍几类工具，看看效果。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(500);

    var PA = procs()[0];
    ck('A1 停止轮产生 1 个 .turn-proc', procs().length === 1, 'got=' + procs().length);
    ck('A2 组头文案含「已停止」', labelOf(PA).indexOf('已停止') >= 0, JSON.stringify(labelOf(PA)));
    var bodyA = PA ? PA.querySelector('.tp-body') : null;
    ck('A3 组正文初始**展开**（data-open=true 且 .tp-body 无 hidden）',
      !!PA && PA.getAttribute('data-open') === 'true'
        && !!bodyA && !bodyA.hasAttribute('hidden'),
      'data-open=' + (PA ? PA.getAttribute('data-open') : '-')
        + ' hidden=' + (bodyA ? bodyA.hasAttribute('hidden') : '-'));
    /* 用户报障的直接判据：被终止的工具行此刻是否真的在屏幕上占位置。
       必须在点击之前量 —— 点完就收起来了，那时 height=0 是正确行为。 */
    var toolA = PA ? PA.querySelector('.tools .row') : null;
    var rectA = toolA ? toolA.getBoundingClientRect() : null;
    ck('A4b 被终止的工具行默认可见（有高度、未 hidden、在组内）',
      !!toolA && !!rectA && rectA.height > 0 && !bodyA.hasAttribute('hidden'),
      'height=' + (rectA ? Math.round(rectA.height) : '-')
        + ' 文本=' + JSON.stringify(toolA ? (toolA.textContent || '').slice(0, 40) : '-'));
    var expA = await toggles(PA);
    ck('A4 组头可自由开合（有 chevron、未 disabled、点一次收起再点一次展开）',
      expA.ok, JSON.stringify(expA));
    var TA = tags();
    ck('A5 该轮正文末尾挂 1 个 .msg-stopped，文本「已停止」',
      TA.length === 1 && TA[0].textContent.trim() === '已停止',
      'got=' + TA.length + ' 文本=' + JSON.stringify(TA.length ? TA[0].textContent.trim() : ''));
    var lastAsst = (function(){
      var l = convo().querySelectorAll('.msg.assistant');
      return l.length ? l[l.length - 1] : null;
    })();
    ck('A5b 标签挂在组外的正文里（用户直接看得见）',
      !!lastAsst && !!lastAsst.querySelector('.msg-stopped') && !PA.contains(lastAsst),
      'inGroup=' + (PA && lastAsst ? PA.contains(lastAsst) : '?'));
    ck('A6 停止轮没有整行「■ 已停止」的 .sys',
      sysTexts().filter(function(s){ return /■.*已停止/.test(s); }).length === 0,
      JSON.stringify(sysTexts()));
    var csA = getComputedStyle(rowOf(PA));
    var lblA = PA.querySelector('.tp-label');
    out.push('DIAG A 组头 computed cursor=' + csA.cursor + ' opacity=' + csA.opacity
      + ' 标题色=' + getComputedStyle(lblA).color
      + ' 标题宽=' + Math.round(lblA.getBoundingClientRect().width));
    ck('A7 组头是普通可点控件：cursor 为 pointer',
      csA.cursor === 'pointer', csA.cursor);
    ck('A8 组头没有被禁用态压暗（opacity 1、标题有宽度）',
      csA.opacity === '1' && lblA.getBoundingClientRect().width > 0,
      'opacity=' + csA.opacity + ' 宽度=' + Math.round(lblA.getBoundingClientRect().width));

    /* ===== B：失败轮 → 组照建，组头「失败」，不可点开，组外有「请求失败」 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    await tool('b1', 'Read', '读取', 'c.md');
    emit({ type: 'stream/delta', params: { text: '读了一半就失败。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'error' } });
    await sleep(500);

    var PB = procs()[0];
    ck('B1 失败轮产生 1 个 .turn-proc 且组头含「失败」',
      procs().length === 1 && labelOf(PB).indexOf('失败') >= 0,
      'procs=' + procs().length + ' 标题=' + JSON.stringify(labelOf(PB)));
    var expB = await toggles(PB);
    ck('B2 失败轮组头同样可自由开合、内容默认展开', expB.ok, JSON.stringify(expB));
    var failSys = [].slice.call(convo().querySelectorAll('.sys'))
      .filter(function(x){ return (x.textContent || '').indexOf('请求失败') >= 0; });
    ck('B3 组外有含「请求失败」的 .sys',
      failSys.length === 1 && !PB.contains(failSys[0]),
      'got=' + failSys.length + ' 在组内=' + (failSys.length ? PB.contains(failSys[0]) : '?'));

    /* ===== C：工具行 → 插话气泡 → 正文，reason=done → 建组、可点开 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(20);
    await tool('c1', 'Glob', '搜索文件', '*.js');
    emit({ type: 'insert/consumed', params: { id: 'i1', text: '顺便把 tests 也搜一遍' } });
    await sleep(40);
    emit({ type: 'stream/delta', params: { text: '好的，加搜 tests。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(500);

    var PC = procs()[0];
    ck('C1 插话轮产生 1 个 .turn-proc', procs().length === 1, 'got=' + procs().length);
    ck('C2 插话轮组头不带状态词',
      labelOf(PC).indexOf('已停止') < 0 && labelOf(PC).indexOf('失败') < 0,
      JSON.stringify(labelOf(PC)));
    var rowC = rowOf(PC);
    ck('C3 插话轮组头有 chevron 且未 disabled',
      !!PC.querySelector('.tp-caret') && rowC && rowC.disabled !== true,
      'caret=' + !!PC.querySelector('.tp-caret') + ' disabled=' + (rowC ? rowC.disabled : '?'));
    if (rowC) { rowC.click(); await sleep(300); }
    ck('C4 插话轮可以点开（data-open 变成 true）',
      PC.getAttribute('data-open') === 'true', 'got=' + PC.getAttribute('data-open'));

    /* ===== D：同一场景去掉插话气泡 → 也建组、可点开 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 4 } });
    await sleep(20);
    await tool('d1', 'Glob', '搜索文件', '*.md');
    emit({ type: 'stream/delta', params: { text: '搜完了。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(500);

    var PD = procs()[0];
    var rowD = rowOf(PD);
    ck('D1 无插话的同构场景也产生 1 个 .turn-proc', procs().length === 1,
      'got=' + procs().length);
    if (rowD) { rowD.click(); await sleep(300); }
    ck('D2 无插话场景同样可以点开',
      PD.getAttribute('data-open') === 'true' && rowD && rowD.disabled !== true,
      'data-open=' + PD.getAttribute('data-open'));

    /* ===== E：插话气泡在最前（首个过程节点之前）+ done → 建组、可点开 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 5 } });
    await sleep(20);
    emit({ type: 'insert/consumed', params: { id: 'e1', text: '（开场就先说一句）' } });
    await sleep(40);
    await tool('e2', 'Grep', '搜索代码', 'bar');
    emit({ type: 'stream/delta', params: { text: '开场那句不算插话。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(500);

    var PE = procs()[0];
    var rowE = rowOf(PE);
    ck('E1 开场插话场景产生 1 个 .turn-proc', procs().length === 1, 'got=' + procs().length);
    if (rowE) { rowE.click(); await sleep(300); }
    ck('E2 开场插话场景组头可点开',
      PE.getAttribute('data-open') === 'true' && rowE && rowE.disabled !== true,
      'data-open=' + PE.getAttribute('data-open'));
    var bub = convo().querySelector('.msg[data-inserted]');
    ck('E3 开场插话气泡留在组外', !!bub && !PE.contains(bub),
      bub ? ('inGroup=' + PE.contains(bub)) : '(找不到气泡)');

    /* ===== F：正常完成（无插话、无工具）→ 不建组（保持现状） ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 6 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '正常回答完成。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(400);
    ck('F1 正常完成轮（无工具、无插话）不产生组', procs().length === 0,
      'got=' + procs().length);
    ck('F2 正常完成轮不挂任何停止标签', tags().length === 0, 'got=' + tags().length);

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-stopped-group</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-stopped-group');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-stopped-group.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const txt = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT';
console.log(txt);
process.exit(/fail=0/.test(txt) ? 0 : 2);
