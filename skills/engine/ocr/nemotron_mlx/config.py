from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


DEFAULT_HF_MODEL_DIR = Path.home() / ".omlx/models-vision/nemotron-parse-v1.2-hf"
DEFAULT_MLX_MODEL_DIR = Path.home() / ".omlx/models-vision/nemotron-parse-v1.2-mlx"


@dataclass(frozen=True)
class NemotronParseConfig:
    image_height: int = 2048
    image_width: int = 1664
    encoder_hidden_size: int = 1280
    encoder_layers: int = 32
    encoder_heads: int = 16
    decoder_hidden_size: int = 1024
    decoder_layers: int = 10
    decoder_heads: int = 16
    decoder_ffn_dim: int = 4096
    vocab_size: int = 52352
    decoder_start_token_id: int = 2
    eos_token_id: int = 2
    pad_token_id: int = 1
    max_sequence_length: int = 9000

    @classmethod
    def from_hf_dir(cls, model_dir: str | Path = DEFAULT_HF_MODEL_DIR) -> "NemotronParseConfig":
        path = Path(model_dir) / "config.json"
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        dec = data.get("decoder", {})
        enc = data.get("encoder", {})
        return cls(
            image_height=int(data.get("image_size", [2048, 1664])[0]),
            image_width=int(data.get("image_size", [2048, 1664])[1]),
            encoder_hidden_size=int(enc.get("hidden_size", 1280)),
            encoder_layers=32,
            encoder_heads=16,
            decoder_hidden_size=int(dec.get("d_model", dec.get("hidden_size", 1024))),
            decoder_layers=int(dec.get("decoder_layers", 10)),
            decoder_heads=int(dec.get("decoder_attention_heads", 16)),
            decoder_ffn_dim=int(dec.get("decoder_ffn_dim", 4096)),
            vocab_size=int(dec.get("vocab_size", data.get("vocab_size", 52352))),
            decoder_start_token_id=int(data.get("decoder_start_token_id", 2)),
            eos_token_id=int(data.get("eos_token_id", dec.get("eos_token_id", 2))),
            pad_token_id=int(data.get("pad_token_id", dec.get("pad_token_id", 1))),
            max_sequence_length=int(data.get("max_sequence_length", 9000)),
        )

