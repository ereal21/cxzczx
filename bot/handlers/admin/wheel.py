from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Message

from bot.database.methods import (
    check_role,
    get_user_language,
    create_wheel_prize,
    get_wheel_users,
    ban_wheel_user,
    check_user_by_username,
    add_wheel_spins,
    get_active_wheel_prizes,
)
from bot.database.models import Permission
from bot.handlers.other import get_bot_user_ids
from bot.keyboards import (
    wheel_management_menu,
    wheel_assign_more_keyboard,
    wheel_assign_spins_more_keyboard,
    wheel_remove_more_keyboard,
    back,
)
from bot.localization import t
from bot.misc import TgConfig


def _wheel_state_key(user_id: int, suffix: str) -> str:
    return f"{user_id}_wheel_{suffix}"


def _reset_wheel_state(user_id: int) -> None:
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'name'), None)
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'location'), None)
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'emoji'), None)
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'message_id'), None)
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'lang'), None)
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'remove_message_id'), None)
    TgConfig.STATE.pop(_wheel_state_key(user_id, 'assign_spins_message_id'), None)


async def _open_wheel_menu(call: CallbackQuery, lang: str) -> None:
    prizes = get_active_wheel_prizes()
    if prizes:
        lines = [t(lang, 'wheel_menu_title'), '']
        lines.append(t(lang, 'wheel_menu_prizes_header'))
        for entry in prizes:
            emoji_symbol = entry.emoji or '🎁'
            lines.append(
                t(
                    lang,
                    'wheel_menu_prize_entry',
                    emoji=emoji_symbol,
                    name=entry.name,
                    location=entry.location,
                )
            )
    else:
        lines = [t(lang, 'wheel_menu_title'), '', t(lang, 'wheel_menu_no_prizes')]
    text = '\n'.join(lines)
    await call.message.edit_text(
        text,
        reply_markup=wheel_management_menu(lang),
    )
    TgConfig.STATE[call.from_user.id] = None


async def wheel_menu_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    _reset_wheel_state(user_id)
    TgConfig.STATE[user_id] = None
    await _open_wheel_menu(call, lang)
    await call.answer()


async def wheel_assign_prize_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    _reset_wheel_state(user_id)
    TgConfig.STATE[user_id] = 'wheel_assign_name'
    TgConfig.STATE[_wheel_state_key(user_id, 'message_id')] = call.message.message_id
    TgConfig.STATE[_wheel_state_key(user_id, 'lang')] = lang
    await bot.edit_message_text(
        t(lang, 'wheel_assign_name_prompt'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


async def _handle_assign_name(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'wheel_assign_name':
        return
    lang = TgConfig.STATE.get(_wheel_state_key(user_id, 'lang')) or get_user_language(user_id) or 'en'
    name = (message.text or '').strip()
    if not name or len(name) > 120:
        await message.reply(t(lang, 'wheel_assign_name_invalid'))
        return
    TgConfig.STATE[_wheel_state_key(user_id, 'name')] = name
    TgConfig.STATE[user_id] = 'wheel_assign_location'
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    message_id = TgConfig.STATE.get(_wheel_state_key(user_id, 'message_id'))
    if message_id:
        await bot.edit_message_text(
            t(lang, 'wheel_assign_location_prompt'),
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=back('wheel_menu'),
        )


async def _handle_assign_location(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'wheel_assign_location':
        return
    lang = TgConfig.STATE.get(_wheel_state_key(user_id, 'lang')) or get_user_language(user_id) or 'en'
    location = (message.text or '').strip()
    if not location or len(location) > 120:
        await message.reply(t(lang, 'wheel_assign_location_invalid'))
        return
    TgConfig.STATE[_wheel_state_key(user_id, 'location')] = location
    TgConfig.STATE[user_id] = 'wheel_assign_emoji'
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    message_id = TgConfig.STATE.get(_wheel_state_key(user_id, 'message_id'))
    if message_id:
        await bot.edit_message_text(
            t(lang, 'wheel_assign_emoji_prompt'),
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=back('wheel_menu'),
        )


async def _handle_assign_emoji(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'wheel_assign_emoji':
        return
    lang = TgConfig.STATE.get(_wheel_state_key(user_id, 'lang')) or get_user_language(user_id) or 'en'
    emoji = (message.text or '').strip()
    if not emoji or len(emoji) > 16:
        await message.reply(t(lang, 'wheel_assign_emoji_invalid'))
        return
    TgConfig.STATE[_wheel_state_key(user_id, 'emoji')] = emoji
    TgConfig.STATE[user_id] = 'wheel_assign_photo'
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    message_id = TgConfig.STATE.get(_wheel_state_key(user_id, 'message_id'))
    if message_id:
        await bot.edit_message_text(
            t(lang, 'wheel_assign_photo_prompt'),
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=back('wheel_menu'),
        )


async def _handle_assign_photo(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'wheel_assign_photo' or not message.photo:
        return
    lang = TgConfig.STATE.get(_wheel_state_key(user_id, 'lang')) or get_user_language(user_id) or 'en'
    name = TgConfig.STATE.get(_wheel_state_key(user_id, 'name'))
    location = TgConfig.STATE.get(_wheel_state_key(user_id, 'location'))
    emoji = TgConfig.STATE.get(_wheel_state_key(user_id, 'emoji'))
    message_id = TgConfig.STATE.get(_wheel_state_key(user_id, 'message_id'))
    if not all([name, location, emoji, message_id]):
        await bot.send_message(message.chat.id, t(lang, 'wheel_assign_restart'))
        TgConfig.STATE[user_id] = None
        _reset_wheel_state(user_id)
        return
    file_id = message.photo[-1].file_id
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    create_wheel_prize(name, location, emoji, file_id)
    TgConfig.STATE[user_id] = None
    _reset_wheel_state(user_id)
    await bot.edit_message_text(
        t(lang, 'wheel_assign_success', name=name, location=location, emoji=emoji),
        chat_id=message.chat.id,
        message_id=message_id,
        reply_markup=wheel_assign_more_keyboard(lang),
    )


async def _handle_assign_photo_invalid(message: Message):
    if TgConfig.STATE.get(message.from_user.id) != 'wheel_assign_photo':
        return
    lang = TgConfig.STATE.get(_wheel_state_key(message.from_user.id, 'lang')) or get_user_language(message.from_user.id) or 'en'
    await message.reply(t(lang, 'wheel_assign_photo_invalid'))


async def wheel_assign_more_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    TgConfig.STATE[user_id] = 'wheel_assign_name'
    TgConfig.STATE[_wheel_state_key(user_id, 'message_id')] = call.message.message_id
    TgConfig.STATE[_wheel_state_key(user_id, 'lang')] = lang
    await bot.edit_message_text(
        t(lang, 'wheel_assign_name_prompt'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


async def wheel_assign_spins_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    _reset_wheel_state(user_id)
    TgConfig.STATE[user_id] = 'wheel_assign_spins'
    TgConfig.STATE[_wheel_state_key(user_id, 'assign_spins_message_id')] = call.message.message_id
    TgConfig.STATE[_wheel_state_key(user_id, 'lang')] = lang
    await bot.edit_message_text(
        t(lang, 'wheel_assign_spins_prompt'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


async def _handle_assign_spins(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'wheel_assign_spins':
        return
    lang = TgConfig.STATE.get(_wheel_state_key(user_id, 'lang')) or get_user_language(user_id) or 'en'
    raw_text = (message.text or '').strip()
    if not raw_text:
        await message.reply(t(lang, 'wheel_assign_spins_invalid_format'))
        return
    parts = raw_text.split()
    username = parts[0].lstrip('@')
    if not username:
        await message.reply(t(lang, 'wheel_assign_spins_invalid_format'))
        return
    amount = 1
    if len(parts) > 1:
        try:
            amount = int(parts[1])
        except ValueError:
            await message.reply(t(lang, 'wheel_assign_spins_invalid_amount'))
            return
    if amount <= 0:
        await message.reply(t(lang, 'wheel_assign_spins_invalid_amount'))
        return
    user = check_user_by_username(username)
    if not user:
        await message.reply(t(lang, 'wheel_assign_spins_user_not_found', username=username))
        return
    if not add_wheel_spins(user.telegram_id, amount):
        await message.reply(t(lang, 'wheel_assign_spins_failed'))
        return
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    message_id = TgConfig.STATE.get(_wheel_state_key(user_id, 'assign_spins_message_id'))
    TgConfig.STATE[user_id] = None
    _reset_wheel_state(user_id)
    if message_id:
        await bot.edit_message_text(
            t(
                lang,
                'wheel_assign_spins_success',
                username=user.username or username,
                amount=amount,
            ),
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=wheel_assign_spins_more_keyboard(lang),
        )


async def wheel_assign_spins_more_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    _reset_wheel_state(user_id)
    TgConfig.STATE[user_id] = 'wheel_assign_spins'
    TgConfig.STATE[_wheel_state_key(user_id, 'assign_spins_message_id')] = call.message.message_id
    TgConfig.STATE[_wheel_state_key(user_id, 'lang')] = lang
    await bot.edit_message_text(
        t(lang, 'wheel_assign_spins_prompt'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


async def wheel_see_users_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    users = [entry for entry in get_wheel_users() if entry.spins > 0 and not entry.is_banned]
    if not users:
        text = t(lang, 'wheel_users_empty')
    else:
        lines = [t(lang, 'wheel_users_header')]
        for entry in users:
            lines.append(t(lang, 'wheel_users_entry', user_id=entry.user_id, spins=entry.spins))
        text = '\n'.join(lines)
    await bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


async def wheel_remove_users_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    TgConfig.STATE[user_id] = 'wheel_remove_user'
    TgConfig.STATE[_wheel_state_key(user_id, 'remove_message_id')] = call.message.message_id
    TgConfig.STATE[_wheel_state_key(user_id, 'lang')] = lang
    await bot.edit_message_text(
        t(lang, 'wheel_remove_prompt'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


async def _handle_remove_user(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'wheel_remove_user':
        return
    lang = TgConfig.STATE.get(_wheel_state_key(user_id, 'lang')) or get_user_language(user_id) or 'en'
    try:
        target_id = int((message.text or '').strip())
    except (TypeError, ValueError):
        await message.reply(t(lang, 'wheel_remove_invalid'))
        return
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    ban_wheel_user(target_id)
    TgConfig.STATE[user_id] = None
    message_id = TgConfig.STATE.get(_wheel_state_key(user_id, 'remove_message_id'))
    _reset_wheel_state(user_id)
    if message_id:
        await bot.edit_message_text(
            t(lang, 'wheel_remove_success', user_id=target_id),
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=wheel_remove_more_keyboard(lang),
        )


async def wheel_remove_more_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    TgConfig.STATE[user_id] = 'wheel_remove_user'
    TgConfig.STATE[_wheel_state_key(user_id, 'remove_message_id')] = call.message.message_id
    TgConfig.STATE[_wheel_state_key(user_id, 'lang')] = lang
    await bot.edit_message_text(
        t(lang, 'wheel_remove_prompt'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('wheel_menu'),
    )
    await call.answer()


def register_wheel_management(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(wheel_menu_handler, lambda c: c.data == 'wheel_menu', state='*')
    dp.register_callback_query_handler(wheel_assign_prize_handler, lambda c: c.data == 'wheel_assign_prizes', state='*')
    dp.register_callback_query_handler(wheel_assign_spins_handler, lambda c: c.data == 'wheel_assign_spins', state='*')
    dp.register_callback_query_handler(wheel_assign_more_handler, lambda c: c.data == 'wheel_assign_more', state='*')
    dp.register_callback_query_handler(wheel_assign_spins_more_handler, lambda c: c.data == 'wheel_assign_spins_more', state='*')
    dp.register_callback_query_handler(wheel_see_users_handler, lambda c: c.data == 'wheel_see_users', state='*')
    dp.register_callback_query_handler(wheel_remove_users_handler, lambda c: c.data == 'wheel_remove_users', state='*')
    dp.register_callback_query_handler(wheel_remove_more_handler, lambda c: c.data == 'wheel_remove_more', state='*')

    dp.register_message_handler(_handle_assign_name, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_assign_name', state='*')
    dp.register_message_handler(_handle_assign_location, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_assign_location', state='*')
    dp.register_message_handler(_handle_assign_emoji, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_assign_emoji', state='*')
    dp.register_message_handler(_handle_assign_photo, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_assign_photo', content_types=['photo'], state='*')
    dp.register_message_handler(_handle_assign_photo_invalid, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_assign_photo', state='*')
    dp.register_message_handler(_handle_assign_spins, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_assign_spins', state='*')
    dp.register_message_handler(_handle_remove_user, lambda m: TgConfig.STATE.get(m.from_user.id) == 'wheel_remove_user', state='*')
