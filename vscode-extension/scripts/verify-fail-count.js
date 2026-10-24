/**
 * 验收折叠行「失败」口径变更（用户报障）：
 *
 * 用户原话：「这里有个失败字样，后面是 950 次工具调度，用户很容易理解成
 * 这 950 次工具调度都失败了」——截图里是 `失败 · 950 次工具调用 · 403 条消息`。
 *
 * 新口径（对齐 dsh：状态词只由整轮决定，个别工具失败只是计数里的子集）：
 *   1) 组内个别工具失败 → 写成「N 次工具调用（其中 M 次失败）」，**不再**前置「失败 · 」
 *   2) 整轮 reason==='error' → 仍前置「失败 · 」（这确实是整轮失败）
 *   3) 用户主动打断 → 仍前置「已停止 · 」
 *   4) 副标题不重复说失败
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
  function label(){ return (convo().querySelector('.turn-proc .tp-label') || {}).textContent || ''; }
  function sub(){ return (convo().querySelector('.turn-proc .tp-sub') || {}).textContent || ''; }

  /** 跑一轮：tools = [{ok:bool}]，reason 为 turn/end 的 reason */
  async function round(round, tools, reason){
    convo().innerHTML = '';
    emit({ type: 'turn/begin', params: { round: round } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '开始一轮工具调度。' } });
    await sleep(20);
    for (var i = 0; i < tools.length; i++) {
      var id = 't' + round + '_' + i;
      emit({ type: 'tool/call', params: { id: id, name: 'Read', label: '读取文件',
        summary: 'f' + i + '.md', arguments: { path: 'f' + i + '.md' } } });
      await sleep(8);
      emit({ type: 'tool/result', params: { id: id, ok: tools[i].ok, durationMs: 5,
        output: tools[i].ok ? 'ok' : '[exit code: 1]\n命令返回非零' } });
      await sleep(8);
    }
    emit({ type: 'stream/delta', params: { text: '这一轮完成，结论如下。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: reason } });
    await sleep(600);
  }

  async function run(){
    boot();
    await sleep(80);

    /* ===== 1. 用户报障场景：多次调用 + 1 次失败，整轮正常完成 ===== */
    var ten = [];
    for (var i = 0; i < 10; i++) ten.push({ ok: i !== 4 });
    await round(1, ten, 'done');
    var L1 = label(), S1 = sub();
    out.push('DIAG 1 失败+9成功 正常结束 标题=' + JSON.stringify(L1) + ' 副标题=' + JSON.stringify(S1));
    ck('A1 个别失败不前置「失败 · 」', L1.indexOf('失败 · ') !== 0, JSON.stringify(L1));
    ck('A2 失败数是工具计数的子集说明',
      /10 次工具调用（其中 1 次失败）/.test(L1), JSON.stringify(L1));
    ck('A3 副标题不重复说失败', S1.indexOf('失败') < 0, JSON.stringify(S1));
    ck('A4 标题仍带总计数', /10 次工具调用/.test(L1), JSON.stringify(L1));
    ck('A5 「失败」前有数量与「其中」限定，读不成全部失败',
      !/失败 ·\s*\d+\s*次工具调用/.test(L1), JSON.stringify(L1));

    /* ===== 2. 全成功：标题里不出现「失败」二字 ===== */
    var ok10 = [];
    for (var j = 0; j < 10; j++) ok10.push({ ok: true });
    await round(2, ok10, 'done');
    var L2 = label();
    out.push('DIAG 2 全成功 标题=' + JSON.stringify(L2));
    ck('B1 全成功时标题无「失败」', L2.indexOf('失败') < 0, JSON.stringify(L2));

    /* ===== 3. 整轮失败（reason=error）仍要标「失败 · 」 ===== */
    await round(3, [{ ok: false }], 'error');
    var L3 = label();
    out.push('DIAG 3 整轮 error 标题=' + JSON.stringify(L3));
    ck('C1 整轮失败仍前置「失败 · 」', L3.indexOf('失败 · ') === 0, JSON.stringify(L3));
    ck('C2 整轮失败标题仍带计数', /1 次工具调用/.test(L3), JSON.stringify(L3));
    ck('C3 整轮失败不重复写「其中 N 次失败」', L3.indexOf('其中') < 0, JSON.stringify(L3));

    /* ===== 4. 用户主动打断仍标「已停止」，不被内部失败改写 ===== */
    await round(4, [{ ok: false }, { ok: true }], 'cancelled');
    var L4 = label();
    out.push('DIAG 4 打断 标题=' + JSON.stringify(L4));
    ck('D1 打断仍标「已停止」', L4.indexOf('已停止 · ') === 0, JSON.stringify(L4));
    ck('D2 打断时不出现「失败」状态前缀', L4.indexOf('失败 · ') !== 0, JSON.stringify(L4));

    /* ===== 5. 历史重放同口径 ===== */
    convo().innerHTML = '';
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h',
      messages: [
        { role: 'user', content: '跑一下' },
        { role: 'assistant', content: '好的。' },
        { role: 'tool', name: 'Shell', label: '执行命令', tool_call_id: 'h1',
          arguments: { command: 'false' }, summary: 'false', failed: true,
          output: '[exit code: 1]\n历史里的错误' },
        { role: 'tool', name: 'Read', label: '读取文件', tool_call_id: 'h2',
          arguments: { path: 'b.md' }, summary: 'b.md', failed: false, output: '内容' }
      ] } });
    await sleep(800);
    var procs = convo().querySelectorAll('.turn-proc');
    var L5 = procs.length ? (procs[0].querySelector('.tp-label') || {}).textContent : '';
    var S5 = procs.length ? (procs[0].querySelector('.tp-sub') || {}).textContent : '';
    out.push('DIAG 5 历史 标题=' + JSON.stringify(L5) + ' 副标题=' + JSON.stringify(S5));
    ck('E1 历史重放建出折叠组', procs.length >= 1, 'got=' + procs.length);
    ck('E2 历史：个别失败不前置「失败 · 」', L5.indexOf('失败 · ') !== 0, JSON.stringify(L5));
    ck('E3 历史：失败数是子集说明', /2 次工具调用（其中 1 次失败）/.test(L5), JSON.stringify(L5));
    ck('E4 历史：副标题不重复说失败', S5.indexOf('失败') < 0, JSON.stringify(S5));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-fail-count</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-failcount');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-fail-count.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT');
