import datetime
import random
from decimal import Decimal, ROUND_HALF_UP

import sqlalchemy.exc

from bot.database.models import (
    User,
    ItemValues,
    Goods,
    Categories,
    BoughtGoods,
    Operations,
    UnfinishedOperations,
    PromoCode,
    UsedPromoCode,
    PromoCodeGeo,
    PromoCodeProductFilter,
    ProductChangeLog,
    WheelPrize,
    WheelUser,
)
from bot.database import Database


def _quantize_price(value: Decimal | float | int | str) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def create_user(telegram_id: int, registration_date, referral_id, role: int = 1,
                language: str | None = None, username: str | None = None) -> None:
    session = Database().session
    try:
        user = session.query(User).filter(User.telegram_id == telegram_id).one()
        if user.username != username:
            user.username = username
            session.commit()
    except sqlalchemy.exc.NoResultFound:
        if referral_id != '':
            session.add(
                User(telegram_id=telegram_id, role_id=role, registration_date=registration_date,
                     referral_id=referral_id, language=language, username=username))
            session.commit()
        else:
            session.add(
                User(telegram_id=telegram_id, role_id=role, registration_date=registration_date,
                     referral_id=None, language=language, username=username))
            session.commit()


def create_item(item_name: str, item_description: str, item_price, category_name: str,
                delivery_description: str | None = None) -> None:
    session = Database().session
    session.add(
        Goods(name=item_name, description=item_description, price=_quantize_price(item_price),
              category_name=category_name, delivery_description=delivery_description))
    session.commit()


def add_values_to_item(item_name: str, value: str, is_infinity: bool) -> None:
    session = Database().session
    if is_infinity is False:
        session.add(
            ItemValues(name=item_name, value=value, is_infinity=False))
    else:
        session.add(
            ItemValues(name=item_name, value=value, is_infinity=True))
    session.commit()


def create_category(category_name: str, parent: str | None = None) -> None:
    session = Database().session
    session.add(
        Categories(name=category_name, parent_name=parent))
    session.commit()


def create_operation(user_id: int, value: int, operation_time: str) -> None:
    session = Database().session
    session.add(
        Operations(user_id=user_id, operation_value=value, operation_time=operation_time))
    session.commit()


def start_operation(user_id: int, value: int, operation_id: str, message_id: int | None = None) -> None:
    session = Database().session
    session.add(
        UnfinishedOperations(user_id=user_id, operation_value=value, operation_id=operation_id, message_id=message_id))
    session.commit()


def add_bought_item(item_name: str, value: str, price: int, buyer_id: int,
                    bought_time: str) -> int:
    session = Database().session
    unique_id = random.randint(1000000000, 9999999999)
    session.add(
        BoughtGoods(name=item_name, value=value, price=price, buyer_id=buyer_id, bought_datetime=bought_time,
                    unique_id=str(unique_id)))
    session.commit()
    return unique_id


def _persist_promocode_geo(session, code: str, geo_targets: list[tuple[str, str | None]]) -> None:
    session.query(PromoCodeGeo).filter(PromoCodeGeo.code == code).delete()
    for city, district in geo_targets:
        session.add(PromoCodeGeo(code=code, city=city, district=district))


def _persist_promocode_filters(
    session,
    code: str,
    allowed: list[tuple[str, str]],
    excluded: list[tuple[str, str]],
) -> None:
    session.query(PromoCodeProductFilter).filter(PromoCodeProductFilter.code == code).delete()
    for target_type, target_name in allowed:
        session.add(
            PromoCodeProductFilter(
                code=code,
                target_type=target_type,
                target_name=target_name,
                is_allowed=True,
            )
        )
    for target_type, target_name in excluded:
        session.add(
            PromoCodeProductFilter(
                code=code,
                target_type=target_type,
                target_name=target_name,
                is_allowed=False,
            )
        )


def create_promocode(
    code: str,
    discount: int,
    expires_at: str | None,
    geo_targets: list[tuple[str, str | None]] | None = None,
    allowed_filters: list[tuple[str, str]] | None = None,
    excluded_filters: list[tuple[str, str]] | None = None,
) -> None:
    session = Database().session
    session.add(PromoCode(code=code, discount=discount, expires_at=expires_at, active=True))
    geo_targets = geo_targets or []
    allowed_filters = allowed_filters or []
    excluded_filters = excluded_filters or []
    _persist_promocode_geo(session, code, geo_targets)
    _persist_promocode_filters(session, code, allowed_filters, excluded_filters)
    session.commit()


def mark_promocode_used(
    user_id: int,
    code: str,
    item_name: str,
    city: str | None = None,
    district: str | None = None,
) -> None:
    session = Database().session
    session.add(
        UsedPromoCode(
            user_id=user_id,
            code=code,
            item_name=item_name,
            city=city,
            district=district,
        )
    )
    session.commit()


def log_product_change(
    item_name: str,
    field: str,
    old_value: str | None,
    new_value: str | None,
    changed_by: int,
    changed_at: datetime.datetime | None = None,
) -> None:
    session = Database().session
    moment = changed_at or datetime.datetime.utcnow()
    session.add(
        ProductChangeLog(
            item_name=item_name,
            field=field,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
            changed_at=moment,
        )
    )
    session.commit()


def create_wheel_prize(
    name: str,
    location: str,
    emoji: str,
    photo_file_id: str | None,
) -> WheelPrize:
    session = Database().session
    prize = WheelPrize(name=name, location=location, emoji=emoji, photo_file_id=photo_file_id)
    session.add(prize)
    session.commit()
    session.refresh(prize)
    return prize


def ensure_wheel_user(user_id: int) -> WheelUser:
    session = Database().session
    user = session.get(WheelUser, user_id)
    if user is None:
        user = WheelUser(user_id=user_id)
        session.add(user)
        session.commit()
    return user
