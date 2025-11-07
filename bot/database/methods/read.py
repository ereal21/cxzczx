import datetime

import sqlalchemy
from sqlalchemy import exc, func, exists

from bot.database.models import (
    Database,
    User,
    ItemValues,
    Goods,
    Categories,
    Role,
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


def check_user(telegram_id: int) -> User | None:
    try:
        return Database().session.query(User).filter(User.telegram_id == telegram_id).one()
    except exc.NoResultFound:
        return None


def check_user_by_username(username: str) -> User | None:
    try:
        return Database().session.query(User).filter(User.username == username).one()
    except exc.NoResultFound:
        return None


def check_role(telegram_id: int) -> User | None:
    role_id = Database().session.query(User.role_id).filter(User.telegram_id == telegram_id).one()[0]
    return Database().session.query(Role.permissions).filter(Role.id == role_id).one()[0]


def check_role_name_by_id(role_id: int):
    return Database().session.query(Role.name).filter(Role.id == role_id).one()[0]


def get_role_id_by_name(role_name: str) -> int | None:
    result = Database().session.query(Role.id).filter(Role.name == role_name).one_or_none()
    if result:
        return result[0]
    return None


def select_max_role_id() -> int:
    return Database().session.query(func.max(Role.id)).scalar()


def select_today_users(date: str) -> int | None:
    try:
        date_obj = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        start_of_day = datetime.datetime.combine(date_obj, datetime.time.min)
        end_of_day = datetime.datetime.combine(date_obj, datetime.time.max)

        return Database().session.query(User).filter(
            User.registration_date >= str(start_of_day),
            User.registration_date <= str(end_of_day)
        ).count()
    except exc.NoResultFound:
        return None


def get_user_count() -> int:
    return Database().session.query(User).count()


def select_admins() -> int | None:
    try:
        return Database().session.query(func.count()).filter(User.role_id > 1).scalar()
    except exc.NoResultFound:
        return None


def get_all_users() -> list[tuple[int]]:
    return Database().session.query(User.telegram_id).all()


def item_in_stock(item_name: str) -> bool:
    """Return True if item has unlimited quantity or remaining stock."""
    if check_value(item_name):
        return True
    return select_item_values_amount(item_name) > 0


def get_all_categories() -> list[str]:
    """Return categories that contain at least one item in stock."""
    categories = [c[0] for c in Database().session.query(Categories.name)
                  .filter(Categories.parent_name.is_(None)).all()]
    result = []
    for name in categories:
        if get_all_items(name) or get_subcategories(name):
            result.append(name)
    return result


def get_all_category_names() -> list[str]:
    """Return all top-level categories regardless of contents."""
    return [c[0] for c in Database().session.query(Categories.name)
            .filter(Categories.parent_name.is_(None)).all()]


def get_all_subcategories(parent_name: str) -> list[str]:
    """Return all subcategories of a given category."""
    return [c[0] for c in Database().session.query(Categories.name)
            .filter(Categories.parent_name == parent_name).all()]


def get_subcategories(parent_name: str) -> list[str]:
    subs = [c[0] for c in Database().session.query(Categories.name)
            .filter(Categories.parent_name == parent_name).all()]
    result = []
    for sub in subs:
        if get_all_items(sub):
            result.append(sub)
    return result


def get_category_parent(category_name: str) -> str | None:
    result = (Database().session.query(Categories.parent_name)
              .filter(Categories.name == category_name).first())
    return result[0] if result else None


def get_all_items(category_name: str) -> list[str]:
    items = [item[0] for item in
             Database().session.query(Goods.name)
             .filter(Goods.category_name == category_name).all()]
    return [name for name in items if item_in_stock(name)]


def get_all_item_names(category_name: str) -> list[str]:
    """Return all items for a category regardless of stock."""
    return [item[0] for item in
            Database().session.query(Goods.name)
            .filter(Goods.category_name == category_name).all()]


def get_bought_item_info(item_id: str) -> dict | None:
    result = Database().session.query(BoughtGoods).filter(BoughtGoods.id == item_id).first()
    return result.__dict__ if result else None


def get_item_info(item_name: str) -> dict | None:
    result = Database().session.query(Goods).filter(Goods.name == item_name).first()
    return result.__dict__ if result else None


def get_user_balance(telegram_id: int) -> float | None:
    result = Database().session.query(User.balance).filter(User.telegram_id == telegram_id).first()
    return result[0] if result else None


def get_user_language(telegram_id: int) -> str | None:
    result = Database().session.query(User.language).filter(User.telegram_id == telegram_id).first()
    return result[0] if result else None


def get_all_admins() -> list[int]:
    return [admin[0] for admin in Database().session.query(User.telegram_id).filter(User.role_id == 'ADMIN').all()]


def check_item(item_name: str) -> dict | None:
    result = Database().session.query(Goods).filter(Goods.name == item_name).first()
    return result.__dict__ if result else None


def check_category(category_name: str) -> dict | None:
    result = Database().session.query(Categories).filter(Categories.name == category_name).first()
    return result.__dict__ if result else None


def get_item_value(item_name: str) -> dict | None:
    result = Database().session.query(ItemValues).filter(ItemValues.item_name == item_name).first()
    return result.__dict__ if result else None


def get_item_values(item_name: str):
    return Database().session.query(ItemValues).filter(ItemValues.item_name == item_name).all()


def get_active_wheel_prizes() -> list[WheelPrize]:
    session = Database().session
    return session.query(WheelPrize).filter(WheelPrize.is_active.is_(True)).all()


def get_wheel_user(user_id: int) -> WheelUser | None:
    return Database().session.get(WheelUser, user_id)


def get_wheel_user_spins(user_id: int) -> int:
    user = get_wheel_user(user_id)
    return user.spins if user else 0


def count_user_wheel_wins(user_id: int) -> int:
    session = Database().session
    return session.query(func.count()).filter(WheelPrize.winner_id == user_id).scalar() or 0


def get_wheel_users(include_banned: bool = False) -> list[WheelUser]:
    session = Database().session
    query = session.query(WheelUser)
    if not include_banned:
        query = query.filter(WheelUser.is_banned.is_(False))
    return query.order_by(WheelUser.user_id).all()


def get_item_value_by_id(value_id: int) -> dict | None:
    result = Database().session.query(ItemValues).filter(ItemValues.id == value_id).first()
    return result.__dict__ if result else None


def select_item_values_amount(item_name: str) -> int:
    return Database().session.query(func.count()).filter(ItemValues.item_name == item_name).scalar()


def check_value(item_name: str) -> bool | None:
    try:
        result = False
        values = select_item_values_amount(item_name)
        for i in range(values):
            is_inf = Database().session.query(ItemValues).filter(ItemValues.item_name == item_name).first()
            if is_inf and is_inf.is_infinity:
                result = True
    except exc.NoResultFound:
        return False
    return result


def select_user_items(buyer_id: int) -> int:
    return Database().session.query(func.count()).filter(BoughtGoods.buyer_id == buyer_id).scalar()


def select_bought_items(buyer_id: int) -> list[str]:
    return Database().session.query(BoughtGoods).filter(BoughtGoods.buyer_id == buyer_id).all()


def select_bought_item(unique_id: int) -> dict | None:
    result = Database().session.query(BoughtGoods).filter(BoughtGoods.unique_id == unique_id).first()
    return result.__dict__ if result else None


def bought_items_list(buyer_id: int) -> list[str]:
    return [
        item[0] for item in
        Database().session.query(BoughtGoods.item_name).filter(BoughtGoods.buyer_id == buyer_id).all()]


def get_purchase_dates() -> list[str]:
    return [d[0] for d in Database().session.query(func.date(BoughtGoods.bought_datetime)).distinct().all()]


def get_purchases_by_date(date: str) -> list[dict]:
    rows = (
        Database().session.query(BoughtGoods)
        .filter(func.date(BoughtGoods.bought_datetime) == date)
        .all()
    )
    return [r.__dict__ for r in rows]


def select_all_users() -> int:
    return Database().session.query(func.count()).filter(User).scalar()


def select_count_items() -> int:
    return Database().session.query(ItemValues).count()


def select_count_goods() -> int:
    return Database().session.query(Goods).count()


def select_count_categories() -> int:
    return Database().session.query(Categories).count()


def select_count_bought_items() -> int:
    return Database().session.query(BoughtGoods).count()


def select_today_orders(date: str) -> int | None:
    try:
        date_obj = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        start_of_day = datetime.datetime.combine(date_obj, datetime.time.min)
        end_of_day = datetime.datetime.combine(date_obj, datetime.time.max)

        return (
                Database().session.query(func.sum(BoughtGoods.price))
                .filter(
                    func.date(BoughtGoods.bought_datetime) >= start_of_day.date(),
                    func.date(BoughtGoods.bought_datetime) <= end_of_day.date()
                )
                .scalar() or 0
        )
    except exc.NoResultFound:
        return None


def select_all_orders() -> float:
    return Database().session.query(func.sum(BoughtGoods.price)).scalar() or 0


def select_today_operations(date: str) -> int | None:
    try:
        date_obj = datetime.datetime.strptime(date, "%Y-%m-%d").date()
        start_of_day = datetime.datetime.combine(date_obj, datetime.time.min)
        end_of_day = datetime.datetime.combine(date_obj, datetime.time.max)

        return (
                Database().session.query(func.sum(Operations.operation_value))
                .filter(
                    func.date(Operations.operation_time) >= start_of_day.date(),
                    func.date(Operations.operation_time) <= end_of_day.date()
                )
                .scalar() or 0
        )
    except exc.NoResultFound:
        return None


def select_all_operations() -> float:
    return Database().session.query(func.sum(Operations.operation_value)).scalar() or 0


def select_users_balance() -> float:
    return Database().session.query(func.sum(User.balance)).scalar()


def select_user_operations(user_id: int) -> list[float]:
    return [operation[0] for operation in
            Database().session.query(Operations.operation_value).filter(Operations.user_id == user_id).all()]


def select_unfinished_operations(operation_id: str) -> list[int] | None:
    try:
        return Database().session.query(UnfinishedOperations.operation_value).filter(
            UnfinishedOperations.operation_id == operation_id).one()
    except sqlalchemy.exc.NoResultFound:
        return None


def get_unfinished_operation(operation_id: str) -> tuple[int, int, int | None] | None:
    """Return (user_id, operation_value, message_id) for unfinished operation."""
    result = (
        Database()
        .session.query(
            UnfinishedOperations.user_id,
            UnfinishedOperations.operation_value,
            UnfinishedOperations.message_id,
        )
        .filter(UnfinishedOperations.operation_id == operation_id)
        .first()
    )
    return (result.user_id, result.operation_value, result.message_id) if result else None


def check_user_referrals(user_id: int) -> list[int]:
    return Database().session.query(User).filter(User.referral_id == user_id).count()


def get_user_referral(user_id: int) -> int | None:
    result = Database().session.query(User.referral_id).filter(User.telegram_id == user_id).first()
    return result[0] if result else None


def get_promocode(code: str) -> dict | None:
    session = Database().session
    promo = (
        session.query(PromoCode)
        .filter(PromoCode.code == code, PromoCode.active.is_(True))
        .first()
    )
    if not promo:
        return None
    data = {k: v for k, v in promo.__dict__.items() if not k.startswith('_')}
    geo_rows = session.query(PromoCodeGeo).filter(PromoCodeGeo.code == code).all()
    filter_rows = (
        session.query(PromoCodeProductFilter)
        .filter(PromoCodeProductFilter.code == code)
        .all()
    )
    data['geo_targets'] = [{'city': row.city, 'district': row.district} for row in geo_rows]
    data['product_filters'] = [
        {
            'type': row.target_type,
            'name': row.target_name,
            'is_allowed': row.is_allowed,
        }
        for row in filter_rows
    ]
    return data


def get_all_promocodes() -> list[PromoCode]:
    return Database().session.query(PromoCode).filter(PromoCode.active.is_(True)).all()


def is_promocode_used(user_id: int, code: str, item_name: str) -> bool:
    return (Database().session.query(UsedPromoCode)
            .filter(UsedPromoCode.user_id == user_id,
                    UsedPromoCode.code == code,
                    UsedPromoCode.item_name == item_name)
            .first() is not None)


def has_used_promo_for_item(user_id: int, item_name: str) -> bool:
    """Return True if the user already applied any promo code for the given item."""
    session = Database().session
    subq = session.query(UsedPromoCode.id).filter(
        UsedPromoCode.user_id == user_id,
        UsedPromoCode.item_name == item_name,
    )
    return session.query(subq.exists()).scalar()


def get_promocode_usage_by_geo(code: str) -> list[tuple[str | None, str | None, int]]:
    session = Database().session
    rows = (
        session.query(UsedPromoCode.city, UsedPromoCode.district, func.count())
        .filter(UsedPromoCode.code == code)
        .group_by(UsedPromoCode.city, UsedPromoCode.district)
        .order_by(UsedPromoCode.city, UsedPromoCode.district)
        .all()
    )
    return [(city, district, count) for city, district, count in rows]


def get_product_change_log(item_name: str, limit: int = 10) -> list[ProductChangeLog]:
    session = Database().session
    return (
        session.query(ProductChangeLog)
        .filter(ProductChangeLog.item_name == item_name)
        .order_by(ProductChangeLog.id.desc())
        .limit(limit)
        .all()
    )
