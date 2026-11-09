/**
 * 验收 T03：「写文件时先出现『准备写入』行，并实时刷新路径/行数/体积」。
 *
 * 用户报障原文：「ai 写文件的时候，界面会卡住，原因是需要等到 ai 写完毕后才在
 * 界面上显示写了什么」。对照 dsh：模型还在生成工具参数时，界面上就已经有一行
 * 「准备写入文件 · path/to/a.js · 12KB」，数字持续往上跳。
 *
 * 断言分两组：
 *   · **P1~P9** —— 任务书 ④.B 的编号，逐条一一对应；
 *   · **S1~S5** —— 半截 JSON 扫描器的容错判据（任务书 ②.1 要求「必须为每一条
 *     写一个断言」）。全部走真实 DOM：扫描器在页面 IIFE 里，外部拿不到引用，
 *     所以用「注入分片 → 看行头显示出什么」来断言它的行为，不额外开测试口子。
 *
 * 无头环境的两个实测约束（不满足就没法验，已在报告的「实测」一节记录）：
 *   1. headless Edge + `--virtual-time-budget` 下 **requestAnimationFrame 不触发**
 *      （加载后只跑第 1 个帧回调，之后连排 20 次一次都没执行；同一页的
 *      setTimeout 链 20 步全跑完）。界面侧的 afterFrame 因此是「rAF 与 50ms
 *      定时器赛跑」，本脚本的一「帧」= 那个 50ms 节拍（见 frames()）。
 *   2. `--dump-dom` 只在虚拟时间预算耗尽后 dump，所以页面自己把结果写进 <pre>。
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
    out.push((ok ? 'PASS ' : 'FAIL ') + n +
      (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function diag(s){ out.push('DIAG ' + s); }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  /* 一「帧」= 界面侧 afterFrame 的兜底节拍（50ms）。无头环境里 rAF 不触发，
     而 setTimeout 正常，所以这里按定时器节拍等，等得比 50ms 宽一点。 */
  async function frames(n){ for (var i = 0; i < n; i++) await sleep(60); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function tools(){ return convo().querySelectorAll('.tools'); }
  function rows(){ return convo().querySelectorAll('.tools .row'); }
  function rowByArg(sub){
    var list = rows();
    for (var i = 0; i < list.length; i++) {
      var a = list[i].querySelector('.arg');
      if (a && String(a.textContent).indexOf(sub) >= 0) return list[i];
    }
    return null;
  }
  function nameOf(row){
    var n = row && row.querySelector('.name');
    return n ? String(n.textContent) : '(无 .name)';
  }
  function metaOf(row){
    var m = row && row.querySelector('.meta');
    return m ? String(m.textContent) : '(无 .meta)';
  }
  function argOf(row){
    var a = row && row.querySelector('.arg');
    return a ? String(a.textContent) : '(无 .arg)';
  }
  /* .slot 是 .row 的**兄弟**（骨架是 .tools > .row + .slot），不是它的后代：
     从 row 上 querySelector('.slot ...') 永远是空，这一条踩过一次。 */
  function slotOf(row){
    var w = (row && row.closest) ? row.closest('.tools') : null;
    return w ? w.querySelector('.slot') : null;
  }
  function ioCount(row){
    var s = slotOf(row);
    return s ? s.querySelectorAll('.io').length : -1;
  }
  function delta(id, name, d){
    emit({ type: 'tool/call_delta', params: { id: id, name: name, delta: d } });
  }
  var BS = '\\';                       // 一个反斜杠（页面源码里 '\\' 就是一个）
  var AAA100 = new Array(101).join('a');   // 100 个 a

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: '甲', messages: [] }, workdir: 'E:/x' } });
    await sleep(200);
    var errBase = window.__ERR.length;

    /* ─────────── P1 ─────────── */
    delta('tc_x', 'Write', '{"file_path":"src/a.js","content":"line1\\n');
    await frames(2);
    var r1 = rowByArg('src/a.js');
    ck('P1 一条 delta 就出现 1 个 .tools 行，.name 严格等于 Write（不是「写入」）',
      tools().length === 1 && !!r1 && nameOf(r1) === 'Write',
      'tools=' + tools().length + ' name=' + JSON.stringify(r1 ? nameOf(r1) : '(无行)'));
    ck('P1b 行头 .arg 含 src/a.js', !!r1 && argOf(r1).indexOf('src/a.js') >= 0,
      JSON.stringify(argOf(r1)));

    /* ─────────── P2 ─────────── */
    ck('P2 该行带 data-preparing=1 与 data-st=running，.meta 含「准备中」',
      !!r1 && r1.getAttribute('data-preparing') === '1'
        && r1.getAttribute('data-st') === 'running'
        && metaOf(r1).indexOf('准备中') >= 0,
      'preparing=' + (r1 && r1.getAttribute('data-preparing')) +
      ' st=' + (r1 && r1.getAttribute('data-st')) + ' meta=' + JSON.stringify(metaOf(r1)));

    /* ─────────── P3 ─────────── 21 片 × 100 字符，片间等帧 ─────────── */
    for (var i = 0; i < 21; i++) { delta('tc_x', 'Write', AAA100); await sleep(20); }
    await frames(2);
    var m3 = metaOf(r1);
    ck('P3 content > 2048 字符后 .meta 出现 2KB，且 .slot 里没有 .io 卡片（未挂 IN 段）',
      m3.indexOf('2KB') >= 0 && ioCount(r1) === 0,
      'meta=' + JSON.stringify(m3) + ' io=' + ioCount(r1));
    diag('P3 内容总长=' + (6 + 21 * 100) + ' 字符，floor(2106/1024)=' + Math.floor(2106 / 1024));

    /* ─────────── P4 ─────────── 变化量闸门 ─────────── */
    var before4 = metaOf(r1);
    delta('tc_x', 'Write', new Array(11).join('a'));   // 再 +10 字符，不跨 1024 台阶
    await frames(3);
    ck('P4 未跨 1024 台阶时 .meta 逐字不变（一个字节的 DOM 都不写）',
      metaOf(r1) === before4,
      JSON.stringify(before4) + ' -> ' + JSON.stringify(metaOf(r1)));

    /* ─────────── P5 ─────────── 尾部半个转义 ─────────── */
    var errBefore5 = window.__ERR.length;
    delta('tc_x', 'Write', 'abc' + BS);        // 尾部孤立反斜杠
    await frames(1);
    delta('tc_x', 'Write', BS + 'u12');        // 半个 \uXXXX
    await frames(2);
    ck('P5 尾部孤立 \\ 与半个 \\u12 不抛异常，.meta 仍是上一个合法值',
      window.__ERR.length === errBefore5 && metaOf(r1) === before4,
      'err=' + JSON.stringify(window.__ERR.slice(errBefore5)) +
      ' meta=' + JSON.stringify(metaOf(r1)) + ' 期望=' + JSON.stringify(before4));

    /* ─────────── P6 ─────────── tool/call 原地升级 ─────────── */
    window.__probeRow = r1;
    var big = 'line1\n' + new Array(2101).join('a');   // 2048+ 字符的真实内容
    emit({ type: 'tool/call', params: { id: 'tc_x', name: 'Write', label: '写入',
      summary: 'src/a.js', arguments: { file_path: 'src/a.js', content: big } } });
    await frames(2);
    var nowRow = document.querySelector('#convo .tools .row');
    ck('P6a 没有新建第二行（.tools 仍为 1 个）', tools().length === 1,
      'tools=' + tools().length);
    ck('P6b DOM 节点身份不变且仍在文档里',
      window.__probeRow === nowRow && window.__probeRow.isConnected === true,
      'same=' + (window.__probeRow === nowRow) +
      ' connected=' + (nowRow && nowRow.isConnected));
    ck('P6c data-preparing 已不存在、.meta 含「执行中」',
      !!r1 && !r1.hasAttribute('data-preparing') && metaOf(r1).indexOf('执行中') >= 0,
      'hasPreparing=' + (r1 && r1.hasAttribute('data-preparing')) +
      ' meta=' + JSON.stringify(metaOf(r1)));
    var slotEl = slotOf(r1);
    var ioEl = slotEl ? slotEl.querySelector('.io') : null;
    var ioLb = slotEl ? slotEl.querySelector('.io-sec .io-lb') : null;
    var ioTx = slotEl ? slotEl.querySelector('.io-sec .io-tx') : null;
    ck('P6d .slot .io 存在且 IN 段出现（.io-lb=IN、.io-tx 非空）',
      !!ioEl && !!ioLb && String(ioLb.textContent) === 'IN' && !!ioTx
        && String(ioTx.textContent).length > 0,
      'io=' + !!ioEl + ' lb=' + JSON.stringify(ioLb ? String(ioLb.textContent) : '(无)') +
      ' tx=' + JSON.stringify(ioTx ? String(ioTx.textContent).slice(0, 60) : '(无)'));
    diag('P6 披露：任务书希望 .io-tx 以 line1 开头，但 IN 段取的是 toolInputText() 的' +
      '字段优先级（command/file_path/path/…/content），Write 的 arguments 里 file_path' +
      ' 先命中 → IN 段显示的是路径。实测 .io-tx=' +
      JSON.stringify(ioTx ? String(ioTx.textContent) : '(无)'));

    /* ─────────── P7 ─────────── tool/result ─────────── */
    emit({ type: 'tool/result', params: { id: 'tc_x', ok: true, durationMs: 9,
      output: '[已写入: src/a.js (30000字节)]',
      diff: { added: 341, removed: 0, lines: [
        { type: 'add', no: 1, text: 'line1' }, { type: 'add', no: 2, text: 'line2' }] } } });
    await frames(2);
    ck('P7 tool/result 后 .meta 含 +341、data-st=ok',
      metaOf(r1).indexOf('+341') >= 0 && r1.getAttribute('data-st') === 'ok',
      'meta=' + JSON.stringify(metaOf(r1)) + ' st=' + r1.getAttribute('data-st'));

    /* ─────────── P8 ─────────── 老路径回归（一条 delta 都没有） ─────────── */
    var nBefore = tools().length;
    emit({ type: 'tool/call', params: { id: 'tc_y', name: 'Write', label: '写入',
      summary: 'old/p8.js', arguments: { file_path: 'old/p8.js', content: 'aaa\nbbb\n' } } });
    await sleep(60);
    emit({ type: 'tool/result', params: { id: 'tc_y', ok: true, durationMs: 7,
      output: '[已写入: old/p8.js (8字节)]',
      diff: { added: 4, removed: 1, lines: [
        { type: 'add', no: 1, text: 'aaa' }, { type: 'del', no: 2, text: 'bbb' }] } } });
    await frames(2);
    var r8 = rowByArg('old/p8.js');
    ck('P8 不发 delta 的老路径与改动前一致：新建 1 行、.io 存在、行头 +4 −1',
      tools().length === nBefore + 1 && !!r8
        && ioCount(r8) === 1
        && /\+4/.test(metaOf(r8)) && /−1/.test(metaOf(r8)),
      'tools=' + tools().length + '(before ' + nBefore + ')' +
      ' io=' + (r8 ? ioCount(r8) : '-') +
      ' meta=' + JSON.stringify(metaOf(r8)));

    /* ─────────── P9 ─────────── 打断收口 ─────────── */
    convo().innerHTML = '';
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(60);
    delta('tc_z', 'Write', '{"file_path":"cancel/p9.js","content":"zz');
    await frames(2);
    var r9before = rowByArg('cancel/p9.js');
    var meta9before = metaOf(r9before);
    /* 必须在 turn/end 之前取快照：settleRunningTools 会把 data-preparing 清掉，
       同一个节点之后再读就永远是 null（踩过这个坑，P9a 会变成假失败）。 */
    var wasPreparing = !!r9before
      && r9before.getAttribute('data-preparing') === '1'
      && meta9before.indexOf('准备中') >= 0;
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await frames(2);
    var r9 = rowByArg('cancel/p9.js');
    ck('P9 只发 delta 后被 turn/end{cancelled} 收口：不再停在「准备中」',
      !!r9 && !r9.hasAttribute('data-preparing') && metaOf(r9).indexOf('准备中') < 0,
      'preparing=' + (r9 && r9.getAttribute('data-preparing')) +
      ' meta=' + JSON.stringify(metaOf(r9)));
    ck('P9a 收口前它确实是「准备中」（不是空断言）', wasPreparing,
      'meta=' + JSON.stringify(meta9before) + ' preparing(收口前)=' + wasPreparing);

    /* ═══════════════ S1~S5：半截 JSON 扫描器的容错判据 ═══════════════ */
    convo().innerHTML = '';
    await sleep(40);
    var errS = window.__ERR.length;

    /* S1 尾部孤立 \ —— 停住等下一片（file_path 已在它前面闭合，应当显示） */
    delta('s1', 'Write', '{"file_path":"s1.js","content":"abc' + BS);
    await frames(2);
    var rs1 = rowByArg('s1.js');
    ck('S1 尾部孤立反斜杠：停在原地等下一片，不抛，已闭合的路径照常显示',
      !!rs1 && window.__ERR.length === errS && argOf(rs1) === 's1.js'
        && metaOf(rs1).indexOf('1 行') >= 0,
      'arg=' + JSON.stringify(argOf(rs1)) + ' meta=' + JSON.stringify(metaOf(rs1)) +
      ' err=' + (window.__ERR.length - errS));

    /* S2 半个 \uXXXX —— 先停住；补齐后按 4 位十六进制解码（行数会从 1 变 3） */
    delta('s2', 'Write', '{"file_path":"s2.js","content":"ab\\u12');
    await frames(2);
    var rs2 = rowByArg('s2.js');
    var metaS2a = metaOf(rs2);
    delta('s2', 'Write', '34\\n\\n"');
    await frames(2);
    ck('S2 半个 \\uXXXX 先停住等下一片，补齐后正常解码（内容=ab+U+1234+两换行 → 3 行）',
      !!rs2 && window.__ERR.length === errS
        && metaS2a.indexOf('1 行') >= 0 && metaOf(rs2).indexOf('3 行') >= 0,
      '前面=' + JSON.stringify(metaS2a) + ' 补齐后=' + JSON.stringify(metaOf(rs2)) +
      ' err=' + (window.__ERR.length - errS));

    /* S3 4 位但不是十六进制 —— 该字段作废，但绝不抛，行头照常可用 */
    delta('s3', 'Write', '{"file_path":"s3.js","content":"x' + BS + 'uZZZZy"}');
    await frames(2);
    var rs3 = rowByArg('s3.js');
    ck('S3 非法 \\uZZZZ：字段作废（不抛），行仍然可用',
      !!rs3 && window.__ERR.length === errS && argOf(rs3) === 's3.js'
        && metaOf(rs3).indexOf('准备中') >= 0,
      'arg=' + JSON.stringify(argOf(rs3)) + ' meta=' + JSON.stringify(metaOf(rs3)) +
      ' err=' + (window.__ERR.length - errS));

    /* S4 反斜杠奇偶性：4 个反斜杠（解码成 2 个）能闭合 */
    delta('s4', 'Write', '{"file_path":"a' + BS + BS + BS + BS + '","content":"q123"}');
    await frames(2);
    var rs4 = rows()[rows().length - 1];
    ck('S4a 奇偶性：JSON 里 "a\\\\"（4 个反斜杠）→ 解码成 2 个反斜杠并闭合',
      argOf(rs4) === 'a' + BS + BS, JSON.stringify(argOf(rs4)));

    /* S4b 反斜杠奇偶性：'b\' + 引号（转义引号）不能判成闭合 */
    delta('s5', 'Write', '{"file_path":"b' + BS + '"');
    await frames(2);
    var rs5 = rows()[rows().length - 1];
    ck('S4b 奇偶性：转义引号（反斜杠 + 引号）不判成闭合（路径不显示）',
      argOf(rs5) === '', JSON.stringify(argOf(rs5)));

    /* S5 字段顺序任意：content 先到、file_path 后到 */
    delta('s6', 'Write', '{"content":"l1\\nl2\\nl3","file_path":"z6.js"}');
    await frames(2);
    var rs6 = rowByArg('z6.js');
    ck('S5 字段乱序：content 在前、file_path 在后，两者都正确（3 行 + 路径）',
      !!rs6 && argOf(rs6) === 'z6.js' && metaOf(rs6).indexOf('3 行') >= 0,
      'arg=' + JSON.stringify(argOf(rs6)) + ' meta=' + JSON.stringify(metaOf(rs6)));

    if (window.__ERR.length) diag('页面里记录到的异常：' + JSON.stringify(window.__ERR));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-write-preparing</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__ERR=[];
window.onerror=function(m,s,l,c,e){window.__ERR.push(String(m));return false;};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-write-preparing');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
if (!browser) {
  console.error('找不到 Edge，无法跑无头验收');
  process.exit(3);
}
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=40000', '--dump-dom',
  '--window-size=760,900', 'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
