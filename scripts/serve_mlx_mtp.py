#!/usr/bin/env python3
"""OpenAI-compatible MLX/VLM sidecar with Gemma 4 MTP draft support."""

from __future__ import annotations

import argparse
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.speculative.drafters import load_drafter


DEFAULT_MODEL = "/Users/ai/.omlx/models/gemma-4-e4b-it-4bit"
DEFAULT_DRAFT_MODEL = "/Users/ai/.omlx/models/gemma-4-E4B-it-assistant-bf16"


class ChatMessage(BaseModel):
    role: str = "user"
    content: Any = ""


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[ChatMessage]
    temperature: float = 0.2
    max_tokens: int = Field(default=512, alias="max_tokens")
    stream: bool = False
    draft_model: str = ""
    draft_kind: str = "mtp"
    draft_block_size: int | None = 4


class RuntimeState:
    def __init__(self) -> None:
        self.model_path = ""
        self.model_id = ""
        self.draft_model_path = ""
        self.draft_model_id = ""
        self.draft_kind = "mtp"
        self.draft_block_size: int | None = 4
        self.model = None
        self.processor = None
        self.drafter = None
        self.lock = threading.Lock()
        self.started_at = time.time()


STATE = RuntimeState()


def _model_id(path: str) -> str:
    return str(path or "").strip().rstrip("/").split("/")[-1]


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in {"text", "input_text"}:
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _normalize_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.role or "user").strip() or "user"
        if role not in {"system", "user", "assistant"}:
            role = "user"
        normalized.append({"role": role, "content": _content_to_text(message.content)})
    return normalized


def _load_runtime(args: argparse.Namespace) -> None:
    STATE.model_path = args.model
    STATE.model_id = args.model_id or _model_id(args.model)
    STATE.draft_model_path = args.draft_model
    STATE.draft_model_id = args.draft_model_id or _model_id(args.draft_model)
    STATE.draft_kind = args.draft_kind
    STATE.draft_block_size = args.draft_block_size
    STATE.model, STATE.processor = load(args.model)
    if args.draft_model:
        STATE.drafter, STATE.draft_kind = load_drafter(args.draft_model, kind=args.draft_kind)


def create_app(args: argparse.Namespace) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _load_runtime(args)
        yield

    app = FastAPI(title="MAGI MLX MTP Sidecar", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": STATE.model is not None,
            "model": STATE.model_id,
            "draft_model": STATE.draft_model_id,
            "draft_kind": STATE.draft_kind,
            "uptime_sec": round(time.time() - STATE.started_at, 3),
        }

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        data = [{"id": STATE.model_id, "object": "model", "owned_by": "mlx-vlm"}]
        if STATE.draft_model_id:
            data.append({"id": STATE.draft_model_id, "object": "model", "owned_by": "mlx-vlm-draft"})
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatRequest) -> dict[str, Any]:
        if req.stream:
            raise HTTPException(status_code=400, detail="streaming is not supported by this sidecar yet")
        if STATE.model is None or STATE.processor is None:
            raise HTTPException(status_code=503, detail="model is not loaded")
        messages = _normalize_messages(req.messages)
        prompt = apply_chat_template(STATE.processor, STATE.model.config, messages)
        draft = STATE.drafter if (req.draft_model or STATE.drafter is not None) else None
        gen_kwargs: dict[str, Any] = {
            "temperature": float(req.temperature),
            "max_tokens": int(req.max_tokens),
            "verbose": False,
        }
        if draft is not None:
            gen_kwargs["draft_model"] = draft
            gen_kwargs["draft_kind"] = req.draft_kind or STATE.draft_kind
            if req.draft_block_size is not None:
                gen_kwargs["draft_block_size"] = int(req.draft_block_size)
        with STATE.lock:
            started = time.perf_counter()
            result = generate(STATE.model, STATE.processor, prompt, **gen_kwargs)
            elapsed = time.perf_counter() - started
            accept_lens = list(getattr(STATE.drafter, "accept_lens", None) or []) if STATE.drafter is not None else []
        usage = {
            "prompt_tokens": int(result.prompt_tokens or 0),
            "completion_tokens": int(result.generation_tokens or 0),
            "total_tokens": int(result.total_tokens or 0),
        }
        payload = {
            "id": f"chatcmpl-mlx-mtp-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": STATE.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
            "magi_mlx": {
                "elapsed_sec": elapsed,
                "prompt_tps": float(result.prompt_tps or 0.0),
                "generation_tps": float(result.generation_tps or 0.0),
                "peak_memory_gb": float(result.peak_memory or 0.0),
                "draft_model": STATE.draft_model_id if draft is not None else "",
                "draft_kind": gen_kwargs.get("draft_kind", ""),
                "draft_accept_mean": (sum(accept_lens) / len(accept_lens)) if accept_lens else None,
                "draft_rounds": len(accept_lens),
            },
        }
        return payload

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("MAGI_MLX_MTP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MAGI_MLX_MTP_PORT", "8090")))
    parser.add_argument("--model", default=os.environ.get("MAGI_MLX_MTP_MODEL", DEFAULT_MODEL))
    parser.add_argument("--model-id", default=os.environ.get("MAGI_MLX_MTP_MODEL_ID", "gemma-4-e4b-it-4bit"))
    parser.add_argument("--draft-model", default=os.environ.get("MAGI_MLX_MTP_DRAFT_MODEL", DEFAULT_DRAFT_MODEL))
    parser.add_argument("--draft-model-id", default=os.environ.get("MAGI_MLX_MTP_DRAFT_MODEL_ID", "gemma-4-E4B-it-assistant-bf16"))
    parser.add_argument("--draft-kind", default=os.environ.get("MAGI_MLX_MTP_DRAFT_KIND", "mtp"))
    parser.add_argument("--draft-block-size", type=int, default=int(os.environ.get("MAGI_MLX_MTP_DRAFT_BLOCK_SIZE", "4")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
