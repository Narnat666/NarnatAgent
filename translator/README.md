# translator

把 narnat agent 会话 JSON 翻译成人类可读的 Markdown 文档。

## 用法

把 `.json` 会话文件放到脚本所在目录，然后：

```powershell
cd D:\desktop\FILE\NarnatAgent\translator
python translate_sessions.py
```

输出在 `translated\` 目录下，每个 JSON 对应一份 `.md`。

---

### 自定义目录

```powershell
python translate_sessions.py <输入目录> <输出目录>
```

例：

```powershell
python translate_sessions.py D:\sessions D:\sessions\markdown
```

---

## 输出格式

每份 Markdown 包含：

- 会话名称 + 时间戳
- 每轮用户提问（引用块）
- 每个工具调用 → 参数（JSON 格式化）+ 完整返回值
- AI 分析/结论

---

## 注意

- 脚本自动识别目录下所有带 `messages` 字段的 `.json`，不会误处理其他 JSON
- 新增会话文件后重新跑一次即可全部刷新
- 工具返回值一字不丢，完整保留
