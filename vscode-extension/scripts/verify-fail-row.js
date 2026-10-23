/**
 * 验收两处显示口径变更（用户明确要求）：
 *
 *  1) 工具执行失败后，**不再**把错误内容顶到行头（`▶ 执行命令 [exit code: 1]`）。
 *     行头保留参数摘要，失败只靠名字变红 + OUT 段里的完整错误。
 *     实时路径（finishTool）与历史路径（addHistoryTool）必须同口径。
 *
 *  2) 折叠行**不再**标「未完成」。用户原话：「后续正确的完整的工具调度也会在
 *     头上显示未完成，属于画蛇添足」。用户主动打断仍标「已停止」。
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
  /** 取最后一个 .tools 行的行头四要素 */
  function lastRow(){
    var rows = convo().querySelectorAll('.tools .row');
    var row = rows[rows.length - 1];
    if (!row) return null;
    var arg = row.querySelector('.arg');
    var meta = row.querySelector('.meta');
    return {
      row: row,
      name: (row.querySelector('.name') || {}).textContent || '',
      arg: arg ? arg.textContent : '',
      argCls: arg ? arg.className : '',
      meta: meta ? meta.textContent : '',
      st: row.getAttribute('data-st')
    };
  }

  async function run(){
    boot();
    await sleep(80);

    /* ================= 实时路径 ================= */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '先跑一条会失败的命令。' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'x1', name: 'Shell', label: '执行命令',
      summary: 'echo hi && exit 1', arguments: { command: 'echo hi && exit 1' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'x1', ok: false, durationMs: 31,
      output: '[exit code: 1]\n第一行错误：命令返回了非零状态\n第二行细节' } });
    await sleep(60);

    var r = lastRow();
    out.push('DIAG 实时失败行 name=' + JSON.stringify(r.name) +
             ' arg=' + JSON.stringify(r.arg) + ' meta=' + JSON.stringify(r.meta) +
             ' data-st=' + r.st);
    ck('L1 实时失败：行头摘要仍是参数（不是错误首行）',
       r.arg.indexOf('echo hi && exit 1') >= 0, JSON.stringify(r.arg));
    ck('L2 实时失败：行头不含 [exit code',
       r.arg.indexOf('exit code') < 0, JSON.stringify(r.arg));
    ck('L3 实时失败：行头没有「失败」二字（只靠颜色）',
       r.meta.indexOf('失败') < 0, JSON.stringify(r.meta));
    ck('L4 实时失败：名字仍是工具标签', r.name === '执行命令', JSON.stringify(r.name));
    ck('L5 实时失败：data-st=err（名字变红靠它）', r.st === 'err', r.st);
    var nameColor = getComputedStyle(r.row.querySelector('.name')).color;
    ck('L6 实时失败：名字确实是红色（颜色不是默认前景色）',
       /rgb\(\s*2[0-9]{2}\s*,\s*[0-9]{1,2}\s*,\s*[0-9]{1,2}\s*\)/.test(nameColor) ||
       /#(e|f)/i.test(nameColor) || nameColor !== getComputedStyle(convo()).color,
       nameColor);
    // 错误内容必须还在 OUT 里
    var rowCard = r.row.parentNode.querySelector('.io');
    var cardText = rowCard ? rowCard.textContent : '';
    ck('L7 实时失败：错误内容完整留在 OUT 段',
       cardText.indexOf('第一行错误') >= 0 && cardText.indexOf('第二行细节') >= 0,
       cardText.replace(/\s+/g, ' ').slice(0, 90));

    /* ========= 实时：正常工具 + 正常结束（最后一节点是工具）不应标未完成 ========= */
    emit({ type: 'tool/call', params: { id: 'x2', name: 'Read', label: '读取文件',
      summary: 'a.md', arguments: { path: 'a.md' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'x2', ok: true, durationMs: 12, output: 'ok' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(500);
    var label = (convo().querySelector('.turn-proc .tp-label') || {}).textContent || '';
    out.push('DIAG 实时正常结束（以工具收尾）折叠行=' + JSON.stringify(label));
    ck('N1 实时：以工具收尾但正常结束，折叠行不标「未完成」',
       label.indexOf('未完成') < 0, JSON.stringify(label));
    ck('N2 实时：折叠行仍带计数', /\d+ 次工具调用/.test(label), JSON.stringify(label));

    /* ================= 实时：主动打断仍要标「已停止」 ================= */
    convo().innerHTML = '';
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我来跑一组探测。' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'y1', name: 'Shell', label: '执行命令',
      summary: 'sleep 9', arguments: { command: 'sleep 9' } } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(500);
    var l2 = (convo().querySelector('.turn-proc .tp-label') || {}).textContent || '';
    out.push('DIAG 实时打断折叠行=' + JSON.stringify(l2));
    ck('S1 实时：用户主动打断仍标「已停止」', l2.indexOf('已停止') === 0, JSON.stringify(l2));

    /* ================= 历史路径 ================= */
    convo().innerHTML = '';
    var HIST = [
      { role: 'user', content: '跑个命令' },
      { role: 'assistant', content: '好的，我来跑。' },
      { role: 'tool', name: 'Shell', label: '执行命令', tool_call_id: 'h1',
        arguments: { command: 'false' }, summary: 'false', failed: true,
        output: '[exit code: 1]\n历史里的错误首行\n更多细节' },
      { role: 'tool', name: 'Read', label: '读取文件', tool_call_id: 'h2',
        arguments: { path: 'b.md' }, summary: 'b.md', failed: false, output: '文件内容' }
    ];
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'h', messages: HIST } });
    await sleep(700);

    var procs = convo().querySelectorAll('.turn-proc');
    ck('H1 历史：这一轮建出了折叠组', procs.length >= 1, 'got=' + procs.length);
    var hl = procs.length ? (procs[0].querySelector('.tp-label') || {}).textContent : '';
    out.push('DIAG 历史折叠行=' + JSON.stringify(hl));
    ck('H2 历史：折叠行不标「未完成」', hl.indexOf('未完成') < 0, JSON.stringify(hl));
    ck('H3 历史：折叠行不标「已停止」', hl.indexOf('已停止') < 0, JSON.stringify(hl));

    // 展开组，逐行检查失败工具的行头
    var tprow = procs.length ? procs[0].querySelector('.tp-row') : null;
    if (tprow) { tprow.click(); await sleep(350); }
    var inner = procs.length ? procs[0].querySelector('.tp-inner') : null;
    var hrows = inner ? inner.querySelectorAll('.row') : [];
    var failRow = null;
    for (var i = 0; i < hrows.length; i++) {
      if (hrows[i].getAttribute('data-st') === 'err') { failRow = hrows[i]; break; }
    }
    ck('H4 历史：失败工具行存在且 data-st=err', !!failRow,
       failRow ? 'data-st=err' : '(没找到)');
    if (failRow) {
      var harg = failRow.querySelector('.arg');
      var hmeta = failRow.querySelector('.meta');
      var hargText = harg ? harg.textContent : '';
      var hmetaText = hmeta ? hmeta.textContent : '';
      out.push('DIAG 历史失败行 arg=' + JSON.stringify(hargText) +
               ' meta=' + JSON.stringify(hmetaText));
      ck('H5 历史失败：行头摘要仍是参数（不是错误首行）',
         hargText.indexOf('false') >= 0 && hargText.indexOf('历史里的错误首行') < 0,
         JSON.stringify(hargText));
      ck('H6 历史失败：行头没有「失败」二字', hmetaText.indexOf('失败') < 0,
         JSON.stringify(hmetaText));
      ck('H7 历史失败：arg 不带 bad 类（不再走错误色摘要）',
         harg && harg.className.indexOf('bad') < 0, harg ? harg.className : '');
      // 错误内容仍在 OUT 里
      var hcard = failRow.parentNode.querySelector('.io');
      var hcardText = hcard ? hcard.textContent : '';
      ck('H8 历史失败：错误内容完整留在 OUT 段',
         hcardText.indexOf('历史里的错误首行') >= 0 && hcardText.indexOf('更多细节') >= 0,
         hcardText.replace(/\s+/g, ' ').slice(0, 90));
    }

    // 成功行仍标「完成」
    var okRow = null;
    for (var j = 0; j < hrows.length; j++) {
      if (hrows[j].getAttribute('data-st') === 'ok') { okRow = hrows[j]; break; }
    }
    if (okRow) {
      var okMeta = (okRow.querySelector('.meta') || {}).textContent || '';
      ck('H9 历史成功行仍标「完成」', okMeta.indexOf('完成') === 0, JSON.stringify(okMeta));
    } else {
      ck('H9 历史成功行仍标「完成」', false, '没找到 data-st=ok 的行');
    }

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-fail-row</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-failrow');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-fail-row.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT');
