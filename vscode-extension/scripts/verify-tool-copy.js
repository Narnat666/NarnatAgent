/**
 * 验收「复制按钮按 dsh 口径收窄」（用户要求）：
 *
 * 用户原话：「所有复制按钮，你看看 dsh 是否有，如果 dsh 这个地方没给复制按钮
 * 我们也不给，ai 输出的代码和命令，是要给复制按钮的」。
 *
 * dsh 事实（`D:\deepseek-harness\packages\client\ui-primitives\src\`）：
 *   有复制控件：CodeBlock（markdown 代码块）、ReadBlock、TerminalBlock、
 *               DiffBlock、SearchBlock、以及 user/assistant 整条消息的图标复制。
 *   没有：      普通工具的 ToolRow `.ioCard`（IN/OUT 纯文本段）、WebBlock、
 *               MCP / Serial 这类无专用卡片的工具、思考块（ReasoningRow）。
 *
 * 本脚本断言 narnat 侧的对齐结果。
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

/** 三反引号围栏：直接写在模板里会截断脚本本身，用插值拼 */
const FENCE = '```';

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
  /** 开一轮 + 跑一个工具（实时路径），返回该工具行所在 .tools 里的 .io-copy 数量 */
  async function runTool(seq, name, label, argsText, output){
    emit({ type: 'turn/begin', params: { round: seq } });
    await sleep(15);
    emit({ type: 'tool/call', params: { id: 'x' + seq, name: name, label: label,
      summary: argsText, arguments: {} } });
    await sleep(15);
    emit({ type: 'tool/result', params: { id: 'x' + seq, ok: true, durationMs: 3, output: output } });
    await sleep(40);
    var wraps = convo().querySelectorAll('.tools');
    var last = wraps[wraps.length - 1];
    return last ? last.querySelectorAll('.io-copy').length : -1;
  }

  async function run(){
    boot();
    await sleep(60);

    /* ===== dsh 有复制控件的四类工具：必须仍然给 ===== */
    var readN = await runTool(1, 'Read', '读取', 'a.md', '第一行\n第二行');
    out.push('DIAG Read 复制按钮数=' + readN);
    ck('R1 Read（dsh ReadBlock 有复制）保留复制按钮', readN >= 1, String(readN));
    ck('R2 Read 的 IN 与 OUT 两段各一个', readN === 2, String(readN));

    var shellN = await runTool(2, 'Shell', '执行命令', 'echo hi', 'hi');
    out.push('DIAG Shell 复制按钮数=' + shellN);
    ck('R3 Shell（dsh TerminalBlock 有复制）保留复制按钮', shellN >= 1, String(shellN));

    var grepN = await runTool(3, 'Grep', '搜索内容', 'foo', 'a.md:1:foo');
    ck('R4 Grep（dsh SearchBlock 有复制）保留复制按钮', grepN >= 1, String(grepN));

    var globN = await runTool(4, 'Glob', '搜索文件', '*.md', 'a.md');
    ck('R5 Glob（dsh SearchBlock 有复制）保留复制按钮', globN >= 1, String(globN));

    var editN = await runTool(5, 'Edit', '编辑', 'a.md', '[已编辑: a.md]');
    ck('R6 Edit（dsh DiffBlock 有复制）保留复制按钮', editN >= 1, String(editN));

    var writeN = await runTool(6, 'Write', '写入', 'a.md', '[已写入: a.md (10字节)]');
    ck('R7 Write（dsh DiffBlock 有复制）保留复制按钮', writeN >= 1, String(writeN));

    /* ===== dsh 没有复制控件的：一律不给 ===== */
    var webN = await runTool(7, 'WebSearch', '联网搜索', 'narnat', '结果 1\n结果 2');
    out.push('DIAG WebSearch 复制按钮数=' + webN);
    ck('N1 WebSearch（dsh WebBlock 无复制）不给复制按钮', webN === 0, String(webN));

    var serialN = await runTool(8, 'Serial', '串口', 'COM3', 'boot ok');
    ck('N2 Serial（dsh 无对应卡片）不给复制按钮', serialN === 0, String(serialN));

    var mcpN = await runTool(9, 'mcp__fs__read', 'mcp__fs__read', 'x', 'ok');
    ck('N3 mcp__* 动态工具（dsh 无对应卡片）不给复制按钮', mcpN === 0, String(mcpN));

    var unknownN = await runTool(10, 'WeirdTool', 'WeirdTool', 'x', 'ok');
    ck('N4 未知工具不给复制按钮', unknownN === 0, String(unknownN));

    /* ===== 思考块：dsh 的 ReasoningRow 没有复制 ===== */
    emit({ type: 'turn/begin', params: { round: 11 } });
    await sleep(15);
    emit({ type: 'thinking/delta', params: { text: '想一想' } });
    await sleep(15);
    emit({ type: 'thinking/end', params: {} });
    await sleep(60);
    var think = convo().querySelector('.think');
    ck('T1 思考块存在', !!think);
    ck('T2 思考块没有复制按钮（dsh ReasoningRow 也没有）',
       !!think && think.querySelectorAll('button').length === 0,
       think ? String(think.querySelectorAll('button').length) : '(无)');

    /* ===== markdown 代码块仍给复制（dsh CodeBlock 有） ===== */
    convo().innerHTML = '';
    emit({ type: 'stream/delta', params: { text: '看这段代码：\n\n${FENCE}js\nvar x = 1;\n${FENCE}\n\n完。' } });
    await sleep(120);
    var codeCopy = convo().querySelectorAll('.code-copy');
    ck('C1 markdown 代码块仍有复制按钮（dsh CodeBlock 有）', codeCopy.length >= 1,
       String(codeCopy.length));

    /* ===== 历史路径同口径 ===== */
    convo().innerHTML = '';
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: 'h',
      messages: [
        { role: 'user', text: '跑一下' },
        { role: 'assistant', text: '' },
        { role: 'tool', id: 'h1', name: 'Read', label: '读取', summary: 'a.md',
          arguments: {}, output: '内容' },
        { role: 'tool', id: 'h2', name: 'WebSearch', label: '联网搜索', summary: 'q',
          arguments: {}, output: '搜索结果' },
        { role: 'tool', id: 'h3', name: 'Serial', label: '串口', summary: 'COM3',
          arguments: {}, output: 'boot ok' },
        { role: 'assistant', text: '完成。' }
      ] } });
    await sleep(700);
    var histRead = convo().querySelectorAll('.tools .io-copy').length;
    var toolsTotal = convo().querySelectorAll('.tools').length;
    out.push('DIAG 历史 .io-copy 总数=' + histRead + '（3 个工具行，只有 Read 该给）');
    ck('H1 历史路径：3 个工具行都建出来了', toolsTotal === 3, String(toolsTotal));
    ck('H2 历史路径：只有 Read 那一行有复制按钮（2 个：IN+OUT）', histRead === 2, String(histRead));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-tool-copy</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-toolcopy');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-tool-copy.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT');
