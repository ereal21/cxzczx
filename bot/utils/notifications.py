"""Notification helpers with graceful fallbacks.

Historically the project depended on an external ``utils.notifications`` module
that lived outside of the bot package.  Deployments that still ship that
package should keep their existing behaviour, while modern setups ought to work
without installing the legacy dependency.  This module mirrors the public
surface that callers rely on and delegates to the old helpers when possible,
providing local implementations otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from aiogram import Bot
from aiogram.types import FSInputFile

from bot.logger_mesh import logger
from bot.misc import EnvKeys
from bot.utils import display_name

try:  # pragma: no cover - optional dependency
    from utils.notifications import (  # type: ignore
        notify_owner_of_purchase as _legacy_notify_owner_of_purchase,
        notify_owner_of_prize_win as _legacy_notify_owner_of_prize_win,
        notify_owner_of_topup as _legacy_notify_owner_of_topup,
    )
except (ImportError, AttributeError):  # pragma: no cover - legacy package absent
    _legacy_notify_owner_of_purchase = None
    _legacy_notify_owner_of_prize_win = None
    _legacy_notify_owner_of_topup = None

__all__ = [
    "notify_owner_of_purchase",
    "notify_owner_of_prize_win",
    "notify_owner_of_topup",
]


@dataclass(slots=True)
class _OwnerConfig:
    owner_id: int | None
    warning_logged: bool = False


_OWNER_CONFIG = _OwnerConfig(owner_id=None, warning_logged=False)


def _resolve_owner_id() -> int | None:
    """Return the configured owner identifier if available."""

    if _OWNER_CONFIG.owner_id is not None:
        return _OWNER_CONFIG.owner_id

    owner_id_raw = EnvKeys.OWNER_ID
    if not owner_id_raw:
        if not _OWNER_CONFIG.warning_logged:
            logger.warning("OWNER_ID is not set; owner notifications are disabled")
            _OWNER_CONFIG.warning_logged = True
        return None

    try:
        owner_id = int(owner_id_raw)
    except (TypeError, ValueError):
        if not _OWNER_CONFIG.warning_logged:
            logger.error("OWNER_ID=%r is invalid; notifications will be skipped", owner_id_raw)
            _OWNER_CONFIG.warning_logged = True
        return None

    _OWNER_CONFIG.owner_id = owner_id
    return owner_id


def _format_amount(value: object) -> str:
    """Return a human friendly representation of *value* treated as currency."""

    if isinstance(value, (int, float)):
        return f"{value:.2f}"

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)

    quantized = amount.quantize(Decimal("0.01"))
    return f"{quantized:.2f}"


def _join_non_empty(parts: Iterable[str]) -> str:
    return "\n".join(part for part in parts if part)


async def _send_owner_message(
    bot: Bot,
    text: str,
    *,
    disable_web_page_preview: bool = True,
) -> None:
    owner_id = _resolve_owner_id()
    if owner_id is None:
        return

    try:
        await bot.send_message(
            owner_id,
            text,
            disable_web_page_preview=disable_web_page_preview,
        )
    except Exception as exc:  # pragma: no cover - best effort logging
        logger.error("Failed to deliver owner notification: %s", exc)


async def _send_owner_file(bot: Bot, file_path: Path, caption: str | None = None) -> None:
    owner_id = _resolve_owner_id()
    if owner_id is None:
        return

    try:
        await bot.send_document(
            owner_id,
            FSInputFile(str(file_path)),
            caption=caption or None,
        )
    except Exception as exc:  # pragma: no cover - optional attachment
        logger.error("Failed to deliver owner attachment %s: %s", file_path, exc)


async def _local_notify_owner_of_purchase(
    bot: Bot,
    username: str | None,
    formatted_time: str,
    item_name: str,
    price: object,
    parent_category: str | None,
    category_name: str | None,
    photo_description: str | None,
    file_path: str | None,
) -> None:
    buyer = username or "unknown"
    price_display = _format_amount(price)
    item_display = display_name(item_name)
    category_bits = []
    if parent_category:
        category_bits.append(parent_category)
    if category_name and category_name not in category_bits:
        category_bits.append(category_name)

    header_lines = [
        "🛍️ New purchase",
        f"👤 Buyer: {buyer}",
        f"🕒 Time: {formatted_time}",
        f"📦 Item: {item_display}",
        f"💶 Price: {price_display}€",
    ]
    if category_bits:
        header_lines.insert(4, f"📁 Category: {' → '.join(category_bits)}")

    if photo_description:
        header_lines.extend(["", photo_description.strip()])

    await _send_owner_message(bot, _join_non_empty(header_lines))

    if not file_path:
        return

    path = Path(file_path)
    if not path.is_file():
        logger.debug("Owner attachment missing or not a file: %s", file_path)
        return

    caption_parts = [f"{item_display} — {price_display}€"]
    if photo_description:
        caption_parts.append(photo_description.strip())
    caption = _join_non_empty(caption_parts)
    await _send_owner_file(bot, path, caption=caption[:1024] if caption else None)


async def _local_notify_owner_of_prize_win(
    bot: Bot,
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    prize_name: str,
    prize_location: str | None,
    prize_emoji: str | None,
    photo_file_id: str | None,
    formatted_time: str,
) -> None:
    owner_id = _resolve_owner_id()
    if owner_id is None:
        return

    user_repr = username if username else (full_name or str(user_id))
    prize_bits = [prize_emoji or "🎁", prize_name]
    prize_line = " ".join(bit for bit in prize_bits if bit)

    lines = [
        "🎡 Wheel prize claimed",
        f"👤 User: {user_repr} (ID: {user_id})",
        f"🕒 Time: {formatted_time}",
        f"🎁 Prize: {prize_line}",
    ]
    if prize_location:
        lines.append(f"📍 Location: {prize_location}")

    message = _join_non_empty(lines)

    if photo_file_id:
        try:
            await bot.send_photo(owner_id, photo_file_id, caption=message)
            return
        except Exception as exc:  # pragma: no cover - fall back to text
            logger.error("Failed to send prize photo notification: %s", exc)

    await _send_owner_message(bot, message)


async def _local_notify_owner_of_topup(
    bot: Bot,
    username: str | None,
    amount: object,
    formatted_time: str,
) -> None:
    price_display = _format_amount(amount)
    lines = [
        "💳 Balance replenished",
        f"👤 User: {username or 'unknown'}",
        f"💶 Amount: {price_display}€",
        f"🕒 Time: {formatted_time}",
    ]
    await _send_owner_message(bot, _join_non_empty(lines))


async def notify_owner_of_purchase(
    bot: Bot,
    username: str | None,
    formatted_time: str,
    item_name: str,
    price: object,
    parent_category: str | None,
    category_name: str | None,
    photo_description: str | None,
    file_path: str | None,
) -> None:
    if _legacy_notify_owner_of_purchase is not None:
        await _legacy_notify_owner_of_purchase(
            bot,
            username,
            formatted_time,
            item_name,
            price,
            parent_category,
            category_name,
            photo_description,
            file_path,
        )
        return

    await _local_notify_owner_of_purchase(
        bot,
        username,
        formatted_time,
        item_name,
        price,
        parent_category,
        category_name,
        photo_description,
        file_path,
    )


async def notify_owner_of_prize_win(
    bot: Bot,
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    prize_name: str,
    prize_location: str | None,
    prize_emoji: str | None,
    photo_file_id: str | None,
    formatted_time: str,
) -> None:
    if _legacy_notify_owner_of_prize_win is not None:
        await _legacy_notify_owner_of_prize_win(
            bot,
            user_id=user_id,
            username=username,
            full_name=full_name,
            prize_name=prize_name,
            prize_location=prize_location,
            prize_emoji=prize_emoji,
            photo_file_id=photo_file_id,
            formatted_time=formatted_time,
        )
        return

    await _local_notify_owner_of_prize_win(
        bot,
        user_id=user_id,
        username=username,
        full_name=full_name,
        prize_name=prize_name,
        prize_location=prize_location,
        prize_emoji=prize_emoji,
        photo_file_id=photo_file_id,
        formatted_time=formatted_time,
    )


async def notify_owner_of_topup(
    bot: Bot,
    username: str | None,
    amount: object,
    formatted_time: str,
) -> None:
    if _legacy_notify_owner_of_topup is not None:
        await _legacy_notify_owner_of_topup(bot, username, amount, formatted_time)
        return

    await _local_notify_owner_of_topup(bot, username, amount, formatted_time)
