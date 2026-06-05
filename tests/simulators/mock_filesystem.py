"""仿真文件系统 —— 增强tempfile，提供典型项目结构

创建临时目录并填充典型项目文件，测试Read/Glob/Grep/Edit/Write工具。
测试结束自动清理。

用法:
    with MockFileSystem() as fs:
        # fs.root 是临时目录路径
        # 已创建: src/main.py, tests/test_main.py, pyproject.toml 等
        result = read.execute(os.path.join(fs.root, "src", "main.py"))
"""

import os
import shutil
import tempfile
from typing import Optional


class MockFileSystem:
    """仿真文件系统

    创建临时目录，填充典型项目结构，测试后自动清理。

    默认创建:
        root/
        ├── src/
        │   ├── main.py          # 简单Python入口
        │   ├── utils.py         # 工具函数
        │   └── __init__.py
        ├── tests/
        │   ├── test_main.py     # 测试文件
        │   └── __init__.py
        ├── pyproject.toml       # 项目配置
        ├── README.md            # 说明文档
        ├── .gitignore
        └── config.json          # JSON配置
    """

    def __init__(self, create_default: bool = True):
        self.root = tempfile.mkdtemp(prefix="narnat_sim_fs_")
        if create_default:
            self._create_default_structure()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()

    def _create_default_structure(self):
        """创建默认项目结构"""
        # 目录
        self.create_dir("src")
        self.create_dir("tests")

        # Python源码
        self.create_file("src/__init__.py", "")
        self.create_file("src/main.py", '''"""项目入口"""

def main():
    print("Hello, World!")

if __name__ == "__main__":
    main()
''')
        self.create_file("src/utils.py", '''"""工具函数"""

def add(a: int, b: int) -> int:
    """加法"""
    return a + b

def multiply(a: int, b: int) -> int:
    """乘法"""
    return a * b

def greet(name: str) -> str:
    """问候"""
    return f"Hello, {name}!"
''')

        # 测试文件
        self.create_file("tests/__init__.py", "")
        self.create_file("tests/test_main.py", '''"""测试main模块"""

from src.main import main
from src.utils import add, multiply, greet

def test_main():
    main()

def test_add():
    assert add(1, 2) == 3

def test_multiply():
    assert multiply(2, 3) == 6

def test_greet():
    assert greet("World") == "Hello, World!"
''')

        # 项目配置
        self.create_file("pyproject.toml", '''[project]
name = "test-project"
version = "0.1.0"
description = "A test project"

[tool.pytest.ini_options]
testpaths = ["tests"]
''')

        self.create_file("README.md", """# Test Project

A test project for NarnatAgent simulation.
""")

        self.create_file(".gitignore", """__pycache__/
*.pyc
.pytest_cache/
dist/
build/
""")

        self.create_file("config.json", '''{
    "name": "test-project",
    "version": "0.1.0",
    "debug": false,
    "max_retries": 3
}''')

    def create_file(self, rel_path: str, content: str) -> str:
        """创建文件，返回绝对路径"""
        abs_path = os.path.join(self.root, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return abs_path

    def create_dir(self, rel_path: str) -> str:
        """创建目录，返回绝对路径"""
        abs_path = os.path.join(self.root, rel_path.replace("/", os.sep))
        os.makedirs(abs_path, exist_ok=True)
        return abs_path

    def read_file(self, rel_path: str) -> str:
        """读取文件内容"""
        abs_path = os.path.join(self.root, rel_path.replace("/", os.sep))
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    def file_exists(self, rel_path: str) -> bool:
        """检查文件是否存在"""
        return os.path.exists(os.path.join(self.root, rel_path.replace("/", os.sep)))

    def abs_path(self, rel_path: str) -> str:
        """相对路径转绝对路径"""
        return os.path.join(self.root, rel_path.replace("/", os.sep))

    def cleanup(self):
        """清理临时目录"""
        try:
            shutil.rmtree(self.root, ignore_errors=True)
        except Exception:
            pass
