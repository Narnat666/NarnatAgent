'use strict';

const { spawn } = require('child_process');
const { EventEmitter } = require('events');

const DEFAULT_TIMEOUT = 30000;
const SHUTDOWN_GRACE = 2000;
const MAX_BUFFER = 8 * 1024 * 1024;

/**
 * 标记一条「确定没发出去」的错误。
 *
 * 调用方（extension.js）只有看到 sent === false 才允许对用户说“消息未发送”；
 * 超时/连接中断但请求已写出时一律按「可能已执行」处理——后端可能已经把这一轮
 * 跑完了，用户重发就是再执行一遍（实测：磁盘上出现两份同样的会话）。
 * @param {Error} err
 * @param {string} [method]
 * @returns {Error}
 */
function notSent(err, method) {
  err.sent = false;
  err.writeFailed = true;
  if (method) {
    err.method = method;
  }
  return err;
}

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
    /**
     * 进程**根本没被创建**（spawn ENOENT/EACCES/ENOTDIR）。
     *
     * 与「进程起来后又退出」必须分开：后者写进 stdin 的字节可能已被处理，
     * 只能按「可能已执行」上报；前者写进的是一个没有任何进程读的管道，
     * 字节永远不会到达后端 —— 一律算「未写出」，否则界面会告诉用户
     * 「可能已在执行」，而消息其实从未送达（用户因此不重发，消息就丢了）。
     * @type {boolean}
     */
    this._spawnFailed = false;
    /**
     * 子进程未启动（或 stdin 暂不可写）时的写缓冲。
     * 元素类型与 _write() 的入参 **必须一致**：都是未序列化的原始 message 对象，
     * 不是 NDJSON 行串。序列化只在 _write() 内发生一次，flush 时把对象原样回灌
     * _write()，绝不在这里二次 stringify（否则后端收到顶层 JSON 字符串，
     * 回 Invalid Request，请求只能等满 timeout）。
     * @type {object[]}
     */
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
      this._spawnFailed = true;
      this._queue = [];
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
      // pid 为 undefined = 进程从未被创建（ENOENT/EACCES/ENOTDIR 都走这里）。
      // 此时 stdin 是个死管道：之前 write() 返回成功只代表「字节进了缓冲区」，
      // 没有任何进程会读到它 —— 所以把所有待决请求复位成「未写出」。
      const unsent = this._spawnFailed || !child.pid;
      if (unsent) {
        this._spawnFailed = true;
        // 队列里是还没写出去的原始消息：进程都没起来，留着只会在下次 start()
        // 时被 flush 成「已写出」，等于把没发出去的话报成可能已执行。
        this._queue = [];
      }
      this._emitLog('子进程错误：' + (err && err.message ? err.message : err) +
        (unsent ? '（进程未启动，本次写入一律按「未发出」处理）' : ''));
      this._failAll(err, unsent);
      this._onExit(null, null, err);
    });
    // stdin 也要挂 error：后端被强杀后立刻写请求会抛 EPIPE，
    // 没有监听者时是 uncaught 异常，会直接把扩展宿主干掉（实测）。
    if (child.stdin) {
      child.stdin.on('error', (err) => {
        const unsent = this._spawnFailed || !child.pid;
        this._emitLog('写入后端失败：' + (err && err.message ? err.message : err));
        this._failAll(err, unsent);
        this._onExit(null, null, err);
      });
    }
    child.on('exit', (code, signal) => this._onExit(code, signal, null));

    // 队列里是原始 message 对象，直接交给 _write() 序列化（它期望对象入参）
    const queued = this._queue;
    this._queue = [];
    for (const message of queued) {
      const state = this._write(message);
      // flush 出去的请求要补记「确实写进 stdin 了」：调用方靠这个字段区分
      // 「没发出去」与「发出去了但回执超时」（前者才能对用户说“未发送”）。
      if (state === 'written' && message && message.id !== undefined && message.id !== null) {
        const entry = this._pending.get(message.id);
        if (entry) {
          entry.written = true;
        }
      }
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
   * 进程是否从未启动成功（spawn ENOENT/EACCES/ENOTDIR）。
   *
   * 调用方靠它区分「后端崩了」（写出去的字节可能已被执行 → 只能说「可能已执行」）
   * 与「后端根本没起来」（字节没被任何进程读过 → 必须说「没发出去」）。
   * @returns {boolean}
   */
  get spawnFailed() {
    return this._spawnFailed;
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
      return Promise.reject(notSent(new Error('RPC 未连接：' + method), method));
    }
    const ms = typeof timeoutMs === 'number' && timeoutMs > 0 ? timeoutMs : this.timeout;
    const id = ++this._seq;
    const entry = { method, resolve: null, reject: null, timer: null, written: false };
    const promise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (!this._pending.has(id)) {
          return;
        }
        this._pending.delete(id);
        const err = new Error('请求超时（' + ms + 'ms 无响应）：' + method);
        err.method = method;
        err.timeout = true;
        err.timeoutMs = ms;
        // 已写出但没等到回执：请求可能已被后端执行，调用方**不能**说“未发送”
        err.sent = !!entry.written;
        reject(err);
      }, ms);
      if (timer.unref) {
        timer.unref();
      }
      entry.resolve = resolve;
      entry.reject = reject;
      entry.timer = timer;
      this._pending.set(id, entry);
      const state = this._write({ jsonrpc: '2.0', id, method, params: params === undefined ? {} : params });
      entry.written = state === 'written';
      if (state === 'failed' || state === 'dropped') {
        // 写入就失败了（EPIPE / 子进程不在）：确定没到达后端，立即报错不必等满超时
        clearTimeout(timer);
        this._pending.delete(id);
        entry.written = false;
        reject(notSent(new Error('请求未发出（写入失败）：' + method), method));
      }
    });
    // 写进 stdin 是**同步**发生的：promise 一返回，这条请求「有没有真的写出去」
    // 就已经定了。宿主要在最坏情况（进程崩溃）的同步路径上判断这一点——
    // 那时它只能拿到 promise，拿不到 reject 之后才有的 err.sent。
    // 用 getter 而不是快照：请求可能先排队（子进程还没就绪）稍后才 flush 进stdin。
    Object.defineProperty(promise, 'written', {
      get: () => entry.written === true,
      configurable: true
    });
    return promise;
  }

  /**
   * 唯一的序列化点：入参是原始 JSON-RPC 消息对象；_queue 里存的也是同一种对象，
   * 所以任何时候一行 NDJSON 都只被 stringify 一次。
   * @param {object} message
   * @returns {'written'|'queued'|'failed'|'dropped'} 写入结果：
   *   written=已写进 stdin；queued=暂存等子进程可用；failed=写入抛错；dropped=已退出丢弃
   */
  _write(message) {
    if (this._spawnFailed) {
      // 进程根本没起来：绝不能返回 written/queued，否则调用方会把这条算成
      // 「已写出、可能已执行」——而它永远不会到达后端。
      return 'failed';
    }
    if (!this._child || !this._child.stdin || this._child.stdin.destroyed) {
      if (!this._exited && !this._disposed) {
        this._queue.push(message);
        return 'queued';
      }
      return 'dropped';
    }
    const line = JSON.stringify(message) + '\n';
    try {
      this._child.stdin.write(line);
      return 'written';
    } catch (err) {
      this._emitLog('写入失败：' + (err && err.message ? err.message : err));
      return 'failed';
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

  /**
   * 把所有待决请求 reject 掉。
   * @param {any} error 失败原因
   * @param {boolean} [forceUnsent] 进程从未启动（spawn 失败）：写入记录一律作废。
   *        write() 返回成功只代表字节进了管道缓冲，没有进程读过它，
   *        必须复位成「未写出」，否则界面会说「可能已在执行」而消息其实没送到。
   */
  _failAll(error, forceUnsent) {
    if (!this._pending.size) {
      return;
    }
    const err = error instanceof Error ? error : new Error(String(error));
    const entries = Array.from(this._pending.values());
    this._pending.clear();
    for (const entry of entries) {
      clearTimeout(entry.timer);
      const e = new Error('请求失败（' + entry.method + '）：' + err.message);
      e.method = entry.method;
      if (forceUnsent) {
        entry.written = false;
        e.writeFailed = true;
      }
      // 连接级失败：只有「本来就没写出去」才算确定未送达；写出去过的一律按
      // 「可能已执行」上报，否则用户会重发、后端再执行一遍。
      e.sent = !!entry.written;
      entry.reject(e);
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
