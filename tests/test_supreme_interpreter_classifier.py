import importlib.util
import sys
from pathlib import Path


_MODULE_PATH = Path("/Users/ai/Desktop/MAGI_v2/scripts/classify_supreme_interpreter_mentions.py")
_SPEC = importlib.util.spec_from_file_location("supreme_interpreter_classifier", _MODULE_PATH)
classifier = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = classifier
_SPEC.loader.exec_module(classifier)


def test_pure_legal_template_is_not_quality_issue():
    context = "又以原判決所憑之證言、鑑定或通譯已證明其為虛偽者，得聲請再審。刑事訴訟法第420條定有明文。"
    primary, categories, role, issue_result, confidence = classifier.classify_contexts([context], "抗告駁回")
    assert primary == "法條或程序清單引用"
    assert role == "非通譯爭點"
    assert issue_result == "非通譯爭點"


def test_actual_translation_quality_issue_is_classified():
    context = "抗告人主張其於警詢、偵訊時，通譯並未如實翻譯，且譯文與其真意不符。"
    primary, categories, role, issue_result, confidence = classifier.classify_contexts([context], "抗告駁回")
    assert primary == "通譯/翻譯品質或真實性爭議"
    assert role == "通譯為上訴/抗告/再審爭點"


def test_no_interpreter_issue_is_classified():
    context = "上訴意旨主張證人聽不懂國語，偵訊供述未經通譯傳譯，應無證據能力。"
    primary, categories, role, issue_result, confidence = classifier.classify_contexts([context], "上訴駁回")
    assert primary == "未使用或未充分使用通譯爭議"
    assert role == "通譯為上訴/抗告/再審爭點"
