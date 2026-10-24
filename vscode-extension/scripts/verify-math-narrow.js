/**
 * 窄面板 + 折叠组里的公式：真实使用形态下的边界。
 *
 * 为什么单独测：verify-math 用的是 760px 宽，而插件默认停在**辅助侧边栏**
 * （约 249px 宽）。长公式在 249px 里会不会撑破对话区、把整个面板顶出横向滚动条，
 * 这是用户天天会遇到的形态，宽面板测不出来。
 *
 * 判据：
 *   N1 249px 宽时，块级公式不撑破对话区（scrollWidth <= clientWidth + 1）
 *   N2 249px 宽时，块级公式自身可横向滚动（overflow-x:auto 且 scrollWidth > clientWidth）
 *   N3 公式在折叠组（.turn-proc）里，展开后仍正常显示
 *   N4 折叠状态下不渲染失败（成员在 hidden 里，getComputedStyle 仍应有值）
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
const KCSS = fs.readFileSync(path.join(ROOT, 'media', 'katex', 'katex.min.css'), 'utf8');
const KJS = fs.readFileSync(path.join(ROOT, 'media', 'katex', 'katex.min.js'), 'utf8');

const LONG_TEX = '\\frac{\\partial \\mathbf{u}}{\\partial t} + (\\mathbf{u} \\cdot \\nabla)\\mathbf{u} = -\\frac{1}{\\rho}\\nabla p + \\nu \\nabla^2 \\mathbf{u} + \\mathbf{f} + \\alpha \\beta \\gamma \\delta \\epsilon \\zeta \\eta \\theta';

const SCENE = `
(function(){
  var LONG = ${JSON.stringify(LONG_TEX)};
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\x' } });
    await sleep(200);

    /* 一段相对长的公式，必然超出 249px */
    var doc = '## 公式在窄面板\\n\\n$$\\n' + LONG + '\\n$$\\n\\n后面还有正文。';
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '窄面板',
      isChild: false, messages: [
        { role: 'user', text: '给个长公式' },
        { role: 'assistant', text: doc }
      ] } });
    await sleep(900);

    var convo = document.getElementById('convo');
    var disp = convo.querySelector('.katex-display');

    /* N1 对话区不出现横向滚动 */
    ck('N1 长公式没把对话区撑出横向滚动',
      convo.scrollWidth <= convo.clientWidth + 1,
      'scrollWidth=' + convo.scrollWidth + ' clientWidth=' + convo.clientWidth);

    /* N2 公式自己可滚 */
    if (disp) {
      var st = getComputedStyle(disp);
      var canScroll = disp.scrollWidth > disp.clientWidth;
      ck('N2 公式自身可横向滚动（长内容不丢失）',
        st.overflowX === 'auto' && canScroll,
        'overflowX=' + st.overflowX + ' scrollW=' + disp.scrollWidth +
        ' clientW=' + disp.clientWidth);
    } else {
      ck('N2 公式自身可横向滚动（长内容不丢失）', false, '找不到 .katex-display');
    }

    /* N3 公式不超出视口右边界 */
    if (disp) {
      var r = disp.getBoundingClientRect();
      ck('N3 公式块的右边界不超出对话区',
        r.right <= convo.getBoundingClientRect().right + 1,
        'disp.right=' + Math.round(r.right) + ' convo.right=' + Math.round(convo.getBoundingClientRect().right));
    } else {
      ck('N3 公式块的右边界不超出对话区', false, '找不到 .katex-display');
    }

    /* N4 折叠组里的公式：再造一轮「工具 + 公式」，检查收起/展开两态 */
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 'c1', name: 'Read', label: '读取',
      summary: 'x.txt', arguments: { file_path: 'x.txt' } } });
    await sleep(20);
    emit({ type: 'tool/result', params: { id: 'c1', ok: true, durationMs: 3, output: 'ok' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '中间正文 $a^2$ 有行内公式。' } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '\\n\\n$$\\nb^2 + c^2\\n$$' } });
    await sleep(20);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(700);

    var lastMsg = (function(){
      var all = document.querySelectorAll('#convo .msg.assistant');
      return all[all.length - 1] || null;
    })();
    var finalK = lastMsg ? lastMsg.querySelectorAll('.katex').length : -1;
    ck('N4 轮末最终回复里的公式已渲染（不受折叠影响）', finalK >= 1, 'katex=' + finalK);

    var tp = convo.querySelector('.turn-proc');
    ck('N4b 有折叠组（一轮里含工具时应收成组）', !!tp, tp ? 'ok' : '找不到 .turn-proc');
    if (tp) {
      var row = tp.querySelector('.tp-row');
      var body = tp.querySelector('.tp-body');
      var openBefore = body ? body.hasAttribute('hidden') : null;
      if (row) row.click();
      await sleep(400);
      var openAfter = body ? body.hasAttribute('hidden') : null;
      ck('N4c 点标题能展开折叠组', openBefore === true && openAfter === false,
        'hidden: ' + openBefore + ' -> ' + openAfter);
      // 展开后量一下有没有撑破
      ck('N4d 展开后对话区仍无横向滚动',
        convo.scrollWidth <= convo.clientWidth + 1,
        'scrollWidth=' + convo.scrollWidth + ' clientWidth=' + convo.clientWidth);
    }

    out.push('SUMMARY total=' + out.length + ' fail=' + fails);
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\\n');
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-math-narrow</title>
<style>${KCSS}</style><style>${CSS}</style>
<!-- 浏览器窗口有最小宽度（实测 --window-size=249 实际拿到 436px），
     所以不能靠窗口尺寸模拟窄面板。这里直接把面板容器钉成 249px
     —— 也就是插件默认停在辅助侧边栏时的真实宽度。 -->
<style>
html,body{width:249px !important;min-width:0 !important;max-width:249px !important;overflow-x:hidden}
.shell{width:249px !important;max-width:249px !important}
</style></head><body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${KJS}</script><script>${JS}</script><script>${SCENE}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-math-narrow');
fs.mkdirSync(dir, { recursive: true });
const fsrc = path.join(ROOT, 'media', 'katex', 'fonts');
const fdst = path.join(dir, 'fonts');
if (fs.existsSync(fsrc)) {
  fs.mkdirSync(fdst, { recursive: true });
  for (const f of fs.readdirSync(fsrc)) {
    if (f.endsWith('.woff2')) fs.copyFileSync(path.join(fsrc, f), path.join(fdst, f));
  }
}
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
// 249px = 插件默认停在辅助侧边栏时的真实宽度
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=22000',
  '--window-size=249,900', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 96 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
