/**
 * 诊断：为什么 verify-interrupt-ask 里 initialize 没生效（data-offline 仍是 null）。
 * 只观察不修改：打印 window.onerror、askDlg 是否存在、监听是否挂上。
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

const TEST = String.raw`
(function(){
  var errs = [];
  window.addEventListener('error', function(e){
    errs.push('onerror: ' + (e.message || '') + ' @' + (e.lineno || '?'));
  });
  window.addEventListener('load', function(){
    setTimeout(function(){
      var out = [];
      out.push('askDlg 存在=' + (!!document.getElementById('askDlg')));
      out.push('askOk 存在=' + (!!document.getElementById('askOk')));
      out.push('body.innerHTML 长度=' + document.body.innerHTML.length);
      out.push('innerHTML 里 askDlg 位置=' + document.body.innerHTML.indexOf('askDlg'));
      out.push('--- 位置附近 ---');
      out.push(JSON.stringify(document.body.innerHTML.slice(3980, 4400)));
      out.push('querySelector(.askdlg)=' + (!!document.querySelector('.askdlg')));
      out.push('mdFull 存在=' + (!!document.getElementById('mdFull')));
      out.push('composer 存在=' + (!!document.querySelector('.composer')));
      out.push('onerror=' + JSON.stringify(errs));
      out.push('body data-run(初始)=' + document.body.getAttribute('data-run'));
      out.push('body data-offline(初始)=' + document.body.getAttribute('data-offline'));
      // 试发 initialize
      window.dispatchEvent(new MessageEvent('message', { data: {
        type: 'initialize', params: { version: 't', protocol: 1,
          model: { current: 'm', options: ['m'] },
          thinking: { effort: 'high', options: { high: '高' } },
          goal: { enabled: false, maxRounds: 10 }, plugins: [],
          session: { current: '甲', messages: [] }, workdir: '<t>' } } }));
      setTimeout(function(){
        out.push('after initialize: data-offline=' + document.body.getAttribute('data-offline'));
        out.push('after initialize: data-run=' + document.body.getAttribute('data-run'));
        out.push('onerror 累计=' + JSON.stringify(errs));
        var pre = document.createElement('pre');
        pre.id = 'RESULT';
        pre.textContent = out.join('\n');
        document.body.appendChild(pre);
      }, 400);
    }, 300);
  });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>diag-ask</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-diag-ask');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=10000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
