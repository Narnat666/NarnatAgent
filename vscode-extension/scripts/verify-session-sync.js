/**
 * 验收「切会话后插件面板 / 审核模式跟着刷新」（任务 D 的前端契约）。
 *
 * 后端新契约：`plugins` / `pluginsLocked` / `goal` 三项不再只随 `initialize`
 * 下发，凡是会换掉当前会话的回执（session/*、command/exec）都带同一份快照。
 *
 * 断言：
 *   8  session/result（sessionSwitch）带三项 → 插件文案按新值重算、锁定行出现、
 *      审核模式文案与锁定态跟着换（字面数据：Goal 插件关）
 *   8b 同一条通路在 Goal 插件开着时 → 说明行用「不可更改」那套文案
 *   9  command/result（/cd，stateChanged 含 session）带三项 → 解锁、文案回改
 *   10 旧后端形态（回执不带三项）→ 界面**保持上一条的值**，不清空也不解锁
 *   11 session/result（sessionNew）带默认值 → 回到默认（覆盖另一个调用点）
 *   12 同一会话内「问完第一句 → 锁定」当场生效：未锁定 → turn/begin →
 *      session/config(pluginsLocked:true) → 两个下拉都锁定、#goalRounds 禁用、
 *      点条目不发 config/set（每条出这一条：插件锁定不能等下一次握手）
 *   13 失败回滚不得把锁定态冲成可点（回滚基准必须跟着 session/config 推进）
 *   14 不会「每轮重算成未锁定」：只由后端布尔驱动
 *   15 SUMMARY total=N fail=0
 *
 * 页面组装与收尾照抄 scripts/verify-plugin-lock.js（headless Edge + --dump-dom）。
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
  function note(sel){
    var n = document.querySelector(sel + ' .plock');
    return n ? (n.textContent || '') : '';
  }
  function noteCount(sel){ return document.querySelectorAll(sel + ' .plock').length; }
  function cfgSets(){
    return (window.__sent || []).filter(function(m){ return m && m.type === 'configSet'; });
  }
  function lockOf(sel){
    var e = $(sel);
    return e ? e.getAttribute('data-lock') : '(无此节点)';
  }
  /* 后端下发的 5 个插件（enabled 是**当前会话**的生效值） */
  function plugins(goalOn){
    return [
      { name: 'Terminal', label: '多终端持久 SSH', enabled: true },
      { name: 'WebSearch', label: '联网搜索', enabled: true },
      { name: 'Serial', label: '多终端持久串口', enabled: true },
      { name: 'MCP', label: 'MCP 服务器管理', enabled: true },
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
    /* ============ 起点：全开、未锁定、Goal 插件开 ============ */
    init({
      goal: { enabled: false, maxRounds: 10, locked: false },
      plugins: plugins(true),
      pluginsLocked: false
    });
    await sleep(250);
    ck('0 起点：插件按钮「插件 5」、未锁定、审核模式关',
      txt('btnPlugin') === '插件 5' &&
      lockOf('#popPlugin') === null &&
      noteCount('#popPlugin') === 0 &&
      txt('btnGoal') === '审核模式 关',
      'btn=' + JSON.stringify(txt('btnPlugin')) +
      ' lock=' + lockOf('#popPlugin') +
      ' plock=' + noteCount('#popPlugin') +
      ' goal=' + JSON.stringify(txt('btnGoal')));

    /* ============ 8. session/result（sessionSwitch）驱动三项 ============
       字面数据：新会话的 Goal 插件关、锁定、审核模式关。
       Goal 插件关时说明行走的是 applyGoalLock 的「Goal 未启用」那一支
       （既有判据，任务 D5 明确不许改），「不可更改」那支由 8b 覆盖。 */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'B', messageCount: 0, messages: [],
      plugins: plugins(false), pluginsLocked: true,
      goal: { enabled: false, maxRounds: 10, locked: true }
    } });
    await sleep(250);

    ck('8a 插件按钮按回执里的 plugins 重算（4/5，不是上一个会话的 5）',
      txt('btnPlugin') === '插件 4/5', JSON.stringify(txt('btnPlugin')));
    ck('8b #popPlugin 锁定且出现锁定说明行',
      lockOf('#popPlugin') === 'true' && noteCount('#popPlugin') === 1,
      'lock=' + lockOf('#popPlugin') + ' plock=' + noteCount('#popPlugin') +
      ' text=' + JSON.stringify(note('#popPlugin')));
    ck('8c #btnGoal 文案跟着换成「审核模式 关」',
      txt('btnGoal') === '审核模式 关', JSON.stringify(txt('btnGoal')));
    ck('8d #popGoal 锁定（审核模式不可更改）',
      lockOf('#popGoal') === 'true' && noteCount('#popGoal') === 1 &&
      note('#popGoal').length > 0,
      'lock=' + lockOf('#popGoal') + ' plock=' + noteCount('#popGoal') +
      ' text=' + JSON.stringify(note('#popGoal')));

    /* ---- 8e 关键新语义：Goal 插件开着时，会话锁定不影响审核模式 ----
       审核模式不改工具表、不占缓存前缀，所以「本会话已开始工作」不该锁它；
       唯一的锁定原因是插件 Goal 未启用（8d 覆盖）。这里 Goal 开着 + 会话已锁，
       期望：插件灰显只读，审核模式照常可配置。 */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'B2', messageCount: 0, messages: [],
      plugins: plugins(true).map(function(p){
        return p.name === 'MCP' ? { name: p.name, label: p.label, enabled: false } : p;
      }),
      pluginsLocked: true,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(250);
    ck('8e Goal 插件开着 + 会话已锁定 → 插件灰显但审核模式仍可配置',
      txt('btnPlugin') === '插件 4/5' && txt('btnGoal') === '审核模式 关' &&
      lockOf('#popPlugin') === 'true' &&
      lockOf('#popGoal') === null && noteCount('#popGoal') === 0,
      'btnPlugin=' + JSON.stringify(txt('btnPlugin')) +
      ' btnGoal=' + JSON.stringify(txt('btnGoal')) +
      ' popPlugin=' + lockOf('#popPlugin') +
      ' popGoal=' + lockOf('#popGoal') + ' plockG=' + noteCount('#popGoal'));

    /* ============ 9. command/result（/cd）驱动三项 ============
       真实后端 /cd 的回执形状：lines 非空、带 current/messages，且 stateChanged
       含 session 时另带 plugins/pluginsLocked/goal。 */
    emit({ type: 'command/result', params: {
      command: 'cd', args: 'A', handled: true, ok: true, noop: false,
      lines: ['已进入会话: A'], stateChanged: ['session'], current: 'A',
      isChild: false, messages: [],
      plugins: plugins(true), pluginsLocked: false,
      goal: { enabled: true, maxRounds: 5, locked: false }
    } });
    await sleep(300);

    ck('9a /cd 回执后 #popPlugin 解锁（data-lock 被移除）',
      lockOf('#popPlugin') === null, 'lock=' + lockOf('#popPlugin'));
    ck('9b /cd 回执后 #popPlugin 的锁定说明行消失',
      noteCount('#popPlugin') === 0, 'plock=' + noteCount('#popPlugin'));
    ck('9c /cd 回执后 #btnGoal 是「审核模式 开 · 0/5」',
      txt('btnGoal') === '审核模式 开 · 0/5', JSON.stringify(txt('btnGoal')));
    ck('9d /cd 回执后 #btnPlugin 回到「插件 5」',
      txt('btnPlugin') === '插件 5', JSON.stringify(txt('btnPlugin')));

    /* ============ 10. 字段缺失不清空、不解锁（旧后端形态）============ */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'C', messageCount: 0, messages: []
    } });
    await sleep(250);
    ck('10a 回执不带 plugins 时按钮文案保持上一条的值（插件 5）',
      txt('btnPlugin') === '插件 5', JSON.stringify(txt('btnPlugin')));
    ck('10b 回执不带 goal 时审核模式保持上一条的值（开 · 0/5）',
      txt('btnGoal') === '审核模式 开 · 0/5', JSON.stringify(txt('btnGoal')));
    ck('10c 回执不带锁定字段时不擅自解锁/加锁',
      lockOf('#popPlugin') === null && lockOf('#popGoal') === null &&
      noteCount('#popPlugin') === 0 && noteCount('#popGoal') === 0,
      'popPlugin=' + lockOf('#popPlugin') + ' popGoal=' + lockOf('#popGoal') +
      ' plockP=' + noteCount('#popPlugin') + ' plockG=' + noteCount('#popGoal'));

    /* ============ 11. session/result（sessionNew）也走同一入口 ============ */
    emit({ type: 'session/result', params: {
      action: 'sessionNew', current: null, messageCount: 0, messages: [],
      plugins: plugins(false), pluginsLocked: false,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(250);
    ck('11a 新建对话回执把插件换成默认值（Goal 关 → 4/5）',
      txt('btnPlugin') === '插件 4/5', JSON.stringify(txt('btnPlugin')));
    ck('11b 新建对话回执后审核模式关且可配置（data-lock 撤掉）',
      txt('btnGoal') === '审核模式 关' && lockOf('#popGoal') === null &&
      noteCount('#popGoal') === 0,
      'btnGoal=' + JSON.stringify(txt('btnGoal')) +
      ' lock=' + lockOf('#popGoal') + ' plock=' + noteCount('#popGoal'));

    /* ============ 12. 同一会话内「问完第一句 → 锁定」当场生效 ============
       这是独立验证员点出的缺陷：锁定态原来只在 initialize / 会话回执里，
       用户新建对话问第一句、AI 跑完后下拉仍是未锁定外观，点一下才被 -32000 拒绝。
       后端修法：每轮真正收口时 emit session/config（载荷 = initialize 同一份快照）。
       这里模拟真实时序：未锁定 → turn/begin → session/config(pluginsLocked:true)。 */
    emit({ type: 'session/result', params: {
      action: 'sessionNew', current: null, messageCount: 0, messages: [],
      plugins: plugins(true), pluginsLocked: false,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(200);
    ck('12a 起点：新建对话后未锁定、5 个插件全开',
      lockOf('#popPlugin') === null && noteCount('#popPlugin') === 0 &&
      txt('btnPlugin') === '插件 5',
      'lock=' + lockOf('#popPlugin') + ' btn=' + JSON.stringify(txt('btnPlugin')));

    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(60);
    var nRun = cfgSets().length;
    $('#popGoal .pi[data-v="on"]').click();
    await sleep(80);
    ck('12a1 AI 工作中审核模式不可配置（点「开」不发 config/set）',
      cfgSets().length === nRun &&
      document.getElementById('goalRounds').disabled === true,
      'configSet 增量=' + (cfgSets().length - nRun) +
      ' roundsDisabled=' + document.getElementById('goalRounds').disabled);

    /* 真实后端时序：turn/end 之后才 emit session/config（见 RpcServer._on_turn_end） */
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(60);
    emit({ type: 'session/config', params: {
      plugins: plugins(true), pluginsLocked: true,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(200);
    ck('12b 轮次收口通知后 #popPlugin 立刻锁定',
      lockOf('#popPlugin') === 'true' && noteCount('#popPlugin') === 1 &&
      note('#popPlugin').indexOf('不可更改') >= 0,
      'lock=' + lockOf('#popPlugin') + ' plock=' + noteCount('#popPlugin') +
      ' text=' + JSON.stringify(note('#popPlugin')));
    ck('12c 轮次收口后审核模式可配置（Goal 插件开着 → 不锁）',
      lockOf('#popGoal') === null && noteCount('#popGoal') === 0,
      'lock=' + lockOf('#popGoal') + ' plock=' + noteCount('#popGoal') +
      ' text=' + JSON.stringify(note('#popGoal')));
    ck('12d 轮次收口后 #goalRounds 恢复可编辑（运行态解除）',
      document.getElementById('goalRounds').disabled === false,
      'disabled=' + document.getElementById('goalRounds').disabled);

    var nLock = cfgSets().length;
    $('#popPlugin .pi').click();
    await sleep(100);
    ck('12e 锁定时点插件条目不发的 config/set（不发无效请求）',
      cfgSets().length === nLock, 'configSet 增量=' + (cfgSets().length - nLock));

    /* ============ 13. 失败回滚不得把锁定态冲成可点 ============
       真实时序：未锁定时点一下 → 请求在路上 → 这一轮收口，后端已锁定 →
       失败回执 -32000 到达 → revertConfigUi('plugin') 按「回滚基准」重画。
       基准没跟着 session/config 推进的话，下拉会退回可点外观，用户再点还是失败。 */
    emit({ type: 'session/result', params: {
      action: 'sessionNew', current: null, messageCount: 0, messages: [],
      plugins: plugins(true), pluginsLocked: false,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(200);
    var nRevert = cfgSets().length;
    $('#popPlugin .pi[data-v="Terminal"]').click();   /* 此刻未锁定 → 真的发请求 */
    await sleep(80);
    emit({ type: 'session/config', params: {
      plugins: plugins(true), pluginsLocked: true,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(120);
    emit({ type: 'config/result', params: { ok: false,
      message: '插件配置不可更改：本会话已开始工作（新建对话后可调整）' } });
    await sleep(200);
    ck('13a 这一下点击确实发出去了（证明走的是「请求在路上」这条真实路径）',
      cfgSets().length - nRevert === 1, 'configSet 增量=' + (cfgSets().length - nRevert));
    ck('13b 失败回滚后 #popPlugin 仍是锁定态（没被陈旧基准冲成可点）',
      lockOf('#popPlugin') === 'true' && noteCount('#popPlugin') === 1,
      'lock=' + lockOf('#popPlugin') + ' plock=' + noteCount('#popPlugin') +
      ' text=' + JSON.stringify(note('#popPlugin')));
    ck('13c 失败回滚不牵连审核模式（Goal 开着 → 仍可配置）',
      lockOf('#popGoal') === null,
      'lock=' + lockOf('#popGoal'));

    /* ============ 14. 不会「每轮重算成未锁定」============
       锁定只由后端布尔驱动：后续轮次再推 session/config 时值仍是 true → 保持锁定；
       中间轮（turn/begin/end 不带配置快照）也不会把它清掉。 */
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(80);
    emit({ type: 'session/config', params: {
      plugins: plugins(true), pluginsLocked: true,
      goal: { enabled: false, maxRounds: 10, locked: false }
    } });
    await sleep(150);
    ck('14a 再跑一轮（pluginsLocked 仍为 true）→ 插件保持锁定，不回到未锁定',
      lockOf('#popPlugin') === 'true' && lockOf('#popGoal') === null,
      'popPlugin=' + lockOf('#popPlugin') + ' popGoal=' + lockOf('#popGoal'));
    emit({ type: 'turn/begin', params: { round: 2,
      goal: { enabled: false, current: 2, max: 10 } } });
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(150);
    ck('14b turn/begin、turn/end 本身不带锁定字段 → 锁定态不被清掉',
      lockOf('#popPlugin') === 'true' && noteCount('#popPlugin') === 1,
      'lock=' + lockOf('#popPlugin') + ' plock=' + noteCount('#popPlugin'));

    /* ===== 15 运行期值（模型 / 思考强度）随 session/config 回退 =====
       插件里改这两项**只影响运行**（不写 narnat.json，避免扩展宿主弹
       「配置已修改，需要重启后端」）。既然不落盘，换会话就必须恢复成
       narnat.json 的值 —— 后端在 session/config 里带 runtime 字段，
       界面不跟着回退就会「按钮写着 v4-pro、后端在用 flash」。 */
    document.getElementById('btnModel').textContent = 'deepseek-v4-pro';
    document.getElementById('btnThink').textContent = '快速';
    emit({ type: 'session/config', params: {
      runtime: { model: 'deepseek-flash', thinking: 'high' } } });
    await sleep(150);
    ck('15a session/config.runtime 把模型按钮退回配置值',
      document.getElementById('btnModel').textContent === 'deepseek-flash',
      JSON.stringify(document.getElementById('btnModel').textContent));
    ck('15b session/config.runtime 把思考强度按钮退回配置值（取下拉里的 label）',
      // 期望值从下拉里取，不在断言里写死文案 —— 文案属于配置（narnat.json
      // 「思考.强度选项」），测试写死会在用户改配置后变成假失败。
      (function(){
        var el = document.querySelector('#popThink .pi[data-v="high"] .lb');
        var want = el ? el.textContent : 'high';
        return document.getElementById('btnThink').textContent === want;
      })(),
      JSON.stringify(document.getElementById('btnThink').textContent));

    document.getElementById('btnModel').textContent = 'deepseek-v4-pro';
    emit({ type: 'session/config', params: {
      plugins: [], pluginsLocked: false } });
    await sleep(150);
    ck('15c 不带 runtime 的 session/config 不动模型按钮（缺字段保持现状）',
      document.getElementById('btnModel').textContent === 'deepseek-v4-pro',
      JSON.stringify(document.getElementById('btnModel').textContent));
    document.getElementById('btnModel').textContent = 'deepseek-flash';

    out.push('SUMMARY total=' + (out.length + 0) + ' fail=' + fails);
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-session-sync</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-sessionsync');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=60000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const text = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
