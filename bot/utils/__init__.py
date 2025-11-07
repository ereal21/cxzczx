"""Compatibility layer exposing utility helpers under the bot namespace."""
from __future__ import annotations

try:
    from .names import generate_internal_name, display_name  # noqa: F401
except ImportError:  # pragma: no cover - fallback for legacy deployments
    from utils import generate_internal_name, display_name  # type: ignore  # noqa: F401

__all__ = [
    "generate_internal_name",
    "display_name",
]
