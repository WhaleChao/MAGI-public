import json
import logging
import os
from typing import Dict, Any, Optional

# Import unified inference gateway
try:
    from skills.bridge.inference_gateway import InferenceGateway
except ImportError:
    # Fail-safe for testing or if checked out in a different structure
    import sys
    import os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))
    from skills.bridge.inference_gateway import InferenceGateway

# Configure Logger
logger = logging.getLogger("LegalDocAnalysis")

def analyze_document_content(table_contents: list, case_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Analyzes document table contents using Melchior (local LLM) to identify replacement fields.
    
    Args:
        table_contents: List of strings representing the table content key-values.
        case_data: Dictionary containing case details (client_name, case_number, etc.)
        
    Returns:
        Dict[str, str]: A dictionary mapping cell keys (e.g., '狀頭_[0,2]') to replacement text.
    """
    
    # 1. Construct the Prompt (Adapted from osc.py)
    case_info = f"""
## 新案件資料（請用這些資料替換對應欄位）：
- 當事人姓名: {case_data.get('client_name', '未知')}
- 法院案號: {case_data.get('court_case_number', '')}
- 股別: {case_data.get('court_division', '')}
- 法院名稱: {case_data.get('court_name', '')}
- 案由: {case_data.get('case_reason', '')}
- 對造姓名: {case_data.get('opponent_name', '')}
"""

    prompt = f"""你是專業的法律文件分析助手。請分析以下 Word 法律文件的「狀頭」和「狀尾」表格內容。

## 原始文件表格內容：
{chr(10).join(table_contents)}

{case_info}

## 任務：
1. 分析每個儲存格的用途（如：原告姓名、案號、股別、地址、法院等）
2. 根據「新案件資料」，判斷哪些儲存格需要替換
3. 只替換「當事人姓名、案號、股別」等會因案件不同而改變的欄位
4. 不要替換「固定文字」如「原告」、「被告」、「案號」等標籤
5. 地址如果新資料沒有提供，保持原樣

## 輸出格式（只輸出 JSON，不要任何其他文字）：
{{
  "replacements": {{
    "狀頭_[行,列]": "建議的新內容",
    "狀尾_[行,列]": "建議的新內容"
  }}
}}

例如：
{{
  "replacements": {{
    "狀頭_[0,2]": "王小明",
    "狀頭_案號_[1,2]": "113年度訴字第123號",
    "狀尾_[0,1]": "臺灣花蓮地方法院"
  }}
}}
"""

    # 2. Call inference gateway (remote->backup->local)
    print("INFO: [Skills/Legal] Sending request to InferenceGateway...")
    try:
        gateway = InferenceGateway()
        response = gateway.dispatch(
            prompt=prompt,
            task_type="legal_analysis",
            timeout=120,
            force_quality=os.environ.get("LEGAL_DOC_FORCE_QUALITY", "0").strip().lower() in {"1", "true", "yes", "on"},
            tc_review=False,
        )
        
        # 3. Parse Response
        response_text = response.get("response", "").strip()
        
        # Clean markdown code blocks if present
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
             response_text = response_text.split("```")[1].split("```")[0].strip()
            
        # Parse JSON
        result = json.loads(response_text)
        replacements = result.get('replacements', {})
        
        print(
            f"INFO: [Skills/Legal] Gateway returned {len(replacements)} suggestions "
            f"(route={response.get('route', '')}, degraded={response.get('degraded', False)})."
        )
        return replacements

    except json.JSONDecodeError as e:
        print(f"ERROR: [Skills/Legal] Failed to parse JSON from gateway output: {e}")
        print(f"Raw Response: {response_text if 'response_text' in locals() else 'None'}")
        return {}
    except Exception as e:
        print(f"ERROR: [Skills/Legal] Gateway communication failed: {e}")
        return {}
