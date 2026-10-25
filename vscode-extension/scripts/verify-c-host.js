/**
 * 任务 C 的宿主侧验收：直接调 extension.js 里的附件函数，
 * 用真实的临时文件验证「核对路径 / 落临时图片 / 读缩略图」三件事。
 *
 * 为什么不经过 webview：这里验的是宿主与文件系统的那一半；
 * webview 那一半（消息名、字段、chip、发送文本）由 scripts/verify-c.js
 * 在真实 DOM 里跑。两边合起来才是完整的协议。
 *
 * 用法：node scripts/verify-c-host.js
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const Module = require('module');

/* ------------------------- 最小 vscode 桩 ------------------------- */
const noopDisposable = { dispose() {} };
const vscodeStub = {
  Uri: {
    file: (p) => ({ fsPath: p, scheme: 'file' }),
    joinPath: (base, ...rest) => ({ fsPath: path.join(base.fsPath || base, ...rest) })
  },
  RelativePattern: class { constructor(base, pattern) { this.base = base; this.pattern = pattern; } },
  ViewColumn: { Beside: -2 },
  window: {
    registerWebviewViewProvider: () => noopDisposable,
    showInformationMessage: () => Promise.resolve(undefined),
    showWarningMessage: () => Promise.resolve(undefined),
    showErrorMessage: () => Promise.resolve(undefined),
    showInputBox: () => Promise.resolve(undefined),
    showOpenDialog: () => Promise.resolve(undefined),
    showTextDocument: () => Promise.resolve(undefined),
    createWebviewPanel: () => ({ webview: {}, onDidDispose() {}, dispose() {}, reveal() {} })
  },
  workspace: {
    workspaceFolders: undefined,
    getConfiguration: () => ({ get: (k, d) => d }),
    onDidChangeConfiguration: () => noopDisposable,
    onDidChangeWorkspaceFolders: () => noopDisposable,
    createFileSystemWatcher: () => ({ onDidChange() {}, onDidCreate() {}, dispose() {} }),
    openTextDocument: () => Promise.resolve({ getText: () => '' })
  },
  commands: {
    registerCommand: () => noopDisposable,
    executeCommand: () => Promise.resolve(undefined)
  }
};

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === 'vscode') return vscodeStub;
  return origLoad.apply(this, arguments);
};

const ext = require('../src/extension.js');

const results = [];
function check(ok, name, extra) {
  const tail = extra === undefined ? '' : ' :: ' + String(extra).replace(/\r?\n/g, '\\n');
  results.push('RESULT ' + (ok ? 'PASS' : 'FAIL') + ' ' + name + tail);
}

/* ------------------------------ 造素材 ------------------------------ */
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'narnat-attach-test-'));
const pngPath = path.join(dir, 'tiny.png');
// 最小的合法 PNG（1x1），确保 attachThumb 拿到的是真图片字节
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64'
);
fs.writeFileSync(pngPath, PNG);
const txtPath = path.join(dir, 'note.txt');
fs.writeFileSync(txtPath, 'hello');
const subDir = path.join(dir, 'sub');
fs.mkdirSync(subDir);

/* --------------------------- attach/check --------------------------- */
const items = ext.attachCheck([pngPath, txtPath, subDir, path.join(dir, 'ghost.png')]);
check(items.length === 4, 'attach/check 逐个返回', items.length);
check(items[0].exists === true && items[0].kind === 'image', 'attach/check 识别图片', JSON.stringify(items[0]));
check(items[1].exists === true && items[1].kind === 'file', 'attach/check 识别普通文件', JSON.stringify(items[1]));
check(items[2].exists === true && items[2].kind === 'folder', 'attach/check 识别文件夹', JSON.stringify(items[2]));
check(items[3].exists === false, 'attach/check 不存在的路径如实返回', JSON.stringify(items[3]));

/* ---------------------------- attach/save ---------------------------- */
const saved = ext.attachSave('shot.png', PNG.toString('base64'));
check(!!saved.path && saved.kind === 'image', 'attach/save 写进临时目录', JSON.stringify(saved));
check(!!saved.path && fs.existsSync(saved.path), 'attach/save 返回的路径真实存在', saved.path);
check(!!saved.path && fs.statSync(saved.path).size === PNG.length, 'attach/save 内容长度一致',
  saved.path ? fs.statSync(saved.path).size : '(none)');
check(saved.path && saved.path.indexOf('attachments_user') >= 0,
  'attach/save 落在项目附件库 attachments_user 下', saved.path);
/* 这两条随实现改口径（脚本原先写的是「临时目录 + 时间戳文件名、同名不覆盖」）：
   现在图片按 sha256 内容寻址存进项目附件库（attach-store.js 的 storeUserImage），
   同一份内容重复保存**就应该是同一个路径** —— 那正是「同一张图贴两次不占两份」
   的设计目的。原来的「同名不覆盖」判据在内容寻址下不成立（同内容本就该复用），
   所以改成验幂等。这不是放宽标准：下面新增了一条反向断言，证明不同内容
   仍然得到不同路径。 */
const saved2 = ext.attachSave('shot.png', PNG.toString('base64'));
check(saved2.path && saved2.path === saved.path,
  'attach/save 同内容幂等（内容寻址：同一张图只存一份）', saved2.path);
const otherPng = Buffer.from(PNG);
otherPng[otherPng.length - 20] = (otherPng[otherPng.length - 20] + 1) & 0xff;
const saved3 = ext.attachSave('shot.png', otherPng.toString('base64'));
check(saved3.path && saved3.path !== saved.path,
  'attach/save 不同内容得到不同路径（幂等不等于覆盖）', saved3.path);
const bad = ext.attachSave('x.png', '');
check(!!bad.error, 'attach/save 空数据返回 error', JSON.stringify(bad));
const huge = ext.attachSave('big.png', Buffer.alloc(9 * 1024 * 1024).toString('base64'));
check(!!huge.error, 'attach/save 超过 8MB 返回 error', JSON.stringify(huge));

/* ---------------------------- attach/thumb --------------------------- */
const thumb = ext.attachThumb(pngPath);
check(typeof thumb === 'string' && thumb.indexOf('data:image/png;base64,') === 0,
  'attach/thumb 返回图片 dataURL', thumb ? thumb.slice(0, 28) : '(none)');
check(ext.attachThumb(path.join(dir, 'ghost.png')) === null,
  'attach/thumb 文件被清理后返回 null（界面显示 [图片]）', String(ext.attachThumb(path.join(dir, 'ghost.png'))));
check(ext.attachThumb(txtPath) === null, 'attach/thumb 非图片返回 null', String(ext.attachThumb(txtPath)));

/* ------------------- attach/pick：宿主用原生选择框 ------------------- */
check(typeof ext.handleMessage === 'function', 'handleMessage 仍导出（webview 消息入口）', 'ok');
const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'extension.js'), 'utf8');
/* 口径变更（R45 交付前）：原断言要求 canSelectFolders: true。
   那是**错的** —— Windows/Linux 上 canSelectFiles 与 canSelectFolders 不能
   同时为 true，同时开会由文件夹选择器胜出，文本/代码文件一个都选不中
   （用户报障「只能传文件夹、不能传文本文件」的根因）。
   现在固定只选文件，断言跟着反转为 false。 */
check(src.indexOf('canSelectFolders: false') > 0,
  'attach/pick 用 canSelectFolders: false（不支持目录）', 'grep');
/* 判据只扫**代码行**：注释里为了说明「双 true 为什么是错的」必然会写出
   `canSelectFolders: true` 这几个字，拿整份源码做正则必然误报（上一版就这么红的）。
   逐行剔掉注释与块注释行后再匹配。 */
const codeLines = src.split('\n').filter((l) => {
  const t = l.trim();
  return t && !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*');
});
check(!codeLines.some((l) => /canSelectFolders:\s*true/.test(l)),
  '代码里没有 canSelectFolders: true（双 true 是失效组合）',
  '命中行数=' + codeLines.filter((l) => /canSelectFolders:\s*true/.test(l)).length);
check(src.indexOf("canSelectFiles: true") > 0, 'attach/pick 用 canSelectFiles: true', 'grep');
check(src.indexOf("'session/saved'") > 0, "EVENT_METHODS 含 'session/saved'",
  (/const EVENT_METHODS = .*/.exec(src) || ['(none)'])[0]);

/* ------------------------------ 收尾 ------------------------------ */
const fails = results.filter((l) => l.indexOf('RESULT FAIL') === 0);
console.log(results.join('\n'));
console.log('SUMMARY total=' + results.length + ' fail=' + fails.length);
try {
  fs.rmSync(dir, { recursive: true, force: true });
  // 内容寻址：saved/saved2 是同一个文件、saved3 是另一个，三个都要清
  [saved.path, saved2.path, saved3.path].forEach((p) => { if (p) fs.rmSync(p, { force: true }); });
} catch (err) { /* 清理失败不影响结论 */ }
process.exit(fails.length ? 2 : 0);
