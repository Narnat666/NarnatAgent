/**
 * 诊断：`--bg-code-hd` 为什么没生效（浅色主题下标题栏与块体同色）。
 *
 * 现象（shot-light.js 实测）：`.code` 与 `.code-hd` 的 computed background 都是
 * #d3d7dd，而 --bg-code-hd 应当由 color-mix 稍微再深一档。
 *
 * 三个候选原因，本脚本逐个隔离：
 *   A. color-mix 出现在**自定义属性**里，且参数又是另一个自定义属性
 *      （--bg-code-hd: color-mix(in srgb, var(--bg-code) 92%, ...)）
 *   B. --bg-code 本身是一条 var() 链（var(--a, var(--b, color-mix(...)))）
 *   C. 使用点 .code-hd 的 background:var(--bg-code-hd) 解析失败 → unset → 透明
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const PAGE = `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
:root{
  --g:#3b3b3b;
  --base:#d3d7dd;
  /* A：自定义属性里嵌 color-mix + var */
  --mixA: color-mix(in srgb, var(--base) 92%, var(--g) 8%);
  /* B：--base 换成 var 链 */
  --chain: var(--nope, color-mix(in srgb, #ffffff 92%, #3b3b3b 8%));
  --mixB: color-mix(in srgb, var(--chain) 92%, var(--g) 8%);
  /* C：直接字面量 */
  --mixC: color-mix(in srgb, #d3d7dd 92%, #3b3b3b 8%);
}
#a{background:var(--mixA);width:40px;height:10px}
#b{background:var(--mixB);width:40px;height:10px}
#c{background:var(--mixC);width:40px;height:10px}
#d{background:color-mix(in srgb, var(--base) 92%, var(--g) 8%);width:40px;height:10px}
</style></head><body>
<div id="a"></div><div id="b"></div><div id="c"></div><div id="d"></div>
<pre id="RESULT"></pre>
<script>
(function(){
  var out = [];
  function bg(id){
    var s = getComputedStyle(document.getElementById(id)).backgroundColor;
    return s;
  }
  out.push('A 属性里 color-mix(var + 字面量)   = ' + bg('a'));
  out.push('B 属性里 color-mix(var链 + 字面量) = ' + bg('b'));
  out.push('C 属性里 color-mix(全字面量)      = ' + bg('c'));
  out.push('D 使用点直接写 color-mix(var)      = ' + bg('d'));
  out.push('getPropertyValue(--mixA) = ' + getComputedStyle(document.documentElement).getPropertyValue('--mixA'));
  out.push('getPropertyValue(--mixB) = ' + getComputedStyle(document.documentElement).getPropertyValue('--mixB'));
  out.push('CSS.supports(color-mix) = ' + (window.CSS && CSS.supports ? CSS.supports('color', 'color-mix(in srgb, red, blue)') : '?'));
  out.push('UA = ' + navigator.userAgent);
  document.getElementById('RESULT').textContent = out.join('\\n');
})();
</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-diag-mix');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 't.html');
fs.writeFileSync(file, PAGE, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=4000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')],
{ encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
console.log(m ? m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&quot;/g, '"').replace(/&amp;/g, '&') : '(无 RESULT)');
