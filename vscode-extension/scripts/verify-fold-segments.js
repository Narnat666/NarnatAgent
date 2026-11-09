/**
 * 验收「一轮里的过程按插话位置分段成多个折叠组」（T9）。
 *
 * 背景（dsh `process-groups.ts`）：遇到 `isAtOrOutside(node, 'process')` 的节点
 * （用户消息 / steering 消息都在 `input` 层）就先 flush 封组，再让该 input 节点
 * 作为独立 node 输出 → `G1 → steering 1 → G2 → steering 2 → G3`。
 * 改前 narnat 把整轮过程收成**一个**组，插了两次话的屏幕上只有
 * 「一个折叠行（22 次工具调用）→ 插话 → 插话 → 正文」，看不出每段干了什么。
 *
 * 本文件守着现在的实现：
 *   1. 无插话的正常轮 → 仍然只有 1 个组（行为与改动前一致）
 *   2. 一次插话 → 2 个组
 *   3. 顺序 = 组1 → 插话 → 组2 → 正文
 *   4/5. 每组组头的计数只统计自己的成员
 *   6. 两组的展开互相独立
 *   7. 两次插话 → 3 个组，顺序 G1 → 插话1 → G2 → 插话2 → G3 → 正文
 *   8. 停止轮 + 插话 → 3 个组，只有末段带「已停止」、各段都默认展开且可自由开合，正文末尾 1 个 .msg-stopped
 *   9. 失败轮 + 插话 → 3 个组，只有末段带「失败」，各段都默认展开且可自由开合
 *   10/11. 插话气泡与最终回复都不在任何组内部
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
  function procs(){ return [].slice.call(convo().querySelectorAll('.turn-proc')); }
  function tags(){ return [].slice.call(convo().querySelectorAll('.msg-stopped')); }
  function labelOf(p){ return p ? ((p.querySelector('.tp-label') || {}).textContent || '') : ''; }
  function rowOf(p){ return p ? p.querySelector('.tp-row') : null; }
  function countInLabel(p){
    var m = /(\d+)\s*次工具调用/.exec(labelOf(p));
    return m ? Number(m[1]) : -1;
  }
  function toolCountAttr(p){ return p ? Number(p.getAttribute('data-tool-count')) : -1; }
  function toolNamesIn(p){
    if (!p) return [];
    return p.querySelectorAll('.tools').length;
  }
  /** #convo 顶层节点的有序签名，用来逐项比对顺序 */
  function topOrder(){
    return [].slice.call(convo().children).map(function(el){
      if (el.classList && el.classList.contains('turn-proc')) return 'proc';
      if (el.hasAttribute && el.hasAttribute('data-inserted')) return 'insert';
      if (el.classList && el.classList.contains('msg')
        && el.classList.contains('assistant')) return 'assistant';
      if (el.classList && el.classList.contains('sys')) return 'sys';
      return 'other:' + (el.className || el.nodeName);
    });
  }
  function sameSeq(a, b){
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }
  /** 组头能否点开：无 chevron + disabled + 点击后 data-open 不变，三者都要成立 */
  async function unexpandable(p){
    var row = rowOf(p);
    if (!row) return { ok: false, why: '没有 .tp-row' };
    var caret = p.querySelector('.tp-caret, .chevron');
    var before = p.getAttribute('data-open');
    row.click();
    await sleep(250);
    var after = p.getAttribute('data-open');
    row.click();
    await sleep(250);
    var back = p.getAttribute('data-open');
    /* 停止 / 失败轮的正确形态是「默认展开 + 组头可自由开合」：
       dsh 的组级 header 是普通 button（没有 disabled），禁收起只属于整轮层，
       我们没实现那一层。这里保留函数名，判据改成「可开合且默认展开」。 */
    return {
      caret: !!caret,
      disabled: row.disabled === true,
      before: before,
      after: after,
      back: back,
      ok: !!caret && row.disabled !== true && before === 'true'
        && after === 'false' && back === 'true'
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
  function insertedBubbles(){
    return [].slice.call(convo().querySelectorAll('.msg[data-inserted]'));
  }
  function lastAssistant(){
    var l = convo().querySelectorAll('.msg.assistant');
    return l.length ? l[l.length - 1] : null;
  }
  function inAnyGroup(el){
    if (!el) return '?';
    return procs().some(function(p){ return p.contains(el); });
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(80);

    /* ===== A：无插话的正常轮（工具1 → 工具2 → 正文）→ 仍然只有 1 个组 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    await tool('a1', 'Glob', '搜索文件', '*.js');
    await tool('a2', 'Read', '读取', 'a.js');
    emit({ type: 'stream/delta', params: { text: '两件事都做完了。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(500);

    ck('A1 无插话的正常轮只产生 1 个 .turn-proc（不许变成 2 个）',
      procs().length === 1, 'got=' + procs().length);
    ck('A2 该组的计数 = 2 次工具调用（整轮同一段）',
      toolCountAttr(procs()[0]) === 2 && countInLabel(procs()[0]) === 2,
      'attr=' + toolCountAttr(procs()[0]) + ' label=' + JSON.stringify(labelOf(procs()[0])));
    ck('A3 顶层顺序 = 组 → 正文（正文仍在组外）',
      sameSeq(topOrder(), ['proc', 'assistant']), JSON.stringify(topOrder()));

    /* ===== B：一次插话（工具1 → 工具2 → 插话 → 工具3 → 正文）→ 2 个组 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    await tool('b1', 'Glob', '搜索文件', '*.js');
    await tool('b2', 'Grep', '搜索代码', 'foo');
    emit({ type: 'insert/consumed', params: { id: 'i1', text: '顺便把 tests 也搜一遍' } });
    await sleep(40);
    await tool('b3', 'Shell', '执行命令', 'ls');
    emit({ type: 'stream/delta', params: { text: '按你说的把 tests 也搜了。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(600);

    var PB = procs();
    ck('B1 一次插话产生 2 个 .turn-proc', PB.length === 2, 'got=' + PB.length);
    ck('B2 顶层顺序 = 组1 → 插话 → 组2 → 正文（逐项比对）',
      sameSeq(topOrder(), ['proc', 'insert', 'proc', 'assistant']),
      JSON.stringify(topOrder()));
    var bubB = insertedBubbles()[0];
    ck('B3 组1 的组头计数只含工具1+工具2 == 2（不含工具3）',
      toolCountAttr(PB[0]) === 2 && countInLabel(PB[0]) === 2
        && toolNamesIn(PB[0]) === 2,
      'attr=' + toolCountAttr(PB[0]) + ' 标题=' + JSON.stringify(labelOf(PB[0]))
        + ' 组内工具行=' + toolNamesIn(PB[0]));
    ck('B4 组2 的组头计数只含工具3 == 1',
      toolCountAttr(PB[1]) === 1 && countInLabel(PB[1]) === 1
        && toolNamesIn(PB[1]) === 1,
      'attr=' + toolCountAttr(PB[1]) + ' 标题=' + JSON.stringify(labelOf(PB[1]))
        + ' 组内工具行=' + toolNamesIn(PB[1]));

    /* 6：两组各自有 chevron、未 disabled，点击互不影响 */
    var row1 = rowOf(PB[0]), row2 = rowOf(PB[1]);
    ck('B5 两组组头都有 chevron 且未禁用',
      !!PB[0].querySelector('.tp-caret') && !!PB[1].querySelector('.tp-caret')
        && row1 && row2 && row1.disabled === false && row2.disabled === false,
      'caret=' + (!!PB[0].querySelector('.tp-caret')) + '/'
        + (!!PB[1].querySelector('.tp-caret'))
        + ' disabled=' + (row1 ? row1.disabled : '?') + '/' + (row2 ? row2.disabled : '?'));
    ck('B6 两组初始都收起',
      PB[0].getAttribute('data-open') === 'false'
        && PB[1].getAttribute('data-open') === 'false',
      PB[0].getAttribute('data-open') + '/' + PB[1].getAttribute('data-open'));
    if (row1) { row1.click(); await sleep(300); }
    ck('B7 点开组1 后只有组1 的 data-open 变成 true（组2 不受影响）',
      PB[0].getAttribute('data-open') === 'true'
        && PB[1].getAttribute('data-open') === 'false',
      PB[0].getAttribute('data-open') + '/' + PB[1].getAttribute('data-open'));
    if (row2) { row2.click(); await sleep(300); }
    ck('B8 再点开组2，两组各自独立变成 true',
      PB[0].getAttribute('data-open') === 'true'
        && PB[1].getAttribute('data-open') === 'true',
      PB[0].getAttribute('data-open') + '/' + PB[1].getAttribute('data-open'));
    ck('B9 插话气泡不在任何组的内部（仍可见）',
      !!bubB && bubB.isConnected && !inAnyGroup(bubB),
      bubB ? ('inGroup=' + inAnyGroup(bubB) + ' connected=' + bubB.isConnected) : '(找不到气泡)');
    var tailB = lastAssistant();
    ck('B10 最终回复不在任何组的内部',
      !!tailB && !inAnyGroup(tailB),
      tailB ? ('inGroup=' + inAnyGroup(tailB)) : '(找不到正文)');

    /* ===== C：两次插话（工具1 → 插话1 → 工具2 → 插话2 → 工具3 → 正文）→ 3 个组 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(20);
    await tool('c1', 'Glob', '搜索文件', '*.js');
    emit({ type: 'insert/consumed', params: { id: 'c1i', text: '第一句插话' } });
    await sleep(40);
    await tool('c2', 'Grep', '搜索代码', 'bar');
    emit({ type: 'insert/consumed', params: { id: 'c2i', text: '第二句插话' } });
    await sleep(40);
    await tool('c3', 'Read', '读取', 'b.md');
    emit({ type: 'stream/delta', params: { text: '三件事按顺序都做了。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(700);

    var PC = procs();
    ck('C1 两次插话产生 3 个 .turn-proc', PC.length === 3, 'got=' + PC.length);
    ck('C2 顶层顺序 = G1 → 插话1 → G2 → 插话2 → G3 → 正文（逐项比对）',
      sameSeq(topOrder(), ['proc', 'insert', 'proc', 'insert', 'proc', 'assistant']),
      JSON.stringify(topOrder()));
    ck('C3 三个组的计数依次为 1 / 1 / 1',
      PC.length === 3 && toolCountAttr(PC[0]) === 1
        && toolCountAttr(PC[1]) === 1 && toolCountAttr(PC[2]) === 1,
      PC.map(toolCountAttr).join('/'));
    var bubsC = insertedBubbles();
    ck('C4 两条插话气泡都不在任何组内',
      bubsC.length === 2 && !inAnyGroup(bubsC[0]) && !inAnyGroup(bubsC[1]),
      'got=' + bubsC.length + ' inGroup=' + bubsC.map(inAnyGroup).join('/'));
    ck('C5 三个组都可点开（done 轮）',
      PC.length === 3 && PC.every(function(p){
        var r = rowOf(p);
        return !!p.querySelector('.tp-caret') && r && r.disabled === false;
      }),
      PC.map(function(p){ return rowOf(p) ? rowOf(p).disabled : '?'; }).join('/'));

    /* ===== D：停止轮 + 两次插话 → 3 个组，每组「已停止」且都默认展开、可自由开合 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 4 } });
    await sleep(20);
    await tool('d1', 'Glob', '搜索文件', '*.js');
    emit({ type: 'insert/consumed', params: { id: 'd1i', text: '第一句插话' } });
    await sleep(40);
    await tool('d2', 'Grep', '搜索代码', 'bar');
    emit({ type: 'insert/consumed', params: { id: 'd2i', text: '第二句插话' } });
    await sleep(40);
    await tool('d3', 'Read', '读取', 'b.md');
    emit({ type: 'stream/delta', params: { text: '跑到一半被喊停。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(700);

    var PD = procs();
    ck('D1 停止轮 + 插话仍产生 3 个组', PD.length === 3, 'got=' + PD.length);
    /* 状态词是**整轮**属性：只落在最后一段（与耗时同规则）。
       每段都写的话，停止后屏幕上并排三个「已停止」，加正文末尾内联标签共四处。 */
    ck('D2 只有最后一个组头带「已停止」，前两段不带',
      PD.length === 3 && PD[PD.length - 1] &&
      labelOf(PD[PD.length - 1]).indexOf('已停止') >= 0 &&
      PD.slice(0, 2).every(function(p){ return labelOf(p).indexOf('已停止') < 0; }),
      PD.map(labelOf).join(' | '));
    var dExp = [], dOk = true;
    for (var i = 0; i < PD.length; i++) {
      var r = await unexpandable(PD[i]);
      dExp.push(JSON.stringify(r));
      if (!r.ok) dOk = false;
    }
    ck('D3 每个组头都默认展开且可自由开合（有 chevron、未 disabled、点击能收起再展开）', dOk, dExp.join(' '));
    ck('D4 正文末尾挂 1 个 .msg-stopped，文本「已停止」',
      tags().length === 1 && tags()[0].textContent.trim() === '已停止',
      'got=' + tags().length + ' 文本=' + JSON.stringify(tags().length ? tags()[0].textContent.trim() : ''));
    ck('D5 该标签挂在组外的最终回复里（用户直接看得见）',
      !!lastAssistant() && !!lastAssistant().querySelector('.msg-stopped')
        && !inAnyGroup(lastAssistant()),
      'inGroup=' + inAnyGroup(lastAssistant()));

    /* ===== E：失败轮 + 两次插话 → 3 个组，每组「失败」且都默认展开、可自由开合 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 5 } });
    await sleep(20);
    await tool('e1', 'Glob', '搜索文件', '*.js');
    emit({ type: 'insert/consumed', params: { id: 'e1i', text: '第一句插话' } });
    await sleep(40);
    await tool('e2', 'Grep', '搜索代码', 'bar');
    emit({ type: 'insert/consumed', params: { id: 'e2i', text: '第二句插话' } });
    await sleep(40);
    await tool('e3', 'Read', '读取', 'b.md');
    emit({ type: 'stream/delta', params: { text: '跑到第三步失败。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'error' } });
    await sleep(700);

    var PE = procs();
    ck('E1 失败轮 + 插话产生 3 个组', PE.length === 3, 'got=' + PE.length);
    ck('E2 只有最后一个组头带「失败」，前两段不带（与「已停止」同规则）',
      PE.length === 3 && PE[PE.length - 1] &&
      labelOf(PE[PE.length - 1]).indexOf('失败') >= 0 &&
      PE.slice(0, 2).every(function(p){ return labelOf(p).indexOf('失败') < 0; }),
      PE.map(labelOf).join(' | '));
    var eExp = [], eOk = true;
    for (var j = 0; j < PE.length; j++) {
      var r2 = await unexpandable(PE[j]);
      eExp.push(JSON.stringify(r2));
      if (!r2.ok) eOk = false;
    }
    ck('E3 每个组头都默认展开且可自由开合（有 chevron、未 disabled、点击能收起再展开）', eOk, eExp.join(' '));
    ck('E4 组外仍有「请求失败」的系统行（重试出口没被收进组）',
      [].slice.call(convo().querySelectorAll('.sys')).filter(function(x){
        return (x.textContent || '').indexOf('请求失败') >= 0 && !inAnyGroup(x);
      }).length === 1,
      JSON.stringify([].slice.call(convo().querySelectorAll('.sys'))
        .map(function(x){ return (x.textContent || '').replace(/\s+/g, ' ').trim(); })));

    /* ===== F：插话在最前（首个过程节点之前）→ 组仍只有 1 个，气泡在组外 ===== */
    reset();
    emit({ type: 'turn/begin', params: { round: 6 } });
    await sleep(20);
    emit({ type: 'insert/consumed', params: { id: 'f1', text: '开场先说一句' } });
    await sleep(40);
    await tool('f2', 'Grep', '搜索代码', 'baz');
    emit({ type: 'stream/delta', params: { text: '收到开场那句。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(600);

    ck('F1 开场插话（首个过程节点之前）仍只有 1 个组', procs().length === 1,
      'got=' + procs().length);
    var bubF = insertedBubbles()[0];
    ck('F2 开场插话气泡在组外，且顺序 = 插话 → 组 → 正文',
      !!bubF && !inAnyGroup(bubF)
        && sameSeq(topOrder(), ['insert', 'proc', 'assistant']),
      JSON.stringify(topOrder()) + ' inGroup=' + inAnyGroup(bubF));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-fold-segments</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-fold-segments');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-fold-segments.html');
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
