/**
 * 验收 R44：用户消息气泡里的非图片附件（文件卡）。
 *
 * 用户报障原文：「用户ui选择添加附件发送，实际上发送的是路径是吧，但是发送后，
 * 在ui里面看不到自己发送过附件」—— 之前只有图片会画缩略图，`file` / `folder`
 * 两类附件在气泡里一个像素的痕迹都没有（发给 AI 的文本里有，渲染层被剥掉了）。
 *
 * 判据全部可计算（DOM 数量、textContent、getBoundingClientRect），
 * 断言编号与任务书 F1~F9 一一对应；F6b/F6c 是改动 4「历史回放异步补字节数」
 * 的附加验证（任务书的 F6 只要求文件卡被重建）。
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
const MARK = '[用户附带了以下本地路径，请用相应工具自行读取处理]';

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n +
      (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function diag(s){ out.push('DIAG ' + s); }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function sentAll(){ return window.__sent || []; }
  function sentSince(type, base){
    var list = sentAll();
    for (var i = list.length - 1; i >= base; i--) {
      if (list[i] && list[i].type === type) return list[i];
    }
    return null;
  }
  function setVal(v){
    var i = document.getElementById('input');
    i.value = v;
    i.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function userMsgs(){ return convo().querySelectorAll('.msg[data-role="user"]'); }
  function lastUser(){
    var a = userMsgs();
    return a.length ? a[a.length - 1] : null;
  }
  /* 走真实的附件链路：点「＋附件」→ 宿主回路径 → 页面发 attach/check →
     宿主回类型与字节数。不直接操作页面内部变量（它们在 IIFE 里，外部拿不到）。 */
  async function addAttach(paths, items){
    var base = sentAll().length;
    document.getElementById('attachBtn').click();
    await sleep(40);
    var pick = sentSince('attach/pick', base);
    if (!pick) { diag('attach/pick 没有发出'); return; }
    emit({ type: 'attach/picked', params: { id: pick.params.id, paths: paths } });
    await sleep(40);
    var chk = sentSince('attach/check', base);
    if (!chk) { diag('附件路径未触发 attach/check'); return; }
    emit({ type: 'attach/checked', params: { id: chk.params.id, items: items } });
    await sleep(60);
  }
  function sendNow(text){
    /* 上一轮还没结束（data-run=true）时 sendChat 会拦下普通消息。
       这里只测气泡渲染，手动复位运行态，不依赖 turn/end。 */
    document.body.setAttribute('data-run', 'false');
    setVal(text);
    document.getElementById('sendBtn').click();
  }
  function initParams(extra){
    var p = { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:/x' };
    if (extra) Object.keys(extra).forEach(function(k){ p[k] = extra[k]; });
    return p;
  }

  async function run(){
    emit({ type: 'initialize', params: initParams() });
    await sleep(150);

    /* ── 场景 1：带一个 .py 文件附件发一条消息（F1 / F2 / F3） ── */
    await addAttach(['E:/t/main.py'],
      [{ path: 'E:/t/main.py', exists: true, kind: 'file', bytes: 2048 }]);
    sendNow('看看这个脚本');
    await sleep(200);

    var cards = convo().querySelectorAll('.msg[data-role="user"] .filecard');
    ck('F1 发文件后气泡里出现文件卡', cards.length === 1, 'cards=' + cards.length);
    var card1 = cards[0] || null;
    var fn1 = card1 ? card1.querySelector('.fn') : null;
    var fm1 = card1 ? card1.querySelector('.fm') : null;
    diag('场景1 卡内 fn=' + (fn1 ? JSON.stringify(fn1.textContent) : '(无)') +
      ' fm=' + (fm1 ? JSON.stringify(fm1.textContent) : '(无)'));
    ck('F2 文件名正确', !!fn1 && fn1.textContent === 'main.py',
      fn1 ? fn1.textContent : '(无)');
    ck('F3 meta 含扩展名与大小（PY + 大小单位）',
      !!fm1 && /^PY\s/.test(fm1.textContent) && /(KB|B)/.test(fm1.textContent),
      fm1 ? fm1.textContent : '(无)');

    /* ── 场景 2：文件夹附件（F4） ── */
    await addAttach(['E:/t/src'],
      [{ path: 'E:/t/src', exists: true, kind: 'folder', bytes: -1 }]);
    sendNow('这个文件夹里的结构');
    await sleep(200);
    var lu2 = lastUser();
    var card2 = lu2 ? lu2.querySelector('.filecard') : null;
    var fm2 = card2 ? card2.querySelector('.fm') : null;
    diag('场景2 文件夹卡 fm=' + (fm2 ? JSON.stringify(fm2.textContent) : '(无)'));
    ck('F4 文件夹卡不显示字节数（只写「文件夹」）',
      !!fm2 && fm2.textContent === '文件夹', fm2 ? fm2.textContent : '(无)');

    /* ── 场景 3：图片 + 文件同一条消息（F5） ── */
    await addAttach(['E:/t/a.png', 'E:/t/b.py'],
      [{ path: 'E:/t/a.png', source: 'E:/t/a.png', exists: true, kind: 'image', bytes: 1234 },
       { path: 'E:/t/b.py', exists: true, kind: 'file', bytes: 300 }]);
    sendNow('图加文件');
    await sleep(200);
    var lu5 = lastUser();
    var thumbs = lu5 ? lu5.querySelectorAll('.attach .atch') : [];
    var fcards5 = lu5 ? lu5.querySelectorAll('.filecard') : [];
    diag('场景3 混排 atch=' + thumbs.length + ' filecard=' + fcards5.length);
    ck('F5 图片仍显示缩略图、没被文件卡顶掉',
      thumbs.length === 1 && fcards5.length === 1,
      'atch=' + thumbs.length + ' filecard=' + fcards5.length);

    /* ── 场景 3b：顺序 = 添加顺序（F5b） ── */
    await addAttach(['E:/t/first.py', 'E:/t/later.png'],
      [{ path: 'E:/t/first.py', exists: true, kind: 'file', bytes: 100 },
       { path: 'E:/t/later.png', source: 'E:/t/later.png', exists: true, kind: 'image', bytes: 900 }]);
    sendNow('先文件后图片');
    await sleep(200);
    var lu5b = lastUser();
    var box5b = lu5b ? lu5b.querySelector('.attach') : null;
    var firstChild = box5b && box5b.firstElementChild ? box5b.firstElementChild : null;
    diag('场景3b 容器首个子节点 class=' + (firstChild ? firstChild.className : '(无)'));
    ck('F5b 附件顺序 = 添加顺序（先文件后图片时文件卡在前）',
      !!firstChild && String(firstChild.className).indexOf('filecard') >= 0,
      firstChild ? firstChild.className : '(无)');

    /* ── 场景 4：文件名带尖括号（F8） ── */
    await addAttach(['E:/t/a<b>c.py'],
      [{ path: 'E:/t/a<b>c.py', exists: true, kind: 'file', bytes: 100 }]);
    sendNow('带尖括号的文件名');
    await sleep(200);
    var lu8 = lastUser();
    var fn8 = lu8 ? lu8.querySelector('.filecard .fn') : null;
    diag('场景4 fn=' + (fn8 ? JSON.stringify(fn8.textContent) : '(无)') +
      ' children=' + (fn8 ? fn8.children.length : '-'));
    ck('F8 文件名带尖括号不会被解析成 HTML',
      !!fn8 && fn8.textContent === 'a<b>c.py' && fn8.children.length === 0,
      fn8 ? fn8.textContent + ' children=' + fn8.children.length : '(无)');

    /* ── 场景 5：历史回放（F6 / F7 / F9 + 附加 F6b / F6c） ── */
    var histText = '看下这个脚本\n\n' + window.__MARK + '\n- 文件：E:/t/hist.py';
    emit({ type: 'initialize', params: initParams({
      session: { current: 'hist', messages: [{ role: 'user', text: histText }] }
    }) });
    await sleep(600);

    var histCards = convo().querySelectorAll('.msg[data-role="user"] .filecard');
    diag('历史回放 filecard=' + histCards.length +
      ' 气泡数=' + userMsgs().length);
    ck('F6 历史回放能重建文件卡', histCards.length === 1, 'cards=' + histCards.length);

    var hBubble = convo().querySelector('.msg[data-role="user"] .text');
    var hText = hBubble ? String(hBubble.textContent) : '';
    diag('F7 气泡正文=' + JSON.stringify(hText));
    ck('F7 协议块不出现在气泡文字里',
      hText.indexOf(window.__MARK) < 0 && hText.indexOf('- 文件：') < 0,
      JSON.stringify(hText));

    /* 改动 4 的异步回填：请求只能带非图片路径 */
    var fillReq = sentSince('attach/check', 0);
    var fillPaths = (fillReq && fillReq.params && fillReq.params.paths) || [];
    diag('历史补大小请求 paths=' + JSON.stringify(fillPaths));
    ck('F6b 历史回放只就非图片路径补大小',
      fillPaths.length === 1 && fillPaths[0] === 'E:/t/hist.py',
      JSON.stringify(fillPaths));
    if (fillReq) {
      emit({ type: 'attach/checked', params: { id: fillReq.params.id, items: [
        { path: 'E:/t/hist.py', exists: true, kind: 'file', bytes: 5120 }
      ] } });
      await sleep(250);
    }
    var hfm = histCards.length ? histCards[0].querySelector('.fm') : null;
    diag('历史卡 fm=' + (hfm ? JSON.stringify(hfm.textContent) : '(无)'));
    ck('F6c 历史卡回执后补出大小',
      !!hfm && /^PY\s/.test(hfm.textContent) && /(KB|B)/.test(hfm.textContent),
      hfm ? hfm.textContent : '(无)');

    /* F9：窄面板（右侧辅助侧边栏约 289px）里卡片不能溢出。
       .convo 是 .body（row flex）主轴上的 flex:1，只写 width 会被 flex-basis
       覆盖，宽度还是满面板 —— 必须同时把 flex 摘掉，宽度才真的是 289px。 */
    var c = convo();
    c.style.flex = 'none';
    c.style.width = '289px';
    await sleep(250);
    var narrowCard = c.querySelector('.msg[data-role="user"] .filecard');
    var nr = narrowCard ? narrowCard.getBoundingClientRect() : null;
    diag('窄面板 convo 宽=' + Math.round(c.getBoundingClientRect().width) +
      ' filecard 宽=' + (nr ? Math.round(nr.width) : '(无)') +
      ' scrollWidth=' + c.scrollWidth + ' clientWidth=' + c.clientWidth);
    ck('F9 窄面板不溢出（卡片宽 ≤ 289px）',
      !!nr && nr.width > 0 && nr.width <= 289,
      nr ? 'width=' + Math.round(nr.width) : '(无卡片)');

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-msg-files</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__MARK=${JSON.stringify(MARK)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-msg-files');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'f.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  '--window-size=760,900', 'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
