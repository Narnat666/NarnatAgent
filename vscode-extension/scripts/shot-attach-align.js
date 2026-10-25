/** 出图：窄面板下「三个附件 + 文字」的对齐（验证 P1-4 修复）。 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = 'E:\\桌面\\narnat\\NarnatAgent\\vscode-extension';
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media/narnat.js'), 'utf8');

const LIGHT = `
:root{--vscode-editor-background:#ffffff;--vscode-editor-foreground:#3b3b3b;
--vscode-sideBar-background:#ffffff;--vscode-editorWidget-background:#f8f8f8;
--vscode-list-hoverBackground:#e8e8e8;--vscode-panel-border:#c8c8c8;
--vscode-textLink-foreground:#006ab1;--vscode-charts-green:#388a34;
--vscode-editorWarning-foreground:#bf8803;--vscode-errorForeground:#e51400;
--vscode-chat-requestBackground:#f2f4f7;--vscode-button-secondaryBackground:#5f6a79;}`;

const TEST = String.raw`
(function(){
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  async function run(){
    emit({ type:'initialize', params:{ version:'t', protocol:1,
      model:{current:'DeepSeek V4',options:['DeepSeek V4']},
      thinking:{effort:'high',options:{high:'高'}},
      goal:{enabled:false,maxRounds:10}, plugins:[], session:{current:'对齐核对'}, workdir:'E:\\p' } });
    await sleep(200);
    var MARK = window.__MARK;
    emit({ type:'session/result', params:{ action:'sessionSwitch', current:'s1',
      messages: [
        { role:'user', text:'看这三个文件\n\n' + MARK +
          '\n- 文件：E:\\p\\alpha.py\n- 文件：E:\\p\\beta.md\n- 文件：E:\\p\\gamma.json' },
        { role:'assistant', text:'收到，我逐个读一下。' }
      ] } });
    await sleep(800);
    // 提交 checked 回执，让文件卡拿到字节数
    var ids = [];
    (window.__sent||[]).forEach(function(m){ if (m && m.type === 'attach/check') ids.push(m.params.id); });
    emit({ type:'attach/checked', params:{ id: ids[ids.length-1] || 1, items:[
      { path:'E:\\p\\alpha.py', exists:true, kind:'file', bytes:4301 },
      { path:'E:\\p\\beta.md', exists:true, kind:'file', bytes:12800 },
      { path:'E:\\p\\gamma.json', exists:true, kind:'file', bytes:512 }
    ] } });
    await sleep(500);
    var pre = document.createElement('pre'); pre.id='RESULT';
    pre.textContent = 'ok';
    document.body.appendChild(pre);
  }
  window.addEventListener('load', function(){
    run().catch(function(e){
      var p = document.createElement('pre'); p.id='ERR'; p.textContent=''+e; document.body.appendChild(p);
    });
  });
})();
`;

const MARK = '[用户附带了以下本地路径，请用相应工具自行读取处理]';
const page = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>align</title>
<style>${LIGHT}</style><style>${CSS}
body{background:var(--vscode-editor-background);}
</style></head><body data-real="true" data-offline="false" data-run="false" data-theme="light">${BODY}
<script>window.__sent=[];window.__MARK=${JSON.stringify(MARK)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const out = path.join(ROOT, 'scripts', 'shots');
fs.mkdirSync(out, { recursive: true });
const tmp = path.join(os.tmpdir(), 'shot-attach-align');
fs.mkdirSync(tmp, { recursive: true });
const file = path.join(tmp, 'a.html');
fs.writeFileSync(file, page, 'utf8');
const png = path.join(out, 'attach-align-narrow.png');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
  '--user-data-dir=' + path.join(tmp, 'p'), '--virtual-time-budget=20000',
  '--screenshot=' + png, '--window-size=400,760', 'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
console.log('SHOT ' + png + '  ' + fs.statSync(png).size + ' bytes');
