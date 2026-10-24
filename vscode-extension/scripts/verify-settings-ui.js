/**
 * 界面侧验证：设置面板两类配置的回显、切换、以及 warning 显示。
 * 数据用真实后端产出的 describe 结构（不是手搓）。
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

// 真实后端的 describe 结构（字段与 server.py 一致）
const DESC = {
  restartPending: false,
  groups: [{ id: 'api', label: '模型 API' }, { id: 'websearch', label: '联网工具' }],
  items: [
    { id: 'api.baseUrl', group: 'api', label: '接口地址', kind: 'text',
      value: 'https://api.deepseek.com/anthropic', writable: true },
    { id: 'api.apiKey', group: 'api', label: '接口密钥', kind: 'secret',
      value: '', masked: 'sk-9***c4b7', isSet: true, writable: true },
    { id: 'api.protocol', group: 'api', label: '协议', kind: 'select',
      value: 'anthropic', writable: true,
      options: [{ value: 'anthropic', label: 'anthropic' }, { value: 'openai', label: 'openai' }] },
    { id: 'api.model', group: 'api', label: '模型名', kind: 'text',
      value: 'deepseek-flash', writable: true,
      options: [{ value: 'deepseek-v4-pro', label: 'deepseek-v4-pro' }] },
    { id: 'api.thinking', group: 'api', label: '思考强度', kind: 'select',
      value: 'high', writable: true,
      options: [{ value: 'high', label: '深度' }, { value: 'max', label: '极致' }] },

    { id: 'websearch.url_search', group: 'websearch', label: '搜索接口地址', kind: 'text',
      value: 'https://api.deepseek.com/v1/tools/search', writable: true },
    { id: 'websearch.key', group: 'websearch', label: '密钥', kind: 'secret',
      value: '', masked: 'sk-9***c4b7', isSet: true, writable: true },
    { id: 'websearch.auth', group: 'websearch', label: '认证方式', kind: 'select',
      value: 'bearer', writable: true,
      options: [{ value: 'x-api-key', label: 'X-API-Key' }, { value: 'bearer', label: 'Bearer' },
                { value: 'none', label: '不认证' }] },
    { id: 'websearch.url_fetch', group: 'websearch', label: '抓取接口地址', kind: 'text',
      value: 'https://api.deepseek.com/v1/tools/fetch', writable: true },
    { id: 'websearch.query_field', group: 'websearch', label: '搜索·查询字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.count_field', group: 'websearch', label: '搜索·条数字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.rail_list', group: 'websearch', label: '搜索·结果数组路径', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.rail_title', group: 'websearch', label: '搜索·标题字段', kind: 'text',
      value: 'title', writable: true, advanced: true },
    { id: 'websearch.rail_url', group: 'websearch', label: '搜索·链接字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.rail_desc', group: 'websearch', label: '搜索·描述字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.args_search', group: 'websearch', label: '搜索·附加参数（JSON）', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.url_field', group: 'websearch', label: '抓取·链接字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.rail_fetch_title', group: 'websearch', label: '抓取·标题字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.rail_fetch_content', group: 'websearch', label: '抓取·正文字段', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.args_fetch', group: 'websearch', label: '抓取·附加参数（JSON）', kind: 'text',
      value: '', writable: true, advanced: true },
    { id: 'websearch.extra_params', group: 'websearch', label: '搜索·扩展参数（JSON）', kind: 'text',
      value: '', writable: true, advanced: true }
  ]
};

// 把 DESCRIPTOR 作为真实 JSON 注入页面（避免字符串替换的可读性问题）
const TEST = String.raw`
(function(){
  var DESC = window.__DESC;
  var out = [], fails = 0;
  function ck(n, ok, d){
    if (!ok) fails++;
    out.push((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  :: ' + d : ''));
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function emit(m){ window.dispatchEvent(new MessageEvent('message', { data: m })); }
  function val(id){
    var el = document.querySelector('[data-id="' + id + '"]');
    if (!el) return null;
    var inp = el.querySelector('input,select,textarea');
    return inp ? inp.value : null;
  }
  function visibleRows(){
    return [].slice.call(document.querySelectorAll('#setBody .set-row'))
      .map(function(r){ return r.getAttribute('data-id'); });
  }
  function tabAt(i){
    var bs = document.querySelectorAll('#setTabs button');
    return bs[i] || null;
  }
  function advRows(){
    return [].slice.call(document.querySelectorAll('#setBody .set-adv .set-row'))
      .map(function(r){ return r.getAttribute('data-id'); });
  }
  function advOpen(){
    var b = document.querySelector('.set-adv');
    return b ? b.getAttribute('data-open') : null;
  }

  async function run(){
    emit({ type: 'initialize', params: { version: 'r', protocol: 1,
      model: { current: 'm', options: ['m'] },
      thinking: { effort: 'high', options: { high: '高' } },
      goal: { enabled: false, maxRounds: 10 }, plugins: [],
      session: { current: null }, workdir: '<t>' } });
    await sleep(50);
    document.getElementById('btnSettings').click();
    await sleep(30);
    emit({ type: 'config/describe', params: DESC });
    await sleep(120);

    // ── 模型 API 分组 ──
    var tabApi = tabAt(0);
    if (tabApi) { tabApi.click(); await sleep(80); }
    ck('模型API：显示接口地址且回显真值',
       val('api.baseUrl') === 'https://api.deepseek.com/anthropic', val('api.baseUrl'));
    ck('模型API：密钥框留空（不回显真值）', val('api.apiKey') === '', val('api.apiKey'));
    ck('模型API：协议回显', val('api.protocol') === 'anthropic', val('api.protocol'));
    ck('模型API：模型名回显', val('api.model') === 'deepseek-flash', val('api.model'));
    ck('模型API：思考强度回显', val('api.thinking') === 'high', val('api.thinking'));
    var rowsApi = visibleRows();
    ck('模型API：5 个字段全在',
       ['api.baseUrl','api.apiKey','api.protocol','api.model','api.thinking']
         .every(function(x){ return rowsApi.indexOf(x) >= 0; }), rowsApi.join(','));

    // 改模型名 → 检查发出的载荷
    window.__sent.length = 0;
    var mInp = document.querySelector('[data-id="api.model"] input');
    mInp.value = 'kimi-k2';
    mInp.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(60);
    var sentModel = (window.__sent||[]).filter(function(x){ return x.type === 'configSet'; });
    ck('改模型名发 configSet', sentModel.length === 1, 'got=' + sentModel.length);
    ck('载荷只有 model 一项（不整包覆盖）',
       sentModel[0] && JSON.stringify(sentModel[0].params.value) === '{"model":"kimi-k2"}',
       sentModel[0] ? JSON.stringify(sentModel[0].params) : '-');

    // 后端提示「模型名不在候选列表」—— 设置面板的提示走 configSet/result 通道
    // （config/result 是主界面下拉用的，两者由 extension.js 同时广播）
    emit({ type: 'configSet/result', params: { ok: true, needRestart: true,
      warning: '无效值: kimi-k2（可用: deepseek-v4-pro / deepseek-flash）',
      current: { restartPending: true } } });
    await sleep(100);
    /* 页面上有两处 .set-msg：setBody 里的「正在读取配置…」（loadDescribe 写的）
       与 setFoot 前那条保存结果。保存结果带 data-kind，据此区分。 */
    var msgs = [].slice.call(document.querySelectorAll('.set-msg[data-kind]'));
    var msg = msgs.length ? msgs[msgs.length - 1] : null;
    ck('warning 显示出来了（用户能知道为什么没切）',
       msg && msg.textContent.indexOf('无效值') >= 0, msg ? msg.textContent : '(无提示)');

    // ── 联网工具分组（REST 双接口，没有接入方式切换）──
    emit({ type: 'config/describe', params: DESC });
    await sleep(120);
    var tabWs = tabAt(1);
    if (tabWs) { tabWs.click(); await sleep(80); }
    ck('联网工具：搜索接口地址回显真值',
       val('websearch.url_search') === 'https://api.deepseek.com/v1/tools/search',
       val('websearch.url_search'));
    ck('联网工具：抓取接口地址回显真值',
       val('websearch.url_fetch') === 'https://api.deepseek.com/v1/tools/fetch',
       val('websearch.url_fetch'));
    ck('联网工具：认证方式回显', val('websearch.auth') === 'bearer', val('websearch.auth'));
    var rowsWs = visibleRows();
    ck('常用 4 项直接可见（两个地址 + 密钥 + 认证）',
       ['websearch.url_search','websearch.key','websearch.auth','websearch.url_fetch']
         .every(function(x){ return rowsWs.indexOf(x) >= 0; }), rowsWs.join(','));
    var rowsAdvOff = advRows();
    ck('高级字段收在折叠区里（不在常用区）',
       ['websearch.url_search','websearch.key','websearch.auth','websearch.url_fetch']
         .every(function(x){ return rowsAdvOff.indexOf(x) < 0; })
       && ['websearch.rail_list','websearch.extra_params','websearch.args_search']
         .every(function(x){ return rowsAdvOff.indexOf(x) >= 0; }), rowsAdvOff.join(','));
    ck('高级区默认收起', advOpen() === 'false', advOpen());

    // 展开高级
    var tgl = document.querySelector('.set-adv-toggle');
    tgl.click();
    await sleep(80);
    ck('点「高级设置」后展开', advOpen() === 'true', advOpen());
    var rowsAdv = visibleRows();
    ck('展开后解析/扩展字段出现',
       ['websearch.query_field','websearch.rail_desc','websearch.args_search',
        'websearch.url_field','websearch.rail_fetch_content','websearch.extra_params']
         .every(function(x){ return rowsAdv.indexOf(x) >= 0; }), rowsAdv.join(','));
    ck('展开后标题字段回显真值', val('websearch.rail_title') === 'title',
       val('websearch.rail_title'));

    // 搜索的 rail_desc 走「合并成 rail 对象 + 逗号拆数组」
    window.__sent.length = 0;
    var dInp = document.querySelector('[data-id="websearch.rail_desc"] input');
    dInp.value = 'snippet, summary';
    dInp.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(60);
    var sentRail = (window.__sent||[]).filter(function(x){ return x.type === 'configSet'; });
    ck('改搜索描述字段发 configSet', sentRail.length === 1, 'got=' + sentRail.length);
    ck('载荷是 rail.desc 数组',
       sentRail[0] && JSON.stringify(sentRail[0].params.value) === '{"rail":{"desc":["snippet","summary"]}}',
       sentRail[0] ? JSON.stringify(sentRail[0].params) : '-');

    // 抓取的 rail 字段走单键提交（后端按路径落到 fetch.rail.*）
    window.__sent.length = 0;
    var cInp = document.querySelector('[data-id="websearch.rail_fetch_content"] input');
    cInp.value = 'content, markdown';
    cInp.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(60);
    var sentFetch = (window.__sent||[]).filter(function(x){ return x.type === 'configSet'; });
    ck('改抓取正文字段发 configSet', sentFetch.length === 1, 'got=' + sentFetch.length);
    ck('载荷是单键 rail_fetch_content 数组',
       sentFetch[0] && JSON.stringify(sentFetch[0].params.value)
         === '{"rail_fetch_content":["content","markdown"]}',
       sentFetch[0] ? JSON.stringify(sentFetch[0].params) : '-');

    // extra_params：坏 JSON 必须挡在前端，不能发出去
    window.__sent.length = 0;
    var eInp = document.querySelector('[data-id="websearch.extra_params"] input');
    eInp.value = '{broken';
    eInp.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(60);
    ck('扩展参数坏 JSON 不发送',
       (window.__sent||[]).filter(function(x){ return x.type === 'configSet'; }).length === 0,
       JSON.stringify(window.__sent));
    var errMsgs = [].slice.call(document.querySelectorAll('.set-msg[data-kind]'));
    var errMsg = errMsgs.length ? errMsgs[errMsgs.length - 1] : null;
    ck('扩展参数坏 JSON 有明确提示',
       errMsg && errMsg.textContent.indexOf('必须是合法 JSON') >= 0,
       errMsg ? errMsg.textContent : '(无提示)');

    // extra_params：合法 JSON 作为对象提交
    window.__sent.length = 0;
    eInp = document.querySelector('[data-id="websearch.extra_params"] input');
    eInp.value = '{"domains":{"type":"array","field":"include_domains"}}';
    eInp.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(60);
    var sentEp = (window.__sent||[]).filter(function(x){ return x.type === 'configSet'; });
    ck('扩展参数合法 JSON 发 configSet', sentEp.length === 1, 'got=' + sentEp.length);
    ck('载荷是 extra_params 对象',
       sentEp[0] && JSON.stringify(sentEp[0].params.value)
         === '{"extra_params":{"domains":{"type":"array","field":"include_domains"}}}',
       sentEp[0] ? JSON.stringify(sentEp[0].params) : '-');

    out.push('SUMMARY total=' + (out.length) + ' fail=' + fails);
    var p = document.createElement('pre');
    p.id = 'RESULT';
    p.textContent = out.join('\n');
    document.body.appendChild(p);
  }
  window.addEventListener('load', function(){
    run().catch(function(e){
      var p = document.createElement('pre');
      p.id = 'ERR';
      p.textContent = 'run() 抛错: ' + (e && e.stack ? e.stack : e) + '\n已完成: ' + out.join(' | ');
      document.body.appendChild(p);
    });
  });
})();
`;

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>settings-ui</title>
<style>${CSS}</style></head><body data-run="false">${BODY}
<script>window.__sent=[];window.__DESC=${JSON.stringify(DESC)};
window.acquireVsCodeApi=function(){return{postMessage:function(m){window.__sent.push(m);}};};</script>
<script>${JS}</script><script>${TEST}</script></body></html>`;

const dir = path.join(os.tmpdir(), 'narnat-setui');
fs.mkdirSync(dir, { recursive: true });
const file = path.join(dir, 'settings-ui.html');
fs.writeFileSync(file, page, 'utf8');
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));
const dom = execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=15000', '--dump-dom',
  'file:///' + file.replace(/\\/g, '/')], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
const m = /<pre id="RESULT">([\s\S]*?)<\/pre>/.exec(dom);
if (!m) {
  // 页面没跑完：把控制台错误抓出来，别只吐一句 NO RESULT
  const errs = [];
  const re = /<pre id="ERR">([\s\S]*?)<\/pre>/g;
  let mm;
  while ((mm = re.exec(dom)) !== null) errs.push(mm[1]);
  console.log('NO RESULT —— 页面未产出断言');
  console.log('setBody 存在:', /id="setBody"/.test(dom));
  console.log('settingsPanel data-open:', (/id="settingsPanel"[^>]*data-open="([^"]*)"/.exec(dom) || [])[1]);
  if (errs.length) console.log('页面错误:', errs.join('\n'));
  // 打印一段 dom 片段帮助定位
  const seg = /<div id="setBody"[\s\S]{0,400}/.exec(dom);
  if (seg) console.log('setBody 片段:', seg[0].replace(/\s+/g, ' ').slice(0, 400));
  process.exit(2);
}
const txt = m[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
console.log(txt);
process.exit(/fail=0/.test(txt) ? 0 : 2);
