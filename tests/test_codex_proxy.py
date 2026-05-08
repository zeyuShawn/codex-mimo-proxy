import json

import codex_proxy as proxy


def parse_sse(chunks):
    events = []
    for chunk in chunks:
        for block in chunk.strip().split("\n\n"):
            if not block:
                continue
            event = None
            data = None
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            events.append((event, data))
    return events


def test_anthropic_payload_merges_system_and_tools():
    payload = proxy.build_anthropic_payload({
        "model": "qwen,qwen3-coder",
        "instructions": "base system",
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev rules"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        ],
        "tools": [{"type": "function", "name": "run", "description": "Run command", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "run"},
    })

    assert payload["model"] == "qwen,qwen3-coder"
    assert payload["system"] == "base system\n\ndev rules"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["tools"][0]["input_schema"] == {"type": "object"}
    assert payload["tool_choice"] == {"type": "tool", "name": "run"}


def test_openai_payload_supports_domestic_openai_compatible_models():
    payload = proxy.build_openai_chat_payload({
        "model": "deepseek-chat",
        "instructions": "system",
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ],
    })

    assert payload["model"] == "deepseek-chat"
    assert payload["messages"][0] == {"role": "system", "content": "system"}
    assert payload["messages"][1] == {"role": "user", "content": "hi"}
    assert payload["messages"][2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert payload["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}


def test_anthropic_stream_text_and_tool_events():
    class FakeResponse:
        def iter_lines(self):
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {"type": "content_block_delta", "delta": {"text": "hello"}},
                {"type": "content_block_stop"},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tool_1", "name": "run"}},
                {"type": "content_block_delta", "delta": {"partial_json": '{"cmd"'}},
                {"type": "content_block_delta", "delta": {"partial_json": ':"pwd"}' }},
                {"type": "content_block_stop"},
                {"type": "message_delta", "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ]
            for evt in events:
                yield ("data: " + json.dumps(evt)).encode()

    parsed = parse_sse(proxy._stream_anthropic(FakeResponse(), "resp_1", "m", [{"role": "user", "content": "hi"}]))
    names = [name for name, _ in parsed]
    assert "response.output_text.delta" in names
    done = [data for name, data in parsed if name == "response.function_call_arguments.done"][0]
    assert done["output_index"] == 1
    assert parsed[-1][0] == "response.completed"
    assert parsed[-1][1]["response"]["output"][0]["content"][0]["text"] == "hello"


def test_openai_stream_text():
    class FakeResponse:
        def iter_lines(self):
            chunks = [
                {"choices": [{"delta": {"content": "你"}}]},
                {"choices": [{"delta": {"content": "好"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
            ]
            for evt in chunks:
                yield ("data: " + json.dumps(evt, ensure_ascii=False)).encode()
            yield b"data: [DONE]"

    parsed = parse_sse(proxy._stream_openai(FakeResponse(), "resp_1", "deepseek-chat", [{"role": "user", "content": "hi"}]))
    assert parsed[-1][0] == "response.completed"
    assert parsed[-1][1]["response"]["output"][0]["content"][0]["text"] == "你好"


def test_models_endpoint_reports_default_model():
    client = proxy.app.test_client()
    res = client.get("/v1/models")
    assert res.status_code == 200
    body = res.get_json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == proxy.DEFAULT_MODEL
