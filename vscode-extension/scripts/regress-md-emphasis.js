/**
 * 专项排查：行内斜体/删除线正则的「误配对」风险。
 *
 * 害怕的是这个：`*` 在正文里不只用来做斜体，还用来做乘法、做列表星号、
 * 写指针/通配符。若正则不检查分隔符两侧是否贴着内容（CommonMark 的
 * left/right-flanking 规则），就会把两段本来无关的文字配成一对斜体：
 *   `a * b * c`  →  `a <em> b </em> c`
 * 而改之前这些是原样显示的（因为那时候根本没有斜体）。
 * 这就是**回归**：不是"新功能不好"，是把原来对的显示改坏了。
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

// 每条：[输入, 是否应该出现 <em>/<del>, 说明]
const CASES = [
  ['**粗体** 正常',            'strong', '粗体基线'],
  ['正文 *真斜体* 结束',        'em',     '两侧贴内容 → 应该斜体'],
  ['正文 * 空白不是斜体 * 结束', 'none',   '星号后紧跟空格 → 不该斜体（乘法/列表）'],
  ['a * b * c',               'none',   '乘法/占位，不该斜体'],
  ['文件A * 文件B * 文件C',     'none',   '中文列举，不该斜体'],
  ['x ~~ 分隔 ~~ y',          'none',   '波浪线两侧空白，不该删除线'],
  ['两点~~中间~~三点',          'del',    '贴内容 → 应该删除线'],
  ['snake_case_name 保留',     'keep',   '标识符不被吃'],
  ['C 指针 char* p 保留',       'keep',   '单个星号不配对'],
  ['2*3*4 保留',               'keep',   '算式不被吃'],
  ['通配符 *.log 保留',         'keep',   '**只一个**星号，不该配对'],
  // 嵌套：粗体里套斜体。原实现 `**([^*\n]+)**` 内部一律不许有星号，
  // 所以整条都不匹配、连外层 `**` 都原样露出（截图确认过）。
  ['**粗里有 *斜* 吗** 边界',    'strongAndEm', '外层该粗、内层该斜'],
  ['**a** 与 **b** 两个粗体',    'strongTwo',   '两个独立粗体不该串配']
];

const TEST = `
(function(){
  var CASES = ${JSON.stringify(CASES)};
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'E:\\\\x' } });
    await sleep(120);

    for (var i = 0; i < CASES.length; i++) {
      var c = CASES[i];
      var before = document.querySelectorAll('.msg').length;
      emit({ type: 'turn/begin', params: { round: i + 1 } });
      await sleep(10);
      emit({ type: 'stream/delta', params: { text: c[0] } });
      await sleep(10);
      emit({ type: 'turn/end', params: { reason: 'done' } });
      await sleep(260);
      var msgs = document.querySelectorAll('.msg');
      var box = msgs[msgs.length - 1];
      var nEm = box ? box.querySelectorAll('em').length : 0;
      var nDel = box ? box.querySelectorAll('del').length : 0;
      var txt = box ? box.textContent : '(无)';
      var want = c[1];
      var ok, detail;
      if (want === 'em')   { ok = nEm >= 1; detail = 'em=' + nEm; }
      else if (want === 'del')  { ok = nDel >= 1; detail = 'del=' + nDel; }
      else if (want === 'strong') {
        var nSt = box ? box.querySelectorAll('strong').length : 0;
        ok = nSt >= 1;
        detail = 'strong=' + nSt;
      }
      else if (want === 'none') { ok = (nEm === 0 && nDel === 0); detail = 'em=' + nEm + ' del=' + nDel; }
      else if (want === 'strongAndEm') {
        var sN = box ? box.querySelectorAll('strong').length : 0;
        ok = sN >= 1 && nEm >= 1 && txt.indexOf('**') < 0;
        detail = 'strong=' + sN + ' em=' + nEm + ' 原文=' + JSON.stringify(txt.slice(0, 30));
      }
      else if (want === 'strongTwo') {
        var s2 = box ? box.querySelectorAll('strong').length : 0;
        ok = s2 === 2 && txt.indexOf('**') < 0;
        detail = 'strong=' + s2 + ' 原文=' + JSON.stringify(txt.slice(0, 30));
      }
      else { // keep：原文必须一字不差地保留关键片段
        var key = c[0].split(' ')[0];
        ok = txt.indexOf(key) >= 0 && nEm === 0;
        detail = '文本=' + JSON.stringify(txt.slice(0, 40)) + ' em=' + nEm;
      }
      ck('R' + (i + 1) + ' ' + JSON.stringify(c[0]) + ' → ' + want, ok, detail);
    }

    out.push('SUMMARY total=' + out.length + ' fail=' + fails);
    var pre = document.createElement('pre');
    pre.id = 'RESULT';
    pre.textContent = out.join('\\n');
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>regress-md</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-regress-md');
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
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无结果)';
console.log(text);
process.exit(/\bfail=[1-9]/.test(text) ? 1 : 0);
