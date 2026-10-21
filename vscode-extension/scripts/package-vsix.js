'use strict';

/**
 * 打包 vsix：把编译好的 narnat.exe 内嵌进扩展，用户装插件即可用。
 *
 * 步骤：
 *   1. 校验 output/narnat.exe（或 --exe 指定）存在且能响应 -v
 *   2. 复制到 vscode-extension/bin/narnat.exe
 *   3. 用 @vscode/vsce 打包成 .vsix
 *   4. 校验 vsix 内确实包含 bin/narnat.exe（用 tar -tf 列目录）
 *
 * 用法：
 *   node scripts/package-vsix.js                 # 用 output/narnat.exe
 *   node scripts/package-vsix.js --exe D:\x\narnat.exe
 *   node scripts/package-vsix.js --skip-exe      # 不打包二进制，但仍生成 vsix
 *   node scripts/package-vsix.js --no-package    # 只校验结构，不生成 vsix
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const REPO = path.resolve(ROOT, '..');
const BIN_DIR = path.join(ROOT, 'bin');
const BIN_EXE = path.join(BIN_DIR, 'narnat.exe');

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
// --skip-exe 只跳过二进制；打包另由 --no-package 控制。
// 曾把两者绑在同一个开关上，导致「以为打过包其实没打包」，
// 用户装到的还是旧 vsix（界面停在旧形态）。
const SKIP_EXE = process.argv.includes('--skip-exe');
const NO_PACKAGE = process.argv.includes('--no-package');
const EXE = path.resolve(arg('--exe', path.join(REPO, 'output', 'narnat.exe')));

let failed = 0;
let criticalFailed = 0;
/**
 * isCritical：关键校验（exe 可执行、支持 --rpc、RPC 方法集完整）。
 * 关键校验一旦 FAIL 就中止，绝不继续把 exe 复制进 bin/、也不重新打包 vsix——
 * 否则上一次的好产物会被坏产物覆盖（实测：bin 被 0 字节 exe 覆盖后仍报 PASS）。
 */
function ck(name, ok, detail, isCritical) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name +
    (isCritical ? '（关键校验）' : '') + (detail ? ' → ' + detail : ''));
  if (!ok) {
    failed++;
    if (isCritical) criticalFailed++;
  }
}

/**
 * 起一次 exe --rpc，逐个探方法是否已注册。
 *
 * 判据只看返回是不是 `Method not found`（-32601）：
 * 参数错误（-32602）、业务拒绝（-32000）都说明方法存在。
 */
function probeRpcMethods(exe, methods) {
  const out = { notFound: [], responses: 0, error: '' };
  const os = require('os');
  let tmpHome;
  try {
    tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'pkgexe-'));
  } catch (e) {
    out.error = String(e.message);
    return out;
  }
  const lines = methods.map((m, i) => JSON.stringify({
    jsonrpc: '2.0', id: i + 1, method: m, params: {}
  })).join('\n') + '\n';
  try {
    const res = require('child_process').spawnSync(
      exe, ['--rpc'],
      {
        input: lines, encoding: 'utf8', timeout: 90000,
        env: Object.assign({}, process.env, { NARNAT_HOME: tmpHome }),
        cwd: tmpHome
      });
    const stdout = (res && res.stdout) || '';
    for (const raw of stdout.split('\n')) {
      const s = raw.trim();
      if (!s.startsWith('{')) continue;
      let msg;
      try { msg = JSON.parse(s); } catch (e) { continue; }
      if (!msg || typeof msg.id !== 'number') continue;
      const name = methods[msg.id - 1];
      if (!name) continue;
      out.responses++;
      const err = msg.error;
      if (err && (err.code === -32601 || /method not found/i.test(String(err.message || '')))) {
        if (out.notFound.indexOf(name) < 0) out.notFound.push(name);
      }
    }
    // 一条可用的 RPC 响应都没有（读 stdin 后一句不回、或只吐非 JSON-RPC 文本）：
    // 「方法没缺失」只是因为压根没探到，不能算通过。
    if (out.responses === 0) {
      out.error = 'exe 无任何 RPC 响应（' +
        (stdout.trim() ? '输出非 JSON-RPC：' + stdout.trim().slice(0, 120)
          : String((res && res.stderr) || '').slice(0, 160)) + '）';
    }
  } catch (e) {
    out.error = String(e && e.message);
  } finally {
    try { fs.rmSync(tmpHome, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
  }
  return out;
}

/**
 * 列出 vsix（本质是 zip）内的条目名与解压后大小。
 *
 * 原来靠 `tar -tf`：Windows 上的 GNU tar 会把 `E:\...` 里的盘符当成远程主机，
 * 报 `tar: Cannot connect to E: resolve failed`，于是下面的条目校验被整体跳过，
 * 「包里根本没 exe」也能一路 ALL_PASS。这里改读 zip 的中央目录，不再依赖外部工具。
 */
function listZipEntries(file) {
  const buf = fs.readFileSync(file);
  let eocd = -1;
  // EOCD（End of Central Directory）签名 0x06054b50，后面可能跟最多 64KB 注释
  const stop = Math.max(buf.length - 22 - 0xffff, 0);
  for (let i = buf.length - 22; i >= stop; i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('不是有效的 zip（未找到 EOCD）');

  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const entries = [];
  for (let i = 0; i < count; i++) {
    if (p + 46 > buf.length || buf.readUInt32LE(p) !== 0x02014b50) {
      throw new Error('中央目录第 ' + (i + 1) + ' 条损坏');
    }
    const size = buf.readUInt32LE(p + 24);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    entries.push({
      name: buf.toString('utf8', p + 46, p + 46 + nameLen),
      size
    });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

function main() {
  // ── 0. 先同步设计真源 → media/ ──
  // 不先同步会打出旧 UI 的包（曾出现「源码改了但安装后没变」）
  try {
    execFileSync(process.execPath, [path.join(__dirname, 'sync-ui.js')],
      { encoding: 'utf8', stdio: 'pipe' });
    ck('已同步 design/ui-full.html → media/', true);
  } catch (e) {
    ck('已同步 design/ui-full.html → media/', false,
      String(e && e.stderr ? e.stderr : e.message).slice(0, 200));
  }

  // ── 1. 二进制 ──
  if (SKIP_EXE) {
    console.log('SKIP 二进制检查（--skip-exe）');
  } else {
    ck('编译产物存在', fs.existsSync(EXE), EXE);
    if (!fs.existsSync(EXE)) {
      console.log('\n先跑 python build.py 生成 ' + EXE);
      process.exit(1);
    }
    let ver = '';
    let verErr = '';
    try {
      ver = execFileSync(EXE, ['-v'], { encoding: 'utf8', timeout: 60000 }).trim();
    } catch (e) {
      verErr = String(e.message).slice(0, 120);
    }
    // 打不出 -v（异常退出或干脆没输出）都算关键校验失败：
    // 原来「无输出且不抛异常」时这条校验会被整条跳过，等于没查。
    ck('narnat.exe -v 可执行', !verErr && /^narnat \d+\.\d+\.\d+/.test(ver),
      verErr || ver || '未输出任何内容', true);
    // 关键：新编译的 exe 必须支持 --rpc
    let help = '';
    try {
      help = execFileSync(EXE, ['--help'], { encoding: 'utf8', timeout: 60000 });
    } catch (e) { /* --help 可能非零退出 */ }
    ck('exe 支持 --rpc', help.indexOf('--rpc') >= 0,
      help.indexOf('--rpc') >= 0 ? '已声明' : '未找到 --rpc（exe 过旧，需重新编译）', true);

    // 关键：exe 的 RPC 方法集必须与源码一致。
    // 只校验「能跑 -v、含 --rpc」是不够的——实测交付过一个 09:59 的旧 exe，
    // 它缺 session/new、session/rename，客户点「新建对话」直接报
    // Method not found，而打包流程一路绿灯（因为它确实能 -v、确实含 --rpc）。
    // 这里真的起一次 RPC，逐个探方法是否已注册。
    const REQUIRED_METHODS = [
      'initialize', 'session/list', 'session/new', 'session/switch',
      'session/delete', 'session/rename', 'session/explore', 'session/done',
      'chat/send', 'chat/cancel', 'command/exec', 'config/set', 'confirm/reply'
    ];
    const probe = probeRpcMethods(EXE, REQUIRED_METHODS);
    const missing = REQUIRED_METHODS.filter((m) => probe.notFound.indexOf(m) >= 0);
    // 判据是「既有响应、也没有方法缺失」：
    // probe.error 非空表示一条 RPC 响应都没拿到，此时 missing 必然为空，
    // 但那是「没探到」而不是「方法齐全」——实测一个读完 stdin 一句不回的桩 exe
    // 曾被判 PASS「已注册 13 个方法」（实际响应 0 条）并一路 ALL_PASS 进 dist/。
    const rpcOk = !probe.error && missing.length === 0;
    ck('exe 的 RPC 方法集与源码一致', rpcOk,
      probe.error ? probe.error
        : missing.length
          ? '缺失: ' + missing.join(', ') + '（exe 过旧，必须先 python build.py 重新编译）'
          : '已注册 ' + REQUIRED_METHODS.length + ' 个方法', true);

    // 关键校验不过就停，不能继续往下复制。
    // 继续复制的话 bin/narnat.exe 已经被换成了坏 exe，而命令最终仍返回
    // FAILED——下一次只跑 install-vsix 而不跑 package 时，就会把坏 exe
    // 装到用户机上（实测踩过：校验报 FAIL 但 bin 已被覆盖）。
    if (criticalFailed) {
      console.log('\n中止：' + criticalFailed +
        ' 项关键校验未通过，未复制到 bin/、未打包 vsix（上一份产物保持原样）。');
      process.exit(1);
    }

    fs.mkdirSync(BIN_DIR, { recursive: true });
    fs.copyFileSync(EXE, BIN_EXE);
    const size = fs.statSync(BIN_EXE).size;
    ck('已复制到 bin/narnat.exe', fs.existsSync(BIN_EXE), (size / 1048576).toFixed(1) + 'MB');
  }

  // ── 2. 扩展必备文件 ──
  const need = [
    'package.json', 'src/extension.js', 'src/rpc-client.js', 'src/webview.js',
    'media/narnat.css', 'media/narnat.js', 'media/narnat.body.js', 'media/icon.svg',
    'README.md'
  ];
  const missing = need.filter((f) => !fs.existsSync(path.join(ROOT, f)));
  ck('扩展文件齐全', missing.length === 0, missing.length ? '缺 ' + missing.join(',') : need.length + ' 个');

  // ── 3. .vscodeignore（排除测试与设计源，控制体积）──
  const ignorePath = path.join(ROOT, '.vscodeignore');
  const wantIgnore = [
    '.vscode-test/**', 'node_modules/**', 'test/**', 'design/**',
    'scripts/**', 'shots/**', '.playwright-mcp/**', '**/*.map'
  ];
  let cur = '';
  try { cur = fs.readFileSync(ignorePath, 'utf8'); } catch (e) { /* 新建 */ }
  const lines = cur.split('\n').map((s) => s.trim()).filter(Boolean);
  let changed = false;
  for (const w of wantIgnore) {
    if (lines.indexOf(w) < 0) { lines.push(w); changed = true; }
  }
  if (changed) {
    fs.writeFileSync(ignorePath, lines.join('\n') + '\n', 'utf8');
  }
  ck('.vscodeignore 已就绪', lines.length >= wantIgnore.length,
    lines.length + ' 条规则');

  // ── 4. 打包 ──
  if (NO_PACKAGE) {
    console.log('\nSKIP vsce 打包（--no-package）');
  } else {
    const out = path.join(ROOT, 'narnat-vscode.vsix');
    try {
      fs.rmSync(out, { force: true });
      execFileSync('npx', ['--yes', '@vscode/vsce', 'package', '--no-dependencies',
        '--allow-missing-repository', '-o', out], {
        cwd: ROOT, stdio: 'inherit', timeout: 600000, shell: true
      });
    } catch (e) {
      ck('vsce 打包', false, String(e.message).slice(0, 200));
    }
    if (fs.existsSync(out)) {
      const mb = (fs.statSync(out).size / 1048576).toFixed(1);
      ck('生成 vsix', true, mb + 'MB  ' + out);
      // 校验内含 exe（解析 zip 中央目录；不可解析本身就算失败，不能再静默跳过）
      let entries = null;
      try {
        entries = listZipEntries(out);
      } catch (e) {
        ck('可列出 vsix 内容', false, String(e.message).slice(0, 160));
      }
      if (entries) {
        const names = entries.map((x) => x.name);
        const exeEntry = entries.filter((x) => x.name === 'extension/bin/narnat.exe')[0];
        if (SKIP_EXE) {
          console.log('WARN 本次未打包二进制，vsix 内无 bin/narnat.exe（开发态可用源码后端）');
        } else {
          ck('vsix 内含 bin/narnat.exe', !!exeEntry,
            exeEntry ? exeEntry.size + ' 字节 / 共 ' + names.length + ' 个条目'
              : '未找到（共 ' + names.length + ' 个条目）');
          // 大小必须与 bin/ 里那份完全一致：曾出现「打进包的是旧 exe」
          if (exeEntry) {
            const binSize = fs.statSync(BIN_EXE).size;
            ck('vsix 内 bin/narnat.exe 与 bin/ 大小一致', exeEntry.size === binSize,
              exeEntry.size + ' / ' + binSize + ' 字节');
          }
        }
        ck('vsix 不含 test/', names.indexOf('extension/test/') < 0
          && !names.some((n) => n.indexOf('extension/test/') === 0));
        ck('vsix 不含 design/', !names.some((n) => n.indexOf('extension/design/') === 0));
        // 曾出过「装的还是旧界面」事故：确认界面产物确实进了包
        ck('vsix 含 media/narnat.js', names.indexOf('extension/media/narnat.js') >= 0);
        ck('vsix 含 src/extension.js', names.indexOf('extension/src/extension.js') >= 0);
      }
    } else {
      ck('生成 vsix', false, '未生成 ' + out);
    }
  }

  console.log(failed ? '\nFAILED ' + failed : '\nALL_PASS');
  process.exit(failed ? 1 : 0);
}

main();
