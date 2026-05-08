# codex_mimo_proxy.py — OpenAI Responses API ↔ Anthropic Messages API (CCR) streaming proxy
# Lets Codex CLI use MiMo (or any Anthropic-compatible model) via a local CCR bridge.
import sys
import os
import json
import uuid

from flask import Flask, request, Response
import requests

DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_debug.log")

app = Flask(__name__)

CCR_URL = os.environ.get("CCR_URL", "http://127.0.0.1:3456/v1/messages").strip()
CCR_KEY = os.environ.get("CCR_API_KEY", "local-ccr-key").strip()
MIMO_MODEL = os.environ.get("MIMO_MODEL", "mimo,mimo-v2.5-pro").strip()
MIMO_DEBUG = os.environ.get("MIMO_DEBUG", "0").strip() in ("1", "true", "True", "yes")


def _convert_tools_anthropic(tools: list) -> list:
    """Responses API tools -> Anthropic tools"""
    result = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = tool.get("name", "")
        if not name:
            continue
        item = {"name": name, "description": tool.get("description", "")}
        params = tool.get("parameters")
        if params:
            item["input_schema"] = params
        else:
            item["input_schema"] = {"type": "object", "properties": {}}
        result.append(item)
    return result


def _convert_tool_choice_anthropic(tc):
    """Responses API tool_choice -> Anthropic tool_choice"""
    if tc is None or tc == "auto":
        return {"type": "auto"}
    if tc == "required":
        return {"type": "any"}
    if isinstance(tc, dict) and tc.get("type") == "function":
        return {"type": "tool", "name": tc.get("name", "")}
    return {"type": "auto"}


def extract_anthropic_payload(data: dict):
    """Build Anthropic Messages API request from Responses API request."""
    raw_tools = data.get("tools", [])
    tools = _convert_tools_anthropic(raw_tools)
    tool_choice = _convert_tool_choice_anthropic(data.get("tool_choice"))

    system_prompt = data.get("instructions", "")
    inp = data.get("input", [])

    messages = []
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        pending_tool_uses = []

        def _flush_tool_uses():
            nonlocal pending_tool_uses
            if pending_tool_uses:
                messages.append({"role": "assistant", "content": pending_tool_uses})
                pending_tool_uses = []

        for item in inp:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")

            if item_type == "message":
                _flush_tool_uses()
                role = item.get("role", "user")
                if role in ("developer", "system"):
                    if system_prompt:
                        system_prompt += "\n\n"
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") in ("text", "input_text")
                        )
                    system_prompt += content if isinstance(content, str) else ""
                    continue
                content = item.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ct = c.get("type")
                        if ct in ("text", "input_text", "output_text"):
                            t = c.get("text", "")
                            if t.strip():
                                parts.append({"type": "text", "text": t})
                    if parts:
                        messages.append({"role": role, "content": parts})
                elif isinstance(content, str) and content.strip():
                    messages.append({"role": role, "content": content})

            elif item_type == "function_call":
                _flush_tool_uses()
                pending_tool_uses.append({
                    "type": "tool_use",
                    "id": item.get("call_id", f"toolu_{uuid.uuid4().hex[:12]}"),
                    "name": item.get("name", ""),
                    "input": json.loads(item.get("arguments", "{}")) if item.get("arguments") else {},
                })

            elif item_type == "function_call_output":
                _flush_tool_uses()
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": item.get("call_id", ""),
                        "content": item.get("output", ""),
                    }],
                })

        _flush_tool_uses()

    payload = {
        "model": MIMO_MODEL,
        "max_tokens": 16384,
        "messages": messages,
        "stream": True,
    }
    if system_prompt:
        payload["system"] = system_prompt
    if tools:
        payload["tools"] = tools
        if tool_choice.get("type") != "auto":
            payload["tool_choice"] = tool_choice

    return payload


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


def _make_response():
    if request.method == "OPTIONS":
        return Response()

    req_data = request.get_json(silent=True) or {}
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    effective_model = req_data.get("model", MIMO_MODEL)

    anthropic_payload = extract_anthropic_payload(req_data)
    messages = anthropic_payload.get("messages", [])

    if MIMO_DEBUG:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- [{__import__('datetime').datetime.now()}] ---\n")
            f.write(f"Input items: {len(req_data.get('input', []))}\n")
            f.write(f"Anthropic messages: {len(messages)}\n")
            f.write(f"Tools: {len(anthropic_payload.get('tools', []))}\n")

    def generate():
        if not messages:
            yield "event: response.completed\n"
            yield "data: " + json.dumps({
                "type": "response.completed",
                "response": {
                    "id": response_id, "object": "response",
                    "status": "completed", "model": effective_model,
                    "output": [], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            }, ensure_ascii=False) + "\n\n"
            return

        for evt in ("response.created", "response.in_progress"):
            yield f"event: {evt}\n"
            yield "data: " + json.dumps({
                "type": evt,
                "response": {
                    "id": response_id, "object": "response",
                    "status": "in_progress", "model": effective_model,
                    "output": [], "usage": None,
                },
            }, ensure_ascii=False) + "\n\n"

        headers = {
            "x-api-key": CCR_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        text_item_id = f"item_{uuid.uuid4().hex[:12]}"
        full_text = ""
        has_text = False
        text_started = False

        tool_calls_acc = {}
        input_tokens = 0
        output_tokens = 0
        seq = 0
        upstream = None

        try:
            upstream = requests.post(
                CCR_URL, headers=headers, json=anthropic_payload,
                stream=True, timeout=180,
            )
            upstream.raise_for_status()

            current_content_block = None
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
                    msg = evt.get("message", {})
                    usage = msg.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)

                elif evt_type == "content_block_start":
                    cb = evt.get("content_block", {})
                    cb_type = cb.get("type", "")

                    if cb_type == "text":
                        current_content_block = "text"
                        if not text_started:
                            text_started = True
                            has_text = True
                            yield "event: response.output_item.added\n"
                            yield "data: " + json.dumps({
                                "type": "response.output_item.added",
                                "output_index": 0,
                                "item": {
                                    "id": text_item_id, "type": "message",
                                    "status": "in_progress", "role": "assistant",
                                    "content": [],
                                },
                            }, ensure_ascii=False) + "\n\n"
                            yield "event: response.content_part.added\n"
                            yield "data: " + json.dumps({
                                "type": "response.content_part.added",
                                "item_id": text_item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "part": {"type": "text", "text": ""},
                            }, ensure_ascii=False) + "\n\n"

                    elif cb_type == "tool_use":
                        current_content_block = "tool_use"
                        current_tool_idx += 1
                        tool_id = cb.get("id", f"toolu_{uuid.uuid4().hex[:12]}")
                        tool_name = cb.get("name", "")
                        item_id = f"item_{uuid.uuid4().hex[:12]}"
                        out_idx = (1 if has_text else 0) + current_tool_idx

                        tool_calls_acc[current_tool_idx] = {
                            "id": tool_id, "name": tool_name,
                            "arguments": "", "item_id": item_id, "started": False,
                        }

                        yield "event: response.output_item.added\n"
                        yield "data: " + json.dumps({
                            "type": "response.output_item.added",
                            "output_index": out_idx,
                            "item": {
                                "id": item_id, "type": "function_call",
                                "status": "in_progress",
                                "call_id": tool_id, "name": tool_name,
                                "arguments": "",
                            },
                        }, ensure_ascii=False) + "\n\n"
                        tool_calls_acc[current_tool_idx]["started"] = True

                elif evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    delta_type = delta.get("type", "")

                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            full_text += text
                            seq += 1
                            yield "event: response.output_text.delta\n"
                            yield "data: " + json.dumps({
                                "type": "response.output_text.delta",
                                "delta": text, "item_id": text_item_id,
                                "output_index": 0, "content_index": 0,
                                "sequence_number": seq,
                            }, ensure_ascii=False) + "\n\n"

                    elif delta_type == "input_json_delta":
                        partial = delta.get("partial_json", "")
                        if partial and current_tool_idx in tool_calls_acc:
                            acc = tool_calls_acc[current_tool_idx]
                            acc["arguments"] += partial
                            out_idx = (1 if has_text else 0) + current_tool_idx
                            yield "event: response.function_call_arguments.delta\n"
                            yield "data: " + json.dumps({
                                "type": "response.function_call_arguments.delta",
                                "item_id": acc["item_id"],
                                "output_index": out_idx,
                                "delta": partial,
                            }, ensure_ascii=False) + "\n\n"

                elif evt_type == "content_block_stop":
                    if current_content_block == "tool_use" and current_tool_idx in tool_calls_acc:
                        acc = tool_calls_acc[current_tool_idx]
                        out_idx = (1 if has_text else 0) + current_tool_idx
                        yield "event: response.function_call_arguments.done\n"
                        yield "data: " + json.dumps({
                            "type": "response.function_call_arguments.done",
                            "item_id": acc["item_id"],
                            "output_index": out_idx,
                            "arguments": acc["arguments"],
                        }, ensure_ascii=False) + "\n\n"
                    current_content_block = None

                elif evt_type == "message_delta":
                    usage2 = evt.get("usage", {})
                    output_tokens = usage2.get("output_tokens", 0)

                elif evt_type == "message_stop":
                    if has_text:
                        yield "event: response.output_text.done\n"
                        yield "data: " + json.dumps({
                            "type": "response.output_text.done",
                            "text": full_text, "item_id": text_item_id,
                            "output_index": 0, "content_index": 0,
                        }, ensure_ascii=False) + "\n\n"
                        yield "event: response.content_part.done\n"
                        yield "data: " + json.dumps({
                            "type": "response.content_part.done",
                            "item_id": text_item_id,
                            "output_index": 0, "content_index": 0,
                            "part": {"type": "text", "text": full_text},
                        }, ensure_ascii=False) + "\n\n"
                        yield "event: response.output_item.done\n"
                        yield "data: " + json.dumps({
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "id": text_item_id, "type": "message",
                                "status": "completed", "role": "assistant",
                                "content": [{"type": "text", "text": full_text}],
                            },
                        }, ensure_ascii=False) + "\n\n"

                    for idx in sorted(tool_calls_acc.keys()):
                        acc = tool_calls_acc[idx]
                        out_idx = (1 if has_text else 0) + idx
                        yield "event: response.output_item.done\n"
                        yield "data: " + json.dumps({
                            "type": "response.output_item.done",
                            "output_index": out_idx,
                            "item": {
                                "id": acc["item_id"], "type": "function_call",
                                "status": "completed",
                                "call_id": acc["id"], "name": acc["name"],
                                "arguments": acc["arguments"],
                            },
                        }, ensure_ascii=False) + "\n\n"

                    output_items = []
                    if has_text:
                        output_items.append({
                            "id": text_item_id, "type": "message",
                            "status": "completed", "role": "assistant",
                            "content": [{"type": "text", "text": full_text}],
                        })
                    for idx in sorted(tool_calls_acc.keys()):
                        acc = tool_calls_acc[idx]
                        output_items.append({
                            "id": acc["item_id"], "type": "function_call",
                            "status": "completed",
                            "call_id": acc["id"], "name": acc["name"],
                            "arguments": acc["arguments"],
                        })

                    yield "event: response.completed\n"
                    yield "data: " + json.dumps({
                        "type": "response.completed",
                        "response": {
                            "id": response_id, "object": "response",
                            "status": "completed", "model": effective_model,
                            "output": output_items,
                            "usage": {
                                "input_tokens": input_tokens or max(1, len(json.dumps(messages)) // 4),
                                "output_tokens": output_tokens or max(1, len(full_text) // 4),
                                "total_tokens": (input_tokens + output_tokens) or 1,
                            },
                        },
                    }, ensure_ascii=False) + "\n\n"

        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                if upstream is not None:
                    body = upstream.text[:2000]
            except Exception:
                body = "(unable to read error body)"
            err_msg = f"CCR API {e.response.status_code}: {body}"
            if MIMO_DEBUG:
                with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                    f.write(f"ERROR: {err_msg}\n")
            yield "event: response.failed\n"
            yield "data: " + json.dumps({
                "type": "response.failed",
                "response": {
                    "id": response_id, "object": "response",
                    "status": "failed", "model": effective_model,
                    "error": {"message": err_msg, "type": "upstream_error"},
                    "output": [], "usage": None,
                },
            }, ensure_ascii=False) + "\n\n"

        except requests.exceptions.RequestException as e:
            yield "event: response.failed\n"
            yield "data: " + json.dumps({
                "type": "response.failed",
                "response": {
                    "id": response_id, "object": "response",
                    "status": "failed", "model": effective_model,
                    "error": {"message": str(e), "type": "upstream_error"},
                    "output": [], "usage": None,
                },
            }, ensure_ascii=False) + "\n\n"

        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.add_url_rule("/responses", "responses", _make_response, methods=["POST", "OPTIONS"])
app.add_url_rule("/v1/responses", "v1_responses", _make_response, methods=["POST", "OPTIONS"])
app.add_url_rule("/v1/chat/completions", "v1_chat", _make_response, methods=["POST", "OPTIONS"])


if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("MIMO_PORT", "5001"))
    print("codex_mimo_proxy starting ...")
    print(f"   Endpoint: http://127.0.0.1:{port}")
    print(f"   CCR:      {CCR_URL}")
    print(f"   Model:    {MIMO_MODEL}")
    print(f"   Debug:    {'ON' if MIMO_DEBUG else 'OFF'}")
    print(f"   Routes:   /responses, /v1/responses, /v1/chat/completions")
    serve(app, host="127.0.0.1", port=port, threads=4)
