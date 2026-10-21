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

// ── 演示数据：只给 preview.html，不进 vsix ──
// 生产骨架（narnat.body.js）里一个演示节点都没有：模板的 #convo 是空的，
// 会话列表来自后端，会话名/状态栏是空或占位符。演示内容全部搬到
// design/demo-convo.html，在这里生成 media/narnat.demo.js。
// .vscodeignore 已排除 media/narnat.demo.js 与 preview.html —— 打包进插件
// 的产物里根本不存在这些演示数据，不是「显示了再擦掉」。
const demoPath = path.join(ROOT, 'design/demo-convo.html');
const demoHtml = fs.existsSync(demoPath) ? toLf(fs.readFileSync(demoPath, 'utf8')).trim() : '';
const demoJs = `'use strict';
// 预览专用演示数据 —— 由 design/demo-convo.html 生成。
// 只被 preview.html 加载；不进 vsix（见 .vscodeignore），生产面板永远看不到它。
var DEMO = {
  convo: ${JSON.stringify(demoHtml)},
  current: 'grep-ignore-case',
  sessions: [
    { name: 'grep-ignore-case', time: '14:32', count: 28,
      children: [
        { name: 'try-regex', status: 'completed', time: '14:20' },
        { name: 'alt-approach', status: 'active', time: '14:25', count: 12 }
      ] },
    { name: 'ui-webview', time: '11:05', count: 64, children: [] },
    { name: 'refactor-tools', time: '10-16', count: 41, children: [] },
    { name: 'theme-tokens', time: '10-15', count: 22, children: [] },
    { name: 'docs-audit', time: '10-14', count: 18, children: [] },
    { name: 'perf-probe', time: '10-12', count: 33, children: [] },
    { name: 'legacy-cleanup', time: '10-09', count: 7, children: [] },
    { name: 'proto-e2e', time: '10-08', count: 5, children: [] }
  ]
};
if (typeof module !== 'undefined' && module.exports) {
  module.exports = DEMO;
} else {
  window.NARNAT_DEMO = DEMO;
}
`;
fs.writeFileSync(path.join(ROOT, 'media/narnat.demo.js'), demoJs, 'utf8');
console.log('demo bytes', demoHtml.length, '(仅 preview.html 使用)');

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
