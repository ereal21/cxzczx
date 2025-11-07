import datetime
import os
import re
import shutil
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from aiogram import Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.exceptions import ChatNotFound


from bot.database.methods import (
    add_values_to_item,
    check_category,
    check_item,
    check_role,
    check_value,
    create_category,
    create_item,
    delete_category,
    delete_item,
    delete_only_items,
    get_all_categories,
    get_all_category_names,
    get_all_item_names,
    get_all_items,
    get_all_subcategories,
    get_category_parent,
    get_item_info,
    get_user_count,
    select_admins,
    select_all_operations,
    select_all_orders,
    select_bought_item,
    select_count_bought_items,
    select_count_categories,
    select_count_goods,
    select_count_items,
    select_today_operations,
    select_today_orders,
    select_today_users,
    select_users_balance,
    update_category,
    update_item,
    create_promocode,
    delete_promocode,
    get_promocode,
    get_all_promocodes,
    get_promocode_usage_by_geo,
    update_promocode,
)
from bot.utils import generate_internal_name, display_name


from bot.utils.files import get_next_file_path
from bot.database.models import Permission
from bot.handlers.other import get_bot_user_ids
from bot.keyboards import (shop_management, goods_management, categories_management, back, item_management,
                           question_buttons, promo_codes_management, promo_expiry_keyboard, promo_codes_list,
                           promo_manage_actions, reset_all_stock_caches)
from bot.logger_mesh import logger
from bot.misc import TgConfig, EnvKeys


def format_geo_targets(targets: list[dict | tuple]) -> str:
    if not targets:
        return 'All locations'
    grouped: dict[str, set[str | None]] = {}
    for entry in targets:
        if isinstance(entry, dict):
            city = entry.get('city') or ''
            district = entry.get('district')
        else:
            city, district = entry
        grouped.setdefault(city, set()).add(district)
    lines = []
    for city in sorted(grouped):
        districts = grouped[city]
        if None in districts:
            lines.append(city)
        else:
            lines.append(f"{city}: {', '.join(sorted(districts))}")
    return '\n'.join(lines)


def format_product_filters(filters: list[dict | tuple]) -> str:
    if not filters:
        return 'All products'
    allowed = [f for f in filters if (f.get('is_allowed') if isinstance(f, dict) else True)]
    excluded = [f for f in filters if isinstance(f, dict) and not f.get('is_allowed')]

    def _format(entries):
        chunks = []
        for entry in entries:
            if isinstance(entry, dict):
                prefix = 'Category' if entry['type'] == 'category' else 'Item'
                chunks.append(f"{prefix}: {entry['name']}")
            else:
                prefix = 'Category' if entry[0] == 'category' else 'Item'
                chunks.append(f"{prefix}: {entry[1]}")
        return '\n'.join(chunks) if chunks else '—'

    allowed_text = _format(allowed)
    excluded_text = _format(excluded)
    return f"Allowed:\n{allowed_text}\n\nExcluded:\n{excluded_text}"


def _gather_promo_creation_data(user_id: int):
    code = TgConfig.STATE.get(f'{user_id}_promo_code')
    discount = TgConfig.STATE.get(f'{user_id}_promo_discount')
    expiry = TgConfig.STATE.get(f'{user_id}_promo_expiry')
    geo = TgConfig.STATE.get(f'{user_id}_promo_geo', [])
    allowed = TgConfig.STATE.get(f'{user_id}_promo_allowed', [])
    excluded = TgConfig.STATE.get(f'{user_id}_promo_excluded', [])
    return code, discount, expiry, geo, allowed, excluded


def _clear_promo_creation_state(user_id: int) -> None:
    keys = [
        f'{user_id}_promo_code',
        f'{user_id}_promo_discount',
        f'{user_id}_promo_expiry',
        f'{user_id}_promo_geo',
        f'{user_id}_promo_allowed',
        f'{user_id}_promo_excluded',
        f'{user_id}_promo_expiry_unit',
        f'{user_id}_message_id',
    ]
    for key in keys:
        TgConfig.STATE.pop(key, None)
    _clear_promo_selection_state(user_id)
    TgConfig.STATE[user_id] = None


async def _complete_promo_creation(bot, user_id: int, chat_id: int, message_id: int) -> None:
    code, discount, expiry, geo, allowed, excluded = _gather_promo_creation_data(user_id)
    create_promocode(
        code,
        discount,
        expiry,
        geo_targets=geo,
        allowed_filters=allowed,
        excluded_filters=excluded,
    )
    _clear_promo_creation_state(user_id)
    await bot.edit_message_text('✅ Promo code created',
                                chat_id=chat_id,
                                message_id=message_id,
                                reply_markup=back('promo_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) created promo code {code}")


def _clear_promo_selection_state(user_id: int) -> None:
    suffixes = [
        'promo_geo_cities',
        'promo_geo_districts',
        'promo_product_allowed_categories',
        'promo_product_allowed_items',
        'promo_product_excluded_categories',
        'promo_product_excluded_items',
        'promo_product_mode',
        'promo_geo_city_choices',
        'promo_geo_district_category_choices',
        'promo_geo_district_choices',
        'promo_geo_district_current',
        'promo_product_category_choices',
        'promo_product_subcategory_choices',
        'promo_product_subcategory_parent',
        'promo_product_item_choices',
        'promo_product_current_category',
        'promo_context',
    ]
    for suffix in suffixes:
        TgConfig.STATE.pop(f'{user_id}_{suffix}', None)


def _get_state_set(user_id: int, suffix: str) -> set:
    key = f'{user_id}_{suffix}'
    value = TgConfig.STATE.get(key)
    if isinstance(value, set):
        return value
    if value is None:
        value = set()
    else:
        value = set(value)
    TgConfig.STATE[key] = value
    return value


def _selected_cities(user_id: int) -> set[str]:
    return _get_state_set(user_id, 'promo_geo_cities')


def _selected_districts(user_id: int) -> set[tuple[str, str]]:
    return _get_state_set(user_id, 'promo_geo_districts')


def _allowed_categories(user_id: int) -> set[str]:
    return _get_state_set(user_id, 'promo_product_allowed_categories')


def _allowed_items(user_id: int) -> set[str]:
    return _get_state_set(user_id, 'promo_product_allowed_items')


def _excluded_categories(user_id: int) -> set[str]:
    return _get_state_set(user_id, 'promo_product_excluded_categories')


def _excluded_items(user_id: int) -> set[str]:
    return _get_state_set(user_id, 'promo_product_excluded_items')


def _get_product_mode(user_id: int) -> str:
    key = f'{user_id}_promo_product_mode'
    mode = TgConfig.STATE.get(key)
    if mode not in {'allowed', 'excluded'}:
        mode = 'allowed'
    TgConfig.STATE[key] = mode
    return mode


def _set_product_mode(user_id: int, mode: str) -> None:
    TgConfig.STATE[f'{user_id}_promo_product_mode'] = mode if mode in {'allowed', 'excluded'} else 'allowed'


def _descendant_categories(category: str) -> set[str]:
    descendants: set[str] = set()
    stack = [category]
    while stack:
        current = stack.pop()
        for child in get_all_subcategories(current):
            if child in descendants:
                continue
            descendants.add(child)
            stack.append(child)
    return descendants


def _collect_category_items(category: str) -> set[str]:
    related_categories = {category}
    related_categories.update(_descendant_categories(category))
    items: set[str] = set()
    for name in related_categories:
        items.update(get_all_item_names(name))
    return items


def _load_promo_into_selection(user_id: int, promo: dict | None) -> None:
    _selected_cities(user_id).clear()
    _selected_districts(user_id).clear()
    _allowed_categories(user_id).clear()
    _allowed_items(user_id).clear()
    _excluded_categories(user_id).clear()
    _excluded_items(user_id).clear()
    if not promo:
        return
    for entry in promo.get('geo_targets', []):
        city = (entry.get('city') or '').strip()
        district = (entry.get('district') or '').strip() or None
        if not city:
            continue
        if district:
            _selected_districts(user_id).add((city, district))
        else:
            _selected_cities(user_id).add(city)
    for entry in promo.get('product_filters', []):
        target_type = entry.get('type')
        name = entry.get('name')
        is_allowed = entry.get('is_allowed', True)
        if not name or target_type not in {'category', 'item'}:
            continue
        if is_allowed:
            if target_type == 'category':
                _allowed_categories(user_id).add(name)
            else:
                _allowed_items(user_id).add(name)
        else:
            if target_type == 'category':
                _excluded_categories(user_id).add(name)
            else:
                _excluded_items(user_id).add(name)


def _collect_geo_targets(user_id: int) -> list[tuple[str, str | None]]:
    cities = sorted(_selected_cities(user_id))
    districts = sorted(_selected_districts(user_id))
    result: list[tuple[str, str | None]] = [(city, None) for city in cities]
    for city, district in districts:
        if city in _selected_cities(user_id):
            continue
        result.append((city, district))
    return result


def _collect_product_filters(user_id: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    allowed: list[tuple[str, str]] = []
    excluded: list[tuple[str, str]] = []
    for name in sorted(_allowed_categories(user_id)):
        allowed.append(('category', name))
    for name in sorted(_allowed_items(user_id)):
        allowed.append(('item', name))
    for name in sorted(_excluded_categories(user_id)):
        excluded.append(('category', name))
    for name in sorted(_excluded_items(user_id)):
        excluded.append(('item', name))
    return allowed, excluded


def _promo_summary_text(user_id: int) -> str:
    geo_entries = [
        {'city': city, 'district': district}
        for city, district in _collect_geo_targets(user_id)
    ]
    geo_summary = format_geo_targets(geo_entries)
    allowed, excluded = _collect_product_filters(user_id)
    filter_entries: list[dict] = []
    filter_entries.extend(
        {'type': target_type, 'name': name, 'is_allowed': True}
        for target_type, name in allowed
    )
    filter_entries.extend(
        {'type': target_type, 'name': name, 'is_allowed': False}
        for target_type, name in excluded
    )
    products_summary = format_product_filters(filter_entries)
    return (
        "📍 Click where you want to assign this code to only.\n\n"
        "🏙️ Miestai ir rajonai:\n"
        f"{geo_summary}\n\n"
        "🛍️ Produktai:\n"
        f"{products_summary}"
    )


def _promo_main_keyboard(back_target: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton('🏙️ Miestai', callback_data='promo_target_cities'))
    markup.add(InlineKeyboardButton('🏘️ Rajonai', callback_data='promo_target_districts'))
    markup.add(InlineKeyboardButton('🛍️ Produktai', callback_data='promo_target_products'))
    markup.add(InlineKeyboardButton('✅ Save', callback_data='promo_target_save'))
    markup.add(InlineKeyboardButton('🔙 Cancel', callback_data=back_target))
    return markup


def _promo_context(user_id: int) -> dict:
    ctx = TgConfig.STATE.get(f'{user_id}_promo_context')
    return ctx if isinstance(ctx, dict) else {}


def _promo_target_state(user_id: int) -> str | None:
    state = TgConfig.STATE.get(user_id)
    return state if state in {'promo_create_targets', 'promo_manage_targets'} else None


async def _show_promo_target_main(bot, user_id: int, chat_id: int, message_id: int) -> None:
    context = _promo_context(user_id)
    back_target = context.get('back', 'promo_management')
    await bot.edit_message_text(
        _promo_summary_text(user_id),
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=_promo_main_keyboard(back_target),
        disable_web_page_preview=True,
    )


async def _open_promo_target_menu(
    bot,
    user_id: int,
    chat_id: int,
    message_id: int,
    back_target: str,
    mode: str,
    promo_code: str | None = None,
) -> None:
    _clear_promo_selection_state(user_id)
    TgConfig.STATE[f'{user_id}_promo_context'] = {'mode': mode, 'back': back_target, 'code': promo_code}
    _set_product_mode(user_id, 'allowed')
    if mode == 'manage' and promo_code:
        promo = get_promocode(promo_code)
        _load_promo_into_selection(user_id, promo)
    else:
        _load_promo_into_selection(user_id, None)
    state_value = 'promo_create_targets' if mode == 'create' else 'promo_manage_targets'
    TgConfig.STATE[user_id] = state_value
    await _show_promo_target_main(bot, user_id, chat_id, message_id)


async def promo_code_receive_geo(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    state = TgConfig.STATE.get(user_id)
    relevant_states = {
        'promo_create_geo',
        'promo_manage_geo',
        'promo_create_targets',
        'promo_manage_targets',
    }
    if state not in relevant_states:
        return

    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if message_id is None:
        return

    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)

    context = _promo_context(user_id)
    mode = context.get('mode')
    if not mode:
        mode = 'manage' if state and state.startswith('promo_manage') else 'create'

    promo_code = context.get('code')
    if not promo_code and mode == 'manage':
        promo_code = TgConfig.STATE.get(f'{user_id}_promo_manage_code')

    back_target = context.get('back')
    if not back_target:
        if mode == 'manage' and promo_code:
            back_target = f'manage_promo_code_{promo_code}'
        else:
            back_target = 'promo_management'

    if state in {'promo_create_targets', 'promo_manage_targets'}:
        await _show_promo_target_main(bot, user_id, message.chat.id, message_id)
    else:
        await _open_promo_target_menu(
            bot,
            user_id,
            message.chat.id,
            message_id,
            back_target=back_target,
            mode=mode,
            promo_code=promo_code,
        )


async def promo_manage_receive_geo(message: Message):
    await promo_code_receive_geo(message)


def _city_selection_text() -> str:
    return (
        '🏙️ Miestai\n\n'
        'Pasirinkite kategorijas, kurios atitinka miestus. ✅ reiškia, kad kodas bus taikomas '
        'pasirinktiems miestams.'
    )


def _build_city_keyboard(user_id: int) -> InlineKeyboardMarkup:
    categories = sorted(get_all_category_names())
    mapping: dict[str, str] = {}
    markup = InlineKeyboardMarkup(row_width=1)
    selected = _selected_cities(user_id)
    if not categories:
        markup.add(InlineKeyboardButton('— Nėra kategorijų —', callback_data='promo_target_main'))
    else:
        for idx, name in enumerate(categories, start=1):
            choice_id = str(idx)
            mapping[choice_id] = name
            label = f"{'✅' if name in selected else '☐'} {name}"
            markup.add(InlineKeyboardButton(label, callback_data=f'promo_target_city_toggle_{choice_id}'))
        markup.add(InlineKeyboardButton('🧹 Išvalyti pasirinkimą', callback_data='promo_target_city_clear'))
    markup.add(InlineKeyboardButton('✅ Done', callback_data='promo_target_main'))
    TgConfig.STATE[f'{user_id}_promo_geo_city_choices'] = mapping
    return markup


def _district_category_text() -> str:
    return (
        '🏘️ Rajonai\n\n'
        'Pasirinkite miestą, kurio rajonus norite apriboti.'
    )


def _build_district_category_keyboard(user_id: int) -> InlineKeyboardMarkup:
    categories = sorted(get_all_category_names())
    mapping: dict[str, str] = {}
    markup = InlineKeyboardMarkup(row_width=1)
    available = False
    for idx, name in enumerate(categories, start=1):
        subcategories = get_all_subcategories(name)
        if not subcategories:
            continue
        available = True
        choice_id = str(idx)
        mapping[choice_id] = name
        markup.add(InlineKeyboardButton(name, callback_data=f'promo_target_district_open_{choice_id}'))
    if not available:
        markup.add(InlineKeyboardButton('— Nėra rajonų —', callback_data='promo_target_main'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='promo_target_main'))
    TgConfig.STATE[f'{user_id}_promo_geo_district_category_choices'] = mapping
    return markup


def _district_selection_text(category: str) -> str:
    return (
        f'🏘️ Rajonai – {category}\n\n'
        'Pasirinkite rajonus, kuriems galios kodas. ✅ reiškia, kad rajonas įtrauktas.'
    )


def _build_district_keyboard(user_id: int, category: str) -> InlineKeyboardMarkup:
    districts = sorted(get_all_subcategories(category))
    mapping: dict[str, str] = {}
    markup = InlineKeyboardMarkup(row_width=1)
    selected = _selected_districts(user_id)
    if not districts:
        markup.add(InlineKeyboardButton('— Nėra rajonų —', callback_data='promo_target_districts'))
    else:
        for idx, name in enumerate(districts, start=1):
            choice_id = str(idx)
            mapping[choice_id] = name
            is_selected = (category, name) in selected
            label = f"{'✅' if is_selected else '☐'} {name}"
            markup.add(InlineKeyboardButton(label, callback_data=f'promo_target_district_toggle_{choice_id}'))
        markup.add(InlineKeyboardButton('🧹 Išvalyti', callback_data='promo_target_district_clear'))
    markup.add(InlineKeyboardButton('✅ Done', callback_data='promo_target_districts'))
    TgConfig.STATE[f'{user_id}_promo_geo_district_choices'] = mapping
    TgConfig.STATE[f'{user_id}_promo_geo_district_current'] = category
    return markup


def _product_categories_text(mode: str) -> str:
    label = 'leidžiamus' if mode == 'allowed' else 'draudžiamus'
    return (
        f'🛍️ Produktai ({"leidžiama" if mode == "allowed" else "neleidžiama"})\n\n'
        f'Pasirinkite kategorijas, kurioms {label} taikyti kodą.'
    )


def _build_product_categories_keyboard(user_id: int, mode: str) -> InlineKeyboardMarkup:
    categories = sorted(get_all_category_names())
    mapping: dict[str, str] = {}
    selected = _allowed_categories(user_id) if mode == 'allowed' else _excluded_categories(user_id)
    markup = InlineKeyboardMarkup(row_width=1)
    if not categories:
        markup.add(InlineKeyboardButton('— Nėra kategorijų —', callback_data='promo_target_main'))
    else:
        for idx, name in enumerate(categories, start=1):
            choice_id = str(idx)
            mapping[choice_id] = name
            label = f"{'✅' if name in selected else '☐'} {name}"
            markup.add(InlineKeyboardButton(label, callback_data=f'promo_target_product_toggle_cat_{choice_id}'))
            subcategories = get_all_subcategories(name)
            if subcategories:
                markup.add(InlineKeyboardButton(
                    f'📂 {name} subkategorijos',
                    callback_data=f'promo_target_product_open_sub_{choice_id}'
                ))
            items = get_all_item_names(name)
            if items:
                markup.add(InlineKeyboardButton(f'📦 {name} produktai', callback_data=f'promo_target_product_open_{choice_id}'))
        markup.add(InlineKeyboardButton('🧹 Išvalyti', callback_data='promo_target_product_clear'))
    switch_label = '🔁 Pereiti prie draudžiamų' if mode == 'allowed' else '🔁 Pereiti prie leidžiamų'
    markup.add(InlineKeyboardButton(switch_label, callback_data='promo_target_product_switch'))
    markup.add(InlineKeyboardButton('✅ Done', callback_data='promo_target_main'))
    TgConfig.STATE[f'{user_id}_promo_product_category_choices'] = mapping
    return markup


def _product_subcategories_text(category: str, mode: str) -> str:
    label = 'leidžiamas' if mode == 'allowed' else 'draudžiamas'
    mode_text = 'leidžiama' if mode == 'allowed' else 'neleidžiama'
    return (
        f'🛍️ {category} subkategorijos ({mode_text})\n\n'
        f'Pasirinkite {label} subkategorijas šiai kategorijai. ✅ reiškia, kad subkategorija įtraukta.'
    )


def _build_product_subcategories_keyboard(user_id: int, category: str, mode: str) -> InlineKeyboardMarkup:
    subcategories = sorted(get_all_subcategories(category))
    mapping: dict[str, str] = {}
    selected = _allowed_categories(user_id) if mode == 'allowed' else _excluded_categories(user_id)
    markup = InlineKeyboardMarkup(row_width=1)
    if not subcategories:
        markup.add(InlineKeyboardButton('— Nėra subkategorijų —', callback_data='promo_target_products'))
    else:
        for idx, name in enumerate(subcategories, start=1):
            choice_id = str(idx)
            mapping[choice_id] = name
            label = f"{'✅' if name in selected else '☐'} {name}"
            markup.add(InlineKeyboardButton(label, callback_data=f'promo_target_product_toggle_sub_{choice_id}'))
            items = get_all_item_names(name)
            if items:
                markup.add(InlineKeyboardButton(
                    f'📦 {name} produktai',
                    callback_data=f'promo_target_product_open_subitem_{choice_id}'
                ))
        markup.add(InlineKeyboardButton('🧹 Išvalyti subkategorijas', callback_data='promo_target_product_clear_sub'))
    markup.add(InlineKeyboardButton('✅ Done', callback_data='promo_target_products'))
    TgConfig.STATE[f'{user_id}_promo_product_subcategory_choices'] = mapping
    TgConfig.STATE[f'{user_id}_promo_product_subcategory_parent'] = category
    return markup


def _product_items_text(category: str, mode: str) -> str:
    prefix = 'leidžiamus' if mode == 'allowed' else 'draudžiamus'
    return (
        f'🛍️ Produktai – {category}\n\n'
        f'Pasirinkite {prefix} produktus šiai kategorijai. ✅ reiškia, kad produktas įtrauktas.'
    )


def _build_product_items_keyboard(user_id: int, category: str, mode: str) -> InlineKeyboardMarkup:
    items = sorted(get_all_item_names(category))
    mapping: dict[str, str] = {}
    selected = _allowed_items(user_id) if mode == 'allowed' else _excluded_items(user_id)
    markup = InlineKeyboardMarkup(row_width=1)
    if not items:
        markup.add(InlineKeyboardButton('— Nėra produktų —', callback_data='promo_target_products'))
    else:
        for idx, name in enumerate(items, start=1):
            choice_id = str(idx)
            mapping[choice_id] = name
            label = f"{'✅' if name in selected else '☐'} {display_name(name)}"
            markup.add(InlineKeyboardButton(label, callback_data=f'promo_target_product_toggle_item_{choice_id}'))
        markup.add(InlineKeyboardButton('🧹 Išvalyti produktus', callback_data='promo_target_product_clear_items'))
    markup.add(InlineKeyboardButton('✅ Done', callback_data='promo_target_products'))
    TgConfig.STATE[f'{user_id}_promo_product_item_choices'] = mapping
    TgConfig.STATE[f'{user_id}_promo_product_current_category'] = category
    return markup


async def shop_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('⛩️ Shop management menu',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=shop_management())
        return
    await call.answer('Insufficient rights')


async def logs_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    file_path = 'bot.log'
    if role & Permission.SHOP_MANAGE:
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            with open(file_path, 'rb') as document:
                await bot.send_document(chat_id=call.message.chat.id,
                                        document=document)
                return
        else:
            await call.answer(text="❗️ Kolkas nėra logų")
            return
    await call.answer('Insufficient rights')


async def goods_management_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🛒 Prekių valdymo meniu',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=goods_management())
        return
    await call.answer('Insufficient rights')


async def promo_management_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    _clear_promo_selection_state(user_id)
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🏷 Promo codes menu',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=promo_codes_management())
        return
    await call.answer('Insufficient rights')


async def create_promo_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'promo_create_code'
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    await bot.edit_message_text('Enter promo code:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('promo_management'))


async def promo_code_receive_code(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'promo_create_code':
        return
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if message_id is None:
        return
    code = (message.text or '').strip()
    if not code or len(code) > 50:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        await bot.edit_message_text('⚠️ Invalid code. Use up to 50 characters.',
                                    chat_id=message.chat.id,
                                    message_id=message_id,
                                    reply_markup=back('promo_management'))
        return
    code = code.upper()
    TgConfig.STATE[f'{user_id}_promo_code'] = code
    TgConfig.STATE[user_id] = 'promo_create_discount'
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    await bot.edit_message_text('Enter discount percent:',
                                chat_id=message.chat.id,
                                message_id=message_id,
                                reply_markup=back('promo_management'))


async def promo_code_receive_discount(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'promo_create_discount':
        return
    text = (message.text or '').strip()
    try:
        discount = int(text)
    except ValueError:
        message_id = TgConfig.STATE.get(f'{user_id}_message_id')
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        await bot.edit_message_text('⚠️ Enter a numeric discount between 1 and 100.',
                                    chat_id=message.chat.id,
                                    message_id=message_id,
                                    reply_markup=back('promo_management'))
        return
    if discount <= 0 or discount > 100:
        message_id = TgConfig.STATE.get(f'{user_id}_message_id')
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        await bot.edit_message_text('⚠️ Discount must be between 1 and 100.',
                                    chat_id=message.chat.id,
                                    message_id=message_id,
                                    reply_markup=back('promo_management'))
        return
    TgConfig.STATE[f'{user_id}_promo_discount'] = discount
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if message_id is None:
        return
    TgConfig.STATE[user_id] = 'promo_create_expiry_type'
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    await bot.edit_message_text('Choose expiry type:',
                                chat_id=message.chat.id,
                                message_id=message_id,
                                reply_markup=promo_expiry_keyboard('promo_management'))


async def promo_create_expiry_type_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if TgConfig.STATE.get(user_id) != 'promo_create_expiry_type':
        return
    unit = call.data[len('promo_expiry_'):]
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if message_id is None:
        return
    if unit == 'none':
        TgConfig.STATE[f'{user_id}_promo_expiry'] = None
        await _open_promo_target_menu(
            bot,
            user_id,
            call.message.chat.id,
            message_id,
            back_target='promo_management',
            mode='create',
        )
        return
    TgConfig.STATE[f'{user_id}_promo_expiry_unit'] = unit
    TgConfig.STATE[user_id] = 'promo_create_expiry_number'
    await bot.edit_message_text(f'Enter number of {unit}:',
                                chat_id=call.message.chat.id,
                                message_id=message_id,
                                reply_markup=back('promo_management'))


async def promo_code_receive_expiry_number(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'promo_create_expiry_number':
        return
    text = (message.text or '').strip()
    try:
        number = int(text)
    except ValueError:
        message_id = TgConfig.STATE.get(f'{user_id}_message_id')
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        await bot.edit_message_text('⚠️ Enter a whole number for expiry.',
                                    chat_id=message.chat.id,
                                    message_id=message_id,
                                    reply_markup=back('promo_management'))
        return
    unit = TgConfig.STATE.get(f'{user_id}_promo_expiry_unit')
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if message_id is None:
        return
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if number <= 0:
        expiry = None
    else:
        days = {'days': number, 'weeks': number * 7, 'months': number * 30}[unit]
        expiry_date = datetime.date.today() + datetime.timedelta(days=days)
        expiry = expiry_date.strftime('%Y-%m-%d')
    TgConfig.STATE[f'{user_id}_promo_expiry'] = expiry
    TgConfig.STATE.pop(f'{user_id}_promo_expiry_unit', None)
    await _open_promo_target_menu(
        bot,
        user_id,
        message.chat.id,
        message_id,
        back_target='promo_management',
        mode='create',
    )


async def delete_promo_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    codes = [p.code for p in get_all_promocodes()]
    if codes:
        await bot.edit_message_text('Select promo code to delete:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=promo_codes_list(codes, 'delete_promo_code', 'promo_management'))
    else:
        await call.answer('No promo codes available', show_alert=True)


async def promo_code_delete_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('delete_promo_code_'):]
    delete_promocode(code)
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) deleted promo code {code}")
    codes = [p.code for p in get_all_promocodes()]
    if codes:
        await bot.edit_message_text('Select promo code to delete:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=promo_codes_list(codes, 'delete_promo_code', 'promo_management'))
    else:
        await bot.edit_message_text('✅ Promo code deleted',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back('promo_management'))


async def manage_promo_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    codes = [p.code for p in get_all_promocodes()]
    if codes:
        await bot.edit_message_text('Select promo code:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=promo_codes_list(codes, 'manage_promo_code', 'promo_management'))
    else:
        await call.answer('No promo codes available', show_alert=True)


async def promo_manage_select_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('manage_promo_code_'):]
    _clear_promo_selection_state(user_id)
    await bot.edit_message_text(f'Promo code: {code}',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=promo_manage_actions(code))


async def promo_manage_discount_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('promo_manage_discount_'):]
    TgConfig.STATE[user_id] = 'promo_manage_discount'
    TgConfig.STATE[f'{user_id}_promo_manage_code'] = code
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    await bot.edit_message_text('Enter new discount percent:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back(f'manage_promo_code_{code}'))


async def promo_manage_receive_discount(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'promo_manage_discount':
        return
    code = TgConfig.STATE.get(f'{user_id}_promo_manage_code')
    text = (message.text or '').strip()
    try:
        new_discount = int(text)
    except ValueError:
        message_id = TgConfig.STATE.get(f'{user_id}_message_id')
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        await bot.edit_message_text('⚠️ Enter a numeric discount between 1 and 100.',
                                    chat_id=message.chat.id,
                                    message_id=message_id,
                                    reply_markup=promo_manage_actions(code))
        return
    if new_discount <= 0 or new_discount > 100:
        message_id = TgConfig.STATE.get(f'{user_id}_message_id')
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        await bot.edit_message_text('⚠️ Discount must be between 1 and 100.',
                                    chat_id=message.chat.id,
                                    message_id=message_id,
                                    reply_markup=promo_manage_actions(code))
        return
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    update_promocode(code, discount=new_discount)
    TgConfig.STATE[user_id] = None
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) updated promo code {code} discount to {new_discount}")
    await bot.edit_message_text('✅ Discount updated',
                                chat_id=message.chat.id,
                                message_id=message_id,
                                reply_markup=promo_manage_actions(code))
    TgConfig.STATE.pop(f'{user_id}_promo_manage_code', None)
    TgConfig.STATE.pop(f'{user_id}_message_id', None)


async def promo_manage_expiry_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('promo_manage_expiry_'):]
    TgConfig.STATE[user_id] = 'promo_manage_expiry_type'
    TgConfig.STATE[f'{user_id}_promo_manage_code'] = code
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    await bot.edit_message_text('Choose expiry type:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=promo_expiry_keyboard(f'manage_promo_code_{code}'))


async def promo_manage_geo_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('promo_manage_geo_'):]
    await _open_promo_target_menu(
        bot,
        user_id,
        call.message.chat.id,
        call.message.message_id,
        back_target=f'manage_promo_code_{code}',
        mode='manage',
        promo_code=code,
    )
    await call.answer()


async def promo_target_main_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    await _show_promo_target_main(bot, user_id, call.message.chat.id, call.message.message_id)
    await call.answer()


async def promo_target_cities_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    await bot.edit_message_text(
        _city_selection_text(),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_city_keyboard(user_id),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_city_toggle_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    choice_id = call.data[len('promo_target_city_toggle_'):]
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_geo_city_choices') or {}
    city = mapping.get(choice_id)
    if not city:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    cities = _selected_cities(user_id)
    if city in cities:
        cities.remove(city)
    else:
        cities.add(city)
        districts = _selected_districts(user_id)
        districts.difference_update({entry for entry in districts if entry[0] == city})
    await call.message.edit_reply_markup(reply_markup=_build_city_keyboard(user_id))
    await call.answer()


async def promo_target_city_clear_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    _selected_cities(user_id).clear()
    await call.message.edit_reply_markup(reply_markup=_build_city_keyboard(user_id))
    await call.answer('Išvalyta')


async def promo_target_districts_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    await bot.edit_message_text(
        _district_category_text(),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_district_category_keyboard(user_id),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_district_open_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    choice_id = call.data[len('promo_target_district_open_'):]
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_geo_district_category_choices') or {}
    category = mapping.get(choice_id)
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    await bot.edit_message_text(
        _district_selection_text(category),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_district_keyboard(user_id, category),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_district_toggle_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_geo_district_choices') or {}
    category = TgConfig.STATE.get(f'{user_id}_promo_geo_district_current')
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    choice_id = call.data[len('promo_target_district_toggle_'):]
    district = mapping.get(choice_id)
    if not district:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    entries = _selected_districts(user_id)
    key = (category, district)
    if key in entries:
        entries.remove(key)
    else:
        entries.add(key)
        _selected_cities(user_id).discard(category)
    await call.message.edit_reply_markup(reply_markup=_build_district_keyboard(user_id, category))
    await call.answer()


async def promo_target_district_clear_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    category = TgConfig.STATE.get(f'{user_id}_promo_geo_district_current')
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    entries = _selected_districts(user_id)
    entries.difference_update({entry for entry in entries if entry[0] == category})
    await call.message.edit_reply_markup(reply_markup=_build_district_keyboard(user_id, category))
    await call.answer('Išvalyta')


async def promo_target_products_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mode = _get_product_mode(user_id)
    await bot.edit_message_text(
        _product_categories_text(mode),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_product_categories_keyboard(user_id, mode),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_product_switch_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    current = _get_product_mode(user_id)
    new_mode = 'excluded' if current == 'allowed' else 'allowed'
    _set_product_mode(user_id, new_mode)
    await bot.edit_message_text(
        _product_categories_text(new_mode),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_product_categories_keyboard(user_id, new_mode),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_product_toggle_cat_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_product_category_choices') or {}
    choice_id = call.data[len('promo_target_product_toggle_cat_'):]
    category = mapping.get(choice_id)
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    if mode == 'allowed':
        selected = _allowed_categories(user_id)
        opposite_items = _excluded_items(user_id)
        opposite_categories = _excluded_categories(user_id)
    else:
        selected = _excluded_categories(user_id)
        opposite_items = _allowed_items(user_id)
        opposite_categories = _allowed_categories(user_id)
    if category in selected:
        selected.remove(category)
    else:
        selected.add(category)
        opposite_categories.discard(category)
    category_items = _collect_category_items(category)
    item_set = _allowed_items(user_id) if mode == 'allowed' else _excluded_items(user_id)
    if category not in selected:
        item_set.difference_update(category_items)
    else:
        opposite_items.difference_update(category_items)
    await call.message.edit_reply_markup(reply_markup=_build_product_categories_keyboard(user_id, mode))
    await call.answer()


async def promo_target_product_open_subcategories_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_product_category_choices') or {}
    choice_id = call.data[len('promo_target_product_open_sub_'):]
    category = mapping.get(choice_id)
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    await bot.edit_message_text(
        _product_subcategories_text(category, mode),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_product_subcategories_keyboard(user_id, category, mode),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_product_open_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_product_category_choices') or {}
    choice_id = call.data[len('promo_target_product_open_'):]
    category = mapping.get(choice_id)
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    await bot.edit_message_text(
        _product_items_text(category, mode),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_product_items_keyboard(user_id, category, mode),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_product_toggle_sub_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_product_subcategory_choices') or {}
    choice_id = call.data[len('promo_target_product_toggle_sub_'):]
    subcategory = mapping.get(choice_id)
    parent = TgConfig.STATE.get(f'{user_id}_promo_product_subcategory_parent')
    if not subcategory or not parent:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    if mode == 'allowed':
        target_set = _allowed_categories(user_id)
        opposite_set = _excluded_categories(user_id)
        target_items = _allowed_items(user_id)
        opposite_items = _excluded_items(user_id)
    else:
        target_set = _excluded_categories(user_id)
        opposite_set = _allowed_categories(user_id)
        target_items = _excluded_items(user_id)
        opposite_items = _allowed_items(user_id)
    if subcategory in target_set:
        target_set.remove(subcategory)
    else:
        target_set.add(subcategory)
        opposite_set.discard(subcategory)
    related_items = _collect_category_items(subcategory)
    if subcategory not in target_set:
        target_items.difference_update(related_items)
    else:
        opposite_items.difference_update(related_items)
    await call.message.edit_reply_markup(
        reply_markup=_build_product_subcategories_keyboard(user_id, parent, mode)
    )
    await call.answer()


async def promo_target_product_open_subitem_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_product_subcategory_choices') or {}
    choice_id = call.data[len('promo_target_product_open_subitem_'):]
    subcategory = mapping.get(choice_id)
    if not subcategory:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    await bot.edit_message_text(
        _product_items_text(subcategory, mode),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=_build_product_items_keyboard(user_id, subcategory, mode),
        disable_web_page_preview=True,
    )
    await call.answer()


async def promo_target_product_toggle_item_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mapping: dict[str, str] = TgConfig.STATE.get(f'{user_id}_promo_product_item_choices') or {}
    choice_id = call.data[len('promo_target_product_toggle_item_'):]
    item_name = mapping.get(choice_id)
    category = TgConfig.STATE.get(f'{user_id}_promo_product_current_category')
    if not item_name or not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    if mode == 'allowed':
        target_set = _allowed_items(user_id)
        opposite_set = _excluded_items(user_id)
    else:
        target_set = _excluded_items(user_id)
        opposite_set = _allowed_items(user_id)
    if item_name in target_set:
        target_set.remove(item_name)
    else:
        target_set.add(item_name)
        opposite_set.discard(item_name)
    await call.message.edit_reply_markup(reply_markup=_build_product_items_keyboard(user_id, category, mode))
    await call.answer()


async def promo_target_product_clear_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    mode = _get_product_mode(user_id)
    if mode == 'allowed':
        _allowed_categories(user_id).clear()
        _allowed_items(user_id).clear()
    else:
        _excluded_categories(user_id).clear()
        _excluded_items(user_id).clear()
    await call.message.edit_reply_markup(reply_markup=_build_product_categories_keyboard(user_id, mode))
    await call.answer('Išvalyta')


async def promo_target_product_clear_subcategories_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    parent = TgConfig.STATE.get(f'{user_id}_promo_product_subcategory_parent')
    if not parent:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    target_set = _allowed_categories(user_id) if mode == 'allowed' else _excluded_categories(user_id)
    target_items = _allowed_items(user_id) if mode == 'allowed' else _excluded_items(user_id)
    for subcategory in get_all_subcategories(parent):
        if subcategory in target_set:
            related_items = _collect_category_items(subcategory)
            target_items.difference_update(related_items)
        target_set.discard(subcategory)
    await call.message.edit_reply_markup(
        reply_markup=_build_product_subcategories_keyboard(user_id, parent, mode)
    )
    await call.answer('Išvalyta')


async def promo_target_product_clear_items_handler(call: CallbackQuery):
    _, user_id = await get_bot_user_ids(call)
    if not _promo_target_state(user_id):
        await call.answer()
        return
    category = TgConfig.STATE.get(f'{user_id}_promo_product_current_category')
    if not category:
        await call.answer('Pasirinkimas nebegalioja', show_alert=True)
        return
    mode = _get_product_mode(user_id)
    item_set = _allowed_items(user_id) if mode == 'allowed' else _excluded_items(user_id)
    item_set.difference_update(get_all_item_names(category))
    await call.message.edit_reply_markup(reply_markup=_build_product_items_keyboard(user_id, category, mode))
    await call.answer('Išvalyta')


async def promo_target_save_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    state = _promo_target_state(user_id)
    if not state:
        await call.answer()
        return
    geo_targets = _collect_geo_targets(user_id)
    allowed, excluded = _collect_product_filters(user_id)
    if state == 'promo_create_targets':
        TgConfig.STATE[f'{user_id}_promo_geo'] = geo_targets
        TgConfig.STATE[f'{user_id}_promo_allowed'] = allowed
        TgConfig.STATE[f'{user_id}_promo_excluded'] = excluded
        await _complete_promo_creation(bot, user_id, call.message.chat.id, call.message.message_id)
        await call.answer('Išsaugota')
        return
    context = _promo_context(user_id)
    code = context.get('code')
    if not code:
        await call.answer('Nepavyko nustatyti kodo', show_alert=True)
        return
    geo_summary = format_geo_targets([
        {'city': city, 'district': district}
        for city, district in geo_targets
    ])
    filters_payload = []
    filters_payload.extend(
        {'type': t, 'name': n, 'is_allowed': True}
        for t, n in allowed
    )
    filters_payload.extend(
        {'type': t, 'name': n, 'is_allowed': False}
        for t, n in excluded
    )
    product_summary = format_product_filters(filters_payload)
    update_promocode(code, geo_targets=geo_targets, allowed_filters=allowed, excluded_filters=excluded)
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) updated promo {code} restrictions")
    _clear_promo_selection_state(user_id)
    TgConfig.STATE[user_id] = None
    await bot.edit_message_text(
        '✅ Promo settings updated\n\n'
        f'🌍 Vietos:\n{geo_summary}\n\n'
        f'🛍️ Produktai:\n{product_summary}',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=promo_manage_actions(code),
        disable_web_page_preview=True,
    )
    await call.answer('Išsaugota')


async def promo_manage_stats_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('promo_manage_stats_'):]
    rows = get_promocode_usage_by_geo(code)
    if not rows:
        text = 'No usage yet.'
    else:
        lines = []
        for city, district, count in rows:
            city_display = city or 'Unknown city'
            district_display = district or 'All districts'
            lines.append(f'{city_display} – {district_display}: {count}')
        text = '📈 Usage by geography:\n' + '\n'.join(lines)
    await bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=promo_manage_actions(code),
    )
    await call.answer()


async def promo_manage_expiry_type_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if TgConfig.STATE.get(user_id) != 'promo_manage_expiry_type':
        return
    unit = call.data[len('promo_expiry_'):]
    code = TgConfig.STATE.get(f'{user_id}_promo_manage_code')
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if unit == 'none':
        update_promocode(code, expires_at=None)
        TgConfig.STATE[user_id] = None
        admin_info = await bot.get_chat(user_id)
        logger.info(f"User {user_id} ({admin_info.first_name}) updated promo code {code} expiry")
        await bot.edit_message_text('✅ Expiry updated',
                                    chat_id=call.message.chat.id,
                                    message_id=message_id,
                                    reply_markup=promo_manage_actions(code))
        return
    TgConfig.STATE[f'{user_id}_promo_expiry_unit'] = unit
    TgConfig.STATE[user_id] = 'promo_manage_expiry_number'
    await bot.edit_message_text(f'Enter number of {unit}:',
                                chat_id=call.message.chat.id,
                                message_id=message_id,
                                reply_markup=back(f'manage_promo_code_{code}'))


async def promo_manage_receive_expiry_number(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if TgConfig.STATE.get(user_id) != 'promo_manage_expiry_number':
        return
    number = int(message.text.strip())
    unit = TgConfig.STATE.get(f'{user_id}_promo_expiry_unit')
    code = TgConfig.STATE.get(f'{user_id}_promo_manage_code')
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if number <= 0:
        expiry = None
    else:
        days = {'days': number, 'weeks': number * 7, 'months': number * 30}[unit]
        expiry_date = datetime.date.today() + datetime.timedelta(days=days)
        expiry = expiry_date.strftime('%Y-%m-%d')
    update_promocode(code, expires_at=expiry)
    TgConfig.STATE[user_id] = None
    TgConfig.STATE.pop(f'{user_id}_promo_expiry_unit', None)
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) updated promo code {code} expiry")
    await bot.edit_message_text('✅ Expiry updated',
                                chat_id=message.chat.id,
                                message_id=message_id,
                                reply_markup=promo_manage_actions(code))


async def promo_manage_delete_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    code = call.data[len('promo_manage_delete_'):]
    delete_promocode(code)
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) deleted promo code {code}")
    codes = [p.code for p in get_all_promocodes()]
    if codes:
        await bot.edit_message_text('Select promo code:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=promo_codes_list(codes, 'manage_promo_code', 'promo_management'))
    else:
        await bot.edit_message_text('✅ Promo code deleted',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back('promo_management'))


async def assign_photos_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE or role & Permission.ASSIGN_PHOTOS):
        await call.answer('Insufficient rights')
        return
    TgConfig.STATE[user_id] = None
    categories = get_all_category_names()
    markup = InlineKeyboardMarkup()
    for cat in categories:
        markup.add(InlineKeyboardButton(cat, callback_data=f'assign_photo_cat_{cat}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='goods_management'))
    await bot.edit_message_text('Choose category:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def assign_photo_category_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE or role & Permission.ASSIGN_PHOTOS):
        await call.answer('Insufficient rights')
        return
    category = call.data[len('assign_photo_cat_'):]
    subcats = get_all_subcategories(category)
    markup = InlineKeyboardMarkup()
    for sub in subcats:
        markup.add(InlineKeyboardButton(sub, callback_data=f'assign_photo_sub_{sub}'))
    items = get_all_item_names(category)
    for item in items:
        markup.add(InlineKeyboardButton(display_name(item), callback_data=f'assign_photo_item_{item}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='assign_photos'))
    await bot.edit_message_text('Choose subcategory or item:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def assign_photo_subcategory_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE or role & Permission.ASSIGN_PHOTOS):
        await call.answer('Insufficient rights')
        return
    sub = call.data[len('assign_photo_sub_'):]
    items = get_all_item_names(sub)
    markup = InlineKeyboardMarkup()
    for item in items:
        markup.add(InlineKeyboardButton(display_name(item), callback_data=f'assign_photo_item_{item}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='assign_photos'))
    await bot.edit_message_text('Choose item:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def assign_photo_item_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE or role & Permission.ASSIGN_PHOTOS):
        await call.answer('Insufficient rights')
        return
    item = call.data[len('assign_photo_item_'):]
    TgConfig.STATE[user_id] = 'assign_photo_wait_media'
    TgConfig.STATE[f'{user_id}_item'] = item
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    await bot.edit_message_text('Send photo or video for this item:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('assign_photos'))


async def assign_photo_receive_media(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE or role & Permission.ASSIGN_PHOTOS):
        return
    item = TgConfig.STATE.get(f'{user_id}_item')
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if not item:
        return
    preview_folder = os.path.join('assets', 'product_photos', item)
    os.makedirs(preview_folder, exist_ok=True)
    if message.photo:
        file = message.photo[-1]
        ext = 'jpg'
    elif message.video:
        file = message.video
        ext = 'mp4'
    else:
        await bot.send_message(user_id, '❌ Send a photo or video')
        return
    stock_path = get_next_file_path(item, ext)
    await file.download(destination_file=stock_path)
    preview_path = os.path.join(preview_folder, os.path.basename(stock_path))
    shutil.copy(stock_path, preview_path)
    TgConfig.STATE[f'{user_id}_stock_path'] = stock_path
    TgConfig.STATE[user_id] = 'assign_photo_wait_desc'
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    await bot.edit_message_text('Send description for this media:',
                                chat_id=message.chat.id,
                                message_id=message_id,
                                reply_markup=back('assign_photos'))


async def assign_photo_receive_desc(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE or role & Permission.ASSIGN_PHOTOS):
        return
    item = TgConfig.STATE.get(f'{user_id}_item')
    stock_path = TgConfig.STATE.get(f'{user_id}_stock_path')
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if not item or not stock_path:
        return
    preview_folder = os.path.join('assets', 'product_photos', item)
    with open(os.path.join(preview_folder, 'description.txt'), 'w') as f:
        f.write(message.text)
    with open(f'{stock_path}.txt', 'w') as f:
        f.write(message.text)
    add_values_to_item(item, stock_path, False)
    TgConfig.STATE[user_id] = None
    TgConfig.STATE.pop(f'{user_id}_stock_path', None)
    TgConfig.STATE.pop(f'{user_id}_item', None)
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    await bot.edit_message_text('✅ Photo assigned',
                                chat_id=message.chat.id,
                                message_id=message_id,
                                reply_markup=goods_management())

    owner_id = int(EnvKeys.OWNER_ID) if EnvKeys.OWNER_ID else None
    if owner_id:
        username = f'@{message.from_user.username}' if message.from_user.username else message.from_user.full_name
        info = get_item_info(item)
        category = info['category_name']
        parent = get_category_parent(category)
        if parent:
            category_name = parent
            subcategory = category
        else:
            category_name = category
            subcategory = '-'
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
        info_id = f'{user_id}_{int(now.timestamp())}'
        TgConfig.STATE[f'photo_info_{info_id}'] = {
            'username': username,
            'time': now.strftime("%Y-%m-%d %H:%M:%S"),
            'product': display_name(item),
            'category': category_name,
            'subcategory': subcategory,
            'description': message.text,
            'file': stock_path,
        }
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton('Yes', callback_data=f'photo_info_{info_id}'))
        await bot.send_message(owner_id,
                               f'{username}, uploaded a photo to a ({display_name(item)}) in ({category_name}), ({subcategory}).',
                               reply_markup=markup)


async def photo_info_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    data_id = call.data[len('photo_info_'):]
    info = TgConfig.STATE.pop(f'photo_info_{data_id}', None)
    if not info:
        await call.answer('No data')
        return
    text = (
        f"{info['username']}\n"
        f"{info['time']}\n"
        f"Product: {info['product']}\n"
        f"Category: {info['category']} | {info['subcategory']}\n"
        f"Description: {info['description']}\n"
        f"File: {info['file']}"
    )
    await bot.edit_message_text(text,
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id)


async def categories_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🧾 Kategorijų valdymo meniu',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=categories_management())
        return
    await call.answer('Insufficient rights')


async def add_category_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'add_category'
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('Enter category name',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back("categories_management"))
        return
    await call.answer('Insufficient rights')


async def add_subcategory_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        categories = get_all_category_names()
        markup = InlineKeyboardMarkup()
        for cat in categories:
            markup.add(InlineKeyboardButton(cat, callback_data=f'choose_sub_parent_{cat}'))
        markup.add(InlineKeyboardButton('🔙 Back', callback_data='categories_management'))
        await bot.edit_message_text('Select parent category:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=markup)
        return
    await call.answer('Insufficient rights')


async def choose_subcategory_parent(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    parent = call.data[len('choose_sub_parent_'):]
    TgConfig.STATE[user_id] = 'add_subcategory_name'
    TgConfig.STATE[f'{user_id}_parent'] = parent
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    if not check_category(parent):
        await bot.edit_message_text(chat_id=call.message.chat.id,
                                    message_id=message_id,
                                    text='❌ Parent category does not exist',
                                    reply_markup=back('categories_management'))
        TgConfig.STATE[user_id] = None
        return
    await bot.edit_message_text(chat_id=call.message.chat.id,
                                message_id=message_id,
                                text='Enter subcategory name',
                                reply_markup=back('categories_management'))


async def statistics_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        await bot.edit_message_text('Shop statistics:\n'
                                    '➖➖➖➖➖➖➖➖➖➖➖➖➖\n'
                                    '<b>◽USERS</b>\n'
                                    f'◾️Users in last 24h: {select_today_users(today)}\n'
                                    f'◾️Total administrators: {select_admins()}\n'
                                    f'◾️Total users: {get_user_count()}\n'
                                    '➖➖➖➖➖➖➖➖➖➖➖➖➖\n'
                                    '◽<b>FUNDS</b>\n'
                                    f'◾Sales in 24h: {select_today_orders(today)}€\n'
                                    f'◾Items sold for: {select_all_orders()}€\n'
                                    f'◾Top-ups in 24h: {select_today_operations(today)}€\n'
                                    f'◾Funds in system: {select_users_balance()}€\n'
                                    f'◾Total topped up: {select_all_operations()}€\n'
                                    '➖➖➖➖➖➖➖➖➖➖➖➖➖\n'
                                    '◽<b>OTHER</b>\n'
                                    f'◾Items: {select_count_items()}pcs.\n'
                                    f'◾Positions: {select_count_goods()}pcs.\n'
                                    f'◾Categories: {select_count_categories()}pcs.\n'
                                    f'◾Items sold: {select_count_bought_items()}pcs.',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back('shop_management'),
                                    parse_mode='HTML')
        return
    await call.answer('Insufficient rights')


async def process_category_for_add(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    msg = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    TgConfig.STATE[user_id] = None
    category = check_category(msg)
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    if category:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='❌ Category not created (already exists)',
                                    reply_markup=back('categories_management'))
        return
    create_category(msg)
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='✅ Category created',
                                reply_markup=back('categories_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) "
                f'created new category "{msg}"')


async def process_subcategory_name(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    sub = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    parent = TgConfig.STATE.get(f'{user_id}_parent')
    TgConfig.STATE[user_id] = None
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if check_category(sub):
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='❌ Subcategory already exists',
                                    reply_markup=back('categories_management'))
        return
    create_category(sub, parent)
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='✅ Subcategory created',
                                reply_markup=back('categories_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) "
                f'created subcategory "{sub}" under "{parent}"')


async def delete_category_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE):
        await call.answer('Insufficient rights')
        return
    categories = get_all_category_names()
    markup = InlineKeyboardMarkup()
    for cat in categories:
        markup.add(InlineKeyboardButton(cat, callback_data=f'delete_cat_{cat}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='categories_management'))
    await bot.edit_message_text('Select category to delete:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def delete_category_choose_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    category = call.data[len('delete_cat_'):]
    subcats = get_all_subcategories(category)
    markup = InlineKeyboardMarkup()
    for sub in subcats:
        markup.add(InlineKeyboardButton(sub, callback_data=f'delete_cat_{sub}'))
    markup.add(InlineKeyboardButton(f'🗑️ Delete {category}', callback_data=f'delete_cat_confirm_{category}'))
    back_parent = get_category_parent(category)
    back_data = 'delete_category' if back_parent is None else f'delete_cat_{back_parent}'
    markup.add(InlineKeyboardButton('🔙 Back', callback_data=back_data))
    await bot.edit_message_text('Choose subcategory or delete:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def delete_category_confirm_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    category = call.data[len('delete_cat_confirm_'):]
    delete_category(category)
    await bot.edit_message_text('✅ Category deleted',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('categories_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) deleted category \"{category}\"")


async def update_category_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    TgConfig.STATE[user_id] = 'check_category'
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('Enter category name to update:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back("categories_management"))
        return
    await call.answer('Insufficient rights')


async def check_category_for_update(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    category_name = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    category = check_category(category_name)
    if not category:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='❌ Category cannot be updated (does not exist)',
                                    reply_markup=back('categories_management'))
        return
    TgConfig.STATE[user_id] = 'update_category_name'
    TgConfig.STATE[f'{user_id}_check_category'] = message.text
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Enter new category name:',
                                reply_markup=back('categories_management'))


async def check_category_name_for_update(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    category = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    old_name = TgConfig.STATE.get(f'{user_id}_check_category')
    TgConfig.STATE[user_id] = None
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    update_category(old_name, category)
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text=f'✅ Category "{category}" updated successfully.',
                                reply_markup=back('categories_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) "
                f'changed category "{old_name}" to "{category}"')


async def goods_settings_menu_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🛒 Pasirinkite veiksmą šiai prekei',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=item_management())
        return
    await call.answer('Insufficient rights')


async def add_item_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    TgConfig.STATE[user_id] = 'create_item_name'
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🏷️ Įveskite prekės pavadinimą',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back("item-management"))
        return
    await call.answer('Insufficient rights')


async def check_item_name_for_add(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    item_name = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    item = check_item(item_name)
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if item:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='❌ Item cannot be created (already exists)',
                                    reply_markup=back('item-management'))
        return
    TgConfig.STATE[user_id] = 'create_item_description_choice'
    TgConfig.STATE[f'{user_id}_name'] = message.text
    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton('✅ Yes', callback_data='add_item_desc_yes'),
        InlineKeyboardButton('❌ No', callback_data='add_item_desc_no')
    )
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='item-management'))
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Add description for item?',
                                reply_markup=markup)


async def add_item_desc_yes(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'create_item_description'
    await bot.edit_message_text('Enter description for item:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('item-management'))


async def add_item_desc_no(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[f'{user_id}_description'] = ''
    TgConfig.STATE[user_id] = 'create_item_price'
    await bot.edit_message_text('Enter price for item:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('item-management'))


async def add_item_description(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    TgConfig.STATE[f'{user_id}_description'] = message.text
    TgConfig.STATE[user_id] = 'create_item_price'
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Enter price for item:',
                                reply_markup=back('item-management'))


async def add_item_price(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    raw_price = (message.text or '').replace(',', '.').strip()
    try:
        price = Decimal(raw_price)
    except (InvalidOperation, ValueError):
        price = None
    if not price or price <= 0 or price != price.quantize(Decimal('1')):
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='⚠️ Invalid price value.',
                                    reply_markup=back('item-management'))
        return
    TgConfig.STATE[f'{user_id}_price'] = int(price)
    categories = get_all_category_names()
    markup = InlineKeyboardMarkup()
    for cat in categories:
        markup.add(InlineKeyboardButton(cat, callback_data=f'add_item_cat_{cat}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='item-management'))
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Select category:',
                                reply_markup=markup)


async def add_item_choose_category(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    categories = get_all_category_names()
    markup = InlineKeyboardMarkup()
    for cat in categories:
        markup.add(InlineKeyboardButton(cat, callback_data=f'add_item_cat_{cat}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='item-management'))
    await bot.edit_message_text('Select category:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def add_item_category_selected(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    category = call.data[len('add_item_cat_'):]
    subs = get_all_subcategories(category)
    if subs:
        markup = InlineKeyboardMarkup()
        for sub in subs:
            markup.add(InlineKeyboardButton(sub, callback_data=f'add_item_sub_{sub}'))
        markup.add(InlineKeyboardButton('🔙 Back', callback_data='add_item_choose_cat'))
        await bot.edit_message_text('Select subcategory:',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=markup)
        return
    item_name = TgConfig.STATE.get(f'{user_id}_name')
    item_description = TgConfig.STATE.get(f'{user_id}_description')
    item_price = TgConfig.STATE.get(f'{user_id}_price')
    internal_name = generate_internal_name(item_name)
    create_item(internal_name, item_description, item_price, category, None)
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) created new item \"{internal_name}\"")
    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton('✅ Yes', callback_data='add_item_more_yes'),
        InlineKeyboardButton('❌ No', callback_data='add_item_more_no')
    )
    await bot.edit_message_text('Add this product somewhere else?',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def add_item_subcategory_selected(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    sub = call.data[len('add_item_sub_'):]
    item_name = TgConfig.STATE.get(f'{user_id}_name')
    item_description = TgConfig.STATE.get(f'{user_id}_description')
    item_price = TgConfig.STATE.get(f'{user_id}_price')
    internal_name = generate_internal_name(item_name)
    create_item(internal_name, item_description, item_price, sub, None)
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) created new item \"{internal_name}\"")
    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton('✅ Yes', callback_data='add_item_more_yes'),
        InlineKeyboardButton('❌ No', callback_data='add_item_more_no')
    )
    await bot.edit_message_text('Add this product somewhere else?',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def add_item_more_yes(call: CallbackQuery):
    await add_item_choose_category(call)


async def add_item_more_no(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    for key in ('name', 'description', 'price'):
        TgConfig.STATE.pop(f'{user_id}_{key}', None)
    TgConfig.STATE.pop(f'{user_id}_message_id', None)
    await bot.edit_message_text('✅ Items created, products added',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('item-management'))


async def update_item_amount_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    TgConfig.STATE[user_id] = 'update_amount_of_item'
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🏷️ Įveskite prekės pavadinimą',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back("item-management"))
        return
    await call.answer('Insufficient rights')


async def check_item_name_for_amount_upd(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    item_name = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    item = check_item(item_name)
    if not item:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='❌ Товар не может быть добавлен (Такой позиции не существует)',
                                    reply_markup=back('goods_management'))
    else:
        if check_value(item_name) is False:
            TgConfig.STATE[user_id] = 'add_new_amount'
            TgConfig.STATE[f'{user_id}_name'] = message.text
            await bot.edit_message_text(chat_id=message.chat.id,
                                        message_id=message_id,
                                        text='Send folder path with product files or list values separated by ;:',
                                        reply_markup=back('goods_management'))
        else:
            await bot.edit_message_text(chat_id=message.chat.id,
                                        message_id=message_id,
                                        text='❌ Товар не может быть добавлен (У данной позиции бесконечный товар)',
                                        reply_markup=back('goods_management'))


async def updating_item_amount(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if message.photo:
        file_path = get_next_file_path(TgConfig.STATE.get(f'{user_id}_name'))
        file_name = f"{TgConfig.STATE.get(f'{user_id}_name')}_{int(datetime.datetime.now().timestamp())}.jpg"
        file_path = os.path.join('assets', 'uploads', file_name)
        await message.photo[-1].download(destination_file=file_path)
        values_list = [file_path]
    else:
        if os.path.isdir(message.text):
            folder = message.text
            values_list = [os.path.join(folder, f) for f in os.listdir(folder)]
        else:
            values_list = message.text.split(';')
    TgConfig.STATE[user_id] = None
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    item_name = TgConfig.STATE.get(f'{user_id}_name')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    for i in values_list:
        add_values_to_item(item_name, i, False)
    group_id = TgConfig.GROUP_ID if TgConfig.GROUP_ID != -988765433 else None
    if group_id:
        try:
            await bot.send_message(
                chat_id=group_id,
                text=f'🎁 Upload\n🏷️ Item: <b>{item_name}</b>',
                parse_mode='HTML'
            )
        except ChatNotFound:
            pass
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='✅ Товар добавлен',
                                reply_markup=back('goods_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) "
                f'добавил товары к позиции "{item_name}" в количестве {len(values_list)} шт')


async def update_item_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'check_item_name'
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text('🏷️ Įveskite prekės pavadinimą',
                                    chat_id=call.message.chat.id,
                                    message_id=call.message.message_id,
                                    reply_markup=back("goods_management"))
        return
    await call.answer('Insufficient rights')


async def check_item_name_for_update(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    item_name = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    item = check_item(item_name)
    if not item:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='❌ Item cannot be changed (does not exist)',
                                    reply_markup=back('goods_management'))
        return
    TgConfig.STATE[user_id] = 'update_item_name'
    TgConfig.STATE[f'{user_id}_old_name'] = message.text
    TgConfig.STATE[f'{user_id}_category'] = item['category_name']
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Введите новое имя для позиции:',
                                reply_markup=back('goods_management'))


async def update_item_name(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    TgConfig.STATE[f'{user_id}_name'] = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    TgConfig.STATE[user_id] = 'update_item_description'
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Enter description for item:',
                                reply_markup=back('goods_management'))


async def update_item_description(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    TgConfig.STATE[f'{user_id}_description'] = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    TgConfig.STATE[user_id] = 'update_item_price'
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='Enter price for item:',
                                reply_markup=back('goods_management'))


async def update_item_price(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    TgConfig.STATE[user_id] = None
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    text = (message.text or '').strip().replace(',', '.')
    try:
        price_value = Decimal(text)
    except InvalidOperation:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='⚠️ Invalid price value. Use numbers like 19.99.',
                                    reply_markup=back('goods_management'))
        return
    if price_value <= 0:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='⚠️ Price must be greater than zero.',
                                    reply_markup=back('goods_management'))
        return
    price_value = price_value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    TgConfig.STATE[f'{user_id}_price'] = str(price_value)
    item_old_name = TgConfig.STATE.get(f'{user_id}_old_name')
    if check_value(item_old_name) is False:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='Do you want to make unlimited goods?',
                                    reply_markup=question_buttons('change_make_infinity', 'goods_management'))
    else:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text='Do you want to disable unlimited goods?',
                                    reply_markup=question_buttons('change_deny_infinity', 'goods_management'))


async def update_item_process(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    answer = call.data.split('_')
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    item_old_name = TgConfig.STATE.get(f'{user_id}_old_name')
    item_new_name = TgConfig.STATE.get(f'{user_id}_name')
    item_description = TgConfig.STATE.get(f'{user_id}_description')
    category = TgConfig.STATE.get(f'{user_id}_category')
    price = TgConfig.STATE.get(f'{user_id}_price')
    if answer[3] == 'no':
        TgConfig.STATE[user_id] = None
        delivery_desc = check_item(item_old_name).get('delivery_description')
        normalized_price = Decimal(str(price).replace(',', '.'))
        update_item(
            item_old_name,
            item_new_name,
            item_description,
            normalized_price,
            category,
            delivery_desc,
            changed_by=user_id,
        )
        reset_all_stock_caches()
        await bot.edit_message_text(chat_id=call.message.chat.id,
                                    message_id=message_id,
                                    text='✅ Item updated',
                                    reply_markup=back('goods_management'))
        admin_info = await bot.get_chat(user_id)
        logger.info(f"User {user_id} ({admin_info.first_name}) "
                    f'обновил позицию "{item_old_name}" на "{item_new_name}"')
    else:
        if answer[1] == 'make':
            await bot.edit_message_text(chat_id=call.message.chat.id,
                                        message_id=message_id,
                                        text='Enter item value:',
                                        reply_markup=back('goods_management'))
            TgConfig.STATE[f'{user_id}_change'] = 'make'
        elif answer[1] == 'deny':
            await bot.edit_message_text(chat_id=call.message.chat.id,
                                        message_id=message_id,
                                        text='Send folder path with product files or list values separated by ;:',
                                        reply_markup=back('goods_management'))
            TgConfig.STATE[f'{user_id}_change'] = 'deny'
    TgConfig.STATE[user_id] = 'apply_change'


async def update_item_infinity(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if message.photo:
        file_path = get_next_file_path(TgConfig.STATE.get(f'{user_id}_old_name'))
        file_name = f"{TgConfig.STATE.get(f'{user_id}_old_name')}_{int(datetime.datetime.now().timestamp())}.jpg"
        file_path = os.path.join('assets', 'uploads', file_name)
        await message.photo[-1].download(destination_file=file_path)
        msg = file_path
    else:
        msg = message.text
    change = TgConfig.STATE[f'{user_id}_change']
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    item_old_name = TgConfig.STATE.get(f'{user_id}_old_name')
    item_new_name = TgConfig.STATE.get(f'{user_id}_name')
    item_description = TgConfig.STATE.get(f'{user_id}_description')
    category = TgConfig.STATE.get(f'{user_id}_category')
    price = TgConfig.STATE.get(f'{user_id}_price')
    await bot.delete_message(chat_id=message.chat.id,
                             message_id=message.message_id)
    if change == 'make':
        delete_only_items(item_old_name)
        add_values_to_item(item_old_name, msg, False)
    elif change == 'deny':
        delete_only_items(item_old_name)
        if os.path.isdir(msg):
            values_list = [os.path.join(msg, f) for f in os.listdir(msg)]
        else:
            values_list = msg.split(';')
        for i in values_list:
            add_values_to_item(item_old_name, i, False)
    TgConfig.STATE[user_id] = None
    delivery_desc = check_item(item_old_name).get('delivery_description')
    normalized_price = Decimal(str(price).replace(',', '.'))
    update_item(
        item_old_name,
        item_new_name,
        item_description,
        normalized_price,
        category,
        delivery_desc,
        changed_by=user_id,
    )
    reset_all_stock_caches()
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text='✅ Item updated',
                                reply_markup=back('goods_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) "
                f'обновил позицию "{item_old_name}" на "{item_new_name}"')


async def delete_item_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    role = check_role(user_id)
    if not (role & Permission.SHOP_MANAGE):
        await call.answer('Insufficient rights')
        return
    categories = get_all_category_names()
    markup = InlineKeyboardMarkup()
    for cat in categories:
        markup.add(InlineKeyboardButton(cat, callback_data=f'delete_item_cat_{cat}'))
    markup.add(InlineKeyboardButton('🔙 Back', callback_data='goods_management'))
    await bot.edit_message_text('Choose category:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def delete_item_category_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    category = call.data[len('delete_item_cat_'):]
    subcats = get_all_subcategories(category)
    items = get_all_item_names(category)
    markup = InlineKeyboardMarkup()
    for sub in subcats:
        markup.add(InlineKeyboardButton(sub, callback_data=f'delete_item_cat_{sub}'))
    for item in items:
        markup.add(InlineKeyboardButton(display_name(item), callback_data=f'delete_item_item_{item}'))
    back_parent = get_category_parent(category)
    back_data = 'delete_item' if back_parent is None else f'delete_item_cat_{back_parent}'
    markup.add(InlineKeyboardButton('🔙 Back', callback_data=back_data))
    await bot.edit_message_text('Choose subcategory or item to delete:',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def delete_item_item_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    item_name = call.data[len('delete_item_item_'):]
    delete_item(item_name)
    await bot.edit_message_text('✅ Item deleted',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=back('goods_management'))
    admin_info = await bot.get_chat(user_id)
    logger.info(f"User {user_id} ({admin_info.first_name}) удалил позицию \"{item_name}\"")


async def show_bought_item_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'show_item'
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    role = check_role(user_id)
    if role & Permission.SHOP_MANAGE:
        await bot.edit_message_text(
            '🔍 Enter the unique ID of the purchased item',
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=back("goods_management"))
        return
    await call.answer('Insufficient rights')


async def process_item_show(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    msg = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    TgConfig.STATE[user_id] = None
    item = select_bought_item(msg)
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    if item:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=message_id,
            text=f'<b>Item</b>: <code>{item["item_name"]}</code>\n'
                 f'<b>Price</b>: <code>{item["price"]}</code>€\n'
                 f'<b>Purchase date</b>: <code>{item["bought_datetime"]}</code>\n'
                 f'<b>Buyer</b>: <code>{item["buyer_id"]}</code>\n'
                 f'<b>Unique operation ID</b>: <code>{item["unique_id"]}</code>\n'
                 f'<b>Value</b>:\n<code>{item["value"]}</code>',
            parse_mode='HTML',
            reply_markup=back('show_bought_item')
        )
        return
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=message_id,
        text='❌ Item with the specified unique ID was not found',
        reply_markup=back('show_bought_item')
    )



def register_shop_management(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(statistics_callback_handler,
                                       lambda c: c.data == 'statistics')
    dp.register_callback_query_handler(goods_settings_menu_callback_handler,
                                       lambda c: c.data == 'item-management')
    dp.register_callback_query_handler(add_item_callback_handler,
                                       lambda c: c.data == 'add_item')
    dp.register_callback_query_handler(update_item_amount_callback_handler,
                                       lambda c: c.data == 'update_item_amount')
    dp.register_callback_query_handler(update_item_callback_handler,
                                       lambda c: c.data == 'update_item')
    dp.register_callback_query_handler(delete_item_callback_handler,
                                       lambda c: c.data == 'delete_item')
    dp.register_callback_query_handler(delete_item_category_handler,
                                       lambda c: c.data.startswith('delete_item_cat_'))
    dp.register_callback_query_handler(delete_item_item_handler,
                                       lambda c: c.data.startswith('delete_item_item_'))
    dp.register_callback_query_handler(show_bought_item_callback_handler,
                                       lambda c: c.data == 'show_bought_item')
    dp.register_callback_query_handler(assign_photos_callback_handler,
                                       lambda c: c.data == 'assign_photos')
    dp.register_callback_query_handler(assign_photo_category_handler,
                                       lambda c: c.data.startswith('assign_photo_cat_'))
    dp.register_callback_query_handler(assign_photo_subcategory_handler,
                                       lambda c: c.data.startswith('assign_photo_sub_'))
    dp.register_callback_query_handler(assign_photo_item_handler,
                                       lambda c: c.data.startswith('assign_photo_item_'))
    dp.register_callback_query_handler(photo_info_callback_handler,
                                       lambda c: c.data.startswith('photo_info_'))
    dp.register_callback_query_handler(shop_callback_handler,
                                       lambda c: c.data == 'shop_management')
    dp.register_callback_query_handler(logs_callback_handler,
                                       lambda c: c.data == 'show_logs')
    dp.register_callback_query_handler(goods_management_callback_handler,
                                       lambda c: c.data == 'goods_management')
    dp.register_callback_query_handler(promo_management_callback_handler,
                                       lambda c: c.data == 'promo_management')
    dp.register_callback_query_handler(categories_callback_handler,
                                       lambda c: c.data == 'categories_management')
    dp.register_callback_query_handler(add_category_callback_handler,
                                       lambda c: c.data == 'add_category')
    dp.register_callback_query_handler(add_subcategory_callback_handler,
                                       lambda c: c.data == 'add_subcategory')
    dp.register_callback_query_handler(choose_subcategory_parent,
                                       lambda c: c.data.startswith('choose_sub_parent_'))
    dp.register_callback_query_handler(add_item_category_selected,
                                       lambda c: c.data.startswith('add_item_cat_'))
    dp.register_callback_query_handler(add_item_subcategory_selected,
                                       lambda c: c.data.startswith('add_item_sub_'))
    dp.register_callback_query_handler(add_item_desc_yes,
                                       lambda c: c.data == 'add_item_desc_yes')
    dp.register_callback_query_handler(add_item_desc_no,
                                       lambda c: c.data == 'add_item_desc_no')
    dp.register_callback_query_handler(add_item_more_yes,
                                       lambda c: c.data == 'add_item_more_yes')
    dp.register_callback_query_handler(add_item_more_no,
                                       lambda c: c.data == 'add_item_more_no')
    dp.register_callback_query_handler(add_item_choose_category,
                                       lambda c: c.data == 'add_item_choose_cat')
    dp.register_callback_query_handler(delete_category_callback_handler,
                                       lambda c: c.data == 'delete_category')
    dp.register_callback_query_handler(delete_category_confirm_handler,
                                       lambda c: c.data.startswith('delete_cat_confirm_'))
    dp.register_callback_query_handler(delete_category_choose_handler,
                                       lambda c: c.data.startswith('delete_cat_') and not c.data.startswith('delete_cat_confirm_'))
    dp.register_callback_query_handler(update_category_callback_handler,
                                       lambda c: c.data == 'update_category')
    dp.register_callback_query_handler(create_promo_callback_handler,
                                       lambda c: c.data == 'create_promo')
    dp.register_callback_query_handler(delete_promo_callback_handler,
                                       lambda c: c.data == 'delete_promo')
    dp.register_callback_query_handler(manage_promo_callback_handler,
                                       lambda c: c.data == 'manage_promo')
    dp.register_callback_query_handler(promo_code_delete_callback_handler,
                                       lambda c: c.data.startswith('delete_promo_code_'))
    dp.register_callback_query_handler(promo_manage_select_handler,
                                       lambda c: c.data.startswith('manage_promo_code_'))
    dp.register_callback_query_handler(promo_manage_discount_handler,
                                       lambda c: c.data.startswith('promo_manage_discount_'))
    dp.register_callback_query_handler(promo_manage_expiry_handler,
                                       lambda c: c.data.startswith('promo_manage_expiry_'))
    dp.register_callback_query_handler(promo_manage_geo_handler,
                                       lambda c: c.data.startswith('promo_manage_geo_'))
    dp.register_callback_query_handler(promo_target_main_handler,
                                       lambda c: c.data == 'promo_target_main' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_cities_handler,
                                       lambda c: c.data == 'promo_target_cities' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_city_toggle_handler,
                                       lambda c: c.data.startswith('promo_target_city_toggle_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_city_clear_handler,
                                       lambda c: c.data == 'promo_target_city_clear' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_districts_handler,
                                       lambda c: c.data == 'promo_target_districts' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_district_open_handler,
                                       lambda c: c.data.startswith('promo_target_district_open_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_district_toggle_handler,
                                       lambda c: c.data.startswith('promo_target_district_toggle_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_district_clear_handler,
                                       lambda c: c.data == 'promo_target_district_clear' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_products_handler,
                                       lambda c: c.data == 'promo_target_products' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_switch_handler,
                                       lambda c: c.data == 'promo_target_product_switch' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_toggle_cat_handler,
                                       lambda c: c.data.startswith('promo_target_product_toggle_cat_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_open_subcategories_handler,
                                       lambda c: c.data.startswith('promo_target_product_open_sub_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_open_handler,
                                       lambda c: c.data.startswith('promo_target_product_open_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_toggle_sub_handler,
                                       lambda c: c.data.startswith('promo_target_product_toggle_sub_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_open_subitem_handler,
                                       lambda c: c.data.startswith('promo_target_product_open_subitem_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_toggle_item_handler,
                                       lambda c: c.data.startswith('promo_target_product_toggle_item_') and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_clear_handler,
                                       lambda c: c.data == 'promo_target_product_clear' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_clear_subcategories_handler,
                                       lambda c: c.data == 'promo_target_product_clear_sub' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_product_clear_items_handler,
                                       lambda c: c.data == 'promo_target_product_clear_items' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_target_save_handler,
                                       lambda c: c.data == 'promo_target_save' and _promo_target_state(c.from_user.id))
    dp.register_callback_query_handler(promo_manage_stats_handler,
                                       lambda c: c.data.startswith('promo_manage_stats_'))
    dp.register_callback_query_handler(promo_manage_delete_handler,
                                       lambda c: c.data.startswith('promo_manage_delete_'))
    dp.register_callback_query_handler(promo_create_expiry_type_handler,
                                       lambda c: c.data.startswith('promo_expiry_') and TgConfig.STATE.get(c.from_user.id) == 'promo_create_expiry_type')
    dp.register_callback_query_handler(promo_manage_expiry_type_handler,
                                       lambda c: c.data.startswith('promo_expiry_') and TgConfig.STATE.get(c.from_user.id) == 'promo_manage_expiry_type')

    dp.register_message_handler(check_item_name_for_amount_upd,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'update_amount_of_item')
    dp.register_message_handler(updating_item_amount,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'add_new_amount')
    dp.register_message_handler(check_item_name_for_add,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'create_item_name')
    dp.register_message_handler(add_item_description,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'create_item_description')
    dp.register_message_handler(add_item_price,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'create_item_price')
    dp.register_message_handler(assign_photo_receive_media,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'assign_photo_wait_media',
                                content_types=['photo', 'video'])
    dp.register_message_handler(assign_photo_receive_desc,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'assign_photo_wait_desc')
    dp.register_message_handler(check_item_name_for_update,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'check_item_name')
    dp.register_message_handler(update_item_name,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'update_item_name')
    dp.register_message_handler(update_item_description,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'update_item_description')
    dp.register_message_handler(update_item_price,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'update_item_price')
    dp.register_message_handler(process_item_show,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'show_item')
    dp.register_message_handler(process_category_for_add,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'add_category')
    dp.register_message_handler(process_subcategory_name,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'add_subcategory_name')
    dp.register_message_handler(check_category_for_update,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'check_category')
    dp.register_message_handler(check_category_name_for_update,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'update_category_name')
    dp.register_message_handler(update_item_infinity,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'apply_change')
    dp.register_message_handler(promo_code_receive_code,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'promo_create_code')
    dp.register_message_handler(promo_code_receive_discount,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'promo_create_discount')
    dp.register_message_handler(promo_code_receive_expiry_number,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'promo_create_expiry_number')
    dp.register_message_handler(
        promo_code_receive_geo,
        lambda c: TgConfig.STATE.get(c.from_user.id) in {
            'promo_create_geo',
            'promo_manage_geo',
            'promo_create_targets',
            'promo_manage_targets',
        }
    )
    dp.register_message_handler(promo_manage_receive_discount,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'promo_manage_discount')
    dp.register_message_handler(promo_manage_receive_expiry_number,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'promo_manage_expiry_number')
    dp.register_message_handler(promo_manage_receive_geo,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'promo_manage_geo')

    dp.register_callback_query_handler(update_item_process,
                                       lambda c: c.data.startswith('change_'))
