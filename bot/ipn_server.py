from flask import Flask, request, abort
import datetime
import hmac
import hashlib
import asyncio
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.localization import t

from bot.misc import EnvKeys, TgConfig
from bot.database import Database
from bot.database.models.main import UnfinishedOperations
from bot.database.methods import (
    finish_operation,
    create_operation,
    update_balance,
    get_user_referral,
    get_user_language,
)
from bot.logger_mesh import logger
from bot.utils.security import SecurityManager
from bot.utils.notifications import notify_owner_of_topup

app = Flask(__name__)


def verify_signature(data: bytes, signature: str | None) -> bool:
    if not EnvKeys.NOWPAYMENTS_IPN_SECRET:
        return True
    if not signature:
        return False
    calc = hmac.new(
        EnvKeys.NOWPAYMENTS_IPN_SECRET.encode(),
        data,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(calc, signature)


@app.route("/nowpayments-ipn", methods=["POST"])
@app.route("/", methods=["POST"])  # fallback if IPN path omitted
def nowpayments_ipn():
    SecurityManager.cleanup()
    ip_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    if "," in ip_addr:
        ip_addr = ip_addr.split(",", 1)[0].strip()

    if SecurityManager.is_ip_blocked(ip_addr):
        logger.warning("Blocked IP %s attempted to access %s", ip_addr, request.path)
        abort(429)

    allowed, reason = SecurityManager.record_ip_request(ip_addr)
    if not allowed:
        logger.warning(
            "Rate limiting %s for %s due to %s", ip_addr, request.path, reason
        )
        if reason == "anomaly":
            SecurityManager.record_ip_failure(ip_addr, "anomalous_activity")
        return "", 429

    if not verify_signature(request.data, request.headers.get("x-nowpayments-sig")):
        SecurityManager.record_ip_failure(ip_addr, "invalid_signature")
        abort(400)

    # try to parse JSON regardless of Content-Type header
    data = request.get_json(force=True, silent=True) or {}
    payment_id_raw = data.get("payment_id")
    status = data.get("payment_status")
    if not payment_id_raw or not status:
        SecurityManager.record_ip_failure(ip_addr, "missing_fields")
        return "", 400
    payment_id = str(payment_id_raw)

    if status in ("finished", "confirmed", "sending", "paid", "partially_paid"):
        session = Database().session
        record = (
            session.query(UnfinishedOperations)
            .filter(UnfinishedOperations.operation_id == payment_id)
            .first()
        )
        if record:
            value = record.operation_value
            user_id = record.user_id
            message_id = record.message_id
            finish_operation(payment_id)
            formatted_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            create_operation(user_id, value, formatted_time)
            update_balance(user_id, value)

            referral_id = get_user_referral(user_id)
            if referral_id and TgConfig.REFERRAL_PERCENT != 0:
                referral_operation = round((TgConfig.REFERRAL_PERCENT / 100) * value)
                update_balance(referral_id, referral_operation)

            logger.info(
                "NOWPayments IPN confirmed payment %s for user %s from %s",
                payment_id,
                user_id,
                ip_addr,
            )

            # notify user and delete invoice
            bot = Bot(token=EnvKeys.TOKEN, parse_mode="HTML")
            lang = get_user_language(user_id) or 'en'
            markup = InlineKeyboardMarkup().add(
                InlineKeyboardButton(t(lang, 'back_home'), callback_data='home_menu')
            )
            asyncio.run(bot.delete_message(chat_id=user_id, message_id=message_id))
            asyncio.run(
                bot.send_message(
                    chat_id=user_id,
                    text=t(lang, 'payment_successful', amount=value),
                    reply_markup=markup,
                )
            )
            try:
                chat = asyncio.run(bot.get_chat(user_id))
                username = (
                    f"@{chat.username}" if chat and chat.username else chat.full_name or str(user_id)
                )
            except Exception:
                username = str(user_id)
            asyncio.run(notify_owner_of_topup(bot, username, float(value), formatted_time))
    logger.info("Processed IPN callback %s from %s with status %s", payment_id, ip_addr, status)
    return "", 200
