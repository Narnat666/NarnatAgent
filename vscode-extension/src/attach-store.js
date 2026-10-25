'use strict';

/**
 * 用户附图的落盘与核对（纯函数，不依赖 vscode）。
 *
 * ## 为什么要单独一层
 *
 * 这段逻辑决定「用户拖进来的图会不会丢」，是用户报障过的点
 *（「图丢了」、「dsh 也能找到吗」）。放在 extension.js 里就必须带着
 * `require('vscode')` 才能加载，等于不可测 —— 而这条路径恰恰最需要测试
 * （去重、原文件消失、超大图、已是库内路径不该重复复制）。
 *
 * 所以：目录推导、入库、核对三件事都在这里，extension.js 只做「把
 * 配置路径喂进来」的胶水。可直接 `require` 并用 node 跑断言。
 *
 * ## 为什么单独一个库目录，而不是复用后端附件库
 *
 * 后端附件库（`narnat_agent/core/attachment.py`）存的是内容寻址、
 * **无扩展名**的归一化对象（`<data>/attachments/<2>/<sha>`）。而 Read 工具
 * 靠扩展名判定图片（`tools/read/__init__.py` 的 `_IMAGE_EXTS`）——
 * 无扩展名的对象 AI 读不了。所以用户附图单独放 `<data>/attachments_user/`，
 * 内容寻址命名但**保留扩展名**。
 *
 * ## 为什么不放系统临时目录
 *
 * 原实现写 `os.tmpdir()/narnat_attachments`。Windows 磁盘清理与各类清理
 * 工具默认就清 %TEMP%，图随时消失，历史里那条消息就永久变成「图片已不在」。
 * dsh 的图不丢也不是因为存了多份，而是因为它存在 DSH_HOME 这种不会被系统
 * 清理的位置（`attachment-local/src/store.ts` 的 `DSH_HOME/attachments/v1`）。
 * 这里对齐那个思路。
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

/** 图片扩展名 → MIME */
const IMAGE_MIME = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
  '.svg': 'image/svg+xml',
};

/**
 * 取路径的图片扩展名（小写，带点）。
 * @param {string} p
 * @returns {string} 不是已知图片扩展名时返回空串
 */
function imageExt(p) {
  const m = /\.([A-Za-z0-9]+)$/.exec(String(p || ''));
  if (!m) return '';
  const ext = '.' + m[1].toLowerCase();
  return IMAGE_MIME[ext] ? ext : '';
}

/**
 * 由 narnat.json 的绝对路径推出用户附图目录。
 *
 * `<项目>/.narnat/config/narnat.json` → `<项目>/.narnat/data/attachments_user`
 * @param {string} configPath narnat.json 的绝对路径（可为空）
 * @param {string} fallbackDir 推不出来时的兜底目录（通常是系统临时目录）
 * @returns {string}
 */
function userImageDir(configPath, fallbackDir) {
  if (configPath) {
    // config/narnat.json 往上两级就是 .narnat
    const narnatDir = path.dirname(path.dirname(configPath));
    return path.join(narnatDir, 'data', 'attachments_user');
  }
  return fallbackDir;
}

/**
 * 把图片字节存进用户附图目录（内容寻址 + 保留扩展名）。
 *
 * 同一张图存多少次都只有一份（sha 前 16 位做文件名），重复调用是幂等的。
 * @param {string} dir 库目录
 * @param {Buffer} buf 图片字节
 * @param {string} name 原始文件名（只用来取扩展名）
 * @returns {string} 落盘绝对路径
 */
function storeUserImage(dir, buf, name) {
  const ext = imageExt(name) || '.png';
  const sha = crypto.createHash('sha256').update(buf).digest('hex');
  fs.mkdirSync(dir, { recursive: true });
  const target = path.join(dir, sha.slice(0, 16) + ext);
  if (!fs.existsSync(target)) fs.writeFileSync(target, buf);
  return target;
}

/**
 * 判断一条路径是不是已经在库目录里（避免把库内文件再复制一遍）。
 * @param {string} p
 * @param {string} dir
 * @returns {boolean}
 */
function isInUserImageDir(p, dir) {
  try {
    return path.dirname(path.resolve(p)) === path.resolve(dir);
  } catch (err) {
    return false;
  }
}

/**
 * 核对一批路径：存在性、类型、字节数；**图片会被复制进库**。
 *
 * 图片返回库路径（`path`），原路径放在 `source` 里只作说明。复制失败
 * （超大 / 读不了 / 磁盘满）时保留原路径 —— 功能降级，但附件仍可用。
 *
 * `bytes` 是给界面显示用的（用户消息气泡里的文件卡写「PY 4.2KB」，
 * 与 dsh 的 `UserContent` 一致）。文件夹给 `-1`：目录没有有意义的字节数，
 * 界面据此显示「文件夹」而不是「0B」。
 * @param {string[]} paths
 * @param {object} opts
 * @param {string} opts.dir 用户附图目录
 * @param {number} opts.maxBytes 单张图片复制上限
 * @param {(msg: string) => void} [opts.onWarn]
 * @returns {{path: string, source?: string, exists: boolean, kind: string, bytes: number}[]}
 */
function checkPaths(paths, opts) {
  const dir = opts && opts.dir;
  const maxBytes = (opts && opts.maxBytes) || 8 * 1024 * 1024;
  const onWarn = (opts && opts.onWarn) || (() => {});
  const items = [];

  for (const raw of (Array.isArray(paths) ? paths : [])) {
    const p = String(raw || '');
    if (!p) continue;

    let exists = false;
    let isDir = false;
    let size = 0;
    try {
      exists = fs.existsSync(p);
      if (exists) {
        const st = fs.statSync(p);
        isDir = st.isDirectory();
        // 目录的 st.size 是目录项本身的大小（Windows 通常 0），拿它做展示
        // 会显示成「文件夹 0B」；用 -1 明确表达「这个类型没有字节数」。
        size = isDir ? -1 : st.size;
      }
    } catch (err) {
      exists = false;
    }
    const kind = !exists ? '' : (isDir ? 'folder' : (imageExt(p) ? 'image' : 'file'));
    const bytes = exists ? size : 0;

    if (!exists || kind !== 'image') {
      items.push({ path: p, exists, kind, bytes });
      continue;
    }
    if (!dir || isInUserImageDir(p, dir)) {
      // 已经在库里（粘贴截图走的就是这条路），不重复复制
      items.push({ path: p, source: p, exists: true, kind, bytes });
      continue;
    }
    try {
      const buf = fs.readFileSync(p);
      if (buf.length > maxBytes) {
        items.push({ path: p, source: p, exists: true, kind, bytes });
        continue;
      }
      const stored = storeUserImage(dir, buf, p);
      items.push({ path: stored, source: p, exists: true, kind, bytes });
    } catch (err) {
      onWarn('图片入库失败，保留原路径：' + (err && err.message ? err.message : String(err)));
      items.push({ path: p, source: p, exists: true, kind, bytes });
    }
  }
  return items;
}

module.exports = {
  IMAGE_MIME, imageExt, userImageDir, storeUserImage, isInUserImageDir, checkPaths,
};
