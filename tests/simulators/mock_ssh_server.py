"""仿真SSH服务器 —— 本地闭环测试Terminal/Remote工具

基于paramiko的SSH server实现，模拟真实Linux主机行为。
支持:
  - invoke_shell PTY交互式终端
  - SFTP子系统（remote.py测试需要）
  - 虚拟文件系统（mkdir/touch/cat/ls等操作真实可见）
  - sudo密码验证
  - 后台进程模拟(nohup/disown)

用法:
    with MockSSHServer() as server:
        result = terminal.execute(
            action="connect",
            host=server.host,
            username=server.username,
            password=server.password,
            port=server.port,
        )
"""

import os
import re
import socket
import stat
import threading
import time
from typing import Optional

import paramiko


# ── 虚拟文件系统 ──

class VirtualFileSystem:
    """仿真Linux文件系统

    在内存中维护目录树和文件内容，支持mkdir/touch/write/read/ls/stat等操作。
    SimulatedShell通过VFS操作文件，SFTP子系统也通过VFS提供文件传输。
    """

    def __init__(self, username: str = "testuser"):
        self.username = username
        home = f"/home/{username}"
        # _nodes: path → {"type": "dir"/"file", "content": bytes, "mode": int, "mtime": float}
        self._nodes = {}
        # 初始化标准目录结构
        for d in ["/", "/home", home, f"{home}/Desktop", f"{home}/Documents",
                  "/tmp", "/usr", "/usr/bin", "/usr/lib", "/etc", "/var", "/opt"]:
            self._nodes[d] = {"type": "dir", "content": b"", "mode": 0o755, "mtime": time.time()}
        # 初始化一些标准文件
        self._write_file("/etc/hostname", "mockhost\n")
        self._write_file("/etc/os-release",
            'NAME="Ubuntu"\nVERSION="22.04.5 LTS (Jammy Jellyfish)"\nID=ubuntu\n')

    def _write_file(self, path: str, content: str):
        """内部写入文件（自动创建父目录）"""
        parent = os.path.dirname(path).replace("\\", "/")
        if parent and parent not in self._nodes:
            self._nodes[parent] = {"type": "dir", "content": b"", "mode": 0o755, "mtime": time.time()}
        self._nodes[path] = {
            "type": "file",
            "content": content.encode("utf-8"),
            "mode": 0o644,
            "mtime": time.time(),
        }

    def exists(self, path: str) -> bool:
        p = self._normalize(path)
        return p in self._nodes

    def is_dir(self, path: str) -> bool:
        p = self._normalize(path)
        return p in self._nodes and self._nodes[p]["type"] == "dir"

    def is_file(self, path: str) -> bool:
        p = self._normalize(path)
        return p in self._nodes and self._nodes[p]["type"] == "file"

    def read_file(self, path: str) -> Optional[bytes]:
        p = self._normalize(path)
        if p in self._nodes and self._nodes[p]["type"] == "file":
            return self._nodes[p]["content"]
        return None

    def write_file(self, path: str, content: bytes):
        p = self._normalize(path)
        parent = os.path.dirname(p)
        if parent not in self._nodes:
            self.mkdir(parent)
        self._nodes[p] = {
            "type": "file",
            "content": content,
            "mode": 0o644,
            "mtime": time.time(),
        }

    def mkdir(self, path: str):
        p = self._normalize(path)
        # 递归创建父目录
        parts = p.split("/")
        for i in range(1, len(parts)):
            sub = "/".join(parts[:i+1])
            if sub and sub not in self._nodes:
                self._nodes[sub] = {"type": "dir", "content": b"", "mode": 0o755, "mtime": time.time()}

    def rm(self, path: str) -> bool:
        p = self._normalize(path)
        if p in self._nodes:
            del self._nodes[p]
            return True
        return False

    def ls(self, path: str) -> list:
        """列出目录内容"""
        p = self._normalize(path)
        if p not in self._nodes or self._nodes[p]["type"] != "dir":
            return []
        prefix = p if p.endswith("/") else p + "/"
        entries = []
        for node_path in self._nodes:
            if node_path.startswith(prefix) and "/" not in node_path[len(prefix):]:
                entries.append(node_path[len(prefix):])
        return sorted(entries)

    def stat(self, path: str) -> Optional[dict]:
        p = self._normalize(path)
        if p in self._nodes:
            node = self._nodes[p]
            return {
                "st_mode": stat.S_IFDIR | node["mode"] if node["type"] == "dir" else stat.S_IFREG | node["mode"],
                "st_size": len(node["content"]),
                "st_mtime": node["mtime"],
                "st_uid": 1000,
                "st_gid": 1000,
            }
        return None

    def _normalize(self, path: str) -> str:
        """路径规范化"""
        p = path.replace("\\", "/")
        # 处理 ..
        parts = []
        for part in p.split("/"):
            if part == "..":
                if parts:
                    parts.pop()
            elif part and part != ".":
                parts.append(part)
        result = "/" + "/".join(parts) if parts else "/"
        return result


# ── 仿真Linux Shell ──

class SimulatedShell:
    """模拟Linux bash shell行为

    基于VirtualFileSystem，文件操作真实可见。
    """

    def __init__(self, username: str = "testuser", hostname: str = "mockhost",
                 vfs: Optional[VirtualFileSystem] = None):
        self.username = username
        self.hostname = hostname
        self.cwd = f"/home/{username}"
        self.sudo_password = "0"
        self._bg_pids = []
        self.vfs = vfs or VirtualFileSystem(username)

    @property
    def prompt(self) -> str:
        display = self.cwd
        home = f"/home/{self.username}"
        if self.cwd == home:
            display = "~"
        elif self.cwd.startswith(home + "/"):
            display = "~" + self.cwd[len(home):]
        return f"{self.username}@{self.hostname}:{display}$ "

    def execute(self, command: str) -> str:
        """执行命令，返回输出（不含prompt）"""
        command = command.strip()
        if not command:
            return ""

        # 处理 __MARKER__ 和 __PWD_MARKER__（Terminal工具的命令结束标记）
        # 这些是Terminal内部机制，不影响命令本身

        parts = self._split_commands(command)
        outputs = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            out = self._execute_single(part)
            if out is not None:
                outputs.append(out)

        return "\n".join(outputs)

    def _split_commands(self, command: str) -> list:
        return command.split(";")

    def _resolve_path(self, path: str) -> str:
        """将相对路径转为绝对路径"""
        path = path.strip()
        if not path:
            return self.cwd
        if path.startswith("~"):
            path = f"/home/{self.username}" + path[1:]
        if not path.startswith("/"):
            path = self.cwd.rstrip("/") + "/" + path
        return self.vfs._normalize(path)

    def _execute_single(self, cmd: str) -> str:
        cmd = cmd.strip()
        if not cmd:
            return ""

        # 管道（优先级最高，包含|的命令先走管道处理）
        if "|" in cmd:
            return self._handle_pipe(cmd)
        # 重定向
        if ">" in cmd or ">>" in cmd:
            return self._handle_redirect(cmd)
        # echo
        if cmd.startswith("echo "):
            return self._handle_echo(cmd[5:])
        # pwd
        if cmd in ("pwd", "pwd -P"):
            return self.cwd
        # whoami
        if cmd == "whoami":
            return self.username
        # hostname
        if cmd == "hostname":
            return self.hostname
        # id
        if cmd == "id":
            return f"uid=1000({self.username}) gid=1000({self.username})"
        # ls
        if cmd.startswith("ls"):
            return self._handle_ls(cmd)
        # cd
        if cmd.startswith("cd "):
            return self._handle_cd(cmd[3:])
        # cat
        if cmd.startswith("cat "):
            return self._handle_cat(cmd[4:])
        # mkdir
        if cmd.startswith("mkdir"):
            return self._handle_mkdir(cmd)
        # touch
        if cmd.startswith("touch "):
            return self._handle_touch(cmd[6:])
        # rm
        if cmd.startswith("rm "):
            return self._handle_rm(cmd)
        # mv
        if cmd.startswith("mv "):
            return self._handle_mv(cmd)
        # cp
        if cmd.startswith("cp "):
            return ""
        # chmod
        if cmd.startswith("chmod"):
            return ""
        # sudo
        if "sudo" in cmd:
            return self._handle_sudo(cmd)
        # sleep
        if cmd.startswith("sleep "):
            return ""
        # disown
        if cmd.startswith("disown"):
            return ""
        # nohup / setsid
        if cmd.startswith("nohup") or cmd.startswith("setsid"):
            return self._handle_nohup(cmd)
        # ps / pgrep
        if cmd.startswith("pgrep") or cmd.startswith("ps"):
            return self._handle_ps(cmd)
        # which / command -v
        if cmd.startswith("which ") or cmd.startswith("command -v "):
            return self._handle_which(cmd)
        # file
        if cmd.startswith("file "):
            return "ELF 64-bit LSB executable, x86-64"
        # unzip
        if cmd.startswith("unzip"):
            return "Archive:  test.zip\n  inflating: test.txt"
        # tar
        if cmd.startswith("tar"):
            return ""
        # cmake
        if cmd.startswith("cmake"):
            return "-- The CXX compiler identification is GNU 11.4.0\n-- Configuring done\n-- Build files have been written to: /tmp/build"
        # make
        if cmd.startswith("make"):
            return "[1/3] Building CXX object...\n[3/3] Linking CXX executable...\nBuilt target app"
        # sed
        if cmd.startswith("sed"):
            return ""
        # find
        if cmd.startswith("find"):
            return "3"
        # grep (独立grep，非管道)
        if cmd.startswith("grep "):
            return ""
        # uname
        if cmd.startswith("uname"):
            if "-a" in cmd:
                return f"Linux {self.hostname} 6.8.0-generic #1 SMP x86_64 GNU/Linux"
            return "Linux"
        # df
        if cmd.startswith("df"):
            return "Filesystem     1K-blocks    Used Available Use% Mounted on\n/dev/sda1       51200000 25600000  25600000  50% /"
        # free
        if cmd.startswith("free"):
            return "              total        used        free      shared  buff/cache   available\nMem:        8192000     2048000     4096000      128000     2048000     5632000"
        # env / printenv
        if cmd.startswith("env") or cmd.startswith("printenv"):
            return f"HOME=/home/{self.username}\nUSER={self.username}\nSHELL=/bin/bash\nPATH=/usr/local/bin:/usr/bin:/bin"
        # date
        if cmd == "date":
            return "Thu Jun  5 10:00:00 UTC 2026"
        # head/tail (独立使用)
        if cmd.startswith("head") or cmd.startswith("tail"):
            return ""
        # wc
        if cmd.startswith("wc"):
            return "0"
        # xargs
        if cmd.startswith("xargs"):
            return ""
        # sort
        if cmd.startswith("sort"):
            return ""
        # awk
        if cmd.startswith("awk"):
            return ""
        # exit
        if cmd == "exit":
            return ""
        # type
        if cmd.startswith("type "):
            tool = cmd.split()[-1]
            return f"{tool} is /usr/bin/{tool}" if tool in {"bash","sh","python3","make","cmake","gcc","g++"} else f"bash: type: {tool}: not found"
        # apt/dpkg
        if "apt" in cmd:
            return "Reading package lists... Done\n0 upgraded, 0 newly installed."
        # scp (不应在shell中出现，但防御性处理)
        if cmd.startswith("scp"):
            return ""
        # ssh
        if cmd.startswith("ssh"):
            return ""
        # curl/wget
        if cmd.startswith("curl") or cmd.startswith("wget"):
            return ""
        # git
        if cmd.startswith("git"):
            return self._handle_git(cmd)
        # python3/python
        if cmd.startswith("python3") or cmd.startswith("python "):
            return ""
        # 未知命令
        return ""

    def _handle_echo(self, args: str) -> str:
        args = args.strip()
        args = args.replace("$?", "0")
        if self._bg_pids:
            args = args.replace("$!", str(self._bg_pids[-1]))
        # 处理 $HOME, $USER, $PWD 等环境变量
        args = args.replace("$HOME", f"/home/{self.username}")
        args = args.replace("$USER", self.username)
        args = args.replace("$PWD", self.cwd)
        args = args.replace("$HOSTNAME", self.hostname)
        if (args.startswith("'") and args.endswith("'")) or \
           (args.startswith('"') and args.endswith('"')):
            args = args[1:-1]
        return args

    def _handle_ls(self, cmd: str) -> str:
        # 解析参数
        parts = cmd.split()
        long_format = "-l" in parts or "-la" in parts or "-al" in parts
        show_all = "-a" in parts or "-la" in parts or "-al" in parts

        # 找出路径参数
        target = self.cwd
        for p in parts[1:]:
            if not p.startswith("-"):
                target = self._resolve_path(p)
                break

        entries = self.vfs.ls(target)
        if not entries and not self.vfs.is_dir(target):
            return f"ls: cannot access '{target}': No such file or directory"

        if show_all:
            entries = [".", ".."] + entries

        if long_format:
            lines = [f"total {len(entries) * 4}"]
            for e in entries:
                full = target.rstrip("/") + "/" + e
                s = self.vfs.stat(full)
                if s:
                    is_dir = stat.S_ISDIR(s["st_mode"])
                    perm = "drwxr-xr-x" if is_dir else "-rw-r--r--"
                    size = s["st_size"]
                    lines.append(f"{perm}  1 {self.username} {self.username} {size:>5} Jun  5 10:00 {e}")
                else:
                    lines.append(f"drwxr-xr-x  2 {self.username} {self.username} 4096 Jun  5 10:00 {e}")
            return "\n".join(lines)

        return "\n".join(entries)

    def _handle_cd(self, path: str) -> str:
        path = path.strip()
        target = self._resolve_path(path)
        if self.vfs.is_dir(target):
            self.cwd = target
        else:
            return f"bash: cd: {path}: No such file or directory"
        return ""

    def _handle_cat(self, path: str) -> str:
        path = path.strip()
        full = self._resolve_path(path)
        content = self.vfs.read_file(full)
        if content is not None:
            return content.decode("utf-8", errors="replace")
        return f"cat: {path}: No such file or directory"

    def _handle_mkdir(self, cmd: str) -> str:
        # mkdir [-p] path
        parts = cmd.split()
        recursive = "-p" in parts
        paths = [p for p in parts[1:] if not p.startswith("-")]
        for p in paths:
            full = self._resolve_path(p)
            if recursive:
                self.vfs.mkdir(full)
            else:
                parent = os.path.dirname(full)
                if not self.vfs.is_dir(parent):
                    return f"mkdir: cannot create directory '{p}': No such file or directory"
                self.vfs.mkdir(full)
        return ""

    def _handle_touch(self, path: str) -> str:
        full = self._resolve_path(path.strip())
        if not self.vfs.exists(full):
            self.vfs.write_file(full, b"")
        return ""

    def _handle_rm(self, cmd: str) -> str:
        parts = cmd.split()
        recursive = "-r" in parts or "-rf" in parts or "-fr" in parts
        paths = [p for p in parts[1:] if not p.startswith("-")]
        for p in paths:
            full = self._resolve_path(p)
            self.vfs.rm(full)
        return ""

    def _handle_mv(self, cmd: str) -> str:
        parts = cmd.split()
        paths = [p for p in parts[1:] if not p.startswith("-")]
        if len(paths) >= 2:
            src = self._resolve_path(paths[0])
            dst = self._resolve_path(paths[1])
            content = self.vfs.read_file(src)
            if content is not None:
                self.vfs.write_file(dst, content)
                self.vfs.rm(src)
        return ""

    def _handle_redirect(self, cmd: str) -> str:
        """处理重定向: echo xxx > file, echo xxx >> file"""
        # 简化处理：提取文件路径并写入
        if ">>" in cmd:
            left, right = cmd.split(">>", 1)
            filepath = right.strip()
            content_to_write = self._extract_output(left.strip())
            full = self._resolve_path(filepath)
            existing = self.vfs.read_file(full) or b""
            self.vfs.write_file(full, existing + content_to_write.encode("utf-8"))
        elif ">" in cmd:
            left, right = cmd.split(">", 1)
            filepath = right.strip()
            content_to_write = self._extract_output(left.strip())
            full = self._resolve_path(filepath)
            self.vfs.write_file(full, content_to_write.encode("utf-8"))
        return ""

    def _extract_output(self, cmd: str) -> str:
        """提取命令的输出（用于重定向）"""
        if cmd.startswith("echo "):
            return self._handle_echo(cmd[5:]) + "\n"
        return ""

    def _handle_sudo(self, cmd: str) -> str:
        if "echo" in cmd and "sudo -S" in cmd:
            m = re.search(r"echo\s+['\"]?(\S+?)['\"]?\s*\|\s*sudo\s+-S", cmd)
            if m:
                pwd = m.group(1)
                if pwd == self.sudo_password:
                    rest = cmd.split("sudo -S", 1)[1].strip()
                    if rest:
                        # sudo后以root身份执行
                        result = self._execute_single(rest.lstrip())
                        # whoami/id等命令在sudo下返回root
                        if rest.strip() in ("whoami", "whoami "):
                            return "root"
                        return result
                    return "root"
                return "Sorry, try again.\n[sudo] password for testuser:"
        if cmd.strip() == "sudo whoami":
            return "root"
        if "apt" in cmd:
            return "Reading package lists... Done\n0 upgraded, 0 newly installed."
        return ""

    def _handle_nohup(self, cmd: str) -> str:
        pid = 10000 + len(self._bg_pids)
        self._bg_pids.append(pid)
        return f"[1] {pid}"

    def _handle_ps(self, cmd: str) -> str:
        if self._bg_pids:
            return "\n".join(str(p) for p in self._bg_pids)
        return ""

    def _handle_which(self, cmd: str) -> str:
        tool = cmd.split()[-1]
        common = {"cmake","gcc","g++","make","ninja","ccache","python3","python",
                  "git","curl","wget","unzip","ssh","bash","sh","scp","sftp",
                  "tar","gzip","cat","ls","mkdir","rm","mv","cp","chmod",
                  "grep","sed","awk","find","head","tail","wc","sort"}
        return f"/usr/bin/{tool}" if tool in common else ""

    def _handle_pipe(self, cmd: str) -> str:
        # 特殊处理: echo PASS | sudo -S ...
        if "sudo -S" in cmd and "|" in cmd:
            return self._handle_sudo(cmd)
        if "wc -l" in cmd:
            return "5"
        if "head" in cmd:
            return "output line 1"
        if "tail" in cmd:
            return "output line N"
        if "grep -v grep" in cmd:
            return ""
        return ""

    def _handle_git(self, cmd: str) -> str:
        if "clone" in cmd:
            return "Cloning into 'repo'...\nDone."
        if "status" in cmd:
            return "On branch main\nnothing to commit, working tree clean"
        if "log" in cmd:
            return "abc1234 (HEAD -> main) Initial commit"
        return ""


# ── SSH Server 实现 ──

class _MockServerInterface(paramiko.ServerInterface):
    """paramiko ServerInterface实现"""

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password
        self._shell_channel = None
        self._sftp_requested = False

    def check_channel_request(self, kind: str, chanid: int):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username: str, password: str):
        if username == self._username and password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str):
        return "password"

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        self._shell_channel = channel
        return True

    def check_channel_exec_request(self, channel, command):
        return True

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp":
            self._sftp_requested = True
            return True
        return False

    def check_channel_window_change_request(self, channel, width, height, pixelwidth, pixelheight):
        return True

    def check_channel_env_request(self, channel, name, value):
        return True


class _MockSFTPServer(paramiko.SFTPServerInterface):
    """仿真SFTP服务器，基于VirtualFileSystem"""

    def __init__(self, channel, vfs: VirtualFileSystem, username: str):
        super().__init__(channel)
        self.vfs = vfs
        self.username = username

    def list_folder(self, path):
        entries = self.vfs.ls(path)
        result = []
        for name in entries:
            full = path.rstrip("/") + "/" + name
            s = self.vfs.stat(full)
            if s:
                attr = paramiko.SFTPAttributes()
                attr.st_mode = s["st_mode"]
                attr.st_size = s["st_size"]
                attr.st_mtime = int(s["st_mtime"])
                attr.st_uid = s["st_uid"]
                attr.st_gid = s["st_gid"]
                attr.filename = name
                result.append(attr)
        return result

    def stat(self, path):
        s = self.vfs.stat(path)
        if s is None:
            raise FileNotFoundError(path)
        attr = paramiko.SFTPAttributes()
        attr.st_mode = s["st_mode"]
        attr.st_size = s["st_size"]
        attr.st_mtime = int(s["st_mtime"])
        attr.st_uid = s["st_uid"]
        attr.st_gid = s["st_gid"]
        return attr

    def open(self, path, flags):
        # 简化：返回一个SFTPHandle
        if flags & os.O_WRONLY or flags & os.O_RDWR or flags & os.O_CREAT:
            # 写模式
            handle = _MockSFTPWriteHandle(path, self.vfs, flags)
        else:
            # 读模式
            handle = _MockSFTPReadHandle(path, self.vfs)
        return handle

    def mkdir(self, path, attr):
        self.vfs.mkdir(path)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        self.vfs.rm(path)
        return paramiko.SFTP_OK

    def remove(self, path):
        self.vfs.rm(path)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        content = self.vfs.read_file(oldpath)
        if content is not None:
            self.vfs.write_file(newpath, content)
            self.vfs.rm(oldpath)
        return paramiko.SFTP_OK


class _MockSFTPReadHandle(paramiko.SFTPHandle):
    def __init__(self, path, vfs):
        super().__init__()
        self.path = path
        self.vfs = vfs
        self._data = vfs.read_file(path) or b""
        self._pos = 0

    def read(self, offset, length):
        return self._data[offset:offset + length]

    def close(self):
        return paramiko.SFTP_OK


class _MockSFTPWriteHandle(paramiko.SFTPHandle):
    def __init__(self, path, vfs, flags):
        super().__init__()
        self.path = path
        self.vfs = vfs
        self.flags = flags
        if flags & os.O_APPEND:
            self._data = vfs.read_file(path) or b""
        else:
            self._data = b""

    def write(self, offset, data):
        # 简化：追加写入
        self._data = self._data[:offset] + data + self._data[offset + len(data):]
        return paramiko.SFTP_OK

    def close(self):
        self.vfs.write_file(self.path, self._data)
        return paramiko.SFTP_OK


class MockSSHServer:
    """仿真SSH服务器

    支持:
      - invoke_shell PTY交互式终端
      - SFTP文件传输
      - 虚拟文件系统

    用法:
        with MockSSHServer() as server:
            result = terminal.execute(
                action="connect",
                host=server.host,
                username=server.username,
                password=server.password,
                port=server.port,
            )
            # server.vfs 可直接操作虚拟文件系统
            server.vfs.write_file("/tmp/test.txt", b"hello")
    """

    _host_key = paramiko.RSAKey.generate(2048)

    def __init__(
        self,
        username: str = "testuser",
        password: str = "0",
        hostname: str = "mockhost",
    ):
        self.username = username
        self.password = password
        self.hostname = hostname
        self.host = "127.0.0.1"
        self.port = 0
        self.vfs = VirtualFileSystem(username)
        self._sock = None
        self._thread = None
        self._running = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        """启动SSH服务器"""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._running = True

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        time.sleep(0.2)

    def stop(self):
        """停止SSH服务器"""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def _serve(self):
        """服务器主循环"""
        while self._running:
            try:
                self._sock.settimeout(0.5)
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            t = threading.Thread(target=self._handle_client, args=(client,), daemon=True)
            t.start()

    def _handle_client(self, client_sock: socket.socket):
        """处理单个SSH客户端连接"""
        try:
            transport = paramiko.Transport(client_sock)
            transport.add_server_key(self._host_key)
            server = _MockServerInterface(self.username, self.password)

            try:
                transport.start_server(server=server)
            except paramiko.SSHException:
                return

            chan = transport.accept(timeout=10)
            if chan is None:
                transport.close()
                return

            # SFTP子系统
            if server._sftp_requested:
                sftp_server = _MockSFTPServer(chan, self.vfs, self.username)
                paramiko.SFTPServer(transport, sftp_server, chan, chan)
                return

            # 交互式shell
            self._handle_shell(chan, server)
        except Exception:
            pass

    def _handle_shell(self, chan: paramiko.Channel, server: _MockServerInterface):
        """处理交互式shell会话"""
        shell = SimulatedShell(self.username, self.hostname, self.vfs)

        welcome = (
            f"Welcome to Ubuntu 22.04.5 LTS (GNU/Linux 6.8.0-generic x86_64)\n"
            f"\n"
            f"Last login: Thu Jun  5 10:00:00 2026 from 127.0.0.1\n"
            f"{shell.prompt}"
        )
        try:
            chan.send(welcome)
        except Exception:
            return

        cmd_buffer = ""
        while self._running:
            try:
                chan.settimeout(1.0)
                data = chan.recv(4096)
                if not data:
                    break

                text = data.decode("utf-8", errors="replace")

                if "\x03" in text:
                    cmd_buffer = ""
                    chan.send(f"^C\n{shell.prompt}")
                    continue

                if "\x04" in text:
                    break

                cmd_buffer += text

                while "\n" in cmd_buffer or "\r" in cmd_buffer:
                    if "\r\n" in cmd_buffer:
                        line, cmd_buffer = cmd_buffer.split("\r\n", 1)
                    elif "\n" in cmd_buffer:
                        line, cmd_buffer = cmd_buffer.split("\n", 1)
                    elif "\r" in cmd_buffer:
                        line, cmd_buffer = cmd_buffer.split("\r", 1)
                    else:
                        break

                    line = line.strip()
                    if not line:
                        chan.send(shell.prompt)
                        continue

                    # PTY回显
                    chan.send(line + "\r\n")

                    output = shell.execute(line)
                    if output:
                        chan.send(f"{output}\r\n{shell.prompt}")
                    else:
                        chan.send(shell.prompt)

            except socket.timeout:
                continue
            except EOFError:
                break
            except Exception:
                break

        try:
            chan.close()
        except Exception:
            pass
