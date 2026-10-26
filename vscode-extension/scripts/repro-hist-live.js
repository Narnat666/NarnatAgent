/**
 * 复现：AI 还在工作时点「加载更早」，正在进行的这一轮（用户刚发的提问 + 已经
 * 流出来的正文）会被整段抹掉。
 *
 * 现象（用户报障）：打开历史会话 → 提问 → 趁 AI 工作点几次「加载更早」 →
 * 等 AI 完成回头看，刚才那句提问在界面上不见了，只剩下 AI 的回复；
 * 退出会话重新进入才恢复正常。
 *
 * 根因假设：`applyHistoryPage` 用 `resetConversationDom()` + `renderHistory(HIST_PAGE)`
 * 整段重画，而 HIST_PAGE 是「打开会话时的尾页快照」——实时产生的提问与流式
 * 正文都不在里面，清屏后没有任何恢复路径。
 *
 * 运行：node scripts/repro-hist-live.js
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

function turns(prefix, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({ role: 'user', text: prefix + '问' + i });
    out.push({ role: 'assistant', text: prefix + '答' + i });
  }
  return out;
}

const TAIL = turns('尾', 5);   // 10 条 = 打开会话时的尾页快照
const OLDER = turns('早', 6);  // 12 条 = 「加载更早」拿回来的那一页

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
  function allText(){ return String(convo().textContent || '').replace(/\s+/g, ' '); }
  function has(t){ return allText().indexOf(t) >= 0; }
  function flat(t){ return String(t == null ? '' : t).replace(/\s+/g, ' ').trim(); }
  function userMsgs(){
    return [].slice.call(convo().querySelectorAll('.msg[data-role="user"]'));
  }
  function countText(t){
    return allText().split(t).length - 1;
  }
  /** 精确统计「某个用户气泡」出现了几次（countText 是子串统计，会被回答里
      引用同一句话污染）。 */
  function countUser(t){
    return userMsgs().filter(function(el){ return flat(el.textContent) === t; }).length;
  }
  function send(text){
    var inp = document.getElementById('input');
    inp.value = text;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('sendBtn').click();
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);

    /* ---------- 场景 A：AI 正在工作时翻更早的页 ---------- */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'h',
      messages: window.__TAIL, historyHasMore: true,
      historyBefore: 10, historyTotal: 22 } });
    await sleep(300);

    var btn0 = histBtn();
    ck('A0 尾页回执后有「加载更早」', !!btn0);
    ck('A0b 尾页 10 条都画出来了',
       convo().querySelectorAll('.msg').length === 10,
       String(convo().querySelectorAll('.msg').length));

    send('我刚问的问题');
    await sleep(60);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '前半截回答。' } });
    await sleep(60);

    ck('A1 发送后提问在对话区里', has('我刚问的问题'));
    ck('A1b 提问是最后一条用户消息',
       userMsgs().length === 6 && flat(userMsgs()[5].textContent) === '我刚问的问题',
       'user=' + userMsgs().length);
    ck('A2 流式正文已画出来', has('前半截回答'));

    var btn = histBtn();
    ck('A3 运行中「加载更早」仍在', !!btn);
    if (btn) btn.click();
    await sleep(40);
    emit({ type: 'history/result', params: {
      messages: window.__OLDER, hasMore: false, before: 0, total: 22 } });
    await sleep(250);

    ck('A4 翻页后更早那一页画出来了', has('早问0') && has('早答5'));
    ck('A5 尾页仍在', has('尾问0') && has('尾答4'));
    ck('A6 【关键】翻页后刚发的提问仍在', has('我刚问的问题'),
       'convo 末尾 = ' + JSON.stringify(allText().slice(-120)));
    ck('A7 【关键】翻页后已流出的正文仍在', has('前半截回答'));
    ck('A8 提问只出现一次（没有被重复画）', countUser('我刚问的问题') === 1,
       '次数=' + countUser('我刚问的问题'));

    /* 再点一次「加载更早」也不行 —— 用户报障里是「点了数次」 */
    var btn2 = histBtn();
    if (btn2) btn2.click();
    await sleep(40);
    emit({ type: 'history/result', params: {
      messages: [], hasMore: false, before: 0, total: 22 } });
    await sleep(200);
    ck('A9 连点第二次翻页后提问仍在', has('我刚问的问题'),
       'convo 末尾 = ' + JSON.stringify(allText().slice(-120)));

    /* AI 收尾：整段回答必须完整，不能被翻页拦腰截断 */
    emit({ type: 'stream/delta', params: { text: '后半截回答。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'stop' } });
    await sleep(400);

    ck('A10 回答完整（前半截 + 后半截都在）',
       has('前半截回答') && has('后半截回答'),
       '前半=' + has('前半截回答') + ' 后半=' + has('后半截回答'));
    ck('A11 收尾后提问仍在', has('我刚问的问题'));
    ck('A12 这一轮的过程没有被折叠成两个组',
       convo().querySelectorAll('.turn-proc').length <= 1,
       '组=' + convo().querySelectorAll('.turn-proc').length);

    /* ---------- 场景 B：AI 已完成（本轮内容不在尾页快照里）时翻页 ---------- */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'h2',
      messages: window.__TAIL, historyHasMore: true,
      historyBefore: 10, historyTotal: 22 } });
    await sleep(300);

    send('第二问');
    await sleep(60);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '第二问的回答。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'stop' } });
    await sleep(400);

    ck('B1 完成后提问在对话区里', has('第二问'));
    ck('B2 完成后回答在对话区里', has('第二问的回答'));

    var btn3 = histBtn();
    ck('B3 空闲时「加载更早」仍在', !!btn3);
    if (btn3) btn3.click();
    await sleep(40);
    emit({ type: 'history/result', params: {
      messages: window.__OLDER, hasMore: false, before: 0, total: 22 } });
    await sleep(250);

    ck('B4 【关键】空闲翻页后提问仍在', has('第二问'),
       'convo 末尾 = ' + JSON.stringify(allText().slice(-120)));
    ck('B5 【关键】空闲翻页后回答仍在', has('第二问的回答'));
    ck('B6 更早那一页也画出来了', has('早问0'));
    ck('B7 提问只出现一次', countUser('第二问') === 1,
       '次数=' + countUser('第二问'));

    /* ---------- 场景 C：运行中被 initialize 重画过一次，再翻页 ----------
       重画后本轮提问已经进了 HIST_PAGE（后端 messages 里有了），只剩还在长的
       正文没进快照 —— 用户对齐判据全部匹配，只能靠「流式节点」兜底。 */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'h3',
      messages: window.__TAIL, historyHasMore: true,
      historyBefore: 10, historyTotal: 23 } });
    await sleep(300);

    send('第三问');
    await sleep(60);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'stream/delta', params: { text: '第三问前半。' } });
    await sleep(60);

    // 后端重连：initialize 带着「已经包含本轮提问」的尾页 + 分页字段
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: 'h3', messages: window.__TAIL.concat([
        { role: 'user', text: '第三问' }]), historyHasMore: true,
        historyBefore: 10, historyTotal: 23 },
      workdir: '<t>' } });
    await sleep(400);

    ck('C1 重画后提问在（来自历史）', has('第三问'));
    ck('C2 重画后已流出的正文在（来自流式缓冲）', has('第三问前半。'));

    var btn4 = histBtn();
    ck('C3 重画后「加载更早」仍在', !!btn4);
    if (btn4) btn4.click();
    await sleep(40);
    emit({ type: 'history/result', params: {
      messages: window.__OLDER, hasMore: false, before: 0, total: 23 } });
    await sleep(250);

    ck('C4 【关键】这种情况下翻页，已流出的正文仍在', has('第三问前半。'),
       'convo 末尾 = ' + JSON.stringify(allText().slice(-120)));
    ck('C5 更早那一页也画出来了', has('早问0'));
    ck('C6 提问只出现一次', countUser('第三问') === 1,
       '次数=' + countUser('第三问'));

    emit({ type: 'stream/delta', params: { text: '第三问后半。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'stop' } });
    await sleep(400);
    ck('C7 收尾后回答完整', has('第三问前半。') && has('第三问后半。'),
       '前半=' + has('第三问前半。') + ' 后半=' + has('第三问后半。'));

    /* ---------- 场景 D：翻页时这一轮的工具行/思考块也要留住 ----------
       工具行是「这一轮到底干了什么」的唯一记录（后端不会重发 tool/call），
       重画时丢了就永久没了；toolMap 不恢复则会永远停在「执行中」。 */
    emit({ type: 'session/result', params: {
      action: 'sessionSwitch', current: 'h4',
      messages: window.__TAIL, historyHasMore: true,
      historyBefore: 10, historyTotal: 23 } });
    await sleep(300);

    send('第四问');
    await sleep(60);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/begin', params: {} });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '先想一想。' } });
    await sleep(20);
    emit({ type: 'tool/call', params: { id: 't1', name: 'Read', label: '读取',
      summary: 'a.js', arguments: { file_path: 'a.js' } } });
    await sleep(60);

    ck('D1 翻页前有工具行、思考块',
       convo().querySelectorAll('.tools').length === 1 &&
       convo().querySelectorAll('.think').length === 1,
       'tools=' + convo().querySelectorAll('.tools').length +
       ' think=' + convo().querySelectorAll('.think').length);

    var btn5 = histBtn();
    if (btn5) btn5.click();
    await sleep(40);
    emit({ type: 'history/result', params: {
      messages: window.__OLDER, hasMore: false, before: 0, total: 23 } });
    await sleep(250);

    ck('D2 【关键】翻页后工具行仍在', convo().querySelectorAll('.tools').length === 1,
       'tools=' + convo().querySelectorAll('.tools').length);
    ck('D3 【关键】翻页后思考块仍在', convo().querySelectorAll('.think').length === 1,
       'think=' + convo().querySelectorAll('.think').length);
    ck('D4 提问仍在', countUser('第四问') === 1);

    /* toolMap 没恢复的话，这条结果会被静默丢弃，行永远停在「执行中」 */
    emit({ type: 'tool/result', params: { id: 't1', ok: true, durationMs: 3, output: 'x = 1' } });
    await sleep(200);
    var meta = convo().querySelector('.tools .meta');
    var metaText = meta ? flat(meta.textContent) : '(无)';
    ck('D5 翻页后到达的工具结果仍能收口（不再「执行中」）',
       metaText.indexOf('执行中') < 0 && metaText.indexOf('3ms') >= 0, metaText);

    emit({ type: 'stream/delta', params: { text: '第四问的回答。' } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'stop' } });
    await sleep(400);
    ck('D6 收尾后这一轮被折成 1 个组（历史里没有第二个组）',
       convo().querySelectorAll('.turn-proc').length === 1,
       '组=' + convo().querySelectorAll('.turn-proc').length);
    ck('D7 收尾后提问与回答都在',
       countUser('第四问') === 1 && has('第四问的回答。'),
       'user=' + countUser('第四问'));

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>repro-hist-live</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__TAIL=${JSON.stringify(TAIL)};window.__OLDER=${JSON.stringify(OLDER)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-histlive');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'repro-hist-live.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], {
  encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore']
});
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
const t = m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : 'NO RESULT';
console.log(t);
process.exit(/fail=0/.test(t) ? 0 : 2);
