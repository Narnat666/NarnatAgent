/**
 * 目视核对：浅色主题下的代码块（用户报障「配色太耀眼，看不清代码」）。
 *
 * 走真实管线：真实 theme 快照 → initialize → 渲染一段 C++ 代码。
 * 出图后人工看。产物：scripts/shots/code-light.png、code-dark.png
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const REPO = path.resolve(__dirname, '..', '..');
const EXT = path.join(REPO, 'vscode-extension');
const html = fs.readFileSync(path.join(EXT, 'design/ui-full.html'), 'utf8');
const CSS = /<style>([\s\S]*?)<\/style>/.exec(html)[1];
const BODY = /<body[^>]*>([\s\S]*?)<script>/.exec(html)[1];
const JS = fs.readFileSync(path.join(EXT, 'media', 'narnat.js'), 'utf8');
const KCSS = fs.readFileSync(path.join(EXT, 'media', 'katex', 'katex.min.css'), 'utf8');
const KJS = fs.readFileSync(path.join(EXT, 'media', 'katex', 'katex.min.js'), 'utf8');

const py = [
  'import sys, io, json',
  'sys.path.insert(0, r"' + REPO + '")',
  'from narnat_agent.ui.theme import snapshot',
  'from narnat_agent.config.loader import load_config',
  'c = load_config()',
  'print(json.dumps(snapshot(getattr(c.ui, "raw", None) or {}), ensure_ascii=False))'
].join('\n');
const theme = JSON.parse(execFileSync('python', ['-c', py], { cwd: REPO, encoding: 'utf8' }).trim());

const DARK_VARS = `:root{
  --vscode-editor-background:#1f1f1f; --vscode-editor-foreground:#cccccc;
  --vscode-sideBar-background:#181818; --vscode-editorWidget-background:#202020;
  --vscode-list-hoverBackground:#2a2d2e; --vscode-panel-border:#2b2b2b;
  --vscode-textLink-foreground:#4daafc; --vscode-charts-green:#89d185;
  --vscode-errorForeground:#f14c4c; --vscode-chat-requestBackground:#1f1f1f;
  --vscode-button-secondaryBackground:#313131; --vscode-button-secondaryForeground:#cccccc;
  --vscode-button-background:#0078d4; --vscode-button-foreground:#ffffff;
  --vscode-icon-foreground:#cccccc; --vscode-focusBorder:#0078d4;
  --vscode-descriptionForeground:#9d9d9d; --vscode-editorWidget-border:#3c3c3c;
  --vscode-input-background:#313131;
  --vscode-debugTokenExpression-string:#ce9178; --vscode-debugTokenExpression-number:#b5cea8;
  --vscode-debugTokenExpression-name:#c586c0; --vscode-symbolIcon-methodForeground:#b180d7;
  --vscode-editorLineNumber-foreground:#495162;
  --vscode-gitDecoration-addedResourceForeground:#81b88b;
  --vscode-gitDecoration-deletedResourceForeground:#c74e39;
}`;
const LIGHT_VARS = `:root{
  --vscode-editor-background:#ffffff; --vscode-editor-foreground:#3b3b3b;
  --vscode-sideBar-background:#f8f8f8; --vscode-editorWidget-background:#f8f8f8;
  --vscode-list-hoverBackground:#e8e8e8; --vscode-panel-border:#e5e5e5;
  --vscode-textLink-foreground:#006ab1; --vscode-charts-green:#388a34;
  --vscode-errorForeground:#e51400; --vscode-chat-requestBackground:#f2f4f7;
  --vscode-button-secondaryBackground:#e5e5e5; --vscode-button-secondaryForeground:#3b3b3b;
  --vscode-button-background:#005fb8; --vscode-button-foreground:#ffffff;
  --vscode-icon-foreground:#3b3b3b; --vscode-focusBorder:#0090f1;
  --vscode-descriptionForeground:#717171; --vscode-editorWidget-border:#cecece;
  --vscode-input-background:#ffffff;
  --vscode-editorLineNumber-foreground:#6e7681;
  --vscode-gitDecoration-addedResourceForeground:#587c0c;
  --vscode-gitDecoration-deletedResourceForeground:#ad0707;
}`;

const FENCE = String.fromCharCode(96, 96, 96);
const CPP = [
  '#include <thread>', '#include <mutex>', '#include <vector>', '',
  'class ThreadPool {', 'public:',
  '  explicit ThreadPool(size_t n = std::thread::hardware_concurrency()) {',
  '    for (size_t i = 0; i < n; ++i) {',
  '      workers.emplace_back([this] {',
  '        for (;;) {',
  '          std::function<void()> job;',
  '          { std::unique_lock<std::mutex> lk(m_);',
  '            cv_.wait(lk, [this] { return stop_ || !jobs_.empty(); });',
  '            if (stop_ && jobs_.empty()) return;  // 注释：退出',
  '            job = std::move(jobs_.front()); jobs_.pop(); }',
  '          job();', '        }', '      });', '    }', '  }', '};'
].join('\n');

const MD = ['这是一段 C++ 线程池实现：', '', FENCE + 'cpp', CPP, FENCE, '',
  '行内代码也能看：`std::mutex` 与 `size_t`。'].join('\n');

const SCENE = `
(function(){
  var MD = ${JSON.stringify(MD)};
  window.addEventListener('load', function(){
    window.dispatchEvent(new MessageEvent('message',{data:{type:'initialize',params:{
      version:'t',protocol:1,model:{current:'deepseek-flash',options:['deepseek-flash']},
      thinking:{effort:'high',options:{high:'高'}},goal:{enabled:false,maxRounds:10},
      plugins:[],session:{current:null},workdir:'E:\\\\x',theme:THEME}}}));
    setTimeout(function(){
      window.dispatchEvent(new MessageEvent('message',{data:{type:'session/result',params:{
        action:'sessionSwitch',current:'代码配色',isChild:false,
        messages:[{role:'user',text:'随便输出一些c++代码'},{role:'assistant',text:MD}]}}}));
    }, 250);
  });
})();
`;

function build(vars) {
  return `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><title>shot-code</title>
<style>${KCSS}</style><style>${vars}</style><style>${CSS}</style></head><body data-run="false">${BODY}
<script>var THEME=${JSON.stringify(theme)};window.acquireVsCodeApi=function(){return{postMessage:function(){}};};</script>
<script>${KJS}</script><script>${JS}</script><script>${SCENE}</script></body></html>`;
}

const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });
const browser = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find((p) => fs.existsSync(p));

function shoot(vars, tag) {
  const dir = path.join(os.tmpdir(), 'shotcode-' + tag);
  fs.mkdirSync(dir, { recursive: true });
  const fsrc = path.join(EXT, 'media', 'katex', 'fonts');
  const fdst = path.join(dir, 'fonts');
  if (fs.existsSync(fsrc)) {
    fs.mkdirSync(fdst, { recursive: true });
    for (const f of fs.readdirSync(fsrc)) {
      if (f.endsWith('.woff2')) fs.copyFileSync(path.join(fsrc, f), path.join(fdst, f));
    }
  }
  const file = path.join(dir, 't.html');
  fs.writeFileSync(file, build(vars), 'utf8');
  const png = path.join(OUT, 'code-' + tag + '.png');
  execFileSync(browser, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--user-data-dir=' + path.join(dir, 'p'), '--virtual-time-budget=12000',
    '--window-size=760,900', '--screenshot=' + png,
    'file:///' + file.replace(/\\/g, '/')], { stdio: 'ignore' });
  console.log('SHOT ' + png);
  fs.rmSync(dir, { recursive: true, force: true });
}

shoot(DARK_VARS, 'dark');
shoot(LIGHT_VARS, 'light');
