/**
 * 验收「被打断但没标记」的两个场景（用户报障）：
 *   1. 创建探索分支 → 强行进入分支会话 → 再切回原会话时被打断且**没有打断标记**
 *   2. 新会话里「另存为新会话」→ 进入后同样会打断（也没标记）
 *
 * 根因：后端换会话前一定先 cancel 当前轮，于是 `turn/end{reason:'cancelled'}`
 * 与 `session/result` 是**两条独立事件**，顺序不确定：
 *   · session/result 先到 → data-run 还是 true，老的判据能补上（侥幸）
 *   · turn/end 先到 → 它写的「■ 已停止」紧接着被 enterSessionFrom /
 *     resetConversationDom 的清屏抹掉，而此时 data-run 已是 false →
 *     两条判据全失效，屏幕上一个字都不提（用户看到的就是这个）
 *
 * 修法：turn/end(cancelled) 时 `noteTurnCut()` 记时间戳，
 * 各条换会话路径用 `turnRunning() || takeCutNotice()` 判断。
 *
 * 断言（顺序 A = 先 turn/end 后 result，也就是原来漏掉的那一种）：
 *   Y1 探索分支：turn/end → session/result(explore) 之后**有**「已停止上一轮」
 *   Y2 切回原会话：同样有（这是用户描述的操作链最后一步）
 *   Y3 另存为新会话：turn/end → session/saved{switched:true} 之后有标记
 *   Y4 新建对话：turn/end → session/result(sessionNew) 之后有标记
 *   Y5 删当前会话：turn/end → session/result(delete,wasCurrent) 之后有标记
 *   Y6 顺序 B（result 先到）仍然只补一次，不重复
 *   Y7 没被打断时（正常 done 收尾后切会话）**不能**乱报「已停止上一轮」
 *   Y8 新开一轮会清掉记忆（不陈漏到下一轮）
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
  function convo(){ return document.getElementById('convo'); }
  /* 数「已停止上一轮」出现几次（这是唯一判据：屏幕上看得到的证据） */
  function cutCount(){
    return [].slice.call(convo().querySelectorAll('.sys')).filter(function(s){
      return (s.textContent || '').indexOf('已停止上一轮') >= 0;
    }).length;
  }
  function msgs(){
    return [
      { role: 'user', text: '问一句' },
      { role: 'assistant', text: '答一句' }
    ];
  }
  async function boot(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: '甲', messages: [] },
      workdir: '<t>', theme: window.__THEME } });
    await sleep(180);
    emit({ type: 'session/list', params: { current: '甲', groups: [
      { label: '今天', sessions: [
        { name: '甲', timestamp: 2, children: [] },
        { name: '乙', timestamp: 1 }
      ] }
    ] } });
    await sleep(200);
  }
  /* 开一轮并留在「回答中」 */
  async function startTurn(round){
    emit({ type: 'turn/begin', params: { round: round || 1 } });
    await sleep(25);
    emit({ type: 'stream/delta', params: { text: '正在回答第 ' + (round || 1) + ' 轮。' } });
    await sleep(60);
  }
  /* 后端 cancel 造成的收口（顺序 A：先 turn/end 后 session/result） */
  async function cancelThenResult(result){
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(120);
    emit({ type: 'session/result', params: result });
    await sleep(400);
  }
  function clear(){ convo().innerHTML = ''; }

  async function run(){
    await boot();

    /* ---- Y1 探索分支：先 turn/end 再 explore 回执 ---- */
    await startTurn(1);
    await cancelThenResult({ action: 'sessionExplore', current: '甲/分支1',
      messages: msgs() });
    ck('Y1 探索分支：被打断后有「已停止上一轮」（原来一个标记都没有）',
       cutCount() === 1, '出现 ' + cutCount() + ' 次');

    /* ---- Y2 再切回原会话：同样要有 ---- */
    await startTurn(2);
    await cancelThenResult({ action: 'sessionSwitch', current: '甲', messages: msgs() });
    ck('Y2 从分支切回原会话：被打断后有标记', cutCount() === 1,
       '出现 ' + cutCount() + ' 次');

    /* ---- Y3 另存为新会话（session/saved switched:true） ----
       与其它几条**不同**：保存不清屏，turn/end 挂的「已停止」还在正文下方，
       所以这里**不该**再补「已停止上一轮」（补了就是同一件事说两遍）。 */
    clear();
    emit({ type: 'turn/begin', params: { round: 3 } });
    await sleep(25);
    emit({ type: 'stream/delta', params: { text: '正在回答。' } });
    await sleep(60);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(120);
    emit({ type: 'session/saved', params: { name: '另存的名字', switched: true,
      current: '另存的名字' } });
    await sleep(400);
    ck('Y3 另存为新会话：不重复补「已停止上一轮」（turn/end 的标记还在屏上）',
       cutCount() === 0, '出现 ' + cutCount() + ' 次');
    ck('Y3a 但屏幕上确实看得到「已停止」（来自 turn/end）',
       (convo().textContent || '').indexOf('已停止') >= 0,
       (convo().textContent || '').slice(0, 80));
    ck('Y3b 另存后退出运行态（data-run=false）',
       document.body.getAttribute('data-run') === 'false',
       document.body.getAttribute('data-run'));

    /* ---- Y4 新建对话 ---- */
    clear();
    await startTurn(4);
    await cancelThenResult({ action: 'sessionNew', current: null });
    ck('Y4 新建对话：被打断后有标记', cutCount() === 1, '出现 ' + cutCount() + ' 次');

    /* ---- Y5 删除当前会话 ---- */
    clear();
    await startTurn(5);
    await cancelThenResult({ action: 'sessionDelete', name: '甲', wasCurrent: true,
      current: null });
    ck('Y5 删除当前会话：被打断后有标记', cutCount() === 1, '出现 ' + cutCount() + ' 次');

    /* ---- Y6 顺序 B（session/result 先到）：仍然只补一次 ---- */
    clear();
    await startTurn(6);
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '乙',
      messages: msgs() } });
    await sleep(200);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(350);
    ck('Y6 顺序反过来时只补一次（不重复刷两行）', cutCount() === 1,
       '出现 ' + cutCount() + ' 次');

    /* ---- Y7 正常收尾后切会话：不能乱报 ---- */
    clear();
    emit({ type: 'turn/begin', params: { round: 7 } });
    await sleep(25);
    emit({ type: 'stream/delta', params: { text: '这一轮会正常说完。' } });
    await sleep(25);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(250);
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '甲',
      messages: msgs() } });
    await sleep(400);
    ck('Y7 正常收尾后切会话：不报「已停止上一轮」', cutCount() === 0,
       '出现 ' + cutCount() + ' 次');

    /* ---- Y8 记忆不跨轮泄漏 ---- */
    clear();
    await startTurn(8);
    emit({ type: 'turn/end', params: { reason: 'cancelled' } });
    await sleep(150);
    // 新开一轮（会清记忆），然后正常收尾再切会话
    await startTurn(9);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(200);
    emit({ type: 'session/result', params: { action: 'sessionSwitch', current: '乙',
      messages: msgs() } });
    await sleep(400);
    ck('Y8 上一轮被打断的记忆不会漏到下一轮（不假报）', cutCount() === 0,
       '出现 ' + cutCount() + ' 次');

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-cut-notice</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-vrfy-cut');
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
