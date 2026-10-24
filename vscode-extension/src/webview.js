'use strict';

const crypto = require('crypto');
const vscode = require('vscode');

const BODY = require('../media/narnat.body.js');

function getNonce() {
  return crypto.randomBytes(16).toString('base64').replace(/[^A-Za-z0-9]/g, '');
}

/**
 * 生成 webview 与 preview.html 共用的 HTML。
 * 骨架来自 media/narnat.body.js，样式与交互来自 media/narnat.css / narnat.js，
 * 三处同一份真源，预览页与 VSCode 内渲染完全一致。
 * @param {import('vscode').Webview} webview
 * @param {import('vscode').Uri} extensionUri 扩展根目录
 * @returns {string}
 */
function getHtml(webview, extensionUri) {
  const nonce = getNonce();
  const cssUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'narnat.css')
  );
  const jsUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'narnat.js')
  );
  // KaTeX（TeX → 公式）的第三方产物，与 dsh 用的是同一个库。
  // 单独放 media/katex/，不进 design/ui-full.html 的真源 ——
  // 那是**别人写的** 25KB CSS + 273KB JS，混进界面真源会把它淹掉，
  // 也没法随版本升级只换这一个目录。
  const katexCssUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'katex', 'katex.min.css')
  );
  const katexJsUri = webview.asWebviewUri(
    vscode.Uri.joinPath(extensionUri, 'media', 'katex', 'katex.min.js')
  );

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- font-src 是 KaTeX 必须的：它的 CSS 里 @font-face 引 woff2 数学字体，
     缺这一项 webview 会拒载字体，公式会退化成乱码字形（比不渲染更糟）。 -->
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'nonce-${nonce}'; script-src 'nonce-${nonce}'; img-src ${webview.cspSource} data:; font-src ${webview.cspSource};">
<title>Narnat</title>
<link rel="stylesheet" href="${cssUri}">
<link rel="stylesheet" href="${katexCssUri}">
<!-- 防闪烁（FOUC）：webview 里样式表走 vscode-resource 协议异步取，
     取回来之前浏览器会先把整页 HTML 当纯文本画出来 —— 用户看到「满屏文字闪一下」。
     这里先把 html 藏起来，等样式表就绪后再显形。
     显形由下面的行内脚本完成：经典脚本会等前面的样式表加载完才执行，
     所以那一刻样式一定已经生效。 -->
<style nonce="${nonce}">
  html{visibility:hidden}
  html.narnat-ready{visibility:visible}
</style>
<!-- 脚本被禁用时不能永久隐藏（那等于白屏，比闪烁更糟），这里解掉 -->
<noscript><style>html{visibility:visible}</style></noscript>
</head>
<body data-run="false">
${BODY}
<script nonce="${nonce}">document.documentElement.classList.add('narnat-ready');</script>
<script nonce="${nonce}" src="${katexJsUri}"></script>
<script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
}

module.exports = { getHtml, getNonce };
