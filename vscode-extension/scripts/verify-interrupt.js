/**
 * 验证「一轮结束是否折叠 / 折叠组能不能点开」的判据（对齐 dsh 的两层）。
 *
 * 规则（T7 起的形态，与 dsh 实机一致）：
 *   · 用户主动停止 / 这一轮失败 → **照建折叠组**、组正文初始收起，但组头
 *     `disabled`（不可展开、无 chevron），文案带状态词「已停止」/「失败」；
 *     停止轮正文末尾**另挂** dsh 那个内联小标签 `.msg-stopped`
 *   · 首个过程节点之后出现过插话气泡 → 照建组、组头不带状态词、可以点开
 *     （dsh 的 hasInterleavedInput 只影响整轮层，组级照常可开合）
 *   · 正常完成（有过程可折）→ 建折叠组，最终回复裸在组外
 *   · 停止轮连正文都没有 → 退回整行「■ 已停止」
 *
 * 另有：重复 turn/end 幂等、样式对齐 dsh、正常完成不挂任何打断标记。
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
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  :: ' + d : ''));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function tags(){ return convo().querySelectorAll('.msg-stopped'); }
  function procs(){ return convo().querySelectorAll('.turn-proc'); }
  function inserts(){ return convo().querySelectorAll('.msg[data-inserted]'); }
  function tools(){ return convo().querySelectorAll(':scope > .tools'); }
  function sysLines(){
    return [].slice.call(convo().querySelectorAll('.sys'))
      .map(function(x){ return (x.textContent || '').replace(/\s+/g, ' ').trim(); });
  }
  function labelOf(p){
    return p ? ((p.querySelector('.tp-label') || {}).textContent || '') : '';
  }
  function lastAssistant(){
    var l = convo().querySelectorAll('.msg.assistant');
    return l.length ? l[l.length - 1] : null;
  }
  /** 组头能否点开：无 chevron + disabled + 点击后 data-open 不变 */
  async function canExpand(p){
    var row = p ? p.querySelector('.tp-row') : null;
    if (!row) return { ok: false, why: '没有 .tp-row' };
    var caret = !!p.querySelector('.tp-caret, .chevron');
    var before = p.getAttribute('data-open');
    row.click();
    await sleep(250);
    var after = p.getAttribute('data-open');
    row.click();
    await sleep(250);
    var back = p.getAttribute('data-open');
    /* 组头可自由开合（dsh 的组级 header 是普通 button），停止轮只要求**默认展开** */
    return { caret: caret, disabled: row.disabled === true,
      before: before, after: after, back: back,
      ok: caret && row.disabled !== true && before === 'true' && after === 'false' && back === 'true' };
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);

    /* ===== A：停止轮（有工具行 + 有正文）→ 组照建，组头不可点开 ===== */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'a1', name: 'Grep', label: '搜索代码',
      summary: 'foo', arguments: { pattern: 'foo' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'a1', ok: true, durationMs: 8, output: 'x' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我来实际跑几个工具。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(400);

    var A = procs()[0];
    ck('A1 停止轮产生 1 个折叠组', procs().length === 1, 'got=' + procs().length);
    ck('A2 组头文案含「已停止」', labelOf(A).indexOf('已停止') >= 0, JSON.stringify(labelOf(A)));
    var bodyA = A ? A.querySelector('.tp-body') : null;
    /* 停止轮组正文**展开**（dsh 的 turnProcessAlwaysOpen = keep open）：
       收起且打不开会让被终止的工具彻底消失（用户报障）。 */
    ck('A3 组正文初始展开（内容一直露着）', !!A && A.getAttribute('data-open') === 'true'
       && !!bodyA && !bodyA.hasAttribute('hidden'),
       'data-open=' + (A ? A.getAttribute('data-open') : '-')
         + ' hidden=' + (bodyA ? bodyA.hasAttribute('hidden') : '-'));
    var expA = await canExpand(A);
    ck('A4 组头可自由开合（有 chevron、未 disabled、点击能收起再展开）',
       expA.ok, JSON.stringify(expA));
    var ta = tags();
    ck('A5 正文末尾挂了 1 个 .msg-stopped', ta.length === 1, 'got=' + ta.length);
    ck('A6 标签文本为「已停止」', ta.length && ta[0].textContent.trim() === '已停止',
       ta.length ? JSON.stringify(ta[0].textContent) : '(无)');
    ck('A7 工具行收进了组里（不再平铺在 #convo 顶层）',
       tools().length === 0 && !!A && A.querySelectorAll('.tp-inner .tools').length === 1,
       '顶层 tools=' + tools().length
         + ' 组内 tools=' + (A ? A.querySelectorAll('.tp-inner .tools').length : '-'));
    ck('A8 不再有整行「■ 已停止」',
       sysLines().filter(function(s){ return /■.*已停止/.test(s); }).length === 0,
       JSON.stringify(sysLines()));
    ck('A9 全屏不再出现「任务被打断」文案',
       (convo().textContent || '').indexOf('任务被打断') < 0,
       JSON.stringify(labelOf(null)));

    /* ===== B：重复 turn/end 幂等 ===== */
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(200);
    ck('B1 重复 turn/end 不重复处理', procs().length === 1 && tags().length === 1,
       'procs=' + procs().length + ' tags=' + tags().length);

    /* ===== C：只有一句话就被打断（无过程节点）→ 内联标签 ===== */
    var tagBeforeC = tags().length;
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我想说点什么' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(350);

    ck('C1 无过程节点时不建组（组数不变）', procs().length === 1, 'got=' + procs().length);
    ck('C2 这时在正文末尾多挂 1 个内联标签', tags().length === tagBeforeC + 1,
       'got=' + tags().length + ' before=' + tagBeforeC);
    var tl2 = tags();
    var t = tl2.length ? tl2[tl2.length - 1] : null;
    ck('C3 标签文本是「已停止」', !!t && t.textContent.trim() === '已停止',
       t ? JSON.stringify(t.textContent) : '(无)');
    ck('C4 标签挂在正文里（不是别处）',
       !!(lastAssistant() && lastAssistant().querySelector('.msg-stopped')), 'ok');

    if (t) {
      var cs = getComputedStyle(t);
      ck('C5 样式：字号 11px（对齐 dsh）', cs.fontSize === '11px', cs.fontSize);
      ck('C6 样式：圆角 6px（对齐 dsh）', cs.borderRadius === '6px', cs.borderRadius);
      ck('C7 样式：有底色', cs.backgroundColor !== 'rgba(0, 0, 0, 0)'
         && cs.backgroundColor !== 'transparent', cs.backgroundColor);
      ck('C8 样式：无动画（dsh: no animation）',
         cs.animationName === 'none' || cs.animationName === '', cs.animationName);
      ck('C9 样式：不占满行', t.getBoundingClientRect().width < convo().clientWidth,
         Math.round(t.getBoundingClientRect().width) + '/' + convo().clientWidth);
    }

    /* ===== D：正常完成不挂任何打断标记 ===== */
    var tagBefore = tags().length;
    var sysBefore = sysLines().filter(function(s){ return /已停止|已打断/.test(s); }).length;
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '正常回答完成。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(300);
    ck('D1 正常完成不挂打断标签', tags().length === tagBefore, 'got=' + tags().length);
    ck('D2 正常完成不新增打断提示',
       sysLines().filter(function(s){ return /已停止|已打断/.test(s); }).length === sysBefore,
       JSON.stringify(sysLines()));

    /* ===== E：失败轮（turn/end reason=error）→ 组照建，组头「失败」点不开 ===== */
    var procBeforeE = procs().length;
    emit({ type: 'turn/begin', params: { round: 4 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'e1', name: 'Read', label: '读取',
      summary: 'c.md', arguments: { file_path: 'c.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'e1', ok: true, durationMs: 4, output: 'x' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '读了一半就失败。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'error' } });
    await sleep(400);
    var E = procs()[procs().length - 1];
    ck('E1 失败轮产生 1 个折叠组', procs().length === procBeforeE + 1,
       'got=' + procs().length + ' before=' + procBeforeE);
    ck('E2 组头文案含「失败」', labelOf(E).indexOf('失败') >= 0, JSON.stringify(labelOf(E)));
    var expE = await canExpand(E);
    ck('E3 失败轮组头不可展开', expE.ok, JSON.stringify(expE));
    ck('E4 失败轮的工具行收进了组里', tools().length === 0
       && !!E && E.querySelectorAll('.tp-inner .tools').length >= 1,
       '顶层 tools=' + tools().length);
    ck('E5 组外有含「请求失败」的系统行',
       sysLines().filter(function(s){ return s.indexOf('请求失败') >= 0; }).length === 1,
       JSON.stringify(sysLines()));

    /* ===== F：插话轮照建组，但**可以**点开（插话只影响 dsh 的整轮层） ===== */
    /* F 正向：工具行 → 插话气泡 → 正文，reason=done */
    var procBeforeF = procs().length;
    var insBeforeF = inserts().length;
    emit({ type: 'turn/begin', params: { round: 5 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'f1', name: 'Glob', label: '搜索文件',
      summary: '*.js', arguments: { pattern: '*.js' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'f1', ok: true, durationMs: 6, output: 'a.js' } });
    await sleep(20);
    emit({ type: 'insert/consumed', params: { id: 'i1', text: '顺便把 tests 也搜一遍' } });
    await sleep(40);
    emit({ type: 'stream/delta', params: { text: '好的，加搜 tests。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(400);
    var F = procs()[procs().length - 1];
    ck('F1 工具行→插话→正文，done 结束时照建折叠组',
       procs().length === procBeforeF + 1, 'got=' + procs().length + ' before=' + procBeforeF);
    ck('F2 插话气泡仍在对话流里可见（data-inserted）', inserts().length === insBeforeF + 1,
       'got=' + inserts().length + ' before=' + insBeforeF);
    ck('F3 插话轮组头不带状态词',
       labelOf(F).indexOf('已停止') < 0 && labelOf(F).indexOf('失败') < 0,
       JSON.stringify(labelOf(F)));
    var rowF = F ? F.querySelector('.tp-row') : null;
    if (rowF) { rowF.click(); await sleep(300); }
    ck('F4 插话轮组头可以点开（data-open 变 true）',
       !!F && F.getAttribute('data-open') === 'true' && rowF && rowF.disabled !== true,
       'data-open=' + (F ? F.getAttribute('data-open') : '-'));

    /* F 反向：同一场景去掉插话气泡 → 同样建组、可点开 */
    var procBeforeF2 = procs().length;
    emit({ type: 'turn/begin', params: { round: 6 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'f2', name: 'Glob', label: '搜索文件',
      summary: '*.md', arguments: { pattern: '*.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'f2', ok: true, durationMs: 6, output: 'a.md' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '搜完了。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(400);
    var F2 = procs()[procs().length - 1];
    var rowF2 = F2 ? F2.querySelector('.tp-row') : null;
    if (rowF2) { rowF2.click(); await sleep(300); }
    ck('F5 同一场景去掉插话气泡同样产生折叠组（反向）',
       procs().length === procBeforeF2 + 1, 'got=' + procs().length + ' before=' + procBeforeF2);
    ck('F6 无插话场景组头也可点开',
       !!F2 && F2.getAttribute('data-open') === 'true' && rowF2 && rowF2.disabled !== true,
       'data-open=' + (F2 ? F2.getAttribute('data-open') : '-'));

    /* ===== G：开场插话 → 仍建组、组头无状态词、可点开 ===== */
    var procBeforeG = procs().length;
    var insBeforeG = inserts().length;
    emit({ type: 'turn/begin', params: { round: 7 } });
    await sleep(20);
    emit({ type: 'insert/consumed', params: { id: 'g1', text: '（开场就先说一句）' } });
    await sleep(40);
    emit({ type: 'tool/call', params: { id: 'g2', name: 'Grep', label: '搜索代码',
      summary: 'bar', arguments: { pattern: 'bar' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'g2', ok: true, durationMs: 5, output: 'y' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '开场那句不算插话。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(400);
    ck('G1 开场插话场景同样折叠',
       procs().length === procBeforeG + 1,
       'got=' + procs().length + ' before=' + procBeforeG);
    var gi = inserts();
    var gBubble = insBeforeG < gi.length ? gi[gi.length - 1] : null;
    var gBox = procs()[procs().length - 1];
    ck('G2 开场那条插话气泡留在组外（原位置在组之前）',
       !!gBubble && !!gBox && !gBox.contains(gBubble)
       && (gBubble.compareDocumentPosition(gBox) & 4) !== 0,
       gBubble ? ('inBox=' + (gBox ? gBox.contains(gBubble) : '?')) : '(找不到气泡)');
    ck('G3 开场插话场景组头不带状态词',
       labelOf(gBox).indexOf('已停止') < 0 && labelOf(gBox).indexOf('失败') < 0,
       JSON.stringify(labelOf(gBox)));
    var rowG = gBox ? gBox.querySelector('.tp-row') : null;
    if (rowG) { rowG.click(); await sleep(300); }
    ck('G4 开场插话场景组头可点开',
       !!gBox && gBox.getAttribute('data-open') === 'true' && rowG && rowG.disabled !== true,
       'data-open=' + (gBox ? gBox.getAttribute('data-open') : '-'));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-interrupt</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-intr2');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-interrupt.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const txt = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT';
console.log(txt);
process.exit(/fail=0/.test(txt) ? 0 : 2);
