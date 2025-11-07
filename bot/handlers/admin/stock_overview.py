from __future__ import annotations

from typing import Iterable

from aiogram import Dispatcher
from aiogram.types import CallbackQuery

from bot.database.methods import (
    check_role,
    get_all_category_names,
    get_all_item_names,
    get_all_subcategories,
    check_value,
    select_item_values_amount,
)
from bot.database.models import Permission
from bot.handlers.other import get_bot_user_ids
from bot.keyboards import back
from bot.logger_mesh import logger
from bot.utils import display_name
from bot.utils.safe_sender import safe_send_message


def _collect_category_lines(category: str, depth: int = 0) -> tuple[list[str], int]:
    lines: list[str] = []
    indent = "    " * depth
    prefix = "📂" if depth == 0 else "📁"
    lines.append(f"{indent}{prefix} {category}")

    items = get_all_item_names(category)
    total_items = 0
    for item in items:
        amount = '∞' if check_value(item) else select_item_values_amount(item)
        lines.append(f"{indent}  • {display_name(item)} — {amount}")
        total_items += 1

    subcategories = get_all_subcategories(category)
    for sub in subcategories:
        sub_lines, sub_count = _collect_category_lines(sub, depth + 1)
        lines.extend(sub_lines)
        total_items += sub_count

    return lines, total_items


def _build_stock_overview() -> tuple[list[str], int, int]:
    categories = get_all_category_names()
    if not categories:
        return ["📭 No categories or products found."], 0, 0

    overview_lines: list[str] = []
    total_items = 0
    for index, category in enumerate(categories):
        lines, item_count = _collect_category_lines(category)
        overview_lines.extend(lines)
        if index != len(categories) - 1:
            overview_lines.append("")
        total_items += item_count
    return overview_lines, total_items, len(categories)


def _chunk_lines(lines: Iterable[str], max_length: int = 3800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line = line.rstrip()
        line_length = len(line) + 1  # account for newline
        if current and current_len + line_length > max_length:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += line_length

    if current:
        chunks.append("\n".join(current))

    return chunks


async def stock_overview_callback_handler(call: CallbackQuery) -> None:
    bot, user_id = await get_bot_user_ids(call)
    role = check_role(user_id)
    if not role & Permission.OWN:
        await call.answer("Insufficient rights")
        return

    await call.answer()
    lines, total_items, category_count = _build_stock_overview()
    header = [
        "📊 Stock overview",
        f"📁 Categories: {category_count}",
        f"🏷️ Products: {total_items}",
        "",
    ]
    chunks = _chunk_lines(header + lines)

    if not chunks:
        chunks = ["📊 Stock overview\n\n📭 No categories or products found."]

    first_chunk = chunks[0]
    if not first_chunk.startswith("📊 Stock overview"):
        first_chunk = "📊 Stock overview\n\n" + first_chunk

    await bot.edit_message_text(
        first_chunk,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=back("console"),
    )

    for chunk in chunks[1:]:
        await safe_send_message(call.message, chunk)

    logger.info("User %s viewed stock overview", user_id)


def register_stock_overview(dp: Dispatcher) -> None:
    dp.register_callback_query_handler(
        stock_overview_callback_handler,
        lambda c: c.data == "view_stock_overview",
        state="*",
    )
