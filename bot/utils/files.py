"""File helper utilities with graceful fallbacks.

This module mirrors the public surface that historical deployments expect from
``utils.files``.  The original project bundled a top-level ``utils`` package
outside of the bot package, but that dependency is not always available in
modern environments.  To keep the bot working out of the box we provide local
implementations and transparently defer to the external helpers when they are
installed.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
import re

__all__ = [
    "sanitize_name",
    "get_next_file_path",
    "cleanup_item_file",
]


# The legacy dependency exposed the helpers from ``utils.files``.  Import it
# lazily so that we can fall back to our local implementations when it is not
# present or when the expected attributes are missing.
try:  # pragma: no cover - optional dependency
    from utils.files import (  # type: ignore
        sanitize_name as _legacy_sanitize_name,
        get_next_file_path as _legacy_get_next_file_path,
        cleanup_item_file as _legacy_cleanup_item_file,
    )
except (ImportError, AttributeError):  # pragma: no cover - legacy path absent
    _legacy_sanitize_name = None
    _legacy_get_next_file_path = None
    _legacy_cleanup_item_file = None


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOADS_ROOT = Path("assets") / "uploads"


def sanitize_name(name: str | None) -> str:
    """Return a filesystem-friendly slug for *name*.

    The legacy helper replaced unsafe characters with underscores.  We follow a
    similar strategy and keep the behaviour deterministic so that generated
    folders match older installations.  If the optional dependency is
    available, defer to it first.
    """

    if _legacy_sanitize_name is not None:  # pragma: no branch - trivial guard
        try:
            return _legacy_sanitize_name(name)
        except Exception:  # pragma: no cover - keep local fallback resilient
            pass

    if not name:
        return "item"

    sanitized = _SAFE_NAME_RE.sub("_", name.strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("._")
    return sanitized or "item"


def get_next_file_path(item_name: str | None, extension: str | None = None) -> str:
    """Generate a unique path inside the uploads directory for *item_name*.

    Files are placed under ``assets/uploads/<sanitized item>/`` with a numeric
    suffix so consecutive uploads do not collide.  An optional *extension* can
    be provided with or without the leading dot.
    """

    if _legacy_get_next_file_path is not None:  # pragma: no branch
        try:
            return _legacy_get_next_file_path(item_name, extension)
        except Exception:  # pragma: no cover - ensure compatibility
            pass

    safe_name = sanitize_name(item_name)
    uploads_dir = _UPLOADS_ROOT / safe_name
    uploads_dir.mkdir(parents=True, exist_ok=True)

    suffix = ""
    if extension:
        suffix = extension if extension.startswith(".") else f".{extension}"

    for idx in count(1):
        candidate = uploads_dir / f"{safe_name}_{idx:03d}{suffix}"
        if not candidate.exists():
            return str(candidate)

    raise RuntimeError("Unable to allocate a file path for item uploads")


def cleanup_item_file(path: str | None) -> None:
    """Remove empty upload directories left behind after moving *path*.

    The bot frequently moves stock files into a ``Sold`` subdirectory, leaving
    the original upload folder empty.  Clearing the empty directories keeps the
    uploads tree tidy.  When the legacy helper is available we delegate to it to
    preserve existing behaviour.
    """

    if _legacy_cleanup_item_file is not None:  # pragma: no branch
        try:
            _legacy_cleanup_item_file(path)
            return
        except Exception:  # pragma: no cover - fall back to local logic
            pass

    if not path:
        return

    current = Path(path).resolve(strict=False).parent
    if current == current.parent:
        return

    uploads_root = _UPLOADS_ROOT.resolve(strict=False)

    try:
        while True:
            resolved_current = current.resolve(strict=False)

            if resolved_current == uploads_root:
                if not current.exists() or any(current.iterdir()):
                    break
                current.rmdir()
                break

            if uploads_root not in getattr(resolved_current, "parents", ()):
                break

            if not current.exists():
                current = current.parent
                if current == current.parent:
                    break
                continue

            if any(current.iterdir()):
                break

            parent = current.parent
            current.rmdir()
            current = parent
            if current == current.parent:
                break
    except Exception:  # pragma: no cover - best-effort cleanup
        return

