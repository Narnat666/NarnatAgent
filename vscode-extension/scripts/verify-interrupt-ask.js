/**
 * 验收这一轮的三处改动（用户反馈）：
 *   1. 对话记录 / 设置的「回到对话」× 太小看不见 → 放大（28×28 / 18px / 默认淡底）
 *   2. 正在回答中切会话 / 新建对话 → 先二次确认（取消就什么都不做）
 *   3. 用户气泡底色掺强调色（原来与编辑器底几乎同亮度，扫一眼没层次）
 *
 * 断言都可计算：
 *   X1 × 的点击热区 ≥ 28×28（原来 22×22 / 设置 26×26）
 *   X2 × 的默认色不是 --fg-faint（80% 前景色 = 看不见）
 *   X3 跑着的时候点别的会话 → 弹确认，且**没有**发 sessionSwitch
 *   X4 点「取消」→ 仍然没发，pendingSwitchTo 未被占用（能再点别的会话）
 *   X5 点确认 → 发出 sessionSwitch 且名字正确
 *   X6 不跑的时候点会话 → 直接切，不弹确认
 *   X7 跑着的时候点「新建对话」→ 弹确认、不发 sessionNew
 *   X8 气泡底色与页面底有可测差异（亮度差 ≥ 2%），且不是纯中性灰（带色相）
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const REPO = path.resolve(ROOT, '..');
const html = fs.readFileSync(path.join(ROOT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(ROOT, 'media', 'narnat.js'), 'utf8');

const py = 'import json,sys;sys.path.insert(0,".");'
  + 'from narnat_agent.config.loader import normalize_ui_raw;'
  + 'from narnat_agent.ui.theme import snapshot;'
  + 'cfg=json.load(open(".narnat/config/narnat.json",encoding="utf-8-sig"));'
  + 'print(json.dumps(snapshot(normalize_ui_raw(cfg.get("界面") or {})),ensure_ascii=False))';
const THEME = execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim();

const TEST = String.raw`
(function(){
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + String(d)));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function $(s){ return document.querySelector(s); }
  function input(){ return document.getElementById('input'); }
  function setVal(v){
    input().value = v;
    input().dispatchEvent(new Event('input', { bubbles: true }));
  }
  function sent(type){
    return (window.__sent || []).filter(function(m){
      return m && (m.type === type || m.method === type);
    }).length;
  }
  function askOpen(){ return document.getElementById('askDlg').getAttribute('data-open') === 'true'; }
  function rgb(s){
    var m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (m) {
      var p = m[1].split(',').map(function(x){ return parseFloat(x); });
      if (p.length >= 4 && !p[3]) return null;
      return p.slice(0, 3);
    }
    var c = /color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)/.exec(s || '');
    if (c) return [parseFloat(c[1]) * 255, parseFloat(c[2]) * 255, parseFloat(c[3]) * 255];
    return null;
  }
  function lum(c){
    var a = c.map(function(v){ v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
  }
  function hex(c){ return '#' + c.map(function(v){ return ('0' + Math.round(v).toString(16)).slice(-2); }).join(''); }
  /* 色相跨度（0~180）：纯中性灰三通道相等 → 0；带色相 → 明显大于 0 */
  function chroma(c){
    return Math.max.apply(null, c) - Math.min.apply(null, c);
  }

  async function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: '甲', messages: [] },
      workdir: '<t>', theme: window.__THEME } });
    await sleep(200);
    emit({ type: 'session/list', params: { current: '甲', groups: [
      { label: '今天', sessions: [
        { name: '甲', timestamp: 2, children: [] },
        { name: '乙', timestamp: 1 }
      ] }
    ] } });
    await sleep(250);
  }
  /* 让「有一条会话 + 抽屉打开」，方便点行 */
  async function openDrawer(){
    document.getElementById('sessBtn').click();
    await sleep(200);
  }

  /* 打开抽屉（入口是顶栏最左的 ☰ #histBtn，不是会话胶囊 #sessBtn） */
  async function openDrawer(){
    document.getElementById('histBtn').click();
    await sleep(220);
  }

  async function run(){
    await boot();

    /* ---- X3~X6 切会话的二次确认 ---- */
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: '我正在回答，随便说点什么。' } });
    await sleep(120);
    ck('X0 现在处于「回答中」', document.body.getAttribute('data-run') === 'true',
       'run=' + document.body.getAttribute('data-run') +
       ' offline=' + document.body.getAttribute('data-offline'));

    /* ---- X1 / X2 关闭按钮（要在对应面板打开时才量得到热区） ---- */
    await openDrawer();
    var dw = document.getElementById('dwClose');
    var rb = dw.getBoundingClientRect();
    out.push('DIAG 对话记录 × 热区=' + Math.round(rb.width) + '×' + Math.round(rb.height));
    ck('X1 对话记录的 × 热区 ≥ 28×28', rb.width >= 27.5 && rb.height >= 27.5,
       Math.round(rb.width) + '×' + Math.round(rb.height));
    var cs = getComputedStyle(dw);
    out.push('DIAG × 颜色=' + cs.color + ' 底色=' + cs.backgroundColor);
    // 默认底色必须可见（原来是 transparent）
    var bgs = cs.backgroundColor;
    ck('X2 × 默认有可见底色（不是 transparent）',
       bgs !== 'rgba(0, 0, 0, 0)' && bgs !== 'transparent', bgs);
    // 字形必须比 faint 档更实：faint 是前景色 80%，dim 是 92%
    var alpha = /\/ ([\d.]+)\)/.exec(cs.color);
    var a = alpha ? parseFloat(alpha[1]) : 1;
    ck('X2b × 的字形不是 faint 档（alpha ≥ 0.9）', a >= 0.9, String(a));

    // 设置面板的 ×
    document.getElementById('btnSettings').click();
    await sleep(250);
    var st = document.getElementById('setClose');
    var rs = st.getBoundingClientRect();
    out.push('DIAG 设置 × 热区=' + Math.round(rs.width) + '×' + Math.round(rs.height));
    ck('X1b 设置的 × 热区 ≥ 28×28', rs.width >= 27.5 && rs.height >= 27.5,
       Math.round(rs.width) + '×' + Math.round(rs.height));
    st.click();
    await sleep(200);
    await openDrawer();
    var rows = [].slice.call(document.querySelectorAll('#sessList .dw-item'));
    out.push('DIAG 抽屉行数=' + rows.length);
    // 找「乙」那一行点进去
    var rowB = rows.filter(function(r){ return r.textContent.indexOf('乙') >= 0; })[0];
    ck('X3a 抽屉里有目标会话「乙」', !!rowB);
    if (!rowB) { return finish(); }
    var before = sent('sessionSwitch');
    rowB.click();
    await sleep(250);
    ck('X3 回答中点别的会话 → 弹二次确认', askOpen(), 'open=' + askOpen());
    ck('X3b 确认框出现时**没有**发 sessionSwitch',
       sent('sessionSwitch') === before, 'sent=' + sent('sessionSwitch'));
    var title = document.getElementById('askTitle').textContent;
    out.push('DIAG 确认标题=' + JSON.stringify(title));
    ck('X3c 确认标题点名了目标会话', title.indexOf('乙') >= 0, title);

    // 取消
    document.getElementById('askCancel').click();
    await sleep(200);
    ck('X4 点取消后确认框关闭', !askOpen());
    ck('X4b 取消后仍然没发 sessionSwitch',
       sent('sessionSwitch') === before, 'sent=' + sent('sessionSwitch'));

    // 再点一次、这次确认
    rows = [].slice.call(document.querySelectorAll('#sessList .dw-item'));
    rowB = rows.filter(function(r){ return r.textContent.indexOf('乙') >= 0; })[0];
    rowB.click();
    await sleep(200);
    ck('X5a 取消后能再次弹出确认（pending 没被占用）', askOpen());
    document.getElementById('askOk').click();
    await sleep(250);
    ck('X5 点确认后真的发出 sessionSwitch',
       sent('sessionSwitch') === before + 1, 'sent=' + sent('sessionSwitch'));
    ck('X5b 确认后确认框已关闭', !askOpen());

    // 收尾这一轮（后端实际会 cancel，这里手动收口）
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(300);

    /* ---- X6 不跑的时候不弹确认 ---- */
    await openDrawer();
    rows = [].slice.call(document.querySelectorAll('#sessList .dw-item'));
    var rowA = rows.filter(function(r){ return r.textContent.indexOf('甲') >= 0; })[0];
    var before2 = sent('sessionSwitch');
    if (rowA) rowA.click();
    await sleep(250);
    ck('X6 不回答时切会话 → 不弹确认、直接发请求',
       !askOpen() && sent('sessionSwitch') === before2 + 1,
       'open=' + askOpen() + ' sent=' + sent('sessionSwitch'));
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '甲', messages: [] } });
    await sleep(300);

    /* ---- X7 新建对话 ---- */
    emit({ type: 'turn/begin', params: { round: 2 } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: '又在回答。' } });
    await sleep(120);
    await openDrawer();
    var beforeNew = sent('sessionNew');
    document.getElementById('newChatBtn').click();
    await sleep(250);
    ck('X7 回答中点「新建对话」→ 弹确认', askOpen(), 'open=' + askOpen());
    ck('X7b 此时没有发 sessionNew',
       sent('sessionNew') === beforeNew, 'sent=' + sent('sessionNew'));
    document.getElementById('askCancel').click();
    await sleep(150);
    ck('X7c 取消后没发 sessionNew', sent('sessionNew') === beforeNew);

    /* ---- X8 气泡底色 ---- */
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(250);
    document.getElementById('dwClose').click();
    await sleep(150);
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '乙',
      messages: [{ role: 'user', text: '这是我说的一句话' },
                 { role: 'assistant', text: '收到了' }] } });
    await sleep(500);
    var bubble = document.querySelector('.msg[data-role="user"] .text');
    ck('X8a 画出了用户气泡', !!bubble);
    if (bubble) {
      var bBg = rgb(getComputedStyle(bubble).backgroundColor);
      var pBg = rgb(getComputedStyle(document.body).backgroundColor);
      out.push('DIAG 气泡底=' + (bBg ? hex(bBg) : '?') + ' 页面底=' + (pBg ? hex(pBg) : '?') +
        ' 色度差=' + (bBg ? chroma(bBg) : '?'));
      ck('X8 气泡底与页面底有可测差异（≥2% 亮度）',
         !!bBg && !!pBg && Math.abs(lum(bBg) - lum(pBg)) >= 0.02,
         bBg ? Math.abs(lum(bBg) - lum(pBg)).toFixed(3) : '(取不到)');
      // 掺强调色后三通道不该再相等（One Dark 的 #2c313c 是纯中性灰）
      ck('X8b 气泡底带色相（不是纯中性灰，chroma ≥ 3）',
         !!bBg && chroma(bBg) >= 3, bBg ? String(chroma(bBg)) : '(取不到)');
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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-interrupt-ask</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-vrfy-ask');
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
