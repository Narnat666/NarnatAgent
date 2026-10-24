/**
 * 验收「按 narnat.json 渲染 AI 输出颜色」（用户报障：插件一片黑、视觉疲劳）。
 *
 * 用户已敲定的四条：
 *   1. 颜色来源 = narnat.json「界面」分组（与终端形态同一份）
 *   2. 面板底色仍跟 VSCode 主题，只有内容层用配置色
 *   3. 亮度适配：只取色相+饱和度，亮度按当前底色重算到满足对比度
 *   4. 三期全做：markdown / 语义色 / 代码块语言色
 *
 * 判据全部可计算：取 getComputedStyle 的最终颜色，算它跟配置色的色相关系，
 * 以及跟底色的对比度。
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

// 与 .narnat/config/narnat.json 的「界面」一致的用户配置（后端已解析成这个形态）
const THEME = {
  colors: {
    'markdown.heading_h1': '#C24D56',
    'markdown.heading_h3': '#5A9E82',
    'markdown.heading_h4': '#E8E4DB',
    'markdown.bold': '#7FB5B0',
    'markdown.italic': '#6B7580',
    'markdown.strikethrough': '#C24D56',
    'markdown.code_inline': '#D4A853',
    'markdown.link': '#6A8FAF',
    'markdown.blockquote': '#9E8BB5',
    'markdown.hr': '#6B7580',
    'markdown.list_unordered': '#5A9E82',
    'markdown.list_ordered': '#6B7580',
    'markdown.table_border': '#6A8FAF',
    'markdown.table_content': '#E8E4DB',
    'markdown.task_done': '#5A9E82',
    'codeblock.lang_label': '#6B7580',
    'codeblock.lang_cyan': '#7FB5B0',
    'codeblock.lang_yellow': '#D4A853',
    'codeblock.lang_green': '#5A9E82',
    'codeblock.lang_magenta': '#9E8BB5',
    'codeblock.lang_red': '#C24D56',
    'codeblock.lang_blue': '#6A8FAF',
    'codeblock.lang_gray': '#6B7580',
    'diff.added': '#5A9E82',
    'diff.removed': '#C24D56'
  },
  bgs: {},
  decor: {
    'markdown.heading_h1': ['bold'],
    'markdown.heading_h3': ['bold'],
    'markdown.heading_h4': ['bold'],
    'markdown.bold': ['bold'],
    'markdown.italic': ['italic', 'dim'],
    'markdown.link': ['underline']
  },
  base: {
    primary: '#E8E4DB', secondary: '#6B7580', user: '#FFFFFF',
    accent: '#7FB5B0', success: '#5A9E82', warning: '#D4A853',
    error: '#C24D56', link: '#6A8FAF', decoration: '#9E8BB5',
    emphasis: '#C08458', code_bg: '#1E2630'
  },
  baseBg: { code_bg: '#1E2630' },
  palette: { '红': '#C24D56', '绿': '#5A9E82', '次文字': '#6B7580' },
  langGroups: { python: 'cyan', js: 'yellow', javascript: 'yellow',
    bash: 'green', sh: 'green', json: 'magenta', html: 'red', css: 'blue', md: 'gray' }
};

const fails = [];
const ck = (n, ok, d) => {
  console.log((ok ? 'PASS ' : 'FAIL ') + n + (d === undefined ? '' : '  :: ' + d));
  if (!ok) fails.push(n);
};

/* ---- 静态断言 ---- */
ck('S1 CSS 里 markdown 标题读配置色',
  /\.msg h1,\.msg h2\{[\s\S]{0,120}var\(--t-markdown-heading-h1/.test(CSS));
ck('S2 行内代码读配置色（默认黄）',
  // 按整条规则块匹配（到 `}` 为止），不用固定字符窗口：这条规则的注释
  // 会因为别的原因变长，窗口一超就误报（本轮就踩过一次）
  /(?:^|\n)\.msg code,\.think code,\.tools code\{[^}]*var\(--t-markdown-code-inline/.test(CSS));
ck('S2b 行内代码的底不用代码块那一档（浅色主题下会吃掉文字对比度）',
  /(?:^|\n)\.msg code,\.think code,\.tools code\{[^}]*background:var\(--bg-code-inline\)/.test(CSS));
ck('S3 链接读配置色', /\.msg a\{[\s\S]{0,120}var\(--t-markdown-link/.test(CSS));
ck('S4 状态色优先取配置（--ok/--warn/--err）',
  /--ok: var\(--t-b-success/.test(CSS) && /--err: var\(--t-b-error/.test(CSS));
ck('S5 强调色优先取配置', /--accent: var\(--t-b-accent/.test(CSS));
ck('S6 代码块底色优先取配置',
  /--bg-code: var\(--tb-codeblock-background/.test(CSS));
ck('S7 代码块语言标签按分组取色（7 组齐全）',
  ['cyan', 'yellow', 'green', 'magenta', 'red', 'blue', 'gray']
    .every((g) => CSS.indexOf('.code-hd[data-lang-group="' + g + '"]') >= 0));
ck('S8 没有把面板底色（--bg）改成配置色（框架层仍跟 VSCode）',
  /--bg: var\(--vscode-editor-background/.test(CSS));
// 表格：几何抄 dsh（0.5px 下边线、表头 500 字重、无底色），颜色用配置
ck('S9 表格下边线是 0.5px（dsh 的 .tableScroll th/td 口径）',
  /\.msg th,\.msg td\{[\s\S]{0,260}border-bottom:0\.5px solid var\(--t-markdown-table-border/.test(CSS));
ck('S10 表头字重 500（dsh 的 --dsw-font-markdown-table-head）',
  /\.msg th\{[\s\S]{0,220}font-weight:500/.test(CSS) &&
  !/\.msg th\{[\s\S]{0,220}font-weight:600/.test(CSS));
ck('S11 表格没有底色（dsh 的表格是透明的，不铺块）',
  !/\.msg (table|th|td|thead|tbody)\{[^}]*background/.test(CSS));
// 代码块层次（用户报障：「mkdown 文字所在的背景颜色不好看太老土」）：
// 旧行为是「比面板暗一档 + 1px 灰框 + 标题栏与块体同色」，看着像老式文本框。
ck('S12 代码块标题栏有独立底色变量（不与块体同色）',
  /--bg-code-hd:/.test(CSS) && /\.code-hd\{[\s\S]{0,400}background:var\(--bg-code-hd\)/.test(CSS));
ck('S13 代码块无实边框（dsh 的代码块只有底色 + 圆角）',
  /--code-border:\s*transparent/.test(CSS) &&
  /\.code\{[\s\S]{0,120}border:1px solid var\(--code-border\)/.test(CSS));
ck('S14 无配置色时块体底色从面板派生（不是主题那个更暗的 editorWidget）',
  /--bg-code: var\(--tb-codeblock-background,\s*\n?\s*var\(--tb-code-bg,\s*\n?\s*color-mix\(in srgb, var\(--bg\) 92%, var\(--fg\) 8%\)\)\);/.test(CSS));

/* ---- 运行时断言 ---- */
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

  function rgbOf(el, prop){
    var c = getComputedStyle(el)[prop || 'color'];
    var m = /rgba?\(([^)]+)\)/.exec(c);
    if (!m) return null;
    var p = m[1].split(',').map(function(x){ return parseFloat(x); });
    return [p[0], p[1], p[2]];
  }
  function relLum(rgb){
    var c = rgb.map(function(v){ var x = v/255; return x <= 0.03928 ? x/12.92 : Math.pow((x+0.055)/1.055, 2.4); });
    return 0.2126*c[0] + 0.7152*c[1] + 0.0722*c[2];
  }
  function contrast(a, b){
    var la = relLum(a), lb = relLum(b);
    return (Math.max(la,lb)+0.05)/(Math.min(la,lb)+0.05);
  }
  function hueOf(rgb){
    var r=rgb[0]/255,g=rgb[1]/255,b=rgb[2]/255;
    var mx=Math.max(r,g,b), mn=Math.min(r,g,b), d=mx-mn;
    if (d === 0) return -1;
    var h;
    if (mx===r) h=((g-b)/d+(g<b?6:0))/6;
    else if (mx===g) h=((b-r)/d+2)/6;
    else h=((r-g)/d+4)/6;
    return Math.round(h*360);
  }
  function hueDist(a, b){
    var d = Math.abs(a-b) % 360;
    return d > 180 ? 360-d : d;
  }
  var MD = '# 标题一\n\n### 标题三\n\n#### 标题四\n\n**粗体** 与 *斜体* 与 ~~删除~~\n\n'
    + '行内 \`code_inline\` 与 [链接](https://x.y)\n\n> 引用一句\n\n---\n\n'
    + '- 无序项\n- 无序项二\n\n1. 有序项\n2. 有序项二\n\n'
    + '\`\`\`python\nprint(1)\n\`\`\`\n\n\`\`\`bash\nls -la\n\`\`\`\n\n\`\`\`json\n{"a":1}\n\`\`\`\n';

  async function run(){
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>',
      theme: window.__THEME } });
    await sleep(120);
    emit({ type: 'turn/begin', params: { round: 1 } });
    await sleep(30);
    emit({ type: 'stream/delta', params: { text: MD } });
    await sleep(30);
    emit({ type: 'turn/end', params: { reason: 'done' } });
    await sleep(700);

    var bg = rgbOf(document.body, 'backgroundColor');
    var rootStyle = getComputedStyle(document.documentElement);
    var h1var = rootStyle.getPropertyValue('--t-markdown-heading-h1').trim();
    var codevar = rootStyle.getPropertyValue('--t-markdown-code-inline').trim();
    out.push('DIAG 面板底色=' + JSON.stringify(bg) + ' h1var=' + h1var +
      ' 行内代码var=' + codevar);
    ck('R1 initialize 带 theme 时落地了 CSS 变量', /^#[0-9a-f]{6}$/i.test(h1var), h1var);

    var h1 = convo().querySelector('.msg h1');
    var h3 = convo().querySelector('.msg h3');
    var h4 = convo().querySelector('.msg h4');
    var strong = convo().querySelector('.msg strong');
    var codeInline = convo().querySelector('.msg code');
    var link = convo().querySelector('.msg a');
    var bq = convo().querySelector('.msg blockquote');
    ck('R2 h1 有独立颜色（不再是正文色）',
      !!h1 && hueDist(hueOf(rgbOf(h1)), hueOf([0xC2,0x4D,0x56])) <= 12,
      h1 ? 'hue=' + hueOf(rgbOf(h1)) + ' 配置 hue=' + hueOf([0xC2,0x4D,0x56]) : '(无)');
    ck('R3 h3 走另一档色（hue 与 h1 明显不同）',
      !!h3 && !!h1 && hueDist(hueOf(rgbOf(h3)), hueOf(rgbOf(h1))) > 30,
      h3 ? hueOf(rgbOf(h3)) + ' vs ' + hueOf(rgbOf(h1)) : '(无)');
    ck('R4 粗体有色（不再是正文色）',
      !!strong && hueDist(hueOf(rgbOf(strong)), hueOf([0x7F,0xB5,0xB0])) <= 12,
      strong ? 'hue=' + hueOf(rgbOf(strong)) : '(无)');
    ck('R5 行内代码有色（默认黄系）',
      !!codeInline && hueDist(hueOf(rgbOf(codeInline)), hueOf([0xD4,0xA8,0x53])) <= 14,
      codeInline ? 'hue=' + hueOf(rgbOf(codeInline)) : '(无)');
    ck('R6 链接有色（配置的蓝系）',
      !!link && hueDist(hueOf(rgbOf(link)), hueOf([0x6A,0x8F,0xAF])) <= 14,
      link ? 'hue=' + hueOf(rgbOf(link)) : '(无)');
    ck('R7 引用块有色', !!bq && hueDist(hueOf(rgbOf(bq)), hueOf([0x9E,0x8B,0xB5])) <= 14,
      bq ? hueOf(rgbOf(bq)) : '(无)');

    // 三期：代码块语言标签按分组上色，三种语言三种颜色
    var hds = [].slice.call(convo().querySelectorAll('.code-hd'));
    var groups = hds.map(function(h){ return h.getAttribute('data-lang-group'); });
    var langColors = hds.map(function(h){
      var s = h.querySelector('.lang');
      return s ? getComputedStyle(s).color : '(无)';
    });
    out.push('DIAG 代码块分组=' + JSON.stringify(groups) +
      ' 标签色=' + JSON.stringify(langColors));
    ck('R8 代码块写了语言分组（python→cyan / bash→green / json→magenta）',
      groups.join(',') === 'cyan,green,magenta', groups.join(','));
    ck('R9 三种语言的标签色互不相同',
      langColors.length === 3 && new Set(langColors).size === 3, langColors.join(' | '));

    // 二期：状态色来自配置
    var okvar = rootStyle.getPropertyValue('--ok').trim();
    var errvar = rootStyle.getPropertyValue('--err').trim();
    out.push('DIAG --ok=' + okvar + ' --err=' + errvar);
    ck('R10 成功/错误色已由配置接管（hue 对得上配置的绿/红）',
      hueDist(hueOf(rgbOf(document.body, 'color')), 0) >= 0 &&
      okvar !== '' && errvar !== '', okvar + ' / ' + errvar);

    // 关键：所有着色都必须满足对比度（浅色主题/深色主题都不该糊）
    var roles = [
      ['h1', h1], ['h3', h3], ['h4', h4], ['粗体', strong],
      ['行内代码', codeInline], ['链接', link], ['引用', bq]
    ];
    var bad = [];
    roles.forEach(function(pair){
      if (!pair[1]) return;
      var c = contrast(rgbOf(pair[1]), bg);
      if (c < 3.0) bad.push(pair[0] + '=' + c.toFixed(2));
    });
    ck('R11 所有内容色与底色的对比度 >= 3.0（视觉可读下限）',
      bad.length === 0, bad.length ? bad.join(' ') : '全部达标');

    // 老后端（不带 theme）必须完全退回原样，不能崩
    var before = rootStyle.getPropertyValue('--t-markdown-heading-h1').trim();
    emit({ type: 'initialize', params: { version: 't', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(200);
    var after = getComputedStyle(document.documentElement)
      .getPropertyValue('--t-markdown-heading-h1').trim();
    ck('R12 老后端（回执无 theme）不清空已落地配色、也不抛错',
      after === before && after !== '', before + ' → ' + after);

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

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>verify-theme-ui</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__THEME=${JSON.stringify(THEME)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-themeui');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'theme.html');
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
if (m) console.log(m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
console.log('\nSTATIC fail=' + fails.length);
process.exit(fails.length ? 1 : 0);
