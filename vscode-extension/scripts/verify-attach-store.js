/**
 * 验收「用户附图不再丢」（用户要求：「把用户拖进来的图也存到临时文件，
 * 这样就不用担心图丢失了」）。
 *
 * 这里测的是**真正随扩展发布的那个模块**（`src/attach-store.js`），
 * 不是复制一份逻辑重写：extension.js 也 require 同一份文件。
 *
 * dsh 的事实（回答用户的「临时文件被清理了 dsh 也能找到？」）：
 * dsh 的图**不在临时目录**，存在 `DSH_HOME/attachments/v1/objects/<2>/<sha>`
 * （`attachment-local/src/store.ts`）。它的健壮性来自「库是唯一权威、
 * 位置不被系统清理」，不是「存了多份」。库里那个对象一旦被删，
 * `readImageFile` 抛 `ATTACHMENT_NOT_FOUND`，界面只剩可点重试的失败态，
 * 没有任何自动找回路径。所以我们的目标也是「放对位置」，不是多存一份。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const store = require(path.resolve(__dirname, '..', 'src', 'attach-store.js'));

const fails = [];
const ck = (n, ok, d) => {
  console.log((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  if (!ok) fails.push(n);
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'attach-store-'));
const libDir = path.join(tmp, '.narnat', 'data', 'attachments_user');
const srcDir = path.join(tmp, 'user_files');
fs.mkdirSync(srcDir, { recursive: true });

// 两张不同的最小 PNG（内容不同 → sha 不同）
const PNG_A = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64');
const PNG_B = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64');
const imgA = path.join(srcDir, 'shot a.PNG');
const imgB = path.join(srcDir, 'shot b.png');
const txt = path.join(srcDir, 'note.txt');
const missing = path.join(srcDir, 'gone.png');
const big = path.join(srcDir, 'huge.png');
fs.writeFileSync(imgA, PNG_A);
fs.writeFileSync(imgB, PNG_B);
fs.writeFileSync(txt, 'hello');
fs.writeFileSync(big, Buffer.alloc(2048));

/* ---- 1. 目录推导 ---- */
const cfgPath = path.join(tmp, '.narnat', 'config', 'narnat.json');
ck('D1 由 narnat.json 路径推出 <项目>/.narnat/data/attachments_user',
  store.userImageDir(cfgPath, '/fallback') === libDir,
  store.userImageDir(cfgPath, '/fallback'));
ck('D2 推不出来时回落兜底目录（降级也要能用）',
  store.userImageDir('', '/fallback') === '/fallback',
  store.userImageDir('', '/fallback'));

/* ---- 2. 跨目录复制进库（核心：原文件不再被依赖） ---- */
let items = store.checkPaths([imgA, txt, missing], { dir: libDir, maxBytes: 1024 * 1024 });
ck('C1 三条输入产出三条结果', items.length === 3, String(items.length));
const a = items[0];
ck('C2 图片被复制进库（path 变成库内路径）',
  a.path !== imgA && path.dirname(a.path) === libDir, a.path);
ck('C3 原路径保留在 source 里（只作说明）', a.source === imgA, a.source);
ck('C4 库文件名保留扩展名（Read 工具靠扩展名判图片）',
  /\.png$/i.test(a.path), a.path);
ck('C5 库文件内容与原图逐字节一致',
  fs.readFileSync(a.path).equals(PNG_A), String(fs.statSync(a.path).size) + 'B');
ck('C6 文本文件不复制（只有图片例外）', items[1].path === txt && !items[1].source,
  items[1].path);
ck('C7 不存在的路径如实报 exists:false（由界面剔除）',
  items[2].exists === false && items[2].kind === '', JSON.stringify(items[2]));

/* ---- 3. 原图删除后，库内副本仍然可读（用户要的「不怕图丢」） ---- */
fs.rmSync(imgA);
const still = fs.readFileSync(a.path);
ck('K1 原图删掉后库内副本仍完整可读', still.equals(PNG_A),
  String(still.length) + 'B');
const recheck = store.checkPaths([a.path], { dir: libDir, maxBytes: 1024 * 1024 });
ck('K2 库内路径再核对时不被重复复制（path 不变）',
  recheck[0].path === a.path, recheck[0].path);

/* ---- 4. 内容寻址去重：同一张图拖两次只占一份 ---- */
const copyOfA = path.join(srcDir, 'same-content-diff-name.png');
fs.writeFileSync(copyOfA, PNG_A);
const before = fs.readdirSync(libDir).length;
const dup = store.checkPaths([copyOfA], { dir: libDir, maxBytes: 1024 * 1024 });
const after = fs.readdirSync(libDir).length;
ck('U1 同一内容不同文件名 → 落到同一个库文件',
  dup[0].path === a.path, dup[0].path + ' vs ' + a.path);
ck('U2 库里没有多出第二份', after === before, before + ' → ' + after);

/* ---- 5. 不同内容 → 不同库文件（不能互相覆盖） ---- */
const bItem = store.checkPaths([imgB], { dir: libDir, maxBytes: 1024 * 1024 })[0];
ck('S1 不同内容落到不同库文件', bItem.path !== a.path, bItem.path);
ck('S2 两张图都在', fs.existsSync(a.path) && fs.existsSync(bItem.path));

/* ---- 6. 超大图不复制，保留原路径（降级但不丢功能） ---- */
const warned = [];
const bigItem = store.checkPaths([big], {
  dir: libDir, maxBytes: 1024, onWarn: (m) => warned.push(m),
})[0];
ck('G1 超过上限的图不复制，返回原路径', bigItem.path === big, bigItem.path);
ck('G2 但仍标记为可用（exists:true, kind:image）',
  bigItem.exists === true && bigItem.kind === 'image', JSON.stringify(bigItem));

/* ---- 7. 磁盘写失败时降级而不抛（用「父级是个文件」模拟） ---- */
const roDir = path.join(tmp, 'ro-lib');
fs.mkdirSync(roDir, { recursive: true });
// 先放一个同名**文件**，让库目录的父级没法被当成目录 mkdir
const blocker = path.join(roDir, 'not-writable-file');
fs.writeFileSync(blocker, 'x');
const roWarn = [];
const roItem = store.checkPaths([copyOfA], {
  dir: path.join(blocker, 'sub'),
  maxBytes: 1024 * 1024,
  onWarn: (m) => roWarn.push(m),
})[0];
ck('F1 入库失败不抛异常，返回原路径', roItem.path === copyOfA, roItem.path);
ck('F2 失败有告警文案（便于排查）', roWarn.length === 1, roWarn.join(' | '));

/* ---- 8. 截图/粘贴路径（attachSave）与拖拽路径共用同一个库 ---- */
const saved = store.storeUserImage(libDir, PNG_B, 'clipboard.png');
ck('P1 storeUserImage 落到同一库目录', path.dirname(saved) === libDir, saved);
ck('P2 与同内容拖进来的图合并成同一个文件', saved === bItem.path, saved);

/* ---- 9. 非图片扩展名一律不复制 ---- */
const fake = path.join(srcDir, 'fake.png.txt');
fs.writeFileSync(fake, PNG_A);
const fakeItem = store.checkPaths([fake], { dir: libDir, maxBytes: 1024 * 1024 })[0];
ck('N1 非图片扩展名不复制', fakeItem.path === fake && fakeItem.kind === 'file',
  fakeItem.kind);

fs.rmSync(tmp, { recursive: true, force: true });
console.log('\nSUMMARY fail=' + fails.length);
process.exit(fails.length ? 1 : 0);
