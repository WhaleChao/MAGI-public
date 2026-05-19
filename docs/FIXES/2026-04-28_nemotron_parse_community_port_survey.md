# Nemotron Parse v1.2 — 社群 MLX Port 調查
**日期：** 2026-04-28
**執行人：** Sonnet（Phase −1）
**目的：** 避免重造輪子，確認是否有現成 MLX port 可接入

---

## 1. mlx-vlm 支援模型清單

**指令：** `curl -s https://api.github.com/repos/Blaizzy/mlx-vlm/contents/mlx_vlm/models`

已支援模型（2026-04-28 查詢，共 58 個目錄）：

```
aya_vision, deepseek_vl_v2, deepseekocr, deepseekocr_2, dots_ocr,
ernie4_5_moe_vl, falcon_ocr, falcon_perception, fastvlm, florence2,
gemma3, gemma3n, gemma4, glm4v, glm4v_moe, glm_ocr, granite4_vision,
granite_vision, hunyuan_vl, idefics2, idefics3, internvl_chat,
jina_vlm, kimi_k25, kimi_vl, lfm2_vl, llama4, llava, llava_bunny,
llava_next, minicpmo, mistral3, mistral4, mllama, molmo, molmo2,
molmo_point, moondream3, multi_modality, paddleocr_vl, paligemma,
phi3_v, phi4_siglip, phi4mm, pixtral, qwen2_5_vl, qwen2_vl, qwen3_5,
qwen3_5_moe, qwen3_omni_moe, qwen3_vl, qwen3_vl_moe, rfdetr, sam3,
sam3_1, smolvlm, youtu_vl
```

**結論：**
- **無 nemotron / radio / mbart / vision-encoder-decoder** 相關條目
- mlx-vlm 以 decoder-only VLM 為主（LLaVA, Qwen-VL 系），未支援 encoder-decoder 架構

---

## 2. HuggingFace mlx-community 搜尋

### 2a. `search=nemotron-parse`

回傳 8 筆，**全為 HF 上的原版或社群量化版**（無 MLX 格式）：

| 模型 ID | 說明 |
|---|---|
| `nvidia/NVIDIA-Nemotron-Parse-v1.2` | 官方 HF 版 |
| `nvidia/NVIDIA-Nemotron-Parse-v1.1` | 官方舊版 |
| `nvidia/NVIDIA-Nemotron-Parse-v1.1-TC` | 繁中微調版 |
| `BEE-spoke-data/NVIDIA-Nemotron-Parse-v1.2` | 社群轉存 |
| `machinadeusex/NVIDIA-Nemotron-Parse-v1.2` | 社群轉存 |
| `beaupi/NVIDIA-Nemotron-Parse-v1.2-oQ6` | GGUF Q6 量化 |
| `beaupi/NVIDIA-Nemotron-Parse-v1.2-oQ8` | GGUF Q8 量化 (tags: `safetensors, 8-bit`) |
| `richtext/NVIDIA-Nemotron-Parse-v1.1` | 社群轉存 |

`beaupi` 的量化版 license tag 為 `other`（非 MLX 格式，是 llama.cpp GGUF）。

### 2b. `search=C-RADIO+mlx`

**零筆回傳。** C-RADIO encoder 無任何 MLX 社群移植版。

### 2c. `search=radio+mlx`

回傳 1 筆：`felixmanojh/DJ-AI-Radio-MLX`（DJ 音樂串流，完全無關）。

---

## 3. NVIDIA 官方 GitHub repos

**指令：** `curl -s "https://api.github.com/users/NVIDIA/repos?per_page=100"`

搜尋 nemotron / radio 關鍵字：**零匹配**（NVIDIA 官方 GitHub 一頁 100 個 repo 無 MLX port 相關項目）

---

## 4. mlx-vlm PR 搜尋

**指令：** `gh pr list --repo Blaizzy/mlx-vlm --state all --search "nemotron OR radio OR parse"`

回傳 18 筆 PR 均與 nemotron / RADIO / parse 無關（主要是 tool-call parsing、batch、Gemma4 fixes）。

**結論：無任何 in-flight PR。**

---

## 驗收結論

| 問題 | 結果 |
|---|---|
| 是否有可直接 import 的完整 MLX port？ | **否** |
| 是否有 C-RADIO encoder MLX port？ | **否**（連 HF 上也無） |
| 是否有任何 in-flight PR / community 嘗試？ | **否**（mlx-vlm 無相關 PR；社群只有 GGUF 量化） |

### 最終結論：**完全自寫**

理由：
1. mlx-vlm 不支援 encoder-decoder 架構，且無 MBart decoder 支援
2. C-RADIO (radio_v2.5-h, ViT-H 32-block) 無任何 MLX 移植，需從頭實作
3. 社群量化版均為 GGUF 格式（llama.cpp），不可直接接入 MLX
4. 架構太特殊（RADIO encoder + 1D/2D Conv Neck + MBart decoder），不符合任何現有 mlx-vlm 模板

**建議 Phase 2 策略：**
- Encoder: 以 MLX ViT-H 為基礎，移植 RADIO 的 CPE-free learned pos_embed（16384 token）、8 個 cls_token、neck（Conv1d + Conv2d 1×4 stride 4 + sum_proj）
- Decoder: 移植 MBart decoder（10 層），cross-attention 接 neck 輸出
- 可參考 mlx-vlm 的 `paligemma` 模組做為 encoder-decoder 拓墣的參考框架
