const fs = require('fs');
const path = require('path');

// 相对脚本自身定位，不写死绝对路径：整个仓库可能被移动、复制到别的盘，
// 写死之后脚本会去改「原来那个位置」的文件 —— 移动后要么报不存在，
// 要么更糟：另一个副本被悄悄覆盖，当前仓库反而没更新。
// 同目录的 sync-ext.js / package-vsix.js / install-vsix.js 都是这个写法。
const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');

// 换行符一律用 \r?\n：Windows 上 core.autocrlf=true 会把工作区文件转成 CRLF，
// 只写 \n 的正则会突然全部失配（曾导致 sync 直接报「提取失败」）。
const mStyle = /<style>([\s\S]*?)<\/style>/.exec(html);
const mScript = /<script>\r?\n([\s\S]*?)\r?\n<\/script>\s*<\/body>/.exec(html);
const mBody = /<body[^>]*>([\s\S]*?)<script>\r?\n/.exec(html);

if (!mStyle || !mScript || !mBody) {
  console.error('提取失败', !!mStyle, !!mScript, !!mBody);
  process.exit(1);
}

// 统一成 LF 再写盘：产物换行符不该随工作区检出方式变化，
// 否则同一份源码在不同机器上生成的 narnat.js 会有全文件 diff。
const toLf = (s) => s.replace(/\r\n/g, '\n');
const css = toLf(mStyle[1]).trim() + '\n';
const js = toLf(mScript[1]).trim() + '\n';
const body = toLf(mBody[1]).trim();

fs.writeFileSync(path.join(ROOT, 'media/narnat.css'), css, 'utf8');
fs.writeFileSync(path.join(ROOT, 'media/narnat.js'), js, 'utf8');

const bodyJs = `'use strict';
// 由 design/ui-full.html 生成的界面骨架 —— 单一真源，webview 与 preview.html 共用。
// 修改界面请改 design/ui-full.html 后重新执行 scripts/sync-ui.js，勿直接编辑本文件。
var BODY = ${JSON.stringify(body)};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = BODY;
} else {
  window.NARNAT_BODY = BODY;
}
`;
fs.writeFileSync(path.join(ROOT, 'media/narnat.body.js'), bodyJs, 'utf8');

console.log('css lines', css.split('\n').length);
console.log('js lines', js.split('\n').length);
console.log('body bytes', body.length);
console.log('uses vscode theme vars', css.indexOf('--vscode-editor-background') > 0);
console.log('scrollbar uses vscode slider', css.indexOf('--vscode-scrollbarSlider-background') > 0);

// 防事故：RPC 渲染层必须写在 design/ui-full.html 里，否则会被本脚本覆盖掉。
// 曾经因为它写在 media/ 下，一次 sync 就把 592 行真实事件渲染冲没了。
const hasRpc = js.indexOf("case 'stream/delta'") > 0;
console.log('js has rpc handler', hasRpc);
if (!hasRpc) {
  console.error('[警告] design/ui-full.html 里没有 RPC 事件处理（case \'stream/delta\'）。');
  console.error('       生成的 media/narnat.js 将只能演示模拟数据，真实后端事件不会渲染。');
  process.exitCode = 2;
}
