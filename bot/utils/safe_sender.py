"""Resilient helpers for safely delivering messages to Telegram chats.

This module mirrors the legacy :mod:`utils.safe_sender` helpers but provides
local fallbacks so the bot can operate without the optional dependency
installed.
"""
from __future__ import annotations

from typing import Any

from aiogram.types import Message
from aiogram.utils.exceptions import TelegramAPIError

try:  # pragma: no cover - prefer legacy helpers when available
    from utils.safe_sender import safe_send_copy as _legacy_safe_send_copy  # type: ignore
    from utils.safe_sender import safe_send_message as _legacy_safe_send_message  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    _legacy_safe_send_copy = None
    _legacy_safe_send_message = None

__all__ = ["safe_send_copy", "safe_send_message"]


async def _send_message_with_fallback(
    message: Message,
    text: str,
    chat_id: int | str,
    **kwargs: Any,
) -> Message:
    """Send ``text`` to ``chat_id`` attempting to preserve formatting.

    If Telegram rejects the message due to formatting issues, the helpers retry
    with relaxed options (for example without ``parse_mode``) before giving up.
    """

    bot = message.bot
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError:
        if "parse_mode" in kwargs:
            relaxed_kwargs = dict(kwargs)
            relaxed_kwargs.pop("parse_mode", None)
            return await bot.send_message(chat_id, text, **relaxed_kwargs)
        raise


async def _local_safe_send_copy(
    message: Message,
    chat_id: int | str,
    **kwargs: Any,
) -> Message:
    """Copy ``message`` to ``chat_id`` with graceful degradation."""

    try:
        return await message.copy_to(chat_id=chat_id, **kwargs)
    except TelegramAPIError:
        if message.text:
            return await _send_message_with_fallback(message, message.text, chat_id, **kwargs)
        raise


async def _local_safe_send_message(message: Message, text: str, **kwargs: Any) -> Message:
    """Send ``text`` to the chat originating ``message`` safely."""

    return await _send_message_with_fallback(message, text, message.chat.id, **kwargs)


async def safe_send_copy(message: Message, chat_id: int | str, **kwargs: Any) -> Message:
    """Forward the message contents to ``chat_id`` safely.

    The legacy helper is preferred when available so behaviour remains familiar
    for deployments that still ship the external ``utils`` package.
    """

    if _legacy_safe_send_copy is not None:
        return await _legacy_safe_send_copy(message, chat_id, **kwargs)
    return await _local_safe_send_copy(message, chat_id, **kwargs)


async def safe_send_message(message: Message, text: str, **kwargs: Any) -> Message:
    """Send ``text`` to the originating chat of ``message`` safely."""

    if _legacy_safe_send_message is not None:
        return await _legacy_safe_send_message(message, text, **kwargs)
    return await _local_safe_send_message(message, text, **kwargs)
