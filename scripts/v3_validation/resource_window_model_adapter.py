#!/usr/bin/env python3
"""Drive a matched model request through the active arm's real tools API.

Each invocation owns a fresh, model-path-bound MLX HTTP server and sends the
same request through the V2 tools process or V3 production gateway.  The model
server is a descendant in the adapter's collector-owned process group.  Arms
therefore never share a direct in-process backend or a warm model instance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from scripts.v3_validation.isolated_resource_window_collector import MODEL_RESULT_PREFIX


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("model tree contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _request(url: str, *, body: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"arm production request returned HTTP {response.status}")
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError("arm production response is not a JSON object")
    return payload


def _wait_server(port: int, process: subprocess.Popen[Any], timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/v1/models"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("owned MLX server exited before readiness")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("owned MLX server readiness timed out")


def _response_text(payload: dict[str, Any]) -> str:
    for key in ("response", "reply", "text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    result = payload.get("result")
    if isinstance(result, dict):
        return _response_text(result)
    raise RuntimeError("production arm response contains no generated text")


def _token_count(model: Path, text: str) -> int:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model), local_files_only=True)
    return len(tokenizer.encode(text, add_special_tokens=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("v2-reference", "v3-candidate"), required=True)
    parser.add_argument("--backend", choices=("mlx_lm", "mlx_vlm"), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--model-port", type=int, default=18080)
    parser.add_argument("--arm-endpoint", default="http://127.0.0.1:5003/collab/chat")
    args = parser.parse_args()
    model = args.model.resolve(strict=True)
    prompt_path = args.prompt.resolve(strict=True)
    if not model.is_dir() or not prompt_path.is_file() or args.max_tokens < 128:
        raise SystemExit("model/prompt/max-token contract is invalid")
    expected_tree = os.environ.get("MAGI_V3_RESOURCE_MODEL_TREE_SHA256", "")
    if _tree_sha256(model) != expected_tree:
        raise SystemExit("model tree differs from the sealed workload")
    prompt = prompt_path.read_text(encoding="utf-8")
    request_body = {
        "prompt": prompt,
        "model": model.name,
        "timeout_sec": 900,
        "allow_fallback": False,
        "allow_template_fallback": False,
    }
    request_sha = hashlib.sha256(
        json.dumps(request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected_request = os.environ.get("MAGI_V3_RESOURCE_HTTP_REQUEST_SHA256", "")
    if request_sha != expected_request:
        raise SystemExit("HTTP workload differs from the sealed matched request")
    module = "mlx_lm.server" if args.backend == "mlx_lm" else "mlx_vlm.server"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            module,
            "--model",
            str(model),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.model_port),
            "--max-tokens",
            str(args.max_tokens),
            "--log-level",
            "ERROR",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_server(args.model_port, server)
        started = time.perf_counter()
        payload = _request(
            args.arm_endpoint,
            body=request_body,
            key=os.environ["MAGI_EXTERNAL_API_KEY"],
            timeout=905,
        )
        elapsed = time.perf_counter() - started
        if payload.get("degraded") is True or payload.get("route") == "template_fallback":
            raise RuntimeError("production arm used a degraded/template response")
        text = _response_text(payload)
        generated_tokens = _token_count(model, text)
        if generated_tokens < 128 or elapsed <= 0:
            raise RuntimeError("production arm generated too few measured tokens")
        print(
            MODEL_RESULT_PREFIX
            + json.dumps(
                {
                    "generated_tokens": generated_tokens,
                    "generation_seconds": elapsed,
                    "request_sha256": request_sha,
                    "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "arm": args.arm,
                    "transport": "arm_owned_production_process_http",
                    "owned_model_server_pid": server.pid,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
