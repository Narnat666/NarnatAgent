/**
 * 验收「用户附图」的三条改动（用户报障）：
 *   1. `[用户附带了以下本地路径…]` 这块**不该显示在 UI 上**（它是给 AI 的协议块）
 *   2. 气泡里的图片太小（原来复用输入区 chip 的 26×18）→ 按内容尺寸展示
 *   3. 图片被删后要有可读的失败态（dsh 的 MessageImage 在 ATTACHMENT_NOT_FOUND
 *      时渲染成可点击重试的按钮）
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
const PY = fs.readFileSync(path.join(ROOT, '..', 'narnat_agent', 'core', 'input_history.py'), 'utf8');

const fails = [];
const ck = (n, ok, d) => {
  console.log((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  if (!ok) fails.push(n);
};

const MARK = '[用户附带了以下本地路径，请用相应工具自行读取处理]';

// 一张**真的有尺寸**的图（240×160 SVG），用来量容器高度。
// 用 1×1 的占位 PNG 测不出裁剪问题：那样容器本来就只有 1px 高，
// 「被裁」和「没裁」看起来一样（上一版就是这样误判成 PASS 之外的假失败）。
const BIG_IMG_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">'
  + '<rect width="240" height="160" fill="#335577"/></svg>';
const BIG_IMG_URL = 'data:image/svg+xml;base64,'
  + Buffer.from(BIG_IMG_SVG, 'utf8').toString('base64');

/* ---- 静态：标记串必须与后端完全一致（两处各写一份迟早分叉） ---- */
ck('S1 前端与后端的附件块标记一字不差',
  PY.indexOf('_ATTACH_MARK = "' + MARK + '"') >= 0 && html.indexOf(MARK) >= 0,
  '后端=' + (PY.indexOf(MARK) >= 0) + ' 前端=' + (html.indexOf(MARK) >= 0));
ck('S2 气泡里的图片不再被 26×18 限制（.msg 内有独立尺寸规则）',
  /\.msg \.atch img\{[\s\S]{0,200}max-width:min\(240px, 100%\)/.test(CSS));
ck('S3 输入区 chip 仍是小图标（两处互不影响）',
  /\.atch img\{flex:none;max-width:26px;max-height:18px/.test(CSS));
ck('S4 图片读不到时有可点重试的占位态',
  /\.msg \.atch\[data-missing="true"\]\{/.test(CSS) &&
  JS.indexOf("data-missing") >= 0 && JS.indexOf('图片已不在') >= 0);
ck('S5 有 lightbox（点图放大）', /\.imgbox\{/.test(CSS) && JS.indexOf('openImgBox') >= 0);
ck('S6 前端会把宿主回的库路径换成附件路径（图不再依赖原文件）',
  JS.indexOf('it.source') >= 0 && /a\.path = it\.path/.test(JS));
ck('S7 发给 AI 的附件块里说明「库内副本，读它」',
  html.indexOf('上面这条是库内副本，读它') >= 0);
ck('S8 图片入库逻辑抽到可单测的模块（extension.js 不再自己拼路径）',
  fs.existsSync(path.join(ROOT, 'src', 'attach-store.js')) &&
  /require\('\.\/attach-store'\)/.test(
    fs.readFileSync(path.join(ROOT, 'src', 'extension.js'), 'utf8')));
// 用户报障「只显示了五分之一」：消息里的图容器不能沿用输入区的
// `max-height:5.5em`（约 71px），否则整张图被裁成顶部一条。
ck('S9 消息里的图片容器不套用输入区的 5.5em 限高',
  /\.msg \.attach\{[\s\S]{0,160}max-height:none/.test(CSS),
  (() => {
    const m = /\.msg \.attach\{([\s\S]{0,200}?)\}/.exec(CSS);
    return m ? m[1].replace(/\s+/g, ' ').slice(0, 120) : '(规则缺失)';
  })());

/* ---- 运行时 ---- */
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
  var MARK = window.__MARK;
  var IMG = 'C:\\temp\\shot_a.png';
  var IMG2 = 'C:\\temp\\shot_b.png';

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(100);

    /* ---- 1. 历史重放：气泡里不该出现协议块，但要有图 ---- */
    var histText = '看看这个是什么\n\n' + MARK + '\n- 图片：' + IMG;
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h',
      messages: [
        { role: 'user', text: histText },
        { role: 'assistant', text: '这是 VS Code 截图。' }
      ] } });
    await sleep(900);

    var bubble = convo().querySelector('.msg[data-role="user"] .text');
    var bubbleText = bubble ? String(bubble.textContent) : '';
    out.push('DIAG 气泡正文=' + JSON.stringify(bubbleText));
    ck('H1 气泡里没有协议块标记', bubbleText.indexOf('用户附带了以下本地路径') < 0, JSON.stringify(bubbleText));
    ck('H2 气泡里没有本地路径明文', bubbleText.indexOf('C:\\temp') < 0, JSON.stringify(bubbleText));
    ck('H3 气泡里保留了用户自己打的字', bubbleText.indexOf('看看这个是什么') === 0, JSON.stringify(bubbleText));
    ck('H4 整条消息的可见文本里也没有协议块',
      String(convo().textContent).indexOf('用户附带了以下本地路径') < 0);
    var atch = convo().querySelector('.msg .attach .atch');
    ck('H5 仍然画出了图片位置（附件节点存在）', !!atch, atch ? 'yes' : 'no');
    if (atch) {
      // 宿主还没回执时必须是可读的「加载中」，不是一块空白
      var r0 = atch.getBoundingClientRect();
      ck('H6 回执未到时显示「加载中」而不是空白',
        String(atch.textContent).indexOf('图片加载中') >= 0 && r0.width > 40,
        JSON.stringify(String(atch.textContent)) + ' ' + Math.round(r0.width) + 'px');

      // 宿主回「读不到」→ 失败态 + 可点重试。
      // 回执按 id 匹配（见 atchOnReply），不是按 path。
      var reqId = 0;
      (window.__sent || []).forEach(function(m){
        if (m && m.type === 'attach/thumb' && m.params && m.params.path === IMG) {
          reqId = m.params.id;
        }
      });
      out.push('DIAG attach/thumb 请求 id=' + reqId);
      emit({ type: 'attach/thumbResult', params: { id: reqId, dataURL: null } });
      await sleep(400);
      var r = atch.getBoundingClientRect();
      out.push('DIAG 缺失态尺寸=' + Math.round(r.width) + 'x' + Math.round(r.height) +
        ' missing=' + atch.getAttribute('data-missing') +
        ' 文案=' + JSON.stringify(String(atch.textContent)));
      ck('H7 宿主读不到图时是「可读的失败态」而不是空白',
        atch.getAttribute('data-missing') === 'true' &&
        String(atch.textContent).indexOf('图片已不在') >= 0,
        JSON.stringify(String(atch.textContent)));
      ck('H8 失败态占位块有尺寸（不是 0×0）', r.width > 40 && r.height > 20,
        Math.round(r.width) + 'x' + Math.round(r.height));
      ck('H9 失败态可点击重试（有 cursor:pointer）',
        getComputedStyle(atch).cursor === 'pointer', getComputedStyle(atch).cursor);
    }

    /* ---- 2. 图片能读到时的显示尺寸（用另一条消息重新触发一次请求） ---- */
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h1b',
      messages: [
        { role: 'user', text: '这张图\n\n' + MARK + '\n- 图片：' + IMG2 },
        { role: 'assistant', text: '收到。' }
      ] } });
    await sleep(900);
    var reqId2 = 0;
    (window.__sent || []).forEach(function(m){
      if (m && m.type === 'attach/thumb' && m.params && m.params.path === IMG2) {
        reqId2 = m.params.id;
      }
    });
    out.push('DIAG IMG2 请求 id=' + reqId2);
    emit({ type: 'attach/thumbResult', params: { id: reqId2, dataURL: window.__BIG } });
    await sleep(500);
    var imgEl = convo().querySelector('.msg .attach .atch img');
    ck('R1 读到图后换成 img', !!imgEl);
    if (imgEl) {
      var cs = getComputedStyle(imgEl);
      out.push('DIAG 消息图 max-width=' + cs.maxWidth + ' max-height=' + cs.maxHeight +
        ' border-radius=' + cs.borderRadius);
      ck('R2 消息里的图片上限是 240px（不再是 26×18）',
        cs.maxWidth.indexOf('240px') >= 0 && cs.maxHeight === '240px',
        cs.maxWidth + ' / ' + cs.maxHeight);
      ck('R3 圆角与用户气泡一致（18px）', cs.borderRadius === '18px', cs.borderRadius);
      // 用户报障「只显示了五分之一」：容器不能把图裁掉，必须是完整可见高度
      var boxEl = convo().querySelector('.msg .attach');
      var boxCs = boxEl ? getComputedStyle(boxEl) : null;
      var fr = boxEl ? boxEl.getBoundingClientRect() : { height: 0 };
      out.push('DIAG 图片容器 max-height=' + (boxCs ? boxCs.maxHeight : '(无)') +
        ' 实际高=' + Math.round(fr.height) + 'px');
      ck('R6 图片容器没有把高度裁到 71px 左右（那条是输入区的规则）',
        !!boxCs && boxCs.maxHeight === 'none' && fr.height > 100,
        (boxCs ? 'max-height=' + boxCs.maxHeight : '') + ' 高=' + Math.round(fr.height) + 'px');
      // 点击开 lightbox
      imgEl.parentNode.click();
      await sleep(150);
      var lb = document.querySelector('.imgbox');
      ck('R4 点图打开放大层', !!lb);
      if (lb) {
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await sleep(150);
        ck('R5 Esc 关闭放大层', !document.querySelector('.imgbox'));
      }
    }

    /* ---- 3. 多张图铺成方块 ---- */
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h2',
      messages: [
        { role: 'user', text: '两张图\n\n' + MARK + '\n- 图片：' + IMG + '\n- 图片：' + IMG2 },
        { role: 'assistant', text: '收到。' }
      ] } });
    await sleep(900);
    var box = convo().querySelector('.msg .attach');
    ck('M1 多张图带 data-many 标记',
      !!box && box.getAttribute('data-many') === 'true',
      box ? box.getAttribute('data-many') : '(无)');
    ck('M2 气泡里仍无协议块明文',
      String(convo().textContent).indexOf('用户附带了以下本地路径') < 0);

    finish();
    function finish(){
      out.push('SUMMARY total=' + (out.length + 1) + ' fail=' + fails);
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-attach-ui</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__MARK=${JSON.stringify(MARK)};
window.__BIG=${JSON.stringify(BIG_IMG_URL)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-attachui');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'a.html');
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
if (m) console.log(m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
console.log('\nSTATIC fail=' + fails.length);
process.exit(fails.length ? 1 : 0);
