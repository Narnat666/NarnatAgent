(function(){
  var body = document.body;
  var input = document.getElementById('input');
  var palette = document.getElementById('palette');
  var drawer = document.getElementById('drawer');
  var convo = document.getElementById('convo');

  /* ---------- RPC 桥：webview 只能 postMessage，不能 require ---------- */
  var vscodeApi = null;
  try {
    if (typeof acquireVsCodeApi === 'function') vscodeApi = acquireVsCodeApi();
  } catch (err) {
    vscodeApi = null;
  }

  /**
   * 离线时会被后端吞掉、界面上又完全没有反应的请求。
   * 会话类操作（新建/切换/删除/改名/开分支/合并）全靠后端回执刷新界面，
   * 掉线后点它们：请求发不出去、回执永远不来，界面一动不动，
   * 用户只能反复点（实测：点「新建对话」5 次，一次反应都没有）。
   */
  var OFFLINE_BLOCKED = {
    sessionNew: 1, sessionSwitch: 1, sessionDelete: 1, sessionRename: 1,
    sessionExplore: 1, sessionDone: 1, sessionList: 1,
    // configSet（模型/思考/目标/插件切换）也必须拦。它原来不在表里，
    // 离线点下拉会真的发出请求并压进 configKeyQueue；重连后成功回执被这些
    // 陈旧 key 配对，失败回退时模型按钮显示成 high、下拉连勾都没有（实测）。
    // 而且下拉本身已经把界面改成新值了，离线根本不可能生效。
    configSet: 1,
    chat: 0, command: 0, cancel: 0
  };

  function post(type, params) {
    if (REAL && body.getAttribute('data-offline') === 'true' && OFFLINE_BLOCKED[type]) {
      // 说清楚「没执行」，而不是让用户以为是点歪了。
      // 但要按类型去重：连点 5 次「新建对话」不该刷 5 行一样的提示（实测）。
      var dupKey = 'offline:' + type;
      if (lastOfflineHint !== dupKey) {
        lastOfflineHint = dupKey;
        sysLine('·', '未连接后端，操作未执行', '');
        scrollBottom();
      }
      // 拦截后不会有回执：所有「等回执」的锁必须在这里解掉，
      // 否则那一条永久点不动（实测：离线时点删除→是，条目锁死，
      // 0 请求 0 提示，直到会话列表重画）。
      if (type === 'sessionNew') unlockNewChat();
      unlockAllSess();
      pendingSwitchTo = '';
      return;
    }
    if (vscodeApi) vscodeApi.postMessage({ type: type, params: params || {} });
  }
  var lastOfflineHint = '';

  var REAL = false;          // 收到 initialize 后切真实态（此前保留演示数据）
  var FIRST_INIT_SEEN = false; // 是否已经历过一次握手（区分冷启动与首次演示态）
  var assistantBuf = '';     // 当前助手消息正文缓冲（原始 Markdown）
  var authHintShown = false; // 本轮是否已提示过「去填 API Key」（避免重复刷）
  var lastSentText = '';     // 最后一条发出去的消息正文（回执里没有请求 id，靠它找回被拒的那条）
  /* 运行中被拦下的发送：不能当场提示（写系统行会 closeAssistantSegment 把正在
     流式的正文劈成两个气泡），所以记一笔，等这一轮收口时再补一行说明。
     见 sendChat 里的守卫注释。 */
  var blockedWhileRunning = false;
  // 离线/演示态下最后一次「发出去但没连上」的内容与时间：同一句话 1.5 秒内不重发
  var lastOfflineSend = { text: '', ts: 0 };
  // 「刚才那条可能已经被后端接受了」。回执是广播的、不带发起方 id，收到
  // accepted:true 时无法确定它说的是不是本面板这条。保守记一笔：
  //   · 有这条标记 → 断连时**不**撤回（它多半已经送达，撤回会丢内容 + 谎报没发出去）
  //   · 没有 → 断连时撤回（那才是「点了发送但请求没转出去」的真情形）
  var lastSentMaybeAccepted = false;
  var curAssistant = null;   // 当前 .msg.assistant
  var curThink = null;       // 当前思考块
  var pendingTodoRow = null; // 由 tool/call 预留的「更新计划」行，等 todo/update 填充
  var toolMap = {};          // tool id → { wrap, row, slot, isTodo }
  /* ---- 一轮的「工作过程」折叠（抄 dsh 的 TurnProcessNodeView）----
     一轮跑完后，那一整串工具调用对用户已经没用了：他关心的是最后的回答。
     dsh 的做法是收成一条带下边框的摘要行（「已工作 · 1分23秒」/「已停止」），
     点一下才展开看细节。这里照做。
     三个变量记「本轮」的边界与规模；tool/result 到达时打 data-turn 标记，
     轮末只收标记过的节点 —— 历史重放的工具行没有标记，不会被卷进来。 */
  var turnStartNode = null;  // 本轮第一个节点之前的兄弟节点（null=从头算）
  var turnStartTime = 0;
  var turnToolCount = 0;
  var turnThinkCount = 0;
  var sysMap = {};           // 系统行（verify / compress）句柄
  var offlineShown = false;
  var offlineLine = null;
  // 对话区里已经画过的「后端历史」条数。
  //
  // 为什么用计数而不是布尔：布尔只能表达「画过/没画过」，而实际需要同时回答
  // 「画过多少」和「后端后来又多了几条」。用布尔时连续踩了三次坑（实测）：
  //   · 面板重建 + 运行中：turn/begin{resumed} 置 true → 随后带 4 条历史的
  //     fresh initialize 被挡住 → 正文只剩 18/32 字，本轮用户消息行也消失
  //   · 空闲重建：cached 快照 2 条置 true → fresh 的 4 条只画出 2 条
  //     （丢了本轮刚完成的问答）
  //   · sessionSwitch//cd/sessionDone 画完历史没置位 → 下一个 initialize
  //     把整段清空重画
  var historyDrawn = 0;      // 这个 webview 已画过几条后端历史（0=还没画过）
  var historyDrawnFor = '';  // 上面的历史属于哪个会话。会话变了就必须重画
  // 「正在切往哪个会话」：回执回来前挡住连点产生的重复 sessionSwitch
  var pendingSwitchTo = '';
  /**
   * 会话操作的就绪锁：会话名 → 到期时间戳。
   *
   * 为什么不能只用 DOM 属性（`data-busy`）：属性跟着节点走，而任何一次
   * `renderSessions()` 都会换掉节点 —— 锁凭空消失，同一会话可被重复提交
   *（实测）。反过来，属性也不会自己过期：离线时请求被前端拦下、永远没有回执，
   * 那一条就永久锁死（实测点它 0 请求 0 提示）；一次失败回执又会把所有
   * 条目的锁一起清掉（querySelectorAll 扫全表），另一条还在等回执的也跟着开锁
   *（实测可重复提交）。
   *
   * 所以真正的状态放这里（唯一权威），DOM 只负责显示：
   *   · 每条独立加锁/解锁（不扫全表）
   *   · 15 秒自动过期（任何回执都会在此之前到；离线拦下的则由 post() 立刻解）
   *   · 重连（initialize）时整体清空 —— 旧连接上的请求不可能再有回执
   */
  var sessLocks = {};
  /** cancel 已回执但 turn/end 迟迟不来时的兜底收口定时器（见 applyCancelResult） */
  var cancelSettleTimer = null;
  var cancelSettleTimerFired = false;
  /** 「已提交确认，等待后端执行…」那行的兜底定时器（见 replyConfirm） */
  var confirmWaitTimer = null;
  /** 会话列表读取失败的系统行（状态类提示，下次成功时撤掉） */
  var sessListErrLine = null;
  /** 本次会话列表是否**没读到**（区别于「真的没有会话」）。renderSessions 用它决定空态文案。 */
  var SESS_LIST_ERROR = false;
  /** 是否已经显示过正的余额（用来区分「还没查到」与「真的花光了」，见 applyStats） */
  var balanceSeen = false;
  function lockSess(name, action){
    if (!name) return;
    sessLocks[name] = { until: Date.now() + 15000, action: action || '' };
  }
  function unlockSess(name){
    if (name) delete sessLocks[name];
  }
  function unlockAllSess(){
    sessLocks = {};
  }
  /** 解锁「某一类操作」的锁：回执里没有可靠的名字时按动作类型清，不影响别的动作。 */
  function unlockSessByAction(action){
    if (!action) return;
    Object.keys(sessLocks).forEach(function (k) {
      if (sessLocks[k] && sessLocks[k].action === action) delete sessLocks[k];
    });
  }
  function isSessLocked(name){
    if (!name) return false;
    var rec = sessLocks[name];
    if (!rec) return false;
    if (Date.now() > rec.until) { delete sessLocks[name]; return false; }  // 过期即解锁
    return true;
  }
  // 「当前状态」类提示的行节点：状态好了要能把它们撤掉，否则界面会一直
  // 指路去做已经做完的事（实测：填好 Key 后「还没填 API Key」永久留着）。
  var apiKeyHintLine = null;
  var cfgWarnLines = [];
  // 掉线过 ⇒ 下次握手是新进程，状态栏里的上下文/费用是上一个进程的，得清
  var statsStale = false;
  // 刚提交的那次确认（等 confirm/result 回执后替换文案）
  var pendingConfirmLine = null;

  /** 供诊断/回放断言用的内部状态 */
  var RPC_STATE = {
    initialized: false,
    protocol: 0,
    lastRender: null,
    renders: [],
    init: null
  };
  window.NARNAT_STATE = RPC_STATE;

  function esc(s) {
    return String(s === undefined || s === null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /**
   * 走宿主的剪贴板写入。真正的 VSCode 面板里只有这条路可靠：
   * webview 是沙箱 iframe，navigator.clipboard 在部分宿主/权限策略下会以
   * NotAllowedError（"Document is not focused" 等）拒绝，而 Promise 一拒，
   * 按钮就成了「点了没反应的摆设」（用户实测反馈）。
   * vscode.env.clipboard 是官方 API，不受 iframe 权限影响。
   * 带回执：写失败要如实显示「复制失败」，不能骗用户已复制。
   */
  var clipSeq = 0;
  var clipWaiters = {};
  function copyViaHost(text) {
    return new Promise(function (resolve, reject) {
      var id = ++clipSeq;
      var timer = setTimeout(function () {
        delete clipWaiters[id];
        reject(new Error('host clipboard timeout'));
      }, 1500);
      clipWaiters[id] = function (ok) {
        clearTimeout(timer);
        ok ? resolve() : reject(new Error('host clipboard failed'));
      };
      post('clipboard/write', { text: text, id: id });
    });
  }
  /** 宿主回执：clipboard/written {id, ok} */
  function onClipboardWritten(p) {
    var id = p && p.id;
    var cb = clipWaiters[id];
    if (!cb) return;
    delete clipWaiters[id];
    cb(p.ok === true);
  }

  /**
   * 在 webview 内部自己写剪贴板（无宿主时用：预览页、回放测试）。
   * navigator.clipboard 要求安全上下文，没有就退到 execCommand。
   */
  function copyTextLocal(s) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(s);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = s;
      ta.style.position = 'fixed';
      ta.style.top = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand copy failed'));
    });
  }

  /**
   * 写剪贴板（复制按钮共用）。
   * 真实面板优先走宿主；宿主不可用或失败时再退回 webview 内部实现，
   * 保证「预览/回放」这类没有宿主的场景仍然能复制。
   */
  function copyText(text) {
    var s = String(text === undefined || text === null ? '' : text);
    if (!s) return Promise.reject(new Error('nothing to copy'));
    if (vscodeApi) {
      return copyViaHost(s).catch(function () { return copyTextLocal(s); });
    }
    return copyTextLocal(s);
  }

  /* 鼠标选中 → 右键「复制」拿不到内容的问题。
   *
   * 现象：文字能用鼠标选中、高亮也对，但粘贴出来是空的。
   * 原因：webview 是沙箱 iframe，右键菜单是 Electron 的**原生菜单**，
   * 它复制什么由宿主决定；渲染进程 `copy` 事件里 setData 写的只是临时数据，
   * 原生菜单读不到 —— 结果就是「能选中、粘贴为空白」，而且全程不报错。
   * 上面 copyText（复制按钮用的）走 navigator.clipboard，所以按钮是好的。
   *
   * 修法：选中后主动把选区发给宿主，由宿主用 vscode.env.clipboard 写，
   * 那是官方 API，不受 iframe 剪贴板权限限制。
   * 时机用 mouseup / keyup：用户刚选完就写，随后的右键「粘贴」拿到的就是它。
   * 只在**真的产生新选区**时发，避免每次点击都写一次剪贴板。 */
  var lastCopiedSel = '';
  function pushSelectionToHost(){
    var sel = '';
    try { sel = String(window.getSelection() || ''); } catch (err) { return; }
    if (!sel) { lastCopiedSel = ''; return; }
    /* 选区落在输入框**内部**时才不接管：那是用户在编辑自己打的字，
       复制该由浏览器/输入框自己处理。
       注意判据必须是「选区在不在输入框里」，不能只看 activeElement ——
       面板启动时会把焦点放到 #input，而用户完全可能没在里面打字、
       只是在对话区框选一段回答；只判 activeElement 会把这种正常复制挡掉
       （实测：activeElement 恒为 input，clipCount 永远是 0）。 */
    try {
      var ae = document.activeElement;
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) {
        var s = window.getSelection();
        if (s && s.rangeCount) {
          var node = s.getRangeAt(0).commonAncestorContainer;
          var host = node && (node.nodeType === 1 ? node : node.parentNode);
          if (host && (host === ae || (ae.contains && ae.contains(host)))) return;
        }
      }
    } catch (err) { /* 取不到就按「不在输入框」处理，宁可多写一次剪贴板 */ }
    if (sel === lastCopiedSel) return;
    lastCopiedSel = sel;
    if (vscodeApi) vscodeApi.postMessage({ type: 'clipboard/write', params: { text: sel } });
  }
  document.addEventListener('mouseup', function(){ setTimeout(pushSelectionToHost, 0); });
  document.addEventListener('keyup', function(e){
    // 只有选择类按键才需要（Shift+方向键、Ctrl+A）
    if (e.shiftKey || e.key === 'a' || e.key === 'A') setTimeout(pushSelectionToHost, 0);
  });
  /* Ctrl+C 兜底：某些环境下用户习惯按快捷键，走同一条宿主通道。
     有选区时拦下默认行为，避免它又走那条静默失败的原生通路。 */
  document.addEventListener('copy', function(e){
    var sel = '';
    try { sel = String(window.getSelection() || ''); } catch (err) { sel = ''; }
    if (!sel) return;
    if (vscodeApi) vscodeApi.postMessage({ type: 'clipboard/write', params: { text: sel } });
    lastCopiedSel = sel;
    e.preventDefault();
  });

  /* ---------- Markdown 表格 ---------- */

  /** 拆一行表格为单元格：去首尾竖线，按未转义的 | 分割 */
  function splitRow(line) {
    var s = String(line).trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    var cells = [];
    var buf = '';
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      if (c === '\\' && s.charAt(i + 1) === '|') {
        buf += '|';
        i++;
        continue;
      }
      if (c === '|') {
        cells.push(buf.trim());
        buf = '';
        continue;
      }
      buf += c;
    }
    cells.push(buf.trim());
    return cells;
  }

  /** 是否是表格分隔行（|---|:--:|---:|），至少一列且每列都是 - 加可选冒号 */
  function isTableSep(line) {
    if (String(line).indexOf('-') < 0) return false;
    var cells = splitRow(line);
    if (!cells.length) return false;
    return cells.every(function (c) {
      return /^:?-+:?$/.test(c.replace(/\s/g, ''));
    });
  }

  /** 分隔行 → 每列对齐方式 */
  function alignOf(cell) {
    var c = String(cell).replace(/\s/g, '');
    var left = c.charAt(0) === ':';
    var right = c.charAt(c.length - 1) === ':';
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return '';
  }

  function renderTable(head, aligns, body) {
    function cell(tag, text, idx) {
      var a = aligns[idx] || '';
      return '<' + tag + (a ? ' data-a="' + a + '"' : '') + '>' + (text || '') + '</' + tag + '>';
    }
    var h = '<div class="tw"><table><thead><tr>';
    for (var i = 0; i < head.length; i++) h += cell('th', head[i], i);
    h += '</tr></thead><tbody>';
    for (var r = 0; r < body.length; r++) {
      h += '<tr>';
      // 以表头列数为准，缺列补空、多列截断，保证表格不畸形
      for (var c = 0; c < head.length; c++) h += cell('td', body[r][c], c);
      h += '</tr>';
    }
    return h + '</tbody></table></div>';
  }

  /**
   * 提取管道表格并替换为占位符。
   * 输入是已转义 + 已做行内替换的文本，因此单元格内容可直接放进 HTML。
   * 表格行之间只有单换行，必须在「按空行分段」之前处理。
   */
  function extractTables(src) {
    var lines = String(src).split('\n');
    var tables = [];
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var isHead = lines[i].indexOf('|') >= 0 &&
        i + 1 < lines.length && isTableSep(lines[i + 1]);
      if (isHead) {
        // 分隔行的列数必须与表头一致。只判「第二行是分隔行」会怎样（实测）：
        // 普通文本 `版本 A | 版本 B` 后面跟一行 `---`（那是分隔线），
        // 会被判成「一列表头 + 一列分隔」，结果段落变成空表头、`---` 被吃掉。
        // 注意单列表格（`| a |` + `| --- |`）是合法的，所以下限是 1 列而不是 2。
        var headCells = splitRow(lines[i]);
        var sepCells = splitRow(lines[i + 1]);
        if (headCells.length < 1 || sepCells.length !== headCells.length) isHead = false;
      }
      if (!isHead) {
        out.push(lines[i]);
        i++;
        continue;
      }
      var head = splitRow(lines[i]);
      var aligns = splitRow(lines[i + 1]).map(alignOf);
      var body = [];
      var j = i + 2;
      while (j < lines.length && lines[j].trim() !== '' && lines[j].indexOf('|') >= 0) {
        body.push(splitRow(lines[j]));
        j++;
      }
      tables.push(renderTable(head, aligns, body));
      out.push('\u0000TABLE' + (tables.length - 1) + '\u0000');
      i = j;
    }
    return { text: out.join('\n'), tables: tables };
  }

  /**
   * 块级 Markdown：标题 / 引用 / 无序列表 / 有序列表 / 段落。
   *
   * 入参 src 已经过 esc()，所以 `>` 此刻是 `&gt;` —— 匹配引用必须用转义后的形式。
   * 同理 `<` 变 `&lt;`，其余标记字符（# - * | [ ] ( )）原样保留。
   *
   * 为什么必须做：AI 回复几乎每条都用 # / - / 1. / > / 链接，
   * 不做就只能显示成裸符号，可读性明显下降（实测用户反馈可读性差）。
   */
  function renderBlocks(src){
    var lines = src.split('\n');
    var out = [];
    var para = [];

    function flushPara(){
      if (!para.length) return;
      var body = para.join('<br>').replace(/^(<br>)+|(<br>)+$/g, '').trim();
      para = [];
      if (body) out.push('<p>' + body + '</p>');
    }

    /**
     * 从第 start 行开始解析一个列表块，返回 {html, next}。
     *
     * 为什么需要它（实测真后端输出）：
     *   · 原来只收集「trim 后仍以 - 或 1. 开头」的行，缩进的子项被一起
     *     收成同级 → 嵌套层级被压平（`li li` 数量恒为 0，AI 写的三级
     *     大纲在界面上变成一长条平铺）
     *   · 列表项之间夹一个空行或一个缩进代码块，原来的 while 就 break，
     *     后续项被当成新列表 → 编号从 1 重新开始（1/1/1），列表里的代码块
     *     还会脱出到顶层
     * @param {number} start 起始行号
     * @param {number} baseIndent 本级缩进（空格数，Tab 按 2 算）
     * @param {boolean} ordered 有序还是无序
     */
    function parseListAt(start, baseIndent, ordered) {
      var items = [];
      var i = start;
      var re = ordered ? /^(\s*)\d+[.)]\s+(.*)$/ : /^(\s*)[-*+]\s+(.*)$/;
      /* 有序列表的起始编号：AI 常写「3. …4. …5. …」（接着上文编号），
         而 <ol> 不写 start 时浏览器一律从 1 开始 —— 屏幕上就变成 1/2/3，
         用户照着编号对照会指错条目（实测）。
         只认第一项的编号，后面仍按连续递增（与 Markdown 常见实现一致）。 */
      var olStart = 0;
      if (ordered) {
        var sm = /^(\s*)(\d+)[.)]\s+/.exec(lines[start] || '');
        if (sm) olStart = parseInt(sm[2], 10) || 0;
      }
      while (i < lines.length) {
        var ln = lines[i];
        if (/^\s*$/.test(ln)) {
          // 空行不一定是列表结束：跳过连续空行，看下一行是否还属于本列表
          var j = i + 1;
          while (j < lines.length && /^\s*$/.test(lines[j])) j++;
          if (j < lines.length) {
            var nm = /^(\s*)([-*+]|\d+[.)])\s+/.exec(lines[j]);
            var nInd = nm ? nm[1].replace(/\t/g, '  ').length : -1;
            if ((nm && nInd >= baseIndent) || /^\s{2,}\S/.test(lines[j])) { i = j; continue; }
          }
          break;
        }
        var m = re.exec(ln);
        var indent = m ? m[1].replace(/\t/g, '  ').length : -1;
        if (m && indent === baseIndent) {
          items.push({ text: m[2], subs: [] });
          i++;
          continue;
        }
        if (m && indent > baseIndent) {
          // 更深缩进 → 子列表，挂到当前项下（真正生成 <ul>/<ol> 嵌套）
          var subOrdered = /^\s*\d/.test(ln);
          var sub = parseListAt(i, indent, subOrdered);
          if (items.length) items[items.length - 1].subs.push(sub.html);
          i = sub.next;
          continue;
        }
        // 缩进处是「另一种」列表（一级无序项下挂有序子项这类混排）：
        // 上面的 re 只认同类型，会把它当续行并成 `<br>1. xxx`（实测）。
        var other = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(ln);
        if (other) {
          var oInd = other[1].replace(/\t/g, '  ').length;
          if (oInd > baseIndent) {
            var sub2 = parseListAt(i, oInd, /^\d/.test(other[2]));
            if (items.length) items[items.length - 1].subs.push(sub2.html);
            i = sub2.next;
            continue;
          }
        }
        // 缩进续行（段落续行或列表内的代码块占位符）并进当前项
        if (/^\s{2,}\S/.test(ln) && items.length) {
          items[items.length - 1].text += '<br>' + ln.trim();
          i++;
          continue;
        }
        break;
      }
      var tag = ordered ? 'ol' : 'ul';
      // 起始编号不是 1 时才写 start（写 start="1" 无害但多余）
      var startAttr = (ordered && olStart > 1) ? ' start="' + olStart + '"' : '';
      var html = '<' + tag + startAttr + '>' + items.map(function (it) {
        return '<li>' + it.text + it.subs.join('') + '</li>';
      }).join('') + '</' + tag + '>';
      return { html: html, next: i };
    }

    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var t = line.trim();

      if (!t) { flushPara(); i++; continue; }

      // 代码块 / 表格占位符独占一行 → 块级元素，不包 <p>
      if (/^\u0000(BLOCK|TABLE)\d+\u0000$/.test(t)) {
        flushPara();
        out.push(t);
        i++;
        continue;
      }

      // 标题
      var hm = /^(#{1,6})\s+(.+)$/.exec(t);
      if (hm) {
        flushPara();
        var lv = hm[1].length;
        out.push('<h' + lv + '>' + hm[2] + '</h' + lv + '>');
        i++;
        continue;
      }

      // 引用（连续多行合并成一块）
      if (/^&gt;\s?/.test(t)) {
        flushPara();
        var qs = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i].trim())) {
          qs.push(lines[i].trim().replace(/^&gt;\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + qs.join('<br>') + '</blockquote>');
        continue;
      }

      // 列表（有序 / 无序，支持嵌套与项间空行）。
      // 必须拿 line（保留前导空格）而不是 t（trim 过）来匹配：
      // 缩进量决定层级，trim 之后所有子项都会被当成同级。
      var lm = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (lm) {
        flushPara();
        var ind0 = lm[1].replace(/\t/g, '  ').length;
        var ordered0 = /^\d/.test(lm[2]);
        var lres = parseListAt(i, ind0, ordered0);
        out.push(lres.html);
        i = lres.next;
        continue;
      }

      para.push(line);
      i++;
    }
    flushPara();
    return out.join('');
  }

  /**
   * 代码块语法高亮。
   *
   * 自己实现而不引第三方库：插件在离线环境运行，不能走 CDN；highlight.js
   * 整包 250KB 进 vsix 只为了给几句代码上色，不划算。这里只按「注释 / 字符串 /
   * 数字 / 关键字 / 调用名」五类着色，覆盖日常阅读所需的结构辨识。
   *
   * 单遍字符扫描而不是多趟正则替换：多趟替换会把上一趟插进去的 <span class="tk-st">
   * 里的单词再匹配一次（字符串里的 for 被当关键字再包一层），颜色就乱了。
   *
   * 不认识的语言一律原样转义，不猜——猜错会把正常代码染成花斑。
   */
  /* 代码块的两个用户偏好：换行开/关、超长块展开/折叠。
     用户在 249px 里点过一次，就该对所有代码块（含本轮结束后重渲染的）生效。
     换行默认**关**：折行会破坏代码的行结构与缩进（见 .code[data-wrap] 的注释），
     而折行带来的「不用拖」抵不过「读错一行」的代价。与 VSCode 编辑器
     wordWrap 默认关闭保持一致；用户想要折行点一次即可，偏好会被记住。 */
  var wrapPref = false;
  var foldOpenPref = false;

  var HL_ALIAS = {
    js:'js', javascript:'js', jsx:'js', mjs:'js', cjs:'js', node:'js',
    ts:'ts', typescript:'ts', tsx:'ts',
    py:'py', python:'py', python3:'py',
    json:'json', jsonc:'json',
    sh:'sh', bash:'sh', shell:'sh', zsh:'sh', console:'sh', powershell:'sh', ps1:'sh',
    sql:'sql',
    go:'go', golang:'go',
    rs:'rs', rust:'rs',
    java:'java', kt:'java', kotlin:'java', cs:'java', csharp:'java',
    c:'c', h:'c', cpp:'c', 'c++':'c', cc:'c', hpp:'c', objc:'c',
    html:'html', htm:'html', xhtml:'html', xml:'html', svg:'html',
    css:'css', scss:'css', less:'css',
    bat:'bat', cmd:'bat', batch:'bat',
    diff:'diff', patch:'diff',
    yml:'yaml', yaml:'yaml', toml:'yaml', ini:'yaml', conf:'yaml'
  };
  var HL_KW = {
    js: 'break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new of return static super switch this throw try typeof var void while with yield async await from as get set true false null undefined',
    ts: 'break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new of return static super switch this throw try typeof var void while with yield async await from as get set true false null undefined interface type enum implements namespace declare readonly public private protected abstract any unknown never keyof infer module require',
    py: 'and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield True False None self match case',
    json: 'true false null',
    go: 'break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false make new len cap append',
    rs: 'as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while true false Some None Ok Err Vec String',
    java: 'abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for goto if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while true false null String Integer',
    c: 'auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while true false NULL include define ifdef ifndef endif',
    sh: 'if then else elif fi for while do done case esac function return exit echo cd export local readonly set unset source shift test',
    sql: 'select from where insert into update delete create table drop alter add index join left right inner outer on group by order having limit offset union all as and or not null is like in exists distinct values set begin commit rollback primary key foreign references',
    yaml: 'true false null yes no on off',
    /* AI 在前端/Script 场景最常返回的三种，原来全没映射 → 整块灰白一片。
       标签名与属性名放一起（HTML 里两者都是「关键字」），够用且不会染错。 */
    html: 'div span a p img ul li ol table tr td th thead tbody h1 h2 h3 h4 h5 h6 form input button select option label script style link meta head body html section header footer nav main aside article br hr pre code strong em class id href src alt title type name value rel charset content charset async defer',
    css: 'color background background-color background-image display position margin padding border border-radius font font-family font-size font-weight font-style flex grid align-items justify-content width height max-width min-width max-height min-height top left right bottom z-index overflow overflow-x overflow-y text-align line-height opacity transform transition animation box-shadow cursor content white-space word-break important inherit none auto solid block inline flex grid relative absolute fixed sticky',
    bat: 'if else goto call echo set setlocal endlocal for in do del copy move ren mkdir rmdir pushd popd exit pause start shift errorlevel exist not defined rem'
  };

  /**
   * diff 专用：按行首字符染色。
   * 它走不了通用 tokenizer（`+`/`-` 不是 token），但恰恰是「着色比不着色强太多」
   * 的一类——工具行里的 diff 只能靠行底色区分（实测对比度 1.14~1.52:1，
   * 肉眼几乎无差别），代码块里的 ```diff 至少得让用户一眼看出哪行加、哪行删。
   */
  function hlDiff(raw){
    var lines = String(raw).split('\n');
    for (var k = 0; k < lines.length; k++) {
      var s = lines[k];
      var cls = '';
      if (/^\+\+\+|^---|^diff |^index /.test(s)) cls = 'tk-meta';
      else if (/^@@/.test(s)) cls = 'tk-hdr';
      else if (/^\+/.test(s)) cls = 'tk-add';
      else if (/^-/.test(s)) cls = 'tk-del';
      lines[k] = cls ? '<span class="' + cls + '">' + esc(s) + '</span>' : esc(s);
    }
    return lines.join('\n');
  }

  /** 代码块高亮：入参是原始代码（未转义），返回可直接放进 <pre> 的 HTML。 */
  function hlCode(raw, lang){
    var key = HL_ALIAS[String(lang || '').toLowerCase()] || '';
    if (!key) return esc(raw);
    if (key === 'diff') return hlDiff(raw);
    var kws = ' ' + (HL_KW[key] || '') + ' ';
    var hash = (key === 'py' || key === 'sh' || key === 'yaml');
    // 注释定界符：HTML 的 `<!-- -->` 是 4 字符，bat 用 `::`，其余两字符够用。
    // html/css 没有行注释——给它们 `//` 会把 `url(http://x)` 整段当注释吃掉。
    var lineCm = key === 'sql' ? '--' : (key === 'bat' ? '::'
      : (hash ? '#' : (key === 'html' || key === 'css' ? '' : '//')));
    var blockCm = hash || key === 'sql' || key === 'bat' ? ''
      : (key === 'html' ? '<!--' : '/*');
    var blockEnd = key === 'html' ? '-->' : '*/';
    var out = '';
    var i = 0;
    var n = raw.length;
    while (i < n) {
      // 行注释
      if (lineCm && raw.substr(i, lineCm.length) === lineCm) {
        var le = raw.indexOf('\n', i);
        if (le < 0) le = n;
        out += '<span class="tk-cm">' + esc(raw.slice(i, le)) + '</span>';
        i = le;
        continue;
      }
      // 块注释
      if (blockCm && raw.substr(i, blockCm.length) === blockCm) {
        var be = raw.indexOf(blockEnd, i + blockCm.length);
        be = be < 0 ? n : be + blockEnd.length;
        out += '<span class="tk-cm">' + esc(raw.slice(i, be)) + '</span>';
        i = be;
        continue;
      }
      var ch = raw.charAt(i);
      // 字符串（含反引号模板串；普通引号串不跨行）
      if (ch === '"' || ch === "'" || ch === '`') {
        var j = i + 1;
        while (j < n) {
          if (raw.charAt(j) === '\\') { j += 2; continue; }
          if (raw.charAt(j) === ch) { j++; break; }
          if (ch !== '`' && raw.charAt(j) === '\n') break;
          j++;
        }
        out += '<span class="tk-st">' + esc(raw.slice(i, j)) + '</span>';
        i = j;
        continue;
      }
      // 数字（前置不能是标识符字符，避免把 foo2 里的 2 染色）
      if (ch >= '0' && ch <= '9' && !/[A-Za-z0-9_$]/.test(raw.charAt(i - 1))) {
        var jn = i;
        while (jn < n && /[0-9a-fA-FxX._n]/.test(raw.charAt(jn))) jn++;
        out += '<span class="tk-nu">' + esc(raw.slice(i, jn)) + '</span>';
        i = jn;
        continue;
      }
      // 标识符：关键字 / 调用名 / 原样
      if (/[A-Za-z_$]/.test(ch)) {
        var ji = i;
        while (ji < n && /[A-Za-z0-9_$]/.test(raw.charAt(ji))) ji++;
        var word = raw.slice(i, ji);
        if (kws.indexOf(' ' + word + ' ') >= 0) {
          out += '<span class="tk-kw">' + esc(word) + '</span>';
        } else if (raw.charAt(ji) === '(') {
          out += '<span class="tk-fn">' + esc(word) + '</span>';
        } else {
          out += esc(word);
        }
        i = ji;
        continue;
      }
      out += esc(ch);
      i++;
    }
    return out;
  }

  /** 行内 Markdown：围栏代码块 → <pre>、`x` → <code>、**x** → <strong>、[t](u) → <a>、管道表格 → <table>。先整体转义再替换。 */
  function markdownInline(text) {
    var src = String(text || '');
    var blocks = [];
    function pushBlock(lang, code) {
      var raw = code.replace(/\n$/, '');
      // 语言标签 + 换行开关 + 复制按钮放在外层 .code 的头部：<pre> 自身
      // overflow-x:auto，把按钮放进 <pre> 会被长行一起横向推出可视区，永远点不到。
      // 行数进 data-lines，超阈值时由 CSS 折叠（见 .code[data-fold]）。
      var lines = raw ? raw.split('\n').length : 0;
      // 换行开关 / 折叠开关要跨块记忆：用户在 249px 里把「换行」关掉一次，
      // 就该对所有代码块生效。原来每来一个新块都重置成 true，等于一条回答里
      // 要重复点若干次；轮末重渲染（renderAssistantFinal）还会把已经点过的
      // 状态一起抹掉（实测：流式中点「展开全部」，轮末又折回去）。
      var wrapOn = wrapPref ? 'true' : 'false';
      // 「是否真的折叠了」与「要不要给展开按钮」必须是同一个判据。
      // 只判 lines > 30 会怎样（实测）：用户点过一次「展开全部」之后
      // foldOpenPref 变 true，块不再折叠（data-fold 无、max-height 为 none、不可滚），
      // 但按钮照样渲染 —— 本窗口内所有 >30 行的代码块底部都挂着一个
      // 「展开全部 N 行」，点下去高度和行数一点变化都没有，是个彻底的死按钮。
      // 三条渲染路径（流式分段 / 轮末重渲染 / 历史重建）都会命中。
      var doFold = lines > 30 && !foldOpenPref;
      var folded = doFold ? ' data-fold="true"' : '';
      blocks.push('<div class="code" data-wrap="' + wrapOn + '"' + folded + '>' +
        '<div class="code-hd"><span class="lang">' + esc(lang || 'text') + '</span>' +
        '<button class="code-wrap" type="button" data-on="' + wrapOn + '">换行</button>' +
        '<button class="code-copy" type="button">复制</button></div>' +
        '<pre data-lang="' + esc(lang) + '" data-lines="' + lines + '">' +
        hlCode(raw, lang) + '</pre>' +
        (doFold ? '<button class="code-more" type="button">展开全部 ' + lines + ' 行</button>' : '') +
        '</div>');
      return '\u0000BLOCK' + (blocks.length - 1) + '\u0000';
    }
    // 1) 成对的围栏（四个以上反引号也要能包住三个反引号：AI 讲 Markdown
    //    语法时就会这么写）。先处理围栏更长的，避免被短的抢先配对。
    src = src.replace(/(`{4,})([a-zA-Z0-9_+-]*)\n?([\s\S]*?)\1/g, function (_m, _f, lang, code) {
      return pushBlock(lang, code);
    });
    // 2) 普通的三个反引号围栏。闭合标记必须处在**行首**（允许前导空白）：
    //    不要求行首会怎样（实测）：代码里出现 `const s = "```"` 时，正文中
    //    任意位置的三个反引号都会被当闭合标记，代码块被截断、后面的正文
    //    被吞进 <pre>。
    src = src.replace(/```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*?)(?:^[ \t]*```[ \t]*$|\n```)/gm,
      function (_m, lang, code) {
        return pushBlock(lang, code);
      });
    // 3) 未闭合的围栏：流式输出到一半、或回答被截断/停止时必然出现。
    //    不处理会怎样（实测）：整段代码降级成普通段落，里面的 `**`、`#`
    //    被当 Markdown 解析 → 标记字符被吃掉、代码无高亮，而且这是**流式
    //    过程中每一轮都会经过的状态**。
    //    顺序要求：先长的后短的。只写三条反引号那条会怎样（实测）：
    //    流式中的 ````markdown\n```js 会被 "```" + 语言标签 "markdown" 消化掉，
    //    首个反引号留在正文里渲染成一个只含 ` 的 <p>，还多出一个空段落。
    //    （AI 讲解「怎么嵌套代码块」时就会输出长围栏，这不是罕见形态。）
    src = src.replace(/(`{4,})([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*)$/, function (_m, _f, lang, code) {
      return pushBlock(lang, code);
    });
    src = src.replace(/```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*)$/, function (_m, lang, code) {
      return pushBlock(lang, code);
    });
    src = esc(src);
    src = src.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    src = src.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    // 链接：挡掉可执行协议，其余放行。
    // 不能用「白名单 https/mailto」——AI 常给相对路径（doc.md、../x.md、C:\x），
    // 白名单会把它们整段吞成纯文本，用户看到的链接就没了。
    // href 里要允许成对括号：维基类链接 `.../Foo_(bar)` 很常见，
    // 用 `[^)\s]+` 会在第一个 `)` 处截断，剩下的 `)` 泄漏成正文（实测）。
    src = src.replace(/\[([^\]\n]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/g, function (_m, label, href) {
      var h = href.replace(/&amp;/g, '&');
      if (/^\s*(javascript|data|vbscript):/i.test(h)) return label;
      return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    var t = extractTables(src);
    src = t.text;
    var out = renderBlocks(src);
    out = out.replace(/\u0000BLOCK(\d+)\u0000/g, function (_m, i) {
      return blocks[parseInt(i, 10)] || '';
    });
    return out.replace(/\u0000TABLE(\d+)\u0000/g, function (_m, i) {
      return t.tables[parseInt(i, 10)] || '';
    });
  }

  /**
   * 是否「跟随底部」。用户往上翻看历史时必须为 false——
   * 否则来一段流式正文就把他弹回底部，读到一半被拽走（实测上翻 500px
   * 后离底归 0）。用一个「离底多少」的阈值而不是严格相等：贴底时
   * 偶尔差几像素也算贴底。
   */
  var followBottom = true;
  /**
   * 重新判断「用户是不是贴在底部」。
   *
   * 难点：程序自己滚到底也会触发 scroll 事件，而滚动是延到帧末做的
   * （见 scrollBottom），这中间内容可能又长高了一大截。此时算出来的
   * 差值 = scrollHeight − scrollTop − clientHeight 会很大，被误判成
   * 「用户上翻了」→ followBottom 变 false，此后回答再多也不跟，
   * 界面就停在中途（实测 4 轮里 1 轮中途永久失跟：48px → 1283px）。
   *
   * 用 progTarget 区分：只有「当前 scrollTop 正好等于我上一次设的值」才认为
   * 是程序滚动，跳过判断；用户自己滚到别处就照常算，上翻依然能停跟。
   */
  var progTarget = -1;
  var lastProgHeight = 0;
  /**
   * 重新判断「用户是不是贴在底部」。
   *
   * 难点：程序自己滚到底也会触发 scroll 事件，而滚动是延到帧末做的
   * （见 scrollBottom），这中间内容可能又长高了一大截。此时算出来的
   * 差值 = scrollHeight − scrollTop − clientHeight 会很大，被误判成
   * 「用户上翻了」→ followBottom 变 false，此后回答再多也不跟，
   * 界面就停在中途（实测：一帧涌入 30 片积压事件时离底 316~1124px）。
   *
   * 区分办法看两个量：
   *   · 内容长高了（scrollHeight 变大）→ scrollTop 相对落后是必然的，
   *     这是程序滚动的后果，不算用户操作
   *   · 内容没变而 scrollTop 变小 → 才是用户真的往上拖
   * 不用时间窗：时间窗会把「程序刚滚完、用户立刻上翻」也一并吞掉（实测 432px）。
   */
  function updateFollow() {
    var grew = convo.scrollHeight > lastProgHeight + 4;
    if (progTarget >= 0 && (Math.abs(convo.scrollTop - progTarget) < 4 || grew)) {
      if (!grew) progTarget = -1;
      return;
    }
    progTarget = -1;
    followBottom = (convo.scrollHeight - convo.scrollTop - convo.clientHeight) < 48;
    syncToBottomBtn();
  }
  convo.addEventListener('scroll', updateFollow);

  /**
   * 同步「回到底部」按钮：用户上翻时才出现。
   * 为什么要有：上翻后跟随会停（这是对的），但界面上原来没有任何回头路——
   * 249px 侧边栏里一轮 200 行输出要往下滚十几屏，用户只能一路拖回去。
   * Cline / Roo / Claude Code / Copilot Chat 都有这个按钮。
   */
  function syncToBottomBtn(){
    var btn = document.getElementById('toBottom');
    if (!btn) return;
    btn.setAttribute('data-on', followBottom ? 'false' : 'true');
    if (followBottom) {
      btn.setAttribute('data-new', '0');
      btn.textContent = '↓ 回到底部';
    }
  }
  /** 用户上翻期间又来了新内容 → 按钮变成「新内容」并高亮，让他知道下面还有 */
  function markNewBelow(){
    if (followBottom) return;
    var btn = document.getElementById('toBottom');
    if (!btn) return;
    btn.setAttribute('data-on', 'true');
    if (btn.getAttribute('data-new') !== '1') {
      btn.setAttribute('data-new', '1');
      btn.textContent = '↓ 新内容';
    }
  }
  (function(){
    var tb = document.getElementById('toBottom');
    if (!tb) return;
    tb.addEventListener('click', function(){
      followBottom = true;
      syncToBottomBtn();
      scrollBottom(true);
    });
  })();

  /**
   * 滚到底（跟随时才滚）。
   *
   * 用「帧末只滚一次」而不是每次都直接写：读 `convo.scrollHeight` 会强制
   * 同步布局，而这个函数在一轮里被调用几十次（每个系统行、工具行、流片段）。
   * 文档越长重排越贵：实测同一会话累积到 2400 轮时，每轮耗时从 5.5ms 涨到
   * 104.6ms，而清空后立刻回到 2.9ms —— 变慢的只有这一次次强制布局。
   * 合并到下一次重绘前统一滚，副作用是「同一帧内多次滚动合并成一次」，
   * 用户看到的仍是滚到底。
   * @param {boolean} force 无视 followBottom（用户主动要求，如点确认条）
   */
  var scrollPending = { queued: false, force: false };
  // 无 rAF 时退回 setTimeout（Node 侧跑测试台没有 requestAnimationFrame）
  var raf = (typeof requestAnimationFrame === 'function')
    ? requestAnimationFrame
    : function(fn){ return setTimeout(fn, 0); };
  function scrollBottom(force) {
    if (!force && !followBottom) {
      // 用户正在上翻：这一波新内容不拽他回去，但要让「↓ 新内容」亮起来
      markNewBelow();
      return;
    }
    if (force) scrollPending.force = true;
    if (scrollPending.queued) return;
    scrollPending.queued = true;
    raf(function(){
      scrollPending.queued = false;
      var f = scrollPending.force;
      scrollPending.force = false;
      if (!f && !followBottom) return;
      convo.scrollTop = convo.scrollHeight;
      // 记下我设进去的位置与当时的内容高度，供 updateFollow 区分
      // 「程序滚动」与「用户上翻」
      progTarget = convo.scrollTop;
      lastProgHeight = convo.scrollHeight;
    });
  }

  /** 当前助手消息的正文（取**最后**一条）。
   *
   * 早先用 querySelector('.assistant') 取第一条：当时每次 initialize 都清空
   * 对话区，只剩一条恰好正确。现在重发 initialize 不清空了（重连不丢对话），
   * 历史里会有多条 .assistant，取第一条就永远返回第一轮的正文——
   * turn/end 的渲染回执因此失真，测试断言也跟着失效。
   */
  function assistantText() {
    var els = convo.querySelectorAll('.assistant');
    var el = els.length ? els[els.length - 1] : null;
    return el ? el.textContent : '';
  }

  /**
   * 清空对话区，并复位所有「跟着 DOM 一起活」的索引。
   *
   * 只写 convo.innerHTML='' 不够：toolMap / sysMap / pendingTodoRow 持有
   * 已经离开 DOM 的节点——整棵工具行子树（含尚未展开的完整输出）一直挂在
   * 内存里，切几次会话就积一份（实测）。原先 5 处清空点各写一遍，必然漏
   * （toolMap 就漏了），所以收成一个函数。
   */
  function resetConversationDom() {
    convo.innerHTML = '';
    toolMap = {};
    sysMap = {};
    pendingTodoRow = null;
    // 本轮折叠的边界也要复位：不清会怎样 —— 切到新会话后 turnStartNode
    // 还指向上一个会话的节点（已脱离文档），轮末折叠时 insertBefore
    // 会抛错或把内容插到错误位置。
    turnStartNode = null;
    turnStartTime = 0;
    turnToolCount = 0;
    turnThinkCount = 0;
    curAssistant = null;
    curThink = null;
    assistantBuf = '';
    resetStreamRender();
    // 跟随状态必须复位，而且必须在清空之后立刻复位。
    // 不清会怎样（实测）：上个会话已经翻到最顶端（scrollTop=0），清空后仍是 0、
    // 不产生 scroll 事件 → followBottom 永久停在 false，此后这个会话整场都不再
    // 自动跟随（离底 2554px）。就算 scrollTop>0 被清成 0 产生了一条事件，也会被
    // progTarget 守卫判成「自己滚的」而跳过计算——唯一能纠正它的路径被自己吃掉。
    followBottom = true;
    progTarget = -1;
    syncToBottomBtn();
    // offlineLine 是刚被删掉的那个 DOM 节点的引用。不清掉会怎样（实测）：
    // 之后再掉线时 showOffline 发现 offlineShown 还是 true，于是去 updateSysLine
    // 一个已经脱离文档的节点——更新的是空气，界面上永远不再出现离线提示。
    offlineShown = false;
    offlineLine = null;
    // historyDrawn 也必须复位：它表示「对话区已画过几条后端历史」。
    // 一旦对话区被清空（清屏/切会话/新建），这个前提就不成立了——
    // 不复位的话下一次 initialize 明明带着历史也不会再画，用户只能看到空白
    //（实测：/clear 之后重载窗口，历史永远回不来）。
    historyDrawn = 0;
    historyDrawnFor = '';
    pendingSwitchTo = '';
    lastSentText = '';
    lastOfflineSend = { text: '', ts: 0 };
    lastSentMaybeAccepted = false;
    // 这两个行节点跟着 convo 一起被删了：不清引用会指向已脱离文档的节点
    apiKeyHintLine = null;
    cfgWarnLines = [];
    // pendingConfirmLine 指向的是刚被删掉的那个系统行。不清掉会怎样：
    // 下一次 confirm/result 到达时 applyConfirmResult 会去改一个已脱离文档的
    // 节点——改的是空气，用户看不到「已确认/已失效」的最终结论。
    pendingConfirmLine = null;
    // sessListErrLine 同理（第 25 轮新增的「读取会话列表失败」行）。
    // 它不挂在 #convo 里，但引用同一批被清掉的节点；不清会留下悬空引用，
    // 而且与上面几个字段的处理不一致 —— 这里按同一模式统一复位。
    // SESS_LIST_ERROR 不在这里清：它描述的是「宿主上一次读列表成没成功」，
    // 与对话区清屏无关；清掉会让空态文案在下一次 session/list 到达前说错话。
    sessListErrLine = null;
  }

  /** 渲染回执：扩展侧据此判断 webview 画到了哪一步 */
  function rendered(what, extra) {
    var payload = { what: what };
    if (extra) {
      Object.keys(extra).forEach(function (k) { payload[k] = extra[k]; });
    }
    RPC_STATE.lastRender = payload;
    RPC_STATE.renders.push(payload);
    if (RPC_STATE.renders.length > 50) RPC_STATE.renders.shift();
    post('rendered', payload);
  }

  /**
   * 渲染回执里的正文摘要。
   *
   * 回执只用于诊断与测试断言，不需要全文。留全文会怎样（实测）：这条数据会进
   * RPC_STATE.renders（保留最近 50 条）并经 postMessage 序列化一份——10MB 的
   * 一条回答 × 若干轮，GC 之后仍常驻 1.2GB。切成 2000 字与 stream/delta 的回执
   * 同一口径，足够断言「渲染到哪了」。
   */
  function briefAssistant() {
    return assistantText().slice(0, 2000);
  }

  /**
   * 命令清单（面板补全用）。必须与后端 _STATE_COMMANDS 逐字对齐——
   * 这里多写一个不存在的命令，用户选中后会被当成普通对话发给 AI；
   * 少写一个则补全不到。有测试守着这份对齐。
   *
   * s    = 参数提示（只用于显示，空串代表无参数）
   * req  = 参数是否必需。决定「打全命令名后按 Enter」是执行还是先补全：
   *        必需参数的 /cd 直接执行只会报「请指定会话名称」，先补全更顺；
   *        而 /ls、/mode、/thinking 不填参数是「查询当前状态」，必须能直接执行。
   * g    = 分组标题
   */
  /** 探索分支状态下后端没有的命令（与 _STATE_COMMANDS['child'] 对齐）：
      面板不该列出必然被拒的项 */
  var CHILD_BLOCKED = { '/save': 1, '/rm': 1, '/explore': 1 };

  var COMMANDS = [
    { c: '/clear', d: '清理屏幕', s: '', g: '会话' },
    { c: '/save', d: '保存当前会话', s: '可选名称', g: '会话' },
    { c: '/ls', d: '显示所有会话', s: '--all', g: '会话' },
    { c: '/cd', d: '进入历史会话', s: '名称', g: '会话', req: true },
    { c: '/rm', d: '删除会话', s: '名称', g: '会话', req: true },
    { c: '/explore', d: '创建探索分支', s: '名称', g: '会话', req: true },
    { c: '/done', d: '完成探索分支并合并结论', s: '', g: '会话' },
    { c: '/exit', d: '退出当前会话（回到未保存）', s: '', g: '会话' },
    { c: '/compact', d: '压缩上下文', s: '', g: '上下文' },
    { c: '/skill', d: '加载技能', s: '名称', g: '上下文', req: true },
    { c: '/mode', d: '切换模型', s: '模型名', g: '模型与行为' },
    { c: '/thinking', d: '切换思考强度', s: '档位', g: '模型与行为' },
    { c: '/thinkback', d: '思考回传开关', s: 'on / off', g: '模型与行为' },
    { c: '/goal', d: '目标模式开关', s: 'on / off', g: '模型与行为' },
    { c: '/plugin', d: '插件工具开关', s: '名称 on/off', g: '模型与行为' }
  ];

  /** 当前可见的命令（输入 /xxx 后过滤）与选中下标 */
  var palItems = [];
  var palSel = 0;
  /** 当前是否在探索分支里。由后端 isChild 决定（名字里的 / 不代表分支）。 */
  var CUR_IS_CHILD = false;
  /** 'cmd'=正在补命令名，'arg'=正在补参数（此时 palCmd 记住是哪个命令） */
  var palMode = 'cmd';
  var palCmd = '';

  /**
   * 哪些命令的参数可以就地补全，以及从哪里取候选。
   * 候选取自界面上已有的真实数据（会话列表、模型/强度/插件下拉），
   * 不另建一份——否则后端一加模型，这里就过期了。
   */
  var ARG_SOURCE = {
    '/cd': 'session',
    '/rm': 'session',
    '/mode': 'model',
    '/thinking': 'think',
    '/plugin': 'plugin'
  };

  function listArgCandidates(src){
    if (src === 'session') {
      // 子会话也要能进：列表为了不混淆已扁平化成只显示根会话，
      // 若候选里也只有根会话，抽屉里就再也没有入口能进子会话了。
      // 用「父/子」形式给出（后端 resolve_session_name 认这个写法）。
      var out = [];
      SESS.forEach(function(s){
        out.push({ v: s.name, d: '会话' });
        (s.children || []).forEach(function(c){
          out.push({ v: s.name + '/' + c.name, d: '子会话' });
        });
      });
      return out;
    }
    var popId = { model: 'popModel', think: 'popThink', plugin: 'popPlugin' }[src];
    var pop = popId && document.getElementById(popId);
    if (!pop) return [];
    return Array.prototype.map.call(pop.querySelectorAll('.pi'), function(pi){
      var lb = pi.querySelector('.lb');
      return { v: pi.getAttribute('data-v'), d: lb ? lb.textContent : '' };
    }).filter(function(x){ return x.v; });
  }

  function openPalette(open){
    palette.setAttribute('data-open', open ? 'true' : 'false');
    if (!open) return;
    // 命令面板与状态行「⋯」抽屉都是从 .composer 向上弹的浮层，同时开着会
    // 上下叠在一起、互相遮挡（实测两浮层几何相交 63px）。面板是当前操作焦点，
    // 所以打开它时把状态行抽屉收起来。setStDrawer 定义在后面（函数声明会提升）。
    if (typeof stDrawerOpen === 'function' && stDrawerOpen()) setStDrawer(false);
    // 同样要收起「下拉 / 右键菜单」：它们与面板候选行共用输入框上方那条空间带。
    // 不收起会怎样（实测）：面板与下拉同屏，被遮住的候选只露上半截，
    // 点下去**穿透到下层语义完全不同的控件**（想选命令、实际命中了改名/删除）。
    // 与 showPop 里反向那一条成对，只修一侧等于没修。
    if (typeof closePop === 'function') closePop();
    var ctxM2 = document.getElementById('sessMenu');
    if (ctxM2 && ctxM2.getAttribute('data-open') === 'true') {
      ctxM2.setAttribute('data-open', 'false');
    }
    fitPalette();
  }

  /**
   * 按输入框上方的可用空间限制面板高度。
   * 面板是 absolute + bottom:100% 向上弹，只写死 max-height:300px 时，
   * 窗口矮（侧边栏被拖矮到 320px 高）会让面板顶部越出屏幕上边界，
   * 前几条命令看不到也滚不回来（scrollTop 不能为负）。
   */
  function fitPalette(){
    var cbox = palette.parentElement;   // .cbox
    if (!cbox) return;
    var top = cbox.getBoundingClientRect().top;
    var avail = top - 12;               // 留 12px 呼吸
    if (avail < 80) avail = 80;         // 太小就不限了，至少能用键盘
    palette.style.maxHeight = Math.min(300, avail) + 'px';
  }

  function paletteVisible(){
    return palette.getAttribute('data-open') === 'true';
  }

  /**
   * 按输入框内容重算候选列表。
   *   /cd          → 补命令名
   *   /cd stu      → 补该命令的参数（这里是会话名）
   * 命令名补全只在「还没打空格」时进行；打了空格即视为在填参数，
   * 若该命令没有可补的参数就收起面板，免得挡住内容。
   */
  function refreshPalette(){
    var firstLine = input.value.split('\n')[0];
    if (firstLine.charAt(0) !== '/') {
      palItems = [];
      openPalette(false);
      return;
    }
    var m = /^\/([a-z]*)(\s+([\s\S]*))?$/.exec(firstLine);
    if (!m) {
      palItems = [];
      openPalette(false);
      return;
    }
    var name = '/' + (m[1] || '');

    if (m[2] === undefined) {
      // 还在打命令名
      palMode = 'cmd';
      palCmd = '';
      var q = m[1].toLowerCase();
      palItems = COMMANDS.filter(function(x){
        // 探索分支里 /save /rm /explore 一定会被后端拒（_STATE_COMMANDS['child']
        // 里没有它们）。面板照旧列出来会怎样（实测）：用户选中执行，得到
        // 「/rm 在当前状态下不可用」——列表承诺了不存在的能力。
        if (CUR_IS_CHILD && CHILD_BLOCKED[x.c]) return false;
        return !q || x.c.slice(1).toLowerCase().indexOf(q) === 0;
      });
    } else {
      // 已在填参数
      var src = ARG_SOURCE[name];
      if (!src) {
        palItems = [];
        openPalette(false);
        return;
      }
      palMode = 'arg';
      palCmd = name;
      var arg = (m[3] || '').toLowerCase();
      palItems = listArgCandidates(src)
        .filter(function(x){
          if (!arg) return true;
          var v = x.v.toLowerCase();
          if (v.indexOf(arg) === 0) return true;
          // 「父/子」形式也允许直接用子会话名开头找：
          // 只按整串前缀匹配的话，想进子会话就必须先准确打出父会话名，
          // 用户记住的多半是子会话名（浏览器书签式的心智）。
          var cut = v.indexOf('/');
          return cut >= 0 && v.slice(cut + 1).indexOf(arg) === 0;
        })
        .map(function(x){ return { c: x.v, d: x.d, s: '', g: '可进入的' + x.d }; });
    }

    if (!palItems.length) {
      openPalette(false);
      return;
    }
    if (palSel >= palItems.length) palSel = 0;
    renderPalette();
    openPalette(true);
  }

  function renderPalette(){
    var html = '';
    var lastGroup = null;
    palItems.forEach(function(it, i){
      if (it.g !== lastGroup) {
        lastGroup = it.g;
        html += '<div class="grp">' + esc(it.g) + '</div>';
      }
      html += '<div class="item" data-i="' + i + '"' +
        (i === palSel ? ' data-sel="true"' : '') + '>' +
        '<span class="c">' + esc(it.c) + '</span>' +
        '<span class="d">' + esc(it.d) + '</span>' +
        (it.s ? '<span class="s">' + esc(it.s) + '</span>' : '') +
        '</div>';
    });
    palette.innerHTML = html;
  }

  /**
   * 让选中项滚进可视区。
   * 面板限高 300px，命令有 15 条——不跟滚动时，按↓选到第 8 条以后
   * 选中项就在框外，用户看不到自己选了什么。
   * 用 nearest：只在与边界相交时滚动，不会每次都把列表顶到最上面。
   */
  function scrollPalSelIntoView(){
    var el = palette.querySelector('.item[data-sel="true"]');
    if (!el) return;
    var pr = palette.getBoundingClientRect();
    var er = el.getBoundingClientRect();
    if (er.top < pr.top) {
      palette.scrollTop -= (pr.top - er.top);
    } else if (er.bottom > pr.bottom) {
      palette.scrollTop += (er.bottom - pr.bottom);
    }
  }

  /** 循环到首项时把列表滚回顶部：否则从末尾跳回第 1 条，选中项在框外看不见 */
  function resetPaletteScrollIfFirst(){
    if (palSel === 0) palette.scrollTop = 0;
  }

  /** 把选中项补全进输入框（末尾留个空格方便接着打参数） */
  function completePalette(){
    var it = palItems[palSel];
    if (!it) return false;
    var wasArg = palMode === 'arg';
    var lines = input.value.split('\n');
    if (wasArg) {
      // 参数补全：替换第一行的「命令 + 参数」，保留其余行
      // 同样补一个尾随空格：补完会话名后接着打字的场景很常见，
      // 不留空格会拼成 `/cd grep-ignore-caseZ` —— 一个不存在的会话名，
      // 用户只能自己发现少了个空格（实测）。
      lines[0] = palCmd + ' ' + it.c + ' ';
      input.value = lines.join('\n');
    } else {
      lines[0] = it.c;
      input.value = lines.join('\n') + ' ';
    }
    openPalette(false);
    autoGrowInput();
    input.focus();
    // 补完命令名且该命令有参数可补 → 立刻接着弹参数列表，
    // 否则用户还得自己再敲一个字符才看到候选。
    if (!wasArg && ARG_SOURCE[it.c]) {
      palSel = 0;
      refreshPalette();
    }
    return true;
  }

  var sessBtnEl = document.getElementById('sessBtn');

  /**
   * Esc 的全局兜底：关掉挡在眼前的浮层，按最上层收（命令候选 → 下拉 → 抽屉）。
   *
   * 为什么不放在输入框的 keydown 里：那个处理器只在 #input 有焦点时才跑。
   * 焦点在状态栏按钮上时（用户刚点开模型下拉）按 Esc 完全没反应，
   * 键盘用户会以为界面卡住了（实测）。这里挂到 document 上，
   * 任何焦点位置都能关。确认条不参与：它代表后端在等 y/n，关掉等于永久不回话。
   */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    // 焦点在主输入框时交给它自己的处理器：那里已经有一套「按最上层逐层关」的
    // 顺序（候选 → 下拉 → 抽屉），在这儿再抢一层会打乱它。
    // 弹层内的输入框（#goalRounds）不在它管辖范围，仍走这里。
    if (document.activeElement === input) return;
    if (e.isComposing || e.keyCode === 229) return;
    if (openPop) {
      // 弹层里可能有输入框（#goalRounds）带着焦点；弹层一关，那个节点就被隐藏，
      // 焦点掉回 <body>——下一个 Tab 从面板顶部重来（实测）。交还输入框。
      var inPop = e.target && openPop.contains(e.target);
      closePop();
      if (inPop) { try { input.focus(); } catch (err) { /* 忽略 */ } }
      e.preventDefault();
      return;
    }
    // 右键菜单是最上层：先关它，再依次收下拉/抽屉/候选面板。
    // 不放在最前面会怎样：菜单开着按 Esc，抽屉先被收掉，菜单还浮在屏幕上。
    var ctxMenu = document.getElementById('sessMenu');
    if (ctxMenu && ctxMenu.getAttribute('data-open') === 'true') {
      ctxMenu.setAttribute('data-open', 'false');
      e.preventDefault();
      return;
    }
    if (drawer.getAttribute('data-open') === 'true') { setDrawer(false); e.preventDefault(); return; }
    if (palette.getAttribute('data-open') === 'true') { openPalette(false); e.preventDefault(); return; }
  }, true);
  function setDrawer(open){
    // 关抽屉时把焦点交还出去。不交还会怎样（实测）：焦点在 #dwClose 或行内
    // 「是/否」上按 Esc 收起抽屉，节点被隐藏 → 焦点掉回 <body>，
    // 键盘用户后续 Tab 只能从页面顶端重来（抽屉里一共 11 个停靠点，全丢）。
    // 弹层那条路径早就修过同类问题（见上面 openPop 的注释），抽屉这两处漏了。
    var focusWasInDrawer = open ? false :
      !!(drawer && drawer.contains(document.activeElement));
    drawer.setAttribute('data-open', open ? 'true' : 'false');
    // 给 body 挂标记：#toBottom 会压住抽屉 footer 的按钮，CSS 用它让路
    //（见 #toBottom 那段 body[data-drawer] 注释）。
    document.body.setAttribute('data-drawer', open ? 'true' : 'false');
    // 这里**不要**再写 #sessBtn 的 data-open。
    // 历史原因：早先顶栏会话名胶囊就是抽屉的入口，所以 setDrawer 顺手同步它的
    // 高亮与箭头。后来胶囊改成「打开子会话下拉」、抽屉入口改到输入行的历史按钮，
    // 这行就成了**同属性双写**（showPop/closePop 也写它）：
    //   · 打开抽屉 → 胶囊高亮 + 箭头翻转，但下拉并没开
    //   · 再点胶囊开下拉、再点收起 → closePop 把 data-open 设 false，
    //     而抽屉还开着 → 胶囊指示与实际相反（实测 A8-01）
    // 胶囊的状态只由它自己的下拉决定，别的地方一律不碰。
    if (open && REAL) post('sessionList', { all: false });
    // 列表与设置是互斥的两个状态，同屏会互相盖住（都铺满内容区）。
    // 两个方向都要关：这里管「开列表 → 关设置」，setPanelOpen 管反向。
    if (open) {
      var sp = document.getElementById('settingsPanel');
      if (sp && sp.getAttribute('data-open') === 'true') {
        if (typeof setPanelOpen === 'function') setPanelOpen(false);
      }
    }
    // 顶栏按钮跟着变成「当前态」，让用户知道再点一下就是关掉
    var histBtnEl = document.getElementById('histBtn');
    if (histBtnEl) {
      histBtnEl.setAttribute('data-on', open ? 'true' : 'false');
    }
    // 打开抽屉时把状态详情浮层收起（与 setStDrawer 里反向那一条成对）。
    // 249px 下两者都是覆盖全宽的浮层，状态抽屉在上层会把抽屉 footer 的按钮
    // 整片吞掉；反过来先开状态抽屉再点历史按钮，同样会叠在一起。
    // 只修一侧的话，另一侧的入口顺序仍然能撞出「按钮点不到」（实测）。
    if (open && typeof stDrawerOpen === 'function' && stDrawerOpen()) {
      setStDrawer(false);
    }
    // 关掉抽屉时清掉「手动收起」记忆：下次打开时仍希望一眼看到我在哪条分支里。
    // 打开时不清（清了下一次 renderSessions 就会把用户刚收起的又展开）。
    if (!open) {
      MANUAL_COLLAPSED = {};
    } else if (typeof renderSessions === 'function') {
      // 打开时重算一次：清掉手动收起记忆后，「我在子会话里就展开父会话」这条
      // 才能生效。不重算会怎样（实测）：上次手动收起过，关掉再打开，
      // 抽屉里仍然看不到自己在哪条分支（记忆清了但列表没重画）。
      renderSessions();
    }
    // 焦点交还：交给打开抽屉的那个历史按钮（用户按 Tab 走几格就能回到列表），
    // 找不到就退回输入框 —— 两者都比掉到 <body> 好。
    if (focusWasInDrawer) {
      var backTo = document.getElementById('histBtn') || input;
      if (backTo && typeof backTo.focus === 'function') {
        try { backTo.focus(); } catch (e) { /* 忽略 */ }
      }
    }
  }
  /* 249px 下抽屉与对话区完全重合，屏幕上没有「外面」可点：点顶栏空白、
     点输入行、点状态栏都不收起它（实测 6 个位置全不动）。用户的下一个动作
     往往是「算了，直接打字」，而输入框在抽屉下沿之外——点它就该收起抽屉，
     否则消息发出去后回答被抽屉盖住，用户以为没反应。 */
  (function () {
    var inp = document.getElementById('input');
    if (!inp) return;
    var closeOnFocus = function () {
      if (drawer.getAttribute('data-open') === 'true') setDrawer(false);
    };
    inp.addEventListener('focus', closeOnFocus);
    inp.addEventListener('pointerdown', closeOnFocus);
  })();

  /* 顶栏会话名：点了开「子会话」下拉，不再开会话抽屉（抽屉入口在输入行的历史按钮）。
     未保存（游离态）时没有子会话，但**下拉照样要开**：里面写「还未保存」，
     否则按钮点下去毫无反应、箭头也不翻，用户只会以为它坏了。 */
  sessBtnEl.addEventListener('click', function(e){
    e.stopPropagation();
    renderChildPop();
    showPop(sessBtnEl, 'popChild');
  });

  // 命令面板「Narnat: 切换调度面板」→ 宿主 postMessage {type:'toggleInspector'}。
  // 界面上唯一对应「面板」的就是这个会话抽屉，所以这里管它的开合。
  // 不接会怎样（实测）：命令面板里那一条永久点了没反应，用户不知道是插件坏了
  // 还是自己点错——38 个 case 里没有一个认这个消息。
  function toggleDrawer(){
    setDrawer(drawer.getAttribute('data-open') !== 'true');
  }

  document.getElementById('dwClose').addEventListener('click', function(){
    setDrawer(false);
  });

  /* ---------- 设置面板 ----------
     两个入口：顶栏最右的 ⚙（主入口）、对话记录标题栏那个小 ⚙（顺手可达）。
     两者都只做「打开面板」，不再像旧版那样直接去宿主里打开配置文件 ——
     配置该在这里改，不该让用户去手编 JSON。

     数据全部来自后端 config/describe + narnatmd/get，界面**不自己保存
     任何配置副本**。这样设置面板与主界面（状态栏那排按钮）永远是同一份真值：
     在这里改完，关掉面板，主界面的模型按钮会自动跟上（它收 config/result）。 */

  /** 最近一次 config/describe 的结果；null 表示还没拉到 */
  var setItems = null;
  /** describe 里的分组定义（顺序即分段顺序） */
  var setGroups = [];
  /** 当前选中的分段 id（默认第一段 = 模型 API） */
  var setGroup = 'api';
  /** narnat.md 原文（与后端的差异即"有未保存修改"） */
  var mdText = '';
  var mdSaved = '';

  function setPanelOpen(on){
    var p = document.getElementById('settingsPanel');
    if (!p) return;
    p.setAttribute('data-open', on ? 'true' : 'false');
    document.body.setAttribute('data-settings', on ? 'true' : 'false');
    var btn = document.getElementById('btnSettings');
    if (btn) btn.setAttribute('data-on', on ? 'true' : 'false');
    if (on) {
      // 打开时把列表收起（两者都是铺满内容区的状态，同屏会互相盖住）
      if (typeof setDrawer === 'function') setDrawer(false);
      loadDescribe();
    } else {
      // 关面板时必须把全屏层一起关掉。不关会怎样：用户在全屏改完按 ×
      // 关面板，全屏层留在 data-open=true —— 下次打开设置直接是全屏，
      // 而且看不到分段和字段区，像界面坏了。
      if (typeof setMdFull === 'function') setMdFull(false);
    }
  }

  /** 拉一次配置全貌并重画。每次打开都拉 —— 后端可能被别的途径改过。 */
  function loadDescribe(){
    var body = document.getElementById('setBody');
    if (body) body.innerHTML = '<div class="set-msg">正在读取配置…</div>';
    post('configDescribe', {});
  }

  function setMsg(text, kind){
    var foot = document.getElementById('setFoot');
    if (!foot) return;
    var old = foot.parentNode.querySelector('.set-msg');
    if (old) old.parentNode.removeChild(old);
    if (!text) return;
    var d = document.createElement('div');
    d.className = 'set-msg';
    if (kind) d.setAttribute('data-kind', kind);
    d.textContent = text;
    foot.parentNode.insertBefore(d, foot);
  }

  /** 画分段按钮 */
  function renderTabs(){
    var tabs = document.getElementById('setTabs');
    if (!tabs) return;
    tabs.innerHTML = '';
    setGroups.forEach(function(g){
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = g.label || g.id;
      b.setAttribute('data-on', g.id === setGroup ? 'true' : 'false');
      b.addEventListener('click', function(){
        setGroup = g.id;
        renderTabs();
        /* 必须重画底栏。不画会怎样（实测）：从「模型 API」切到「提示词」后，
           底部还是 list 态那组「重新读取 / 打开配置文件」按钮，**保存按钮根本
           不存在** —— 用户改了 narnat.md 却没有任何地方能保存，只能去全屏层
           里存，而他又不知道全屏层有独立的保存按钮。 */
        renderFoot();
        renderBody();
      });
      tabs.appendChild(b);
    });
  }

  /** 按当前分组画字段 */
  function renderBody(){
    var body = document.getElementById('setBody');
    if (!body) return;
    body.innerHTML = '';
    /* 指令分组用专门的布局：字段区不滚动、textarea 吃掉全部剩余高度。
       其它分组是普通列表（可滚动）。用 data-mode 切换而不是加两个类，
       因为 CSS 那边要按「这一个 row 撑满」来写，判断依据得留在 DOM 上。 */
    body.setAttribute('data-mode', setGroup === 'prompt' ? 'md' : 'list');
    if (!setItems) {
      body.innerHTML = '<div class="set-msg">配置还没读到（后端未连接？）</div>';
      return;
    }
    var rows = setItems.filter(function(it){ return it.group === setGroup; });
    // 条件字段：showFor 列出的模式才显示。判据取同分组里 provider 那项的值 ——
    // 选 deepseek 时 MCP 的「服务器地址」对它毫无意义，摆在界面上只会让人
    // 以为必须填（实测这种"看起来必填但实际无关"的字段最容易劝退）。
    if (setGroup === 'websearch') {
      var prov = (setItems.filter(function(x){ return x.id === 'websearch.provider'; })[0] || {}).value || 'mcp';
      rows = rows.filter(function(it){
        return !it.showFor || it.showFor.indexOf(prov) >= 0;
      });
    }
    if (!rows.length) {
      body.innerHTML = '<div class="set-msg">这一组没有可配置项</div>';
      return;
    }
    rows.forEach(function(it){ body.appendChild(renderRow(it)); });
    // narnat.md 的正文要额外拉一次（describe 只给元信息）
    if (setGroup === 'prompt') loadMarkdown();
  }

  function renderRow(it){
    var row = document.createElement('div');
    row.className = 'set-row';
    row.setAttribute('data-id', it.id);

    var lab = document.createElement('div');
    lab.className = 'set-lab';
    var name = document.createElement('span');
    name.textContent = it.label || it.id;
    lab.appendChild(name);
    // 当前值贴右显示：用户一眼看到"现在是啥"，不用点开下拉
    var cur = document.createElement('span');
    cur.className = 'set-cur';
    if (it.kind === 'bool') cur.textContent = it.value ? '开' : '关';
    else if (it.kind !== 'number' && it.kind !== 'markdown') cur.textContent = String(it.value == null ? '' : it.value);
    else if (it.kind === 'number') cur.textContent = String(it.value == null ? '' : it.value);
    lab.appendChild(cur);
    row.appendChild(lab);

    if (it.kind === 'select') {
      var sel = document.createElement('select');
      sel.className = 'set-sel';
      sel.disabled = it.writable === false;
      (it.options || []).forEach(function(o){
        var op = document.createElement('option');
        op.value = o.value;
        op.textContent = o.label || o.value;
        if (o.value === it.value) op.selected = true;
        sel.appendChild(op);
      });
      sel.addEventListener('change', function(){ submitConfig(it, sel.value); });
      row.appendChild(sel);
    } else if (it.kind === 'bool') {
      var sw = document.createElement('button');
      sw.type = 'button';
      sw.className = 'set-sw';
      sw.textContent = it.value ? '已开启' : '已关闭';
      sw.setAttribute('data-on', it.value ? 'true' : 'false');
      sw.disabled = it.writable === false;
      sw.addEventListener('click', function(){
        sw.setAttribute('data-busy', 'true');
        submitConfig(it, !it.value);
      });
      row.appendChild(sw);
    } else if (it.kind === 'text') {
      var inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'set-inp';
      inp.value = it.value == null ? '' : String(it.value);
      if (it.placeholder) inp.placeholder = it.placeholder;
      inp.disabled = it.writable === false;
      // 失焦提交：与数值项同理，边打字边提交会把半截地址写进配置
      inp.addEventListener('change', function(){ submitConfig(it, inp.value); });
      row.appendChild(inp);
      // 模型名有候选列表时给一排快捷按钮（点一下直接填），
      // 但保留手输 —— 换厂商后模型名不在列表里，必须能自己写
      if (it.options && it.options.length) {
        var quick = document.createElement('div');
        quick.className = 'set-quick';
        it.options.forEach(function(o){
          var qb = document.createElement('button');
          qb.type = 'button';
          qb.textContent = o.label || o.value;
          qb.setAttribute('data-on', o.value === it.value ? 'true' : 'false');
          qb.addEventListener('click', function(){
            inp.value = o.value;
            submitConfig(it, o.value);
          });
          quick.appendChild(qb);
        });
        row.appendChild(quick);
      }
    } else if (it.kind === 'secret') {
      /* 密钥字段：永远只在界面上显示打码值。
         真值不回传（见后端 _mask_key 的注释），所以这里给的是
         「已设置 + 掩码」，用户要换就整串重填；留空表示不改。 */
      var sk = document.createElement('input');
      sk.type = 'password';
      sk.className = 'set-inp';
      sk.value = '';
      sk.autocomplete = 'off';
      sk.spellcheck = false;
      sk.placeholder = it.placeholder || '未设置';
      sk.disabled = it.writable === false;
      sk.addEventListener('change', function(){
        var v = sk.value.trim();
        // 留空 = 不改。这条必须明说，否则用户以为清空了密钥
        if (!v) { setMsg('', ''); return; }
        submitConfig(it, v);
      });
      row.appendChild(sk);
      // 当前掩码用 .set-cur 而不是 .set-hint：这是**实际信息**（现在是哪个 key），
      // 不是解释性废话。样式上贴在标签右侧，与其它字段的当前值一致。
      var st = document.createElement('div');
      st.className = 'set-hint set-cur-mask';
      st.textContent = it.isSet ? ('当前 ' + (it.masked || '已设置')) : '未设置';
      row.appendChild(st);
    } else if (it.kind === 'number') {
      var num = document.createElement('input');
      num.type = 'number';
      num.className = 'set-num';
      num.value = it.value == null ? '' : String(it.value);
      if (it.min != null) num.min = String(it.min);
      num.disabled = it.writable === false;
      // 失焦提交而不是每次输入都提交：否则用户打「10」时先提交「1」
      num.addEventListener('change', function(){
        var v = parseInt(num.value, 10);
        if (isNaN(v)) { setMsg('请输入数字', 'err'); return; }
        submitConfig(it, v);
      });
      row.appendChild(num);
    } else if (it.kind === 'markdown') {
      /* 工具栏：内容规模 + 复制 + 全屏。
         「复制」是用户明确要的 —— 原来只能手动框选，而内容有几百行时
         框选本身就不可能（要拖动滚几十屏）。「全屏」解决侧边栏太窄看不全。 */
      var bar = document.createElement('div');
      bar.className = 'set-md-bar';
      var lenEl = document.createElement('span');
      lenEl.className = 'set-md-len';
      lenEl.setAttribute('data-act', 'mdLen');
      bar.appendChild(lenEl);

      var copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.textContent = '复制';
      copyBtn.addEventListener('click', function(){
        if (!mdText) return;
        // 走宿主的 clipboard 通道：webview 里 navigator.clipboard 会被沙箱拒绝
        post('clipboard/write', { text: mdText, id: 'md' });
      });
      bar.appendChild(copyBtn);

      var bigBtn = document.createElement('button');
      bigBtn.type = 'button';
      bigBtn.textContent = '全屏';
      // setMdFull 的函数声明在下面，回调触发时必然已赋值（事件在用户点击时才跑），
      // 所以这里直接按名调用即可，不需要提前声明顺序。
      bigBtn.addEventListener('click', function(){
        if (typeof setMdFull === 'function') setMdFull(true);
      });
      bar.appendChild(bigBtn);
      row.appendChild(bar);

      var ta = document.createElement('textarea');
      ta.className = 'set-md';
      ta.id = 'setMd';
      ta.spellcheck = false;
      ta.value = mdText;
      ta.placeholder = '在这里写 narnat.md 的内容（会拼进系统提示词）';
      ta.addEventListener('input', function(){
        mdText = ta.value;
        updateMdFoot();
        updateMdLen();
        // 全屏态下把内容镜像过去（两个 textarea 共享同一份 mdText）
        var big = document.getElementById('setMdBig');
        if (big && big.value !== mdText) big.value = mdText;
      });
      row.appendChild(ta);
    }

    if (it.hint) {
      var hint = document.createElement('div');
      hint.className = 'set-hint';
      hint.textContent = it.hint;
      row.appendChild(hint);
    }
    return row;
  }

  /** 提交单项配置。成功后重新拉一次 describe —— 不本地改状态，
      因为后端可能规范化了值（比如把轮数夹到合法区间）。 */
  function submitConfig(it, value){
    var key, payload;
    if (it.id.indexOf('plugin:') === 0) {
      key = 'plugin';
      payload = { name: it.pluginName, enabled: !!value };
    } else if (it.id === 'goalRoundLimit') {
      key = 'goal';
      payload = { enabled: true, maxRounds: value };
    } else if (it.id.indexOf('api.') === 0) {
      /* 模型 API 的四个字段走同一个 key: 'api'，每次只提交**改的那一项**。
         不整包提交：整包会把用户没动过的字段（尤其密钥）一起覆盖 ——
         而密钥在界面上是打码的，整包提交等于把打码串写回磁盘。 */
      key = 'api';
      if (it.id === 'api.baseUrl') payload = { baseUrl: value };
      else if (it.id === 'api.apiKey') payload = { apiKey: value };
      else if (it.id === 'api.protocol') payload = { protocol: value };
      else if (it.id === 'api.model') payload = { model: value };
      else if (it.id === 'api.thinking') {
        // 思考强度有热切换路径（on_thinking），不走 api 整包
        key = 'thinking';
        payload = value;
      } else payload = {};
    } else if (it.id.indexOf('websearch.') === 0) {
      key = 'websearch';
      if (it.id === 'websearch.provider') payload = { provider: value };
      else if (it.id === 'websearch.url') payload = { url: value };
      else if (it.id === 'websearch.key') payload = { key: value };
      else payload = {};
    } else {
      key = it.id;
      payload = value;
    }
    setMsg('正在保存…', '');
    post('configSet', { key: key, value: payload });
  }

  /** 拉 narnat.md 原文 */
  function loadMarkdown(){
    post('narnatmdGet', {});
  }

  function updateMdFoot(){
    var foot = document.getElementById('setFoot');
    if (!foot) return;
    var dirty = mdText !== mdSaved;
    var saveBtn = foot.querySelector('[data-act="saveMd"]');
    if (saveBtn) saveBtn.disabled = !dirty;
    var tip = foot.querySelector('[data-act="mdTip"]');
    if (tip) tip.textContent = dirty ? '有未保存的修改' : '已保存';
    // 全屏层的保存按钮也跟着联动（两个视图，同一份状态）
    var bigSave = document.getElementById('mdFullSave');
    if (bigSave) bigSave.disabled = !dirty;
    updateMdLen();
  }

  /** 更新两处的「N 行 · M 字符」。内容规模要能让用户判断要不要全屏。 */
  function updateMdLen(){
    var lines = mdText ? mdText.split('\n').length : 0;
    var txt = lines + ' 行 · ' + mdText.length + ' 字符';
    ['mdLen', 'mdFullLen'].forEach(function(id){
      var el = (id === 'mdLen')
        ? document.querySelector('#setFoot [data-act="mdLen"], .set-md-len')
        : document.getElementById(id);
      if (el) el.textContent = txt;
    });
    // 字段区里那个是渲染时插入的，单独按类名找一遍
    var small = document.querySelector('.set-md-len');
    if (small) small.textContent = txt;
  }

  /* ---------- 全屏编辑层 ---------- */
  function setMdFull(on){
    var box = document.getElementById('mdFull');
    if (!box) return;
    box.setAttribute('data-open', on ? 'true' : 'false');
    if (on) {
      var big = document.getElementById('setMdBig');
      if (big) {
        big.value = mdText;
        updateMdLen();
        big.focus();
        // 光标放到末尾：用户点「全屏」多半是要接着写，不是从头看
        try { big.setSelectionRange(mdText.length, mdText.length); } catch (e) {}
        big.scrollTop = big.scrollHeight;
      }
      var bigSave = document.getElementById('mdFullSave');
      if (bigSave) bigSave.disabled = (mdText === mdSaved);
    } else {
      // 收起时把全屏层里的编辑结果写回唯一真源（mdText）
      var big2 = document.getElementById('setMdBig');
      if (big2 && big2.value !== mdText) {
        mdText = big2.value;
        var small = document.getElementById('setMd');
        if (small) small.value = mdText;
      }
      updateMdFoot();
    }
  }

  var mdFullEl = document.getElementById('mdFull');
  if (mdFullEl) {
    var bigTa = document.getElementById('setMdBig');
    if (bigTa) {
      bigTa.addEventListener('input', function(){
        mdText = bigTa.value;
        // 同步回小编辑区，两处永远一致（保存提交的是 mdText，不会错）
        var small = document.getElementById('setMd');
        if (small && small.value !== mdText) small.value = mdText;
        updateMdFoot();
      });
    }
    var bc = document.getElementById('mdFullCopy');
    if (bc) bc.addEventListener('click', function(){
      if (!mdText) return;
      post('clipboard/write', { text: mdText, id: 'md' });
    });
    var bs = document.getElementById('mdFullSave');
    if (bs) bs.addEventListener('click', function(){
      
      post('narnatmdSet', { text: mdText });
    });
    var bx = document.getElementById('mdFullClose');
    if (bx) bx.addEventListener('click', function(){ setMdFull(false); });
  }

  /** 重画底部动作条。指令分组才有保存按钮。 */
  function renderFoot(){
    var foot = document.getElementById('setFoot');
    if (!foot) return;
    foot.innerHTML = '';
    if (setGroup === 'prompt') {
      var tip = document.createElement('span');
      tip.setAttribute('data-act', 'mdTip');
      tip.style.cssText = 'flex:1;font-size:10.5px;color:var(--fg-faint);align-self:center';
      tip.textContent = '已保存';
      foot.appendChild(tip);
      var save = document.createElement('button');
      save.setAttribute('data-act', 'saveMd');
      save.className = 'set-primary';
      save.style.flex = 'none';
      save.style.padding = '0 14px';
      save.textContent = '保存';
      save.disabled = true;
      save.addEventListener('click', function(){
        save.disabled = true;
        
        post('narnatmdSet', { text: mdText });
      });
      foot.appendChild(save);
    } else {
      var b = document.createElement('button');
      b.textContent = '重新读取';
      b.addEventListener('click', function(){ setMsg('', ''); loadDescribe(); });
      foot.appendChild(b);
      var open = document.createElement('button');
      open.textContent = '打开配置文件';
      open.addEventListener('click', function(){ post('openSettings', {}); });
      foot.appendChild(open);
    }
  }

  /** 顶栏 ⚙ 开面板 */
  var settingsEntry = document.getElementById('btnSettings');
  if (settingsEntry) settingsEntry.addEventListener('click', function(e){
    e.stopPropagation();
    var p = document.getElementById('settingsPanel');
    setPanelOpen(p.getAttribute('data-open') !== 'true');
  });
  var setCloseEl = document.getElementById('setClose');
  if (setCloseEl) setCloseEl.addEventListener('click', function(){ setPanelOpen(false); });
  window.__narnatOnConfigDescribe = function(p){
    setItems = (p && Array.isArray(p.items)) ? p.items : [];
    setGroups = (p && Array.isArray(p.groups) && p.groups.length)
      ? p.groups
      // 老后端不给 groups：按已知分组兜底，至少面板不空白
      : [{ id: 'api', label: '模型 API' }, { id: 'websearch', label: '联网工具' }, { id: 'prompt', label: '提示词' }];
    if (!setGroups.some(function(g){ return g.id === setGroup; })) setGroup = setGroups[0].id;
    // 有改动还没生效 → 面板顶部常驻一行提示。
    // 不提示会怎样：用户改完 key 直接提问、拿到 401，以为新 key 是错的。
    if (p && p.restartPending) {
      setMsg('需重启后端才生效', 'err');
    }
    renderTabs();
    renderFoot();
    renderBody();
  };

  /* narnatmd/result 到达时的处理。
     「有未保存修改」用 mdText !== mdSaved 判断；所以这里绝不能无条件用
     服务器的值覆盖 mdText —— 用户在回执到达前打的字会被静默丢掉（实测：
     点「提示词」分组触发一次 narnatmdGet，回执一到，刚敲的几行全没了，
     而且不报错）。只有本地没有改动（mdText === mdSaved）时才接受服务器的值。 */
  window.__narnatOnNarnatMd = function(p){
    var incoming = (p && typeof p.text === 'string') ? p.text : '';
    // describe 里那条 narnat.md 的 path 可能还没到，这里补一次
    setItems = (setItems || []).map(function(it){
      if (it.id === 'narnatmd' && p && p.path) it.path = p.path;
      return it;
    });
    var clean = (mdText === mdSaved);
    if (!clean) {
      // 本地有未保存的改动：保留用户的输入，只把「服务器那份」记下来备查
      mdSaved = incoming;
      updateMdFoot();
      return;
    }
    mdText = incoming;
    mdSaved = incoming;
    /* 就地写进两个 textarea，**不调 renderBody() 重建**。
       重建的代价（实测）：光标跳回开头、滚动位置归零、输入法组合态被打断 ——
       用户在几百行里刚滚到某处，一次回执就把他弹回顶部，感受就是
       「不能上下滑动看完整内容」（其实能滚，只是每次都被重置）。
       只有编辑区还没画出来（切成别的分组了）时才需要重建。 */
    var small = document.getElementById('setMd');
    if (small && setGroup === 'prompt') {
      if (small.value !== mdText) small.value = mdText;
      var big = document.getElementById('setMdBig');
      if (big && big.value !== mdText) big.value = mdText;
    } else if (setGroup === 'prompt') {
      renderBody();
    }
    updateMdFoot();
    renderFoot();
  };

  window.__narnatOnConfigResult = function(p){
    var ok = p && (p.ok === true || p.accepted === true);
    if (!ok) {
      setMsg('失败：' + ((p && (p.error || p.message)) || ''), 'err');
      // 失败后重拉，让界面回到真实值（不能停在用户以为改成的那个值上）
      loadDescribe();
      return;
    }
    /* needRestart：接口地址/密钥/协议 改动写的是磁盘，
       而当前进程里的 LLMClient 还在用旧值（AIConfig 是加载时构造的）。
       必须明说，否则用户改完 key 直接提问拿到 401，会以为新 key 是错的。 */
    var need = p && (p.needRestart === true ||
      (p.current && p.current.needRestart === true));
    setMsg(need ? '已保存 · 需重启后端' : '已保存', 'ok');
    loadDescribe();
  };

  window.__narnatOnNarnatMdSaved = function(p){
    if (p && p.ok) {
      mdSaved = mdText;
      updateMdFoot();
      setMsg(p.needRestart ? '已保存 · 需重启后端' : '已保存', 'ok');
    } else {
      setMsg('失败：' + ((p && (p.error || p.message)) || ''), 'err');
    }
  };

  palette.addEventListener('click', function(e){
    var it = e.target.closest('.item');
    if (!it) return;
    var i = parseInt(it.getAttribute('data-i'), 10);
    if (!isNaN(i)) palSel = i;
    completePalette();
  });

  input.addEventListener('input', function(){
    palSel = 0;
    /* 用户真在打字 → 退出历史浏览状态。
       不退出会怎样（实测）：按 ↑ 翻出一条旧提问（histIdx 变成 >=0），
       接着把它改成 `/`，此时补全面板已经弹出，但方向键要用的那条分支
       有 `histIdx === -1` 这个前提 —— 于是 ↑↓ 既选不了候选、也不再翻历史，
       用户以为「敲 / 之后上下键失灵」。终端里也是这个语义：
       一旦开始编辑，历史浏览就结束了。
       程序化赋值（historyStep 里 input.value = ...）不会触发 input 事件，
       所以这里不会把自己的历史浏览打断。 */
    histIdx = -1;
    refreshPalette();
  });

  // 窗口变矮/变高都会改变面板可用空间，重算一次
  window.addEventListener('resize', function(){
    if (paletteVisible()) fitPalette();
  });

  /** 输入框随内容长高（上限由 CSS max-height 控制，超出后内部滚动） */
  function autoGrowInput(){
    var max = 160;
    // 空内容时不能一律固定 21px：极窄面板（180px）下占位符会折成两行，
    // 固定 21px 就把第二行裁掉了（实测 scrollHeight 42 > clientHeight 21），
    // 用户看不到「输入 / 命令」的提示。
    // 做法：先把高度放开量一次真实 scrollHeight，再收到「至少一行」。
    if (!input.value) {
      input.style.height = 'auto';
      var empty = Math.min(Math.max(input.scrollHeight, 21), max);
      input.style.height = empty + 'px';
      input.style.overflowY = 'hidden';
      return;
    }
    input.style.height = 'auto';
    var h = Math.min(input.scrollHeight, max);
    input.style.height = Math.max(h, 21) + 'px';
    input.style.overflowY = input.scrollHeight > max ? 'auto' : 'hidden';
  }

  /**
   * 发送按钮的「空/有字」外观。
   *
   * 空输入时按钮变暗：一眼看出「现在按了也没用」，而不是按下去才被无声忽略。
   * 只改颜色（data-empty → CSS），不改尺寸/位置：按钮跳动比颜色不好看更刺眼。
   * 只判有没有字；运行中这个按钮本身是隐藏的（body[data-run] 换成停止按钮）。
   */
  function refreshSendState(){
    var b = document.getElementById('sendBtn');
    if (!b) return;
    b.setAttribute('data-empty', input.value.trim() ? 'false' : 'true');
  }
  /* 让「发完就清空」这类程序化改动也走同一条路：直接写 input.value 不会触发
     input 事件，发送按钮就会停在亮态（看着像「还能再发一条」）。原来是在两处
     手动补调 refreshSendState()，容易漏；这里从源头兜住，新增清空点不必记得补。 */
  (function watchInputValue(){
    var desc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    if (!desc || !desc.set) return;   // 环境不支持就退回手动调用，不抛错
    Object.defineProperty(input, 'value', {
      configurable: true,
      get: function () { return desc.get.call(this); },
      set: function (v) {
        desc.set.call(this, v);
        refreshSendState();
      }
    });
  })();

  input.addEventListener('input', autoGrowInput);
  input.addEventListener('input', refreshSendState);
  refreshSendState();
  // 面板宽度变化时占位符可能从一行变两行（或反过来），
  // 只在 input 事件里调是跟不上的。
  window.addEventListener('resize', function () { autoGrowInput(); });

  /* ---------- 附件：文件 / 文件夹 / 图片 ----------
     语义（用户明确要求）：拖进来的文件、文件夹**不复制、不存储**，
     只把绝对路径发给 AI，让它自己用 Read/Grep/Glob 处理；
     只有「没有磁盘路径的图片」（现场截图 / 剪贴板图片）才先让宿主落到
     临时目录，再把那个临时路径发过去。
     宿主侧消息（见 extension.js）：attach/pick、attach/check、attach/save、attach/thumb，
     回执：attach/picked、attach/checked、attach/saved、attach/thumbResult。 */

  /** 待发送的附件：[{id, path, name, kind:'image'|'folder'|'file', dataUrl, thumbAsked}] */
  var ATCH = [];
  /** 附件请求的自增 id：宿主回执里带着它，避免把 A 的缩略图贴到 B 上 */
  var atchSeq = 0;
  /** id → 回执回调 */
  var atchWaiters = {};
  var KIND_MARK = { image: '▣', folder: '▤', file: '▢' };
  var KIND_TEXT = { image: '图片', folder: '文件夹', file: '文件' };
  /** 单张图片上限（与宿主侧一致）：超过就不传，界面说清原因 */
  var ATCH_MAX_IMG = 8 * 1024 * 1024;

  function atchSend(type, params, cb){
    var id = ++atchSeq;
    var p = params || {};
    p.id = id;
    if (cb) atchWaiters[id] = cb;
    post(type, p);
    return id;
  }

  /** 宿主回执统一入口：按 id 找回调（找不到就丢，说明这条请求早已过了时限） */
  function atchOnReply(p){
    if (!p) return;
    var cb = atchWaiters[p.id];
    if (!cb) return;
    delete atchWaiters[p.id];
    cb(p);
  }

  /** 按文件名猜类型；文件夹没有扩展名，真实类型由宿主 attach/check 给 */
  function atchGuessKind(name, path){
    return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(String(name || path || '')) ? 'image' : 'file';
  }

  /** 重画附件 chip 列表（整块重画，避免逐个补丁对不上） */
  function renderAttach(){
    var box = document.getElementById('attachBox');
    if (!box) return;
    box.innerHTML = '';
    ATCH.forEach(function(a){
      var el = document.createElement('span');
      el.className = 'atch';
      el.setAttribute('data-path', a.path || '');
      if (a.kind === 'image') {
        // 缩略图由宿主 attach/thumb 读本地文件回 dataURL；
        // 文件已被清理 / 过大 / 读失败 → 回 null，这里显示 [图片] 占位符。
        // 留一个空 img 会变成「一条看不见内容的附件」，用户以为没加进来。
        if (a.dataUrl) {
          var img = document.createElement('img');
          img.alt = '';
          img.src = a.dataUrl;      // 只来自宿主，不拼进 innerHTML
          el.appendChild(img);
        } else {
          var ph = document.createElement('span');
          ph.className = 'ph';
          ph.textContent = '[图片]';
          el.appendChild(ph);
        }
      }
      var k = document.createElement('span');
      k.className = 'k';
      k.textContent = KIND_MARK[a.kind] + ' ' + (KIND_TEXT[a.kind] || '文件');
      el.appendChild(k);
      // 文件名可能带 < > "，一律走 textContent
      var n = document.createElement('span');
      n.className = 'n';
      n.textContent = a.name || a.path;
      n.setAttribute('data-tip', a.path || '');
      el.appendChild(n);
      var x = document.createElement('button');
      x.className = 'x';
      x.type = 'button';
      x.setAttribute('data-tip', '移除');
      x.textContent = '×';
      x.addEventListener('click', function(){
        ATCH = ATCH.filter(function(it){ return it.id !== a.id; });
        renderAttach();
        input.focus();
      });
      el.appendChild(x);
      box.appendChild(el);
    });
    box.setAttribute('data-on', ATCH.length ? 'true' : 'false');
  }

  /** 读一张本地图片的缩略图（文件没了就留 [图片]，不阻塞其它附件） */
  function atchThumb(a){
    if (!a || a.dataUrl || a.thumbAsked) return;
    a.thumbAsked = true;
    // 走共享缓存：同一张图在输入区、气泡、历史里只向宿主请求一次
    atchThumbOf(a.path, function(url){
      a.dataUrl = url;
      renderAttach();
    });
  }

  /**
   * 收下一批「有磁盘路径」的附件。
   * 先按扩展名猜类型让 chip 立刻可见，再让宿主 attach/check 核对
   * 存在性与真实类型（文件夹只能由宿主判断）；路径不存在的当场剔除并说明，
   * 不能静默失败——用户会以为文件已经跟着发出去了。
   */
  function atchAddPaths(paths){
    var fresh = [];
    // 必须判数组：宿主回执若把 paths 给成字符串/对象（版本不匹配、宿主异常），
    // 直接 .forEach 会抛 TypeError，整条回执的处理中断，附件一个都加不进来，
    // 而且异常会冒到 dispatchServerMessage 外层（实测 page-error）。
    if (!Array.isArray(paths)) return;
    paths.forEach(function(raw){
      var p = String(raw || '');
      if (!p) return;
      var dup = false;
      ATCH.forEach(function(a){ if (a.path === p) dup = true; });
      if (dup) return;
      var name = p.split(/[\\/]/).pop() || p;
      var item = {
        id: ++atchSeq, path: p, name: name,
        kind: atchGuessKind(name, p), dataUrl: null, thumbAsked: false
      };
      ATCH.push(item);
      fresh.push(item);
    });
    if (!fresh.length) return;
    renderAttach();
    atchSend('attach/check', { paths: fresh.map(function(a){ return a.path; }) }, function(res){
      var items = (res && res.items) || [];
      var bad = [];
      fresh.forEach(function(a, i){
        var it = items[i] || null;
        if (!it) {
          for (var j = 0; j < items.length; j++) {
            if (items[j].path === a.path) { it = items[j]; break; }
          }
        }
        if (it && it.exists === false) {
          a.bad = true;
          bad.push(a.path);
          return;
        }
        // 宿主说得更准（尤其文件夹）：以它为准 —— 但只认这三种已知类型。
        // 宿主回了未知 kind（版本不匹配）时保留按扩展名猜的值，
        // 否则 chip 的图标兜底会渲染成「undefined 文件」（实测）。
        if (it && (it.kind === 'image' || it.kind === 'folder' || it.kind === 'file')) {
          a.kind = it.kind;
        }
        if (a.kind === 'image') atchThumb(a);
      });
      if (bad.length) {
        ATCH = ATCH.filter(function(it){ return !it.bad; });
        sysLine('✗', '附件路径不存在，已移除',
          esc(bad.slice(0, 3).join('、')) + (bad.length > 3 ? ' 等 ' + bad.length + ' 个' : ''), 'err');
        scrollBottom();
      }
      renderAttach();
    });
  }

  /**
   * 没有磁盘路径的图片（截图 / 剪贴板）：
   * 先让宿主写进临时目录，拿到路径后当普通图片附件处理。
   * 写失败必须说出来——否则用户以为图已经跟着发出去了。
   */
  function atchSaveImage(file){
    var name = (file && file.name) || ('clipboard-' + Date.now() + '.png');
    if (file && file.size && file.size > ATCH_MAX_IMG) {
      sysLine('✗', '图片超过 8MB，未添加', esc(name), 'err');
      scrollBottom();
      return;
    }
    var fr = new FileReader();
    fr.onload = function(){
      var s = String(fr.result || '');
      var cut = s.indexOf(',');
      var b64 = cut >= 0 ? s.slice(cut + 1) : s;
      if (!b64) {
        sysLine('✗', '图片读取失败', esc(name), 'err');
        scrollBottom();
        return;
      }
      atchSend('attach/save', { name: name, dataB64: b64 }, function(res){
        if (!res || res.error || !res.path) {
          sysLine('✗', '图片保存失败', esc((res && res.error) || '未知原因'), 'err');
          scrollBottom();
          return;
        }
        atchAddPaths([res.path]);
      });
    };
    fr.onerror = function(){
      sysLine('✗', '图片读取失败', esc(name), 'err');
      scrollBottom();
    };
    fr.readAsDataURL(file);
  }

  /**
   * 把 File 列表变成附件。
   * VS Code（Electron）里 dataTransfer.files[i].path 就是绝对路径，直接用，
   * 不复制任何内容；拿不到 path 时（沙箱 / 某些平台）只有图片能救——
   * 读成 base64 交给宿主落临时文件；其它类型必须提示用「＋附件」按钮，
   * 不能静默丢弃。
   */
  function atchAcceptFiles(files){
    var arr = [];
    for (var i = 0; i < (files ? files.length : 0); i++) arr.push(files[i]);
    if (!arr.length) return;
    var paths = [];
    arr.forEach(function(f){
      if (!f) return;
      var p = f.path || '';
      // 相对路径没有意义：AI 在别的工作目录里读不到它，当「没有路径」处理
      if (p && p.charAt(0) !== '/' && !/^[A-Za-z]:[\\/]/.test(p) && p.indexOf('\\\\') !== 0) {
        p = '';
      }
      if (p) {
        paths.push(p);
        return;
      }
      var isImg = /^image\//.test(f.type || '') || atchGuessKind(f.name) === 'image';
      if (isImg) {
        atchSaveImage(f);
        return;
      }
      sysLine('✗', '拿不到文件路径', esc(f.name || '（未命名）'), 'warn');
      scrollBottom();
    });
    if (paths.length) atchAddPaths(paths);
  }

  /**
   * 挂一个拖拽落点。
   * drop 必须 preventDefault：不拦的话 Electron 会拿这份文件去导航整个
   * webview，面板直接变成一张图片或空白页（拖一张图进来就复现）。
   */
  function atchDropZone(el){
    if (!el) return;
    el.addEventListener('dragenter', function(e){
      if (!e.dataTransfer) return;
      e.preventDefault();
      el.setAttribute('data-drop', 'true');
    });
    el.addEventListener('dragover', function(e){
      if (!e.dataTransfer) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      el.setAttribute('data-drop', 'true');
    });
    el.addEventListener('dragleave', function(){
      el.removeAttribute('data-drop');
    });
    el.addEventListener('drop', function(e){
      var dt = e.dataTransfer;
      if (!dt) return;
      e.preventDefault();
      e.stopPropagation();
      el.removeAttribute('data-drop');
      atchAcceptFiles(dt.files);
    });
  }
  atchDropZone(document.querySelector('.cbox'));
  atchDropZone(convo);

  // 粘贴：截图工具/剪贴板里的图片没有 path，只能走「存临时文件」这条路；
  // 纯文本粘贴不拦（照常进输入框）。
  input.addEventListener('paste', function(e){
    var cd = e && e.clipboardData;
    if (!cd) return;
    var files = cd.files && cd.files.length ? cd.files : null;
    if (!files && cd.items && cd.items.length) {
      var picked = [];
      for (var i = 0; i < cd.items.length; i++) {
        var it = cd.items[i];
        if (!it || it.kind !== 'file') continue;
        var f = it.getAsFile ? it.getAsFile() : null;
        if (f) picked.push(f);
      }
      if (picked.length) files = picked;
    }
    if (!files || !files.length) return;
    e.preventDefault();
    atchAcceptFiles(files);
  });

  // 「＋附件」：webview 没有文件系统权限，必须让宿主弹 VS Code 原生选择框
  //（showOpenDialog 才能同时支持选文件与选文件夹）。
  var attachBtnEl = document.getElementById('attachBtn');
  if (attachBtnEl) {
    attachBtnEl.addEventListener('click', function(){
      atchSend('attach/pick', {}, function(res){
        if (!res || res.cancelled) return;
        atchAddPaths(res.paths || []);
      });
    });
  }

  /**
   * 附件路径块：后端协议不变（仍走 chat/send），把绝对路径写进文本，
   * 由 AI 自己用工具去读——插件不复制、不存储文件内容。
   */
  function attachBlock(list){
    if (!list || !list.length) return '';
    var lines = ['[用户附带了以下本地路径，请用相应工具自行读取处理]'];
    list.forEach(function(a){
      lines.push('- ' + (KIND_TEXT[a.kind] || '文件') + '：' + a.path);
    });
    return '\n\n' + lines.join('\n');
  }

  /** 附件清单块里图片行：只认前置写入的这一种格式（行首 + 全角冒号） */
  var ATTACH_IMG_LINE = '- 图片：';
  /** 缩略图缓存：path → {url, t}（只存宿主成功读到的 dataURL + 载入时间）。
      发送区与历史、重进会话都复用同一份结果，同一张图只向宿主请求一次。
      为什么只存成功：失败结果一旦进了缓存就再也不会重试，图片被清理后界面
      会永远停在旧图上（实测：删图后重渲染仍 img=1）。失败不缓存，代价只是
      重新渲染时多问一次宿主（一次本地读文件），换来「文件没了就显示 [图片]」。 */
  var THUMB = {};
  /** 缓存有效期（毫秒）：超过后重新渲染会重新向宿主确认文件是否还在 */
  var THUMB_TTL = 60000;
  /** path → 正在等回执的回调队列：并发请求合并成一次 */
  var THUMB_WAIT = {};

  /** 丢掉全部缩略图缓存（窗口重新可见时用）：下次渲染重新确认文件是否还在 */
  function atchThumbFlush(){
    Object.keys(THUMB).forEach(function(p){ delete THUMB[p]; });
  }

  /**
   * 做一次「允许重新请求」判定用的事件源：窗口从隐藏变成可见。
   * 用户切走再切回来是「图片可能已被清理」的典型时机（面板一直开着、
   * 文件在别处被删掉），这里把缓存全判过期，下次渲染即重新确认。
   */
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden) atchThumbFlush();
  });

  /** 取一张本地图片的缩略图（走宿主 attach/thumb，成功结果按路径短时缓存） */
  function atchThumbOf(path, cb){
    var hit = Object.prototype.hasOwnProperty.call(THUMB, path) ? THUMB[path] : null;
    // 命中条件：缓存未过期。过期按未命中处理 —— 文件可能在缓存有效期内被清理，
    // 永久命中会让 [图片] 占位符永远不出现。
    if (hit && (Date.now() - hit.t) < THUMB_TTL) {
      cb(hit.url);
      return;
    }
    if (THUMB_WAIT[path]) {
      THUMB_WAIT[path].push(cb);
      return;
    }
    THUMB_WAIT[path] = [cb];
    atchSend('attach/thumb', { path: path }, function(res){
      var url = (res && typeof res.dataURL === 'string' && res.dataURL) ? res.dataURL : null;
      if (url) THUMB[path] = { url: url, t: Date.now() };
      else delete THUMB[path];
      var list = THUMB_WAIT[path] || [];
      delete THUMB_WAIT[path];
      list.forEach(function(f){ f(url); });
    });
  }

  /**
   * 从消息正文里解析附件清单块中的图片路径。
   * 只认上面这一种格式（行首「- 图片：」+ 路径），不做宽松猜测：
   * 没有该块的老消息一个节点都不多画。
   */
  function attachPathsInText(text){
    var out = [];
    String(text == null ? '' : text).split(/\r?\n/).forEach(function(line){
      if (line.indexOf(ATTACH_IMG_LINE) !== 0) return;
      var p = line.slice(ATTACH_IMG_LINE.length).trim();
      if (p) out.push(p);
    });
    return out;
  }

  /**
   * 在用户气泡里画缩略图行：复用输入区那套 .attach / .atch / .ph 样式与
   * attach/thumb 宿主消息，不另造一套。
   * 宿主读不到（文件已被清理 / 路径不存在）→ 留 [图片] 占位符：
   * 画一个空 img 等于给用户看「一条看不见内容的附件」。
   */
  function addMsgThumbs(host, paths){
    var list = (paths || []).filter(Boolean);
    if (!host || !list.length) return null;
    var box = document.createElement('div');
    box.className = 'attach';
    box.setAttribute('data-on', 'true');
    list.forEach(function(p){
      var el = document.createElement('span');
      el.className = 'atch';
      el.setAttribute('data-path', p);
      var ph = document.createElement('span');
      ph.className = 'ph';
      ph.textContent = '[图片]';
      el.appendChild(ph);
      var n = document.createElement('span');
      n.className = 'n';
      n.textContent = String(p).split(/[\\/]/).pop() || p;
      n.setAttribute('data-tip', p);
      el.appendChild(n);
      box.appendChild(el);
      atchThumbOf(p, function(url){
        if (!url) return;            // 读不到就留 [图片]
        var img = document.createElement('img');
        img.alt = '';
        img.src = url;               // 只来自宿主，不拼进 innerHTML
        if (ph.parentNode === el) el.replaceChild(img, ph);
        else el.insertBefore(img, n);
      });
    });
    host.appendChild(box);
    return box;
  }

  /**
   * 状态行「这一项有没有真值」的统一维护。
   *
   * 模型 / 思考 / 插件 三项没有 data-empty 属性（它们的值是按钮文字，被 14 处
   * JS 直接赋值），所以在窄面板（470~381px，标签已让掉）会出现一个孤零零的
   * 「—」或「插件 —」。逐个改那 14 处赋值太容易漏，这里用 MutationObserver
   * 盯着这几个按钮的文本：一变更就把 data-empty 标到外层那一项上，
   * 隐藏规则交给 CSS（见 @container 470~381px 那段）。
   *
   * 「没真值」的判定：文本为空、或等于占位符（「—」/「插件 —」）。
   * 只 observer 这几个节点，开销与状态行更新频率同级（每轮几次）。
   */
  function watchStatusValues() {
    var targets = [
      { id: 'btnModel', empty: function (t) { return !t || t === '—' || t === '-'; } },
      { id: 'btnThink', empty: function (t) { return !t || t === '—' || t === '-'; } },
      { id: 'btnPlugin', empty: function (t) {
        return !t || /^插件\s*(—|-)?$/.test(t) || t === '插件';
      } },
      { id: 'btnGoal', empty: function (t) { return !t || t === '目标模式 —'; } }
    ];
    targets.forEach(function (t) {
      var btn = document.getElementById(t.id);
      if (!btn) return;
      var box = btn.closest('.st-core, .st-opt, .st-think') || btn.parentNode;
      function sync() {
        var txt = (btn.textContent || '').trim();
        box.setAttribute('data-empty', t.empty(txt) ? 'true' : 'false');
      }
      sync();
      try {
        new MutationObserver(sync).observe(btn, {
          childList: true, characterData: true, subtree: true
        });
      } catch (err) { /* 没有 MutationObserver 就退回只在初始化时判一次 */ }
    });
  }
  watchStatusValues();

  /**
   * 同步顶栏「对话记录」按钮的无障碍名称。
   *
   * 历史：输入行原来有个「历史对话」chip，会话名直接写在 chip 文字上。
   * 那个 chip 已删除（入口挪到顶栏、会话名顶栏本来就有），但调用点还留着几处——
   * 全部收敛到这里改造，而不是逐个删调用：调用点分布在 initialize /
   * session/result / 切会话等多个分支上，逐个删很容易漏一处，
   * 漏掉的地方就会引用一个不存在的节点（静默失效，没人会发现）。
   * @param {string} name 会话名；空串 = 未保存
   */
  function setChipSess(name) {
    var el = document.getElementById('histBtn');
    if (!el) return;
    var n = String(name == null ? '' : name);
    // 按钮本身是纯图标（☰），会话名在顶栏胶囊里已经有了，这里只补 aria-label，
    // 让读屏软件说出「当前在哪段对话」，视觉上不占地方。
    el.setAttribute('data-tip', '对话记录');
    el.setAttribute('aria-label', '对话记录，当前 ' + (n || '未保存') + '，点击打开列表');
  }

  /* ---------- 输入历史（上下键调出） ----------
     HIST 是「按 ↑ 能翻到的提问」列表，最旧在前。来源有三个：
     ① 磁盘上的全局历史（initialize 的 inputHistory，与终端形态同一个
        `<data_dir>/.narnat_history`）—— 关掉 VSCode、重连后端都还在，
        单独存一份 HIST_PERSIST，因为 initialize 会重复到达且要幂等；
     ② 当前会话里 role=user 的消息（seedHistory 传进来的 msgs）；
     ③ 本次面板生命周期内发出的（pushHistory，只活在内存里）。
     换会话、新建对话**不再清空**：用户要的是「我上次问过的都能 ↑ 翻回来」，
     而不是「只有当前会话的提问」。旧设计（换会话后旧会话的提问不该出现）已废弃。
     只放用户真正打的文字：目标模式注入的内部指令（`[自动续跑]…`）
     不是用户输入，翻出来会让他以为自己说过那句话。 */
  var HIST = [];          // 已提交的输入，最旧在前
  var HIST_PERSIST = [];  // 磁盘全局历史（后端 inputHistory），最旧在前
  var HIST_MAX = 100;
  var histIdx = -1;       // -1 = 不在浏览历史（正在打新的）
  var histDraft = '';     // 开始浏览前正在打的内容，↓ 回到这里

  /** 追加一条历史（不重置浏览状态以外的任何东西） */
  function pushHistory(text){
    var t = String(text || '').trim();
    if (!t) return;
    // 浏览态必须**无条件**复位，且要放在去重 return 之前。
    // 踩过的坑（实测）：按 ↑ 翻出上一条 → Enter 重发 → 这条与 HIST 末尾相同，
    // 去重命中直接 return，而 histIdx 还停在浏览位置 —— 于是
    //   · 下次按 ↑ 从这个位置继续往前，**跳过刚发的那条**；
    //   · 历史只有 1 条时，↑ 干脆一点反应都没有（idx 已在 0 且没被复位）。
    // 复位浏览态与「要不要记这条」是两件事，不能因为后者不做就不做前者。
    histIdx = -1;
    histDraft = '';
    // 连续重复不重复记（连按两次发送同一条很常见）
    if (HIST.length && HIST[HIST.length - 1] === t) return;
    HIST.push(t);
    if (HIST.length > HIST_MAX) HIST.shift();
    histIdx = -1;
    histDraft = '';
  }

  /**
   * 合并「全局历史 + 本次传入的会话消息」→ 新的 HIST。
   * 规则：同一文本只留一份（位置靠后的胜出）、保持最旧在前、超长从头截。
   * 一次遍历从尾往头去重，避免二次拼接把顺序弄错。
   *
   * 第二个参数**同时接受两种形状**：
   *   · 后端消息对象 `{role, text}`（只取 user）
   *   · 纯字符串数组（内存里的 HIST 就是这个形状）
   * 只认对象会怎样（实测）：`mergeHistory(HIST_PERSIST, HIST)` 里每个元素
   * 都没有 .role，于是整批被跳过 —— 看着合并了，其实内存里刚发的提问全丢了。
   */
  function mergeHistory(persist, msgs){
    var out = [];
    (persist || []).forEach(function(t){
      var s = String(t == null ? '' : t).trim();
      if (s) out.push(s);
    });
    (msgs || []).forEach(function(m){
      if (!m) return;
      var t;
      if (typeof m === 'string') {
        t = m.trim();
      } else {
        if (m.role !== 'user') return;
        t = String(m.text == null ? '' : m.text).trim();
        if (isGoalInjection(t)) return;      // 内部注入的指令不算用户提问
      }
      if (!t) return;
      out.push(t);
    });
    var seen = [];
    var rev = [];
    for (var i = out.length - 1; i >= 0; i--) {
      if (seen.indexOf(out[i]) >= 0) continue;
      seen.push(out[i]);
      rev.push(out[i]);
    }
    rev.reverse();
    if (rev.length > HIST_MAX) rev = rev.slice(-HIST_MAX);
    return rev;
  }

  /**
   * 用「全局历史 + 后端历史消息」重算 HIST。
   *
   * 只在「拿到一份历史」时调用（initialize 的 session.messages、
   * sessionSwitch/sessionDone 与 /cd 的 messages）。原来这里是整份替换，
   * 换会话就把旧会话的提问全扔掉——用户明确要全局（跨会话、跨重启都能 ↑），
   * 所以现在改成以 HIST_PERSIST 为底再叠本次消息；seedHistory([])
   * （新建对话）= 只留全局历史。
   * @param {Array} msgs 后端历史（role/text）
   */
  function seedHistory(msgs){
    if (!Array.isArray(msgs)) return;
    // 底线必须是「磁盘全局历史 + 当前内存 HIST」，只按 HIST_PERSIST 重算会丢东西：
    // 本次会话里刚发的提问此刻**只在内存 HIST 里**（磁盘那份要等下一次
    // initialize 才回灌回来），所以一切换会话就按 HIST_PERSIST 重算，
    // 用户刚问过的内容会从 ↑ 历史里消失 —— 明明磁盘还在、重连又能翻到，
    // 但当场就是翻不出来（实测）。这跟「全局历史」的承诺直接冲突。
    HIST = mergeHistory(mergeHistory(HIST_PERSIST, HIST), msgs);
    histIdx = -1;
    histDraft = '';
  }

  /**
   * 在历史里前后移动。返回 true 表示已处理（调用方要 preventDefault）。
   * 到顶/到底时仍返回 true 并停住，避免按键漏到别处。
   */
  function historyStep(dir){
    if (!HIST.length) return false;
    if (dir < 0) {                       // ↑ 往旧
      if (histIdx === -1) {
        histDraft = input.value;
        histIdx = HIST.length - 1;
      } else if (histIdx > 0) {
        histIdx -= 1;
      } else {
        return true;                     // 已在最旧一条
      }
    } else {                             // ↓ 往新
      if (histIdx === -1) return false;  // 没在浏览历史，让光标自己走
      if (histIdx < HIST.length - 1) {
        histIdx += 1;
      } else {
        histIdx = -1;
        input.value = histDraft;
        autoGrowInput();
        // 回到草稿说明用户放弃浏览历史了，补全面板必须跟着关。
        // 不关会怎样（实测）：↑ 调出 /cd s1（面板自动打开）→ ↓ 回到草稿
        // 「帮我看下这个报错」→ 面板还开着、列的还是 s1 的候选 →
        // 按 Enter 直接被面板守卫接管，把草稿整行换成 /cd s1（草稿丢失），
        // 再按一次 Enter 就真切了会话。Tab 也会改。
        refreshPalette();
        return true;
      }
    }
    input.value = HIST[histIdx];
    autoGrowInput();
    // 光标放到末尾，方便直接改
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) {}
    // 调出的若是命令，顺带把补全面板打开
    refreshPalette();
    return true;
  }

  /* ---------- 输入框键盘：命令补全 + 发送 ---------- */

  /** 把输入框里的内容当命令执行（与 sendChat 对称），执行后清空 */
  function runInputCommand(){
    var v = input.value.trim();
    if (!v) return;
    // 已经掉线时敲命令：不能把输入框清空，命令根本发不出去，
    // 清掉等于用户白打一遍（实测，与 sendChat 同因）。
    // 判定用 data-offline 而不是 !REAL：模拟态（还没连上）要保留本地演示命令。
    if (REAL && body.getAttribute('data-offline') === 'true') {
      pushHistory(v);
      openPalette(false);
      // 同 sendChat：即使离线也要发一次给宿主，触发那边的「用户主动发请求
      // = 再试一次」自救（重置重连计数并重新拉起后端）。
      post('command', { command: v.replace(/^\//, '').split(/\s+/)[0].toLowerCase(),
                        args: v.replace(/^\//, '').split(/\s+/).slice(1).join(' '),
                        text: v });
      sysLine('·', '未连接后端，命令未执行', '');
      scrollBottom();
      return;
    }
    pushHistory(v);
    openPalette(false);
    input.value = '';
    input.style.height = '21px';
    input.style.overflowY = 'hidden';
    // 程序化清空不触发 input 事件：发送按钮的亮暗得手动同步一次，
    // 否则发完之后按钮还亮着（看着像「还能再发一条」）。
    refreshSendState();
    if (REAL) {
      // /ls 与 /help 走本地渲染，不发后端。
      // 为什么：后端回的是纯文本 lines，靠空格对齐，中英混排必然错列
      //（实测同一列起点相差 40px），而且没法按面板宽度换紧凑格式——
      // 真实默认面板只有 249px，后端那份宽格式要横拖才看得全。
      // 会话数据已经通过 session/list 同步进 SESS（含 children/status/ghost），
      // 本地渲染内容与后端一致、排版更好。
      var bare = v.replace(/^\//, '').split(/\s+/)[0].toLowerCase();
      if (bare === 'ls' || bare === 'help' || bare === 'h' || bare === '?') {
        runCommand(v);
        return;
      }
      // 输入框敲 /done 与抽屉「完成并合并结论」是**同一个操作**，但两条通路
      // 回执字段不同：命令通路只回 handled/ok/lines，请求通路还回
      // messages/current/isChild（server.py:984-991 的注释就是为修这个坑写的）。
      // 走命令通路会怎样（实测）：顶栏已改成父会话、后端上下文也换成
      // 「父会话历史 + 子会话结论」，而对话区仍是分支那几条 —— 合并出来的
      // 结论一个字都看不见，用户以为合并失败，重做一遍分支。
      // 这里改走请求通路，完全复用按钮的处理逻辑，不新增任何分支。
      if (bare === 'done' && REAL) {
        post('sessionDone', {});
        return;
      }
      // /thinking 的中文档位名要转成后端认的档位名。
      // 界面上「思考」下拉写的是「低/中/高」，用户自然会照着敲 /thinking 高，
      // 而后端只认 low/medium/high，于是回「无效值: 高」（实测）。
      // 反查表以后端下发的显示名为准（用户可以在配置里改「强度选项」），
      // 后端没给时退回本地兜底表。
      if (bare === 'thinking') {
        var tArg = v.replace(/^\/thinking\s*/, '').trim();
        if (tArg) {
          var hitV = '';
          var table = thinkLabels || THINK_LABEL;
          for (var key in table) {
            if (table[key] === tArg) { hitV = key; break; }
          }
          if (!hitV) {
            for (var k2 in THINK_LABEL) {
              if (THINK_LABEL[k2] === tArg) { hitV = k2; break; }
            }
          }
          if (hitV) v = '/thinking ' + hitV;
        }
      }
      sendCommand(v);
    } else {
      runCommand(v);
    }
  }

  input.addEventListener('keydown', function(e){
    // 输入法组词中（拼音还没上屏）：Enter / 方向键都属于输入法，不能当命令处理。
    // 不判断会把未上屏的拼音当消息发出去、还吞掉上屏（实测发出去的是 "nihao"）。
    // keyCode 229 是组词期间部分浏览器的统一码，两个都要看。
    if (e.isComposing || e.keyCode === 229) return;
    // 1) 命令面板打开时，方向键/Enter/Tab 归它用。
    //    例外：正在浏览输入历史（histIdx !== -1）时 ↑↓ 仍归历史——
    //    否则调出一条以 / 开头的历史后，面板会劫持后续 ↑↓，
    //    更早的历史永远翻不到（实测 100% 复现）。
    if (paletteVisible() && palItems.length && histIdx === -1) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        palSel = (palSel + 1) % palItems.length;
        renderPalette();
        scrollPalSelIntoView();
        resetPaletteScrollIfFirst();
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        palSel = (palSel - 1 + palItems.length) % palItems.length;
        renderPalette();
        scrollPalSelIntoView();
        return;
      }
      if (e.key === 'Tab') {
        // Tab 是补全的通用直觉，且不会误发消息
        e.preventDefault();
        completePalette();
        return;
      }
      if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault();
        var typed = input.value.split('\n')[0].trim();
        var sel = palItems[palSel];
        // 补全而不执行的情况：
        //   1. 命令名还没打完（"/sa"）
        //   2. 命令需要参数但还没打参数（"/rm" + Enter 只会报「请指定名称」）
        //   3. 正在补参数（"/cd oo" 还没选定）
        // 无参命令（/done）打全了就执行。
        if (palMode === 'arg') {
          completePalette();
          return;
        }
        if (sel) {
          // 只有「参数必需」的命令才拦下 Enter 去补全；
          // /ls、/mode 这类不填参数就是查状态的，必须能直接执行。
          if (sel.req || sel.c.toLowerCase() !== typed.toLowerCase()) {
            completePalette();
            return;
          }
        }
        runInputCommand();
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        // Esc 要能关掉挡在眼前的浮层，按最上层关。原来只关命令候选，
        // 下拉和抽屉完全不理 Esc（实测：连按 4 次 Esc，模型下拉一直开着，
        // 只有改用鼠标点空白处才关得掉），键盘用户会以为界面卡住了。
        // 确认条不参与：它代表后端在等 y/n，关掉就等于永久不回话。
        // 顺序：右键菜单 → 命令候选 → 下拉 → 抽屉，一次一层。
        var ctxM = document.getElementById('sessMenu');
        if (ctxM && ctxM.getAttribute('data-open') === 'true') {
          ctxM.setAttribute('data-open', 'false');
          return;
        }
        if (paletteVisible()) { openPalette(false); return; }
        if (openPop) { closePop(); return; }
        if (drawer.getAttribute('data-open') === 'true') { setDrawer(false); return; }
        openPalette(false);
        return;
      }
    }

    // 2) 面板没开
    if (e.key === 'Escape') {
      // 同 1)：没有候选可关时，继续往下一层收
      var ctxM2 = document.getElementById('sessMenu');
      if (ctxM2 && ctxM2.getAttribute('data-open') === 'true') {
        ctxM2.setAttribute('data-open', 'false');
        return;
      }
      if (openPop) { closePop(); return; }
      if (drawer.getAttribute('data-open') === 'true') { setDrawer(false); return; }
      openPalette(false);
      return;
    }
    // 上下键调出输入历史（像终端那样）。多行输入时让给光标移动。
    // 输入以 / 开头时不翻历史：那是命令补全的地盘（上面那条分支处理有候选的
    // 情形），连一个候选都没有时（比如只敲了 "/" 又删掉字符）翻历史会把
    // 「/」直接换成一条旧提问，用户以为自己的输入被吃了（实测）。
    //
    // 但「正在浏览历史」时必须放行（histIdx >= 0）：历史里本来就有 /ls、/save
    // 这种命令条目，翻到它之后输入框内容以 / 开头，若还按上面那条判据拦，
    // 用户就再也翻不到更早的输入了 —— 按多少次 ↑ 都停在同一条（实测）。
    // 两种情况要分开：`histIdx === -1` 才是「用户正在打新内容」，
    // `histIdx >= 0` 是「输入框里的字是我刚给他翻出来的」，不是命令补全的场景。
    var browsingHist = histIdx >= 0;
    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') &&
        input.value.indexOf('\n') < 0 &&
        (browsingHist || input.value.trim().charAt(0) !== '/')) {
      if (historyStep(e.key === 'ArrowUp' ? -1 : 1)) {
        e.preventDefault();
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      var v = input.value.trim();
      if (!v) return;
      if (v.charAt(0) === '/') {
        runInputCommand();
      } else {
        sendChat();
      }
    }
  });

  document.getElementById('stopBtn').addEventListener('click', function(ev){
    var btn = document.getElementById('stopBtn');
    // 两个按钮占同一块位置：停止一旦收口、发送按钮就显出来，连点的第二下
    // 会落在它身上。这里先起保护窗口，与 sendBtn 的守卫配对。
    guardSendAfterStop();
    // 反方向也要保护，但只保护**真正的连点**：连点「发送」时第一下发出消息
    // 并把按钮换成「停止」，同坐标的第二下就落在停止上——若不拦，会把自己
    // 刚发的那轮取消掉。判据是「同位置 + 250ms 内」，不是原来的「400ms 内一律
    // 忽略」——后者会让「发出去马上后悔、点停止」完全无效（实测后端继续写盘）。
    if (isSameSpotAsSend(ev)) {
      var lr = convo.lastElementChild;
      var dupStop = lr && lr.className.indexOf('sys') >= 0 &&
        (lr.textContent || '').indexOf('这一下没有停止') >= 0;
      if (!dupStop) {
        sysLine('·', '这一下没有停止，请再点一次', '');
        scrollBottom();
      }
      return;
    }
    if (REAL) {
      // 点过一次就锁住：后端 cancel 要等这一轮真正收口（可能 1~2 秒），
      // 期间按钮还是「停止」的样子，用户会以为没点上而连点，把 cancel
      // 请求和后续 busy 判断搅在一起。这里立刻给出「停止中」并禁点：
      // 圆钮放不下文字，改成方块脉冲 + title 说明。
      if (btn.getAttribute('data-pending') === 'true') return;
      btn.setAttribute('data-pending', 'true');
      btn.setAttribute('data-tip', '停止中…');
      post('cancel', {});
      return;
    }
    body.setAttribute('data-run', 'false');
    var line = document.createElement('div');
    line.className = 'sys';
    line.innerHTML = '<span class="ico">■</span><span>已打断</span><span class="tail">可继续输入</span>';
    convo.appendChild(line);
    scrollBottom(true);
  });

  /** 一轮收口后把「停止中」的状态还原。任何 turn 结束路径都要调。 */
  function resetStopBtn(){
    var btn = document.getElementById('stopBtn');
    if (!btn) return;
    btn.removeAttribute('data-pending');
    btn.setAttribute('data-tip', '停止');
  }
  /**
   * 刚点过停止后的短暂窗口里，忽略发送按钮的点击。
   * 两个按钮占同一块位置，连点停止的第二下会落在发送上（见 sendBtn 的监听器）。
   */
  var sendGuardUntil = 0;
  /**
   * 刚点过发送后的短暂窗口里，忽略停止按钮的点击（连点发送的第二下会落到停止上）。
   *
   * 这里必须区分「同一位置连点」与「用户真的要停」：
   * 原来一刀切 400ms 内全部忽略，代价是**用户发了消息就后悔、立刻点停止 →
   * 一点都没停**，按钮也不变色，只在对话区多一行灰字；后端继续跑并把文件
   * 写完了（实测 0ms / 300ms 两次点击 `posts` 里只有 chat、没有 cancel，
   * 目标文件照样落盘 2310B）。用户以为停了，转身去干别的。
   * 现在窗口收到 150ms 且要求「位置几乎不变」：真人从「点了发送」到
   * 「决定停下并点停止」最快也要 200ms 以上，而一次物理连点（双击）的两下
   * 间隔在 150ms 内、落点差不到 10px —— 两者因此分得开。
   */
  var stopGuardUntil = 0;
  var sendClickXY = { x: 0, y: 0 };
  var lastSendClick = { t: 0, x: 0, y: 0 };
  function guardSendAfterStop(){
    sendGuardUntil = Date.now() + 400;
  }
  function guardStopAfterSend(){
    stopGuardUntil = Date.now() + 150;
    lastSendClick = { t: Date.now(), x: sendClickXY.x, y: sendClickXY.y };
    // 用完即清：键盘 Enter 发送时不该沿用上一次鼠标点击的坐标，
    // 否则「鼠标发过一条 → Enter 发下一条 → 鼠标点停止」会被误判成连点。
    sendClickXY = { x: 0, y: 0 };
  }
  /** 这一次点击是不是「刚才那一下发送」的连点？（同位置 + 150ms 内） */
  function isSameSpotAsSend(ev){
    if (Date.now() >= stopGuardUntil) return false;
    var dx = Math.abs(((ev && ev.clientX) || 0) - lastSendClick.x);
    var dy = Math.abs(((ev && ev.clientY) || 0) - lastSendClick.y);
    return dx <= 10 && dy <= 10;
  }

  document.getElementById('sendBtn').addEventListener('click', function(ev){
    // 停止按钮和发送按钮占同一块位置（data-run 切换显隐），用户连点「停止」
    // 时第二下正好落在刚变出来的发送按钮上，把输入框里的草稿直接发出去
    // （实测：连点 2 次就发出一段用户根本没打算发的消息）。
    // 停止后 400ms 内的发送视为误触；但要**说一句**——静默丢弃会让用户
    // 以为按钮坏了，然后再点一次（实测）。
    if (Date.now() < sendGuardUntil) {
      var lastRow = convo.lastElementChild;
      var dup2 = lastRow && lastRow.className.indexOf('sys') >= 0 &&
        (lastRow.textContent || '').indexOf('这一下没有发送') >= 0;
      if (!dup2) {
        sysLine('·', '这一下没有发送，请再点一次', '');
        scrollBottom();
      }
      return;
    }
    // 记下这一击的位置：停止按钮要知道「落在同一处的下一击」才是连点误触
    sendClickXY = { x: ev ? ev.clientX : 0, y: ev ? ev.clientY : 0 };
    sendChat();
  });

  /**
   * 键盘访问思考块/工具行的标题。
   *
   * 这两个标题是 div，只能点。纯键盘用户既展不开也折不上，而里面有工具输出
   * 和完整思考——没有任何等价命令能替代（实测）。
   * 做法：Enter / 空格 当成点击派发，复用同一套委托逻辑，不复制行为。
   */
  convo.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    // 长按 Enter 会连发 keydown（e.repeat）。不挡会怎样（实测）：
    // 按住不放，标题连续 toggle 十几次，松手后的终态取决于次数的奇偶——
    // 用户看到的是「展开了又合上」的随机结果。
    if (e.repeat) return;
    // 带修饰键的是别的东西（Ctrl+Enter 提交、Shift+Enter 换行等），
    // 不该顺手把标题展折一次。
    if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return;
    var row = e.target.closest && e.target.closest('.row');
    if (!row) return;
    // 只处理标题本身获得焦点的情况；行内的按钮自己会响应 Enter
    if (e.target !== row) return;
    e.preventDefault();
    // 合成的 click 会冒泡到「点空白处关下拉」的处理器上，顺手把开着的下拉
    // 关掉、已做的选择丢弃（实测）。这里先主动关掉并停掉传播，行为可预测：
    // 焦点都移到标题上了，下拉本就该收。
    if (openPop) closePop();
    var ev = new MouseEvent('click', { bubbles: true });
    Object.defineProperty(ev, '__fromKeyboard', { value: true });
    row.dispatchEvent(ev);
  });

  convo.addEventListener('click', function(e){
    // 代码块交互。放在 .row 之前判断：这些按钮不在 .row 里，
    // 但都靠事件委托，顺序错了会被 .row 分支吃掉。
    if (e.target.closest) {
      // 工具输出「还有 N 行未显示 · 点击展开」。
      // 剩下的行等到点开时才填进 DOM——一次几百行会拖慢渲染。
      var moreOut = e.target.closest('.snippet .more');
      if (moreOut) {
        var toolsWrap = moreOut.closest('.tools');
        var entry = null;
        Object.keys(toolMap).forEach(function(k){
          if (toolMap[k].wrap === toolsWrap) entry = toolMap[k];
        });
        // 历史重放的工具行不在 toolMap 里（它们是只读快照，没有实时 entry）。
        // 那类行的剩余内容挂在 .more 节点的 __rest 上（见 addHistoryTool），
        // 两处都认，展开按钮才是通用能力 —— 否则历史里的「还有 N 行」
        // 点了没反应（比不显示按钮更糟）。
        var restLines = (entry && entry.restLines) || moreOut.__rest;
        if (restLines && restLines.length) {
          var frag = '';
          restLines.forEach(function(t){
            frag += '<div class="ln"><span class="tx">' + esc(t) + '</span></div>';
          });
          moreOut.insertAdjacentHTML('beforebegin', frag);
          if (entry) entry.restLines = null;
          moreOut.__rest = null;
        }
        moreOut.parentNode.removeChild(moreOut);
        scrollBottom();
        // 展开后内容可能有两屏高，用户眼睛还停在原来那一行，脚下突然多出
        // 800px（实测 200 行输出展开后 slot 高 874px，块尾在视口外 300px）。
        // 把视野带到展开区域的末尾附近，否则会以为「点了没反应」。
        try {
          var foot = toolsWrap ? toolsWrap.querySelector('.foot') : null;
          if (foot && foot.scrollIntoView) foot.scrollIntoView({ block: 'nearest' });
        } catch (err) { /* 忽略 */ }
        return;
      }
      var wrapBtn = e.target.closest('.code-wrap');
      if (wrapBtn) {
        var box = wrapBtn.closest('.code');
        var on = box.getAttribute('data-wrap') === 'true';
        box.setAttribute('data-wrap', on ? 'false' : 'true');
        wrapBtn.setAttribute('data-on', on ? 'false' : 'true');
        // 记住这次选择：新来的代码块、以及本轮结束后重渲染的块都按它来
        wrapPref = !on;
        return;
      }
      // 折叠的代码块展开：去掉 data-fold，按钮自己消失
      var moreBtn = e.target.closest('.code-more');
      if (moreBtn) {
        var mbox = moreBtn.closest('.code');
        mbox.removeAttribute('data-fold');
        foldOpenPref = true;
        moreBtn.parentNode.removeChild(moreBtn);
        // 按钮被移除后焦点会掉回 <body>（同确认条）。交给代码块的 <pre>，
        // 用户能接着 Tab 到「复制」或继续往下走。
        try {
          var pre = mbox.querySelector('pre');
          if (pre) { pre.setAttribute('tabindex', '-1'); pre.focus(); }
        } catch (err) { /* 忽略 */ }
        return;
      }
      var copyBtn = e.target.closest('.code-copy');
      if (copyBtn) {
        var pre = copyBtn.closest('.code') ? copyBtn.closest('.code').querySelector('pre') : null;
        if (pre) {
          copyText(pre.textContent).then(function(){
            copyBtn.setAttribute('data-done', 'true');
            copyBtn.textContent = '已复制';
            setTimeout(function(){
              copyBtn.removeAttribute('data-done');
              copyBtn.textContent = '复制';
            }, 1200);
          }).catch(function(){ copyBtn.textContent = '复制失败'; });
        }
        return;
      }
      // 工具卡片里的命令「复制」：只复制那段命令本身。
      var ioCopy = e.target.closest('.io-copy');
      if (ioCopy) {
        var ioBox = ioCopy.closest('.io-sec');
        var ioTx = ioBox ? ioBox.querySelector('.io-tx') : null;
        if (ioTx) {
          copyText(ioTx.textContent).then(function(){
            ioCopy.setAttribute('data-done', 'true');
            ioCopy.textContent = '已复制';
            setTimeout(function(){
              ioCopy.removeAttribute('data-done');
              ioCopy.textContent = '复制';
            }, 1200);
          }).catch(function(){ ioCopy.textContent = '复制失败'; });
        }
        return;
      }
    }
    var row = e.target.closest('.row');
    if (!row) return;
    var next = row.getAttribute('data-open') === 'true' ? 'false' : 'true';
    row.setAttribute('data-open', next);
    // 同步外层容器的 data-open：思考块的标题颜色靠 .think[data-open] 控制，
    // 不同步会出现「内容展开了但标题还是折叠样式」。
    var wrap = row.closest('.think, .tools');
    if (wrap) wrap.setAttribute('data-open', next);
  });

  /* ---------- 待确认命令（删除 / git 安全检查） ---------- */

  /**
   * 后端在轮次中途停下等确认时渲染的操作条。
   * 没有它，用户在界面上只看到「运行中」，不知道后端在等他回 y/n。
   */
  function showConfirm(p){
    // 确认条插在当前正文之后：先收口，否则后续 delta 会写回上方旧气泡，
    // 确认条看起来被"越过"了。
    closeAssistantSegment();
    // 强制成字符串：prompt 万一不是字符串（后端版本不一致/字段被改写），
    // 下面的 replace 会抛 TypeError —— 确认条整张画不出来，而后端仍站在原地
    // 等 y/n，这一轮就废了（实测 prompt=123 时 q.innerHTML 与 appendChild
    // 都没执行，界面上没有按钮，只剩停止按钮）。
    var prompt = String((p && p.prompt) || '确认执行？');
    var cmds = (p && p.commands) || [];
    if (!Array.isArray(cmds)) cmds = [];
    var q = document.createElement('div');
    q.className = 'confirm';
    q.setAttribute('data-real', 'true');
    // 把「要执行的命令原文」铺出来。只给一句「确认执行此命令?」等于让用户
    // 盲批准——终端形态里命令就在上面几行打印着，面板里没有那段回显（实测）。
    // 命令可能很长，容器限高 + 内部滚动，不把对话区顶走。
    var cmdHtml = '';
    if (cmds.length) {
      cmdHtml = '<div class="cmd">';
      cmds.forEach(function(c){
        var detail = (c && c.detail) || '';
        var name = (c && c.name) || '';
        cmdHtml += '<div class="cmd-line">' +
          '<span class="cmd-name">' + esc(name) + '</span>' +
          '<code>' + esc(detail || '（无参数）') + '</code></div>';
      });
      cmdHtml += '</div>';
    }
    q.innerHTML = '<div class="q">' + esc(prompt.replace(/\[\s*y\s*\/\s*n\s*\]/ig, '').trim() || '需要你确认') + '</div>' +
      cmdHtml +
      '<div class="acts">' +
      '<button class="primary" data-cf="y">确认执行</button>' +
      '<button data-cf="n">取消</button></div>';
    // 同一条确认只允许一张条：后端重发（重建补发、重连补发）时先把旧的换掉。
    // 不去重会堆出两张可点的条，点哪张都只回一次 y，另一张变成僵尸。
    clearConfirmBoxes();
    convo.appendChild(q);
    // 后端在等用户点确认，这一条必须看得见（用户可能正翻在历史里）
    scrollBottom(true);
    rendered('confirm/request', { prompt: prompt });
  }

  /**
   * 收掉界面上所有「还挂着」的确认条，并留一句说明。
   *
   * 什么时候该收：后端已经不再等这条确认了——`turn/end`（用户点了停止、
   * 这一轮结束了）、`rpc/error`（后端崩了）。不收会怎样（实测）：
   * 后端早就不等了，界面上还摆着可点的「确认执行」；用户点下去只得到
   * 「已提交确认，等待后端执行…」，而那个回执永远不会来（那条请求会
   * 被 ignored 或掉线吞掉），用户就一直等一个不会发生的执行。
   * @param {string} why 为什么要收（空字符串=静默收掉，不写系统行）
   */
  function clearConfirmBoxes(why){
    var boxes = convo.querySelectorAll('.confirm[data-real="true"]');
    if (!boxes.length) return;
    Array.prototype.forEach.call(boxes, function(b){
      if (b.parentNode) b.parentNode.removeChild(b);
    });
    pendingConfirmLine = null;
    if (why) {
      sysLine('·', why, '');
      scrollBottom();
    }
  }

  /** 用户点了确认/取消：把 y/n 回给后端（此时 chat/send 会被 busy 拒绝） */
  function replyConfirm(box, yes){
    // 演示态（后端还没连上）：这条确认条是模板里的示例数据，点了不能
    // 假装"已确认执行"——它的正文写着 rm -rf 三个会话文件，用户会以为
    // 自己真批准了一次删除（实测点了就回一句「已确认执行」，实际什么都没发生）。
    if (!REAL) {
      box.parentNode.removeChild(box);
      sysLine('·', '示例数据，后端未连接', '');
      scrollBottom();
      rendered('confirm/reply', { yes: yes, demo: true });
      return;
    }
    if (REAL) {
      post('confirmReply', { text: yes ? 'y' : 'n' });
    }
    box.parentNode.removeChild(box);
    // 先画「已提交」而不是「已确认执行」：真正执行是由后端在这一轮里完成的，
    // 它可能已经被别人消费掉（ignored）、也可能超时。直接写「已确认执行」
    // 就出现了「界面说执行了、后端根本没收到」的假成功。
    // 回执（confirm/result）到了会替换这一行。
    var line = document.createElement('div');
    line.className = 'sys';
    line.innerHTML = '<span class="ico">·</span><span>' +
      (yes ? '已提交确认，等待后端执行…' : '已取消该命令') + '</span>';
    convo.appendChild(line);
    pendingConfirmLine = { el: line, yes: yes };
    // 回执再也不来时这行不能永远挂着（实测：后端卡在长工具里、请求被丢、
    // 后端刚好退出，三种情况都出现过）。20 秒后如实改成「没等到回应」——
    // 不断言命令执行了还是没执行（真的不知道），只说清现状与下一步。
    if (confirmWaitTimer) clearTimeout(confirmWaitTimer);
    confirmWaitTimer = setTimeout(function () {
      confirmWaitTimer = null;
      var pc = pendingConfirmLine;
      if (!pc || pc.el !== line || !line.parentNode) return;
      updateSysLine(line, '⚠', '没有等到后端的确认回执',
        '这条命令执行与否未知；可看工具行，或重发前先确认目标状态', 'warn');
      pendingConfirmLine = null;
    }, 20000);
    if (confirmWaitTimer.unref) confirmWaitTimer.unref();
    scrollBottom();
    // 点确认条的按钮（或按 y/n）之后，那个按钮已经随 box 一起被移除，
    // 焦点会掉回 <body>——下一个 Tab 从面板顶部重新开始，用户得连按十几次
    // 才回得到输入区（实测）。把焦点交还输入框。
    try { input.focus(); } catch (err) { /* 忽略 */ }
    rendered('confirm/reply', { yes: yes });
  }

  /**
   * confirm/reply 的回执。
   *
   * 后端可能回 accepted:true（真的排进队列了），也可能回
   * ignored:true / accepted:false（这条确认已经失效：别处消费掉了、或超时）。
   * 原来前端完全不处理这个回执，一律显示「已确认执行」——实测后端回
   * ignored 的时候界面也那么写，用户以为批准了，而那一轮还在等确认，
   * 之后就卡在那里（所有消息被 busy 拒）。
   */
  function applyConfirmResult(p){
    var line = pendingConfirmLine;
    if (!line || !line.el || !line.el.parentNode) return;
    if (confirmWaitTimer) { clearTimeout(confirmWaitTimer); confirmWaitTimer = null; }
    var accepted = !!(p && (p.accepted || p.ok)) && !(p && p.ignored);
    var msg = (p && (p.reason || p.error || p.message)) || '';
    updateSysLine(line.el,
      accepted ? '✓' : '⚠',
      // accepted 只代表「后端接受了这次确认」，命令还在跑：写「已确认执行」
      // 是把「开始」说成「完成」。真正执行完由那条工具行的 tool/result 收口。
      accepted ? (line.yes ? '已确认，正在执行…' : '已取消该命令') : '确认已失效',
      accepted ? '' : esc(msg || '后端没有接受这次确认'),
      accepted ? 'ok' : 'warn');
    pendingConfirmLine = null;
    // 确认没被接受 = 那条命令**不会执行了**。此时必须立刻给等确认的工具行定性，
    // 否则它会一直停在「等待确认」+ data-st=running，屏幕上同时写着「已打断」
    // 或「确认已失效」，两处结论互相打脸（实测）。用户最想知道的恰是
    // 「rm -rf 到底跑没跑」——答案是没跑，必须说出来。
    // 不能等 turn/end：确认被 ignored 时后端可能已经认为这一轮结束了，
    // 不再发任何事件（实测 E5 场景：等 3 秒、点停止、收 turn/end 都试过）。
    if (accepted) {
      // 接受了 = 命令真的开始执行了。行头必须从「等待确认」改成「执行中」：
      // 不改会怎样（实测）：用户点完确认回头一看行头没变化，会以为没点上
      // 而再点一次，第二次得到 ignored —— 然后掉进「永久等待确认」那个坑。
      // 摘掉 data-await-confirm 之后，后续 turn/end 的收口就能正常把它
      // 判成「已中断/未完成」了（不再被当成"还在等用户"而跳过）。
      Object.keys(toolMap).forEach(function (k) {
        var e2 = toolMap[k];
        if (!e2 || !e2.row) return;
        if (e2.row.getAttribute('data-await-confirm') !== '1') return;
        e2.row.removeAttribute('data-await-confirm');
        var mt = e2.row.querySelector('.meta');
        if (mt) { mt.className = 'meta'; mt.textContent = '执行中'; }
      });
    } else {
      settleRunningTools('cancelled', { final: true });
    }
    scrollBottom();
    rendered('confirm/result', { accepted: accepted });
  }

  convo.addEventListener('click', function(e){
    var btn = e.target.closest('.confirm [data-cf]');
    if (!btn) return;
    var box = btn.closest('.confirm');
    if (!box || !box.parentNode) return;
    replyConfirm(box, btn.getAttribute('data-cf') === 'y');
  });


  /**
   * 自绘提示框：接管所有 data-tip，**不再用原生 title**。
   *
   * 为什么不用 title：那是 Electron 的系统提示框，直角 + 系统配色 + 字号固定，
   * 在深色面板上很突兀，也无法跟随 VS Code 主题（实测被点名「太丑、不跟随主题」）。
   * 这里的实现要点：
   *   · 事件委托（mouseover/mouseout 冒泡）：会话列表、工具行都是 innerHTML
   *     重建的，逐个 addEventListener 会随重绘丢失，委托一次就够。
   *   · 只认最近的 data-tip 祖先，且要判断「鼠标确实是从外面进来的」——
   *     mouseover 在子元素之间移动也会冒泡，不判 relatedTarget 会疯狂闪烁。
   *   · 定位：优先在元素上方居中；越界翻到下方；左右夹在视口内。
   *   · 触摸/键盘：focusin 也给提示（键盘用户 Tab 到图标上看得到说明）。
   */
  var tipEl = document.getElementById('tip');
  var tipFor = null;
  var tipTimer = 0;
  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = 0; }
    tipFor = null;
    if (tipEl) tipEl.setAttribute('data-on', 'false');
  }
  function showTip(el) {
    if (!tipEl || !el) return;
    var text = el.getAttribute('data-tip') || '';
    if (!text) { hideTip(); return; }
    tipFor = el;
    tipEl.textContent = text;
    tipEl.setAttribute('data-on', 'true');
    // 夹取基准一律用 .shell 的实际几何，不用 window/innerWidth 或 100vw：
    // 面板宽 = iframe 宽，但 innerWidth 在某些宿主环境里并不等于它（测试台里就
    // 恒为 764，而面板被拖到 200）。用错基准的后果实测有两个：提示框在 ≤210px
    // 面板里比面板还宽、右侧被裁；以及 left 夹取算到面板外面去。
    var shellEl = document.querySelector('.shell');
    var sr = shellEl ? shellEl.getBoundingClientRect()
                     : { left: 0, right: window.innerWidth, width: window.innerWidth };
    // 先在量尺寸之前把上限写死，否则量到的是旧宽度
    tipEl.style.maxWidth = Math.min(220, Math.max(80, sr.width - 8)) + 'px';
    var r = el.getBoundingClientRect();
    var tr = tipEl.getBoundingClientRect();
    var vh = window.innerHeight;
    // 左右：以元素中心对齐，再夹在**面板**内（留 4px 余量，贴边不切断圆角）
    var left = r.left + r.width / 2 - tr.width / 2;
    left = Math.max(sr.left + 4, Math.min(left, sr.right - tr.width - 4));
    // 上下：优先上方；上方放不下就翻到下方；都放不下就压在上方边界内
    var top = r.top - tr.height - 6;
    if (top < 4) top = Math.min(r.bottom + 6, vh - tr.height - 4);
    tipEl.style.left = Math.round(left) + 'px';
    tipEl.style.top = Math.round(top) + 'px';
  }
  function tipTarget(node) {
    return (node && node.closest) ? node.closest('[data-tip]') : null;
  }
  document.addEventListener('mouseover', function (e) {
    var el = tipTarget(e.target);
    if (!el || el === tipFor) return;
    // 鼠标在这块元素内部移动时 relatedTarget 仍属于它 → 视为没离开，不重算
    var from = tipTarget(e.relatedTarget);
    if (from === el) return;
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = 0; }
    showTip(el);
  });
  document.addEventListener('mouseout', function (e) {
    var el = tipTarget(e.target);
    if (!el) return;
    var to = tipTarget(e.relatedTarget);
    if (to === el) return;       // 还在同一块里
    if (el === tipFor) hideTip();
  });
  document.addEventListener('focusin', function (e) {
    var el = tipTarget(e.target);
    if (el) showTip(el);
  });
  document.addEventListener('focusout', hideTip);
  // 滚动/改宽度时提示框会错位，直接收掉比追着跑更干净
  document.addEventListener('scroll', hideTip, true);
  window.addEventListener('resize', hideTip);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hideTip();
  });
  // 点任何地方都收（点完按钮提示立刻消失，否则会盖在刚弹出的下拉上）
  document.addEventListener('mousedown', hideTip, true);

  /* ---------- 下拉面板 ---------- */
  var popMap = { btnModel: 'popModel', btnThink: 'popThink', btnGoal: 'popGoal', btnPlugin: 'popPlugin' };
  var openPop = null;

  function closePop(){
    if (openPop) {
      openPop.setAttribute('data-open', 'false');
      // 触发按钮上的 data-open 一起复位：箭头靠它旋转（见 .sess[data-open] .dim）
      var owner = openPop.__owner;
      if (owner && owner.setAttribute) owner.setAttribute('data-open', 'false');
      openPop.__owner = null;
      openPop = null;
    }
  }

  /**
   * 打开「小浮层」（下拉 / 右键菜单）之前统一调用：收起命令面板，并关掉其它小浮层。
   *
   * 为什么必须有这一层：下拉与右键菜单的触发入口全都 stopPropagation（为了让
   * 「点别处收起」不把它们自己也关掉），于是 document 上那个「点别处收面板」的
   * 监听器**根本不会执行** —— 面板能与它们同时开着，谁也管不了谁。
   * 同屏的后果不是「难看」，而是**点击穿透**：被面板遮住的候选只露上半截，
   * 点下去命中的是下层语义完全不同的控件（想选命令、实际执行了改名/删除）。
   *
   * 抽成一处而不是在每个入口各写一遍：这一族问题已经出现过三次
   *（palette↔状态抽屉、状态抽屉↔会话抽屉、palette↔下拉），每次都是「补一处漏一处」。
   * 现在的规则是：**任何小浮层打开前都走这里**，新增入口只要调一次就不会漏。
   */
  function dismissOtherOverlays(){
    if (typeof paletteVisible === 'function' && paletteVisible()) openPalette(false);
    if (openPop) closePop();
    var ctxM = document.getElementById('sessMenu');
    if (ctxM && ctxM.getAttribute('data-open') === 'true') {
      ctxM.setAttribute('data-open', 'false');
    }
  }

  function showPop(btn, popId){
    var pop = document.getElementById(popId || popMap[btn.id]);
    if (!pop) return;
    if (openPop === pop) { closePop(); return; }
    closePop();
    dismissOtherOverlays();
    var r = btn.getBoundingClientRect();
    // 子会话下拉的宽度等于触发它的顶栏会话名按钮，但不小于 150px。
    // 这个下限是「左名字 + 右状态」两列各能读到几个字的极限，再窄读不出内容。
    // 注意：判据是**面板宽度**而不是按钮宽度 —— 会话名短（"alpha" → 按钮 63px）
    // 时面板本身很宽，下拉按 150px 展开完全放得下，此时不该隐藏
    //（实测按按钮宽判会把 700px 面板下的下拉也误藏）。
    var childW = 0;
    if (pop.id === 'popChild') {
      // 判据用 .shell 的实际宽度，不能用 window.innerWidth：webview 的 innerWidth
      // 未必等于面板宽（探测环境里两者就不同），拿错了会让阈值形同失效。
      var shellEl = document.querySelector('.shell');
      var avail = shellEl ? shellEl.getBoundingClientRect().width : window.innerWidth;
      // 180px 以下不弹：扣掉左右内边距后可用宽已不足 150px，展开的下拉必然
      // 被裁掉一截或被迫横向拖，读不出内容。
      if (avail < 180) return;
      childW = Math.max(150, Math.round(r.width));
      if (childW > avail - 4) childW = avail - 4;
    }
    pop.setAttribute('data-open', 'true');
    // 记住是谁触发、并在它身上也标 data-open：箭头旋转、按钮高亮都读这个属性。
    // 只给 .pop 设属性的话，按钮上永远读不到"已展开"，箭头就一直是朝下的常态。
    pop.__owner = btn;
    if (btn && btn.setAttribute) btn.setAttribute('data-open', 'true');
    if (pop.id === 'popChild') {
      // 子会话下拉从顶栏按钮**向下**展开：它是全屏最靠上的控件，下方才是空余空间。
      // 沿用「向上弹」那套算法会怎样（实测）：anchorTop 取到按钮自身的 top(≈20)，
      // maxHeight 只剩 80px，三四条子会话就要滚动 —— 而子会话本来就少，
      // 用户要的是一眼全看完再点。
      // 高度给足（视口下方剩余全部），子会话少时自然全部展开，多了才滚。
      pop.style.maxHeight = Math.max(120, window.innerHeight - r.bottom - 12) + 'px';
      // 宽度必须落在这里：childW 只算进了局部变量，而下面那句 pop.style.width
      // 在本分支 return 之后，永远执行不到 —— 结果宽度被 #popChild 里 nowrap
      // 的条目按内容撑开（实测未保存态脚注把下拉撑到 339px，249px 面板里溢出 146px）。
      // 条目是 flex:0 0 auto + nowrap，不会被 width 自动挤窄，所以 width 与
      // max-width 一起给；放不下的部分由 #popChild 的 overflow-x 横向拖。
      // min-width 显式写 0：CSS 里已解掉 .pop 的 196px 下限，这里再兜一层，
      // 否则任何一处加回 min-width 都会让上面两行失效。
      pop.style.width = childW ? childW + 'px' : '';
      pop.style.maxWidth = childW ? childW + 'px' : '';
      pop.style.minWidth = childW ? '0' : '';
      pop.style.top = Math.round(r.bottom + 6) + 'px';
      // 左边界的夹取判据也换成 .shell 的 right：与上面 maxHeight 用的口径一致。
      // 用 window.innerWidth 会怎样（实测）：webview 里两者不等，窄面板下
      // 夹出来的 left 仍在面板外，左侧一整截不可见。
      var shellRight = shellEl ? shellEl.getBoundingClientRect().right : window.innerWidth;
      pop.style.left = Math.max(2, Math.min(r.left,
        shellRight - pop.offsetWidth - 12)) + 'px';
      openPop = pop;
      return;
    }
    // 弹层必须停在**输入区之上**，不能盖住输入框和发送/停止按钮。
    // 只按按钮位置往上弹会怎样（实测，249px 真机默认宽）：弹层 y=632..758，
    // 而输入框 697..718、停止按钮 723..748 全在它下面 —— 用户点输入框继续打字，
    // 实际点到弹层里的某一项，**模型被静默切换并写回 config**；
    // 运行中想点停止也点不到。这是「看一眼下拉然后改主意」这种极常见的操作。
    // 锚点取「按钮」与「输入区顶部」里更靠上的那个。
    var composer = document.querySelector('.composer');
    var anchorTop = r.top;
    if (composer) {
      var ct = composer.getBoundingClientRect().top;
      if (ct < anchorTop) anchorTop = ct;
    }
    pop.style.maxHeight = Math.max(80, anchorTop - 14) + 'px';
    // 子会话下拉宽度锁定成按钮宽度；名字放不下不截断，交给 #popChild 的 overflow-x 横向拖。
    pop.style.width = childW ? childW + 'px' : '';
    // 左边也要夹在面板内：下拉 min-width:196px，面板窄于 208px 时
    // min(r.left, innerWidth-196-12) 恒为负数，左侧一整截跑到面板外面
    // （实测 180px 面板 → left=-28，28px 不可见、模型名前几个字看不到）。
    pop.style.left = Math.max(2, Math.min(r.left, window.innerWidth - pop.offsetWidth - 12)) + 'px';
    pop.style.top = Math.max(8, anchorTop - pop.offsetHeight - 6) + 'px';
    openPop = pop;
  }

  Object.keys(popMap).forEach(function(id){
    var b = document.getElementById(id);
    if (b) b.addEventListener('click', function(e){ e.stopPropagation(); showPop(b); });
  });

  /**
   * 下拉的键盘操作：↑↓ 移动、Enter/空格 确认、Esc 关闭。
   *
   * 原来打开下拉后键盘完全无效（↑↓ 没反应、Enter 直接把下拉关掉），
   * 而按钮能 Tab 到、也能用 Enter 打开——用户自然以为下一步还能用键盘选，
   * 结果只能去摸鼠标（实测）。这里把「选中态」做成 data-sel，
   * 复用既有的 .item:hover 视觉。
   */
  function movePopSel(pop, step) {
    var items = Array.prototype.slice.call(pop.querySelectorAll('.pi'));
    if (!items.length) return;
    var cur = items.findIndex(function (x) { return x.getAttribute('data-sel') === 'true'; });
    var next = cur < 0 ? (step > 0 ? 0 : items.length - 1) : cur + step;
    if (next < 0) next = items.length - 1;
    if (next >= items.length) next = 0;
    items.forEach(function (x, i) { x.setAttribute('data-sel', i === next ? 'true' : 'false'); });
    var el = items[next];
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('keydown', function (e) {
    if (!openPop) return;
    if (e.isComposing || e.keyCode === 229) return;
    // 焦点在任何文本输入控件上时 ↑↓/Enter 归它自己。
    // 只判主输入框不够：目标下拉里有个「轮数上限」输入框（#goalRounds），
    // 焦点在它上面按 ↑ 会被这里拦走，结果数字不变、选中项被改成「关」，
    // 再按 Enter 直接把目标模式关掉（实测）。
    var ae = document.activeElement;
    if (ae === input || (ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName))) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      movePopSel(openPop, e.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (e.key === 'Enter' || e.key === ' ') {
      var sel = openPop.querySelector('.pi[data-sel="true"]');
      if (!sel) return;                 // 还没用方向键选过：让 Enter 走原路径
      e.preventDefault();
      sel.click();
    }
  });

  /* 顶栏的「对话记录」按钮：打开面板内的全宽会话列表。
     入口原来在输入行那个小小的「历史对话」chip 上，藏得深、又和路径 chip
     挤在一起；挪到顶栏后它和「这是哪个对话」的会话名在同一行，语义更近。
     工作目录 chip 仍只给复制（改目录要重开会话，属另一件事）。 */
  var chipSessEl = document.getElementById('histBtn');
  if (chipSessEl) {
    chipSessEl.style.cursor = 'pointer';
    // 顶栏会话名改成开「子会话」下拉后，这里就是会话抽屉的入口。
    // **不要**在这里写死 title：setChipSess 会按当前会话名生成
    // 「历史对话 · 当前：xxx（点击打开会话列表）」，写死会把它冲掉。
    function openHistDrawer(e) {
      e.stopPropagation();
      setDrawer(drawer.getAttribute('data-open') !== 'true');
    }
    chipSessEl.addEventListener('click', openHistDrawer);
    // role=button + tabindex=0 就必须能用键盘触发，否则键盘用户停在这里按 Enter 没反应
    chipSessEl.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      // 按住不放的自动重复要挡掉：不挡会怎样（实测）——按住 Enter 两秒，
      // 抽屉被反复开合十几次，松手时的终态取决于重复次数奇偶，
      // 用户看到的是「抽屉闪一下又关上」。.think .row / .tools .row 都挡了，这里漏了。
      if (e.repeat) return;
      e.preventDefault();
      openHistDrawer(e);
    });
  }
  /* 工作目录 chip 已删除。
     它显示的是 VS Code 当前工作区目录，与「你在编辑器里打开的文件夹」完全同一件事——
     用户已经知道自己在哪个目录，再显示一遍只是占宽度。
     而侧边栏只有 ~300px，CSS 在 460px 以下本来就直接把它藏了（见 @container 那条），
     真实面板里从来没显示过；反而因为删掉 #chipSess 之后下面 applyInitialize 里
     `chips[1]` 的下标失效，它还一直停在模板占位文字「工作目录」。
     要看当前目录用 VS Code 自己的资源管理器标题，或命令面板的「复制路径」。
     复制功能没丢：需要复制时用编辑器里的右键 → 复制路径。 */

  document.addEventListener('click', function(e){
    if (openPop && !e.target.closest('.pop')) closePop();
    // 命令面板也照此关闭：它挡住对话区上半屏，用户点内容区想收起却收不了，
    // 只能按 Escape 或去删输入框字符
    if (paletteVisible() && !e.target.closest('.palette') && e.target !== input &&
        !e.target.closest('.cbox')) {
      openPalette(false);
    }
    // 状态行抽屉：点别处收起。抽屉里的模型/思考按钮会打开 .pop，
    // 点 pop 里的条目不该把抽屉一起关掉（用户还要接着看别的项），所以放行 .pop。
    if (stDrawerOpen() && !e.target.closest('.status') && !e.target.closest('.pop')) {
      setStDrawer(false);
    }
  });

  window.addEventListener('resize', closePop);

  /* ---------- 状态行抽屉（窄面板）----------
     窄面板下状态行固定单行，只留「模型 · 上下文 · 费用」；思考/目标模式/插件/
     缓存/余额收进这个「⋯」，点开整条状态行变成浮层摊平 8 项（CSS 见窄宽度段）。
     收起规则的判据统一在 CSS 里（.status:not([data-open="true"]) ...），
     所以这里只负责 data-open，不必逐个元素 show/hide —— 也就不会出现
     「JS 显示回来的项和 CSS 又藏掉」这种两套规则打架。 */
  var statusBar = document.querySelector('.status');
  var stToggle = document.getElementById('stToggle');
  var composerEl = document.querySelector('.composer');
  function stDrawerOpen(){
    return !!statusBar && statusBar.getAttribute('data-open') === 'true';
  }
  function setStDrawer(on){
    if (!statusBar) return;
    // 展开前先量高度：一旦 data-open=true，状态行就变绝对定位、脱离文档流，
    // .composer 少掉这 20 多像素，输入框会整体上移/下移一下（实测 288→333，
    // 位移 45px，正是收起态那行的高度）。用 padding-bottom 把这段空间补回来，
    // 输入框就钉在原位不动。量必须在改 data-open **之前**。
    if (composerEl) {
      if (composerEl.__stBasePad === undefined) {
        composerEl.__stBasePad = parseFloat(getComputedStyle(composerEl).paddingBottom) || 0;
      }
      if (on && !stDrawerOpen()) {
        var h = statusBar.getBoundingClientRect().height;
        composerEl.style.paddingBottom = (composerEl.__stBasePad + h) + 'px';
      } else if (!on) {
        // 收起时**清掉内联值**，让 CSS 的 padding 重新接管。
        // 写死成 __stBasePad 会怎样（实测）：那个值是在某一档宽度下量的
        // （≤400px 是 8px，更宽是 10px），一旦写成内联就压住了所有宽度档 ——
        // 从 380px 拉宽到 800px 后输入区下方少 2px（面测 base800=10 而实际 8）。
        // 清空内联样式，各档的 padding 自然回到 CSS 定义的值。
        composerEl.style.paddingBottom = '';
      }
    }
    statusBar.setAttribute('data-open', on ? 'true' : 'false');
    // 给 body 挂一个标记：抽屉展开时 CSS 用它把「回到底部」藏起来
    //（两者几何相交，按钮会被抽屉吞掉点击 —— 见 #toBottom 那段注释）。
    document.body.setAttribute('data-stdrawer', on ? 'true' : 'false');
    if (stToggle) {
      stToggle.setAttribute('data-open', on ? 'true' : 'false');
      stToggle.setAttribute('aria-expanded', on ? 'true' : 'false');
      stToggle.textContent = on ? '收起' : '⋯';
    }
    // 抽屉收起时顺手关掉从抽屉里打开的下拉：否则弹层失去锚点飘在原处，
    // 用户既看不出它属于谁，也点不到触发它的按钮。
    if (!on) closePop();
    // 展开抽屉时把命令面板收起来（与 openPalette 里反向那一条对称）：
    // 两者都是从 .composer 向上弹的浮层，同屏会互相遮挡（实测几何相交 63px）。
    // 只做单向的话，先开面板再开抽屉仍然会叠在一起，那一侧看着像没修。
    if (on && typeof paletteVisible === 'function' && paletteVisible()) {
      openPalette(false);
    }
    // 同理：展开状态抽屉时把会话抽屉收起来。两者在 249px 下都是覆盖全宽的浮层，
    // 状态抽屉（z 55）压在会话抽屉（z 50）上，会把它 footer 的三颗按钮
    //（新建对话 / 保存当前为… / 新建探索分支；分支态下还有两颗出口按钮）
    // **整片吞掉** —— 用户点上去零反应，而且看不出来被谁盖住了（实测）。
    // 这一条与 setDrawer(true) 里反向那一条必须成对，否则只修一侧等于没修。
    if (on && typeof setDrawer === 'function' &&
        drawer && drawer.getAttribute('data-open') === 'true') {
      setDrawer(false);
    }
  }
  if (stToggle) {
    stToggle.addEventListener('click', function(e){
      // 不 stopPropagation 的话，下面那条「点别处收起」会立刻把它关回去
      e.stopPropagation();
      setStDrawer(!stDrawerOpen());
    });
  }
  document.addEventListener('keydown', function(e){
    if (e.key !== 'Escape' || !stDrawerOpen()) return;
    setStDrawer(false);
  });
  // 面板被拖宽到「不该有抽屉」的宽度时自动收起。
  // 为什么必须补这一条：一旦拖宽到 ⋯ 不显示的档位，抽屉若还开着
  //   · data-open="true" 会让所有 :not([data-open]) 的收起规则失效 ——
  //     600px 下余额不让位、500px 下目标/插件的文字被省略号截断（实测）
  //   · .composer 的 padding-bottom 补偿也留着 —— 输入区下方多 26px 空白
  //   · 而那个档位没有 ⋯ 按钮，用户**找不到收起入口**，只能碰巧点一下别处
  // 也就是「打开了关不掉」的死局，所以宽度回到不该有的档位就复位。
  // 阈值是 470（不是 380）：⋯ 从 470 起就出现了（见 CSS 那段注释），
  // 超过 470 才真的没有入口。
  window.addEventListener('resize', function(){
    if (!stDrawerOpen()) return;
    var sh = document.querySelector('.shell');
    var w = sh ? sh.getBoundingClientRect().width : window.innerWidth;
    if (w > 470) setStDrawer(false);
  });

  function setChecked(pop, v){
    Array.prototype.forEach.call(pop.querySelectorAll('.pi'), function(pi){
      var on = pi.getAttribute('data-v') === v;
      pi.setAttribute('data-on', on ? 'true' : 'false');
      var ck = pi.querySelector('.ck');
      if (ck) ck.textContent = on ? '✓' : '';
    });
  }

  /**
   * config/set 的回执里没有 key，发起时先按顺序记下来：失败要回滚、成功要记账。
   *
   * 必须是**队列**而不是一个变量。用一个变量会怎样（实测）：
   * 连点两次（先改模型、再改思考）都成功后，变量停在 'thinking'；
   * 此时任何一次失败的回执都会拿着 'thinking' 这个 key、配上一个模型名，
   * 把模型名写进思考档位 → 状态栏「思考」按钮显示成 `deepseek-v4-pro`，
   * 下拉里一个都不打勾。按 FIFO 配对才能一次一清。
   */
  var configKeyQueue = [];
  function postConfigSet(key, value){
    // 离线时**不能入队**：post() 虽然会拦下请求并给出「未连接后端」提示，
    // 但 key 已经先压进队列了 —— 重连后的第一个成功回执会配到这个陈旧 key 上，
    // 之后任何一次失败回退都会拿着「模型」的 key 去写「思考」档位，
    // 状态栏于是出现「思考 = deepseek-flash」或「模型 = high」这种错位（实测）。
    // 所以在 push 之前就判离线，一个都不入队。
    if (REAL && body.getAttribute('data-offline') === 'true') {
      post('configSet', { key: key, value: value });   // 走 post 的离线提示
      return;
    }
    configKeyQueue.push(key);
    post('configSet', { key: key, value: value });
  }

  document.getElementById('popModel').addEventListener('click', function(e){
    var pi = e.target.closest('.pi');
    if (!pi) return;
    var v = pi.getAttribute('data-v');
    if (REAL) postConfigSet('model', v);
    setChecked(this, v);
    document.getElementById('btnModel').textContent = v;
    closePop();
  });

  document.getElementById('popThink').addEventListener('click', function(e){
    var pi = e.target.closest('.pi');
    if (!pi) return;
    var v = pi.getAttribute('data-v');
    if (REAL) postConfigSet('thinking', v);
    setChecked(this, v);
    document.getElementById('btnThink').textContent = pi.querySelector('.lb').textContent;
    closePop();
  });

  document.getElementById('popGoal').addEventListener('click', function(e){
    var pi = e.target.closest('.pi');
    if (!pi) return;
    var v = pi.getAttribute('data-v');
    setChecked(this, v);
    var rounds = document.getElementById('goalRounds').value || '10';
    if (REAL) postConfigSet('goal', { enabled: v === 'on', maxRounds: Number(rounds) });
    var btn = document.getElementById('btnGoal');
    btn.textContent = v === 'on' ? '目标模式 开 · 0/' + rounds : '目标模式 关';
    // 图标档（≤380px 时文字 font-size:0）里，CSS 靠 [data-on="true"] 上强调色，
    // 这是唯一能看出「现在开着没有」的线索；不回写这个属性，点开也看不出变化。
    btn.setAttribute('data-on', v === 'on' ? 'true' : 'false');
    closePop();
  });

  // 轮数上限改了要真的发出去。原来只在「点开/关」时读一次这个输入框，
  // 于是目标已开的情况下把 10 改成 3：不发请求、按钮还写 0/10、输入框却
  // 留着 3 —— 用户以为上限改了，实际后端还是 10（实测，纯假反馈）。
  document.getElementById('goalRounds').addEventListener('change', function(){
    var n = Math.floor(Number(this.value));
    if (!isFinite(n) || n < 1) {
      // 非法输入退回界面上的真值，不留一个不会生效的数字
      var btn0 = document.getElementById('btnGoal');
      var m0 = btn0.textContent.match(/\/(\d+)$/);
      this.value = m0 ? m0[1] : '10';
      return;
    }
    this.value = String(n);
    var btn = document.getElementById('btnGoal');
    var on = btn.textContent.indexOf('开') >= 0;
    if (!on) return;   // 目标关着：上限存着不生效，等开启时一起提交
    if (REAL) postConfigSet('goal', { enabled: true, maxRounds: n });
    // 按钮上的「当前/上限」要跟着改，否则显示的还是旧上限
    var cur = btn.textContent.match(/·\s*(\d+)\//);
    btn.textContent = '目标模式 开 · ' + (cur ? cur[1] : '0') + '/' + n;
  });

  document.getElementById('popPlugin').addEventListener('click', function(e){
    var pi = e.target.closest('.pi');
    if (!pi) return;
    var sw = pi.querySelector('.sw');
    var on = sw.getAttribute('data-on') === 'true';
    if (REAL) postConfigSet('plugin', { name: pi.getAttribute('data-v'), enabled: !on });
    sw.setAttribute('data-on', on ? 'false' : 'true');
    sw.textContent = on ? 'off' : 'on';
    pi.setAttribute('data-on', on ? 'false' : 'true');
    var total = this.querySelectorAll('.pi').length;
    var enabled = this.querySelectorAll('.pi[data-on="true"]').length;
    document.getElementById('btnPlugin').textContent =
      (enabled === total ? '插件 ' + total : '插件 ' + enabled + '/' + total);
    // 与目标模式同理：图标档里 accent 色是「还有插件开着」的唯一线索（见 CSS 1268-1275）
    document.getElementById('btnPlugin').setAttribute('data-on', enabled > 0 ? 'true' : 'false');
  });

  /* ---------- 会话数据与渲染 ---------- */
  /* 生产态这里必须是空的：会话列表完全由后端 session/list 填写。
     演示数据只活在 design/demo-convo.html → media/narnat.demo.js 里，
     由 preview.html 通过 window.NARNAT_DEMO 注入（vsix 不含该文件）。
     曾经把示例会话写死在这个数组里会怎样（实测）：刚启动点顶栏会话名，
     子会话下拉里列出 try-regex、alt-approach 两条根本不存在的分支。 */
  var DEMO = window.NARNAT_DEMO || null;
  var SESS = (DEMO && DEMO.sessions) || [];
  // 抽屉里默认显示多少个父会话（其余折叠成一行「展开其余 N 条」）
  var VISIBLE_SESSIONS = 6;
  // 后端因截断没给的会话条数（只给计数，不给内容），「展开其余 N 条」要用
  var SESS_HIDDEN = 0;
  var CUR = String((DEMO && DEMO.current) || '');
  // 用户点了「展开」后置 true：真实态发 session/list{all:true}，模拟态直接全显示
  var SHOW_ALL = false;
  /* 哪些父会话的分支在抽屉里展开着（key = 父会话名）。
     只影响列表显示，不改变当前会话，也不需要问后端——children 本来就在
     session/list 里给了。 */
  var EXPANDED = {};
  /* 用户手动收起过的父会话（key = 父会话名）。renderSessions 里「当前在子会话
     就自动展开父会话」的逻辑会看它：手动收起过就不展开。
     为什么需要它：以前自动展开是无条件的，而收起操作是
     `delete EXPANDED[父]; renderSessions()` —— 重绘立刻又置回 true，
     于是在子会话里工作时那条分支永远收不起来（点几次都没反应，连箭头不动）。
     关掉抽屉时清空：换了时点再看，仍希望一眼看到自己在哪。 */
  var MANUAL_COLLAPSED = {};

  function sessMeta(s){
    return s.time || '';
  }

  function itemHtml(fullName, metaText, opts){
    var o = opts || {};
    // data-name 必须转义：会话名里可能有 "，不转义会把属性截断
    //（`a"b` → data-name="a"，该会话从此点不进去），还会让名字里的 HTML 注入进 DOM
    var out = '<div class="dw-item' + (o.sub ? ' dw-sub' : '') +
      '" data-name="' + esc(fullName) + '"' +
      (o.cur ? ' data-cur="true"' : '') +
      (o.ghost ? ' data-ghost="true"' : '') + '>';
    // title 给完整名字：249px 里长名字一律省略号，hover 出来的「改名/删除」
    // 又浮在右端盖住一截，靠原生 tooltip 才能读全（省掉一次点进去再退出来）
    // 子会话条目只显示子会话名本身：缩进已经表明从属关系，
    // 再挂一遍「父/子」全路径在 249px 里只剩几个字。data-name 仍是全路径。
    var shownName = o.sub ? String(fullName).split('/').slice(1).join('/') : fullName;
    out += '<span class="n" data-tip="' + esc(fullName) + '">' + esc(shownName) + '</span>';
    // 有子会话才出现的展开标记：点它展开/收起该会话的分支，不影响「点整行进会话」。
    // 不显示的话（改动前）分支在列表里完全没有入口，用户只能记得敲 /ls --all。
    if (o.branchCount) {
      var open = !!EXPANDED[fullName];
      out += '<span class="br" data-branch="1" data-open="' + (open ? 'true' : 'false') +
        '" data-tip="' + (open ? '收起分支' : '展开 ' + o.branchCount + ' 个分支') + '">' +
        '⑂' + o.branchCount + (open ? '▾' : '▸') + '</span>';
    }
    // ghost：父会话文件已不在，只剩子会话。别让它显示成一个正常空会话。
    out += '<span class="m">' + esc(o.ghost ? '无记录' : metaText) + '</span>';
    // 改名 / 删除不再放在行内（hover 会盖住分支标记），改由右键菜单唤起。
    // 这里只留删除的行内二次确认条：右键「删除」后仍需用户点一次「是」。
    out += '<span class="asks"><span>删除？</span><button class="y">是</button><button>否</button></span>';
    out += '</div>';
    return out;
  }

  function renderSessions(){
    /* 会话列表：一列扁平，仿 Copilot 的历史面板。
       分支（子会话）默认折叠不占位，但**有入口**：有分支的会话名后面挂一个
       「⑂N ▸」标记，点它就地展开该会话的子会话（缩进显示），再点收起。
       完全不给入口会怎样（改动前就是这样）：用户在抽屉里找不到分支，
       只能记得去输入框敲 /ls --all 或 /explore，而界面上没有任何提示。
       当前所在的那条若是子会话，进抽屉时自动把它父会话展开好，
       否则用户会看到「顶栏写着 alpha/beta，列表里却只有 alpha」。 */
    /* 当前在子会话里 → 自动展开它的父会话，除非用户手动收起过这一条。
       判据就是「有没有手动收起过」，不依赖调用顺序：
       renderSessions 的重绘来源很多（轮末自动保存、删除回执、切会话、
       session/list 到达……），任何「只在这个时机做一次」的写法都会因为
       这些时序组合而漏掉自动展开。
       而「手动收起」是一个明确的用户意图，记住它就不会被重绘顶回来。 */
    if (CUR.indexOf('/') >= 0) {
      var _root = CUR.split('/')[0];
      if (!MANUAL_COLLAPSED[_root]) EXPANDED[_root] = true;
    }

    var all = SESS.slice();
    var shown = SHOW_ALL ? all : all.slice(0, VISIBLE_SESSIONS);
    /* 后端默认只把「更早」的 3 条塞进 SESS，其余的都只是 hidden 计数。
       原来 rest 只按 all.length 算，于是「今天没聊 / 聊得少」时 rest 恒为 0，
       「展开其余 N 条」那一行根本不出现 —— 用户抽屉里只看得到 3~5 条，
       剩下几十上百条会话在界面上没有任何入口（实测 today=0 时 97 条进不去）。
       把 hidden 加进来，这一行才会本该出现的时候出现。 */
    var rest = all.length - shown.length + (SHOW_ALL ? 0 : SESS_HIDDEN);
    var html = '';

    shown.forEach(function(s){
      var kids = s.children || [];
      html += itemHtml(s.name, sessMeta(s), {
        cur: s.name === CUR, ghost: !!s.ghost, branchCount: kids.length
      });
      if (kids.length && EXPANDED[s.name]) {
        kids.forEach(function(c){
          var full = s.name + '/' + c.name;
          html += itemHtml(full, c.time || '', {
            cur: full === CUR, sub: true
          });
        });
      }
    });

    if (rest > 0) {
      html += '<div class="dw-more" data-more="1">展开其余 ' + rest + ' 条</div>';
    } else if (SHOW_ALL && all.length > VISIBLE_SESSIONS) {
      html += '<div class="dw-more" data-more="0">收起</div>';
    }
    if (!all.length) {
      // 「读不到」和「真的没有」必须分开说 —— 这是两件完全不同的事。
      // 只在列表为空、且**没有**读取失败时，才能说「还没有已保存的对话」。
      // 读失败时还这么说会怎样（第 25 轮实测）：抽屉里同时挂着
      // 「✗ 读取会话列表失败」和「还没有已保存的对话」，两句话语义相反，
      // 而「还没有对话」是更强的断言 —— 用户会以为**会话全被删了**，
      // 于是不再去排查读取问题，直接开始重建工作。
      html += SESS_LIST_ERROR
        ? '<div class="dw-empty">会话列表暂时读不到（原因见对话区提示）</div>'
        : '<div class="dw-empty">还没有已保存的对话</div>';
    }

    // 重绘会整块换掉 innerHTML。若此刻正有一条「删除？」在等用户点，
    // 那个确认框会被一起冲掉——用户点完删除、正在犹豫，一轮对话恰好结束
    // （轮末会补拉会话列表），确认框无声消失，看起来像「点了没反应」（实测）。
    // 重绘前先记下它在等哪一条，重绘后把 data-del="ask" 补回去。
    var askingName = '';
    var askingEl = document.querySelector('#sessList .dw-item[data-del="ask"]');
    if (askingEl) askingName = askingEl.getAttribute('data-name') || '';

    /* 焦点也要一起保住。重绘换掉 innerHTML 后，原来获得焦点的那个节点
       已经脱离文档 → activeElement 掉回 <body>，键盘用户的位置全丢
       （Tab 只能从页面顶端重来）。恢复「状态」不恢复「焦点」只修了一半。
       记下：焦点在哪条会话行、以及行内哪个按钮（靠 class 定位，不靠索引）。 */
    var focusName = '';
    var focusBtnCls = '';
    var act0 = document.activeElement;
    if (act0 && act0 !== document.body) {
      var focusRow = act0.closest ? act0.closest('#sessList .dw-item') : null;
      if (focusRow) {
        focusName = focusRow.getAttribute('data-name') || '';
        // 是「是/否」这种行内确认按钮，还是删除/改名那种图标按钮
        focusBtnCls = act0.className || '';
      }
    }

    document.getElementById('sessList').innerHTML = html;

    if (askingName) {
      // 不能用属性选择器拼名字：data-name 是转义过的（名字里可能有引号），
      // 直接拼进选择器会对不上。逐个比属性值。
      var items = document.querySelectorAll('#sessList .dw-item');
      for (var ai = 0; ai < items.length; ai++) {
        if (items[ai].getAttribute('data-name') === askingName) {
          items[ai].setAttribute('data-del', 'ask');
          break;
        }
      }
    }
    // 焦点回填：同一行、同一类按钮。找不到就退到抽屉关闭按钮，
    // 总之不要留在 <body> 上（那就等于把键盘用户丢回页面顶端）。
    if (focusName) {
      var rows2 = document.querySelectorAll('#sessList .dw-item');
      var target = null;
      for (var bi = 0; bi < rows2.length; bi++) {
        if (rows2[bi].getAttribute('data-name') === focusName) {
          target = focusBtnCls
            ? rows2[bi].querySelector('.' + focusBtnCls.split(/\s+/)[0])
            : rows2[bi];
          break;
        }
      }
      if (!target) target = document.getElementById('dwClose');
      if (target && typeof target.focus === 'function') {
        try { target.focus(); } catch (e) { /* 忽略 */ }
      }
    }

    // 游离态（尚未 /save）时 CUR 为空：显示「未保存」，不能留着模拟会话名。
    // 文案用「未保存」而不是「(未保存)」：括号在窄面板里很占位，且胶囊边框 +
    // 虚线样式已经足够说明状态（见 .sess[data-unsaved]）。
    var unsaved = !CUR;
    var sessLabel = CUR || (REAL ? '未保存' : '');
    document.querySelector('#sessBtn .branch').textContent = sessLabel;
    // 未保存态交给 CSS：虚线边框 + 次要色，点开下拉也能读到「还未保存」
    var sessBtnBox = document.getElementById('sessBtn');
    if (sessBtnBox) sessBtnBox.setAttribute('data-unsaved', unsaved ? 'true' : 'false');
    // 传给 setChipSess 的必须是**真会话名**（空串 = 未保存）：
    // 把「未保存」当名字传进去，历史按钮的 aria-label 会念成
    // 「当前 未保存」，虽然不通但也别造假数据。
    setChipSess(CUR);
    // 是否是分支：真实态听后端的 isChild（名字里的 / 不代表分支，
    // 否则 /save fix/a/b 这种会话会被误判、把「新建探索分支」禁用掉）；
    // 模拟态没有后端，退回按 / 推断。
    var inChild = REAL ? CUR_IS_CHILD : (CUR.indexOf('/') >= 0);
    // 用 data-on 属性切换而不是 style.display。
    // 技术上网关的 CSSOM 赋值（element.style.x = y）不在 CSP 限制内，能生效；
    // 换成属性选择器是为了把「默认隐藏」这件事写在 CSS 里——JS 没跑到时
    // 也不会让「完成并合并结论」按钮在普通会话里露出来。
    document.getElementById('branchTip').setAttribute('data-on', inChild ? 'true' : 'false');
    var nb = document.getElementById('newBranchBtn');
    nb.disabled = inChild;
    nb.setAttribute('data-off', inChild ? 'true' : 'false');
    // 分支里换成出口按钮：光禁掉「新建分支」不给出口，用户会卡在分支里
    var acts = document.getElementById('branchActs');
    if (acts) acts.setAttribute('data-on', inChild ? 'true' : 'false');
    // 顶栏不再显示「N 个分支」：分支不进列表，这个计数只会让人困惑
    // （「3 个分支」但列表里只有 3 条对话，用户会以为是两回事）
    var spans = document.querySelectorAll('#sessBtn span');
    if (spans[1]) spans[1].textContent = '';
  }

  /* 顶栏「子会话」下拉的内容。
     当前会话可能是根（CUR="alpha"），也可能本身就在分支里（CUR="alpha/beta"）：
     后一种情况该看的是同一个根的兄弟分支，所以一律先取根名再列它的全部子会话，
     并把当前所在的那条标出来。
     条目带完整「父/子」路径——后端 resolve_session_name 认这个写法，
     只写子会话名会找不到（同名子会话可以存在于不同父会话下）。 */
  function renderChildPop(){
    var box = document.getElementById('childList');
    var foot = document.getElementById('childFoot');
    if (!box) return;
    var rootName = String(CUR || '').split('/')[0];
    // 未保存（游离态）：没有会话、更没有子会话，但下拉**照样要能开**——
    // 原来这里直接 return 让按钮点了毫无反应，箭头也不翻，用户以为按钮坏了。
    // 现在明确写一句「还未保存」，顺手把「怎么保存」也告诉用户。
    if (!rootName) {
      box.innerHTML = '<div class="pi" data-empty="true">' +
        '<span class="ck"></span><span class="lb">还未保存</span></div>';
      if (foot) {
        foot.textContent = '这段对话还没存到磁盘：输入 /save 或点抽屉里的「保存当前为…」';
      }
      return;
    }
    var root = SESS.filter(function(s){ return s.name === rootName; })[0];
    var kids = (root && root.children) || [];
    box.innerHTML = '';
    if (!kids.length) {
      var empty = document.createElement('div');
      empty.className = 'pi';
      empty.setAttribute('data-empty', 'true');
      empty.innerHTML = '<span class="ck"></span><span class="lb">无子会话</span>';
      box.appendChild(empty);
      if (foot) foot.textContent = '当前会话没有分支。';
      return;
    }
    kids.forEach(function(c){
      var full = rootName + '/' + c.name;
      var isCur = full === CUR;
      var d = document.createElement('div');
      d.className = 'pi';
      d.setAttribute('data-v', full);
      d.setAttribute('data-on', isCur ? 'true' : 'false');
      // 只显示名字：状态（已完成/待完成）与条数在这么窄的下拉里挤得名字只剩
      // 几个字，而用户点开这个列表的唯一目的就是「找那条会话」。
      d.innerHTML = '<span class="ck">' + (isCur ? '✓' : '') + '</span>' +
        '<span class="lb" data-tip="' + esc(full) + '">' + esc(c.name) + '</span>';
      d.addEventListener('click', function(){
        closePop();
        if (isCur) return;
        enterSess(full, false);
      });
      box.appendChild(d);
    });
    if (foot) {
      foot.textContent = kids.length + ' 个子会话 · 点击进入';
    }
  }

  function findSess(fullName){
    var parts = fullName.split('/');
    var root = SESS.filter(function(s){ return s.name === parts[0]; })[0];
    if (!root) return null;
    if (parts.length === 1) return { root: root, child: null };
    var c = root.children.filter(function(x){ return x.name === parts[1]; })[0];
    return c ? { root: root, child: c } : null;
  }

  function enterSess(fullName, silent){
    if (REAL) {
      // 正在切往同一条会话时，连点不该再发一条。
      // 不拦会怎样（实测 20ms 内连点 2 次 = 2 条 sessionSwitch）：后端每条都要
      // 切上下文、前端每条都要清空重画一次，屏幕上闪两下。
      // 注意：**不能**用 `fullName === CUR` 拦 —— 点当前会话是「重新拉一次这个
      // 会话的历史」的合法操作（界面状态可能与后端不一致，比如刚压缩过），
      // 拦掉会让它永远刷不出来（实测 ui 回归「进入历史会话后显示真实消息」超时）。
      if (pendingSwitchTo === fullName) return true;
      pendingSwitchTo = fullName;
      // 正在回答时点别的会话 = 这一轮会被后端打断（agent_loop 换掉消息列表
      // 前必须先 cancel，否则这一轮的正文和工具记录会串进新会话）。
      // 原来这里什么都不说：用户只是点开另一条会话看一眼，正在生成的那段
      // 就被砍断了，而界面上一个字都没提（实测切换后 1ms 收到 cancelled，
      // 之后 stream/delta 归零）。
      if (body.getAttribute('data-run') === 'true' && fullName !== CUR) {
        sysLine('⚠', '正在回答中，进入「' + fullName + '」会停止这一轮', '', 'warn');
      }
      post('sessionSwitch', { name: fullName });
      return true;
    }
    if (!findSess(fullName)) {
      sysLine('✗', '会话不存在：' + fullName, '');
      return false;
    }
    CUR = fullName;
    renderSessions();
    if (!silent) {
      // 演示态要点明：这条会话里的内容是不存在的示例数据，点进去对话内容
      // 一个字都不会变。原来照样说「已进入会话 · 上下文已切换」，用户会
      // 以为切过去了、内容丢了（同一份演示数据里点 rm -rf 确认条反倒老实
      // 说明「这是示例数据」，处理方式不一致）。
      sysLine('↳', '已切换至对话 ' + fullName, '');
    }
    return true;
  }

  function markDelete(fullName, silent){
    if (REAL) {
      post('sessionDelete', { name: fullName });
      return true;
    }
    var f = findSess(fullName);
    if (!f) return false;
    if (f.child) {
      f.root.children = f.root.children.filter(function(c){ return c !== f.child; });
    } else {
      SESS = SESS.filter(function(s){ return s !== f.root; });
    }
    if (!findSess(CUR) && SESS.length) CUR = SESS[0].name;
    renderSessions();
    // 演示态只是在本地把它从列表里去掉，磁盘上什么都没有。
    // 不能说「退出时生效」——那暗示有个真实的延迟删除在排队（实测反过来：
    // 连上后端后 /rm 是立即删除、不可撤销）。两套语义必须说清楚。
    if (!silent) sysLine('✓', '已移除 ' + fullName, '', 'ok');
    return true;
  }

  function createBranch(n, silent){
    if (REAL) {
      post('sessionExplore', { name: n });
      return true;
    }
    var parts = CUR.split('/');
    if (parts.length > 1) return false;
    var root = SESS.filter(function(s){ return s.name === parts[0]; })[0];
    if (!root) return false;
    if (root.children.some(function(c){ return c.name === n; })) return false;
    root.children.push({ name: n, status: 'active', time: '刚刚', count: 0 });
    CUR = root.name + '/' + n;
    renderSessions();
    if (!silent) sysLine('⑂', '已切换至对话 ' + CUR, '');
    return true;
  }

  /* ---------- 命令执行 ---------- */
  function cmdMulti(cmdText, outHtml, tail, outCls){
    // 命令回执同样是「插入到当前正文之后」的事件，先收口正文（同 cmdOne）
    closeAssistantSegment();
    var d = document.createElement('div');
    d.className = 'cmdwrap';
    // /ls 这类多列输出靠空格对齐（见 padDisp），必须保留 pre；
    // 而 /help 这种「一行一条、每行独立」的输出不需要跨行对齐，
    // 窄面板里应该换行而不是横滚（实测 249px 下 /help 溢出 25px）。
    var cls = 'cmdout' + (outCls ? ' ' + outCls : '');
    d.innerHTML = '<div class="cmdhead"><span class="p">❯</span>' +
      '<span class="ct">' + cmdText + '</span>' +
      (tail ? '<span class="tail">' + tail + '</span>' : '') + '</div>' +
      '<div class="' + cls + '">' + outHtml + '</div>';
    convo.appendChild(d);
    scrollBottom(true);
    return d;
  }

  function cmdResult(cmdText, icon, text, tail, cls){
    closeAssistantSegment();
    var d = document.createElement('div');
    d.className = 'cmdwrap';
    d.innerHTML = '<div class="cmdhead"><span class="p">❯</span>' +
      '<span class="ct">' + cmdText + '</span></div>' +
      '<div class="cmdrow' + (cls ? ' ' + cls : '') + '">' +
      '<span class="ico">' + icon + '</span>' +
      '<span class="tx">' + text + '</span>' +
      (tail ? '<span class="tail">' + tail + '</span>' : '') + '</div>';
    convo.appendChild(d);
    scrollBottom(true);
    return d;
  }

  function cmdOne(icon, text, tail, cls){
    // 系统行要落在「当前这一句正文」之后：不断开的话，后续 stream/delta 仍会
    // 追加进上面那个旧气泡（renderAssistant 只认 curAssistant），屏幕上就变成
    // 「正文（含系统行之后的全部新文字）」→「系统行」——系统行被文字越过去，
    // 用户看到事件提示夹在句子中间。流中断重试就是这条路（先 notice 再续流）。
    closeAssistantSegment();
    var d = document.createElement('div');
    d.className = 'sys' + (cls ? ' ' + cls : '');
    d.innerHTML = '<span class="ico">' + icon + '</span><span>' + text + '</span>' +
      (tail ? '<span class="tail">' + tail + '</span>' : '');
    convo.appendChild(d);
    scrollBottom(true);
    return d;
  }

  function lsText(all){
    // 后端 session/list 早就不再给「今天 / 更早」分组了（只给一列平铺），
    // 前端 applySessionList 也不存 group 字段。仍然按 s.group 过滤会怎样
    //（实测）：today/earlier 恒为空 → /ls 永远显示「(无已保存会话)」，
    // 而旁边的抽屉里明明列着 8 条。有 group 就分组，没有就平铺一段。
    var grouped = SESS.some(function(s){ return !!s.group; });
    var today = grouped ? SESS.filter(function(s){ return s.group === 'today'; }) : [];
    var earlier = grouped ? SESS.filter(function(s){ return s.group === 'earlier'; }) : SESS;
    var L = [];
    // 默认侧边栏只有约 249px 可用宽。完整格式（树 + 名字 + 时间 + 条数 +
    // 当前标记）要 45 个显示宽（约 300px），窄面板里必须横滚才能看到
    // 「◀ 当前」——而那正是用户最先要找的信息。
    // 窄的时候换紧凑格式：树符号简写、名字截断、只留条数，时间让位。
    // 两个宽度下都不横滚，也都能一眼找到当前会话。
    var narrow = convo.clientWidth > 0 && convo.clientWidth < 340;
    var NAME_W = narrow ? 0 : 22;
    function cutDisp(s, w){
      var str = String(s == null ? '' : s);
      if (!w || dispWidth(str) <= w) return str;
      var out = '';
      for (var i = 0; i < str.length; i++) {
        if (dispWidth(out + str[i]) > w - 1) break;
        out += str[i];
      }
      return out + '…';
    }
    function tree(list, label, rest, totalOverride){
      var total = (typeof totalOverride === 'number') ? totalOverride : list.length;
      L.push('  <span class="dim">' + label + ' (' + total + ')</span>');
      list.forEach(function(s, i){
        var lastRoot = i === list.length - 1 && !rest;
        var marks = s.name === CUR ? ' <span class="ok">◀ 当前</span>' : '';
        if (narrow) {
          var conn2 = lastRoot ? '└ ' : '├ ';
          // 紧凑分支也要保留「这一项有没有子会话、子会话什么状态」这两个
          // 关键信息的一点点线索：用 ✓/⚠ 标记 + 条数。时间在这种宽度下
          // 确实放不下（实测 249px 只剩 45 显示宽），但「有没有待完成的分支」
          // 是用户要判断「能不能直接关掉」的依据，不能一起丢掉。
          var subMark = '';
          if (s.children && s.children.length) {
            var hasPending = s.children.some(function(c){ return c.status !== 'completed' && c.status !== 'new'; });
            subMark = hasPending ? ' <span class="warn">⚠</span>' : ' <span class="ok">✓</span>';
          }
          L.push('  <span class="dim">' + conn2 + '</span>' +
            '<span class="hl">' + esc(cutDisp(s.name, 13)) + '</span>' + marks +
            ' <span class="dim">' + s.count + '条</span>' + subMark);
          s.children.forEach(function(c, j){
            var cl2 = j === s.children.length - 1 ? '└ ' : '├ ';
            var cst = c.status === 'completed' ? ' <span class="ok">✓</span>'
              : (c.status === 'new' ? '' : ' <span class="warn">⚠</span>');
            L.push('    <span class="dim">' + cl2 + '</span>' +
              esc(cutDisp(c.name, 11)) + cst +
              (s.name + '/' + c.name === CUR ? ' <span class="ok">◀ 当前</span>' : ''));
          });
        } else {
          var conn = lastRoot ? '└── ' : '├── ';
          // 超长名字（>NAME_W）必须截断：不截会怎样（实测）——一个 100 字符的
          // 会话名让整块输出溢出 410px，面板越宽反而越糟；而它后面跟着的
          // 时间/条数/「◀ 当前」全被推到横向滚动之外。
          var sname = cutDisp(s.name, NAME_W);
          L.push('  <span class="dim">' + conn + '</span>' +
            '<span class="hl col22">' + esc(sname) + '</span>' +
            '<span class="dim">(' + esc(s.time) + ', ' + s.count + '条)</span>' + marks);
          var pre = lastRoot ? '    ' : '│   ';
          s.children.forEach(function(c, j){
            var cl = j === s.children.length - 1 ? '└── ' : '├── ';
            var st;
            if (c.status === 'completed') st = '<span class="ok">✓ 已完成 ' + esc(c.time) + '</span>';
            else if (c.status === 'new') st = '<span class="dim">(' + esc(c.time) + ', ' + (c.count || 0) + '条)</span>';
            else st = '<span class="warn">⚠ 待完成 ' + esc(c.time) + ', ' + (c.count || 0) + '条</span>';
            var cname = cutDisp(c.name, Math.max(8, NAME_W - 4));
            L.push('  <span class="dim">' + pre + cl + '</span>' +
              '<span class="hl col18">' + esc(cname) + '</span>' + st +
              (s.name + '/' + c.name === CUR ? ' <span class="ok">◀ 当前</span>' : ''));
          });
        }
      });
      if (rest) {
        L.push('  <span class="dim">… 还有 ' + rest + ' 条（/ls --all 查看）</span>');
      }
    }
    if (grouped) {
      if (today.length) tree(today, '今天', 0);
      if (earlier.length) { if (today.length) L.push(''); tree(earlier, '更早', all ? 0 : VISIBLE_SESSIONS); }
    } else {
      // 平铺：不带 --all 时只列首屏那几条，剩下的用一行提示带过
      var shown = all ? earlier : earlier.slice(0, VISIBLE_SESSIONS);
      var rest = earlier.length - shown.length;
      // 标题里的数字必须是「会话总数」，不是「这一屏列了几条」。
      // 原来传 shown.length：500 个会话时标题写「已保存 (6)」，与事实不符，
      // 用户会以为只有 6 个（实测）。
      if (shown.length) tree(shown, '已保存', rest, earlier.length);
    }
    if (!today.length && !earlier.length) return '  <span class="dim">(无已保存会话)</span>';
    return L.join('\n');
  }

  /**
   * 一个 CJK 字符相当于几个英文格。
   *
   * 原来是写死的 2 格。但等宽字体回退到系统字体后，CJK 的实际宽度不是
   * 英文的 2 倍——实测 11.5px 字号下英文 6.75px、CJK 11.5px，比例 1.70。
   * 按 2 格补齐会让含汉字多的名字多补几个空格，同一列的时间/条数就错开
   * （实测三行时间列 x 相差 5.1px，肉眼能看出不齐）。
   * 用 canvas 量一次真实比例，后面按它算。
   */
  var CJK_RATIO = 0;
  function cjkRatio(){
    if (CJK_RATIO) return CJK_RATIO;
    try {
      var cv = document.createElement('canvas');
      var ctx = cv.getContext('2d');
      // 与 .cmdout 的字体保持一致（font-size 11.5px + --mono 的回退链）
      ctx.font = '11.5px ui-monospace, "Cascadia Mono", Consolas, "Courier New", monospace';
      var en = ctx.measureText('x').width;
      var zh = ctx.measureText('中').width;
      CJK_RATIO = (en > 0 && zh > 0) ? (zh / en) : 2;
      // 只在合理区间内采信；量不到就退回 2
      if (!(CJK_RATIO >= 1 && CJK_RATIO <= 2.5)) CJK_RATIO = 2;
    } catch (err) {
      CJK_RATIO = 2;
    }
    return CJK_RATIO;
  }

  function dispWidth(s){
    var k = cjkRatio();
    var width = 0;
    var str = String(s == null ? '' : s);
    for (var i = 0; i < str.length; i++) {
      var code = str.charCodeAt(i);
      // CJK 统一表意文字、全角标点、假名、谚文：按实测比例，而不是固定 2 格
      width += (code >= 0x2e80 && code <= 0x9fff) ||
               (code >= 0xac00 && code <= 0xd7af) ||
               (code >= 0xf900 && code <= 0xfaff) ||
               (code >= 0xff00 && code <= 0xff60) ? k : 1;
    }
    return width;
  }

  /**
   * 把一段文本补齐到指定「显示宽度」。
   * 超长的名字不截断（截断会丢信息），只吐一个空格当分隔。
   * @param {string} s 原文（用来量宽度）
   * @param {number} w 目标显示宽度
   */
  function padDisp(s, w){
    var width = dispWidth(s);
    if (width >= w) return ' ';
    // 宽度可能是小数（CJK 比例 1.70），四舍五入到整数个空格
    return ' '.repeat(Math.max(0, Math.round(w - width)));
  }

  function runCommand(raw){
    var s = raw.trim();
    var parts = s.slice(1).split(/\s+/);
    var cmd = parts[0].toLowerCase();
    var arg = parts.slice(1).join(' ');

    var CMD = '/' + cmd + (arg ? ' ' + arg : '');

    if (cmd === 'help' || cmd === '?' || cmd === 'h') {
      // 模拟态（后端还没连上、界面跑演示数据）也要能查命令。
      // 不处理会落到最后的「未知命令」，用户敲 /help 得到「没有这个命令」——
      // 而后端其实是支持的，只是这条本地分支没跟上（实测）。
      // 一条命令一行：原来把三条挤在一行（用空格对齐），默认 249px 面板里
      // 整块宽 1380px，要横向拖 4 屏才看得完（实测）。
      var helps = [
        // 描述一律短到能在一行里放下：249px 的可用宽只有 213px，
        // 实测「列出会话（--all 看全部）」这一行 238.3px 会折成两行，
        // 而折出来的续行起点（x=28）与下一行命令名完全相同——
        // 17 行的表看上去像 18 行，多出来那行像一条"没有名字的命令"。
        ['/ls', '会话列表（--all）'], ['/cd', '切换会话'], ['/rm', '删除会话'],
        ['/save', '保存当前对话'], ['/explore', '开探索分支'], ['/done', '合并分支结论'],
        ['/exit', '暂离分支'], ['/mode', '切模型'], ['/thinking', '思考强度'],
        ['/thinkback', '思考回传开关'], ['/goal', '目标模式'], ['/plugin', '插件开关'],
        ['/compact', '压缩上下文'], ['/skill', '加载技能'], ['/clear', '清屏（不删上下文）'],
        ['/help', '这张表']
      ];
      var rows = ['  <span class="dim">可用命令</span>'];
      helps.forEach(function(h){
        rows.push('  <span class="hl">' + h[0] + '</span>' + padDisp(h[0], 12) +
          '<span class="dim">' + h[1] + '</span>');
      });
      cmdMulti(CMD, rows.join('\n'), '输入 / 也有补全面板', 'cmdout--wrap');
      return;
    }
    if (cmd === 'ls') {
      // /ls --all 的承诺是「看全部」，但 SESS 里只有后端推过来的那部分：
      // 后端对较早的分组只给 3 个父会话 + hidden 计数（config/session_store.py
      // 的 session_tree_payload）。原来这里完全不向后端要数据，于是
      // `/ls --all` 与 `/ls` 逐字相同、标题还写着「已保存 (3)」——真实 8 条，
      // 而同屏抽屉里正写着「展开其余 5 条」，两个结论互相矛盾（实测）。
      // 抽屉早就有现成通路（post sessionList all:true），这里复用。
      var wantAll = String(arg || '').trim() === '--all';
      if (wantAll && REAL && SESS_HIDDEN > 0) {
        post('sessionList', { all: true });
      }
      var t1 = SESS.filter(function(s){ return s.group === 'today'; }).length;
      var t2 = SESS.filter(function(s){ return s.group === 'earlier'; }).length;
      var tail = SESS.some(function(s){ return !!s.group; })
        ? '今天 ' + t1 + ' · 更早 ' + t2
        : SESS.length + ' 个会话';
      // 还有没加载进来的会话时必须说出来：数字小是后端截断，不是真的只有这些
      if (SESS_HIDDEN > 0) {
        tail += ' · 还有 ' + SESS_HIDDEN + ' 条未加载' + (wantAll ? '（正在拉取…）' : '，用 /ls --all 查看');
      }
      cmdMulti(CMD, lsText(wantAll), tail);
      return;
    }
    if (cmd === 'cd') {
      if (!arg) {
        cmdResult(CMD, '✗', '用法: /cd &lt;名称&gt;', '当前 ' + CUR, 'err');
        return;
      }
      if (!findSess(arg)) {
        cmdResult(CMD, '✗', '会话不存在: ' + arg, '', 'err');
        return;
      }
      enterSess(arg, true);
      cmdResult(CMD, '↳', '已进入会话：' + arg, '上下文已切换');
      return;
    }
    if (cmd === 'rm') {
      if (!arg) {
        cmdResult(CMD, '✗', '用法: /rm &lt;名称 | --all&gt;', '', 'err');
        return;
      }
      if (!markDelete(arg, true)) {
        cmdResult(CMD, '✗', '会话不存在: ' + arg, '', 'err');
        return;
      }
      cmdResult(CMD, '✓', '已移除 ' + arg, '', 'ok');
      return;
    }
    if (cmd === 'save') {
      // 演示态没有后端，这里只动界面；回执不能写「会话已保存」，
      // 否则用户以为真存盘了（实测：后端没连上时敲 /save 会得到"已保存"）。
      cmdResult(CMD, '✓', '已保存为「' + (arg || CUR) + '」',
        (REAL ? '' : '仅界面生效'), 'ok');
      return;
    }
    if (cmd === 'explore') {
      if (!arg) {
        cmdResult(CMD, '✗', '用法: /explore &lt;名称&gt;', '', 'err');
        return;
      }
      if (!createBranch(arg, true)) {
        cmdResult(CMD, '✗', '创建分支失败', '', 'err');
        return;
      }
      cmdResult(CMD, '⑂', '已切换至对话 ' + CUR, '');
      return;
    }
    if (cmd === 'done') {
      if (CUR.indexOf('/') < 0) {
        cmdResult(CMD, '✗', '当前不在探索分支中', '', 'err');
        return;
      }
      cmdResult(CMD, '✓', '已切换至对话 ' + CUR.split('/')[0], '', 'ok');
      return;
    }
    if (cmd === 'mode') {
      // 可用模型列表由 initialize 的 options 重建，这里必须读真实列表，
      // 不能写死——写死会在后端换模型后给出错误提示。
      var models = Array.prototype.map.call(
        document.querySelectorAll('#popModel .pi'),
        function(pi){ return pi.getAttribute('data-v'); });
      if (!arg) {
        cmdMulti(CMD, [
          '<span class="dim">当前</span> <span class="hl">' +
            document.getElementById('btnModel').textContent + '</span>',
          '<span class="dim">可用</span> ' + (models.length ? models.join(' / ') : '（未收到模型列表）')
        ].join('\n'), '', 'cmdout--wrap');
        return;
      }
      if (models.indexOf(arg) < 0) {
        cmdResult(CMD, '✗', '无效值: ' + arg,
          '可用 ' + (models.length ? models.join(' / ') : '（未收到模型列表）'), 'err');
        return;
      }
      setChecked(document.getElementById('popModel'), arg);
      document.getElementById('btnModel').textContent = arg;
      cmdResult(CMD, '✓', '已切换模型 ' + arg, '', 'ok');
      return;
    }
    if (cmd === 'thinking') {
      // 强度档位同样由 initialize 的 options 重建（不同后端可能是 high/max，
      // 也可能是 low/medium/high）。读真实列表，别写死。
      var levels = Array.prototype.map.call(
        document.querySelectorAll('#popThink .pi'),
        function(pi){
          return { v: pi.getAttribute('data-v'), lb: pi.querySelector('.lb').textContent };
        });
      if (!arg) {
        var real = levels.length
          ? '可用 ' + levels.map(function(x){ return x.lb; }).join(' / ')
          : '（未收到强度列表）';
        cmdResult(CMD, '·', '当前思考强度：' +
          document.getElementById('btnThink').textContent, real);
        return;
      }
      var want = arg.toLowerCase();
      var pick = null;
      for (var i = 0; i < levels.length; i++) {
        if (levels[i].v.toLowerCase() === want || levels[i].lb === arg) {
          pick = levels[i];
          break;
        }
      }
      if (!pick) {
        cmdResult(CMD, '✗', '无效值: ' + arg,
          levels.length ? '可用 ' + levels.map(function(x){ return x.lb; }).join(' / ') : '（未收到强度列表）',
          'err');
        return;
      }
      setChecked(document.getElementById('popThink'), pick.v);
      document.getElementById('btnThink').textContent = pick.lb;
      cmdResult(CMD, '✓', '思考强度 ' + pick.lb, '', 'ok');
      return;
    }
    if (cmd === 'goal') {
      var g = arg.split(/\s+/)[0];
      if (g === 'on' || g === 'off') {
        var rounds = arg.split(/\s+/)[1] || document.getElementById('goalRounds').value || '10';
        setChecked(document.getElementById('popGoal'), g);
        var btn = document.getElementById('btnGoal');
        btn.textContent = g === 'on' ? '目标模式 开 · 0/' + rounds : '目标模式 关';
        if (g === 'on') {
          cmdResult(CMD, '✓', '目标模式已开启', '轮数上限 ' + rounds, 'ok');
        } else {
          cmdResult(CMD, '✓', '目标模式已关闭', '', 'ok');
        }
        return;
      }
      var onNow = document.getElementById('btnGoal').textContent.indexOf('开') >= 0;
      cmdResult(CMD, '·', '目标模式' + (onNow ? '已开启' : '已关闭'), '');
      return;
    }
    if (cmd === 'plugin') {
      if (!arg) {
        var pis = document.querySelectorAll('#popPlugin .pi');
        var rows = [];
        Array.prototype.forEach.call(pis, function(pi){
          var on = pi.getAttribute('data-on') === 'true';
          rows.push('<span class="hl">' + pi.getAttribute('data-v').padEnd(12) + '</span>' +
            (on ? '<span class="ok">on </span>' : '<span class="dim">off</span>') +
            '  <span class="dim">' + pi.querySelector('.lb').textContent + '</span>');
        });
        var enabled0 = document.querySelectorAll('#popPlugin .pi[data-on="true"]').length;
        cmdMulti(CMD, rows.join('\n'), enabled0 + '/' + pis.length + ' 开启', 'cmdout--wrap');
        return;
      }
      var pp = arg.split(/\s+/);
      // 按 display 名逐个 toLowerCase 比较，**不能拼进 CSS 选择器**：
      // `.pi` 的 data-v 是原始大小写（Terminal / WebSearch），用户敲 `terminal`
      // 时拼出来的 '#popPlugin .pi[data-v="terminal"]' 一个都匹配不到，
      // 于是「无效插件名」（第 7 轮在 syncUiAfterCommand 里已经踩过同一个坑）。
      var target = null;
      Array.prototype.some.call(
        document.querySelectorAll('#popPlugin .pi'),
        function (pi) {
          if (String(pi.getAttribute('data-v') || '').toLowerCase() === pp[0].toLowerCase()) {
            target = pi;
            return true;
          }
          return false;
        });
      if (!target) {
        cmdResult(CMD, '✗', '无效插件名: ' + pp[0],
          '可用 terminal / serial / mcp / web_search', 'err');
        return;
      }
      var want = (pp[1] || '').toLowerCase();
      if (want !== 'on' && want !== 'off') {
        cmdResult(CMD, '✗', '用法: /plugin &lt;名称&gt; &lt;on|off&gt;', '', 'err');
        return;
      }
      var sw = target.querySelector('.sw');
      var isOn = want === 'on';
      sw.setAttribute('data-on', isOn ? 'true' : 'false');
      sw.textContent = isOn ? 'on' : 'off';
      target.setAttribute('data-on', isOn ? 'true' : 'false');
      var tot = document.querySelectorAll('#popPlugin .pi').length;
      var en = document.querySelectorAll('#popPlugin .pi[data-on="true"]').length;
      document.getElementById('btnPlugin').textContent =
        (en === tot ? '插件 ' + tot : '插件 ' + en + '/' + tot);
      // 顶栏按钮的高亮（data-on）必须跟着一起写：只写文字会怎样（实测）——
      // 下拉项已经被本分支改成了 off，顶栏却仍带 data-on="true" 的强调色，
      // 同一屏上「顶栏说开着、下拉说关了」。这是「同属性多处写、漏一处同步」
      // 这一类缺陷的又一处（前 9 轮修过 4 处，这条在演示态可达）。
      document.getElementById('btnPlugin').setAttribute('data-on', en > 0 ? 'true' : 'false');
      cmdResult(CMD, '✓', '插件' + (isOn ? '开' : '关') + ' ' + pp[0], '', 'ok');
      return;
    }
    if (cmd === 'compact') {
      document.getElementById('ctxPct').textContent = '3%';
      // 同 compress/end：真实回执里 before/after 恒为 null，演示态也不该编数字
      cmdResult(CMD, '✓', '压缩完成', '仅界面', 'ok');
      return;
    }
    if (cmd === 'skill') {
      if (!arg) {
        cmdResult(CMD, '✗', '用法: /skill &lt;名称 | 项目目录/文件.md&gt;', '', 'err');
        return;
      }
      cmdResult(CMD, '✓', '已加载技能 ' + arg, '', 'ok');
      return;
    }
    if (cmd === 'thinkback') {
      cmdResult(CMD, '·', '思考回传：开', '');
      return;
    }
    if (cmd === 'clear' || cmd === 'exit') {
      cmdResult(CMD, '·', 'UI 形态下不用此命令', '');
      return;
    }
    cmdResult('/' + cmd, '✗', '没有这个命令：/' + cmd, '', 'err');
  }

  /* ---------- 会话抽屉 ---------- */
  var sysLine = cmdOne;

  document.getElementById('sessList').addEventListener('click', function(e){
    // 展开 / 收起：必须在 .dw-item 判断之前，它是独立的一行
    var more = e.target.closest('.dw-more');
    if (more) {
      // 已经有请求在飞：再点一次也只是重复发。回执到达前按钮文字不变，
      // 用户会以为没点上而连点（实测点击后条目数与文案都不动）。
      if (more.getAttribute('data-pending') === 'true') return;
      var wantAll = more.getAttribute('data-more') === '1';
      if (REAL) {
        // 离线时**一个字节都不改**，连 SHOW_ALL 也不写。
        // 只置 pending 不够：SHOW_ALL 原来写在判断离线**之前**，于是
        // 界面明确告诉用户「未连接后端，操作未执行」，可状态已经变成"显示全部"了 ——
        // 之后任何一次纯本地重绘（关一次抽屉、重绘会话列表）就会把全部会话摊开，
        // 重连后也保持全展开（实测）。说了没执行，却执行了一半。
        if (body.getAttribute('data-offline') === 'true') {
          post('sessionList', { all: wantAll });   // 交给 post() 给离线提示
          return;
        }
        SHOW_ALL = wantAll;
        // 真实态：让后端给全量列表，回来时 applySessionList 会按 SHOW_ALL 渲染
        more.setAttribute('data-pending', 'true');
        more.textContent = wantAll ? '正在加载全部会话…' : '正在收起…';
        post('sessionList', { all: wantAll });
      } else {
        // 模拟态数据本来就在本地，直接重画
        SHOW_ALL = wantAll;
        renderSessions();
      }
      rendered('session/more', { all: wantAll });
      return;
    }
    var item = e.target.closest('.dw-item');
    if (!item) return;
    var full = item.getAttribute('data-name');

    // 分支展开/收起：优先于整行点击判断，否则点标记会顺带进会话。
    // 只改本地折叠状态并重绘，不发请求——子会话数据已经在 session/list 里。
    var br = e.target.closest('.br');
    if (br) {
      e.stopPropagation();
      if (EXPANDED[full]) {
        delete EXPANDED[full];
        // 记下「这个父会话是我手动收起的」，让 renderSessions 的
        // 「进抽屉自动展开我在的分支」不再把它顶回来。
        // 不记会怎样（实测）：当前就在这条分支里工作时，点收起无效
        //（每次重绘都被自动展开逻辑置回 true），连箭头都不动。
        MANUAL_COLLAPSED[full] = true;
      } else {
        EXPANDED[full] = true;
        delete MANUAL_COLLAPSED[full];
      }
      renderSessions();
      rendered('session/branchToggle', { name: full, open: !!EXPANDED[full] });
      return;
    }

    // 这一条已经被点过一次（删除已提交 / 正在等回执）：整条不再响应任何点击。
    // 不锁会怎样（实测）：连点「删除」→「是」的第二下，同坐标已经变成
    // 该条目的「改名」按钮，于是弹出 4 个改名输入框（宿主 showInputBox 是
    // 模态的，会真的排队弹 4 次），而会话其实已经删掉了。
    if (isSessLocked(full)) return;

    if (e.target.closest('.asks')) {
      if (e.target.classList.contains('y')) {
        item.setAttribute('data-del', '');
        // 先锁住整条再提交：同坐标下一击不能再触发任何按钮
        lockSess(full, 'delete');
        markDelete(full);
      } else {
        item.setAttribute('data-del', '');
      }
      return;
    }
    enterSess(full);
  });

  /* ---------- 会话行的右键菜单（改名 / 删除） ----------
     入口换成右键，解决两个问题：
     ① hover 出来的两个按钮绝对定位在行尾，正好盖住分支标记「⑂2 ▸」，
        用户点不到它（那是列表里唯一的展开入口）；
     ② 鼠标扫过列表时按钮逐个跳出，视觉噪音大。
     右键的目标行记在 menuTarget 里：菜单本身是独立节点，点击时靠它认人。 */
  var sessMenuEl = document.getElementById('sessMenu');
  var menuTarget = '';

  function closeSessMenu(){
    if (!sessMenuEl) return;
    sessMenuEl.setAttribute('data-open', 'false');
    menuTarget = '';
  }

  function openSessMenu(x, y, full){
    if (!sessMenuEl) return;
    // 与下拉同一族：打开前先收起命令面板和其它小浮层（否则同屏会点击穿透）
    dismissOtherOverlays();
    menuTarget = full;
    // 先显示再量尺寸：display:none 时 offsetWidth 是 0，夹不出正确位置
    sessMenuEl.setAttribute('data-open', 'true');
    var w = sessMenuEl.offsetWidth;
    var h = sessMenuEl.offsetHeight;
    // 贴边时往内夹，避免菜单跑出面板（249px 下很容易超右边界）
    var left = Math.max(2, Math.min(x, window.innerWidth - w - 6));
    var top = Math.max(2, Math.min(y, window.innerHeight - h - 6));
    sessMenuEl.style.left = left + 'px';
    sessMenuEl.style.top = top + 'px';
  }

  document.getElementById('sessList').addEventListener('contextmenu', function(e){
    var item = e.target.closest('.dw-item');
    if (!item) return;
    // 屏蔽宿主/浏览器自带的右键菜单，否则会盖在我们的菜单上
    e.preventDefault();
    e.stopPropagation();
    openSessMenu(e.clientX, e.clientY, item.getAttribute('data-name') || '');
  });

  if (sessMenuEl) {
    sessMenuEl.addEventListener('click', function(e){
      var it = e.target.closest('.ctx-item');
      if (!it) return;
      var act = it.getAttribute('data-act');
      var full = menuTarget;
      closeSessMenu();
      if (!full) return;
      if (act === 'rename') {
        doRename(full);
        return;
      }
      // 删除仍走「行内二次确认」那条既有路径：
      // 直接把 data-del="ask" 打回该行，用户可以确认也可以反悔，
      // 比右键菜单里再弹一层确认更省事，也复用了已经验证过的删除逻辑。
      var row = null;
      var rows = document.querySelectorAll('#sessList .dw-item');
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute('data-name') === full) { row = rows[i]; break; }
      }
      if (row) row.setAttribute('data-del', 'ask');
    });
  }

  /* 点别处 / 按 Esc / 滚列表 都关掉右键菜单。
     不关会怎样：菜单留着盖在列表上，下一次点行会点进菜单而不是进会话。 */
  document.addEventListener('click', function(e){
    if (!sessMenuEl || sessMenuEl.getAttribute('data-open') !== 'true') return;
    if (e.target.closest('#sessMenu')) return;
    closeSessMenu();
  });
  document.addEventListener('contextmenu', function(e){
    if (!sessMenuEl || sessMenuEl.getAttribute('data-open') !== 'true') return;
    if (e.target.closest('#sessList') || e.target.closest('#sessMenu')) return;
    closeSessMenu();
  });
  document.getElementById('sessList').addEventListener('scroll', closeSessMenu);

  /* ---------- 原生输入框（保存为 / 新建分支 / 重命名 共用） ---------- */
  var newSessBtn = document.getElementById('newSessBtn');
  var newBranchBtn = document.getElementById('newBranchBtn');

  /**
   * 向用户要一段文本：交给 VSCode 原生输入框，而不是在这里自绘一个
   * 挤在抽屉底部的 input（面板一窄就难看，还要自己处理确定/取消/校验）。
   * 返回 Promise<string|null>，null 表示用户取消。
   */
  var promptSeq = 0;
  var promptWaiters = {};

  function askText(opts){
    var o = opts || {};
    return new Promise(function(resolve){
      if (!REAL) {
        // 模拟态（浏览器预览）没有扩展可调用，退回原生 prompt
        var v = window.prompt(o.title || '', o.value || '');
        resolve(v === null || !String(v).trim() ? null : String(v).trim());
        return;
      }
      // 同一时间只允许一个输入框。宿主的 showInputBox 是**模态**的，连点
      // 「保存当前为…」5 次会真的排队弹 5 个框（实测 6 个），用户得连点 5 次
      // 取消才能回到界面。已经有一个在等就直接当「这次没点」（静默，不刷提示，
      // 屏幕上本来就摆着那个模态框）。
      if (Object.keys(promptWaiters).length > 0) {
        resolve(null);
        return;
      }
      promptSeq += 1;
      var id = promptSeq;
      // 30 秒兜底：扩展那边 showInputBox 是模态的，正常情况下一定会回执。
      // 但面板若在等输入时被销毁（用户关了视图、重载窗口），回执就永远不来，
      // 这个 Promise 会一直挂着——调用方（保存/分支/改名按钮）的「正在保存…」
      // 也永远不解锁，用户只能关窗口（实测）。
      var timer = setTimeout(function(){
        if (promptWaiters[id]) {
          delete promptWaiters[id];
          resolve(null);
        }
      }, 30000);
      if (timer.unref) timer.unref();
      promptWaiters[id] = function(v){ clearTimeout(timer); resolve(v); };
      post('prompt', {
        id: id,
        title: o.title || '',
        placeholder: o.placeholder || '',
        value: o.value || '',
        prompt: o.prompt || '',
        // requireValue：这次调用是否必须有值（分支名要，会话名可由 AI 自动命名）
        requireValue: !!o.requireValue,
        validate: o.validate || ''
      });
    });
  }

  function onPromptResult(p){
    var id = p && p.id;
    var done = promptWaiters[id];
    if (!done) {
      // 兜底已超时（30s）或面板换过一轮：这个回执没人等了。
      // 不能完全静默——用户明明在输入框里敲了字按了 Enter，屏幕上却
      // 一点反应都没有（实测：停留超过 30 秒再按 Enter，什么都不发生）。
      if (p && !p.cancelled && String(p.value || '').trim()) {
        sysLine('·', '输入已超时，请重新操作', '');
        scrollBottom();
      }
      return;
    }
    delete promptWaiters[id];
    // 区分「取消」与「留空确认」：取消 → null，留空确认 → ''（空串）。
    // 原来把空值一律折成 null 会怎样（实测）：「保存当前对话」的输入框
    // placeholder 写着「留空则由 AI 自动命名」，用户真的留空按 Enter，
    // 却什么都不发生（调用方看到 null 直接 return），零反馈。
    // 留空是否合法由调用方决定（分支名必须非空，会话名可交给 AI）。
    if (!p || p.cancelled) {
      done(null);
      return;
    }
    done(String(p.value === undefined || p.value === null ? '' : p.value).trim());
  }

  newSessBtn.addEventListener('click', function(){
    askText({
      title: '保存当前对话',
      prompt: '起个名字，之后可从会话列表进入',
      placeholder: '留空则由 AI 自动命名',
      value: ''
    }).then(function(n){
      if (n === null) return;             // 取消：不保存
      // 允许留空走 AI 自动命名：原生输入框要求非空，所以留空时这里改发无参 /save
      if (REAL) {
        post('command', { command: 'save', args: n || '', text: '/save' + (n ? ' ' + n : ''), origin: 'ui' });
        return;
      }
      var nm = n || ('session-' + (SESS.length + 1));
      if (SESS.some(function(s){ return s.name === nm; })) {
        sysLine('✗', '会话已存在：' + nm, '');
        return;
      }
      SESS.unshift({ name: nm, time: '刚刚', count: 0, children: [] });
      CUR = nm;
      renderSessions();
      sysLine('＋', '已切换至对话 ' + nm, '');
    });
  });

  newBranchBtn.addEventListener('click', function(){
    if (this.disabled) return;
    askText({
      title: '新建探索分支',
      prompt: '分支用于并行试一个思路，/done 后结论合并回当前会话',
      placeholder: '分支名称',
      value: '',
      // 分支必须有名（不像保存会话可以交给 AI 自动命名）
      requireValue: true
    }).then(function(n){
      // null = 取消；'' = 留空确认。分支名必须非空（不像保存会话能交给 AI 命名），
      // 所以留空时要明确告诉用户原因，不能静默 return。
      if (n === null) return;
      if (!n) {
        sysLine('·', '分支名不能为空', '');
        return;
      }
      if (!createBranch(n)) {
        sysLine('✗', '创建分支失败', '');
      }
    });
  });

  // 分支出口。两个按钮都要防重：/done 会调一次 LLM 总结（几秒），
  // 期间连点会发出多个 session/done，第二个必然报「没有新的讨论内容」，
  // 用户会以为自己点坏了。
  (function(){
    var doneBtn = document.getElementById('doneBranchBtn');
    var exitBtn = document.getElementById('exitBranchBtn');
    var busy = false;
    var timer = null;

    function run(btn, label, fn){
      if (busy) return;
      busy = true;
      var old = btn.textContent;
      btn.disabled = true;
      btn.textContent = label;
      fn();
      // 无论后端成败都要解锁：回执没来时靠定时兜底，
      // 否则按钮永久卡在「处理中」——比连点更糟（只能重载窗口）。
      // timer 必须自己清：不清的话，用户第二次点按钮后，第一次留下的
      // 闹钟到点会把**新的**那一次提前解锁（按钮在请求还在飞时就活了）。
      var unlock = window.__narnatBranchUnlock = function(){
        if (timer) { clearTimeout(timer); timer = null; }
        busy = false;
        btn.disabled = false;
        btn.textContent = old;
        window.__narnatBranchUnlock = null;
      };
      timer = setTimeout(function(){
        timer = null;
        if (window.__narnatBranchUnlock) window.__narnatBranchUnlock();
      }, 30000);
    }

    if (doneBtn) doneBtn.addEventListener('click', function(){
      // 合并不可逆：后端会把分支结论并进父会话、父会话上下文被替换、
      // 这条分支从列表里消失，界面上没有任何回退入口。
      // 两个按钮并排放在抽屉底部，误点一下就把活干完了 —— 而抽屉里
      // 删一条会话都要问「删除？」。同一份确认能力，这里也必须问。
      if (doneBtn.getAttribute('data-ask') === '1') {
        doneBtn.setAttribute('data-ask', '0');
        doneBtn.textContent = '✓ 完成并合并结论';
        /* 真正执行 */
        if (REAL) {
          run(doneBtn, '正在总结…', function(){ post('sessionDone', {}); });
        } else {
          CUR = CUR.split('/')[0];
          CUR_IS_CHILD = false;
          renderSessions();
          sysLine('✓', '已切换至对话 ' + CUR, '');
        }
        return;
      }
      doneBtn.setAttribute('data-ask', '1');
      doneBtn.textContent = '确认合并？再点一次（不可撤销）';
      var askLine = sysLine('⚠', '合并不可撤销，再点一次才执行', '');
      scrollBottom();
      // 8 秒内没再点就撤销「待确认」状态，避免按钮长期停在确认态。
      // 撤销时必须把上面那条提示一起改掉：否则屏幕上一直挂着「再点一次才执行」，
      // 而按钮已经回到初始文案，用户照提示再点一次只重新进入确认态（什么也没发生）。
      var askTimer = setTimeout(function(){
        if (doneBtn.getAttribute('data-ask') === '1') {
          doneBtn.setAttribute('data-ask', '0');
          doneBtn.textContent = '✓ 完成并合并结论';
          if (askLine && askLine.parentNode) {
            // 直接重建整行，不要去找 .tail：上面 sysLine 的第三个参数是空串，
            // cmdOne 只在 tail 非空时才生成 .tail 节点，所以 querySelector('.tail')
            // 恒为 null —— 原来那句 `if (tail) tail.textContent = ...` 永远不执行，
            // 屏幕上就留着一句「再点一次才执行」，而按钮已回到初始文案，
            // 用户按提示再点一次只会重新进入确认态，什么也没发生（实测）。
            // 主文本也必须一起换掉：只改 tail 会得到「…再点一次才执行 这次没有合并」
            // 这种自相矛盾的一行。
            askLine.className = 'sys';
            askLine.innerHTML = '<span class="ico">·</span><span>这次没有合并</span>';
          }
        }
      }, 8000);
      if (askTimer.unref) askTimer.unref();
    });

    if (exitBtn) exitBtn.addEventListener('click', function(){
      if (REAL) {
        run(exitBtn, '正在暂离…', function(){
          post('command', { command: 'exit', args: '', text: '/exit' });
        });
      } else {
        CUR = CUR.split('/')[0];
        CUR_IS_CHILD = false;
        renderSessions();
        sysLine('←', '已切换至对话 ' + CUR, '');
      }
    });
  })();

  /** 重命名会话（会话行右键菜单里的「改名」） */
  function doRename(fullName){
    /* 子会话（alpha/gamma）只把「gamma」放进输入框并作为新名字提交：
       初值给全路径会让人以为要连父会话名一起写，而输进去的新名字一旦带 /，
       改的就不再是这条子会话了（后端会把 / 前面的部分当父会话）。
       提交时若当前是子会话，新名字里的 / 一律去掉，只改它自己那一段。 */
    var parts = String(fullName).split('/');
    var isSub = parts.length > 1;
    var curName = isSub ? parts.slice(1).join('/') : fullName;
    askText({
      title: isSub ? '重命名子会话' : '重命名会话',
      placeholder: '新名称',
      value: curName,
      // 改名不能留空（留空会得到一个没有名字的会话）
      requireValue: true
    }).then(function(n){
      // null = 取消；'' = 留空确认（改名不允许留空，要明确说）
      if (n === null) return;
      if (!n) {
        sysLine('·', '名称不能为空', '');
        return;
      }
      if (isSub) n = n.split('/').pop();
      if (n === curName) return;
      if (REAL) {
        post('sessionRename', { name: fullName, to: isSub ? parts[0] + '/' + n : n });
        return;
      }
      var f = findSess(fullName);
      if (!f) return;
      if (f.child) {
        f.child.name = n;
      } else {
        f.root.name = n;
      }
      if (CUR === fullName) CUR = n;
      renderSessions();
      sysLine('✏', '已重命名为 ' + n, '');
    });
  }

  /**
   * 新建对话：不要求先 /save，直接把当前上下文清空重开。
   * 已保存的会话会先落盘（之后仍能从列表进入）；没保存的直接丢弃——
   * 用户点这个按钮就是不想继续这段。
   */
  /** 新建对话的 pending 解锁（回执到了、或走了离线/失败分支）。 */
  function unlockNewChat(){
    var btn = document.getElementById('newChatBtn');
    if (btn) btn.removeAttribute('data-pending');
  }

  function doNewChat(){
    if (REAL) {
      // 发出到回执回来之前不再发第二次。连点 5 次会真的建 5 个会话
      //（每次后端都落盘一个文件、换一次上下文），用户只想点一下
      //（实测 20ms 内连点 2 次就是 2 条 sessionNew）。
      // 解锁点在 applySessionResult 的 sessionNew 分支。
      var btn = document.getElementById('newChatBtn');
      if (btn.getAttribute('data-pending') === 'true') return;
      btn.setAttribute('data-pending', 'true');
      post('sessionNew', {});
      return;
    }
    // 模拟态：清空对话区、回到未保存状态
    resetConversationDom();
    CUR = '';
    seedHistory([]);
    renderSessions();
    sysLine('＋', '已新建对话', '上下文已清空');
  }

  document.getElementById('newChatBtn').addEventListener('click', doNewChat);

  renderSessions();

  convo.scrollTop = 0;

  /* ==================================================================
     真实态：RPC 事件 → 界面
     扩展侧把协议 §4 的通知原样 postMessage 进来（{type, params}），
     这里分发给下面 16 类渲染函数。浏览器里双击本文件时收不到消息，
     页面保持纯模拟演示。
     ================================================================== */

  function sendChat(){
    var text = input.value.trim();
    if (!text) return;
    // 附件只写进**发给后端的文本**（绝对路径清单），气泡里仍是用户原文：
    // 插件不复制、不存储任何文件内容，由 AI 自己用 Read/Grep/Glob 去读。
    var wireText = text + attachBlock(ATCH);
    // 本次带出去的图片路径：气泡里要能看到图（不只是输入区那张 chip）。
    // 必须在下面 ATCH 被清空之前取下来。
    var sendImgs = ATCH.filter(function(a){ return a.kind === 'image'; })
      .map(function(a){ return a.path; });
    // 点「发送」按钮发以 / 开头的内容，必须和按 Enter 一样当命令执行。
    // 不这样会怎样（实测）：/mode 被当成普通对话发给模型——后端执行了命令，
    // 但这一轮不产生 turn/end，_turn_active 永远停在 true，
    // 之后所有消息都被 busy 拒绝、点停止也没用，只能重载窗口。
    if (text.charAt(0) === '/') {
      runInputCommand();
      return;
    }
    /* 这一轮还在跑时，不发新的普通消息。
       为什么必须拦（实测：损伤不可逆）：
         · 下面会无条件 `curAssistant = null; assistantBuf = ''` —— 正在流式的那条
           回答被丢弃引用，后续 stream/delta 会新建气泡，于是同一轮回答在屏幕上
           被劈成两个，前半截成了再也接不上的孤儿；
         · 后端这一轮没结束时会 busy 拒绝，而附件已经在末尾被清空（第 5978 行），
           消息没发出去、附件也没了，且只有一条泛泛的提示；
         · 更糟的是没有任何前置信号：只是「发送按钮变成了停止」，用户完全可能
           在等回答的过程中补充一句（这是很自然的动作）。
       同文件里 retry、新建对话都有 `data-run` 守卫，唯独这里漏了。
       拦下时把内容留在输入框里（不清、不进历史），用户按停止或等回答完再发即可。
       命令（/ 开头）不受影响：走 runInputCommand，后端对命令的处理与对话不同。

       **这里刻意不写系统行**（这一点踩过一次）：`sysLine` → `cmdOne` 会无条件
       `closeAssistantSegment()`（系统行必须落在「当前正文之后」，否则后面流进来的
       文字会把它夹在句子中间，那是它存在的理由）。而运行中插入系统行恰恰要断开
       正在流式的这一段 —— 于是「拦住了发送」却把回答劈成了两个气泡，
       和修复前一样坏（第 15 轮独立验收实测：A 无插入=1 气泡 / B 插话=2 气泡，
       且前半截的下一个兄弟正是那行提示）。
       改为「记一笔，等这一轮收口时再提示」：turn/end 之后正文已定稿，那时插入
       系统行不会拆散任何东西，用户也能得到明确反馈（不是零反馈）。 */
    if (body.getAttribute('data-run') === 'true') {
      blockedWhileRunning = true;
      return;
    }
    // 离线时不能先清空输入框：消息发不出去，用户打的字就凭空没了（实测
    // 未连后端按 Enter，输入框被清空、字也没进对话，等于白打一遍）。
    // 框内清空留到确实要发送时再做；历史照记（见下面分支）。
    // 判定要包含 data-offline：REAL 只表示「webview 在扩展里」，
    // 后端进程死掉时 REAL 仍是 true，只看它会照常清空输入框而消息发不出去
    // （实测：后端断开后按 Enter，输入框变空、对话区一条回执都没有）。
    var offline = REAL && body.getAttribute('data-offline') === 'true';
    if (!REAL || offline) {
      // 同一句话在 1.5 秒内只发一次。连点 5 次会发出 5 条 chat（每条都触发一次
      // 重连尝试）并刷 5 行一模一样的提示（实测演示态/断线态都是这样）。
      // 为什么仍然要发那一条：宿主把「用户主动发请求」当作明确的再试一次信号。
      var nowTs = Date.now();
      if (lastOfflineSend.text === text && nowTs - lastOfflineSend.ts < 1500) {
        return;
      }
      lastOfflineSend = { text: text, ts: nowTs };
      // 入历史照旧（用户下次按 ↑ 还能调出来），但输入框里的字必须留着：
      // 消息根本没发出去，清了就等于白打（实测离线按 Enter 文字凭空消失）。
      pushHistory(text);
      // 关键：即使离线也要把请求发给宿主一次。宿主那边有「用户主动发请求
      // = 明确的再试一次信号」的自救逻辑（extension.js dispatch），会自动
      // 重置重连计数并重新拉起后端。不发的话那条自救路径是死的——实测
      // 自动重连 5 次放弃后，界面上无论发消息还是敲命令都被这里拦下，
      // 后端起不来，只能改配置或重载窗口。
      // 那一次请求本身会失败（后端还没起来），但用户再按一次 Enter 就成了。
      post('chat', { text: wireText });
      sysLine('·', '未连接后端，消息未发送', '');
      scrollBottom();
      return;
    }
    pushHistory(text);
    input.value = '';
    input.style.height = '21px';
    input.style.overflowY = 'hidden';
    // 程序化清空不触发 input 事件：发送按钮的亮暗得手动同步一次，
    // 否则发完之后按钮还亮着（看着像「还能再发一条」）。
    refreshSendState();
    openPalette(false);
    var m = document.createElement('div');
    m.className = 'msg';
    m.setAttribute('data-role', 'user');
    // pending = 这条还没被后端接受。收到 chat/result accepted:false 时要撤回，
    // 否则「被拒绝的一句话」会以已发送的样子留在对话里、永远没有回答（实测会误导）。
    m.setAttribute('data-pending', '1');
    m.innerHTML = '<div class="text">' + esc(text) + '</div>';
    // 气泡里正文之后显示缩略图行（与输入区的 chip 共用同一套样式与缓存）
    addMsgThumbs(m, sendImgs);
    convo.appendChild(m);
    curAssistant = null;
    assistantBuf = '';
    authHintShown = false;
    lastSentMaybeAccepted = false;
    body.setAttribute('data-run', 'true');
    scrollBottom(true);
    // 按钮此刻从「发送」变成「停止」，同坐标的下一击会落在停止上（见 stopBtn 监听器）
    guardStopAfterSend();
    // 记下这次发出去的内容：回执里没有请求 id，一旦 pending 标记被别人摘掉
    //（同窗口另一面板的 accepted:true 是广播的），只能靠内容把这条消息找回来。
    lastSentText = text;
    lastSentMaybeAccepted = false;
    post('chat', { text: wireText });
    // 路径已经随这条消息交给后端了：清空附件列表。
    // 没发出去的分支（离线）不走这里，附件与输入框里的文字一起留着。
    if (ATCH.length) {
      ATCH = [];
      renderAttach();
    }
  }

  /**
   * 找回「还没被后端接受」的那条消息的 DOM 节点。
   *
   * 只认 [data-pending] 标记，**不做内容兜底**。曾经用「内容与 lastSentText
   * 一致的最后一条用户消息」兜底，结果是：发送成功后气泡和 lastSentText 都还在，
   * 后端一崩就把一句已经发出去的话从对话区删掉、塞回输入框，还写「这条消息
   * 没有发出去」——用户以为没发出去，重连后再发一次就重复投递（实测）。
   */
  function findPendingMessage(){
    var pend = convo.querySelectorAll('[data-pending]');
    if (!pend.length) return null;
    return pend[pend.length - 1];
  }

  /**
   * 把「还没被接受」的那条消息收场：能退就退回输入框，退不了就标成未发送。
   * @param {boolean} sureRejected 确知这条没被接受（收到 accepted:false）。
   *        只有确知时才无视 lastSentMaybeAccepted 标记。
   * @returns {boolean} 是否真的处理了一条
   */
  function settlePendingMessage(sureRejected){
    if (!sureRejected && lastSentMaybeAccepted) {
      // 这条多半已经送达：后端崩在它之后，不该把它撤回（实测会丢内容 + 谎报没发出去）
      return false;
    }
    var node = findPendingMessage();
    if (!node) return false;
    var text = (node.querySelector('.text') || {}).textContent || '';
    node.removeAttribute('data-pending');
    if (text && !input.value.trim()) {
      // 输入框还空着：能完整退回，最干净的收场
      if (node.parentNode) node.parentNode.removeChild(node);
      input.value = text;
      autoGrowInput();
    } else {
      // 用户已经又打了字：不能覆盖他正在写的内容，就地标成「未发送」
      node.setAttribute('data-failed', '1');
    }
    lastSentText = '';
    return true;
  }

  /**
   * chat/send 的回执。accepted:false 时必须退出运行态，
   * 否则界面永远停在「运行中」（stop 按钮不还原、输入框不可用）。
   * 最常见原因是上一轮还没结束（后端返回 busy）。
   */
  function applyChatResult(p){
    var accepted = !!(p && p.accepted);
    // 后端把「以 / 开头的文本」当命令执行了（兜底路径）：这不是发送失败，
    // 要把命令回执画出来，并退掉那条 pending 消息。
    if (p && p.handledAsCommand) {
      Array.prototype.forEach.call(convo.querySelectorAll('[data-pending]'), function(el){
        el.parentNode && el.parentNode.removeChild(el);
      });
      applyCommandResult(Object.assign({}, (p.result || {}), {
        command: p.command || ''
      }));
      return;
    }
    if (accepted) {
      // 被接受：界面已在发送时进入运行态，这里只需回执（勿提前 return，否则测试与诊断看不到）
      //
      // **不摘 pending**：回执是广播的、不带发起方 id，同窗口有两个 webview 时
      // A 的这条会把 B 的 pending 一起摘掉，B 随后被 busy 拒时找不到待定消息、
      // 撤回失效（#70）。pending 的真正清理时机是「这一轮开始」（turn/begin）
      // 和「这一轮结束」（turnEnd）——那时这条发送必然已被处理。
      //
      // 只记一笔「本面板那条可能已被接受」：断连时据此不撤回，
      // 免得把已经送达的话从对话区删掉还谎报「没发出去」（实测）。
      if (findPendingMessage()) lastSentMaybeAccepted = true;
      rendered('chat/result', { accepted: true });
      return;
    }
    var msg = (p && p.error) || '发送失败';
    // 没被接受：把刚才那条「看起来已经发出去」的消息撤回。
    // 不撤回会怎样（实测）：这句话以普通用户消息留在对话里、永远没有回答，
    // 而输入框已被清空——用户既不知道没发出去，也找不回自己打的字。
    var pendingNode = findPendingMessage();
    // hadPending 要在 settle 之前记：settle 会把那条待定消息摘掉/回填，
    // 之后就无法区分「本面板这条被拒」与「别的面板这条被拒」。
    var hadPending = !!pendingNode;
    var sentText = pendingNode ? ((pendingNode.querySelector('.text') || {}).textContent || '') : '';
    var isBusy = /busy|上一轮还在运行/i.test(msg);
    // busy 回执要能判断「是不是我这条」：本面板没有待定消息时肯定不是我的
    // ——那种情况下说「这句话没有发出去」会让用户去检查自己刚打的东西，
    // 而它一个字都没发（实测双面板下必现）。
    //
    // 有 pending 时仍然按「就是我这条」处理（即撤回 + 提示）。这里不用
    // lastSentMaybeAccepted 兜：那个标记会被**别人的** accepted:true 广播置位，
    // 拿它当判据会让「本面板这条真被 busy 拒」时不撤回，消息留在对话里
    // 永远没有回答，用户以为发出去了（实测，round11 的 Z 场景）。
    // 协议不带发起方 id，这层歧义无法彻底消除；两个方向里选「撤回」——
    // 误撤的代价是可能要重发一次，不撤的代价是用户以为任务已经交给 AI。
    if (isBusy && !hadPending) {
      rendered('chat/result', { accepted: false, error: msg, busy: true, notMine: true });
      return;
    }
    // 收到明确的 accepted:false = 确知这条没被接受，无视「可能已接受」的保守标记
    settlePendingMessage(true);
    if (isBusy) {
      // busy = 上一轮还在跑，这一条没发出去。此时**不能**碰 data-run：
      // 把它置 false 会让停止按钮消失，用户明明还在跑却停不下来（实测）。
      // 退回输入框：用户可以直接等这轮结束再按发送，不用重打。
      if (sentText && !input.value.trim()) {
        input.value = sentText;
      }
      // 去重：连抢两次时不再堆第二行一模一样的提示（实测会堆两行）。
      var lastRow = convo.lastElementChild;
      var dup = lastRow && lastRow.className.indexOf('sys') >= 0 &&
        (lastRow.textContent || '').indexOf('上一轮还在运行') >= 0;
      if (!dup) {
        sysLine('·', '上一轮还在运行，本条未发送', '');
      }
      scrollBottom();
      rendered('chat/result', { accepted: false, error: msg, busy: true });
      return;
    }
    body.setAttribute('data-run', 'false');
    if (sentText && !input.value.trim()) {
      input.value = sentText;
    }
    // 超时/无响应：后端可能还在跑，只是不吭声。此时把运行态打掉会让
    // 「停止」按钮消失，用户想停都停不下来（实测假死后没法中断）。
    // 保持运行态，并明确告诉他可以点停止。
    var isTimeout = /timeout|timed out|超时|无响应|未响应/i.test(String(msg));
    if (isTimeout) {
      body.setAttribute('data-run', 'true');
      sysLine('⚠', '请求超时，可点停止', '');
      scrollBottom();
      rendered('chat/result', { accepted: false, error: msg, timeout: true });
      return;
    }
    sysLine('✗', '请求失败，请检查网络或配置', esc(String(msg).slice(0, 80)), 'err');
    scrollBottom();
    rendered('chat/result', { accepted: false, error: msg });
  }

  /** cancel 的回执：cancelled:false 说明本来就没有可打断的轮次（除非带 error） */
  function applyCancelResult(p){
    // 兜底收口的定时器与标记（见下面的 cancelled 分支）
    if (cancelSettleTimer) { clearTimeout(cancelSettleTimer); cancelSettleTimer = null; }
    cancelSettleTimerFired = false;
    // bgCancelled>0：点停止时连同 agent 自己派出去的后台任务一起停了。
    // 必须说出来——否则用户不知道那些后台进程是被停掉了还是还在跑。
    var bg = (p && p.bgCancelled) || 0;
    if (p && p.cancelled) {
      // 必须把按钮的「停止中…」锁解开。不解会怎样（实测）：
      // 后端已经打断了、但它认为这一轮早已结束，于是不再发 turn/end，
      // 而 resetStopBtn 只在 turn/end 里调 —— data-pending 永久留在按钮上，
      // 下一次点停止会被「点过了，忽略」的守卫吞掉，看起来就是按钮失灵。
      resetStopBtn();
      if (bg > 0) {
        // 这里**不能**写「■ 已打断」：真正的收口是紧随其后的 turn/end，
        // 两处各写一次就变成两行「已打断」（实测 bgCancelled=1 时必现）。
        // 这一行只说后台任务的事，打断本身由 turn/end 那条统一说。
        sysLine('■', '同时停止了 ' + bg + ' 个后台任务', '');
        scrollBottom();
      }
      // 兜底收口：后端说「已取消」了，但界面退不退出运行态全靠它随后发
      // 的那条 turn/end。万一不发（实测：后端认为这一轮早就结束、或消息在
      // 路上丢了），界面会永远停在运行中——停止按钮挂着、输入框发不出去，
      // 用户只能关掉面板。这里等 2 秒，期间还收到 turn/end 就什么都不做。
      if (cancelSettleTimer) clearTimeout(cancelSettleTimer);
      cancelSettleTimer = setTimeout(function () {
        cancelSettleTimer = null;
        if (body.getAttribute('data-run') !== 'true') return;
        cancelSettleTimerFired = true;
        turnEnd({ reason: 'cancelled' });
      }, 2000);
      return;   // 真打断了，turn/end 会负责收尾
    }
    // 带 error = 这次 cancel 请求本身失败了（超时/掉线），不是「本来就没有任务」。
    // 两者混为一谈会怎样（实测）：后端卡在长工具里 30 秒不应答，界面先说
    // 「当前没有运行中的任务」再把停止按钮收起——用户以为停了，实际还在跑，
    // 此后每条消息都被 busy 拒，而唯一能脱身的停止按钮已经没了。
    if (p && p.error) {
      resetStopBtn();
      sysLine('✗', '停止失败', esc(String(p.error).slice(0, 80)), 'err');
      scrollBottom();
      rendered('cancel/result', { cancelled: false, error: String(p.error).slice(0, 200) });
      return;
    }
    body.setAttribute('data-run', 'false');
    resetStopBtn();
    if (bg > 0) {
      sysLine('■', '已停止 ' + bg + ' 个后台任务', '');
    } else {
      sysLine('·', '当前没有运行中的任务', '');
    }
    scrollBottom();
    rendered('cancel/result', { cancelled: false, bgCancelled: bg });
  }

  function sendCommand(raw){
    var s = String(raw).trim().replace(/^\//, '');
    var parts = s.split(/\s+/);
    var name = (parts[0] || '').toLowerCase();
    // 打错的命令要当场说清，别当需求发给模型。
    // 判据：命令名是「纯字母短词」却不认识 → 就是打错了，提示 + 给出可用命令；
    // 含 / . \ - 等路径特征的（/usr/local/bin、./a.md）仍按对话发——
    // 用户可能就是想发一个以斜杠开头的路径（这是第 3 轮就定下的行为，别退回去）。
    var looksLikePath = /[\/\\.\-]/.test(name) || name.length > 14;
    if (name && !looksLikePath && !COMMANDS.some(function(x){ return x.c.slice(1) === name; })) {
      sysLine('✗', '没有这个命令：/' + name, '');
      scrollBottom();
      rendered('command/result', { command: name, handled: false, unknownCommand: true });
      return;
    }
    // /clear 在面板里是纯界面操作：清空对话区显示，会话上下文一律不动。
    // 交给后端会怎样（实测）：终端那条 /clear 只写 ANSI 转义（\033[2J\033[H），
    // RPC 层把它剥掉后 lines 为空，界面于是走兜底分支打一句「✓ 已执行」——
    // 屏幕一点没变，用户以为命令坏了或以为自己敲错。
    // 也不能真让后端清上下文：面板里的「清屏」不该顺手丢掉正在聊的历史。
    if (name === 'clear' && REAL) {
      clearScreen();
      rendered('command/result', { command: 'clear', ok: true, local: true });
      return;
    }
    post('command', {
      command: name,
      args: parts.slice(1).join(' '),
      text: String(raw).trim()
    });
  }

  /**
   * 清屏：把当前对话从「显示」里拿掉，会话上下文与磁盘内容一律不动。
   * 清完留一行说明，否则用户会以为对话被删了（内容其实还在会话里，
   * 点抽屉能回去）。
   *
   * 运行中清屏要**如实交代**。清屏会 resetConversationDom()，它把
   * curAssistant / assistantBuf 一起丢掉但不碰 data-run，于是后续 stream/delta
   * 会新建气泡、只装「清屏之后」的那半截；轮末重渲染也只覆盖这半截 ——
   * 这一轮回答的前半截在屏幕上永久消失。
   * 这里**不拦**清屏（清屏就是用户的明确意图，拦了反而违背操作），
   * 但必须说清两件事：这一轮没停、完整内容还能从会话里取回来。
   * 同族另外三个清屏入口（切会话 / 新建对话 / /rm 当前会话）都写了各自的交代，
   * 只有 /clear 原来一个字不说 —— 用户会以为回答丢了或界面坏了（实测）。
   */
  function clearScreen(){
    var wasRunning = body.getAttribute('data-run') === 'true';
    resetConversationDom();
    authHintShown = false;
    cmdResult('/clear', '·',
      wasRunning ? '已清屏 · 这一轮仍在后台继续，完整回答可在会话里重新载入' : '已清屏', '');
    scrollBottom();
  }

  /* ---------- 助手正文 ---------- */

  // 流式渲染的分段状态：已定稿前缀 + 活跃尾巴。
  // 每个 delta 都整段重渲染会怎样（实测，300px 面板宽度）：
  //   正文 4KB→11.6ms、16KB→48.6ms、40KB→120.5ms、200KB→667ms，
  //   其中约 77% 是浏览器重新布局（DOM 节点数从 538 涨到 27366）。
  // 用户让它读一份长日志时，输出期间每来一段界面就冻住几百毫秒，
  // 滚不动、点不到「停止」。分段后每次只重建尾巴那一小块。
  var mdLockNode = null;    // 已定稿前缀的容器（渲染一次后不再动）
  var mdTailNode = null;    // 活跃尾巴的容器（每个 delta 重建）
  var mdLockLen = 0;        // 前缀对应的原文长度

  function resetStreamRender(){
    mdLockNode = null;
    mdTailNode = null;
    mdLockLen = 0;
  }

  /**
   * 找安全切点：从 from 起，最后一个可以安全切开的位置之后。
   *
   * 切点是「行尾」而不是「空行」：日志、清单这类内容每行一个换行，
   * 要求空行的话前缀永远推不动，长日志仍旧每次整段重渲染（实测无效）。
   *
   * 三处不能切：
   *   · 代码围栏内（切开会把代码块劈成两个残块）
   *   · 表格行内（表格行之间只有单换行，切开就是两张残表）
   *   · 列表内部（切开会让每段各自从 1 编号 / 续行掉到列表外）
   *
   * **判据必须与渲染器同源**（这是踩过的最大一个坑）：切点判据和
   * `parseListAt` / `markdownInline` 各自写一套规则，只要两边对「什么算同一个块」
   * 的理解有一点偏差，流式期间就会出现「渲染错误的内容」，而轮末整段重渲染
   * 又把它修好 —— 于是 bug 只在几十秒的流式窗口里可见，前 10 轮全部漏掉。
   * 具体偏差（第 11 轮实测）：
   *   · 列表：渲染器认为「空行后仍是同缩进列表项」和「≥2 空格缩进续行」都还属于
   *     同一个列表，而切点判据只比「相邻两行都是列表项」→ 松散列表被切开后
   *     屏幕上每段各从 1 编号；续行被剥离成列表外的独立段落
   *   · 围栏：渲染器只认**行首**（允许前导空白）且长度 ≥3 的反引号串，切点判据
   *     却是「全文任意位置见到 ``` 就取反」→ 代码正文里出现 ``` 时后续正文被当代码
   */
  function safeCut(buf, from){
    var n = buf.length;
    if (from >= n) return -1;
    var LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s/;
    var LIST_CONT = /^\s{2,}\S/;          // 缩进续行（渲染器把它并进上一项）
    var BLANK = /^\s*$/;
    // 行首围栏：与 markdownInline 同判据（允许前导空白、长度 >= 3）
    function fenceLenAt(pos){
      var rest = buf.slice(pos, pos + 96);
      var m = /^[ \t]*(`{3,})/.exec(rest);
      if (!m) return 0;
      // 必须整行都是反引号（后面只允许空白或语言标签），行内出现的反引号不算围栏
      var lineEnd = rest.indexOf('\n');
      var line = lineEnd < 0 ? rest : rest.slice(0, lineEnd);
      if (!/^[ \t]*`{3,}[^`]*$/.test(line)) return 0;
      return m[1].length;
    }
    // 取 pos 起始的完整行（不含换行）；未写完返回 null
    function lineAt(pos){
      var e = buf.indexOf('\n', pos);
      return e < 0 ? null : buf.slice(pos, e);
    }
    /**
     * 这个切点（= nextStart 处的行边界）是否落在**同一个列表内部**。
     *
     * 必须「跨空行」判断，不能只看紧邻两行。踩过两次：
     *   ① 只看 prevLine/nextLine 都是列表项 → `1. a\n\n2. b` 的空行处被判可切，
     *      切开后每段各从 1 编号（屏幕上 1. 1. 1.）
     *   ② 改成「prevLine 是项时看后面」→ `1. a\n` 与 `\n2. b` 之间那个边界，
     *      它的 prevLine 是空行，判据整个不生效，照样切
     * 正确语义取自渲染器的 parseListAt：**跳过空行后仍是列表项或缩进续行，
     * 就还属于同一个列表**。所以这里往前找最近的非空行 B、往后找最近的非空行 A，
     * 只要 B 是列表项且 A 仍是列表项/缩进续行，这个边界就在列表内部。
     */
    function listSpansHere(nextStart){
      // 往后：跳过空行找第一个非空行
      var p = nextStart;
      var ahead = null;
      while (p <= buf.length) {
        var la = (p === buf.length) ? null : lineAt(p);
        if (la === null) {
          if (p === buf.length && buf.length > 0 && buf.charAt(buf.length - 1) !== '\n') return true;
          break;                                  // 还没写完 → 保守当作在列表内
        }
        if (!BLANK.test(la)) { ahead = la; break; }
        p += la.length + 1;
      }
      if (ahead !== null && !(LIST_ITEM.test(ahead) || LIST_CONT.test(ahead))) {
        return false;                             // 后面就是普通正文 → 列表到此结束
      }
      // 往前：跳过空行找第一个非空行
      var q = nextStart - 1;                      // 指向边界前的那个 '\n'
      var behind = null;
      while (q >= 0) {
        var ls = buf.lastIndexOf('\n', q - 1) + 1;
        var lb = buf.slice(ls, q);
        if (!BLANK.test(lb)) { behind = lb; break; }
        if (ls === 0) break;
        q = ls - 1;
      }
      if (behind === null) return false;          // 前面没有内容 → 不在列表内
      // 前面是列表项 **或缩进续行** → 都还在同一个列表里。
      // 漏掉 LIST_CONT 会怎样（实测）：`- 安装依赖` / `  执行 npm install` / `- 第二项`
      // 三行里，最后那个边界的前一行是「缩进续行」而不是列表项，判据返回 false
      // 于是切开 —— 屏幕上一个 <ul> 变成两个。
      return LIST_ITEM.test(behind) || LIST_CONT.test(behind);
    }
    // 前缀里当前是否困在代码块中：逐行扫，按长度配对
    var fenceLen = 0;
    var preEnd = from === 0 ? 0 : buf.lastIndexOf('\n', from - 1) + 1;
    for (var q = 0; q < preEnd; ) {
      var L0 = fenceLenAt(q);
      if (L0) {
        if (fenceLen === 0) fenceLen = L0;
        else if (L0 >= fenceLen) fenceLen = 0;
      }
      var e0 = buf.indexOf('\n', q);
      if (e0 < 0) break;
      q = e0 + 1;
    }
    var cut = -1;          // 任意的可切边界（退化档：找不到块边界时用）
    var cutBlock = -1;     // 块边界（优先用：切在这里不会把一段拆成两段）
    var lineStart = from === 0 ? 0 : buf.lastIndexOf('\n', from - 1) + 1;
    var i = from;
    while (i < n) {
      // 围栏只在行首认（行内反引号不是围栏）
      if (i === lineStart) {
        var Lf = fenceLenAt(i);
        if (Lf) {
          if (fenceLen === 0) fenceLen = Lf;
          else if (Lf >= fenceLen) fenceLen = 0;
        }
      }
      if (buf.charAt(i) === '\n') {
        if (fenceLen === 0) {
          var prevLine = buf.slice(lineStart, i);
          var nextStart = i + 1;
          var nextEnd = buf.indexOf('\n', nextStart);
          // 下一行还没写完（没有结尾换行）：它可能是 "6." 这种被截断的列表项，
          // 按残缺内容判定会误判成「不是列表项」而切下去，流式中间态于是把
          // 一张 <ol> 切成好几张（实测 8 项清单 ol=2/3/3）。
          // 不完整就一律不切——它本来就在尾巴里，不影响前缀推进。
          var nextComplete = nextEnd >= 0;
          if (nextComplete) {
            var nextLine = buf.slice(nextStart, nextEnd);
            // 表格行、列表项、引用行之间切开都会破坏块结构：
            //   · 表格：行之间只有单换行，切开就是两张残表（漏判无前导竖线的
            //     写法 "a | b" —— 实测切完 <tr> 从 9 行变 1 行）
            //   · 引用块：连续 "> x" 行切开会变成多个 <blockquote>
            //     （实测 1 个变 6 个，每块各带一条竖线）
            //   · 列表：判据与 parseListAt 同源，见上面 listSpansHere
            var breaksTable = isTableRow(prevLine) || isTableRow(nextLine);
            // 列表：跨空行判断（见 listSpansHere 的注释），不再只看紧邻两行
            var breaksList = listSpansHere(nextStart);
            var breaksQuote = /^\s*>/.test(prevLine) && /^\s*>/.test(nextLine);
            if (!breaksTable && !breaksList && !breaksQuote) {
              cut = i + 1;
              /* 但「能切」不等于「该切」：还要看这里是不是一个**块边界**。
                 为什么必须分两档（实测，频率 100%）：
                   renderBlocks 的 flushPara 把「连续非空行」渲染成**一个** <p>
                   （段内单换行变 <br>），而前缀 mdLockNode 一旦写入就永不重渲染。
                   所以切在「普通段落的两行之间」时，前几行被永久定稿成一个独立 <p>，
                   剩下的行又成一个 <p> —— 用户看到一段话被拆成几段、每段之间多出
                   段间距，随输出推进不断重排，长回答里会持续几十秒。
                   实测：3 行普通段落、任意步长（2/3/5/7/11）全部命中；真实长回答
                   4/4 种切法都命中。
                 块边界判据（切在这里不会拆段）：
                   · 前一行是空行 → 后面是新段落起点
                   · 下一行是块级标记（列表/表格/围栏/引用/标题）→ 是本块结束
                 注意**不能**直接禁掉「段落行之间切」：长日志每行一个换行、全文没有
                 空行，禁掉之后前缀完全推不动（第 6232 行注释记录过这个性能教训）。
                 所以保留 cut 作为退化档，只在找不到块边界时用它。 */
              var BLOCKISH = /^\s*(?:[-*+]|\d+[.)]|>|#{1,6}[ \t]|`{3,})/;
              var atBlockEdge = BLANK.test(prevLine) || BLOCKISH.test(nextLine);
              if (atBlockEdge) cutBlock = i + 1;
            }
          }
        }
        lineStart = i + 1;
      }
      i++;
    }
    // 优先块边界。两个候选都没有时返回 -1（不切），调用方保持整段在尾巴里重渲染。
    //
    // 退化档的判据是「尾巴已经很长」而不是「有任意可切边界」：
    // 只看 cut 的话，3 行普通段落（无空行、无块标记）仍会在行间切开，
    // 段落照样被拆（实测就是这条路）。而「不切」的代价只是这一段每次 delta
    // 都整体重渲染 —— 第 11 轮 J6 实测 50 万字符整段重渲染仅 30ms，
    // 所以短内容完全承受得起，没必要为它牺牲排版。
    // 只有尾巴长到 2 万字符以上（典型是几百行日志、全文没有空行）才退化，
    // 否则那种内容前缀完全推不动，每次 delta 重解析全部历史。
    if (cutBlock > 0) return cutBlock;
    if (cut > 0 && (n - from) > 20000) return cut;
    return -1;
  }

  /**
   * 是不是表格行。
   *
   * 不能只看「以 | 开头」：GFM 允许省略前导竖线（`a | b` 就是两列表格），
   * 而 AI 写表格时两种都常见。判据改为「含至少 1 个未转义竖线」。
   *
   * 放宽会不会误判普通文字？会（"用 a | b 表示或"），但代价不对称：
   *   · 多判 → 这一处不切，前缀少推进一次。只是性能略降，结果仍正确。
   *   · 漏判 → 表格被切成两张残表，屏幕上永久显示错（实测 9 行变 1 行）。
   * 所以宁可多判。注意这不会让普通文字被渲染成 <table>——那是
   * markdownInline 里 renderTable 的事，与切点判定无关。
   */
  function isTableRow(line){
    var s = String(line || '');
    if (!s.trim()) return false;
    for (var k = 0; k < s.length; k++) {
      if (s.charAt(k) === '\\') { k++; continue; }
      if (s.charAt(k) === '|') return true;
    }
    return false;
  }

  function ensureAssistant(){
    if (!curAssistant) {
      // 助手消息直接以 .msg 承载 <p>，不要套 .text：
      // .text 是用户消息的容器，带左侧竖线与负边距，套上会让正文错位。
      curAssistant = document.createElement('div');
      curAssistant.className = 'msg assistant';
      convo.appendChild(curAssistant);
      resetStreamRender();
    }
    return curAssistant;
  }

  function renderAssistant(){
    // 正文开写 = 这段思考结束了（后端此刻也会发 thinking/end，但这里先收口
    // 能保证 DOM 顺序一定是 思考 → 正文，而不是正文先出现、思考块后补）
    closeThinkSegment();
    var host = ensureAssistant();
    if (!mdLockNode) {
      // 第一次渲染：切成「前缀容器 + 尾巴容器」。分开是为了让前缀
      // 只渲染一次——尾巴每次重建，前缀的 DOM 树原封不动，
      // 浏览器只需重排尾巴那一小块。
      host.innerHTML = '';
      mdLockNode = document.createElement('div');
      mdTailNode = document.createElement('div');
      host.appendChild(mdLockNode);
      host.appendChild(mdTailNode);
      mdLockLen = 0;
    }
    var cut = safeCut(assistantBuf, mdLockLen);
    if (cut > mdLockLen) {
      // 前缀只增量追加、永不重渲染。整段重渲染前缀会怎样：每来一个 delta
      // 都要把已定稿的那几万字重新解析一遍，比不做分段还慢（实测 0.6x）。
      mdLockNode.insertAdjacentHTML('beforeend', markdownInline(assistantBuf.slice(mdLockLen, cut)));
      mdLockLen = cut;
    }
    mdTailNode.innerHTML = markdownInline(assistantBuf.slice(mdLockLen));
  }

  /** 整段重渲染一次（轮次结束时用，保证最终结果与「一次性渲染」完全一致）。 */
  function renderAssistantFinal(){
    var host = ensureAssistant();
    resetStreamRender();
    host.innerHTML = markdownInline(assistantBuf);
  }

  /**
   * 收口当前正文段：定稿并断开引用，后续增量会另起一个气泡。
   *
   * 一次请求内部会有多段正文（AI 先说一句 → 调工具 → 再接着说），中间还夹着
   * 思考块与工具行。不在这里断开会怎样（实测）：第二段正文继续追加进第一段的
   * 气泡，几段话被拼成一整段，且整块文字都留在最上面、所有工具行排到它下面——
   * 用户看到的是一条读不通的长文，而不是「说一句 → 做一步 → 再说一句」。
   */
  function closeAssistantSegment(){
    if (curAssistant && assistantBuf) renderAssistantFinal();
    curAssistant = null;
    assistantBuf = '';
    resetStreamRender();
  }

  /**
   * 鉴权失败时补一行可操作指引。
   * 后端把 LLM 错误当正文流式输出，只说「AI调度失败，请检查 narnat.json」，
   * 而扩展用户并不知道这个文件怎么打开，所以在这里给出 VSCode 内的入口。
   *
   * 判据必须收紧到「后端错误行的具体形态」：早先只要正文里出现
   * api key / 接口密钥 / 401 这几个词就弹——用户让它「检查一下安全配置」，
   * 回答里提到 API Key 就被误判成「你需要配置 API Key」（实测假警报）。
   * 现在只认后端错误前缀 `[错误: API调用失败(401` / `AI调度失败` 这类。
   */
  function authHintIfNeeded(){
    if (authHintShown) return;
    var t = assistantBuf;
    var isAuthError = /\[错误:\s*AI\s*调度失败/i.test(t)
      || /\[错误:\s*API调用失败\(\s*401/i.test(t)
      || /Authentication Fails/i.test(t)
      || /api\s*key\s*(?:is\s*)?(?:invalid|empty|not\s*set|未配置|无效)/i.test(t);
    if (!isAuthError) return;
    authHintShown = true;
    // 后端正文里已经有「AI调度失败，请检查 narnat.json」这句结论了，系统行
    // 不再复述一遍（同屏两遍同一句话，用户只会觉得吵）。这里只补它给不出的
    // 东西：在 VSCode 里怎么打开那个文件。
    sysLine('·', '打开配置文件', 'Ctrl+Shift+P → Narnat: 打开配置文件', '');
    rendered('auth/hint', { hint: '打开配置文件' });
  }

  function appendStream(p){
    if (!p || typeof p.text !== 'string') return;
    assistantBuf += p.text;
    renderAssistant();
    authHintIfNeeded();
    scrollBottom();
    // 回执里只带正文开头一段。这个字段是给测试/诊断看的，用户看不到；
    // 每个 delta 都取一次全文（assistantText → textContent）会怎样：
    // 120KB 正文时每次增量多花 187~256ms、累计分配 184MB（实测）——
    // 用户只是在看回答，却在为一份诊断数据付卡顿的代价。
    rendered('stream/delta', { assistant: assistantBuf.slice(0, 2000) });
  }

  /* ---------- 思考块 ---------- */

  function beginThink(){
    // 正文在前、思考在后时同样要断开：否则这之后的正又会追加到那段正文里
    closeAssistantSegment();
    /* 同一段思考里 begin 重复到达时，复用它而不是新建第二块。
       后端 on_thinking_delta 有 _thinking_started 守卫，正常不会重发；但事件
       重放、宿主补发、面板重建都可能让它再来一次。新建会怎样（实测）：
       屏幕上出现两块思考，第一块永远停在「思考中 · N 字」（data-stream 不摘），
       后半段内容跑到第二块里——用户看到一段思考被劈成两半、前一半永远在转。 */
    if (curThink) {
      rendered('thinking/begin', { open: curThink.wrap.getAttribute('data-open') === 'true',
                                   reused: true });
      return;
    }
    var wrap = document.createElement('div');
    wrap.className = 'think';
    // 与工具行一致：默认折叠，点标题展开看全文。
    // 默认展开会把整段思考铺满屏幕，正文反而被推到看不见。
    wrap.setAttribute('data-open', 'false');
    wrap.setAttribute('data-stream', 'true');
    wrap.innerHTML = '<div class="row" data-open="false" tabindex="0" role="button">' +
      '<span class="caret">▶</span><span class="name">思考中</span></div>' +
      '<div class="slot"><p></p></div>';
    convo.appendChild(wrap);
    wrap.setAttribute('data-turn-think', '1');
    turnThinkCount += 1;
    curThink = { wrap: wrap, p: wrap.querySelector('.slot p'), buf: '', t0: Date.now() };
    scrollBottom();
    rendered('thinking/begin', { open: false });
  }

  /** 把当前思考块收口成「思考 N s · N 字」并断开引用。
   *
   *  为什么需要「本地收口」而不只依赖后端的 thinking/end：
   *  后端 `_end_thinking()` 只在三种时机被调 —— 收到正文流、轮末、中断。
   *  于是「思考 → 工具调用 → 思考 → 工具调用」这种一轮里多次思考、
   *  期间没有正文的序列，整轮只有一次 begin、末尾才有 end。
   *  接着往下走会怎样（实测）：后面每段思考的 delta 都追加进同一块，
   *  合并成一大段（看到「思考中 · 6870 字」这种），而且因为没到轮末，
   *  data-stream 摘不掉，标题永远停在「思考中」。
   *  正确形态是「思考一段 → 做一步 → 再思考一段」各自成块，
   *  所以插入任何非思考内容（工具行 / 正文 / 系统行）时在这里就地收口。
   *  @param {number} [ms] 后端给的耗时；没给就用本地计时
   *  @param {number} [chars] 后端给的字数；没给就用本地缓冲长度 */
  function closeThinkSegment(ms, chars){
    if (!curThink) return;
    var n = (typeof chars === 'number') ? chars : curThink.buf.length;
    var dur = (typeof ms === 'number')
      ? ms
      : (curThink.t0 ? (Date.now() - curThink.t0) : 0);
    curThink.wrap.removeAttribute('data-stream');
    curThink.wrap.querySelector('.name').textContent =
      '思考' + (dur ? ' ' + (dur / 1000).toFixed(1) + 's' : '') + ' · ' + n + ' 字';
    curThink = null;
    rendered('thinking/closed', { chars: n, durationMs: dur });
  }

  function appendThink(p){
    if (!curThink) beginThink();
    if (!p || typeof p.text !== 'string') return;
    curThink.buf += p.text;
    curThink.p.textContent = curThink.buf;
    curThink.wrap.querySelector('.name').textContent = '思考中 · ' + curThink.buf.length + ' 字';
    scrollBottom();
    rendered('thinking/delta', { chars: curThink.buf.length });
  }

  function endThink(p){
    if (!curThink) {
      rendered('thinking/end', { text: '', chars: 0 });
      return;
    }
    var text = curThink.buf;
    // 后端给了 chars/durationMs 就用它的（那是权威值），否则退回本地计数
    closeThinkSegment(
      (p && typeof p.durationMs === 'number') ? p.durationMs : undefined,
      (p && typeof p.chars === 'number') ? p.chars : undefined
    );
    scrollBottom();
    rendered('thinking/end', { text: text, chars: text.length });
  }

  /* ---------- 工具行 ---------- */

  /**
   * 把仍停在「执行中」的工具行收尾。
   *
   * 后端被掐断时不会为那个工具补 tool/result（它自己也不知道结果），
   * 界面于是留着「执行中」+ 同屏「已打断」两个互相矛盾的结论。
   * 被打断就是没跑完，必须这么说，不能让它一直转。
   */
  /**
   * 一轮结束时把还在 running 的工具行收口。
   *
   * 「等待确认」的行要分开处理：它在等用户点按钮，不是执行中被掐断，
   * 一律判成「已中断」会误伤（确认后后端会发 turn/end{intermediate} +
   * turn/begin，那一行被写成「已中断」，紧接着真实结果到达又落到
   * 「中断后返回」——命令其实成功了，界面说自己中断了）。
   *
   * 但完全不处理也不行（实测）：用户点了确认、后端回 confirm/result{ignored}
   * 或这一轮被停止/掉线，确认条会被 clearConfirmBoxes 收掉，而这一行**永久**
   * 停在「等待确认」+ data-st=running。屏幕上同时写着「已打断」，
   * 两处结论互相打脸，而卡住的恰是「rm -rf 到底执行了没有」这个判断。
   *
   * 判据：只有**这一轮真的收口了**（非 intermediate）才给「等待确认」的行定性。
   * intermediate 说明同一请求内部还会续跑，那一行仍有机会拿到真实结果。
   */
  function settleRunningTools(reason, opts){
    var forGood = !!(opts && opts.final);
    Object.keys(toolMap).forEach(function (k) {
      var entry = toolMap[k];
      if (!entry || !entry.row) return;
      // row 可能已经不在文档里（清空对话区后 toolMap 会被复位，但复位前
      // 若刚好有一次收口就会遇到）。脱离文档的节点改它的属性没有意义。
      // 用 === false 而不是 !isConnected：DOM 替身（测试/预览）可能没有
      // 这个属性，undefined 时不跳过，保持原有行为。
      if (entry.row.isConnected === false) return;
      if (entry.row.getAttribute('data-st') !== 'running') return;
      var awaiting = entry.row.getAttribute('data-await-confirm') === '1';
      if (awaiting && !forGood) return;
      entry.row.setAttribute('data-st', 'err');
      // 打上「已由收口逻辑定性」的标记：被打断的工具，它的 tool/result 往往
      // 还会晚几百毫秒才到（实测 242~294ms 后到达）。那个迟到结果会把
      // 「已中断」直接覆盖成「完成 · 1915ms」——用户点过停止，却看到最后一步
      // 写着完成，无法判断它到底执行了没有。
      entry.row.setAttribute('data-settled', '1');
      var meta = entry.row.querySelector('.meta');
      if (meta) {
        meta.className = 'meta';
        // 「等待确认」的行没执行过：不能说「已中断」（它压根没开始），
        // 也不能说「完成」。它要回答的是「这条命令到底跑了没有」——没跑。
        var why = awaiting ? '未执行（确认没生效）'
          : (reason === 'error' ? '未完成' : '已中断');
        meta.innerHTML = '<span class="bad">' + why + '</span>';
      }
      if (awaiting) entry.row.removeAttribute('data-await-confirm');
      // 留一个痕迹：这一行是「等确认」被收口的，不是执行中被掐断。
      // 用户之后仍可能点确认（confirm/result 与 turn/end 的先后没有保证），
      // 真实结果到达时靠它把上面那个临时结论撤销掉（见 finishTool）。
      if (awaiting) entry.row.setAttribute('data-await-settled', '1');
    });
  }

  /**
   * 工具调用的「完整输入」：展开卡片里 IN 那一段的正文。
   *
   * 收起态只有一行摘要（会被省略号截断），用户要点开才看得到到底执行了什么。
   * 优先取最有信息量的那个字段：Shell 的 command、读写工具的路径、搜索的
   * 模式词；都不是就退回整段参数 JSON。
   *
   * 超过 IN_MAX 就截断：Write 一次写整份文件时，content 可能是几万字符，
   * 整段塞进 DOM（而且卡片收起时也常驻）会拖慢渲染，也让这一行失去可读性。
   */
  var IN_MAX = 2000;
  function toolInputText(p){
    var a = p && p.arguments;
    if (!a || typeof a !== 'object') return '';
    var keys = ['command', 'file_path', 'path', 'pattern', 'query', 'url',
      'old_string', 'content', 'text'];
    var out = '';
    for (var i = 0; i < keys.length; i++) {
      var v = a[keys[i]];
      if (typeof v === 'string' && v) { out = v; break; }
    }
    if (!out) {
      try { out = JSON.stringify(a, null, 2); } catch (e) { return ''; }
    }
    if (out.length > IN_MAX) {
      out = out.slice(0, IN_MAX) + '\n…（共 ' + out.length + ' 字符，已截断）';
    }
    return out;
  }

  /** IN 段（命令 / 路径 / 参数正文），带一个复制按钮 —— 命令值得复制。 */
  function ioInHtml(text){
    if (!text) return '';
    return '<div class="io"><div class="io-sec">' +
      '<span class="io-lb">IN</span>' +
      '<span class="io-tx">' + esc(text) + '</span>' +
      '<button class="io-copy" type="button">复制</button>' +
      '</div></div>';
  }

  function addToolRow(p){
    if (!p || !p.id) return;
    // 离线态不该出现「执行中」的工具行：后端已经没了，那个工具永远不会
    // 有结果，行会一直转。同屏「⚡离线」+「执行中」两个矛盾结论（实测）。
    if (body.getAttribute('data-offline') === 'true') {
      rendered('tool/call', { label: p.label || p.name || '', id: p.id, ignored: true });
      return;
    }
    var isTodo = p.label === '更新计划' || p.name === 'TodoWrite';
    // 摘要像路径（含分隔符且以扩展名结尾）时打上 .path：窄面板里要保尾部
    // （文件名才是区分点，头部目录对所有文件都一样）。
    var sumText = String(p.summary || '');
    var isPath = /[\\/]/.test(sumText) && /\.[A-Za-z0-9]{1,6}$/.test(sumText);
    // RTL 盒里 `…`（U+2026）是「中性」字符，会被甩到行尾并把顺序翻过来：
    // `…\sandbox\rect_area.py` 显示成 `ndbox\rect_area.py\…`（实测）。
    // 前缀一个 U+200E（左到右标记）把它钉成 LTR 段落，顺序就正常了。
    if (isPath && sumText.charAt(0) === '…') sumText = '\u200E' + sumText;
    // 工具行之前先把当前正文段收口：正文 → 工具 → 正文 才是真实顺序
    closeAssistantSegment();
    // 思考也要收口：思考 → 工具 → 思考 里，后端整轮只发一次 thinking/end，
    // 不收口的话后一段思考会追加进同一块（实测合并成「思考中 · 6870 字」）
    closeThinkSegment();
    var wrap = document.createElement('div');
    wrap.className = 'tools';
    if (isTodo) wrap.setAttribute('data-todo', 'true');
    // 计划行的参数是整份清单，塞进 IN 段只会是一大坨 JSON，跳过
    var inputText = isTodo ? '' : toolInputText(p);
    wrap.innerHTML = '<div class="row" data-open="' + (isTodo ? 'true' : 'false') + '" data-st="running" tabindex="0" role="button">' +
      '<span class="caret">▶</span>' +
      '<span class="name">' + esc(p.label || p.name || '工具') + '</span>' +
      '<span class="arg' + (isPath ? ' path' : '') + '">' + esc(sumText) + '</span>' +
      '<span class="meta dot-anim">执行中</span></div>' +
      '<div class="slot">' +
      (isTodo ? '<div class="todo"></div><div class="extra"></div>' : ioInHtml(inputText)) +
      '</div>';
    convo.appendChild(wrap);
    var entry = {
      wrap: wrap,
      row: wrap.querySelector('.row'),
      slot: wrap.querySelector('.slot'),
      isTodo: isTodo,
      input: inputText,
      todo: isTodo ? wrap.querySelector('.todo') : null,
      extra: isTodo ? wrap.querySelector('.extra') : null
    };
    toolMap[p.id] = entry;
    // 打本轮标记：轮末按它挑出「这一轮的工具行」收进折叠组。
    // 历史重放走 addHistoryTool，不设这个标记，所以不会被卷进来。
    wrap.setAttribute('data-turn-tool', '1');
    // 工具名要留着：折叠后摘要行写的是「已读取文件并搜索代码」这类
    // **按类别合成的短语**（抄 dsh 的 processTitle），没有名字就合成不出来。
    wrap.setAttribute('data-tool', String(p.name || ''));
    wrap.setAttribute('data-tool-label', String(p.label || ''));
    turnToolCount += 1;
    if (isTodo) {
      // 计划行先占位，todo/update 到了复用这一行，不新建第二行「更新计划」
      pendingTodoRow = entry;
    }
    scrollBottom();
    rendered('tool/call', { label: p.label || p.name || '', id: p.id });
  }

  function finishTool(p){
    if (!p || !p.id) return;
    var entry = toolMap[p.id];
    if (!entry) return;
    var ok = !(p.failed || p.ok === false);
    var outText = String((p && p.output) || '');
    // 后端在「等用户确认」时会先把 AWAIT_CONFIRM 哨兵换成文本、照发一条
    // tool/result{ok:true, durationMs:0, output:'等待确认…'}。按它收口会怎样
    //（实测）：确认条就在下面等着，上面的工具行却已经写着「完成 · 0ms」；
    // 点「取消」后那条 result 依然 ok:true（'[操作已取消 此命令需用户确认]'），
    // 行继续显示「完成」—— 同屏出现「完成」和「已取消」两个结论。
    // 这里把这两种中间态单独画，等真正的执行结果到了再正常收口。
    // 这一行若已经由收口逻辑定过性（打断/未完成），下面所有分支都不许改它的结论。
    var alreadySettled = entry.row.getAttribute('data-settled') === '1';
    if (/^\s*等待确认/.test(outText) || (p.awaitConfirm === true)) {
      // 已被判为「已中断」的行，再接一条「等待确认」就自相矛盾了：确认条
      // 早被 turn/end 收掉，屏幕上却出现「已中断」→ 又变「等待确认」（实测）。
      if (alreadySettled) {
        rendered('tool/result', { id: p.id, awaiting: true, ignored: true });
        return;
      }
      entry.row.setAttribute('data-await-confirm', '1');
      var mAwait = entry.row.querySelector('.meta');
      if (mAwait) { mAwait.className = 'meta'; mAwait.innerHTML = '等待确认'; }
      rendered('tool/result', { id: p.id, awaiting: true });
      return;
    }
    // 「取消」必须优先于任何定性判断，而且两种标记都要认。
    // 只看 data-await-confirm 会怎样（实测）：这一行若已被收口逻辑定过性
    //（data-await-confirm 被移除、只剩 data-await-settled），取消结果就落到
    // 下面的「撤销定性」分支 → settled=false → 按普通成功收口，于是屏幕上
    // 写着「0ms · 完成」（绿色），而这条命令其实被取消了。
    if (/操作已取消|需用户确认/.test(outText) &&
        (entry.row.getAttribute('data-await-confirm') === '1' ||
         entry.row.getAttribute('data-await-settled') === '1')) {
      entry.row.removeAttribute('data-await-confirm');
      entry.row.removeAttribute('data-await-settled');
      // 同一条工具若之后再收到一条结果（后端的「取消」是同一个 id 的第二条
      // result），残留的 data-settled 会把它误判成「中断后返回」。
      // 取消是明确结论，把临时的定性标记一起清掉。
      entry.row.removeAttribute('data-settled');
      var mCancel = entry.row.querySelector('.meta');
      if (mCancel) { mCancel.className = 'meta'; mCancel.innerHTML = '<span class="bad">已取消</span>'; }
      entry.row.setAttribute('data-st', 'err');
      rendered('tool/result', { id: p.id, cancelled: true });
      return;
    }
    // 这一行在等确认、现在拿到真实结果（用户点了确认，后端重放完毕）：
    // 早先若被收口逻辑定过性（「未执行（确认没生效）」），那个结论已经不成立——
    // 命令确实执行了。不清这两个标记，行头会被改写成「失败 · 中断后返回」，
    // 而命令其实成功了（实测）。真实结果到了就以它为准。
    if (entry.row.getAttribute('data-await-confirm') === '1' ||
        entry.row.getAttribute('data-await-settled') === '1') {
      entry.row.removeAttribute('data-await-confirm');
      entry.row.removeAttribute('data-await-settled');
      entry.row.removeAttribute('data-settled');
    }
    // 退出码非 0 就是失败，即使后端说 ok:true。
    // 后端用「有没有框架级错误标签」判失败，Shell 的非 0 退出码不算——
    // 于是 `pytest` 因找不到文件退出 4、`git` 因冲突退出 1，界面全都显示
    // 「完成」，与真正成功的那一行一模一样，只有折叠区第一行藏着
    // `[exit code: 4]`（实测，agent 每天都要跑测试）。
    // 退出码是客观事实，界面按它纠正显示，不动后端的判定。
    var mExit = outText.match(/\[\s*exit code:\s*(\d+)\s*\]/i);
    if (mExit && parseInt(mExit[1], 10) !== 0) ok = false;
    // 退出码 0 也可能不是真成功：命令里带管道时退出码取的是**末端**命令
    // （`pytest | more` 的退出码来自 more，恒为 0）。此时输出里明明写着
    // `1 failed` / `FAILED test_x.py::y`，行头却是绿色「完成」——
    // 用户只看行头就会以为测试全过（实测，AI 很常这么写命令）。
    // 无法从退出码判断时，至少把「输出里有失败信号」这件事说出来。
    var exitSaysOk = !mExit || parseInt(mExit[1], 10) === 0;
    var outHasFail = /\b\d+\s+failed\b|\bFAILED\b|\bTraceback\b|\b\d+\s+errors?\b/i.test(outText);
    var uncertain = ok && exitSaysOk && outHasFail && !!mExit;
    var ms = (typeof p.durationMs === 'number') ? p.durationMs + 'ms' : '';
    var meta = entry.row.querySelector('.meta');
    // 判空与同函数上面的 awaiting 分支保持一致（那里是 if (meta)）。
    // 不能改成 return：下面还要把输出渲染进 slot，提前返回会把结果吞掉。
    if (meta) meta.className = 'meta';
    // 有 diff 就把 +N −M 写进行头。原来行头只有「9ms」，用户要展开才知道
    // 改了几个文件里的几行——「跑过了」不等于「改对了」（实测：改完一个 bug
    // 看到 `写入 sample.js 9ms`，无法判断到底改了什么）。
    // 演示模板的行头本来就有 `+4 −1 · 9ms`，真实路径漏了这一步。
    var hasDiff = !!(p.diff && p.diff.lines && p.diff.lines.length);
    var diffTag = hasDiff
      ? '+' + (p.diff.added || 0) + ' −' + (p.diff.removed || 0)
      : '';
    var parts = [];
    if (!ok) parts.push('<span class="bad">失败</span>');
    // 退出码 0 但输出里有失败信号：命令可能带管道（退出码取末端），
    // 别让人以为干净通过。用 warn 色而不是红，因为确实不知道结果。
    if (uncertain) parts.push('<span class="warn">退出码0·输出含失败</span>');
    if (diffTag) parts.push(esc(diffTag));
    if (ms) parts.push(esc(ms));
    // 这一行已经被收口逻辑定过性（打断/未完成）：迟到的结果不能把它改回「完成」。
    // 输出内容照旧渲染（那些信息仍然有用），只在行头注明它是被打断之后才返回的。
    var settled = entry.row.getAttribute('data-settled') === '1';
    if (settled) {
      entry.row.removeAttribute('data-settled');
      parts.push('<span class="bad">中断后返回</span>');
      if (meta) meta.innerHTML = parts.join(' · ');
      // 不改 data-st：保持 err，行名继续是红的
    } else {
      if (meta) meta.innerHTML = parts.join(' · ');
      entry.row.setAttribute('data-st', ok ? 'ok' : 'err');
    }

    // 展开卡片 = IN（完整输入，见 addToolRow）+ 结果（diff 或输出）。
    // 输入段在 addToolRow 里就画好了，这里重建整块时带上它，顺序不会乱。
    var html = entry.isTodo ? '' : ioInHtml(entry.input);
    if (p.diff && p.diff.lines && p.diff.lines.length) {
      html += '<div class="io-lb io-out-lb">OUT</div>';
      html += '<div class="snippet">';
      p.diff.lines.forEach(function(l){
        var cls = l.type === 'add' ? 'ln add' : (l.type === 'del' ? 'ln del' : 'ln');
        html += '<div class="' + cls + '"><span class="no">' +
          esc(l.no === undefined || l.no === null ? '' : l.no) + '</span>' +
          '<span class="tx">' + esc(l.text) + '</span></div>';
      });
      html += '<div class="foot">+' + (p.diff.added || 0) + ' −' + (p.diff.removed || 0) +
        (p.diff.truncated ? ' · 只显示前 ' + p.diff.lines.length + ' 行改动' : '') + '</div>';
      html += '</div>';
    } else if (p.output) {
      html += '<div class="io-lb io-out-lb">OUT</div>';
      html += '<div class="snippet out">';
      // 只铺前 40 行，但必须说清还有多少行。
      // 原来直接 slice(0, 40) 且 foot 只写「完成」——用户以为输出就这 40 行，
      // 而失败原因往往在最后几行（编译错误、找不着文件），等于把关键信息藏了。
      var outLines = String(p.output).split('\n');
      var head = outLines.slice(0, 40);
      var rest = outLines.length - head.length;
      head.forEach(function(t){
        html += '<div class="ln"><span class="tx">' + esc(t) + '</span></div>';
      });
      if (rest > 0) {
        // 剩下的行不塞进 DOM（一次几百行会拖慢渲染），点开时再填
        html += '<div class="more" data-rest="' + rest + '">还有 ' + rest +
          ' 行未显示 · 点击展开</div>';
        entry.restLines = outLines.slice(40);
      }
      // 脚注要说实话：这一行可能是被打断之后才回来的结果（行头写着
      // 「中断后返回」、data-st 仍是 err）。脚注照打「完成」就是同屏矛盾——
      // 用户在折叠区看到「完成」，行头却是红的（实测）。
      // 判据与行头一致：settled 或 !ok 都不能说完成。
      var footText = (!ok || settled) ? (settled ? '未完成（被中断）' : '执行失败')
        : (uncertain ? '退出码 0，但输出里有失败' : '完成');
      html += '<div class="foot">' + footText + '</div></div>';
    }
    if (entry.isTodo) {
      // 带计划的行：slot 里的 .todo 归 todo/update 独占，这里绝不覆盖 slot，
      // 否则清单会被清空（无论 tool/result 与 todo/update 谁先到）。
      if (entry.extra && html) {
        entry.extra.innerHTML = html;
      }
    } else {
      entry.slot.innerHTML = html;
    }
    scrollBottom();
    rendered('tool/result', { tool: p.id, ok: ok });
  }

  /* ---------- 计划清单 ---------- */

  function renderTodo(p){
    // `|| []` 挡不住「非空非数组」（例如 todos 变成对象/字符串）：那种值会让
    // todos.forEach 抛 TypeError，整条计划更新连 rendered() 回执都发不出去。
    // 类型不对就按空清单降级——少画一个清单，好过静默丢掉整条通知。
    var raw = p && p.todos;
    var todos = Array.isArray(raw) ? raw.filter(function(t){ return t && typeof t === 'object'; }) : [];
    var done = 0;
    todos.forEach(function(t){ if (t.status === 'completed') done++; });

    var target = pendingTodoRow;
    if (!target || !target.todo) {
      // 新建计划行也是插入事件：不断开正文，后续 delta 会写回上方旧气泡
      closeAssistantSegment();
      var wrap = document.createElement('div');
      wrap.className = 'tools';
      wrap.setAttribute('data-todo', 'true');
      wrap.innerHTML = '<div class="row" data-open="true" tabindex="0" role="button">' +
        '<span class="caret">▶</span><span class="name">更新计划</span>' +
        '<span class="arg"></span><span class="meta"></span></div>' +
        '<div class="slot"><div class="todo"></div></div>';
      convo.appendChild(wrap);
      target = {
        wrap: wrap,
        row: wrap.querySelector('.row'),
        slot: wrap.querySelector('.slot'),
        isTodo: true,
        todo: wrap.querySelector('.todo')
      };
      pendingTodoRow = target;
    }

    target.row.querySelector('.arg').textContent = todos.length + ' 项 · ' + done + ' 完成';
    var html = '';
    todos.forEach(function(t){
      var cls = t.status === 'completed' ? 'done' : (t.status === 'in_progress' ? 'now' : '');
      var mk = t.status === 'completed' ? '✓' : (t.status === 'in_progress' ? '●' : '○');
      html += '<div' + (cls ? ' class="' + cls + '"' : '') +
        '><span class="mk">' + mk + '</span>' + esc(t.content) + '</div>';
    });
    target.todo.innerHTML = html;
    scrollBottom();
    rendered('todo/update', { count: todos.length });
  }

  /* ---------- 系统行 ---------- */

  function addSysLine(icon, text, tail, cls){
    // 同 cmdOne：系统行前先收口正文，否则后续增量会写回上方旧气泡
    closeAssistantSegment();
    // 思考同理：系统行插进来就是「这件事之间断了一次」，前后思考不该粘一起
    closeThinkSegment();
    var el = document.createElement('div');
    el.className = 'sys' + (cls ? ' ' + cls : '');
    el.innerHTML = '<span class="ico">' + icon + '</span><span>' + text + '</span>' +
      (tail ? '<span class="tail">' + tail + '</span>' : '');
    convo.appendChild(el);
    scrollBottom();
    return el;
  }

  function updateSysLine(el, icon, text, tail, cls){
    el.className = 'sys' + (cls ? ' ' + cls : '');
    el.innerHTML = '<span class="ico">' + icon + '</span><span>' + text + '</span>' +
      (tail ? '<span class="tail">' + tail + '</span>' : '');
    scrollBottom();
  }

  /* ---------- 复核 ---------- */

  function verifyStart(){
    sysMap.verify = addSysLine('⟳', '独立复核…', '', '');
    rendered('verify/start', {});
  }

  /** 文案与 ui_design.show_verify_result / show_verify_skip 保持一致 */
  function verifyText(p){
    var verdict = (p && p.verdict) || '';
    var blocks = (p && p.blocks) || 0;
    var limit = (p && p.limit) || 0;
    var t = (p && typeof p.durationMs === 'number' && p.durationMs > 0)
      ? (p.durationMs / 1000).toFixed(1) + 's' : '';
    if (verdict === 'pass') {
      var cov = limit ? blocks + '/' + limit : '';
      var tail = [cov, t].filter(Boolean).join(' · ');
      return { icon: '✓', cls: 'ok', text: '复核通过' + (tail ? ' · ' + tail : '') };
    }
    if (verdict === 'uncertain') {
      return { icon: '⚠', cls: 'warn', text: '复核存疑（放行待核查）' + (t ? ' · ' + t : '') };
    }
    if (verdict === 'skip') {
      return { icon: '○', cls: '', text: '无文件改动，无需复核' };
    }
    // verdict 缺失/为 null 时不能落到「未通过」：协议里这个字段不是必填的，
    // 后端某些路径（复核未产生结论、被中断）会留空。判成失败会怎样（实测）：
    // 界面上出现红字「✗ 复核未通过」，而实际上复核什么都没说——
    // 用户以为代码有问题，回头去查一个不存在的缺陷。
    if (verdict !== 'fail' && verdict !== 'forced') {
      return { icon: '·', cls: '', text: '复核已结束（没有结论）' + (t ? ' · ' + t : '') };
    }
    var head = '复核未通过' + (verdict === 'fail' && limit ? ' ' + blocks + '/' + limit : '');
    if (verdict === 'forced') head += '（预算耗尽，强制放行）';
    var gaps = ((p && p.gaps) || []).filter(function(g){ return String(g).trim(); });
    var detail = gaps.length ? ' · ' + gaps.slice(0, 3).join('；') : '';
    return { icon: '✗', cls: 'err', text: head + detail };
  }

  function verifyResult(p){
    var v = verifyText(p);
    if (sysMap.verify && sysMap.verify.parentNode) {
      updateSysLine(sysMap.verify, v.icon, esc(v.text), '', v.cls);
    } else {
      addSysLine(v.icon, esc(v.text), '', v.cls);
    }
    sysMap.verify = null;
    rendered('verify/result', { verdict: (p && p.verdict) || '', text: v.text });
  }

  /* ---------- 上下文压缩 ---------- */

  function compressBegin(){
    sysMap.compress = addSysLine('◌', '压缩上下文中', '', '');
    // 新一次压缩开始：上一轮留下的「压缩完成」行从此不该再被任何回执改写
    compressLine = null;
    rendered('compress/begin', {});
  }

  /** 最近画出的「压缩完成」行：如果这次压缩其实什么也没做，要把它撤掉（见 applyCommandResult） */
  var compressLine = null;

  function compressEnd(p){
    var ok = !p || p.ok !== false;
    // 协议 §4.8：ok/before/after 当前恒为 null（ui.py:643 无论成败都发 null）。
    // 所以这里**没有任何信息**能判断压缩成没成 —— 打「✓ 压缩完成」是在猜：
    // 自动压缩失败时界面上照样一个绿勾（实测）。真正确凿的结论只来自
    // command/result（/compact 的回执），那条路径会把这一行改成失败。
    // 这里改成中性陈述：说「结束了、花了多久」，不承诺成功。
    var hasCounts = p && typeof p.before === 'number' && typeof p.after === 'number';
    var text = !ok ? '压缩失败'
      : (hasCounts ? '压缩结束 · ' + p.before + ' 条 → ' + p.after + ' 条' : '压缩结束');
    var tail = (p && typeof p.durationMs === 'number') ? (p.durationMs / 1000).toFixed(1) + 's' : '';
    if (sysMap.compress && sysMap.compress.parentNode) {
      updateSysLine(sysMap.compress, ok ? '·' : '✗', text, tail, ok ? '' : 'err');
      compressLine = sysMap.compress;
    } else {
      compressLine = addSysLine(ok ? '·' : '✗', text, tail, ok ? '' : 'err');
    }
    sysMap.compress = null;
    // 压缩会裁掉上下文里的消息，会话的「N 条」跟着变。不补拉的话抽屉和 /ls
    // 一直显示压缩前的旧数字，用户看着它判断「还剩多少」，而那个数字是错的
    //（实测：压缩后仍显示 42 条）。轮末的补拉不覆盖这种情况——压缩可能发生在
    // 一轮中间，也可能由命令触发。
    if (REAL && ok) post('sessionList', {});
    rendered('compress/end', { ok: ok });
  }

  /* ---------- 后台任务 ---------- */

  function bgSubmit(p){
    var slot = (p && p.slot) || '';
    addSysLine('⚙', '后台任务 ' + esc(slot) + ' 已提交', esc((p && p.logPath) || ''), '');
    rendered('bg/submit', { slot: slot });
  }

  /* ---------- 框架运行提示（压缩中/重试中/超限…） ---------- */

  /**
   * 后端的运行提示用系统行显示，**不能**混进 AI 正文。
   * 早先这些文案走 stream/delta，用户看到「⚠ 上下文占比超阈值」出现在回复里；
   * 又不进消息历史，切走再切回就消失——同一轮内容前后不一致。
   */
  function showNotice(p){
    // notice 承载「超限 / 保存失败 / 没填 Key」这类关键信息，宁可降级也不能整条丢。
    // text 只有字符串能 split；数字一律 String() 显示，对象/数组这种拼不出人话的
    // （String() 会得到 "[object Object]"）直接忽略这条通知。
    var raw = p && p.text;
    var text;
    if (typeof raw === 'string') text = raw;
    else if (typeof raw === 'number' || typeof raw === 'boolean') text = String(raw);
    else text = '';
    if (!text) return;
    // 文案里可能带多行（例如「⚠ 超限」+「请缩减输入」），逐行落成系统行
    var lines = text.split('\n').map(function (s) { return s.trim(); })
      .filter(function (s) { return s; });
    lines.forEach(function (line) {
      // 文案本身常以 ⚠ 开头，别再补一个图标（否则「⚠⚠ 上下文占比超阈值」）
      var hadMark = /^[⚠!\u26a0]/.test(line);
      var clean = hadMark ? line.replace(/^[⚠!\u26a0]+\s*/, '') : line;
      // 最近两条系统行里已有同一句话就不再画：同一件事经两条路径报两遍
      // （如 turn/end 已写「已停止」，后端通知又来一句「已停止」）只会显得吵。
      var prevs = convo.querySelectorAll('.sys');
      var flat = clean.replace(/\s+/g, '');
      var dupLine = false;
      for (var pi = prevs.length - 1; pi >= 0 && pi >= prevs.length - 2; pi--) {
        // 只比正文那一段：图标与尾注不参与（否则「■ 已停止」与「· 已停止」永远不相等）
        var body2 = prevs[pi].querySelectorAll('span')[1];
        if (body2 && (body2.textContent || '').replace(/\s+/g, '') === flat) { dupLine = true; break; }
      }
      if (dupLine) return;
      // 告警级别从文案推断。后端的文案精简掉 ⚠ 前缀之后，光靠「失败/超限」
      // 这几个词会把「AI 返回空回复」「达到输出上限」「服务端繁忙」降级成
      // 中性点号——它们和「响应流中断」一样都是这一轮没跑成，级别应当一致。
      var isWarn = hadMark ||
        /超出|超限|失败|拦截|中断|空回复|繁忙|上限/.test(line);
      addSysLine(isWarn ? '⚠' : '·', esc(clean), '', isWarn ? 'warn' : '');
    });
    rendered('notice', { text: text.slice(0, 200) });
  }

  /* ---------- 统计 ---------- */

  /**
   * 写状态行里的一项，同时维护 data-empty。
   *
   * data-empty="true" 时 CSS 把整项藏掉：还没有真值时显示一个孤零零的「—」
   * 毫无信息量，窄面板下标签又被让掉，屏幕上就剩「— —」几个破折号并排
   * （用户原话：非常丑陋）。所以「没值」= 整项不显示，而不是显示占位符。
   * 同一个函数负责赋值与清空，就不会出现「有值却仍被藏」或「清了却还在」。
   *
   * @param {string} sel  这一项的 .v 选择器
   * @param {string|null} text  null/'' = 清空（整项隐藏）
   */
  function setStatValue(sel, text) {
    var v = document.querySelector(sel);
    if (!v) return;
    var box = v.closest('[data-empty]') || v.parentNode;
    var has = text !== null && text !== undefined && text !== '';
    v.textContent = has ? text : '—';
    if (box && box.setAttribute) {
      box.setAttribute('data-empty', has ? 'false' : 'true');
    }
  }

  /**
   * 把状态栏的统计清零。
   *
   * 上下文、缓存、费用、余额全是「这个后端进程」的内存累计值：换会话、
   * 新建会话、后端重启后它们都不再代表任何东西。
   * 原来只清上下文：一是换会话后新会话还挂着上一个的占比，二是重启后端时
   * 屏幕上留着已死进程的「缓存 80% ¥0.5000」，而新进程从 0 开始计费——
   * 用户看到一个自己并没花过的金额（实测）。
   * 等新会话/新进程第一轮 stats 到达再更新。
   * 清空 = 传 null，整项隐藏（不是留着「—」占位）。
   */
  function resetContextStats() {
    setStatValue('.status .st-ctx .v', null);
    setStatValue('.status .st-cache .v', null);
    setStatValue('.status .st-cost .v', null);
    setStatValue('.status .st-bal .v', null);
    balanceSeen = false;
  }

  function applyStats(p){
    if (!p) return;
    // 按类名定位而不是「第几个 .v」：缓存/费用/余额会按面板宽度整块隐藏，
    // 用下标既容易随结构调整错位，也看不出改的是哪一项。
    var vCache = '.status .st-cache .v';
    var vCost = '.status .st-cost .v';
    var vBal = '.status .st-bal .v';
    if (typeof p.contextRatio === 'number') {
      // 取整会让 0.4%~1.4% 全都显示成「1%」——窗口 100 万 token 时，
      // 连聊十轮都还是「1%」，这个数字就成了装饰，起不到「快到 50%，该压缩了」的预警作用。
      // 1% 以下给一位小数，能看出它在动；1% 以上取整（4.2% → 4%）。
      var pct = p.contextRatio * 100;
      setStatValue('.status .st-ctx .v',
        (pct < 1 ? pct.toFixed(1) : String(Math.round(pct))) + '%');
    }
    if (typeof p.cacheRatio === 'number') {
      setStatValue(vCache, Math.round(p.cacheRatio * 100) + '%');
    }
    // 费用为 0 时显示「—」而不是「¥0.0000」。
    // 后端把 cost 恒发成数字（`round(float(cost or 0.0), 6)`），刚打开面板、
    // 还没发生任何调用时它就是 0 —— 这时写「¥0.0000」会让用户以为已经计费了，
    // 而且和其余三项的「—」不一致（实测反馈「一开始费用怎么显示 0.00」）。
    // 费用是累计值、单调不减，所以「0」只可能是「还没花」，不存在余额那种
    // 「真的花光了」的歧义，直接判 >0 即可。
    if (typeof p.cost === 'number' && p.cost > 0) {
      setStatValue(vCost, '¥' + p.cost.toFixed(4));
    }
    if (typeof p.balance === 'number') {
      // 后端把 balance 恒发成数字（ui.py:615 `round(float(balance or 0.0), 4)`），
      // 所以 0 有两种含义：还没查到、以及真的花光了。原来一律当成没查到
      //（`if (p.balance > 0)` 才写），代价是余额真的归零之后屏幕上一直挂着
      // 上一次的正数——用户以为还有钱，继续开一个 40 分钟的长任务，跑到一半
      // 被拒（实测）。用「显示过正数」作为分界：见过正数之后再收到 0，
      // 那就是真花光了，必须如实说。
      if (p.balance > 0) {
        balanceSeen = true;
        setStatValue(vBal, '¥' + p.balance.toFixed(2));
      } else if (balanceSeen) {
        setStatValue(vBal, '¥0.00');
      }
    }
    if (p.thinkingEffort) {
      var thinkBtn = document.getElementById('btnThink');
      thinkBtn.textContent = p.thinkingEffort;
      // 按钮只写「显示名」（深度/极致），下拉却是按 data-v（high/max）打勾，
      // 两者的映射在 applyThink 重建条目时就建好了。只改文字不同步勾选会怎样：
      // 每轮 stats 都可能把按钮改成后端推的档位，而下拉勾选只在 initialize /
      // config 时更新 —— 于是按钮写「深度」、点开下拉却勾在「极致」（实测）。
      // 用显示名反查条目（按钮文字 = 该条目的 .lb 文本），查不到就不动：
      // 后端可能推出一个下拉里还没有的自定义档位，这时保持原勾选比乱勾安全。
      var popT = document.getElementById('popThink');
      if (popT) {
        var want = String(p.thinkingEffort).trim();
        var hit = null;
        Array.prototype.forEach.call(popT.querySelectorAll('.pi'), function (pi) {
          var lb = pi.querySelector('.lb');
          if (lb && lb.textContent.trim() === want) hit = pi;
        });
        if (hit) setChecked(popT, hit.getAttribute('data-v'));
      }
    }
    rendered('stats', {
      context: (document.querySelector('.status .st-ctx .v') || {}).textContent || ''
    });
  }

  /* ---------- 一轮开始 / 结束 ---------- */

  function turnBegin(p){
    // 演示态/离线态不该进运行态：这两种状态下没有任何轮次在跑。
    // 进了会怎样（实测）：发送按钮被 CSS 隐藏、停止按钮常显，用户点了停止
    // 什么也不会发生（后端根本不在），界面永久停在「运行中」出不来，
    // 而且宿主侧的 turnActive 也被弄脏，之后重建面板会发假的 turn/begin。
    if (!REAL || body.getAttribute('data-offline') === 'true') {
      rendered('turn/begin', { round: (p && p.round) || 0, ignored: true });
      return;
    }
    body.setAttribute('data-run', 'true');
    resetStopBtn();
    // 记这一轮的起点：tool/result 到达时要知道「这条属于本轮」
    //（历史重放的工具行不带 data-turn，不该被本轮收口卷进去）
    turnStartNode = convo.lastElementChild;
    turnStartTime = Date.now();
    turnToolCount = 0;
    turnThinkCount = 0;
    // 这一轮真的开始了 = 刚才那次发送必然已被后端接受（busy 的情况不会走到
    // turn/begin）。pending 标记和「可能已接受」的保守标记都可以收工了。
    Array.prototype.forEach.call(convo.querySelectorAll('[data-pending]'), function(el){
      el.removeAttribute('data-pending');
    });
    // 上一轮被 busy 拒掉的那条会被就地标成「未发送」。这一轮真的开始了 = 那次
    // 发送已被后端接受、用户也已经重发成功，徽标必须摘掉——留着就是在同一屏上
    // 同时说「发了」和「没发」（原来全文件只有 setAttribute，没有一处 remove，
    // 徽标一直跟到对话区被整段重画）。
    Array.prototype.forEach.call(convo.querySelectorAll('[data-failed]'), function(el){
      el.removeAttribute('data-failed');
    });
    lastSentMaybeAccepted = false;
    // resumed=true：这是面板重建后宿主补发的「这一轮还在跑」，不是新的一轮。
    // 不能重置正文状态——清了 assistantBuf / curAssistant 之后，后续
    // stream/delta 会新建一条助手消息，用户看到同一轮回答被劈成两个气泡
    //（前半段是重建时恢复的历史，后半段是新的）。实测：一轮正文拆成两条。
    if (p && p.resumed) {
      applyTurnGoal(p && p.goal);
      scrollBottom();
      rendered('turn/begin', { round: (p && p.round) || 0, resumed: true });
      return;
    }
    // 助手容器延迟到第一段正文才创建，保证思考/工具行按事件顺序落在正确位置
    curAssistant = null;
    assistantBuf = '';
    resetStreamRender();
    applyTurnGoal(p && p.goal);
    scrollBottom();
    rendered('turn/begin', { round: (p && p.round) || 0 });
  }

  /**
   * turn/begin 里的目标模式状态：按钮文字 + data-on + 下拉勾选 + 轮数输入框。
   *
   * 抽成一处是因为原来 resumed 分支与正常分支各写了一遍刷新逻辑，
   * 于是「改一处漏另一处」：
   *   · resumed 分支只刷按钮、不回写 #goalRounds —— 面板重建后按钮写「1/15」，
   *     点开下拉看到的输入框还是 12（实测）
   *   · 两处都只处理 enabled=true，enabled=false 时按钮还留着上一轮的
   *     「开 · 5/10」、图标仍亮着强调色 —— 多面板/旁路把目标模式关掉后，
   *     这个面板的目标状态就永远陈旧
   * 现在四种组合（开/关 × 新轮/重建）都走同一条路径，不会再分叉。
   */
  function applyTurnGoal(goal){
    var btn = document.getElementById('btnGoal');
    if (!btn) return;
    // 没带 goal 字段就**不改写**：普通轮次的 turn/begin 本来就不带 goal，
    // 这时后端没告诉目标状态，武断改成「关」会把真实开态冲掉。
    // 「明确 enabled:false」才是回落信号（多面板/旁路关掉目标模式的情形）。
    if (!goal) return;
    var on = !!goal.enabled;
    if (on) {
      btn.textContent = '目标模式 开 · ' + (goal.current || 0) + '/' + (goal.max || 0);
      btn.setAttribute('data-on', 'true');
      // 只在 max 有真值时写输入框，避免把 undefined 写成字符串
      if (goal.max) {
        var gi = document.getElementById('goalRounds');
        if (gi) gi.value = String(goal.max);
      }
      var pg = document.getElementById('popGoal');
      if (pg) setChecked(pg, 'on');
    } else {
      btn.textContent = '目标模式 关';
      btn.setAttribute('data-on', 'false');
      var pg2 = document.getElementById('popGoal');
      if (pg2) setChecked(pg2, 'off');
    }
  }

  /* ---------- 折叠摘要的标题：把工具类别合成一句话 ----------
     抄 dsh 的 process-activity.ts + step-process.ts + locale 那三条。
     dsh 的折叠标题不是「工作过程 · 13 次工具调用」这种计数标签，而是把这一段
     做过的**事**说成一句人话：「已读取文件并搜索代码」「执行了命令，修改了文件」。
     计数对用户没有信息量 —— 他要的是「AI 刚才干了什么类别的事」。
     
     三个拼接规则也照抄：
       两个类别      「已读取文件并搜索代码」  ← 共享的「已」前缀只留一个
       三个以上      「已读取文件，执行了命令，修改了文件等」
       同类别去重    按**次数**降序，次数相同按首次出现顺序 */
  var TP_ACTIVITY = {
    read: '已读取文件',
    readImage: '已读取图片',
    search: '已搜索代码',
    edit: '修改了文件',
    write: '已写入文件',
    commands: '执行了命令',
    code: '运行了代码',
    webSearch: '已搜索网页',
    webFetch: '已访问网页',
    subagents: '已协调子智能体',
    plan: '更新了计划',
    questions: '向用户提出了问题',
    tools: '已调用工具',
    thinking: '已完成分析'
  };

  /** 工具名 → 活动类别。顺序与 dsh 的 activity() 一致。
      除了 name 也认 label：历史重放与老版本事件的 payload 里 name 可能缺失，
      只靠 name 会整段退化成「已调用工具」，用户看不出到底干了什么。 */
  function tpActivityName(name, label) {
    var n = String(name || '');
    if (n === 'Read') return 'read';
    if (n === 'Glob' || n === 'Grep') return 'search';
    if (n === 'Edit') return 'edit';
    if (n === 'Write') return 'write';
    if (n === 'Shell' || n === 'Terminal') return 'commands';
    if (n === 'WebSearch') return 'webSearch';
    if (n === 'TodoWrite' || n === 'GoalComplete') return 'plan';
    if (n === 'Serial') return 'tools';
    if (n.indexOf('mcp__') === 0) return 'tools';
    // name 缺失时的兜底：界面上的 label 是中文，逐个对回去
    var l = String(label || '');
    if (l === '读取') return 'read';
    if (l === '搜索内容' || l === '搜索') return 'search';
    if (l === '编辑') return 'edit';
    if (l === '写入') return 'write';
    if (l === '执行命令') return 'commands';
    if (l === '联网搜索' || l === '搜索网页') return 'webSearch';
    if (l === '更新计划') return 'plan';
    return 'tools';
  }

  /**
   * 把一组节点里的工具名合成标题。
   * @param {HTMLElement[]} moving 组内成员
   * @returns {string} 例如「已读取文件并搜索代码」
   */
  function tpTitle(moving) {
    var counts = {};
    var order = [];
    var hasThink = false;
    moving.forEach(function (el) {
      if (el.classList.contains('think')) { hasThink = true; return; }
      var kind = tpActivityName(el.getAttribute('data-tool'), el.getAttribute('data-tool-label'));
      if (!counts[kind]) { counts[kind] = 0; order.push(kind); }
      counts[kind] += 1;
    });
    // 按次数降序、次数相同按首次出现顺序（dsh 也是这个排序）
    var ranked = order.slice().sort(function (a, b) {
      if (counts[b] !== counts[a]) return counts[b] - counts[a];
      return order.indexOf(a) - order.indexOf(b);
    });
    var labels = ranked.slice(0, 3).map(function (k) { return TP_ACTIVITY[k]; });
    if (!labels.length && hasThink) return TP_ACTIVITY.thinking;
    if (!labels.length) return TP_ACTIVITY.tools;
    var first = labels[0];
    if (labels.length === 1) return first;
    if (labels.length === 2) {
      // 共享前缀「已」只留一个：「已读取文件」+「已搜索代码」→ 已读取文件并搜索代码
      var second = labels[1];
      if (first.charAt(0) === '已' && second.charAt(0) === '已') second = second.slice(1);
      return first + '并' + second;
    }
    // 三个以上：第一个原样，后面去掉共享的「已」前缀，末尾加「等」
    var rest = labels.slice(1).map(function (l) {
      return l.charAt(0) === '已' ? l.slice(1) : l;
    });
    return first + '，' + rest.join('，') + (ranked.length > 3 ? '等' : '');
  }

  /**
   * 把一组节点收成一个「工作过程」折叠组，挂在它们原本的位置上。
   *
   * 抄 dsh 的 TurnProcessNodeView（ui-chat/chat/TurnProcessNodeView.tsx）：
   *   一轮结束后，那一整串工具调用对用户已经没价值了 —— 他要看的是最终回答。
   *   dsh 收成一条带下边框的行：「已工作 · 1分23秒」/「已停止」/「失败」，
   *   点开才展开细节。这里同样处理。
   *
   * ## 为什么要把 DOM 搬进一个容器，而不是只加 CSS 隐藏
   *
   * 只隐藏的话，那些节点还在 convo 的顶层兄弟流里 —— 后面新到的内容
   * （下一轮正文、系统行）会插在它们**中间**，视觉上「折叠区」就被撑破了。
   * 搬进一个独立容器后，它就是一个整体：要么整块收起，要么整块展开。
   *
   * 实时轮次（foldTurnProcess）与历史重放（renderHistory）共用这一份 ——
   * 单一真源。分两份的后果很实在：改了一边忘了另一边，历史里的折叠干脆
   * 就不存在（第一版就是这样，用户切回历史会话看不到任何变化）。
   *
   * @param {Node[]} nodes  要收进去的节点（工具行 / 思考块），任意顺序
   * @param {Object} o
   *   o.reason       'done' | 'cancelled' | 'error' | 'history'
   *   o.elapsedMs    该轮耗时；0 / 不传表示未知（历史里就没有时长）
   *   o.headline     这一组是否承担「整轮收口」的文案（一组只有最后那段是）
   *   o.animate      建组时是否播「收起」动画。实时轮次收口时 true
   *                  （屏幕上正摊着一大片工具行，必须让它收得有过程）；
   *                  历史重放时 false（刚渲染出来的东西不该先展开再收起）
   * @returns {HTMLElement|null} 折叠组；没有可收的节点时返回 null
   */
  function buildTurnProc(nodes, o) {
    o = o || {};
    var reason = o.reason || 'done';
    var moving = (nodes || []).filter(function (el) {
      return el && el.parentNode === convo;
    }).sort(function (a, b) {
      return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
    });
    if (!moving.length) return null;

    // 按 class 区分，不按 data-turn-* 标记：那两个标记只有实时路径会打，
    // 历史重放走 addHistoryTool/addHistoryThink 不打标记，靠标记数会全是 0。
    var tools = moving.filter(function (el) { return el.classList.contains('tools'); });
    var thinks = moving.filter(function (el) { return el.classList.contains('think'); });
    var stopped = (reason === 'cancelled' || reason === 'aborted' || reason === 'interrupted');
    var failed = reason === 'error';

    var box = document.createElement('div');
    box.className = 'turn-proc';
    box.setAttribute('data-open', 'false');
    box.setAttribute('data-reason', reason);

    var row = document.createElement('button');
    row.type = 'button';
    row.className = 'tp-row';
    /* 标题 = 把这一段做过的**事**说成一句人话（抄 dsh 的 processTitle）：
       「已读取文件并搜索代码」「执行了命令，修改了文件」。
       原来写的是「工作过程」/「已工作」这种空标签 + 「13 次工具调用」计数 ——
       计数对用户没有信息量，他要知道的是「AI 刚才干了什么类别的事」。 */
    var label = tpTitle(moving);
    /* 耗时与状态并排放右侧，不挤进标题里：
       标题要留给「干了什么」，时间/成败是附注。 */
    var metaText = '';
    var elapsed = o.elapsedMs || 0;
    if (stopped) metaText = '任务被打断';
    else if (failed) metaText = '这一轮失败';
    else if (elapsed > 0) {
      var sec = Math.round(elapsed / 1000);
      metaText = sec >= 60 ? (Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒') : (sec + ' 秒');
    }
    // 计数留在 DOM 属性上（测试与无障碍要用），但不显示出来
    box.setAttribute('data-tool-count', String(tools.length));
    box.setAttribute('data-think-count', String(thinks.length));
    row.innerHTML = '<span class="tp-caret">▶</span>' +
      '<span class="tp-label">' + esc(label) + '</span>' +
      (metaText ? '<span class="tp-meta">' + esc(metaText) + '</span>' : '');

    var bodyBox = document.createElement('div');
    bodyBox.className = 'tp-body';
    // 外层负责高度动画与（展开后的）滚动上限，内层承载真实内容。
    // 双层是抄 dsh 的 ChatGroupSeat：外层 .body 有 max-height + overflow-y:auto，
    // 内层 .content 是实际内容。合成一层的话，高度动画会和自己内部的滚动条打架 ——
    // 收缩过程中 max-height 仍然生效，测到的 offsetHeight 就已经被夹过，
    // 动画起点不是屏幕上真实高度，会先「跳」一下再收。
    var innerBox = document.createElement('div');
    innerBox.className = 'tp-inner';

    // 插在第一个被搬走的节点原来的位置，顺序原样保留
    convo.insertBefore(box, moving[0]);
    box.appendChild(row);
    box.appendChild(bodyBox);
    bodyBox.appendChild(innerBox);
    moving.forEach(function (el) { innerBox.appendChild(el); });

    // 收起态下补一条分割线，把「这一段结束了」与下面的正文分开。
    // 用独立元素而不是 border-bottom：展开时正文会被 bodyBox 推远，边框就没意义了。
    var hr = document.createElement('div');
    hr.className = 'tp-sep';
    box.appendChild(hr);

    row.setAttribute('aria-expanded', 'false');
    /* 这些节点在折叠前一直是「展现在外面」的，所以这里要做一次真动画：
       从当前可见高度收缩到 0 —— 用户看到的是「工具过程被收起来了」，
       而不是「一大片内容瞬间消失」。
       注意顺序：必须先量高度再设 hidden。反过来设的话 offsetHeight 已经是 0，
       tpCollapse 会走「本来就没高度」的快速路径，动画根本不会发生（实测）。 */
    if (o.animate) {
      tpCollapse(bodyBox, function () { bodyBox.setAttribute('hidden', ''); });
    } else {
      // 历史重放：内容刚建出来就没展开过，直接是收起态，没有「收起过程」可演
      bodyBox.setAttribute('hidden', '');
    }
    row.addEventListener('click', function () {
      var on = box.getAttribute('data-open') === 'true';
      box.setAttribute('data-open', on ? 'false' : 'true');
      row.setAttribute('aria-expanded', on ? 'false' : 'true');
      if (on) {
        tpCollapse(bodyBox, function () { bodyBox.setAttribute('hidden', ''); });
      } else {
        tpReveal(bodyBox, true);
      }
    });
    return box;
  }

  /* ---------- 折叠 / 展开的过渡（抄 dsh 的 flow-motion.ts）----------
     dsh 的时序（FADE_OUT_MS=70 / CLOSE_DELAY_MS=50 / CLOSE_MS=110）：
       0ms      开始淡出，同时不动高度
       50ms     透明度已到 ~28%，开始收高度
       160ms    高度归零，此时几乎不可见
     先淡出再收高度，是因为「一边透明一边缩」会让人眼觉得内容在抽搐；
     等它快看不见了再收，感知上是「淡出后自然塌陷」。
     缓动用 cubic-bezier(0.4,0,0.2,1)（Material 的 standard easing）。 */
  var TP_FADE_MS = 70;
  var TP_CLOSE_DELAY_MS = 50;
  var TP_CLOSE_MS = 110;
  var TP_SLACK_MS = 80;
  var TP_EASE = 'cubic-bezier(0.4, 0, 0.2, 1)';

  function tpReduced(){
    return typeof matchMedia === 'function'
      && matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /** 清掉动画留下的内联样式，让 CSS 的自然尺寸/滚动上限接管 */
  function tpClearMotion(el){
    el.style.transition = '';
    el.style.height = '';
    el.style.marginTop = '';
    el.style.opacity = '';
    el.style.overflow = '';
    el.removeAttribute('data-motion');
  }

  /**
   * 维护展开态的上下渐隐标记（抄 dsh 的 useProcessScroll）。
   *
   * 为什么需要：展开后组体有 400px 上限，超出的内容要靠内部滚动。但滚动条
   * 在深色窄面板里几乎看不见，用户不会知道「这块还能往下滚」—— 他会以为
   * 工具就这么多。边缘渐隐是最省字的一句话说「上面/下面还有」。
   */
  function tpBindFades(el){
    if (el.__tpFades) return;
    var sync = function () {
      var up = el.scrollTop > 1;
      var down = el.scrollTop < el.scrollHeight - el.clientHeight - 1;
      el.setAttribute('data-fade-up', up ? 'true' : 'false');
      el.setAttribute('data-fade-down', down ? 'true' : 'false');
    };
    el.addEventListener('scroll', sync, { passive: true });
    if (typeof ResizeObserver === 'function') {
      var ro = new ResizeObserver(sync);
      ro.observe(el);
      var inner = el.firstElementChild;
      if (inner) ro.observe(inner);
      el.__tpFades = { sync: sync, ro: ro };
    } else {
      el.__tpFades = { sync: sync, ro: null };
    }
    sync();
  }

  /**
   * 收缩一个块到 0 高，结束后回调。
   * @param {HTMLElement} el 要收的块（.tp-body）
   * @param {Function} done 收完（高度归零）后调用 —— 调用方在这里设 hidden
   */
  function tpCollapse(el, done){
    // 正在动画的同一块先取消，避免两次收尾互相覆盖
    if (el.__tpCancel) el.__tpCancel();
    var h = el.offsetHeight;
    var mt = parseFloat(getComputedStyle(el).marginTop) || 0;
    if (tpReduced() || (h === 0 && mt === 0)) { tpClearMotion(el); done(); return; }
    el.setAttribute('data-motion', 'collapse');
    el.style.transition = 'none';
    el.style.overflow = 'hidden';
    el.style.height = h + 'px';
    el.style.marginTop = mt + 'px';
    void el.offsetHeight;      // 强制 reflow，否则下面的 transition 不会生效
    el.style.transition = 'opacity ' + TP_FADE_MS + 'ms ' + TP_EASE + ', '
      + 'height ' + TP_CLOSE_MS + 'ms ' + TP_EASE + ' ' + TP_CLOSE_DELAY_MS + 'ms, '
      + 'margin-top ' + TP_CLOSE_MS + 'ms ' + TP_EASE + ' ' + TP_CLOSE_DELAY_MS + 'ms';
    el.style.opacity = '0';
    el.style.height = '0px';
    el.style.marginTop = '0px';
    var finished = false;
    var finish = function () {
      if (finished) return;
      finished = true;
      el.removeEventListener('transitionend', onEnd);
      clearTimeout(timer);
      el.__tpCancel = null;
      tpClearMotion(el);
      done();
    };
    var onEnd = function (ev) {
      if (ev.target === el && ev.propertyName === 'height') finish();
    };
    // transitionend 有不来的情况（元素被移出文档、浏览器丢帧），必须有兜底
    var timer = setTimeout(finish, TP_CLOSE_DELAY_MS + TP_CLOSE_MS + TP_SLACK_MS);
    el.addEventListener('transitionend', onEnd);
    el.__tpCancel = finish;
  }

  /**
   * 把一个 hidden 的块从 0 长到自然高度。
   * @param {HTMLElement} el 要展开的块
   * @param {boolean} capped 展开后是否套用滚动上限（内容超高时不再无限拉长）
   */
  function tpReveal(el, capped){
    if (el.__tpCancel) { el.__tpCancel(); el.__tpCancel = null; }
    el.removeAttribute('hidden');
    // 先按「无上限」量出真实高度，再决定动画终点。
    // 不这么做会怎样：hidden 刚摘掉时 max-height 已经生效，量到的高度是夹过的，
    // 而动画终点写成夹过的值 —— 展开到一半又跳一下（实测）。
    el.removeAttribute('data-capped');
    var natural = el.offsetHeight;
    var mt = parseFloat(getComputedStyle(el).marginTop) || 0;
    if (tpReduced()) {
      tpClearMotion(el);
      if (capped) {
        el.setAttribute('data-capped', 'true');
        tpBindFades(el);
        if (el.__tpFades) el.__tpFades.sync();
      }
      return;
    }
    el.setAttribute('data-motion', 'reveal');
    el.style.transition = 'none';
    el.style.overflow = 'hidden';
    el.style.opacity = '0';
    el.style.height = '0px';
    el.style.marginTop = '0px';
    void el.offsetHeight;
    el.style.transition = 'height ' + TP_CLOSE_MS + 'ms ' + TP_EASE + ' ' + TP_CLOSE_DELAY_MS + 'ms, '
      + 'margin-top ' + TP_CLOSE_MS + 'ms ' + TP_EASE + ' ' + TP_CLOSE_DELAY_MS + 'ms, '
      + 'opacity ' + TP_CLOSE_MS + 'ms ' + TP_EASE + ' ' + TP_CLOSE_DELAY_MS + 'ms';
    el.style.height = natural + 'px';
    el.style.marginTop = mt + 'px';
    el.style.opacity = '1';
    var finished = false;
    var finish = function () {
      if (finished) return;
      finished = true;
      el.removeEventListener('transitionend', onEnd);
      clearTimeout(timer);
      el.__tpCancel = null;
      tpClearMotion(el);
      // 动画结束才套滚动上限：动画中用 max-height 会截断高度过渡
      if (capped) {
        el.setAttribute('data-capped', 'true');
        // 边缘渐隐要等上限生效（能滚了）再绑，否则算出的 scrollHeight 是没夹过的
        tpBindFades(el);
        if (el.__tpFades) el.__tpFades.sync();
      }
    };
    var onEnd = function (ev) {
      if (ev.target === el && ev.propertyName === 'height') finish();
    };
    var timer = setTimeout(finish, TP_CLOSE_DELAY_MS + TP_CLOSE_MS + TP_SLACK_MS);
    el.addEventListener('transitionend', onEnd);
    el.__tpCancel = finish;
  }

  /**
   * 实时轮次收口：把这一轮的工具行与思考块**按段**折起来。
   *
   * ## 为什么必须分段（第 29 轮修复）
   *
   * 一轮里 AI 是「做几件事 → 说一句 → 再做几件 → 再说一句」这样推进的。
   * 原来这里把所有 `[data-turn-tool]` 一次性收进**一个**组，再把组插到
   * 第一个工具的位置 —— 于是后面几段的工具全被搬到最前面，正文被挤到末尾。
   * 实测一发就是：`[正文A][组: 全部4条工具][正文B][正文C]`，
   * 而真实顺序是 `[工具][正文A][工具][正文B][工具][正文C]`。
   * 用户看到的是「AI 先把所有工具跑完才说话」，跟当时的过程完全对不上。
   *
   * dsh 的做法（ui-chat/conversation-nodes/process-groups.ts）是同一条：
   *   「遇到带正文的 assistant-step 就 flush 一次组」
   * 这里用 DOM 相邻性表达同一个意思：**夹了别的东西（正文/用户插话/系统行）
   * 就断开成新的一段**，每段各自成为一个折叠组。
   *
   * 什么情况不折叠：
   *   · 本轮没有任何工具/思考（纯聊天）→ 没东西可折，加一行摘要反而碍眼
   *   · 节点已经属于某个折叠组 → 幂等，重复的 turn/end 不再套一层
   */
  function foldTurnProcess(reason) {
    /* 只取「还没被收进任何折叠组」的节点。
       原来直接 querySelectorAll('[data-turn-tool]') 会把**上一轮**已经收好的
       那些也捞出来 —— 它们的 closest('.turn-proc') 非空，于是第一轮那个
       「已经收过」的守卫立刻 return，第二轮的新工具行就永远折不上（实测）。 */
    var nodes = Array.prototype.slice.call(convo.children).filter(function (el) {
      return (el.hasAttribute('data-turn-tool') || el.hasAttribute('data-turn-think'))
        && !el.closest('.turn-proc');
    });
    // 本轮没有任何工具/思考（纯聊天）→ 没东西可折，加一行摘要反而碍眼
    if (!nodes.length) return null;

    // 按 DOM 相邻性切段：上一个处理过的节点不是自己的前一个兄弟 = 中间夹了别的东西
    var segments = [];
    var cur = [];
    nodes.forEach(function (el) {
      if (cur.length && cur[cur.length - 1].nextElementSibling !== el) {
        segments.push(cur);
        cur = [];
      }
      cur.push(el);
    });
    if (cur.length) segments.push(cur);

    var last = null;
    segments.forEach(function (seg, i) {
      /* 只有最后一段承担整轮收口的文案（耗时 / 被打断 / 失败）。
         中间段写「工作过程」—— 文案与颜色都别再重复一遍，
         否则一屏出现三行「已工作」，读者分不清哪些是同一轮。 */
      var tail = (i === segments.length - 1);
      last = buildTurnProc(seg, {
        reason: reason,
        elapsedMs: tail && turnStartTime ? Math.max(1000, Date.now() - turnStartTime) : 0,
        headline: tail,
        animate: true
      }) || last;
    });
    turnStartNode = null;
    return last;
  }

  function turnEnd(p){
    // 真正的收口到了，兜底定时器就不该再开火
    if (cancelSettleTimer) { clearTimeout(cancelSettleTimer); cancelSettleTimer = null; }
    var reason = (p && p.reason) || 'done';
    // 幂等：一轮只收口一次。重复的 turn/end 会怎样（实测）：第二次进来时
    // data-run 已经是 false、正文也渲染过了，却又补一行「■ 已打断」——
    // 用户莫名其妙多出一条中断提示。宿主侧已经尽力不重复发，但事件重放、
    // 补发路径都可能撞上，这里兜住。
    // intermediate 不算收口（后面还会续跑），不参与这个守卫。
    if (!(p && p.intermediate) && body.getAttribute('data-run') !== 'true') {
      // 回执仍要带上当时的正文：这条渲染记录是诊断与测试唯一的观察面，
      // 少了 assistant 就分不清「正文丢了」和「这一轮本就没跑起来」
      //（实测被 ui/rpc 回归测试判成「正文没保留」）。
      rendered('turn/end', { reason: reason, duplicate: true, assistant: briefAssistant() });
      return;
    }
    // intermediate：同一请求内部的中间轮收口（目标模式自动续跑）。
    // 这时不能退出运行态——退出会让停止按钮消失，用户以为跑完了，
    // 实际它还在自动续跑下一轮（实测）。
    if (p && p.intermediate) {
      if (curThink) endThink({});
      // 中间轮里也可能有工具行没收到结果（后端被掐断、或工具自身异常没回）。
      // 这是本轮最后一次收口机会——不扫就会一直转到下一轮结束，
      // 用户看到一个永远「执行中」的行。
      settleRunningTools(reason);
      // 中间轮同样要把正文整段重渲染：下一轮 turn/begin 会把 assistantBuf
      // 清空，分段渲染留下的畸形排版（<p>/<tr>/<blockquote> 被切碎）就再也
      // 修不回来了，会永久留在屏幕上（实测 <p>3 vs 1、<tr>1 vs 9）。
      if (curAssistant && assistantBuf) {
        renderAssistantFinal();
      }
      rendered('turn/end', { reason: reason, intermediate: true });
      // 中间轮也会往会话里加消息，条数要跟上（同末尾那条补拉）
      if (REAL) post('sessionList', {});
      return;
    }
    body.setAttribute('data-run', 'false');
    resetStopBtn();
    if (curThink) endThink({});
    // 轮次已结束 = 后端不再等任何确认。挂着的确认条必须收掉，
    // 否则界面上还摆着可点的「确认执行」（实测：确认中点停止，
    // 后端已放弃等待，用户点确认只得到「等待后端执行…」永远等不到回执）。
    clearConfirmBoxes('这一轮已结束');
    // 还在 running 的工具行必须收尾。不处理会怎样（实测）：用户点停止时
    // 后端不会为那个被掐断的工具发 tool/result，界面于是留着
    // 「▶ 写入 sample.js 执行中」——同一屏下又写着「■ 已打断」。
    // 用户无法判断最后一步到底执行完没有，而它多半没执行。
    // final:true —— 这一轮真的收口了（不是中间轮），「等待确认」的行也要定性：
    // 确认条刚在上面被 clearConfirmBoxes 收掉，那条命令不会再执行了。
    settleRunningTools(reason, { final: true });
    // 轮次结束把正文整段重渲染一次。这一步不能省，也不能设长度阈值：
    // 分段渲染为了「流式不卡」允许在行尾切，而软换行（单换行）在 Markdown
    // 里属于同一段落——切两半就是两个 <p>。实测 40KB 日志流式后 <p> 从 1 个
    // 变成 1001 个，段落间距全乱。原来 >32KB 时跳过重渲染以省一次 600ms，
    // 结果把错误的排版永久留在屏幕上（回答结束后用户还会回看、复制它）。
    // 一次性的 600ms 换排版正确，值得。
    if (curAssistant && assistantBuf) {
      renderAssistantFinal();
    }
    /* 正文定稿之后再收工具行 —— 顺序要紧：
       提前收会把「正在流式写的那条助手气泡」一起卷进折叠区（它此刻在 DOM 里
       也是 convo 的子节点），用户在收起状态下就看不到答案了。 */
    foldTurnProcess(reason);
    if (reason === 'cancelled') {
      // 兜底收口（后端回了 cancel 却没发 turn/end）要说明白，
      // 否则用户看到的和正常打断一模一样，就分不清「真停了」和「没等到确认」。
      addSysLine('■', '已停止',
        cancelSettleTimerFired ? '后端无回应，界面按已停止处理' : '', '');
      cancelSettleTimerFired = false;
    } else if (reason === 'error') {
      // 出错后必须给一个「重试」出口。不给会怎样（实测）：限流/网络抖动/
      // 模型 5xx 是最常见的中断，用户唯一的补救是把刚才那段长 prompt 重新敲
      // 一遍——整条用户消息连复制按钮都没有，只能鼠标框选。
      // Cline / Roo / Cursor / Copilot Chat 都把「重试」放在错误行上。
      var errLine = addSysLine('✗', '请求失败', '', 'err');
      if (errLine && lastSentText) {
        // 把这一刻的正文**捕获**进闭包，不要点的时侯再读 lastSentText。
        // 不捕获会怎样（实测）：屏幕上留着两条错误行时，点第一条的「重试」
        // 发出去的是**最后一条**的内容 —— 用户点的是上面那条，发的是下面那条。
        var retryText = lastSentText;
        var retryBtn = document.createElement('button');
        retryBtn.className = 'retry';
        retryBtn.textContent = '重试';
        retryBtn.addEventListener('click', function(){
          if (body.getAttribute('data-run') === 'true') return;  // 还在跑，不重复发
          // 输入框里已有草稿时不能覆盖：那是用户正在写的新内容，
          // 覆盖掉等于把他的手稿删了（实测）。
          if (input.value.trim()) {
            sysLine('·', '输入框有内容，未重试', '');
            scrollBottom();
            return;
          }
          if (retryBtn.parentNode) retryBtn.parentNode.removeChild(retryBtn);
          input.value = retryText;
          autoGrowInput();
          sendChat();
        });
        errLine.appendChild(retryBtn);
      }
    } else if (reason === 'round_limit') {
      addSysLine('·', '已达轮数上限', '', 'warn');
    } else if (reason === 'goal_complete') {
      addSysLine('✓', '目标已完成', '', 'ok');
    }
    curAssistant = null;
    pendingTodoRow = null;
    // 正文缓冲必须清掉。不清会怎样（实测）：这一轮的正文已经在屏幕上，
    // 而 assistantBuf 还留着全文；之后任何触发整段重画的 initialize
    //（断网重连、压缩后、切会话回来）都会把 assistantBuf 当成「正在流式的
    // 后半截」再画一遍——6 条消息画成 7 条，最后一句回答重复出现。
    assistantBuf = '';
    // 这一轮里用户想发但被守卫拦下的内容：现在正文已定稿，插入系统行不会拆散
    // 任何东西，可以如实告诉他「刚才那条没发出去、还在输入框里」。
    // 不在这里补的话，用户按 Enter 后几十秒内看不到任何反应（第 15 轮验收点名
    // 「零反馈」）。当场提示又会把正文劈成两个气泡，所以只能延到这里。
    if (blockedWhileRunning) {
      blockedWhileRunning = false;
      sysLine('·', '刚才那条没有发出去（这一轮还在回答）', '内容仍在输入框');
    }
    rendered('turn/end', { reason: reason, assistant: briefAssistant() });
    // 轮末补拉一次会话列表。
    // 不拉会怎样（实测）：会话的「N 条」和列表顺序都来自 session/list 快照，
    // 而这一轮又往当前会话里加了几条——界面显示 15 条、后端实际 17 条，
    // 要等下次切会话才纠正。用户看着这个数字判断「聊了多少」，错的数字会误导。
    if (REAL) post('sessionList', {});
  }

  /* ---------- 错误 / 离线 ---------- */

  /* 最后一条助手正文的纯文本。用于判断「这句错误是不是已经在正文里说过了」。 */
  function lastAssistantText(){
    var list = convo.querySelectorAll('.msg.assistant');
    if (!list.length) return '';
    return list[list.length - 1].textContent || '';
  }

  function notifyError(p){
    var msg = (p && p.message) || '后端错误';
    // 401 这类错误后端分了两次说：正文里是一整行 `[错误: XXX]`（llm._auth_fail），
    // 再把 `[错误: ` 和 `]` 剥掉发一条 error 通知（agent_loop._failure_text）。
    // 界面上两处都铺就成了同一句话连说两遍——实测 401 时同屏：
    //   [错误: AI调度失败，请检查 narnat.json 配置文件]   ← 正文
    //   ✗ AI调度失败，请检查 narnat.json 配置文件          ← 这条
    // authHintIfNeeded 早就按「正文说过就不复述」处理了，这里对齐同一个取舍。
    // 只认「正文含 [错误: 前缀 且 含这条 message」的精确形态：否则正文里恰好
    // 出现一个短词（如「失败」）就会把真正的错误整条吞掉。
    var t = lastAssistantText();
    if (msg && t.indexOf('[错误:') >= 0 && t.indexOf(msg) >= 0){
      rendered('error', { message: msg, merged: true });
      return;
    }
    addSysLine('✗', esc(msg), esc((p && p.detail) || ''), 'err');
    rendered('error', { message: msg });
  }

  function showOffline(p){
    var message = (p && p.message) || '后端未连接';
    var detail = (p && p.detail) || '';
    // 掉线之后的下一次握手必然连着新进程：状态栏里的上下文/费用是旧进程的，
    // 不标脏会一直显示（实测重启后端后仍是「上下文 42% ¥0.5000」，
    // 而新进程的上下文是空的）。
    statsStale = true;
    body.setAttribute('data-offline', 'true');
    body.setAttribute('data-run', 'false');
    // 后端掉线的这条路径不走 turnEnd（没有 turn/end 事件），工具行会一直
    // 停在「执行中」。这里补一次收口，否则界面同屏写着「离线」和「执行中」。
    // final:true 必须传：下面那条 clearConfirmBoxes 会把确认条删掉，之后
    // 再没有任何事件能触及「等待确认」的行——不传 final，settleRunningTools
    // 会跳过它（awaiting && !forGood → return），那一行就永久停在
    // 「等待确认 + 执行中」，与红色「后端未连接」同屏且无法自愈。
    resetStopBtn();
    settleRunningTools('cancelled', { final: true });
    // 确认条也要收：后端崩了就没有人再等这条 y/n 了。留着它，用户点
    // 「确认执行」只得到「已提交确认，等待后端执行…」，永远等不到回执
    //（实测：崩在等确认那一刻，离线屏上留着一张僵尸确认条）。
    clearConfirmBoxes('后端已离线');
    // 断连瞬间还可能挂着一条「刚点发送、还没被接受」的消息：请求根本没转给
    // 后端（实测重启后端那一刻发一句，转发 0 条 RPC），气泡却以已发送的样子
    // 永久留在对话里，输入框已空、字也找不回来。这里把它撤回并退回输入框，
    // 用户重连后直接再按一次发送即可。
    var hadPending = settlePendingMessage();
    if (hadPending) {
      sysLine('✗', '消息未发送', '', 'err');
      scrollBottom();
    }
    // 只说结果 + 原因：离线就是「后端未连接」，detail 是唯一的诊断线索，
    // 截断到一行保留。原来这里还会拼两条「该去改什么配置」的建议，
    // 每次掉线都铺一大段，而绝大多数掉线会自己重连上。
    var reason = String(message || '').trim();
    if (detail) reason = reason + ' · ' + String(detail).trim();
    reason = reason.slice(0, 160);
    if (!offlineShown) {
      offlineShown = true;
      offlineLine = addSysLine('⚡', '后端未连接', esc(reason), 'err');
    } else if (offlineLine) {
      updateSysLine(offlineLine, '⚡', '后端未连接', esc(reason), 'err');
    }
    var info = {
      message: message,
      detail: detail,
      assistant: briefAssistant(),
      offline: body.getAttribute('data-offline'),
      text: offlineLine ? offlineLine.textContent : ''
    };
    rendered('offline', info);
    rendered('rpc/error', info);
  }

  /* ---------- 初始化：切真实态 ---------- */

  function rebuildModelPop(options){
    var pop = document.getElementById('popModel');
    Array.prototype.forEach.call(pop.querySelectorAll('.pi'), function(pi){
      pi.parentNode.removeChild(pi);
    });
    var anchor = pop.querySelector('.pd');
    options.forEach(function(name){
      var d = document.createElement('div');
      d.className = 'pi';
      d.setAttribute('data-v', name);
      d.setAttribute('data-on', 'false');
      d.innerHTML = '<span class="ck"></span><span class="lb">' + esc(name) + '</span>';
      pop.insertBefore(d, anchor);
    });
  }

  // 档位名 → 中文显示名。只在**演示态**与后端没给 options 时兜底；
  // 真实态一律用后端下发的显示名（见 rebuildThinkPop）。
  // 这里必须和后端默认配置对齐（loader.py:56 `{"high": "高", "max": "全开"}`）：
  // 原来写 '最高'，而按钮走的是 stats 里的后端显示名「全开」——同一屏上
  // 按钮写「全开」、点开下拉写「最高」，用户会怀疑档位到底切没切（实测）。
  var THINK_LABEL = { low: '低', medium: '中', high: '高', max: '全开', off: '关闭' };
  /** 后端下发的 {档位: 显示名}，供「/thinking 高」这类中文反查用 */
  var thinkLabels = null;
  function rebuildThinkPop(options){
    var pop = document.getElementById('popThink');
    Array.prototype.forEach.call(pop.querySelectorAll('.pi'), function(pi){
      pi.parentNode.removeChild(pi);
    });
    var anchor = pop.querySelector('.pd');
    // 原来用 Object.keys(options)：数组的 key 是 "0"/"1"/"2"，于是 data-v 变成
    // 下标——`setChecked(pop,'high')` 永远匹配不到，按钮显示英文档位名 `high`
    // 而不是「高」，`/thinking 高` 也无法在下拉里打勾（实测）。
    // 直接用元素值当 data-v；显示名优先取后端下发的（用户可以在配置里改名）。
    var isObj = options && !Array.isArray(options) && typeof options === 'object';
    var list = Array.isArray(options) ? options : Object.keys(options || {});
    thinkLabels = isObj ? options : null;
    list.forEach(function(v){
      var d = document.createElement('div');
      d.className = 'pi';
      d.setAttribute('data-v', v);
      d.setAttribute('data-on', 'false');
      var lbText = (isObj && options[v]) ? options[v] : (THINK_LABEL[v] || String(v));
      d.innerHTML = '<span class="ck"></span><span class="lb">' + esc(lbText) + '</span>';
      pop.insertBefore(d, anchor);
    });
  }

  function applyGoal(goal){
    var enabled = !!(goal && goal.enabled);
    var max = (goal && goal.maxRounds) || 10;
    var current = (goal && goal.current) || 0;
    setChecked(document.getElementById('popGoal'), enabled ? 'on' : 'off');
    document.getElementById('btnGoal').textContent = enabled
      ? '目标模式 开 · ' + current + '/' + max
      : '目标模式 关';
    // 同步 data-on：CSS 1268-1275 只在 [data-on="true"] 时才把图标点上强调色，
    // 全文件原来没有一处写这个属性，那条高亮规则等于永不命中。
    document.getElementById('btnGoal').setAttribute('data-on', enabled ? 'true' : 'false');
    var roundsInput = document.getElementById('goalRounds');
    if (roundsInput) roundsInput.value = String(max);
  }

  function applyPlugins(plugins){
    var pop = document.getElementById('popPlugin');
    Array.prototype.forEach.call(pop.querySelectorAll('.pi'), function(pi){
      pi.parentNode.removeChild(pi);
    });
    // 先过滤掉畸形项：缺 name 的条目会让 data-v="undefined"，点一下会往一个
    // 不存在的插件发 configSet；更糟的是后续 esc(pl.label) 若碰到 null 会抛
    // TypeError，把 applyInitialize 后半段（workdir / 会话 / 历史）整段打断，
    // 界面停在「半初始化」——顶栏没会话名、历史也不出来（实测）。
    // 一个坏数据项不该让整个握手失效，所以跳过它、继续渲染其余项。
    if (!Array.isArray(plugins)) plugins = [];
    plugins = plugins.filter(function(pl){
      // name 允许字符串或数字（数字转成字符串照样能用），只丢真正没有名字的项
      return pl && pl.name !== null && pl.name !== undefined && pl.name !== '';
    });
    var anchor = pop.querySelector('.pd');
    var enabled = 0;
    plugins.forEach(function(pl){
      var on = pl.enabled !== false;
      if (on) enabled++;
      var d = document.createElement('div');
      d.className = 'pi';
      d.setAttribute('data-v', pl.name);
      d.setAttribute('data-on', on ? 'true' : 'false');
      d.innerHTML = '<span class="sw" data-on="' + (on ? 'true' : 'false') + '">' +
        (on ? 'on' : 'off') + '</span><span class="lb">' + esc(pl.label || pl.name) + '</span>';
      pop.insertBefore(d, anchor);
    });
    document.getElementById('btnPlugin').textContent =
      enabled === plugins.length ? '插件 ' + plugins.length : '插件 ' + enabled + '/' + plugins.length;
    // 「有插件开着」就算开：图标档里「插件 0/4」这行字是看不见的（font-size:0），
    // 没有这个属性用户就分不出「全开」和「全关」。
    document.getElementById('btnPlugin').setAttribute('data-on', enabled > 0 ? 'true' : 'false');
  }

  /**
   * 清掉对话区里可能残留的连接状态行。
   *
   * 这个函数的存在是为了兼容「旧版本写过、新版不再写」的情况：用户升级前
   * 打开的会话里可能已经有一条 `● 已连接后端 · v16.8.0 · 协议 1`，
   * 升级后重绘历史时它不会被历史覆盖（历史是消息，这条是系统行）——
   * 所以要主动扫掉，不能只做到「以后不再写」。
   * 现在界面上不再显示这句话：它每次开面板都占第一屏最显眼的位置，
   * 而「连上了」这件事没有可动作性；真正要看的是「没连上」，那有离线横幅。
   */
  function dropConnLine(){
    Array.prototype.forEach.call(
      convo.querySelectorAll('.sys[data-conn="1"]'),
      function(el){ if (el.parentNode) el.parentNode.removeChild(el); }
    );
  }

  /**
   * 撤掉「当前状态」类提示里已经不成立的那些。
   *
   * 这些提示说的是「后端现在的状态」（缺密钥 / 配置文件坏了 / 掉线），
   * 不是「历史事件」。状态变好之后还留着，界面就在指路去做已经做完的事：
   * 实测填好 Key 并重启后，「⚠ 还没填 API Key」还在；配置修好后
   * 「配置文件有问题」「修好后需要重启」两行还在，同屏又写着「已连接后端」。
   * @param {object} p 这次 initialize 的结果
   */
  function dropStaleNotices(p){
    // 连上了：离线提示的去重标记必须清掉。
    // 不清会怎样（实测）：第一次掉线时提示过一次；重连正常；再掉线时
    // 因为 dupKey 与上次相同而整条静默 —— 用户点会话、点新建全都毫无反应，
    // 也看不到任何解释，只能以为界面坏了。
    lastOfflineHint = '';
    // 重连了：旧连接上的请求不可能再有回执，所有等回执的锁必须清掉。
    // 不清会怎样（实测）：掉线时点过的会话残留 pendingSwitchTo，重连后
    // 再点同一条 → 0 请求 0 提示，非得先点别的会话才能解开。
    unlockAllSess();
    pendingSwitchTo = '';
    // configKeyQueue 也属于「等回执」的状态，同样必须清。
    // 漏了会怎样（实测）：切配置的请求还在飞时掉线 → 队列里留着那个 key →
    // 重连后下一条**失败**的配置回执会先把这个陈旧 key 消费掉，
    // 于是真正失败的那一项没被回退，界面显示与后端长期不一致。
    configKeyQueue.length = 0;
    // 先把上一轮留下的「当前状态」提示全部撤掉，再按这次的状态重新画。
    // 只在「状态变好」时删会怎样（实测）：状态一直没变（比如一直没填 Key），
    // 每次重连都会再画一行，屏幕上堆成两行、三行一模一样的提示。
    if (apiKeyHintLine) {
      if (apiKeyHintLine.parentNode) apiKeyHintLine.parentNode.removeChild(apiKeyHintLine);
      apiKeyHintLine = null;
    }
    if (cfgWarnLines.length) {
      cfgWarnLines.forEach(function(el){
        if (el && el.parentNode) el.parentNode.removeChild(el);
      });
      cfgWarnLines = [];
    }
    // 重新连上了：撤掉离线横幅的引用（节点已在函数开头删掉）
    if (offlineLine) {
      if (offlineLine.parentNode) offlineLine.parentNode.removeChild(offlineLine);
      offlineLine = null;
      offlineShown = false;
    }
    // 新进程（掉线后重连/重启）的状态栏数据是上一个进程的：清零，等新的 stats
    if (statsStale) {
      statsStale = false;
      resetContextStats();
    }
  }

  /**
   * 把一段文本压成「用来比对的指纹」：去掉空白与 Markdown 记号，取前 40 个字符。
   *
   * 用途：判断「这条历史消息屏幕上有没有」。两侧来源不同——
   * 历史里是原始 markdown（`**粗体**`、` ```js `），DOM 里是渲染后的纯文本
   * （`粗体`）。逐字比较会永远失配，于是每次握手都被判成「没画过」，
   * 触发一次不必要的整段重画，把工具行/思考块一起抹掉。
   *
   * 再带上长度档：只比前 40 字会让「好的!」和「好的」、"帮我改这个文件"
   * 和「帮我改这个文件里的 bug」都撞成同一个指纹 → 后一条被判成「已画过」
   * 而漏画（实测）。长度按 10 字符分档，既区分长短，又能容忍 markdown
   * 渲染造成的少量长度差。
   */
  function normCmp(s) {
    var t = String(s == null ? '' : s);
    var fp = t.replace(/[\s*_`#>~\[\]()!|+\-]/g, '').slice(0, 40);
    return fp + '#' + Math.round(t.length / 10);
  }

  function applyInitialize(p){
    // 是不是第一次握手。后端自动重连、重开面板都会再发一次 initialize，
    // 每次都清空对话区的话，用户正看着的内容会凭空消失（且不会回显）。
    // 记下「握手前是否正在跑」：面板重建时宿主会先补发缓存的 initialize、
    // 再补 turn/begin{resumed}，随后我又补发一次 fresh initialize ——
    // 那次不能把运行态抹掉，否则重建后停止按钮消失，那一轮就停不下来了
    //（实测：一轮还在跑，面板重建后按钮没了，只能关窗口）。
    var wasRunning = body.getAttribute('data-run') === 'true';
    var firstInit = !REAL;
    REAL = true;
    FIRST_INIT_SEEN = true;
    // 重新连上了：离线横幅必须从对话区里**删掉**，不只是把变量置空。
    // 原来只赋 null，节点一直留在屏幕上（实测：改回 executable、重启成功后，
    // 界面上「⚡ 离线：后端未连接」还挂着，同屏又写着「已连接后端」）。
    if (offlineLine) {
      if (offlineLine.parentNode) offlineLine.parentNode.removeChild(offlineLine);
    }
    offlineShown = false;
    offlineLine = null;
    body.setAttribute('data-offline', 'false');
    if (!wasRunning) {
      body.setAttribute('data-run', 'false');
    }
    // 隐藏「示例数据」提示条
    body.setAttribute('data-real', 'true');
    if (firstInit) {
      // 首次切真实态：把对话区的任何残留清空（生产骨架本来就是空的，
      // preview 演示态注入的内容也在这里让位）
      resetConversationDom();
      SHOW_ALL = false;
    }

    var model = (p && p.model) || {};
    var think = (p && p.thinking) || {};
    var goal = (p && p.goal) || {};
    var plugins = (p && p.plugins) || [];
    var session = (p && p.session) || {};

    RPC_STATE.initialized = true;
    RPC_STATE.protocol = (p && p.protocol) || 0;
    RPC_STATE.init = p || null;

    // 与下面 plugins 的口径保持一致：空数组也要重建（清空旧条目），
    // 只有字段整个缺失才跳过。原来判 `model.options.length`，
    // 后端把可选模型收窄成空时，下拉里还留着上一批名字，点了会发一个
    // 后端不认的模型名（实测脏残留，和 plugins:[] 是同一类问题）。
    if (Array.isArray(model.options)) rebuildModelPop(model.options);
    if (model.current) {
      setChecked(document.getElementById('popModel'), model.current);
      document.getElementById('btnModel').textContent = model.current;
    }
    if (think.options) rebuildThinkPop(think.options);
    if (think.effort) {
      setChecked(document.getElementById('popThink'), think.effort);
      var lb = document.querySelector('#popThink .pi[data-v="' + think.effort + '"] .lb');
      document.getElementById('btnThink').textContent = lb ? lb.textContent : think.effort;
    }
    applyGoal(goal);
    // plugins 为空数组时也必须处理 —— 原来 `if (plugins.length)` 会把整段跳过，
    // 于是「重新握手时后端把插件全删/全关」这种情况，界面还留着上一批插件名：
    // 按钮写着旧数字，下拉里点一下还会往一个后端已经不认的插件发 configSet
    // （实测脏数据残留）。applyPlugins([]) 本身是安全的：它清空下拉并把按钮
    // 写成「插件 0」。只有字段整个缺失（undefined）时才跳过。
    if (Array.isArray(p && p.plugins)) applyPlugins(plugins);
    // workdir 不再显示：它与 VS Code 当前工作区目录是同一件事，用户已经看得到。
    // 这里原来按 `chips[1]` 下标去改工作目录 chip —— 而下标依赖 #chipSess 占着
    // chips[0]。chipSess 已删除（入口挪到顶栏），chips[1] 变成 undefined，
    // 于是工作目录永远停在模板占位，连「复制路径」都复制不出东西。
    // 现在整段去掉，不再有死代码去改一个不存在的节点。

    SESS = [];
    // 后端 session.current 为 null 表示游离态（还没 /save），必须清空而不是沿用模拟名
    var prevCUR = CUR;
    CUR = String(session.current || '');
    CUR_IS_CHILD = !!session.isChild;
    // 后端说「现在不在任何会话里」，而界面还显示着某个会话的内容 → 必须清掉。
    // 典型场景：后端进程重启/重连后 current 归 null，chip 已经变成「(未保存)」，
    // 对话区却还留着上一个会话的整段消息——用户以为那段对话还在上下文里，
    // 实际后端上下文是空的（实测，属「显示 A、实际按 B」）。
    // 只在「从有会话变成没会话」时清，避免每次重连都清掉刚聊的内容。
    //
    // 但清空必须留下说明：后端崩溃后宿主会先发「这一轮被中断」「后端已退出」，
    // 紧接着重连的 initialize 把整个 convo 抹掉——用户读了一半的回答消失、
    // 历史消失，屏幕上只剩一句「已连接后端」，完全不知道为什么（实测）。
    // 所以先记住原会话名，清完补一行可恢复的提示。
    if (!CUR && prevCUR && !firstInit) {
      resetConversationDom();
      authHintShown = false;
      resetContextStats();
      // 内容没有真的丢：还在会话文件里，从抽屉能点回去。一行说清即可。
      if (prevCUR) sysLine('·', '上下文已清空', '上一段仍在会话「' + prevCUR + '」');
    }
    renderSessions();

    // 重载窗口/重开面板后 webview 是全新的，对话区空白。后端在
    // session.messages 里把当前会话的历史带过来了，这里画回去。
    // 不回填会怎样（实测）：用户重载一次窗口，刚才那几十分钟的对话在界面上
    // 全没了，只剩一行「已连接后端」——他以为内容丢了（内容其实还在内存里）。
    //
    // 判据用「后端条数 vs 已画条数」，三种情况分开处理：
    //   ① 有确认条在等用户点：绝不能重画（会把确认条清掉，后端永远等不到应答）
    //   ② 运行中且已画过历史：后端多出来的那些，对话区已经实时画过，只更新计数
    //   ③ 其余（空闲 / 全新 webview 还没画过）：整体重画，拿到最新一版
    var initMsgs = (session && session.messages) || [];
    // ↑ 能翻到的提问来自磁盘上的全局输入历史（与终端形态同一个文件）。
    // initialize 会在重载窗口、重连后端时**重复到达**：整份替换而不是追加，
    // 所以同一份重复灌不会让列表变长或乱序（幂等）。
    if (p && Array.isArray(p.inputHistory)) {
      var persist = [];
      p.inputHistory.forEach(function(t){
        var s = String(t == null ? '' : t).trim();
        if (s) persist.push(s);
      });
      if (persist.length > HIST_MAX) persist = persist.slice(-HIST_MAX);
      HIST_PERSIST = persist;
      // 会话历史这条分支可能不重画（已画过），但 HIST 必须跟着刷新
      seedHistory(initMsgs);
    }
    // 后端说「当前会话就是空的」时也必须清屏。
    // 原来整段逻辑包在 `if (initMsgs.length)` 里，0 条就直接跳过：若这次握手
    // 把当前会话换成一个空会话，顶栏变了、对话区还留着上一个会话的整段内容
    //（实测 12 条消息原样留着，用户以为那些话还在这个会话的上下文里）。
    if (!initMsgs.length && historyDrawn > 0 && CUR !== historyDrawnFor) {
      resetConversationDom();
      authHintShown = false;
      resetContextStats();
      sysLine('·', '已切换到' + (CUR ? '会话「' + esc(CUR) + '」' : '一个空会话'), '');
    }
    if (initMsgs.length) {
      var pendingBox = convo.querySelector('.confirm[data-real="true"]');
      // 判据只用 historyDrawn，不看「对话区空不空」。
      // 用 convo.children.length 判会怎样（实测）：面板重建后 turn/begin{resumed}
      // 和 stream/delta 先到，对话区已经不空了（有正在流式的正文），于是被判成
      // 「已经画过历史」→ 历史一条都不恢复，屏幕上只有后半截回答。
      // historyDrawn 是「这个 webview 画过几条后端历史」，重建后必然是 0，
      // 所以它才是「要不要画历史」的正确依据。
      //
      // 再看会话名：CUR 已经被上面赋成后端这次说的当前会话。若这次历史属于
      // 另一个会话（宿主补发的 lastInit 是旧会话快照，fresh 才换成真实当前会话），
      // 只更新计数就会把上一个会话的消息留在屏幕上——顶栏写着 beta，
      // 对话区却是 alpha 的 11 条，直到下次切会话才纠正（实测）。
      // 会话变了就必须重画，这条信号的优先级高于「已画过几条」。
      var histSess = CUR;
      // 条数变少 = 后端上下文缩水了（压缩、裁剪），界面必须跟着缩。
      // 只更新计数会怎样（实测）：压缩后后端只剩 3 条，界面还摆着 6 条，
      // 用户以为那 6 条还在上下文里，后续追问时模型看不到其中一半。
      var shrunk = initMsgs.length < historyDrawn;
      // 「只更新计数」只适用于两种情况：
      //   ① 条数没变：没什么可画的
      //   ② 条数变多，且多出来的那条对话区里已经有了（这一轮实时流出来的）
      // 关键判据不能只看 wasRunning：面板重建后 historyDrawn 是**用宿主补发的
      // 缓存快照**画出来的（例如 1 条），fresh 里已经是 2 条——那第 2 条正是
      // 用户刚问的问题，屏幕上从来没见过。照 wasRunning 跳过重画，用户重开面板
      // 后看不到自己问的什么（实测）。
      // 所以真正的判据是：未画的第一条，在对话区里找得到吗？
      var firstUndrawn = (initMsgs.length > historyDrawn) ? initMsgs[historyDrawn] : null;
      var undrawnShown = true;
      if (firstUndrawn) {
        var wantText = normCmp(firstUndrawn.text);
        var found = false;
        if (wantText) {
          var nodes = convo.querySelectorAll('.msg');
          for (var ui2 = 0; ui2 < nodes.length; ui2++) {
            var el2 = nodes[ui2];
            var isAssist = el2.className.indexOf('assistant') >= 0;
            // user 消息的正文在 .text 里；assistant 的正文直接铺在 .msg 内
            var got = isAssist
              ? String(el2.textContent || '')
              : String((el2.querySelector('.text') || {}).textContent || '');
            // 必须用 normCmp 而不是逐字比较：DOM 里是**渲染后**的文本
            //（`**粗体**` 变成 <b>、代码块变成多个 span），而历史里是原始
            // markdown。逐字比会永远失配 → 误判「没画过」→ 触发重画 → 把
            // 已有的工具行/思考块一起抹掉（实测）。
            if (normCmp(got) === wantText) { found = true; break; }
          }
        }
        undrawnShown = found;
      }
      var nothingToDraw = (initMsgs.length === historyDrawn) || undrawnShown;
      if (historyDrawn > 0 && histSess === historyDrawnFor && !shrunk && nothingToDraw) {
        // 已画过历史且还是同一个会话：只更新计数。
        // 重画会顶掉正在流式的正文，也会抹掉工具行/思考块/计划占位行
        // （那些行没有任何恢复路径——后端不会重发 tool/call）。
        // 确认条挂着时更不能重画——那会把条清掉，后端永远等不到应答。
        historyDrawn = Math.max(historyDrawn, initMsgs.length);
      } else {
        // 这个 webview 还没画过后端历史（全新 / 重建后第一次握手）：整体重画。
        //
        // 确认条若已挂着（重建后后端先把 confirm/request 补发到了空对话区），
        // 重画会把它清掉。做法是把节点先摘出来，画完历史再原样挂回去——
        // 历史和确认条都得在，缺哪个都失效（实测：只保确认条则历史 0 条，
        // 只保历史则后端永远等不到 y/n）。
        // 用摘节点而不是 cloneNode：保住节点身份，任何绑在它上面的状态都不丢。
        var keepConfirm = pendingBox;
        if (keepConfirm && keepConfirm.parentNode) keepConfirm.parentNode.removeChild(keepConfirm);
        // 工具行与思考块也必须捞出来。它们是「这一轮到底干了什么」的唯一记录，
        // 而 `_history_payload` 会主动过滤掉 role:tool —— 重画等于永久删除。
        // 典型触发：首次握手时还没聊（historyDrawn=0），之后流式画出了一串工具行，
        // 再来一次带历史的 initialize → 整段重画 → 工具行全没（实测 tools=0）。
        //
        // 但只在**运行中**保留。空闲时重画意味着历史被裁剪过（压缩）或换了会话，
        // 那时旧工具行已经和它所属的轮次脱钩——照留就会把它们全部搬到末尾，
        // 屏幕上出现「20 条压缩后的历史 + 全部旧工具行」这种错乱（实测）。
        // 这也和「历史会话重开时不显示工具行」的既有行为一致。
        var keepRuns = wasRunning
          ? Array.prototype.slice.call(convo.querySelectorAll('.tools, .think'))
          : [];
        keepRuns.forEach(function (el) {
          if (el.parentNode) el.parentNode.removeChild(el);
        });
        // toolMap 也必须留着：resetConversationDom 会把它换成新对象，而挂回去的
        // 那些行若不在 map 里，之后到达的 tool/result 找不到 entry 就被静默丢弃，
        // 行会永远停在「执行中」。
        var keepToolMap = toolMap;
        // 思考块的「正在流式」句柄也要留着：节点虽然被 keepRuns 摘出来挂回去了，
        // 但 resetConversationDom 把 curThink 置成了 null，之后的 thinking/delta
        // 找不到它就在末尾新建一块——同一个思考被劈成两段（实测）。
        var keepCurThink = curThink;
        //
        // 重画前必须先把「正在流式的正文」捞出来再放回去。不捞会怎样（实测）：
        // 面板重建时 turn/begin{resumed} 已到、流式正文已经画了几十字，
        // 这时带历史的 initialize 一重画，那几十字就被抹掉——用户看着
        // 回答凭空少了半截（18/32 → 剩下 18）。
        var keepBuf = assistantBuf;
        // 但若这段正文后端历史里已经有了（重画的正是它），就不能再画一遍。
        // 不判会怎样（实测）：`normCmp` 对链接/有序列表/代码块这类在 DOM 里
        // 变形较大的内容会失配 → 误判「没画过」→ 重画（历史画了这段）→
        // 又把 keepBuf 画一次 → 屏幕上同一段回答出现两遍。
        var bufAlreadyInHist = false;
        if (keepBuf) {
          var wantBuf = normCmp(keepBuf);
          if (wantBuf) {
            for (var hi = initMsgs.length - 1; hi >= 0; hi--) {
              if (initMsgs[hi] && initMsgs[hi].role === 'assistant' &&
                  normCmp(initMsgs[hi].text) === wantBuf) { bufAlreadyInHist = true; break; }
            }
          }
        }
        resetConversationDom();
        historyDrawn = initMsgs.length;
        historyDrawnFor = histSess;
        authHintShown = false;
        renderHistory(initMsgs);
        // ↑ 能翻到的提问：全局历史 + 这个会话的提问（重载后 HIST 本来是空的）
        seedHistory(initMsgs);
        if (keepBuf && !bufAlreadyInHist) {
          assistantBuf = keepBuf;
          renderAssistant();
        }
        // 工具行/思考块按原顺序接回末尾：它们在时间上属于「本轮」，而本轮
        // 内容就在历史末尾附近，接在最后不会错位。顺序由节点数组保证。
        keepRuns.forEach(function (el) {
          convo.appendChild(el);
        });
        if (keepRuns.length) {
          toolMap = keepToolMap;
          if (keepCurThink) curThink = keepCurThink;
        }
        if (keepConfirm) {
          convo.appendChild(keepConfirm);
          pendingConfirmLine = keepConfirm;
          scrollBottom(true);
        }
        // renderHistory 画完会把 assistantBuf 清掉，后续 stream/delta 才能正常接。
        // 压缩后条数变少时也不再多说一句：屏幕上的内容就是当前上下文，
        // 「已被压缩/还在记录里」属于实现细节，用户看着内容变短自然明白。
      }
    }

    // 密钥为空：首屏就说清楚，别等用户发完第一条消息撞上 401。
    // 一句话 + 一个动作即可：这里是状态提示，不是操作教程。
    //
    // 这一类提示是「当前状态」而不是「历史事件」，状态好了就必须撤掉。
    // 只加不删会怎样（实测）：用户填好 Key、重启后端，界面上那条
    // 「⚠ 还没填 API Key」一直留着，还指路去填 —— 与实际状态相反。
    dropStaleNotices(p);

    if (p && p.apiKeyMissing) {
      apiKeyHintLine = sysLine('⚠', 'narnat.json 缺少接口密钥',
        'Ctrl+Shift+P → Narnat: 打开配置文件', 'warn');
    }

    /* 不再在对话区写「已连接后端 · v16.8.0 · 协议 1」这一行。
       理由：正常使用时它每开一个面板就出现一次，占着第一屏最显眼的位置，
       而它承载的信息（连上了、版本号、协议号）对用户没有任何可动作性——
       真正需要知道的是「没连上」，那有独立的离线横幅。
       宿主重连时会把上一行删掉（见 dropConnLine），不会留下过期的状态。 */
    dropConnLine();
    // 配置文件坏了：后端会「按默认配置继续」，界面上一切正常，直到发消息
    // 收到 401——用户会去重填密钥，而真因是 JSON 语法错了。
    // 只给一句结论 + 一个动作：原来每个警告一行、再加一行「修好后需要重启」
    // 和一行命令面板入口，一次启动能刷出四五行，把真正要看的东西埋了。
    var cfgWarn = (p && p.configWarnings) || [];
    if (cfgWarn.length) {
      // 配置整份没读进来时，「缺少接口密钥」是它的**后果**不是另一个问题，
      // 同屏两句 narnat.json 只会让人以为要改两处。留一条带文件名的即可。
      if (apiKeyHintLine) {
        if (apiKeyHintLine.parentNode) apiKeyHintLine.parentNode.removeChild(apiKeyHintLine);
        apiKeyHintLine = null;
      }
      cfgWarnLines.push(sysLine('⚠', 'narnat.json 有问题',
        'Ctrl+Shift+P → Narnat: 打开配置文件', 'err'));
    }
    scrollBottom();
    rendered('initialize', {
      assistant: briefAssistant(),
      protocol: (p && p.protocol) || 0,
      model: model.current || '',
      session: session.current || ''
    });
  }

  /* ---------- 会话 / 命令 / 配置回执 ---------- */

  function applySessionList(p){
    // 类型校验必须在 SESS = [] 之前：这条通知原来先清空再遍历 groups，
    // groups 一旦不是数组（后端字段类型变了），异常就发生在「已经清空、
    // 还没 renderSessions」的中间态——内部列表空了、画面还是旧的，
    // 用户点哪条都进不去。所以宁可不认这条通知，也不进那个状态。
    if (!p || !Array.isArray(p.groups)) return;
    // 后端读不到会话目录时会带回 error（见 server.py `_m_session_list`：
    // 「只回空 groups 等于告诉用户你的会话全没了」）。
    // 前端原来完全不读这个字段 —— 于是磁盘读失败 / 目录被占用时，
    // 抽屉里显示「还没有已保存的对话」，用户以为**所有会话都被删了**，
    // 而实际只是这次读不到。必须明说。
    // 注意：不 early-return —— groups 可能是不完整的，能显示多少先显示多少，
    // 只是额外挂一行错误提示，让用户知道"看到的不全"。
    //
    // 这一行是**状态类提示**（描述"现在读不到"），不是历史事件，所以下次
    // 读取成功时必须撤掉：留着会变成"永久报错"的假象——用户明明恢复正常了，
    // 屏幕上还挂着 Permission denied，以为一直没好。与 dropStaleNotices 里
    // 「连上了就撤掉离线/缺 Key 提示」同一处理原则。
    if (p.error) {
      SESS_LIST_ERROR = true;
      if (sessListErrLine && sessListErrLine.parentNode) {
        sessListErrLine.parentNode.removeChild(sessListErrLine);
      }
      sessListErrLine = sysLine('✗', '读取会话列表失败', String(p.error).slice(0, 200), 'err');
      scrollBottom();
    } else {
      SESS_LIST_ERROR = false;
      if (sessListErrLine) {
        if (sessListErrLine.parentNode) sessListErrLine.parentNode.removeChild(sessListErrLine);
        sessListErrLine = null;
      }
    }
    var groups = p.groups;
    SESS = [];
    var hidden = 0;
    // groups 只是后端的传输容器（按今天/更早切段），前端不再显示分组，
    // 直接按顺序平铺成一列。
    groups.forEach(function(g){
      if (!g || !Array.isArray(g.sessions)) return;
      g.sessions.forEach(function(s){
        if (!s || typeof s !== 'object') return;
        SESS.push({
          name: s.name,
          time: s.time || '',
          count: s.count || 0,
          // ghost：父会话文件已缺失，只有子会话还活着。列表里要标「无记录」，
          // 不能显示成 `01-01 08:00, 0条` 这种假数据（实测会误导）。
          ghost: !!s.ghost,
          // children 同样只在真是数组时才映射：非数组的 truthy 值会让 .map 抛错，
          // 而这个函数已经清过 SESS 了——异常等于把列表留在半成品状态。
          children: (Array.isArray(s.children) ? s.children : []).filter(function(c){
            return c && typeof c === 'object';
          }).map(function(c){
            return { name: c.name, status: c.status || 'new', time: c.time || '', count: c.count || 0 };
          })
        });
      });
      if (g.hidden) hidden += g.hidden;
    });
    // 后端因截断而没给出来的条数，renderSessions 需要它来决定「展开其余 N 条」
    SESS_HIDDEN = hidden;
    // current 为 null 表示游离态（未进入任何会话）：必须清空 CUR，
    // 否则删掉当前会话后列表里还高亮着已经不存在的那个。
    CUR = String((p && p.current) || '');
    CUR_IS_CHILD = !!(p && p.isChild);
    renderSessions();
    // 冷启动（跨进程/第二天）时后端 current 是 null，对话区只剩一句
    // 「已连接后端」——这里不再补提示：会话列表在顶栏（点会话名即见），
    // 用户不需要被解释一遍「内容还在磁盘、点抽屉能回去」。
    // 用户已展开，但这次回来的是截断列表（切会话会触发不带 all 的刷新）：
    // 补一次 all，避免界面看起来自己收起来了。
    if (REAL && SHOW_ALL && hidden > 0) {
      post('sessionList', { all: true });
    }
    rendered('session/list', { count: SESS.length, shown: SHOW_ALL ? SESS.length : Math.min(SESS.length, VISIBLE_SESSIONS), hidden: hidden });
  }

  /**
   * 后端自动保存会话的通知。
   * 同一份保存会发两次：先 switched:false（已写盘），后 switched:true
   * （状态已切成正式会话）。此前界面只靠 /save 的 command/result 刷新，
   * 自动保存这条路径一条回执都没有——顶栏和输入框旁的会话名一直停在
   * 「(未保存)」，会话列表也是旧的，用户以为没存上。
   * 幂等：同一个 name + switched 组合只处理一次（宿主重连、面板重建会重发）。
   */
  var lastSavedKey = '';
  function applySessionSaved(p){
    if (!p) return;
    var name = String(p.name || '');
    var switched = !!p.switched;
    var key = name + '|' + (switched ? '1' : '0');
    if (key === lastSavedKey) return;
    lastSavedKey = key;
    if (!switched) {
      // 已写盘、状态还没切：先把名字显示出来（用户最急着确认的就是这个）
      if (name) {
        setChipSess(name);
        sysLine('✓', '已保存为「' + esc(name) + '」', '', 'ok');
      }
      if (REAL) post('sessionList', {});
      rendered('session/saved', { name: name, switched: false });
      return;
    }
    // 状态已切到正式会话：CUR 和顶栏一起跟过去。
    // 不跟会怎样：后续 /save、新建对话都按旧的空名字处理（自动保存完再点
    // 「保存当前为…」会又存一份，会话列表里出现两条内容相同的记录）。
    var cur = (typeof p.current === 'string' && p.current) ? p.current : name;
    if (cur) {
      CUR = cur;
      renderSessions();
      var br = document.querySelector('#sessBtn .branch');
      if (br) br.textContent = cur;
      setChipSess(cur);
    }
    /* 保存完成后要**真的回到那个会话**：用户点的是抽屉里的「保存当前为…」，
       保存成功却还把抽屉摊在那儿，他会以为没成（实测被点名「理应自动跳转」）。
       历史已经在屏幕上（他本来就在这段对话里），所以只收抽屉、不重画。 */
    setDrawer(false);
    if (REAL) post('sessionList', {});
    rendered('session/saved', { name: name, switched: true, current: cur });
  }

  /**
   * 把后端给的历史消息画回对话区。
   * 进入历史会话时若不画，界面看着像「这个会话是空的」——
   * 明明后端有 24 条消息，用户却以为丢了。
   */

  /**
   * 历史消息里的思考块。
   * 后端已把思考写进会话 json（assistant 项的可选字段 thinking），进入历史
   * 会话时也要能看见。DOM 结构与流式思考块完全一致（不另造样式），
   * 默认折叠；展开/收起由 convo 上的委托监听统一处理（它按 .row 找到最近的
   * .think/.tools 并同步 data-open），所以这里不需要单独绑事件。
   * @param {string} text 思考全文
   * @returns {HTMLElement}
   */
  function addHistoryThink(text){
    var wrap = document.createElement('div');
    wrap.className = 'think';
    wrap.setAttribute('data-open', 'false');
    wrap.innerHTML = '<div class="row" data-open="false" tabindex="0" role="button">' +
      '<span class="caret">▶</span><span class="name"></span></div>' +
      '<div class="slot"><p></p></div>';
    // 思考全文可能很长且含 < >，用 textContent 而不是拼 HTML
    wrap.querySelector('.name').textContent = '思考 · ' + text.length + ' 字';
    wrap.querySelector('.slot p').textContent = text;
    convo.appendChild(wrap);
    return wrap;
  }

  /**
   * 是不是后端注入的内部指令（目标模式「轮数上限收尾」/「自动续跑」）。
   *
   * 后端只用 user 角色注入这两条固定文案（core/agent.py 交互与 headless 各一份，
   * 文案完全相同）：
   *   ① `[系统提示]目标模式已达到轮数上限（N轮），任务尚未完成。请向用户总结…`
   *   ② `[自动续跑]已完成N轮，任务：<goal_task>\n请继续推进任务。\n[完成审计] …`
   *      （尾部是 config/defaults.py 的 GOAL_AUDIT_HINT 固定文案）
   *
   * 判据必须**整条匹配**，不能只看开头：用户自己完全可以打一句
   * `[系统提示] 我引用一段话…`（引用别人的话、贴日志都很常见），
   * 只看开头的旧判据会把这种消息当内部行渲染、只显示首行 120 字，
   * 第二行起全部丢失（实测）。
   * ②里的 goal_task 是用户任务原文、内容任意，所以只锚定开头与尾部固定串。
   */
  var RE_GOAL_LIMIT_INJECT = /^\[系统提示\]目标模式已达到轮数上限（\d+轮），任务尚未完成。请向用户总结当前进度、已完成工作和未完成原因，无需继续执行新任务。$/;
  var RE_GOAL_CONT_INJECT = /^\[自动续跑\]已完成\d+轮，任务：[\s\S]*\n请继续推进任务。\n\[完成审计\] [\s\S]*$/;

  function isGoalInjection(text){
    var s = String(text == null ? '' : text);
    return RE_GOAL_LIMIT_INJECT.test(s) || RE_GOAL_CONT_INJECT.test(s);
  }

  /**
   * 画一条**历史**工具行（会话切回来 / 面板重建时重放）。
   *
   * 为什么单独写一份而不复用 addToolRow + finishTool：
   *   那两个函数会调 rendered('tool/call' | 'tool/result') 给宿主发回执，
   *   而回执的语义是「这一条**实时**事件界面画到了哪一步」。历史重放不是实时
   *   事件，发回执会让宿主的流控账本错乱（把重放的 N 条当成刚发生的 N 条）。
   *   另外 addToolRow 建的是「执行中」占位行，而历史里的工具**早就结束了**，
   *   直接建一个带结论的行才是对的（否则屏幕上会挂一串永远转圈的行）。
   *
   * 结构与实时高度一致：同一套 .tools/.row/.slot 类名，所以样式、折叠、
   * 复制按钮、路径保尾全都自动继承。
   */
  function addHistoryTool(m){
    var wrap = document.createElement('div');
    wrap.className = 'tools';
    /** 超过 40 行的部分，等用户点「还有 N 行」时再填进 DOM */
    var restOutLines = null;
    var isTodo = m.label === '更新计划' || m.name === 'TodoWrite';
    if (isTodo) wrap.setAttribute('data-todo', 'true');
    var failed = m.failed === true;
    var sumText = String(m.summary || '');
    var isPath = /[\\/]/.test(sumText) && /\.[A-Za-z0-9]{1,6}$/.test(sumText);
    // 与实时行同一条 RTL 修正（见 addToolRow 里的长注释）
    if (isPath && sumText.charAt(0) === '…') sumText = '\u200E' + sumText;
    // 历史工具默认收起：一次切会话可能有几十上百条，全展开等于把屏幕淹掉。
    // 计划行例外（它本身就是一屏摘要，展开才有用）。
    var open = isTodo ? 'true' : 'false';
    var meta = failed ? '<span class="bad">失败</span>' : '完成';
    if (m.truncated) meta += ' · 已截断';
    var inputText = isTodo ? '' : toolInputText(m);
    var outHtml = '';
    // 编辑类工具：优先画 diff（红绿行），没有 diff 才退回纯文本输出。
    // 与实时 finishTool 用同一套 .snippet/.ln add|del/.foot 结构，
    // 所以颜色、行号、+N −M 行头全都一致。
    var diff = m.diff;
    if (diff && Array.isArray(diff.lines) && diff.lines.length) {
      var dHtml = '<div class="io-lb io-out-lb">OUT</div><div class="snippet">';
      diff.lines.forEach(function (l) {
        var cls = l.type === 'add' ? 'ln add' : (l.type === 'del' ? 'ln del' : 'ln');
        dHtml += '<div class="' + cls + '"><span class="no">' +
          esc(l.no === undefined || l.no === null ? '' : l.no) + '</span>' +
          '<span class="tx">' + esc(l.text) + '</span></div>';
      });
      dHtml += '<div class="foot">+' + (diff.added || 0) + ' −' + (diff.removed || 0) +
        (diff.truncated ? ' · 只显示前 ' + diff.lines.length + ' 行改动' : '') +
        '</div></div>';
      outHtml = dHtml;
    } else {
      var outText = String(m.output || '');
      if (outText) {
        /* 【结构必须与实时一致】实时 finishTool 画的是
             .io-lb(OUT 标签) + .snippet.out > .ln > .tx
           一开始这里套了 `.io > .io-sec > .io-h + pre.snippet` —— 三层容器各自
           有 padding/border/max-height，挤在侧边栏里结果就是：外层卡片很大、
           真正的文字区只剩一条窄缝（实测用户点名「画布很大但中间显示文字的
           只有一个小的长方形区域」）。现在完全照搬实时那套扁平结构，
           只有 .snippet 一个滚动体（overflow-x:auto），行用 .ln 排列。 */
        var outLines = outText.split('\n');
        // 与实时相同的上限：前 40 行进 DOM，其余给一行「还有 N 行未显示」。
        // 一次几百行会拖慢渲染，而用户真要全文可以去翻原始输出文件。
        var headLines = outLines.slice(0, 40);
        var restLines = outLines.length - headLines.length;
        var oHtml = '<div class="io-lb io-out-lb">OUT</div><div class="snippet out">';
        headLines.forEach(function (t) {
          oHtml += '<div class="ln"><span class="tx">' + esc(t) + '</span></div>';
        });
        if (restLines > 0) {
          // data-rest 供展开按钮使用（与实时 .more 同一套交互）
          oHtml += '<div class="more" data-rest="' + restLines + '">还有 ' +
            restLines + ' 行未显示 · 点击展开</div>';
          restOutLines = outLines.slice(40);
        }
        oHtml += '</div>';
        outHtml = oHtml;
      }
    }
    // 行头也带上 +N −M：与实时一致，用户不展开也能看出改了多少
    var diffTag = (diff && Array.isArray(diff.lines) && diff.lines.length)
      ? '+' + (diff.added || 0) + ' −' + (diff.removed || 0)
      : '';
    if (diffTag) meta += ' · ' + diffTag;
    wrap.innerHTML = '<div class="row" data-open="' + open + '" data-st="' +
      (failed ? 'err' : 'ok') + '" tabindex="0" role="button">' +
      '<span class="caret">▶</span>' +
      '<span class="name">' + esc(m.label || m.name || '工具') + '</span>' +
      '<span class="arg' + (isPath ? ' path' : '') + '">' + esc(sumText) + '</span>' +
      '<span class="meta">' + meta + '</span></div>' +
      '<div class="slot">' +
      (isTodo ? '<div class="todo"></div><div class="extra"></div>' : ioInHtml(inputText) + outHtml) +
      '</div>';
    convo.appendChild(wrap);
    // 同实时行：留工具名供折叠摘要合成「已读取文件」这类短语
    wrap.setAttribute('data-tool', String(m.name || ''));
    wrap.setAttribute('data-tool-label', String(m.label || ''));
    // 剩余行挂到 .more 节点上（历史行不在 toolMap 里，见事件委托那段）
    if (restOutLines && restOutLines.length) {
      var moreEl = wrap.querySelector('.snippet .more');
      if (moreEl) moreEl.__rest = restOutLines;
    }
    // 计划行：历史里 arguments 就是当时的完整清单，直接画出来。
    // 与实时 todo/update 用同一套 class（done/now/mk），样式自动一致。
    if (isTodo) {
      var todos = (m.arguments && m.arguments.todos) || null;
      if (Array.isArray(todos)) {
        var done = 0;
        todos.forEach(function (t) { if (t && t.status === 'completed') done++; });
        var argEl = wrap.querySelector('.arg');
        if (argEl) argEl.textContent = todos.length + ' 项 · ' + done + ' 完成';
        var html = '';
        todos.forEach(function (t) {
          if (!t || typeof t !== 'object') return;
          var cls = t.status === 'completed' ? 'done' : (t.status === 'in_progress' ? 'now' : '');
          var mk = t.status === 'completed' ? '✓' : (t.status === 'in_progress' ? '●' : '○');
          html += '<div' + (cls ? ' class="' + cls + '"' : '') +
            '><span class="mk">' + mk + '</span>' + esc(t.content) + '</div>';
        });
        var todoEl = wrap.querySelector('.todo');
        if (todoEl) todoEl.innerHTML = html;
      }
    }
    return wrap;
  }

  /**
   * 是不是**纯内部纠偏指令**（后端解决模型跑偏时插的 user 消息）。
   *
   * 这类消息对模型有用，但用户看到会以为「自己在某个时刻真的打过这句话」。
   * 后端在 rpc/server.py 的 _history_payload 里已经过滤掉了；这里再挡一次是
   * **兜底**：用户可能还在跑旧版后端（改后端的代码要重启进程才生效），
   * 或者历史来自别处（导入的会话文件）。
   *
   * ⚠ 改这里必须同步 `narnat_agent/config/defaults.py` 的
   *    INTERNAL_NOTICE_PREFIX / INTERNAL_NOTICE_MARKERS —— 那是唯一真源。
   *    两份不一致的后果：后端的注入在界面上漏出来（或反过来把用户的话吃掉）。
   *
   * ## 判据必须锚定整条开头，不能只看「以 [ 开头」
   * 用户的真实消息里就有大量方括号开头的**粘贴日志**，实测见过：
   *   `[Mapping Extensions and Extra Properties (MEEP)] …`
   *   `[ValleyTalk] VT-HTTP-START…`
   *   `[Wind Effects] …`
   * 只看开头会把它们全吃掉。
   */
  var RE_INTERNAL_ONLY = /^(?:\[系统提醒\]|\[\]对话历史过长已自动压缩)/;

  function isInternalOnly(text){
    var s = String(text == null ? '' : text);
    // 后端注入时可能在前面带空白（append 之间夹了空行），先去掉再判
    return RE_INTERNAL_ONLY.test(s.replace(/^\s+/, ''));
  }

  function renderHistory(msgs){
    if (!msgs || !msgs.length) return;
    /* 历史里没有 turn/begin、turn/end 事件，只有一串 user / assistant / tool。
       轮次边界只能靠 **user 消息** 划：它之后到下一个 user 之前，就是一轮。
       收口时把这些工具行与思考块折起来，用户切回会话看到的才是
       「▸ 这一轮的工作过程 · 3 次工具调用」，而不是几十行平铺的工具。

       ## 和实时一样要**分段**（第 29 轮修复）

       一轮里 AI 是「做几件事 → 说一句 → 再做几件 → 再说一句」。
       只在 user 边界收口的话，后面几段的工具会被搬到最前面、正文被挤到末尾，
       跟当时的过程对不上（实测 order 从 `[工具][正文A][工具][正文B]`
       变成 `[组: 全部工具][正文A][正文B]`）。
       dsh 的做法是「遇到带正文的 assistant-step 就 flush 一次组」，
       这里同理：**每段正文之前先把已累积的工具收口**。 */
    var roundNodes = [];
    function flushRound(){
      if (!roundNodes.length) return null;
      var box = buildTurnProc(roundNodes, { reason: 'history', headline: false });
      roundNodes = [];
      return box;
    }
    msgs.forEach(function(m){
      if (!m) return;
      // 工具条目：后端在 _history_payload 里按真实顺序插进来，
      // 结构对齐实时事件（见 server.py 那段注释）。
      if (m.role === 'tool') {
        var toolEl = addHistoryTool(m);
        if (toolEl) roundNodes.push(toolEl);
        return;
      }
      // 纯内部纠偏指令：一个字都不显示（不是画成系统行 —— 用户没必要知道
      // 模型被提醒过要建计划、要收后台任务，那是框架自己的事）。
      // 注意顺序：必须在「目标模式续跑」判断**之前**，否则当系统行画出来。
      if (m.role === 'user' && isInternalOnly(m.text)) {
        return;
      }
      // 后端只在有思考时才带 thinking 这个键；旧会话 json 里没有它，
      // 走原来的路径、一个多余的节点都不加。
      var thinkText = (m.role === 'assistant' && typeof m.thinking === 'string') ? m.thinking : '';
      if (!m.text && !thinkText) return;
      if (m.role === 'user') {
        // 新的一条用户消息 = 上一轮结束了，先把上一轮收口
        flushRound();
        // 目标模式注入的内部指令（"[自动续跑]…" "[系统提示]…"）在历史里
        // 也是 user 角色（必须如此，它们要发给模型）。直接当用户气泡画出来，
        // 用户切回会话会看到「自己说过」一段根本没打过的字——实测会以为
        // 账号串了或被改了记录。这里按系统行渲染，内容仍完整保留。
        if (isGoalInjection(m.text)) {
          // 不再截断到 120 字：注入文案本身是多行的（续跑指令后面带着整段
          // [完成审计] 提示），截掉就再也看不到原文。系统行没有展开机制，
          // 所以整段放进这一行，并保留换行（.sys 默认把换行折成空格，
          // 全塞成一行读不出结构）。
          var injLine = addSysLine('·', esc(m.text), '', '');
          var injBody = injLine && injLine.querySelectorAll('span')[1];
          if (injBody) injBody.style.whiteSpace = 'pre-wrap';
          return;
        }
        var u = document.createElement('div');
        u.className = 'msg';
        u.setAttribute('data-role', 'user');
        u.innerHTML = '<div class="text">' + esc(m.text) + '</div>';
        // 历史正文里的附件清单块（- 图片：<路径>）也要画出缩略图：
        // 否则切回会话 / 重载窗口后只剩一段路径文本，看不到自己发过的图。
        // 没有该块的旧消息 → 一个节点都不加。
        addMsgThumbs(u, attachPathsInText(m.text));
        convo.appendChild(u);
      } else if (m.role === 'system') {
        // 探索分支合并回来的结论（"# 子会话 [X] 结论"）。它是 system 角色
        // （要发给模型当上下文），但对用户来说就是「刚才 /done 的结果」，
        // 必须能看见——不能只在界面打一句「已合并」。
        var n = document.createElement('div');
        n.className = 'msg sys-note';
        n.innerHTML = '<div class="text">' + esc(m.text) + '</div>';
        convo.appendChild(n);
      } else {
        // 助手消息不套 .text（.text 是用户消息的容器）
        // 思考必须画在正文**之前**，顺序与当时流式出现的顺序一致
        // 思考进组（dsh 也是把 reasoning 归到 process 区）
        if (thinkText) roundNodes.push(addHistoryThink(thinkText));
        // 正文出现 = 前一段工具到此为止，先收口再画正文
        flushRound();
        var a = document.createElement('div');
        a.className = 'msg assistant';
        a.innerHTML = markdownInline(m.text);
        convo.appendChild(a);
      }
    });
    // 最后一条 user 之后的那一轮没有「下一个 user」来触发收口，这里补上
    flushRound();
    // 刚进入/加载一个会话，用户期待停在最新一条
    scrollBottom(true);
  }

  /**
   * 「切进某个会话」的唯一入口。
   *
   * 四条成功路径（切换 / 新建 / 新建分支 / 完成合并 / 保存会话）在**切换**
   * 这件事上完全一样：清屏、画历史、顶上名字、收起列表。以前每条各写一遍，
   * 结果 sessionSwitch 里有 setDrawer(false)，其它几条都没有 —— 用户在列表里
   * 点「新建对话」，后端真的建好了、列表却没有收起，屏幕上还是那张列表，
   * 看起来就是「没跳转」（实测被点名）。抽成一处，以后不会有第五条漏掉。
   *
   * @param {Object} p 后端回执（要 current / messages / isChild）
   * @returns {boolean} 是否真的切了（没有 current 或没带 messages 时不动）
   */
  function enterSessionFrom(p) {
    if (!p || typeof p.current !== 'string' || !p.current) return false;
    CUR = p.current;
    CUR_IS_CHILD = !!p.isChild;
    if (Array.isArray(p.messages)) {
      resetConversationDom();
      authHintShown = false;
      body.setAttribute('data-run', 'false');
      resetContextStats();
      renderHistory(p.messages);
      // 这个会话的提问也叠进来（全局历史保留，见 seedHistory）
      seedHistory(p.messages);
      historyDrawn = p.messages.length;
      historyDrawnFor = p.current;
    } else {
      // 后端没带历史（老版本 / 空会话）：只换名字与列表状态，不清屏 ——
      // 清屏会得到一片空白，比留着旧内容更让人以为数据丢了。
      resetContextStats();
    }
    renderSessions();
    // 列表态是**整块替换**对话区（body[data-drawer="true"] 下 .convo 是 display:none），
    // 所以必须收起 —— 不收的话对话区仍是 display:none，用户看到的还是那张列表。
    setDrawer(false);
    return true;
  }

  function applySessionResult(p){
    // 会话操作回执：解锁分支出口按钮（分支里点「完成并合并」后要还原）
    if (window.__narnatBranchUnlock) window.__narnatBranchUnlock();
    if (!p) return;
    // 新建失败（如后端是旧版本、没有 session/new 方法）：必须说清
    // 「没有新建成功」，否则用户以为已经开了新对话，接着 /save 就把
    // 上一段的全部历史存进新会话（实测真的发生过）。
    if (p.action === 'sessionNew' && p.error) {
      unlockNewChat();
      sysLine('✗', '新建对话失败', esc(String(p.error).slice(0, 120)), 'err');
      scrollBottom();
      rendered('session/result', { action: 'sessionNew', error: p.error });
      return;
    }
    // 其它会话操作失败了：必须说出来。
    // 不说的后果（实测）：点幽灵父会话时后端回了「父会话记录已丢失…」，
    // 前端因为没处理 error 而完全静默——用户点了没反应，只会以为界面坏了。
    if (p.error && p.action !== 'sessionNew') {
      // 切换失败也要解锁，否则那一条会话从此点不动（一直被判「正在切过去」）
      pendingSwitchTo = '';
      var label = {
        sessionSwitch: '进入会话失败',
        sessionDelete: '删除会话失败',
        sessionRename: '重命名失败',
        sessionExplore: '新建分支失败',
        sessionDone: '合并失败'
      }[p.action] || '操作失败';
      // /rm 走的是 command 路径，错误在 lines 里；这里主要是抽屉/按钮触发的
      sysLine('✗', label, esc(String(p.error).slice(0, 160)), 'err');
      // 失败也必须解锁：锁是「这条操作已提交、等回执」，失败就是回执到了。
      // 只解这一条（按回执里的会话名），不能扫全表 —— 另一条还在等回执的
      // 会被一起开锁，用户可以重复提交（实测）。
      var failedName = p.name || p.current || pendingSwitchTo || '';
      if (failedName) {
        unlockSess(failedName);
        unlockSess(pendingSwitchTo);
      } else {
        // 回执既没带会话名也没带 current（极少见）时，才按动作类型兜底；
        // 有名字时**不能**这么做：会把同一动作下另一条还在等回执的锁一起清掉。
        unlockSessByAction({
          sessionDelete: 'delete', sessionRename: 'rename',
          sessionSwitch: 'switch', sessionExplore: 'explore', sessionDone: 'done'
        }[p.action] || '');
      }
      scrollBottom();
      rendered('session/result', { action: p.action, error: p.error });
      return;
    }
    if (p.action === 'sessionNew') {
      // 新建对话：清空对话区，回到「未保存」态
      unlockNewChat();
      resetConversationDom();
      authHintShown = false;
      CUR = '';
      CUR_IS_CHILD = false;
      // 新对话本身没有消息，但 ↑ 仍要能翻到磁盘上的全局历史（用户要的全局）
      seedHistory([]);
      body.setAttribute('data-run', 'false');
      resetContextStats();
      renderSessions();
      // 用户是从会话列表里点的「＋ 新建对话」：建好了就必须把列表收起来，
      // 否则屏幕上还是那张列表、对话区仍 display:none，看起来「点完没反应」。
      setDrawer(false);
      // p.autoSaved：上一段内容被后端自动存成的会话名。
      // 必须说出来——否则用户看到对话区清空，会以为内容丢了。
      if (p.autoSaved) {
        sysLine('＋', '已新建对话', '上一轮保存为「' + esc(p.autoSaved) + '」');
      } else {
        sysLine('＋', '已新建对话', '');
      }
      rendered('session/result', { action: 'sessionNew' });
      return;
    }
    if (p.action === 'sessionSwitch' && p.current) {
      pendingSwitchTo = '';
      // 这一轮是不是被这次切换打断的。后端切会话前会 cancel_active()，
      // 但那条 turn/end 的「■ 已打断」正好被下面的 resetConversationDom 清掉，
      // 于是用户正在读的回答被掐断、屏幕上直接换成另一个会话的历史，
      // 一个字都没提（实测：run true → 切完 false，sys 行 0 条）。
      var cutTurn = body.getAttribute('data-run') === 'true';
      enterSessionFrom(p);
      if (cutTurn) {
        sysLine('■', '已停止上一轮', '');
      }
      // 一句到底：切换结果 + （必要时）上一轮被保存成了什么名字。
      // 用 ✓ + 「已进入对话」：这是**成功回执**，不是过程叙述 —— 用户从列表点
      // 进来时必须看到一句明确的「进去了」，否则切完只是换了一屏内容，
      // 分不清是切换成功还是列表刚好关掉（实测被点名要求显示出来）。
      sysLine('✓', '已进入对话 ' + esc(p.current || '（未命名）'),
        p.autoSaved ? '上一轮保存为「' + esc(p.autoSaved) + '」' : '');
    } else if (p.action === 'sessionExplore' && p.current) {
      /* 新建/进入探索分支：与切换完全同一件事，必须走同一个入口。
         原来这里只打一行系统提示，对话区仍停在上一个会话的内容上 ——
         用户点「新建探索分支」，后端建好了、顶栏名字变了，内容却没换，
         只能自己去列表再点一次才真正进去（实测被点名「理应自动跳转」）。 */
      enterSessionFrom(p);
      sysLine('⑂', '已进入探索分支 ' + esc(p.current),
        p.reentered ? '这是之前建过的同名分支' : '');
    } else if (p.action === 'sessionDone') {
      if (p.merged === false) {
        // 分支没有增量属于业务提示，不是错误：用中性行，不用红色
        sysLine('·', p.message || '没有新的讨论内容', '');
      } else {
        // /done 合并后后端 already replace_messages 成父会话了，界面必须重绘：
        // 不重绘会怎样（实测）：顶栏写着父会话、对话区还留着分支的全部内容，
        // 用户接着在「父会话 + 分支内容」的错位状态下提问；而刚合并出来的
        // 「# 子会话 [x] 结论」在界面上根本看不到（要重载面板才出现）。
        // 走统一入口：顺带把会话列表收起（用户是从列表里点的「完成并合并」）。
        enterSessionFrom(p);
        sysLine('✓', '已回到父会话 ' + esc(p.current || ''), '');
      }
    } else if (p.action === 'sessionDelete') {
      // 删的若是当前会话，后端已切回游离态，对话区必须清：后端已经没有这段
      // 上下文，屏幕上却还留着整段内容，用户再点保存会得到「没有对话内容，
      // 无需保存」——以为自己的对话丢了。
      //
      // **清屏必须在写系统行之前**。原来顺序反了：先 sysLine('✓ 已删除 …')，
      // 再 resetConversationDom() 把整块清空，刚写的那行跟着被抹掉 ——
      // 用户删完当前会话，屏幕上一条反馈都没有，不知道删成功没有（实测）。
      if (p.wasCurrent) {
        resetConversationDom();
        authHintShown = false;
        body.setAttribute('data-run', 'false');
        resetContextStats();
      }
      // 面板里删除是立即生效的（后端没有「退出时清理」这个时机），
      // 所以文案不能说「退出时生效」——那会让用户以为文件还在。
      // broken：删掉的是列表里根本看不到的损坏文件。
      if (p.broken) {
        sysLine('✓', '已删除损坏的会话文件' + (p.name ? ' ' + esc(p.name) : ''), '', 'ok');
      } else {
        sysLine('✓', '已删除 ' + (p.name ? esc(p.name) : '会话'), '', 'ok');
      }
      // CUR 必须跟着走，否则列表里高亮的还是那个已经不存在的会话。
      CUR = String(p.current || '');
      CUR_IS_CHILD = false;
      renderSessions();
      // 删的是当前会话时，用户本来就在列表里操作：删完要回到对话区，
      // 而不是让他盯着一张「已经少了一条」的列表自己找出口。
      if (p.wasCurrent) setDrawer(false);
    } else if (p.action === 'sessionRename') {
      sysLine('✏', '已重命名为 ' + esc(p.to || ''), '', 'ok');
      // 改的是当前会话时后端已把状态指到新名，CUR 同步过去
      CUR = String(p.current || '');
      renderSessions();
    }
    rendered('session/result', { action: p.action || '' });
  }

  /**
   * 设置类命令执行成功后，把状态栏与下拉同步到新值。
   *
   * 真实态下这些命令由后端执行，回执只有文本；界面不主动同步的话，按钮文字
   * 仍是旧值——用户看到「已切换模型：X」和状态栏「Y」同时存在，会以为自己
   * 没切成功，再点一次（又发一次 config/set）。
   * 只处理确定能从前端参数推出来的四项；有疑问的一律不碰。
   * @param {string} command 命令名（不含 /）
   * @param {string} args 命令参数原文
   */
  function syncUiAfterCommand(command, args){
    var cmd = String(command || '').toLowerCase();
    var arg = String(args || '').trim();
    if (!arg) return;
    // 参数里的子命令也要按不区分大小写比较：后端对 `on/off/true/false` 都是
    // 转小写后判断的，用户敲 `/goal ON 5`、`/plugin terminal OFF` 后端都接受，
    // 而界面若逐字比较 `'OFF' === 'off'` 就会判成「开」——回执说成功、
    // 状态栏却还是旧值，显示与实际相反（实测）。
    var argLow = arg.split(/\s+/).map(function (x) { return x.toLowerCase(); }).join(' ');
    var init = RPC_STATE.init;
    if (cmd === 'mode') {
      // 大小写/别名要归一：/mode DEEPSEEK-V4-PRO 时后端接受（不区分大小写），
      // 而界面若原样写进按钮，下拉里一个都不打勾、`/mode` 无参又显示另一个值
      //（实测三处显示各不相同）。从下拉选项里取回规范名。
      var canonical = arg;
      var mps = document.querySelectorAll('#popModel .pi');
      for (var mi = 0; mi < mps.length; mi++) {
        var mv = mps[mi].getAttribute('data-v');
        if (mv && mv.toLowerCase() === arg.toLowerCase()) { canonical = mv; break; }
      }
      setChecked(document.getElementById('popModel'), canonical);
      document.getElementById('btnModel').textContent = canonical;
      // 回滚基准也要跟着走。只改 DOM 不改快照会怎样（实测）：
      // /mode model-b 成功（后端已是 b）→ 点下拉切 model-c 失败 →
      // revertConfigUi 退回到快照里的 model-a，界面显示 a 而后端在跑 b，
      // 下次重开面板还会变回 a。纯下拉路径因为 syncConfigSnapshot 没这问题。
      if (init) {
        init.model = init.model || {};
        init.model.current = canonical;
      }
    } else if (cmd === 'goal') {
      // /goal on 3 后端已经生效，界面原来一个字都不改：同屏出现
      // 「目标模式 关」和回执「目标模式已开启（轮数上限 3）」（实测）。
      var gp = argLow.split(/\s+/);
      var gon = gp[0] === 'on';
      var gr = parseInt(gp[1], 10);
      if (!isFinite(gr) || gr <= 0) {
        var curGoal = RPC_STATE.init && RPC_STATE.init.goal;
        // 后端字段是 maxRounds（不是 rounds）：读错会拿到 undefined → 回落成 10，
        // 于是 `/goal on` 把「0/3」显示成「0/10」，还把这个值写进快照，
        // 之后一次配置失败回滚就把轮数框也写成 10（实测）。
        gr = (curGoal && (curGoal.maxRounds || curGoal.max)) || 10;
      }
      var curNow = 0;
      var mCur = document.getElementById('btnGoal').textContent.match(/·\s*(\d+)\//);
      if (mCur) curNow = parseInt(mCur[1], 10) || 0;
      document.getElementById('btnGoal').textContent = gon
        ? '目标模式 开 · ' + curNow + '/' + gr : '目标模式 关';
      // data-on 必须一起写：≤380px 时按钮只剩靶心图标，图标颜色是**唯一**
      // 能看出开关状态的线索（CSS 只在 [data-on="true"] 时点强调色）。
      // 这条 /goal 回执路径原来漏了，于是命令把目标模式打开后，
      // 窄面板上的图标还是不亮（实测，和第 1 轮修的是同一类问题，这是漏网的一处）。
      document.getElementById('btnGoal').setAttribute('data-on', gon ? 'true' : 'false');
      setChecked(document.getElementById('popGoal'), gon ? 'on' : 'off');
      var grin = document.getElementById('goalRounds');
      if (grin) grin.value = String(gr);
      if (init) {
        // 字段名必须与后端一致（maxRounds）。写 rounds 会怎样（实测）：
        // 快照里多出一个后端不认的键，而 applyGoal/revertConfigUi 读的是
        // maxRounds —— 之后任何一次配置失败回滚，轮数就退回旧的 10。
        init.goal = { enabled: gon, maxRounds: gr, current: curNow };
      }
    } else if (cmd === 'plugin') {
      // /plugin terminal off 后端已生效，界面原来不动：状态栏还写「插件 4/4」，
      // `/plugin` 列表却写「3/4 开启」（实测）。
      var pp2 = argLow.split(/\s+/);
      var pname = pp2[0];
      var pton = !(pp2[1] === 'off' || pp2[1] === 'false' || pp2[1] === '0');
      // 按小写比较逐个找，**不要拼进选择器**：pname 来自 argLow（已小写化，
      // 形如 "terminal"），而 .pi 的 data-v 是后端给的原始大小写（"Terminal"）。
      // 拼选择器永远选不中 → pNode 为 null → 整段回执静默失效：
      // 命令明明执行了，状态行还挂着旧数字（实测）。
      var pNode = null;
      Array.prototype.forEach.call(document.querySelectorAll('#popPlugin .pi'), function(pi){
        if (String(pi.getAttribute('data-v') || '').toLowerCase() === pname) pNode = pi;
      });
      if (pNode) {
        var sw = pNode.querySelector('.sw');
        if (sw) {
          sw.setAttribute('data-on', pton ? 'true' : 'false');
          sw.textContent = pton ? 'on' : 'off';
        }
        pNode.setAttribute('data-on', pton ? 'true' : 'false');
        var tot = document.querySelectorAll('#popPlugin .pi').length;
        var en = document.querySelectorAll('#popPlugin .pi[data-on="true"]').length;
        document.getElementById('btnPlugin').textContent =
          (en === tot ? '插件 ' + tot : '插件 ' + en + '/' + tot);
        // 按钮的 data-on 必须一起算：≤380px 时按钮只剩插头图标，
        // 图标颜色是唯一能看出「有没有插件开着」的线索（CSS 只在
        // [data-on="true"] 时点强调色）。这条回执路径原来只改文字、漏了这个属性 ——
        // 用 /plugin 把插件全关掉之后，窄面板上的图标还亮着（实测，
        // 和第 1 轮修的是同一类问题，这是最后漏网的一处）。
        document.getElementById('btnPlugin').setAttribute('data-on', en > 0 ? 'true' : 'false');
        if (init) {
          var pl = (init.plugins || []).map(function(x){
            // 同样按小写比较，否则快照里的 enabled 改不到，下次回滚又退回旧值
            return (x && String(x.name || '').toLowerCase() === pname)
              ? { name: x.name, enabled: pton } : x;
          });
          init.plugins = pl;
        }
      }
    } else if (cmd === 'thinking') {
      // 参数可能是档位名（high）也可能是显示名（高），按两者都匹配一次。
      // 档位名要比小写：`/thinking HIGH` 后端接受（转小写判断），
      // 界面逐字比会打不上勾、按钮仍是旧的（实测）。
      var thArg = argLow;
      var tips = document.querySelectorAll('#popThink .pi');
      for (var i = 0; i < tips.length; i++) {
        var v = tips[i].getAttribute('data-v');
        var lb = (tips[i].querySelector('.lb') || {}).textContent || '';
        if ((v && v.toLowerCase() === thArg) || lb === arg) {
          setChecked(document.getElementById('popThink'), v);
          document.getElementById('btnThink').textContent = lb;
          if (init) {
            init.thinking = init.thinking || {};
            init.thinking.effort = v;
          }
          break;
        }
      }
    }
    // /goal、/plugin 的回执里带的是后端真值（轮数会被夹取），这里不猜，
    // 交给紧随其后的 stats / initialize 同步。
  }

  function applyCommandResult(p){
    if (!p) return;
    // 暂离分支按钮发出的是 /exit，回执从这里回来。任何命令回执都说明
    // 这次请求已有应答，解锁分支出口按钮 —— 不解锁的话按钮会卡在
    // 「正在暂离…」直到 30 秒兜底，用户以为界面死了。
    if (window.__narnatBranchUnlock) window.__narnatBranchUnlock();
    // handled:false = 不是已知命令，被扩展转成了普通对话。
    // 这时要把用户输入原样显示出来，否则界面像什么都没发生。
    if (p.handled === false) {
      var raw = p.text || ('/' + (p.command || '') + (p.args ? ' ' + p.args : ''));
      var um = document.createElement('div');
      um.className = 'msg';
      um.setAttribute('data-role', 'user');
      // 标 data-pending：这条消息此刻只是「已提交」，后端还没接受。
      // 内层 chat/send 若被 busy 拒（运行中发一条像路径的斜杠内容就会这样），
      // 退场逻辑只认 [data-pending] 的节点——不标的话它留在对话区里，
      // 没有「未发送」标记、输入框已清空，用户以为发出去了在等回答（实测）。
      um.setAttribute('data-pending', '1');
      um.innerHTML = '<div class="text">' + esc(raw) + '</div>';
      convo.appendChild(um);
      curAssistant = null;
      assistantBuf = '';
      authHintShown = false;
      lastSentMaybeAccepted = false;
      body.setAttribute('data-run', 'true');
      scrollBottom();
      rendered('command/result', { command: p.command || '', ok: true, handled: false });
      return;
    }
    // 界面按钮触发的命令（如抽屉里点「保存当前对话」）不该回显成 ❯ /save xxx：
    // 用户没打过这行，看到终端式命令回显只会困惑。改成一句结果告知。
    if (p.origin === 'ui') {
      var uiLines = p.lines || [];
      if (p.ok === false) {
        sysLine('✗', '保存失败', esc(uiLines.length ? uiLines.join('\n') : '未知原因'), 'err');
      } else {
        // 后端 /save 的输出可能是「会话已保存: 名字」，也可能被包在
        // 「已保存为「…」」里。界面自己已经说了「已保存为」，不剥干净会
        // 显示成「已保存为「会话已保存: 早会」」这种套娃（实测）。
        var named = uiLines.join(' ')
          .replace(/^(?:会话)?已保存(?:为)?\s*[:：]?\s*/, '')
          .replace(/^[「『"']+|[」』"']+$/g, '')
          .replace(/^(?:会话)?已保存(?:为)?\s*[:：]?\s*/, '')
          .replace(/^[「『"']+|[」』"']+$/g, '')
          .trim();
        sysLine('✓', named ? '已保存为「' + named + '」' : '已保存', '', 'ok');
      }
      scrollBottom();
      rendered('command/result', {
        command: p.command || '', ok: p.ok !== false, origin: 'ui'
      });
      return;
    }
    var cmdText = '/' + (p.command || '');
    if (p.args) cmdText += ' ' + p.args;
    // lines 必须兜住非数组：宿主/后端若把它给成字符串或对象（版本不匹配），
    // 后面的 lines.map 会抛 TypeError，整条命令回执**静默消失** ——
    // 用户点了 /save 却什么都没显示，以为没生效（实测）。
    var lines = Array.isArray(p.lines) ? p.lines : (p.lines ? [String(p.lines)] : []);
    // 设置类命令成功后必须把状态栏同步过来。
    // 真实态下 /mode、/thinking、/goal、/plugin 是发给后端执行的（不是本地分支），
    // 回来后只画一行回执；状态栏那四个按钮仍是旧值——同屏写着「已切换模型：
    // deepseek-v4-pro」，按钮还显示 deepseek-chat（实测，用户以为自己没切成功）。
    if (p.ok !== false) syncUiAfterCommand(p.command, p.args);
    if (p.ok === false) {
      // 有两类回执不是「失败」，只是后端用 ok:false 表示「没有执行变更」：
      //   · 需要二次确认：`/rm --all` 返回「删除全部会话不可恢复。确认删除请
      //     执行: /rm --all confirm」——打红叉会让用户以为删失败了（实测）。
      //   · 中性事实：`/save` 在空对话上返回「没有对话内容，无需保存」，
      //     同族的 /compact 走的是中性点号，这里却画红叉。
      // 判据只看回执原文，不改后端、不加协议字段。
      var firstLine = String(lines.length ? lines[0] : '');
      var needConfirm = /确认删除请执行|确认.*请执行|不可恢复/.test(firstLine);
      var neutral = /无需保存|没有对话内容/.test(firstLine);
      if (needConfirm || neutral) {
        cmdResult(esc(cmdText), needConfirm ? '⚠' : '·',
          esc(lines.join('\n')),
          needConfirm ? '待确认，尚未执行' : '',
          needConfirm ? 'warn' : '');
      } else {
        cmdResult(esc(cmdText), '✗', esc(lines.length ? lines.join('\n') : '命令执行失败'), '', 'err');
      }
      // /thinking 没有「关闭」这一档：强度只有 low/medium/high，真正的开关是
      // 配置里的「智能体.思考.启用」。用户按 /goal、/plugin 的惯例敲
      // `/thinking off` 只会得到「无效值: off」，界面上任何地方都没说
      // 「关闭」该去哪 —— 补一句指路（不新增控件、不改协议）。
      if (String(p.command || '').toLowerCase() === 'thinking') {
        sysLine('·', '关闭思考需改配置', '「智能体.思考.启用」= false，改完重启');
        scrollBottom();
      }
      // /compact 失败时后端也只发 compress/end{ok:null}（协议里 ok 恒为 null），
      // 前端已经画了「✓ 压缩完成」。命令回执才是权威：把它改成失败，
      // 否则屏幕上一行「✓ 压缩完成」+ 一行「✗ 压缩失败」（实测）。
      if (String(p.command || '').toLowerCase() === 'compact' && compressLine) {
        updateSysLine(compressLine, '✗', '压缩失败', '', 'err');
        compressLine = null;
      }
      // 注意：成功分支与 noop 分支也各自清空了 compressLine（见下）。
      // 不清会怎样（实测）：上一次成功压缩画的那一行会一直挂着，
      // 之后再敲一次 `/compact`（这次因带参数被判失败、根本没起压缩），
      // 上面那段「改成失败」就把**上一次成功**的那行改成「压缩失败」——
      // 屏幕上凭空出现一条不属于这次操作的失败记录。
    } else if (p.noop) {
      // 执行了但无变更（如 /compact 时没有历史）：用中性行。
      // 打「✓ 已执行」会让用户以为真压缩了，实际什么都没发生。
      //
      // 但后端对「没有可压缩的历史」也会发 compress/begin + compress/end，
      // 前端在那时已经画了「✓ 压缩完成 0.0s」。两行同屏就是
      // 「压缩完成」+「没有可压缩的历史对话」——一句说成了、一句说没做（实测）。
      // 这里的回执才是权威：把它撤掉。
      if (String(p.command || '').toLowerCase() === 'compact' && compressLine) {
        if (compressLine.parentNode) compressLine.parentNode.removeChild(compressLine);
        compressLine = null;
      }
      cmdResult(esc(cmdText), '·', esc(lines.length ? lines.join('\n') : '无可执行的内容'), '', '');
    } else if (lines.length) {
      // 成功的命令回执到了：这一轮的压缩结论已经定稿，别让下一次回执再回头改它。
      if (String(p.command || '').toLowerCase() === 'compact') compressLine = null;
      // /rm 删掉的是当前会话时，后端已回到游离态：对话区必须清掉，
      // 否则屏幕留着整段内容、后端却说没有上下文（与抽屉删除同因）。
      var rmCurrent = (p.command === 'rm' && p.wasCurrent);
      // /cd 换了会话：后端把切换后的历史一起回过来了，必须重绘。
      // 不重绘会怎样（实测）：顶栏 chip、抽屉高亮、后端 current 都切到了 B，
      // 对话区还显示 A 的消息——用户在「B 的标题 + A 的内容」下继续提问。
      var cdSwitched = (p.command === 'cd' && p.current);
      // /exit 的语义是「暂离」，**不是**「丢掉上下文」，两种情况完全不同：
      //   · 在探索分支里：ChildSession.exit() 把内存换成父会话历史并切过去
      //     （session_callbacks.py:713-723）——必须重绘成父会话，否则
      //     顶栏写父会话、屏幕还是分支内容、模型看到父会话+结论，三方各不相同。
      //   · 在根会话里：RootSession.exit() 只落盘加换状态，**消息一条都不清**
      //     （session_callbacks.py:510-514，on_new 的注释也写明「/exit 只切状态、
      //     消息还留在内存里（再聊就接上了上一段）」）。
      // 曾经按「回执里没有 current 就算回到游离态」就清屏——这个条件对 /exit
      // 恒成立，于是把后端还活着的上下文从屏幕上抹掉：用户以为可以重新开始，
      // 接着提问时模型仍带着整段旧历史（实测清屏后再握手，父会话历史原样回来）。
      var exitLeave = (p.command === 'exit');
      // 这一轮是否被这次切会话打断：必须在 resetConversationDom 之前取，
      // 之后再读 data-run 就已经是 false 了。
      // 为什么要单独交代：后端对 cd/explore/done 会 `_cancel_and_wait_turn()`
      //（server.py:694-695），也就是**必然打断**当前这一轮。抽屉与顶栏下拉两条
      // 入口都有事前提示 + 事后「■ 已停止上一轮」，只有 /cd 命令这条通路一个字
      // 都不说 —— 用户看到的是「我刚问的问题，回答没了，屏幕直接换成另一个会话」，
      // 无从判断是回答失败还是被自己切走了（实测）。
      var cutTurnByCd = cdSwitched && body.getAttribute('data-run') === 'true';
      if (rmCurrent || cdSwitched) {
        resetConversationDom();
        authHintShown = false;
        body.setAttribute('data-run', 'false');
        if (cdSwitched) {
          CUR = String(p.current || '');
          CUR_IS_CHILD = !!p.isChild;
          resetContextStats();
          renderSessions();
        }
        if (cutTurnByCd) sysLine('■', '已停止上一轮', '');
      }
      // 后端回执是纯文本，每行独立、不需要跨行对齐 → 一律用换行布局。
      // 不加 wrap 会怎样（实测）：249px 下真实 /plugin 的 5 行回执
      // scrollWidth 344 > clientWidth 233；单行的 /goal 用法提示
      // 「目标模式: 已关闭（用法: /goal on [轮数] | off）」也会溢出 78px，
      // 尾部要横拖才看得见。原来的条件写成「含 \n 才 wrap」，单行漏掉了。
      var cmdLines = lines.map(esc).join('\n');
      cmdMulti(esc(cmdText), cmdLines, '', 'cmdout--wrap');
      if (cdSwitched && p.messages && p.messages.length) {
        renderHistory(p.messages);
        seedHistory(p.messages);
        // 同 sessionSwitch：画完必须记账，否则下一个 initialize 会清空重画
        historyDrawn = p.messages.length;
        historyDrawnFor = CUR;
      }
      if (rmCurrent) {
        // 删掉的是当前会话：状态栏那几项（上下文/缓存/费用/余额）属于这个
        // 已经不在的会话，必须清零。抽屉删除路径有这一句，/rm 这条漏了（实测）。
        resetContextStats();
        sysLine('·', '当前对话已删除', '');
      }
      if (exitLeave) {
        // 回执里没有 current，前端只能自己判断走了哪条路：
        //   · 分支里：目标是父会话名（CUR 形如 `alpha/beta`）。发 sessionSwitch
        //     拿父会话历史重绘——这条通路早就有（抽屉点会话走的就是它），
        //     且父会话文件已落盘，重新加载内容不会丢（/done 的结论也在里面）。
        //     不能用 post('ready')：宿主会先广播上一次的 initialize 旧快照，
        //     屏幕先闪回分支内容再覆盖，看起来像「点了没反应又变回去」。
        //   · 根会话里：后端消息仍在内存（再聊会接上），所以**不清屏**，
        //     只把 chip 更新成「未保存」，并说清代价：轮末不再自动保存。
        var parent = CUR.indexOf('/') > 0 ? CUR.split('/')[0] : '';
        if (CUR_IS_CHILD && parent && REAL) {
          post('sessionSwitch', { name: parent });
        } else if (REAL) {
          CUR = '';
          CUR_IS_CHILD = false;
          renderSessions();
          resetContextStats();
          post('sessionList', {});
          sysLine('·', '已离开会话', '');
        }
      }
    } else {
      // 面板里的 /exit 不是退出进程：后端把 EXIT 折算成 HANDLED 且一行输出都没有，
      // 落到这里就成了一个勾「已执行」——屏幕上打了勾，会话没变、进程也没退，
      // 用户以为退出成功（实测）。这里说实话。
      if (String(p.command || '').toLowerCase() === 'exit') {
        cmdResult(esc(cmdText), '·', '当前不在任何会话里', '', '');
        scrollBottom();
        rendered('command/result', { command: p.command || '', ok: true, lines: 0 });
        return;
      }
      cmdResult(esc(cmdText), '✓', '已执行', '', 'ok');
    }
    scrollBottom();
    rendered('command/result', {
      command: p.command || '', ok: p.ok !== false, noop: !!p.noop
    });
  }

  function applyConfigResult(p){
    var ok = !p || p.ok !== false;
    if (!ok) {
      // 失败的回执同样消费掉一个 key，否则队列会错位、下一次成功的回执
      // 会拿着上一次的 key 去写快照（实测会把模型名写进思考档位）。
      var failedKey = configKeyQueue.shift();
      sysLine('✗', '配置未生效', esc((p && p.message) || ''), 'err');
      // 点下拉时界面已经本地打了勾、换了按钮文字。后端没写成功的话必须
      // 把这些显示退回上一次握手时的真值——不回退会怎样（实测）：
      // 用户以为切换了模型，之后几轮还在按旧模型跑（花的是另一个价），
      // 下次重开面板又莫名变回旧值。
      //
      // 但**只回退失败的那一项**。原来无条件恢复四项会踩另一个坑：
      // 用户先切模型、紧接着切思考（两条请求都在飞），模型那条失败 →
      // 四项全被冲回快照 → 思考的界面改动也被抹掉；随后思考的成功回执
      // 只推进快照、不改显示 → 界面上思考还是旧档位，后端已经是新的，
      // 两者长期不一致（实测，反向顺序同样复现）。
      revertConfigUi(failedKey);
    } else {
      // 成功后必须把「真值快照」推进到新值。不推进会怎样（实测）：
      // 依次切 a→b→c→d 都成功，再切 e 失败，界面退回的不是 d 而是**最初那版 a**，
      // 而后端实际停在 d —— 界面显示一个模型，实际在用另一个模型跑。
      syncConfigSnapshot(p);
    }
    rendered('config/result', { ok: ok });
  }

  /**
   * 成功落盘后同步「真值快照」（回滚基准）。
   * 回执里没有 key，按发起顺序 FIFO 取出（见 configKeyQueue）。
   */
  function syncConfigSnapshot(p){
    var init = RPC_STATE.init;
    var key = configKeyQueue.shift() || '';
    if (!init || !p || p.current === undefined) return;
    // 回执与 key 的配对只能靠 FIFO 猜（协议不带请求 id）。万一某条回执丢失，
    // 后面的 key 就会整体错位 —— 这时若照写，会把一个模型名写进 goal 快照，
    // 目标模式从此显示成「关」而后端是开着的（实测）。
    // 用「回执形状」做最后一道校验：形状不符就不写，宁可少更新一次。
    if (key === 'model' && typeof p.current !== 'string') return;
    if (key === 'thinking' && typeof p.current !== 'string') return;
    if (key === 'goal' && (typeof p.current !== 'object' || p.current === null)) return;
    if (key === 'plugin' && (typeof p.current !== 'object' || p.current === null)) return;
    if (key === 'model') {
      init.model = init.model || {};
      init.model.current = p.current;
    } else if (key === 'thinking') {
      init.thinking = init.thinking || {};
      init.thinking.effort = p.current;
    } else if (key === 'goal') {
      init.goal = p.current;
    } else if (key === 'plugin' && p.current && p.current.name) {
      var list = init.plugins || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i] && list[i].name === p.current.name) list[i].enabled = !!p.current.enabled;
      }
      init.plugins = list;
    }
  }

  /** 把模型/思考/目标/插件的界面显示退回最后一次 initialize 的真值
   *  @param {string} [onlyKey] 只回退这一项（'model'/'thinking'/'goal'/'plugin'）。
   *         不传 = 全部回退（用于「重连后整体对齐」这类场景）。
   *         为什么要能限定：多条 configSet 同时在飞时，其中一条失败只该撤自己那一项，
   *         把别的项的界面改动一起冲掉会让「已切换但回执未到」的那项显示与后端脱节。 */
  function revertConfigUi(onlyKey){
    var init = RPC_STATE.init;
    if (!init) return;
    var want = function(k){ return !onlyKey || onlyKey === k; };
    var model = init.model || {};
    if (want('model') && model.current) {
      setChecked(document.getElementById('popModel'), model.current);
      document.getElementById('btnModel').textContent = model.current;
    }
    var think = init.thinking || {};
    if (want('thinking') && think.effort) {
      setChecked(document.getElementById('popThink'), think.effort);
      var lb = document.querySelector('#popThink .pi[data-v="' + think.effort + '"] .lb');
      document.getElementById('btnThink').textContent = lb ? lb.textContent : think.effort;
    }
    if (want('goal') && init.goal) applyGoal(init.goal);
    if (want('plugin') && init.plugins && init.plugins.length) applyPlugins(init.plugins);
  }

  /* ---------- UI 自省通道（供扩展/测试驱动真实 DOM） ---------- */

  /**
   * 扩展发来 ui/* 消息，webview 执行后把结果 post('ui/result') 回去。
   * 用途：验证「点这行能不能展开」「表格有没有渲染成 <table>」「输入框有没有长高」
   * 这类只有真实 DOM 才知道的事——宿主测试读不到 webview 内部。
   */
  function handleUiAction(p){
    var id = (p && p.id) || 0;
    var action = (p && p.action) || '';
    var selector = (p && p.selector) || '';
    var index = (p && typeof p.index === 'number') ? p.index : 0;
    var res = { id: id, action: action, selector: selector, ok: true };
    /** index=-1 表示最后一个匹配（断言「最新一条」时用） */
    function pick(sel){
      var list = document.querySelectorAll(sel);
      if (!list.length) return null;
      var i = index < 0 ? list.length + index : index;
      return list[i] || null;
    }
    try {
      if (action === 'query') {
        var el = pick(selector);
        res.found = !!el;
        res.count = document.querySelectorAll(selector).length;
        if (el) {
          res.tag = el.tagName.toLowerCase();
          res.text = (el.textContent || '').slice(0, 4000);
          res.html = (el.innerHTML || '').slice(0, 8000);
          res.height = Math.round(el.getBoundingClientRect().height);
          res.width = Math.round(el.getBoundingClientRect().width);
          // 横向溢出判定用：scrollWidth > clientWidth 即内容比容器宽（窄宽度适配的回归断言）
          res.scrollWidth = el.scrollWidth;
          res.clientWidth = el.clientWidth;
          res.attrs = {};
          var attrs = (p && p.attrs) || [];
          attrs.forEach(function (a) { res.attrs[a] = el.getAttribute(a); });
          // input 的当前内容不在 attribute 里（没写死 value= 时 getAttribute 恒为 null），
          // 测试要断言「补全后输入框是什么」必须读这里
          if (typeof el.value === 'string') res.value = el.value;
          // 纵向滚动与「选中项是否在可视区」：用于断言命令面板会跟着选中项滚动
          res.scrollTop = Math.round(el.scrollTop || 0);
          res.scrollHeight = Math.round(el.scrollHeight || 0);
          res.clientHeight = Math.round(el.clientHeight || 0);
          if (el.id === 'palette') {
            var selEl = el.querySelector('.item[data-sel="true"]');
            if (selEl) {
              var pr = el.getBoundingClientRect();
              var sr = selEl.getBoundingClientRect();
              res.attrs['data-sel-visible'] =
                (sr.top >= pr.top - 1 && sr.bottom <= pr.bottom + 1) ? 'true' : 'false';
            } else {
              res.attrs['data-sel-visible'] = 'none';
            }
          }
        }
      } else if (action === 'palDown') {
        // 与用户按 ↓ 走完全相同的代码路径（含滚动跟随），供回归测试驱动
        if (paletteVisible() && palItems.length) {
          palSel = (palSel + 1) % palItems.length;
          renderPalette();
          scrollPalSelIntoView();
          resetPaletteScrollIfFirst();
        }
        res.found = palItems.length > 0;
        res.sel = (palItems[palSel] || {}).c || null;
      } else if (action === 'palEnter') {
        // 复刻输入框 Enter 的分支逻辑（补全 vs 执行），供回归测试驱动
        if (paletteVisible() && palItems.length) {
          var typed = input.value.split('\n')[0].trim();
          var sel = palItems[palSel];
          if (palMode === 'arg') {
            completePalette();
            res.did = 'completeArg';
          } else if (sel && (sel.req || sel.c.toLowerCase() !== typed.toLowerCase())) {
            completePalette();
            res.did = 'completeCmd';
          } else {
            runInputCommand();
            res.did = 'run';
          }
        } else {
          var v = input.value.trim();
          if (v) {
            if (v.charAt(0) === '/') runInputCommand();
            else sendChat();
            res.did = 'run';
          } else {
            res.did = 'none';
          }
        }
      } else if (action === 'histUp' || action === 'histDown') {
        // 注意：这条路径**不经过** keydown 里的守卫（面板劫持、输入法组词），
        // 直接调 historyStep。用它测「历史能翻」会掩盖守卫相关回归，
        // 所以要测守卫必须用 'keydown' 动作。
        historyStep(action === 'histUp' ? -1 : 1);
        res.value = input.value;
      } else if (action === 'emit') {
        // 测试专用：把一条后端通知原样喂给分发器，走**真实**渲染路径。
        // 和 window.postMessage 等价，但可以由扩展侧驱动（宿主测试用）。
        var em = (p && p.msg) || {};
        if (em.type) dispatchServerMessage(em);
        res.emitted = em.type || null;
      } else if (action === 'keydown') {
        // 在真实输入框上派发 keydown，走**完整**的 keydown 监听器（含守卫）。
        // 回归测试要验「按 ↑ 会不会被命令面板劫持」必须用这个，不能用 histUp。
        var kd = p || {};
        var kev = new KeyboardEvent('keydown', {
          key: kd.key || '',
          code: kd.key || '',
          keyCode: kd.keyCode || 0,
          // shiftKey 必须能传：Shift+Enter 是「换行不发送」，和 Enter 是两条路。
          // 不传的话测试根本区分不了这两种行为。
          shiftKey: !!kd.shiftKey,
          ctrlKey: !!kd.ctrlKey,
          altKey: !!kd.altKey,
          metaKey: !!kd.metaKey,
          bubbles: true,
          cancelable: true
        });
        if (kd.isComposing) {
          Object.defineProperty(kev, 'isComposing', { get: function () { return true; } });
        }
        input.dispatchEvent(kev);
        res.value = input.value;
        res.prevented = kev.defaultPrevented;
        res.palOpen = palette.getAttribute('data-open');
      } else if (action === 'click') {
        var target = pick(selector);
        res.found = !!target;
        if (target) target.click();
      } else if (action === 'setValue') {
        var box = pick(selector);
        res.found = !!box;
        if (box) {
          box.value = (p && p.value) || '';
          // 触发 input 事件，走与用户输入完全相同的处理链路
          box.dispatchEvent(new Event('input', { bubbles: true }));
        }
      } else if (action === 'scrollTop') {
        var st = pick(selector);
        res.found = !!st;
        if (st) res.scrollTop = st.scrollTop;
      } else {
        res.ok = false;
        res.error = '未知动作 ' + action;
      }
    } catch (e) {
      res.ok = false;
      res.error = String(e && e.message ? e.message : e);
    }
    post('ui/result', res);
  }

  /* ---------- 事件分发 ---------- */

  /**
   * 只读状态快照，给宿主回归测试用（与 window.__narnatBranchUnlock 同一种做法：
   * 不改渲染、只在窗口上挂一个读取入口）。有了它，测试可以断言
   * 「CUR 真的切过去了」「附件列表里就是那两条路径」，
   * 而不是从按钮文字反推内部状态。
   */
  window.__narnatState = function(){
    return {
      CUR: CUR,
      curIsChild: CUR_IS_CHILD,
      real: REAL,
      lastSaved: lastSavedKey,
      // 会话列表就在闭包里，没法从外面读：给测试一个只读快照，
      // 才能断言「一条畸形 session/list 之后 SESS 没被清空」，
      // 而不是从抽屉的 DOM 反推（渲染与数据正是可能不一致的那两半）。
      sessions: SESS.map(function(s){ return { name: s.name, children: (s.children || []).length }; }),
      attach: ATCH.map(function(a){
        return { path: a.path, name: a.name, kind: a.kind, hasThumb: !!a.dataUrl };
      })
    };
  };

  window.addEventListener('message', function(ev){
    var msg = ev && ev.data;
    if (!msg || typeof msg.type !== 'string') return;
    dispatchServerMessage(msg);
  });

  /**
   * 分发一条来自扩展/webview 的消息。
   * 抽成函数是为了让测试能原样注入（uiAction('emit')），走真实渲染路径，
   * 而不是在测试里另抄一遍渲染逻辑。
   */
  function dispatchServerMessage(msg){
    if (!msg || typeof msg.type !== 'string') return;
    switch (msg.type) {
      case 'ui/query': handleUiAction(Object.assign({ action: 'query' }, msg.params || {})); break;
      case 'ui/click': handleUiAction(Object.assign({ action: 'click' }, msg.params || {})); break;
      case 'ui/setValue': handleUiAction(Object.assign({ action: 'setValue' }, msg.params || {})); break;
      case 'ui/palDown': handleUiAction(Object.assign({ action: 'palDown' }, msg.params || {})); break;
      case 'ui/palEnter': handleUiAction(Object.assign({ action: 'palEnter' }, msg.params || {})); break;
      case 'ui/histUp': handleUiAction(Object.assign({ action: 'histUp' }, msg.params || {})); break;
      case 'ui/histDown': handleUiAction(Object.assign({ action: 'histDown' }, msg.params || {})); break;
      case 'ui/keydown': handleUiAction(Object.assign({ action: 'keydown' }, msg.params || {})); break;
      case 'ui/emit': handleUiAction(Object.assign({ action: 'emit' }, msg.params || {})); break;
      case 'ui/scrollTop': handleUiAction(Object.assign({ action: 'scrollTop' }, msg.params || {})); break;
      case 'initialize': applyInitialize(msg.params || {}); break;
      case 'toggleInspector': toggleDrawer(); break;
      case 'rpc/error': showOffline(msg.params || {}); break;
      case 'turn/begin': turnBegin(msg.params || {}); break;
      case 'turn/end': turnEnd(msg.params || {}); break;
      case 'stream/delta': appendStream(msg.params || {}); break;
      case 'thinking/begin': beginThink(); break;
      case 'thinking/delta': appendThink(msg.params || {}); break;
      case 'thinking/end': endThink(msg.params || {}); break;
      case 'tool/call': addToolRow(msg.params || {}); break;
      case 'tool/result': finishTool(msg.params || {}); break;
      case 'todo/update': renderTodo(msg.params || {}); break;
      case 'verify/start': verifyStart(); break;
      case 'verify/result': verifyResult(msg.params || {}); break;
      case 'compress/begin': compressBegin(); break;
      case 'compress/end': compressEnd(msg.params || {}); break;
      case 'bg/submit': bgSubmit(msg.params || {}); break;
      case 'confirm/request': showConfirm(msg.params || {}); break;
      case 'confirm/result': applyConfirmResult(msg.params || {}); break;
      case 'stats': applyStats(msg.params || {}); break;
      case 'error': notifyError(msg.params || {}); break;
      case 'session/list': applySessionList(msg.params || {}); break;
      case 'session/saved': applySessionSaved(msg.params || {}); break;
      case 'session/result': applySessionResult(msg.params || {}); break;
      case 'attach/picked': atchOnReply(msg.params || {}); break;
      case 'attach/checked': atchOnReply(msg.params || {}); break;
      case 'attach/saved': atchOnReply(msg.params || {}); break;
      case 'attach/thumbResult': atchOnReply(msg.params || {}); break;
      case 'clipboard/written': onClipboardWritten(msg.params || {}); break;
      case 'command/result': applyCommandResult(msg.params || {}); break;
      case 'config/result': applyConfigResult(msg.params || {}); break;
      // 设置面板的三类专用回执。与 config/result 分开是因为它们的
      // 语义不同：config/result 是「某一项配置写成功了」，
      // 而下面三个是「读到了配置全貌 / 读到了 md / md 写成功了」。
      case 'config/describe': window.__narnatOnConfigDescribe && window.__narnatOnConfigDescribe(msg.params || {}); break;
      case 'narnatmd/result': window.__narnatOnNarnatMd && window.__narnatOnNarnatMd(msg.params || {}); break;
      case 'narnatmd/saved': window.__narnatOnNarnatMdSaved && window.__narnatOnNarnatMdSaved(msg.params || {}); break;
      case 'configSet/result': window.__narnatOnConfigResult && window.__narnatOnConfigResult(msg.params || {}); break;
      case 'chat/result': applyChatResult(msg.params || {}); break;
      case 'cancel/result': applyCancelResult(msg.params || {}); break;
      case 'prompt/result': onPromptResult(msg.params || {}); break;
      case 'notice': showNotice(msg.params || {}); break;
      default: break;
    }
  }

  /* 真身在 VSCode 里跑时，把可能存在的演示态注入物清掉再握手。
     生产骨架本身已经不含任何演示数据（模板里 #convo 是空的、会话名与状态栏是
     空/占位符、SESS 与 CUR 初值为空），这里只兜住一种情况：
     同一个 webview 里先加载过 preview 的演示数据（window.NARNAT_DEMO 存在）——
     那时要把它彻底抹掉，不能留在正式面板上。 */
  if (vscodeApi) {
    resetConversationDom();
    body.setAttribute('data-real', 'true');
    if (DEMO) {
      // 演示注入物（会话列表/当前会话/占位状态值）一律清空，等 initialize 填真值
      DEMO = null;
      window.NARNAT_DEMO = null;
      SESS = [];
      CUR = '';
      CUR_IS_CHILD = false;
      SESS_HIDDEN = 0;
      var demoBranch = document.querySelector('#sessBtn .branch');
      if (demoBranch) demoBranch.textContent = '';
      // 演示态的会话名原来写在输入行 chip 上，chip 已删除；
      // 现在会话名只在顶栏胶囊里，由 setChipSess 统一维护 aria-label。
      renderSessions();
    }
  }
  // 握手：扩展侧收到 ready 才补发 initialize / 离线原因
  post('ready', {});
  // 面板刚打开时焦点在 <body>，用户必须连按十几次 Tab 才到得了输入框
  //（实测：有内容时是 10 次）。VSCode 里输入框是唯一的主要入口，
  // 打开就能直接打字才是对的。用 requestAnimationFrame 让初次渲染先完成。
  requestAnimationFrame(function () {
    try {
      // 空输入框的高度要按实际占位符行数定：极窄面板里占位符会折成两行，
      // CSS 里那一行 21px 就把第二行裁掉了（实测 180px）。
      autoGrowInput();
      if (!document.activeElement || document.activeElement === document.body) input.focus();
    } catch (err) { /* 忽略 */ }
  });
})();
