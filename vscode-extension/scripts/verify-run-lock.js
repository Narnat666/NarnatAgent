/**
 * 验收「模型运行中把配置项与命令输入锁成只读」（用户要求原话：
 * 「模型在运行中的时候这些功能不能切换，只能读……用户输入 / 命令和用户输入文字
 * 一样，不能发送，等模型停止后才支持发送」）。
 *
 * 断言：
 *   L1  data-run="true" 时四个配置入口 computed opacity < 1（暗色只读暗示）
 *   L2  运行中点 #btnModel 仍能展开下拉（只读 ≠ 不能看）
 *   L3  运行中点模型条目 → 不发 configSet，按钮文字不变
 *   L4  运行中点思考条目 → 不发 configSet，按钮文字不变
 *   L5  运行中点目标条目 → 不发 configSet，按钮文字不变
 *   L6  运行中点插件开关 → 不发 configSet，开关状态不变
 *   L7  运行中 #goalRounds.disabled === true
 *   L8  运行中输入 /ls 按 Enter → 不发命令、输入框保留、回车被消费
 *   L9  运行中输入普通文本按 Enter → 走插话（发 chatInsert、清空输入框、
 *       不进对话区、不落普通 chat、data-run 保持 true）
 *   L10 data-run 回到 false 后全部自动恢复（锁解开、能切、外观复原、命令能发）
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
  var out = [], fails = 0, total = 0;
  function ck(n, ok, d){
    total++;
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function $(s){ return document.querySelector(s); }
  function el(id){ return document.getElementById(id); }
  function txt(id){ return (el(id).textContent || '').trim(); }
  function setVal(v){
    el('input').value = v;
    el('input').dispatchEvent(new Event('input', { bubbles: true }));
  }
  function setRun(v){ document.body.setAttribute('data-run', v); }
  function op(id){ return getComputedStyle(el(id)).opacity; }
  function sent(){
    return (window.__sent || []).filter(function(m){ return m && m.type; });
  }
  function cfgSets(){
    return sent().filter(function(m){ return /config/i.test(String(m.type)); });
  }
  function byKind(re){
    return sent().filter(function(m){ return re.test(String(m.type)); });
  }
  function enter(){
    var e = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    el('input').dispatchEvent(e);
    return e.defaultPrevented;
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm1', options: ['m1', 'm2'] },
      thinking: { effort: 'high', options: { high: '高', max: '全开' } },
      goal: { enabled: false, maxRounds: 10 },
      plugins: [{ name: 'term', label: '多终端持久 SSH', enabled: true }],
      session: {} } });
    await sleep(300);
    out.push('DIAG 初始按钮文字 model=' + JSON.stringify(txt('btnModel')) +
      ' think=' + JSON.stringify(txt('btnThink')) +
      ' goal=' + JSON.stringify(txt('btnGoal')) +
      ' plugin=' + JSON.stringify(txt('btnPlugin')));

    /* ---------------- 进入「运行中」 ---------------- */
    setRun('true');
    await sleep(80);

    /* L1 外观变暗 */
    var o = { model: op('btnModel'), think: op('btnThink'),
      goal: op('btnGoal'), plugin: op('btnPlugin') };
    ck('L1 运行中四个配置入口变暗', Number(o.model) < 1 && Number(o.think) < 1 &&
      Number(o.goal) < 1 && Number(o.plugin) < 1, JSON.stringify(o));

    /* L2 仍能展开 */
    el('btnModel').click();
    await sleep(40);
    ck('L2 运行中点 #btnModel 仍能展开下拉（只读 ≠ 不能看）',
      $('#popModel').getAttribute('data-open') === 'true',
      'data-open=' + $('#popModel').getAttribute('data-open'));

    /* L3 模型条目点击无效果 */
    var n0 = cfgSets().length;
    $('#popModel .pi[data-v="m2"]').click();
    await sleep(40);
    ck('L3 运行中点模型条目 m2 不生效',
      cfgSets().length === n0 && txt('btnModel') === 'm1',
      'configSet 增量=' + (cfgSets().length - n0) + ' btnModel=' + JSON.stringify(txt('btnModel')));

    /* L4 思考条目点击无效果 */
    n0 = cfgSets().length;
    $('#popThink .pi[data-v="max"]').click();
    await sleep(40);
    ck('L4 运行中点思考条目「全开」不生效',
      cfgSets().length === n0 && txt('btnThink') === '高',
      'configSet 增量=' + (cfgSets().length - n0) + ' btnThink=' + JSON.stringify(txt('btnThink')));

    /* L5 目标条目点击无效果 */
    n0 = cfgSets().length;
    $('#popGoal .pi[data-v="on"]').click();
    await sleep(40);
    ck('L5 运行中点目标模式「开」不生效',
      cfgSets().length === n0 && txt('btnGoal') === '目标模式 关',
      'configSet 增量=' + (cfgSets().length - n0) + ' btnGoal=' + JSON.stringify(txt('btnGoal')));

    /* L6 插件开关点击无效果 */
    n0 = cfgSets().length;
    var sw = $('#popPlugin .pi[data-v="term"] .sw');
    sw.click();
    await sleep(40);
    ck('L6 运行中点插件开关不生效',
      cfgSets().length === n0 && sw.getAttribute('data-on') === 'true',
      'configSet 增量=' + (cfgSets().length - n0) + ' .sw[data-on]=' + sw.getAttribute('data-on'));

    /* L7 轮数上限输入框不可编辑 */
    ck('L7 运行中 #goalRounds 被禁用', el('goalRounds').disabled === true,
      'disabled=' + el('goalRounds').disabled);

    /* L8 运行中 / 命令不能执行 */
    var cmd0 = byKind(/command/i).length;
    setVal('/ls');
    await sleep(220);
    var prevented8 = enter();
    await sleep(60);
    ck('L8 运行中输入 /ls 回车不执行命令',
      byKind(/command/i).length === cmd0 && el('input').value === '/ls' && prevented8 === true,
      'command 增量=' + (byKind(/command/i).length - cmd0) +
      ' input=' + JSON.stringify(el('input').value) + ' prevented=' + prevented8);

    /* L9 运行中普通文本走插话（chatInsert）：不丢字、不进对话区、不落普通发送 */
    var chat0 = byKind(/^chat$/i).length;
    var ins0 = byKind(/chatInsert/i).length;
    var bub0 = $('#convo').querySelectorAll('.msg[data-role="user"]').length;
    setVal('你好');
    await sleep(80);
    var prevented9 = enter();
    await sleep(60);
    var insD = byKind(/chatInsert/i).length - ins0;
    var bubD = $('#convo').querySelectorAll('.msg[data-role="user"]').length - bub0;
    ck('L9 运行中输入普通文本改走插话（chatInsert，不丢字也不画气泡）',
      insD === 1 && byKind(/^chat$/i).length === chat0 &&
      el('input').value === '' && bubD === 0 &&
      document.body.getAttribute('data-run') === 'true',
      'chatInsert 增量=' + insD + ' chat 增量=' + (byKind(/^chat$/i).length - chat0) +
      ' input=' + JSON.stringify(el('input').value) + ' user气泡增量=' + bubD +
      ' data-run=' + document.body.getAttribute('data-run') +
      ' prevented=' + prevented9);

    /* ---------------- 这一轮结束：全部自动恢复 ---------------- */
    setRun('false');
    await sleep(120);

    ck('L10a 结束后 #goalRounds 解锁', el('goalRounds').disabled === false,
      'disabled=' + el('goalRounds').disabled);

    n0 = cfgSets().length;
    $('#popModel .pi[data-v="m2"]').click();
    await sleep(60);
    var d10 = cfgSets();
    var lastSet = d10[d10.length - 1] || {};
    ck('L10b 结束后点 m2 真的发 configSet 且按钮跟着变',
      d10.length - n0 === 1 && lastSet.params && lastSet.params.key === 'model' &&
      txt('btnModel') === 'm2',
      'configSet 增量=' + (d10.length - n0) + ' key=' +
      JSON.stringify(lastSet.params && lastSet.params.key) +
      ' btnModel=' + JSON.stringify(txt('btnModel')));

    var o2 = { model: op('btnModel'), think: op('btnThink'),
      goal: op('btnGoal'), plugin: op('btnPlugin') };
    ck('L10c 结束后四个入口外观复原', o2.model === '1' && o2.think === '1' &&
      o2.goal === '1' && o2.plugin === '1', JSON.stringify(o2));

    var cmd1 = byKind(/command/i).length;
    setVal('/ls');
    await sleep(220);
    var prevented10 = enter();
    await sleep(80);
    var sentCmd = byKind(/command/i).length > cmd1;
    var emptied = el('input').value === '';
    ck('L10d 结束后 /ls 回车恢复可用（发请求或被本地渲染消费）',
      sentCmd || emptied,
      '形态=' + (sentCmd ? '发出 command 请求' : (emptied ? '本地渲染并清空输入框' : '两者都没发生')) +
      ' command 增量=' + (byKind(/command/i).length - cmd1) +
      ' input=' + JSON.stringify(el('input').value) + ' prevented=' + prevented10);

    out.push('SUMMARY total=' + total + ' fail=' + fails);
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-run-lock</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-verify-runlock');
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
