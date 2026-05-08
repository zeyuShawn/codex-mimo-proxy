<div align="center">

# codex-mimo-proxy

<p>
  <strong>Streaming proxy that bridges OpenAI Codex Responses API to Anthropic Messages API — lets Codex CLI use MiMo, DeepSeek, or any Anthropic-compatible model via CCR.</strong>
</p>

<p>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9+-blue.svg">
  <img alt="Zero deps" src="https://img.shields.io/badge/dependencies-3-orange.svg">
  <img alt="Tool calling" src="https://img.shields.io/badge/tool--calling-supported-2c7a7b.svg">
  <img alt="Streaming" src="https://img.shields.io/badge/streaming-SSE-5f4bb6.svg">
</p>

<p>
  <a href="#what-it-does">What It Does</a> |
  <a href="#why-not-just-use-cc-switch">Why Not Just Use CC Switch</a> |
  <a href="#quick-start">Quick Start</a> |
  <a href="#configuration">Configuration</a> |
  <a href="#how-it-works">How It Works</a> |
  <a href="#demos">Demos</a> |
  <a href="#repository-layout">Repository Layout</a>
</p>

</div>

---

`codex-mimo-proxy` is a lightweight protocol bridge, not a provider manager or config GUI.

OpenAI Codex CLI only speaks the **Responses API** (WebSocket-based SSE), while CCR (Claude Code Router) and most third-party providers speak **Anthropic Messages API** or **Chat Completions API**. This proxy sits between them, translating every streaming event in real-time with full tool/function calling support.

> [!IMPORTANT]
> This proxy does NOT replace your existing provider setup. It works alongside CCR — Codex talks to the proxy, the proxy talks to CCR, CCR talks to your model. No API keys are stored in this project.

---

## What It Does

| Area | What the proxy handles |
| :--- | :--- |
| **Protocol translation** | OpenAI Responses API ↔ Anthropic Messages API, bidirectional SSE streaming |
| **Tool calling** | Full function_call / tool_use translation — definitions, arguments, results |
| **System prompts** | `instructions` field → Anthropic `system` parameter |
| **Streaming** | Real-time event-by-event translation, no buffering or batching |
| **Error handling** | Upstream HTTP errors mapped to Responses API `response.failed` events |
| **Debug logging** | Optional `MIMO_DEBUG=1` for request/response inspection |

---

## Why Not Just Use CC Switch

[CC Switch](https://github.com/farion1231/cc-switch) (63k⭐) is a great desktop GUI for managing provider configs across Claude Code, Codex, Gemini CLI, OpenCode, and OpenClaw. But it's a **config editor**, not a **protocol bridge**.

| | CC Switch | codex-mimo-proxy |
|:---|:---|:---|
| **Solves** | "Which provider is my CLI pointing at?" | "My CLI speaks a different API format than my provider" |
| **Protocol translation** | No — writes config files only | Yes — real-time SSE event translation |
| **MiMo via CCR** | Cannot — CCR speaks Anthropic API, Codex needs Responses API | Yes — this is the core purpose |
| **GUI required** | Yes (Tauri desktop app) | No (Python script, headless/SSH friendly) |
| **Dependencies** | ~200MB (Electron/Tauri runtime) | ~15MB (Flask + requests + waitress) |
| **Embeddable** | No | Yes — systemd, Docker, CI/CD, scripts |
| **Customizable** | Wait for upstream release | Edit 300 lines of Python |

**When to use CC Switch instead:**
- You manage 3+ CLI tools and switch providers frequently
- You want a visual GUI with 50+ provider presets
- Your provider already speaks the same API format as your CLI

**When to use codex-mimo-proxy:**
- You need Codex CLI to work with MiMo or any Anthropic-compatible model through CCR
- You run on a headless server or prefer CLI-only workflows
- You want a minimal, auditable, scriptable bridge

**They work together:** Use CC Switch for multi-tool provider management, and codex-mimo-proxy as the protocol bridge for MiMo.

---

## Quick Start

```bash
# Clone
git clone https://github.com/zeyuShawn/codex-mimo-proxy.git
cd codex-mimo-proxy

# Install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run (requires CCR running on port 3456)
.venv/bin/python codex_mimo_proxy.py
```

Then add to `~/.codex/config.toml`:

```toml
model_provider = "mimo"
model = "mimo,mimo-v2.5-pro"
model_context_window = 131072

[model_providers.mimo]
name = "Mimo via CCR Proxy"
base_url = "http://127.0.0.1:5001/v1"
env_key = "DEEPSEEK_API_KEY"
```

Use Codex normally:

```bash
codex                              # interactive mode
codex exec "your prompt"           # non-interactive
codex -m "mimo,mimo-v2.5-pro"      # explicit model
```

---

## Configuration

All config via environment variables or `.env` file:

| Variable | Default | Description |
|:---|:---|:---|
| `CCR_URL` | `http://127.0.0.1:3456/v1/messages` | CCR Anthropic Messages API endpoint |
| `CCR_API_KEY` | `local-ccr-key` | API key for CCR authentication |
| `MIMO_MODEL` | `mimo,mimo-v2.5-pro` | Model name in `provider,model` format |
| `MIMO_PORT` | `5001` | Port the proxy listens on |
| `MIMO_DEBUG` | `0` | Set `1` to write `proxy_debug.log` |

---

## How It Works

```
┌─────────────┐   Responses API (SSE)   ┌───────────────────┐  Anthropic Messages API  ┌─────┐     ┌──────┐
│  Codex CLI  │ ───────────────────────→ │ codex-mimo-proxy  │ ───────────────────────→  │ CCR │ ──→ │ MiMo │
│  (default)  │   port 5001             │   (this project)  │   port 3456              └─────┘     └──────┘
└─────────────┘                          └───────────────────┘
```

### Request path

1. Codex sends `POST /v1/responses` with `input[]` array, `tools[]`, and `instructions`
2. Proxy translates to Anthropic format: `messages[]`, `system`, `tools[]` with `input_schema`
3. Proxy streams to CCR as `POST /v1/messages` with `stream: true`

### Response path

1. CCR streams Anthropic SSE events back (`message_start`, `content_block_delta`, etc.)
2. Proxy translates each event to Responses API format (`response.output_text.delta`, etc.)
3. Codex receives standard Responses API SSE stream

### Key translation rules

| Responses API | Anthropic Messages API |
|:---|:---|
| `input[].type="message"` | `messages[].role/content` |
| `input[].type="function_call"` | `messages[].role="assistant", content[].type="tool_use"` |
| `input[].type="function_call_output"` | `messages[].role="user", content[].type="tool_result"` |
| `instructions` | `system` parameter |
| `tools[].parameters` | `tools[].input_schema` |
| `tool_choice: "required"` | `tool_choice: {type: "any"}` |

---

## Demos

### Demo 1: Basic text generation

```bash
$ codex exec -m "mimo,mimo-v2.5-pro" "say hello in Chinese"

OpenAI Codex v0.128.0 (research preview)
--------
model: mimo,mimo-v2.5-pro
provider: mimo
tokens used: 99
```

### Demo 2: With tool calling (code generation)

```bash
$ codex exec -m "mimo,mimo-v2.5-pro" "write a fibonacci function in Python"

# Proxy translates tool definitions and function calls bidirectionally
# Codex sees standard Responses API events
# CCR receives standard Anthropic Messages API requests
```

### Demo 3: Switching between providers

```bash
# Use MiMo (via CCR proxy on port 5001)
codex -m "mimo,mimo-v2.5-pro"

# Use DeepSeek (via direct proxy on port 5000)
codex -m deepseek-v4-pro
```

---

## Prerequisites

- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) installed
- [CCR (Claude Code Router)](https://github.com/musistudio/claude-code-router) running with MiMo configured

---

## Repository Layout

<details open>
<summary><strong>View Repository Tree</strong></summary>

```text
codex-mimo-proxy/
├── codex_mimo_proxy.py    # Main proxy — Responses API ↔ Anthropic Messages API
├── requirements.txt       # flask, requests, waitress
├── .gitignore             # .venv, __pycache__, .env, logs
├── LICENSE                # MIT
└── README.md              # This file
```

</details>

---

## Suggested GitHub Topics

<details>
<summary><strong>View Suggested Topics</strong></summary>

```text
codex
openai-codex
codex-cli
mimo
anthropic
claude-code-router
ccr
api-proxy
protocol-bridge
responses-api
streaming
sse
tool-calling
function-calling
python
```

</details>

---

## License

MIT License. See [LICENSE](LICENSE).
