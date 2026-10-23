/**
 * 验证「用户打断」的最终显示规则。
 *
 * 规则（两种出口，每种只说一遍）：
 *   · 这一轮有过程可折（有工具/思考）→ 建折叠组，**折叠行带「已停止 ·」前缀**，
 *     正文按原顺序进组（不挂内联标签、不留整行 ■ 提示）
 *   · 没有过程可折（只有一句话就停）→ 正文末尾挂 dsh 式内联小标签 `.msg-stopped`
 *   · 连正文都没有 → 退回整行「■ 已停止」
 *
 * 另有：样式对齐 dsh、重复 turn/end 幂等、正常完成不挂任何打断标记。
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
  function tags(){ return convo().querySelectorAll('.msg-stopped'); }
  function procs(){ return convo().querySelectorAll('.turn-proc'); }
  function sysLines(){
    return [].slice.call(convo().querySelectorAll('.sys'))
      .map(function(x){ return (x.textContent || '').replace(/\s+/g, ' ').trim(); });
  }
  function labelOf(p){
    return p ? ((p.querySelector('.tp-label') || {}).textContent || '') : '';
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);

    /* ===== A：有工具被打断 → 折叠行前缀，不挂内联标签 ===== */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'a1', name: 'Grep', label: '搜索代码',
      summary: 'foo', arguments: { pattern: 'foo' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'a1', ok: true, durationMs: 8, output: 'x' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我来实际跑几个工具。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(400);

    var p1 = procs();
    ck('A1 建了折叠组', p1.length === 1, 'got=' + p1.length);
    ck('A2 折叠行带「已停止 ·」前缀', labelOf(p1[0]).indexOf('已停止') === 0,
       JSON.stringify(labelOf(p1[0])));
    ck('A3 折叠行仍带计数', /\d+ 次工具调用/.test(labelOf(p1[0])),
       JSON.stringify(labelOf(p1[0])));
    ck('A4 不再挂内联标签（避免同一件事说两遍）', tags().length === 0,
       'got=' + tags().length);
    ck('A5 不再有整行「■ 已停止」',
       sysLines().filter(function(s){ return /■.*已停止/.test(s); }).length === 0,
       JSON.stringify(sysLines()));
    ck('A6 折叠行右侧不写「任务被打断」',
       ((p1[0].querySelector('.tp-meta') || {}).textContent || '').indexOf('任务被打断') < 0,
       JSON.stringify((p1[0].querySelector('.tp-meta') || {}).textContent || ''));

    /* ===== B：重复 turn/end 幂等 ===== */
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(200);
    ck('B1 重复 turn/end 不重复处理', procs().length === 1 && tags().length === 0,
       'procs=' + procs().length + ' tags=' + tags().length);

    /* ===== C：只有一句话就被打断 → 内联标签（dsh 形态） ===== */
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '我想说点什么' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(350);

    ck('C1 无过程节点时不建新组', procs().length === 1, 'got=' + procs().length);
    var t = tags();
    ck('C2 这时在正文末尾挂 1 个内联标签', t.length === 1, 'got=' + t.length);
    ck('C3 标签文本是「已停止」', t.length && t[0].textContent.trim() === '已停止',
       t.length ? JSON.stringify(t[0].textContent) : '(无)');
    var lastA = (function(){
      var l = convo().querySelectorAll('.msg.assistant');
      return l.length ? l[l.length - 1] : null;
    })();
    ck('C4 标签挂在正文里（不是别处）',
       !!(lastA && lastA.querySelector('.msg-stopped')), 'ok');

    if (t.length) {
      var cs = getComputedStyle(t[0]);
      ck('C5 样式：字号 11px（对齐 dsh）', cs.fontSize === '11px', cs.fontSize);
      ck('C6 样式：圆角 6px（对齐 dsh）', cs.borderRadius === '6px', cs.borderRadius);
      ck('C7 样式：有底色', cs.backgroundColor !== 'rgba(0, 0, 0, 0)'
         && cs.backgroundColor !== 'transparent', cs.backgroundColor);
      ck('C8 样式：无动画（dsh: no animation）',
         cs.animationName === 'none' || cs.animationName === '', cs.animationName);
      ck('C9 样式：不占满行', t[0].getBoundingClientRect().width < convo().clientWidth,
         Math.round(t[0].getBoundingClientRect().width) + '/' + convo().clientWidth);
    }

    /* ===== D：正常完成不挂任何打断标记 ===== */
    var tagBefore = tags().length;
    var sysBefore = sysLines().filter(function(s){ return /已停止|已打断/.test(s); }).length;
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '正常回答完成。' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(300);
    ck('D1 正常完成不挂打断标签', tags().length === tagBefore, 'got=' + tags().length);
    ck('D2 正常完成不新增打断提示',
       sysLines().filter(function(s){ return /已停止|已打断/.test(s); }).length === sysBefore,
       JSON.stringify(sysLines()));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-interrupt</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-intr2');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-interrupt.html');
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
