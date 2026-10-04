import asyncio
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from loguru import logger
import re

from src.config import settings
from src.storage import db
from src.services.bot.livelog import LiveLogMessage, RunProgress, get_buffer
from src.services.bot.keyboards import (
    main_menu_keyboard,
    users_list_inline_keyboard,
    confirm_delete_keyboard,
    back_button,
)
from src.services.monitor import monitor_service, clean_html
from src.services.ai import ai_service
from src.services.gsheets import gsheets_service

# Initialize Bot and Dispatcher
bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
router = Router()

# One parsing run at a time -- two runs would fight over the same live message.
_run_lock = asyncio.Lock()


# FSM States
class Form(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_user_name = State()


# --- Middleware / Check Auth ---
# --- Middleware / Check Auth ---
def is_authorized(user_id: int) -> bool:
    """Checks if user is allowed to use the bot (DB or ENV)."""
    # Check DB
    if user_id in db.get_users():
        return True

    # Check Environment Variables (Auto-add to DB if SUPER ADMIN)
    if user_id in settings.TELEGRAM_ADMIN_IDS:
        logger.info(f"Super Admin {user_id} found in ENV. Auto-adding to DB.")
        db.add_user(user_id, name="Super Admin")
        return True

    return False


def is_super_admin(user_id: int) -> bool:
    """Checks if user is a Super Admin (ENV only)."""
    # settings.TELEGRAM_ADMIN_IDS is a list of ints
    return user_id in settings.TELEGRAM_ADMIN_IDS


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен. Обратитесь к администратору.")
        return

    admin_access = is_super_admin(message.from_user.id)
    await message.answer(
        "👋 Привет! Я бот-монитор конкурентов.",
        reply_markup=main_menu_keyboard(is_admin=admin_access),
    )


# 2. Users Management - Menu
@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return

    admin_access = is_super_admin(message.from_user.id)
    await message.answer(
        "Главное меню", reply_markup=main_menu_keyboard(is_admin=admin_access)
    )


@router.message(F.text == "🔙 Назад")
async def cmd_back(message: types.Message, state: FSMContext):
    await state.clear()

    if not is_authorized(message.from_user.id):
        return

    admin_access = is_super_admin(message.from_user.id)
    await message.answer(
        "Главное меню", reply_markup=main_menu_keyboard(is_admin=admin_access)
    )


@router.message(F.text == "🚀 Начать парсинг")
async def cmd_manual_parse(message: types.Message):
    if not is_authorized(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return

    if _run_lock.locked():
        await message.answer("⏳ Парсинг уже идёт, дождись окончания.")
        return

    # Run in background to not block bot. The live log message is the reply.
    asyncio.create_task(run_parsing_job(chat_id=message.chat.id))


@router.message(F.text == "👥 Пользователи")
async def cmd_manage_users(message: types.Message):
    # RBAC: Only Super Admin
    if not is_super_admin(message.from_user.id):
        await message.answer("⛔ Только для Главного Администратора.")
        return

    # Fetch full user dicts
    users_full = db.get_users_full()

    # Text message + Inline Buttons
    await message.answer(
        f"👥 Список пользователей ({len(users_full)}):",
        reply_markup=users_list_inline_keyboard(users_full),
    )


# 2.1 Remove User Flow (Inline Callbacks)
@router.callback_query(F.data.startswith("remove_ask:"))
async def cb_remove_ask(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        await call.answer("⛔ Только для Главного Администратора.", show_alert=True)
        return

    uid_to_remove = int(call.data.split(":")[1])

    # Check if self
    if uid_to_remove == call.from_user.id:
        await call.answer("Нельзя удалить самого себя!", show_alert=True)
        return

    # Ask confirmation replacing the text/keyboard
    await call.message.edit_text(
        f"⚠️ Вы уверены, что хотите удалить пользователя {uid_to_remove}?",
        reply_markup=confirm_delete_keyboard(uid_to_remove),
    )


@router.callback_query(F.data.startswith("remove_confirm:"))
async def cb_remove_confirm(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        return

    uid_to_remove = int(call.data.split(":")[1])

    db.remove_user(uid_to_remove)

    # Go back to list
    users_full = db.get_users_full()
    await call.message.edit_text(
        f"❌ Пользователь {uid_to_remove} удален.\n👥 Список пользователей:",
        reply_markup=users_list_inline_keyboard(users_full),
    )


@router.callback_query(F.data == "remove_cancel")
async def cb_remove_cancel(call: types.CallbackQuery):
    if not is_super_admin(call.from_user.id):
        return

    # Just show list again
    users_full = db.get_users_full()
    await call.message.edit_text(
        f"👥 Список пользователей ({len(users_full)}):",
        reply_markup=users_list_inline_keyboard(users_full),
    )


# 2.2 Add User Flow (2 Steps)


@router.callback_query(F.data == "add_user_start")
async def cb_add_user_start(call: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(call.from_user.id):
        await call.answer("⛔ Только для Главного Администратора.", show_alert=True)
        return

    # Answer callback to stop loading animation
    await call.answer()

    # Trigger the prompt using logic similar to cmd_add_user_prompt
    await call.message.answer(
        "1️⃣ Введите Telegram ID пользователя:", reply_markup=back_button()
    )
    await state.set_state(Form.waiting_for_user_id)


@router.message(F.text == "➕ Добавить пользователя")
async def cmd_add_user_prompt(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return

    await message.answer(
        "1️⃣ Введите Telegram ID пользователя:", reply_markup=back_button()
    )
    await state.set_state(Form.waiting_for_user_id)


@router.message(Form.waiting_for_user_id)
async def process_add_user_id(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await cmd_back(message, state)
        return

    try:
        new_uid = int(message.text.strip())

        # Check logic inside add_user handles duplicates return False
        # But we need name now. So just save ID to state.
        await state.update_data(new_uid=new_uid)

        await message.answer(
            f"✅ ID {new_uid} принят.\n2️⃣ Введите Имя (или заметку) для пользователя:",
            reply_markup=back_button(),
        )
        await state.set_state(Form.waiting_for_user_name)

    except ValueError:
        await message.answer(
            "⚠️ ID должен быть числом. Попробуйте еще раз.", reply_markup=back_button()
        )


@router.message(Form.waiting_for_user_name)
async def process_add_user_name(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await cmd_back(message, state)
        return

    name = message.text.strip()
    data = await state.get_data()
    new_uid = data.get("new_uid")

    # Perform Add
    if db.add_user(new_uid, name):
        # Success
        admin_access = is_super_admin(message.from_user.id)
        await message.answer(
            f"✅ Пользователь добавлен!\n🆔 {new_uid}\n👤 {name}",
            reply_markup=main_menu_keyboard(is_admin=admin_access),
        )
    else:
        admin_access = is_super_admin(message.from_user.id)
        await message.answer(
            f"⚠️ Пользователь {new_uid} уже существует!",
            reply_markup=main_menu_keyboard(is_admin=admin_access),
        )

    await state.clear()


# --- Integration Logic ---


async def broadcast_notification(text: str):
    """Sends a message to all authorized users."""
    users = db.get_users()
    for uid in users:
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception as e:
            logger.warning(
                f"Failed to send HTML to {uid}, failing back to plain text: {e}"
            )
            # Fallback: Strip HTML
            clean_text = clean_html(text)
            try:
                await bot.send_message(
                    chat_id=uid, text=clean_text
                )  # Default parse_mode
            except Exception as e2:
                logger.error(f"Failed to send plain text to {uid}: {e2}")
        # Different chats, so this only protects the global ~30 msg/s budget.
        await asyncio.sleep(0.35)


async def run_parsing_job(chat_id: int | None = None) -> bool:
    """
    Runs the monitor service once.

    With ``chat_id`` the run reports into a single self-updating live log
    message in that chat (one editor, one chat, no flood risk). Without it
    (08:00 cron) it behaves exactly as before: plain start/finish broadcasts.
    Returns False if a run is already in progress.
    """
    if _run_lock.locked():
        logger.warning("Parsing job skipped: a run is already in progress.")
        return False

    async with _run_lock:
        await _run_locked(chat_id)
    return True


async def _run_locked(chat_id: int | None) -> None:
    live: LiveLogMessage | None = None
    logger.info(
        "Manual parsing triggered." if chat_id is not None else "Scheduled parsing triggered."
    )

    if chat_id is not None and settings.TELEGRAM_LIVE_LOG:
        live = LiveLogMessage(
            bot,
            chat_id,
            get_buffer(),
            RunProgress(),
            interval=settings.TELEGRAM_LIVE_LOG_INTERVAL,
            min_gap=settings.TELEGRAM_LIVE_LOG_MIN_GAP,
            chat_type="private",
        )
        try:
            await live.start()
        except Exception as e:
            logger.warning(f"Live log unavailable, falling back: {e}")
            live = None

    if live is None:
        await broadcast_notification("🔄 Запущен поиск обновлений...")

    ok = False
    try:
        await monitor_service.run_check_cycle(progress=live.progress if live else None)
        ok = True
    except Exception as e:
        logger.error(f"Parsing job failed: {e}")
        await broadcast_notification(f"⚠️ Ошибка при парсинге: {e}")
    finally:
        if live is not None:
            try:
                await live.stop()
            except Exception as e:
                logger.warning(f"Live log final update failed: {e}")
        elif chat_id is None:
            await broadcast_notification(
                "✅ Поиск завершен." if ok else "⚠️ Парсинг прерван."
            )


async def run_startup_job():
    """Runs a silent check cycle on startup (init or update)."""
    logger.info("🚀 Running startup initialization (background)...")
    try:
        # We do NOT broadcast start/finish, only actual finding results (handled inside monitor)
        await monitor_service.run_check_cycle()
        logger.info("✅ Startup initialization complete.")
    except Exception as e:
        logger.error(f"Startup initialization failed: {e}")


# Register callback in monitor service
monitor_service.notification_callback = broadcast_notification

dp.include_router(router)
