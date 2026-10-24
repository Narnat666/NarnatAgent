/**
 * 验收「后台任务提交行要跟工具行一起折进折叠组」。
 *
 * 用户报障截图：一轮 12 次工具调用已经收成折叠组（收起态），但组下面还裸着
 * 一行 `⚙ 后台任务 bg1 已提交 C:\Users\…\bg1.log`，夹在折叠行与最终回复之间。
 *
 * 根因：`foldTurnProcess` 只收带 `data-turn-tool` / `data-turn-think` /
 * `data-turn-text` 的节点；`bgSubmit` 用 `addSysLine` 建的是普通 `.sys`，
 * 三个标记一个都没有 → 永远留在组外。
 *
 * 判据（都可计算）：
 *   B1 轮末该行在折叠组**内部**（`box.contains(el)` 为真）
 *   B2 组外不再有 `.sys` 节点残留
 *   B3 组内成员数包含它（DOM 顺序仍按原始位置：工具 → bg 行）
 *   B4 不重复计数：折叠行的工具数仍等于真实工具行数（bg 行不计入）
 *   B5 最终回复仍在组外（没被一起折进去）
 *
 * 另外反向锁一条：**压缩/重试这类状态行不折**（它们是状态，折起来等于藏了）。
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

    /* 复刻截图那一轮：思考 → 2 次工具 → bg 提交 → 最终回复 */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(15);
    emit({ type: 'thinking/delta', params: { text: '跑一下工具自检。' } });
    await sleep(15);
    emit({ type: 'thinking/end', params: {} });
    await sleep(15);
    emit({ type: 'tool/call', params: { id: 't1', name: 'Shell', label: '执行命令',
      summary: 'python main.py --rpc', arguments: { command: 'x' } } });
    await sleep(15);
    emit({ type: 'tool/result', params: { id: 't1', ok: true, durationMs: 900, output: 'bg1' } });
    await sleep(15);
    emit({ type: 'tool/call', params: { id: 't2', name: 'Read', label: '读取',
      summary: 'a.md', arguments: { path: 'a.md' } } });
    await sleep(15);
    emit({ type: 'tool/result', params: { id: 't2', ok: true, durationMs: 3, output: 'ok' } });
    await sleep(15);
    emit({ type: 'bg/submit', params: { slot: 'bg1',
      logPath: 'C:\\Users\\86153\\AppData\\Local\\Temp\\narnat_bg_16508_hbo6mfq1\\bg1.log' } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: '工具都正常。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(900);

    var tp = convo().querySelector('.turn-proc');
    var bgLine = null;
    [].slice.call(convo().querySelectorAll('.sys')).forEach(function(s){
      if (s.textContent.indexOf('后台任务') >= 0) bgLine = s;
    });

    ck('B0 折叠组存在', !!tp);
    ck('B1 后台任务行存在', !!bgLine,
       bgLine ? bgLine.textContent.slice(0, 40) : '(找不到)');
    if (!tp || !bgLine) { return finish(); }

    ck('B1b 后台任务行在折叠组**内部**（用户报障：它裸在组外）',
       tp.contains(bgLine), tp.contains(bgLine) ? '(在组内)' : '(在组外)');
    ck('B2 组外没有残留的 .sys 行',
       convo().querySelectorAll(':scope > .sys').length === 0,
       '组外 .sys = ' + convo().querySelectorAll(':scope > .sys').length);

    // 顺序：工具行 → bg 行（组内 DOM 顺序必须与当时出现的顺序一致）
    var inner = tp.querySelector('.tp-inner');
    var kids = [].slice.call(inner.children);
    var iBg = kids.indexOf(bgLine);
    var iTool = kids.findIndex(function(k){ return k.classList.contains('tools'); });
    ck('B3 组内顺序保持「工具在前、bg 行在后」',
       iBg > iTool && iTool >= 0, 'tool@' + iTool + ' bg@' + iBg);
    ck('B3b 组内成员数 = 工具块 + bg 行等（≥2 个节点）', kids.length >= 2,
       'members=' + kids.length);

    // 不重复计数：Shell 那次调用已经在「2 次工具调用」里了
    var tc = Number(tp.getAttribute('data-tool-count') || '-1');
    ck('B4 工具数 = 2（bg 行不重复计入）', tc === 2, 'data-tool-count=' + tc);
    var label = (tp.querySelector('.tp-label') || {}).textContent || '';
    ck('B4b 折叠行标题读作「2 次工具调用…」', /2 次工具调用/.test(label), label);

    // 最终回复仍在组外
    var asst = [].slice.call(convo().querySelectorAll('.msg.assistant'));
    ck('B5 最终回复仍在组外（没被一起折进去）',
       asst.length === 1 && !tp.contains(asst[0]),
       '正文气泡=' + asst.length + ' 在组内=' + (asst[0] ? tp.contains(asst[0]) : '?'));

    // 反向：状态行（压缩中/重试）不能被折进去 —— 折了等于把状态藏了
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(15);
    emit({ type: 'tool/call', params: { id: 'u1', name: 'Grep', label: '搜索内容',
      summary: 'x', arguments: { pattern: 'x' } } });
    await sleep(15);
    emit({ type: 'tool/result', params: { id: 'u1', ok: true, durationMs: 2, output: 'y' } });
    await sleep(15);
    emit({ type: 'compress/begin', params: { before: 1000 } });
    await sleep(20);
    emit({ type: 'compress/end', params: { ok: true, before: 1000, after: 600 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '压缩完了。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(600);
    var compressOutside = [].slice.call(convo().querySelectorAll(':scope > .sys'))
      .some(function(s){ return s.textContent.indexOf('压缩') >= 0; });
    ck('B6 压缩状态行不折（状态不能被藏进组里）', compressOutside,
       '组外 .sys=' + convo().querySelectorAll(':scope > .sys').length);

    finish();
    function finish(){
      out.push('SUMMARY total=' + out.length + ' fail=' + fails);
      var pre = document.createElement('pre');
      pre.id = 'RESULT';
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-bg-fold</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-bgfold');
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
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
process.exit(0);
