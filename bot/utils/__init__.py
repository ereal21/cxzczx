"""Compatibility layer exposing utility helpers under the bot namespace."""
from __future__ import annotations

from utils import generate_internal_name, display_name  # noqa: F401

__all__ = [
    "generate_internal_name",
    "display_name",
]
