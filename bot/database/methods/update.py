from __future__ import annotations

import datetime
from decimal import Decimal, ROUND_HALF_UP

from bot.database.models import (
    User,
    ItemValues,
    Goods,
    Categories,
    PromoCode,
    PromoCodeGeo,
    PromoCodeProductFilter,
    WheelPrize,
    WheelUser,
)
from bot.database import Database
from bot.database.methods.create import log_product_change


def _quantize_price(value) -> Decimal:
    if value is None:
        raise ValueError('Price value cannot be None')
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def set_role(telegram_id: str, role: int) -> None:
    Database().session.query(User).filter(User.telegram_id == telegram_id).update(
        values={User.role_id: role})
    Database().session.commit()


def update_balance(telegram_id: int | str, summ: int) -> None:
    old_balance = User.balance
    new_balance = old_balance + summ
    Database().session.query(User).filter(User.telegram_id == telegram_id).update(
        values={User.balance: new_balance})
    Database().session.commit()


def update_user_language(telegram_id: int, language: str) -> None:
    Database().session.query(User).filter(User.telegram_id == telegram_id).update(
        values={User.language: language})
    Database().session.commit()


def buy_item_for_balance(telegram_id: str, summ: int) -> int:
    old_balance = User.balance
    new_balance = old_balance - summ
    Database().session.query(User).filter(User.telegram_id == telegram_id).update(
        values={User.balance: new_balance})
    Database().session.commit()
    return Database().session.query(User.balance).filter(User.telegram_id == telegram_id).one()[0]


def update_item(
    item_name: str,
    new_name: str,
    new_description: str,
    new_price,
    new_category_name: str,
    new_delivery_description: str | None,
    *,
    changed_by: int | None = None,
) -> None:
    session = Database().session
    item = session.query(Goods).filter(Goods.name == item_name).first()
    if not item:
        return
    original_name = item.name
    original_description = item.description
    original_price = item.price
    updates = {
        Goods.name: new_name,
        Goods.description: new_description,
        Goods.price: _quantize_price(new_price),
        Goods.category_name: new_category_name,
        Goods.delivery_description: new_delivery_description,
    }
    session.query(ItemValues).filter(ItemValues.item_name == item_name).update(
        values={ItemValues.item_name: new_name}
    )
    session.query(Goods).filter(Goods.name == item_name).update(values=updates)
    session.commit()
    if changed_by is not None:
        if original_name != new_name:
            log_product_change(item_name, 'name', original_name, new_name, changed_by)
        if original_description != new_description:
            log_product_change(item_name, 'description', original_description, new_description, changed_by)
        if Decimal(str(original_price)) != _quantize_price(new_price):
            log_product_change(
                item_name,
                'price',
                f'{Decimal(str(original_price)):.2f}',
                f'{_quantize_price(new_price):.2f}',
                changed_by,
            )


def update_category(category_name: str, new_name: str) -> None:
    Database().session.query(Goods).filter(Goods.category_name == category_name).update(
        values={Goods.category_name: new_name})
    Database().session.query(Categories).filter(Categories.name == category_name).update(
        values={Categories.name: new_name})
    Database().session.commit()


def update_promocode(
    code: str,
    discount: int | None = None,
    expires_at: str | None = None,
    active: bool | None = None,
    geo_targets: list[tuple[str, str | None]] | None = None,
    allowed_filters: list[tuple[str, str]] | None = None,
    excluded_filters: list[tuple[str, str]] | None = None,
) -> None:
    """Update promo code discount, expiry date or activity."""
    values = {}
    if discount is not None:
        values[PromoCode.discount] = discount
    if expires_at is not None or expires_at is None:
        values[PromoCode.expires_at] = expires_at
    if active is not None:
        values[PromoCode.active] = active
    if not values:
        pass
    session = Database().session
    if values:
        session.query(PromoCode).filter(PromoCode.code == code).update(values=values)
    if geo_targets is not None:
        session.query(PromoCodeGeo).filter(PromoCodeGeo.code == code).delete()
        for city, district in geo_targets:
            session.add(PromoCodeGeo(code=code, city=city, district=district))
    if allowed_filters is not None or excluded_filters is not None:
        session.query(PromoCodeProductFilter).filter(PromoCodeProductFilter.code == code).delete()
        for target_type, target_name in allowed_filters or []:
            session.add(
                PromoCodeProductFilter(
                    code=code,
                    target_type=target_type,
                    target_name=target_name,
                    is_allowed=True,
                )
            )
        for target_type, target_name in excluded_filters or []:
            session.add(
                PromoCodeProductFilter(
                    code=code,
                    target_type=target_type,
                    target_name=target_name,
                    is_allowed=False,
                )
            )
    session.commit()


def add_wheel_spins(user_id: int, amount: int = 1) -> bool:
    if amount <= 0:
        return False
    session = Database().session
    user = session.get(WheelUser, user_id)
    if user is None:
        user = WheelUser(user_id=user_id)
        session.add(user)
        session.flush()
    if user.is_banned:
        session.commit()
        return False
    user.spins += amount
    session.commit()
    return True


def consume_wheel_spin(user_id: int) -> bool:
    session = Database().session
    user = session.get(WheelUser, user_id)
    if user is None or user.is_banned or user.spins <= 0:
        return False
    user.spins -= 1
    session.commit()
    return True


def assign_wheel_prize(prize_id: int, user_id: int) -> None:
    session = Database().session
    prize = session.get(WheelPrize, prize_id)
    if prize is None:
        return
    prize.is_active = False
    prize.winner_id = user_id
    prize.won_at = datetime.datetime.utcnow()
    session.commit()


def clear_wheel_user_spins(user_id: int) -> None:
    session = Database().session
    user = session.get(WheelUser, user_id)
    if user is None:
        user = WheelUser(user_id=user_id)
        session.add(user)
    user.spins = 0
    session.commit()


def ban_wheel_user(user_id: int) -> None:
    session = Database().session
    user = session.get(WheelUser, user_id)
    if user is None:
        user = WheelUser(user_id=user_id)
        session.add(user)
    user.spins = 0
    user.is_banned = True
    session.commit()
