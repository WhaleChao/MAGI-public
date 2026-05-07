from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import time

import mlx.core as mx
import numpy as np
from PIL import Image

from .config import DEFAULT_HF_MODEL_DIR, DEFAULT_MLX_MODEL_DIR
from .image_processor import make_test_image, preprocess_image
from .radio_encoder import RadioEncoder, _gelu, _layer_norm, _linear


DECODER_LAYERS = 10
DECODER_HIDDEN = 1024
DECODER_HEADS = 16
DECODER_HEAD_DIM = 64
VOCAB_SIZE = 52352
PROMPT_IDS = [2, 0, 50004, 50008, 50001, 50010]


def _split_heads(x: mx.array) -> mx.array:
    x = mx.reshape(x, (x.shape[0], x.shape[1], DECODER_HEADS, DECODER_HEAD_DIM))
    return mx.transpose(x, (0, 2, 1, 3))


def _merge_heads(x: mx.array) -> mx.array:
    x = mx.transpose(x, (0, 2, 1, 3))
    return mx.reshape(x, (x.shape[0], x.shape[1], DECODER_HIDDEN))


def _causal_self_attention(q: mx.array, k: mx.array, v: mx.array) -> mx.array:
    n = q.shape[2]
    scores = (q @ mx.transpose(k, (0, 1, 3, 2))) * (DECODER_HEAD_DIM ** -0.5)
    mask_np = np.triu(np.full((n, n), -1e9, dtype=np.float32), k=1)
    scores = scores + mx.array(mask_np)[None, None, :, :]
    probs = mx.softmax(scores, axis=-1)
    return probs @ v


class NemotronRuntime:
    def __init__(
        self,
        weights: dict[str, mx.array],
        *,
        repetition_penalty: float = 1.1,
        eos_token_id: int = 2,
    ):
        self.w = weights
        self.encoder = RadioEncoder(weights)
        self.repetition_penalty = float(repetition_penalty)
        self.eos_token_id = int(eos_token_id)
        self._tokenizer = None
        self.model_dir = Path(DEFAULT_HF_MODEL_DIR)

    @classmethod
    def load(
        cls,
        weights_dir: str | Path | None = None,
        model_dir: str | Path = DEFAULT_HF_MODEL_DIR,
    ) -> "NemotronRuntime":
        if weights_dir is None:
            weights_dir = DEFAULT_MLX_MODEL_DIR / "bf16"
        weights_path = Path(weights_dir).expanduser() / "model.safetensors"
        gen_config_path = Path(model_dir).expanduser() / "generation_config.json"
        gen_config = json.loads(gen_config_path.read_text(encoding="utf-8")) if gen_config_path.is_file() else {}
        runtime = cls(
            mx.load(str(weights_path)),
            repetition_penalty=float(gen_config.get("repetition_penalty", 1.1)),
            eos_token_id=int(gen_config.get("eos_token_id", 2)),
        )
        runtime.model_dir = Path(model_dir).expanduser()
        return runtime

    def encode(self, pixel_values: mx.array) -> mx.array:
        return self.encoder(pixel_values)

    def _decoder_layer(self, x: mx.array, encoder_hidden: mx.array, i: int) -> mx.array:
        w = self.w
        p = f"decoder.layers.{i}"

        residual = x
        h = _layer_norm(x, w[f"{p}.ln_self.weight"], w[f"{p}.ln_self.bias"])
        q = _split_heads(_linear(h, w[f"{p}.self_attn.q_proj.weight"], w[f"{p}.self_attn.q_proj.bias"]))
        k = _split_heads(_linear(h, w[f"{p}.self_attn.k_proj.weight"], w[f"{p}.self_attn.k_proj.bias"]))
        v = _split_heads(_linear(h, w[f"{p}.self_attn.v_proj.weight"], w[f"{p}.self_attn.v_proj.bias"]))
        attn = _merge_heads(_causal_self_attention(q, k, v))
        x = residual + _linear(attn, w[f"{p}.self_attn.out_proj.weight"], w[f"{p}.self_attn.out_proj.bias"])

        residual = x
        h = _layer_norm(x, w[f"{p}.ln_cross.weight"], w[f"{p}.ln_cross.bias"])
        q = _split_heads(_linear(h, w[f"{p}.cross_attn.q_proj.weight"], w[f"{p}.cross_attn.q_proj.bias"]))
        k = _split_heads(_linear(encoder_hidden, w[f"{p}.cross_attn.k_proj.weight"], w[f"{p}.cross_attn.k_proj.bias"]))
        v = _split_heads(_linear(encoder_hidden, w[f"{p}.cross_attn.v_proj.weight"], w[f"{p}.cross_attn.v_proj.bias"]))
        attn = mx.fast.scaled_dot_product_attention(q, k, v, scale=DECODER_HEAD_DIM ** -0.5)
        attn = _merge_heads(attn)
        x = residual + _linear(attn, w[f"{p}.cross_attn.out_proj.weight"], w[f"{p}.cross_attn.out_proj.bias"])

        residual = x
        h = _layer_norm(x, w[f"{p}.ln_final.weight"], w[f"{p}.ln_final.bias"])
        h = _linear(h, w[f"{p}.fc1.weight"], w[f"{p}.fc1.bias"])
        h = _gelu(h)
        h = _linear(h, w[f"{p}.fc2.weight"], w[f"{p}.fc2.bias"])
        x = residual + h
        return x

    def decoder_hidden(self, input_ids: list[int] | mx.array, encoder_hidden: mx.array) -> mx.array:
        w = self.w
        ids = mx.array([input_ids], dtype=mx.int32) if isinstance(input_ids, list) else input_ids
        # NemotronParseDecoder in the HF snapshot intentionally omits MBart's
        # learned position embeddings; it applies only scaled token embeddings
        # followed by layernorm_embedding.
        x = mx.take(w["decoder.embed_tokens.weight"], ids, axis=0) * math.sqrt(DECODER_HIDDEN)
        x = _layer_norm(x, w["decoder.ln_embed.weight"], w["decoder.ln_embed.bias"])
        for i in range(DECODER_LAYERS):
            x = self._decoder_layer(x, encoder_hidden, i)
        x = _layer_norm(x, w["decoder.final_norm.weight"], w["decoder.final_norm.bias"])
        return x

    def logits(self, input_ids: list[int] | mx.array, encoder_hidden: mx.array) -> mx.array:
        hidden = self.decoder_hidden(input_ids, encoder_hidden)
        logits = _linear(hidden, self.w["lm_head.weight"])
        if "final_logits_bias" in self.w:
            logits = logits + self.w["final_logits_bias"]
        return logits

    def _apply_repetition_penalty(self, scores: mx.array, ids: list[int]) -> np.ndarray:
        arr = np.array(scores.astype(mx.float32), copy=True)
        if self.repetition_penalty == 1.0 or not ids:
            return arr
        unique_ids = sorted(set(int(i) for i in ids))
        selected = arr[unique_ids]
        arr[unique_ids] = np.where(
            selected < 0,
            selected * self.repetition_penalty,
            selected / self.repetition_penalty,
        )
        return arr

    def generate(self, encoder_hidden: mx.array, prompt_ids: list[int] | None = None, max_new_tokens: int = 50) -> list[int]:
        ids = list(prompt_ids or PROMPT_IDS)
        for _ in range(max_new_tokens):
            logits = self.logits(ids, encoder_hidden)
            scores = self._apply_repetition_penalty(logits[0, -1, :], ids)
            next_id = int(scores.argmax())
            ids.append(next_id)
            if next_id == self.eos_token_id:
                break
        return ids

    def _load_tokenizer(self):
        if self._tokenizer is None:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_dir, trust_remote_code=True)
        return self._tokenizer

    def decode(self, token_ids: list[int], *, skip_special_tokens: bool = False) -> str:
        return str(self._load_tokenizer().decode(token_ids, skip_special_tokens=skip_special_tokens))

    @staticmethod
    def post_process_generation(text: str) -> str:
        return str(text or "").replace("<s>", "").replace("</s>", "").strip()

    def parse_image(
        self,
        image: Image.Image,
        *,
        prompt_ids: list[int] | None = None,
        max_new_tokens: int = 9000,
    ) -> dict:
        started = time.monotonic()
        pixels = mx.array(preprocess_image(image))
        enc = self.encode(pixels)
        mx.eval(enc)
        token_ids = self.generate(enc, prompt_ids or PROMPT_IDS, max_new_tokens=max_new_tokens)
        decoded = self.decode(token_ids, skip_special_tokens=False)
        return {
            "ok": True,
            "token_ids": token_ids,
            "decoded_text": decoded,
            "text": self.post_process_generation(decoded),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "model": "nvidia/NVIDIA-Nemotron-Parse-v1.2-MLX",
        }

    def parse_image_path(self, image_path: str | Path, *, max_new_tokens: int = 9000) -> dict:
        with Image.open(Path(image_path).expanduser()) as img:
            return self.parse_image(img, max_new_tokens=max_new_tokens)


def _test_encoder(runtime: NemotronRuntime) -> mx.array:
    pixels = mx.array(preprocess_image(make_test_image()))
    enc = runtime.encode(pixels)
    mx.eval(enc)
    return enc


def self_test_decoder_step(weights_dir: str | Path | None = None, model_dir: str | Path = DEFAULT_HF_MODEL_DIR) -> dict:
    golden = json.loads((Path(model_dir) / "golden_outputs.json").read_text(encoding="utf-8"))
    rt = NemotronRuntime.load(weights_dir, model_dir)
    started = time.monotonic()
    enc = _test_encoder(rt)
    logits = rt.logits([2], enc)
    mx.eval(logits)
    arr = np.array(logits[0, -1, :].astype(mx.float32))
    top_idx = arr.argsort()[-10:][::-1].tolist()
    top_vals = arr[top_idx].tolist()
    g = golden["forward_pass"]
    errors = []
    warnings = []
    if list(logits.shape) != g["logits_shape"]:
        errors.append(f"shape {list(logits.shape)} != {g['logits_shape']}")
    if top_idx != g["top_k_indices"]:
        warnings.append(f"top_k_indices_exact {top_idx} != {g['top_k_indices']}")
    if top_idx[0] != g["top_k_indices"][0]:
        errors.append(f"top1 {top_idx[0]} != {g['top_k_indices'][0]}")
    for i, (a, e) in enumerate(zip(top_vals, g["top_k_values"])):
        if abs(a - e) >= 1.0:
            errors.append(f"top_k_values[{i}] {a} != {e}")
            break
    return {
        "ok": not errors,
        "logits_shape": list(logits.shape),
        "top_k_indices": top_idx,
        "top_k_values": top_vals,
        "duration_sec": round(time.monotonic() - started, 3),
        "errors": errors,
        "warnings": warnings,
    }


def self_test_generation(weights_dir: str | Path | None = None, model_dir: str | Path = DEFAULT_HF_MODEL_DIR) -> dict:
    golden = json.loads((Path(model_dir) / "golden_outputs.json").read_text(encoding="utf-8"))
    rt = NemotronRuntime.load(weights_dir, model_dir)
    started = time.monotonic()
    enc = _test_encoder(rt)
    ids = rt.generate(enc, PROMPT_IDS, max_new_tokens=int(golden["generation"]["max_new_tokens"]))
    errors = []
    if ids != golden["generation"]["token_ids"]:
        errors.append(f"token_ids mismatch: {ids} != {golden['generation']['token_ids']}")
    decoded = rt.decode(ids, skip_special_tokens=False)
    text = rt.post_process_generation(decoded)
    if decoded != golden["generation"]["decoded_text"]:
        errors.append("decoded_text mismatch")
    return {
        "ok": not errors,
        "token_ids": ids,
        "decoded_text": decoded,
        "text": text,
        "duration_sec": round(time.monotonic() - started, 3),
        "errors": errors,
    }


def parse_image_file(
    image_path: str | Path,
    weights_dir: str | Path | None = None,
    model_dir: str | Path = DEFAULT_HF_MODEL_DIR,
    *,
    max_new_tokens: int = 9000,
) -> dict:
    rt = NemotronRuntime.load(weights_dir, model_dir)
    return rt.parse_image_path(image_path, max_new_tokens=max_new_tokens)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test-decoder-step", action="store_true")
    parser.add_argument("--self-test-generation", action="store_true")
    parser.add_argument("--parse-image")
    parser.add_argument("--max-new-tokens", type=int, default=9000)
    parser.add_argument("--weights", default=str(DEFAULT_MLX_MODEL_DIR / "bf16"))
    parser.add_argument("--model-dir", default=str(DEFAULT_HF_MODEL_DIR))
    args = parser.parse_args()
    if args.self_test_decoder_step:
        result = self_test_decoder_step(args.weights, args.model_dir)
    elif args.self_test_generation:
        result = self_test_generation(args.weights, args.model_dir)
    elif args.parse_image:
        result = parse_image_file(args.parse_image, args.weights, args.model_dir, max_new_tokens=args.max_new_tokens)
    else:
        parser.error("no action requested")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
