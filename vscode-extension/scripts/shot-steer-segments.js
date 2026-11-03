/**
 * 目视核对：一轮里插过话之后，过程是否**按插话位置分段**折叠。
 *
 * 场景（对应 dsh 的 `G1 → steering → G2 → 正文`）：
 *   工具1 → 工具2 → 插话气泡 → 工具3 → 正文 → turn/end
 * 期望：两个折叠组，中间夹着那条插话，正文在最后。
 *
 * 产出四张 PNG，供人工目视（也可作为交付前自检）：
 *   steer-seg-collapsed.png  收起态：两个组头 + 插话 + 正文
 *   steer-seg-open.png       依次点开两个组：各自只显示自己的成员
 *   steer-seg-stopped.png    同一场景但 reason=cancelled：每个组头「已停止」且不可点
 *   steer-seg-plain.png      无插话的同构场景：只有 1 个组（回归对照）
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

// 主题取真实配置，截图才是用户实际看到的配色
const py = 'import json,sys;sys.path.insert(0,".");'
  + 'from narnat_agent.config.loader import normalize_ui_raw;'
  + 'from narnat_agent.ui.theme import snapshot;'
  + 'cfg=json.load(open(".narnat/config/narnat.json",encoding="utf-8-sig"));'
  + 'print(json.dumps(snapshot(normalize_ui_raw(cfg.get("界面") or {})),ensure_ascii=False))';
let THEME = '{}';
try {
  THEME = execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim();
} catch (e) {
  console.warn('取主题失败，用默认配色：' + e.message);
}

/**
 * 场景脚本。
 * @param {{steer:boolean, reason:string, expand:boolean}} opt
 *   steer  是否在工具2与工具3之间插入一条插话气泡
 *   reason turn/end 的 reason（'done' / 'cancelled'）
 *   expand 是否点开折叠组
 */
function scene(opt) {
  const st = JSON.stringify(opt);
  return `
(function(){
  var O = ${st};
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function tool(id, name, label, summary){
    emit({ type: 'tool/call', params: { id: id, name: name, label: label,
      summary: summary, arguments: { q: summary } } });
  }
  function done(id, ms){
    emit({ type: 'tool/result', params: { id: id, ok: true, durationMs: ms, output: 'ok' } });
  }
  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'deepseek-flash', options: ['deepseek-flash'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: 'd:\\\\desktop\\\\NarnatAgent',
      theme: window.__THEME } });
    await sleep(150);

    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(20);
    emit({ type: 'thinking/delta', params: { text: '先跑一遍工具自检。' } });
    emit({ type: 'thinking/end', params: {} });
    await sleep(20);

    // 第一段过程：工具1 + 工具2
    tool('t1', 'Shell', '执行命令', 'npm test'); await sleep(20);
    done('t1', 900); await sleep(20);
    tool('t2', 'Read', '读取文件', 'a.md'); await sleep(20);
    done('t2', 3); await sleep(30);

    // 插话（用户在中途补的一句）——insert/consumed 才会画成气泡
    if (O.steer) {
      emit({ type: 'insert/queued', params: { id: 7, text: '顺便把 C++ 也试一下', position: 1 } });
      await sleep(40);
      emit({ type: 'insert/consumed', params: { id: 7, text: '顺便把 C++ 也试一下' } });
      await sleep(60);
    }

    // 第二段过程：工具3
    tool('t3', 'Grep', '搜索代码', 'quicksort'); await sleep(20);
    done('t3', 12); await sleep(30);

    // 正文（最终回复）
    emit({ type: 'stream/delta', params: { text: '工具全部验证完毕：命令执行、文件读取、代码搜索都正常。' } });
    await sleep(40);
    emit({ type: 'turn/end', params: { reason: O.reason } });
    await sleep(900);

    if (O.expand) {
      var rows = document.querySelectorAll('.turn-proc .tp-row');
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].disabled) continue;
        rows[i].click();
        await sleep(320);
      }
      await sleep(300);
    }
    document.title = 'shot-ok';
  }
  window.addEventListener('load', function(){ run(); });
})();
`;
}

function buildPage(opt) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-steer-segments</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${THEME};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${scene(opt)}</script></body></html>`;
}

const dir = path.join(os.tmpdir(), 'narnat-shot-steerseg');
fs.mkdirSync(dir, { recursive: true });
const shots = path.join(ROOT, 'shots');
fs.mkdirSync(shots, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
if (!browser) {
  console.error('找不到 Edge，无法截图');
  process.exit(1);
}

const CASES = [
  ['steer-seg-collapsed.png', { steer: true, reason: 'done', expand: false }],
  ['steer-seg-open.png', { steer: true, reason: 'done', expand: true }],
  ['steer-seg-stopped.png', { steer: true, reason: 'cancelled', expand: false }],
  ['steer-seg-plain.png', { steer: false, reason: 'done', expand: false }],
];

CASES.forEach(([name, opt], i) => {
  const file = path.join(dir, 't' + i + '.html');
  fs.writeFileSync(file, buildPage(opt), 'utf8');
  const out = path.join(shots, name);
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p' + i), '--virtual-time-budget=15000',
    '--window-size=760,640', '--hide-scrollbars', '--screenshot=' + out,
    'file:///' + file.replace(/\\/g, '/')],
  { stdio: ['ignore', 'ignore', 'ignore'], timeout: 180000 });
  console.log(out);
});
