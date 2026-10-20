'use strict';

const vscode = require('vscode');
const { getHtml } = require('./webview');
const { RpcClient } = require('./rpc-client');

/** 对话视图 id（与 package.json contributes.views 一致） */
const CHAT_VIEW_ID = 'narnat.chatView';
/** 降级面板的 viewType（仅宿主 < 1.106 时使用） */
const PANEL_TYPE = 'narnat.chatPanel';

/** webview 消息类型 → RPC 方法（协议 §3） */
const MESSAGE_METHODS = {
  chat: 'chat/send',
  cancel: 'chat/cancel',
  confirmReply: 'confirm/reply',
  command: 'command/exec',
  sessionList: 'session/list',
  sessionNew: 'session/new',
  sessionSwitch: 'session/switch',
  sessionDelete: 'session/delete',
  sessionRename: 'session/rename',
  sessionExplore: 'session/explore',
  sessionDone: 'session/done',
  configSet: 'config/set'
};

/** 原样转发给 webview 的通知前缀（协议 §4） */
const EVENT_PREFIXES = ['turn/', 'stream/', 'thinking/', 'tool/', 'todo/', 'verify/', 'compress/', 'bg/', 'confirm/'];
/** 原样转发给 webview 的完整通知方法名 */
const EVENT_METHODS = ['stats', 'error', 'session/list', 'notice'];

/** @type {vscode.WebviewView | undefined} 辅助侧边栏里的对话视图 */
let chatView;
/** @type {vscode.WebviewPanel | undefined} 低版本宿主的降级面板 */
let panel;
/** 最近一次实际使用的界面形态：secondary / explorer / panel / null */
let surfaceKind = null;

/** 已激活的 webview，用于广播命令 @type {Set<vscode.Webview>} */
const webviews = new Set();

/** @type {RpcClient | null} */
let rpc = null;

/** initialize 的返回结果，webview 重新就绪时补发 @type {any} */
let lastInit = null;
/** 最近一次致命/非致命错误文案 @type {string | null} */
let lastError = null;
/** 最近一次实际使用的后端启动配置（诊断/测试用，含 kind） @type {any} */
let lastRpcSettings = null;
/** webview 是否已发过 ready */
let webviewReady = false;
/** webview 最近一次渲染回执（测试与诊断用） @type {any} */
let lastRender = null;
const renderLog = [];
/**
 * 渲染回执的累计条数。renderLog 会被裁到 50 条，它的 length 不能当总数；
 * 测试要按「基数 + 后续增量」取窗口，缺了总数算不出窗口的全局起点，
 * 跑过 50 条后 slice(50) 恒为空 → 明明发生了却判没发生（实测 confirm/reply）。
 */
let renderTotal = 0;
/** webview 尚未就绪时暂存的事件，就绪后补发 @type {any[]} */
const pendingEvents = [];
const PENDING_LIMIT = 200;
/**
 * 队列满时被丢掉的片段数。
 * 队列只有 200 条，面板关着跑一轮长回答会溢出——丢掉的是**最旧的**那些，
 * 也就是正文开头。重开后正文从中间开始，不说的话用户会把半截回答当完整结论。
 */
let droppedEvents = 0;

/* ------------------------------ RPC 接线 ------------------------------ */

/**
 * @param {string} method 通知方法名
 * @returns {boolean}
 */
function isForwardedEvent(method) {
  if (EVENT_METHODS.indexOf(method) >= 0) {
    return true;
  }
  return EVENT_PREFIXES.some((prefix) => method.startsWith(prefix));
}

/**
 * 是否有一轮对话正在跑（由 turn/begin 与 turn/end 通知驱动）。
 * 后端进程意外退出时用它判断要不要提示「这一轮被中断」——
 * 没有这个判断，用户只会看到半截回答永远停在那里。
 */
let turnActive = false;
function turnWasActive() {
  return turnActive;
}

/**
 * 最后一条还没被应答的确认请求。
 * webview 重建（关面板再打开、VSCode 重载界面）后必须补发：确认条只发一次，
 * 不补发的话用户眼前没有任何按钮可点，而后端还在原地等确认——
 * 这一轮永远不结束，之后所有消息都被 busy 拒绝（实测死锁）。
 */
let pendingConfirm = null;

/**
 * 转发一条 RPC 事件。webview 还没就绪时先入队，等 webview ready 后补发。
 * @param {any} message
 */
function forwardEvent(message) {
  if (message && message.type === 'turn/begin') {
    turnActive = true;
  } else if (message && message.type === 'turn/end') {
    turnActive = false;
    pendingConfirm = null;          // 轮次结束，确认条自然失效
  } else if (message && message.type === 'confirm/request') {
    pendingConfirm = message;
  } else if (message && message.type === 'confirm/result') {
    pendingConfirm = null;
  }
  if (!webviews.size) {
    pendingEvents.push(message);
    if (pendingEvents.length > PENDING_LIMIT) {
      pendingEvents.shift();
      // 丢掉的是最旧的流式片段。不说会怎样（实测）：面板关着时跑一轮，
      // 重开后正文从中间开始、前后不接，而界面上没有任何提示——
      // 用户会把半截回答当成完整结论（也可能据此改代码）。
      droppedEvents += 1;
    }
    return;
  }
  broadcast(message);
}

/**
 * 主动结束这一轮（后端被杀 / 主动重启）：补一条「不完整」提示 + turn/end。
 *
 * 必须同时把 turnActive 清掉。原先是直接 broadcast()，而 turnActive 只在
 * forwardEvent() 里更新——于是标记永久停在 true：
 *   · 重建面板时会发出假的 turn/begin{resumed}，界面显示「运行中」实际没跑
 *   · 此后每次后端退出都误报「这一轮被中断」（实测）
 * @param {string} why 给用户看的原因
 */
function endTurnLocally(why) {
  if (!turnActive) {
    return;
  }
  turnActive = false;
  pendingConfirm = null;
  broadcast({ type: 'notice', params: {
    text: '⚠ 这一轮被中断，上面的回答可能不完整\n' + why
  } });
  broadcast({ type: 'turn/end', params: { reason: 'cancelled' } });
}

/** 把 webview 就绪前攒下的事件补发出去 */
function flushPendingEvents() {
  if (!webviews.size) {
    return;
  }
  if (!pendingEvents.length) {
    // 队列里没东西，但之前丢过片段：单独说明一次，否则用户看到的正文
    // 从中间开始而毫无提示（见 forwardEvent 里的 droppedEvents）
    if (droppedEvents > 0) {
      const n = droppedEvents;
      droppedEvents = 0;
      broadcast({ type: 'notice', params: {
        text: '⚠ 面板关闭期间有 ' + n + ' 段内容没能保留，上面的回答可能不完整'
      } });
    }
    return;
  }
  const items = pendingEvents.splice(0, pendingEvents.length);
  const lost = droppedEvents;
  droppedEvents = 0;
  if (lost > 0) {
    // 放在补发之前：用户先看到「前面缺了东西」，再读到从中间开始的正文
    broadcast({ type: 'notice', params: {
      text: '⚠ 面板关闭期间有 ' + lost + ' 段内容没能保留，上面的回答可能不完整'
    } });
  }
  for (const message of items) {
    if (message.type === 'initialize' || message.type === 'rpc/error') {
      continue;
    }
    broadcast(message);
  }
}

/**
 * 向所有 webview 推送一条消息。
 * @param {any} message
 */
function broadcast(message) {
  for (const webview of webviews) {
    try {
      webview.postMessage(message);
    } catch (err) {
      console.warn('[narnat] postMessage 失败：', err && err.message ? err.message : err);
    }
  }
}

/* ---------------- UI 自省：驱动并读取 webview 真实 DOM ---------------- */

let uiSeq = 0;
/** @type {Map<number, {resolve: Function, reject: Function, timer: any}>} */
const uiWaiters = new Map();

/**
 * 让 webview 执行一个动作并返回结果。
 * 宿主测试读不到 webview 内部 DOM，靠它验证「点得开」「表格渲染成 table」这类事。
 * @param {string} action query | click | setValue | scrollTop
 * @param {object} params
 * @returns {Promise<any>}
 */
function uiAction(action, params) {
  return new Promise((resolve, reject) => {
    if (!webviews.size) {
      reject(new Error('webview 未就绪'));
      return;
    }
    const id = ++uiSeq;
    const timer = setTimeout(() => {
      uiWaiters.delete(id);
      reject(new Error('webview 无响应：' + action + ' ' + (params && params.selector)));
    }, 10000);
    uiWaiters.set(id, { resolve, reject, timer });
    broadcast({ type: 'ui/' + action, params: Object.assign({ id }, params || {}) });
  });
}

/**
 * 查询 webview 里某个元素。
 * @param {string} selector
 * @param {string[]} [attrs] 额外要读的属性
 * @param {number} [index] 第几个匹配，-1 表示最后一个（默认 0）
 */
function uiQuery(selector, attrs, index) {
  return uiAction('query', { selector, attrs: attrs || [], index: index || 0 });
}

/** 点击 webview 里的元素（走真实事件链路） */
function uiClick(selector, index) {
  return uiAction('click', { selector, index: index || 0 });
}

/** 设置输入框内容并触发 input 事件 */
function uiSetValue(selector, value) {
  return uiAction('setValue', { selector, value });
}

/**
 * 用 VSCode 原生输入框向用户要一段文本，结果回给 webview。
 *
 * webview 发 { type:'prompt', params:{ id, title, placeholder, value } }，
 * 这里回 { type:'prompt/result', params:{ id, value, cancelled } }。
 * 用 id 配对：同时点两次也只有对应的那个回执会被认领。
 *
 * @param {{id?: number|string, title?: string, placeholder?: string,
 *          value?: string, prompt?: string, validate?: string}} p
 */
async function showPrompt(p) {
  const id = p.id;
  let value;
  try {
    value = await vscode.window.showInputBox({
      title: p.title || 'Narnat',
      prompt: p.prompt || undefined,
      placeHolder: p.placeholder || undefined,
      value: typeof p.value === 'string' ? p.value : '',
      ignoreFocusOut: true,
      // 空名有没有意义取决于调用方：
      //   「保存当前对话」允许留空 → 由 AI 自动命名（placeholder 已经这么承诺，
      //     这里再拦「不能为空」就是自相矛盾，用户按提示留空反而被挡回去）
      //   「新建探索分支」不允许空名 → 由 p.requireValue 明确要求
      validateInput: (v) => {
        const s = String(v == null ? '' : v);
        if (!s.trim()) {
          if (p.requireValue) return p.validate || '不能为空';
          return undefined;          // 允许留空，交给后端自动命名
        }
        if (/[\\/:*?"<>|]/.test(s)) return '名称不能包含 \\ / : * ? " < > |';
        return undefined;
      }
    });
  } catch (err) {
    console.warn('[narnat] 输入框失败：', err && err.message ? err.message : err);
    value = undefined;
  }
  broadcast({
    type: 'prompt/result',
    params: { id: id, value: value === undefined ? '' : value, cancelled: value === undefined }
  });
}

/**
 * @param {string} message
 * @param {string} [detail]
 */
function reportError(message, detail) {
  lastError = message;
  console.warn('[narnat] ' + message + (detail ? '（' + detail + '）' : ''));
  broadcast({ type: 'rpc/error', params: { message, detail } });
}

/**
 * 解析后端可执行文件。优先级：
 *   1. 用户显式配置的 narnat.rpc.executable（非默认值即视为显式）
 *   2. 工作区里能跑源码（有 main.py + python 可用）→ python main.py --rpc
 *   3. 扩展自带的 bin/narnat.exe（发布形态）
 *   4. PATH 里的 narnat.exe
 * 前两条让「开发态」无需编译就能联调，后两条是发布形态。
 * @returns {{executable: string, args: string[], cwd?: string, timeout: number, kind: string}}
 */
function rpcSettings() {
  const config = vscode.workspace.getConfiguration('narnat');
  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  const cwd = folder ? folder.uri.fsPath : undefined;
  const timeout = config.get('rpc.timeout', 30000);
  const rawArgs = config.get('rpc.args', ['--rpc']);
  const args = Array.isArray(rawArgs) ? rawArgs : ['--rpc'];

  const configured = config.get('rpc.executable', '');
  if (configured && configured !== 'narnat.exe') {
    return { executable: configured, args, cwd, timeout, kind: 'configured' };
  }

  if (cwd) {
    const mainPy = require('path').join(cwd, 'main.py');
    if (require('fs').existsSync(mainPy)) {
      return {
        executable: process.platform === 'win32' ? 'python' : 'python3',
        args: ['main.py', ...args],
        cwd,
        timeout,
        kind: 'source'
      };
    }
  }

  // 开发态兜底：扩展目录的上一级若是仓库根（含 main.py），用源码启动。
  // 让「任意工作区 + 扩展开发宿主」都能联调，不必把后端拷进工作区。
  const repoRoot = require('path').resolve(__dirname, '..', '..');
  if (require('fs').existsSync(require('path').join(repoRoot, 'main.py'))) {
    return {
      executable: process.platform === 'win32' ? 'python' : 'python3',
      args: ['main.py', ...args],
      cwd: repoRoot,
      timeout,
      kind: 'source-repo'
    };
  }

  const bundled = require('path').join(__dirname, '..', 'bin', 'narnat.exe');
  if (require('fs').existsSync(bundled)) {
    return { executable: bundled, args, cwd, timeout, kind: 'bundled' };
  }

  return { executable: 'narnat.exe', args, cwd, timeout, kind: 'path' };
}

/**
 * 定位后端使用的配置文件路径。
 * 规则必须与后端 config/loader.py:_find_project_root 逐条一致：
 *   1. NARNAT_HOME 环境变量 → 直接用它
 *   2. 打包 exe 装在临时目录（VSCode 扩展目录 / 系统 temp）→ 用户主目录
 *      （升级 vsix 会把扩展目录删掉重建，后端因此刻意改道 HOME）
 *   3. 其它打包 exe / 指定可执行文件 → 该文件所在目录
 *   4. 源码启动（python main.py）→ 从 cwd 向上找已有的 .narnat，找不到用 cwd
 * @param {object} settings rpcSettings() 的返回值
 * @returns {string|null}
 */
function resolveConfigPath(settings) {
  const path = require('path');
  const fs = require('fs');
  const os = require('os');

  const cfg = (base) => path.join(base, '.narnat', 'config', 'narnat.json');

  // 1. 显式指定优先（后端同样如此）
  const envHome = process.env.NARNAT_HOME;
  if (envHome) return cfg(envHome);

  if (settings.kind === 'path') {
    return null;   // 裸命令名，交给 PATH 解析，扩展推不出位置
  }

  const home = os.homedir();

  // 2. 扩展内自带的 exe：后端会把数据放到 HOME。
  //    这里不跟着走的话，「打开配置文件（填 API Key）」会指向
  //    <扩展目录>/bin/.narnat/config/narnat.json —— 一个永远不会存在的路径，
  //    新装客户唯一能填 Key 的入口直接弹「找不到配置文件」（实测）。
  if (settings.kind === 'bundled') {
    return cfg(home);
  }

  // 3. 用户指定了可执行文件
  if (settings.kind === 'configured') {
    const exe = settings.executable || '';
    if (exe && path.isAbsolute(exe) && fs.existsSync(exe)) {
      const dir = path.dirname(exe);
      if (isEphemeralDir(dir)) return cfg(home);
      return cfg(dir);
    }
  }

  // 4. 源码启动：与后端开发模式一致，从启动目录向上找一个已有的 .narnat
  const args = settings.args || [];
  const script = args.find((a) => typeof a === 'string' && /\.py$/i.test(a));
  let base = null;
  if (script) {
    base = path.isAbsolute(script) ? path.dirname(script) : settings.cwd;
  }
  if (!base) base = settings.cwd;
  if (!base) return null;

  let candidate = base;
  for (let i = 0; i < 10; i++) {
    if (fs.existsSync(path.join(candidate, '.narnat'))) return cfg(candidate);
    const parent = path.dirname(candidate);
    if (parent === candidate) break;
    candidate = parent;
  }
  return cfg(base);
}

/**
 * 这个目录是不是「升级就会被删掉」的临时安装位置。
 * 标记列表与后端 loader.py:_is_ephemeral_install_dir 的 markers 逐一对应。
 */
function isEphemeralDir(dir) {
  const low = String(dir || '').replace(/\\/g, '/').toLowerCase();
  return low.includes('/.vscode/extensions/')
    || low.includes('/.vscode-insiders/extensions/')
    || low.includes('/.vscode-server/extensions/')
    || low.includes('/.cursor/extensions/')
    || low.includes('/appdata/local/temp/')
    || low.includes('/tmp/');
}

/**
 * 打开后端配置文件（首次使用时填 API Key）。
 * 文件不存在就先拉起一次后端——后端启动时会自动生成默认配置。
 */
async function openSettings() {
  const fs = require('fs');
  const path = require('path');
  const settings = lastRpcSettings || rpcSettings();
  let cfgPath = resolveConfigPath(settings);

  if (!cfgPath) {
    // 推不出位置（配置成了裸命令名等）：退回最可能的位置——工作区根。
    // 原先这里只弹一条「请在它所在目录查找」，用户根本不知道该去哪个目录，
    // 点了命令等于什么也没发生（实测）。至少试一下工作区根。
    const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
    if (folder) {
      cfgPath = path.join(folder.uri.fsPath, '.narnat', 'config', 'narnat.json');
    }
    if (!cfgPath) {
      const hint = settings.executable || '外部可执行文件';
      vscode.window.showWarningMessage(
        'Narnat: 当前用 ' + hint + ' 启动，配置文件位置由它自行决定。' +
        '请先打开一个工作区文件夹，再执行本命令。');
      return;
    }
  }

  if (!fs.existsSync(cfgPath)) {
    // 后端首次启动会自动写入默认配置，先拉一次
    const client = rpc;
    if (!client || !client.isConnected()) {
      try {
        await startRpc();
      } catch (e) {
        /* 启动失败也继续尝试：文件可能已被生成 */
      }
    }
    for (let i = 0; i < 40 && !fs.existsSync(cfgPath); i++) {
      await new Promise((r) => setTimeout(r, 100));
    }
  }

  if (!fs.existsSync(cfgPath)) {
    const dir = path.dirname(cfgPath);
    vscode.window.showErrorMessage(
      'Narnat: 找不到配置文件 ' + cfgPath + '（目录 ' + dir + '）');
    return;
  }

  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(cfgPath));
  await vscode.window.showTextDocument(doc, { preview: false });
  const hasKey = /"接口密钥"\s*:\s*"[^"]+"/.test(doc.getText());
  if (!hasKey) {
    // 不能写「重开面板即可生效」：后端只在**进程启动时**读一次配置，
    // 重开面板（webview 重建）不会重读，密钥填了也不会生效（实测）。
    // 真实路径是：改完保存 → 扩展的配置监视会提示「需要重启后端」→ 点那个按钮。
    vscode.window.showInformationMessage(
      'Narnat: 请在「智能体 → 接口密钥」处填入 API Key，保存后按提示重启后端即可生效。');
  }
}

/**
 * 后端意外退出后的自动重连（退避 + 上限）。
 *
 * 为什么需要：后端是无窗口子进程，被系统回收、被人 taskkill、自身崩溃都
 * 不会惊动 VSCode。此前只有「重载窗口」能恢复，用户看到的是
 * 「离线：后端未连接」然后卡死。这里自动重试，连上后界面自己回到正轨。
 *
 * 退避序列 1s/2s/4s/8s（封顶 8s），连续失败 5 次后停下并把原因留在
 * lastError 里——避免后端真坏了时每 8 秒刷一次错误。
 */
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 8000];
let reconnectTimer = null;
let reconnectAttempt = 0;

function scheduleReconnect(reason) {
  if (reconnectTimer) {
    return; // 已经排上了，别叠加
  }
  if (reconnectAttempt >= RECONNECT_DELAYS.length) {
    console.warn('[narnat] 自动重连已达上限，放弃（' + reason + '）');
    // 放弃后必须留下能自救的路径：用户装完 python、改好配置、或网络恢复后，
    // 只要再发一句话就该重新起后端。没有这条会怎样（实测）：重连 5 次
    // 放弃后，发消息、点界面都不会再触发重连，只能改配置或重载整个窗口——
    // 用户根本不知道还有这个土办法。
    broadcast({ type: 'notice', params: {
      text: '⚠ 自动重连已停止（' + reason + '）\n修好后随便发一句话即可重新连接'
    } });
    return;
  }
  const delay = RECONNECT_DELAYS[reconnectAttempt];
  reconnectAttempt++;
  console.log('[narnat] ' + (delay / 1000) + 's 后自动重连（第 ' +
    reconnectAttempt + ' 次，原因：' + reason + '）');
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    // 用户可能在这期间手动重载了窗口或改了配置：那时 rpc 已就绪，不必重连
    if (rpc && rpc.isConnected()) {
      console.log('[narnat] 后端已就绪，跳过自动重连');
      return;
    }
    try {
      startRpc();
    } catch (err) {
      console.warn('[narnat] 自动重连失败：', err && err.message ? err.message : err);
      scheduleReconnect('重连抛错');
    }
  }, delay);
}

/**
 * 启动（或重启）后端子进程，成功后发 initialize。
 */
function startRpc() {
  stopRpc();
  const settings = rpcSettings();
  lastRpcSettings = settings;
  const client = new RpcClient({
    command: settings.executable,
    args: settings.args,
    cwd: settings.cwd,
    timeout: settings.timeout,
    onLog: (line) => console.log('[narnat:rpc] ' + line)
  });
  rpc = client;
  lastInit = null;
  lastError = null;

  client.on('*', (msg) => {
    if (isForwardedEvent(msg.method)) {
      forwardEvent({ type: msg.method, params: msg.params });
    }
  });

  client.on('exit', (info) => {
    if (rpc !== client) {
      // 主动重启（改配置 / 点「立即重启」）换掉的旧实例：不报错误（新实例马上接上），
      // 但正在跑的那一轮必须收口。原来这里直接 return —— 用户改完 API Key 点重启，
      // 正在写的回答被掐断且没有任何「不完整」标记（用户把半句话当结论）。
      endTurnLocally('后端已重启。');
      return;
    }
    rpc = null;
    const code = info ? info.code : undefined;
    let why;
    if (info && info.error) {
      why = '启动失败：' + (info.error.message || info.error.code);
    } else if (code === 2) {
      // argparse 用法错误（最常见：exe 太旧，不认识 --rpc 参数）。
      // 只说「code=2」用户完全无从下手，而真因往往就是 PATH 里那个
      // 旧的 narnat.exe（它的 -v 还照样报版本号，光看版本号看不出来）。
      why = '后端不认识启动参数（code=2）。通常是 PATH 里的 narnat.exe 版本过旧、'
        + '不支持 --rpc，请升级插件或改用新版可执行文件';
    } else if (typeof code === 'number' && code !== 0) {
      // 非 0 退出要说成「异常退出」：和正常结束用同一句话，
      // 用户无法判断是「它干完了」还是「它崩了」（实测 code=3 也这么写）。
      why = '后端异常退出（code=' + code +
        (info && info.signal ? ', signal=' + info.signal : '') + '）';
    } else {
      why = '后端进程已退出（code=' + (code === undefined ? '?' : code) +
        (info && info.signal ? ', signal=' + info.signal : '') + '）';
    }
    reportError('Narnat 后端不可用：' + why, info && info.error ? String(info.error.code) : undefined);
    // 进程已经没了，缓存的握手快照必须作废。不作废会怎样（实测）：
    // 面板关着时后端崩掉，再打开面板走到 onWebviewReady —— 那里
    // `if (lastInit) 补 initialize; else if (lastError) 补 rpc/error`
    // 会把 rpc/error 整条吞掉，界面显示「● 已连接后端」，而进程早已退出。
    lastInit = null;
    // 跑轮中被杀：界面上那半截回答会永远停在那里，没有任何「这一轮没说完」
    // 的标记，用户回看时会把半句话当完整结论。这里补一条明确提示。
    // 走 endTurnLocally 而不是自己 broadcast：它会把 turnActive 一起清掉，
    // 否则标记永久停在 true，之后每次退出都误报「这一轮被中断」（实测）。
    endTurnLocally('后端已退出（' + why + '）');
    scheduleReconnect(why);
  });

  client.start();

  client.request('initialize', {}).then(
    (result) => {
      if (rpc !== client) {
        return;
      }
      lastInit = result;
      lastError = null;
      // 握手结果必须是个对象。为空/非对象时前端 applyInitialize 一行都读不到，
      // REAL 永远不置 true —— 界面就永久停在演示态（假对话、假回答），
      // 而后端其实是好的（实测 initialize 返回 null 就复现）。
      // 这种情况当「握手失败」处理：报错并让界面知道后端不能用。
      if (!result || typeof result !== 'object') {
        lastError = '后端返回了空的握手结果（initialize 没有内容）';
        reportError(lastError);
        return;
      }
      // 连上就清零重试计数：下次意外退出重新从 1s 开始，而不是继承旧计数
      reconnectAttempt = 0;
      console.log('[narnat] initialize 成功，version=' + (result && result.version) +
        ' protocol=' + (result && result.protocol));
      broadcast({ type: 'initialize', params: result });
      if (result && typeof result.protocol === 'number' && result.protocol > 1) {
        reportError('协议版本 ' + result.protocol + ' 高于扩展支持的版本 1，请升级扩展');
      }
      // 思考回传不再由扩展强写。
      // 原先这里每次 initialize 都发 config/set{thinkback:true}，而后端会把
      // 它持久化进用户的 narnat.json（session_callbacks.py 的 on_thinkback
      // 会 _persist_config）。后果：用户在 CLI 里 /thinkback off 关掉思考回传，
      // 下次在 VSCode 打开面板就被静默改回 true —— 面板偷偷改用户配置文件，
      // 这是不能接受的副作用（实测复现）。
      // 现在尊重用户配置：要开思考请用界面的「思考」下拉或 /thinkback 命令。
      // 连上就拉一次会话列表：否则用户打开面板看到的是空抽屉
      // （早先只在命令/session 操作后才刷新，首次打开永远是空的）
      refreshSessions(client);
    },
    (err) => {
      if (rpc !== client) {
        return;
      }
      reportError('Narnat 后端初始化失败：' + (err && err.message ? err.message : err));
    }
  );
}

function stopRpc() {
  // 主动停止（配置变更/停用扩展）不该触发重连：取消已排队的重试
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const old = rpc;
  rpc = null;
  if (old) {
    old.dispose().catch((err) => {
      console.warn('[narnat] 关闭旧后端失败：', err && err.message ? err.message : err);
    });
  }
}

/**
 * 拉一次会话列表并推给 webview。
 * @param {RpcClient} client
 */
async function refreshSessions(client) {
  try {
    const result = await client.request('session/list', {});
    if (rpc === client) {
      broadcast({ type: 'session/list', params: result });
    }
  } catch (err) {
    console.warn('[narnat] session/list 失败：', err && err.message ? err.message : err);
  }
}

/**
 * 把一条 webview 消息转成 RPC 请求。
 * @param {string} type
 * @param {string} method
 * @param {any} params
 */
async function dispatch(type, method, params) {
  const client = rpc;
  if (!client || !client.isConnected()) {
    reportError('后端未连接，无法执行 ' + type);
    // 用户此刻在等结果：顺手把重连排上，而不是让他干等到手动重载
    if (reconnectAttempt >= RECONNECT_DELAYS.length && !reconnectTimer) {
      // 自动重连已经放弃过。用户主动发请求 = 明确的「再试一次」信号，
      // 这里重置计数重新起后端。不重置的话（实测）：重连 5 次放弃后，
      // 无论发消息还是点界面都不会再重连，只能改配置或重载窗口。
      reconnectAttempt = 0;
    }
    scheduleReconnect('请求时后端未连接');
    return;
  }
  try {
    const result = await client.request(method, params);
    if (rpc !== client) {
      return;
    }
    if (type === 'chat') {
      // accepted:true 时界面已在发送时进入运行态，无需额外通知；
      // 但非 accepted（后端拒绝）必须回执，否则界面永远卡在运行态。
      // handledAsCommand：后端把「以 / 开头的文本」当命令执行了（兜底路径），
      // 命令回执也要一起带给界面，否则用户看不到执行结果。
      broadcast({ type: 'chat/result', params: Object.assign(
        { accepted: !!(result && result.accepted) }, result || {}) });
      return;
    }
    if (type === 'cancel') {
      broadcast({ type: 'cancel/result', params: result || {} });
      return;
    }
    if (type === 'confirmReply') {
      // 用户已经应答过了，这条确认条不该再补发。不清会怎样（实测）：
      // 点了确认/取消、后端还在跑，这时关掉面板再打开 —— onWebviewReady
      // 会把 pendingConfirm 原样补发，界面上又出现一张已经消费过的确认条，
      // 点下去没有任何效果（后端早就不等了）。
      // 原来等后端发 confirm/result 通知来清，但后端从不发这个通知（那是
      // 宿主发给前端的），等于永远清不掉。
      pendingConfirm = null;
      // 回执仅用于诊断；界面在点击时已本地收起确认条
      broadcast({ type: 'confirm/result', params: result || { accepted: true } });
      return;
    }
    if (type === 'command') {
      if (result && result.handled === false) {
        // 不是已知命令：告诉 webview「这不是命令」，由它把输入当普通对话渲染，
        // 然后这里再转成 chat/send。少了这条回执，用户输入 /xxx 会什么都不显示。
        broadcast({ type: 'command/result', params: {
          command: params.command || '',
          args: params.args || '',
          text: params.text || '',
          handled: false
        } });
        const text = typeof params.text === 'string' && params.text
          ? params.text
          : '/' + params.command + (params.args ? ' ' + params.args : '');
        // 内层 chat/send 失败（最常见是上一轮还在跑 → busy）必须回执。
        // 不回会怎样（实测）：界面已把这句话画成已发出并进入运行态，
        // 而这一轮永远不会有 turn/end —— 一直「运行中」等一个不会来的回答，
        // 界面上也没有任何提示说「上一轮还在跑」。
        try {
          await client.request('chat/send', { text });
        } catch (e2) {
          broadcast({ type: 'chat/result', params: {
            accepted: false,
            error: e2 && e2.message ? e2.message : String(e2)
          } });
        }
        return;
      }
      // command/exec 的结果里没有命令名，必须由这里补上：
      // 否则 webview 拼出来只有 "/"，用户看不出执行了什么
      broadcast({ type: 'command/result', params: Object.assign({
        command: params.command || '',
        args: params.args || '',
        text: params.text || '',
        // origin:'ui' = 用户点界面按钮触发的，不是自己敲的命令。
        // 界面据此显示「保存成功」而不是回显 /save xxx（用户没打过这行）。
        origin: params.origin || ''
      }, result) });
      if (result && Array.isArray(result.stateChanged) && result.stateChanged.indexOf('session') >= 0) {
        await refreshSessions(client);
      }
      return;
    }
    if (type === 'sessionList') {
      broadcast({ type: 'session/list', params: result });
      return;
    }
    if (type === 'sessionSwitch' || type === 'sessionDelete' ||
        type === 'sessionRename' ||
        type === 'sessionExplore' || type === 'sessionDone' ||
        type === 'sessionNew') {
      broadcast({ type: 'session/result', params: Object.assign({ action: type }, result) });
      await refreshSessions(client);
      return;
    }
    if (type === 'configSet') {
      broadcast({ type: 'config/result', params: result });
      return;
    }
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    // chat/cancel 失败必须显式回执：否则 webview 会一直停在「运行中」
    // （stop 按钮不还原、输入框不可用）。busy 是最常见的失败原因。
    if (type === 'chat') {
      broadcast({ type: 'chat/result', params: { accepted: false, error: message } });
    } else if (type === 'cancel') {
      broadcast({ type: 'cancel/result', params: { cancelled: false, error: message } });
    } else if (type === 'sessionNew' || type === 'sessionSwitch' ||
               type === 'sessionDelete' || type === 'sessionRename' ||
               type === 'sessionExplore' || type === 'sessionDone') {
      // 会话操作失败也必须回执：原来这类失败只走下面的 reportError，
      // 前端压根不知道「新建没成功」，用户会以为已经开了新对话，
      // 接着 /save 就把上一段的全部历史存进新会话（实测真的发生过）。
      broadcast({ type: 'session/result', params: {
        action: type, error: message
      } });
    } else if (type === 'command') {
      // command/exec 抛 JSON-RPC 错误（-32000 业务拒绝、-32603 内部错误）时也必须回执。
      // 原来这条只在 handled:false 时回，异常路径一条都不发 —— 界面点了「保存当前为…」
      // 完全没反应，用户不知道存没存（实测：后端回 -32000 时 webview 收到 0 条）。
      broadcast({ type: 'command/result', params: Object.assign({
        command: params && params.command || '',
        args: params && params.args || '',
        text: params && params.text || '',
        origin: params && params.origin || ''
      }, { ok: false, handled: true, noop: false, lines: [message], stateChanged: [],
           error: message }) });
    } else if (type === 'configSet') {
      // config/set 抛错（-32000 写盘失败 / -32603 内部错误）必须回执。
      // 不回执会怎样（实测）：界面点下拉时已经本地打了勾、换了按钮文字，
      // 后端其实没写成功 —— 用户以为切换了模型，下次重开面板又变回旧值，
      // 中间那几轮还在按旧模型跑（花的是另一个价）。
      broadcast({ type: 'config/result', params: {
        ok: false, error: message, message: message
      } });
    } else if (type === 'confirmReply') {
      // confirm/reply 失败（超时、后端已不在等这条确认）也必须回执。
      // 不回执会怎样（实测）：界面点完就写了「已提交确认，等待后端执行…」，
      // 而后端根本没收到 —— 那句话永久停在等待状态，用户一直等一个不会发生的执行。
      broadcast({ type: 'confirm/result', params: {
        accepted: false, ok: false, error: message, message: message
      } });
    }
    // 业务拒绝不是掉线：busy 表示「上一轮还在跑」，属于正常应答。
    // 把它当离线会挂一条「⚡离线：后端未连接」横幅，还会把运行态打成 false——
    // 停止按钮随之消失，那一轮明明还在跑却停不下来（实测）。
    // -32601「方法不存在」也不是掉线：那是后端版本不匹配（常见于 exe 过旧），
    // 报成离线会让用户去查网络，而真正要做的是升级后端。
    const isBusinessReject = (err && err.code === -32000) ||
      (err && err.code === -32601) ||
      /\bbusy\b/i.test(message) ||
      /上一轮还在运行/.test(message);
    if (rpc === client && !isBusinessReject) {
      // 后端还活着（这是它回的 JSON-RPC 错误）→ 只是这一条请求失败，
      // 绝不能挂「离线」横幅。实测：后端回 -32603 内部错误时界面写着
      // 「⚡离线：后端未连接」，用户会去查网络，而真正的原因在后端日志里。
      if (rpc.isConnected && rpc.isConnected()) {
        broadcast({ type: 'notice', params: {
          text: '⚠ 请求失败（' + type + '）：' + message
        } });
      } else {
        reportError(message + '（' + type + '）');
      }
    }
  }
}

/**
 * webview 就绪：补发握手结果或离线原因。
 * @param {string} source
 */
function onWebviewReady(source) {
  webviewReady = true;
  console.log(`[narnat] webview ready (${source})`);
  if (lastInit) {
    broadcast({ type: 'initialize', params: lastInit });
  } else if (lastError) {
    broadcast({ type: 'rpc/error', params: { message: lastError } });
  }
  flushPendingEvents();
  // webview 重建后会话列表是空的（initialize 会清空），补拉一次
  if (rpc && rpc.isConnected()) {
    const conn = rpc;      // 记住发起这次请求时的连接
    refreshSessions(conn);
    // 补发的 lastInit 是「第一次握手那一刻」的快照，里面的 session.messages
    // 可能是空的（那时还没聊）——重建后的面板于是只剩一句「已连接后端」，
    // 用户以为记录丢了。这里重新问一次后端拿最新的会话状态。
    // 拿到后要覆盖 lastInit：缓存快照比它旧，留给下一次重建只会更旧。
    conn.request('initialize', {}).then((fresh) => {
      // 必须同时判 rpc === conn：上一次连接的回调可能在重启之后才回来，
      // 那时 rpc 已经指向新连接，只判 `rpc && fresh` 会把旧进程的快照
      // 写进 lastInit —— 界面于是拿到上一个后端（旧 cwd、旧会话、旧模型）。
      if (rpc && rpc === conn && fresh) {
        lastInit = fresh;
        broadcast({ type: 'initialize', params: fresh });
        refreshSessions(conn);
      }
    }).catch((err) => {
      console.warn('[narnat] 重建后重新握手失败：', err && err.message ? err.message : err);
    });
  }
  // 后端还在等确认：把那条确认条补发，否则用户眼前没有可点的按钮，
  // 这一轮永远不结束（实测死锁，之后所有消息被 busy 拒绝）。
  if (pendingConfirm) {
    broadcast(pendingConfirm);
  }
  // 一轮正在跑：把运行态补回去，否则重建后的界面没有停止按钮，
  // 用户既停不下来也发不出去（实测）。
  if (turnActive) {
    broadcast({ type: 'turn/begin', params: { resumed: true } });
  }
}

/**
 * 对话视图（辅助侧边栏）的 provider。
 *
 * 用 WebviewView 而不是 WebviewPanel：面板会占用一个编辑器标签（和打开的文件
 * 平级），而侧边栏视图是「文件目录」那种形态——固定在辅助侧边栏里、不挤占代码。
 * 这也是 Copilot Edits 的形态。
 */
class ChatViewProvider {
  /**
   * @param {vscode.Uri} extensionUri
   */
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
  }

  /**
   * @param {vscode.WebviewView} view
   */
  resolveWebviewView(view) {
    chatView = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this.extensionUri, 'media'),
        vscode.Uri.joinPath(this.extensionUri, 'src')
      ]
    };
    view.webview.html = getHtml(view.webview, this.extensionUri);
    webviews.add(view.webview);
    view.webview.onDidReceiveMessage((msg) => handleMessage(msg, 'view'));
    view.onDidChangeVisibility(() => {
      // 从隐藏切回可见时 webview 可能已被重建，补一次会话列表
      if (view.visible && rpc && rpc.isConnected()) {
        refreshSessions(rpc);
      }
      // context key 要跟着可见性走，不能只在 dispose 时置 false：
      // 用户点 × 隐藏侧边栏并不 dispose（只是不可见），于是「切换调度面板」
      // 这个按钮的 when 条件一直是 true，标题栏里那个按钮再也回不来（实测）。
      vscode.commands.executeCommand('setContext', 'narnatPanelVisible', !!view.visible);
    });
    view.onDidDispose(() => {
      webviews.delete(view.webview);
      if (chatView === view) {
        chatView = undefined;
      }
      vscode.commands.executeCommand('setContext', 'narnatPanelVisible', false);
    });
    vscode.commands.executeCommand('setContext', 'narnatPanelVisible', true);
  }
}

/**
 * 降级路径：宿主不支持辅助侧边栏容器时，退回编辑器区面板。
 *
 * 为什么留着：secondarySidebar 容器要 VSCode 1.106+。低于这个版本的宿主
 * 不会注册该容器，focus 命令也就不存在。此时若什么都不做，用户点图标后
 * 一片空白——比"旧形态"糟得多。宁可形态差一点，也要能用。
 *
 * @param {vscode.Uri} extensionUri
 * @param {vscode.ViewColumn} [column]
 */
function openChatPanelFallback(extensionUri, column) {
  if (panel) {
    try {
      panel.reveal();
      return panel;
    } catch (err) {
      panel = undefined;
    }
  }
  const created = vscode.window.createWebviewPanel(
    PANEL_TYPE,
    'Narnat',
    column || vscode.ViewColumn.Beside,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [
        vscode.Uri.joinPath(extensionUri, 'media'),
        vscode.Uri.joinPath(extensionUri, 'src')
      ]
    }
  );
  panel = created;
  const webview = created.webview;
  created.iconPath = vscode.Uri.joinPath(extensionUri, 'media', 'icon.svg');
  webview.html = getHtml(webview, extensionUri);
  webviews.add(webview);
  webview.onDidReceiveMessage((msg) => handleMessage(msg, 'panel'));
  created.onDidDispose(() => {
    webviews.delete(webview);
    if (panel === created) {
      panel = undefined;
    }
    vscode.commands.executeCommand('setContext', 'narnatPanelVisible', false);
  });
  vscode.commands.executeCommand('setContext', 'narnatPanelVisible', true);
  return created;
}

/**
 * 打开/聚焦对话视图。
 *
 * 三种宿主的实际情况（都实测过）：
 *   1. VSCode ≥ 1.106：`narnatSidePanel` 容器存在 → 视图在**右侧辅助侧边栏**
 *   2. VSCode < 1.106：不认 `viewsContainers.secondarySidebar`，VSCode 会把视图
 *      退化注册到 explorer 容器（实测日志 `Added views:narnat.chatView in
 *      workbench.view.explorer`）→ 视图仍在侧边栏形态，只是在左侧
 *   3. 连视图都没注册：退回编辑器区面板，至少能用
 *
 * @param {vscode.Uri} [extensionUri]
 * @returns {Promise<'secondary'|'explorer'|'panel'|'none'>} 实际使用的形态
 */
async function openChatView(extensionUri) {
  let secondary = false;
  // 第一步：展开辅助侧边栏（低版本没有这个容器，失败属正常）
  try {
    await vscode.commands.executeCommand('workbench.view.extension.narnatSidePanel');
    secondary = true;
  } catch (err) {
    console.warn('[narnat] 无辅助侧边栏容器，视图将退化到侧边栏：',
      err && err.message ? err.message : err);
  }
  // 第二步：聚焦视图本身。视图被退化注册到 explorer 时这条依然有效
  try {
    await vscode.commands.executeCommand(CHAT_VIEW_ID + '.focus');
    vscode.commands.executeCommand('setContext', 'narnatPanelVisible', true);
    surfaceKind = secondary ? 'secondary' : 'explorer';
    return surfaceKind;
  } catch (err) {
    console.warn('[narnat] 聚焦对话视图失败，退回编辑器面板：',
      err && err.message ? err.message : err);
  }
  // 第三步：降级为编辑器面板
  if (extensionUri) {
    openChatPanelFallback(extensionUri);
    surfaceKind = 'panel';
    return 'panel';
  }
  vscode.window.showWarningMessage(
    'Narnat: 无法打开对话界面，请把 VSCode 升级到 1.106 或更高版本。');
  surfaceKind = 'none';
  return 'none';
}

/**
 * @param {any} msg
 * @param {string} source
 */
function handleMessage(msg, source) {
  if (!msg || typeof msg.type !== 'string') {
    return;
  }
  if (msg.type === 'ready') {
    onWebviewReady(source);
    return;
  }
  if (msg.type === 'prompt') {
    // 用 VSCode 原生输入框，而不是 webview 里自绘的小表单：
    // 原生的跟随主题、居中、自带确定/取消、Esc 关闭、有校验提示位，
    // 也不受面板宽度挤压（窄侧边栏里自绘 input 会被挤得很难看）。
    showPrompt(msg.params || {});
    return;
  }
  if (msg.type === 'rendered') {
    lastRender = msg.params || {};
    renderLog.push(lastRender);
    renderTotal += 1;
    if (renderLog.length > 50) {
      renderLog.shift();
    }
    return;
  }
  if (msg.type === 'ui/result') {
    const r = msg.params || {};
    const waiter = uiWaiters.get(r.id);
    if (waiter) {
      clearTimeout(waiter.timer);
      uiWaiters.delete(r.id);
      if (r.ok === false) {
        waiter.reject(new Error(r.error || 'webview 操作失败'));
      } else {
        waiter.resolve(r);
      }
    }
    return;
  }
  const method = MESSAGE_METHODS[msg.type];
  if (!method) {
    return;
  }
  const params = msg.params && typeof msg.params === 'object' ? msg.params : {};
  dispatch(msg.type, method, params);
}

/**
 * 切换调度面板：由 media/narnat.js 折叠/展开右侧面板。
 */
function toggleInspector() {
  for (const webview of webviews) {
    webview.postMessage({ type: 'toggleInspector' });
  }
}

/**
 * 左侧活动栏图标：平时只放一张引导卡（点一下就在右侧唤起面板）。
 * 活动栏图标本身不能绑定命令，所以靠视图被展开时立刻打开右侧面板实现
 * 「点左边图标 → 右边弹出来」。
 */
const LAUNCHER_HTML = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<style>
  body{margin:0;padding:16px;font-family:var(--vscode-font-family);
    font-size:12px;color:var(--vscode-descriptionForeground);line-height:1.7}
  .t{color:var(--vscode-editor-foreground);font-size:13px;margin-bottom:6px}
  .k{font-family:var(--vscode-editor-font-family);font-size:11px;
    background:var(--vscode-textCodeBlock-background);padding:1px 4px;border-radius:3px}
</style></head><body>
<div class="t">对话在右侧辅助侧边栏</div>
<div>点一下这个图标，右侧辅助侧边栏会打开 Narnat 对话（和 Copilot Edits 同一位置）。</div>
<div style="margin-top:10px">也可以用命令
<span class="k">Narnat: 打开对话</span> 唤起。</div>
</body></html>`;

class NarnatLauncherProvider {
  /**
   * @param {vscode.Uri} extensionUri
   */
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
  }

  /**
   * 视图被展开 → 聚焦对话视图，随后把左栏还给资源管理器。
   *
   * 只有视图确实在**右侧**辅助侧边栏时才切回 explorer：
   * 低版本宿主会把视图退化注册进 explorer 容器（左侧），
   * 这时切回资源管理器会把刚聚焦的对话视图顶掉。
   * @param {vscode.WebviewView} view
   */
  resolveWebviewView(view) {
    view.webview.options = { enableScripts: false };
    view.webview.html = LAUNCHER_HTML;
    // 视图可能被 VSCode 缓存：用户关掉面板后再点图标，不会重新走
    // resolveWebviewView，只会改变可见性。不监听就「点图标没反应」——
    // 而卡片上明明写着点一下就会打开（实测连点 10 次都不出现）。
    this._open();
    view.onDidChangeVisibility(() => {
      if (view.visible) {
        this._open();
      }
    });
  }

  /** 唤起右侧对话面板；在右侧时把左栏还给资源管理器 */
  _open() {
    setTimeout(async () => {
      try {
        const surface = await openChatView(this.extensionUri);
        if (surface === 'secondary') {
          // 对话在右侧，左栏还给文件树
          await vscode.commands.executeCommand('workbench.view.explorer');
        } else if (surface === 'explorer') {
          // 对话就在左侧侧边栏里，保持显示，不要再切走
          console.log('[narnat] 低版本宿主：对话视图位于左侧侧边栏');
        }
      } catch (err) {
        console.warn('[narnat] 唤起对话视图失败：', err && err.message ? err.message : err);
      }
    }, 0);
  }
}

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
  const extensionUri = context.extensionUri;

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      'narnat.launcher',
      new NarnatLauncherProvider(extensionUri)
    )
  );

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      CHAT_VIEW_ID,
      new ChatViewProvider(extensionUri),
      { webviewOptions: { retainContextWhenHidden: true } }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('narnat.openChat', () => {
      openChatView(extensionUri);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('narnat.toggleInspector', () => {
      toggleInspector();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('narnat.openSettings', () => {
      openSettings();
    })
  );

  // 手动重启后端。
  // 为什么需要：后端代码/配置文件改了之后，只有重启进程才会重新读——
  // 而扩展原先没有任何重启入口，开发者只能去任务管理器杀 python.exe，
  // 普通用户改完配置更是一点办法都没有（实测：手改 narnat.json 后
  // 界面零提示、配置不生效，与「打开配置文件」这条引导正好矛盾）。
  context.subscriptions.push(
    vscode.commands.registerCommand('narnat.restartBackend', () => {
      // startRpc 是同步的（内部起子进程 + 异步握手），所以这里不能 await。
      // 用握手结果来判断成功与否更准确。
      try {
        startRpc();
        // 握手是异步的：等一小会儿再拉会话列表，避免拿到旧连接
        setTimeout(() => {
          if (rpc && rpc.isConnected()) refreshSessions(rpc);
        }, 1500);
        watchConfigFile();
        vscode.window.showInformationMessage('Narnat: 正在重启后端…');
      } catch (e) {
        vscode.window.showWarningMessage(
          'Narnat: 后端重启失败 — ' + (e && e.message ? e.message : e));
      }
    })
  );

  // 窗口重载后的恢复由 VSCode 负责：secondarySidebar 里的视图位置与可见性
  // 会被记住，不需要（也不能）自己注册 webview panel serializer。

  // 后端路径/参数改了就重连，避免必须重载窗口
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration('narnat.rpc')) {
        console.log('[narnat] RPC 配置已变更，重启后端');
        startRpc();
      }
    })
  );

  // 换工作区（打开另一个文件夹）必须重启后端。
  // 后端的 cwd / 配置路径 / 会话目录都是在启动时按「当时的工作区」定的，
  // 不重启会怎样（实测）：用户打开新项目，界面上路径、会话列表、对话内容
  // 全是旧项目的，agent 执行的读写也落在旧项目里 —— 用户以为在改新项目，
  // 实际改的是上一个项目。这比「没反应」严重得多。
  // 先判方法存在：宿主环境（含测试桩、极旧 VSCode）不一定实现它，
  // 直接调会让整个 activate 抛错，插件完全不可用（实测 bridge 测试全项失败）。
  if (typeof vscode.workspace.onDidChangeWorkspaceFolders === 'function') {
    context.subscriptions.push(
      vscode.workspace.onDidChangeWorkspaceFolders(() => {
        console.log('[narnat] 工作区已切换，重启后端');
        if (rpc) {
          endTurnLocally('工作区已切换。');
        }
        startRpc();
        // 配置文件监视也要跟着挂到新项目。不重挂会怎样（实测）：
        // configWatcher 仍盯着旧项目的 narnat.json —— 旧文件被改时弹「需要重启
        // 后端」而那个文件已经不被后端使用；新项目的配置被改时一句话都没有。
        watchConfigFile();
      })
    );
  }

  // 监视后端配置文件（narnat.json）的手工修改。
  // 为什么要管：命令面板里那条「打开配置文件（填 API Key）」会把用户送到
  // 这个文件前，用户改完保存——但后端进程只在启动时读一次配置，
  // 于是「改了没反应、也没有任何提示」（实测）。这与那条引导正好矛盾。
  // 这里不自动重启（可能正在跑一轮任务，静默重启会打断），
  // 而是提示 + 给一个一键重启的按钮，把决定权留给用户。
  watchConfigFile();

  startRpc();

  const autoOpen = vscode.workspace.getConfiguration('narnat').get('autoOpen', true);
  if (autoOpen) {
    setTimeout(() => {
      if (!chatView && !panel) {
        openChatView(extensionUri);
      }
    }, 200);
  }

  // VSCode 的 extension.exports 取的是 activate() 的返回值（不是 module.exports）
  return { handleMessage, rpcState, uiQuery, uiClick, uiSetValue, uiAction };
}

/**
 * 监视后端配置文件的手工修改，改动后提示用户重启后端。
 *
 * 只在「文件确实变了」时提示一次，并且 3 秒内合并多次保存（编辑器
 * 保存时可能连写几次）。不自动重启：那一轮可能正在跑，静默重启会打断它。
 */
let configWatcher = null;
let configNoticeTimer = null;
function watchConfigFile() {
  try {
    if (configWatcher) {
      configWatcher.dispose();
      configWatcher = null;
    }
    const settings = lastRpcSettings || rpcSettings();
    const cfgPath = resolveConfigPath(settings);
    if (!cfgPath) return;
    const folderUri = vscode.Uri.file(cfgPath);
    const pattern = new vscode.RelativePattern(
      vscode.Uri.file(require('path').dirname(cfgPath)),
      require('path').basename(cfgPath));
    configWatcher = vscode.workspace.createFileSystemWatcher(pattern);
    const onChange = () => {
      // 合并连写：3 秒内的多次保存只提示一次
      if (configNoticeTimer) clearTimeout(configNoticeTimer);
      configNoticeTimer = setTimeout(() => {
        configNoticeTimer = null;
        vscode.window.showInformationMessage(
          'Narnat: 配置文件已修改，需要重启后端才会生效',
          '立即重启'
        ).then((pick) => {
          if (pick === '立即重启') {
            vscode.commands.executeCommand('narnat.restartBackend');
          }
        });
      }, 3000);
    };
    configWatcher.onDidChange(onChange);
    configWatcher.onDidCreate(onChange);
    console.log('[narnat] 已监视配置文件: ' + folderUri.fsPath);
  } catch (e) {
    console.warn('[narnat] 配置监视启动失败：', e && e.message ? e.message : e);
  }
}

async function deactivate() {
  // 扩展停用/窗口关闭：必须取消重连，否则会在卸载后拉起孤儿后端
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const client = rpc;
  rpc = null;
  // 侧边栏视图由 VSCode 管理生命周期；降级面板需要自己释放
  chatView = undefined;
  if (panel) {
    panel.dispose();
    panel = undefined;
  }
  if (client) {
    try {
      await client.dispose();
    } catch (err) {
      console.warn('[narnat] 关闭后端失败：', err && err.message ? err.message : err);
    }
  }
}

/** 供宿主测试读取内部状态 */
function rpcState() {
  return {
    connected: !!(rpc && rpc.isConnected()),
    pid: rpc ? rpc.pid : undefined,
    webviewReady,
    lastInit,
    lastError,
    lastRender,
    renders: renderLog.slice(-50),
    /**
     * 渲染日志的累计总条数（不是渲染窗口长度，窗口只留最后 50 条）。
     * 测试用「基数 + 后续增量」取窗口，缺了总数就算不出窗口的全局起点，
     * 跑过 50 条后 slice(50) 恒为空 → 明明发生了却判没发生（实测 confirm/reply）。
     */
    renderTotal,
    webviewCount: webviews.size,
    panelVisible: !!(chatView && chatView.visible) || !!panel,
    /**
     * 界面形态：secondary=右侧辅助侧边栏（1.106+）
     *          explorer=低版本退化到左侧侧边栏
     *          panel=降级为编辑器面板
     */
    surface: surfaceKind,
    rpcKind: lastRpcSettings ? lastRpcSettings.kind : null,
    rpcCommand: lastRpcSettings ? lastRpcSettings.executable : null,
    rpcArgs: lastRpcSettings ? lastRpcSettings.args : null
  };
}

module.exports = {
  activate, deactivate, handleMessage, rpcState,
  uiQuery, uiClick, uiSetValue
};

