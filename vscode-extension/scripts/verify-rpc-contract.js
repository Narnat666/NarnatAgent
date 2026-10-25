/**
 * 钉住 RPC 契约：webview 发出的**每一种** post 类型，宿主的 dispatch 都必须能接住。
 *
 * ## 为什么必须有这个脚本（R45-3 的 P1-1 实测）
 * `skill/list` 这个请求从前端发出去后，宿主 `MESSAGE_METHODS[msg.type]` 取不到
 * （表里写的是驼峰 `skillList`）→ `if (!method) return;` **静默丢弃** →
 * `/skill` 的参数补全候选永远为空。用户看到的是「敲 /skill 空格没反应」，
 * 而代码里两处都「看起来写了」。
 *
 * 这类缺陷的共同点：**两处各写一份名字，迟早分叉，而且坏得很安静**。
 * 所以判据不是「我记得改过」，而是「把前端所有 post 类型抽出来，逐个在宿主里
 * 找到接住它的分支」——差集必须为空。
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');
const EXT = fs.readFileSync(path.join(ROOT, 'src', 'extension.js'), 'utf8');

const fails = [];
const ck = (n, ok, d) => {
  console.log((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  if (!ok) fails.push(n);
};

/* ---- 1. 抽出前端所有 post 类型 ----
   两种写法都要认：
     post('xxx', {...})
     atchSend('attach/xxx', {...}, cb)   → 内部就是 post(type)
   还有变量形式 post(type, ...)，那种从调用处传进去，单独收集。 */
const types = new Set();
let m;
const rePost = /\bpost\(\s*'([A-Za-z0-9_\/]+)'/g;
while ((m = rePost.exec(JS))) types.add(m[1]);
const reAtch = /\batchSend\(\s*'([A-Za-z0-9_\/]+)'/g;
while ((m = reAtch.exec(JS))) types.add(m[1]);

const list = [...types].sort();
console.log('DIAG 前端 post 类型共 ' + list.length + ' 种：' + list.join(', '));
ck('S1 能抽出前端 post 类型（脚本自身有效）', list.length >= 20, '抽到 ' + list.length + ' 种');

/* ---- 2. 宿主侧：MESSAGE_METHODS 的键 + 各独立分支里判定的 type ---- */
const msgMethods = new Set();
const mmBlock = /const MESSAGE_METHODS = \{([\s\S]*?)\n\};/.exec(EXT);
if (mmBlock) {
  const reKey = /^\s*'?([A-Za-z0-9_\/]+)'?\s*:/gm;
  let k;
  while ((k = reKey.exec(mmBlock[1]))) msgMethods.add(k[1]);
}
console.log('DIAG MESSAGE_METHODS 键 ' + msgMethods.size + ' 个：' + [...msgMethods].sort().join(', '));
ck('S2 能抽出 MESSAGE_METHODS 的键', msgMethods.size >= 15, '抽到 ' + msgMethods.size + ' 个');

/* 独立分支：msg.type === 'xxx' / type === 'xxx' / isSkillListType(type) */
const standalone = new Set();
const reBranch = /(?:msg\.)?type\s*===\s*'([A-Za-z0-9_\/]+)'/g;
while ((m = reBranch.exec(EXT))) standalone.add(m[1]);
/* 走辅助函数的类型：把函数认的拼写也算作已接住 */
if (/function isSkillListType[\s\S]{0,200}skillList/.test(EXT)) {
  standalone.add('skillList');
  standalone.add('skill/list');
}
console.log('DIAG 宿主独立分支判定的 type：' + [...standalone].sort().join(', '));

/* ---- 3. 逐类型核对：前端发的每一种，宿主都要接住 ---- */
const missing = [];
list.forEach((t) => {
  if (msgMethods.has(t) || standalone.has(t)) return;
  missing.push(t);
});
console.log('');
console.log('=== 逐类型核对 ===');
list.forEach((t) => {
  const via = msgMethods.has(t) ? 'MESSAGE_METHODS'
    : (standalone.has(t) ? '独立分支' : '✗ 没有接住');
  console.log('  ' + (via === '✗ 没有接住' ? '✗ ' : '✓ ') + t.padEnd(18) + via);
});
console.log('');
ck('S3 前端每一种 post 类型都能被宿主接住（差集为空）',
  missing.length === 0, missing.length ? '没有接住的：' + missing.join(', ') : '差集为空');

/* ---- 4. skill/list 专项：三个断点都要兼容 ----
   ① MESSAGE_METHODS 有键  ② 失败回执判定  ③ 成功回执判定
   只要有一处只认一种拼写，请求就发得出去但回执回不来。 */
const mm = /const MESSAGE_METHODS = \{([\s\S]*?)\n\};/.exec(EXT);
const mmBody = mm ? mm[1] : '';
const bothKeys = /'skill\/list':\s*'skill\/list'/.test(mmBody) && /skillList:\s*'skill\/list'/.test(mmBody);
ck('S4 skill/list：MESSAGE_METHODS 里两种拼写都有键', bothKeys,
  'skillList=' + /skillList:\s*'skill\/list'/.test(mmBody) +
  ' skill/list=' + /'skill\/list':\s*'skill\/list'/.test(mmBody));
/* 判据要在**函数体之外**找裸比较：isSkillListType 内部当然要写
   `type === 'skillList'`，那是实现本身。上一版的正则把实现也算进
   「还有裸比较」，于是恒红（假失败）。 */
const isSkillBody = /function isSkillListType\([^)]*\)\s*\{[\s\S]*?\n\}/.exec(EXT);
const outsideHelper = isSkillBody
  ? EXT.slice(0, isSkillBody.index) + EXT.slice(isSkillBody.index + isSkillBody[0].length)
  : EXT;
const nakedCompare = /(?:msg\.)?type\s*===\s*'skillList'/.test(outsideHelper);
ck('S5 skill/list：两处回执判定都走 isSkillListType（函数体外没有裸比较）',
  /function isSkillListType/.test(EXT) && !nakedCompare,
  '有函数=' + /function isSkillListType/.test(EXT) + ' 函数体外裸比较=' + nakedCompare);
ck('S6 skill/list：RECEIPT_TYPES 两种拼写都登记',
  /RECEIPT_TYPES = \{[\s\S]*?skillList: 1[\s\S]*?'skill\/list': 1[\s\S]*?\}/.test(EXT));

/* ---- 5. 反向：宿主注册的后端方法是否存在（防止前端能收、后端没有） ---- */
const methods = new Set();
const reM = /'([a-z]+\/[a-z]+)'/g;
while ((m = reM.exec(mmBody))) methods.add(m[1]);
const server = fs.readFileSync(path.join(ROOT, '..', 'narnat_agent', 'rpc', 'server.py'), 'utf8');
const backendMissing = [];
methods.forEach((meth) => {
  // 后端方法名形如 _m_chat_send / chat/send 出现在 map 里，两种都查
  const snake = '_m_' + meth.replace(/\//g, '_');
  if (server.indexOf(snake) < 0 && server.indexOf("'" + meth + "'") < 0 &&
      server.indexOf('"' + meth + '"') < 0) {
    backendMissing.push(meth);
  }
});
console.log('');
ck('S7 宿主转发的后端方法在 server.py 里都存在', backendMissing.length === 0,
  backendMissing.length ? '找不到：' + backendMissing.join(', ') : '全部命中');

console.log('\nSUMMARY total=' + (fails.length ? '有失败' : 'ok') + ' fail=' + fails.length);
process.exit(fails.length ? 1 : 0);
