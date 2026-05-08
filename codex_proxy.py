#!/usr/bin/env python3
"""Codex Proxy: OpenAI Responses API bridge for Chinese and OpenAI/Anthropic-compatible models.

The proxy accepts the subset of the OpenAI Responses API used by Codex CLI and forwards it to
one of two upstream protocols:

* Anthropic Messages API (for Claude Code Router / CCR and Anthropic-compatible routers)
* OpenAI Chat Completions API (for DeepSeek, Qwen, Kimi, GLM, Baichuan, StepFun, Yi, etc.)
"""

import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Flask, Response, request
import requests

APP_NAME = "codex-proxy"
DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_debug.log")

app = Flask(__name__)

UPSTREAM_TYPE = os.environ.get("CODEX_PROXY_UPSTREAM", "anthropic").strip().lower()
UPSTREAM_URL = os.environ.get("CODEX_PROXY_URL", os.environ.get("CCR_URL", "http://127.0.0.1:3456/v1/messages")).strip()
UPSTREAM_API_KEY = os.environ.get("CODEX_PROXY_API_KEY", os.environ.get("CCR_API_KEY", "local-ccr-key")).strip()
DEFAULT_MODEL = os.environ.get("CODEX_PROXY_MODEL", os.environ.get("MIMO_MODEL", "mimo,mimo-v2.5-pro")).strip()
PORT = int(os.environ.get("CODEX_PROXY_PORT", os.environ.get("MIMO_PORT", "5001")))
HOST = os.environ.get("CODEX_PROXY_HOST", "127.0.0.1").strip()
REQUEST_TIMEOUT = int(os.environ.get("CODEX_PROXY_TIMEOUT", "180"))
MAX_TOKENS = int(os.environ.get("CODEX_PROXY_MAX_TOKENS", "16384"))
DEBUG = os.environ.get("CODEX_PROXY_DEBUG", os.environ.get("MIMO_DEBUG", "0")).strip().lower() in {"1", "true", "yes", "on"}

SUPPORTED_UPSTREAMS = {"anthropic", "openai"}
TEXT_CONTENT_TYPES = {"text", "input_text", "output_text"}


class ProxyConfigError(ValueError):
    """Raised when the proxy receives an unsupported configuration or request."""


def _json_dumps(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {_json_dumps(data)}\n\n"


def _debug(message: str, payload: Optional[Dict[str, Any]] = None) -> None:
    if not DEBUG:
        return
    with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"\n--- [{datetime.now().isoformat(timespec='seconds')}] {message} ---\n")
        if payload is not None:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2)[:12000])
            fh.write("\n")


def _safe_json_loads(value: Any) -> Any:
    if value in (None, ""):
        return {}
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in TEXT_CONTENT_TYPES and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def _response_input_to_messages(data: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    """Normalize Responses API input into role/content messages and an extracted system prompt."""
    system_prompt = data.get("instructions") or ""
    messages: List[Dict[str, Any]] = []
    inp = data.get("input", [])

    if isinstance(inp, str):
        return system_prompt, [{"role": "user", "content": inp}]

    if not isinstance(inp, list):
        return system_prompt, []

    for item in inp:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role", "user")
            text = _text_from_content(item.get("content", ""))
            if role in {"developer", "system"}:
                if text:
                    system_prompt = f"{system_prompt}\n\n{text}".strip() if system_prompt else text
                continue
            if text:
                messages.append({"role": "assistant" if role == "assistant" else "user", "content": text})
        elif item_type == "function_call":
            messages.append({
                "role": "assistant",
                "type": "function_call",
                "call_id": item.get("call_id") or f"call_{uuid.uuid4().hex[:12]}",
                "name": item.get("name", ""),
                "arguments": item.get("arguments", "{}"),
            })
        elif item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "call_id": item.get("call_id", ""),
                "content": item.get("output", ""),
            })
    return system_prompt, messages


def _convert_tools_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not tool.get("name"):
            continue
        result.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
        })
    return result


def _convert_tool_choice_anthropic(tool_choice: Any) -> Dict[str, str]:
    if tool_choice in (None, "auto"):
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function" and tool_choice.get("name"):
        return {"type": "tool", "name": tool_choice["name"]}
    return {"type": "auto"}


def build_anthropic_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt, normalized = _response_input_to_messages(data)
    messages: List[Dict[str, Any]] = []
    pending_tool_uses: List[Dict[str, Any]] = []

    def flush_tool_uses() -> None:
        nonlocal pending_tool_uses
        if pending_tool_uses:
            messages.append({"role": "assistant", "content": pending_tool_uses})
            pending_tool_uses = []

    for msg in normalized:
        msg_type = msg.get("type")
        if msg_type == "function_call":
            flush_tool_uses()
            pending_tool_uses.append({
                "type": "tool_use",
                "id": msg["call_id"],
                "name": msg.get("name", ""),
                "input": _safe_json_loads(msg.get("arguments")),
            })
        elif msg.get("role") == "tool":
            flush_tool_uses()
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("call_id", ""),
                    "content": msg.get("content", ""),
                }],
            })
        else:
            flush_tool_uses()
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    flush_tool_uses()

    payload: Dict[str, Any] = {
        "model": data.get("model") or DEFAULT_MODEL,
        "max_tokens": int(data.get("max_output_tokens") or MAX_TOKENS),
        "messages": messages,
        "stream": True,
    }
    if system_prompt:
        payload["system"] = system_prompt
    tools = _convert_tools_anthropic(data.get("tools", []))
    if tools:
        payload["tools"] = tools
        tool_choice = _convert_tool_choice_anthropic(data.get("tool_choice"))
        if tool_choice.get("type") != "auto":
            payload["tool_choice"] = tool_choice
    return payload


def _convert_tools_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not tool.get("name"):
            continue
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return result


def _convert_tool_choice_openai(tool_choice: Any) -> Any:
    if tool_choice in (None, "auto", "none", "required"):
        return tool_choice or "auto"
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "auto"


def build_openai_chat_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt, normalized = _response_input_to_messages(data)
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    for msg in normalized:
        if msg.get("type") == "function_call":
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": msg["call_id"],
                    "type": "function",
                    "function": {"name": msg.get("name", ""), "arguments": msg.get("arguments", "{}")},
                }],
            })
        elif msg.get("role") == "tool":
            messages.append({"role": "tool", "tool_call_id": msg.get("call_id", ""), "content": msg.get("content", "")})
        else:
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    payload: Dict[str, Any] = {
        "model": data.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "stream": True,
        "max_tokens": int(data.get("max_output_tokens") or MAX_TOKENS),
    }
    tools = _convert_tools_openai(data.get("tools", []))
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = _convert_tool_choice_openai(data.get("tool_choice"))
    return payload


def build_upstream_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    if UPSTREAM_TYPE == "anthropic":
        return build_anthropic_payload(data)
    if UPSTREAM_TYPE == "openai":
        return build_openai_chat_payload(data)
    raise ProxyConfigError(f"Unsupported CODEX_PROXY_UPSTREAM={UPSTREAM_TYPE!r}; expected one of {sorted(SUPPORTED_UPSTREAMS)}")


def _upstream_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_TYPE == "anthropic":
        headers.update({"x-api-key": UPSTREAM_API_KEY, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    return headers


def _response_started(response_id: str, model: str) -> Iterable[str]:
    for evt in ("response.created", "response.in_progress"):
        yield _sse(evt, {"type": evt, "response": {"id": response_id, "object": "response", "status": "in_progress", "model": model, "output": [], "usage": None}})


def _empty_response(response_id: str, model: str) -> str:
    return _sse("response.completed", {"type": "response.completed", "response": {"id": response_id, "object": "response", "status": "completed", "model": model, "output": [], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})


def _failed_response(response_id: str, model: str, message: str, error_type: str = "upstream_error") -> str:
    return _sse("response.failed", {"type": "response.failed", "response": {"id": response_id, "object": "response", "status": "failed", "model": model, "error": {"message": message, "type": error_type}, "output": [], "usage": None}})


def _stream_anthropic(upstream: requests.Response, response_id: str, model: str, messages: List[Dict[str, Any]]) -> Iterable[str]:
    text_item_id = f"item_{uuid.uuid4().hex[:12]}"
    full_text = ""
    has_text = False
    text_started = False
    tool_calls: Dict[int, Dict[str, Any]] = {}
    input_tokens = 0
    output_tokens = 0
    current_content_block: Optional[str] = None
    current_tool_idx = -1

    for raw_line in upstream.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8")
        if not line.startswith("data: "):
            continue
        raw = line[6:].strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue

        evt_type = evt.get("type", "")
        if evt_type == "message_start":
            input_tokens = evt.get("message", {}).get("usage", {}).get("input_tokens", 0)
        elif evt_type == "content_block_start":
            cb = evt.get("content_block", {})
            cb_type = cb.get("type", "")
            if cb_type == "text":
                current_content_block = "text"
                if not text_started:
                    text_started = True
                    has_text = True
                    yield _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {"id": text_item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                    yield _sse("response.content_part.added", {"type": "response.content_part.added", "item_id": text_item_id, "output_index": 0, "content_index": 0, "part": {"type": "text", "text": ""}})
            elif cb_type == "tool_use":
                current_content_block = "tool_use"
                current_tool_idx = int(evt.get("index", len(tool_calls)))
                item_id = f"fc_{uuid.uuid4().hex[:12]}"
                tool_calls[current_tool_idx] = {"item_id": item_id, "id": cb.get("id") or f"call_{uuid.uuid4().hex[:12]}", "name": cb.get("name", ""), "arguments": "", "output_index": (1 if has_text else 0) + len(tool_calls)}
                out_idx = tool_calls[current_tool_idx]["output_index"]
                yield _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": out_idx, "item": {"id": item_id, "type": "function_call", "status": "in_progress", "call_id": tool_calls[current_tool_idx]["id"], "name": tool_calls[current_tool_idx]["name"], "arguments": ""}})
        elif evt_type == "content_block_delta":
            delta = evt.get("delta", {})
            if current_content_block == "text" and "text" in delta:
                text = delta.get("text", "")
                full_text += text
                yield _sse("response.output_text.delta", {"type": "response.output_text.delta", "item_id": text_item_id, "output_index": 0, "content_index": 0, "delta": text})
            elif current_content_block == "tool_use" and current_tool_idx in tool_calls:
                partial = delta.get("partial_json", "")
                if partial:
                    tool_calls[current_tool_idx]["arguments"] += partial
                    out_idx = tool_calls[current_tool_idx].get("output_index", (1 if has_text else 0) + current_tool_idx)
                    yield _sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": tool_calls[current_tool_idx]["item_id"], "output_index": out_idx, "delta": partial})
        elif evt_type == "content_block_stop":
            if current_content_block == "tool_use" and current_tool_idx in tool_calls:
                acc = tool_calls[current_tool_idx]
                out_idx = acc.get("output_index", (1 if has_text else 0) + current_tool_idx)
                yield _sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": acc["item_id"], "output_index": out_idx, "arguments": acc["arguments"]})
            current_content_block = None
        elif evt_type == "message_delta":
            output_tokens = evt.get("usage", {}).get("output_tokens", 0)
        elif evt_type == "message_stop":
            break

    yield from _final_response(response_id, model, text_item_id, full_text, has_text, tool_calls, input_tokens, output_tokens, messages)


def _stream_openai(upstream: requests.Response, response_id: str, model: str, messages: List[Dict[str, Any]]) -> Iterable[str]:
    text_item_id = f"item_{uuid.uuid4().hex[:12]}"
    full_text = ""
    has_text = False
    text_started = False
    tool_calls: Dict[int, Dict[str, Any]] = {}
    input_tokens = 0
    output_tokens = 0

    for raw_line in upstream.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8")
        if not line.startswith("data: "):
            continue
        raw = line[6:].strip()
        if raw == "[DONE]":
            break
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        usage = evt.get("usage") or {}
        input_tokens = usage.get("prompt_tokens", input_tokens)
        output_tokens = usage.get("completion_tokens", output_tokens)
        for choice in evt.get("choices", []):
            delta = choice.get("delta", {}) or {}
            content = delta.get("content")
            if content:
                if not text_started:
                    text_started = True
                    has_text = True
                    yield _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {"id": text_item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                    yield _sse("response.content_part.added", {"type": "response.content_part.added", "item_id": text_item_id, "output_index": 0, "content_index": 0, "part": {"type": "text", "text": ""}})
                full_text += content
                yield _sse("response.output_text.delta", {"type": "response.output_text.delta", "item_id": text_item_id, "output_index": 0, "content_index": 0, "delta": content})
            for tool_delta in delta.get("tool_calls") or []:
                idx = int(tool_delta.get("index", len(tool_calls)))
                fn = tool_delta.get("function") or {}
                if idx not in tool_calls:
                    item_id = f"fc_{uuid.uuid4().hex[:12]}"
                    tool_calls[idx] = {"item_id": item_id, "id": tool_delta.get("id") or f"call_{uuid.uuid4().hex[:12]}", "name": fn.get("name", ""), "arguments": "", "output_index": (1 if has_text else 0) + len(tool_calls)}
                    out_idx = tool_calls[idx]["output_index"]
                    yield _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": out_idx, "item": {"id": item_id, "type": "function_call", "status": "in_progress", "call_id": tool_calls[idx]["id"], "name": tool_calls[idx]["name"], "arguments": ""}})
                if fn.get("name"):
                    tool_calls[idx]["name"] = fn["name"]
                if fn.get("arguments"):
                    tool_calls[idx]["arguments"] += fn["arguments"]
                    out_idx = tool_calls[idx].get("output_index", (1 if has_text else 0) + idx)
                    yield _sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": tool_calls[idx]["item_id"], "output_index": out_idx, "delta": fn["arguments"]})

    for idx in sorted(tool_calls):
        acc = tool_calls[idx]
        out_idx = acc.get("output_index", (1 if has_text else 0) + idx)
        yield _sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": acc["item_id"], "output_index": out_idx, "arguments": acc["arguments"]})
    yield from _final_response(response_id, model, text_item_id, full_text, has_text, tool_calls, input_tokens, output_tokens, messages)


def _final_response(response_id: str, model: str, text_item_id: str, full_text: str, has_text: bool, tool_calls: Dict[int, Dict[str, Any]], input_tokens: int, output_tokens: int, messages: List[Dict[str, Any]]) -> Iterable[str]:
    if has_text:
        yield _sse("response.output_text.done", {"type": "response.output_text.done", "text": full_text, "item_id": text_item_id, "output_index": 0, "content_index": 0})
        yield _sse("response.content_part.done", {"type": "response.content_part.done", "item_id": text_item_id, "output_index": 0, "content_index": 0, "part": {"type": "text", "text": full_text}})
        yield _sse("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": {"id": text_item_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "text", "text": full_text}]}})

    output_items: List[Dict[str, Any]] = []
    if has_text:
        output_items.append({"id": text_item_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "text", "text": full_text}]})
    for idx in sorted(tool_calls):
        acc = tool_calls[idx]
        out_idx = acc.get("output_index", (1 if has_text else 0) + idx)
        item = {"id": acc["item_id"], "type": "function_call", "status": "completed", "call_id": acc["id"], "name": acc["name"], "arguments": acc["arguments"]}
        output_items.append(item)
        yield _sse("response.output_item.done", {"type": "response.output_item.done", "output_index": out_idx, "item": item})

    guessed_input = input_tokens or max(1, len(json.dumps(messages, ensure_ascii=False)) // 4)
    guessed_output = output_tokens or max(1, len(full_text) // 4) if (has_text or tool_calls) else 0
    yield _sse("response.completed", {"type": "response.completed", "response": {"id": response_id, "object": "response", "status": "completed", "model": model, "output": output_items, "usage": {"input_tokens": guessed_input, "output_tokens": guessed_output, "total_tokens": guessed_input + guessed_output}}})


@app.after_request
def add_cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    return resp


@app.get("/health")
def health() -> Response:
    return Response(_json_dumps({"ok": True, "name": APP_NAME, "upstream": UPSTREAM_TYPE, "model": DEFAULT_MODEL}), mimetype="application/json")


@app.get("/v1/models")
def models() -> Response:
    return Response(_json_dumps({"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": APP_NAME}]}), mimetype="application/json")


def _make_response() -> Response:
    if request.method == "OPTIONS":
        return Response()

    req_data = request.get_json(silent=True) or {}
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    model = req_data.get("model") or DEFAULT_MODEL

    def generate() -> Iterable[str]:
        try:
            payload = build_upstream_payload(req_data)
            messages = payload.get("messages", [])
            _debug("request", {"incoming": req_data, "upstream": payload})
            if not messages:
                yield _empty_response(response_id, model)
                return
            yield from _response_started(response_id, model)
            upstream = requests.post(UPSTREAM_URL, headers=_upstream_headers(), json=payload, stream=True, timeout=REQUEST_TIMEOUT)
            try:
                upstream.raise_for_status()
                if UPSTREAM_TYPE == "anthropic":
                    yield from _stream_anthropic(upstream, response_id, model, messages)
                else:
                    yield from _stream_openai(upstream, response_id, model, messages)
            finally:
                upstream.close()
        except ProxyConfigError as exc:
            yield _failed_response(response_id, model, str(exc), "configuration_error")
        except requests.exceptions.HTTPError as exc:
            body = ""
            response = getattr(exc, "response", None)
            if response is not None:
                body = response.text[:2000]
            yield _failed_response(response_id, model, f"Upstream API {getattr(response, 'status_code', 'error')}: {body}")
        except requests.exceptions.RequestException as exc:
            yield _failed_response(response_id, model, str(exc))
        except Exception as exc:  # last-resort streaming error boundary
            yield _failed_response(response_id, model, str(exc), "proxy_error")

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.add_url_rule("/responses", "responses", _make_response, methods=["POST", "OPTIONS"])
app.add_url_rule("/v1/responses", "v1_responses", _make_response, methods=["POST", "OPTIONS"])


if __name__ == "__main__":
    from waitress import serve

    if UPSTREAM_TYPE not in SUPPORTED_UPSTREAMS:
        print(f"Unsupported CODEX_PROXY_UPSTREAM={UPSTREAM_TYPE!r}; expected one of {sorted(SUPPORTED_UPSTREAMS)}", file=sys.stderr)
        raise SystemExit(2)
    print(f"{APP_NAME} starting ...")
    print(f"   Endpoint: http://{HOST}:{PORT}")
    print(f"   Upstream: {UPSTREAM_TYPE} {UPSTREAM_URL}")
    print(f"   Model:    {DEFAULT_MODEL}")
    print(f"   Debug:    {'ON' if DEBUG else 'OFF'}")
    print("   Routes:   /health, /v1/models, /responses, /v1/responses")
    serve(app, host=HOST, port=PORT, threads=4)
