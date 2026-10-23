/**
 * 精确定位「粘贴后按钮仍是灰的」。
 *
 * 假设：renderFoot() 建按钮时硬编码 disabled=true，且结尾**不**调 updateMdFoot。
 * 而 renderFoot 在每次 tab 切换 / describe 回执 / narnatmd 回执时都会跑 ——
 * 于是任何一次重绘都把按钮打回灰色，直到用户再敲一次键盘。
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

const DESC = {
  restartPending: false,
  groups: [{ id: 'api', label: '模型 API' }, { id: 'prompt', label: '提示词' }],
  items: [
    { id: 'api.baseUrl', group: 'api', label: '接口地址', kind: 'text',
      value: 'https://x', writable: true },
    { id: 'narnatmd', group: 'prompt', label: 'narnat.md', kind: 'markdown',
      value: '', path: 'D:\\narnat.md', writable: true }
  ]
};
const ORIGINAL = '# 原内容\n';

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  :: ' + d : ''));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function btn(){ return document.querySelector('#setFoot [data-act="saveMd"]'); }
  function tip(){ var t = document.querySelector('#setFoot [data-act="mdTip"]'); return t ? t.textContent : '(无)'; }
  function st(){ var b = btn(); return b ? (b.disabled ? '灰' : '可点') + '/' + tip() : '不存在'; }
  function tabByText(s){
    var bs = document.querySelectorAll('#setTabs button');
    for (var i = 0; i < bs.length; i++) {
      if ((bs[i].textContent || '').indexOf(s) >= 0) return bs[i];
    }
    return null;
  }
  function typeInto(el, text){
    el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(60);
    document.getElementById('btnSettings').click();
    await sleep(40);
    emit({ type: 'config/describe', params: window.__DESC });
    await sleep(100);
    emit({ type: 'narnatmd/result', params: { path: 'x', text: window.__ORIG, exists: true } });
    await sleep(120);

    var pt = tabByText('提示词');
    if (pt) { pt.click(); await sleep(120); }

    var ta = document.getElementById('setMd');
    ck('进入提示词分组后 textarea 存在', !!ta, ta ? 'ok' : '无');
    if (!ta) { finish(); return; }

    /* ── 场景 A：粘贴后立刻看按钮 ── */
    typeInto(ta, '# 粘贴的内容 A\n');
    await sleep(60);
    ck('A 粘贴后按钮可点', btn() && btn().disabled === false, st());

    /* ── 场景 B：粘贴后点一下别的 tab 再回来（用户常见动作）── */
    var apiTab = tabByText('模型');
    if (apiTab) { apiTab.click(); await sleep(80); }
    if (pt) { pt.click(); await sleep(120); }
    var ta2 = document.getElementById('setMd');
    out.push('DIAG B 切回后 textarea=' + JSON.stringify((ta2 ? ta2.value : '').slice(0, 20)) + ' 按钮=' + st());
    ck('B 切 tab 回来后，未保存的内容仍在', ta2 && ta2.value.indexOf('粘贴的内容 A') >= 0,
       ta2 ? JSON.stringify(ta2.value.slice(0, 20)) : '无');
    ck('B 切 tab 回来后，按钮应该仍是「可点」（内容确实没保存）',
       btn() && btn().disabled === false, st());

    /* ── 场景 C：粘贴后再收一次 describe 回执（改别的设置就会触发）── */
    if (ta2) { typeInto(ta2, '# 粘贴的内容 C\n'); }
    await sleep(60);
    emit({ type: 'config/describe', params: window.__DESC });
    await sleep(120);
    var ta3 = document.getElementById('setMd');
    out.push('DIAG C describe 回执后 textarea=' +
             JSON.stringify((ta3 ? ta3.value : '').slice(0, 20)) + ' 按钮=' + st());
    ck('C describe 回执后内容仍在', ta3 && ta3.value.indexOf('粘贴的内容 C') >= 0,
       ta3 ? JSON.stringify(ta3.value.slice(0, 20)) : '无');
    ck('C describe 回执后按钮应该仍「可点」', btn() && btn().disabled === false, st());

    /* ── 场景 D：粘贴后再收一次 narnatmd 回执 ── */
    emit({ type: 'narnatmd/result', params: { path: 'x', text: window.__ORIG, exists: true } });
    await sleep(140);
    var ta4 = document.getElementById('setMd');
    out.push('DIAG D narnatmd 回执后 textarea=' +
             JSON.stringify((ta4 ? ta4.value : '').slice(0, 20)) + ' 按钮=' + st());
    ck('D narnatmd 回执后内容没被服务器值覆盖',
       ta4 && ta4.value.indexOf('粘贴的内容 C') >= 0,
       ta4 ? JSON.stringify(ta4.value.slice(0, 20)) : '无');
    ck('D narnatmd 回执后按钮应该仍「可点」', btn() && btn().disabled === false, st());

    /* ── 场景 E：全屏层保存按钮同样要联动 ── */
    var bigBtn = tabByText('提示词');  // 重新确认在提示词分组
    if (bigBtn) { bigBtn.click(); await sleep(100); }
    var full = document.getElementById('mdFullSave');
    if (full) {
      out.push('DIAG E 全屏保存按钮=' + (full.disabled ? '灰' : '可点'));
      ck('E 全屏层保存按钮也应可点', full.disabled === false,
         full.disabled ? '灰' : '可点');
    } else {
      ck('E 全屏层保存按钮存在', false, '未找到 #mdFullSave');
    }

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>repro-md2</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__DESC=${JSON.stringify(DESC)};window.__ORIG=${JSON.stringify(ORIGINAL)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-md2');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'repro-md2.html');
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
