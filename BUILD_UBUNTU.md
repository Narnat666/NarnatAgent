# Narnat Agent Ubuntu 编译 & 移植指南

## 环境基准

| 组件 | Windows (已验证) | Ubuntu (本次编译) |
|------|:---:|:---:|
| Nuitka | 4.1.2 | 4.1.2 |
| Python | 3.12.9 | 3.12.9 (源码编译) |
| C 编译器 | MSVC cl 14.3 | gcc 11.4.0 |
| 压缩 | zstandard | zstandard |
| 产物大小 | 30MB | 35MB |

> 差异 5MB 来自 gcc vs MSVC 编译器差异，属正常范围。

---

## 一、环境准备

### 1.1 系统依赖

```bash
# APT 换阿里源（中国大陆必需）
sudo sed -i 's|http://cn.archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' /etc/apt/sources.list
sudo sed -i 's|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' /etc/apt/sources.list
sudo apt update

# 编译依赖
sudo apt install -y gcc patchelf build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev libncursesw5-dev libgdbm-dev \
  liblzma-dev tk-dev libffi-dev
```

### 1.2 安装 Python 3.12.9

> Ubuntu 22.04 默认 Python 3.10，PPA 在国内超时，所以走源码编译。

```bash
# 下载（使用 npmmirror 中转 Python 源码）
cd /tmp
wget https://npmmirror.com/mirrors/python/3.12.9/Python-3.12.9.tgz
tar xzf Python-3.12.9.tgz
cd Python-3.12.9

# 编译安装到独立目录，不覆盖系统 Python
./configure --prefix=/usr/local/python3.12
make -j$(nproc)
sudo make install

# 验证
/usr/local/python3.12/bin/python3.12 --version   # Python 3.12.9
```

### 1.3 安装 Nuitka + 项目依赖

```bash
# 配置阿里 pip 镜像
/usr/local/python3.12/bin/pip3.12 config set global.index-url https://mirrors.aliyun.com/pypi/simple/

# 锁定 Nuitka 版本（必须和 Windows 一致）
/usr/local/python3.12/bin/pip3.12 install nuitka==4.1.2

# 项目依赖
/usr/local/python3.12/bin/pip3.12 install httpx openai paramiko prompt_toolkit

# 压缩支持（不加这个产物 120MB，加了 35MB）
/usr/local/python3.12/bin/pip3.12 install zstandard
```

---

## 二、编译

### 2.1 命令

```bash
cd /path/to/NarnatAgent
/usr/local/python3.12/bin/python3.12 -m nuitka \
  --onefile \
  --output-dir=output \
  --output-filename=narnat.exe \
  --jobs=16 \
  --lto=yes \
  --python-flag=no_docstrings \
  --follow-imports \
  --include-module=openai \
  --nofollow-import-to=tkinter \
  --nofollow-import-to=unittest \
  --nofollow-import-to=unittest.mock \
  --nofollow-import-to=invoke \
  --nofollow-import-to=test \
  --nofollow-import-to=tests \
  --nofollow-import-to=setuptools \
  --nofollow-import-to=pip \
  --nofollow-import-to=distutils \
  main.py
```

### 2.2 耗时参考

| 阶段 | 耗时 | 说明 |
|------|------|------|
| Python 分析 | ~4 分钟 | 追踪导入、去冗余 |
| C 编译 (1752 文件) | ~7 分钟 | `--jobs=16` 并行 |
| LTO 链接 | **~21 分钟** | `--lto=yes` 全局优化，CPU 满 |
| Onefile 打包 | <1 分钟 | zstandard 压缩 |
| **总计** | **~28 分钟** | 首次编译，二次编译 ccache 命中会快很多 |

> 如果不需要极致体积，去掉 `--lto=yes` 可将链接从 21 分钟降到几十秒，产物增加 5-10%。

---

## 三、移植配置

### 3.1 关键发现：跨平台传输中文文件名

```
PowerShell Compress-Archive → ZIP → Linux unzip → 中文文件名乱码（GBK vs UTF-8）
```

**正确做法**：

```bash
# Windows 端：用 Python tarfile（UTF-8）
python -c "
import tarfile
with tarfile.open('narnat_config.tar.gz', 'w:gz') as tar:
    tar.add('.narnat')
"

# Linux 端：用 tar 解压
tar xzf narnat_config.tar.gz -C ~/local/bin/
```

### 3.2 配置位置

narnat onefile 模式查找 `.narnat` 的逻辑：

```
1. $NARNAT_HOME 环境变量
2. shutil.which("narnat.exe") 找到的路径（需在 PATH 中）
3. sys.executable 所在目录（Nuitka onefile 是临时目录，退出即删）
```

**推荐部署方式**：

```bash
mkdir -p ~/local/bin
cp narnat.exe ~/local/bin/narnat

# 加入 PATH
echo 'export PATH="$HOME/local/bin:$PATH"' >> ~/.bashrc
```

这样 `.narnat/config/` 落在 `~/local/bin/.narnat/`，有写权限且不会丢失。

### 3.3 终端颜色适配

Ubuntu GNOME Terminal 默认不设 `COLORTERM`，Narnat 检测不到 True Color 支持会走降级，导致自定义色全变紫色。

**修复**：

```bash
echo 'export COLORTERM=truecolor' >> ~/.bashrc
```

如果仍然有问题，删掉 `narnat.json` 中自定义颜色字段，走系统默认配色。

---

## 四、动态库依赖

```bash
ldd narnat
# 仅依赖 libc.so.6（glibc ≥ 2.35）
```

- Python 解释器（libpython3.12.a）已静态链接
- 第三方 `.so` 已嵌入 onefile
- **不需要目标系统安装 Python 或任何三方库**
- 最低要求：glibc 2.35（Ubuntu 22.04+ / CentOS 9+）

---

## 五、常见问题

### Q1: apt update 卡住不动
可能是 PPA 源超时。检查 `/etc/apt/sources.list.d/` 里是否有残留 PPA：
```bash
ls /etc/apt/sources.list.d/
# 删除无用的 PPA 后重试
```

### Q2: FATAL: need 'patchelf'
```bash
sudo apt install -y patchelf
```

### Q3: 产物 120MB 太大
缺少 `zstandard` 压缩：
```bash
pip install zstandard
# 重编后 120MB → 35MB
```

### Q4: LTO 链接阶段疑似卡住
LTO 链接 1752 个文件正常需 15-25 分钟，期间日志无输出。检查进程：
```bash
ps aux | grep lto1 | wc -l   # 应有 16 个进程全速运行
```

### Q5: 中文文件名在 Linux 上乱码
见 3.1 节，用 Python tarfile 替代 PowerShell Compress-Archive 传文件。

---

## 六、版本对齐清单

编译前逐项确认：

- [ ] Nuitka 版本与基准环境一致（`pip show nuitka`）
- [ ] Python 版本与基准环境一致（大版本必须相同）
- [ ] `zstandard` 已安装（否则产物大三倍）
- [ ] `patchelf` 已安装
- [ ] 编译命令参数与基准环境完全相同
- [ ] `COLORTERM=truecolor` 已设置（Linux 终端）
