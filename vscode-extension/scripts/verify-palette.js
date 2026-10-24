/**
 * 验收命令面板的参数补全（用户报障：「用户输入 /skill 不能补全已有的 skill，
 * /ls 不能补全 --all，输入 /plugin 应该也不能补全」）。
 *
 * 实测结论（本轮之前的真实形态）：
 *   /skill  → 敲空格后面板直接关掉，一条候选都没有   ← 缺陷
 *   /ls     → 同上                                   ← 缺陷
 *   /rm     → 只列会话，没有 --all（后端认这个开关）  ← 缺陷
 *   /plugin → 有候选（Terminal/WebSearch/Serial/MCP） ← 本来就是好的
 *
 * 根因是 `ARG_SOURCE` 只登记了 5 个命令：没登记 = 敲完命令名加空格后
 * `refreshPalette` 直接 return，面板关闭。候选来源本身都是现成的
 * （技能由后端 `skill/list` 新下发；`--all` 是固定开关）。
 *
 * 断言：
 *   P1 /skill 空格后弹出候选，且内容与后端下发的技能一致
 *   P2 选中后 Tab/Enter 补全进输入框（补的是技能名，不是命令名）
 *   P3 /ls 空格后有 --all
 *   P4 /rm 空格后既有会话又有 --all
 *   P5 /plugin 空格后仍有插件候选（回归：别被本次改动弄坏）
 *   P6 未登记参数的命令（/compact）敲空格后不弹面板（不该被误改）
 *   P7 技能候选按输入前缀过滤（敲 /skill 代码 只留代码审查）
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
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function input(){ return document.getElementById('input'); }
  function pal(){ return document.getElementById('palette'); }
  function setVal(v){
    input().value = v;
    input().dispatchEvent(new Event('input', { bubbles: true }));
  }
  function codeItems(){
    return [].slice.call(pal().querySelectorAll('.item')).map(function(it){
      return it.querySelector('.c').textContent;
    });
  }
  function palOpen(){ return pal().getAttribute('data-open') === 'true'; }
  function enter(){
    var e = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    input().dispatchEvent(e);
    return e.defaultPrevented;
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
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
        { name: '甲', timestamp: 2, children: [{ name: '甲1' }] },
        { name: '乙', timestamp: 1 }
      ] }
    ] } });
    await sleep(200);
    /* 后端 skill/list 的回执（本脚本不跑真后端，喂等价的回执） */
    emit({ type: 'skill/result', params: { items: [
      { v: '代码审查', d: '系统技能', dir: false },
      { v: '写作规范', d: '系统技能', dir: false },
      { v: 'docs/api.md', d: '项目技能', dir: false }
    ] } });
    await sleep(100);

    /* ---- P1/P2 /skill ---- */
    setVal('/skill ');
    await sleep(200);
    var it = codeItems();
    out.push('DIAG /skill 候选=' + JSON.stringify(it));
    ck('P1 /skill 空格后弹出候选', palOpen() && it.length === 3, 'open=' + palOpen() + ' n=' + it.length);
    ck('P1b 候选内容就是后端下发的技能（含项目技能层级路径）',
       it.indexOf('代码审查') >= 0 && it.indexOf('docs/api.md') >= 0, JSON.stringify(it));
    var prevented = enter();
    out.push('DIAG 补全后输入框=' + JSON.stringify(input().value));
    ck('P2 回车补全成技能名（不是命令名，末尾留空格便于接着打参数）',
       input().value === '/skill 代码审查 ', JSON.stringify(input().value));
    ck('P2b 补全这一步消费了回车（不触发发送）', prevented);

    /* ---- P7 前缀过滤 ---- */
    setVal('/skill 代');
    await sleep(200);
    var f = codeItems();
    out.push('DIAG /skill 代 候选=' + JSON.stringify(f));
    ck('P7 按前缀过滤（只剩代码审查）', palOpen() && f.length === 1 && f[0] === '代码审查',
       JSON.stringify(f));

    /* ---- P3 /ls ---- */
    setVal('/ls ');
    await sleep(200);
    var ls = codeItems();
    out.push('DIAG /ls 候选=' + JSON.stringify(ls));
    ck('P3 /ls 空格后有 --all', palOpen() && ls.indexOf('--all') >= 0, JSON.stringify(ls));

    /* ---- P4 /rm ---- */
    setVal('/rm ');
    await sleep(200);
    var rm = codeItems();
    out.push('DIAG /rm 候选=' + JSON.stringify(rm));
    ck('P4 /rm 有会话候选', palOpen() && rm.indexOf('甲') >= 0, JSON.stringify(rm));
    ck('P4b /rm 也有 --all（后端 delete_session 认这个开关）',
       rm.indexOf('--all') >= 0, JSON.stringify(rm));

    /* ---- P5 /plugin 回归 ---- */
    setVal('/plugin ');
    await sleep(200);
    var pl = codeItems();
    out.push('DIAG /plugin 候选=' + JSON.stringify(pl));
    ck('P5 /plugin 仍有插件候选（未被本次改动弄坏）',
       palOpen() && pl.length === 4 && pl.indexOf('Terminal') >= 0, JSON.stringify(pl));

    /* ---- P6 没参数可补的命令不弹面板 ---- */
    setVal('/compact ');
    await sleep(200);
    ck('P6 没有参数候选的命令（/compact）不弹面板', !palOpen(),
       'open=' + palOpen() + ' items=' + JSON.stringify(codeItems()));

    /* ---- P8 目录式技能也能补全成"往下走"的形式 ---- */
    setVal('/skill docs');
    await sleep(200);
    var d = codeItems();
    ck('P8 层级路径可按前缀补全（/skill docs → docs/api.md）',
       palOpen() && d.length === 1 && d[0] === 'docs/api.md', JSON.stringify(d));

    out.push('SUMMARY total=' + out.length + ' fail=' + fails);
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-palette</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-pal');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=18000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
