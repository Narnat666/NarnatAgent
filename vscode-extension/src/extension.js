'use strict';

const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');
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
const EVENT_METHODS = ['stats', 'error', 'session/list', 'session/saved', 'notice'];

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
/**
 * 最近一次「曾经是权威连接」的实例。
 *
 * 进程崩溃时 exit 处理器会同步把 rpc 置空，而挂在那条连接上的请求是**之后**
 * 的微任务里才 reject —— 那一刻 rpc 已经是 null，光比对 `client !== rpc` 会把
 * 「当前连接崩了」误判成「旧客户端的迟到错误」，进而把「可能已在执行」的提示
 * 整条吞掉（R28 引入、第 22 轮实测的 P0）。这里记住它，守卫只看一件事：
 * 这个 client 有没有资格代表界面说话。
 * @type {RpcClient | null}
 */
let lastAuthority = null;

/**
 * 这个 client 是否（曾经或正在）是权威连接。
 *
 * true = 它写出/未写出的结论可以决定界面对用户说什么；
 * false = 它已经被换掉（手动重连、配置变更），它的迟到错误由重连时的补发交代。
 * @param {RpcClient | null} client
 * @returns {boolean}
 */
function isAuthorityClient(client) {
  return !!client && (client === rpc || client === lastAuthority);
}

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

/* --------------------------- 附件（webview → 宿主） ---------------------------
 * 语义（用户明确要求）：拖 / 粘贴进来的**文件、文件夹不复制、不存储**，
 * 只把绝对路径发给 AI，让它自己用 Read/Grep/Glob 处理；
 * 只有「没有磁盘路径的图片」（现场截图 / 剪贴板图片）才写进临时目录。
 * 这里只做三件事：核对路径、落临时图片、读缩略图；不碰文件内容本身。
 */

/** 临时图片目录：只有截图/剪贴板图片才落这里 */
const ATTACH_DIR = path.join(os.tmpdir(), 'narnat_attachments');
/** 单张图片上限（与前端 ATCH_MAX_IMG 一致）：超过不读、不落盘 */
const ATTACH_MAX_BYTES = 8 * 1024 * 1024;
/** 扩展名 → MIME */
const IMAGE_MIME = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml'
};

/**
 * @param {string} p 文件路径
 * @returns {string} 命中的图片扩展名（小写），不是图片返回空串
 */
function imageExt(p) {
  const m = /\.([A-Za-z0-9]+)$/.exec(String(p || ''));
  if (!m) return '';
  const ext = '.' + m[1].toLowerCase();
  return IMAGE_MIME[ext] ? ext : '';
}

/**
 * 附件类型：folder / image / file。
 * 前端猜不出文件夹（拖进来的文件夹没有扩展名），只能由宿主看 stat。
 * @param {string} p
 * @returns {string} 不存在时返回空串
 */
function attachKind(p) {
  try {
    if (fs.statSync(p).isDirectory()) return 'folder';
  } catch (err) {
    return '';
  }
  return imageExt(p) ? 'image' : 'file';
}

/**
 * 核对一批路径：存在性与类型。不存在的要如实返回 exists:false，
 * 由界面剔除并提示（不能静默丢掉用户拖进来的东西）。
 * @param {string[]} paths
 * @returns {{path: string, exists: boolean, kind: string}[]}
 */
function attachCheck(paths) {
  const items = [];
  for (const raw of (Array.isArray(paths) ? paths : [])) {
    const p = String(raw || '');
    if (!p) continue;
    let exists = false;
    try {
      exists = fs.existsSync(p);
    } catch (err) {
      exists = false;
    }
    items.push({ path: p, exists, kind: exists ? attachKind(p) : '' });
  }
  return items;
}

/**
 * 把 webview 传来的 base64 图片写到临时目录，返回绝对路径。
 * 文件名带时间戳：同名截图连拖两次不会互相覆盖（后一张顶掉前一张，
 * AI 读到的就是错的那张）。
 * @param {string} name
 * @param {string} dataB64
 * @returns {{path?: string, kind?: string, error?: string}}
 */
function attachSave(name, dataB64) {
  const safe = String(name || 'image.png').replace(/[\\/:*?"<>|]/g, '_').slice(-60) || 'image.png';
  let buf;
  try {
    buf = Buffer.from(String(dataB64 || ''), 'base64');
  } catch (err) {
    return { error: '图片数据无法解码' };
  }
  if (!buf.length) return { error: '图片数据为空' };
  if (buf.length > ATTACH_MAX_BYTES) return { error: '图片超过 8MB' };
  try {
    fs.mkdirSync(ATTACH_DIR, { recursive: true });
    const stamp = new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 17);
    const target = path.join(ATTACH_DIR, stamp + '_' + safe);
    fs.writeFileSync(target, buf);
    return { path: target, kind: 'image' };
  } catch (err) {
    return { error: err && err.message ? err.message : String(err) };
  }
}

/**
 * 读本地图片成 dataURL（webview 的 CSP 允许 img-src … data:）。
 * 文件已被清理 / 不是图片 / 超过 8MB → null，界面显示 [图片] 占位符。
 * @param {string} p
 * @returns {string|null}
 */
function attachThumb(p) {
  const file = String(p || '');
  const ext = imageExt(file);
  if (!ext) return null;
  try {
    const st = fs.statSync(file);
    if (!st.isFile() || st.size > ATTACH_MAX_BYTES) return null;
    return 'data:' + IMAGE_MIME[ext] + ';base64,' + fs.readFileSync(file).toString('base64');
  } catch (err) {
    return null;
  }
}

/**
 * 「＋附件」按钮：用 VS Code 原生选择框，文件和文件夹都要能选
 * （webview 没有文件系统权限，canSelectFolders 只有宿主能开）。
 * @param {{id?: number}} p
 */
async function pickAttachments(p) {
  const id = p ? p.id : undefined;
  let paths = [];
  let cancelled = false;
  try {
    const picked = await vscode.window.showOpenDialog({
      title: '选择要发给 AI 的文件或文件夹',
      openLabel: '添加附件',
      canSelectFiles: true,
      canSelectFolders: true,
      canSelectMany: true
    });
    if (!picked || !picked.length) {
      cancelled = true;
    } else {
      paths = picked.map((uri) => uri.fsPath);
    }
  } catch (err) {
    cancelled = true;
    console.warn('[narnat] 选择附件失败：', err && err.message ? err.message : err);
  }
  broadcast({ type: 'attach/picked', params: { id, paths, cancelled } });
}

/**
 * @param {string} message
 * @param {string} [detail]
 */
function reportError(message, detail) {
  lastError = message;
  // 【R27】这条 rpc/error 会让前端进离线态（showOffline → settlePendingMessage）：
  // 把「刚点发送、已经写到后端、只是没等到确认」的那条消息删掉、塞回输入框，
  // 并写「这条消息没有发出去」。只要曾有一条请求写出去过，就必须先补
  // chat/result{accepted:true,uncertain:true} 让它别撤回，否则同屏出现
  // 「没发出去」+「可能已在执行」——两句互斥，用户按前者重发就是再执行一遍。
  // 放在这一个出口而不是各调用点：掉线有 exit / 握手失败 / 初始化失败 / 派发时
  // 后端已不在等多条路径，漏一条就退回原 P0。
  flushUncertainReceipts('offline');
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
 *   1. NARNAT_HOME 环境变量 → 用它（但要求 <NARNAT_HOME>/.narnat 已是目录，
 *      否则视同未设置，继续走后续规则——后端就是这么判的）
 *   2. 打包 exe 装在临时目录（VSCode 扩展目录 / 系统 temp）→ 用户主目录
 *      （升级 vsix 会把扩展目录删掉重建，后端因此刻意改道 HOME）
 *   3. 其它打包 exe / 指定可执行文件 → 该文件所在目录
 *   4. 源码启动（python main.py）→ 从 cwd 向上找已有的 .narnat，找不到用 cwd
 *      （跳过用户主目录那一层；cwd 本身是主目录时仍用它）
 * @param {object} settings rpcSettings() 的返回值
 * @returns {string|null}
 */
function resolveConfigPath(settings) {
  const path = require('path');
  const fs = require('fs');
  const os = require('os');

  const cfg = (base) => path.join(base, '.narnat', 'config', 'narnat.json');

  // 1. 显式指定优先（后端同样如此）。
  //    后端只在 <NARNAT_HOME>/.narnat 是目录时才采用该变量，否则忽略、继续往下走；
  //    前端若无条件采用，设了 NARNAT_HOME 但还没生成 .narnat 的用户（首次安装最常见）
  //    点「打开配置文件」就会指向后端根本不用的路径（实测）。
  const envHome = process.env.NARNAT_HOME;
  if (envHome) {
    try {
      if (fs.statSync(path.join(envHome, '.narnat')).isDirectory()) return cfg(envHome);
    } catch (e) { /* 不存在 / 不可读：视同未设置 */ }
  }

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

  // 4. 源码启动：与后端开发模式一致，从启动目录向上找一个已有的 .narnat。
  //    与后端同步跳过用户主目录这一层：装过 bundled/vsix 版的人 HOME 下已有一份
  //    自动生成的默认配置（Key 为空），跟着它会指向用户根本没在用的文件——
  //    在工作区里填 Key 自然不生效（后端读的是工作区那份）。
  //    cwd 本身就是主目录时照旧用它（直接在主目录启动是合法用法）。
  const args = settings.args || [];
  const script = args.find((a) => typeof a === 'string' && /\.py$/i.test(a));
  let base = null;
  if (script) {
    base = path.isAbsolute(script) ? path.dirname(script) : settings.cwd;
  }
  if (!base) base = settings.cwd;
  if (!base) return null;

  const homeKey = normKey(home);
  if (normKey(base) === homeKey) return cfg(base);
  let candidate = base;
  for (let i = 0; i < 10; i++) {
    if (homeKey && normKey(candidate) === homeKey) break;
    if (fs.existsSync(path.join(candidate, '.narnat'))) return cfg(candidate);
    const parent = path.dirname(candidate);
    if (parent === candidate) break;
    candidate = parent;
  }
  return cfg(base);
}

/**
 * 路径比对用的归一化（Windows 大小写不敏感），对应后端 loader.py 的 _norm_path。
 */
function normKey(p) {
  const abs = require('path').resolve(p);
  return process.platform === 'win32' ? abs.toLowerCase() : abs;
}

/**
 * 这个目录是不是「升级就会被删掉」的临时安装位置。
 * 标记列表与后端 loader.py:_is_ephemeral_install_dir 的 markers 逐条对应（顺序也一致）：
 *   /.vscode/extensions/ · /.vscode-insiders/extensions/ · /.vscode-server/extensions/
 *   / .cursor/extensions/ · /data/extensions/（VSCode 便携版）
 *   /appdata/local/temp/ · /tmp/
 */
function isEphemeralDir(dir) {
  const low = String(dir || '').replace(/\\/g, '/').toLowerCase();
  return low.includes('/.vscode/extensions/')
    || low.includes('/.vscode-insiders/extensions/')
    || low.includes('/.vscode-server/extensions/')
    || low.includes('/.cursor/extensions/')
    || low.includes('/data/extensions/')
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
    // 只弹「找不到配置文件」等于把用户扔在原地：他没被告知该做什么。
    // 给可操作指引——路径、找谁重新生成、以及「改完要重启后端」这条口径
    // （与文件被删除、手工修改后的提示保持一致）。
    vscode.window.showErrorMessage(
      'Narnat: 找不到配置文件 ' + cfgPath + '（目录 ' + dir + '）。' +
      '后端仍在使用内存里的旧配置；点「重启后端」（命令面板里执行 ' +
      'Narnat: 重启后端（改完配置后用））会让它重新生成默认配置，' +
      '改完需要重启后端才会生效。');
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
 * 退避序列 1s/2s/4s/8s/16s/30s（指数增长，封顶 30s），连续失败 6 次后停下并把
 * 原因留在 lastError 里——避免后端真坏了时无限刷错误。
 */
const RECONNECT_BASE_DELAY = 1000;
const RECONNECT_MAX_DELAY = 30000;
const RECONNECT_MAX_TRIES = 6;
/**
 * 连接稳定跑满这么久，才认为这次重连「有效」，下次崩溃时把退避计数清零。
 * 原先握手成功就清零 —— 于是「握手成功→立刻崩溃」的循环永远停在第一档
 * （实测 0.29s 一次无限重试），退避等于没起作用。
 */
const RECONNECT_STABLE_MS = 10000;
let reconnectTimer = null;
let reconnectAttempt = 0;
/** 最近一次握手成功的时刻（0 = 当前没有活动连接） */
let connectedAt = 0;
/** 重启/重连之前的当前会话名（见 notifySessionAfterRestart） */
let sessionBeforeRestart = '';

/**
 * 握手（initialize）超时后的重试退避（毫秒）：1s/2s/4s/8s，最多 4 次。
 *
 * 为什么要重试而不是判离线：后端冷启动（首次加载配置/模型、杀软扫描 exe）
 * 常常晚于 narnat.rpc.timeout 才回 initialize，此时进程是活的、后面那条
 * chat/send 会被**完整执行**。原先超时即判死：面板永久离线（只能重载窗口），
 * 界面还写着「消息未发送」，用户重发 → 后端又执行一遍（实测 data\sessions\
 * 下出现两份同样的会话）。超时只代表「状态未知」，不代表连接坏了。
 */
const HANDSHAKE_RETRY_DELAYS = [1000, 2000, 4000, 8000];
let handshakeTimer = null;
/** 已经重试过几次（0 = 首次请求） */
let handshakeAttempt = 0;
/** 连接在、但握手还没成功（状态未知：既不是就绪，也不能判离线） */
let handshakeWaiting = false;
/**
 * 每一条 chat/send 的账本：`{ wrote, reported, noticed, promise }`。
 *
 * 为什么要一张表：R25 起这段逻辑用三个互相折算的整数（uncertainChats /
 * inflightChats / inflightReported）记「谁写出去过、谁交代过了」，同一批请求
 * 会同时落在好几个字段里——漏减一处就让界面把一条消息数成两条，多加一处就
 * 让「可能已执行」的提示消失。R27、R28 连修两次都栽在这上面（R28 甚至因为
 * 「记账归零」把崩溃路径的提示整条吞掉，退回原 P0）。
 *
 * 现在收敛成单一映射：一条请求一行，四个事实都在这一行上——
 *   wrote    已写进后端 stdin（后端可能已经在执行；false = 确定没送达）
 *   reported 已经补发过 `chat/result{uncertain:true}`（界面据此不撤回它）
 *   noticed  已经发过「可能已在执行」的提示（同一条只说一次）
 *   promise  原始请求，`promise.written` 可实时取到「有没有真的写出去」
 * 补发、收口、守卫全部读同一份数据，不再有「计数之间对不上」这种状态。
 * @type {Set<{wrote: boolean, reported: boolean, noticed: boolean, promise: any}>}
 */
const chatJournal = new Set();

/**
 * 这条 chat 到底有没有写进后端。
 *
 * **以请求自己的实时状态为准**（`promise.written`），不在登记时抄快照：
 *   · spawn 失败时 write() 会先「成功」（字节进了没人读的管道），等 ENOENT
 *     回来才被复位成「未写出」—— 抄快照就会把这条当成「可能已执行」，
 *     界面于是让用户不敢重发（R28 修的正是这个 P0）；
 *   · 反过来，请求先排队、稍后才 flush 进 stdin 时，快照是 false 而实时是 true。
 * 只有拿不到实时状态（极端情况）时才退回登记值。
 * @param {any} entry
 * @returns {boolean}
 */
function entryWrote(entry) {
  if (!entry) {
    return false;
  }
  if (entry.promise && typeof entry.promise.written === 'boolean') {
    return entry.promise.written;
  }
  return !!entry.wrote;
}

/** 已写出（可能已在执行）的 chat 条数 */
function journalSent() {
  let n = 0;
  for (const entry of chatJournal) {
    if (entryWrote(entry)) {
      n += 1;
    }
  }
  return n;
}

/** 已写出、但还没交代给界面的 chat 条数 */
function journalUnreported() {
  let n = 0;
  for (const entry of chatJournal) {
    if (entryWrote(entry) && !entry.reported) {
      n += 1;
    }
  }
  return n;
}

/** 已经发过「可能在执行」提示的 chat 条数 */
function journalNoticed() {
  let n = 0;
  for (const entry of chatJournal) {
    if (entry.noticed) {
      n += 1;
    }
  }
  return n;
}

/** 账已结清（回执与提示都做过）的条目可以退场，别让它在下次补发里再被数一遍 */
function dropSettledChats() {
  for (const entry of chatJournal) {
    if (entry.reported && entry.noticed) {
      chatJournal.delete(entry);
    }
  }
}

/**
 * 跟踪一条 chat/send 的「在途」状态（已写出、还没回执）。
 *
 * 成功 → 这条已经有回执，账直接结掉；失败 → 条目留给 catch 定性
 * （「没写出去」撤回 / 「可能已执行」提示并等补发回执）。
 * @param {() => Promise<any>} run 实际发请求的动作
 * @returns {Promise<any>}
 */
function trackChat(run) {
  const entry = { wrote: false, reported: false, noticed: false, promise: null };
  chatJournal.add(entry);
  let promise;
  try {
    promise = run();
  } catch (err) {
    chatJournal.delete(entry);
    throw err;
  }
  entry.promise = promise;
  if (promise && promise.written) {
    entry.wrote = true;
  }
  return promise.then(
    (result) => { chatJournal.delete(entry); return result; },
    (err) => {
      if (err && typeof err === 'object') {
        // 把账本条目挂在错误上：catch 里要靠它判断「这条到底写出去没有、
        // 交代过没有」，而不是靠「当前连接是不是它」这种间接推断（R29 的根因）。
        err.chatEntry = entry;
        if (err.sent === true) {
          entry.wrote = true;
        }
      }
      throw err;
    }
  );
}

/**
 * 第 attempt 次重连要等多久（指数退避，封顶 RECONNECT_MAX_DELAY）
 * @param {number} attempt 从 0 开始
 * @returns {number} 毫秒
 */
function reconnectDelay(attempt) {
  return Math.min(RECONNECT_BASE_DELAY * Math.pow(2, attempt), RECONNECT_MAX_DELAY);
}

/**
 * lastInit 里的当前会话名；没有会话时返回空串。
 * @returns {string}
 */
function currentSessionName() {
  const session = lastInit && typeof lastInit === 'object' ? lastInit.session : null;
  return session && typeof session.current === 'string' ? session.current : '';
}

/**
 * 「重启/重连后处于新对话」的提示。
 *
 * 后端没有会话恢复机制：重启后 session.current 为空，但会话仍在磁盘与列表里。
 * 原先一句提示都没有，用户看到空对话区就以为历史被删了（测试方实测）。
 * 这里只提示「去哪儿找回」，**不做**自动 session/switch：自动切回会让「重启」
 * 的语义变得不可预期，还会打断用户此刻正想开的新对话。
 * @param {any} result 新连接的 initialize 返回值
 * @param {string} reason 触发原因：restart（手动/配置变更）| crash（崩溃后自动重连）
 */
function notifySessionAfterRestart(result, reason) {
  const name = sessionBeforeRestart;
  sessionBeforeRestart = '';        // 一条只提示一次
  if (!name) {
    return;
  }
  const cur = result && result.session && typeof result.session.current === 'string'
    ? result.session.current : '';
  if (cur) {
    return;                         // 后端自己回到了某个会话，不该误报
  }
  const how = reason === 'crash' ? '后端已自动重连' : '后端已重启';
  // 一行说清两件事：现在是新对话、上一段去哪儿找。
  // 原来还带「已保存在列表里」「点右上角会话按钮可切回」两句，等于把
  // 同一个意思讲三遍。
  forwardEvent({ type: 'notice', params: { text:
    how + '，进入新对话；上一段仍在会话「' + name + '」' } });
}

function scheduleReconnect(reason) {
  if (reconnectTimer) {
    return; // 已经排上了，别叠加
  }
  if (reconnectAttempt >= RECONNECT_MAX_TRIES) {
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
  const delay = reconnectDelay(reconnectAttempt);
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
      startRpc('crash');
    } catch (err) {
      console.warn('[narnat] 自动重连失败：', err && err.message ? err.message : err);
      scheduleReconnect('重连抛错');
    }
  }, delay);
}

/**
 * 启动（或重启）后端子进程，成功后发 initialize。
 * @param {string} [reason] 谁触发的：restart（手动/配置变更）| crash（崩溃重连）
 */
function startRpc(reason) {
  // 重启前的会话名必须在 stopRpc / lastInit 清空之前抓：后端「重启即新对话」，
  // 而会话仍躺在列表里，不提示用户就会以为历史被删了（实测）。
  // 切工作区例外：旧会话属于上一个项目，新项目的列表里根本没有它，
  // 提示「已保存在列表里」是错的。
  if (reason === 'workspace') {
    sessionBeforeRestart = '';
  } else {
    const before = currentSessionName();
    if (before) {
      sessionBeforeRestart = before;
    }
  }
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
  lastAuthority = client;
  lastInit = null;
  lastError = null;

  client.on('*', (msg) => {
    if (msg && msg.method === 'session/saved') {
      // 自动保存后会话变成了正式会话：缓存的握手快照也要跟着改。
      // 不改会怎样（实测）：重载窗口时 onWebviewReady 补发的是旧的
      // lastInit（session.current 还是 null），界面回到「(未保存)」，
      // 而磁盘上那个会话明明已经存在——用户以为自动保存没生效。
      const saved = msg.params || {};
      const cur = (typeof saved.current === 'string' && saved.current)
        ? saved.current
        : (saved.switched ? String(saved.name || '') : '');
      if (cur && lastInit && typeof lastInit === 'object') {
        lastInit.session = lastInit.session || {};
        lastInit.session.current = cur;
      }
    }
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
    // 进程没了：握手重试也没意义了，停掉等待中的那一次
    clearHandshakeTimer();
    handshakeWaiting = false;
    rpc = null;
    // 这条连接崩了，但它**曾经**是权威连接：挂在它上面的请求随后会 reject，
    // 那些错误必须照常走「可能已执行」的流程（见 isAuthorityClient）。
    lastAuthority = client;
    // 进程没了，但会话还在磁盘和列表里：先记下名字（下面 lastInit 会被清空），
    // 重连成功后要告诉用户去哪儿找回
    const beforeCrash = currentSessionName();
    if (beforeCrash) {
      sessionBeforeRestart = beforeCrash;
    }
    // 只有稳定跑满 RECONNECT_STABLE_MS 的连接才把退避清零；「握手成功→立刻崩溃」
    // 的循环必须继续升级退避（原先握手即清零，实测 0.29s 一次无限重试）
    if (connectedAt && Date.now() - connectedAt >= RECONNECT_STABLE_MS) {
      reconnectAttempt = 0;
    }
    connectedAt = 0;
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
    // 【R28】spawn 失败（exe 路径配错最常见）：进程根本没起来，之前 write()
    // 的字节进的是一个没有任何进程读的管道，等于没发出去。这种情况不再需要
    // 在这里清任何计数：那条请求的 `promise.written` 恒为 false
    //（rpc-client 的 _spawnFailed 分支让 write 直接返回 'failed'），账本里
    // wrote=false 的条目永远不会被算成「已写出」，下面的 reportError 也就
    // 补不出 uncertain 回执 —— 界面只说「这条消息没有发出去」。
    if (client.spawnFailed) {
      console.log('[narnat] 后端子进程未启动（spawn 失败），在途消息一律按「未发出」处理');
    }
    // 【R29】崩溃时「已写出、还在等回执」的那条消息：前端只有 turn/begin
    // 会摘掉 [data-pending]（accepted 回执刻意不摘，见 media/narnat.js 的
    // applyChatResult）。不补这一条，回放结束后气泡仍写着「发送中」——
    // 用户既不敢重发，也等不到任何回答（第 22 轮实测的 P0：pending=1）。
    // 这一条只是把界面从「发送中」推进到「可能已执行」，不代表后端真的开工
    // 了，所以用 broadcast 而不是 forwardEvent：不能污染宿主自己的 turnActive，
    // 否则下面的 endTurnLocally 会再补一句「这一轮被中断」，而崩溃时并没有
    // 真正在跑的一轮。turnActive 为 true 时不补 —— 那种情况后端早已接受这条
    // 消息，pending 早在真正的 turn/begin 里摘过了，再补会把流式正文重置。
    if (!turnActive && journalSent() > 0) {
      broadcast({ type: 'turn/begin', params: {} });
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

  beginHandshake(client, reason);
}

/** 停掉等待中的握手重试（重启/退出/重新握手前调用） */
function clearHandshakeTimer() {
  if (handshakeTimer) {
    clearTimeout(handshakeTimer);
    handshakeTimer = null;
  }
}

/**
 * 握手彻底失败时的文案。
 * 不能说「消息未发送」：后端可能只是慢，那条消息很可能已经被执行。
 * @returns {string}
 */
function handshakeFailureText() {
  return '握手超时（后端可能仍在启动），点「重新连接」重试';
}

/**
 * 给离线态一个可点的重连入口。
 *
 * webview 里的离线提示是一行文本，没有按钮（前端不在本轮可改范围），
 * 所以用 VS Code 原生通知带一个「重新连接」按钮——这是宿主侧能提供的
 * 真·可点击入口；同时 host 也支持 webview 发 {type:'reconnect'}（见 handleMessage）。
 */
function offerReconnect() {
  try {
    const picked = vscode.window.showWarningMessage(
      'Narnat: 后端握手超时（它可能仍在启动，或已经卡住）。可以点下面的按钮重启后端。',
      '重新连接'
    );
    if (picked && typeof picked.then === 'function') {
      picked.then((pick) => {
        if (pick === '重新连接') {
          requestReconnect('通知按钮');
        }
      }, () => { /* 用户关掉通知：不影响自动重连 */ });
    }
  } catch (err) {
    console.warn('[narnat] 弹重连提示失败：', err && err.message ? err.message : err);
  }
}

/**
 * 用户主动要求重连（离线提示上的「重新连接」/ webview 的 reconnect 消息）。
 * 与命令 narnat.restartBackend 等价：重置退避计数后重新拉起后端。
 * @param {string} [from] 触发来源（日志用）
 */
function requestReconnect(from) {
  reconnectAttempt = 0;
  handshakeAttempt = 0;
  clearHandshakeTimer();
  handshakeWaiting = false;
  try {
    startRpc('restart');
    // 握手是异步的：等一小会儿再拉会话列表，避免拿到旧连接
    setTimeout(() => {
      if (rpc && rpc.isConnected()) refreshSessions(rpc);
    }, 1500);
    watchConfigFile();
    console.log('[narnat] 用户主动重连（来源：' + (from || '未标注') + '）');
    vscode.window.showInformationMessage('Narnat: 正在重新连接后端…');
  } catch (e) {
    vscode.window.showWarningMessage(
      'Narnat: 后端重连失败 — ' + (e && e.message ? e.message : e));
  }
}

/**
 * 开始（或重新开始）握手。
 * @param {RpcClient} client
 * @param {string} [reason]
 */
function beginHandshake(client, reason) {
  clearHandshakeTimer();
  handshakeAttempt = 0;
  handshakeWaiting = false;
  // 【R27】重连（手动/自动）前必须先把「已写出但没收到后端确认」的消息交代给界面，
  // 再开始新一轮握手。此前这里直接 uncertainChats = 0：超时期间那条消息的
  // 「可能已在执行」信息就此丢失，新一轮握手一旦失败，前端 showOffline() 会把
  // 它撤回输入框并写「这条消息没有发出去」，而同屏还挂着「可能已在执行」的
  // notice——用户按前者重发，后端再执行一遍（与 R25 修之前同一个 P0）。
  // 选「重连前补发」而不是「跨重连保留计数」：补发后信息落在界面上（前端只认
  // chat/result），后续无论是新握手成功、失败还是再重连，界面都不会撤回它；
  // 保留计数则要覆盖握手成功/失败/超时/写失败每条分支，漏一条就退回原 P0。
  // 第二个参数 announced=true：下面紧跟着一句汇总提示（「重连前有 N 条消息
  // 已写出…」），所以这批条目不必再逐条补「可能已在执行」，账当场结清。
  const carried = flushUncertainReceipts('reconnect', true);
  if (carried > 0) {
    // 只说「已写出、是否被执行不确定」，绝不说「没发出去」——两者互斥。
    forwardEvent({ type: 'notice', params: { text:
      '⚠ 重连前有 ' + carried + ' 条消息已写出但没等到后端确认\n' +
      '界面会保留它们；是否已在执行不确定，请先看会话列表再决定是否重发（重发会再执行一遍）'
    } });
  }
  requestInitialize(client, reason);
}

/**
 * 发一次 initialize。超时 → 后台退避重试（不判离线）；重试全失败才判离线。
 * @param {RpcClient} client
 * @param {string} [reason]
 */
function requestInitialize(client, reason) {
  client.request('initialize', {}).then(
    (result) => {
      if (rpc !== client) {
        return;
      }
      const wasWaiting = handshakeWaiting;
      handshakeWaiting = false;
      handshakeAttempt = 0;
      clearHandshakeTimer();
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
      // 记下握手时刻：退出时用它判断这次连接「撑够久了没有」，
      // 决定要不要把退避计数清零。不能在这里直接清零（见 RECONNECT_STABLE_MS）。
      connectedAt = Date.now();
      console.log('[narnat] initialize 成功，version=' + (result && result.version) +
        ' protocol=' + (result && result.protocol) +
        (wasWaiting ? '（超时后重试成功，此前只是启动慢）' : ''));
      broadcast({ type: 'initialize', params: result });
      if (wasWaiting) {
        // 超时期间界面被告知的是「可能已在执行」而不是「未发送」：这里把状态说清楚，
        // 免得用户还把刚才那条当成没发出去、再发一遍（后端会再执行一遍）。
        const lookedSent = journalNoticed();
        const extra = lookedSent > 0
          ? '\n超时期间发出的 ' + lookedSent + ' 条消息没有丢，请以会话列表为准'
          : '';
        // 这里**不**把它们的账删掉：这些条目还等着「补一条 uncertain 回执」把
        // 界面钉住（前端只在收到 chat/result 后，离线时才不撤回那条消息）。
        // 删了会怎样（实测）：下一次崩溃时 showOffline() 找不到「可能已被接受」
        // 的标记，把消息撤回输入框并写「这条消息没有发出去」，而屏幕上还挂着
        // 刚才那句「可能已在执行」——两句互斥（R25 的 P0）。
        forwardEvent({ type: 'notice', params: { text:
          '✓ 后端握手成功（之前只是启动较慢），面板已恢复' + extra } });
      }
      notifySessionAfterRestart(result, reason);
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
      const message = err && err.message ? err.message : String(err);
      const timeout = !!(err && err.timeout) || /请求超时|timed out|timeout/i.test(message);
      if (!timeout) {
        // 真写不出去（EPIPE / 进程已不在 / initialize 回了错误）：
        // 直接报可读错误，不重试、不刷屏。
        handshakeWaiting = false;
        clearHandshakeTimer();
        reportError('Narnat 后端初始化失败：' + message);
        return;
      }
      if (!client.isConnected()) {
        // 进程已经不在了：退出路径会负责提示与重连，这里不叠加
        handshakeWaiting = false;
        clearHandshakeTimer();
        return;
      }
      if (handshakeAttempt >= HANDSHAKE_RETRY_DELAYS.length) {
        handshakeWaiting = false;
        const text = handshakeFailureText();
        console.warn('[narnat] initialize 重试 ' + HANDSHAKE_RETRY_DELAYS.length +
          ' 次全部超时，判为不可用：' + message);
        // 重试全部失败才判离线，且文案只说「握手超时」——绝不说「消息未发送」，
        // 后端可能仍在启动，超时期间发出去的消息很可能已经执行。
        //
        // 【顺序要紧】补回执必须排在 reportError 之前。
        // 前端 `design/ui-full.html` 的撤回逻辑挂在 chat/result 上：收到
        // accepted:true 才记下「这条可能已被接受」，showOffline() 时才不撤回。
        // 而 reportError 会立刻推 rpc/error → 前端 showOffline() 把 [data-pending]
        // 那条从对话区删掉、塞回输入框、写「这条消息没有发出去」。
        // R24 把超时改发 notice 后这里漏了回执，结果同屏两句互相矛盾的话：
        // 「这条消息没有发出去」+ notice「可能已在执行 / 重发会再执行一遍」，
        // 用户照前者重发 → 后端再执行一遍（P0，R25 修的就是这个）。
        const lookedSent = journalSent();
        reportError(text, '已重试 ' + HANDSHAKE_RETRY_DELAYS.length + ' 次；最后一次：' + message);
        if (lookedSent > 0) {
          // 离线屏上也要交代清楚：那几条只是「没收到回执」，不代表没执行
          forwardEvent({ type: 'notice', params: { text:
            '⚠ 握手超时前发出的 ' + lookedSent + ' 条消息可能已被后端执行\n' +
            '是否已在执行不确定，请先看会话列表再决定是否重发（重发会再执行一遍）' } });
          // reportError 里的补发已经把回执发过了，这里再补一句汇总提示，
          // 这批条目就交代完毕，当场结清（否则下次重连会再数一遍）。
          for (const entry of chatJournal) {
            entry.noticed = true;
          }
          dropSettledChats();
        }
        offerReconnect();
        return;
      }
      const delay = HANDSHAKE_RETRY_DELAYS[handshakeAttempt];
      handshakeAttempt += 1;
      handshakeWaiting = true;
      console.log('[narnat] initialize 超时，' + (delay / 1000) + 's 后重试握手（第 ' +
        handshakeAttempt + '/' + HANDSHAKE_RETRY_DELAYS.length + ' 次）：' + message);
      // 用 notice 而不是 rpc/error：rpc/error 会让界面进离线态并把待发消息退回输入框，
      // 而这时连接是好的、消息可能已经被执行。
      forwardEvent({ type: 'notice', params: { text:
        '· 后端握手超时（可能仍在启动），正在第 ' + handshakeAttempt + ' 次重试…' } });
      clearHandshakeTimer();
      handshakeTimer = setTimeout(() => {
        handshakeTimer = null;
        if (rpc === client) {
          requestInitialize(client, reason);
        }
      }, delay);
    }
  );
}

function stopRpc() {
  // 主动停止（配置变更/停用扩展）不该触发重连：取消已排队的重试
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  // 旧连接的握手重试也必须停：否则它会在新连接就绪后继续跑，
  // 把「已重试 N 次」的计数和 notice 带到新会话上。
  clearHandshakeTimer();
  handshakeWaiting = false;
  handshakeAttempt = 0;
  const old = rpc;
  rpc = null;
  if (old) {
    // 主动换掉的实例仍然「曾经是权威连接」：它上面在途的请求会 reject，
    // 那些错误的去向由 beginHandshake 的补发交代（见 isAuthorityClient）。
    lastAuthority = old;
    old.dispose().catch((err) => {
      console.warn('[narnat] 关闭旧后端失败：', err && err.message ? err.message : err);
    });
  }
}

/**
 * 把「当前会话」记进握手快照。
 *
 * 为什么要单独记：lastInit 只在 initialize / session/saved 时更新，而用户
 * 用会话按钮切来切去走的是 session/switch —— 不记的话重启前 lastInit 里
 * 还是 null，P1-1 的「去哪儿找回」提示就永远不会出现（实测切完即重启时
 * 提示一条都没有）。
 * @param {unknown} name 后端报的当前会话名；空值表示回到了游离态
 */
function rememberCurrentSession(name) {
  if (!lastInit || typeof lastInit !== 'object') {
    return;
  }
  lastInit.session = lastInit.session || {};
  lastInit.session.current = typeof name === 'string' && name ? name : null;
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
      // 只在后端明确报了 current（字符串或 null）时更新；老后端不带该字段时
      // 别把已知的会话名抹掉
      if (result && (result.current === null || typeof result.current === 'string')) {
        rememberCurrentSession(result.current);
      }
    }
  } catch (err) {
    console.warn('[narnat] session/list 失败：', err && err.message ? err.message : err);
  }
}

/**
 * 这条 chat/send 是不是「已经写到后端、只是没等到回执」？
 *
 * rpc-client 给每个请求盖 sent 字段：true=已写进 stdin（后端可能已经在执行），
 * false=确定没发出去（EPIPE / 未 spawn）。只有后者才能对用户说“消息未发送”。
 * @param {any} err
 * @returns {boolean}
 */
function chatMaybeSent(err) {
  return !!(err && err.sent === true);
}

/**
 * 如实告诉用户「可能已在执行」，而不是「未发送」。
 *
 * 走 notice 而不是 chat/result：前端收到 accepted:false 会把消息撤回输入框并
 * 写「消息未发送」，用户照着重发 → 后端再执行一遍（实测两份同样的会话）。
 * 同一条请求只提示一次（账本上的 noticed）：崩溃时它既会碰到离线路径、又会
 * 碰到 catch，提示两遍等于让用户怀疑是不是发了两条。
 * @param {any} err
 * @returns {boolean} 这次是否真的提示了（false = 之前已提示过）
 */
function noteChatMaybeAccepted(err) {
  const entry = err && err.chatEntry;
  if (entry) {
    if (entry.noticed) {
      return false;
    }
    entry.noticed = true;
  }
  const ms = err && typeof err.timeoutMs === 'number' && err.timeoutMs > 0
    ? err.timeoutMs
    : (lastRpcSettings && lastRpcSettings.timeout ? lastRpcSettings.timeout : 30000);
  const secs = Math.max(1, Math.round(ms / 1000));
  const text = err && err.timeout
    ? '⚠ 后端未在 ' + secs + ' 秒内确认，这条消息可能已在执行\n' +
      '请先看会话列表再决定是否重发（重发会再执行一遍）'
    : '⚠ 与后端的连接在处理这条消息时中断，它可能已被执行\n' +
      '请先看会话列表再决定是否重发（重发会再执行一遍）';
  console.log('[narnat] chat/send 未收到回执（' + (err && err.message ? err.message : err) +
    '），按「可能已执行」上报');
  forwardEvent({ type: 'notice', params: { text } });
  return true;
}

/**
 * 把「已写出但没收到后端确认」的 chat 条数如实交代给界面。
 *
 * 补一条 `chat/result{accepted:true, uncertain:true}`：前端只认 accepted:true
 * 才在离线时**不**把那条消息撤回输入框（前端禁改，只能这样表达）。这不是
 * 「后端已受理」——uncertain:true / count / inFlight 明说只是写出去过。
 * @param {string} reason 触发场景（写进回执，便于诊断）
 * @param {boolean} [announced] 调用方紧接着还会发一句汇总提示：那这批条目就算
 *        已经交代过，catch 里不必再逐条补「可能已在执行」。
 * @returns {number} 交代了几条；0 = 没有这类消息，什么也没发
 */
function flushUncertainReceipts(reason, announced) {
  // 谁还没交代过？只认账本：一条请求一行，`reported` 一置就再也不会被数第二遍
  //（R28 之前靠两个计数相减，漏减一处就把同一条消息数成两条）。
  const fresh = [];
  for (const entry of chatJournal) {
    if (entryWrote(entry) && !entry.reported) {
      fresh.push(entry);
    }
  }
  if (!fresh.length) {
    return 0;
  }
  for (const entry of fresh) {
    entry.reported = true;
    if (announced) {
      entry.noticed = true;
    }
  }
  broadcast({ type: 'chat/result', params: {
    accepted: true,
    uncertain: true,
    count: fresh.length,
    inFlight: chatJournal.size,
    reason: reason || ''
  } });
  dropSettledChats();
  return fresh.length;
}

/**
 * chat 类请求失败时的统一出口（两个调用点共用，避免逻辑漂移）。
 *
 * 判据只有一条：**这条请求到底有没有写进后端**。写出去过就按「可能已执行」
 * 上报（绝不能说没发出去），确定没写出就交回调用方撤回。
 * @param {RpcClient} client 发起这条请求的连接
 * @param {any} err
 * @returns {boolean} true = 已经处理完，调用方直接 return
 */
function settleChatFailure(client, err) {
  const entry = err && err.chatEntry;
  const wrote = chatMaybeSent(err) || entryWrote(entry);
  if (!wrote) {
    // 确定没送出（EPIPE / spawn 失败 / 未连接）：不留在账上，由调用方回
    // accepted:false 让界面把它撤回输入框 —— 这时说「没发出去」是真话。
    if (entry) {
      chatJournal.delete(entry);
    }
    return false;
  }
  const authority = isAuthorityClient(client);
  if (!authority && !(entry && entry.reported)) {
    // 真正被换掉的旧实例、且这笔账还没交代过：它有没有执行会由重连时的补发
    // 统一交代（beginHandshake → flushUncertainReceipts）。这里再说一遍就是
    // 同一个错误记两次账（R28 的 P1-1 实测 count=2 而实际 1 条）。
    return true;
  }
  noteChatMaybeAccepted(err);
  if (entry && entry.reported) {
    // 补发的回执（界面不撤回）+ 这句提示都已完成，这条到此结清，
    // 别让它在下一次重连时被再数一遍（R28 的 P1-2）。
    chatJournal.delete(entry);
  }
  return true;
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
    if (reconnectAttempt >= RECONNECT_MAX_TRIES && !reconnectTimer) {
      // 自动重连已经放弃过。用户主动发请求 = 明确的「再试一次」信号，
      // 这里重置计数重新起后端。不重置的话（实测）：重连 5 次放弃后，
      // 无论发消息还是点界面都不会再重连，只能改配置或重载窗口。
      reconnectAttempt = 0;
    }
    scheduleReconnect('请求时后端未连接');
    return;
  }
  try {
    // chat 走 trackChat：握手最终失败时要靠「有多少条已经写出去」决定
    // 是否给界面补一条 chat/result，让它不要把这句话撤回输入框。
    const result = type === 'chat'
      ? await trackChat(() => client.request(method, params))
      : await client.request(method, params);
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
          await trackChat(() => client.request('chat/send', { text }));
        } catch (e2) {
          // 与主 catch 走同一个出口：写出去过就按「可能已执行」上报，
          // 确定没写出才回 accepted:false。两处逻辑分开写过一次，
          // 后来只改了其中一处，命令兜底路径就退回旧行为（R28 的教训）。
          if (settleChatFailure(client, e2)) {
            return;
          }
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
    const authority = isAuthorityClient(client);
    // 「请求已写出、只是没等到回执」不能回 chat/result accepted:false：
    // 前端收到它会把那条消息撤回输入框/标成「未发送」，用户重发 →
    // 后端再执行一遍（实测磁盘上出现两份同样的会话）。是否已写出只认账本
    // 上那一条（err.chatEntry），不靠「连接还是不是当前这个」来推断。
    if (type === 'chat' && settleChatFailure(client, err)) {
      return;
    }
    if (!authority) {
      // 这条请求属于**已经被换掉的旧连接**（手动/自动重连换掉的实例）。
      // 它到底有没有执行，重连流程开始时已经如实交代过一次
      // （beginHandshake → flushUncertainReceipts）；这里再记一次账会让界面
      // 把一条消息数成两条（实测 count=2 而实际 1）。
      //
      // 【R29 关键】不能再用 `rpc !== client` 判「旧连接」：进程崩溃时 exit
      // 处理器会**同步**把 rpc 置空，而这里的 catch 是**之后**的微任务才跑，
      // 那时 rpc 恒为 null。按旧判据，崩溃路径永远走这一句 return ——
      // 「可能已在执行」的提示整条消失，气泡永久停在「发送中」（P0）。
      // 现在只看这个 client 是否曾经是权威连接（见 isAuthorityClient）。
      return;
    }
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
  } else if (handshakeWaiting) {
    // 握手还在重试：既不能补 initialize（没有），也不能补 rpc/error（会判离线，
    // 面板会把待发消息退回输入框、写成「没发出去」，而消息可能已经执行了）。
    broadcast({ type: 'notice', params: { text:
      '· 后端正在启动（握手进行中），成功后本面板会自动就绪' } });
  }
  flushPendingEvents();
  // webview 重建后会话列表是空的（initialize 会清空），补拉一次
  if (rpc && rpc.isConnected() && !handshakeWaiting) {
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
  if (msg.type === 'reconnect') {
    // webview 的「重新连接」入口（离线提示上的按钮/链接发这个）。
    // 与命令 narnat.restartBackend 等价：重置退避计数后重新拉起后端并重新握手。
    requestReconnect('webview');
    return;
  }
  if (msg.type === 'prompt') {
    // 用 VSCode 原生输入框，而不是 webview 里自绘的小表单：
    // 原生的跟随主题、居中、自带确定/取消、Esc 关闭、有校验提示位，
    // 也不受面板宽度挤压（窄侧边栏里自绘 input 会被挤得很难看）。
    showPrompt(msg.params || {});
    return;
  }
  // 附件四件事都只跟宿主交互，不走 RPC 后端：
  //   attach/pick  → attach/picked      （原生选择框，文件+文件夹）
  //   attach/check → attach/checked     （存在性与类型）
  //   attach/save  → attach/saved       （截图/剪贴板图片落临时目录）
  //   attach/thumb → attach/thumbResult （缩略图 dataURL，读不到回 null）
  if (msg.type === 'attach/pick') {
    pickAttachments(msg.params || {});
    return;
  }
  if (msg.type === 'attach/check') {
    const p = msg.params || {};
    broadcast({ type: 'attach/checked', params: { id: p.id, items: attachCheck(p.paths) } });
    return;
  }
  if (msg.type === 'attach/save') {
    const p = msg.params || {};
    const out = attachSave(p.name, p.dataB64);
    broadcast({ type: 'attach/saved', params: Object.assign({ id: p.id }, out) });
    return;
  }
  if (msg.type === 'attach/thumb') {
    const p = msg.params || {};
    broadcast({ type: 'attach/thumbResult', params: { id: p.id, dataURL: attachThumb(p.path) } });
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
  if (msg.type === 'command') {
    // 命令名的类型校验：不合法就**不转发**，直接回一条可读错误回执。
    // 原先直接 dispatch 下去：{type:'command', params:{cmd:123}} 会因为
    // params.command 是 undefined 而把 "/undefined" 当文本发给后端，回执
    // 里命令名/参数/文本全空（c11.log:10 实证），用户看到一条空失败。
    for (const key of ['cmd', 'command']) {
      if (params[key] !== undefined && typeof params[key] !== 'string') {
        const text = '命令不合法：' + key + ' 必须是字符串（收到 ' + typeof params[key] + '）';
        broadcast({ type: 'command/result', params: {
          command: '', args: '', text: '',
          ok: false, handled: true, noop: false, stateChanged: [], badParams: true,
          error: text, lines: [text]
        } });
        return;
      }
    }
    const name = typeof params.command === 'string'
      ? params.command
      : (typeof params.cmd === 'string' ? params.cmd : '');
    if (!name.trim()) {
      const text = '命令不合法：缺少命令名（command 必须是非空字符串）';
      broadcast({ type: 'command/result', params: {
        command: '', args: '', text: '',
        ok: false, handled: true, noop: false, stateChanged: [], badParams: true,
        error: text, lines: [text]
      } });
      return;
    }
    // cmd 是命令名的别名：归一化成 dispatch 认识的 command 字段
    if (params.command === undefined) {
      params.command = name;
    }
  }
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
      // 走 requestReconnect：它额外把自动重连的退避计数清零，并停掉旧的握手重试
      // （否则「自动重连已放弃」的状态会让这次重启后仍不重连）。
      requestReconnect('命令');
    })
  );

  // 窗口重载后的恢复由 VSCode 负责：secondarySidebar 里的视图位置与可见性
  // 会被记住，不需要（也不能）自己注册 webview panel serializer。

  // 后端路径/参数改了就重连，避免必须重载窗口
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration('narnat.rpc')) {
        console.log('[narnat] RPC 配置已变更，重启后端');
        startRpc('restart');
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
        startRpc('workspace');
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
    // 删除不会触发 onDidChange/onDidCreate：原先把 narnat.json 删掉后
    // 一句提示都没有，而后端仍用内存里的旧配置、用户以为配置已经不生效（实测）。
    // 这里只提示「怎么恢复」，不自动重启（可能正在跑一轮任务）。
    if (typeof configWatcher.onDidDelete === 'function') {
      configWatcher.onDidDelete(() => {
        forwardEvent({ type: 'notice', params: {
          text: '配置文件已被删除：' + cfgPath + '。后端仍在使用内存里的旧配置；' +
            '点「打开配置文件」会重新生成默认配置，改完需重启后端。'
        } });
      });
    }
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
  clearHandshakeTimer();
  handshakeWaiting = false;
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
     * 握手状态：waiting=连接在但 initialize 还没成功（超时后正在退避重试，
     * 不能判离线、也不能把消息说成"未发送"）；attempt=已重试次数。
     */
    handshakeWaiting,
    handshakeAttempt,
    /**
     * 已写出、但还没交代给界面的 chat 条数。
     *
     * 【R29】不再单独维护：它就是账本里「写出去过、还没补过 uncertain 回执」
     * 的那些条目。字段名留给诊断与既有测试，语义以 chatJournal 为准。
     */
    uncertainChats: journalUnreported(),
    /** 还在等回执（或等收口）的 chat 条数 = 账本条目数 */
    inflightChats: chatJournal.size,
    /** 账本快照：一眼看出每条请求「写出去没有 / 交代过没有 / 提示过没有」 */
    chatJournal: Array.from(chatJournal).map((entry) => ({
      wrote: entryWrote(entry), reported: entry.reported, noticed: entry.noticed
    })),
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
  uiQuery, uiClick, uiSetValue,
  /** 附件三个纯函数：scripts/verify-c-host.js 直接调它们做验收（不经过 webview） */
  attachCheck, attachSave, attachThumb
};

