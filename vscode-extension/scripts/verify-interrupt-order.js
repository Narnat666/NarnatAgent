/**
 * 验证「被打断后保持原顺序」（用户报障：AI 第一句话跑到总结位置）。
 *
 * 真实轮次结构：思考 → 正文「我来做个工具自检」→ 工具1 → 工具2
 * 期望：全部按原顺序进组，组内顺序 == 发生顺序，正文不在组外末尾。
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
  function kids(){ return [].slice.call(convo().children); }
  function kind(el){
    if (!el) return '?';
    var c = el.classList;
    if (c.contains('turn-proc')) return 'turn-proc';
    if (c.contains('think')) return 'think';
    if (c.contains('tools')) return 'tools';
    if (c.contains('msg') && c.contains('assistant')) return 'assistant';
    if (c.contains('sys')) return 'sys';
    return el.className || el.tagName;
  }
  // 组内成员的种类序列
  function innerSeq(){
    var box = convo().querySelector('.turn-proc .tp-inner');
    if (!box) return [];
    return [].slice.call(box.children).map(function(el){
      if (el.classList.contains('think')) return 'think';
      if (el.classList.contains('tools')) return 'tools:' + (el.getAttribute('data-tool') || '');
      if (el.classList.contains('msg')) return 'assistant';
      return el.className;
    });
  }

  async function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);
  }

  async function run(){
    await boot();

    /* ===== 场景 A：真实结构 思考 → 正文 → 工具1 → 工具2，被打断 ===== */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/begin', params: {} });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '用户让我自检工具，我并行跑几个探测。' } });
    await sleep(20);
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);
    // AI 开口说的第一句话
    emit({ type: 'stream/delta', params: { text: '我来做个工具自检，并行跑几组探测。' } });
    await sleep(30);
    emit({ type: 'tool/call', params: { id: 'p1', name: 'Glob', label: '搜索文件',
      summary: '*.md', arguments: { pattern: '*.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'p1', ok: true, durationMs: 68, output: 'a.md' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'p2', name: 'Shell', label: '执行命令',
      summary: 'echo === CWD ===', arguments: { command: 'echo === CWD ===' } } });
    await sleep(20);
    // 第二个工具没回结果就被打断
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(500);

    var seq = innerSeq();
    ck('A1 组内成员顺序 == 发生顺序（思考→正文→工具1→工具2）',
       JSON.stringify(seq) === JSON.stringify(['think', 'assistant', 'tools:Glob', 'tools:Shell']),
       JSON.stringify(seq));

    // 关键：正文不能在组外
    var outsideAssistant = kids().filter(function(el){
      return el.classList.contains('msg') && el.classList.contains('assistant');
    });
    ck('A2 被打断时正文不留在组外（否则会被读成总结）',
       outsideAssistant.length === 0, 'got=' + outsideAssistant.length);

    // 正文确实在组内且在第一句话的位置
    var innerText = (convo().querySelector('.turn-proc .tp-inner') || {}).textContent || '';
    ck('A3 组内包含 AI 说的第一句话',
       innerText.indexOf('我来做个工具自检') >= 0,
       innerText.slice(0, 60));

    var label = (convo().querySelector('.turn-proc .tp-label') || {}).textContent || '';
    ck('A4 折叠行带「已停止 ·」前缀', label.indexOf('已停止') === 0, JSON.stringify(label));
    ck('A5 折叠行仍带计数', /\d+ 次工具调用/.test(label), JSON.stringify(label));

    var sys = [].slice.call(convo().querySelectorAll('.sys'))
      .map(function(x){ return (x.textContent || '').replace(/\s+/g,' ').trim(); });
    ck('A6 没有额外整行「■ 已停止」', sys.filter(function(s){ return /■.*已停止/.test(s); }).length === 0,
       JSON.stringify(sys));

    // 展开组，确认正文在正确位置（在工具之前）
    convo().querySelector('.turn-proc .tp-row').click();
    await sleep(300);
    var seq2 = innerSeq();
    ck('A7 展开后顺序不变', JSON.stringify(seq2) === JSON.stringify(seq), JSON.stringify(seq2));
    var order = (function(){
      var inner = convo().querySelector('.turn-proc .tp-inner');
      var nodes = [].slice.call(inner.children);
      var ti = -1, ai = -1, gi = -1;
      nodes.forEach(function(el, i){
        if (el.classList.contains('think')) ti = i;
        if (el.classList.contains('msg')) ai = i;
        if (el.getAttribute('data-tool') === 'Glob') gi = i;
      });
      return { think: ti, assistant: ai, glob: gi };
    })();
    ck('A8 组内正文在思考之后、工具之前（时间顺序）',
       order.think < order.assistant && order.assistant < order.glob,
       'think=' + order.think + ' assistant=' + order.assistant + ' glob=' + order.glob);

    /* ===== 场景 B：正常完成，正文是结论 → 留在组外 ===== */
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'q1', name: 'Read', label: '读取',
      summary: 'b.md', arguments: { file_path: 'b.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'q1', ok: true, durationMs: 5, output: 'x' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '读完了，这就是结论。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(400);

    var procs = convo().querySelectorAll('.turn-proc');
    ck('B1 正常完成仍建组', procs.length === 2, 'got=' + procs.length);
    var lastProc = procs[procs.length - 1];
    var after = [];
    var seen = false;
    kids().forEach(function(el){
      if (el === lastProc) { seen = true; return; }
      if (seen && el.classList.contains('msg') && el.classList.contains('assistant')) after.push(el);
    });
    ck('B2 正常完成时正文留在组外（用户直接看到结论）', after.length === 1,
       'got=' + after.length);
    ck('B3 结论文本正确',
       after.length && (after[0].textContent || '').indexOf('读完了，这就是结论') >= 0,
       after.length ? (after[0].textContent || '').slice(0, 40) : '(无)');
    var labelB = (lastProc.querySelector('.tp-label') || {}).textContent || '';
    ck('B4 正常完成的折叠行不带「已停止」', labelB.indexOf('已停止') < 0, JSON.stringify(labelB));

    /* ===== 场景 C：只有一句话就被打断（无过程节点，不建组）→ 挂内联标签 ===== */
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我想说点什么' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(300);
    ck('C1 无过程节点时不建组', convo().querySelectorAll('.turn-proc').length === 2,
       'got=' + convo().querySelectorAll('.turn-proc').length);
    var tags = convo().querySelectorAll('.msg-stopped');
    ck('C2 这时在正文末尾挂「已停止」内联标签', tags.length === 1, 'got=' + tags.length);

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-order</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-order');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-order.html');
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
