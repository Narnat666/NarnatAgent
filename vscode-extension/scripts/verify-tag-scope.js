/**
 * 专项：打断标记不能挂到「上一轮」的正文上。
 *
 * 隐患场景（代码时序导致）：
 *   第一轮正常完成，留下一条正文「上一轮的回答」
 *   第二轮 turn/begin 后立刻 turn/end cancelled，本轮没有任何内容
 *   → 本轮不建组（box=null）→ 调 markLastAssistantStopped()
 *   → 但 foldTurnProcess 结尾已把 turnStartNode 置 null
 *   → 扫描范围退化成「整个对话」→ 取到上一轮那条正文 → 挂错地方
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
  function assistants(){
    return [].slice.call(convo().querySelectorAll('.msg.assistant'));
  }
  function txt(el){ return (el && el.textContent || '').replace(/\s+/g, ' ').trim(); }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);

    /* ── 轮 1：正常完成，留一条正文 ── */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '上一轮的回答，已经答完了。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(300);

    var a1 = assistants();
    ck('前置：轮 1 有一条正文', a1.length === 1, 'got=' + a1.length);
    ck('前置：轮 1 正文没有停止标记',
       a1.length && !a1[0].querySelector('.msg-stopped'), 'ok');

    /* ── 轮 2：begin 后立刻取消，本轮没有任何内容 ── */
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(400);

    var a2 = assistants();
    var tagged = a2.filter(function(el){ return !!el.querySelector('.msg-stopped'); });
    ck('核心：轮 1 的正文不能被挂上「已停止」', tagged.length === 0,
       '被挂的条数=' + tagged.length +
       (tagged.length ? ' 文本=' + JSON.stringify(txt(tagged[0]).slice(0, 40)) : ''));
    ck('本轮没有新增正文', a2.length === a1.length, 'before=' + a1.length + ' after=' + a2.length);

    /* ── 轮 3：与轮 2 相同但这次本轮有正文（正常挂到本轮） ── */
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '这轮说到一半被打断。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(400);

    var a3 = assistants();
    var tags = convo().querySelectorAll('.msg-stopped');
    ck('有正文被打断时挂 1 个标签', tags.length === 1, 'got=' + tags.length);
    var last = a3[a3.length - 1];
    ck('标签挂在**本轮**（最后一条）正文上', !!(last && last.querySelector('.msg-stopped')),
       txt(last).slice(0, 30));
    ck('本轮正文文本正确', /这轮说到一半被打断/.test(txt(last)), txt(last).slice(0, 30));
    ck('轮 1 的正文依然干净',
       !a3[0].querySelector('.msg-stopped'), txt(a3[0]).slice(0, 30));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-tag-scope</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-tagscope');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-tag-scope.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const t = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT';
console.log(t);
process.exit(/fail=0/.test(t) ? 0 : 2);
