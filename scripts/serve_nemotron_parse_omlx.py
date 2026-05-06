#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import sys
import time

from flask import Flask, jsonify, request
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.engine.ocr.nemotron_mlx.config import DEFAULT_HF_MODEL_DIR, DEFAULT_MLX_MODEL_DIR
from skills.engine.ocr.nemotron_mlx.runtime import NemotronRuntime


def _image_from_request(payload: dict) -> Image.Image:
    image_path = str(payload.get("image_path") or "").strip()
    image_b64 = str(payload.get("image_base64") or "").strip()
    if image_path:
        return Image.open(Path(image_path).expanduser()).convert("RGB")
    if image_b64:
        if "," in image_b64 and image_b64.split(",", 1)[0].startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")
    raise ValueError("image_path or image_base64 is required")


def create_app(weights_dir: str | Path, model_dir: str | Path) -> Flask:
    app = Flask(__name__)
    state: dict[str, object] = {
        "runtime": None,
        "weights_dir": str(weights_dir),
        "model_dir": str(model_dir),
        "started_at": time.time(),
    }

    def runtime() -> NemotronRuntime:
        if state["runtime"] is None:
            state["runtime"] = NemotronRuntime.load(state["weights_dir"], state["model_dir"])
        return state["runtime"]  # type: ignore[return-value]

    @app.get("/health")
    def health():
        weights_path = Path(str(state["weights_dir"])).expanduser() / "model.safetensors"
        return jsonify({
            "ok": weights_path.is_file(),
            "loaded": state["runtime"] is not None,
            "weights": str(weights_path),
            "uptime_sec": round(time.time() - float(state["started_at"]), 1),
        })

    @app.post("/parse")
    def parse():
        started = time.monotonic()
        try:
            payload = request.get_json(force=True, silent=False) or {}
            max_new_tokens = int(payload.get("max_new_tokens") or 9000)
            image = _image_from_request(payload)
            result = runtime().parse_image(image, max_new_tokens=max_new_tokens)
            result["sidecar_elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            return jsonify(result)
        except Exception as exc:
            return jsonify({
                "ok": False,
                "error": str(exc),
                "sidecar_elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }), 500

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Nemotron Parse MLX OCR sidecar.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8094)
    parser.add_argument("--weights", default=str(DEFAULT_MLX_MODEL_DIR / "bf16"))
    parser.add_argument("--model-dir", default=str(DEFAULT_HF_MODEL_DIR))
    args = parser.parse_args()

    app = create_app(args.weights, args.model_dir)
    app.run(host=args.host, port=args.port, threaded=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
