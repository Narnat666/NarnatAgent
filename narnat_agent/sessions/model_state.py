"""模型/思考配置句柄 —— 运行时命令对思考强度、思考回传与模型名的读写。

行为契约：`openspec/changes/recast-v2/specs/sessions/spec.md`
（「思考与模型配置命令（/thinking /thinkback /mode）」）。

- 运行值落在注入的 `AIConfig`（非冻结字段：`thinking_effort` /
  `thinking_passback` / `model`）——LLM 层每轮现读，切换立即生效；
- 同一次切换写回 `narnat.json`（键路径 `智能体.思考.强度` / `智能体.思考.回传` /
  `智能体.模型.当前`），写回失败静默忽略（内存中已生效）；
- 模型切换经注入的 `on_model_change` 同步统计组件（`stats.set_model`，
  design D7：替代现状的外部直改私有字段）。

本模块是「显式小对象」（design D8）：替代现状往会话管理器注入的
思考强度/回传/模型的读写 lambda 对。
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from ..config.defaults import NARNAT_JSON
from ..config.models import AIConfig

__all__ = ["ModelState"]


class ModelState:
    """思考强度 / 思考回传 / 模型名的读写句柄（运行值 + narnat.json 写回）。

    构造注入 `ai_config`（`narnat.json` 解析后的 AI 配置树，其三个字段非冻结）、
    `config_dir`（`narnat.json` 所在目录；空串时写回自动失败并静默）与可选的
    `on_model_change`（模型切换同步回调，如 `stats.set_model`）。
    """

    def __init__(self, ai_config: AIConfig, config_dir: str = "",
                 on_model_change: Optional[Callable[[str], None]] = None) -> None:
        self._ai = ai_config
        self._config_dir = config_dir
        self._on_model_change = on_model_change

    # ── 读 ──

    @property
    def thinking_options(self) -> dict:
        """思考强度选项表（键 = 强度值，值 = 显示标签）。"""
        return self._ai.thinking_options

    @property
    def thinking_effort(self) -> str:
        """当前思考强度值。"""
        return self._ai.thinking_effort

    @property
    def thinking_passback(self) -> bool:
        """思考回传开关。"""
        return self._ai.thinking_passback

    @property
    def model(self) -> str:
        """当前模型名。"""
        return self._ai.model

    @property
    def model_options(self) -> list:
        """可选模型列表（`/mode` 的匹配与候选来源）。"""
        return list(self._ai.model_options or [])

    # ── 写 ──

    def set_thinking_effort(self, effort: str) -> None:
        """切换思考强度并写回配置（键路径 `智能体.思考.强度`）。"""
        self._ai.thinking_effort = effort
        self._write_config("思考", "强度", effort)

    def set_thinking_passback(self, enabled: bool) -> None:
        """切换思考回传并写回配置（键路径 `智能体.思考.回传`）。"""
        self._ai.thinking_passback = enabled
        self._write_config("思考", "回传", enabled)

    def set_model(self, model: str) -> None:
        """切换模型：同步统计组件并写回配置（键路径 `智能体.模型.当前`）。"""
        self._ai.model = model
        if self._on_model_change is not None:
            self._on_model_change(model)
        self._write_config("模型", "当前", model)

    # ── 配置写回 ──

    def _write_config(self, section: str, key: str, value: Any) -> None:
        """把 `智能体.<section>.<key>` 写回 narnat.json。

        任何失败（文件缺失/被占用/非法 JSON）静默忽略——运行值已生效，用户无感，
        narnat.json 保持原样（spec「配置写回失败静默」）。
        """
        if not self._config_dir:
            return
        config_path = os.path.join(self._config_dir, NARNAT_JSON)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("智能体", {}).setdefault(section, {})[key] = value
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
