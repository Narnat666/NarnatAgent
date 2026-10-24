/**
 * 诊断：命令面板的参数补全现状（用户报障：/skill 不补技能、/ls 不补 --all、
 * /plugin 也不补）。
 *
 * 本脚本只**观察**不修改：喂一份标准 initialize（含 plugins / session），
 * 然后在输入框里逐条敲，记录面板里出现的候选。
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
  var out = [];
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function input(){ return document.getElementById('input'); }
  function pal(){ return document.getElementById('palette'); }
  function setVal(v){
    var el = input();
    el.value = v;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function items(){
    return [].slice.call(pal().querySelectorAll('.item')).map(function(it){
      return it.querySelector('.c').textContent;
    });
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash', 'deepseek-v4-pro'] },
      thinking: { effort: 'high', options: { high: '高', medium: '中' } },
      goal: { enabled: false, maxRounds: 10 },
      plugins: [
        { name: 'Terminal', label: '终端', enabled: true },
        { name: 'WebSearch', label: '联网搜索', enabled: false },
        { name: 'Serial', label: '串口', enabled: true },
        { name: 'MCP', label: 'MCP', enabled: true }
      ],
      session: { current: '甲', messages: [] },
      workdir: '<t>' } });
    await sleep(200);
    emit({ type: 'session/list', params: { current: '甲', groups: [
      { label: '今天', sessions: [
        { name: '甲', timestamp: 2, active: true, children: [{ name: '甲1' }] },
        { name: '乙', timestamp: 1 }
      ] }
    ] } });
    await sleep(200);

    var cases = ['/skill', '/skill ', '/ls', '/ls ', '/plugin', '/plugin ',
                 '/rm ', '/mode ', '/thinking ', '/cd '];
    for (var i = 0; i < cases.length; i++) {
      setVal(cases[i]);
      await sleep(150);
      out.push(JSON.stringify(cases[i]) + ' → open=' + pal().getAttribute('data-open') +
        ' items=' + JSON.stringify(items()));
    }
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\n');
    document.body.appendChild(pre);
  }
  window.addEventListener('load', function(){
    run().catch(function(e){
      var pre = document.createElement('pre');
      pre.id = 'ERR';
      pre.textContent = 'run 抛错: ' + (e && e.stack ? e.stack : e);
      document.body.appendChild(pre);
    });
  });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>diag-palette</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-diag-pal');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
