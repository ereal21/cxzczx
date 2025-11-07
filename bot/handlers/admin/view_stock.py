import os
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape, unescape

from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Message

from bot.database.methods import (
    check_role,
    get_all_category_names,
    get_all_subcategories,
    get_all_item_names,
    get_category_parent,
    get_item_values,
    get_item_value_by_id,
    buy_item,
    select_item_values_amount,
    get_item_info,
    update_item,
)
from bot.database.models import Permission
from bot.handlers.other import get_bot_user_ids
from bot.keyboards import (
    resolve_stock_category,
    resolve_stock_item,
    reset_stock_cache,
    stock_categories_list,
    stock_goods_list,
    stock_item_actions,
    stock_item_return,
    stock_price_prompt,
    stock_values_list,
    stock_value_actions,
    reset_all_stock_caches,
)
from bot.misc import TgConfig
from bot.utils import display_name


TITLE_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 2000


async def view_stock_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if role & Permission.OWN:
        reset_stock_cache(user_id)
        categories = get_all_category_names()
        await bot.edit_message_text(
            '📦 Choose category',
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=stock_categories_list(user_id, categories, None),
        )
        await call.answer()
        return
    await call.answer('Insufficient rights')


def _clear_stock_state(user_id: int) -> None:
    """Remove any temporary state related to inline stock editing."""
    if TgConfig.STATE.get(user_id) in {'stock_price_edit', 'stock_title_edit', 'stock_desc_edit'}:
        TgConfig.STATE[user_id] = None
    TgConfig.STATE.pop(f'{user_id}_stock_item', None)
    TgConfig.STATE.pop(f'{user_id}_stock_message', None)


def _apply_display_title(current_name: str, new_title: str) -> str:
    new_title = new_title.strip()
    if not new_title:
        raise ValueError('Title cannot be empty')
    if len(new_title) > TITLE_MAX_LENGTH:
        raise ValueError(f'Title must be {TITLE_MAX_LENGTH} characters or less')
    sanitized = new_title.replace('__', ' ').strip()
    if '__' in current_name:
        suffix = current_name.split('__', 1)[1]
        return f'{sanitized}__{suffix}'
    return sanitized


def _sanitize_description(text: str) -> str:
    content = text.strip()
    if len(content) > DESCRIPTION_MAX_LENGTH:
        raise ValueError(f'Description must be {DESCRIPTION_MAX_LENGTH} characters or less')
    return escape(content)


async def _render_item_overview(
    bot,
    chat_id: int,
    message_id: int,
    user_id: int,
    item_name: str,
    notice: str | None = None,
) -> None:
    """Render item details with management actions."""
    info = get_item_info(item_name)
    if not info:
        await bot.edit_message_text(
            '❌ Item not found',
            chat_id=chat_id,
            message_id=message_id,
        )
        return
    stock_amount = select_item_values_amount(item_name)
    description = unescape(info['description']) if info.get('description') else ''
    if len(description) > 200:
        description_preview = description[:200].rstrip() + '…'
    else:
        description_preview = description
    lines = []
    if notice:
        lines.append(notice)
        lines.append('')
    lines.extend([
        f'🏷 {display_name(item_name)}',
        f'💶 Price: {Decimal(info["price"]):.2f}€',
        f'📦 Stock entries: {stock_amount}',
    ])
    if description_preview:
        lines.append('')
        lines.append(f'📝 {description_preview}')
    text = '\n'.join(lines)
    markup = stock_item_actions(user_id, item_name, info['category_name'])
    await bot.edit_message_text(
        text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=markup,
    )


async def view_stock_category_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    category_token = call.data.split(':', 1)[1]
    category = resolve_stock_category(user_id, category_token)
    if not category:
        await call.answer('Invalid data')
        return
    subs = get_all_subcategories(category)
    if subs:
        parent = get_category_parent(category)
        await bot.edit_message_text(
            '📂 Choose category',
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=stock_categories_list(user_id, subs, parent),
        )
        return
    items = get_all_item_names(category)
    if items:
        await bot.edit_message_text(
            '🏷 Choose item',
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=stock_goods_list(user_id, items, category),
        )
        return
    await call.answer('No items')


async def view_stock_item_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _clear_stock_state(user_id)
    _, item_token = call.data.split(':', 1)
    item_name = resolve_stock_item(user_id, item_token)
    if not item_name:
        await call.answer('Invalid data')
        return
    await _render_item_overview(
        bot,
        call.message.chat.id,
        call.message.message_id,
        user_id,
        item_name,
    )
    await call.answer()


async def view_stock_item_values_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _clear_stock_state(user_id)
    _, item_token = call.data.split(':', 1)
    item_name = resolve_stock_item(user_id, item_token)
    if not item_name:
        await call.answer('Invalid data')
        return
    values = get_item_values(item_name)
    title = f'📦 Stock for {display_name(item_name)}'
    if not values:
        title = f'📭 No stock for {display_name(item_name)}'
    await bot.edit_message_text(
        title,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=stock_values_list(user_id, values, item_name),
    )
    await call.answer()


async def view_stock_price_prompt_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _clear_stock_state(user_id)
    _, item_token = call.data.split(':', 1)
    item_name = resolve_stock_item(user_id, item_token)
    if not item_name:
        await call.answer('Invalid data')
        return
    info = get_item_info(item_name)
    if not info:
        await call.answer('Item not found')
        return
    TgConfig.STATE[user_id] = 'stock_price_edit'
    TgConfig.STATE[f'{user_id}_stock_item'] = item_name
    TgConfig.STATE[f'{user_id}_stock_message'] = call.message.message_id
    prompt_text = (
        f'💶 Enter new price for {display_name(item_name)}\n'
        f'Current price: {info["price"]:.2f}€\n'
        '\nSend numbers only.'
    )
    await bot.edit_message_text(
        prompt_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=stock_price_prompt(user_id, item_name),
    )
    await call.answer()


async def view_stock_title_prompt_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _clear_stock_state(user_id)
    _, item_token = call.data.split(':', 1)
    item_name = resolve_stock_item(user_id, item_token)
    if not item_name:
        await call.answer('Invalid data')
        return
    info = get_item_info(item_name)
    if not info:
        await call.answer('Item not found')
        return
    TgConfig.STATE[user_id] = 'stock_title_edit'
    TgConfig.STATE[f'{user_id}_stock_item'] = item_name
    TgConfig.STATE[f'{user_id}_stock_message'] = call.message.message_id
    prompt = (
        f"✏️ Enter new title for {display_name(item_name)}\n"
        f"Current title: {display_name(item_name)}\n"
        f"Maximum length: {TITLE_MAX_LENGTH} characters."
    )
    await bot.edit_message_text(
        prompt,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=stock_price_prompt(user_id, item_name),
    )
    await call.answer()


async def stock_title_input_handler(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'stock_title_edit':
        return
    item_name = TgConfig.STATE.get(f'{user_id}_stock_item')
    message_id = TgConfig.STATE.get(f'{user_id}_stock_message')
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if not item_name or message_id is None:
        _clear_stock_state(user_id)
        return
    try:
        new_internal_name = _apply_display_title(item_name, message.text or '')
    except ValueError as exc:
        await bot.edit_message_text(
            f'⚠️ {exc}\n\n✏️ Enter new title for {display_name(item_name)}',
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=stock_price_prompt(user_id, item_name),
        )
        return
    info = get_item_info(item_name)
    if not info:
        _clear_stock_state(user_id)
        await bot.edit_message_text(
            '❌ Item not found',
            chat_id=message.chat.id,
            message_id=message_id,
        )
        return
    update_item(
        item_name,
        new_internal_name,
        info['description'],
        info['price'],
        info['category_name'],
        info.get('delivery_description'),
        changed_by=user_id,
    )
    reset_all_stock_caches()
    _clear_stock_state(user_id)
    confirmation = (
        f'✅ Title updated for {display_name(new_internal_name)}.\n\n'
        'Click below to return to the item overview.'
    )
    await bot.edit_message_text(
        confirmation,
        chat_id=message.chat.id,
        message_id=message_id,
        reply_markup=stock_item_return(user_id, new_internal_name),
    )


async def view_stock_description_prompt_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _clear_stock_state(user_id)
    _, item_token = call.data.split(':', 1)
    item_name = resolve_stock_item(user_id, item_token)
    if not item_name:
        await call.answer('Invalid data')
        return
    info = get_item_info(item_name)
    if not info:
        await call.answer('Item not found')
        return
    TgConfig.STATE[user_id] = 'stock_desc_edit'
    TgConfig.STATE[f'{user_id}_stock_item'] = item_name
    TgConfig.STATE[f'{user_id}_stock_message'] = call.message.message_id
    prompt = (
        f"📝 Enter new description for {display_name(item_name)}\n"
        f"Maximum length: {DESCRIPTION_MAX_LENGTH} characters."
    )
    await bot.edit_message_text(
        prompt,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=stock_price_prompt(user_id, item_name),
    )
    await call.answer()


async def stock_description_input_handler(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'stock_desc_edit':
        return
    item_name = TgConfig.STATE.get(f'{user_id}_stock_item')
    message_id = TgConfig.STATE.get(f'{user_id}_stock_message')
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if not item_name or message_id is None:
        _clear_stock_state(user_id)
        return
    try:
        new_description = _sanitize_description(message.text or '')
    except ValueError as exc:
        await bot.edit_message_text(
            f'⚠️ {exc}\n\n📝 Enter new description for {display_name(item_name)}',
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=stock_price_prompt(user_id, item_name),
        )
        return
    info = get_item_info(item_name)
    if not info:
        _clear_stock_state(user_id)
        await bot.edit_message_text(
            '❌ Item not found',
            chat_id=message.chat.id,
            message_id=message_id,
        )
        return
    update_item(
        item_name,
        item_name,
        new_description,
        info['price'],
        info['category_name'],
        info.get('delivery_description'),
        changed_by=user_id,
    )
    reset_all_stock_caches()
    _clear_stock_state(user_id)
    await _render_item_overview(
        bot,
        message.chat.id,
        message_id,
        user_id,
        item_name,
        notice='✅ Description updated',
    )


async def view_stock_value_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _, raw_value_id = call.data.split(':', 1)
    value_id = int(raw_value_id)
    value = get_item_value_by_id(value_id)
    if not value:
        await call.answer('Not found')
        return
    item_name = value['item_name']
    if value['value'] and os.path.isfile(value['value']):
        desc = ''
        desc_file = f"{value['value']}.txt"
        if os.path.isfile(desc_file):
            with open(desc_file) as f:
                desc = f.read()
        with open(value['value'], 'rb') as doc:
            file_lower = value['value'].lower()
            if file_lower.endswith('.mp4'):
                await bot.send_video(user_id, doc, caption=desc or None)
            elif file_lower.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                await bot.send_photo(user_id, doc, caption=desc or None)
            else:
                await bot.send_document(user_id, doc, caption=desc or None)
    else:
        await bot.send_message(user_id, value['value'])
    await bot.edit_message_text(
        f'ID {value_id}',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=stock_value_actions(user_id, value_id, item_name),
    )
    await call.answer()


async def view_stock_delete_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer('Insufficient rights')
        return
    _, raw_value_id = call.data.split(':', 1)
    value_id = int(raw_value_id)
    value = get_item_value_by_id(value_id)
    if not value:
        await call.answer('Not found')
        return
    item_name = value['item_name']
    if value['value'] and os.path.isfile(value['value']):
        os.remove(value['value'])
    buy_item(value_id)
    values = get_item_values(item_name)
    await bot.edit_message_text(
        '✅ Stock deleted',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=stock_values_list(user_id, values, item_name),
    )
    await call.answer('Deleted')


async def stock_price_input_handler(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'stock_price_edit':
        return
    role = check_role(user_id)
    if not role & Permission.OWN:
        _clear_stock_state(user_id)
        return
    item_name = TgConfig.STATE.get(f'{user_id}_stock_item')
    message_id = TgConfig.STATE.get(f'{user_id}_stock_message')
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if not item_name or message_id is None:
        _clear_stock_state(user_id)
        return
    price_text = message.text.strip()
    price_text = price_text.replace(',', '.')
    try:
        price = Decimal(price_text)
    except InvalidOperation:
        warning = (
            '⚠️ Invalid price value. Use numbers only (e.g. 19.99).\n\n'
            f'💶 Enter new price for {display_name(item_name)}'
        )
        await bot.edit_message_text(
            warning,
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=stock_price_prompt(user_id, item_name),
        )
        return
    if price <= 0:
        await bot.edit_message_text(
            '⚠️ Price must be greater than zero.\n\n'
            f'💶 Enter new price for {display_name(item_name)}',
            chat_id=message.chat.id,
            message_id=message_id,
            reply_markup=stock_price_prompt(user_id, item_name),
        )
        return
    price = price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    info = get_item_info(item_name)
    if not info:
        _clear_stock_state(user_id)
        await bot.edit_message_text(
            '❌ Item not found',
            chat_id=message.chat.id,
            message_id=message_id,
        )
        return
    update_item(
        item_name,
        item_name,
        info['description'],
        price,
        info['category_name'],
        info.get('delivery_description'),
        changed_by=user_id,
    )
    reset_all_stock_caches()
    _clear_stock_state(user_id)
    await _render_item_overview(
        bot,
        message.chat.id,
        message_id,
        user_id,
        item_name,
        notice='✅ Price updated',

    )


def register_view_stock(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(
        view_stock_callback_handler, lambda c: c.data == 'manage_stock'
    )
    dp.register_callback_query_handler(
        view_stock_category_handler,
        lambda c: c.data.startswith('stock_cat:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_item_handler,
        lambda c: c.data.startswith('stock_item:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_item_values_handler,
        lambda c: c.data.startswith('stock_vals:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_value_handler,
        lambda c: c.data.startswith('stock_val:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_delete_handler,
        lambda c: c.data.startswith('stock_del:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_title_prompt_handler,
        lambda c: c.data.startswith('stock_title:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_description_prompt_handler,
        lambda c: c.data.startswith('stock_desc:'),
        state='*',
    )
    dp.register_callback_query_handler(
        view_stock_price_prompt_handler,
        lambda c: c.data.startswith('stock_price:'),
        state='*',
    )
    dp.register_message_handler(
        stock_price_input_handler,
        lambda m: TgConfig.STATE.get(m.from_user.id) == 'stock_price_edit',
        state='*',
    )
    dp.register_message_handler(
        stock_title_input_handler,
        lambda m: TgConfig.STATE.get(m.from_user.id) == 'stock_title_edit',
        state='*',
    )
    dp.register_message_handler(
        stock_description_input_handler,
        lambda m: TgConfig.STATE.get(m.from_user.id) == 'stock_desc_edit',
        state='*',
    )
