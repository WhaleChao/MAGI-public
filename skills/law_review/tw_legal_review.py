#!/usr/bin/env python3
"""
臺灣法規用語校正模組 (Taiwan Legal Review)
使用 TAIDE 本地模型校正法律用語，確保符合臺灣法規慣用語。

架構:
  分散式推理 (20B 主模型) → 產出初稿
  → 本模組 (TAIDE 8B, Local) → 法規用語校正
  → 最終回覆
"""

import json
import sys
import requests
from typing import Optional

# ── 設定 ──────────────────────────────────────────────
OLLAMA_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_NAME = "TAIDE-12b-Chat-mlx-4bit"

SYSTEM_PROMPT = """你是臺灣法律用語校正專家。請檢查以下文字，將不符合臺灣法規慣用語的部分修正。

校正規則：
1. 將中國大陸法律用語替換為臺灣用語，例如：
   - 「人民法院」→「法院」
   - 「勞動合同」→「勞動契約」
   - 「勞動者」→「勞工」
   - 「知識產權」→「智慧財產權」
   - 「著作權法實施條例」→「著作權法施行細則」
   - 「商標法實施條例」→「商標法施行細則」
   - 「專利法實施細則」→「專利法施行細則」
   - 「侵權責任」→「侵權行為損害賠償責任」
   - 「民事訴訟法解釋」→「民事訴訟法」
   - 「治安管理處罰法」→ 刪除或標註為非臺灣法規
   - 「行政復議」→「訴願」
   - 「行政訴訟」→「行政訴訟」(此為相同用語)
   - 「刑事附帶民事訴訟」→「刑事附帶民事訴訟」(此為相同用語)
   - 「公司法人」→「法人」
   - 「有限責任公司」→「有限公司」
   - 「股份有限責任公司」→「股份有限公司」

2. 確認引用的法條名稱是臺灣現行法規。
3. 確認《個人資料保護法》相關用語符合臺灣版本。
4. 只修正法律用語，不改變原意和文章結構。
5. 如果文字已經符合臺灣用語，請原文返回。

請直接輸出修正後的全文，不要加任何解釋或說明。"""


def review_legal_text(
    text: str,
    model: str = MODEL_NAME,
    ollama_url: str = OLLAMA_URL,
    timeout: int = 120,
) -> Optional[str]:
    """
    將文字送入 TAIDE 模型進行臺灣法規用語校正。

    Args:
        text: 待校正的文字
        model: Ollama 模型名稱
        ollama_url: Ollama API 位址
        timeout: 請求逾時秒數

    Returns:
        校正後的文字，或 None (如果失敗)
    """
    if not text or not text.strip():
        return text

    # Keep generation bounded so output-guard won't stall webhook replies.
    # Character-based heuristic is sufficient here because this task is "rewrite in-place".
    max_predict = max(128, min(2048, int(len(text) * 1.25)))

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": max_predict,
    }

    def _extract_reply(resp_json):
        choices = resp_json.get("choices") or []
        if choices:
            return (choices[0].get("message", {}).get("content", "") or "").strip()
        return ""

    try:
        resp = requests.post(ollama_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        result = _extract_reply(resp.json())
        return result or text
    except requests.exceptions.ConnectionError:
        print("[tw_legal_review] 錯誤: 無法連接 oMLX，請確認服務已啟動。", file=sys.stderr)
        return None
    except requests.exceptions.Timeout:
        # Retry once with a tighter generation budget to avoid blocking.
        try:
            payload["max_tokens"] = max(96, min(512, int(len(text) * 0.8)))
            resp = requests.post(ollama_url, json=payload, timeout=max(4, int(timeout // 2) or 4))
            resp.raise_for_status()
            result = _extract_reply(resp.json())
            return result or text
        except Exception:
            print(f"[tw_legal_review] 錯誤: 請求超時 ({timeout}s)。", file=sys.stderr)
            return None
    except Exception as e:
        print(f"[tw_legal_review] 錯誤: {e}", file=sys.stderr)
        return None


def review_distributed_output(
    distributed_response: dict,
    model: str = MODEL_NAME,
) -> dict:
    """
    接收分散式推理 (Melchior) 的原始回應，校正其中的文字內容。

    Args:
        distributed_response: Melchior API 回傳的 JSON (OpenAI format)
        model: 用於校正的本地模型

    Returns:
        校正後的回應 (相同格式)
    """
    if not distributed_response:
        return distributed_response

    # 提取 choices 中的 message content
    choices = distributed_response.get("choices", [])
    for choice in choices:
        message = choice.get("message", {})
        content = message.get("content", "")
        if content:
            corrected = review_legal_text(content, model=model)
            if corrected:
                message["content"] = corrected

    return distributed_response


# ── CLI 介面 ──────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python tw_legal_review.py <待校正文字>")
        print("範例: python tw_legal_review.py '根據人民法院的判決，該勞動合同無效。'")
        sys.exit(1)

    input_text = " ".join(sys.argv[1:])
    print(f"[原文] {input_text}")
    print("[校正中...]")

    result = review_legal_text(input_text)
    if result:
        print(f"[校正] {result}")
    else:
        print("[失敗] 無法完成校正。")
        sys.exit(1)
