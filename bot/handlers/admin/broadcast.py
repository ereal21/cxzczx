import asyncio
from aiogram import Dispatcher
from aiogram.types import Message, CallbackQuery
from aiogram.utils.exceptions import BotBlocked, TelegramAPIError

from bot.keyboards import back, close
from bot.database.methods import check_role, get_all_users
from bot.database.models import Permission
from bot.misc import TgConfig
from bot.logger_mesh import logger
from bot.utils.safe_sender import safe_send_copy
from bot.handlers.other import get_bot_user_ids


async def send_message_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'waiting_for_message'
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    role = check_role(user_id)
    if role & Permission.BROADCAST:
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text='Send the message for broadcast:',
            reply_markup=back("console"),
        )
        return
    await call.answer('Insufficient rights')


async def broadcast_messages(message: Message):
    bot, sender_id = await get_bot_user_ids(message)
    user_info = await bot.get_chat(sender_id)
    original_message = message
    message_id = TgConfig.STATE.get(f'{sender_id}_message_id')
    TgConfig.STATE[sender_id] = None
    await bot.delete_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
    )
    users = get_all_users()
    max_users = 0
    for user_row in users:
        max_users += 1
        target_id = int(user_row[0])
        await asyncio.sleep(0.3)
        try:
            await safe_send_copy(
                original_message,
                target_id,
                reply_markup=close(),
            )
        except BotBlocked:
            logger.info("Broadcast skipped for %s: bot blocked", target_id)
        except TelegramAPIError as exc:
            logger.warning("Failed to deliver broadcast to %s: %s", target_id, exc)
    TgConfig.STATE.pop(f'{sender_id}_message_id', None)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=message_id,
        text='Broadcast finished',
        reply_markup=back("console"),
    )
    logger.info(
        f"User {user_info.id} ({user_info.first_name}) performed a broadcast. "
        f"Message was sent to {max_users} users."
    )


def register_mailing(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(
        send_message_callback_handler, lambda c: c.data == 'send_message'
    )

    dp.register_message_handler(
        broadcast_messages,
        lambda c: TgConfig.STATE.get(c.from_user.id) == 'waiting_for_message',
    )
