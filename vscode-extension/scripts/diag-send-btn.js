/**
 * 量发送按钮（空输入禁用态）在两套主题下的符号对比度。
 * 症状：浅色主题下按钮变成「一个实心灰圆」，+ 和 ↑ 看不见。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const DARK = `
  --vscode-editor-background:#1f1f1f;
  --vscode-sideBar-background:#181818;
  --vscode-editorWidget-background:#202020;
  --vscode-foreground:#cccccc;
  --vscode-descriptionForeground:#9d9d9d;
  --vscode-button-background:#0078d4;
  --vscode-button-foreground:#ffffff;
  --vscode-input-background:#313131;
  --vscode-panel-border:#2b2b2b;
  --vscode-editorWidget-border:#3c3c3c;
`;
const LIGHT = `
  --vscode-editor-background:#ffffff;
  --vscode-sideBar-background:#f8f8f8;
  --vscode-editorWidget-background:#f8f8f8;
  --vscode-foreground:#3b3b3b;
  --vscode-descriptionForeground:#717171;
  --vscode-button-background:#005fb8;
  --vscode-button-foreground:#ffffff;
  --vscode-input-background:#ffffff;
  --vscode-panel-border:#e5e5e5;
  --vscode-editorWidget-border:#cecece;
`;

const TEST = String.raw`
(function(){
  var out = [];
  function parseColor(s){
    if (!s) return null;
    var m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(s);
    if (m) return [ +m[1], +m[2], +m[3] ];
    m = /^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/.exec(s);
    if (m) return [ m[1]*255, m[2]*255, m[3]*255 ];
    return null;
  }
  function lum(rgb){
    var a = rgb.map(function(v){
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126*a[0] + 0.7152*a[1] + 0.0722*a[2];
  }
  function ratio(fg, bg){
    var l1 = lum(fg), l2 = lum(bg);
    if (l1 < l2) { var t = l1; l1 = l2; l2 = t; }
    return (l1 + 0.05) / (l2 + 0.05);
  }
  window.addEventListener('load', function(){
    var send = document.getElementById('sendBtn');
    var att = document.getElementById('attachBtn');
    var ar = send && send.querySelector('.ar');
    if (!send) { out.push('no send button'); }
    else {
      send.setAttribute('data-empty', 'true');
      var sb = getComputedStyle(send);
      var ab = getComputedStyle(ar || send);
      var bg = parseColor(sb.backgroundColor);
      var fg = parseColor(ab.color);
      out.push('send[empty] bg=' + sb.backgroundColor + ' color=' + ab.color +
        ' ratio=' + (bg && fg ? ratio(fg, bg).toFixed(2) : 'n/a'));
      // 也量一下有字时的亮态，确认没把它弄坏
      send.setAttribute('data-empty', 'false');
      var sb2 = getComputedStyle(send);
      var ab2 = getComputedStyle(ar || send);
      var bg2 = parseColor(sb2.backgroundColor);
      var fg2 = parseColor(ab2.color);
      out.push('send[ready] bg=' + sb2.backgroundColor + ' color=' + ab2.color +
        ' ratio=' + (bg2 && fg2 ? ratio(fg2, bg2).toFixed(2) : 'n/a'));
    }
    if (att) {
      var at = getComputedStyle(att);
      var atb = parseColor(at.backgroundColor);
      var atf = parseColor(at.color);
      out.push('attach bg=' + at.backgroundColor + ' color=' + at.color +
        ' ratio=' + (atb && atf ? ratio(atf, atb).toFixed(2) : 'n/a'));
    }
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\n');
    document.body.appendChild(pre);
  });
})();
`;

function run(vars, tag) {
  const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>m-${tag}</title>
<style>:root{${vars}}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;
  const dir = path.join(os.tmpdir(), 'narnat-measure-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, page, 'utf8');
  const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
  const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=8000', '--dump-dom',
    'file:///' + file.replace(/\\/g, '/')],
  { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
  console.log('== ' + tag + ' ==');
  console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&') : '(无结果)');
}

run(DARK, 'dark');
run(LIGHT, 'light');
