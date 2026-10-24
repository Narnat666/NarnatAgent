/**
 * 验收围栏代码块解析（用户报障截图：AI 输出的 markdown 文档碎成三块，
 * json 正文漏成裸文本）。
 *
 * 真实复现文本取自 `.narnat/data/sessions/你好，直接输出一个mkdown…json`
 * 的第 5 条 assistant content —— AI 写的是 ```markdown 外层套 ```json 内层
 * （3 包 3）。这在 CommonMark 里是**非法嵌套**：闭合围栏必须是「行首 +
 * 纯反引号 + 其后只有空白」，所以 ```json 这一行是内容、不是闭合标记。
 *
 * dsh 用的就是 CommonMark/GFM 解析器（`ui-primitives/src/markdown/parse.ts`
 * 的 micromark + mdast），它的结果就是：外层块一直延续到 json 那个 ``` 为止，
 * 之后的 `**状态码**` 回到正文。本脚本按这个口径断言。
 *
 * 老实现把闭合分支写成 `\n```"`（不要求行尾），于是 ```json 提前闭合外层，
 * 后面越切越碎 —— 那才是用户看到的形态。
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

const fails = [];
const ck = (n, ok, d) => {
  console.log((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  if (!ok) fails.push(n);
};

/* ---- 真实文本：直接读会话 json，不手抄 ---- */
const REPO = path.resolve(ROOT, '..');
const REAL = (() => {
  const dir = path.join(REPO, '.narnat/data/sessions');
  const f = fs.readdirSync(dir).find((n) => n.indexOf('mkdown') >= 0);
  if (!f) return null;
  const d = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8').replace(/^\uFEFF/, ''));
  const m = d.messages.filter((x) => x.role === 'assistant' && /```markdown/.test(x.content || ''))[0];
  return m ? m.content : null;
})();
if (!REAL) { console.log('FAIL 找不到真实样本（会话 json 已被清理）'); process.exit(1); }
console.log('DIAG 真实样本长度=' + REAL.length + ' 字符');

/* 最小可复现：3 包 3（非法嵌套，AI 的常态写法） */
const CASE_NEST3 = [
  '```markdown', 'A 段', '', '```json', '{"k":1}', '```', '', '**尾巴**', '- `200` 成功', '```', ''
].join('\n');
/* 合法嵌套：4 包 3 */
const CASE_NEST4 = [
  '````markdown', 'A 段', '', '```json', '{"k":1}', '```', '````', ''
].join('\n');
/* 两段独立代码块 */
const CASE_TWO = ['前', '', '```js', 'var a=1;', '```', '', '中', '', '```py', 'b=2', '```', '后', ''].join('\n');
/* 代码正文里出现三个反引号的字面量（不能当闭合） */
const CASE_LITERAL = ['```js', 'var s = "```";', 'var t = 1;', '```', '尾巴', ''].join('\n');
/* 4 开 3 闭（长度不足，闭合不成立） */
const CASE_SHORT = ['````md', 'line', '```', 'still code', '````', '尾巴', ''].join('\n');
/* 缩进 4 空格的围栏不是围栏（是缩进代码块） */
const CASE_INDENT = ['    ```js', '    x', '    ```', ''].join('\n');
/* 未闭合（流式截断） */
const CASE_OPEN = ['```js', 'var a = 1;', 'var b = 2;', ''].join('\n');

const CASES = [
  { k: 'REAL', text: REAL },
  { k: 'NEST3', text: CASE_NEST3 },
  { k: 'NEST4', text: CASE_NEST4 },
  { k: 'TWO', text: CASE_TWO },
  { k: 'LITERAL', text: CASE_LITERAL },
  { k: 'SHORT', text: CASE_SHORT },
  { k: 'INDENT', text: CASE_INDENT },
  { k: 'OPEN', text: CASE_OPEN },
];

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
  var CASES = window.__CASES;
  /* 反引号在 String.raw 模板里会结束模板，只能用字符码拼出来 */
  var F3 = String.fromCharCode(96, 96, 96);
  var F4 = F3 + String.fromCharCode(96);
  /* 每一种 case 走一遍「新提问 → 流式 → 定稿」，然后只看这一轮的正文节点。
     这样走的是真实渲染路径（流式 markdownInline(text,true) + 定稿 false），
     不是把解析函数单独拎出来测。 */
  async function round(i){
    emit({ type: 'turn/begin', params: { round: i + 1 } });
    await sleep(10);
    emit({ type: 'stream/delta', params: { text: CASES[i].text } });
    await sleep(25);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(40);
    var msgs = [].slice.call(convo().querySelectorAll('.msg.assistant'));
    return msgs[msgs.length - 1];
  }
  function codes(host){
    return [].slice.call(host.querySelectorAll('.code pre'));
  }
  function txt(el){ return (el && el.textContent) || ''; }

  async function run(){
    boot();
    await sleep(80);
    var host, pres, loose;

    /* ---- 1. 真实样本 ---- */
    host = await round(0);
    pres = codes(host);
    ck('T1 真实样本：代码块数量 = 1（内层开标记不是闭合标记）', pres.length === 1, 'n=' + pres.length);
    ck('T2 真实样本：该块语言是 markdown', pres.length === 1 && pres[0].getAttribute('data-lang') === 'markdown',
      pres.length === 1 ? pres[0].getAttribute('data-lang') : '-');
    ck('T3 真实样本：json 正文在块内（不再漏成裸文本）',
      pres.length === 1 && txt(pres[0]).indexOf('"code": 0') >= 0);
    // 块外的纯文本里不能出现 json 片段
    loose = [].slice.call(host.querySelectorAll('p')).map(txt).join('\n');
    ck('T4 真实样本：块外没有裸的 json 片段', loose.indexOf('"code": 0') < 0);
    // 3 包 3 的容错口径：整段收在一个块里（含「状态码」这些尾巴），
    // 块外不残留任何正文节点 —— 否则就是用户截图里「碎成三块」的样子。
    ck('T5 真实样本：块外没有任何正文段落残留',
      [].slice.call(host.querySelectorAll('p')).length === 0,
      'p=' + host.querySelectorAll('p').length);
    ck('T6 真实样本：尾巴也进了同一个块（状态码 / 404 都在块内）',
      pres.length === 1 && txt(pres[0]).indexOf('状态码') >= 0 && txt(pres[0]).indexOf('404') >= 0);

    /* ---- 2. 3 包 3 非法嵌套 ---- */
    host = await round(1);
    pres = codes(host);
    ck('T7 3包3：只有一个代码块', pres.length === 1, 'n=' + pres.length);
    ck('T8 3包3：块内含内层开标记（它是内容）',
      pres.length === 1 && txt(pres[0]).indexOf(F3 + 'json') >= 0);
    ck('T9 3包3：块外没有任何正文残留（内层的闭合不结束外层，末尾那行才结束）',
      [].slice.call(host.querySelectorAll('p')).length === 0,
      'p=' + host.querySelectorAll('p').length);
    ck('T9b 3包3：块内含尾巴（**尾巴** 也在同一个块里）',
      pres.length === 1 && txt(pres[0]).indexOf('**尾巴**') >= 0);

    /* ---- 3. 合法 4 包 3 ---- */
    host = await round(2);
    pres = codes(host);
    ck('T10 4包3：一个代码块', pres.length === 1, 'n=' + pres.length);
    ck('T11 4包3：块内含内层开标记与 json 正文',
      pres.length === 1 && txt(pres[0]).indexOf(F3 + 'json') >= 0 && txt(pres[0]).indexOf('"k":1') >= 0);
    ck('T12 4包3：块内不含外层开标记',
      pres.length === 1 && txt(pres[0]).indexOf(F4) < 0);

    /* ---- 4. 两段独立代码块 ---- */
    host = await round(3);
    pres = codes(host);
    ck('T13 两段代码块：数量 = 2', pres.length === 2, 'n=' + pres.length);
    ck('T14 两段代码块：语言分别 js / py',
      pres.length === 2 && pres[0].getAttribute('data-lang') === 'js' &&
      pres[1].getAttribute('data-lang') === 'py');
    ck('T15 两段代码块：中间的「中」仍是正文', txt(host).indexOf('中') >= 0);

    /* ---- 5. 代码正文里的三反引号字面量 ---- */
    host = await round(4);
    pres = codes(host);
    ck('T16 字面量：只有 1 个代码块', pres.length === 1, 'n=' + pres.length);
    ck('T17 字面量：块内含 var t = 1;（没被截断）',
      pres.length === 1 && txt(pres[0]).indexOf('var t = 1;') >= 0);
    ck('T18 字面量：块外的「尾巴」是正文',
      [].slice.call(host.querySelectorAll('p')).some(function(p){ return txt(p).indexOf('尾巴') >= 0; }));

    /* ---- 6. 4 开 3 闭（长度不足） ---- */
    host = await round(5);
    pres = codes(host);
    ck('T19 4开3闭：三个反引号不闭合四反引号，整段算一个块',
      pres.length === 1 && txt(pres[0]).indexOf('still code') >= 0, 'n=' + pres.length);
    ck('T20 4开3闭：块外「尾巴」是正文',
      [].slice.call(host.querySelectorAll('p')).some(function(p){ return txt(p).indexOf('尾巴') >= 0; }));

    /* ---- 7. 缩进 4 空格 ---- */
    host = await round(6);
    pres = codes(host);
    ck('T21 缩进4空格：不当作围栏代码块', pres.length === 0, 'n=' + pres.length);

    /* ---- 8. 未闭合（流式截断） ---- */
    host = await round(7);
    pres = codes(host);
    ck('T22 未闭合：整段仍然是代码块', pres.length === 1, 'n=' + pres.length);
    ck('T23 未闭合：两行代码都在块内',
      pres.length === 1 && txt(pres[0]).indexOf('var a = 1;') >= 0 && txt(pres[0]).indexOf('var b = 2;') >= 0);

    finish();
    function finish(){
      var pre = document.createElement('pre');
      pre.id = 'RESULT';
      out.push('SUMMARY total=' + out.length + ' fail=' + fails);
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-fence</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__CASES=${JSON.stringify(CASES)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-fence');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'verify-fence.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=25000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const e = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (e) { console.log('页面抛错:', e[1]); process.exit(2); }
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
console.log('\nSTATIC fail=' + fails.length);
process.exit(fails.length ? 1 : 0);
