**编译命令**：

```bash
python -m nuitka --onefile --output-dir=output --output-filename=narnat.exe --jobs=16 --lto=yes --python-flag=no_docstrings --follow-imports --include-module=openai --nofollow-import-to=tkinter --nofollow-import-to=unittest --nofollow-import-to=unittest.mock --nofollow-import-to=invoke --nofollow-import-to=test --nofollow-import-to=tests --nofollow-import-to=setuptools --nofollow-import-to=pip --nofollow-import-to=distutils main.py
```