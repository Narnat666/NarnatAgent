/**
 * 验收视觉精修（用户两条报障）：
 *   「dsh 界面为什么这么简洁精致，我们的差点意思」
 *   「我们的图标都是 >，你看 dsh，没把鼠标放上去之前每个都是有自己的图标的，
 *    鼠标放上去后才有这个展开图标」
 *
 * 对齐 dsh 的可见事实（`D:\deepseek-harness\packages\client\ui-primitives\src\`）：
 *   1) 展开箭头是统一描边的 chevron 图标（`IconChevronRightOutline` /
 *      `IconChevronDownOutline`，`ICON_REGULAR_STROKE = 1`），不是 `▶` 字形。
 *   2) 行首静止态是**语义图标**（DisclosureRow 的 `icon` + `.iconIdle`），
 *      hover 时淡出、chevron 淡入（`.chevronHover`）。
 *   3) `名称 · 摘要` 之间是 2px 实体圆点（ReasoningRow/MessageItem 的 `.separator`）。
 *   4) 行 hover 只提亮文字，不铺整行通栏底色。
 *   5) 一行里只有一个「主」：折叠行主标题比副标题实一档。
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

const fails = [];
const ck = (n, ok, d) => {
  console.log((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  if (!ok) fails.push(n);
};

/* ---- 静态断言：源码层面（不依赖运行） ---- */
ck('S1 设计源里不再有 ▶ 字形充当箭头', html.indexOf('>▶<') < 0,
  '>▶< 出现 ' + (html.match(/>▶</g) || []).length + ' 次');
ck('S2 chevron 用 1px 描边（与 dsh 的 ICON_REGULAR_STROKE 一致）',
  /stroke-width="1"/.test(html) && html.indexOf('stroke-width="1.5"') < 0);
ck('S3 名称分隔不再是「·」字符',
  html.indexOf('content:"·"') < 0, String(html.indexOf('content:"·"')));
ck('S4 分隔点是 2px 圆点', /\.tools \.row \.name::after\{[\s\S]{0,200}width:2px;height:2px/.test(CSS));
ck('S5 工具行 hover 不再用整行 --bg-hover',
  !/\.row:hover\{background:var\(--bg-hover\)\}/.test(CSS));
ck('S6 折叠行主标题有独立的实一档颜色',
  /\.tp-label\{[\s\S]{0,220}color:var\(--fg-dim\)/.test(CSS));
ck('S7 hover 时语义图标淡出、chevron 淡入（dsh 的 iconIdle↔chevronHover）',
  /\.row:hover \.caret \.row-ic\{opacity:0\}/.test(CSS) &&
  /\.row:hover \.caret \.caret-ic\{opacity:1\}/.test(CSS));
ck('S8 静止态语义图标可见、chevron 不可见',
  /\.row \.caret \.row-ic\{opacity:1\}/.test(CSS) &&
  /\.row \.caret \.caret-ic\{opacity:0/.test(CSS));
ck('S9 七类语义图标齐备（search/read/run/edit/think/web/plan/other）',
  ['search', 'read', 'run', 'edit', 'think', 'web', 'plan', 'other']
    .every((k) => new RegExp('^\\s*' + k + ':', 'm').test(html)));
// 用户报障「那个 3 秒的位置跑到哪去了」：耗时与 chevron 不能同时用
// margin-left:auto —— flex 下两个 auto 会平分剩余空间，耗时被推到行中间。
// 只匹配**行首的规则块**（`\n.tp-caret{`）：`:has()` 兜底那条选择器是
// `.tp-row:not(...) .tp-caret{...}`，不算基础规则。
const caretAuto = /(?:^|\n)\.tp-caret\{[^}]*margin-left:auto/.test(CSS);
const metaAuto = /(?:^|\n)\.tp-meta\{[^}]*margin-left:auto/.test(CSS);
ck('S10 折叠行里只有一个元素吃 auto 外边距（耗时或 chevron，不能都吃）',
  !(caretAuto && metaAuto),
  'tp-caret 基础规则 auto=' + caretAuto + ' / tp-meta auto=' + metaAuto);

/* ---- 运行时断言：真跑一遍界面 ---- */
const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
  }

  async function run(){
    boot();
    await sleep(60);

    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(15);
    emit({ type: 'thinking/delta', params: { text: '先看一眼目录。' } });
    await sleep(15);
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'a1', name: 'Read', label: '读取',
      summary: 'a.md', arguments: { path: 'a.md' } } });
    await sleep(15);
    emit({ type: 'tool/result', params: { id: 'a1', ok: true, durationMs: 4, output: 'hi' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'a2', name: 'Shell', label: '执行命令',
      summary: 'dir', arguments: { command: 'dir' } } });
    await sleep(15);
    emit({ type: 'tool/result', params: { id: 'a2', ok: true, durationMs: 4, output: 'ok' } });
    await sleep(15);
    emit({ type: 'stream/delta', params: { text: '看完了。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(600);

    var carets = [].slice.call(convo().querySelectorAll('.row .caret'));
    var hasBoth = carets.length > 0 && carets.every(function(c){
      var ic = c.querySelector('svg.row-ic');
      var ch = c.querySelector('svg.caret-ic');
      return !!ic && !!ch;
    });
    out.push('DIAG 行首图标槽 ' + carets.length + ' 个，折叠组 ' +
      convo().querySelectorAll('.turn-proc').length + ' 个');
    ck('R1 每个工具/思考行都有「语义图标 + chevron」两层', hasBoth, String(carets.length));
    ck('R2 语义图标 14px、chevron 12px（槽位固定 16px，切换不跳）',
      carets.length > 0 && carets.every(function(c){
        var ic = c.querySelector('svg.row-ic');
        var ch = c.querySelector('svg.caret-ic');
        // 用计算样式而不是 getBoundingClientRect：折叠组收起后成员在
        // .tp-body[hidden] 里，量出来是 0px（实测），但那不代表槽位没宽度。
        return ic.getAttribute('width') === '14' && ch.getAttribute('width') === '12'
          && getComputedStyle(c).width === '16px';
      }), carets.length ? getComputedStyle(carets[0]).width : '(无)');

    // 静止态：语义图标可见、chevron 透明
    var ic0 = carets.length ? getComputedStyle(carets[0].querySelector('svg.row-ic')).opacity : '';
    var ch0 = carets.length ? getComputedStyle(carets[0].querySelector('svg.caret-ic')).opacity : '';
    out.push('DIAG 静止态 语义图标 opacity=' + ic0 + ' chevron opacity=' + ch0);
    ck('R3 静止态显示语义图标、不显示 chevron', ic0 === '1' && ch0 === '0', ic0 + '/' + ch0);

    // 语义图标按工具类型区分（不是所有行同一个）
    var iconsByTool = {};
    [].slice.call(convo().querySelectorAll('.tools')).forEach(function(w){
      var n = w.getAttribute('data-tool') || '';
      var ic = w.querySelector('.row svg.row-ic');
      if (ic) iconsByTool[n] = ic.innerHTML.slice(0, 40);
    });
    var uniq = Object.keys(iconsByTool).map(function(k){ return iconsByTool[k]; })
      .filter(function(v, i, a){ return a.indexOf(v) === i; });
    out.push('DIAG 工具→图标 ' + JSON.stringify(Object.keys(iconsByTool)) +
      ' 不同图标数=' + uniq.length);
    ck('R4 不同工具用不同语义图标（不是清一色同一个）',
      Object.keys(iconsByTool).length >= 2 && uniq.length >= 2,
      Object.keys(iconsByTool).length + ' 个工具 / ' + uniq.length + ' 种图标');
    ck('R5 DOM 里再没有 ▶ 字符', convo().textContent.indexOf('\u25b6') < 0);
    ck('R6 名称里不再有「·」字符（改用 2px 圆点）',
      [].slice.call(convo().querySelectorAll('.tools .row .name')).every(function(n){
        return String(n.textContent).indexOf('\u00b7') < 0;
      }));

    // 折叠组 chevron 在右侧，收起 -90°、展开 0°
    var tp = convo().querySelector('.turn-proc');
    var tpCaret = convo().querySelector('.tp-caret');
    var closedTf = tpCaret ? getComputedStyle(tpCaret).transform : 'none';
    var rowBox = tp ? tp.querySelector('.tp-row').getBoundingClientRect() : null;
    var carBox = tpCaret ? tpCaret.getBoundingClientRect() : null;
    var onRight = !!(rowBox && carBox && carBox.left > rowBox.left + rowBox.width / 2);
    out.push('DIAG 折叠组 chevron 位置 left=' + (carBox ? Math.round(carBox.left) : '-') +
      ' 行 right=' + (rowBox ? Math.round(rowBox.right) : '-') + ' transform=' + closedTf);
    ck('R7 折叠组 chevron 在行右侧（不是左侧挤占语义图标位）', onRight);
    if (tpCaret) { tpCaret.style.transition = 'none'; }
    await sleep(60);
    var closedTf2 = tpCaret ? getComputedStyle(tpCaret).transform : 'none';
    ck('R8 收起态 chevron 朝右（rotate(-90deg)）',
      closedTf2 === 'matrix(0, -1, 1, 0, 0, 0)', closedTf2);

    if (tp) { tp.querySelector('.tp-row').click(); }
    await sleep(120);
    var openTf = tpCaret ? getComputedStyle(tpCaret).transform : 'none';
    ck('R9 展开态 chevron 朝下（rotate(0)）',
      openTf === 'matrix(1, 0, 0, 1, 0, 0)', openTf);

    // 一行里只有一个「主」：主标题比副标题实
    var lab = convo().querySelector('.tp-label');
    var sub = convo().querySelector('.tp-sub');
    var cl = lab ? getComputedStyle(lab).color : '';
    var cs = sub ? getComputedStyle(sub).color : '';
    out.push('DIAG 主标题色=' + cl + ' 副标题色=' + cs);
    ck('R6 主标题与副标题颜色不同（有主次）', !!lab && !!sub && cl !== cs, cl + ' / ' + cs);

    // 折叠行不再比它下面的工具行高那么多（节奏对齐）
    var h = tp ? Math.round(tp.querySelector('.tp-row').getBoundingClientRect().height) : 0;
    out.push('DIAG 折叠行高=' + h + 'px');
    ck('R7 折叠行高 <= 34px（对齐 24px 行高节奏，不再是 36px 的笨重块）',
      h > 0 && h <= 34, h + 'px');

    // 耗时必须在行的右侧（紧跟 chevron 左边），不能停在行中间。
    // 用户报障原文：「那个 3 秒的位置跑到哪去了」——它当时夹在中间。
    var metaEl = tp ? tp.querySelector('.tp-meta') : null;
    var rowEl = tp ? tp.querySelector('.tp-row') : null;
    if (metaEl && rowEl) {
      var mb = metaEl.getBoundingClientRect();
      var rb = rowEl.getBoundingClientRect();
      var caretEl = tp.querySelector('.tp-caret');
      var cb = caretEl ? caretEl.getBoundingClientRect() : null;
      var centre = rb.left + rb.width / 2;
      out.push('DIAG 耗时 left=' + Math.round(mb.left) +
        ' 行中点=' + Math.round(centre) + ' chevron.left=' + (cb ? Math.round(cb.left) : '-'));
      ck('R8 耗时在行右半侧（不是在中间）', mb.left > centre,
        Math.round(mb.left) + ' > ' + Math.round(centre));
      ck('R9 耗时紧贴 chevron 左侧（两者之间不该有大段空白）',
        !!cb && (cb.left - mb.right) >= 0 && (cb.left - mb.right) < 20,
        cb ? Math.round(cb.left - mb.right) + 'px 间隙' : '(无 chevron)');
    } else {
      ck('R8 耗时在行右半侧（不是在中间）', false, '(这一轮没有耗时，无法判定)');
      ck('R9 耗时紧贴 chevron 左侧（两者之间不该有大段空白）', false, '(同上)');
    }

    finish();
    function finish(){
      var pre = document.createElement('pre');
      pre.id = 'RESULT';
      out.push('SUMMARY total=' + (out.length + fails * 0) + ' fail=' + fails);
      pre.textContent = out.join('\n');
      document.body.appendChild(pre);
    }
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-visual</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-visual');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-visual.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
if (m) console.log(m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
console.log('\nSTATIC fail=' + fails.length);
process.exit(fails.length ? 1 : 0);
