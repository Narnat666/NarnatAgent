/**
 * 任务 C 的验收脚本：在**真实 DOM**里跑 design/ui-full.html 的产物。
 *
 * 为什么不用 node+jsdom：本机没有 jsdom，也不允许为此装依赖。这里改成
 * 「拼一个和 webview 等价的最小页面」——样式与骨架都取 sync-ui.js 抽出来的
 * 同一份真源（design/ui-full.html），脚本用 media/narnat.js（sync 产物），
 * 再用 headless Edge 打开、把断言结果写进 DOM，最后 --dump-dom 取回来。
 * 断言走的是真实事件链路（window.postMessage / drop / paste / keydown），
 * 不是把渲染逻辑抄一遍。
 *
 * 用法：node scripts/verify-c.js
 * 退出码 0 表示全部通过。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const mStyle = /<style>([\s\S]*?)<\/style>/.exec(html);
const mBody = /<body[^>]*>([\s\S]*?)<script>/.exec(html);
if (!mStyle || !mBody) {
  console.error('提取失败', !!mStyle, !!mBody);
  process.exit(1);
}
const CSS = mStyle[1];
const BODY = mBody[1];
const JS = fs.readFileSync(path.join(ROOT, 'media/narnat.js'), 'utf8');

/* ---------------------------- 页面里跑的测试 ---------------------------- */
const TEST = String.raw`
(function(){
  var out = [];
  function log(ok, name, extra){
    // extra 必须是单行：结果靠 --dump-dom 取回，多行会被后续的按行过滤吃掉
    var tail = extra === undefined ? '' : ' :: ' + String(extra).replace(/\r?\n/g, '\\n');
    out.push('RESULT ' + (ok ? 'PASS' : 'FAIL') + ' ' + name + tail);
  }
  function eq(ok, name, got){ log(!!ok, name, 'got=' + got); }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(msg){ window.postMessage(msg, '*'); return sleep(40); }
  function sentAll(type){ return window.__sent.filter(function(m){ return m && m.type === type; }); }
  function sentLast(type){ var a = sentAll(type); return a.length ? a[a.length - 1] : null; }
  function sysCount(){ return document.querySelectorAll('#convo .sys').length; }
  function chips(){ return Array.prototype.slice.call(document.querySelectorAll('#attachBox .atch')); }
  function state(){ return window.__narnatState(); }

  function enter(){
    var input = document.getElementById('input');
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true
    }));
  }
  function dropOn(sel, files){
    var ev = new Event('drop', { bubbles: true, cancelable: true });
    ev.dataTransfer = { files: files, dropEffect: '' };
    document.querySelector(sel).dispatchEvent(ev);
  }

  async function run(){
    /* ---------- 先握手，进入真实态 ---------- */
    await emit({ type: 'initialize', params: {
      version: 'test', protocol: 1,
      model: { current: 'test-model', options: ['test-model'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 },
      plugins: [],
      session: { current: null },
      workdir: '<测试目录>'
    }});
    eq(state().real === true, 'initialize 后进入真实态', state().real);

    /* ---------- C1：历史思考渲染 ---------- */
    await emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 't1', messages: [
      { role: 'user', text: '问一句' },
      { role: 'assistant', text: '回答', thinking: '思考内容' }
    ]}});
    var thinks = document.querySelectorAll('#convo .think');
    eq(thinks.length === 1, 'C1 带 thinking 的历史画出 1 个 .think', thinks.length);
    var slot = thinks[0] && thinks[0].querySelector('.slot');
    eq(slot && slot.textContent.indexOf('思考内容') >= 0, 'C1 .slot 文本含「思考内容」',
      slot ? slot.textContent : '(none)');
    eq(thinks[0] && thinks[0].getAttribute('data-open') === 'false', 'C1 思考块默认收起',
      thinks[0] ? thinks[0].getAttribute('data-open') : '(none)');
    var kids = Array.prototype.slice.call(document.getElementById('convo').children);
    var iThink = kids.indexOf(thinks[0]);
    var iBody = -1;
    kids.forEach(function(k, i){ if (i > iThink && k.className.indexOf('assistant') >= 0) iBody = i; });
    eq(iThink >= 0 && iBody > iThink, 'C1 思考块排在正文之前', 'think=' + iThink + ' body=' + iBody);
    eq(thinks[0] && thinks[0].querySelector('.name').textContent === '思考 · 4 字',
      'C1 名称写「思考 · N 字」', thinks[0] ? thinks[0].querySelector('.name').textContent : '(none)');
    eq(getComputedStyle(slot).display === 'none', 'C1 收起时 .slot 不可见', getComputedStyle(slot).display);
    thinks[0].querySelector('.row').click();
    await sleep(30);
    eq(thinks[0].getAttribute('data-open') === 'true' && getComputedStyle(slot).display !== 'none',
      'C1 点标题复用既有折叠逻辑（展开）',
      thinks[0].getAttribute('data-open') + '/' + getComputedStyle(slot).display);

    /* ---------- C1：旧历史（无 thinking）不出现思考块 ---------- */
    await emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 't2', messages: [
      { role: 'user', text: '问一句' },
      { role: 'assistant', text: '回答' }
    ]}});
    eq(document.querySelectorAll('#convo .think').length === 0,
      'C1 无 thinking 的旧历史不画思考块',
      document.querySelectorAll('#convo .think').length);

    /* ---------- C2：session/saved ---------- */
    var sys0 = sysCount();
    var list0 = sentAll('sessionList').length;
    await emit({ type: 'session/saved', params: { name: 'abc', current: null, switched: false } });
    var chip = document.getElementById('chipSess');
    eq(chip.textContent === 'abc', 'C2 switched:false 后 #chipSess 变成 abc', chip.textContent);
    eq(sysCount() === sys0 + 1, 'C2 switched:false 追加 1 行系统提示', sysCount() + '/' + sys0);
    eq(sentAll('sessionList').length === list0 + 1, 'C2 switched:false 触发一次 sessionList',
      sentAll('sessionList').length + '/' + list0);
    var sys1 = sysCount();
    var list1 = sentAll('sessionList').length;
    await emit({ type: 'session/saved', params: { name: 'abc', current: null, switched: false } });
    eq(sysCount() === sys1, 'C2 重复同一 switched:false 不重复插系统行', sysCount() + '/' + sys1);
    eq(sentAll('sessionList').length === list1, 'C2 重复同一 switched:false 不重复拉列表',
      sentAll('sessionList').length + '/' + list1);
    await emit({ type: 'session/saved', params: { name: 'abc', current: 'abc', switched: true } });
    eq(state().CUR === 'abc', 'C2 switched:true 后 CUR === abc', state().CUR);
    eq(document.getElementById('sessBtn').textContent.indexOf('abc') >= 0,
      'C2 switched:true 后 #sessBtn 内含 abc', document.getElementById('sessBtn').textContent);

    /* ---------- C4 渠道一：拖拽图片文件 ---------- */
    dropOn('.cbox', [{ path: 'E:\\tmp\\a.png', name: 'a.png' }]);
    await sleep(40);
    eq(chips().length === 1, 'C4 拖入图片后出现 1 个附件 chip', chips().length);
    var checkReq = sentLast('attach/check');
    eq(!!checkReq && checkReq.params.paths[0] === 'E:\\tmp\\a.png',
      'C4 前端把绝对路径交给 attach/check',
      checkReq ? JSON.stringify(checkReq.params.paths) : '(none)');
    await emit({ type: 'attach/checked', params: { id: checkReq.params.id,
      items: [{ path: 'E:\\tmp\\a.png', exists: true, kind: 'image' }] }});
    var thumbReq = sentLast('attach/thumb');
    eq(!!thumbReq && thumbReq.params.path === 'E:\\tmp\\a.png', 'C4 图片请求 attach/thumb',
      thumbReq ? thumbReq.params.path : '(none)');
    await emit({ type: 'attach/thumbResult', params: { id: thumbReq.params.id, dataURL: null } });
    var ph = document.querySelector('#attachBox .atch .ph');
    eq(!!ph && ph.textContent === '[图片]', 'C4 thumb 返回 null 时显示 [图片] 占位符',
      ph ? ph.textContent : '(none)');
    eq(chips()[0].textContent.indexOf('图片') >= 0, 'C4 chip 上标出类型「图片」', chips()[0].textContent);

    /* ---------- C4 发送格式（图片） ---------- */
    var input = document.getElementById('input');
    input.value = '看一下这张图';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    enter();
    await sleep(60);
    var chat = sentLast('chat');
    eq(!!chat && chat.params.text.indexOf('E:\\tmp\\a.png') >= 0,
      'C4 发送文本里含图片绝对路径',
      chat ? JSON.stringify(chat.params.text) : '(none)');
    eq(!!chat && chat.params.text.indexOf('[用户附带了以下本地路径，请用相应工具自行读取处理]') >= 0,
      'C4 发送文本带路径块标题', chat ? 'ok' : '(none)');
    eq(!!chat && chat.params.text.indexOf('- 图片：E:\\tmp\\a.png') >= 0,
      'C4 图片行格式正确', chat ? JSON.stringify(chat.params.text) : '(none)');
    eq(chips().length === 0, 'C4 发送成功后清空附件列表', chips().length);

    /* ---------- C4 渠道一：拖入文件夹 ---------- */
    dropOn('#convo', [{ path: 'E:\\proj\\src', name: 'src' }]);
    await sleep(40);
    var checkReq2 = sentLast('attach/check');
    await emit({ type: 'attach/checked', params: { id: checkReq2.params.id,
      items: [{ path: 'E:\\proj\\src', exists: true, kind: 'folder' }] }});
    eq(chips().length === 1 && chips()[0].textContent.indexOf('文件夹') >= 0,
      'C4 文件夹 chip 标出「文件夹」', chips().length ? chips()[0].textContent : '(none)');
    input.value = '看看这个目录';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    enter();
    await sleep(60);
    chat = sentLast('chat');
    eq(!!chat && chat.params.text.indexOf('- 文件夹：E:\\proj\\src') >= 0,
      'C4 发送文本里含文件夹绝对路径', chat ? JSON.stringify(chat.params.text) : '(none)');

    /* ---------- C4 渠道三：＋附件按钮 ---------- */
    document.getElementById('attachBtn').click();
    await sleep(40);
    var pickReq = sentLast('attach/pick');
    eq(!!pickReq, 'C4 点「＋附件」发出 attach/pick', pickReq ? 'yes' : '(none)');
    await emit({ type: 'attach/picked', params: { id: pickReq.params.id,
      paths: ['E:\\proj\\README.md'], cancelled: false } });
    var checkReq3 = sentLast('attach/check');
    await emit({ type: 'attach/checked', params: { id: checkReq3.params.id,
      items: [{ path: 'E:\\proj\\README.md', exists: true, kind: 'file' }] }});
    eq(chips().length === 1, 'C4 选择框回执变成 1 个 chip', chips().length);

    /* ---------- C4 渠道二：粘贴没有路径的图片 ---------- */
    var file = new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' });
    var pev = new Event('paste', { bubbles: true, cancelable: true });
    pev.clipboardData = { files: [file], items: [] };
    input.dispatchEvent(pev);
    await sleep(200);
    var saveReq = sentLast('attach/save');
    eq(!!saveReq && typeof saveReq.params.dataB64 === 'string' && saveReq.params.name === 'shot.png',
      'C4 粘贴的无路径图片走 attach/save', saveReq ? saveReq.params.name : '(none)');
    await emit({ type: 'attach/saved', params: { id: saveReq.params.id,
      path: 'C:\\Temp\\narnat_attachments\\1_shot.png', kind: 'image' } });
    var checkReq4 = sentLast('attach/check');
    await emit({ type: 'attach/checked', params: { id: checkReq4.params.id,
      items: [{ path: 'C:\\Temp\\narnat_attachments\\1_shot.png', exists: true, kind: 'image' }] }});
    var thumbReq2 = sentLast('attach/thumb');
    await emit({ type: 'attach/thumbResult', params: { id: thumbReq2.params.id,
      dataURL: 'data:image/png;base64,iVBORw0KGgo=' } });
    var img = document.querySelector('#attachBox .atch img');
    eq(!!img && img.getAttribute('src').indexOf('data:image/png') === 0,
      'C4 缩略图 dataURL 渲染成 img', img ? img.getAttribute('src').slice(0, 24) : '(none)');

    /* ---------- C4 失败要说话：路径不存在 ---------- */
    dropOn('.cbox', [{ path: 'E:\\nope\\missing.txt', name: 'missing.txt' }]);
    await sleep(40);
    var checkReq5 = sentLast('attach/check');
    var before = chips().length;
    await emit({ type: 'attach/checked', params: { id: checkReq5.params.id,
      items: [{ path: 'E:\\nope\\missing.txt', exists: false, kind: '' }] }});
    eq(chips().length === before - 1, 'C4 不存在的路径被剔除', before + ' -> ' + chips().length);
    eq(document.getElementById('convo').textContent.indexOf('附件路径不存在') >= 0,
      'C4 路径不存在有可见系统提示', 'sys');

    /* ---------- C3：边框与聚焦色 ---------- */
    var cbox = document.querySelector('.cbox');
    // 界面启动时会自动聚焦输入框：先明确失焦，否则「未聚焦」量到的就是聚焦色
    input.blur();
    await sleep(30);
    var cs = getComputedStyle(cbox);
    eq(cs.borderTopWidth === '1px', 'C3 .cbox 有 1px 边框', cs.borderTopWidth);
    eq(cs.borderTopStyle === 'solid', 'C3 .cbox 边框样式 solid', cs.borderTopStyle);
    var blurColor = cs.borderTopColor;
    input.focus();
    await sleep(30);
    var focusColor = getComputedStyle(cbox).borderTopColor;
    eq(blurColor !== focusColor, 'C3 聚焦时边框换色', blurColor + ' -> ' + focusColor);
    eq(getComputedStyle(input).outlineStyle === 'none' && getComputedStyle(input).borderTopWidth === '0px',
      'C3 #input 自身无边框/无 outline（边界交给 .cbox）',
      getComputedStyle(input).outlineStyle + '/' + getComputedStyle(input).borderTopWidth);

    /* ---------- 窄面板 249px 不破版 ---------- */
    dropOn('.cbox', [{ path: 'E:\\proj\\very\\deep\\directory\\with\\a\\long\\name\\file.txt', name: 'file.txt' }]);
    await sleep(40);
    var checkReq6 = sentLast('attach/check');
    await emit({ type: 'attach/checked', params: { id: checkReq6.params.id,
      items: [{ path: 'E:\\proj\\very\\deep\\directory\\with\\a\\long\\name\\file.txt', exists: true, kind: 'file' }] }});
    var shell = document.querySelector('.shell');
    shell.style.width = '249px';
    await sleep(80);
    var box = document.querySelector('.cbox');
    var crow = document.querySelector('.crow');
    eq(box.scrollWidth <= box.clientWidth + 1, '249px 下 .cbox 无横向溢出',
      box.scrollWidth + '/' + box.clientWidth);
    eq(crow.scrollWidth <= crow.clientWidth + 1, '249px 下 .crow 无横向溢出',
      crow.scrollWidth + '/' + crow.clientWidth);
    var chipEl = chips()[0];
    eq(!!chipEl && chipEl.getBoundingClientRect().width <= box.clientWidth,
      '249px 下附件 chip 不超过输入区宽度',
      chipEl ? Math.round(chipEl.getBoundingClientRect().width) + '/' + box.clientWidth : '(none)');
    eq(getComputedStyle(document.getElementById('attachBox')).flexWrap === 'wrap',
      '249px 下附件列表用 flex-wrap 换行',
      getComputedStyle(document.getElementById('attachBox')).flexWrap);

    /* ---------- 汇总 ---------- */
    var fails = out.filter(function(l){ return l.indexOf('FAIL') >= 0; });
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\n') + '\nSUMMARY total=' + out.length + ' fail=' + fails.length;
    document.body.appendChild(pre);
    document.title = 'DONE fail=' + fails.length;
  }

  run().catch(function(err){
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = 'RESULT FAIL 测试脚本抛错 :: ' + (err && err.stack ? err.stack : err);
    document.body.appendChild(pre);
  });
})();
`;

const STUB = String.raw`
window.__sent = [];
window.acquireVsCodeApi = function(){
  return { postMessage: function(m){ window.__sent.push(m); } };
};
`;

const page = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-c</title>
<style>${CSS}</style></head>
<body data-run="false">
${BODY}
<script>${STUB}</script>
<script>${JS}</script>
<script>${TEST}</script>
</body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-c.html');
fs.writeFileSync(file, page, 'utf8');

// headless 浏览器：优先 Edge（Windows 自带），其次 Chrome
const candidates = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'
];
const browser = candidates.find((p) => fs.existsSync(p));
if (!browser) {
  console.error('找不到 Edge/Chrome，无法跑真实 DOM 验收。页面已生成：' + file);
  process.exit(1);
}

const url = 'file:///' + file.replace(/\\/g, '/');
let dom = '';
try {
  dom = execFileSync(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--user-data-dir=' + path.join(dir, 'profile'),
    '--virtual-time-budget=20000',
    '--dump-dom', url
  ], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
} catch (err) {
  console.error('浏览器执行失败：' + (err && err.message ? err.message : err));
  console.error('生成的页面：' + file);
  process.exit(1);
}

const block = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
if (!block) {
  console.error('没拿到任何断言输出（页面可能没跑起来）：' + file);
  process.exit(1);
}
const lines = block[1]
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
  .split(/\r?\n/)
  .filter((l) => /^(RESULT|SUMMARY) /.test(l));
if (!lines.length) {
  console.error('断言输出为空（页面可能没跑起来）：' + file);
  process.exit(1);
}
console.log(lines.join('\n'));
const fails = lines.filter((l) => l.indexOf('RESULT FAIL') === 0);
console.log(fails.length ? '验收失败 ' + fails.length + ' 项' : '验收全部通过');
process.exit(fails.length ? 2 : 0);
