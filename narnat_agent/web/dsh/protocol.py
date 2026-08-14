"""DSH 前端协议编解码（wire 层）

对应 DSH packages/host/apiproxy/src/api/rpc.ts + rpc.schema.ts 的四象限 RPC 消息模型:
- ClientRequest   {type:'client-request',  rpcId, method, payload}
- ServerResponse  {type:'server-response', rpcId, result:{ok:true,value}|{ok:false,error}}
- ServerRequest   {type:'server-request',  rpcId, method, payload}  (下行帧, method == payload.type)
- ClientResponse  {type:'client-response', rpcId, result}
HTTP 载体: POST /api/<method>; 业务错误一律 HTTP 200 + ok:false。
"""

import json
import uuid
from typing import Any, Dict, Optional


def new_rpc_id() -> str:
    return str(uuid.uuid4())


def ok(value: Any = None) -> Dict[str, Any]:
    """RpcResult 成功分支。value 缺省表示 void（JSON 无 undefined，缺省即可）。"""
    result: Dict[str, Any] = {"ok": True}
    if value is not None:
        result["value"] = value
    return result


def error(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """RpcResult 失败分支（details 必填，internal 用 {}）。"""
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}


def server_response(rpc_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "server-response", "rpcId": rpc_id, "result": result}


def server_request_frame(frame: Dict[str, Any]) -> Dict[str, Any]:
    """下行帧：method 取帧自身的 type。rpcId 每帧新铸（纯推送）。"""
    return {
        "type": "server-request",
        "rpcId": new_rpc_id(),
        "method": frame.get("type", "?"),
        "payload": frame,
    }


def server_request_frame_with_id(rpc_id: str, frame: Dict[str, Any]) -> Dict[str, Any]:
    """带固定 rpcId 的下行帧（可应答帧重放时复用）。"""
    return {
        "type": "server-request",
        "rpcId": rpc_id,
        "method": frame.get("type", "?"),
        "payload": frame,
    }


def parse_client_request(raw: bytes) -> Optional[Dict[str, Any]]:
    """解析 POST /api 的 client-request 信封。返回 None 表示结构非法。"""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("type") != "client-request":
        return None
    if not isinstance(body.get("rpcId"), str) or not isinstance(body.get("method"), str):
        return None
    return body


def dumps(msg: Dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
