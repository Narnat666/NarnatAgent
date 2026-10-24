/**
 * 验收 TeX 数学公式渲染（与 dsh 同一套：KaTeX + 四种定界符 + 仅定稿渲染）。
 *
 * dsh 的事实（D:\deepseek-harness\packages\client\ui-primitives\src\markdown）：
 *   · KaTeX 渲染，`throwOnError:true` 失败后降级 `strict:'ignore'`
 *   · 流式期间**完全不解析数学**（parseGfm 不带 math 扩展），定稿才带
 *   · 定界符 `$…$` `\(…\)` 行内，`$$…$$` `\[…\]` 块级
 *   · `.katex-display` max-width:100% + overflow-x:auto
 *
 * 判据：
 *   M1  $$…$$ 独占多行 → 块级公式（.katex-display 存在）
 *   M2  $$…$$ 同一行 → 块级
 *   M3  \[…\] → 块级
 *   M4  $…$ → 行内（.katex 存在且不在 .katex-display 里）
 *   M5  \(…\) → 行内
 *   M6  TeX 里的 * 和 _ 不被当成强调（不能出现 <em>）
 *   M7  TeX 里的 < 不被转义坏（x < y 要渲染成功，不能变成 &lt;）
 *   M8  反引号代码里的 $x$ 保持代码、不渲染公式
 *   M9  围栏代码块里的 $$ 不渲染
 *   M10 流式态（streaming）完全不渲染公式 —— 与 dsh 一致
 *   M11 `价格 $5 到 $10` 不被误判成公式（两端空白的守卫）
 *   M12 渲染失败时退回原文（不出现红框、不丢内容）
 *   M13 .katex-display 的 overflow-x 是 auto（长公式不撑破面板）
 *   M14 KaTeX 字体真的加载成功（不是豆腐块）—— 用 offsetWidth>0 与
 *       实际字形度量间接验证
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
const KCSS = fs.readFileSync(path.join(ROOT, 'media', 'katex', 'katex.min.css'), 'utf8');
const KJS = fs.readFileSync(path.join(ROOT, 'media', 'katex', 'katex.min.js'), 'utf8');

// 每项：[标签, 源文, 断言类型, 期望]
const CASES = [
  ['M1  $$ 独占多行 → 块级',
    '$$\n\\frac{d}{dx}\\sin x = \\cos x\n$$', 'display'],
  ['M2  $$ 同一行 → 块级',
    '$$\\lim_{x\\to 0}\\frac{\\sin x}{x} = 1$$', 'display'],
  ['M3  \\[…\\] → 块级',
    '\\[\\zeta(s) = \\sum_{n=1}^{\\infty}\\frac{1}{n^s}\\]', 'display'],
  ['M4  $…$ → 行内',
    '爱因斯坦写了 $E = mc^2$ 这个式子。', 'inline'],
  ['M5  \\(…\\) → 行内',
    '还有 \\(\\frac{1}{5}\\) 这种写法。', 'inline'],
  ['M6  TeX 里的 * 和 _ 不被当强调',
    '公式 $a*b*c$ 与 $x_1 + y_2$ 都应原样进公式', 'noEm'],
  ['M7  TeX 里的 < 不被转义坏',
    '$a < b$ 应当正常渲染', 'inlineRendered'],
  ['M8  反引号代码里的 $x$ 不渲染',
    '代码 `$x$` 保持原样', 'codeKept'],
  ['M9  围栏代码块里的 $$ 不渲染',
    '```\n$$\nx^2\n$$\n```', 'fenceKept'],
  ['M11 价格 $5 到 $10 不误判',
    '价格 $5 到 $10 元，不是公式。', 'noMath'],
  // ```math 围栏：dsh 的 renderCode 里 `lang === 'math'` 会转成块级公式。
  // 第一版漏了这条，摸底时发现（display=0 pre=1，只是个普通代码块）。
  ['M17 ```math 围栏 → 块级公式',
    '```math\n\\frac{1}{2}\n```', 'mathFence'],
  ['M18 ```mathjs 不是数学围栏（只认完全等于 math）',
    '```mathjs\nconst a = 1\n```', 'notMathFence']
];

const TEST = `
(function(){
  var CASES = ${JSON.stringify(CASES)};
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function lastMsg(){
    var all = document.querySelectorAll('#convo .msg.assistant');
    return all[all.length - 1] || null;
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\x' } });
    await sleep(150);

    for (var i = 0; i < CASES.length; i++) {
      var c = CASES[i];
      emit({ type: 'turn/begin', params: { round: i + 1 } });
      await sleep(10);
      emit({ type: 'stream/delta', params: { text: c[1] } });
      await sleep(10);
      emit({ type: 'turn/end', params: { reason: 'done' } });
      await sleep(320);

      var box = lastMsg();
      if (!box) { ck(c[0], false, '没有消息节点'); continue; }
      var disp = box.querySelectorAll('.katex-display').length;
      var inl = box.querySelectorAll('.katex').length;
      var em = box.querySelectorAll('em').length;
      var code = box.querySelectorAll('code').length;
      var txt = box.textContent || '';
      var kind = c[2];
      var ok = false, detail = 'display=' + disp + ' katex=' + inl + ' em=' + em +
        ' code=' + code + ' text=' + JSON.stringify(txt.slice(0, 40));

      if (kind === 'display') ok = disp >= 1;
      else if (kind === 'inline') ok = inl >= 1 && disp === 0;
      else if (kind === 'noEm') ok = em === 0 && inl >= 2;
      else if (kind === 'inlineRendered') ok = inl >= 1 && txt.indexOf('&lt;') < 0;
      else if (kind === 'codeKept') ok = code >= 1 && inl === 0 && txt.indexOf('$x$') >= 0;
      else if (kind === 'fenceKept') ok = box.querySelectorAll('pre').length >= 1 && inl === 0;
      else if (kind === 'noMath') ok = inl === 0 && txt.indexOf('$5') >= 0;
      else if (kind === 'mathFence') ok = disp >= 1 && box.querySelectorAll('pre').length === 0;
      else if (kind === 'notMathFence') ok = disp === 0 && box.querySelectorAll('pre').length >= 1;

      ck(c[0], ok, detail);
    }

    /* M10 流式态不渲染：只发 delta，不 turn/end。
       注意 TeX 的反斜杠必须写 \\\\：本段是 JS 模板字符串，\\\\frac 落到
       HTML 里是 \\frac，再落到浏览器 JS 单引号串里才是 \frac。
       写成 \\frac 会被浏览器当成 FormFeed 转义（\f），TeX 变成乱码 ——
       第一版就是这么假失败了一次。 */
    var liveText = '流式中 $E=mc^2$ 与 $$\\\\frac{1}{2}$$';
    emit({ type: 'turn/begin', params: { round: 99 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: liveText } });
    await sleep(300);
    var live = lastMsg();
    var liveK = live ? live.querySelectorAll('.katex').length : -1;
    ck('M10 流式态完全不渲染公式（与 dsh 一致）', liveK === 0,
      'katex=' + liveK + ' text=' + JSON.stringify(live ? (live.textContent || '').slice(0, 40) : ''));
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(320);
    var settled = lastMsg();
    var settledK = settled ? settled.querySelectorAll('.katex').length : -1;
    ck('M10b 定稿后同一段才渲染出公式', settledK >= 2, 'katex=' + settledK);

    /* M12 非法 TeX 退回原文（反斜杠同理要写四个） */
    emit({ type: 'turn/begin', params: { round: 100 } });
    await sleep(10);
    emit({ type: 'stream/delta', params: { text: '坏公式 $\\\\frac{$ 之后还有正常文字' } });
    await sleep(10);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(320);
    var bad = lastMsg();
    var badTxt = bad ? (bad.textContent || '') : '';
    ck('M12 非法 TeX 不崩、正文还在', badTxt.indexOf('之后还有正常文字') >= 0,
      JSON.stringify(badTxt.slice(0, 60)));

    /* M13 块级公式 overflow-x:auto */
    var dispEl = document.querySelector('#convo .katex-display');
    if (dispEl) {
      var st = getComputedStyle(dispEl);
      ck('M13 块级公式 overflow-x 是 auto（长公式不撑破面板）',
        st.overflowX === 'auto', st.overflowX);
      ck('M13b 块级公式 max-width 不超过 100%',
        st.maxWidth === '100%', st.maxWidth);
    } else {
      ck('M13 块级公式 overflow-x 是 auto（长公式不撑破面板）', false, '找不到 .katex-display');
      ck('M13b 块级公式 max-width 不超过 100%', false, '找不到 .katex-display');
    }

    /* M14 字体真的加载（用 .katex 的实际宽度判断：字体缺失会退化但仍有宽度，
       所以再查 docs.fonts 里的 FontFace 状态） */
    var fontsOk = false, fontsDetail = 'n/a';
    try {
      if (document.fonts && document.fonts.check) {
        fontsOk = document.fonts.check('12px KaTeX_Main') &&
          document.fonts.check('12px KaTeX_Math');
        fontsDetail = 'check(KaTeX_Main)=' + document.fonts.check('12px KaTeX_Main') +
          ' check(KaTeX_Math)=' + document.fonts.check('12px KaTeX_Math');
      }
    } catch (e) { fontsDetail = 'err ' + e; }
    ck('M14 KaTeX 数学字体可用（不是豆腐块）', fontsOk, fontsDetail);

    /* M15 块级公式不能被包进 <p>（dsh 的 fixture 里它是 markdown 根的直接子节点）。
       包进 <p> 会怎样：块级公式是 display 元素，浏览器会把 <p> 提前闭合，
       DOM 结构与预期不符，行内滚动那条规则也会误命中。 */
    var dispInP = document.querySelectorAll('#convo p > .katex-display').length;
    ck('M15 块级公式没有被包进 <p>（与 dsh 的 DOM 一致）', dispInP === 0,
      'p > .katex-display = ' + dispInP);

    /* M16 块级公式不撑破面板：对话区不出现横向滚动 */
    var convo = document.getElementById('convo');
    var noHScroll = convo ? convo.scrollWidth <= convo.clientWidth + 1 : false;
    ck('M16 公式没有把对话区撑出横向滚动', noHScroll,
      convo ? ('scrollWidth=' + convo.scrollWidth + ' clientWidth=' + convo.clientWidth) : '找不到 #convo');

    out.push('SUMMARY total=' + out.length + ' fail=' + fails);
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\\n');
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-math</title>
<style>${KCSS}</style>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${KJS}</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-math');
fs.mkdirSync(dir, { recursive: true });
// KaTeX 的 CSS 里字体是相对路径 `url(fonts/…)`。测试页写在临时目录，
// 必须把 fonts/ 一起放过去，否则 M14（字体可用）会假失败 ——
// 真实 webview 里 CSS 就在 media/katex/ 下，相对路径本来是对的。
const kFontsSrc = path.join(ROOT, 'media', 'katex', 'fonts');
const kFontsDst = path.join(dir, 'fonts');
if (fs.existsSync(kFontsSrc)) {
  fs.rmSync(kFontsDst, { recursive: true, force: true });
  fs.mkdirSync(kFontsDst, { recursive: true });
  for (const f of fs.readdirSync(kFontsSrc)) {
    if (f.endsWith('.woff2')) {
      fs.copyFileSync(path.join(kFontsSrc, f), path.join(kFontsDst, f));
    }
  }
}
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=25000',
  '--allow-file-access-from-files', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 96 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
