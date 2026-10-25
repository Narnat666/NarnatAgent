/**
 * 验收 P1-1：引用块里的围栏代码块要真正渲染成 .code + <pre>。
 * 修复前：blockquote 内 pre=0、code=0，反引号原样留在句子里。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = 'E:\\桌面\\narnat\\NarnatAgent\\vscode-extension';
const html = fs.readFileSync(path.join(ROOT, 'design', 'ui-full.html'), 'utf8');
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
  var BQ = String.fromCharCode(62);            // 引用前缀字符
  var BT = String.fromCharCode(96,96,96);      // 三个反引号（围栏）
  /* 注意：上面这两行必须用 fromCharCode。
     直接在 String.raw 模板里写三个反引号会**提前闭合模板字符串**，
     整个脚本报 SyntaxError（本次踩过）。 */

  async function run(){
    emit({ type:'initialize', params:{ version:'t', protocol:1,
      model:{current:'m',options:['m']}, thinking:{effort:'high',options:{high:'高'}},
      goal:{enabled:false,maxRounds:10}, plugins:[], session:{current:null}, workdir:'<t>' } });
    await sleep(150);

    /* 用例 1：引用 + 围栏代码块（AI 引用示例的常态写法） */
    var msg1 =
      '看这个引用：' + '\n\n' +
      BQ + ' ' + BT + 'js' + '\n' +
      BQ + ' const a = 1;' + '\n' +
      BQ + ' ' + BT + '\n\n' +
      '上面就是。';
    emit({ type:'session/result', params:{ action:'sessionSwitch', current:'q1',
      messages: [ { role:'assistant', text: msg1 } ] } });
    await sleep(800);

    var m = convo().querySelector('.msg');
    var bq = m ? m.querySelector('blockquote') : null;
    ck('A1 引用块本身渲染了', !!bq);
    var preIn = bq ? bq.querySelectorAll('pre').length : 0;
    var preTop = m ? m.querySelectorAll('pre').length : 0;
    out.push('DIAG blockquote 内 pre=' + preIn + ' 全文 pre=' + preTop);
    ck('A2 引用里的代码块渲染成 <pre>（不是裸反引号）', preIn === 1, 'blockquote 内 pre=' + preIn);
    ck('A3 没有多出一个游离在引用外的代码块', preTop === 1, '全文 pre=' + preTop);

    var codeWrap = bq ? bq.querySelector('.code') : null;
    ck('A4 有语言头（js）', !!codeWrap && /js/i.test(String(codeWrap.textContent)),
      codeWrap ? JSON.stringify(String(codeWrap.textContent).slice(0, 40)) : '(无 .code)');
    /* 复制按钮的类名不是 .cp/.copy —— 按**按钮文字**判，别猜类名。 */
    var btns = codeWrap ? codeWrap.querySelectorAll('button') : [];
    var btnTexts = [];
    for (var bi = 0; bi < btns.length; bi++) btnTexts.push(String(btns[bi].textContent).trim());
    out.push('DIAG 代码块按钮=' + JSON.stringify(btnTexts));
    ck('A5 有复制按钮', btnTexts.join('|').indexOf('复制') >= 0, JSON.stringify(btnTexts));

    var pre = bq ? bq.querySelector('pre') : null;
    var preText = pre ? String(pre.textContent) : '';
    out.push('DIAG 代码内容=' + JSON.stringify(preText));
    ck('A6 代码内容里没有引用前缀（> 已剥掉）', preText.indexOf(BQ) < 0, JSON.stringify(preText));
    ck('A7 代码内容里没有反引号围栏标记', preText.indexOf(BT) < 0, JSON.stringify(preText));
    ck('A8 代码内容正确', /const a = 1;/.test(preText), JSON.stringify(preText));

    /* 用例 2：引用里的普通文字仍要按段落渲染，不能塌成一行 */
    emit({ type:'session/result', params:{ action:'sessionSwitch', current:'q2',
      messages: [ { role:'assistant', text:
        BQ + ' 第一段。' + '\n' + BQ + ' 第二段。' + '\n\n' + '后面正文。' } ] } });
    await sleep(700);
    var m2 = convo().querySelector('.msg');
    var bq2 = m2 ? m2.querySelector('blockquote') : null;
    var bqText = bq2 ? String(bq2.textContent) : '';
    out.push('DIAG 引用文字=' + JSON.stringify(bqText));
    ck('B1 引用里的两段文字都在', bqText.indexOf('第一段') >= 0 && bqText.indexOf('第二段') >= 0);
    ck('B2 引用文字里没有多余的 > 前缀', bqText.indexOf(BQ) < 0, JSON.stringify(bqText));

    /* 用例 3：嵌套引用（> > x）→ 两层 blockquote */
    emit({ type:'session/result', params:{ action:'sessionSwitch', current:'q3',
      messages: [ { role:'assistant', text: BQ + ' ' + BQ + ' 深层内容。' } ] } });
    await sleep(700);
    var m3 = convo().querySelector('.msg');
    var bqOuter = m3 ? m3.querySelector('blockquote') : null;
    var bqInner = bqOuter ? bqOuter.querySelector('blockquote') : null;
    out.push('DIAG 嵌套 blockquote 层数=' + (bqInner ? 2 : (bqOuter ? 1 : 0)));
    ck('C1 嵌套引用渲染成两层 blockquote', !!bqInner);
    ck('C2 深层文字在', bqInner ? String(bqInner.textContent).indexOf('深层内容') >= 0 : false);

    /* 用例 4：回归——顶层围栏仍然正常 */
    emit({ type:'session/result', params:{ action:'sessionSwitch', current:'q4',
      messages: [ { role:'assistant', text: '正文\n\n' + BT + 'py\nx = 1\n' + BT + '\n\n结束' } ] } });
    await sleep(700);
    var m4 = convo().querySelector('.msg');
    ck('D1 顶层围栏代码块不受影响', m4 ? m4.querySelectorAll('.code').length === 1 : false,
      m4 ? 'code 数=' + m4.querySelectorAll('.code').length : '(无)');
    ck('D2 顶层围栏不被包进 blockquote', m4 ? !m4.querySelector('blockquote') : false);

    var preO = document.createElement('pre'); preO.id='RESULT';
    preO.textContent = out.join('\n') + '\nSUMMARY total=' + (out.length + 1) + ' fail=' + fails;
    document.body.appendChild(preO);
  }
  window.addEventListener('load', function(){
    run().catch(function(e){
      var p = document.createElement('pre'); p.id='ERR';
      p.textContent = 'run 抛错: ' + (e && e.stack ? e.stack : e);
      document.body.appendChild(p);
    });
  });
})();
`;

const page = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>q</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'verify-quote-fence');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'a.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=20000', '--dump-dom',
  '--window-size=760,900', 'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const mm = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
const ee = /<pre id="ERR">([\s\S]*?)<\/pre>/.exec(dom);
if (ee) { console.log('页面抛错:', ee[1]); process.exit(2); }
if (mm) console.log(mm[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
