"""Offline-only V3 validation campaign runner.

Keep package import side-effect free so ``python -m scripts.v3_campaign.runner``
does not import the target module before ``runpy`` executes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runner import CampaignContext, CampaignRunner, CampaignSafetyError

__all__ = ["CampaignContext", "CampaignRunner", "CampaignSafetyError"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from . import runner

    return getattr(runner, name)
