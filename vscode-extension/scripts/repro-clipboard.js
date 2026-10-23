/**
 * 测「提示词编辑区」的复制/粘贴/剪切快捷键链路。
 *
 * 已知嫌疑：ui-full.html:2399 的 document 级 `copy` 处理器会
 * preventDefault() 并改走宿主通道，而它**没有排除输入框**。
 * 若在 textarea 里选中文字按 Ctrl+C 被它接管，原生复制就被废掉了。
 *
 * 另外 `pushSelectionToHost`（mouseup/keyup 触发）会在用户「选中任何文字」
 * 时静默写系统剪贴板 —— 这可能冲掉用户刚复制好的内容。
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

const DESC = {
  restartPending: false,
  groups: [{ id: 'prompt', label: '提示词' }],
  items: [
    { id: 'narnatmd', group: 'prompt', label: 'narnat.md', kind: 'markdown',
      value: '', path: 'D:\\narnat.md', writable: true }
  ]
};
const ORIGINAL = '# 原内容\n第二行\n第三行\n';

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  :: ' + d : ''));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);
    document.getElementById('btnSettings').click();
    await sleep(40);
    emit({ type: 'config/describe', params: window.__DESC });
    await sleep(100);
    emit({ type: 'narnatmd/result', params: { path: 'x', text: window.__ORIG, exists: true } });
    await sleep(120);
    var tabs = document.querySelectorAll('#setTabs button');
    if (tabs.length) { tabs[0].click(); await sleep(150); }

    var ta = document.getElementById('setMd');
    ck('提示词 textarea 存在', !!ta, ta ? 'ok' : '无');
    if (!ta) { finish(); return; }

    /* ── 1) textarea 内选中文字后，copy 事件是否被拦 ── */
    ta.focus();
    ta.setSelectionRange(0, 5);
    var copiedText = ta.value.substring(ta.selectionStart, ta.selectionEnd);
    out.push('DIAG 选中了 ' + JSON.stringify(copiedText));

    window.__sent.length = 0;
    var copyEv = new ClipboardEvent('copy', { bubbles: true, cancelable: true });
    ta.dispatchEvent(copyEv);
    await sleep(80);
    out.push('DIAG copy 事件 defaultPrevented=' + copyEv.defaultPrevented);
    out.push('DIAG copy 期间发给宿主的消息数=' + window.__sent.length);
    ck('textarea 内 Ctrl+C 不应被全局 copy 处理器拦下（否则原生复制失效）',
       copyEv.defaultPrevented === false,
       'defaultPrevented=' + copyEv.defaultPrevented +
       ' 宿主消息=' + JSON.stringify(window.__sent.map(function(m){
         return m.type + ':' + String((m.params||{}).text || '').slice(0,12);
       })));

    /* ── 2) paste 事件是否被拦 ── */
    var pasteEv = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
    ta.dispatchEvent(pasteEv);
    await sleep(60);
    out.push('DIAG paste 事件 defaultPrevented=' + pasteEv.defaultPrevented);
    ck('textarea 内 Ctrl+V 不应被拦（否则粘贴不进去）',
       pasteEv.defaultPrevented === false,
       'defaultPrevented=' + pasteEv.defaultPrevented);

    /* ── 3) cut 事件是否被拦 ── */
    var cutEv = new ClipboardEvent('cut', { bubbles: true, cancelable: true });
    ta.dispatchEvent(cutEv);
    await sleep(60);
    ck('textarea 内 Ctrl+X 不应被拦',
       cutEv.defaultPrevented === false, 'defaultPrevented=' + cutEv.defaultPrevented);

    /* ── 4) 在提示词 textarea 里全选（Ctrl+A）→ 绝不能改写系统剪贴板。
       改写会怎样：用户刚在别处复制好的提示词被冲掉，Ctrl+V 出来的是
       textarea 里原有的旧内容，表现就是「粘贴的不是我要的」。 */
    ta.focus();
    ta.setSelectionRange(0, ta.value.length);
    window.__sent.length = 0;
    document.dispatchEvent(new KeyboardEvent('keyup', { key: 'a', ctrlKey: true, bubbles: true }));
    await sleep(100);
    var clipMsgs = (window.__sent || []).filter(function(m){ return m.type === 'clipboard/write'; });
    out.push('DIAG 提示词框内全选后写入剪贴板次数=' + clipMsgs.length);
    ck('提示词框内 Ctrl+A 全选不应改写系统剪贴板',
       clipMsgs.length === 0, clipMsgs.length + ' 次');

    /* ── 5) 模拟真实顺序：用户复制了提示词 → 点进 textarea → 检查剪贴板是否被改写 ── */
    var pastedWanted = '# 我想粘贴进来的提示词\n';
    window.__clip = pastedWanted;   // 模拟「系统剪贴板 = 用户刚复制的提示词」
    window.__sent.length = 0;
    // 点击 textarea 的真实顺序：mousedown → 焦点转移(选区被清) → mouseup
    ta.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    ta.focus();
    await sleep(30);
    ta.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    await sleep(120);
    var after = (window.__sent || []).filter(function(m){ return m.type === 'clipboard/write'; });
    out.push('DIAG 点进 textarea 后剪贴板写入次数=' + after.length);
    ck('点进 textarea 那一刻不应改写系统剪贴板（否则粘贴出来的不是用户要的内容）',
       after.length === 0, after.length + ' 次: ' +
       JSON.stringify(after.map(function(m){ return String((m.params||{}).text||'').slice(0,10); })));

    /* ── 6) 对话区选中文字仍要走宿主（右键菜单复制那条路不能退化）── */
    var convo = document.getElementById('convo');
    convo.innerHTML = '<div class="msg assistant"><div class="md"><p id="pickme">对话区的普通文字</p></div></div>';
    // 真实场景：鼠标按下对话区的文字 → 浏览器把焦点从输入框移走
    ta.blur();
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    await sleep(30);
    var r2 = document.createRange();
    r2.selectNodeContents(document.getElementById('pickme'));
    var s2 = window.getSelection();
    s2.removeAllRanges();
    s2.addRange(r2);
    out.push('DIAG 对话区选中时 activeElement=' +
             ((document.activeElement && document.activeElement.tagName) || 'null'));
    var pk = document.getElementById('pickme');
    out.push('DIAG pickme 存在=' + !!pk + ' 文本长度=' + (pk ? pk.textContent.length : -1));
    out.push('DIAG getSelection 长度=' + String(window.getSelection() || '').length +
             ' rangeCount=' + (window.getSelection() ? window.getSelection().rangeCount : -1) +
             ' 文本=' + JSON.stringify(String(window.getSelection() || '').slice(0, 12)));
    /* headless 下 window.getSelection() 的 toString 恒为空（rangeCount=1 但取不到文本），
       真实浏览器不会。为了仍能验证「activeElement 不是输入框时不被新判断挡住」
       这条分支，这里临时改掉 getSelection 的返回值。 */
    var realSel = window.getSelection;
    window.getSelection = function(){
      return { toString: function(){ return '假选区文字'; }, rangeCount: 0 };
    };
    window.__sent.length = 0;
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    await sleep(150);
    var c2 = (window.__sent || []).filter(function(m){ return m.type === 'clipboard/write'; });
    out.push('DIAG activeElement=BODY 时写入剪贴板=' + c2.length + ' 次 ' +
             JSON.stringify(c2.map(function(m){ return String((m.params||{}).text||'').slice(0,8); })));
    ck('焦点不在输入框时选中文字仍写入宿主剪贴板（右键复制不退化）',
       c2.length > 0, c2.length + ' 次');

    /* 反向：焦点在 textarea 且它有选区时，同一份「选区」必须被挡住 */
    window.__sent.length = 0;
    ta.focus();
    ta.setSelectionRange(0, 5);
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    await sleep(150);
    var c3 = (window.__sent || []).filter(function(m){ return m.type === 'clipboard/write'; });
    out.push('DIAG 焦点在 textarea 时写入剪贴板=' + c3.length + ' 次');
    ck('焦点在 textarea 且有选区时不得写剪贴板', c3.length === 0, c3.length + ' 次');
    window.getSelection = realSel;

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>repro-clip</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__clip='';window.__DESC=${JSON.stringify(DESC)};window.__ORIG=${JSON.stringify(ORIGINAL)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-clip');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'repro-clip.html');
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
