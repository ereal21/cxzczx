import asyncio

from aiogram import types
from aiogram.dispatcher import DEFAULT_RATE_LIMIT, Dispatcher
from aiogram.dispatcher.handler import CancelHandler, current_handler
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils.exceptions import Throttled

from bot.logger_mesh import logger


class AntiSpamMiddleware(BaseMiddleware):
    """Simple anti-spam middleware that throttles user actions."""

    def __init__(
        self,
        message_rate: float = 1.0,
        callback_rate: float = 0.7,
        warn_after: int = 1,
        cooldown_multiplier: float = 1.5,
    ) -> None:
        super().__init__()
        self.message_rate = message_rate or DEFAULT_RATE_LIMIT
        self.callback_rate = callback_rate or DEFAULT_RATE_LIMIT
        self.warn_after = max(warn_after, 0)
        self.cooldown_multiplier = max(cooldown_multiplier, 1.0)
        self._prefix = "antispam"

    async def on_process_message(self, message: types.Message, data: dict) -> None:
        await self._throttle(target=message, event_type="message")

    async def on_process_callback_query(
        self, callback_query: types.CallbackQuery, data: dict
    ) -> None:
        await self._throttle(target=callback_query, event_type="callback")

    async def _throttle(
        self,
        target: types.Message | types.CallbackQuery,
        event_type: str,
    ) -> None:
        dp = Dispatcher.get_current()
        handler = current_handler.get()

        if handler:
            limit = getattr(
                handler,
                "throttling_rate_limit",
                self.callback_rate if event_type == "callback" else self.message_rate,
            )
            key = getattr(
                handler,
                "throttling_key",
                f"{self._prefix}:{event_type}:{handler.__name__}:{target.from_user.id}",
            )
        else:
            limit = self.callback_rate if event_type == "callback" else self.message_rate
            key = f"{self._prefix}:{event_type}:{target.from_user.id}"

        try:
            await dp.throttle(key, rate=limit)
        except Throttled as throttled:
            await self._handle_throttled(target, throttled, event_type)
            raise CancelHandler()

    async def _handle_throttled(
        self,
        target: types.Message | types.CallbackQuery,
        throttled: Throttled,
        event_type: str,
    ) -> None:
        user_id = target.from_user.id
        wait_time = throttled.rate * self.cooldown_multiplier
        warn_user = throttled.exceeded_count <= self.warn_after + 1

        if isinstance(target, types.CallbackQuery):
            try:
                if warn_user:
                    await target.answer(
                        "⚠️ You're interacting too quickly. Please slow down.",
                        show_alert=False,
                    )
                else:
                    await target.answer(
                        "⏳ Too many requests. Try again in a few seconds.",
                        show_alert=True,
                    )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug("Failed to answer throttled callback for %s: %s", user_id, exc)
        else:
            message: types.Message = target
            try:
                if warn_user:
                    await message.reply(
                        "⚠️ Slow down! Please wait a moment before sending another command.",
                    )
                else:
                    await message.reply(
                        f"⏳ You're sending messages too quickly. Try again in {wait_time:.1f}s.",
                    )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug("Failed to notify throttled user %s: %s", user_id, exc)

        await asyncio.sleep(wait_time)


def setup_antispam(dp: Dispatcher) -> None:
    dp.middleware.setup(AntiSpamMiddleware())
