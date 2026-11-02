/**
 * 验收「会话级插件锁定 + 审核模式改名/锁定态」（任务 B 的前端契约）。
 *
 * 后端新契约（`initialize` / `config/set` 的回执形状）：
 *   goal:    { enabled, maxRounds, locked }
 *   plugins: [{ name, label, enabled }]  ← 5 项（Terminal/WebSearch/Serial/MCP/Goal），
 *                                          项数与顺序由后端给，前端不许写死
 *   pluginsLocked: bool                  ← 本会话是否已开始工作（配置锁定）
 *
 * 断言（A 未锁定 / B 锁定 / C 解锁回退 / D 回执驱动）：
 *   A  未锁定时：5 个条目、按钮「插件 1/5」、无 data-lock、无说明行，
 *      且点条目**真的**发 config/set（反向回归：别把正常通路一起禁掉）
 *   A7 失败回执：界面退回快照（含 data-lock 回到未锁），且后端 RpcError 原文可见
 *   B  锁定时：#popPlugin[data-lock] / #btnPlugin[data-lock]、唯一说明行含「不可更改」、
 *      点插件与点审核模式条目都不再发 config/set、#goalRounds 被禁用
 *   B8 turn/begin 的 goal 不带 locked → 已有锁定态**保持**（不清成未锁）
 *   C  goal.locked=false → data-lock 与说明行都撤掉
 *   D  config/set 成功回执的 current 驱动界面（goal 的 current.locked 也照它落地）
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
  function $(sel){ return document.querySelector(sel); }
  function txt(id){
    var e = document.getElementById(id);
    return e ? (e.textContent || '').trim() : null;
  }
  function cfgSets(){
    return (window.__sent || []).filter(function(m){ return m && m.type === 'configSet'; });
  }
  function note(sel){
    var n = document.querySelector(sel + ' .plock');
    return n ? (n.textContent || '') : '';
  }
  function noteCount(sel){ return document.querySelectorAll(sel + ' .plock').length; }
  function plugNames(){
    return [].slice.call(document.querySelectorAll('#popPlugin .pi')).map(function(pi){
      return pi.getAttribute('data-v');
    });
  }
  /* 后端下发的 5 个插件（顺序即插入顺序；enabled 是**当前会话**的生效值） */
  function plugins(goalOn){
    return [
      { name: 'Terminal', label: '多终端持久 SSH', enabled: false },
      { name: 'WebSearch', label: '联网搜索', enabled: true },
      { name: 'Serial', label: '多终端持久串口', enabled: false },
      { name: 'MCP', label: 'MCP 服务器管理', enabled: false },
      { name: 'Goal', label: '目标模式', enabled: !!goalOn }
    ];
  }
  function init(params){
    emit({ type: 'initialize', params: Object.assign({
      version: 't', protocol: 1,
      model: { current: 'm1', options: ['m1'] },
      thinking: { effort: 'high', options: { high: '高' } },
      session: { current: '甲', messages: [] }
    }, params) });
  }

  async function run(){
    /* ================= A. 未锁定 ================= */
    init({
      goal: { enabled: false, maxRounds: 10, locked: false },
      plugins: plugins(false),
      pluginsLocked: false
    });
    await sleep(250);

    var names = plugNames();
    out.push('DIAG 插件条目=' + JSON.stringify(names));
    ck('A1 后端给 5 项就渲染 5 项（不写死 4）',
      names.length === 5 && names.indexOf('Goal') >= 0, JSON.stringify(names));
    ck('A2 状态栏按项数算「插件 1/5」', txt('btnPlugin') === '插件 1/5',
      JSON.stringify(txt('btnPlugin')));
    ck('A3 未锁定：#popPlugin / #btnPlugin 都没有 data-lock',
      $('#popPlugin').getAttribute('data-lock') === null &&
      $('#btnPlugin').getAttribute('data-lock') === null,
      'pop=' + $('#popPlugin').getAttribute('data-lock') +
      ' btn=' + $('#btnPlugin').getAttribute('data-lock'));
    ck('A4 未锁定：没有锁定说明行', noteCount('#popPlugin') === 0,
      'plock=' + noteCount('#popPlugin'));
    ck('A5 审核模式按钮用新文案「审核模式 关」', txt('btnGoal') === '审核模式 关',
      JSON.stringify(txt('btnGoal')));

    /* ---- A6 反向回归：未锁定时点条目必须发 config/set ---- */
    var n0 = cfgSets().length;
    $('#popPlugin .pi').click();
    await sleep(80);
    var sent1 = cfgSets();
    var last1 = sent1[sent1.length - 1] || {};
    ck('A6 未锁定时点插件条目真的发 config/set（正常通路没被一起禁掉）',
      sent1.length - n0 === 1 && last1.params && last1.params.key === 'plugin' &&
      last1.params.value && last1.params.value.name === 'Terminal',
      '增量=' + (sent1.length - n0) + ' params=' + JSON.stringify(last1.params));

    /* ---- A7 成功回执用 current 刷新界面 ---- */
    emit({ type: 'config/result', params: { ok: true,
      current: { name: 'Terminal', enabled: true } } });
    await sleep(120);
    ck('A7 config/set 成功回执后按钮按后端真值变成「插件 2/5」',
      txt('btnPlugin') === '插件 2/5', JSON.stringify(txt('btnPlugin')));

    /* ---- A8 失败回执：界面退回快照 + 后端原文可见 ---- */
    n0 = cfgSets().length;
    $('#popPlugin .pi[data-v="Terminal"]').click();
    await sleep(60);
    emit({ type: 'config/result', params: { ok: false,
      message: '本会话已开始工作，插件配置不可更改（新建对话后可调整）' } });
    await sleep(120);
    ck('A8 失败回执把插件显示退回快照（Terminal 仍是开）',
      cfgSets().length - n0 === 1 && txt('btnPlugin') === '插件 2/5' &&
      $('#popPlugin .pi[data-v="Terminal"]').getAttribute('data-on') === 'true',
      'configSet 增量=' + (cfgSets().length - n0) +
      ' btn=' + JSON.stringify(txt('btnPlugin')) +
      ' terminal=' + $('#popPlugin .pi[data-v="Terminal"]').getAttribute('data-on'));
    ck('A9 失败原因原样到达 webview（RpcError.message 未被包装）',
      document.getElementById('convo').textContent.indexOf('插件配置不可更改') >= 0,
      JSON.stringify((document.getElementById('convo').textContent || '').slice(-120)));

    /* ---- A10 关掉 Goal 插件 → 审核模式跟着变灰只读 ----
       后端 config/set plugin 的回执带完整快照（plugins/pluginsLocked/goal），
       只回 {name,enabled} 的话界面拿不到新的 goal.locked，审核模式不会变灰。 */
    ck('A10 前置：关 Goal 之前审核模式可配置（Goal 开着）',
      $('#popGoal').getAttribute('data-lock') === null &&
      noteCount('#popGoal') === 0,
      'lock=' + $('#popGoal').getAttribute('data-lock'));

    $('#popPlugin .pi[data-v="Goal"]').click();
    await sleep(60);
    var goalOffList = plugins(false);
    emit({ type: 'config/result', params: {
      ok: true,
      current: { name: 'Goal', enabled: false },
      plugins: goalOffList, pluginsLocked: false,
      goal: { enabled: false, maxRounds: 10, locked: true }
    } });
    await sleep(200);
    ck('A11 关掉 Goal 后 #popGoal 变灰只读',
      $('#popGoal').getAttribute('data-lock') === 'true' &&
      $('#btnGoal').getAttribute('data-lock') === 'true',
      'pop=' + $('#popGoal').getAttribute('data-lock') +
      ' btn=' + $('#btnGoal').getAttribute('data-lock'));
    ck('A12 关掉 Goal 后说明行点名 Goal（告诉用户去哪开）',
      noteCount('#popGoal') === 1 && note('#popGoal').indexOf('Goal') >= 0,
      'n=' + noteCount('#popGoal') + ' text=' + JSON.stringify(note('#popGoal')));
    ck('A13 关掉 Goal 后 #goalRounds 被禁用（键盘也改不了）',
      document.getElementById('goalRounds').disabled === true,
      'disabled=' + document.getElementById('goalRounds').disabled);

    n0 = cfgSets().length;
    $('#popGoal .pi[data-v="on"]').click();
    await sleep(80);
    ck('A14 关掉 Goal 后点审核模式条目不发 config/set',
      cfgSets().length === n0, 'configSet 增量=' + (cfgSets().length - n0));

    /* 再打开 Goal → 审核模式恢复可配置（不能只单向变灰） */
    $('#popPlugin .pi[data-v="Goal"]').click();
    await sleep(60);
    emit({ type: 'config/result', params: {
      ok: true,
      current: { name: 'Goal', enabled: true },
      plugins: plugins(true), pluginsLocked: false,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(200);
    ck('A15 重新打开 Goal 后审核模式恢复可配置',
      $('#popGoal').getAttribute('data-lock') === null &&
      noteCount('#popGoal') === 0 &&
      document.getElementById('goalRounds').disabled === false,
      'lock=' + $('#popGoal').getAttribute('data-lock') +
      ' plock=' + noteCount('#popGoal') +
      ' disabled=' + document.getElementById('goalRounds').disabled);

    /* ================= B. 会话已开始工作 → 锁定 ================= */
    init({
      goal: { enabled: false, maxRounds: 10, locked: true },
      plugins: plugins(false),
      pluginsLocked: true
    });
    await sleep(250);

    ck('B1 锁定：#popPlugin[data-lock="true"]',
      $('#popPlugin').getAttribute('data-lock') === 'true',
      'data-lock=' + $('#popPlugin').getAttribute('data-lock'));
    ck('B2 锁定：#btnPlugin[data-lock="true"] 且仍显示「N/M」（不能藏起来）',
      $('#btnPlugin').getAttribute('data-lock') === 'true' &&
      /^插件\s\d+\/\d+$/.test(txt('btnPlugin')),
      'data-lock=' + $('#btnPlugin').getAttribute('data-lock') +
      ' text=' + JSON.stringify(txt('btnPlugin')));
    ck('B3 锁定：唯一一条说明行，文案含「不可更改」',
      noteCount('#popPlugin') === 1 && note('#popPlugin').indexOf('不可更改') >= 0,
      'n=' + noteCount('#popPlugin') + ' text=' + JSON.stringify(note('#popPlugin')));
    ck('B4 Goal 插件关闭 → #popGoal 锁定且说明里点名 Goal',
      $('#popGoal').getAttribute('data-lock') === 'true' &&
      note('#popGoal').indexOf('Goal') >= 0,
      'data-lock=' + $('#popGoal').getAttribute('data-lock') +
      ' text=' + JSON.stringify(note('#popGoal')));
    ck('B5 锁定：#goalRounds 被禁用（键盘也改不了）',
      document.getElementById('goalRounds').disabled === true,
      'disabled=' + document.getElementById('goalRounds').disabled);

    /* ---- B5b 锁定态下两个下拉仍能展开（只读 ≠ 不能看）---- */
    $('#btnGoal').click();
    await sleep(60);
    var openGoal = $('#popGoal').getAttribute('data-open') === 'true';
    $('#btnPlugin').click();
    await sleep(60);
    var openPlug = $('#popPlugin').getAttribute('data-open') === 'true';
    ck('B5b 锁定态点 #btnGoal / #btnPlugin 仍能展开下拉（要看得到当前值）',
      openGoal && openPlug, 'popGoal=' + openGoal + ' popPlugin=' + openPlug);

    /* ---- B6 锁定态点条目不下发任何 config/set ---- */
    n0 = cfgSets().length;
    $('#popPlugin .pi').click();
    $('#popPlugin .pi[data-v="Goal"]').click();
    await sleep(80);
    ck('B6 锁定时点插件条目不发 config/set',
      cfgSets().length === n0, 'configSet 增量=' + (cfgSets().length - n0));

    n0 = cfgSets().length;
    $('#popGoal .pi[data-v="on"]').click();
    document.getElementById('goalRounds').value = '3';
    document.getElementById('goalRounds').dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(80);
    ck('B7 锁定时点审核模式条目 / 改轮数上限都不发 config/set',
      cfgSets().length === n0, 'configSet 增量=' + (cfgSets().length - n0));

    /* ---- B8 turn/begin 的 goal 不带 locked → 锁定态保持 ---- */
    emit({ type: 'turn/begin', params: { round: 1,
      goal: { enabled: true, current: 1, max: 10 } } });
    await sleep(120);
    ck('B8 turn/begin 不带 locked 时不清掉已有锁定态',
      $('#popGoal').getAttribute('data-lock') === 'true' &&
      noteCount('#popGoal') === 1,
      'data-lock=' + $('#popGoal').getAttribute('data-lock') +
      ' plock=' + noteCount('#popGoal'));
    /* 收掉这一轮：不然 data-run 一直是 true，后面的「可点」断言会被运行态锁挡住 */
    emit({ type: 'turn/end', params: { reason: 'completed' } });
    await sleep(150);

    /* ================= C. 解锁（后端说 locked:false）================= */
    init({
      goal: { enabled: false, maxRounds: 10, locked: false },
      plugins: plugins(true),
      pluginsLocked: false
    });
    await sleep(250);

    ck('C1 goal.locked=false → #popGoal 撤掉 data-lock',
      $('#popGoal').getAttribute('data-lock') === null ||
      $('#popGoal').getAttribute('data-lock') === 'false',
      'data-lock=' + $('#popGoal').getAttribute('data-lock'));
    ck('C2 goal.locked=false → #popGoal 的说明行被移除', noteCount('#popGoal') === 0,
      'plock=' + noteCount('#popGoal'));
    ck('C3 解锁后 #goalRounds 恢复可编辑',
      document.getElementById('goalRounds').disabled === false,
      'disabled=' + document.getElementById('goalRounds').disabled);
    ck('C4 pluginsLocked=false → #popPlugin 也解锁',
      $('#popPlugin').getAttribute('data-lock') === null &&
      noteCount('#popPlugin') === 0,
      'data-lock=' + $('#popPlugin').getAttribute('data-lock') +
      ' plock=' + noteCount('#popPlugin'));

    /* ================= D. config/set 回执驱动（不本地推演）================= */
    n0 = cfgSets().length;
    $('#popGoal .pi[data-v="on"]').click();
    await sleep(80);
    ck('D1 解锁态点审核模式「开」发出 config/set(key=goal)',
      cfgSets().length - n0 === 1 &&
      (cfgSets()[cfgSets().length - 1].params || {}).key === 'goal',
      '增量=' + (cfgSets().length - n0));
    emit({ type: 'config/result', params: { ok: true,
      current: { enabled: true, maxRounds: 12, locked: false } } });
    await sleep(120);
    ck('D2 回执 current 驱动按钮文案（0/12 来自后端，不是本地推的）',
      txt('btnGoal') === '审核模式 开 · 0/12', JSON.stringify(txt('btnGoal')));
    ck('D3 回执 locked:false → 仍未锁定',
      $('#popGoal').getAttribute('data-lock') === null, 
      'data-lock=' + $('#popGoal').getAttribute('data-lock'));

    n0 = cfgSets().length;
    $('#popGoal .pi[data-v="off"]').click();
    await sleep(80);
    emit({ type: 'config/result', params: { ok: true,
      current: { enabled: false, maxRounds: 12, locked: true } } });
    await sleep(120);
    ck('D4 回执 locked:true → 立刻灰显 + 说明行（灰显状态只认后端）',
      $('#popGoal').getAttribute('data-lock') === 'true' &&
      noteCount('#popGoal') === 1 &&
      $('#btnGoal').getAttribute('data-lock') === 'true',
      'data-lock=' + $('#popGoal').getAttribute('data-lock') +
      ' plock=' + noteCount('#popGoal') +
      ' btn=' + $('#btnGoal').getAttribute('data-lock'));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-plugin-lock</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-pluglock');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
