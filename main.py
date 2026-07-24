"""
Telegram-бот-посредник для связи клиентов с консультантом
по вопросам городской парковки.

Бот НЕ собирает и не сохраняет персональные данные — только пересылает
сообщения между клиентом и консультантом (AGENT_CHAT_ID). Вся маршрутизация
хранится в оперативной памяти (словарь) и исчезает при перезапуске процесса.

Стек: Python 3.10+, aiogram 3.x.

Запуск:
    1. pip install aiogram==3.*
    2. Задать переменные окружения BOT_TOKEN и AGENT_CHAT_ID
    3. python main.py
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --------------------------------------------------------------------------
# Конфигурация из переменных окружения
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
AGENT_CHAT_ID_RAW = os.environ.get("AGENT_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
if not AGENT_CHAT_ID_RAW:
    raise RuntimeError("Не задана переменная окружения AGENT_CHAT_ID")

try:
    AGENT_CHAT_ID = int(AGENT_CHAT_ID_RAW)
except ValueError as exc:
    raise RuntimeError("AGENT_CHAT_ID должен быть целым числом") from exc

# --------------------------------------------------------------------------
# Тексты бота
# --------------------------------------------------------------------------

TEXT_WELCOME = (
    "Добро пожаловать! Мы помогаем разобраться в вопросах городской "
    "парковки. Выберите нужный раздел ниже."
)

TEXT_ABOUT = (
    "Консультационный центр по вопросам транспортной инфраструктуры. "
    "Работаем с 2020 года. Все консультации бесплатны (для общего "
    "ознакомления)."
)

TEXT_INFO = (
    "Рекомендуем проверять актуальные правила парковки на официальном "
    "сайте администрации вашего города или в приложении \"Парковки России\"."
)

TEXT_CONNECTED = (
    "Вы связаны со специалистом. Напишите ваш вопрос. Для завершения "
    "отправьте /cancel."
)

TEXT_CANCELLED = "Вы вышли из диалога. Всегда рады помочь!"

TEXT_USE_BUTTONS = "Пожалуйста, используйте кнопки меню."

TEXT_SEND_ERROR = (
    "Произошла ошибка, попробуйте позже или свяжитесь с нами по другому "
    "каналу."
)

BTN_ASK = "Задать вопрос консультанту"
BTN_INFO = "Информация о парковках"
BTN_ABOUT = "О нас"

# --------------------------------------------------------------------------
# Логирование — только в консоль, без файлов
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

# --------------------------------------------------------------------------
# Хранилище маршрутизации в оперативной памяти
# --------------------------------------------------------------------------
# Сопоставление "сообщение консультанта -> клиент", которому нужно
# переслать ответ. Ключ — message_id пересланного сообщения в чате
# консультанта, значение — client_id. Хранится только в памяти процесса
# и не сохраняется на диск; при перезапуске бота обнуляется.

forwarded_map: dict[int, int] = {}

# --------------------------------------------------------------------------
# Клавиатуры
# --------------------------------------------------------------------------


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_ASK, callback_data="ask")],
            [InlineKeyboardButton(text=BTN_INFO, callback_data="info")],
            [InlineKeyboardButton(text=BTN_ABOUT, callback_data="about")],
        ]
    )


# --------------------------------------------------------------------------
# FSM-состояние: клиент находится в режиме диалога с консультантом
# --------------------------------------------------------------------------


class ProxyMode(StatesGroup):
    active = State()


# --------------------------------------------------------------------------
# Роутер и хендлеры
# --------------------------------------------------------------------------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(TEXT_WELCOME, reply_markup=main_menu_kb())


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery) -> None:
    await callback.message.answer(TEXT_ABOUT)
    await callback.answer()


@router.callback_query(F.data == "info")
async def cb_info(callback: CallbackQuery) -> None:
    await callback.message.answer(TEXT_INFO)
    await callback.answer()


@router.callback_query(F.data == "ask")
async def cb_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProxyMode.active)
    await callback.message.answer(TEXT_CONNECTED)
    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state == ProxyMode.active.state:
        await state.clear()
        await message.answer(TEXT_CANCELLED)
    else:
        # /cancel вне режима прокси — просто ничего не делаем/подтверждаем
        await message.answer(TEXT_CANCELLED)


# --- Сообщения клиента в режиме прокси: пересылаем консультанту ---------


@router.message(ProxyMode.active, F.text)
async def relay_to_agent(message: Message, bot: Bot) -> None:
    client_id = message.from_user.id
    try:
        # Пересылаем оригинальное сообщение консультанту (без изменений,
        # без сохранения текста где-либо, кроме памяти для маршрутизации)
        forwarded = await bot.forward_message(
            chat_id=AGENT_CHAT_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        # Запоминаем, какому клиенту нужно вернуть ответ консультанта
        forwarded_map[forwarded.message_id] = client_id
        logger.info("Сообщение клиента %s переслано консультанту", client_id)
    except Exception as exc:  # noqa: BLE001 — намеренно широкий except
        logger.error("Не удалось переслать сообщение консультанту: %s", exc)
        await message.answer(TEXT_SEND_ERROR)


# --- Ответ консультанта: пересылаем обратно клиенту ---------------------


@router.message(F.chat.id == AGENT_CHAT_ID, F.reply_to_message)
async def relay_to_client(message: Message, bot: Bot) -> None:
    """
    Консультант отвечает на пересланное сообщение (Reply) — бот находит
    исходного клиента по словарю маршрутизации и пересылает ответ.
    """
    original_message_id = message.reply_to_message.message_id
    client_id = forwarded_map.get(original_message_id)

    if client_id is None:
        # Не найдено соответствие — вероятно, это не ответ на пересланное
        # сообщение клиента, игнорируем.
        return

    try:
        await bot.send_message(chat_id=client_id, text=message.text or message.caption or "")
        logger.info("Ответ консультанта переслан клиенту %s", client_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось доставить ответ клиенту %s: %s", client_id, exc)
        await message.answer(TEXT_SEND_ERROR)


# --- Любой другой текст вне режима прокси и не от консультанта ----------


@router.message(F.text)
async def fallback_text(message: Message) -> None:
    if message.chat.id == AGENT_CHAT_ID:
        # Сообщение от консультанта, не являющееся ответом (Reply) —
        # ничего не пересылаем, т.к. не знаем получателя.
        return
    await message.answer(TEXT_USE_BUTTONS)


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
