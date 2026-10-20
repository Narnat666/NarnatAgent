'use strict';

const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const DEFAULT_TIMEOUT = 30000;
const SHUTDOWN_GRACE = 2000;
const MAX_BUFFER = 8 * 1024 * 1024;

/**
 * NDJSON 分帧器：按 \n 切行，跨 chunk 的半行留在 Buffer 里。
 * 用 Buffer 累积而不是字符串，chunk 切在多字节 UTF-8 字符中间也不会出现替换字符。
 */
class LineParser {
  /**
   * @param {(line: string) => void} [onLine]
   */
  constructor(onLine) {
    this._onLine = typeof onLine === 'function' ? onLine : null;
    /** @type {Buffer} */
    this._buf = Buffer.alloc(0);
  }

  /**
   * 喂入一段字节，返回本条 chunk 里凑齐的完整行。
   * @param {Buffer | string} chunk
   * @returns {string[]}
   */
  feed(chunk) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), 'utf8');
    this._buf = this._buf.length ? Buffer.concat([this._buf, bytes]) : bytes;

    const lines = [];
    let start = 0;
    let nl = this._buf.indexOf(0x0a, start);
    while (nl !== -1) {
      let end = nl;
      if (end > start && this._buf[end - 1] === 0x0d) {
        end -= 1;
      }
      const line = this._buf.toString('utf8', start, end);
      start = nl + 1;
      if (line.trim()) {
        lines.push(line);
      }
      nl = this._buf.indexOf(0x0a, start);
    }

    if (start > 0) {
      this._buf = this._buf.subarray(start);
    }
    if (this._buf.length > MAX_BUFFER) {
      this._buf = Buffer.alloc(0);
      throw new Error('RPC 单行超过 ' + MAX_BUFFER + ' 字节，已丢弃缓冲区');
    }
    if (this._onLine) {
      for (const line of lines) {
        this._onLine(line);
      }
    }
    return lines;
  }

  /** 残留的半个行（诊断用） */
  pending() {
    return this._buf.toString('utf8');
  }

  reset() {
    this._buf = Buffer.alloc(0);
  }
}

class RpcClient {
  /**
   * @param {object} options
   * @param {string} options.command 可执行文件路径，如 narnat.exe
   * @param {string[]} [options.args]
   * @param {string} [options.cwd]
   * @param {object} [options.env]
   * @param {number} [options.timeout] 单请求超时，默认 30 秒
   * @param {(line: string) => void} [options.onLog] stderr / 诊断日志出口
   */
  constructor(options) {
    const opts = options || {};
    if (!opts.command) {
      throw new Error('RpcClient 需要 command');
    }
    this.command = opts.command;
    this.args = Array.isArray(opts.args) ? opts.args : [];
    this.cwd = opts.cwd;
    this.env = opts.env;
    this.timeout = typeof opts.timeout === 'number' && opts.timeout > 0 ? opts.timeout : DEFAULT_TIMEOUT;
    this._log = typeof opts.onLog === 'function' ? opts.onLog : null;

    this._events = new EventEmitter();
    this._events.setMaxListeners(0);
    // 协议里 `error` 是普通通知名（后端下发的运行时错误）。
    // 但 EventEmitter 对 'error' 有特殊语义：没有监听者时直接 throw，
    // 现场表现是后端错误被当成"非法 JSON 行"吞掉，用户永远看不到错误原因。
    // 挂一个空监听即可让它退化成普通事件。
    this._events.on('error', () => {});
    this._parser = new LineParser((line) => this._handleLine(line));

    /** @type {Map<number, {method: string, resolve: Function, reject: Function, timer: any}>} */
    this._pending = new Map();
    this._seq = 0;
    this._child = null;
    this._exited = false;
    this._disposed = false;
    this._disposePromise = null;
    this._exitInfo = null;
    /** @type {string[]} 子进程未启动前的写缓冲 */
    this._queue = [];
  }

  /** 启动子进程。重复调用无副作用。 */
  start() {
    if (this._child || this._disposed) {
      return this;
    }
    let child;
    try {
      child = spawn(this.command, this.args, {
        cwd: this.cwd,
        env: this.env || process.env,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe']
      });
    } catch (err) {
      // 不能在这里先置 _exited = true：_onExit 第一句就是「已退出就返回」，
      // 先置位会把 exit 事件整条吞掉 —— 扩展侧的自动重连、turnActive 收口、
      // 待决请求 reject 全都挂在那个事件上（实测 exit 发出 0 次）。
      // 让 _onExit 自己去置位。
      this._exitInfo = { code: null, signal: null, error: err };
      this._emitLog('spawn 失败：' + (err && err.message ? err.message : err));
      process.nextTick(() => this._onExit(null, null, err));
      return this;
    }

    this._child = child;

    if (child.stdout) {
      child.stdout.on('data', (chunk) => this._feed(chunk));
    }
    if (child.stderr) {
      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (text) => {
        const s = String(text).replace(/\s+$/, '');
        if (s) {
          this._emitLog(s);
        }
      });
    }
    child.on('error', (err) => {
      this._emitLog('子进程错误：' + (err && err.message ? err.message : err));
      this._failAll(err);
      this._onExit(null, null, err);
    });
    // stdin 也要挂 error：后端被强杀后立刻写请求会抛 EPIPE，
    // 没有监听者时是 uncaught 异常，会直接把扩展宿主干掉（实测）。
    if (child.stdin) {
      child.stdin.on('error', (err) => {
        this._emitLog('写入后端失败：' + (err && err.message ? err.message : err));
        this._failAll(err);
        this._onExit(null, null, err);
      });
    }
    child.on('exit', (code, signal) => this._onExit(code, signal, null));

    const queued = this._queue;
    this._queue = [];
    for (const chunk of queued) {
      this._write(chunk);
    }
    return this;
  }

  /** 子进程可用（已 spawn 且未退出） */
  isConnected() {
    return !!this._child && !this._exited && !this._disposed;
  }

  /** 子进程 pid，未启动时为 undefined */
  get pid() {
    return this._child && this._child.pid;
  }

  /**
   * 订阅事件。
   * - `'exit'`：子进程退出 (info:{code,signal,error})
   * - `'log'`：诊断日志 (line)
   * - `'*'`：所有通知 ({method, params})
   * - 其它名字：按通知 method 订阅 handler(params, full)
   * @param {string} name
   * @param {Function} handler
   */
  on(name, handler) {
    this._events.on(name, handler);
    return this;
  }

  off(name, handler) {
    this._events.removeListener(name, handler);
    return this;
  }

  /**
   * 发请求，等待同 id 响应。
   * @param {string} method
   * @param {object} [params]
   * @param {number} [timeoutMs]
   * @returns {Promise<any>}
   */
  request(method, params, timeoutMs) {
    return this._request(method, params, timeoutMs);
  }

  /** 发通知（客户端 → 服务端，无 id、不等待） */
  notify(method, params) {
    this._write({ jsonrpc: '2.0', method, params: params === undefined ? {} : params });
  }

  /**
   * 发 shutdown 请求，最多等 graceMs 让进程自己退，超时则 kill。
   * @param {number} [graceMs]
   * @returns {Promise<void>}
   */
  dispose(graceMs) {
    if (this._disposePromise) {
      return this._disposePromise;
    }
    const grace = typeof graceMs === 'number' ? graceMs : SHUTDOWN_GRACE;
    this._disposePromise = (async () => {
      const child = this._child;
      if (!child || this._disposed) {
        this._disposed = true;
        return;
      }
      if (this._exited) {
        this._disposed = true;
        return;
      }
      const exited = new Promise((resolve) => {
        if (this._exited) {
          resolve('exited');
          return;
        }
        child.once('exit', () => resolve('exited'));
      });
      try {
        // 不用 Math.min(grace,1000)：Python 后端要先让 agent 主循环退出才回响应，
        // 1 秒常不够，会误判超时并打出「未知 id 的响应」噪音
        await this._request('shutdown', {}, grace, true);
      } catch (err) {
        this._emitLog('shutdown 请求失败：' + (err && err.message ? err.message : err));
      }
      const timedOut = await Promise.race([
        exited.then(() => 'exited'),
        new Promise((resolve) => setTimeout(() => resolve('timeout'), grace))
      ]);
      this._disposed = true;
      if (timedOut === 'timeout' && !this._exited) {
        this._emitLog('shutdown 超时，强制 kill');
        try {
          child.kill();
        } catch (err) {
          this._emitLog('kill 失败：' + (err && err.message ? err.message : err));
        }
      }
    })();
    return this._disposePromise;
  }

  /** 强制终止（不做握手） */
  kill(signal) {
    this._disposed = true;
    if (this._child && !this._exited) {
      try {
        this._child.kill(signal);
      } catch (err) {
        this._emitLog('kill 失败：' + (err && err.message ? err.message : err));
      }
    }
  }

  /* ------------------------------ 内部 ------------------------------ */

  /**
   * 把一段 stdout 字节喂给分帧器（RpcClient 的解析入口，测试可直接调用）。
   * @param {Buffer | string} chunk
   * @returns {string[]} 本次凑齐的完整行
   */
  _feed(chunk) {
    try {
      return this._parser.feed(chunk);
    } catch (err) {
      this._emitLog('解析失败：' + (err && err.message ? err.message : err));
      return [];
    }
  }

  _request(method, params, timeoutMs, internal) {
    if (!internal && (this._disposed || this._exited)) {
      return Promise.reject(new Error('RPC 未连接：' + method));
    }
    const ms = typeof timeoutMs === 'number' && timeoutMs > 0 ? timeoutMs : this.timeout;
    const id = ++this._seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const entry = this._pending.get(id);
        if (!entry) {
          return;
        }
        this._pending.delete(id);
        reject(new Error('请求超时（' + ms + 'ms 无响应）：' + method));
      }, ms);
      if (timer.unref) {
        timer.unref();
      }
      this._pending.set(id, { method, resolve, reject, timer });
      this._write({ jsonrpc: '2.0', id, method, params: params === undefined ? {} : params });
    });
  }

  _write(message) {
    const line = JSON.stringify(message) + '\n';
    if (!this._child || !this._child.stdin || this._child.stdin.destroyed) {
      if (!this._exited && !this._disposed) {
        this._queue.push(line);
      }
      return;
    }
    try {
      this._child.stdin.write(line);
    } catch (err) {
      this._emitLog('写入失败：' + (err && err.message ? err.message : err));
    }
  }

  _handleLine(line) {
    let msg;
    try {
      // 去掉可能存在的 BOM：后端若用 utf-8-sig 输出，每一行都会带 U+FEFF，
      // JSON.parse 会拒绝全部响应 —— 表现是「所有请求超时、界面每次发送都卡在
      // 运行中」，而后端其实活得好好的（实测）。去掉首字符是零成本的防御。
      if (line.charCodeAt(0) === 0xfeff) line = line.slice(1);
      msg = JSON.parse(line);
    } catch (err) {
      this._emitLog('非法 JSON 行：' + line.slice(0, 200));
      return;
    }
    if (!msg || typeof msg !== 'object') {
      return;
    }
    const hasId = msg.id !== undefined && msg.id !== null;
    if (hasId && (msg.result !== undefined || msg.error !== undefined)) {
      const entry = this._pending.get(msg.id);
      if (!entry) {
        // 已 dispose 时响应晚到属正常竞态（shutdown 超时后进程才回），不记日志
        if (!this._disposed) {
          this._emitLog('收到未知 id 的响应：' + msg.id);
        }
        return;
      }
      this._pending.delete(msg.id);
      clearTimeout(entry.timer);
      if (msg.error) {
        const err = new Error(
          (msg.error && msg.error.message) || 'JSON-RPC 错误 ' + msg.error.code
        );
        err.code = msg.error.code;
        err.data = msg.error.data;
        err.method = entry.method;
        entry.reject(err);
      } else {
        entry.resolve(msg.result);
      }
      return;
    }
    if (typeof msg.method === 'string') {
      const full = { method: msg.method, params: msg.params === undefined ? {} : msg.params };
      this._events.emit(msg.method, full.params, full);
      this._events.emit('*', full);
    }
  }

  _onExit(code, signal, error) {
    if (this._exited) {
      return;
    }
    this._exited = true;
    this._exitInfo = { code, signal, error };
    this._failAll(error || new Error('后端子进程已退出（code=' + code + '）'));
    this._events.emit('exit', this._exitInfo);
  }

  _failAll(error) {
    if (!this._pending.size) {
      return;
    }
    const err = error instanceof Error ? error : new Error(String(error));
    const entries = Array.from(this._pending.values());
    this._pending.clear();
    for (const entry of entries) {
      clearTimeout(entry.timer);
      entry.reject(new Error('请求失败（' + entry.method + '）：' + err.message));
    }
  }

  _emitLog(text) {
    if (this._log) {
      this._log(text);
    }
    this._events.emit('log', text);
  }
}

module.exports = { RpcClient, LineParser, DEFAULT_TIMEOUT };
