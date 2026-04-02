"""
screenshot-sorter-tw — 對話截圖排序與重新命名
==============================================
將資料夾中的通訊軟體對話截圖（LINE、iMessage、Messenger 等）
按照對話時間順序排序，並重新命名檔案。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ScreenshotSorter")

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
OUTPUT_BASE = MAGI_ROOT / "screenshot_sorted_output"
IMAGE_EXTENSIONS = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.PNG", "*.JPG", "*.JPEG")


def collect_images(source_dir: str) -> list[str]:
    """收集資料夾中所有圖檔。"""
    images = []
    for ext in IMAGE_EXTENSIONS:
        images.extend(glob.glob(os.path.join(source_dir, ext)))
    return sorted(images)


def analyze_screenshot(image_path: str) -> dict[str, Any]:
    """
    使用 oMLX Vision（GLM-OCR）分析單張截圖，提取時間和對話資訊。
    """
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        gw = InferenceGateway()

        prompt = (
            "這是一張通訊軟體的對話截圖。請分析並回傳以下資訊（JSON 格式）：\n"
            '{"date": "日期(YYYY-MM-DD)或null", '
            '"time_start": "最早訊息時間(HH:MM)或null", '
            '"time_end": "最晚訊息時間(HH:MM)或null", '
            '"first_message": "最上方訊息前20字", '
            '"last_message": "最下方訊息前20字", '
            '"app": "LINE/iMessage/Messenger/WeChat/unknown", '
            '"confidence": "high/medium/low"}\n'
            "只回傳 JSON，不要解釋。"
        )

        result = gw.chat(prompt=prompt, task_type="vision", timeout=60)
        text = result.get("text", "")

        # 嘗試解析 JSON
        import re
        json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            data["filename"] = os.path.basename(image_path)
            data["filepath"] = image_path
            return data
    except Exception as e:
        logger.warning("Screenshot analysis failed for %s: %s", image_path, e)

    # Fallback：用檔案修改時間
    mtime = os.path.getmtime(image_path)
    return {
        "filename": os.path.basename(image_path),
        "filepath": image_path,
        "date": None,
        "time_start": None,
        "time_end": None,
        "first_message": "",
        "last_message": "",
        "app": "unknown",
        "confidence": "low",
        "mtime": mtime,
    }


def sort_screenshots(analyses: list[dict]) -> list[dict]:
    """按時間排序截圖分析結果。"""
    def sort_key(a):
        date = a.get("date") or "9999-99-99"
        time_start = a.get("time_start") or "99:99"
        mtime = a.get("mtime") or 0
        return (date, time_start, mtime)

    return sorted(analyses, key=sort_key)


def rename_and_copy(sorted_analyses: list[dict], output_dir: str) -> list[dict]:
    """按排序結果複製並重新命名檔案。"""
    os.makedirs(output_dir, exist_ok=True)
    total = len(sorted_analyses)
    width = max(len(str(total)), 3)

    manifest = []
    for i, a in enumerate(sorted_analyses, 1):
        original = a["filename"]
        new_name = f"{str(i).zfill(width)}_{original}"
        src = a["filepath"]
        dest = os.path.join(output_dir, new_name)
        shutil.copy2(src, dest)
        manifest.append({
            "order": i,
            "original": original,
            "renamed": new_name,
            "date": a.get("date"),
            "time_start": a.get("time_start"),
            "time_end": a.get("time_end"),
            "confidence": a.get("confidence", "low"),
        })

    # 輸出對照表
    manifest_path = os.path.join(output_dir, "_排序對照表.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return manifest


def add_watermark(image_path: str, output_path: str, number: int, total: int):
    """在截圖角落加上序號浮水印。"""
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)
        font_size = max(30, img.width // 20)

        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except Exception:
            font = ImageFont.load_default()

        text = f"{number}/{total}"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        margin = font_size // 2
        x = img.width - text_width - margin
        y = margin

        # 半透明背景
        padding = 10
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            [x - padding, y - padding, x + text_width + padding, y + text_height + padding],
            radius=10,
            fill=(0, 0, 0, 140),
        )

        if img.mode != "RGBA":
            img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        draw.text((x, y), text, fill=(255, 255, 255, 230), font=font)

        if output_path.lower().endswith((".jpg", ".jpeg")):
            img = img.convert("RGB")
        img.save(output_path)
    except ImportError:
        shutil.copy2(image_path, output_path)
        logger.warning("Pillow not available, watermark skipped")


def run(
    source_dir: str = "",
    output_dir: str = "",
    watermark: bool = False,
    **kwargs,
) -> dict[str, Any]:
    """
    主入口。

    Args:
        source_dir: 截圖資料夾路徑
        output_dir: 輸出資料夾路徑（空 = 自動）
        watermark: 是否加浮水印
    """
    if not source_dir:
        return {"success": False, "error": "請提供截圖資料夾路徑。"}

    source = Path(source_dir).expanduser().resolve()
    if not source.is_dir():
        return {"success": False, "error": f"資料夾不存在：{source}"}

    images = collect_images(str(source))
    if not images:
        return {"success": False, "error": f"資料夾中沒有圖檔：{source}"}

    logger.info("Found %d screenshots in %s", len(images), source)

    # 分析每張截圖
    analyses = []
    for i, img in enumerate(images, 1):
        logger.info("Analyzing %d/%d: %s", i, len(images), os.path.basename(img))
        analysis = analyze_screenshot(img)
        analyses.append(analysis)

    # 排序
    sorted_analyses = sort_screenshots(analyses)

    # 複製並重命名
    if not output_dir:
        output_dir = str(OUTPUT_BASE / source.name / time.strftime("%Y%m%d_%H%M%S"))

    manifest = rename_and_copy(sorted_analyses, output_dir)

    # 浮水印
    if watermark:
        wm_dir = os.path.join(output_dir, "watermarked")
        os.makedirs(wm_dir, exist_ok=True)
        total = len(manifest)
        for item in manifest:
            src = os.path.join(output_dir, item["renamed"])
            dst = os.path.join(wm_dir, item["renamed"])
            add_watermark(src, dst, item["order"], total)

    # 統計
    high_conf = sum(1 for m in manifest if m.get("confidence") == "high")
    low_conf = sum(1 for m in manifest if m.get("confidence") == "low")

    return {
        "success": True,
        "total": len(manifest),
        "output_dir": output_dir,
        "high_confidence": high_conf,
        "low_confidence": low_conf,
        "watermark": watermark,
        "manifest": manifest,
    }
