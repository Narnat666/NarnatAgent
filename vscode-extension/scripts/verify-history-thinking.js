/**
 * 验收历史重放的思考显示（用户报障）：
 *
 * 用户原话：「我打开了历史对话记录，只有工具显示，没有思考显示」。
 *
 * 两处根因（后端 `_history_payload` + 本文件所在界面）：
 *   1) 后端只在正文非空时才输出 assistant 条目，`content=None + thinking=…`
 *      的中间步把思考整段丢掉 → 历史里一块思考都没有。
 *   2) 界面给每条 assistant 都建一个 `.msg assistant` 正文气泡，于是「只有
 *      思考、没有正文」的条目（后端现在输出 text=""）会画出空气泡，还会被
 *      计进折叠行的「N 条消息」。
 *
 * 本脚本只喂**后端修复后**的真实 payload 形状，断言界面画得对。
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
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d).replace(/\r?\n/g, '\\n')));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
  }
  function tool(id, name, label, summary, output){
    return { role: 'tool', id: id, name: name, label: label, summary: summary,
      arguments: {}, output: output };
  }

  async function run(){
    boot();
    await sleep(60);

    /* 后端修复后的 payload 形状：
       用户 → [思考][工具] → [思考][工具] → [思考][工具] → 最终回复
       中间步的 text 是空串，只有 thinking。 */
    var HIST = [
      { role: 'user', text: '帮我看看这个插件的历史记录' },
      { role: 'assistant', text: '', thinking: '先看目录结构，确认有哪些文件。' },
      tool('c1', 'Shell', '执行命令', 'dir', 'main.py'),
      { role: 'assistant', text: '', thinking: '再搜一下相关实现，定位渲染入口。' },
      tool('c2', 'Grep', '搜索内容', 'renderHistory', 'ui-full.html:1'),
      { role: 'assistant', text: '', thinking: '读一下这个函数本体。' },
      tool('c3', 'Read', '读取', 'ui-full.html', 'function renderHistory(msgs){'),
      { role: 'assistant', text: '看完了，历史里现在有思考了。', thinking: '可以收尾。' }
    ];
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h', messages: HIST } });
    await sleep(800);

    var thinks = convo().querySelectorAll('.think');
    var procs = convo().querySelectorAll('.turn-proc');
    var asst = convo().querySelectorAll('.msg.assistant');
    var users = convo().querySelectorAll('.msg[data-role="user"]');
    var label = (convo().querySelector('.turn-proc .tp-label') || {}).textContent || '';

    out.push('DIAG 思考块=' + thinks.length + ' 折叠组=' + procs.length +
             ' 正文气泡=' + asst.length + ' 用户气泡=' + users.length +
             ' 折叠行=' + JSON.stringify(label));

    ck('K1 历史里画出思考块（用户报障：一块都没有）', thinks.length >= 1, 'got=' + thinks.length);
    // 3 个中间步 + 最终回复那一步自己的思考 = 4；思考一律归 process 区
    // （与 dsh 一致：reasoning 进折叠区，正文留在组外）。
    ck('K2 每条思考都画出来了（含最终回复那步的思考）', thinks.length === 4, 'got=' + thinks.length);
    ck('K3 思考内容可读（不是空块）',
       thinks.length > 0 && thinks[0].textContent.indexOf('先看目录结构') >= 0,
       thinks.length ? thinks[0].textContent.slice(0, 60) : '(无)');
    ck('K4 思考块默认收起（data-open=false）',
       thinks.length > 0 && thinks[0].getAttribute('data-open') === 'false',
       thinks.length ? thinks[0].getAttribute('data-open') : '(无)');

    ck('K5 不画空气泡正文（只有 1 条正文气泡）', asst.length === 1, 'got=' + asst.length);
    ck('K6 唯一的正文气泡就是最终回复',
       asst.length === 1 && asst[0].textContent.indexOf('历史里现在有思考了') >= 0,
       asst.length ? asst[0].textContent.slice(0, 60) : '(无)');

    ck('K7 整轮只建一个折叠组（工具调度不被劈开）', procs.length === 1, 'got=' + procs.length);
    ck('K8 折叠行工具计数 = 3', /3 次工具调用/.test(label), JSON.stringify(label));
    ck('K9 「N 条消息」不把空正文算进去', label.indexOf('条消息') < 0, JSON.stringify(label));

    var inner = procs.length ? procs[0].querySelector('.tp-body') : null;
    ck('K10 思考块全在折叠组内（展开就能看到）',
       !!inner && inner.querySelectorAll('.think').length === 4,
       inner ? String(inner.querySelectorAll('.think').length) : '(无组)');

    ck('K11 用户气泡只有用户自己发的那一条', users.length === 1, 'got=' + users.length);
    ck('K12 历史里没有「（图片内容见附件）」这种占位气泡',
       convo().textContent.indexOf('（图片内容见附件）') < 0);

    finish();
    function finish(){
      out.push('SUMMARY total=' + out.length + ' fail=' + fails);
      var pre = document.createElement('pre');
      pre.id = 'RESULT';
      pre.textContent = out.join('\n');
      document.body.appendChild(pre);
    }
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-history-thinking</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-histthink');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-history-thinking.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT');
