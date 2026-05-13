from __future__ import annotations

from collections import Counter
from typing import Dict, List

from .entity_extractor import extract_entities


def build_relations(text: str) -> List[Dict[str, object]]:
    entities = extract_entities(text)
    if len(entities) < 2:
        return []

    weights = Counter()
    for idx, source in enumerate(entities):
        for target in entities[idx + 1 :]:
            pair = (source["value"], target["value"])
            weights[pair] += 1

    relations: List[Dict[str, object]] = []
    for (source_value, target_value), weight in weights.items():
        relations.append(
            {
                "source": source_value,
                "target": target_value,
                "relation": "co_occurs",
                "weight": float(weight),
            }
        )
    return relations
