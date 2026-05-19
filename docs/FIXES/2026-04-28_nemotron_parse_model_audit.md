# Nemotron Parse v1.2 — Phase 0 離線架構審計
**日期：** 2026-04-28
**執行人：** Sonnet（靜態讀檔，不 import 任何模型 lib）
**來源：** `~/.omlx/models-vision/nemotron-parse-v1.2-hf/`

---

## B1. 權重清單

| 項目 | 值 |
|---|---|
| 檔案名稱 | `model.safetensors` |
| 檔案大小 | 3,745,188,184 bytes（3.49 GiB） |
| SHA256 | `9d77822f43a04504619e4c3527e1371568b6e33dba94b7c3a0c0cf509a200cd4` |
| safetensors header 長度 | 84,256 bytes |
| 總 tensor 數 | **667** |
| dtype 分布 | F32: 666 tensors / I64: 1 tensor（`encoder.model_encoder.radio_model.summary_idxs`） |

**注意：** 權重全為 F32（非 bf16），與 config 中 `torch_dtype: bfloat16` 不符——HF 儲存時未做精度降低。MLX 轉換時需明確轉 bf16/fp16。

---

## B2. config.json 摘要

| 欄位 | 值 |
|---|---|
| `model_type` | `nemotron_parse` |
| `architectures` | `NemotronParseForConditionalGeneration` |
| `image_size` | `[2048, 1664]`（height × width） |
| `is_encoder_decoder` | `true` |
| `tie_word_embeddings` | `false` |
| `bos_token_id` | 0 |
| `eos_token_id` | 2 |
| `pad_token_id` | 1 |
| `decoder_start_token_id` | 2 |
| `max_sequence_length` | 9000 |
| `vocab_size`（top-level） | 52329（`decoder.vocab_size` = 52352，差異因 added_tokens） |

### Encoder（C-RADIO v2.5-H）
| 欄位 | 值 |
|---|---|
| `version` | `radio_v2.5-h` |
| `model`（timm） | `vit_huge_patch16_224` |
| `patch_size` | 16 |
| `max_resolution` | 2048 |
| `preferred_resolution` | `[768, 768]` |
| `attn_implementation` | `eager`（非 SDPA，MLX 友善） |
| hidden_size（推算） | 1280（QKV weight shape = [3840, 1280] → 3×1280=3840） |
| num_attention_heads（推算） | 16（head_dim=80, 16×80=1280） |
| num_blocks（實測） | **32**（from safetensors tensor names `.blocks.0` ~ `.blocks.31`） |
| register_multiple | 8 |

### Decoder（MBart-based）
| 欄位 | 值 |
|---|---|
| `decoder_layers` | **10**（config.json 實際值；safetensors 驗證 layers.0~9） |
| `d_model` | 1024 |
| `decoder_attention_heads` | 16 |
| `decoder_ffn_dim` | 4096 |
| `activation_function` | `gelu` |
| `vocab_size` | 52352 |
| `_attn_implementation` | `sdpa`（需在 MLX 替換為 scaled_dot_product） |
| `tie_word_embeddings` | `false`（decoder embed ≠ lm_head，各自獨立） |
| `scale_embedding` | `true` |
| `add_final_layer_norm` | `true` |
| `encoder_layers`（MBart encoder，未使用） | 12（結構殘留，實際推理不用） |

---

## B3. C-RADIO Encoder 架構細節

### Patch Embed
- **timm 模型：** `vit_huge_patch16_224`
- **patch_size：** 16×16
- **embedder weight shape：** `[1280, 768]`（輸入 3×16×16=768 → 輸出 1280）
- **pos_embed shape：** `[1, 16384, 1280]`（預計算 128×128=16384 個 patch 位置）
  - 這是**learned positional embedding**（非 RoPE / 2D sincos），固定形狀
  - 128×128 = 16384 > 實際 2048×1664 / 16² = 13312 patches，推測在訓練時以插值支援多解析度

### Register Tokens / CLS Token
- **cls_token shape：** `[8, 1280]`（8 個 cls/summary token，config `register_multiple=8`）
- **summary_idxs：** I64 tensor，形狀 `[3]`，用於從多教師輸出中取 summary（CLIP/SigLIP/DINOv2 各一組）
- **No register tokens separate from cls：** RADIO 將 register 融合進 8-cls 機制

### Multi-resolution / Multi-scale
- **無 CPE（Conditional Positional Encoding）tensor：** 搜尋 `cpe` 關鍵字零結果
- **無 VitDet 視窗分割：** 搜尋 `vitdet`/`window` 關鍵字零結果
- **config `vitdet_window_size: null`** 確認無視窗分割
- **Dynamic resolution 由 processor 處理：** resize+pad 到固定 2048×1664，然後 pos_embed 以插值調整

### Positional Embedding 形式
- **Learned 2D（pre-computed flat）**，shape `[1, 16384, 1280]`
- 推理時需對 pos_embed 做雙線性插值以適應 2048×1664 → 13312 patches（非 16384）
- **MLX 支援度：** 可以，但需實作插值邏輯（`mlx.core.image.resize` 或手動 bilinear）

### Encoder 層數、hidden size、head 數
- **32 blocks**，hidden=1280，heads=16（head_dim=80）
- 與 ViT-H 標準一致

---

## B4. Adapter 拓樸（Neck）

從 `hf_nemotron_parse_modeling.py` `RadioWithNeck` 類，從 safetensors 驗證：

```
encoder.conv1.weight:      [1024, 1280, 1]    # Conv1d(in=1280, out=1024, kernel=1)
encoder.layer_norm1:       [1024]
encoder.conv2.weight:      [1024, 1024, 1, 4]  # Conv2d(1024→1024, kernel=(1,4), stride=(1,4))
encoder.layer_norm2:       [1024]
encoder.sum_proj.weight:   [1024, 3840]        # Linear(3840→1024) for summary tokens
encoder.layer_norm3:       [1024]
```

| 元件 | 規格 |
|---|---|
| `conv1` | Conv1d(1280→1024, kernel=1, groups=1)，作用在 feature sequence |
| `layer_norm1` | LN(1024) |
| `conv2` | Conv2d(1024→1024, kernel=(1,4), stride=(1,4))，**寬度方向 4x 壓縮** |
| `layer_norm2` | LN(1024) |
| `sum_proj` | Linear(3840→1024)，處理 8個cls × 480?（實際 summary shape 3840 = 3教師×1280） |
| `layer_norm3` | LN(1024) |

**Visual token 壓縮比：** 13312 patches → conv2 stride 4 壓縮 → 3328 tokens + 1 summary token = **3329 neck tokens**

**輸入→輸出：** encoder hidden 1280 → neck 1024，與 decoder d_model=1024 對齊

---

## B5. Decoder（mBART-like）

| 欄位 | 值 |
|---|---|
| 實際層數 | **10**（safetensors `decoder.layers.0`~`.9`，config `decoder_layers=10`） |
| hidden_size | 1024（`d_model`） |
| cross-attention key/value 來源 | **Adapter(Neck)輸出**（`encoder_hidden_states` = neck 的 `last_hidden_state`，shape [B, 3329, 1024]） |
| cross-attn Q/K/V weight shape | `[1024, 1024]`（所有 Q/K/V proj 均 1024×1024） |
| share decoder embedding ↔ lm_head | **否**（`tie_word_embeddings=false`，兩者 weight 獨立；`decoder.embed_tokens.weight [52352,1024]` 和 `lm_head.weight [52352,1024]` 各自存在） |
| Final LN | `decoder.layer_norm`（add_final_layer_norm=true） |

---

## B6. Special Tokens

從 `tokenizer.json` `added_tokens` 欄位讀取（base vocab = 50000 BPE tokens）：

| Special Token | Vocab ID |
|---|---|
| `<predict_bbox>` | **50004** |
| `<predict_classes>` | **50008** |
| `<output_markdown>` | **50001** |
| `<predict_text_in_pic>` | **50009** |
| `<predict_no_text_in_pic>` | **50010** |

其他相關 tokens：
- `<no_bbox>` → 50005
- `<bbox>` → 50006
- `<no_classes>` → 50007

標準 tokens：bos=`<s>`(0), eos=`</s>`(2), pad=`<pad>`(1), unk=`<unk>`(3)

**v1.2 prompt 格式（必須 4-token）：**
```
</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>
= [2, 0, 50004, 50008, 50001, 50010]
```

---

## B7. Processor 配置

| 欄位 | 值 |
|---|---|
| 目標影像尺寸 | **2048×1664**（height×width，v1.1 為 2048×1648）**已驗證** |
| normalize | `do_normalize: false`（正規化在 RADIO encoder 內部完成） |
| rescale_factor | 0.00392156862745098（= 1/255） |
| resize 策略 | **保持比例縮小（LongestMaxSizeHW）→ 白色 padding 到 2048×1664** |
| padding 顏色 | RGB(255,255,255) 白色 |
| 輸出 prompt template | `</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>` |
| 依賴套件 | `albumentations`（A.PadIfNeeded）、`cv2`（resize/pad）、`torchvision.transforms` |

---

## B8. License 合規（Hard Requirement）

### 授權資訊
- **License 名稱：** NVIDIA Open Model License（`license_name: nvidia-open-model-license`）
- **連結：** https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/
- **Tokenizer：** CC-BY-4.0（`explainability.md` 提及 NVIDIA Community Model License 及 CC-BY-4.0）

### 合規判斷

| 問題 | 判斷 |
|---|---|
| 是否允許 local derivative weights（MLX 轉檔） | **有疑慮 TODO** — NVIDIA Open Model License 一般允許 derivative works，但 MLX 轉換屬於格式轉換是否算 derivative 需確認 |
| 是否允許法律業務**內部使用** | **允許**（"This model is ready for commercial use."，README 明示商業使用） |
| 是否需 attribution / NOTICE | **有疑慮 TODO** — NVIDIA Open Model License 通常要求保留 NVIDIA 歸因聲明 |
| 分發轉換後的模型 | **有疑慮 TODO** — 若對外分發 MLX 格式需確認授權條款第 3-4 條 |

### 結論：**有疑慮**

- **內部使用（不對外分發）**：允許
- **轉換格式用於 MAGI 內部 OCR pipeline**：大概率允許，但需法務確認 NVIDIA Open Model License 條款中 "modify" 定義是否涵蓋格式轉換
- TODO: 讀完 https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/ 第 2 條（Grant of License）確認 derivative format 的定義

---

## B9. 記憶體預估（純算，不 load 模型）

### 參數量
- safetensors 總資料位元組：3,745,188,184 - 84,256（header）= 3,745,103,928 bytes
- F32（4 bytes/param）→ 約 **0.94 billion params**

### FP16 推理峰值估算

| 元件 | 估算 |
|---|---|
| fp16 weights（0.94B × 2 bytes） | 1.87 GB |
| Encoder activations（32 layers × 13312 patches × 1280 × 2 bytes） | 1.09 GB |
| KV cache decoder self-attn（10 layers × 9000 seq × 1024 × 2 × 2 bytes） | 0.37 GB |
| KV cache decoder cross-attn（10 layers × 3329 tokens × 1024 × 2 × 2 bytes） | 0.14 GB |
| **FP16 峰值合計** | **~3.5 GB** |

### 量化後大小
| 精度 | 估算大小 |
|---|---|
| Q8（1 byte/param） | 0.94 GB |
| Q4（0.5 byte/param） | 0.47 GB |

### 結論
- **FP16 峰值 ~3.5 GB < 8 GB 閾值 → 不需立即量化**
- 在 M-series Mac 16 GB RAM 環境下 fp16 可直接執行
- 量化（q8）可進一步降至 ~1.5 GB（weights + KV），適合長期駐留

---

## B10. SSD Cache 互動

### settings.json 分析
```json
"cache": {
  "enabled": true,
  "ssd_cache_dir": "/Users/ai/.omlx/cache-e4b",
  "ssd_cache_max_size": "10GB",
  "hot_cache_max_size": "2GB",
  "initial_cache_blocks": 4
}
```

- `ssd_cache_dir` = `/Users/ai/.omlx/cache-e4b`
- `model_dirs` = `/Users/ai/.omlx/models-text-smol`
- **`~/.omlx/models-vision/` 不在 model_dirs 內**，也不在 ssd_cache_dir 路徑下

### 結論
- Nemotron Parse 權重（`~/.omlx/models-vision/nemotron-parse-v1.2-hf/`）**不受 oMLX LRU cache 淘汰機制管理**，因為它不在 `model_dirs` 或 `ssd_cache_dir` 所管轄的路徑
- 無需 pin，也不存在被淘汰的風險
- **但 oMLX server 目前不知道這個模型目錄**——Phase 2 整合時需決定是否在 settings.json 加入 `models-vision` 路徑或用獨立推理服務

---

## B11. Special Token 的 Generation 與 Postprocess Hint

從 `postprocessing.py` 分析：

### 輸出格式
模型輸出是以 bbox token 包裹的文字序列：
```
<x_X1><y_Y1>{text_content}<x_X2><y_Y2><class_CLASSNAME>
```

- bbox 座標為 **相對值**（0.0~1.0 × target_w/h），需用 `transform_bbox_to_original()` 轉換回原始像素座標
- 座標格式：`<x_0.123><y_0.456>`（浮點數）

### Postprocess 邏輯
- `extract_classes_bboxes(text)` → classes, bboxes, texts
- `transform_bbox_to_original(bbox, orig_w, orig_h, target_w=1664, target_h=2048)` — 注意 target_w/h 與 image_size 順序相反（width first in function, height first in config）
- `postprocess_text(text, cls, text_format, table_format)` — markdown/plain/HTML 多格式輸出

### Prompt Token 對應功能
| Token | 作用 |
|---|---|
| `<predict_bbox>` | 輸出包含 bbox 座標 |
| `<predict_classes>` | 輸出 `<class_XXX>` 標籤 |
| `<output_markdown>` | 以 markdown 格式輸出文字（表格用 LaTeX） |
| `<predict_text_in_pic>` | 從圖片中的嵌入圖像提取文字 |
| `<predict_no_text_in_pic>` | 跳過圖片中的文字（速度較快） |

### MLX 實作注意
- bbox token `<x_N>` 和 `<y_N>` 不在 added_tokens 中，推測是動態生成的 text token（如 `<x_0.5>`）
- postprocess 只需 stdlib `re`，**不需額外 Python 套件**
- `latex2html.py` 是本地 module，需一同移植

---

## B12. 風險登記（Phase 2-3 可能撞牆點）

### 風險 1：Learned Positional Embedding 需插值（中等風險）
- pos_embed shape `[1, 16384, 1280]`（128×128），實際輸入 2048×1664 → 13312 patches（104×128）
- 推理時需做 2D bilinear interpolation（reshape 到 128×128 grid → interpolate → flatten）
- MLX 支援 `mx.image.resize`（bilinear），但需手動實作 reshape→resize→reshape 的 pos_embed 插值
- **若尺寸永遠固定（2048×1664），可直接預先插值 pos_embed 後凍結，規避此問題**

### 風險 2：C-RADIO 依賴 `timm` 的 ViT-H 實作（高風險）
- RADIO encoder 使用 `timm` 的 `vit_huge_patch16_224`，內部有 `spectral_reparam`（頻譜重參數化，涉及 `torch.linalg.matrix_norm`）
- config `force_spectral_reparam: true` — 需確認 checkpoint 是否已 merge spectral reparam（merged 則推理無需 `torch.linalg`）
- MLX 無 `torch.linalg.matrix_norm`；若未 merge 則 Phase 2 需手動 fuse 或以 MLX norm 替代

### 風險 3：Decoder 使用 SDPA（可解但需測試）
- `decoder._attn_implementation = "sdpa"` 對應 `torch.nn.functional.scaled_dot_product_attention`
- MLX 有 `mlx.core.fast.scaled_dot_product_attention`，語意相容，但 mask 處理（4D causal mask）的格式需調整
- modeling.py 內有 `_prepare_4d_causal_attention_mask_for_sdpa`，需用 MLX 等效替代

### 風險 4：Conv2D 非標準 kernel（低風險）
- `conv2 = Conv2d(1024, 1024, kernel_size=(1,4), stride=(1,4))` — 非正方形 kernel，stride 只在 width 方向
- MLX `mlx.core.conv2d` 支援非正方形 kernel 和 stride，理論上可直接對應
- 需確認 channel-first vs channel-last（MLX 為 NHWC，PyTorch 為 NCHW，neck 的 rearrange 需調整）

### 風險 5：einops 依賴（可解）
- modeling.py 使用 `from einops import rearrange`
- MLX 版本需用 `mx.reshape` + `mx.transpose` 手動複製
- einops 本身不依賴 PyTorch，但移植時需逐行對照

### 風險 6：Processor 依賴 albumentations + cv2（高風險，推理端）
- `NemotronParseImageProcessor` 依賴 `albumentations==2.0.8` 和 `cv2`
- 可用 PIL + numpy 手動實作 `_resize_with_aspect_ratio` + `_pad_to_size`（邏輯已在文件中，無需 albumentations）
- **可完全替換，風險可降至低**

### 風險 7：Tokenizer 格式（無風險）
- Tokenizer 類型：**BPE + ByteLevel decoder**（`tokenizer.json`，HuggingFace fast tokenizer 格式）
- **不需 sentencepiece protobuf**（非 SentencePiece 格式）
- MLX LM 生態有現成 BPE tokenizer 支援（`mlx-lm` tokenizer 載入器），或直接用 `tokenizers` Python lib

### 風險 8：C-RADIO summary 輸出格式（需 NVIDIA code 確認）
- `radio_output = self.model_encoder(pixel_values)` 回傳 `(summary, feature)` tuple
- summary shape 推測為 `[B, 3840]`（3教師 × 1280），但 RADIO HuggingFace model code 是動態載入（`trust_remote_code`）
- Phase 2 需從 `nvidia/C-RADIOv2-H` 的 `hf_model.py` 確認 forward 回傳格式

---

## 附：架構概覽圖（文字版）

```
Input Image (任意尺寸)
    │ resize+pad (LongestMaxSizeHW + white pad)
    ▼
Pixel Values [B, 3, 2048, 1664] (uint8→float32 / 255)
    │
    ▼ RadioWithNeck (encoder)
C-RADIO ViT-H (32 blocks, hidden=1280, patch=16)
    → features: [B, 13312, 1280]
    → summary:  [B, 3840]  (3 teachers × 1280)
    │
    ├─ conv1 (Conv1d 1280→1024) → [B, 13312, 1024]
    ├─ layer_norm1
    ├─ reshape to [B, 1024, 104, 128]
    ├─ conv2 (Conv2d kernel 1×4, stride 1×4) → [B, 1024, 104, 32]
    ├─ reshape to [B, 3328, 1024]
    ├─ layer_norm2
    ├─ sum_proj (3840→1024) → [B, 1, 1024]
    └─ cat → [B, 3329, 1024]  (neck_tokens)
    │
    ▼ NemotronParseDecoder (MBart, 10 layers)
Cross-Attention: Q←decoder, KV←neck [B, 3329, 1024]
    │
    ▼ lm_head Linear(1024→52352)
Output tokens → postprocess_text() → bbox+class+markdown
```
