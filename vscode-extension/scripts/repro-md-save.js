/**
 * 复现「粘贴进去保存按钮还是灰的」。
 *
 * 模拟真实链路：initialize → config/describe → narnatmd/result
 * → 切到「提示词」分组 → 往 textarea 粘贴 → 看保存按钮 disabled
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
  groups: [
    { id: 'api', label: '模型 API' },
    { id: 'websearch', label: '联网工具' },
    { id: 'prompt', label: '提示词' }
  ],
  items: [
    { id: 'narnatmd', group: 'prompt', label: 'narnat.md', kind: 'markdown',
      value: '', path: 'D:\\.narnat\\config\\narnat.md', writable: true }
  ]
};
const ORIGINAL = '# 原内容\n这是已有的提示词。\n';

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  :: ' + d : ''));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function saveBtn(){ return document.querySelector('#setFoot [data-act="saveMd"]'); }
  function tip(){ var t = document.querySelector('#setFoot [data-act="mdTip"]'); return t ? t.textContent : '(无)'; }
  function ta(){ return document.getElementById('setMd'); }
  function state(){
    var b = saveBtn();
    return 'btn=' + (b ? (b.disabled ? '灰色' : '可点') : '不存在') + ' tip=' + JSON.stringify(tip());
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
    await sleep(120);
    emit({ type: 'narnatmd/result', params: {
      path: 'D:\\.narnat\\config\\narnat.md', text: window.__ORIG, exists: true } });
    await sleep(150);

    // 切到「提示词」分组（第 3 个 tab）
    var tabs = document.querySelectorAll('#setTabs button');
    out.push('DIAG tab 数量=' + tabs.length +
             ' 文本=' + [].map.call(tabs, function(b){ return b.textContent; }).join('|'));
    var pt = null;
    for (var i = 0; i < tabs.length; i++) {
      if ((tabs[i].textContent || '').indexOf('提示词') >= 0) { pt = tabs[i]; break; }
    }
    ck('找到「提示词」分组', !!pt, tabs.length + ' 个 tab');
    if (pt) { pt.click(); await sleep(150); }

    var t0 = ta();
    ck('textarea 存在', !!t0, t0 ? 'ok' : '(无)');
    if (!t0) { finish(); return; }
    ck('textarea 显示原文', t0.value.indexOf('原内容') >= 0,
       JSON.stringify(t0.value.slice(0, 30)));
    out.push('DIAG 初始 ' + state());
    ck('初始：未改动时保存按钮是灰的（正确）',
       saveBtn() && saveBtn().disabled === true, state());

    /* ── 模拟粘贴（input 事件，浏览器粘贴必然触发）── */
    var pasted = '# 新内容\n粘进来的提示词。\n';
    t0.value = pasted;
    t0.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(80);
    out.push('DIAG 粘贴后 ' + state());
    ck('粘贴后保存按钮应可点', saveBtn() && saveBtn().disabled === false, state());
    ck('粘贴后提示语变「有未保存的修改」', tip() === '有未保存的修改', JSON.stringify(tip()));

    /* ── 直接改 value 但不发 input（模拟某些程序化路径）── */
    emit({ type: 'narnatmd/result', params: {
      path: 'D:\\.narnat\\config\\narnat.md', text: window.__ORIG, exists: true } });
    await sleep(120);
    out.push('DIAG 再收一次 narnatmd/result 后 ' + state());

    /* ── 切走再切回 ‑ 内容是否还在、按钮状态 ── */
    var apiTab = null;
    for (var j = 0; j < tabs.length; j++) {
      if ((tabs[j].textContent || '').indexOf('模型') >= 0) { apiTab = tabs[j]; break; }
    }
    if (apiTab) { apiTab.click(); await sleep(100); }
    out.push('DIAG 切到模型分组后 saveBtn=' + (saveBtn() ? '在' : '不在'));
    if (pt) { pt.click(); await sleep(150); }
    var t1 = ta();
    out.push('DIAG 切回提示词 ' + state());
    ck('切回后 textarea 内容未被清空', t1 && t1.value.length > 0,
       t1 ? JSON.stringify(t1.value.slice(0, 20)) : '(无)');

    /* ── 点保存，看发出去的载荷 ── */
    if (t1) {
      t1.value = pasted;
      t1.dispatchEvent(new Event('input', { bubbles: true }));
      await sleep(50);
      window.__sent.length = 0;
      var b = saveBtn();
      if (b && !b.disabled) {
        b.click();
        await sleep(80);
      }
      var sent = (window.__sent || []).filter(function(x){ return x.type === 'narnatmdSet'; });
      ck('点保存发出了 narnatmdSet', sent.length === 1, 'got=' + sent.length);
      if (sent.length) {
        ck('载荷是粘贴的内容', sent[0].params.text === pasted,
           JSON.stringify(String(sent[0].params.text).slice(0, 30)));
      }
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>repro-md</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__DESC=${JSON.stringify(DESC)};window.__ORIG=${JSON.stringify(ORIGINAL)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-md');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'repro-md.html');
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
