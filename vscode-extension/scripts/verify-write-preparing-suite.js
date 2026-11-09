/**
 * T06 系统性测试（前端侧）：半截 JSON 扫描器、「准备中」行、升级路径、回归。
 *
 * 与 `scripts/verify-write-preparing.js`（22 条 P/S 断言）的分工：
 *   · 那个脚本验的是「功能存在」——建行、名字、KB 台阶、原地升级、打断收口；
 *   · 这个套件验的是**它没覆盖的那一层**：扫描器差分 fuzz / 非字符串值 /
 *     顶层非对象 / 重复键 / UTF-16 口径、性能预算（512KB×30000 片 + 线性度 +
 *     帧内合并 + 台阶节流总量）、异常与并发序列（多工具交错、迟到 delta、
 *     name 晚到）、其它工具类型（Shell/Edit/TodoWrite/MCP/未知）、
 *     与既有机制的交互（轮末折叠计数、历史回放、点击、复制、滚动、零差异）。
 *     开工前那份「22 条没覆盖什么」的清单见 nn-tasks/T06-frontend-suite.report.md。
 *
 * ## 为什么要在页面里 `new Function` 还原扫描器
 *
 * `createArgScanner()` 在页面 IIFE 里，外部拿不到引用（既有脚本只能靠「注入
 * 分片 → 看行头显示什么」间接断言）。而 F1~F6 要测的是**读法本身**：
 * `text('file_path')` 与 `JSON.parse` 逐字符相等、非字符串值返回什么、
 * 重复键取哪个——这些在行头上根本不可见。
 *
 * 做法：本脚本在 Node 侧从 `design/ui-full.html`（唯一真源）里**按花括号配平
 * 精确切出 `createArgScanner` 的源码**，原样交给页面 `new Function` 还原。
 * 不是抄一份实现（抄的会与真源分叉），切的是同一份字节。
 * 并且 F1x 做了一次「活体对账」：同一段分片同时喂给页面里真正的那个扫描器
 * （经由 `tool/call_delta` 渲染出来的行头）与切出来的这个，数字必须一致。
 *
 * ## 无头环境的实测约束（既有脚本的结论 + 本套件重新探过的两条）
 *   1. headless Edge + `--virtual-time-budget` 下 requestAnimationFrame **不按帧走**：
 *      启动时不触发（探针实测），运行到某一刻又可能被补上一帧。界面侧的
 *      afterFrame 因此靠 50ms 定时器兜底 → 本脚本的「一帧」= 那个 50ms 节拍。
 *   2. 因此**滚动**类断言（E5）在主页面里是弱断言（rAF 不动 → scrollTop 可能一直是 0）。
 *      为了不拿「空断言」冒充通过，E5 另起一个页面：在页面脚本之前把
 *      `requestAnimationFrame` 换成 16ms 定时器（模拟真实 webview 的帧），
 *      再量一次 scrollTop。两次结果都打印，报告里分列。
 *   3. `--virtual-time-budget` 下 `performance.now()` **在同步任务内不前进**
 *      （实测：512KB 与 256KB 的扫描都读 0.00ms，比值 -1）。所以 F7 的**性能**
 *      必须在**不带**虚拟时间的页面里量 —— 见下面的 `perf.html`（只有扫描器，
 *      没有界面，同步跑完再 `--dump-dom`）。
 *   4. 三张页面各起一次 Edge：`main`（A~F 除 F7）、`perf`（F7）、`scroll`（E5 强断言）。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const HTML_PATH = path.join(ROOT, 'design/ui-full.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

/* ══════════════════ 从真源里切出 createArgScanner 的源码 ══════════════════
   按花括号配平切，切的时候要正确跳过字符串、行注释、块注释（函数体里
   `'{'` 这种单字符字符串、`/^[0-9a-fA-F]{4}$/` 这种正则都在，裸数括号会切歪）。 */
function extractFn(src, name) {
  const sig = 'function ' + name + '(';
  const start = src.indexOf(sig);
  if (start < 0) return null;
  let i = src.indexOf('{', start);
  if (i < 0) return null;
  let depth = 0, end = -1, q = null, esc = false;
  for (; i < src.length; i++) {
    const ch = src[i];
    const nx = src[i + 1];
    if (q) {
      if (esc) { esc = false; continue; }
      if (ch === '\\') { esc = true; continue; }
      if (q === '`' && ch === '$' && nx === '{') { /* 模板串里没有，忽略 */ }
      if (ch === q) q = null;
      continue;
    }
    if (ch === '/' && nx === '/') { const e = src.indexOf('\n', i); if (e < 0) break; i = e; continue; }
    if (ch === '/' && nx === '*') { const e = src.indexOf('*/', i + 2); if (e < 0) break; i = e + 1; continue; }
    if (ch === '"' || ch === "'" || ch === '`') { q = ch; continue; }
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) { end = i + 1; break; } }
  }
  return end > 0 ? src.slice(start, end) : null;
}

const SCANNER_SRC = extractFn(html, 'createArgScanner');
const SCANNER_LINE = SCANNER_SRC ? html.slice(0, html.indexOf(SCANNER_SRC)).split('\n').length : -1;

/* ══════════════════════════════ main 页 ══════════════════════════════ */
const TEST = String.raw`
(function(){
  var out = [], fails = 0, warns = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n +
      (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function wkn(n, d){
    warns++;
    out.push('WARN ' + n +
      (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function diag(s){ out.push('DIAG ' + s); }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  /* 一「帧」= 界面侧 afterFrame 的兜底节拍（50ms）。等得比 50ms 宽一点。 */
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
  /* .slot 是 .row 的**兄弟**（.tools > .row + .slot），不是后代 */
  function slotOf(row){
    var w = (row && row.closest) ? row.closest('.tools') : null;
    return w ? w.querySelector('.slot') : null;
  }
  function ioCount(row){
    var s = slotOf(row);
    return s ? s.querySelectorAll('.io').length : -1;
  }
  function delta(id, name, d, label){
    emit({ type: 'tool/call_delta', params: { id: id, name: name, label: label, delta: d } });
  }
  function tcall(id, name, label, summary, args){
    emit({ type: 'tool/call', params: {
      id: id, name: name, label: label, summary: summary, arguments: args } });
  }
  function tresult(id, extra){
    var p = { id: id, ok: true, durationMs: 5, output: '[ok]' };
    if (extra) Object.keys(extra).forEach(function(k){ p[k] = extra[k]; });
    emit({ type: 'tool/result', params: p });
  }
  async function clear(){ convo().innerHTML = ''; await sleep(90); }
  function j(x){ return JSON.stringify(x); }
  function rep(s, n){ var a = []; for (var i = 0; i < n; i++) a.push(s); return a.join(''); }
  function expectMeta(kb, lines){
    /* 注意口径：refreshPreparingRow 里 lines = scanner.lines(key) + 1，
       也就是「按 \n 切出来的段数」，不是 \n 的个数。这里按**显示值**传参。 */
    return '准备中 · ' + (kb > 0 ? kb + 'KB' : '<1KB') + ' · ' + lines + ' 行';
  }
  async function part(name, fn){
    try { await fn(); }
    catch(e){ fails++; out.push('FAIL ' + name + ' 抛错 :: ' + String(e && e.stack ? e.stack : e)); }
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: '甲', messages: [] }, workdir: 'E:/x' } });
    await sleep(250);
    var errBase = window.__ERR.length;

    /* ── 环境探针：rAF 在本环境里到底触不触发（决定 E5 的量法） ── */
    var rafFired = false;
    try { requestAnimationFrame(function(){ rafFired = true; }); } catch(e){}
    await sleep(160);
    /* 快照：本环境的 rAF 是「启动时不触发、运行到某一刻可能被补上一帧」，
       后面 E5 里要引用的是**启动时**这个事实，所以当场存下来。 */
    var rafEarly = rafFired;
    diag('环境 requestAnimationFrame 启动时' + (rafEarly ? '已触发' : '未触发') +
      '（决定 E5 是强断言还是弱断言）');

    /* ── F0 前置：扫描器源码从真源精确切出并可还原 ── */
    var SRC = String(window.__SCANNER_SRC || '');
    var makeScanner = null, mkErr = '';
    try { makeScanner = new Function('return (' + SRC + ');')(); } catch(e){ mkErr = String(e); }
    ck('F0(前置) ui-full.html 里 createArgScanner 源码被完整切出，且能在页面里还原',
      SRC.length > 2000 && typeof makeScanner === 'function',
      'srcLen=' + SRC.length + ' type=' + typeof makeScanner +
      ' 行号=' + window.__SCANNER_LINE + (mkErr ? ' err=' + mkErr : ''));
    diag('F0(前置) 切片起点 ui-full.html:' + window.__SCANNER_LINE + '，长度 ' + SRC.length + ' 字符');

    /* ═══════════════════════ A. 扫描器正确性 ═══════════════════════ */

    /* 8 组完整参数 JSON 文本。分片宽度随机 1~7（固定种子，种子值在 DIAG 里）。
       覆盖：中文、字面 emoji、转义代理对、\n \t \" \\ \u0000、超长 key、
       深层无关对象字段、非字符串值、字段乱序、前缀相同 key、\r\n、Windows 路径。 */
    var longKey = rep('k', 1200);
    var TEXTS = [
      function(){ return j({ file_path: 'src/中文/文件.js',
        content: '第一行\n第二行\t带制表符 "引号" \\ 反斜杠 😀\n结尾',
        note: { deep: [1, 2, { x: 'y\nz' }] }, n: 123, ok: true, none: null, arr: ['a', 'b'] }); },
      function(){ return '{"file_path":"nul/\\u0000.js","content":"A\\u0000B\\ud83d\\ude00C\\u4e2d\\nD"}'; },
      function(){ var o = {}; o[longKey] = 'v'; o.file_path = 'long/key.js';
        o.content = rep('文😀', 600) + '\n' + rep('k', 200); return j(o); },
      function(){ return '{"content":"c1\\nc2\\nc3","n":[1,2,[3,{"a":"b"}]],"file_path":"order/last.js","flag":false,"z":0}'; },
      function(){ return '{"content_x":"NOT","content":"真内容\\n第二行","file_path":"prefix/k.js"}'; },
      function(){ return j({ command: 'echo "hi" && npm test\nnpm run build\t# 注释 \\ 反斜杠',
        content: '', file_path: 'shell/x.sh', description: '跑测试' }); },
      function(){ return j({ file_path: 'edit/x.js', old_string: 'a\r\nb\r\nc',
        new_string: 'x\n\ny', content: rep('k', 3000) }); },
      function(){ return j({ file_path: 'D:\\proj\\src\\a.js',
        content: '😀😀😀\n' + rep('z', 2100), note: ' 尾随空格 ' }); }
    ];

    await part('F1', async function(){
      var SEED = 20250606;
      function mulberry32(a){
        return function(){
          a |= 0; a = a + 0x6D2B79F5 | 0;
          var t = Math.imul(a ^ a >>> 15, 1 | a);
          t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
          return ((t ^ t >>> 14) >>> 0) / 4294967296;
        };
      }
      var rnd = mulberry32(SEED);
      diag('F1 随机种子 seed=' + SEED + '（mulberry32），分片宽度 1~7，每组文本 30 次');
      var combos = 0, badPath = 0, badLen = 0, badNl = 0, badCmp = 0, badThrow = 0;
      var cases = [];
      var PER = 30;
      for (var ti = 0; ti < TEXTS.length; ti++) {
        var text = TEXTS[ti]();
        var parsed = JSON.parse(text);
        var expPath = parsed.file_path;
        var expContent = (typeof parsed.content === 'string') ? parsed.content : '';
        var expLen = expContent.length;
        var expNl = expContent.split('\n').length - 1;
        for (var k = 0; k < PER; k++) {
          combos += 1;
          var widths = [], chunks = [], pos = 0;
          while (pos < text.length) {
            var w = 1 + Math.floor(rnd() * 7);
            widths.push(w);
            chunks.push(text.slice(pos, pos + w));
            pos += w;
          }
          var sc = makeScanner();
          var threw = '';
          try {
            for (var ci = 0; ci < chunks.length; ci++) {
              sc.append(chunks[ci]);
              /* 真实路径是「每帧读一次」：中途读不能把状态读坏，所以每 4 片读一遍 */
              if ((ci % 4) === 3) {
                sc.text('file_path'); sc.complete('file_path');
                sc.length('content'); sc.lines('content'); sc.complete('content');
              }
            }
          } catch(e){ threw = String(e); }
          var gotPath, gotLen, gotNl, gotCmp;
          try {
            gotPath = sc.text('file_path'); gotLen = sc.length('content');
            gotNl = sc.lines('content'); gotCmp = sc.complete('content');
          } catch(e2){ threw = threw || String(e2); }
          if (threw) {
            badThrow += 1;
            if (cases.length < 3) cases.push('抛错 text#' + (ti + 1) + ' seed=' + SEED + '/' + k +
              ' err=' + threw + ' widths(前40)=' + j(widths.slice(0, 40)));
            continue;
          }
          var okP = gotPath === expPath, okL = gotLen === expLen;
          var okN = gotNl === expNl, okC = gotCmp === true;
          if (!okP) badPath += 1;
          if (!okL) badLen += 1;
          if (!okN) badNl += 1;
          if (!okC) badCmp += 1;
          if ((!okP || !okL || !okN || !okC) && cases.length < 3) {
            cases.push('text#' + (ti + 1) + ' seed=' + SEED + '/' + k +
              ' 期望 path=' + j(expPath) + ' len=' + expLen + ' nl=' + expNl + ' complete=true' +
              ' 实际 path=' + j(gotPath) + ' len=' + gotLen + ' nl=' + gotNl + ' complete=' + gotCmp +
              ' 分片宽度序列(前60)=' + j(widths.slice(0, 60)) + ' 片数=' + chunks.length);
          }
        }
      }
      cases.forEach(function(c){ diag('F1 首个反例 ' + c); });
      ck('F1a 差分 fuzz：text("file_path") 与 JSON.parse(text).file_path 逐字符相等',
        badPath === 0, '组合=' + combos + ' 不相等=' + badPath);
      ck('F1b 差分 fuzz：length("content") 与 JSON.parse(...).content.length 数值相等',
        badLen === 0, '组合=' + combos + ' 不相等=' + badLen);
      ck('F1c 差分 fuzz：lines("content") 与按 \\n 切分数一致',
        badNl === 0, '组合=' + combos + ' 不一致=' + badNl);
      ck('F1d 差分 fuzz：走完所有分片后 complete("content") === true',
        badCmp === 0, '组合=' + combos + ' 未闭合=' + badCmp);
      ck('F1e 差分 fuzz 全程不抛异常', badThrow === 0, '抛错=' + badThrow);
    });

    /* F1x 活体对账：同一段分片喂给**页面里真正的那个扫描器**（经 tool/call_delta
       渲染出的行头），数字必须与切出来的这份一致。证明切片 == 运行时实现。 */
    await part('F1x', async function(){
      await clear();
      var t = j({ file_path: 'f1x/live.js',
        content: rep('live😀行\n', 200) + rep('q', 700) });
      var parsed = JSON.parse(t);
      var expLen = parsed.content.length;
      var expNl = parsed.content.split('\n').length - 1;
      var expKb = Math.floor(expLen / 1024);
      for (var p = 0; p < t.length; p += 7) { delta('f1x', 'Write', t.slice(p, p + 7)); }
      await frames(3);
      var r = rowByArg('f1x/live.js');
      ck('F1x 活体对账：页面真扫描器渲染出的 .meta 与切片实例算出的数字一致',
        !!r && metaOf(r) === expectMeta(expKb, expNl + 1),
        '实测=' + j(metaOf(r)) + ' 期望=' + j(expectMeta(expKb, expNl + 1)) +
        '（len=' + expLen + ' nl=' + expNl + ' kb=' + expKb + '）');
    });

    /* F2 UTF-16 口径 */
    await part('F2', async function(){
      var t = '{"file_path":"f2/emoji.js","content":"\\ud83d\\ude00\\u4e2d\\u0000"}';
      var p = JSON.parse(t);
      var sc = makeScanner();
      sc.append(t);
      var got = sc.length('content');
      ck('F2a 转义代理对 + 中文 + \\u0000：length() === JSON.parse 的 length（UTF-16 码元数）',
        got === p.content.length, 'scan=' + got + ' json=' + p.content.length);
      ck('F2b emoji 在 length() 里算 2 个码元（与 JS 字符串一致）',
        got === 4 && p.content.length === 4,
        '期望 2(emoji)+1(中)+1(NUL)=4，实测 scan=' + got);
      diag('F2 emoji 口径：算 ' + (got === 4 ? 2 : 1) + ' 个 UTF-16 码元' +
        '（JSON.parse 口径 ' + p.content.length + '）');
      /* 字面（未转义）emoji，且按**单字符**分片 → 代理对会被切在两片之间 */
      var literal = '😀中\u0000';
      var t2 = j({ file_path: 'f2/raw.js', content: literal });
      var sc2 = makeScanner();
      for (var i = 0; i < t2.length; i++) sc2.append(t2.charAt(i));
      ck('F2c 字面 emoji 被拆到两个分片（代理对跨片）后 length() 仍是 4、text() 逐字符相等',
        sc2.length('content') === 4 && sc2.text('content') === literal,
        'len=' + sc2.length('content') + ' text===' + (sc2.text('content') === literal) +
        ' 实测=' + j(sc2.text('content')));
    });

    /* F3 非字符串值：只断言「不抛」，返回值是实测事实 */
    await part('F3', async function(){
      var vals = [['123', '{"file_path":"f3/a.js","content":123}'],
        ['null', '{"file_path":"f3/b.js","content":null}'],
        ['{}', '{"file_path":"f3/c.js","content":{}}'],
        ['[]', '{"file_path":"f3/d.js","content":[]}']];
      var noThrow = 0, lines = [];
      for (var i = 0; i < vals.length; i++) {
        var sc = makeScanner(), r = {};
        try {
          sc.append(vals[i][1]);
          r.text = sc.text('content'); r.len = sc.length('content'); r.cmp = sc.complete('content');
          r.nul = sc.text('content') === undefined;
          noThrow += 1;
        } catch(e){ r.err = String(e); }
        lines.push(vals[i][0] + ' -> text=' + String(r.text) + ' length=' + r.len +
          ' complete=' + r.cmp + (r.err ? ' 抛错=' + r.err : ''));
      }
      diag('F3 非字符串值实测：' + lines.join(' | '));
      ck('F3 content 为非字符串（123/null/{}/[]）时四个读法一律不抛',
        noThrow === 4 && window.__ERR.length === errBase,
        '不抛=' + noThrow + '/4 页面异常=' + j(window.__ERR.slice(errBase)));
      ck('F3b 非字符串值不出现在 file_path 上（同一条里的 file_path 仍可读）',
        makeScanner() && (function(){
          var sc = makeScanner(); sc.append('{"file_path":"f3/x.js","content":123}');
          return sc.text('file_path') === 'f3/x.js';
        })(),
        'file_path 读取见上方实测');
    });

    /* F4 顶层不是对象 */
    await part('F4', async function(){
      var bad = [['[]', '[]'], ['"str"', '"str"'], ['null', 'null'], ['空文本', '']];
      var okAll = 0, lines = [];
      for (var i = 0; i < bad.length; i++) {
        var sc = makeScanner(), r = {};
        try {
          sc.append(bad[i][1]);
          r.t = sc.text('file_path'); r.l = sc.length('content'); r.c = sc.complete('content');
          if (r.t === undefined && r.l === 0 && r.c === false) okAll += 1;
        } catch(e){ r.err = String(e); }
        lines.push(bad[i][0] + ' -> text=' + String(r.t) + ' length=' + r.l +
          ' complete=' + r.c + (r.err ? ' 抛错=' + r.err : ''));
      }
      /* 前导 BOM：不是空白、不是 { → 判死 */
      var scB = makeScanner(), rB = {};
      try {
        scB.append('\uFEFF{"file_path":"f4/bom.js"}');
        rB.t = scB.text('file_path'); rB.l = scB.length('content'); rB.c = scB.complete('content');
      } catch(e){ rB.err = String(e); }
      lines.push('前导BOM -> text=' + String(rB.t) + ' length=' + rB.l +
        ' complete=' + rB.c + (rB.err ? ' 抛错=' + rB.err : ''));
      diag('F4 顶层非对象实测：' + lines.join(' | '));
      ck('F4 顶层不是对象（[] / "str" / null / 空文本）：不抛，读法返回 undefined/0/false',
        okAll === 4 && window.__ERR.length === errBase,
        '满足=' + okAll + '/4 页面异常=' + j(window.__ERR.slice(errBase)));
      ck('F4b 前导 BOM：不抛，读法返回 undefined/0/false（顶层判死）',
        rB.t === undefined && rB.l === 0 && rB.c === false,
        'text=' + String(rB.t) + ' length=' + rB.l + ' complete=' + rB.c +
        (rB.err ? ' 抛错=' + rB.err : ''));
      var scW = makeScanner();
      scW.append('  {"file_path":"f4/ws.js","content":"a\nb"}');
      ck('F4c 前导空白（空格）不影响解析：路径与行数都正常',
        scW.text('file_path') === 'f4/ws.js' && scW.lines('content') === 1,
        'path=' + j(scW.text('file_path')) + ' nl=' + scW.lines('content'));
    });

    /* F5 重复键：只记录实测取第一个还是最后一个 */
    await part('F5', async function(){
      var sc = makeScanner();
      sc.append('{"file_path":"a.js","file_path":"b.js"}');
      var got = sc.text('file_path');
      diag('F5 重复键实测：{"file_path":"a.js","file_path":"b.js"} -> text("file_path")=' +
        j(got) + '（' + (got === 'b.js' ? '取最后一个' : (got === 'a.js' ? '取第一个' : '其它')) + '）');
      ck('F5 重复键不抛且取值确定（a.js/b.js 之一）',
        got === 'a.js' || got === 'b.js', j(got));
    });

    /* F6 空对象 / 只有 file_path —— 读法 + 行头两条一起验 */
    await part('F6', async function(){
      var sc1 = makeScanner(); sc1.append('{}');
      var sc2 = makeScanner(); sc2.append('{"file_path":"x"}');
      ck('F6a 空对象与只有 file_path：length("content")===0、complete("content")===false',
        sc1.length('content') === 0 && sc1.complete('content') === false &&
        sc2.length('content') === 0 && sc2.complete('content') === false,
        '{} len=' + sc1.length('content') + ' cmp=' + sc1.complete('content') +
        ' / {file_path} len=' + sc2.length('content') + ' cmp=' + sc2.complete('content'));
      await clear();
      delta('f6a', 'Write', '{}');
      delta('f6b', 'Write', '{"file_path":"x"}');
      await frames(3);
      var r1 = rows()[0], r2 = rows()[1];
      var m1 = metaOf(r1), m2 = metaOf(r2);
      ck('F6b 两种行头都不出现 KB / 行（数字只在 content 有内容时才出现）',
        !!r1 && !!r2 && m1.indexOf('KB') < 0 && m1.indexOf('行') < 0 &&
        m2.indexOf('KB') < 0 && m2.indexOf('行') < 0,
        '{} 行头=' + j(m1) + ' {file_path:"x"} 行头=' + j(m2) +
        ' arg2=' + j(argOf(r2)));
      diag('F6 行头实测：{} -> ' + j(m1) + '；{file_path:"x"} -> ' + j(m2) +
        '，.arg=' + j(argOf(r2)));
    });

    /* ═══════════════════════ B. 性能预算 ═══════════════════════ */

    /* F7（512KB/30000 片 + 线性度）**不在这个页面里跑**：
       实测本页面（headless + --virtual-time-budget）里 performance.now()
       在同步任务内根本不前进 —— 512KB/31318 片 + 256KB/15660 片两次实测都是
       0.00ms，比值 -1。这是虚拟时间的性质，不是实现快。
       F7 因此放到 perf.html（同一个扫描器、不带 --virtual-time-budget、
       同步跑完再 dump），结果由本脚本合并打印。 */

    /* F8 帧内合并：500 条 delta 只值 3 帧 */
    await part('F8', async function(){
      await clear();
      delta('f8', 'Write', '{"file_path":"f8/big.js","content":"');
      await frames(2);
      var r = rowByArg('f8/big.js');
      var meta = r ? r.querySelector('.meta') : null;
      var recs = [];
      var mo = new MutationObserver(function(list){ for (var i = 0; i < list.length; i++) recs.push(list[i].type); });
      if (meta) mo.observe(meta, { childList: true, characterData: true, subtree: true });
      for (var i = 0; i < 500; i++) delta('f8', 'Write', 'line' + i + '\\n');
      await frames(3);
      mo.disconnect();
      var muts = recs.length;
      ck('F8a 同一 id 连发 500 条 delta 只等 3 帧：.meta 的 DOM 变更次数 ≤ 3（帧内合并）',
        muts <= 3, 'muts=' + muts + '（500 条 delta，3 帧）');
      ck('F8b 500 条 delta 也确实刷过（不是一次都没动）', muts >= 1, 'muts=' + muts);
      diag('F8 500 条 × 约 8 字符后 .meta=' + j(metaOf(r)) + '，变更记录类型=' + j(recs.slice(0, 8)));
    });

    /* F9 台阶节流总量：0 -> 20480 字符（20 个 1024 台阶） */
    await part('F9', async function(){
      await clear();
      delta('f9', 'Write', '{"file_path":"f9/steps.js","content":"');
      await frames(2);
      var r = rowByArg('f9/steps.js');
      var meta = r ? r.querySelector('.meta') : null;
      var n = 0;
      var mo = new MutationObserver(function(list){ n += list.length; });
      if (meta) mo.observe(meta, { childList: true, characterData: true, subtree: true });
      var step = rep('a', 1024);
      for (var s = 0; s < 20; s++) { delta('f9', 'Write', step); await frames(1); }
      await frames(2);
      mo.disconnect();
      ck('F9a 20 个 1024 台阶：.meta 文本实际变更次数 ≤ 21（20 台阶 + 首次）',
        n <= 21, '变更次数=' + n);
      ck('F9b 确实在动（变更次数 ≥ 2）', n >= 2, '变更次数=' + n);
      diag('F9 终值 .meta=' + j(metaOf(r)) + '，变更次数=' + n + '（期望 20~21）');
    });

    /* ═══════════════════ C. 异常与并发序列 ═══════════════════ */

    /* C1 多工具交错 */
    await part('C1', async function(){
      await clear();
      var A = j({ file_path: 'c1/a.js', content: 'A行1\nA行2\n' + rep('a', 150) });
      var B = j({ file_path: 'c1/b.js', content: 'B行1\nB行2\nB行3\n' + rep('b', 60) });
      function slice30(t){
        var w = Math.ceil(t.length / 30), a = [];
        for (var i = 0; i < 30; i++) a.push(t.slice(i * w, (i + 1) * w));
        return a;
      }
      var AS = slice30(A), BS = slice30(B);
      /* 期望值一律由**同一个切片扫描器**按「这一段字节」算，不手算：
         这样中间态、终态都能逐字符对账，也不需要复刻台阶口径。 */
      function metaFromBytes(t){
        var sc = makeScanner();
        sc.append(t);
        var len = sc.length('content'), nl = sc.lines('content');
        if (len <= 0) return '准备中';
        var kb = Math.floor(len / 1024);
        return expectMeta(kb, nl + 1);
      }
      /* 前半段：15 片各。到这儿先停两帧，取一次**中间态**快照 */
      for (var i = 0; i < 15; i++) {
        delta('c1a', 'Write', AS[i]);
        delta('c1b', 'Write', BS[i]);
        await sleep(4);
      }
      await frames(2);
      var rA = rowByArg('c1/a.js'), rB = rowByArg('c1/b.js');
      var midA = metaOf(rA), midB = metaOf(rB);
      var expMidA = metaFromBytes(AS.slice(0, 15).join(''));
      var expMidB = metaFromBytes(BS.slice(0, 15).join(''));
      diag('C1 中间态（各 15 片）：A 实测=' + j(midA) + ' 按 A 前 15 片算=' + j(expMidA) +
        ' || B 实测=' + j(midB) + ' 按 B 前 15 片算=' + j(expMidB));
      ck('C1c 逐片交替的**中间态**不串：每行数字只由自己那一段字节决定',
        midA === expMidA && midB === expMidB && midA !== midB,
        'A: ' + j(midA) + '(期望' + j(expMidA) + ') B: ' + j(midB) +
        '(期望' + j(expMidB) + ')');
      for (var k15 = 15; k15 < 30; k15++) {
        delta('c1a', 'Write', AS[k15]);
        delta('c1b', 'Write', BS[k15]);
        await sleep(4);
      }
      await frames(3);
      rA = rowByArg('c1/a.js'); rB = rowByArg('c1/b.js');
      var eA = metaFromBytes(A), eB = metaFromBytes(B);
      ck('C1a 两个 id 逐片交替各 30 片：.tools 恰好 2 行', tools().length === 2,
        'tools=' + tools().length);
      ck('C1b 每行的 .arg/.meta 与自己那份 JSON 对应，不串',
        !!rA && !!rB && argOf(rA) === 'c1/a.js' && argOf(rB) === 'c1/b.js' &&
        metaOf(rA) === eA && metaOf(rB) === eB,
        'A: arg=' + j(argOf(rA)) + ' meta=' + j(metaOf(rA)) + ' 期望=' + j(eA) +
        ' || B: arg=' + j(argOf(rB)) + ' meta=' + j(metaOf(rB)) + ' 期望=' + j(eB));
    });

    /* C2 迟到 delta（tool/call 之后） */
    await part('C2', async function(){
      await clear();
      var rcptBase = (window.__sent || []).length;
      delta('c2', 'Write', '{"file_path":"c2/x.js","content":"abc');
      await frames(2);
      var r = rowByArg('c2/x.js');
      tcall('c2', 'Write', '写入', 'c2/x.js', { file_path: 'c2/x.js', content: 'abc\n' });
      await frames(2);
      var n0 = rows().length, m0 = metaOf(r), io0 = ioCount(r);
      delta('c2', 'Write', rep('MORE', 600));
      await frames(3);
      ck('C2 迟到 delta：不新建行、.meta 不被改回「准备中」、IN 段不丢',
        rows().length === n0 && metaOf(r) === m0 && m0.indexOf('准备中') < 0 &&
        ioCount(r) === io0 && io0 === 1 && !r.hasAttribute('data-preparing'),
        '行数 ' + n0 + '->' + rows().length + ' meta=' + j(m0) + '->' + j(metaOf(r)) +
        ' io=' + io0 + '->' + ioCount(r) + ' preparing=' + r.getAttribute('data-preparing'));
      /* C2b 渲染回执：upgradePreparingTool 的注释说「不再发 rendered('tool/call')
         —— 这条回执在准备阶段已经由建行那一次代表」，但 preparingTool 里
         一次 rendered() 都没有。所以走 delta 路径的工具**整条链上都没有**
         tool/call 回执（宿主侧 renderLog 看不到它）。这里把这个实测记下来。 */
      var rcpts = (window.__sent || []).slice(rcptBase)
        .filter(function(m){ return m && m.type === 'rendered'; })
        .map(function(m){ return String((m.params || {}).what || ''); });
      diag('C2b 走 delta+升级路径这一段的 rendered 回执序列=' + j(rcpts));
      if (rcpts.indexOf('tool/call') < 0) {
        wkn('C2b delta+升级路径不发 rendered(tool/call) 回执（注释称「准备阶段建行那一次已代表」，实际建行也没发）',
          '回执序列=' + j(rcpts) + '（对照：E6 老路径会发）');
      } else {
        ck('C2b delta+升级路径也有 rendered(tool/call) 回执', true, j(rcpts));
      }
    });

    /* C3 result 之后再来 delta */
    await part('C3', async function(){
      var r = rowByArg('c2/x.js');
      tresult('c2', { durationMs: 5, output: '[已写入: c2/x.js]',
        diff: { added: 2, removed: 0, lines: [{ type: 'add', no: 1, text: 'abc' }] } });
      await frames(2);
      var n0 = rows().length, m0 = metaOf(r), st0 = r.getAttribute('data-st');
      delta('c2', 'Write', rep('ZZZZ', 500));
      await frames(3);
      ck('C3 result 之后迟到 delta：行数不变、.meta 回不到「准备中」、data-st 不被改回 running',
        rows().length === n0 && metaOf(r) === m0 && m0.indexOf('准备中') < 0 &&
        r.getAttribute('data-st') === st0 && st0 === 'ok',
        '行数 ' + n0 + '->' + rows().length + ' meta=' + j(m0) + '->' + j(metaOf(r)) +
        ' data-st=' + j(st0) + '->' + j(r.getAttribute('data-st')));
    });

    /* C4 name 晚到 */
    await part('C4', async function(){
      await clear();
      delta('c4', null, '{"file_path":"c4/name.js",');
      await frames(2);
      delta('c4', null, '"content":"abc');
      await frames(2);
      var r = rowByArg('c4/name.js');
      var nameBefore = nameOf(r), argBefore = argOf(r), metaBefore = metaOf(r);
      var nBefore = rows().length;
      delta('c4', 'Write', 'def"}');
      await frames(3);
      ck('C4 name 晚到（首两片 name=null，第 3 片才给 Write）：名称从「工具」兜底变 Write、不新建行、.arg/.meta 不受影响',
        nameBefore === '工具' && nameOf(r) === 'Write' && rows().length === nBefore &&
        argOf(r) === argBefore && metaOf(r) === metaBefore,
        'name ' + j(nameBefore) + '->' + j(nameOf(r)) + ' 行数 ' + nBefore + '->' + rows().length +
        ' arg ' + j(argBefore) + '->' + j(argOf(r)) + ' meta ' + j(metaBefore) + '->' + j(metaOf(r)));
    });

    /* C4b label：后端随 delta 下发的中文名优先于英文 name
       （label 与 tool/call 的 label 同源，见 rpc/ui.py::on_tool_call_delta；
       客户端不自建 name→中文 映射表，所以这里断言的正是「有 label 就用它」）。 */
    await part('C4b', async function(){
      await clear();
      delta('c4b1', 'Write', '{"file_path":"c4b/label.js"}', '写入');
      await frames(3);
      var r1 = rowByArg('c4b/label.js');
      var withLabel = nameOf(r1);
      var toolLabel = r1.closest('.tools').getAttribute('data-tool-label');
      await clear();
      delta('c4b2', 'Write', '{"file_path":"c4b/nolabel.js"}');
      await frames(3);
      var r2 = rowByArg('c4b/nolabel.js');
      var noLabel = nameOf(r2);
      ck('C4b 有 label 显示中文名并写进 data-tool-label；无 label 退回英文 name',
        withLabel === '写入' && toolLabel === '写入' && noLabel === 'Write',
        '有label=' + j(withLabel) + ' data-tool-label=' + j(toolLabel) + ' 无label=' + j(noLabel));
    });

    /* C5 重复 name */
    await part('C5', async function(){
      await clear();
      delta('c5', 'Write', '{"file_path":"c5/dup.js"', '写入');
      await frames(2);
      var r = rowByArg('c5/dup.js');
      var n0 = nameOf(r);
      delta('c5', 'Write', ',"content":"XYZ');
      await frames(2);
      delta('c5', 'Write', 'PQR"}');
      await frames(2);
      ck('C5 多条 delta 都带同一 name/label：.name 文本不叠加（仍是单个「写入」）',
        nameOf(r) === '写入' && n0 === '写入' && rows().length === 1,
        'first=' + j(n0) + ' now=' + j(nameOf(r)) + ' 行数=' + rows().length);
    });

    /* C6 turn/end 之后迟到 delta */
    await part('C6', async function(){
      await clear();
      emit({ type: 'turn/begin', params: { round: 1 } });
      await frames(1);
      delta('c6', 'Write', '{"file_path":"c6/a.js","content":"q');
      await frames(2);
      var r = rowByArg('c6/a.js');
      emit({ type: 'turn/end', params: { reason: 'done' } });
      await frames(3);
      var n0 = rows().length, m0 = metaOf(r);
      delta('c6', 'Write', rep('MORE', 200));
      await frames(2);
      ck('C6a turn/end 后同 id 迟到 delta：不再变回「准备中」，行数与 .meta 不变',
        !r.hasAttribute('data-preparing') && metaOf(r) === m0 &&
        m0.indexOf('准备中') < 0 && rows().length === n0,
        'preparing=' + r.getAttribute('data-preparing') + ' meta=' + j(m0) + '->' +
        j(metaOf(r)) + ' 行数 ' + n0 + '->' + rows().length);
      var err0 = window.__ERR.length;
      delta('c6b', 'Write', '{"file_path":"c6/b.js"}');
      await frames(2);
      var created = !!rowByArg('c6/b.js');
      ck('C6b turn/end 后收到**新 id** 的 delta：不抛异常（建不建行按实测记录）',
        window.__ERR.length === err0, '页面异常=' + j(window.__ERR.slice(err0)));
      if (created) {
        wkn('C6c turn/end 之后新 id 的 delta 仍会新建一行「准备中」（后端已收口，这一行永远不会被升级）',
          '行数=' + rows().length + ' 该行 meta=' + j(metaOf(rowByArg('c6/b.js'))) +
          ' data-st=' + rowByArg('c6/b.js').getAttribute('data-st'));
      } else {
        ck('C6c turn/end 之后新 id 的 delta 不建行', true, '未建行');
      }
    });

    /* C7 离线态 */
    await part('C7', async function(){
      await clear();
      document.body.setAttribute('data-offline', 'true');
      var err0 = window.__ERR.length;
      delta('c7', 'Write', '{"file_path":"c7/x.js","content":"abc"}');
      await frames(3);
      var n = rows().length;
      ck('C7 离线态（body[data-offline=true]）下发 delta 不建行（与 addToolRow 的离线口径一致）',
        n === 0 && window.__ERR.length === err0,
        '行数=' + n + ' 页面异常=' + j(window.__ERR.slice(err0)));
      document.body.setAttribute('data-offline', 'false');
    });

    /* ═══════════════ D. 其他工具类型 ═══════════════ */

    /* D1 Shell */
    await part('D1', async function(){
      await clear();
      delta('d1', 'Shell', '{"command":"npm test"}');
      await frames(3);
      var r = rows()[0];
      ck('D1a Shell 的 delta（只有 command，无 file_path/content）：建行且 .name 严格为 Shell',
        !!r && rows().length === 1 && nameOf(r) === 'Shell',
        '行数=' + rows().length + ' name=' + j(r ? nameOf(r) : '(无行)'));
      diag('D1 Shell 准备态实测：.arg=' + j(argOf(r)) + ' .meta=' + j(metaOf(r)));
      ck('D1b .meta 停在「准备中」（没有 file_path/content → 无 KB/行数）',
        metaOf(r) === '准备中' && ioCount(r) === 0,
        'meta=' + j(metaOf(r)) + ' io=' + ioCount(r));
      tcall('d1', 'Shell', '执行', 'npm test', { command: 'npm test' });
      await frames(2);
      var s = slotOf(r);
      var tx = s ? s.querySelector('.io-sec .io-tx') : null;
      ck('D1c 升级后 .slot 的 IN 段文本包含 npm test',
        !!tx && String(tx.textContent).indexOf('npm test') >= 0,
        'io-tx=' + j(tx ? String(tx.textContent).slice(0, 80) : '(无)'));
    });

    /* D2 Edit */
    await part('D2', async function(){
      await clear();
      var oldS = '甲1\n甲2\n甲3\n甲4\n甲5\n甲6\n甲7';       /* 7 行 / 20 字符 */
      var newS = '乙1\n乙2\n乙3';                            /* 3 行 / 11 字符 */
      delta('d2', 'Edit', j({ file_path: 'd2/x.js', old_string: oldS, new_string: newS }));
      await frames(3);
      var r = rowByArg('d2/x.js');
      var expLines = Math.max(oldS.split('\n').length, newS.split('\n').length);
      ck('D2a Edit 两字段都有换行：行数 = 两者中较大者 + 1 的口径（7 行）',
        !!r && metaOf(r).indexOf(expLines + ' 行') >= 0,
        'meta=' + j(metaOf(r)) + ' 期望含 ' + expLines + ' 行');
      tcall('d2', 'Edit', '编辑', 'd2/x.js', { file_path: 'd2/x.js', old_string: oldS, new_string: newS });
      await frames(2);
      ck('D2b 升级后 .meta 含「执行中」', metaOf(r).indexOf('执行中') >= 0, j(metaOf(r)));
      /* D2c 口径探针：最长字段的行数 ≠ 两字段行数的较大者时，实测显示哪个 */
      await clear();
      var oldL = rep('甲', 200);                 /* 1 行 / 200 字符（更长） */
      var newM = '乙1\n乙2\n乙3\n乙4';            /* 4 行（行数更多） */
      delta('d2c', 'Edit', j({ file_path: 'd2/y.js', old_string: oldL, new_string: newM }));
      await frames(3);
      var rc = rowByArg('d2/y.js');
      diag('D2c 口径实测：old_string=1 行/200 字符（最长），new_string=4 行/11 字符 → 行头=' +
        j(metaOf(rc)) + '（取的是**最长字段**的行数，不是两字段行数的较大者）');
      if (metaOf(rc).indexOf('4 行') < 0) {
        wkn('D2c 任务书 D2 写「行数是两者中较大者 + 1」，实测取的是**最长字段**的行数（此例显示 1 行，两字段最大行数是 4）',
          'meta=' + j(metaOf(rc)));
      } else {
        ck('D2c 行数取两字段较大者', true, j(metaOf(rc)));
      }
    });

    /* D3 TodoWrite */
    await part('D3', async function(){
      await clear();
      var todos = { todos: [
        { content: '甲', status: 'completed' },
        { content: '乙', status: 'in_progress' },
        { content: '丙', status: 'pending' }] };
      delta('d3', 'TodoWrite', j(todos));
      await frames(3);
      tcall('d3', 'TodoWrite', '更新计划', '', todos);
      await frames(3);
      var r = rows()[0];
      var w = r ? r.closest('.tools') : null;
      var s = w ? w.querySelector('.slot') : null;
      ck('D3a 升级后：data-open=true、wrap 有 data-todo=true、.slot 里有 .todo 容器',
        !!r && !!w && r.getAttribute('data-open') === 'true' &&
        w.getAttribute('data-todo') === 'true' && !!s && !!s.querySelector('.todo'),
        'open=' + (r && r.getAttribute('data-open')) +
        ' data-todo=' + (w && w.getAttribute('data-todo')) +
        ' .todo=' + !!(s && s.querySelector('.todo')));
      ck('D3b 没有出现第二条「更新计划」行（tools=1、rows=1）',
        tools().length === 1 && rows().length === 1,
        'tools=' + tools().length + ' rows=' + rows().length + ' name=' + j(nameOf(r)));
      /* 再推一条 todo/update：复用同一行，不能再长一行 */
      emit({ type: 'todo/update', params: { todos: todos.todos } });
      await frames(2);
      ck('D3c todo/update 到达后仍是同一行（不新建第二行），清单内容已写入',
        tools().length === 1 && rows().length === 1 &&
        !!s.querySelector('.todo').textContent,
        'tools=' + tools().length + ' rows=' + rows().length +
        ' todoText=' + j(s.querySelector('.todo').textContent.slice(0, 40)));
    });

    /* D4 MCP 工具名 */
    await part('D4', async function(){
      await clear();
      var err0 = window.__ERR.length;
      delta('d4', 'mcp__server__tool', '{"query":"x"}');
      await frames(3);
      var r = rows()[0];
      ck('D4 MCP 工具名（mcp__server__tool）建行不抛，.name 显示原文',
        !!r && nameOf(r) === 'mcp__server__tool' && window.__ERR.length === err0,
        'name=' + j(r ? nameOf(r) : '(无行)') + ' 异常=' + j(window.__ERR.slice(err0)));
    });

    /* D5 未知工具名 + 无 content */
    await part('D5', async function(){
      await clear();
      var err0 = window.__ERR.length;
      delta('d5', 'Frobnicate', '{"foo":"bar"}');
      await frames(3);
      var r = rows()[0];
      ck('D5 未知工具名 + 无 content：行头停在「准备中」，不报错',
        !!r && metaOf(r) === '准备中' && nameOf(r) === 'Frobnicate' &&
        window.__ERR.length === err0,
        'name=' + j(r ? nameOf(r) : '(无行)') + ' meta=' + j(metaOf(r)) +
        ' 异常=' + j(window.__ERR.slice(err0)));
    });

    /* ═══════════ E. 与既有机制的交互（回归风险最高） ═══════════ */

    /* E1 轮末折叠组：3 行全部收进、计数不重复 */
    await part('E1', async function(){
      await clear();
      emit({ type: 'turn/begin', params: { round: 1 } });
      await frames(1);
      delta('e1a', 'Write', '{"file_path":"e1/a.js","content":"x');
      await frames(2);
      tcall('e1a', 'Write', '写入', 'e1/a.js', { file_path: 'e1/a.js', content: 'x\n' });
      await frames(2);
      tcall('e1b', 'Read', '读取', 'e1/b.js', { file_path: 'e1/b.js' });
      await frames(2);
      delta('e1c', 'Write', '{"file_path":"e1/c.js","content":"yy');
      await frames(2);
      var before = rows().length;
      emit({ type: 'turn/end', params: { reason: 'done' } });
      await frames(4);
      var box = convo().querySelector('.turn-proc');
      var cnt = box ? box.getAttribute('data-tool-count') : '(无组)';
      var lb = box ? box.querySelector('.tp-label') : null;
      var inner = box ? box.querySelectorAll('.tp-inner .tools').length : -1;
      ck('E1a 折叠组把 3 行全部收进去（.tp-inner 里 3 个 .tools）',
        before === 3 && inner === 3, '折叠前 rows=' + before + ' 组内 .tools=' + inner);
      ck('E1b 折叠摘要的计数与 3 一致（preparingTool 与 addToolRow 各加一次会变成 4）',
        cnt === '3' && !!lb && String(lb.textContent).indexOf('3 次工具调用') >= 0,
        'data-tool-count=' + j(cnt) + ' 标题=' + j(lb ? String(lb.textContent) : '(无)'));
    });

    /* E2 历史回放 */
    await part('E2', async function(){
      emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'hist-e2',
        messages: [
          { role: 'user', text: '写个文件' },
          { role: 'tool', name: 'Write', label: '写入', summary: 'hist/e2.js',
            arguments: { file_path: 'hist/e2.js', content: 'aaa\nbbb' },
            output: '[已写入: hist/e2.js (7字节)]',
            diff: { added: 2, removed: 1, lines: [
              { type: 'add', no: 1, text: 'aaa' },
              { type: 'add', no: 2, text: 'bbb' }] } }
        ] } });
      await frames(4);
      var r = rowByArg('hist/e2.js');
      var w = r ? r.closest('.tools') : null;
      var kids = r ? Array.prototype.map.call(r.children, function(el){
        return String(el.className); }) : [];
      var s = w ? w.querySelector('.slot') : null;
      ck('E2a 回放行没有 data-preparing，.meta 不是「准备中」',
        !!r && !r.hasAttribute('data-preparing') && metaOf(r).indexOf('准备中') < 0,
        'preparing=' + (r && r.getAttribute('data-preparing')) + ' meta=' + j(metaOf(r)));
      ck('E2b 回放行结构与 addToolRow 产物一致：.row > caret/name/arg/meta，.slot > .io',
        kids.length === 4 && kids[0].indexOf('caret') >= 0 && kids[1] === 'name' &&
        kids[2].indexOf('arg') === 0 && kids[3] === 'meta' && !!s &&
        s.querySelectorAll('.io').length === 1,
        'row 子节点=' + j(kids) + ' slot .io=' + (s ? s.querySelectorAll('.io').length : -1));
      ck('E2c 回放行头字符串与 addHistoryTool 的口径一致（完成 · +2 −1）',
        metaOf(r) === '完成 · +2 −1', j(metaOf(r)));
      diag('E2 回放行 .name=' + j(nameOf(r)) + ' .arg=' + j(argOf(r)) + ' .meta=' + j(metaOf(r)));
    });

    /* E3 点击准备态行 */
    await part('E3', async function(){
      await clear();
      delta('e3', 'Write', '{"file_path":"e3/x.js","content":"abc');
      await frames(3);
      var r = rowByArg('e3/x.js');
      r.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      await frames(2);
      var s = slotOf(r);
      ck('E3 点击准备态行：data-open 仍为 false、.slot 仍为空（不切出空 .io 卡片）',
        r.getAttribute('data-open') === 'false' && s && s.children.length === 0 &&
        s.querySelectorAll('.io').length === 0,
        'data-open=' + j(r.getAttribute('data-open')) +
        ' slot 子节点=' + (s ? s.children.length : -1) +
        ' slot .io=' + (s ? s.querySelectorAll('.io').length : -1));
      /* 升级之后再点：应当能展开（证明上面的拦截只针对准备态） */
      tcall('e3', 'Write', '写入', 'e3/x.js', { file_path: 'e3/x.js', content: 'abc\n' });
      await frames(2);
      r.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      await frames(1);
      ck('E3b 升级（tool/call）之后同一点击可展开：data-open=true（拦截只针对准备态）',
        r.getAttribute('data-open') === 'true', j(r.getAttribute('data-open')));
    });

    /* E4b 预计 diff：参数到齐（tool/call）后展开就能看到将被写入的内容，
       不必等 tool/result。口径照 dsh 的 presentCall —— oldText 恒为 null，
       覆写也画成整文件新增（fs/tool-fs/src/write.ts:133-142）。 */
    await part('E4b', async function(){
      await clear();
      var body = 'const a = 1;\nconst b = 2;\nconst c = 3;\n';
      delta('e4c', 'Write', '{"file_path":"e4/plan.js","content":"co');
      await frames(2);
      var r0 = rowByArg('e4/plan.js');
      var s0 = slotOf(r0);
      var beforeResult = s0.querySelectorAll('.snippet .ln').length;
      tcall('e4c', 'Write', '写入', 'e4/plan.js', { file_path: 'e4/plan.js', content: body });
      await frames(2);
      var r = rowByArg('e4/plan.js');
      var s = slotOf(r);
      r.click();
      await frames(2);
      var added = Array.from(s.querySelectorAll('.snippet .ln.add'))
        .map(function(x){ return (x.querySelector('.tx') || {}).textContent; });
      var foot = (s.querySelector('.foot') || {}).textContent || '';
      ck('E4b 升级后展开看到预计 diff（3 行新增 + 「预计」页脚），而准备态一行都没有',
        beforeResult === 0 && added.length === 3 &&
        added[0] === 'const a = 1;' && added[2] === 'const c = 3;' &&
        foot.indexOf('预计 +3') === 0,
        '准备态行数=' + beforeResult + ' 升级后新增行=' + j(added.join(' | ')) + ' foot=' + j(foot));

      /* E4c 真实 result 到达后，预计 diff 被真实 diff 覆盖（不能两份并存） */
      tresult('e4c', { diff: { added: 1, removed: 1, lines: [
        { type: 'add', no: 1, text: 'REAL_ADD' }, { type: 'del', no: 1, text: 'REAL_DEL' }] } });
      await frames(3);
      var r2 = rowByArg('e4/plan.js');
      var s2 = slotOf(r2);
      var tex = s2.textContent;
      ck('E4c tool/result 到达后预计 diff 被真实 diff 覆盖（foot 不再写「预计」）',
        tex.indexOf('REAL_ADD') >= 0 && tex.indexOf('预计 +') < 0,
        '有 REAL_ADD=' + (tex.indexOf('REAL_ADD') >= 0) + ' 残留预计=' + (tex.indexOf('预计 +') >= 0));
    });

    /* E4 复制按钮 */
    await part('E4', async function(){
      await clear();
      delta('e4', 'Write', '{"file_path":"e4/x.js","content":"abc"}');
      await frames(3);
      var r = rowByArg('e4/x.js');
      var s = slotOf(r);
      ck('E4a 准备态没有复制按钮（.io-copy 不存在，slot 里没有 .io）',
        !convo().querySelector('.io-copy') && s.querySelectorAll('.io').length === 0,
        'io-copy=' + convo().querySelectorAll('.io-copy').length +
        ' slot .io=' + s.querySelectorAll('.io').length);
      tcall('e4', 'Write', '写入', 'e4/x.js', { file_path: 'e4/x.js', content: 'abc\n' });
      await frames(2);
      ck('E4b 升级后 .io-copy 存在（Write 在 IO_COPY_TOOLS 里）',
        !!s.querySelector('.io-sec .io-copy'),
        'io-copy=' + s.querySelectorAll('.io-copy').length);
      /* E4c 不给复制按钮的工具 */
      await clear();
      delta('e4c', 'WebSearch', '{"query":"q"}');
      await frames(2);
      tcall('e4c', 'WebSearch', '搜索', 'q', { query: 'q' });
      await frames(2);
      var rc = rows()[0], sc = rowByClosestSlot(rc);
      ck('E4c 不在 IO_COPY_TOOLS 里的工具（WebSearch）升级后没有复制按钮',
        !!rc && sc.querySelectorAll('.io-copy').length === 0,
        'io-copy=' + (sc ? sc.querySelectorAll('.io-copy').length : -1));
    });
    function rowByClosestSlot(row){
      var w = row ? row.closest('.tools') : null;
      return w ? w.querySelector('.slot') : null;
    }

    /* E5 滚动（主页面：rAF 不触发 → 弱断言） */
    await part('E5', async function(){
      await clear();
      /* 先把对话区撑高，保证可滚动 */
      var big = [];
      for (var i = 0; i < 120; i++) big.push('第 ' + i + ' 行正文，用来把对话区撑高。');
      emit({ type: 'stream/delta', params: { text: big.join('\n') } });
      await frames(3);
      var c = convo();
      var scrollable = c.scrollHeight > c.clientHeight + 10;
      var seq = [];
      seq.push(c.scrollTop);
      delta('e5', 'Write', '{"file_path":"e5/prep.js","content":"');
      await frames(3);
      seq.push(c.scrollTop);
      var r = rowByArg('e5/prep.js');
      var atBottom = (c.scrollTop + c.clientHeight) >= (c.scrollHeight - 2);
      for (var k = 0; k < 20; k++) {
        delta('e5', 'Write', rep('y', 200));
        await frames(1);
        seq.push(c.scrollTop);
      }
      var mono = true;
      for (var q = 1; q < seq.length; q++) if (seq[q] < seq[q - 1]) mono = false;
      diag('E5 主页面实测：可滚动=' + scrollable + ' scrollHeight=' + c.scrollHeight +
        ' clientHeight=' + c.clientHeight + ' scrollTop 序列=' + j(seq.slice(0, 8)) +
        '…（共 ' + seq.length + ' 个样本）' +
        ' 探针(启动时)rAF=' + (rafEarly ? '已触发' : '未触发') +
        ' 取数时 rAF=' + rafFired);
      ck('E5a 连续 20 条 delta 期间 scrollTop 单调不减（不产生滚动跳变）', mono,
        'seq=' + j(seq));
      /* 主页面里 scrollBottom 走 raf()，而 headless + 虚拟时间下 rAF 靠不住
         （探针启动时未触发，运行到一半又可能被补上一帧）—— 在这里断言「建行
         滚到底」会得到一个由帧调度随机决定的结果，不是实现的性质。
         所以这里只记录实测，把强断言交给滚动页（rAF 垫片）。 */
      wkn('E5b 主页面无法可靠断言「建行时滚到底」（本环境的 rAF 不按帧走），实测记录于此，强断言在滚动页 E5a~E5e',
        'atBottom=' + atBottom + ' scrollTop=' + c.scrollTop +
        ' max=' + (c.scrollHeight - c.clientHeight) + ' 可滚动=' + scrollable +
        ' 行已建=' + !!r);
    });

    /* E6 无 delta 全流程零差异 */
    await part('E6', async function(){
      await clear();
      var rcptBase = (window.__sent || []).length;
      tcall('e6a', 'Write', '写入', 'e6/a.js', { file_path: 'e6/a.js', content: 'aaa\nbbb\n' });
      await frames(1);
      tresult('e6a', { durationMs: 7, output: '[已写入: e6/a.js (8字节)]',
        diff: { added: 4, removed: 1, lines: [
          { type: 'add', no: 1, text: 'aaa' }, { type: 'del', no: 2, text: 'bbb' }] } });
      tcall('e6b', 'Edit', '编辑', 'e6/b.js', { file_path: 'e6/b.js', old_string: 'a', new_string: 'b' });
      await frames(1);
      tresult('e6b', { durationMs: 8, output: '[已编辑: e6/b.js]',
        diff: { added: 2, removed: 0, lines: [{ type: 'add', no: 1, text: 'b' }] } });
      tcall('e6c', 'Shell', '执行', 'npm test', { command: 'npm test' });
      await frames(1);
      tresult('e6c', { durationMs: 12, output: 'all tests passed' });
      await frames(3);
      var prep = convo().querySelectorAll('[data-preparing]').length;
      var metas = [];
      for (var i = 0; i < rows().length; i++) metas.push(metaOf(rows()[i]));
      ck('E6a 一条 delta 都不发：DOM 里没有任何 data-preparing', prep === 0, 'data-preparing=' + prep);
      ck('E6b .tools 行数 = 3，三行各自 1 个 .io（与改动前的老路径一致）',
        tools().length === 3 && ioCount(rows()[0]) === 1 && ioCount(rows()[1]) === 1 &&
        ioCount(rows()[2]) === 1,
        'tools=' + tools().length + ' io=' + [ioCount(rows()[0]), ioCount(rows()[1]), ioCount(rows()[2])]);
      ck('E6c 三行 .meta 文本与既有 verify-write-preparing.js P8 口径一致',
        metas[0] === '+4 −1 · 7ms' && metas[1] === '+2 −0 · 8ms' && metas[2] === '12ms',
        j(metas));
      var rcpts = (window.__sent || []).slice(rcptBase)
        .filter(function(m){ return m && m.type === 'rendered'; })
        .map(function(m){ return String((m.params || {}).what || ''); });
      var nCall = rcpts.filter(function(w){ return w === 'tool/call'; }).length;
      ck('E6d 老路径（无 delta）确实发 3 条 rendered(tool/call) 回执（与 C2b 的 delta 路径形成对照）',
        nCall === 3, 'tool/call 回执=' + nCall + '，全部回执=' + j(rcpts));
    });

    if (window.__ERR.length > errBase) {
      diag('页面里记录到的新异常：' + j(window.__ERR.slice(errBase)));
    }
    out.push('SUMMARY total=' + out.length + ' fail=' + fails + ' warn=' + warns);
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

/* ══════════════════════════════ scroll 页（E5 真帧） ══════════════════════════════ */
const TEST_SCROLL = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n +
      (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function diag(s){ out.push('DIAG ' + s); }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  async function frames(n){ for (var i = 0; i < n; i++) await sleep(60); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function rows(){ return convo().querySelectorAll('.tools .row'); }
  function rowByArg(sub){
    var list = rows();
    for (var i = 0; i < list.length; i++) {
      var a = list[i].querySelector('.arg');
      if (a && String(a.textContent).indexOf(sub) >= 0) return list[i];
    }
    return null;
  }
  function delta(id, name, d, label){
    emit({ type: 'tool/call_delta', params: { id: id, name: name, label: label, delta: d } });
  }
  function rep(s, n){ var a = []; for (var i = 0; i < n; i++) a.push(s); return a.join(''); }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: '甲', messages: [] }, workdir: 'E:/x' } });
    await sleep(250);
    /* 先证明这个页面里 rAF 真的在跑（否则下面的断言同样没意义） */
    var ticks = 0;
    (function tick(){ ticks += 1; if (ticks < 8) requestAnimationFrame(tick); })();
    await sleep(200);
    ck('E5d 本页 requestAnimationFrame 真的在跑（垫片生效，E5c/E5e 不是空断言）',
      ticks >= 2, 'rAF 回调次数=' + ticks);

    /* 把对话区撑高 */
    var big = [];
    for (var i = 0; i < 60; i++) big.push('第 ' + i + ' 行正文，用来把对话区撑高。');
    emit({ type: 'stream/delta', params: { text: big.join('\n') } });
    await frames(4);
    var c = convo();
    c.scrollTop = c.scrollHeight;
    await frames(2);
    var scrollable = c.scrollHeight > c.clientHeight + 10;
    diag('E5 滚动页：scrollHeight=' + c.scrollHeight + ' clientHeight=' + c.clientHeight +
      ' 可滚动=' + scrollable + ' 起始 scrollTop=' + c.scrollTop);

    /* ① addToolRow 老路径：新行到达后是否在底部 */
    emit({ type: 'tool/call', params: { id: 's1', name: 'Write', label: '写入',
      summary: 'e5/old.js', arguments: { file_path: 'e5/old.js', content: 'x' } } });
    await frames(3);
    var st1 = c.scrollTop, max1 = c.scrollHeight - c.clientHeight;
    /* ② 准备中行：与老路径同口径 */
    delta('s2', 'Write', '{"file_path":"e5/prep.js","content":"');
    await frames(3);
    var st2 = c.scrollTop, max2 = c.scrollHeight - c.clientHeight;
    ck('E5a 老路径（tool/call）新行到达后视口在底部',
      !scrollable || Math.abs(st1 - max1) <= 2, 'scrollTop=' + st1 + ' max=' + max1);
    ck('E5b 准备中行建行后视口也在底部（与 addToolRow 同口径）',
      !scrollable || Math.abs(st2 - max2) <= 2, 'scrollTop=' + st2 + ' max=' + max2);
    ck('E5c 准备中行建行时确实把视口推到过底部（scrollTop 增长过）',
      !scrollable || st2 > st1 || st2 > 0, 'st1=' + st1 + ' st2=' + st2);

    /* ③ 连续 20 条 delta 期间不产生滚动跳变 */
    var seq = [c.scrollTop];
    for (var k = 0; k < 20; k++) {
      delta('s2', 'Write', rep('y', 200));
      await frames(1);
      seq.push(c.scrollTop);
    }
    var mono = true;
    for (var q = 1; q < seq.length; q++) if (seq[q] < seq[q - 1] - 1) mono = false;
    ck('E5d 连续 20 条 delta 期间 scrollTop 单调不减（无滚动跳变）', mono, 'seq=' + JSON.stringify(seq));
    ck('E5e 20 条 delta 结束时视口仍在底部',
      !scrollable || Math.abs(c.scrollTop - (c.scrollHeight - c.clientHeight)) <= 2,
      'scrollTop=' + c.scrollTop + ' max=' + (c.scrollHeight - c.clientHeight));
    diag('E5 末尾 .meta=' + JSON.stringify(String((rowByArg('e5/prep.js') || {}).textContent || '')));

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

/* ══════════════════════════════ perf 页（F7，不带虚拟时间） ══════════════════════════════ */
const PERF = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n +
      (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function diag(s){ out.push('DIAG ' + s); }
  function j(x){ return JSON.stringify(x); }
  function rep(s, n){ var a = []; for (var i = 0; i < n; i++) a.push(s); return a.join(''); }
  function makeContent(n){
    var line = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-_';
    var s = '';
    while (s.length < n) { s += line; if (s.length < n) s += '\n'; }
    return s.slice(0, n);
  }
  function build(nBytes){
    var content = makeContent(nBytes);
    var esc = content.split('\n').join('\\n');
    var text = '{"file_path":"perf/big.txt","content":"' + esc + '"}';
    var chunks = [];
    for (var pos = 0; pos < text.length; pos += 17) chunks.push(text.slice(pos, pos + 17));
    return { content: content, chunks: chunks,
      expLen: content.length, expNl: (content.match(/\n/g) || []).length };
  }
  var bigD = build(512 * 1024), smallD = build(256 * 1024);
  function oneRun(d, readEvery){
    var sc = makeScanner();
    var ch = d.chunks;
    var t0 = performance.now();
    for (var i = 0; i < ch.length; i++) {
      sc.append(ch[i]);
      /* 真实路径每帧读一次：这里模拟成每 readEvery 片读一次 */
      if (readEvery && (i % readEvery) === 0) {
        sc.text('file_path'); sc.length('content'); sc.lines('content');
      }
    }
    var tA = performance.now();
    var len = sc.length('content'), nl = sc.lines('content');
    var cmp = sc.complete('content'), path = sc.text('file_path');
    var t1 = performance.now();
    return { tAppend: tA - t0, tTotal: t1 - t0,
      len: len, nl: nl, cmp: cmp, path: path };
  }
  try {
    /* 冷启动会吃掉一切：V8 的基线编译 + IC 预热 + 堆扩容在前几遍里是
       20~30ms 量级（实测第 1 遍 25.4ms、第 3 遍 7.0ms）。不预热就量
       比值，量到的是「固定开销 ÷ 另一份固定开销」。所以：
         · 先各跑一遍丢掉（预热）；
         · 再**交替**跑 7 遍（同样的 GC 与缓存条件），取 min（最接近纯算法成本）
           与中位数（含噪声的典型值）两个口径，两个都要落在区间里。 */
    oneRun(smallD); oneRun(bigD);
    var bigRuns = [], smallRuns = [];
    for (var i = 0; i < 7; i++) { smallRuns.push(oneRun(smallD)); bigRuns.push(oneRun(bigD)); }
    function mins(a){ return Math.min.apply(null, a.map(function(r){ return r.tTotal; })); }
    function med(a){
      var b = a.map(function(r){ return r.tTotal; }).sort(function(x, y){ return x - y; });
      return b[Math.floor(b.length / 2)];
    }
    var big = mins(bigRuns), small = mins(smallRuns);
    var bigM = med(bigRuns), smallM = med(smallRuns);
    var bigA = Math.min.apply(null, bigRuns.map(function(r){ return r.tAppend; }));
    var smallA = Math.min.apply(null, smallRuns.map(function(r){ return r.tAppend; }));
    diag('F7 512KB / ' + bigD.chunks.length + ' 片：7 遍 total(ms)=' +
      j(bigRuns.map(function(r){ return +r.tTotal.toFixed(2); })) +
      ' → min=' + big.toFixed(2) + ' 中位=' + bigM.toFixed(2) +
      '；append min=' + bigA.toFixed(2) + 'ms');
    diag('F7 256KB / ' + smallD.chunks.length + ' 片：7 遍 total(ms)=' +
      j(smallRuns.map(function(r){ return +r.tTotal.toFixed(2); })) +
      ' → min=' + small.toFixed(2) + ' 中位=' + smallM.toFixed(2) +
      '；append min=' + smallA.toFixed(2) + 'ms');
    var b = bigRuns[0];
    ck('F7a 512KB / 30000+ 片：length/lines/complete/text 全部正确',
      bigD.chunks.length >= 30000 && b.len === bigD.expLen && b.nl === bigD.expNl &&
      b.cmp === true && b.path === 'perf/big.txt',
      '片数=' + bigD.chunks.length + ' len=' + b.len + '(期望' + bigD.expLen + ')' +
      ' nl=' + b.nl + '(期望' + bigD.expNl + ')' + ' complete=' + b.cmp + ' path=' + j(b.path));
    var ratio = small > 0 ? (big / small) : -1;
    ck('F7b 总耗时线性度（min 口径）：512KB 与 256KB 的耗时比值在 [1.4, 2.8]',
      ratio >= 1.4 && ratio <= 2.8,
      'big=' + big.toFixed(2) + 'ms small=' + small.toFixed(2) + 'ms 比值=' + ratio.toFixed(3));
    var ratioM = smallM > 0 ? (bigM / smallM) : -1;
    ck('F7b2 总耗时线性度（中位口径）：比值同样落在 [1.4, 2.8]（排除「只挑最快一遍」的偶然）',
      ratioM >= 1.4 && ratioM <= 2.8,
      'big=' + bigM.toFixed(2) + 'ms small=' + smallM.toFixed(2) + 'ms 比值=' + ratioM.toFixed(3));
    ck('F7c 计时可用（两组总耗时都 > 0.5ms，比值有物理意义）',
      big > 0.5 && small > 0.5,
      'big=' + big.toFixed(2) + 'ms small=' + small.toFixed(2) + 'ms');
    diag('F7 单字符成本：512KB=' + (big * 1e6 / bigD.expLen).toFixed(2) +
      'ns/char，256KB=' + (small * 1e6 / smallD.expLen).toFixed(2) + 'ns/char（线性则两者接近）');
    /* F7d：每 500 片读一次（模拟每帧读一次）—— 读不读不该改变量级。
       这是「惰性扫描 + 只前进游标」的核心承诺：读法本身不能把总成本推成 O(n²)。 */
    var wr = [];
    for (var w = 0; w < 3; w++) wr.push(oneRun(bigD, 500));
    var withReads = Math.min.apply(null, wr.map(function(r){ return r.tTotal; }));
    var ratio2 = big > 0 ? (withReads / big) : -1;
    diag('F7d 512KB 每 500 片读一次（60 次）：3 遍 total(ms)=' +
      j(wr.map(function(r){ return +r.tTotal.toFixed(2); })) +
      ' → min=' + withReads.toFixed(2) + 'ms，与只在末尾读一次(' + big.toFixed(2) +
      'ms)的比值=' + ratio2.toFixed(2) + '，len=' + wr[0].len + ' nl=' + wr[0].nl);
    ck('F7d 中途读 60 次不使总耗时超线性放大（与末尾一次读的比值 ≤ 2.5）',
      ratio2 >= 0 && ratio2 <= 2.5 && wr[0].len === bigD.expLen,
      'ratio=' + ratio2.toFixed(2) + ' len=' + wr[0].len + '(期望' + bigD.expLen + ')');
  } catch(e) {
    fails += 1;
    out.push('FAIL F7 抛错 :: ' + String(e && e.stack ? e.stack : e));
  }
  out.push('SUMMARY total=' + out.length + ' fail=' + fails);
  var pre = document.createElement('pre');
  pre.id = 'RESULT';
  pre.textContent = out.join('\n');
  document.body.appendChild(pre);
})();
`;


const HEAD = `<script>window.__sent=[];window.__ERR=[];
window.onerror=function(m,s,l,c,e){window.__ERR.push(String(m));return false;};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>`;
const SCANNER_BOOT = `<script>window.__SCANNER_SRC=${JSON.stringify(SCANNER_SRC)};
window.__SCANNER_LINE=${JSON.stringify(SCANNER_LINE)};</script>`;

const mainPage = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-write-preparing-suite</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
${HEAD}${SCANNER_BOOT}
<script>${JS}</script><script>${TEST}</script></body></html>`;

const scrollPage = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-write-preparing-suite-scroll</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
${HEAD}<script>
/* 垫片：让 requestAnimationFrame 真的跑起来（16ms 一帧，模拟真实 webview）。
   无头 + 虚拟时间下原生 rAF 不触发，滚动路径就永远不执行 → 只能这样量。 */
window.requestAnimationFrame = function(fn){
  return setTimeout(function(){ fn(Date.now()); }, 16);
};</script>
<script>${JS}</script><script>${TEST_SCROLL}</script></body></html>`;

const perfPage = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-write-preparing-suite-perf</title></head>
<body>${SCANNER_BOOT}
<script>var makeScanner = new Function('return (' + window.__SCANNER_SRC + ');')();</script>
<script>${PERF}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-write-preparing-suite');
fs.mkdirSync(dir, { recursive: true });
const mainFile = path.join(dir, 'main.html');
const scrollFile = path.join(dir, 'scroll.html');
const perfFile = path.join(dir, 'perf.html');
fs.writeFileSync(mainFile, mainPage, 'utf8');
fs.writeFileSync(scrollFile, scrollPage, 'utf8');
fs.writeFileSync(perfFile, perfPage, 'utf8');

const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
if (!browser) {
  console.error('找不到 Edge，无法跑无头验收');
  process.exit(3);
}

/* budget = 0 表示**不带** --virtual-time-budget：那种页面必须是纯同步的
   （同步写完 <pre> 再让 load 结束），--dump-dom 才能拿到结果。
   F7 必须这样跑：虚拟时间下 performance.now() 在同步任务内不前进。 */
function runPage(file, tag, budget, size) {
  const args = ['--headless=new', '--disable-gpu', '--no-first-run', '--dump-dom',
    '--user-data-dir=' + path.join(dir, 'p-' + tag), '--window-size=' + size];
  if (budget > 0) args.splice(4, 0, '--virtual-time-budget=' + budget);
  args.push('file:///' + file.replace(/\\/g, '/'));
  const dom = execFileSync(browser, args,
  { encoding: 'utf8', maxBuffer: 128 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
    timeout: 600000 });
  const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
  const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
  if (e) return { text: '页面抛错: ' + e[1] + '\nSUMMARY total=0 fail=1', err: true };
  const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
  return { text, err: false };
}

console.log('===== 主页面（A~F 除 F7） =====');
const r1 = runPage(mainFile, 'main', 240000, '760,900');
console.log(r1.text);
console.log('');
console.log('===== 性能页（F7：不带虚拟时间，同步跑完再 dump） =====');
const r3 = runPage(perfFile, 'perf', 0, '900,900');
console.log(r3.text);
console.log('');
console.log('===== 滚动页（E5：垫片让 rAF 真跑） =====');
const r2 = runPage(scrollFile, 'scroll', 120000, '760,300');
console.log(r2.text);
console.log('');
console.log('===== 汇总 =====');
const failRe = /\bfail=(\d+)/g;
let totalFail = 0, summary = [];
const names = ['main', 'perf', 'scroll'];
[r1, r3, r2].forEach(function (r, i) {
  let m, n = 0;
  while ((m = failRe.exec(r.text))) n += parseInt(m[1], 10);
  failRe.lastIndex = 0;
  totalFail += n;
  const s = /SUMMARY[^\n]*/.exec(r.text);
  summary.push(names[i] + ': ' +
    (s ? s[0] : '(无 SUMMARY)') + (r.err ? ' [页面抛错]' : ''));
});
summary.forEach(function (s) { console.log(s); });
console.log('ALL_FAIL_TOTAL=' + totalFail);
process.exit(totalFail > 0 || r1.err || r2.err || r3.err ? 1 : 0);
