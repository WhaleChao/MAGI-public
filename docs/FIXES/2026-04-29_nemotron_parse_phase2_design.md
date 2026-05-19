# Nemotron Parse v1.2 — Phase 2 MLX Port 設計報告

**日期：** 2026-04-29
**設計人：** Opus（靜態檔案分析，未啟動推理）
**前置：**
- `2026-04-28_nemotron_parse_community_port_survey.md`（結論：完全自寫）
- `2026-04-28_nemotron_parse_model_audit.md`（架構審計）

---

## 0. 路線修正（取代原計劃 Phase 1）

原計劃 §4 Phase 1「HF baseline + 繁中 hard gate」**在 24 GB M4 Mac 上不可行**。兩次嘗試（含全部 oMLX bootout 後仍）：

| 配置 | 結果 |
|---|---|
| oMLX 在跑 + max_tokens=2048 async | python 載入後 inference 起跑即被 agent timeout 連帶殺 |
| oMLX 全停 + max_tokens=1024 fg | 12 分鐘卡 sample 1，pages free 跌至 4207、swap 漲到 11 GB，主動 SIGKILL 保護系統 |

根因：transformers + torch + trust_remote_code custom modeling 整套 Python 環境的 working set，疊上 model bf16 權重，實際峰值 ~10 GB（audit 估的 3.5 GB 只算純算術），24 GB Mac 沒餘裕。

**修正策略：跳過 HF baseline，改用 NVIDIA `golden_outputs.json` 作層級對齊 oracle。**

| 原計劃 | 修正 |
|---|---|
| Phase 1: HF baseline → 繁中 hard gate | **取消**。oracle 改為 NVIDIA 內建 golden（英文圖片），繁中驗收延後到 Phase 5 MLX deploy 階段 |
| Phase 2: MLX runtime 對 HF 對齊 | 改為對 `golden_outputs.json` 對齊，標準更嚴格（NVIDIA reference 是 H100 cuda bf16） |
| Phase 5: live benchmark | 不變，但是第一個跑繁中的場景 |

---

## 1. Audit 風險閘門結論

| audit 標記風險 | 等級 | Phase 2 設計時實測 | 結論 |
|---|---|---|---|
| spectral_reparam | 高 | 0 個 `weight_orig`/`weight_u`/`weight_v`/`parametrizations` tensor | **已 merge，無需 `torch.linalg.matrix_norm`** |
| pos_embed 插值 | 中 | 固定輸入 2048×1664 → 13312 patches，可預先插值常數化 | **降為低**（2D bilinear 一次性，pre-fuse 至 weight） |
| processor albumentations + cv2 | 高 | 邏輯為 LongestMaxSizeHW + PadIfNeeded(white) + rescale 1/255 | **降為低**（PIL+numpy 重寫） |
| decoder SDPA | 中 | mlx.fast.scaled_dot_product_attention 語意相容 | **保留中**（mask 格式須測） |
| C-RADIO summary 輸出格式 | 中 | tuple `(summary, feature)`、shape `[B, 3840]` | **降為低**（已 audit 確認） |
| timm 依賴 ViT-H | 高 | 標準 patch16 ViT-H，已 confirmed merged spectral norm | **降為低**（純 nn.Linear/Conv2d MLX 等價） |

**新發現的 Phase 2 風險（audit 沒列）：**

| 新風險 | 等級 | 處置 |
|---|---|---|
| MLX `Conv1d` API 是否支援 stride 與 padding 與 PyTorch 完全一致 | 中 | Phase 2c image_processor 對 golden first_20_values 一致時驗證 |
| MLX 沒有 `torch.nn.Embedding` 對應的 `embedding_lookup`（須用 `mx.take` 或 `mx.gather`） | 低 | 重寫 |
| MLX `LayerNorm` 預設 affine、bias 需手動處理 | 低 | weight conversion 時保留 weight + bias |
| F32 → bf16 量化會影響 first 1e-5 容忍：保留 fp32 進 weight conversion 是否值得 | 中 | **建議 conversion 時雙版本各做一份**，alignment 用 fp32，部署用 bf16 |

---

## 2. 服務架構：獨立 sidecar，非整合 oMLX

**決策：獨立 LaunchAgent + 獨立 port（8094），不掛 oMLX engine_pool。**

理由：

1. oMLX engine_pool 設計目標是 decoder-only LLM（KV cache、greedy/sampling、`/v1/chat/completions` schema），把 vision encoder + encoder-decoder cross-attention 硬塞進去會把現有 4 個 server 搞不穩
2. Failure isolation：Nemotron OOM / crash 不影響 gemma/phi/smol/embed
3. Memory bookkeeping 各自獨立：MLX vision 模型 vs MLX text 模型有不同的 SSD cache 策略
4. 計劃 §4 已指定 port 8094 + label `com.magi.omlx-nemotron-parse`，沿用

服務邊界：

```
MAGI process
  → skills/engine/ocr/nemotron_parse_provider.py    (HTTP client)
  → http://127.0.0.1:8094/v1/ocr/parse              (FastAPI)
  → MLX Nemotron Parse runtime (此設計報告涵蓋)
  → ~/.omlx/models-vision/nemotron-parse-v1.2-mlx/   (轉換後權重)
```

**啟用條件**保留計劃 §1.4：

```bash
MAGI_NEMOTRON_PARSE_ENABLE=0    # 預設關
MAGI_NEMOTRON_PARSE_PORT=8094
MAGI_NEMOTRON_PARSE_MAX_PAGES=1
MAGI_NEMOTRON_PARSE_TIMEOUT_SEC=45
```

LaunchAgent plist 設 `Disabled=true` 第一階段，pass benchmark 後手動 enable。

---

## 3. MLX Runtime 模組架構

```
skills/engine/ocr/nemotron_mlx/
├── __init__.py
├── config.py            # NemotronParseConfig dataclass（從 HF config.json 載入）
├── weight_map.py        # HF tensor name → MLX module path 對映
├── image_processor.py   # PIL + numpy，無 cv2/albumentations
├── radio_encoder.py     # C-RADIO ViT-H 32 layers + adapter (neck)
│   └── 含 vit blocks + cls_tokens + pos_embed + neck convs
├── mbart_decoder.py     # 10 layers，self_attn + cross_attn + FFN + final_LN + lm_head
├── generation.py        # greedy KV cache，max_new_tokens
├── postprocess.py       # bbox/class regex 抽取（純 stdlib re）
└── runtime.py           # 整合：load_weights + forward + generate + decode
```

**禁止 import**：`torch`、`transformers`、`timm`、`einops`、`albumentations`、`cv2`、`torchvision`。
**允許 import**：`mlx.core`、`mlx.nn`、`mlx.utils`、`numpy`、`PIL.Image`、`PIL.ImageDraw`、stdlib（`json`、`re`、`struct`、`pathlib`、`hashlib`）。
**MLX 版本**：固定到計劃實作時 `mlx>=0.20`（避開 deprecated `mx.einsum` 等）。

---

## 4. Weight Conversion 詳細規格

### 4.1 入口腳本

`scripts/ops/convert_nemotron_parse_to_mlx.py`

```bash
./venv/bin/python3 scripts/ops/convert_nemotron_parse_to_mlx.py \
  --src ~/.omlx/models-vision/nemotron-parse-v1.2-hf \
  --dst ~/.omlx/models-vision/nemotron-parse-v1.2-mlx \
  --dtype bf16   # 或 fp32（layer alignment 用）
```

### 4.2 來源權重 → MLX module 路徑映射

完整 667 tensors。分四群：

#### A. Encoder（C-RADIO ViT-H + cls/pos）

| HF prefix | MLX prefix |
|---|---|
| `encoder.model_encoder.radio_model.model.patch_embed.proj.weight/.bias` | `radio.patch_embed.proj.{weight,bias}` |
| `encoder.model_encoder.radio_model.model.cls_token` | `radio.cls_token`（shape [8, 1280]） |
| `encoder.model_encoder.radio_model.model.pos_embed` | `radio.pos_embed`（shape [1, 16384, 1280]） |
| `encoder.model_encoder.radio_model.model.norm.{weight,bias}` | `radio.final_norm.{weight,bias}` |
| `encoder.model_encoder.radio_model.model.blocks.{i}.attn.qkv.{weight,bias}` | `radio.blocks.{i}.attn.qkv.{weight,bias}` |
| `encoder.model_encoder.radio_model.model.blocks.{i}.attn.proj.{weight,bias}` | `radio.blocks.{i}.attn.proj.{weight,bias}` |
| `encoder.model_encoder.radio_model.model.blocks.{i}.norm1/norm2.{weight,bias}` | `radio.blocks.{i}.norm1/norm2.{weight,bias}` |
| `encoder.model_encoder.radio_model.model.blocks.{i}.mlp.fc1/fc2.{weight,bias}` | `radio.blocks.{i}.mlp.fc1/fc2.{weight,bias}` |
| `encoder.model_encoder.radio_model.summary_idxs` | `radio.summary_idxs`（I64 [3]） |

`{i}` = 0..31

#### B. Adapter（Neck）

| HF | MLX |
|---|---|
| `encoder.conv1.{weight,bias}` | `neck.conv1.{weight,bias}`（Conv1d 1280→1024 k=1） |
| `encoder.layer_norm1.{weight,bias}` | `neck.ln1.{weight,bias}` |
| `encoder.conv2.{weight,bias}` | `neck.conv2.{weight,bias}`（Conv2d 1024→1024 k=(1,4) s=(1,4)） |
| `encoder.layer_norm2.{weight,bias}` | `neck.ln2.{weight,bias}` |
| `encoder.sum_proj.{weight,bias}` | `neck.sum_proj.{weight,bias}`（Linear 3840→1024） |
| `encoder.layer_norm3.{weight,bias}` | `neck.ln3.{weight,bias}` |

#### C. Decoder（mBart 10 layers）

| HF | MLX |
|---|---|
| `decoder.embed_tokens.weight` | `decoder.embed_tokens.weight`（[52352, 1024]） |
| `decoder.embed_positions.weight` | `decoder.pos_embed.weight`（learned，9002 max） |
| `decoder.layernorm_embedding.{weight,bias}` | `decoder.ln_embed.{weight,bias}` |
| `decoder.layers.{i}.self_attn.{q,k,v,out}_proj.{weight,bias}` | `decoder.layers.{i}.self_attn.{q,k,v,out}_proj.{weight,bias}` |
| `decoder.layers.{i}.self_attn_layer_norm.{weight,bias}` | `decoder.layers.{i}.ln_self.{weight,bias}` |
| `decoder.layers.{i}.encoder_attn.{q,k,v,out}_proj.{weight,bias}` | `decoder.layers.{i}.cross_attn.{q,k,v,out}_proj.{weight,bias}` |
| `decoder.layers.{i}.encoder_attn_layer_norm.{weight,bias}` | `decoder.layers.{i}.ln_cross.{weight,bias}` |
| `decoder.layers.{i}.fc1/fc2.{weight,bias}` | `decoder.layers.{i}.fc1/fc2.{weight,bias}` |
| `decoder.layers.{i}.final_layer_norm.{weight,bias}` | `decoder.layers.{i}.ln_final.{weight,bias}` |
| `decoder.layer_norm.{weight,bias}` | `decoder.final_norm.{weight,bias}` |

`{i}` = 0..9

#### D. LM Head

| HF | MLX |
|---|---|
| `lm_head.weight` | `lm_head.weight`（[52352, 1024]，**`tie_word_embeddings=false` 須獨立保留**） |
| `final_logits_bias` | `final_logits_bias`（[1, 52352]） |

### 4.3 dtype 處理

- 來源 F32（audit B1 確認）
- 目標 bf16（部署）+ fp32（alignment 用）
- summary_idxs 保留 int32（MLX 沒有 i64 native，cast 為 int32 即可，值都 < 32）
- patch_embed.proj.weight: Conv2d，shape `[1280, 3, 16, 16]` → MLX 是 NHWC 格式，須 transpose 為 `[1280, 16, 16, 3]`
- 所有其他 Conv2d/Conv1d weight 同樣須 NCHW → NHWC transpose

### 4.4 conversion_manifest.json

```json
{
  "source_model": "nvidia/NVIDIA-Nemotron-Parse-v1.2",
  "source_sha256": "9d77822f43a04504619e4c3527e1371568b6e33dba94b7c3a0c0cf509a200cd4",
  "source_safetensors_bytes": 3745188184,
  "source_dtype": "F32",
  "target_dtype": "bf16",
  "tensor_count": 667,
  "skipped_tensors": [],
  "transposed_conv_tensors": [...list of conv weight names...],
  "conversion_time_iso": "2026-04-29T...",
  "conversion_duration_sec": 0.0,
  "weight_map_version": "v1.0"
}
```

### 4.5 驗收（Phase 2a）

- 667 tensors 全部映射、無 skipped
- bf16 輸出檔大小 ≈ 1.87 GB（4 bytes → 2 bytes）
- Conv weight transpose 後 MLX shape 與 HF shape 對應正確
- Manifest 寫出
- **不需要 load 模型推理**

---

## 5. 對齊驗收（Phase 2b-f）

**Oracle：** `~/.omlx/models-vision/nemotron-parse-v1.2-hf/golden_outputs.json`
**Test image：** `make_test_image()` from `test_golden.py`（純 PIL stdlib 可複製）

### Phase 2b：image_processor 對齊

```bash
./venv/bin/python3 -m skills.engine.ocr.nemotron_mlx.image_processor \
  --self-test
```

驗收：
- shape `[1, 3, 2048, 1664]`
- mean ≈ 0.0653718（abs diff < 1e-4）
- std ≈ 0.2386379（abs diff < 1e-4）
- first_20_values 全 0.0（abs diff < 1e-5）

通過：image_processor 與 HF processor 像素級一致。

### Phase 2c：encoder 對齊

```bash
./venv/bin/python3 -m skills.engine.ocr.nemotron_mlx.radio_encoder \
  --self-test --weights ~/.omlx/models-vision/nemotron-parse-v1.2-mlx/fp32
```

注意：**需 load encoder 權重 ~1.5 GB**。預估 MLX 推理峰值 < 4 GB。
若 oMLX 4 server 同時在跑（佔 ~3-4 GB），加 4 GB = 8 GB，**24 GB Mac 應容得下**（compressor + 應用程式佔 12 GB 內均可）。
若實測 RSS > 8 GB，先單獨跑（其他 4 server 不停，只是不要同時做高負載）。

驗收（fp32 path）：
- output shape `[1, 3329, 1024]`
- mean ≈ -0.0001354（abs diff < 0.05）
- std ≈ 0.9438313（abs diff < 0.05）
- token0_first16 各值 abs diff < 0.1

通過：encoder + neck 計算與 NVIDIA cuda bf16 reference 一致（在 bf16 容忍內）。

### Phase 2d：adapter（已涵蓋於 2c，因 neck 是 encoder 一部分）

不獨立驗收。

### Phase 2e：decoder forward 一步

```bash
./venv/bin/python3 -m skills.engine.ocr.nemotron_mlx.runtime \
  --self-test-decoder-step
```

decoder_input_ids = `[[2]]`（EOS 起始）

驗收：
- logits shape `[1, 1, 52352]`
- top-10 indices **完全一致**（exact match）
- top-10 values 各 abs diff < 1.0

通過：decoder + cross-attention + lm_head 計算正確。

### Phase 2f：generation 50 tokens

```bash
./venv/bin/python3 -m skills.engine.ocr.nemotron_mlx.runtime \
  --self-test-generation
```

prompt = `</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic>`
max_new_tokens = 50, do_sample=False, num_beams=1

**驗收：token_ids 與 golden_outputs.json `generation.token_ids` 完全一致（29 tokens）**：

```
[2, 0, 50004, 50008, 50001, 50010, 50412, 51799, 82, 2722, 113, 18121, 579, 115,
 113, 19321, 89, 115, 221, 82, 493, 113, 18121, 579, 115, 50633, 51850, 52327, 2]
```

decoded_text 解碼後等於：
```
</s><s><predict_bbox><predict_classes><output_markdown><predict_no_text_in_pic><x_0.3916><y_0.5969>\begin{tabular}{ccccc}\n\end{tabular}<x_0.6074><y_0.6367><class_Table></s>
```

**這是最強的回歸訊號**——bf16 任何累積誤差到 50 tokens 都會發散，能匹配代表整條管線端到端正確。

---

## 6. KV Cache 設計

decoder 每步 cross-attn 的 K/V 來自 neck（固定 [B, 3329, 1024]，全程不變），self-attn 的 K/V 隨 generation 增長。

```python
class NemotronKVCache:
    # cross-attn: encoder_kv 在 prefill 階段算一次，後續複用
    cross_kv: list[tuple[mx.array, mx.array]]  # 10 layers × (k, v)
    # self-attn: 增長式
    self_kv: list[tuple[mx.array, mx.array]]   # 10 layers × (k, v) 每步 append
    decode_step: int
```

prefill：encode 整張圖 → neck → 對每層算 cross K=k_proj(neck), V=v_proj(neck) 存住
decode step n：
1. token embed + pos_embed[n]
2. self-attn：K[n]=k_proj(x), V[n]=v_proj(x)，append 到 self_kv，然後 attn(q=Q[n], k=self_kv, v=self_kv) 加 causal mask
3. cross-attn：q=q_proj(x), 用 prefill 算好的 cross_kv
4. FFN
5. lm_head → next_token = argmax

---

## 7. Sidecar Server（Phase 4）

```
scripts/serve_nemotron_parse_omlx.py
```

FastAPI（複用 oMLX 慣例）：

```python
@app.get("/health")
async def health():
    return {"ok": True, "engine": "nemotron-parse-v1.2-mlx",
            "model_loaded": _model_loaded, "device": "mlx",
            "memory_mb": _peak_rss_mb, "last_request_at": _last_req_iso}

@app.get("/v1/models")
async def list_models():
    return {"data": [{"id": "nemotron-parse-v1.2-mlx", "object": "model"}]}

@app.post("/v1/ocr/parse")
async def parse(req: ParseRequest):
    # req: {image_path, prompt_mode="markdown_bbox", max_tokens=9000, timeout_sec=45}
    # return: {success, engine, text, blocks, quality, duration_sec, error}
```

LaunchAgent label：`com.magi.omlx-nemotron-parse`，port 8094，預設 `Disabled=true`。

---

## 8. Phase 2 → Phase 6 Sonnet 派工順序

每階段獨立 dispatch，Opus 驗證後才放下一階段。

| 階段 | 任務 | 主要產出 | 安全性 |
|---|---|---|---|
| **2a** | Weight conversion 腳本 + manifest | `~/.omlx/models-vision/nemotron-parse-v1.2-mlx/{fp32,bf16}/` | 無 inference，零風險 |
| **2b** | image_processor.py + self-test | golden 一致 | mx import 但無權重 load |
| **2c** | radio_encoder.py + self-test | golden encoder_output 一致 | load encoder 1.5 GB，oMLX 同跑可行 |
| **2d/2e** | mbart_decoder.py + 1-step forward | golden top-10 一致 | load full model，需 oMLX 不忙 |
| **2f** | generation.py + 50-token exact | golden token_ids 一致 | 同上 |
| **3** | postprocess.py + runtime.py 整合 | E2E 推理 PIL → markdown | 同上 |
| **3.5** | Quantization ablation（保留計劃要求） | bf16/q8 兩版 vs fp32 reference 退化評估 | 同上 |
| **4** | sidecar server + LaunchAgent | `/health` + `/v1/ocr/parse` 通 | sidecar 啟動但 disabled |
| **5** | provider 接 pdf-namer + 繁中 hard gate | 5 份繁中 PDF 比對 macOS Vision | **這才是真繁中驗收** |
| **6** | 50 份 benchmark + cron | benchmark 表 + nightly OCR dataset | live |

---

## 9. 邊界與紅線（每次 Sonnet dispatch 必複貼）

1. 不碰 NAS（`~/Library/CloudStorage/SynologyDrive-homes`）
2. 不碰閱卷 / 筆錄 / 法扶業務模組
3. 不停 oMLX 4 server（除非 Opus 明確授權）
4. 不裝 pip 套件，除了 mlx 系列（mlx, mlx-lm 等）；`pip list` 前後存證
5. 不 commit / push（Opus 驗收後手動 commit）
6. 任何 Phase 2c+ 載入權重的腳本，**先 print free RAM、若 < 2 GB 直接 abort**
7. 推理失敗時把 partial result 寫 JSON、退出，不重試不 retry loop

---

## 10. 記憶體預算修正（vs Audit）

Audit B9 估 fp16 峰值 3.5 GB **僅算純算術**，沒含 Python 環境。

實測（昨天 04-28）：transformers + torch + 模型 load = **5.9 GB RSS**；inference 後 working set 漲到 ~10 GB 觸發 swap thrash。

**MLX 預期實際峰值（無 torch/transformers）：**

| 元件 | bf16 估算 |
|---|---|
| Python 3.14 + mlx + numpy + PIL | ~300 MB |
| bf16 weights | 1.87 GB |
| MLX lazy graph metadata | ~100 MB |
| Encoder activations（13312 tokens × 1280 × 32 layers, peak only at one layer） | ~140 MB（lazy eval 不全駐留） |
| Cross KV cache（10 layers × 3329 × 1024 × 2 × 2 bytes） | 130 MB |
| Self KV cache（10 layers × 9000 × 1024 × 2 × 2 bytes） | 360 MB（max generation） |
| Decoder activations | ~50 MB |
| **MLX 峰值** | **~3.0 GB** |

24 GB Mac + 4 oMLX server（~3-4 GB）+ MLX Nemotron（3 GB）+ 應用 8 GB ≈ 18 GB。應有餘裕。

若實測超過 5 GB，立即降 q8（< 1.5 GB）或 q4（< 0.8 GB）。

---

## 11. 結語

Phase 2 設計依據齊備，所有 audit 風險閘門靜態驗證後均降為「中」或「低」。

最大不確定性：MLX 實測峰值是否真的 3 GB（理論計算）還是會像 transformers 那樣意外膨脹。**Phase 2c 第一次載入 encoder 權重時若 RSS > 4 GB，須立即停手 reassess**。

Sonnet 開工前，Opus 須驗證：
- weight conversion manifest 完整
- self-test 腳本架構齊備（不需要先把所有層寫完，每階段一個 self-test 即可）

設計報告 lock 在此版本，後續修改透過新檔案（`2026-04-29_nemotron_parse_phase2_design_v2.md` 等），不直接覆寫此檔。
