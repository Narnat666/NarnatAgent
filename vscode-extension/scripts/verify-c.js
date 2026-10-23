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
 * 用法：node scripts/verify-c.js [--js-file=<另一个 narnat.js>]
 * 退出码 0 表示全部通过。
 *
 * 任务 D 的两件事都在这个文件里：
 *   D1 让脚本重新能跑：`#chipSess` 那批引用了已删节点的断言改成引用现役节点
 *      （脚本过时，不是产品缺陷 —— 见下面每条断言旁的说明）。
 *   D2 独立验收：用真实历史会话（output/nn-tasks/fixture.json）走**真实的
 *      postMessage 入口**（宿主→webview 的宏任务路径）渲染，从四个与
 *      verify-history.js / verify-toolrow.js 都不重叠的角度判定当前产物：
 *      首个历史折叠组是否展开、窄面板下工具行是否横向溢出、payload 规模、
 *      零 window.onerror。
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
let JS = fs.readFileSync(path.join(ROOT, 'media/narnat.js'), 'utf8');

/* 反向对照：--js-file=<path> 指向另一份 narnat.js（例如 output/nn-tasks/
   narnat.b2-before.js 这份 HEAD 产物），样式与骨架仍取真源。用来证明断言
   真的有区分力，而不是「改完之后当然全 PASS」的口头结论。与
   verify-history.js / verify-toolrow.js 的同名参数一致。 */
const jsOverride = /--js-file=("[^"]+"|\S+)/.exec(process.argv.join(' '));
if (jsOverride) {
  const p = jsOverride[1].replace(/^"|"$/g, '');
  JS = fs.readFileSync(p, 'utf8');
  console.log('[来源] 指定产物 ' + p + '（' + JS.length + ' 字节）');
}

/* D2 的真实历史会话数据（server._history_payload 导出的 1099 条消息 / 675 条工具），
   由 output/nn-tasks/dump-fixture.py 生成。与 verify-history.js 取的是同一个文件，
   但这里不调用它、也不复用它的断言。 */
const FIXTURE_CANDIDATES = [
  path.resolve(__dirname, '../../output/nn-tasks/fixture.json'),
  path.resolve(__dirname, '../output/nn-tasks/fixture.json')
];
const FIXTURE_PATH = FIXTURE_CANDIDATES.find((p) => fs.existsSync(p));
if (!FIXTURE_PATH) {
  console.error('缺少 fixture（D2 需要真实历史会话数据）：' + FIXTURE_CANDIDATES[0]);
  console.error('先跑：python output/nn-tasks/dump-fixture.py');
  process.exit(1);
}
const FIXTURE = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'));
if (!FIXTURE || !Array.isArray(FIXTURE.messages) || !FIXTURE.messages.length) {
  console.error('fixture 结构不对（需要 { current, messages[] }）：' + FIXTURE_PATH);
  process.exit(1);
}

/* ---------------------------- 页面里跑的测试 ---------------------------- */
const TEST = String.raw`
(function(){
  var out = [];
  function log(ok, name, extra){
    // extra 必须是单行：结果靠 --dump-dom 取回，多行会被后续的按行过滤吃掉
    var tail = extra === undefined ? '' : ' :: ' + String(extra).replace(/\r?\n/g, '\\n');
    out.push('RESULT ' + (ok ? 'PASS' : 'FAIL') + ' ' + name + tail);
  }
  function diag(name, val){
    out.push('DIAG ' + name + ' = ' + String(val).replace(/\r?\n/g, '\\n'));
  }
  function eq(ok, name, got){ log(!!ok, name, 'got=' + got); }
  /* window.onerror 的收集器由 STUB 在 narnat.js **之前**挂上（见文件末尾），
     所以连产物自身的加载/初始化异常也能抓到。 */
  function onErrors(){ return window.__onerr || []; }
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
    /* D1 修正（判定：**脚本过时**）。
       原断言按「#convo 的直接子节点下标」比较思考块与正文的先后。现在历史里的
       思考块被收进「工作过程」折叠组（renderHistory 的注释写明「思考进组，
       dsh 也是把 reasoning 归到 process 区」），它不再是 #convo 的直接子节点，
       kids.indexOf(think) 恒为 -1 —— 断言失败但产品行为正确（顺序没变）。
       改成按**文档顺序**比较：思考块必须出现在第一条助手正文之前。 */
    var firstBody = document.querySelector('#convo .msg.assistant') ||
      document.querySelector('#convo .assistant');
    var thinkBeforeBody = !!(firstBody && thinks[0] &&
      (thinks[0].compareDocumentPosition(firstBody) & Node.DOCUMENT_POSITION_FOLLOWING));
    eq(thinkBeforeBody, 'C1 思考块在文档顺序里排在正文之前',
      'in=' + (thinks[0] && thinks[0].parentNode ? thinks[0].parentNode.className : '(none)') +
      ' body=' + (firstBody ? firstBody.className : '(none)'));
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
    var chip = document.getElementById('histBtn');
    /* D1 修正（判定：**脚本过时**，不是产品缺陷）。
       原断言读 #chipSess（输入行那个「历史对话」chip）。该节点已在
       ui-full.html 里删掉，入口挪到顶栏的 #histBtn（见 ui-full.html 5559-5592
       那段注释）；产物里 setChipSess() 改成只把会话名写进 #histBtn 的
       aria-label —— 按钮本身是纯图标 ☰，会话名顶栏胶囊 #sessBtn 已经有了。
       节点不存在 → null.textContent 抛错 → 整支脚本连一条断言都出不来。
       断言的**功能点**没变（「保存后界面知道存成了哪个名字」），只是换成
       现役节点 + 现役写法。 */
    eq(!!chip && String(chip.getAttribute('aria-label') || '').indexOf('abc') >= 0,
      'C2 switched:false 后 #histBtn 的 aria-label 记下会话名 abc',
      chip ? chip.getAttribute('aria-label') : '(无 #histBtn)');
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

    /* 把这一轮收口。产物在 post('chat') 成功后就置 data-run=true（进入运行态），
       之后按 Enter 会被「这一轮还在回答」的守卫拦下（media/narnat.js 5117）——
       这是产品的正确行为（不让发送打断正在流式的一轮），但原脚本假设可以连发，
       于是第二条消息永远发不出去，后面的断言跟着一起错（原脚本没有这一步，
       当年还没有这个守卫）。真实后端会回 turn/begin + turn/end 把它打回空闲。 */
    await emit({ type: 'turn/begin', params: { round: 1 } });
    await emit({ type: 'turn/end', params: { reason: 'done' } });

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

    /* 同上：再收口一次，后面的「粘贴无路径图片 / 路径不存在」用例才在空闲态下跑 */
    await emit({ type: 'turn/begin', params: { round: 1 } });
    await emit({ type: 'turn/end', params: { reason: 'done' } });

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
    /* FileReader.readAsDataURL 是真实异步任务（不是 timer）：固定 sleep(200)
       在 --virtual-time-budget 下不可靠 —— 虚拟时钟会先把 timer 快进完，
       读到的 dataURL 还没回来。改成轮询等 attach/save 出现（最多 2 秒），
       既稳又不再依赖一个凭感觉定下的等待时长。 */
    var saveReq = null;
    var savePolls = 0;
    while (savePolls < 20 && !saveReq) {
      await sleep(100);
      savePolls++;
      saveReq = sentLast('attach/save');
    }
    diag('paste.save.polls', savePolls);
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
    await sleep(60);
    var focusColor = getComputedStyle(cbox).borderTopColor;
    diag('c3.blur.border', blurColor);
    diag('c3.focus.border', focusColor);
    diag('c3.activeElement', document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : '(none)');
    /* D1 修正（判定：**脚本过时**，不是产品缺陷）。
       产品这一条一直是 .cbox:focus-within{border-color:var(--vscode-focusBorder,
       var(--line-strong))}，而常态边框是 var(--vscode-input-border,var(--line-strong))。
       两者在**没注入主题变量**的测试页里都回退到同一个 --line-strong，于是
       「聚焦换色」永远量到同一个颜色 —— 原断言测量的其实是测试环境缺少
       VS Code 主题变量，不是产品行为。修法：页面里注入一对可区分的主题变量
       （见 THEME），断言的才是「真的切到了 focusBorder」。 */
    eq(blurColor !== focusColor,
      'C3 聚焦时边框切到 --vscode-focusBorder（换色）', blurColor + ' -> ' + focusColor);
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

    /* ================= D2：独立验收（真实历史会话） =========================
       与 verify-history.js / verify-toolrow.js 的区别（不是重复它们）：
         · 入口不同：它们同步派发 MessageEvent；这里走**真实的宿主→webview
           入口** window.postMessage（宏任务），也就是 webview 里真正发生的那条路径。
         · 角度不同：它们断「.tools 数量 == fixture 条数」「全部工具行 laidOut」
           「第一个 .tp-row 高度 > 0」；这里断「首个折叠组是否**展开**（data-open
           + .tp-body 无 hidden）」「窄面板下工具行的横向溢出」「payload 规模」
           「零 window.onerror」。
       ====================================================================== */
    var FIX = window.__FIXTURE;
    var fixMsgs = (FIX && FIX.messages) || [];
    var fixTools = fixMsgs.filter(function(m){ return m && m.role === 'tool'; }).length;
    diag('d2.fixture.messages', fixMsgs.length);
    diag('d2.fixture.tool', fixTools);
    await emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: (FIX && FIX.current) || 'fixture', messages: fixMsgs
    }});
    // 1099 条消息 / 675 条工具要重建 3.5 万个节点，常规的 40ms 间隔不够
    await sleep(800);

    var CONVO = document.getElementById('convo');
    var d2Tools = CONVO.querySelectorAll('.tools');
    var d2Procs = CONVO.querySelectorAll('.turn-proc');
    diag('d2.convo.tools', d2Tools.length);
    diag('d2.convo.turn-proc', d2Procs.length);
    diag('d2.convo.children', CONVO.children.length);
    eq(d2Tools.length >= 600,
      'D2-1 fixture 经 sessionSwitch 回执画出 #convo .tools >= 600', d2Tools.length);

    /* 报障「打开历史会话看不到工具调度」的正面复验。
       任务 E 把默认态改成**收起**（对齐 dsh：「dsh 默认是折叠状态」），
       所以这里判的从「初始就展开」反过来：每组初始收起，且 .tp-body 真的
       不可见。data-open 是状态位、hidden 是可见性开关，两条分开判 ——
       只判一个都可能漏（实测过 data-open=true 而 tp-body 仍带 hidden 的中间态）。 */
    var firstProc = d2Procs[0];
    eq(!!firstProc && firstProc.getAttribute('data-open') === 'false',
      'D2-2 首个历史折叠组初始就是收起态（data-open="false"）',
      firstProc ? firstProc.getAttribute('data-open') : '(无 .turn-proc)');
    var d2FirstBody = firstProc ? firstProc.querySelector('.tp-body') : null;
    eq(!!d2FirstBody && d2FirstBody.hasAttribute('hidden'),
      'D2-3 首个历史折叠组的 .tp-body 带 hidden（组内工具行没有铺开）',
      d2FirstBody ? ('hidden=' + d2FirstBody.hasAttribute('hidden') +
        ' display=' + getComputedStyle(d2FirstBody).display) : '(无 .tp-body)');

    /* 下面的 D2-4 / D2-5 要量「工具行在窄面板下的横向溢出」，前提是它们
       处于可见布局。收起态下 clientWidth 恒为 0，0 <= 0+1 会**假通过**，
       所以先点开所有组再量（断言方式改，断言不删）。禁用过渡再点：
       tpReveal 先设 height:0 再设自然高度，同步读几何时量到的是动画起点。 */
    var noMotion = document.createElement('style');
    noMotion.textContent = '.tp-body{transition:none !important}';
    document.head.appendChild(noMotion);
    Array.prototype.forEach.call(d2Procs, function(p){
      var rowEl = p.querySelector('.tp-row');
      if (rowEl && p.getAttribute('data-open') === 'false') rowEl.click();
    });
    void CONVO.offsetHeight;
    diag('d2.afterExpand.open', Array.prototype.map.call(d2Procs, function(p){
      return p.getAttribute('data-open');
    }).slice(0, 8).join(','));

    /* ---------- D2-4 / D2-5：窄面板 249px 下随机 5 条工具行 ----------
       随机用**固定种子**的伪随机：失败必须可复现。Math.random 会让两次运行
       抽到不同的行，报告里贴的证据就和别人跑出来的对不上。 */
    document.querySelector('.shell').style.width = '249px';
    void CONVO.offsetHeight;
    await sleep(200);
    var seed = 20261023;
    function rnd(){
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    }
    var pool = [];
    var pi;
    for (pi = 0; pi < d2Tools.length; pi++) pool.push(pi);
    var picks = [];
    while (picks.length < 5 && pool.length) {
      picks.push(pool.splice(Math.floor(rnd() * pool.length), 1)[0]);
    }
    var rowDiag = [];
    var argDiag = [];
    var overBad = [];
    var argBad = [];
    picks.forEach(function (idx) {
      var wrap = d2Tools[idx];
      var row = wrap ? wrap.querySelector('.row') : null;
      var arg = row ? row.querySelector('.arg') : null;
      var argText = arg ? String(arg.textContent) : '';
      /* clientWidth > 0 是这条断言的前提，不是附加条件：display:none 的祖先下
         scrollWidth/clientWidth 都是 0，0 <= 0+1 会**假通过** —— 那这条断言
         就检测不到「行根本没铺开」（HEAD 产物正是这个情形）。 */
      rowDiag.push(idx + '=' + (row ? row.scrollWidth + '/' + row.clientWidth : '(none)'));
      argDiag.push(idx + '=' + JSON.stringify(argText.slice(0, 28)));
      if (!row || !(row.clientWidth > 0) || row.scrollWidth > row.clientWidth + 1) {
        overBad.push(idx + ':' + (row ? row.scrollWidth + '/' + row.clientWidth : '(none)'));
      }
      if (!argText.trim()) {
        argBad.push(idx + ':' + JSON.stringify(argText));
      }
    });
    diag('d2.narrow.picks', picks.join(','));
    diag('d2.narrow.row.scroll/client', rowDiag.join(' '));
    diag('d2.narrow.arg.head', argDiag.join(' '));
    eq(picks.length === 5 && overBad.length === 0,
      'D2-4 249px 下随机 5 条工具行的 .row 无横向溢出（clientWidth>0 且 scrollWidth<=clientWidth+1）',
      overBad.length ? overBad.join(' ; ') : rowDiag.join(' '));
    eq(picks.length === 5 && argBad.length === 0,
      'D2-5 249px 下随机 5 条工具行的 .arg 摘要文本非空',
      argBad.length ? argBad.join(' ; ') : argDiag.join(' '));

    /* ---------- D2-6：payload 规模上限 ----------
       防止未来某次改动把渲染量炸掉（比如把长输出整段铺进 DOM、或重复渲染）。
       当前实测约 1.6M 字符；4M 是 2.5 倍余量。 */
    var htmlChars = CONVO.innerHTML.length;
    diag('d2.convo.innerHTML.length', htmlChars);
    eq(htmlChars < 4000000,
      'D2-6 渲染真实 fixture 后 #convo.innerHTML.length < 4000000', htmlChars);

    /* ---------- D2-7：整轮零 window.onerror ----------
       挂在 narnat.js 之前（见 STUB），所以产物自身初始化期抛的错也算。 */
    diag('d2.window.onerror.count', onErrors().length);
    eq(onErrors().length === 0, 'D2-7 整轮验收期间零 window.onerror',
      onErrors().length ? JSON.stringify(onErrors()) : '0 条');

    /* ---------- 汇总 ---------- */
    var fails = out.filter(function(l){ return l.indexOf('RESULT FAIL') === 0; });
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\n') + '\nSUMMARY total=' + out.filter(function(l){
      return l.indexOf('RESULT ') === 0;
    }).length + ' fail=' + fails.length;
    document.body.appendChild(pre);
    document.title = 'DONE fail=' + fails.length;
  }

  run().catch(function(err){
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    /* stack 压成一行：结果靠 --dump-dom 取回，多行会被按行过滤吃掉，
       只剩一句 message 就定位不到是哪个用例崩的。 */
    var st = String(err && err.stack ? err.stack : err).replace(/\r?\n/g, ' | ');
    /* 已经产出的断言也一起带出去：中途崩掉时，前面那些 PASS/FAIL 正是
       判断「哪一条过时」的依据，不能因为最后一条抛错就全丢掉。 */
    pre.textContent = (out.length ? out.join('\n') + '\n' : '') +
      'RESULT FAIL 测试脚本抛错 :: ' + st;
    document.body.appendChild(pre);
  });
})();
`;

const STUB = String.raw`
window.__sent = [];
window.__onerr = [];
/* window.onerror 必须在产物**之前**挂上：narnat.js 加载/初始化期抛的错也要
   算进 D2-7。用 onerror 而不是 addEventListener('error')，因为要判的正是
   「window.onerror 有没有被触发」这件事本身。 */
window.onerror = function(msg, src, line, col, err){
  window.__onerr.push(String(msg) + ' @' + String(src) + ':' + String(line) + ':' +
    String(col) + (err && err.stack ? ' :: ' + String(err.stack).split('\n')[0] : ''));
  return false;
};
window.acquireVsCodeApi = function(){
  return { postMessage: function(m){ window.__sent.push(m); } };
};
`;

const fixtureBytes = fs.statSync(FIXTURE_PATH).size;
const FIXTURE_JS = JSON.stringify(FIXTURE).replace(/<\//g, '<\\/');

/* VS Code 主题变量的一对模拟值。真实 webview 里它们由宿主注入，而且**不同**：
   --vscode-input-border 是输入框常态边框，--vscode-focusBorder 是聚焦高亮。
   ui-full.html 的兜底把两者都回退到 --line-strong，所以不注入的话，
   「聚焦换色」这条断言永远量到同一个颜色（测的是测试环境，不是产品）。 */
const THEME = ':root{--vscode-input-border:#3c3c3c;--vscode-focusBorder:#0098ff}';

const page = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-c</title>
<style>${CSS}</style>
<style>${THEME}</style></head>
<body data-run="false">
${BODY}
<script>${STUB}
window.__FIXTURE = ${FIXTURE_JS};
window.__FIXTURE_BYTES = ${fixtureBytes};
</script>
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
    '--window-size=760,900',
    '--virtual-time-budget=20000',
    '--dump-dom', url
    /* maxBuffer 提到 256MB：D2 渲染真实 fixture 后页面有 3.5 万个节点，
       --dump-dom 的输出加上内嵌的 fixture JSON 已经是 MB 级。 */
  ], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'], timeout: 180000 });
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
  .filter((l) => /^(RESULT|DIAG|SUMMARY) /.test(l));
if (!lines.length) {
  console.error('断言输出为空（页面可能没跑起来）：' + file);
  process.exit(1);
}
console.log(lines.join('\n'));
const fails = lines.filter((l) => l.indexOf('RESULT FAIL') === 0);
console.log(fails.length ? '验收失败 ' + fails.length + ' 项' : '验收全部通过');
process.exit(fails.length ? 2 : 0);
