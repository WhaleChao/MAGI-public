"""Context labels for prompt segments.

Each segment injected into a prompt carries a label indicating its
provenance tier.  The LLM system prompt references these labels so the
model knows which information to trust and which to treat with caution.

Usage::

    from api.session.context_labels import label_memory_context, TRUST_TIERS

    labeled = label_memory_context(recall_results)
    # Returns a string with each memory segment prefixed by its trust badge
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Trust tiers (ordered from most to least trustworthy)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrustTier:
    name: str
    badge: str
    instruction: str
    min_confidence: float


TRUST_TIERS = {
    "verified": TrustTier(
        name="verified",
        badge="[已驗證事實]",
        instruction="此資訊經過驗證，可直接引用。",
        min_confidence=0.85,
    ),
    "user_stated": TrustTier(
        name="user_stated",
        badge="[使用者陳述]",
        instruction="使用者自己說的話，可引用但注意時效性。",
        min_confidence=0.70,
    ),
    "retrieved": TrustTier(
        name="retrieved",
        badge="[檢索線索]",
        instruction="從記憶庫檢索到的內容，引用時請標明來源，如有疑慮應查證。",
        min_confidence=0.40,
    ),
    "derived": TrustTier(
        name="derived",
        badge="[衍生推論]",
        instruction="由摘要或推論產生，不可視為確認事實。引用時必須加上保留語氣。",
        min_confidence=0.0,
    ),
}


def classify_trust_tier(
    *,
    source_type: str = "",
    verified: bool = False,
    confidence: float = 0.0,
    derived_from: str = "",
    role: str = "",
) -> TrustTier:
    """Classify a memory entry into a trust tier."""
    if verified and confidence >= 0.85:
        return TRUST_TIERS["verified"]

    if source_type in {
        "user_rule", "user_profile", "user_confirmed",
        "manual", "statute", "official", "judicial_api",
    }:
        return TRUST_TIERS["verified"]

    if source_type in {"user_chat", "chatlog"} and role == "user":
        return TRUST_TIERS["user_stated"]

    if derived_from or source_type in {
        "summary_derived", "generated_summary", "llm_summary",
        "assistant_generated",
    }:
        return TRUST_TIERS["derived"]

    if confidence >= 0.55:
        return TRUST_TIERS["retrieved"]

    return TRUST_TIERS["derived"]


def label_single_memory(
    content: str,
    provenance: dict[str, Any] | None = None,
) -> str:
    """Wrap a single memory entry with its trust badge and instruction."""
    prov = provenance or {}
    tier = classify_trust_tier(
        source_type=prov.get("source_type", ""),
        verified=prov.get("verified", False),
        confidence=prov.get("confidence", 0.0),
        derived_from=prov.get("derived_from", ""),
        role=prov.get("role", ""),
    )
    return f"{tier.badge} {content.strip()}"


def label_memory_context(
    recall_results: list[dict[str, Any]],
) -> str:
    """Label all recall results and combine into a single context string.

    Each result dict is expected to have at least ``content`` and
    ``provenance`` keys (as returned by ``mem_bridge.recall()``).
    """
    if not recall_results:
        return ""

    parts: list[str] = []
    for result in recall_results:
        content = str(result.get("content", "")).strip()
        if not content:
            continue
        prov = result.get("provenance") or {}
        labeled = label_single_memory(content, prov)
        parts.append(labeled)

    return "\n\n".join(parts)


def build_trust_system_instruction() -> str:
    """Generate the system instruction block explaining trust tiers.

    This should be appended to the system prompt so the model understands
    how to interpret trust badges in the context.
    """
    lines = [
        "## 記憶信任等級",
        "以下是各信任等級的標記與使用規則：",
    ]
    for tier in TRUST_TIERS.values():
        lines.append(f"- {tier.badge}：{tier.instruction}")

    lines.append("")
    lines.append("重要原則：")
    lines.append("- 遇到 [衍生推論] 標記的內容，回答時必須加上「根據目前線索」「尚待確認」等保留語氣。")
    lines.append("- 不得將 [衍生推論] 的內容當作已確認事實引用。")
    lines.append("- 若所有相關記憶都是 [衍生推論] 且無法查證，應坦承「我目前無法確認這個資訊」。")
    lines.append("- 上述標記僅供內部判斷，回答時不得直接輸出 [已驗證事實]、[使用者陳述]、[檢索線索]、[衍生推論] 等標籤字樣。")

    return "\n".join(lines)
