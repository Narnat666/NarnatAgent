'use strict';

/**
 * 秒级同步：把仓库里的扩展文件直接覆盖到已装扩展目录，跳过 vsix 打包。
 *
 * 为什么需要它：`install-vsix.js` 要跑 vsce 打包 93MB 的 vsix 再装卸，
 * 一次几分钟；改一行前端想看效果时这个循环太慢。开发阶段只要文件
 * 落到扩展目录即可，VSCode 重载窗口就生效。
 *
 * 注意：它只覆盖文件，不改 VSCode 的扩展注册记录。所以
 *   - 扩展**已经装过**（目录存在）时可用
 *   - 全新安装、或改过 package.json 的 activationEvents/contributes 时，
 *     仍要用 install-vsix.js 走正式装卸
 *
 * 用法：
 *   node scripts/sync-ext.js            # 同步文件
 *   node scripts/sync-ext.js --verify   # 只校验（不写）
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const VERIFY_ONLY = process.argv.includes('--verify');

/** 需要同步到扩展目录的文件（相对仓库 vscode-extension/） */
const FILES = [
  'package.json',
  'src/extension.js',
  'src/rpc-client.js',
  'src/webview.js',
  'media/narnat.js',
  'media/narnat.css',
  'media/narnat.body.js',
  'media/icon.svg'
];

let failed = 0;
function ck(name, ok, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' → ' + detail : ''));
  if (!ok) failed++;
}

/** 找已装扩展目录（可能有多个版本，取第一个） */
function findInstalled() {
  const base = path.join(os.homedir(), '.vscode', 'extensions');
  if (!fs.existsSync(base)) return null;
  const dirs = fs.readdirSync(base)
    .filter((d) => /^narnat\.narnat-vscode-/.test(d))
    .map((d) => path.join(base, d));
  if (!dirs.length) return null;
  // 版本号最大的那个
  dirs.sort();
  return dirs[dirs.length - 1];
}

function md5(file) {
  const crypto = require('crypto');
  return crypto.createHash('md5').update(fs.readFileSync(file)).digest('hex');
}

function main() {
  // 先确保 media/ 是从设计真源同步来的，否则会推上去旧界面
  try {
    require('child_process').execFileSync(process.execPath,
      [path.join(__dirname, 'sync-ui.js')], { encoding: 'utf8', stdio: 'pipe' });
    console.log('PASS 已同步 design/ui-full.html → media/');
  } catch (e) {
    ck('同步 design → media', false, String(e && e.stderr ? e.stderr : e.message).slice(0, 160));
  }

  const ext = findInstalled();
  if (!ext) {
    console.log('\n未找到已装扩展。请先跑一次 node scripts/install-vsix.js 做正式安装。');
    process.exit(1);
  }
  console.log('目标扩展目录: ' + ext);

  const copied = [];
  for (const rel of FILES) {
    const from = path.join(ROOT, rel);
    const to = path.join(ext, rel);
    if (!fs.existsSync(from)) {
      ck(rel + ' 源文件存在', false);
      continue;
    }
    if (!VERIFY_ONLY) {
      fs.mkdirSync(path.dirname(to), { recursive: true });
      fs.copyFileSync(from, to);
    }
    const same = fs.existsSync(to) && md5(from) === md5(to);
    ck(rel + (VERIFY_ONLY ? ' 一致' : ' 已同步'), same);
    if (same) copied.push(rel);
  }

  console.log('\n' + copied.length + '/' + FILES.length + ' 个文件一致');
  if (!VERIFY_ONLY && !failed) {
    console.log('→ 在 VSCode 里执行「Developer: Reload Window」即可看到改动');
  }
  console.log(failed ? '\nFAILED ' + failed : '\nALL_PASS');
  process.exit(failed ? 1 : 0);
}

main();
