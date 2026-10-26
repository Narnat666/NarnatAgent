/**
 * 验收「压缩不阻塞、可中断、界面有动画、结论真实」。
 *
 * 用户报障（截图）：
 *   · /compact 后 30 秒报「请求超时（30000ms 无响应）」红叉，而压缩其实
 *     十几秒后成功了 —— 真实结论随过期请求丢弃；
 *   · 这期间点停止没反应、也不能再输入（后端读循环被同步压缩占死）；
 *   · 压缩行只有一个静态 ◌，46 秒里界面一动不动，分不清在跑还是卡住。
 *
 * 后端已改为异步（立即回执 + compress/result 下发真实结论）。这里验前端：
 *   C1  compress/begin 挂上动画类（.sys.busy），不是静态文字
 *   C2  压缩期间锁住输入框（data-compact + disabled + 占位符说明原因）
 *   C3  压缩期间发送按钮隐藏、停止按钮出现（可中断）
 *   C4  点停止会真的发 cancel 请求
 *   C5  compress/end 解锁并把行改成中性陈述（不承诺成功）
 *   C6  异步「已受理」回执（async:true）不画任何行
 *   C7  compress/result 把那一行改成真实结论，且**不新增行**
 *   C8  取值兜底：lines 是字符串也不能抛错
 *   C9  turn/end 兜底解锁（压缩期间后端没了也不会把输入框永久锁死）
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
  function inp(){ return document.getElementById('input'); }
  function stopBtn(){ return document.getElementById('stopBtn'); }
  function sendBtn(){ return document.getElementById('sendBtn'); }
  function sysLines(){ return [].slice.call(convo().querySelectorAll('.sys')); }
  function lastSys(){ var a = sysLines(); return a[a.length - 1]; }
  function vis(el){ return el ? getComputedStyle(el).display !== 'none' : false; }
  function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
  }

  async function run(){
    boot();
    await sleep(80);

    var ph0 = inp().getAttribute('placeholder');

    /* ── 一次完整的手动压缩 ── */
    emit({ type: 'compress/begin', params: {} });
    await sleep(30);
    var line = lastSys();
    ck('C1a 压缩行存在', !!line && line.textContent.indexOf('压缩') >= 0,
       line ? line.textContent : '(无)');
    ck('C1b 压缩行挂了动画类 .busy（不是静态文字）',
       !!line && line.classList.contains('busy'),
       line ? line.className : '(无)');
    ck('C1c .busy 的图标有 animation（CSS 规则真的命中）',
       !!line && getComputedStyle(line.querySelector('.ico')).animationName === 'syspulse',
       line ? getComputedStyle(line.querySelector('.ico')).animationName : '(无)');

    ck('C2a 压缩期间 body[data-compact=true]',
       document.body.getAttribute('data-compact') === 'true',
       document.body.getAttribute('data-compact'));
    ck('C2b 输入框被禁用', inp().disabled === true, inp().disabled);
    ck('C2c 占位符说明了原因',
       /压缩/.test(inp().getAttribute('placeholder') || ''),
       inp().getAttribute('placeholder'));

    ck('C3a 压缩期间发送按钮隐藏', !vis(sendBtn()), 'display=' + getComputedStyle(sendBtn()).display);
    ck('C3b 压缩期间停止按钮出现（可中断）', vis(stopBtn()),
       'display=' + getComputedStyle(stopBtn()).display);

    /* 点停止 → 必须真的发出 cancel */
    window.__sent.length = 0;
    stopBtn().click();
    await sleep(20);
    ck('C4 点停止发出 cancel 请求',
       window.__sent.some(function(m){ return m && m.type === 'cancel'; }),
       JSON.stringify(window.__sent.map(function(m){ return m.type; })));

    /* ── compress/end：解锁 + 中性陈述 ── */
    emit({ type: 'compress/end', params: { ok: null, before: null, after: null, durationMs: 46200 } });
    await sleep(30);
    ck('C5a 压缩结束后解锁输入框', inp().disabled === false, inp().disabled);
    ck('C5b 压缩结束后占位符逐字还原',
       inp().getAttribute('placeholder') === ph0,
       JSON.stringify([ph0, inp().getAttribute('placeholder')]));
    ck('C5c body[data-compact] 复位',
       document.body.getAttribute('data-compact') === 'false',
       document.body.getAttribute('data-compact'));
    ck('C5d 动画类被摘掉（不再是进行中）',
       !lastSys().classList.contains('busy'), lastSys().className);
    ck('C5e 中性陈述：只说结束+耗时，不承诺成功',
       /压缩结束/.test(lastSys().textContent) && /46\.2s/.test(lastSys().textContent),
       lastSys().textContent);

    /* ── 异步「已受理」回执不能画行 ── */
    var n0 = sysLines().length;
    emit({ type: 'command/result', params: {
      command: 'compact', args: '', text: '/compact',
      handled: true, ok: true, noop: false, async: true, lines: [] } });
    await sleep(30);
    ck('C6 异步已受理回执不画任何行（不提前报成功）',
       sysLines().length === n0,
       'before=' + n0 + ' after=' + sysLines().length);

    /* ── compress/result：改写同一行，不新增 ── */
    emit({ type: 'compress/result', params: {
      command: 'compact', handled: true, ok: true, noop: false,
      lines: ['已压缩 6 条历史消息（约 1234 tokens）'] } });
    await sleep(30);
    ck('C7a 真实结论到达后行数不变（不新增第二行）',
       sysLines().length === n0, 'before=' + n0 + ' after=' + sysLines().length);
    var t = lastSys().textContent;
    ck('C7b 行内容是后端给的真实条数', /已压缩 6 条/.test(t), t);
    ck('C7c 成功用勾+ok 配色', /✓/.test(t) && lastSys().classList.contains('ok'), lastSys().className);

    /* ── lines 给成字符串也不能抛 ── */
    emit({ type: 'compress/begin', params: {} });
    await sleep(20);
    emit({ type: 'compress/end', params: { durationMs: 100 } });
    await sleep(20);
    var threw = null;
    try {
      emit({ type: 'compress/result', params: { ok: true, lines: '已压缩 2 条' } });
    } catch (e) { threw = e; }
    await sleep(30);
    ck('C8 lines 为字符串不抛错', threw === null, threw ? String(threw) : '(无异常)');
    ck('C8b 字符串形态也能显示', /已压缩 2 条/.test(lastSys().textContent), lastSys().textContent);

    /* ── turn/end 兜底解锁 ── */
    emit({ type: 'compress/begin', params: {} });
    await sleep(20);
    ck('C9a 再次进入压缩锁', document.body.getAttribute('data-compact') === 'true',
       document.body.getAttribute('data-compact'));
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(30);
    ck('C9b turn/end 兜底解锁（后端没了也不会永久锁死）',
       document.body.getAttribute('data-compact') === 'false' && inp().disabled === false,
       'data-compact=' + document.body.getAttribute('data-compact') + ' disabled=' + inp().disabled);

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-compact</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-compact');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
if (!browser) { console.log('找不到 Edge，无法跑 DOM 验证'); process.exit(2); }
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
process.exit(0);
