from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.database.models import Permission

from bot.localization import t
from bot.database.methods import (
    get_category_parent,
    select_item_values_amount,
    check_value,
)
from bot.misc import TgConfig
from bot.utils import display_name





def main_menu(role: int, reviews: str = None, price: str = None, lang: str = 'en') -> InlineKeyboardMarkup:
    """Return main menu with layout:
       1) Shop
       2) Profile | Top Up
       3) Reviews | Price List (only those that exist)
       4) Language
       (+ Admin panel if role > 1)
    """
    inline_keyboard = []

    # Row 1: Shop (single wide)
    inline_keyboard.append(
        [InlineKeyboardButton(t(lang, 'shop'), callback_data='shop')]
    )

    # Row 2: Profile | Top Up
    inline_keyboard.append([
        InlineKeyboardButton(t(lang, 'profile'), callback_data='profile'),
        InlineKeyboardButton(t(lang, 'top_up'), callback_data='replenish_balance'),
    ])

    # Row 3: Reviews | Price List (conditionally add one or both)
    row3 = []
    if reviews:
        row3.append(InlineKeyboardButton(t(lang, 'reviews'), url=reviews))
    if price:
        row3.append(InlineKeyboardButton(t(lang, 'price_list'), callback_data='price_list'))
    if row3:
        inline_keyboard.append(row3)

    # Row 4: Language (single wide)
    inline_keyboard.append(
        [InlineKeyboardButton(t(lang, 'language'), callback_data='change_language')]
    )

    # Optional: Admin panel
    if role > 1:
        inline_keyboard.append(
            [InlineKeyboardButton(t(lang, 'admin_panel'), callback_data='console')]
        )

    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def categories_list(list_items: list[str]) -> InlineKeyboardMarkup:
    """Show all categories without pagination."""
    markup = InlineKeyboardMarkup()
    for name in list_items:
        markup.add(InlineKeyboardButton(text=name, callback_data=f'category_{name}'))
    markup.add(InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu'))
    return markup


def goods_list(list_items: list[str], category_name: str) -> InlineKeyboardMarkup:
    """Show all goods for a category without pagination."""
    markup = InlineKeyboardMarkup()
    for name in list_items:
        markup.add(InlineKeyboardButton(text=display_name(name), callback_data=f'item_{name}'))
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data='shop'))
    return markup


def subcategories_list(list_items: list[str], parent: str) -> InlineKeyboardMarkup:
    """Show all subcategories without pagination."""
    markup = InlineKeyboardMarkup()
    for name in list_items:
        markup.add(InlineKeyboardButton(text=name, callback_data=f'category_{name}'))
    back_parent = get_category_parent(parent)
    back_data = 'shop' if back_parent is None else f'category_{back_parent}'
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=back_data))
    return markup


def user_items_list(list_items: list, data: str, back_data: str, pre_back: str, current_index: int, max_index: int)\
        -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    page_items = list_items[current_index * 10: (current_index + 1) * 10]
    for item in page_items:
        markup.add(InlineKeyboardButton(text=display_name(item.item_name), callback_data=f'bought-item:{item.id}:{pre_back}'))
    if max_index > 0:
        buttons = [
            InlineKeyboardButton(text='◀️', callback_data=f'bought-goods-page_{current_index - 1}_{data}'),
            InlineKeyboardButton(text=f'{current_index + 1}/{max_index + 1}', callback_data='dummy_button'),
            InlineKeyboardButton(text='▶️', callback_data=f'bought-goods-page_{current_index + 1}_{data}')
        ]
        markup.row(*buttons)
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=back_data))
    return markup


def item_info(item_name: str, category_name: str, lang: str) -> InlineKeyboardMarkup:
    """Return inline keyboard for a single item without basket option."""
    inline_keyboard = [
        [InlineKeyboardButton('💰 Buy', callback_data=f'confirm_{item_name}')],
        [InlineKeyboardButton('🔙 Go back', callback_data=f'category_{category_name}')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def profile(user_items: int = 0, lang: str = 'en', wheel_spins: int = 0) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('💸 Top up balance', callback_data='replenish_balance')]
    ]
    inline_keyboard.append([InlineKeyboardButton('🃏 Blackjack', callback_data='blackjack')])
    if wheel_spins > 0:
        inline_keyboard.append([
            InlineKeyboardButton(
                t(lang, 'wheel_spin_button', count=wheel_spins),
                callback_data='wheel_spin'
            )
        ])
    if user_items != 0:
        inline_keyboard.append([InlineKeyboardButton('🎁 Purchased items', callback_data='bought_items')])
    inline_keyboard.append([InlineKeyboardButton(t(lang, 'help'), callback_data='help')])
    inline_keyboard.append([InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def rules() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu')
         ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def console(role: int) -> InlineKeyboardMarkup:
    assistant_role = Permission.USE | Permission.ASSIGN_PHOTOS
    if role == assistant_role:
        inline_keyboard = [
            [InlineKeyboardButton('🖼 Assign photos', callback_data='assign_photos')],
            [InlineKeyboardButton('❓ Help', callback_data='admin_help')],
            [InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu')]
        ]
        return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

    inline_keyboard = [
        [InlineKeyboardButton('🏪 Parduotuvės valdymas', callback_data='shop_management'),
         InlineKeyboardButton('🛒 Pirkimai', callback_data='pirkimai')],
        [InlineKeyboardButton('👥 Vartotojų valdymas', callback_data='user_management'),
         InlineKeyboardButton('📢 Pranešimų siuntimas', callback_data='send_message')],
    ]
    if role & Permission.OWN:
        inline_keyboard.insert(0, [
            InlineKeyboardButton('🛠 Assign assistants', callback_data='assistant_management'),
            InlineKeyboardButton('🧰 Manage Stock', callback_data='manage_stock')
        ])
        inline_keyboard.insert(1, [
            InlineKeyboardButton('📊 View Stock', callback_data='view_stock_overview')
        ])
        inline_keyboard.insert(2, [
            InlineKeyboardButton('🎡 Wheel', callback_data='wheel_menu')
        ])
    inline_keyboard.append([InlineKeyboardButton('❓ Help', callback_data='admin_help')])
    inline_keyboard.append([InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

def confirm_purchase_menu(
    item_name: str,
    lang: str,
    user_id: int | None,
    price: float,
    balance: float,
    promo_available: bool = True,
) -> InlineKeyboardMarkup:
    inline_keyboard: list[list[InlineKeyboardButton]] = []

    if balance >= price > 0:
        inline_keyboard.append([
            InlineKeyboardButton(
                t(lang, 'pay_with_balance', amount=f'{price:.2f}'),
                callback_data=f'buy_{item_name}',
            )
        ])
    elif balance > 0:
        due = max(price - balance, 0)
        inline_keyboard.append([
            InlineKeyboardButton(
                t(
                    lang,
                    'pay_with_crypto_after_credits',
                    credits=f'{balance:.2f}',
                    due=f'{due:.2f}',
                ),
                callback_data=f'creditpay_{item_name}',
            )
        ])

    inline_keyboard.append([
        InlineKeyboardButton(
            t(lang, 'pay_with_crypto', amount=f'{price:.2f}'),
            callback_data=f'cryptobuy_{item_name}',
        )
    ])

    if promo_available:
        inline_keyboard.append(
            [InlineKeyboardButton(t(lang, 'apply_promo'), callback_data=f'applypromo_{item_name}')]
        )
    inline_keyboard.append([InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def user_management(admin_role: int, user_role: int, admin_manage: int, items: int, user_id: int) \
        -> InlineKeyboardMarkup:
    inline_keyboard = [
        [
            InlineKeyboardButton('💸 Top up balance User', callback_data=f'fill-user-balance_{user_id}')
        ]
    ]
    if items > 0:
        inline_keyboard.append([InlineKeyboardButton('🎁 Purchased items', callback_data=f'user-items_{user_id}')])
    if admin_role >= admin_manage and admin_role > user_role:
        if user_role == 1:
            inline_keyboard.append(
                [InlineKeyboardButton('⬆️ Assign admin', callback_data=f'set-admin_{user_id}')])
        else:
            inline_keyboard.append(
                [InlineKeyboardButton('⬇️ Remove admin', callback_data=f'remove-admin_{user_id}')])
    inline_keyboard.append([InlineKeyboardButton('🔙 Go back', callback_data='user_management')])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def purchases_dates_list(dates: list[str]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for d in dates:
        markup.add(InlineKeyboardButton(d, callback_data=f'purchases_date_{d}'))
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data='console'))
    return markup


def purchases_list(purchases: list[dict], date: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    for p in purchases:
        markup.add(
            InlineKeyboardButton(
                f"{p['unique_id']} - {display_name(p['item_name'])}",
                callback_data=f"purchase_{p['unique_id']}_{date}"
            )
        )
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data='pirkimai'))
    return markup


def purchase_info_menu(purchase_id: int, date: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton('👁 View file', callback_data=f'view_purchase_{purchase_id}'))
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=f'purchases_date_{date}'))
    return markup


def user_manage_check(user_id: int) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('✅ Yes', callback_data=f'check-user_{user_id}')
         ],
        [InlineKeyboardButton('🔙 Go back', callback_data='user_management')
         ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def shop_management() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('📦 Prekių įpakavimas', callback_data='goods_management')
         ],
        [InlineKeyboardButton('🗂️ Kategorijų sukurimas', callback_data='categories_management')
         ],
        [InlineKeyboardButton('🏷 Promo codes', callback_data='promo_management')
         ],
        [InlineKeyboardButton('📝 Logai', callback_data='show_logs')
         ],
        [InlineKeyboardButton('📊 Statistikos', callback_data='statistics')
         ],
        [InlineKeyboardButton('🔙 Go back', callback_data='console')
         ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def goods_management() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('➕ Pridėti prekę', callback_data='item-management')],
        [InlineKeyboardButton('✏️ Atnaujinti prekę', callback_data='update_item')],
        [InlineKeyboardButton('🖼 Assign photos', callback_data='assign_photos')],
        [InlineKeyboardButton('🗑️ Pašalinti prekę', callback_data='delete_item')],
        [InlineKeyboardButton('🛒 Nupirktų prekių informacija', callback_data='show_bought_item')],
        [InlineKeyboardButton('🔙 Grįžti atgal', callback_data='shop_management')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)



def item_management() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('🆕 Sukurti prekę', callback_data='add_item')],
        [InlineKeyboardButton('➕ Pridėti prie esamos prekės', callback_data='update_item_amount')],
        [InlineKeyboardButton('🔙 Grįžti atgal', callback_data='goods_management')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

def categories_management() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('📁 Pridėti kategoriją', callback_data='add_category')],
        [InlineKeyboardButton('📂 Pridėti subkategoriją', callback_data='add_subcategory')],
        [InlineKeyboardButton('✏️ Atnaujinti kategoriją', callback_data='update_category')],
        [InlineKeyboardButton('🗑️ Pašalinti kategoriją', callback_data='delete_category')],
        [InlineKeyboardButton('🔙 Grįžti atgal', callback_data='shop_management')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def promo_codes_management() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('➕ Create promo code', callback_data='create_promo')],
        [InlineKeyboardButton('🗑️ Delete promo code', callback_data='delete_promo')],
        [InlineKeyboardButton('🛠 Manage promo code', callback_data='manage_promo')],
        [InlineKeyboardButton('🔙 Go back', callback_data='shop_management')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def promo_expiry_keyboard(back_data: str) -> InlineKeyboardMarkup:
    """Keyboard to choose promo code expiry units."""
    inline_keyboard = [
        [InlineKeyboardButton('Days', callback_data='promo_expiry_days')],
        [InlineKeyboardButton('Weeks', callback_data='promo_expiry_weeks')],
        [InlineKeyboardButton('Months', callback_data='promo_expiry_months')],
        [InlineKeyboardButton('No expiry', callback_data='promo_expiry_none')],
        [InlineKeyboardButton('🔙 Go back', callback_data=back_data)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def promo_codes_list(codes: list[str], action: str, back_data: str) -> InlineKeyboardMarkup:
    """Create a list of promo codes with callback prefix."""
    markup = InlineKeyboardMarkup()
    for code in codes:
        markup.add(InlineKeyboardButton(code, callback_data=f'{action}_{code}'))
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=back_data))
    return markup


def promo_manage_actions(code: str) -> InlineKeyboardMarkup:
    """Keyboard with actions for a single promo code."""
    inline_keyboard = [
        [InlineKeyboardButton('✏️ Change discount', callback_data=f'promo_manage_discount_{code}')],
        [InlineKeyboardButton('⏰ Change expiry', callback_data=f'promo_manage_expiry_{code}')],
        [InlineKeyboardButton('🌍 Edit geography', callback_data=f'promo_manage_geo_{code}')],
        [InlineKeyboardButton('📈 Usage analytics', callback_data=f'promo_manage_stats_{code}')],
        [InlineKeyboardButton('🗑️ Delete', callback_data=f'promo_manage_delete_{code}')],
        [InlineKeyboardButton('🔙 Go back', callback_data='manage_promo')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def wheel_management_menu(lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(t(lang, 'wheel_menu_assign_prizes'), callback_data='wheel_assign_prizes')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_assign_spins'), callback_data='wheel_assign_spins')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_see_users'), callback_data='wheel_see_users')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_remove_users'), callback_data='wheel_remove_users')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_back'), callback_data='console')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def wheel_assign_more_keyboard(lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(t(lang, 'wheel_assign_add_more'), callback_data='wheel_assign_more')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_back'), callback_data='wheel_menu')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def wheel_assign_spins_more_keyboard(lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(t(lang, 'wheel_assign_spins_add_more'), callback_data='wheel_assign_spins_more')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_back'), callback_data='wheel_menu')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def wheel_remove_more_keyboard(lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(t(lang, 'wheel_remove_another'), callback_data='wheel_remove_more')],
        [InlineKeyboardButton(t(lang, 'wheel_menu_back'), callback_data='wheel_menu')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def wheel_spin_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(t(lang, 'wheel_spin_confirm_use'), callback_data='wheel_spin_confirm')],
        [InlineKeyboardButton(t(lang, 'wheel_spin_cancel'), callback_data='wheel_spin_cancel')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def _stock_cache_key(user_id: int) -> str:
    return f'{user_id}_stock_cache'


def _ensure_stock_cache(user_id: int) -> dict:
    key = _stock_cache_key(user_id)
    cache = TgConfig.STATE.get(key)
    if not isinstance(cache, dict):
        cache = {
            'category_tokens': {},
            'categories': {},
            'item_tokens': {},
            'items': {},
        }
        TgConfig.STATE[key] = cache
    else:
        cache.setdefault('category_tokens', {})
        cache.setdefault('categories', {})
        cache.setdefault('item_tokens', {})
        cache.setdefault('items', {})
    return cache


def reset_stock_cache(user_id: int) -> None:
    TgConfig.STATE[_stock_cache_key(user_id)] = {
        'category_tokens': {},
        'categories': {},
        'item_tokens': {},
        'items': {},
    }


def reset_all_stock_caches() -> None:
    """Clear cached stock navigation data for every user."""
    keys = [key for key in TgConfig.STATE.keys() if key.endswith('_stock_cache')]
    for key in keys:
        TgConfig.STATE[key] = {
            'category_tokens': {},
            'categories': {},
            'item_tokens': {},
            'items': {},
        }


def _get_category_token(cache: dict, name: str) -> str:
    tokens = cache['category_tokens']
    reverse = cache['categories']
    if name not in tokens:
        token = f'c{len(tokens) + 1}'
        tokens[name] = token
        reverse[token] = name
    return tokens[name]


def _get_item_token(cache: dict, name: str) -> str:
    tokens = cache['item_tokens']
    reverse = cache['items']
    if name not in tokens:
        token = f'i{len(tokens) + 1}'
        tokens[name] = token
        reverse[token] = name
    return tokens[name]


def resolve_stock_category(user_id: int, token: str) -> str | None:
    cache = _ensure_stock_cache(user_id)
    return cache['categories'].get(token)


def resolve_stock_item(user_id: int, token: str) -> str | None:
    cache = _ensure_stock_cache(user_id)
    return cache['items'].get(token)


def stock_categories_list(user_id: int, list_items: list[str], parent: str | None) -> InlineKeyboardMarkup:
    """List categories or subcategories for stock view."""
    cache = _ensure_stock_cache(user_id)
    markup = InlineKeyboardMarkup()
    for name in list_items:
        token = _get_category_token(cache, name)
        markup.add(InlineKeyboardButton(text=name, callback_data=f'stock_cat:{token}'))
    if parent is None:
        back_data = 'console'
    else:
        parent_token = _get_category_token(cache, parent)
        back_data = f'stock_cat:{parent_token}'
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=back_data))
    return markup


def stock_goods_list(user_id: int, list_items: list[str], category_name: str) -> InlineKeyboardMarkup:
    """Show goods with stock counts for a category."""
    cache = _ensure_stock_cache(user_id)
    # Ensure the current category has a token so the "Go back" button can reference it later.
    _get_category_token(cache, category_name)
    markup = InlineKeyboardMarkup()
    for name in list_items:
        amount = '∞' if check_value(name) else select_item_values_amount(name)
        item_token = _get_item_token(cache, name)
        markup.add(InlineKeyboardButton(
            text=f'{display_name(name)} ({amount})',
            callback_data=f'stock_item:{item_token}'
        ))
    parent = get_category_parent(category_name)
    if parent is None:
        back_data = 'console'
    else:
        parent_token = _get_category_token(cache, parent)
        back_data = f'stock_cat:{parent_token}'
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=back_data))
    return markup


def stock_values_list(user_id: int, values, item_name: str) -> InlineKeyboardMarkup:
    """List individual stock entries for an item."""
    cache = _ensure_stock_cache(user_id)
    item_token = _get_item_token(cache, item_name)
    markup = InlineKeyboardMarkup()
    for val in values:
        markup.add(InlineKeyboardButton(
            text=f'ID {val.id}',
            callback_data=f'stock_val:{val.id}'
        ))
    markup.add(InlineKeyboardButton('🔙 Go back', callback_data=f'stock_item:{item_token}'))
    return markup


def stock_value_actions(user_id: int, value_id: int, item_name: str) -> InlineKeyboardMarkup:
    cache = _ensure_stock_cache(user_id)
    item_token = _get_item_token(cache, item_name)
    inline_keyboard = [
        [InlineKeyboardButton('🗑 Delete', callback_data=f'stock_del:{value_id}')],
        [InlineKeyboardButton('🔙 Go back', callback_data=f'stock_vals:{item_token}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def stock_item_actions(user_id: int, item_name: str, category_name: str) -> InlineKeyboardMarkup:
    """Actions available for a single item inside stock management."""
    cache = _ensure_stock_cache(user_id)
    item_token = _get_item_token(cache, item_name)
    category_token = _get_category_token(cache, category_name)
    inline_keyboard = [
        [InlineKeyboardButton('📦 View stock', callback_data=f'stock_vals:{item_token}')],
        [InlineKeyboardButton('✏️ Rename title', callback_data=f'stock_title:{item_token}')],
        [InlineKeyboardButton('📝 Edit description', callback_data=f'stock_desc:{item_token}')],
        [InlineKeyboardButton('💶 Change price', callback_data=f'stock_price:{item_token}')],
        [InlineKeyboardButton('🔙 Go back', callback_data=f'stock_cat:{category_token}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def stock_price_prompt(user_id: int, item_name: str) -> InlineKeyboardMarkup:
    """Inline keyboard shown while waiting for a new price input."""
    cache = _ensure_stock_cache(user_id)
    item_token = _get_item_token(cache, item_name)
    inline_keyboard = [
        [InlineKeyboardButton('🔙 Cancel', callback_data=f'stock_item:{item_token}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def stock_item_return(user_id: int, item_name: str) -> InlineKeyboardMarkup:
    """Inline keyboard that returns back to the item overview."""
    cache = _ensure_stock_cache(user_id)
    item_token = _get_item_token(cache, item_name)
    inline_keyboard = [
        [InlineKeyboardButton('🔙 Back to item', callback_data=f'stock_item:{item_token}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def close() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('Hide', callback_data='close')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def check_sub(channel_username: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('Subscribe', url=f'https://t.me/{channel_username}')],
        [InlineKeyboardButton('Check', callback_data='sub_channel_done')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def back(callback: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('🔙 Go back', callback_data=callback)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def payment_menu(url: str, label: str, lang: str) -> InlineKeyboardMarkup:
    """Return markup for fiat payment invoices."""
    inline_keyboard = [
        [InlineKeyboardButton('✅ Pay', url=url)],
        [InlineKeyboardButton('🔄 Check payment', callback_data=f'check_{label}')],
        [InlineKeyboardButton(t(lang, 'cancel_payment'), callback_data=f'cancel_{label}')],
        [InlineKeyboardButton('🔙 Go back', callback_data='back_to_menu')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def crypto_invoice_menu(invoice_id: str, lang: str) -> InlineKeyboardMarkup:
    """Return markup for crypto invoice."""
    inline_keyboard = [
        [InlineKeyboardButton(t(lang, 'cancel_payment'), callback_data=f'cancel_{invoice_id}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def confirm_cancel(invoice_id: str, lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('✅ Yes', callback_data=f'confirm_cancel_{invoice_id}')],
        [InlineKeyboardButton('🔙 Back', callback_data=f'check_{invoice_id}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def crypto_choice(back_callback: str = 'replenish_balance') -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('SOL', callback_data='crypto_SOL'),
         InlineKeyboardButton('BTC', callback_data='crypto_BTC')],
        [InlineKeyboardButton('TRX', callback_data='crypto_TRX'),
         InlineKeyboardButton('TON', callback_data='crypto_TON')],
        [InlineKeyboardButton('USDT (TRC20)', callback_data='crypto_USDTTRC20'),
         InlineKeyboardButton('ETH', callback_data='crypto_ETH')],
        [InlineKeyboardButton('LTC', callback_data='crypto_LTC')],
        [InlineKeyboardButton('🔙 Go back', callback_data=back_callback)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def purchase_crypto_invoice_menu(invoice_id: str, lang: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('🔄 Check payment', callback_data=f'check_purchase_{invoice_id}')],
        [InlineKeyboardButton(t(lang, 'cancel_payment'), callback_data=f'cancel_purchase_{invoice_id}')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def reset_config(key: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(f'Reset {key}', callback_data=f'reset_{key}')
         ],
        [InlineKeyboardButton('🔙 Go back', callback_data='settings')
         ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def question_buttons(question: str, back_data: str) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('✅ Yes', callback_data=f'{question}_yes'),
         InlineKeyboardButton('❌ No', callback_data=f'{question}_no')
         ],
        [InlineKeyboardButton('🔙 Go back', callback_data=back_data)
         ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def blackjack_controls() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton('🃏 Hit', callback_data='blackjack_hit'),
         InlineKeyboardButton('🛑 Stand', callback_data='blackjack_stand')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def blackjack_bet_input_menu(bet: int | None = None, lang: str = 'en') -> InlineKeyboardMarkup:
    bet_text = f'🎲 Bet! ({bet}€)' if bet else '🎲 Bet!'
    inline_keyboard = [
        [InlineKeyboardButton(bet_text, callback_data='blackjack_place_bet')],
        [InlineKeyboardButton('💵 Set Bet', callback_data='blackjack_set_bet')],
        [InlineKeyboardButton('📜 History', callback_data='blackjack_history_0')],
        [InlineKeyboardButton(t(lang, 'blackjack_rules_button'), callback_data='blackjack_rules')],
        [InlineKeyboardButton('🔙 Back to menu', callback_data='back_to_menu')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def blackjack_end_menu(bet: int) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(f'▶️ Play Again ({bet}€)', callback_data=f'blackjack_play_{bet}')],
        [InlineKeyboardButton('🔙 Back to menu', callback_data='blackjack')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def blackjack_history_menu(index: int, total: int) -> InlineKeyboardMarkup:
    buttons = []
    if index > 0:
        buttons.append(InlineKeyboardButton('◀️', callback_data=f'blackjack_history_{index-1}'))
    buttons.append(InlineKeyboardButton(f'{index+1}/{total}', callback_data='dummy_button'))
    if index < total - 1:
        buttons.append(InlineKeyboardButton('▶️', callback_data=f'blackjack_history_{index+1}'))
    inline_keyboard = [buttons, [InlineKeyboardButton('🔙 Back', callback_data='blackjack')]]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def feedback_menu(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f'{i} Star{"s" if i > 1 else ""} {"⭐" * i}',
            callback_data=f'{prefix}_{i}'
        )
        for i in range(1, 4)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def tip_menu(lang: str) -> InlineKeyboardMarkup:
    amounts = [
        InlineKeyboardButton(f'{i}€', callback_data=f'tip_{i}')
        for i in range(1, 6)
    ]
    inline_keyboard = [amounts, [InlineKeyboardButton(t(lang, 'cancel'), callback_data='tip_cancel')]]
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
