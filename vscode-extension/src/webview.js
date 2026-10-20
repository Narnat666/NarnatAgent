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

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; img-src ${webview.cspSource} data:;">
<title>Narnat</title>
<link rel="stylesheet" href="${cssUri}">
</head>
<body data-run="false">
${BODY}
<script nonce="${nonce}" src="${jsUri}"></script>
</body>
</html>`;
}

module.exports = { getHtml, getNonce };
