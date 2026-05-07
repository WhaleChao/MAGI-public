from __future__ import annotations

import re
from typing import Iterable


WEIGHT_MAP_VERSION = "v1.1"

_RADIO_PREFIX = "encoder.model_encoder.radio_model.model."

_DIRECT_PREFIX_REPLACEMENTS = (
    ("decoder.layernorm_embedding.", "decoder.ln_embed."),
    ("decoder.layer_norm.", "decoder.final_norm."),
    ("encoder.layer_norm1.", "neck.ln1."),
    ("encoder.layer_norm2.", "neck.ln2."),
    ("encoder.layer_norm3.", "neck.ln3."),
    ("encoder.conv1.", "neck.conv1."),
    ("encoder.conv2.", "neck.conv2."),
    ("encoder.sum_proj.", "neck.sum_proj."),
)


def map_tensor_name(name: str) -> str:
    """Map HF safetensors names to the local MLX runtime namespace.

    The local HF snapshot differs from the original design note in one place:
    C-RADIO patch embedding is stored under ``patch_generator.embedder`` as a
    flattened linear projection [1280, 768], not as a Conv2d patch_embed tensor.
    The mapping below follows the actual snapshot so conversion is lossless.
    """
    if name == "final_logits_bias":
        return name
    if name == "lm_head.weight":
        return name

    for src, dst in _DIRECT_PREFIX_REPLACEMENTS:
        if name.startswith(src):
            return dst + name[len(src):]

    if name.startswith("decoder.layers."):
        mapped = name
        mapped = mapped.replace(".self_attn_layer_norm.", ".ln_self.")
        mapped = mapped.replace(".encoder_attn_layer_norm.", ".ln_cross.")
        mapped = mapped.replace(".encoder_attn.", ".cross_attn.")
        mapped = mapped.replace(".final_layer_norm.", ".ln_final.")
        return mapped
    if name.startswith("decoder."):
        return name

    if name == "encoder.model_encoder.radio_model.summary_idxs":
        return "radio.summary_idxs"
    if name == "encoder.model_encoder.radio_model.input_conditioner.norm_mean":
        return "radio.input_conditioner.norm_mean"
    if name == "encoder.model_encoder.radio_model.input_conditioner.norm_std":
        return "radio.input_conditioner.norm_std"

    if name.startswith(_RADIO_PREFIX):
        rest = name[len(_RADIO_PREFIX):]
        if rest == "patch_generator.cls_token.token":
            return "radio.cls_token"
        if rest == "patch_generator.embedder.weight":
            return "radio.patch_embed.embedder.weight"
        if rest == "patch_generator.pos_embed":
            return "radio.pos_embed"
        return "radio." + rest

    raise KeyError(f"unmapped tensor: {name}")


def conv_tensor_names(hf_names: Iterable[str]) -> list[str]:
    """Return tensors that require PyTorch NCHW-style conv transpose.

    In this snapshot only neck convs are true convolution kernels. Patch
    embedding is a flattened linear projection and must not be transposed.
    """
    names = []
    for name in hf_names:
        if name in {"encoder.conv1.weight", "encoder.conv2.weight"}:
            names.append(name)
    return names


def transpose_conv_weight(name: str, array):
    """Convert PyTorch conv weights to MLX channel-last kernel layout."""
    if name == "encoder.conv1.weight":
        # PyTorch Conv1d: [out, in, k] -> MLX Conv1d: [out, k, in]
        return array.transpose(0, 2, 1)
    if name == "encoder.conv2.weight":
        # PyTorch Conv2d: [out, in, kh, kw] -> MLX Conv2d: [out, kh, kw, in]
        return array.transpose(0, 2, 3, 1)
    return array


def expected_tensor_count(names: Iterable[str]) -> int:
    return len(list(names))

