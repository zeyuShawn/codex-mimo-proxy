# codex-mimo-proxy

Streaming proxy that lets [OpenAI Codex CLI](https://github.com/openai/codex) use **MiMo** (or any Anthropic-compatible model) through a local [CCR (Claude Code Router)](https://github.com/musistudio/claude-code-router) bridge.

Translates OpenAI Responses API into Anthropic Messages API in real-time, with full tool/function calling support.

## How It Works

```
Codex CLI  --(Responses API)-->  codex-mimo-proxy  --(Anthropic Messages API)-->  CCR  -->  MiMo
   port 5001                      port 3456
```

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

## Configuration

All config via environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `CCR_URL` | `http://127.0.0.1:3456/v1/messages` | CCR Anthropic Messages API endpoint |
| `CCR_API_KEY` | `local-ccr-key` | API key for CCR |
| `MIMO_MODEL` | `mimo,mimo-v2.5-pro` | Model name (provider,model format) |
| `MIMO_PORT` | `5001` | Proxy listen port |
| `MIMO_DEBUG` | `0` | Set `1` to enable debug logging |

## Codex CLI Config

Add to `~/.codex/config.toml`:

```toml
model_provider = "mimo"
model = "mimo,mimo-v2.5-pro"
model_context_window = 131072

[model_providers.mimo]
name = "Mimo via CCR Proxy"
base_url = "http://127.0.0.1:5001/v1"
env_key = "DEEPSEEK_API_KEY"
```

Then use:

```bash
codex                              # interactive mode
codex exec "your prompt"           # non-interactive
```

## Prerequisites

- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) installed
- [CCR (Claude Code Router)](https://github.com/musistudio/claude-code-router) running with MiMo configured

## License

MIT
