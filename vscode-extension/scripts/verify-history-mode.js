/**
 * 验证「历史重放」与实时显示一致（用户报障核心：
 * 「点击新建对话，或者进入旧的对话再回来，仍然还是老的显示模式」）。
 *
 * 三条要验：
 *  1) 历史里以工具收尾的轮次 → 全部进组 + 折叠行标「未完成」，正文不留组外末尾
 *  2) 历史里以正文收尾的轮次 → 正文摘出组外（正常完成，结论直接可见）
 *  3) 历史里纯工具轮 → 组照建，标「未完成」
 *  4) 顺序：组内成员顺序 == 消息原始顺序
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

// 构造一份真实形状的 _history_payload
const MSGS = [
  { role: 'user', text: '简单的试试你的工具好不好用' },
  // 轮 1：思考 → 正文（AI 第一句）→ 工具 → 工具（以工具收尾 = 没有结论）
  { role: 'assistant', text: '我来做个工具自检，并行跑几组探测。', thinking: '用户让我试试工具。' },
  { role: 'tool', name: 'Glob', label: '搜索文件', summary: '*.md',
    arguments: { pattern: '*.md' }, output: 'a.md', failed: false },
  { role: 'tool', name: 'Shell', label: '执行命令', summary: 'echo === CWD ===',
    arguments: { command: 'echo === CWD ===' }, output: '[exit code: 0]', failed: false },

  { role: 'user', text: '第二个问题' },
  // 轮 2：工具 → 正文（以正文收尾 = 有结论）
  { role: 'tool', name: 'Read', label: '读取', summary: 'b.md',
    arguments: { file_path: 'b.md' }, output: 'x', failed: false },
  { role: 'assistant', text: '读完了，这就是结论。' },

  { role: 'user', text: '第三个问题' },
  // 轮 3：纯工具，没有正文
  { role: 'tool', name: 'Grep', label: '搜索代码', summary: 'foo',
    arguments: { pattern: 'foo' }, output: 'hit', failed: false },
];

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
  function procs(){ return [].slice.call(convo().querySelectorAll('.turn-proc')); }
  function labelOf(p){ return ((p.querySelector('.tp-label') || {}).textContent || ''); }
  function innerSeq(p){
    var box = p.querySelector('.tp-inner');
    if (!box) return [];
    return [].slice.call(box.children).map(function(el){
      if (el.classList.contains('think')) return 'think';
      if (el.classList.contains('tools')) return 'tool:' + (el.getAttribute('data-tool') || '');
      if (el.classList.contains('msg')) return 'assistant';
      return el.className;
    });
  }
  // 组外的助手正文
  function outsideTexts(){
    return [].slice.call(convo().querySelectorAll('.msg.assistant'))
      .filter(function(el){ return !el.closest('.turn-proc'); })
      .map(function(el){ return (el.textContent || '').replace(/\s+/g, ' ').trim(); });
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: '测试会话', messages: window.__MSGS } });
    await sleep(500);

    var ps = procs();
    ck('H1 历史里画出了 3 个折叠组（一轮一个）', ps.length === 3, 'got=' + ps.length);
    if (ps.length !== 3) { finish(); return; }

    /* ── 轮 1：以工具收尾 → 没有结论正文，但**不标状态词** ──
       口径变更（用户要求）：「后续正确的完整的工具调度也会在头上显示未完成，
       属于画蛇添足」。历史重放分不清「用户点了停止」和「正常说完话又去调工具」，
       而后者在真实数据里占多数，所以一律不标。 */
    var p1 = ps[0];
    ck('H2 轮1（以工具收尾）折叠行不标「未完成」',
       labelOf(p1).indexOf('未完成') < 0 && labelOf(p1).indexOf('已停止') < 0,
       JSON.stringify(labelOf(p1)));
    ck('H2b 轮1 折叠行仍带计数', /\d+ 次工具调用/.test(labelOf(p1)), JSON.stringify(labelOf(p1)));
    var seq1 = innerSeq(p1);
    ck('H3 轮1 组内顺序 == 原始顺序（思考→正文→工具→工具）',
       JSON.stringify(seq1) === JSON.stringify(['think', 'assistant', 'tool:Glob', 'tool:Shell']),
       JSON.stringify(seq1));
    var p1text = (p1.textContent || '');
    ck('H4 轮1 的 AI 第一句话在组内（没被摘到组外）',
       p1text.indexOf('我来做个工具自检') >= 0, p1text.slice(0, 50).replace(/\s+/g,' '));

    /* ── 轮 2：以正文收尾 → 有结论，正文在组外 ── */
    var p2 = ps[1];
    ck('H5 轮2（以正文收尾）折叠行不带状态词',
       labelOf(p2).indexOf('未完成') < 0 && labelOf(p2).indexOf('已停止') < 0,
       JSON.stringify(labelOf(p2)));
    var seq2 = innerSeq(p2);
    ck('H6 轮2 组内只有工具（正文被摘到组外）',
       JSON.stringify(seq2) === JSON.stringify(['tool:Read']), JSON.stringify(seq2));

    /* ── 轮 3：纯工具 → 组照建，同样不标状态词 ── */
    var p3 = ps[2];
    ck('H7 轮3（纯工具）折叠行不标「未完成」',
       labelOf(p3).indexOf('未完成') < 0, JSON.stringify(labelOf(p3)));

    /* ── 组外正文只有轮 2 那条结论 ── */
    var outs = outsideTexts();
    ck('H8 组外只有 1 条正文（轮2 的结论）', outs.length === 1, 'got=' + outs.length);
    ck('H9 组外那条就是结论，不是 AI 第一句话',
       outs.length === 1 && outs[0].indexOf('读完了，这就是结论') >= 0,
       JSON.stringify(outs));

    /* ── 切走再回来（用户报障路径）：显示必须一样 ── */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: '别的会话', messages: [
        { role: 'user', text: '无关会话' },
        { role: 'assistant', text: '无关回答' }
      ] } });
    await sleep(300);
    var mid = procs().length;
    ck('H10 切到别的会话后组数变了（确实重画了）', mid === 0, 'got=' + mid);

    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: '测试会话', messages: window.__MSGS } });
    await sleep(400);
    var ps2 = procs();
    ck('H11 切回来仍是 3 个组', ps2.length === 3, 'got=' + ps2.length);
    if (ps2.length === 3) {
      ck('H12 切回来轮1 仍不标「未完成」',
         labelOf(ps2[0]).indexOf('未完成') < 0, JSON.stringify(labelOf(ps2[0])));
      ck('H13 切回来轮1 组内顺序不变',
         JSON.stringify(innerSeq(ps2[0])) ===
         JSON.stringify(['think', 'assistant', 'tool:Glob', 'tool:Shell']),
         JSON.stringify(innerSeq(ps2[0])));
      ck('H14 切回来组外仍只有结论那一条',
         outsideTexts().length === 1, JSON.stringify(outsideTexts()));
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-history-mode</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__MSGS=${JSON.stringify(MSGS)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-histmode');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-history-mode.html');
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
