/**
 * 验证输入框 ↑↓ 与输入历史的关系：**光标先走，到头才翻历史**。
 *
 * 用户口径（报障原话大意）：编辑输入框里的多行文字时按 ↑↓ 只是想挪光标，
 * 不能一按就去翻历史；要「光标到头后，再按一次」才跳。
 *
 * 用例：
 *  E1 多行草稿、光标在文字中间按 ↑：不翻历史（值不变、不被 preventDefault）
 *  E2 单行草稿、光标在末尾按 ↑：第一次只把光标送到行首，不翻历史
 *  E3 光标已在行首再按 ↑：翻出最新一条历史
 *  E4 翻出来的历史本身是多行：再按 ↑ 仍能继续往更旧翻（连翻不被多行卡住）
 *  E5 多行历史里按 ↓：先挪光标，光标到末尾后再按 ↓ 才回草稿
 *  E6 有选区时按 ↑：不翻历史（交给浏览器取消选择）
 *  E7 ↓ 到底回草稿后，草稿内容原样回来且光标在末尾
 *
 * 说明：headless 里 dispatchEvent 不会触发 textarea 的默认编辑行为，
 * 所以辅助函数在「事件没有被 preventDefault」时**按浏览器语义模拟光标移动**，
 * 让守卫的判据（光标位置）走到与真实按键一致的路径。
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

// 磁盘全局历史（最旧在前）：最新一条是用户实际发过的两行消息
const HIST = [
  '第一问',
  '第二问',
  '同意，按你的做法来，做完后，再派三轮子代理复验，三轮测试\n像这种小问题由你来决定，设计到功能改正的再汇报给我'
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
  function input(){ return document.getElementById('input'); }
  function short(s){
    s = String(s).replace(/\n/g, '\\n');
    return JSON.stringify(s.length > 30 ? s.slice(0, 30) + '…' : s);
  }
  function caret(){ return input().selectionStart + '/' + input().selectionEnd; }

  /* 模拟浏览器在 keydown 未被 preventDefault 时的光标移动 */
  function moveUp(pos, v){
    var lineStart = v.lastIndexOf('\n', pos - 1) + 1;
    if (pos > lineStart) return lineStart;     // 行内 → 本行行首
    if (lineStart === 0) return 0;             // 已在首行行首
    return lineStart - 1;                      // 换到上一行末尾
  }
  function moveDown(pos, v){
    var lineEnd = v.indexOf('\n', pos);
    if (lineEnd < 0) return v.length;          // 已末行 → 文本末尾
    return lineEnd + 1;                        // 换到下一行行首
  }

  function key(k){
    var ty = input();
    var ev = new KeyboardEvent('keydown', { key: k, code: k,
      keyCode: k === 'ArrowUp' ? 38 : 40, bubbles: true, cancelable: true });
    ty.dispatchEvent(ev);
    if (!ev.defaultPrevented) {
      var pos = ty.selectionStart;
      var next = k === 'ArrowUp' ? moveUp(pos, ty.value) : moveDown(pos, ty.value);
      ty.setSelectionRange(next, next);
    }
    return ev.defaultPrevented;
  }

  function setValue(v, start, end){
    var ty = input();
    ty.value = v;
    ty.dispatchEvent(new Event('input', { bubbles: true }));
    ty.setSelectionRange(start == null ? v.length : start,
                         end == null ? (start == null ? v.length : start) : end);
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null, messages: [] }, workdir: '<t>',
      inputHistory: window.__HIST } });
    await sleep(120);

    var DRAFT = '第一行草稿\n第二行草稿\n第三行草稿';
    var H2 = window.__HIST[2];
    var H1 = window.__HIST[1];
    var H0 = window.__HIST[0];

    /* E1 光标在中间：只挪光标，不翻历史 */
    setValue(DRAFT, 4, 4);
    key('ArrowUp');
    ck('E1 多行草稿中间按 ↑ 不翻历史', input().value === DRAFT, short(input().value));
    ck('E1b 光标被浏览器挪到行首', caret() === '0/0', caret());

    /* E2 单行草稿、光标在末尾：第一下只到行首 */
    setValue('单行草稿', null);
    key('ArrowUp');
    ck('E2 单行草稿末尾按 ↑ 不翻历史', input().value === '单行草稿', short(input().value));
    ck('E2b 光标移到行首', caret() === '0/0', caret());

    /* E3 光标已在行首：这一下才翻历史 */
    key('ArrowUp');
    ck('E3 光标到行首后再按 ↑ 才翻历史', input().value === H2, short(input().value));
    ck('E3b 翻出来的历史光标在开头（能继续往前翻）', caret() === '0/0', caret());

    /* E4 多行历史不挡连翻 */
    key('ArrowUp');
    ck('E4 多行历史再按 ↑ 继续往旧翻', input().value === H1, short(input().value));
    key('ArrowUp');
    ck('E4b 再按一次翻到最旧一条', input().value === H0, short(input().value));

    /* E5 ↓ 方向：先挪光标，到头后再回草稿 */
    setValue('', 0, 0);
    key('ArrowUp');                 // 光标 0 → 翻到最新一条（多行，光标在开头）
    ck('E5a 空输入框按 ↑ 直接翻到最新一条', input().value === H2, short(input().value));
    key('ArrowDown');               // 光标在开头 → 先挪光标
    ck('E5b 多行历史里按 ↓ 先挪光标、不跳条', input().value === H2, short(input().value));
    var midCaret = caret();
    ck('E5c 光标确实动了', midCaret !== '0/0', midCaret);
    while (input().selectionStart !== input().value.length) key('ArrowDown');
    key('ArrowDown');               // 光标到末尾后再按一次 → 回草稿
    ck('E5d 光标到末尾后按 ↓ 回到草稿', input().value === '', short(input().value));

    /* E6 有选区时不翻历史 */
    setValue(DRAFT, 0, 6);
    var prevented = key('ArrowUp');
    ck('E6 有选区按 ↑ 不翻历史', input().value === DRAFT && !prevented,
       short(input().value) + ' prevented=' + prevented);

    /* E7 草稿内容与光标都要还原 */
    var draft = '写了一半的内容\n还没发';
    setValue(draft, 0, 0);
    key('ArrowUp');                 // → 翻出最新一条
    ck('E7a 翻出历史', input().value === H2, short(input().value));
    while (input().selectionStart !== input().value.length) key('ArrowDown');
    key('ArrowDown');               // 回草稿
    ck('E7b ↓ 到底后草稿原样回来', input().value === draft, short(input().value));
    ck('E7c 草稿光标落在末尾', caret() === draft.length + '/' + draft.length, caret());

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-hist-caret</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__HIST=${JSON.stringify(HIST)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-histcaret');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-hist-caret.html');
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
