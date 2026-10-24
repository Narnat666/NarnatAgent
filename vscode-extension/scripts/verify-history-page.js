/**
 * 验证「历史会话按需加载」的前端半边（对齐 dsh 的 chat.loadOlder）。
 *
 * 四条要验（任务书 ④.8~11）：
 *  8) 尾页回执（historyHasMore=true）→ 对话区顶部出现「加载更早」，且是
 *     .convo 的第一个子元素
 *  9) 点击后 webview 发出 {type:'sessionHistory', before, name}
 * 10) 回灌更早一页 → 按钮消失（已翻到最开头）、两页内容都在、滚到顶能看到
 *     更早那一页的第一条（说明内容确实插在前面）
 * 11) 老后端（回执不带 historyHasMore）→ 没有按钮，对话区照常渲染
 * 12) R30：页首不是 user 的一页（后端允许劈轮）不抛错、条目一条不少，
 *     且回灌更早一页合并重画后，页首孤立工具组与前一页最后一轮不重复计入
 *
 * 运行：node scripts/verify-history-page.js（headless Edge）
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

/** 造 n 轮纯文本对话（每轮 1 条 user + 1 条 assistant）。 */
function turns(prefix, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({ role: 'user', text: prefix + '问' + i });
    out.push({ role: 'assistant', text: prefix + '答' + i });
  }
  return out;
}

const TAIL = turns('尾', 5);    // 10 条 = 尾页
const OLDER = turns('早', 6);   // 12 条 = 更早一页

/** 一条历史工具行（形状与 _history_payload 的 tool 条目一致）。 */
function toolRow(id, name, label, summary, output) {
  return { role: 'tool', id, name, label, summary, arguments: {}, output };
}

/* P12（R30）：后端加了「每页条目硬上限」，允许劈轮 —— 页首可以不是 user。
   12 条，页首是 3 条工具串（上一轮被劈开的尾巴），后面才是 user 边界。
   渲染产物：折叠组 2 个 + .msg 7 条 + .tools 5 条 = 12 条消息。 */
const PAGE1 = [
  toolRow('s1', 'Shell', '执行命令', 'grep -n x main.py', '1: x'),
  toolRow('s2', 'Read', '读取', 'main.py', 'x = 1'),
  toolRow('s3', 'Grep', '搜索内容', 'renderHistory', 'ui-full.html:1'),
  { role: 'assistant', text: '上一轮的工具跑完就停在这了' },
  { role: 'user', text: '继续，第二段' },
  { role: 'assistant', text: '好的，第二段开始' },
  toolRow('s4', 'Shell', '执行命令', 'node a.js', 'ok'),
  { role: 'assistant', text: '第二段做完一半' },
  toolRow('s5', 'Read', '读取', 'b.js', 'b'),
  { role: 'assistant', text: '第二段收尾' },
  { role: 'user', text: '再问一个' },
  { role: 'assistant', text: '回答完毕' }
];

/* 更早一页：6 条，最后一条是 user —— 它与 PAGE1 页首那串工具属于同一轮，
   合并重画后应该重新合成一轮，而不是「孤立工具组 + 前一页最后一轮」两份。 */
const PAGE2 = [
  { role: 'user', text: '最早的问题' },
  { role: 'assistant', text: '最早的回答' },
  toolRow('e1', 'Shell', '执行命令', 'git status', 'clean'),
  toolRow('e2', 'Read', '读取', 'c.js', 'c'),
  { role: 'assistant', text: '最早的中间说明' },
  { role: 'user', text: '页2最后一条 user（紧接页1的页首工具串）' }
];

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function convo(){ return document.getElementById('convo'); }
  function histBtn(){ return convo().querySelector('.hist-more'); }
  function msgNodes(){ return [].slice.call(convo().querySelectorAll('.msg')); }
  function texts(){
    return msgNodes().map(function(el){
      return String(el.textContent || '').replace(/\s+/g, ' ').trim();
    });
  }
  function flat(t){ return String(t == null ? '' : t).replace(/\s+/g, ' ').trim(); }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);

    /* ── 8) 尾页回执带分页字段 → 顶部出现「加载更早」 ── */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'h',
      messages: window.__TAIL, historyHasMore: true,
      historyBefore: 12, historyTotal: 40 } });
    await sleep(500);

    var btn = histBtn();
    ck('P8a 尾页回执后出现 .hist-more 按钮', !!btn);
    ck('P8b 文案是「加载更早」', !!btn && flat(btn.textContent) === '加载更早',
       btn ? flat(btn.textContent) : '(无)');
    ck('P8c 它是 .convo 的第一个子元素',
       !!btn && convo().firstElementChild === btn,
       btn ? (convo().firstElementChild || {}).className : '(无)');
    ck('P8d 尾页 10 条都画出来了', msgNodes().length === 10,
       String(msgNodes().length));

    /* ── 9) 点击 → 发出 sessionHistory{before:12, name:'h'} ── */
    window.__sent = [];
    btn.click();
    await sleep(60);
    var sent = (window.__sent || []).filter(function(m){ return m.type === 'sessionHistory'; });
    ck('P9a 点击后发出 1 条 sessionHistory', sent.length === 1, String(sent.length));
    var s0 = sent[0] || { params: {} };
    ck('P9b before 是历史里的起点索引 12', (s0.params || {}).before === 12,
       JSON.stringify(s0.params || {}));
    ck('P9c name 是当前会话 h', (s0.params || {}).name === 'h',
       JSON.stringify(s0.params || {}));
    ck('P9d 加载中按钮置灰且文案变化',
       !!histBtn() && histBtn().hasAttribute('disabled') &&
       flat(histBtn().textContent) === '加载中…',
       histBtn() ? flat(histBtn().textContent) : '(无)');

    /* ── 10) 回灌更早一页 → 拼在前面、按钮消失、滚到顶看得到第一条 ── */
    // 先把视口放在中间位置，验证「内容插在上面时滚动位置被补偿」
    convo().scrollTop = 40;
    var stBefore = convo().scrollTop;
    emit({ type: 'history/result', params: {
      messages: window.__OLDER, hasMore: false, before: 0, total: 22 } });
    await sleep(400);

    ck('P10a 翻到最开头后按钮消失', !histBtn(),
       convo().firstElementChild ? convo().firstElementChild.className : '(空)');
    ck('P10b 对话区条目数 = 两页之和（10+12=22）', msgNodes().length === 22,
       String(msgNodes().length));
    var all = texts();
    ck('P10c 更早那一页的每条都画出来了',
       window.__OLDER.every(function(m){ return all.indexOf(flat(m.text)) >= 0; }),
       '早0=' + (all.indexOf('早问0') >= 0) + ' 早答5=' + (all.indexOf('早答5') >= 0));
    ck('P10d 尾页仍然都在',
       window.__TAIL.every(function(m){ return all.indexOf(flat(m.text)) >= 0; }),
       '尾0=' + (all.indexOf('尾问0') >= 0));
    // 更早那一页的第一条必须排在尾页最前面那条之前（内容确实插在前面）
    var iFirst = all.indexOf(flat(window.__OLDER[0].text));
    var iTail = all.indexOf(flat(window.__TAIL[0].text));
    ck('P10e 更早一页在尾页之前', iFirst >= 0 && iTail > iFirst,
       '早问0@' + iFirst + ' 尾问0@' + iTail);
    // 滚动位置补偿：原位置 40，插了 12 条之后仍应停留在同一条内容上
    var stAfter = convo().scrollTop;
    ck('P10f 重画后滚动位置被补偿（没有跳回顶部）',
       stAfter > stBefore, stBefore + ' → ' + stAfter);
    // 滚到顶：更早那一页的第一条落在可视区内
    convo().scrollTop = 0;
    await sleep(60);
    var first = msgNodes()[0];
    var cb = convo().getBoundingClientRect();
    var fb = first ? first.getBoundingClientRect() : { top: -9999, bottom: -9999 };
    ck('P10g 滚到顶能看到更早那一页的第一条',
       !!first && flat(first.textContent) === flat(window.__OLDER[0].text) &&
       fb.top >= cb.top - 2 && fb.top < cb.bottom,
       first ? flat(first.textContent) + ' top=' + Math.round(fb.top) +
         ' convoTop=' + Math.round(cb.top) : '(无节点)');

    /* ── 11) 老后端（回执不带 historyHasMore）→ 没有按钮、照常渲染 ── */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'old', messages: [
        { role: 'user', text: '老后端问' },
        { role: 'assistant', text: '老后端答' }
      ] } });
    await sleep(300);
    ck('P11a 老后端回执后没有 .hist-more 按钮', !histBtn());
    ck('P11b 对话区照常渲染（2 条）', msgNodes().length === 2,
       String(msgNodes().length));

    /* ── 12) R30：页首不是 user 的一页（劈轮边界）也要画得住 ── */
    // 「条目数」= 画出来的历史条目节点（正文气泡 / 工具行 / 思考块，含折叠组内），
    // 与后端 _history_payload 的条目一一对应。
    function itemCount(){
      return convo().querySelectorAll('.msg, .tools, .think').length;
    }
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'split',
      messages: window.__PAGE1, historyHasMore: true,
      historyBefore: 12, historyTotal: 18 } });
    await sleep(500);

    var procs1 = convo().querySelectorAll('.turn-proc');
    var firstProc = procs1[0];
    ck('P12a 页首是 tool、不是 user 的一页不抛错（没有 #ERR）',
       !document.getElementById('ERR'), '#ERR=' + !!document.getElementById('ERR'));
    ck('P12b 12 条消息全部画出来（条目数 == 12）', itemCount() === 12,
       '条目=' + itemCount() + ' msg=' + convo().querySelectorAll('.msg').length +
       ' tools=' + convo().querySelectorAll('.tools').length);
    ck('P12c 页首那串孤立工具被收成折叠组（至少 1 个 .turn-proc，内含 3 条工具）',
       procs1.length >= 1 && !!firstProc &&
       firstProc.querySelectorAll('.tools').length === 3,
       '组=' + procs1.length +
       ' 首组工具=' + (firstProc ? firstProc.querySelectorAll('.tools').length : -1));

    // 回灌更早一页（以 user 结尾）：合并重画后，那串孤立工具与它前面那条
    // user 应该重新合成同一轮 —— 不重复计入，也不凭空多一个组。
    emit({ type: 'history/result', params: {
      messages: window.__PAGE2, hasMore: false, before: 0, total: 18 } });
    await sleep(400);

    var procs2 = convo().querySelectorAll('.turn-proc');
    ck('P12d 两页合并后条目数 == 两页之和（6+12=18，不重不漏）',
       itemCount() === 18, '条目=' + itemCount());
    ck('P12e 工具行总数 == 两页之和（5+2=7，页首工具组没有被重复计入）',
       convo().querySelectorAll('.tools').length === 7,
       'tools=' + convo().querySelectorAll('.tools').length);
    ck('P12f 合并后只多出前一页那一组（3 个折叠组，不是 4 个）',
       procs2.length === 3, '组=' + procs2.length);
    var joinedTexts = texts().join('｜');
    ck('P12g 前一页最后那条 user 只出现一次（页边界没有被画两遍）',
       joinedTexts.split('页2最后一条 user').length - 1 === 1,
       '出现次数=' + (joinedTexts.split('页2最后一条 user').length - 1));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-history-page</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__TAIL=${JSON.stringify(TAIL)};window.__OLDER=${JSON.stringify(OLDER)};
window.__PAGE1=${JSON.stringify(PAGE1)};window.__PAGE2=${JSON.stringify(PAGE2)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-histpage');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-history-page.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const t = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT';
console.log(t);
process.exit(/fail=0/.test(t) ? 0 : 2);
