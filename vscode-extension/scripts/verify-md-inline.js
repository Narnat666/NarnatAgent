/**
 * 验收第一轮回归修的两条 P1（R34 报的）：
 *
 *   M1 Markdown 斜体与删除线必须真的渲染
 *      `*x*` / `_x_` → <em>，`~~x~~` → <del>。
 *      修之前：CSS 里 `.msg em` / `.msg del` 与配置角色 markdown.italic /
 *      markdown.strikethrough 全都在，但渲染器从不产出这两个标签，
 *      原文符号原样显示（实测 del=0 em=0 strong=1）。
 *      反向锁：`snake_case_name`、`2*3*4`、`**粗**` 不能被误改成 <em>。
 *
 *   M2 `/skill` 首次敲空格，候选回执到了要自己把面板打开
 *      修之前：敲空格 → 缓存空 → 面板关；回执到 → 只判 paletteVisible()
 *      → 已关 → 什么都不做，用户以为「没有技能」。
 *
 * 判据全部可计算（标签计数、DOM 存在性、可见性）。
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
  function input(){ return document.getElementById('input'); }
  function pal(){ return document.getElementById('palette'); }
  function palOpen(){ return pal() && pal().getAttribute('data-open') === 'true'; }
  function items(){
    return Array.prototype.map.call(
      pal().querySelectorAll('.item, .pi, [data-v]'),
      function(el){ return el.getAttribute('data-v') || el.textContent.trim(); }
    ).filter(function(x){ return x; });
  }
  function setVal(v){
    var i = input();
    i.value = v;
    i.selectionStart = i.selectionEnd = v.length;
    i.dispatchEvent(new Event('input', { bubbles: true }));
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\x' } });
    await sleep(120);

    /* ── M1 斜体 / 删除线 ── */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    var md = '正文A *斜体星* 正文B _斜体下_ 正文C ~~删除线~~ 正文D **粗体** 正文E\n\n'
      + '标识符 snake_case_name 与 2*3*4 与 a_b_c 都不该变斜体';
    emit({ type: 'stream/delta', params: { text: md } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(500);

    var ems = document.querySelectorAll('.msg em');
    var dels = document.querySelectorAll('.msg del');
    var strongs = document.querySelectorAll('.msg strong');
    var emText = Array.prototype.map.call(ems, function(e){ return e.textContent; });
    var delText = Array.prototype.map.call(dels, function(e){ return e.textContent; });
    out.push('DIAG em=' + ems.length + ' del=' + dels.length + ' strong=' + strongs.length +
      ' emText=' + JSON.stringify(emText) + ' delText=' + JSON.stringify(delText));

    ck('M1.1 星号斜体渲染成 <em>', emText.indexOf('斜体星') >= 0, JSON.stringify(emText));
    ck('M1.2 下划线斜体渲染成 <em>', emText.indexOf('斜体下') >= 0, JSON.stringify(emText));
    ck('M1.3 删除线渲染成 <del>', delText.indexOf('删除线') >= 0, JSON.stringify(delText));
    ck('M1.4 粗体未被破坏', strongs.length >= 1 &&
      Array.prototype.some.call(strongs, function(s){ return s.textContent === '粗体'; }),
      strongs.length);
    ck('M1.5 原文不残留标记符号（正文里不该再出现 *斜体星*）',
      document.querySelector('.msg').textContent.indexOf('*斜体星*') < 0,
      '残留=' + (document.querySelector('.msg').textContent.indexOf('*斜体星*') >= 0));

    /* 反向锁：标识符/算式不能被吃掉 */
    var body = document.querySelector('.msg').textContent;
    ck('M1.6 snake_case_name 原样保留（不被当成斜体）',
      body.indexOf('snake_case_name') >= 0, '缺失');
    ck('M1.7 2*3*4 原样保留（不被当成斜体配对）',
      body.indexOf('2*3*4') >= 0, '缺失');
    ck('M1.8 a_b_c 原样保留', body.indexOf('a_b_c') >= 0, '缺失');

    /* em 的样式真的生效（不是有标签无效果） */
    var emEl = ems[0];
    if (emEl) {
      var st = getComputedStyle(emEl);
      ck('M1.9 <em> 的 font-style 真的是 italic', st.fontStyle === 'italic', st.fontStyle);
    } else {
      ck('M1.9 <em> 的 font-style 真的是 italic', false, '没有 em 元素');
    }
    var delEl = dels[0];
    if (delEl) {
      var dst = getComputedStyle(delEl);
      ck('M1.10 <del> 真的有删除线', /line-through/.test(dst.textDecorationLine || dst.textDecoration),
        dst.textDecorationLine || dst.textDecoration);
    } else {
      ck('M1.10 <del> 真的有删除线', false, '没有 del 元素');
    }

    /* ── M2 /skill 首次敲空格后，回执到了要自己重开面板 ── */
    setVal('/skill ');
    await sleep(300);
    var openBefore = palOpen();
    out.push('DIAG 首次敲 /skill 空格后 open=' + openBefore +
      ' items=' + JSON.stringify(items()));
    ck('M2.1 缓存还没到时不显示假候选', items().length === 0, JSON.stringify(items()));

    // 回执到达（模拟后端 skill/list）
    emit({ type: 'skill/result', params: { items: [
      { v: '代码审查', d: '系统技能' },
      { v: '部署', d: '项目技能' }
    ] } });
    await sleep(400);
    var openAfter = palOpen();
    var after = items();
    out.push('DIAG 回执到达后 open=' + openAfter + ' items=' + JSON.stringify(after));
    ck('M2.2 回执到达后候选面板自动打开（不必再敲一个字符）', openAfter === true,
      'open=' + openAfter);
    ck('M2.3 面板里出现技能候选', after.indexOf('代码审查') >= 0 && after.indexOf('部署') >= 0,
      JSON.stringify(after));

    /* M2 反向：用户已经改掉输入，回执到达不该再弹面板 */
    setVal('/compact ');
    await sleep(250);
    var openCompact = palOpen();
    emit({ type: 'skill/result', params: { items: [{ v: '另一个技能', d: '系统技能' }] } });
    await sleep(400);
    ck('M2.4 已离开 /skill 时，迟到的回执不该把面板重新弹出来',
      palOpen() === openCompact, 'before=' + openCompact + ' after=' + palOpen());

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-md-inline</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-mdinline');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
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
