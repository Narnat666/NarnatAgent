"""
会话回调实现 —— /save /load /skill 等命令的具体逻辑

从agent.py中提取，Agent只负责组装。
"""

import json
import os
from typing import Optional, List, Dict, Any, Callable

from ..config.session_store import save_session, load_session, list_sessions, delete_session, format_session_list
from ..config.skill_store import load_skill, list_skill_names
from ..ui.session_commands import SessionCallbacks


class NarnatSessionCallbacks(SessionCallbacks):
    """会话命令回调实现"""

    def __init__(self, narnat_dir: str, get_messages_func: Callable[[], List[Dict[str, Any]]],
                 context_manager=None, config_dir: str = "", thinking_effort_getter: Callable[[], str] = None,
                 thinking_effort_setter: Callable[[str], None] = None,
                 thinking_options: dict = None):
        self._narnat_dir = narnat_dir
        self._get_messages = get_messages_func
        self._active_name: Optional[str] = None
        self._context = context_manager
        self._config_dir = config_dir
        self._get_thinking_effort = thinking_effort_getter
        self._set_thinking_effort = thinking_effort_setter
        self._thinking_options = thinking_options or {"high": "高", "max": "全开"}

    def on_save(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        msgs = self._get_messages()
        err = save_session(self._narnat_dir, name, msgs)
        if err:
            return err
        self._active_name = name
        return ""

    def on_show(self) -> str:
        sessions = list_sessions(self._narnat_dir)
        return format_session_list(sessions)

    def on_enter(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        msgs, err = load_session(self._narnat_dir, name)
        if err:
            return err
        current = self._get_messages()
        current.clear()
        current.extend(msgs)
        self._active_name = name
        # 同步轮次计数，避免压缩机制失效
        if self._context is not None:
            self._context.sync_from_messages(current)
        return ""

    def on_delete(self, name: str) -> str:
        if not name:
            return "错误: 请指定会话名称"
        err = delete_session(self._narnat_dir, name)
        if err:
            return err
        if self._active_name == name:
            self._active_name = None
        return ""

    def on_list_names(self) -> list:
        return [s["name"] for s in list_sessions(self._narnat_dir)]

    def on_skill(self, name: str) -> str:
        content, err = load_skill(self._narnat_dir, name)
        if err:
            return err
        self._get_messages().append({"role": "system", "content": content})
        return ""

    def on_thinking(self, effort: str) -> str:
        """切换思考强度（配置驱动，effort 为空则查询）"""
        options = self._thinking_options

        if not effort:
            current = (self._get_thinking_effort or (lambda: "high"))()
            current_label = options.get(current, current)
            return f"当前思考强度: {current_label}"

        effort_lower = effort.strip().lower()
        if effort_lower not in options:
            available = " / ".join(options.keys())
            return f"无效值: {effort_lower}（可用: {available}）"

        # 更新内存中的配置
        if self._set_thinking_effort:
            self._set_thinking_effort(effort_lower)

        # 回写 narnat.json
        if self._config_dir:
            config_path = os.path.join(self._config_dir, "narnat.json")
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["思考强度"] = effort_lower
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

        return f"思考强度已切换为: {options[effort_lower]}"

    def on_list_thinking_options(self) -> list:
        """返回所有可用的思考强度选项，供Tab补全使用"""
        return list(self._thinking_options.keys())

    def on_exit(self) -> str:
        if not self._active_name:
            return ""
        msgs = self._get_messages()
        err = save_session(self._narnat_dir, self._active_name, msgs)
        if err:
            return ""
        return self._active_name

    def on_list_skill_names(self) -> list:
        return list_skill_names(self._narnat_dir)
