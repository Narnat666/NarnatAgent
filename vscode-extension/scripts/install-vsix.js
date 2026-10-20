'use strict';

/**
 * 打包 → 安装 → 逐字节校验。
 *
 * 为什么需要它：曾出现「命令报 ALL_PASS、用户装的还是旧界面」的事故——
 * 打包步骤被跳过却没人察觉。这里把「装进去的文件必须与仓库源码逐字节一致」
 * 变成硬断言，装错立刻失败。
 *
 * 二进制（exe）默认**自动判断**要不要带：
 *   output/narnat.exe 比 bin/narnat.exe 新 → 自动带上（重新编译过就该装新的）
 *   两者一样新或 bin 更新            → 跳过（避免每次白跑 100 分钟）
 * 早先默认永远跳过，导致「编译完了但装进去的还是旧 exe」——
 * 客户点「新建对话」报 Method not found 而本地测试全绿（第 18 轮 P0 事故）。
 *
 * 用法：
 *   node scripts/install-vsix.js              # 自动判断 + 打包 + 安装 + 校验
 *   node scripts/install-vsix.js --with-exe   # 强制带上 output/narnat.exe
 *   node scripts/install-vsix.js --skip-exe   # 强制跳过二进制
 *   node scripts/install-vsix.js --verify     # 只校验已安装扩展与源码是否一致
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const VSIX = path.join(ROOT, 'narnat-vscode.vsix');
const VERIFY_ONLY = process.argv.includes('--verify');
const FORCE_EXE = process.argv.includes('--with-exe');
const SKIP_EXE = process.argv.includes('--skip-exe');

/**
 * 要不要把 output/narnat.exe 打进包里。
 *
 * 判据：output 的那个比 bin 里的**更新**就带（重新编译过就该装新的）。
 * 这条判断是第 18 轮 P0 事故的直接补丁：当时 exe 编译好了，
 * 但本脚本默认 --skip-exe，装进去的还是 3 小时前的旧 exe，
 * 客户点「新建对话」报 Method not found，而本地测试（走源码）全绿。
 */
function shouldIncludeExe() {
  if (FORCE_EXE) return true;
  if (SKIP_EXE) return false;
  const outExe = path.join(ROOT, '..', 'output', 'narnat.exe');
  const binExe = path.join(ROOT, 'bin', 'narnat.exe');
  try {
    if (!fs.existsSync(outExe)) return false;      // 没编译过：用 bin 现有的
    if (!fs.existsSync(binExe)) return true;       // bin 还没有：必须带
    return fs.statSync(outExe).mtimeMs > fs.statSync(binExe).mtimeMs;
  } catch (e) {
    return true;   // 判断不了就带上，宁可慢也不要装旧 exe
  }
}

/** 装好后必须与仓库逐个字节一致的文件 */
const MUST_MATCH = [
  'src/extension.js',
  'src/rpc-client.js',
  'src/webview.js',
  'media/narnat.js',
  'media/narnat.body.js',
  'media/narnat.css',
  'media/icon.svg'
];

/**
 * 找到本机 VSCode 的 code.cmd / code 可执行文件。
 *
 * 原来这里写死一个安装目录当兜底，换台机器/换个安装位置就装不上（报 ENOENT），
 * 而报错信息只会说「找不到文件」，看不出是路径猜错了。改成按顺序探：
 *   1. 环境变量 NARNAT_CODE_CMD（显式指定，优先级最高）
 *   2. PATH 里的 code / code.cmd（VSCode 安装时默认会加）
 *   3. 常见安装位置（含 %LOCALAPPDATA% 下用户级安装，这里不写死盘符）
 * 都找不到才报错，并把「怎么指定」写清楚。
 */
function resolveCodeCmd() {
  const envCmd = process.env.NARNAT_CODE_CMD;
  if (envCmd) return envCmd;

  // PATH 里能直接跑，就不必自己拼绝对路径（调用方会加引号 + shell:true）
  // 这里不加 shell:true：where/which 本身就是可执行文件，加了反而会触发
  // Node 的 DEP0190（args + shell 有注入风险）。
  const inPath = process.platform === 'win32' ? 'code.cmd' : 'code';
  try {
    const probe = execFileSync(process.platform === 'win32' ? 'where' : 'which',
      [inPath], { encoding: 'utf8', timeout: 10000 });
    const first = String(probe).split(/\r?\n/).map((s) => s.trim()).filter(Boolean)[0];
    if (first && fs.existsSync(first)) return first;
  } catch (e) {
    // PATH 里没有，继续往下探
  }

  // 常见安装位置：全部由环境变量推导，不写死盘符
  const candidates = [];
  if (process.platform === 'win32') {
    const localAppData = process.env.LOCALAPPDATA;
    const programFiles = process.env.ProgramFiles;
    const programFilesX86 = process.env['ProgramFiles(x86)'];
    for (const base of [localAppData, programFiles, programFilesX86]) {
      if (!base) continue;
      candidates.push(path.join(base, 'Programs', 'Microsoft VS Code', 'bin', 'code.cmd'));
      candidates.push(path.join(base, 'Microsoft VS Code', 'bin', 'code.cmd'));
    }
  } else {
    candidates.push('/usr/local/bin/code');
    candidates.push('/usr/bin/code');
    candidates.push('/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code');
  }
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch (e) { /* 忽略无权限的候选 */ }
  }

  console.error('[错误] 找不到 VSCode 的 code 命令。');
  console.error('       可显式指定：set NARNAT_CODE_CMD=<你的 code.cmd 绝对路径>');
  console.error('       Windows 默认在 %LOCALAPPDATA%\\Programs\\Microsoft VS Code\\bin\\code.cmd');
  process.exit(1);
}

/**
 * package.json 不能逐字节比：vsce 会重排格式，VSCode 安装时会写入 __metadata。
 * 改为比对这些真正决定行为的字段。
 */
const PKG_KEYS = [
  'name', 'version', 'engines', 'main', 'activationEvents', 'contributes'
];

function pkgFingerprint(file) {
  const j = JSON.parse(fs.readFileSync(file, 'utf8'));
  const pick = {};
  for (const k of PKG_KEYS) pick[k] = j[k];
  return JSON.stringify(pick);
}

let failed = 0;
function ck(name, ok, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' → ' + detail : ''));
  if (!ok) failed++;
}

/** 已安装扩展目录（优先用 `code --list-extensions --show-versions` 定位） */
function installedDir() {
  const base = path.join(os.homedir(), '.vscode', 'extensions');
  if (!fs.existsSync(base)) return null;
  const hits = fs.readdirSync(base).filter((n) => n.startsWith('narnat.narnat-vscode'));
  if (!hits.length) return null;
  hits.sort();
  return path.join(base, hits[hits.length - 1]);
}

function verifyInstalled() {
  const dir = installedDir();
  ck('已安装 Narnat 扩展', !!dir, dir || '未找到');
  if (!dir) return;

  const bad = [];
  for (const rel of MUST_MATCH) {
    const src = path.join(ROOT, rel);
    const dst = path.join(dir, rel);
    if (!fs.existsSync(src)) { bad.push(rel + '（仓库缺该文件）'); continue; }
    if (!fs.existsSync(dst)) { bad.push(rel + '（未安装）'); continue; }
    const a = fs.readFileSync(src);
    const b = fs.readFileSync(dst);
    if (!a.equals(b)) {
      bad.push(rel + '（内容不同：仓库 ' + a.length + 'B / 已装 ' + b.length + 'B）');
    }
  }
  ck('已装文件与仓库逐字节一致', bad.length === 0,
    bad.length ? bad.join('；') : MUST_MATCH.length + ' 个文件');

  // package.json：比对决定行为的字段（vsce 重排格式、VSCode 写 __metadata 属正常）
  const pkgSrc = path.join(ROOT, 'package.json');
  const pkgDst = path.join(dir, 'package.json');
  if (fs.existsSync(pkgSrc) && fs.existsSync(pkgDst)) {
    let same = false;
    try {
      same = pkgFingerprint(pkgSrc) === pkgFingerprint(pkgDst);
    } catch (e) {
      same = false;
    }
    ck('已装 package.json 关键字段与仓库一致', same, PKG_KEYS.join('/'));
  } else {
    ck('已装 package.json 关键字段与仓库一致', false, '文件缺失');
  }

  // 界面形态的硬特征：新版必须有 secondarySidebar 与视图 id
  const pkg = path.join(dir, 'package.json');
  const ext = path.join(dir, 'src', 'extension.js');
  if (fs.existsSync(pkg) && fs.existsSync(ext)) {
    const p = fs.readFileSync(pkg, 'utf8');
    const e = fs.readFileSync(ext, 'utf8');
    ck('已装 package.json 声明 secondarySidebar', p.indexOf('secondarySidebar') >= 0);
    ck('已装 extension.js 含 openChatView', e.indexOf('openChatView') >= 0);
    ck('已装 extension.js 含 sessionNew', e.indexOf('sessionNew') >= 0);
  }
}

function main() {
  if (!VERIFY_ONLY) {
    const args = [path.join(__dirname, 'package-vsix.js')];
    const withExe = shouldIncludeExe();
    if (!withExe) {
      args.push('--skip-exe');
      console.log('（output/narnat.exe 不比 bin 里的新，跳过二进制）');
    } else {
      console.log('（检测到新编译的 exe，随包一起打）');
    }
    try {
      execFileSync(process.execPath, args, { cwd: ROOT, stdio: 'inherit', timeout: 900000 });
    } catch (e) {
      console.error('\n打包失败，停止。');
      process.exit(1);
    }

    ck('vsix 已生成', fs.existsSync(VSIX), fs.existsSync(VSIX)
      ? (fs.statSync(VSIX).size / 1048576).toFixed(1) + 'MB' : '不存在');
    if (!fs.existsSync(VSIX)) { console.log('\nFAILED'); process.exit(1); }

    // 装进本机 VSCode。用绝对路径调 code.cmd，避免 PATH 里是另一份。
    // shell:true 会把命令与参数拼成一行，路径含空格必须自己加引号，
    // 否则含空格的安装目录会被 shell 从空格处截断。
    const codeCmd = resolveCodeCmd();

    // 先注销再安装。直接 --force 覆盖在「扩展目录被手工删过」时会失败：
    // VSCode 的逐条记录还在，于是报「Please restart VS Code before
    // reinstalling」，而此时根本没有 Code 进程在跑，用户完全无路可走
    //（实测）。先 uninstall 把记录清掉，再装就是幂等的。
    try {
      const un = execFileSync('"' + codeCmd + '"',
        ['--uninstall-extension', 'narnat.narnat-vscode'],
        { encoding: 'utf8', timeout: 120000, shell: true });
      if (un && un.trim()) console.log(un.trim());
    } catch (e) {
      // 本来就没装：这条命令非零退出属于正常，不当失败
    }

    try {
      const out = execFileSync('"' + codeCmd + '"',
        ['--install-extension', '"' + VSIX + '"', '--force'],
        { encoding: 'utf8', timeout: 300000, shell: true });
      console.log(out.trim());
      ck('安装到本机 VSCode', /successfully installed/i.test(out), 'code --install-extension');
    } catch (e) {
      ck('安装到本机 VSCode', false, String(e.message).slice(0, 200));
    }
  }

  console.log('\n── 逐字节校验已安装内容 ──');
  verifyInstalled();

  console.log(failed ? '\nFAILED ' + failed : '\nALL_PASS');
  process.exit(failed ? 1 : 0);
}

main();
