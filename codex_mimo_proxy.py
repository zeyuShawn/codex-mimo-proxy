#!/usr/bin/env python3
"""Backward-compatible launcher for the renamed codex-proxy project."""

from codex_proxy import app, HOST, PORT, DEBUG, DEFAULT_MODEL, UPSTREAM_TYPE, UPSTREAM_URL

if __name__ == "__main__":
    from waitress import serve

    print("codex_mimo_proxy.py is deprecated; use codex_proxy.py / codex-proxy instead.")
    print("codex-proxy starting ...")
    print(f"   Endpoint: http://{HOST}:{PORT}")
    print(f"   Upstream: {UPSTREAM_TYPE} {UPSTREAM_URL}")
    print(f"   Model:    {DEFAULT_MODEL}")
    print(f"   Debug:    {'ON' if DEBUG else 'OFF'}")
    serve(app, host=HOST, port=PORT, threads=4)
