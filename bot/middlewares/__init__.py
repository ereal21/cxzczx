from aiogram import Dispatcher

from .antispam import setup_antispam


def setup_middlewares(dp: Dispatcher) -> None:
    """Register all middlewares used by the bot."""
    setup_antispam(dp)
