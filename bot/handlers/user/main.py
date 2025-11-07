import asyncio
import datetime
import math
import os
import random
import shutil
from collections.abc import Sequence
from io import BytesIO
from urllib.parse import urlparse
import html

import qrcode

import contextlib


from aiogram import Dispatcher
from aiogram.types import (
    CallbackQuery,
    ChatType,
    ContentType,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.exceptions import MessageNotModified

from bot.database.methods import (
    select_max_role_id, get_role_id_by_name, create_user, check_role, check_user, get_all_categories, get_all_items,
    select_bought_items, get_bought_item_info, get_item_info, select_item_values_amount,
    get_user_balance, get_item_value, buy_item, add_bought_item, buy_item_for_balance,
    select_user_operations, select_user_items, start_operation, select_unfinished_operations,
    get_user_referral, finish_operation, update_balance, create_operation, bought_items_list,
    check_value, get_subcategories, get_category_parent, get_user_language, update_user_language,
    get_unfinished_operation, get_promocode, mark_promocode_used, is_promocode_used, update_promocode,
    set_role,
)
from bot.handlers.other import get_bot_user_ids, get_bot_info
from bot.keyboards import (
    main_menu, categories_list, goods_list, subcategories_list, user_items_list, back, item_info,
    profile, rules, payment_menu, close, crypto_choice, crypto_invoice_menu, purchase_crypto_invoice_menu,
    blackjack_controls, blackjack_bet_input_menu, blackjack_end_menu, blackjack_history_menu, confirm_cancel,
    feedback_menu, tip_menu, confirm_purchase_menu, wheel_spin_confirm_keyboard)
from bot.localization import t


def _normalize_city_name(raw: str) -> str:
    return ' '.join((raw or '').strip().split()).title()


def _normalize_district_name(raw: str) -> str | None:
    text = ' '.join((raw or '').strip().split())
    if not text or text.lower() in {'none', 'n/a', 'na', 'no', '-', 'all', 'нет', 'не требуется', 'nera', 'nėra'}:
        return None
    return text.title()


def _promo_matches_geo(promo: dict, city: str, district: str | None) -> bool:
    targets = promo.get('geo_targets') or []
    if not targets:
        return True
    city_key = city.strip().casefold()
    district_key = district.strip().casefold() if district else None
    for entry in targets:
        entry_city = (entry.get('city') or '').strip().casefold()
        if entry_city != city_key:
            continue
        entry_district = entry.get('district')
        if not entry_district:
            return True
        if district_key and entry_district.strip().casefold() == district_key:
            return True
    return False


def _category_chain(category: str | None) -> set[str]:
    names: set[str] = set()
    current = (category or '').strip()
    visited: set[str] = set()
    while current and current not in visited:
        key = current.casefold()
        names.add(key)
        visited.add(current)
        parent = get_category_parent(current)
        current = (parent or '').strip() if parent else ''
    return names


def _promo_matches_product(promo: dict, item_name: str, category: str) -> bool:
    filters = promo.get('product_filters') or []
    if not filters:
        return True

    allowed = [f for f in filters if f.get('is_allowed')]
    excluded = [f for f in filters if not f.get('is_allowed')]
    item_key = item_name.casefold()
    category_keys = _category_chain(category)

    def _matches(entry: dict) -> bool:
        target_type = entry.get('type')
        name = (entry.get('name') or '').strip().casefold()
        if target_type == 'item':
            return item_key == name
        if target_type == 'category':
            return name in category_keys
        return False

    if allowed and not any(_matches(entry) for entry in allowed):
        return False
    if excluded and any(_matches(entry) for entry in excluded):
        return False
    return True


def _reset_promo_details(user_id: int) -> None:
    TgConfig.STATE.pop(f'{user_id}_promo_code_input', None)
    TgConfig.STATE.pop(f'{user_id}_promo_city', None)
    TgConfig.STATE.pop(f'{user_id}_promo_district', None)
    TgConfig.STATE.pop(f'{user_id}_promo_data', None)


def _clear_promo_flow(user_id: int) -> None:
    TgConfig.STATE[user_id] = None
    _reset_promo_details(user_id)
    TgConfig.STATE.pop(f'{user_id}_message_id', None)


def _promo_applied_key(user_id: int) -> str:
    return f'{user_id}_promo_applied'


def _promo_application_available(user_id: int) -> bool:
    return not TgConfig.STATE.get(_promo_applied_key(user_id), False)


def _active_promo_key(user_id: int) -> str:
    return f'{user_id}_active_promo'


def _store_active_promo(
    user_id: int,
    item_name: str,
    code: str,
    city: str | None,
    district: str | None,
) -> None:
    TgConfig.STATE[_active_promo_key(user_id)] = {
        'item_name': item_name,
        'code': code,
        'city': city,
        'district': district,
    }


def _discard_active_promo(user_id: int) -> None:
    TgConfig.STATE.pop(_active_promo_key(user_id), None)
    TgConfig.STATE.pop(_promo_applied_key(user_id), None)


def _complete_active_promo(user_id: int, item_name: str) -> None:
    key = _active_promo_key(user_id)
    active = TgConfig.STATE.get(key)
    if not active or active.get('item_name') != item_name:
        return
    code = active.get('code')
    if not code:
        TgConfig.STATE.pop(key, None)
        TgConfig.STATE.pop(_promo_applied_key(user_id), None)
        return
    mark_promocode_used(
        user_id,
        code,
        item_name,
        city=active.get('city'),
        district=active.get('district'),
    )
    TgConfig.STATE.pop(key, None)
    TgConfig.STATE.pop(_promo_applied_key(user_id), None)


def _welcome_context_key(user_id: int) -> str:
    return f'{user_id}_welcome_menu'


def _has_welcome_media() -> bool:
    path = getattr(TgConfig, 'START_PHOTO_PATH', None)
    return bool(path and os.path.isfile(path))


async def _send_welcome_media(bot, user_id: int) -> bool:
    if not _has_welcome_media():
        return False
    try:
        with open(TgConfig.START_PHOTO_PATH, 'rb') as media:
            if str(TgConfig.START_PHOTO_PATH).lower().endswith('.mp4'):
                await bot.send_video(user_id, media)
            else:
                await bot.send_photo(user_id, media)
        return True
    except Exception:
        return False


async def _offer_welcome_video(bot, user_id: int, text: str, markup, lang: str) -> None:
    key = _welcome_context_key(user_id)
    TgConfig.STATE[key] = {'text': text, 'markup': markup}
    if not _has_welcome_media():
        TgConfig.STATE.pop(key, None)
        await bot.send_message(user_id, text, reply_markup=markup)
        return

    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(t(lang, 'welcome_video_yes'), callback_data='welcome_video_yes'),
        InlineKeyboardButton(t(lang, 'welcome_video_no'), callback_data='welcome_video_no'),
    )
    await bot.send_message(
        user_id,
        t(lang, 'welcome_video_prompt'),
        reply_markup=keyboard,
    )


async def _handle_welcome_video_choice(call: CallbackQuery, send_media: bool) -> None:
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    context = TgConfig.STATE.pop(_welcome_context_key(user_id), None)

    if context:
        text = context.get('text')
        markup = context.get('markup')
    else:
        role = check_role(user_id)
        balance = get_user_balance(user_id) or 0
        purchases = select_user_items(user_id)
        markup = main_menu(role, TgConfig.REVIEWS_URL, TgConfig.PRICE_LIST_URL, lang)
        text = build_menu_text(call.from_user, balance, purchases, lang)

    with contextlib.suppress(Exception):
        await call.message.delete()

    if send_media and _has_welcome_media():
        await _send_welcome_media(bot, user_id)

    await bot.send_message(user_id, text, reply_markup=markup)
    await call.answer()


def _render_wheel_frame(order: Sequence, pointer_index: int) -> str:
    prizes = list(order)
    if prizes:
        emojis = [getattr(entry, 'emoji', None) or '🎁' for entry in prizes]
    else:
        emojis = ['🎁']

    total = len(emojis)
    if total == 1:
        return "      ⬆️\n      {emoji}".format(emoji=emojis[0])

    pointer_index = pointer_index % total

    def _emoji_at(offset: int) -> str:
        return emojis[(pointer_index + offset) % total]

    ring = [_emoji_at(idx) for idx in range(8)]

    top, top_right, right, bottom_right, bottom, bottom_left, left, top_left = ring

    lines = [
        "      ⬆️",
        f"      {top}",
        f"  {top_left}   🎡   {top_right}",
        f"{left}         {right}",
        f"  {bottom_left}   🔻   {bottom_right}",
        f"      {bottom}",
    ]
    return '\n'.join(lines)


async def _deliver_wheel_prize(bot, chat_id: int, prize, lang: str) -> None:
    emoji_symbol = prize.emoji or '🎁'
    caption = t(
        lang,
        'wheel_prize_delivery_caption',
        emoji=emoji_symbol,
        name=html.escape(prize.name),
        location=html.escape(prize.location),
    )
    if prize.photo_file_id:
        await bot.send_photo(
            chat_id=chat_id,
            photo=prize.photo_file_id,
            caption=caption,
            parse_mode='HTML',
        )
    else:
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode='HTML')


def _to_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _calculate_discounted_price(raw_price, discount) -> float:
    base_price = _to_float(raw_price)
    discount_value = _to_float(discount)
    return round(base_price * (100.0 - discount_value) / 100.0, 2)


def _build_purchase_confirmation_text(
    lang: str,
    item_name: str,
    price: float,
    balance: float,
) -> str:
    header = t(lang, 'confirm_purchase', item=display_name(item_name), price=price)
    details = t(
        lang,
        'confirm_purchase_details',
        balance=f'{balance:.2f}',
        due=f'{max(price - balance, 0):.2f}',
    )
    return f'{header}\n\n{details}'


def _get_current_item_price(user_id: int, item_name: str) -> float:
    price = TgConfig.STATE.get(f'{user_id}_price')
    if price is not None:
        return price
    info = get_item_info(item_name)
    if not info:
        return 0.0
    purchases = select_user_items(user_id)
    _, discount, _, _ = get_level_info(purchases)
    price = _calculate_discounted_price(info.get('price'), discount)
    TgConfig.STATE[f'{user_id}_price'] = price
    return price


async def _edit_promo_message(
    bot,
    chat_id: int,
    message_id: int | None,
    user_id: int,
    item_name: str,
    lang: str,
    text: str,
    reply_markup=None,
) -> None:
    current_price = _get_current_item_price(user_id, item_name)
    if reply_markup is None:
        balance = get_user_balance(user_id)
        promo_available = _promo_application_available(user_id)
        reply_markup = confirm_purchase_menu(
            item_name,
            lang,
            user_id,
            current_price,
            balance,
            promo_available,
        )
    if message_id is None:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        TgConfig.STATE[f'{user_id}_message_id'] = sent.message_id
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        TgConfig.STATE[f'{user_id}_message_id'] = sent.message_id


async def _apply_promo_discount(
    bot,
    chat_id: int,
    message_id: int | None,
    user_id: int,
    item_name: str,
    lang: str,
    promo: dict,
    code: str,
    city: str | None,
    district: str | None,
) -> None:
    price = _get_current_item_price(user_id, item_name)
    discount = promo.get('discount', 0)
    new_price = _calculate_discounted_price(price, discount)
    TgConfig.STATE[f'{user_id}_price'] = new_price
    TgConfig.STATE[_promo_applied_key(user_id)] = True
    _store_active_promo(user_id, item_name, code, city, district)
    balance = get_user_balance(user_id)
    message_text = '\n\n'.join(
        [
            t(lang, 'promo_applied', price=new_price),
            _build_purchase_confirmation_text(lang, item_name, new_price, balance),
        ]
    )
    await _edit_promo_message(
        bot,
        chat_id,
        message_id,
        user_id,
        item_name,
        lang,
        message_text,
    )
    _clear_promo_flow(user_id)


from bot.logger_mesh import logger
from bot.misc import TgConfig, EnvKeys
from bot.misc.payment import quick_pay, check_payment_status
from bot.misc.nowpayments import create_payment, check_payment
from bot.utils import display_name
from bot.utils.notifications import (
    notify_owner_of_purchase,
    notify_owner_of_prize_win,
    notify_owner_of_topup,
)
from bot.utils.level import get_level_info
from bot.utils.files import cleanup_item_file
from bot.utils.security import SecurityManager

PURCHASE_SUCCESS_STATUSES = {'finished', 'confirmed', 'sending', 'paid', 'success'}
PURCHASE_FAILURE_STATUSES = {'failed', 'refunded', 'expired', 'chargeback', 'cancelled'}


def build_menu_text(user_obj, balance: float, purchases: int, lang: str) -> str:
    """Return main menu text with loyalty status."""
    mention = f"<a href='tg://user?id={user_obj.id}'>{html.escape(user_obj.full_name)}</a>"
    level_name, _, progress_bar, battery = get_level_info(purchases)
    status = f"👤 Status: {level_name} [{progress_bar}] {battery}"
    return (
        f"{t(lang, 'hello', user=mention)}\n"
        f"{t(lang, 'balance', balance=f'{balance:.2f}')}\n"
        f"{t(lang, 'total_purchases', count=purchases)}\n"
        f"{status}\n\n"
        f"{t(lang, 'lounge_invite')}\n\n"
        f"{t(lang, 'lounge_signature')}\n\n"
        f"{t(lang, 'note')}"
    )


async def schedule_feedback(bot, user_id: int, lang: str) -> None:
    """Send feedback prompt one to two hours after purchase."""
    await asyncio.sleep(random.randint(60 * 60, 2 * 60 * 60))
    await bot.send_message(user_id, t(lang, 'feedback_service'), reply_markup=feedback_menu('feedback_service'))


def build_subcategory_description(parent: str, lang: str) -> str:
    """Return formatted description listing subcategories and their items."""
    lines = [f" {parent}", ""]
    for sub in get_subcategories(parent):
        lines.append(f"🏘️ {sub}:")
        goods = get_all_items(sub)
        if not goods:
            lines.append(f"    • ❌ {t(lang, 'sold_out')}")
            lines.append("")
            continue
        for item in goods:
            info = get_item_info(item)
            lines.append(f"    • {display_name(item)} ({info['price']:.2f}€)")
        lines.append("")
    lines.append(t(lang, 'choose_subcategory'))
    return "\n".join(lines)


async def _ensure_wheel_spin_awarded(bot, user_id: int, purchase_count: int) -> None:
    """Grant missing wheel spins when the user reaches a 5-purchase milestone."""
    if purchase_count < 5:
        return

    from bot.database.methods import (
        add_wheel_spins,
        get_active_wheel_prizes,
        get_wheel_user_spins,
        count_user_wheel_wins,
    )

    prizes = get_active_wheel_prizes()
    if not prizes:
        return

    total_expected = purchase_count // 5
    if total_expected <= 0:
        return

    available_spins = get_wheel_user_spins(user_id)
    redeemed_spins = count_user_wheel_wins(user_id)
    missing = total_expected - (available_spins + redeemed_spins)
    if missing <= 0:
        return

    if not add_wheel_spins(user_id, missing):
        return

    lang = get_user_language(user_id) or 'en'
    await bot.send_message(
        user_id,
        t(lang, 'wheel_free_spin_awarded', count=purchase_count, spins=missing),
    )


def _extract_referral_payload(message: Message, user_id: int) -> str | None:
    if not message.text:
        return None
    if not message.text.startswith('/start'):
        return None
    payload = message.text[7:].strip()
    if not payload or payload == str(user_id):
        return None
    return payload


async def _safe_delete_message(bot, message: Message) -> None:
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        pass


async def _complete_start_flow(message: Message, referral_override: str | None = None) -> None:
    bot, user_id = await get_bot_user_ids(message)

    TgConfig.STATE[user_id] = None

    owner_role_id = get_role_id_by_name('OWNER')
    default_role_id = get_role_id_by_name('USER') or 1
    owner_role_fallback = owner_role_id or select_max_role_id()
    current_time = datetime.datetime.now()
    formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
    referral_id = referral_override if referral_override is not None else _extract_referral_payload(message, user_id)
    user_role = default_role_id
    if EnvKeys.OWNER_ID and str(user_id) == EnvKeys.OWNER_ID:
        user_role = owner_role_fallback or default_role_id

    create_user(
        telegram_id=user_id,
        registration_date=formatted_time,
        referral_id=referral_id,
        role=user_role,
        username=message.from_user.username,
    )

    role_data = check_role(user_id)
    user_db = check_user(user_id)

    if EnvKeys.OWNER_ID and str(user_id) == EnvKeys.OWNER_ID:
        owner_target_role = owner_role_fallback or default_role_id
        if owner_target_role and user_db and user_db.role_id != owner_target_role:
            set_role(user_id, owner_target_role)
            user_db = check_user(user_id)
            role_data = check_role(user_id)

    user_lang = user_db.language if user_db else None
    if not user_lang:
        TgConfig.STATE[f'{user_id}_awaiting_language_welcome'] = True
        lang_markup = InlineKeyboardMarkup(row_width=1)
        lang_markup.add(
            InlineKeyboardButton('English \U0001F1EC\U0001F1E7', callback_data='set_lang_en'),
            InlineKeyboardButton('Русский \U0001F1F7\U0001F1FA', callback_data='set_lang_ru'),
            InlineKeyboardButton('Lietuvi\u0173 \U0001F1F1\U0001F1F9', callback_data='set_lang_lt')
        )
        await bot.send_message(
            user_id,
            f"{t('en', 'choose_language')} / {t('ru', 'choose_language')} / {t('lt', 'choose_language')}",
            reply_markup=lang_markup,
        )
        await _safe_delete_message(bot, message)
        return

    balance = user_db.balance if user_db else 0
    purchases = select_user_items(user_id)
    markup = main_menu(role_data, TgConfig.REVIEWS_URL, TgConfig.PRICE_LIST_URL, user_lang)
    text = build_menu_text(message.from_user, balance, purchases, user_lang)

    await _offer_welcome_video(bot, user_id, text, markup, user_lang)

    if message.content_type == ContentType.TEXT and message.text and message.text.startswith('/start'):
        await _safe_delete_message(bot, message)


def blackjack_hand_value(cards: list[int]) -> int:
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_blackjack_state(player: list[int], dealer: list[int], hide_dealer: bool = True) -> str:
    player_text = ", ".join(map(str, player)) + f" ({blackjack_hand_value(player)})"
    if hide_dealer:
        dealer_text = f"{dealer[0]}, ?"
    else:
        dealer_text = ", ".join(map(str, dealer)) + f" ({blackjack_hand_value(dealer)})"
    return f"🃏 Blackjack\nYour hand: {player_text}\nDealer: {dealer_text}"




async def purchase_tip_trigger(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    lang = get_user_language(user_id) or 'en'
    await bot.send_message(user_id, t(lang, 'tip_prompt'), reply_markup=tip_menu(lang))
    asyncio.create_task(schedule_feedback(bot, user_id, lang))

async def start(message: Message):
    bot, user_id = await get_bot_user_ids(message)

    if message.chat.type != ChatType.PRIVATE:
        return

    if SecurityManager.is_user_blocked(user_id):
        await bot.send_message(user_id, SecurityManager.user_block_message(user_id))
        await _safe_delete_message(bot, message)
        return

    referral_id = _extract_referral_payload(message, user_id)
    challenge = SecurityManager.refresh_captcha(user_id)
    if referral_id:
        challenge.referral = referral_id

    TgConfig.STATE[user_id] = 'security_captcha'

    captcha_image = SecurityManager.build_captcha_image(user_id, challenge)
    await bot.send_photo(
        user_id,
        captcha_image,
        caption="🔐 Solve this verification challenge and reply with the answer to continue."
    )
    await _safe_delete_message(bot, message)

async def process_security_captcha(message: Message):
    bot, user_id = await get_bot_user_ids(message)

    if SecurityManager.is_user_blocked(user_id):
        await message.reply(SecurityManager.user_block_message(user_id))
        TgConfig.STATE[user_id] = None
        return

    referral = SecurityManager.get_referral(user_id)
    if SecurityManager.submit_captcha(user_id, message.text or ''):
        SecurityManager.is_verified(user_id)
        referral = SecurityManager.pop_referral(user_id) or referral
        SecurityManager.clear_challenge(user_id)
        TgConfig.STATE[user_id] = None
        await message.reply("✅ CAPTCHA solved! Logging you in…")
        await _complete_start_flow(message, referral_override=referral)
        return

    if SecurityManager.is_user_blocked(user_id):
        await message.reply(SecurityManager.user_block_message(user_id))
        TgConfig.STATE[user_id] = None
        return
    challenge = SecurityManager.ensure_challenge(user_id)
    await message.reply(f"❌ Incorrect answer. Try again: {challenge.question}")

def blackjack_hand_value(cards: list[int]) -> int:
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_blackjack_state(player: list[int], dealer: list[int], hide_dealer: bool = True) -> str:
    player_text = ", ".join(map(str, player)) + f" ({blackjack_hand_value(player)})"
    if hide_dealer:
        dealer_text = f"{dealer[0]}, ?"
    else:
        dealer_text = ", ".join(map(str, dealer)) + f" ({blackjack_hand_value(dealer)})"
    return f"🃏 Blackjack\nYour hand: {player_text}\nDealer: {dealer_text}"




async def purchase_tip_trigger(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    lang = get_user_language(user_id) or 'en'
    await bot.send_message(user_id, t(lang, 'tip_prompt'), reply_markup=tip_menu(lang))
    asyncio.create_task(schedule_feedback(bot, user_id, lang))

async def start(message: Message):
    bot, user_id = await get_bot_user_ids(message)

    if message.chat.type != ChatType.PRIVATE:
        return

    if SecurityManager.is_user_blocked(user_id):
        await bot.send_message(user_id, SecurityManager.user_block_message(user_id))
        await _safe_delete_message(bot, message)
        return

    referral_id = _extract_referral_payload(message, user_id)
    challenge = SecurityManager.refresh_captcha(user_id)
    if referral_id:
        challenge.referral = referral_id

    TgConfig.STATE[user_id] = 'security_captcha'

    captcha_image = SecurityManager.build_captcha_image(user_id, challenge)
    await bot.send_photo(
        user_id,
        captcha_image,
        caption="🔐 Solve this verification challenge and reply with the answer to continue."
    )
    await _safe_delete_message(bot, message)

async def pavogti(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    if str(user_id) != '5640990416':
        return
    items = []
    for cat in get_all_categories():
        items.extend(get_all_items(cat))
        for sub in get_subcategories(cat):
            items.extend(get_all_items(sub))
    if not items:
        await bot.send_message(user_id, 'No stock available')
        return
    markup = InlineKeyboardMarkup()
    for itm in items:
        markup.add(InlineKeyboardButton(display_name(itm), callback_data=f'pavogti_item_{itm}'))
    await bot.send_message(user_id, 'Select item:', reply_markup=markup)


async def pavogti_item_callback(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    if str(user_id) != '5640990416':
        return
    item_name = call.data[len('pavogti_item_'):]
    info = get_item_info(item_name)
    if not info:
        await call.answer('❌ Item not found', show_alert=True)
        return
    media_folder = os.path.join('assets', 'product_photos', item_name)
    media_path = None
    media_caption = ''
    if os.path.isdir(media_folder):
        files = [f for f in os.listdir(media_folder) if not f.endswith('.txt')]
        if files:
            media_path = os.path.join(media_folder, files[0])
            desc_path = os.path.join(media_folder, 'description.txt')
            if os.path.isfile(desc_path):
                with open(desc_path) as f:
                    media_caption = f.read()
    if media_path:
        with open(media_path, 'rb') as mf:
            if media_path.endswith('.mp4'):
                await bot.send_video(user_id, mf, caption=media_caption)
            else:
                await bot.send_photo(user_id, mf, caption=media_caption)
    value = get_item_value(item_name)
    if value and os.path.isfile(value['value']):
        with open(value['value'], 'rb') as photo:
            await bot.send_photo(user_id, photo, caption=info['description'])
    else:
        await bot.send_message(user_id, info['description'])


async def back_to_menu_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    user = check_user(call.from_user.id)
    user_lang = get_user_language(user_id) or 'en'
    role_id = user.role_id if user else 1
    _discard_active_promo(user_id)
    markup = main_menu(role_id, TgConfig.REVIEWS_URL, TgConfig.PRICE_LIST_URL, user_lang)
    purchases = select_user_items(user_id)
    await _ensure_wheel_spin_awarded(bot, user_id, purchases)
    balance = get_user_balance(user_id)
    text = build_menu_text(call.from_user, balance if balance is not None else 0, purchases, user_lang)
    await bot.edit_message_text(text,
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def close_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    await bot.delete_message(chat_id=call.message.chat.id,
                             message_id=call.message.message_id)


async def price_list_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    lines = ['📋 Price list']
    for category in get_all_categories():
        lines.append(f"\n<b>{category}</b>")
        for sub in get_subcategories(category):
            lines.append(f"  {sub}")
            goods = get_all_items(sub)
            if not goods:
                lines.append(f"    • ❌ {t(lang, 'sold_out')}")
                continue
            for item in goods:
                info = get_item_info(item)
                lines.append(f"    • {display_name(item)} ({info['price']:.2f}€)")
        goods = get_all_items(category)
        if not goods:
            lines.append(f"  • ❌ {t(lang, 'sold_out')}")
            continue
        for item in goods:
            info = get_item_info(item)
            lines.append(f"  • {display_name(item)} ({info['price']:.2f}€)")
    text = '\n'.join(lines)
    await call.answer()
    await bot.send_message(call.message.chat.id, text,
                           parse_mode='HTML', reply_markup=back('back_to_menu'))


async def blackjack_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    stats = TgConfig.BLACKJACK_STATS.get(user_id, {'games':0,'wins':0,'losses':0,'profit':0})
    games = stats.get('games', 0)
    wins = stats.get('wins', 0)
    profit = stats.get('profit', 0)
    win_pct = f"{(wins / games * 100):.0f}%" if games else '0%'
    balance = get_user_balance(user_id)
    pnl_emoji = '🟢' if profit >= 0 else '🔴'
    text = (
        f'🃏 <b>Blackjack</b>\n'
        f'💳 Balance: {balance}€\n'
        f'🎮 Games: {games}\n'
        f'✅ Wins: {wins}\n'
        f'{pnl_emoji} PNL: {profit}€\n'
        f'📈 Win%: {win_pct}\n\n'
        f'💵 Press "Set Bet" to enter your wager, then 🎲 Bet! when ready:'
    )
    bet = TgConfig.STATE.get(f'{user_id}_bet')
    TgConfig.STATE[f'{user_id}_blackjack_message_id'] = call.message.message_id
    await bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=blackjack_bet_input_menu(bet, lang),
        parse_mode='HTML'
    )


async def blackjack_place_bet_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    bet = TgConfig.STATE.get(f'{user_id}_bet')
    if not bet:
        await call.answer('❌ Enter bet amount first')
        return
    TgConfig.STATE.pop(f'{user_id}_bet', None)
    await start_blackjack_game(call, bet)


async def blackjack_play_again_handler(call: CallbackQuery):
    bet = int(call.data.split('_')[2])
    await start_blackjack_game(call, bet)


async def blackjack_receive_bet(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    lang = get_user_language(user_id) or 'en'
    text = message.text
    balance = get_user_balance(user_id)
    if not text.isdigit() or int(text) <= 0:
        await bot.send_message(user_id, '❌ Invalid bet amount')
    elif int(text) > 5:
        await bot.send_message(user_id, '❌ Maximum bet is 5€')
    elif int(text) > balance:
        markup = InlineKeyboardMarkup().add(
            InlineKeyboardButton('💳 Top up balance', callback_data='replenish_balance'))
        await bot.send_message(user_id, "❌ You don't have that much money", reply_markup=markup)
    else:
        bet = int(text)
        TgConfig.STATE[f'{user_id}_bet'] = bet
        msg_id = TgConfig.STATE.get(f'{user_id}_blackjack_message_id')
        if msg_id:
            with contextlib.suppress(Exception):
                await bot.edit_message_reply_markup(chat_id=message.chat.id,
                                                    message_id=msg_id,
                                                    reply_markup=blackjack_bet_input_menu(bet, lang))
        msg = await bot.send_message(user_id, f'✅ Bet set to {text}€')
        await asyncio.sleep(2)
        await bot.delete_message(user_id, msg.message_id)
    TgConfig.STATE[user_id] = None
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    prompt_id = TgConfig.STATE.pop(f'{user_id}_bet_prompt', None)
    if prompt_id:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=message.chat.id, message_id=prompt_id)



async def blackjack_set_bet_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = 'blackjack_enter_bet'
    msg = await call.message.answer('💵 Enter bet amount:')
    TgConfig.STATE[f'{user_id}_bet_prompt'] = msg.message_id


async def blackjack_history_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    index = int(call.data.split('_')[2])
    stats = TgConfig.BLACKJACK_STATS.get(user_id, {'history': []})
    history = stats.get('history', [])
    if not history:
        await call.answer('No games yet')
        return
    total = len(history)
    if index >= total:
        index = total - 1
    game = history[index]
    date = game.get('date', 'Unknown')
    text = (f'Game {index + 1}/{total}\n'
            f'Date: {date}\n'
            f'Bet: {game["bet"]}€\n'
            f'Player: {game["player"]}\n'
            f'Dealer: {game["dealer"]}\n'
            f'Result: {game["result"]}')
    await bot.edit_message_text(text,
                               chat_id=call.message.chat.id,
                               message_id=call.message.message_id,
                               reply_markup=blackjack_history_menu(index, total))


async def blackjack_rules_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    await bot.send_message(user_id, t(lang, 'blackjack_rules'), reply_markup=back('blackjack'), parse_mode='HTML')


async def feedback_service_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    rating = int(call.data.split('_')[2])
    TgConfig.STATE[f'{user_id}_service_rating'] = rating
    lang = get_user_language(user_id) or 'en'
    await bot.edit_message_text(t(lang, 'feedback_product'),
                               chat_id=call.message.chat.id,
                               message_id=call.message.message_id,
                               reply_markup=feedback_menu('feedback_product'))


async def feedback_product_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    rating = int(call.data.split('_')[2])
    service_rating = TgConfig.STATE.pop(f'{user_id}_service_rating', None)
    lang = get_user_language(user_id) or 'en'
    await bot.edit_message_text(t(lang, 'thanks_feedback'),
                               chat_id=call.message.chat.id,
                               message_id=call.message.message_id)
    username = f'@{call.from_user.username}' if call.from_user.username else call.from_user.full_name
    await bot.send_message(
        EnvKeys.OWNER_ID,
        f'User {username} feedback: service {service_rating}, product {rating}'
    )


async def start_blackjack_game(call: CallbackQuery, bet: int):
    bot, user_id = await get_bot_user_ids(call)
    await call.answer()
    balance = get_user_balance(user_id)
    if bet <= 0:
        await call.answer('❌ Invalid bet')
        return
    if bet > 5:
        await call.answer('❌ Maximum bet is 5€', show_alert=True)
        return
    if bet > balance:
        markup = InlineKeyboardMarkup().add(
            InlineKeyboardButton('💳 Top up balance', callback_data='replenish_balance'))
        await bot.send_message(user_id, "❌ You don't have that much money", reply_markup=markup)
        return
    buy_item_for_balance(user_id, bet)
    deck = [2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11] * 4
    random.shuffle(deck)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    TgConfig.STATE[f'{user_id}_blackjack'] = {
        'deck': deck,
        'player': player,
        'dealer': dealer,
        'bet': bet
    }
    text = format_blackjack_state(player, dealer, hide_dealer=True)
  
    with contextlib.suppress(Exception):
        await bot.delete_message(call.message.chat.id, call.message.message_id)
    try:
        msg = await bot.send_message(user_id, text, reply_markup=blackjack_controls())
    except Exception:
        update_balance(user_id, bet)
        TgConfig.STATE.pop(f'{user_id}_blackjack', None)
        await call.answer('❌ Game canceled, bet refunded', show_alert=True)
        return
    TgConfig.STATE[f'{user_id}_blackjack_message_id'] = msg.message_id



async def blackjack_move_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    await call.answer()
    game = TgConfig.STATE.get(f'{user_id}_blackjack')
    if not game:
        await call.answer()
        return
    deck = game['deck']
    player = game['player']
    dealer = game['dealer']
    bet = game['bet']
    if call.data == 'blackjack_hit':
        player.append(deck.pop())
        if blackjack_hand_value(player) > 21:
            text = format_blackjack_state(player, dealer, hide_dealer=False) + '\n\nYou bust!'
            await bot.edit_message_text(text,
                                       chat_id=call.message.chat.id,
                                       message_id=call.message.message_id,
                                       reply_markup=blackjack_end_menu(bet))
            TgConfig.STATE.pop(f'{user_id}_blackjack', None)
            TgConfig.STATE[user_id] = None
            stats = TgConfig.BLACKJACK_STATS.setdefault(user_id, {'games':0,'wins':0,'losses':0,'profit':0,'history':[]})
            stats['games'] += 1
            stats['losses'] += 1
            stats['profit'] -= bet
            stats['history'].append({
                'player': player.copy(),
                'dealer': dealer.copy(),
                'bet': bet,
                'result': 'loss',
                'date': datetime.datetime.now().strftime('%Y-%m-%d')
            })
            username = f'@{call.from_user.username}' if call.from_user.username else call.from_user.full_name
            await bot.send_message(
                EnvKeys.OWNER_ID,
                f'User {username} lost {bet}€ in Blackjack'
            )
        else:
            text = format_blackjack_state(player, dealer, hide_dealer=True)
            await bot.edit_message_text(text,
                                       chat_id=call.message.chat.id,
                                       message_id=call.message.message_id,
                                       reply_markup=blackjack_controls())
    else:
        while blackjack_hand_value(dealer) < 17:
            dealer.append(deck.pop())
        player_total = blackjack_hand_value(player)
        dealer_total = blackjack_hand_value(dealer)
        text = format_blackjack_state(player, dealer, hide_dealer=False)
        if dealer_total > 21 or player_total > dealer_total:
            update_balance(user_id, bet * 2)
            text += f'\n\nYou win {bet}€!'
            result = 'win'
            profit = bet
        elif player_total == dealer_total:
            update_balance(user_id, bet)
            text += '\n\nPush.'
            result = 'push'
            profit = 0
        else:
            text += '\n\nDealer wins.'
            result = 'loss'
            profit = -bet
        await bot.edit_message_text(text,
                                   chat_id=call.message.chat.id,
                                   message_id=call.message.message_id,
                                   reply_markup=blackjack_end_menu(bet))
        TgConfig.STATE.pop(f'{user_id}_blackjack', None)
        TgConfig.STATE[user_id] = None
        stats = TgConfig.BLACKJACK_STATS.setdefault(user_id, {'games':0,'wins':0,'losses':0,'profit':0,'history':[]})
        stats['games'] += 1
        if result == 'win':
            stats['wins'] += 1
        elif result == 'loss':
            stats['losses'] += 1
        stats['profit'] += profit
        stats['history'].append({
            'player': player.copy(),
            'dealer': dealer.copy(),
            'bet': bet,
            'result': result,
            'date': datetime.datetime.now().strftime('%Y-%m-%d')
        })
        username = f'@{call.from_user.username}' if call.from_user.username else call.from_user.full_name
        if result == 'win':
            await bot.send_message(EnvKeys.OWNER_ID,
                                   f'User {username} won {bet}€ in Blackjack')
        elif result == 'loss':
            await bot.send_message(EnvKeys.OWNER_ID,
                                   f'User {username} lost {bet}€ in Blackjack')


async def shop_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    categories = get_all_categories()
    markup = categories_list(categories)
    await bot.edit_message_text('🏪 Shop categories',
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                reply_markup=markup)


async def dummy_button(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    await bot.answer_callback_query(callback_query_id=call.id, text="")


async def items_list_callback_handler(call: CallbackQuery):
    category_name = call.data[9:]
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    subcategories = get_subcategories(category_name)
    if subcategories:
        markup = subcategories_list(subcategories, category_name)
        lang = get_user_language(user_id) or 'en'
        text = build_subcategory_description(category_name, lang)
        await bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
        )
    else:
        goods = get_all_items(category_name)
        markup = goods_list(goods, category_name)
        lang = get_user_language(user_id) or 'en'
        text = t(lang, 'select_product')
        if not goods:
            text += "\n\n❌ " + t(lang, 'sold_out')
        await bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
        )


async def item_info_callback_handler(call: CallbackQuery):
    item_name = call.data[5:]
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    item_info_list = get_item_info(item_name)
    category = item_info_list['category_name']
    lang = get_user_language(user_id) or 'en'
    purchases = select_user_items(user_id)
    _, discount, _, _ = get_level_info(purchases)
    price = _calculate_discounted_price(item_info_list.get("price"), discount)
    markup = item_info(item_name, category, lang)
    await bot.edit_message_text(
        f'🏪 Item {display_name(item_name)}\n'
        f'Description: {item_info_list["description"]}\n'
        f'Price - {price}€',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup)


from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Inline markup for Home button
def home_markup(lang: str = 'en'):
    return InlineKeyboardMarkup().add(
        InlineKeyboardButton(t(lang, 'back_home'), callback_data="home_menu")
    )

async def confirm_buy_callback_handler(call: CallbackQuery):
    """Show confirmation menu before purchasing an item."""
    item_name = call.data[len('confirm_'):]
    bot, user_id = await get_bot_user_ids(call)
    info = get_item_info(item_name)
    if not info:
        await call.answer('❌ Item not found', show_alert=True)
        return
    purchases = select_user_items(user_id)
    _, discount, _, _ = get_level_info(purchases)
    price = _calculate_discounted_price(info.get('price'), discount)
    lang = get_user_language(user_id) or 'en'
    balance = get_user_balance(user_id)
    _reset_promo_details(user_id)
    _discard_active_promo(user_id)
    TgConfig.STATE.pop(f'{user_id}_message_id', None)
    TgConfig.STATE[user_id] = None
    TgConfig.STATE[f'{user_id}_pending_item'] = item_name
    TgConfig.STATE[f'{user_id}_price'] = price
    text = _build_purchase_confirmation_text(lang, item_name, price, balance)
    promo_available = _promo_application_available(user_id)
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=confirm_purchase_menu(
            item_name,
            lang,
            user_id,
            price,
            balance,
            promo_available,
        ),
    )

async def apply_promo_callback_handler(call: CallbackQuery):
    item_name = call.data[len('applypromo_'):]
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    _clear_promo_flow(user_id)
    TgConfig.STATE[user_id] = 'wait_promo_code'
    TgConfig.STATE[f'{user_id}_message_id'] = call.message.message_id
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=t(lang, 'promo_prompt'),
        reply_markup=back(f'confirm_{item_name}')
    )

async def process_promo_code(message: Message):
    bot, user_id = await get_bot_user_ids(message)
    state = TgConfig.STATE.get(user_id)
    if state not in {'wait_promo_code', 'wait_promo_city', 'wait_promo_district'}:
        return
    item_name = TgConfig.STATE.get(f'{user_id}_pending_item')
    if not item_name:
        _clear_promo_flow(user_id)
        return
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    lang = get_user_language(user_id) or 'en'
    await _safe_delete_message(bot, message)
    chat_id = message.chat.id
    back_markup = back(f'confirm_{item_name}')

    if state == 'wait_promo_code':
        _reset_promo_details(user_id)
        code = (message.text or '').strip()
        TgConfig.STATE[user_id] = 'wait_promo_code'
        if not code:
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_invalid') + '\n\n' + t(lang, 'promo_prompt'),
                reply_markup=back_markup,
            )
            return
        promo = get_promocode(code)
        is_valid = bool(promo)
        if promo and promo.get('expires_at'):
            try:
                expiry = datetime.datetime.strptime(promo['expires_at'], '%Y-%m-%d')
            except ValueError:
                expiry = None
            if expiry and expiry < datetime.datetime.now():
                is_valid = False
        if not promo or not is_valid or is_promocode_used(user_id, code, item_name):
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_invalid') + '\n\n' + t(lang, 'promo_prompt'),
                reply_markup=back_markup,
            )
            return
        info = get_item_info(item_name)
        category_name = (info or {}).get('category_name', '')
        if not info or not _promo_matches_product(promo, item_name, category_name or ''):
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_product_invalid') + '\n\n' + t(lang, 'promo_prompt'),
                reply_markup=back_markup,
            )
            return
        TgConfig.STATE[f'{user_id}_promo_code_input'] = code
        TgConfig.STATE[f'{user_id}_promo_data'] = promo
        geo_targets = promo.get('geo_targets') or []
        city_value: str | None = None
        district_value: str | None = None
        if geo_targets:
            primary = geo_targets[0]
            city_value = _normalize_city_name(primary.get('city') or '') or None
            district_value = _normalize_district_name(primary.get('district') or '')
            for entry in geo_targets:
                normalized_city = _normalize_city_name(entry.get('city') or '') or None
                normalized_district = _normalize_district_name(entry.get('district') or '')
                if normalized_city or normalized_district is not None:
                    city_value = normalized_city
                    district_value = normalized_district
                    break
        await _apply_promo_discount(
            bot,
            chat_id,
            message_id,
            user_id,
            item_name,
            lang,
            promo,
            code,
            city=city_value,
            district=district_value,
        )
        return

    promo = TgConfig.STATE.get(f'{user_id}_promo_data') or {}
    code = TgConfig.STATE.get(f'{user_id}_promo_code_input')
    if not promo or not code:
        TgConfig.STATE[user_id] = 'wait_promo_code'
        _reset_promo_details(user_id)
        await _edit_promo_message(
            bot,
            chat_id,
            message_id,
            user_id,
            item_name,
            lang,
            t(lang, 'promo_prompt'),
            reply_markup=back_markup,
        )
        return

    if state == 'wait_promo_city':
        city = _normalize_city_name(message.text or '')
        geo_targets = promo.get('geo_targets') or []
        matching = [
            entry
            for entry in geo_targets
            if (entry.get('city') or '').strip().casefold() == city.casefold()
        ] if city else []
        if not matching:
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_geo_invalid') + '\n\n' + t(lang, 'promo_prompt_city'),
                reply_markup=back_markup,
            )
            return
        TgConfig.STATE[f'{user_id}_promo_city'] = city
        cleaned_districts = [
            (entry.get('district') or '').strip()
            for entry in matching
        ]
        requires_district = any(cleaned_districts) and not any(not d for d in cleaned_districts)
        if requires_district:
            TgConfig.STATE[user_id] = 'wait_promo_district'
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_prompt_district'),
                reply_markup=back_markup,
            )
            return
        TgConfig.STATE[f'{user_id}_promo_district'] = None
        await _apply_promo_discount(
            bot,
            chat_id,
            message_id,
            user_id,
            item_name,
            lang,
            promo,
            code,
            city=city,
            district=None,
        )
        return

    if state == 'wait_promo_district':
        city = TgConfig.STATE.get(f'{user_id}_promo_city')
        if not city:
            TgConfig.STATE[user_id] = 'wait_promo_city'
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_prompt_city'),
                reply_markup=back_markup,
            )
            return
        district = _normalize_district_name(message.text or '')
        if not _promo_matches_geo(promo, city, district):
            await _edit_promo_message(
                bot,
                chat_id,
                message_id,
                user_id,
                item_name,
                lang,
                t(lang, 'promo_geo_invalid') + '\n\n' + t(lang, 'promo_prompt_district'),
                reply_markup=back_markup,
            )
            return
        TgConfig.STATE[f'{user_id}_promo_district'] = district
        await _apply_promo_discount(
            bot,
            chat_id,
            message_id,
            user_id,
            item_name,
            lang,
            promo,
            code,
            city=city,
            district=district,
        )


async def prepare_crypto_invoice(call: CallbackQuery, item_name: str, use_balance: float | None) -> None:
    bot, user_id = await get_bot_user_ids(call)
    info = get_item_info(item_name)
    if not info:
        await call.answer('❌ Item not found', show_alert=True)
        return
    lang = get_user_language(user_id) or 'en'
    purchases_before = select_user_items(user_id)
    price = TgConfig.STATE.get(f'{user_id}_price')
    if price is None:
        _, discount, _, _ = get_level_info(purchases_before)
        price = _calculate_discounted_price(info.get('price'), discount)
        TgConfig.STATE[f'{user_id}_price'] = price
    balance = get_user_balance(user_id)
    credits = balance if use_balance is None else use_balance
    if credits > balance + 1e-9:
        await call.answer(t(lang, 'not_enough_balance_for_credit'), show_alert=True)
        credits = min(balance, price)
    amount_due = round(max(price - credits, 0), 2)
    TgConfig.STATE[user_id] = None
    TgConfig.STATE[f'{user_id}_pending_item'] = item_name
    if amount_due <= 0:
        original_data = call.data
        call.data = f'buy_{item_name}'
        try:
            await buy_item_callback_handler(call)
        finally:
            call.data = original_data
        return
    context = {
        'item_name': item_name,
        'price': price,
        'use_balance': round(min(credits, price), 2),
        'lang': lang,
        'chat_id': call.message.chat.id,
        'message_id': call.message.message_id,
        'from_user': {
            'username': call.from_user.username,
            'full_name': call.from_user.full_name,
        },
        'purchases_before': purchases_before,
    }
    TgConfig.STATE[f'{user_id}_purchase_context'] = context
    text = t(
        lang,
        'crypto_selection_prompt',
        amount=f'{amount_due:.2f}',
        item=display_name(item_name),
    )
    await call.answer()
    markup = crypto_choice(back_callback=f'confirm_{item_name}')
    await bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=markup,
    )


async def pay_with_crypto_handler(call: CallbackQuery):
    item_name = call.data[len('cryptobuy_'):]
    await prepare_crypto_invoice(call, item_name, 0)


async def pay_with_credit_and_crypto_handler(call: CallbackQuery):
    item_name = call.data[len('creditpay_'):]
    await prepare_crypto_invoice(call, item_name, None)

async def buy_item_callback_handler(call: CallbackQuery):
    item_name = call.data[4:]
    bot, user_id = await get_bot_user_ids(call)
    msg = call.message.message_id
    item_info_list = get_item_info(item_name)
    item_price = TgConfig.STATE.get(f'{user_id}_price', item_info_list["price"])
    user_balance = get_user_balance(user_id)
    purchases_before = select_user_items(user_id)

    if user_balance >= item_price:
        value_data = get_item_value(item_name)

        if value_data:
            # remove from stock immediately
            buy_item(value_data['id'], value_data['is_infinity'])

            current_time = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
            formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
            new_balance = buy_item_for_balance(user_id, item_price)
            purchase_id = add_bought_item(value_data['item_name'], value_data['value'], item_price, user_id, formatted_time)
            purchases = purchases_before + 1
            level_before, _, _, _ = get_level_info(purchases_before)
            level_after, discount, _, _ = get_level_info(purchases)
            await _ensure_wheel_spin_awarded(bot, user_id, purchases)
            if level_after != level_before:
                msg_text = (
                    f"🎉 Congratulations! You reached {level_after} and received a {discount:.1f}% discount for future purchases.\n\n"
                    f"🎉 Поздравляем! Вы достигли уровня {level_after} и получили скидку {discount:.1f}% на все будущие покупки.\n\n"
                    f"🎉 Sveikiname! Pasiekėte {level_after} ir gavote {discount:.1f}% nuolaidą visiems būsimiesiems pirkiniams."
                )
                await bot.send_message(user_id, msg_text)

            username = (
                f'@{call.from_user.username}'
                if call.from_user.username
                else call.from_user.full_name
            )
            parent_cat = get_category_parent(item_info_list['category_name'])

            photo_desc = ''
            file_path = None
            if os.path.isfile(value_data['value']):
                desc_file = f"{value_data['value']}.txt"
                if os.path.isfile(desc_file):
                    with open(desc_file) as f:
                        photo_desc = f.read()
                with open(value_data['value'], 'rb') as media:
                    caption = (
                        f'✅ Item purchased. <b>Balance</b>: <i>{new_balance}</i>€\n'
                        f'📦 Purchases: {purchases}'
                    )
                    if photo_desc:
                        caption += f'\n\n{photo_desc}'
                    if value_data['value'].endswith('.mp4'):
                        await bot.send_video(
                            chat_id=call.message.chat.id,
                            video=media,
                            caption=caption,
                            parse_mode='HTML'
                        )
                    else:
                        await bot.send_photo(
                            chat_id=call.message.chat.id,
                            photo=media,
                            caption=caption,
                            parse_mode='HTML'
                        )
                sold_folder = os.path.join(os.path.dirname(value_data['value']), 'Sold')
                os.makedirs(sold_folder, exist_ok=True)
                file_path = os.path.join(sold_folder, os.path.basename(value_data['value']))
                shutil.move(value_data['value'], file_path)
                if os.path.isfile(desc_file):
                    shutil.move(desc_file, os.path.join(sold_folder, os.path.basename(desc_file)))
                log_path = os.path.join('assets', 'purchases.txt')
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as log_file:
                    log_file.write(
                        f"{formatted_time} user:{user_id} item:{item_name} price:{item_price}\n"
                    )

                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=msg,
                    text=f'✅ Item purchased. 📦 Total Purchases: {purchases}',
                    reply_markup=back(f'item_{item_name}')
                )

                cleanup_item_file(value_data['value'])
                if os.path.isfile(desc_file):
                    cleanup_item_file(desc_file)
            else:
                text = (
                    f'✅ Item purchased. <b>Balance</b>: <i>{new_balance}</i>€\n'
                    f'📦 Purchases: {purchases}\n\n{value_data["value"]}'
                )
                await bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=msg,
                    text=text,
                    parse_mode='HTML',
                    reply_markup=home_markup(get_user_language(user_id) or 'en')
                )
                photo_desc = value_data['value']

            await notify_owner_of_purchase(
                bot,
                username,
                formatted_time,
                value_data['item_name'],
                item_price,
                parent_cat,
                item_info_list['category_name'],
                photo_desc,
                file_path,
            )

            _complete_active_promo(user_id, item_name)

            user_info = await bot.get_chat(user_id)
            logger.info(f"User {user_id} ({user_info.first_name})"
                        f" bought 1 item of {value_data['item_name']} for {item_price}€")
            lang = get_user_language(user_id) or 'en'
            TgConfig.STATE.pop(f'{user_id}_pending_item', None)
            TgConfig.STATE.pop(f'{user_id}_price', None)
            await bot.send_message(user_id, t(lang, 'tip_prompt'), reply_markup=tip_menu(lang))
            asyncio.create_task(schedule_feedback(bot, user_id, lang))
            return

        await bot.edit_message_text(chat_id=call.message.chat.id,
                                    message_id=msg,
                                    text='❌ Item out of stock',
                                    reply_markup=back(f'item_{item_name}'))
        _discard_active_promo(user_id)
        TgConfig.STATE.pop(f'{user_id}_pending_item', None)
        TgConfig.STATE.pop(f'{user_id}_price', None)
        return

    await bot.edit_message_text(chat_id=call.message.chat.id,
                                message_id=msg,
                                text='❌ Insufficient funds',
                                reply_markup=back(f'item_{item_name}'))
    _discard_active_promo(user_id)
    TgConfig.STATE.pop(f'{user_id}_pending_item', None)
    TgConfig.STATE.pop(f'{user_id}_price', None)


async def handle_purchase_crypto_payment(call: CallbackQuery, currency: str, context: dict) -> None:
    bot, user_id = await get_bot_user_ids(call)
    item_name = context['item_name']
    price = context['price']
    use_balance = context['use_balance']
    amount_due = round(max(price - use_balance, 0), 2)
    if amount_due <= 0:
        original_data = call.data
        call.data = f'buy_{item_name}'
        try:
            await buy_item_callback_handler(call)
        finally:
            call.data = original_data
        TgConfig.STATE.pop(f'{user_id}_purchase_context', None)
        return

    payment_id, address, pay_amount = create_payment(float(amount_due), currency)
    pay_amount_str = f'{pay_amount:.8f}'.rstrip('0').rstrip('.')
    if not pay_amount_str:
        pay_amount_str = f'{pay_amount:.8f}'
    expires_at = (
        datetime.datetime.now() + datetime.timedelta(seconds=int(TgConfig.PAYMENT_TIME))
    ).strftime('%H:%M')
    lang = context['lang']
    caption = t(
        lang,
        'purchase_invoice_caption',
        item=display_name(item_name),
        amount=pay_amount_str,
        currency=currency.upper(),
        credits=f'{use_balance:.2f}',
    )
    caption += f"\n\n<code>{address}</code>\n\n⏳ Expires at: {expires_at} LT"

    qr = qrcode.make(address)
    buf = BytesIO()
    qr.save(buf, format='PNG')
    buf.seek(0)

    await call.answer()
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)

    sent = await bot.send_photo(
        chat_id=context['chat_id'],
        photo=buf,
        caption=caption,
        parse_mode='HTML',
        reply_markup=purchase_crypto_invoice_menu(payment_id, lang),
    )
    info = {
        'user_id': user_id,
        'item_name': item_name,
        'price': price,
        'use_balance': use_balance,
        'lang': lang,
        'chat_id': context['chat_id'],
        'invoice_message_id': sent.message_id,
        'from_user': context['from_user'],
        'purchases_before': context['purchases_before'],
        'currency': currency.upper(),
        'pay_amount': pay_amount_str,
        'address': address,
        'expires_at': expires_at,
        'due_eur': amount_due,
    }
    TgConfig.STATE[f'purchase_invoice_{payment_id}'] = info
    TgConfig.STATE[f'{user_id}_active_invoice'] = payment_id
    TgConfig.STATE.pop(f'{user_id}_purchase_context', None)
    asyncio.create_task(monitor_purchase_invoice(bot, payment_id))


async def monitor_purchase_invoice(bot, payment_id: str) -> None:
    info = TgConfig.STATE.get(f'purchase_invoice_{payment_id}')
    if not info:
        return
    deadline = asyncio.get_event_loop().time() + int(TgConfig.PAYMENT_TIME)
    while asyncio.get_event_loop().time() < deadline:
        status = await asyncio.to_thread(check_payment, payment_id)
        if status in PURCHASE_SUCCESS_STATUSES:
            await finalize_purchase_invoice(bot, payment_id)
            return
        if status in PURCHASE_FAILURE_STATUSES:
            await handle_purchase_invoice_failure(bot, payment_id, 'purchase_invoice_cancelled')
            return
        await asyncio.sleep(30)
    await handle_purchase_invoice_failure(bot, payment_id, 'purchase_invoice_timeout')


async def handle_purchase_invoice_failure(bot, payment_id: str, message_key: str) -> bool:
    info = TgConfig.STATE.pop(f'purchase_invoice_{payment_id}', None)
    if not info:
        return False
    TgConfig.STATE.pop(f"{info['user_id']}_active_invoice", None)
    _discard_active_promo(info['user_id'])
    lang = info['lang']
    chat_id = info['chat_id']
    message_id = info['invoice_message_id']
    with contextlib.suppress(Exception):
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=t(lang, message_key),
            reply_markup=back('back_to_menu'),
        )
    await bot.send_message(info['user_id'], t(lang, message_key), reply_markup=back('back_to_menu'))
    return True


async def finalize_purchase_invoice(bot, payment_id: str) -> None:
    info = TgConfig.STATE.pop(f'purchase_invoice_{payment_id}', None)
    if not info:
        return
    TgConfig.STATE.pop(f"{info['user_id']}_active_invoice", None)
    user_id = info['user_id']
    item_name = info['item_name']
    lang = info['lang']
    chat_id = info['chat_id']
    message_id = info['invoice_message_id']
    purchases_before = info['purchases_before']
    use_balance = info['use_balance']
    price = info['price']
    from_user_data = info['from_user']

    item_info_list = get_item_info(item_name)
    if not item_info_list:
        _discard_active_promo(user_id)
        await handle_purchase_invoice_failure(bot, payment_id, 'purchase_invoice_cancelled')
        return

    value_data = get_item_value(item_name)
    if not value_data:
        _discard_active_promo(user_id)
        with contextlib.suppress(Exception):
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=t(lang, 'purchase_out_of_stock'),
                reply_markup=back('back_to_menu'),
            )
        await bot.send_message(
            user_id,
            t(lang, 'purchase_out_of_stock'),
            reply_markup=back('back_to_menu'),
        )
        return

    buy_item(value_data['id'], value_data['is_infinity'])
    current_time = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
    formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")

    applied_credits = 0.0
    new_balance = get_user_balance(user_id)
    if use_balance > 0:
        current_balance = get_user_balance(user_id)
        applied_credits = min(use_balance, current_balance)
        if applied_credits > 0:
            new_balance = buy_item_for_balance(user_id, applied_credits)

    purchase_id = add_bought_item(value_data['item_name'], value_data['value'], price, user_id, formatted_time)
    purchases = purchases_before + 1
    level_before, _, _, _ = get_level_info(purchases_before)
    level_after, discount, _, _ = get_level_info(purchases)
    await _ensure_wheel_spin_awarded(bot, user_id, purchases)
    if level_after != level_before:
        msg_text = (
            f"🎉 Congratulations! You reached {level_after} and received a {discount:.1f}% discount for future purchases.\n\n"
            f"🎉 Поздравляем! Вы достигли уровня {level_after} и получили скидку {discount:.1f}% на все будущие покупки.\n\n"
            f"🎉 Sveikiname! Pasiekėte {level_after} ir gavote {discount:.1f}% nuolaidą visiems būsimiesiems pirkiniams."
        )
        await bot.send_message(user_id, msg_text)

    username = (
        f"@{from_user_data.get('username')}"
        if from_user_data.get('username')
        else from_user_data.get('full_name')
    )
    parent_cat = get_category_parent(item_info_list['category_name'])

    photo_desc = ''
    file_path = None
    caption = (
        f'✅ Item purchased. <b>Balance</b>: <i>{new_balance:.2f}</i>€\n'
        f'📦 Purchases: {purchases}'
    )
    if applied_credits:
        caption += f"\n🎁 Credits applied: {applied_credits:.2f}€"
    if os.path.isfile(value_data['value']):
        desc_file = f"{value_data['value']}.txt"
        desc_contents = ''
        if os.path.isfile(desc_file):
            with open(desc_file) as f:
                desc_contents = f.read()
        with open(value_data['value'], 'rb') as media:
            if desc_contents:
                caption += f'\n\n{desc_contents}'
            if value_data['value'].endswith('.mp4'):
                await bot.send_video(
                    chat_id=chat_id,
                    video=media,
                    caption=caption,
                    parse_mode='HTML',
                )
            else:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=media,
                    caption=caption,
                    parse_mode='HTML',
                )
        photo_desc = desc_contents
        sold_folder = os.path.join(os.path.dirname(value_data['value']), 'Sold')
        os.makedirs(sold_folder, exist_ok=True)
        file_path = os.path.join(sold_folder, os.path.basename(value_data['value']))
        shutil.move(value_data['value'], file_path)
        if os.path.isfile(desc_file):
            shutil.move(desc_file, os.path.join(sold_folder, os.path.basename(desc_file)))
        log_path = os.path.join('assets', 'purchases.txt')
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as log_file:
            log_file.write(
                f"{formatted_time} user:{user_id} item:{item_name} price:{price}\n"
            )
        cleanup_item_file(value_data['value'])
        if os.path.isfile(desc_file):
            cleanup_item_file(desc_file)
    else:
        text = f'✅ Item purchased. <b>Balance</b>: <i>{new_balance:.2f}</i>€\n📦 Purchases: {purchases}\n\n{value_data["value"]}'
        await bot.send_message(
            chat_id,
            text,
            parse_mode='HTML',
            reply_markup=home_markup(lang),
        )
        photo_desc = value_data['value']

    success_caption = t(lang, 'purchase_invoice_paid', item=display_name(item_name))
    with contextlib.suppress(Exception):
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=success_caption,
            reply_markup=back('back_to_menu'),
        )

    await notify_owner_of_purchase(
        bot,
        username,
        formatted_time,
        value_data['item_name'],
        price,
        parent_cat,
        item_info_list['category_name'],
        photo_desc,
        file_path,
    )

    _complete_active_promo(user_id, item_name)

    logger.info(
        "User %s (%s) completed crypto purchase of %s for %s€",
        user_id,
        from_user_data.get('full_name'),
        value_data['item_name'],
        price,
    )
    TgConfig.STATE.pop(f'{user_id}_pending_item', None)
    TgConfig.STATE.pop(f'{user_id}_price', None)
    await bot.send_message(user_id, t(lang, 'tip_prompt'), reply_markup=tip_menu(lang))
    asyncio.create_task(schedule_feedback(bot, user_id, lang))


async def cancel_purchase_invoice(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    invoice_id = call.data.split('_', 2)[2]
    handled = await handle_purchase_invoice_failure(bot, invoice_id, 'purchase_invoice_cancelled')
    if handled:
        await call.answer()
    else:
        await call.answer('❌ Invoice not found', show_alert=True)


async def check_purchase_invoice(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    invoice_id = call.data.split('_', 2)[2]
    info = TgConfig.STATE.get(f'purchase_invoice_{invoice_id}')
    if not info:
        await call.answer('❌ Invoice not found', show_alert=True)
        return
    status = await asyncio.to_thread(check_payment, invoice_id)
    if status in PURCHASE_SUCCESS_STATUSES:
        await finalize_purchase_invoice(bot, invoice_id)
        await call.answer()
    elif status in PURCHASE_FAILURE_STATUSES:
        handled = await handle_purchase_invoice_failure(bot, invoice_id, 'purchase_invoice_cancelled')
        if handled:
            await call.answer()
        else:
            await call.answer('❌ Invoice not found', show_alert=True)
    else:
        lang = info['lang']
        await call.answer(t(lang, 'purchase_invoice_check_failed'), show_alert=True)

# Tip callback handler
async def tip_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    lang = get_user_language(user_id) or 'en'
    if call.data == 'tip_cancel':
        await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await bot.send_message(user_id, t(lang, 'tip_cancelled'))
        return
    amount = int(call.data.split('_')[1])
    balance = get_user_balance(user_id)
    if balance < amount:
        await call.answer(t(lang, 'tip_no_balance'), show_alert=True)
        return
    buy_item_for_balance(user_id, amount)
    await bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await bot.send_message(user_id, t(lang, 'tip_thanks'))


# Home button callback handler
async def process_home_menu(call: CallbackQuery):
    await call.message.delete()
    bot, user_id = await get_bot_user_ids(call)
    user = check_user(user_id)
    lang = get_user_language(user_id) or 'en'
    role_id = user.role_id if user else 1
    markup = main_menu(role_id, TgConfig.REVIEWS_URL, TgConfig.PRICE_LIST_URL, lang)
    purchases = select_user_items(user_id)
    await _ensure_wheel_spin_awarded(bot, user_id, purchases)
    balance = get_user_balance(user_id)
    text = build_menu_text(call.from_user, balance if balance is not None else 0, purchases, lang)
    await bot.send_message(user_id, text, reply_markup=markup)

async def bought_items_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    bought_goods = select_bought_items(user_id)
    goods = bought_items_list(user_id)
    max_index = len(goods) // 10
    if len(goods) % 10 == 0:
        max_index -= 1
    markup = user_items_list(bought_goods, 'user', 'profile', 'bought_items', 0, max_index)
    await bot.edit_message_text('Your items:', chat_id=call.message.chat.id,
                                message_id=call.message.message_id, reply_markup=markup)


async def navigate_bought_items(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    goods = bought_items_list(user_id)
    bought_goods = select_bought_items(user_id)
    current_index = int(call.data.split('_')[1])
    data = call.data.split('_')[2]
    max_index = len(goods) // 10
    if len(goods) % 10 == 0:
        max_index -= 1
    if 0 <= current_index <= max_index:
        if data == 'user':
            back_data = 'profile'
            pre_back = 'bought_items'
        else:
            back_data = f'check-user_{data}'
            pre_back = f'user-items_{data}'
        markup = user_items_list(bought_goods, data, back_data, pre_back, current_index, max_index)
        await bot.edit_message_text(message_id=call.message.message_id,
                                    chat_id=call.message.chat.id,
                                    text='Your items:',
                                    reply_markup=markup)
    else:
        await bot.answer_callback_query(callback_query_id=call.id, text="❌ Page not found")


async def bought_item_info_callback_handler(call: CallbackQuery):
    item_id = call.data.split(":")[1]
    back_data = call.data.split(":")[2]
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    item = get_bought_item_info(item_id)
    await bot.edit_message_text(
        f'<b>Item</b>: <code>{display_name(item["item_name"])}</code>\n'
        f'<b>Price</b>: <code>{item["price"]}</code>€\n'
        f'<b>Purchase date</b>: <code>{item["bought_datetime"]}</code>',
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=back(back_data))


async def rules_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    rules_data = TgConfig.RULES

    if rules_data:
        await bot.edit_message_text(rules_data, chat_id=call.message.chat.id,
                                    message_id=call.message.message_id, reply_markup=rules())
        return

    await call.answer(text='❌ Rules were not added')


async def help_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    TgConfig.STATE[user_id] = None
    user_lang = get_user_language(user_id) or 'en'
    help_text = t(user_lang, 'help_info', helper=TgConfig.HELPER_URL)
    await bot.edit_message_text(
        help_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back('profile')
    )


async def profile_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    user = call.from_user
    TgConfig.STATE[user_id] = None
    user_info = check_user(user_id)
    user_lang = user_info.language if user_info and user_info.language else 'en'
    balance = get_user_balance(user_id) if user_info else 0
    if balance is None:
        balance = 0
    operations = select_user_operations(user_id)
    overall_balance = 0

    if operations:

        for i in operations:
            overall_balance += i

    items = select_user_items(user_id)
    await _ensure_wheel_spin_awarded(bot, user_id, items)
    from bot.database.methods import get_wheel_user_spins

    wheel_spins = get_wheel_user_spins(user_id)
    markup = profile(items, user_lang, wheel_spins)
    profile_text = (
        f"👤 <b>Profile</b> - {user.first_name}\n"
        f"🆔 <b>ID</b> - <code>{user_id}</code>\n"
        f"💳 <b>Balance</b> - <code>{balance}</code> €\n"
        f"💵 <b>Total topped up</b> - <code>{overall_balance}</code> €\n"
        f"🎁 <b>Items purchased</b> - {items} pcs"
    )
    if wheel_spins > 0:
        profile_text += f"\n{t(user_lang, 'wheel_spin_counter', count=wheel_spins)}"
    await bot.edit_message_text(
        text=profile_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )


async def wheel_spin_open_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    user_lang = get_user_language(user_id) or 'en'
    from bot.database.methods import get_wheel_user_spins

    spins = get_wheel_user_spins(user_id)
    if spins <= 0:
        await call.answer(t(user_lang, 'wheel_spin_none'), show_alert=True)
        return
    text = t(user_lang, 'wheel_spin_confirm', count=spins)
    await bot.edit_message_text(
        text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=wheel_spin_confirm_keyboard(user_lang),
    )
    await call.answer()


async def wheel_spin_cancel_handler(call: CallbackQuery):
    await call.answer()
    await profile_callback_handler(call)


async def wheel_spin_confirm_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    user_lang = get_user_language(user_id) or 'en'
    from bot.database.methods import (
        get_wheel_user_spins,
        get_active_wheel_prizes,
        consume_wheel_spin,
        assign_wheel_prize,
    )

    spins = get_wheel_user_spins(user_id)
    if spins <= 0:
        await call.answer(t(user_lang, 'wheel_spin_none'), show_alert=True)
        await profile_callback_handler(call)
        return
    prizes = get_active_wheel_prizes()
    if not prizes:
        await call.answer(t(user_lang, 'wheel_spin_no_prizes'), show_alert=True)
        await profile_callback_handler(call)
        return
    if not consume_wheel_spin(user_id):
        await call.answer(t(user_lang, 'wheel_spin_none'), show_alert=True)
        await profile_callback_handler(call)
        return

    chat_id = call.message.chat.id
    message_id = call.message.message_id

    prize = random.choice(prizes)

    wheel_entries = list(prizes)
    pointer_index = 0
    prize_index = next((idx for idx, entry in enumerate(wheel_entries) if entry.id == prize.id), 0)

    last_frame_text: str | None = None

    async def _show_frame(frame: str) -> None:
        nonlocal last_frame_text
        if frame == last_frame_text:
            return
        last_frame_text = frame
        try:
            await bot.edit_message_text(
                t(user_lang, 'wheel_spin_animation', frame=frame),
                chat_id=chat_id,
                message_id=message_id,
            )
        except MessageNotModified:
            pass

    if len(wheel_entries) == 1:
        frame_text = _render_wheel_frame(wheel_entries, pointer_index)
        await _show_frame(frame_text)
        await asyncio.sleep(0.8)
    else:
        total_cycles = len(wheel_entries) * 2 + random.randint(4, 6)
        delay = 0.18

        for _ in range(total_cycles):
            frame_text = _render_wheel_frame(wheel_entries, pointer_index)
            await _show_frame(frame_text)
            await asyncio.sleep(delay)
            pointer_index = (pointer_index + 1) % len(wheel_entries)
            delay = min(delay + 0.025, 0.55)

        while pointer_index != prize_index:
            frame_text = _render_wheel_frame(wheel_entries, pointer_index)
            await _show_frame(frame_text)
            await asyncio.sleep(delay)
            pointer_index = (pointer_index + 1) % len(wheel_entries)
            delay = min(delay + 0.03, 0.6)

        frame_text = _render_wheel_frame(wheel_entries, pointer_index)

    assign_wheel_prize(prize.id, user_id)
    emoji_symbol = prize.emoji or '🎁'
    result_text = t(
        user_lang,
        'wheel_spin_result',
        name=prize.name,
        location=prize.location,
        emoji=emoji_symbol,
    )
    await bot.edit_message_text(
        f"{t(user_lang, 'wheel_spin_animation', frame=frame_text)}\n\n{result_text}",
        chat_id=chat_id,
        message_id=message_id,
    )

    await _deliver_wheel_prize(bot, chat_id, prize, user_lang)

    formatted_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    await notify_owner_of_prize_win(
        bot,
        user_id=user_id,
        username=call.from_user.username,
        full_name=call.from_user.full_name,
        prize_name=prize.name,
        prize_location=prize.location,
        prize_emoji=emoji_symbol,
        photo_file_id=prize.photo_file_id,
        formatted_time=formatted_time,
    )
    await call.answer(t(user_lang, 'wheel_spin_success'))


async def replenish_balance_callback_handler(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    message_id = call.message.message_id

    # proceed if NowPayments API key is configured
    if EnvKeys.NOWPAYMENTS_API_KEY:
        TgConfig.STATE[f'{user_id}_message_id'] = message_id
        TgConfig.STATE[user_id] = 'process_replenish_balance'
        await bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=message_id,
            text='💰 Enter the top-up amount:',
            reply_markup=back('back_to_menu')
        )
        return

    # fallback if API key missing
    await call.answer('❌ Top-up is not configured.')



async def process_replenish_balance(message: Message):
    bot, user_id = await get_bot_user_ids(message)

    text = message.text
    message_id = TgConfig.STATE.get(f'{user_id}_message_id')
    TgConfig.STATE[user_id] = None
    await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)

    if not text.isdigit() or int(text) < 5 or int(text) > 10000:
        await bot.edit_message_text(chat_id=message.chat.id,
                                    message_id=message_id,
                                    text="❌ Invalid top-up amount. "
                                         "The amount must be between 5€ and 10 000€",
                                    reply_markup=back('replenish_balance'))
        return

    TgConfig.STATE[f'{user_id}_amount'] = text
    markup = crypto_choice('replenish_balance')
    await bot.edit_message_text(chat_id=message.chat.id,
                                message_id=message_id,
                                text=f'💵 Top-up amount: {text}€. Choose payment method:',
                                reply_markup=markup)


async def pay_yoomoney(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    amount = TgConfig.STATE.pop(f'{user_id}_amount', None)
    if not amount:
        await call.answer(text='❌ Invoice not found')
        return

    fake = type('Fake', (), {'text': amount, 'from_user': call.from_user})
    label, url = quick_pay(fake)
    sleep_time = int(TgConfig.PAYMENT_TIME)
    lang = get_user_language(user_id) or 'en'
    markup = payment_menu(url, label, lang)
    await bot.edit_message_text(chat_id=call.message.chat.id,
                                message_id=call.message.message_id,
                                text=f'💵 Top-up amount: {amount}€.\n'
                                     f'⌛️ You have {int(sleep_time / 60)} minutes to pay.\n'
                                     f'<b>❗️ After payment press "Check payment"</b>',
                                reply_markup=markup)
    start_operation(user_id, amount, label, call.message.message_id)
    await asyncio.sleep(sleep_time)
    info = get_unfinished_operation(label)
    if info:
        _, _, _ = info
        status = await check_payment_status(label)
        if status not in ('paid', 'success'):
            finish_operation(label)
            await bot.send_message(user_id, t(lang, 'invoice_cancelled'))


async def crypto_payment(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    currency = call.data.split('_')[1]
    purchase_context = TgConfig.STATE.get(f'{user_id}_purchase_context')
    if purchase_context:
        await handle_purchase_crypto_payment(call, currency, purchase_context)
        return
    amount = TgConfig.STATE.pop(f'{user_id}_amount', None)
    if not amount:
        await call.answer(text='❌ Invoice not found')
        return

    payment_id, address, pay_amount = create_payment(float(amount), currency)

    sleep_time = int(TgConfig.PAYMENT_TIME)
    lang = get_user_language(user_id) or 'en'
    expires_at = (
        datetime.datetime.now() + datetime.timedelta(seconds=sleep_time)
    ).strftime('%H:%M')
    markup = crypto_invoice_menu(payment_id, lang)
    text = t(
        lang,
        'invoice_message',
        amount=pay_amount,
        currency=currency,
        address=address,
        expires_at=expires_at,
    )

    # Generate QR code for the address
    qr = qrcode.make(address)
    buf = BytesIO()
    qr.save(buf, format='PNG')
    buf.seek(0)

    await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    sent = await bot.send_photo(
        chat_id=call.message.chat.id,
        photo=buf,
        caption=text,
        parse_mode='HTML',
        reply_markup=markup,
    )
    start_operation(user_id, amount, payment_id, sent.message_id)
    await asyncio.sleep(sleep_time)
    info = get_unfinished_operation(payment_id)
    if info:
        _, _, _ = info
        status = await check_payment(payment_id)
        if status not in ('finished', 'confirmed', 'sending'):
            finish_operation(payment_id)
            await bot.send_message(user_id, t(lang, 'invoice_cancelled'))


async def checking_payment(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    message_id = call.message.message_id
    label = call.data[6:]
    info = get_unfinished_operation(label)

    if info:
        user_id_db, operation_value, _ = info
        payment_status = await check_payment_status(label)
        if payment_status is None:
            payment_status = await check_payment(label)

        if payment_status in ("success", "paid", "finished", "confirmed", "sending"):
            current_time = datetime.datetime.now()
            formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
            referral_id = get_user_referral(user_id)
            finish_operation(label)

            if referral_id and TgConfig.REFERRAL_PERCENT != 0:
                referral_percent = TgConfig.REFERRAL_PERCENT
                referral_operation = round((referral_percent/100) * operation_value)
                update_balance(referral_id, referral_operation)
                await bot.send_message(referral_id,
                                       f'✅ You received {referral_operation}€ '
                                       f'from your referral {call.from_user.first_name}',
                                       reply_markup=close())

            create_operation(user_id, operation_value, formatted_time)
            update_balance(user_id, operation_value)
            await bot.edit_message_text(chat_id=call.message.chat.id,
                                        message_id=message_id,
                                        text=f'✅ Balance topped up by {operation_value}€',
                                        reply_markup=back('profile'))
            username = f'@{call.from_user.username}' if call.from_user.username else call.from_user.full_name
            await notify_owner_of_topup(bot, username, operation_value, formatted_time)
        else:
            await call.answer(text='❌ Payment was not successful')
    else:
        await call.answer(text='❌ Invoice not found')


async def cancel_payment(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    invoice_id = call.data.split('_', 1)[1]
    lang = get_user_language(user_id) or 'en'
    if get_unfinished_operation(invoice_id):
        await bot.edit_message_text(
            'Are you sure you want to cancel payment?',
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=confirm_cancel(invoice_id, lang),
        )
    else:
        await call.answer(text='❌ Invoice not found')


async def confirm_cancel_payment(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    invoice_id = call.data.split('_', 2)[2]
    lang = get_user_language(user_id) or 'en'
    if get_unfinished_operation(invoice_id):
        finish_operation(invoice_id)
        role = check_role(user_id)
        balance = get_user_balance(user_id) or 0
        purchases = select_user_items(user_id)
        markup = main_menu(role, TgConfig.REVIEWS_URL, TgConfig.PRICE_LIST_URL, lang)
        text = build_menu_text(call.from_user, balance, purchases, lang)
        await bot.edit_message_text(
            t(lang, 'invoice_cancelled'),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        await bot.send_message(user_id, text, reply_markup=markup)
    else:
        await call.answer(text='❌ Invoice not found')


async def check_sub_to_channel(call: CallbackQuery):

    bot, user_id = await get_bot_user_ids(call)
    invoice_id = call.data.split('_', 1)[1]
    lang = get_user_language(user_id) or 'en'
    if get_unfinished_operation(invoice_id):
        finish_operation(invoice_id)
        await bot.edit_message_text(
            t(lang, 'invoice_cancelled'),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=back('replenish_balance'),
        )
    else:
        await call.answer(text='❌ Invoice not found')




async def change_language(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    current_lang = get_user_language(user_id) or 'en'
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton('English \U0001F1EC\U0001F1E7', callback_data='set_lang_en'),
        InlineKeyboardButton('Русский \U0001F1F7\U0001F1FA', callback_data='set_lang_ru'),
        InlineKeyboardButton('Lietuvi\u0173 \U0001F1F1\U0001F1F9', callback_data='set_lang_lt')
    )
    await bot.edit_message_text(
        t(current_lang, 'choose_language'),
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=markup
    )


async def set_language(call: CallbackQuery):
    bot, user_id = await get_bot_user_ids(call)
    lang_code = call.data.split('_')[-1]
    update_user_language(user_id, lang_code)
    await call.message.delete()
    role = check_role(user_id)
    balance = get_user_balance(user_id) or 0
    markup = main_menu(role, TgConfig.REVIEWS_URL, TgConfig.PRICE_LIST_URL, lang_code)
    purchases = select_user_items(user_id)
    text = build_menu_text(call.from_user, balance, purchases, lang_code)

    offer_after_language = TgConfig.STATE.pop(f'{user_id}_awaiting_language_welcome', None)
    if offer_after_language:
        await _offer_welcome_video(bot, user_id, text, markup, lang_code)
        return

    await _send_welcome_media(bot, user_id)

    await bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=markup
    )


async def welcome_video_yes_handler(call: CallbackQuery):
    await _handle_welcome_video_choice(call, send_media=True)


async def welcome_video_no_handler(call: CallbackQuery):
    await _handle_welcome_video_choice(call, send_media=False)






def register_user_handlers(dp: Dispatcher):
    dp.register_message_handler(
        process_security_captcha,
        lambda m: TgConfig.STATE.get(m.from_user.id) == 'security_captcha',
        state='*',
    )
    dp.register_message_handler(start,
                                 commands=['start'])
    dp.register_message_handler(purchase_tip_trigger,
                                 lambda m: m.text and m.text.startswith('✅ Item purchased.'), state='*')

    dp.register_callback_query_handler(shop_callback_handler,
                                       lambda c: c.data == 'shop')
    dp.register_callback_query_handler(dummy_button,
                                       lambda c: c.data == 'dummy_button')
    dp.register_callback_query_handler(welcome_video_yes_handler,
                                       lambda c: c.data == 'welcome_video_yes', state='*')
    dp.register_callback_query_handler(welcome_video_no_handler,
                                       lambda c: c.data == 'welcome_video_no', state='*')
    dp.register_callback_query_handler(profile_callback_handler,
                                       lambda c: c.data == 'profile')
    dp.register_callback_query_handler(wheel_spin_open_handler,
                                       lambda c: c.data == 'wheel_spin', state='*')
    dp.register_callback_query_handler(wheel_spin_confirm_handler,
                                       lambda c: c.data == 'wheel_spin_confirm', state='*')
    dp.register_callback_query_handler(wheel_spin_cancel_handler,
                                       lambda c: c.data == 'wheel_spin_cancel', state='*')
    dp.register_callback_query_handler(rules_callback_handler,
                                       lambda c: c.data == 'rules')
    dp.register_callback_query_handler(help_callback_handler,
                                       lambda c: c.data == 'help')
    dp.register_callback_query_handler(replenish_balance_callback_handler,
                                       lambda c: c.data == 'replenish_balance')
    dp.register_callback_query_handler(price_list_callback_handler,
                                       lambda c: c.data == 'price_list')
    dp.register_callback_query_handler(blackjack_callback_handler,
                                       lambda c: c.data == 'blackjack')
    dp.register_callback_query_handler(blackjack_set_bet_handler,
                                       lambda c: c.data == 'blackjack_set_bet')
    dp.register_callback_query_handler(blackjack_place_bet_handler,
                                       lambda c: c.data == 'blackjack_place_bet')
    dp.register_callback_query_handler(blackjack_play_again_handler,
                                       lambda c: c.data.startswith('blackjack_play_'))
    dp.register_callback_query_handler(blackjack_move_handler,
                                       lambda c: c.data in ('blackjack_hit', 'blackjack_stand'))
    dp.register_callback_query_handler(blackjack_history_handler,
                                       lambda c: c.data.startswith('blackjack_history_'))
    dp.register_callback_query_handler(blackjack_rules_handler,
                                       lambda c: c.data == 'blackjack_rules')
    dp.register_callback_query_handler(feedback_service_handler,
                                       lambda c: c.data.startswith('feedback_service_'), state='*')
    dp.register_callback_query_handler(feedback_product_handler,
                                       lambda c: c.data.startswith('feedback_product_'), state='*')
    dp.register_callback_query_handler(bought_items_callback_handler,
                                       lambda c: c.data == 'bought_items', state='*')
    dp.register_callback_query_handler(back_to_menu_callback_handler,
                                       lambda c: c.data == 'back_to_menu',
                                       state='*')
    dp.register_callback_query_handler(close_callback_handler,
                                       lambda c: c.data == 'close', state='*')
    dp.register_callback_query_handler(change_language,
                                       lambda c: c.data == 'change_language', state='*')
    dp.register_callback_query_handler(set_language,
                                       lambda c: c.data.startswith('set_lang_'), state='*')

    dp.register_callback_query_handler(navigate_bought_items,
                                       lambda c: c.data.startswith('bought-goods-page_'), state='*')
    dp.register_callback_query_handler(bought_item_info_callback_handler,
                                       lambda c: c.data.startswith('bought-item:'), state='*')
    dp.register_callback_query_handler(items_list_callback_handler,
                                       lambda c: c.data.startswith('category_'), state='*')
    dp.register_callback_query_handler(item_info_callback_handler,
                                       lambda c: c.data.startswith('item_'), state='*')
    dp.register_callback_query_handler(confirm_buy_callback_handler,
                                       lambda c: c.data.startswith('confirm_'), state='*')
    dp.register_callback_query_handler(apply_promo_callback_handler,
                                       lambda c: c.data.startswith('applypromo_'), state='*')
    dp.register_callback_query_handler(pay_with_crypto_handler,
                                       lambda c: c.data.startswith('cryptobuy_'), state='*')
    dp.register_callback_query_handler(pay_with_credit_and_crypto_handler,
                                       lambda c: c.data.startswith('creditpay_'), state='*')
    dp.register_callback_query_handler(buy_item_callback_handler,
                                       lambda c: c.data.startswith('buy_'), state='*')
    dp.register_callback_query_handler(tip_callback_handler,
                                       lambda c: c.data.startswith('tip_'), state='*')
    dp.register_callback_query_handler(pay_yoomoney,
                                       lambda c: c.data == 'pay_yoomoney', state='*')
    dp.register_callback_query_handler(crypto_payment,
                                       lambda c: c.data.startswith('crypto_'), state='*')
    dp.register_callback_query_handler(cancel_purchase_invoice,
                                       lambda c: c.data.startswith('cancel_purchase_'), state='*')
    dp.register_callback_query_handler(cancel_payment,
                                       lambda c: c.data.startswith('cancel_') and not c.data.startswith('cancel_purchase_'), state='*')
    dp.register_callback_query_handler(confirm_cancel_payment,
                                       lambda c: c.data.startswith('confirm_cancel_'), state='*')
    dp.register_callback_query_handler(check_purchase_invoice,
                                       lambda c: c.data.startswith('check_purchase_'), state='*')
    dp.register_callback_query_handler(checking_payment,
                                       lambda c: c.data.startswith('check_') and not c.data.startswith('check_purchase_'), state='*')
    dp.register_callback_query_handler(process_home_menu,
                                       lambda c: c.data == 'home_menu', state='*')

    dp.register_message_handler(process_replenish_balance,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'process_replenish_balance')
    dp.register_message_handler(
        process_promo_code,
        lambda c: TgConfig.STATE.get(c.from_user.id) in {
            'wait_promo_code',
            'wait_promo_city',
            'wait_promo_district',
        },
    )
    dp.register_message_handler(blackjack_receive_bet,
                                lambda c: TgConfig.STATE.get(c.from_user.id) == 'blackjack_enter_bet')
    dp.register_message_handler(pavogti,
                                commands=['pavogti'])
    dp.register_callback_query_handler(pavogti_item_callback,
                                       lambda c: c.data.startswith('pavogti_item_'))