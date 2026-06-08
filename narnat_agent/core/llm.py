"""
LLM调用层 —— 双协议支持（OpenAI兼容 + Anthropic兼容），流式输出，token计数

通过 base_url 中是否包含 "anthropic" 自动选择协议：
- 含 "anthropic" → AnthropicClient（/v1/messages 端点，x-api-key 认证）
- 其他 → OpenAI SDK（/chat/completions 端点，Bearer 认证）

上层统一使用 OpenAI 格式的 messages/tool_calls，AnthropicClient 内部做双向转换。
"""

import json
import re
from typing import List, Dict, Any, Iterator, Optional

from ..config.loader import AIConfig
from ..tools.registry import get_tool_definitions

# ESC中断：持有当前活跃的LLM HTTP连接，供ESC轮询线程关闭
_active_llm_response = None


def abort_active_llm_request():
    """关闭当前LLM请求的HTTP连接，解除流式迭代器的阻塞。"""
    global _active_llm_response
    resp = _active_llm_response
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass


class LLMClient:
    """
    LLM客户端，自动选择 OpenAI 或 Anthropic 协议。

    对外接口统一：chat_stream() yield OpenAI 格式的 chunk。
    """

    def __init__(self, config: AIConfig, logger=None):
        self._config = config
        self._logger = logger
        self._tool_defs = get_tool_definitions()

        if "anthropic" in config.base_url.lower():
            self._backend = _AnthropicBackend(config, self._tool_defs, logger)
        else:
            self._backend = _OpenAIBackend(config, self._tool_defs, logger)

    def chat_stream(self, messages: List[Dict[str, Any]], no_tools: bool = False) -> Iterator:
        """流式调用LLM，yield OpenAI格式chunk。no_tools=True时不传工具定义（压缩请求用）"""
        return self._backend.chat_stream(messages, no_tools=no_tools)

    def count_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """粗略估算token数（中文1字≈2token，英文1词≈1token）"""
        total = 0
        for m in messages:
            content = m.get("content") or ""
            if not content:
                continue
            cn_chars = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
            en_words = len(content.split())
            total += cn_chars * 2 + en_words
        return total


# ═══════════════════════════════════════════════════════════════
# OpenAI 兼容后端
# ═══════════════════════════════════════════════════════════════

class _OpenAIBackend:
    """OpenAI SDK 后端"""

    def __init__(self, config, tool_defs, logger):
        from openai import OpenAI
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    def chat_stream(self, messages, no_tools=False):
        if self._logger:
            self._logger.info("core.llm", f"发送请求(OpenAI), messages={len(messages)}条")

        try:
            kwargs = dict(
                model=self._config.model,
                messages=messages,
                stream=True,
            )
            if not no_tools:
                kwargs["tools"] = self._tool_defs
            stream = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            if self._logger:
                self._logger.error("core.llm", f"API调用失败: {e}")
            yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
            return

        global _active_llm_response
        _active_llm_response = stream

        try:
            tool_calls_buffer = {}      # tc_id → {id, name, arguments}
            _index_to_id = {}           # index → tc_id（用于增量chunk匹配）
            content_buffer = []
            _tc_idx = 0

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if delta.content:
                    content_buffer.append(delta.content)
                    yield {"content": delta.content}

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        tc_index = getattr(tc, 'index', None)
                        if tc.id:
                            # 新tool_call的开始chunk（含id和name）
                            tc_id = tc.id
                            if tc_index is not None:
                                _index_to_id[tc_index] = tc_id
                        elif tc_index is not None and tc_index in _index_to_id:
                            # 增量chunk（无id但有index）→ 匹配已有buffer
                            tc_id = _index_to_id[tc_index]
                        else:
                            # 兜底：无id无index，生成fallback id
                            tc_id = f"_tc_{_tc_idx}"
                            _tc_idx += 1
                        buf = tool_calls_buffer.setdefault(
                            tc_id, {"id": tc_id, "name": "", "arguments": ""},
                        )
                        if tc.function.name:
                            buf["name"] += tc.function.name
                        if tc.function.arguments:
                            buf["arguments"] += tc.function.arguments

                if finish_reason:
                    if tool_calls_buffer:
                        completed_calls = []
                        for tc_id, buf in tool_calls_buffer.items():
                            completed_calls.append({
                                "id": buf["id"],
                                "type": "function",
                                "function": {"name": buf["name"], "arguments": buf["arguments"]},
                            })
                        yield {"tool_calls": completed_calls, "finish_reason": finish_reason}
                    else:
                        yield {"finish_reason": finish_reason}

                    if self._logger:
                        total_out = len("".join(content_buffer))
                        self._logger.info("core.llm", f"响应完成, content_len={total_out}")
        finally:
            _active_llm_response = None


# ═══════════════════════════════════════════════════════════════
# Anthropic 兼容后端
# ═══════════════════════════════════════════════════════════════

class _AnthropicBackend:
    """
    Anthropic 兼容后端。

    端点: {base_url}/v1/messages
    认证: x-api-key + anthropic-version
    流式: content_block_start/delta 事件

    内部做 OpenAI ↔ Anthropic 消息格式双向转换，
    对外统一 yield OpenAI 格式的 chunk。
    """

    def __init__(self, config, tool_defs, logger):
        self._config = config
        self._tool_defs = tool_defs
        self._logger = logger
        self._url = f"{config.base_url.rstrip('/')}/v1/messages"
        self._headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def chat_stream(self, messages, no_tools=False):
        if self._logger:
            self._logger.info("core.llm", f"发送请求(Anthropic), messages={len(messages)}条")

        # 转换消息格式：OpenAI → Anthropic
        try:
            system, anthropic_msgs = self._convert_messages(messages)
            anthropic_tools = self._convert_tools(self._tool_defs)
        except Exception as e:
            yield {"content": f"错误: 消息格式转换失败: {e}", "finish_reason": "error"}
            return

        body = {
            "model": self._config.model,
            "messages": anthropic_msgs,
            "max_tokens": 8192,
            "stream": True,
        }
        if system:
            body["system"] = system
        if anthropic_tools and not no_tools:
            body["tools"] = anthropic_tools

        # 发送请求
        try:
            import requests
            resp = requests.post(
                self._url, headers=self._headers, json=body,
                stream=True, timeout=120.0,
            )
            resp.raise_for_status()
        except Exception as e:
            if self._logger:
                self._logger.error("core.llm", f"API调用失败: {e}")
            yield {"content": f"错误: API调用失败: {e}", "finish_reason": "error"}
            return

        # 解析 Anthropic SSE 流
        global _active_llm_response
        _active_llm_response = resp

        try:
            content_buffer = []
            tool_use_blocks = {}  # index → {id, name, input_json}

            for raw_line in resp.iter_lines(decode_unicode=True):
                line = (raw_line or "").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                dtype = data.get("type", "")

                if dtype == "content_block_start":
                    cb = data.get("content_block", {})
                    idx = data.get("index", 0)
                    ctype = cb.get("type", "")
                    if ctype == "tool_use":
                        tool_use_blocks[idx] = {
                            "id": cb.get("id", ""),
                            "name": cb.get("name", ""),
                            "input_json": "",
                        }

                elif dtype == "content_block_delta":
                    delta = data.get("delta", {})
                    idx = data.get("index", 0)
                    d_type = delta.get("type", "")

                    if d_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            content_buffer.append(text)
                            yield {"content": text}

                    elif d_type == "input_json_delta":
                        pj = delta.get("partial_json", "")
                        if idx in tool_use_blocks:
                            tool_use_blocks[idx]["input_json"] += pj

                elif dtype == "message_delta":
                    stop_reason = data.get("delta", {}).get("stop_reason", "")
                    if stop_reason:
                        # 转换 stop_reason → finish_reason
                        if stop_reason == "end_turn":
                            finish_reason = "stop"
                        elif stop_reason == "tool_use":
                            finish_reason = "tool_calls"
                        else:
                            # max_tokens / stop_sequence 等都映射为 stop
                            finish_reason = "stop"

                        # 转换 tool_use_blocks → OpenAI tool_calls 格式
                        if tool_use_blocks:
                            completed_calls = []
                            for idx in sorted(tool_use_blocks.keys()):
                                tu = tool_use_blocks[idx]
                                completed_calls.append({
                                    "id": tu["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tu["name"],
                                        "arguments": tu["input_json"],
                                    },
                                })
                            yield {"tool_calls": completed_calls, "finish_reason": finish_reason}
                        else:
                            yield {"finish_reason": finish_reason}

                        if self._logger:
                            total_out = len("".join(content_buffer))
                            self._logger.info("core.llm", f"响应完成, content_len={total_out}")

                elif dtype == "error":
                    err_msg = data.get("error", {}).get("message", "未知错误")
                    yield {"content": f"错误: {err_msg}", "finish_reason": "error"}
                    return
        finally:
            _active_llm_response = None

    # ── 消息格式转换：OpenAI → Anthropic ──

    def _convert_messages(self, messages):
        """
        将 OpenAI 格式 messages 转换为 Anthropic 格式。
        返回 (system_text, anthropic_messages)。
        """
        system_parts = []
        anthropic_msgs = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            if role == "user":
                anthropic_msgs.append({"role": "user", "content": content or ""})

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    # assistant 带 tool_calls → 多个 content block
                    blocks = []
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        try:
                            inp = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            inp = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": inp,
                        })
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                else:
                    anthropic_msgs.append({"role": "assistant", "content": content or ""})

            elif role == "tool":
                # tool 结果 → 转为 user 消息中的 tool_result block
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content or "",
                }
                # 合并到相邻的 user 消息
                if anthropic_msgs and anthropic_msgs[-1]["role"] == "user" \
                        and isinstance(anthropic_msgs[-1]["content"], list):
                    anthropic_msgs[-1]["content"].append(tool_result)
                else:
                    anthropic_msgs.append({"role": "user", "content": [tool_result]})

        system_text = "\n\n".join(system_parts) if system_parts else ""
        return system_text, anthropic_msgs

    # ── 工具定义转换：OpenAI → Anthropic ──

    def _convert_tools(self, tool_defs):
        """将 OpenAI 格式工具定义转换为 Anthropic 格式"""
        if not tool_defs:
            return []
        anthropic_tools = []
        for t in tool_defs:
            func = t.get("function", t)
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {
                    "type": "object", "properties": {},
                }),
            })
        return anthropic_tools
