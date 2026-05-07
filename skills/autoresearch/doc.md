# autoresearch — 自主 ML 研究技能

基於 Andrej Karpathy 的 [autoresearch](https://github.com/karpathy/autoresearch) 框架。

## 用途

讓 MAGI 自主進行機器學習實驗：修改模型架構/超參數 → 訓練 5 分鐘 → 評估 val_bpb → 保留或放棄 → 循環。

## 需求

- NVIDIA GPU（H100/A100/4090 等）+ CUDA 12.8
- `uv` 套件管理器
- ~10 GB 磁碟空間（FineWeb-Edu 資料）
- 可透過 SSH 連線的 GPU 主機（或本機有 GPU）

## 指令

| 指令 | 說明 |
|------|------|
| `autoresearch setup <host>` | 在目標主機上準備環境 |
| `autoresearch run <host> [--tag TAG]` | 啟動自主實驗循環 |
| `autoresearch status [host]` | 查看實驗進度 |
| `autoresearch results [host]` | 取得 results.tsv |
| `autoresearch stop <host>` | 停止實驗 |

## 架構

```
prepare.py   — 資料下載/tokenizer（不可修改）
train.py     — 模型/優化器/訓練迴圈（AI 可修改）
program.md   — 實驗規範
results.tsv  — 實驗記錄
action.py    — MAGI 技能包裝器
```
