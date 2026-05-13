"""Compatibility package for Iron Dome.

Existing code imports `skills.iron_dome.*`, while the canonical skill folder
is `skills/iron-dome` (kebab-case). Python cannot import from hyphenated
module names, so this package bridges both paths.

DO NOT place real implementation here. All logic lives in `skills/iron-dome/`.
"""
