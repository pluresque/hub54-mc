import asyncio
import logging
import re
import html
import time
import unicodedata

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .config import ADMIN_TELEGRAM_ID, MINECRAFT_SERVER_ADDRESS, MINECRAFT_SERVER_VERSION
from .database import (
    add_to_whitelist,
    cast_poll_vote,
    close_poll,
    create_access_request,
    enqueue_minecraft_chat_message,
    enqueue_telegram_private_message,
    get_private_message_reply_target,
    get_account_profile,
    get_nickname,
    get_notification_preferences,
    get_poll_results,
    is_whitelisted,
    list_pending_requests,
    list_poll_deliveries,
    list_whitelisted_users,
    nickname_is_taken,
    private_message_bridge_enabled,
    remove_from_whitelist,
    resolve_access_request,
    set_notification_preference,
    save_nickname as store_nickname,
    save_password_hash,
)
from .keyboards import (
    account_keyboard,
    access_keyboard,
    access_review_keyboard,
    back_keyboard,
    main_menu_keyboard,
    notification_settings_keyboard,
)
from .passwords import hash_password
from .notifications import NOTIFICATION_LABELS, notification_settings_text
from .polls import (
    create_and_broadcast_poll,
    notify_admin_of_results,
    poll_keyboard,
    poll_message_text,
    validate_poll,
)
from .server_status import query_server_status


logger = logging.getLogger(__name__)
router = Router(name="minecraft-account")
MINECRAFT_NICKNAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
NICKNAME_PROMPT_TEXT = (
    "Введите никнейм Minecraft. Используйте от 3 до 16 латинских букв, "
    "цифр или символов подчёркивания."
)
EMPTY_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])
CHAT_RELAY_COOLDOWN_SECONDS = 2.0
PRIVATE_RELAY_COOLDOWN_SECONDS = 1.0
MAX_MINECRAFT_CHAT_UTF16_UNITS = 256
CHAT_RELAY_LOCK = asyncio.Lock()
CHAT_RELAY_LAST_SENT_AT: dict[int, float] = {}
PRIVATE_RELAY_LAST_SENT_AT: dict[int, float] = {}


class NicknameForm(StatesGroup):
    waiting_for_nickname = State()


class PasswordForm(StatesGroup):
    waiting_for_password = State()


@router.message(F.chat.type != "private")
async def reject_group_messages(message: Message, state: FSMContext) -> None:
    is_password = await state.get_state() == PasswordForm.waiting_for_password.state
    await state.clear()

    if is_password:
        deleted = False
        try:
            await message.delete()
            deleted = True
        except TelegramAPIError:
            pass
        notice = "Бот работает только в личном чате. "
        notice += (
            "Сообщение удалено. Напиши боту в личные сообщения."
            if deleted
            else (
                "Если отправил пароль, удали сообщение вручную. "
                "Напиши боту в личные сообщения."
            )
        )
        await message.answer(notice)
        return

    await message.answer("Бот работает только в личном чате. Напиши мне туда.")


async def require_admin(message: Message) -> bool:
    if message.from_user.id == ADMIN_TELEGRAM_ID:
        return True
    await message.answer("Эта команда доступна только администратору.")
    return False


def access_message_text(telegram_id: int) -> str:
    return (
        "🔒 <b>Доступ пока закрыт</b>\n\n"
        "Твоего Telegram-аккаунта пока нет в списке доступа к серверу. "
        "Нажми кнопку ниже, чтобы попросить доступ.\n"
        f"Твой Telegram ID: <code>{telegram_id}</code>"
    )


async def show_access_message(message: Message) -> None:
    await message.answer(
        access_message_text(message.from_user.id),
        reply_markup=access_keyboard(),
    )


async def edit_callback_message(
    callback: CallbackQuery, text: str, reply_markup=None
) -> bool:
    if not isinstance(callback.message, Message):
        return False
    keyboard = reply_markup if reply_markup is not None else EMPTY_KEYBOARD
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as error:
        if "message is not modified" in error.message.casefold():
            return False
        raise
    return True


async def require_private_callback(callback: CallbackQuery) -> bool:
    if (
        isinstance(callback.message, Message)
        and callback.message.chat.type == "private"
    ):
        return True
    await callback.answer("Открой бота в личном чате.", show_alert=True)
    return False


async def update_nickname_prompt(
    message: Message, state: FSMContext, text: str, reply_markup=None
) -> None:
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")
    prompt_chat_id = data.get("prompt_chat_id", message.chat.id)
    if not prompt_message_id:
        await message.answer(text, reply_markup=reply_markup)
        return

    try:
        await message.bot.edit_message_text(
            chat_id=prompt_chat_id,
            message_id=prompt_message_id,
            text=text,
            reply_markup=(
                reply_markup if reply_markup is not None else EMPTY_KEYBOARD
            ),
        )
    except TelegramAPIError:
        logger.exception("Не удалось обновить сообщение с вводом")
        await message.answer(text, reply_markup=reply_markup)


async def prompt_for_password(message: Message, state: FSMContext) -> None:
    await state.set_state(PasswordForm.waiting_for_password)
    prompt = await message.answer(
        "Теперь придумайте пароль для входа на сервер.\n\n"
        "Отправьте пароль отдельным сообщением сюда. Бот удалит его после обработки."
    )
    await state.update_data(
        prompt_chat_id=prompt.chat.id, prompt_message_id=prompt.message_id
    )


async def prompt_for_nickname_from_callback(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Сначала выбери никнейм.", show_alert=True)
        return

    await state.set_state(NicknameForm.waiting_for_nickname)
    await callback.answer()
    await edit_callback_message(callback, NICKNAME_PROMPT_TEXT)
    await state.update_data(
        prompt_chat_id=callback.message.chat.id,
        prompt_message_id=callback.message.message_id,
    )


async def show_menu(message: Message, telegram_id: int) -> None:
    nickname = await get_nickname(telegram_id)
    await message.answer(main_menu_text(nickname), reply_markup=main_menu_keyboard())


def main_menu_text(nickname: str | None) -> str:
    if nickname:
        return f"Привет, <b>{html.escape(nickname)}</b>. Выбери действие"
    return "Привет. Выбери действие"


def account_card_text(profile: tuple[str, str | None, str | None] | None) -> str:
    nickname = html.escape(profile[0]) if profile and profile[0] else "не задан"
    last_login = (
        html.escape(profile[2]) if profile and profile[2] else "нет данных"
    )
    return (
        "👤 <b>Мой аккаунт</b>\n\n"
        f"Никнейм: <b>{nickname}</b>\n"
        f"Последний заход на сервер: {last_login}"
    )


def access_request_text(telegram_id: int, username: str | None, full_name: str) -> str:
    safe_name = html.escape(full_name)
    safe_username = f"@{html.escape(username)}" if username else "не указан"
    return (
        "🔔 <b>Запрос доступа к Minecraft</b>\n\n"
        f"Имя: {safe_name}\n"
        f"Telegram: {safe_username}\n"
        f"ID: <code>{telegram_id}</code>"
    )


async def send_access_request_to_admin(
    bot: Bot, telegram_id: int, username: str | None, full_name: str
) -> None:
    await bot.send_message(
        ADMIN_TELEGRAM_ID,
        access_request_text(telegram_id, username, full_name),
        reply_markup=access_review_keyboard(telegram_id),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await state.clear()

    if not await is_whitelisted(telegram_id):
        await show_access_message(message)
        return

    profile = await get_account_profile(telegram_id)
    if profile and profile[0]:
        if not profile[1]:
            await prompt_for_password(message, state)
            return
        await show_menu(message, telegram_id)
        return

    await state.set_state(NicknameForm.waiting_for_nickname)
    prompt = await message.answer(NICKNAME_PROMPT_TEXT)
    await state.update_data(
        prompt_chat_id=prompt.chat.id, prompt_message_id=prompt.message_id
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await is_whitelisted(message.from_user.id):
        await start(message, state)
    else:
        await show_access_message(message)


@router.message(Command("msg"))
async def minecraft_private_message(
    message: Message, command: CommandObject
) -> None:
    if message.from_user is None:
        return
    parts = (command.args or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /msg ник текст")
        return
    recipient_nickname, text = parts
    if not MINECRAFT_NICKNAME.fullmatch(recipient_nickname):
        await message.answer("Проверь никнейм игрока.")
        return
    await relay_private_message_to_minecraft(
        message, text, recipient_nickname
    )


@router.message(Command("access_requests"))
async def access_requests(message: Message) -> None:
    if not await require_admin(message):
        return

    requests = await list_pending_requests()
    if not requests:
        await message.answer("Нет ожидающих заявок.")
        return

    await message.answer(f"Ожидают решения заявок: {len(requests)}")
    for telegram_id, username, full_name in requests:
        await message.answer(
            access_request_text(telegram_id, username, full_name),
            reply_markup=access_review_keyboard(telegram_id),
        )


@router.message(Command("poll"))
async def create_server_poll(message: Message, command: CommandObject) -> None:
    if not await require_admin(message):
        return

    parts = [part.strip() for part in (command.args or "").split("|")]
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/poll вопрос | вариант 1 | вариант 2</code>\n"
            "Можно добавить до 10 вариантов."
        )
        return

    question, options = parts[0], parts[1:]
    validation_error = validate_poll(question, options)
    if validation_error:
        await message.answer(validation_error)
        return

    try:
        result = await create_and_broadcast_poll(
            message.bot,
            question,
            options,
            message.from_user.id,
        )
    except Exception:
        logger.exception("Не удалось создать голосование")
        await message.answer("Не удалось создать голосование. Попробуй ещё раз.")
        return

    poll = await get_poll_results(result["id"])
    if poll is not None:
        try:
            await notify_admin_of_results(message.bot, poll)
        except TelegramAPIError:
            logger.exception("Не удалось отправить результаты голосования админу")

    report = (
        f"Голосование №{result['id']} отправлено: "
        f"{result['delivered']} из {result['total']} пользователей."
    )
    if result["failed"]:
        report += f"\nНе доставлено: {result['failed']}."
    await message.answer(report)


@router.message(Command("poll_close"))
async def close_server_poll(message: Message, command: CommandObject) -> None:
    if not await require_admin(message):
        return

    try:
        poll_id = int((command.args or "").strip())
        if poll_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /poll_close <номер>")
        return

    poll = await close_poll(poll_id)
    if poll is None:
        await message.answer(f"Голосование №{poll_id} не найдено.")
        return

    deliveries = await list_poll_deliveries(poll_id)
    final_text = poll_message_text(poll, closed=True)
    for telegram_id, message_id in deliveries:
        try:
            await message.bot.edit_message_text(
                chat_id=telegram_id,
                message_id=message_id,
                text=final_text,
                reply_markup=None,
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.casefold():
                logger.info(
                    "Не удалось закрыть голосование у пользователя %s: %s",
                    telegram_id,
                    error,
                )
        except TelegramAPIError:
            logger.info(
                "Не удалось закрыть голосование у пользователя %s", telegram_id
            )

    try:
        await notify_admin_of_results(message.bot, poll)
    except TelegramAPIError:
        logger.exception("Не удалось отправить итог голосования админу")

    await message.answer(
        f"Голосование №{poll_id} завершено.\nГолосов: {poll['total_votes']}."
    )


@router.message(Command("whitelist_add"))
async def whitelist_add(message: Message, command: CommandObject) -> None:
    if not await require_admin(message):
        return

    try:
        telegram_id = int((command.args or "").strip())
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /whitelist_add <telegram_id>")
        return

    added, request_approved = await add_to_whitelist(
        telegram_id, message.from_user.id
    )
    if added:
        await message.answer(f"ID <code>{telegram_id}</code> добавлен в белый список.")
    else:
        await message.answer(f"ID <code>{telegram_id}</code> уже в белом списке.")
    if request_approved:
        try:
            await message.bot.send_message(
                telegram_id,
                "✅ Доступ открыт. Напиши /start, чтобы начать.",
            )
        except TelegramAPIError:
            logger.info(
                "Не удалось сообщить пользователю %s о добавлении в белый список",
                telegram_id,
            )


@router.message(Command("whitelist_remove"))
async def whitelist_remove(message: Message, command: CommandObject) -> None:
    if not await require_admin(message):
        return

    try:
        telegram_id = int((command.args or "").strip())
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /whitelist_remove <telegram_id>")
        return

    if telegram_id == ADMIN_TELEGRAM_ID:
        await message.answer("Нельзя удалить администратора из белого списка.")
        return

    removed = await remove_from_whitelist(telegram_id, message.from_user.id)
    if removed:
        await message.answer(f"ID <code>{telegram_id}</code> удалён. Доступ отозван.")
    else:
        await message.answer(f"ID <code>{telegram_id}</code> не найден в списке.")


@router.message(Command("whitelist_list"))
async def whitelist_list(message: Message) -> None:
    if not await require_admin(message):
        return

    telegram_ids = await list_whitelisted_users()
    if not telegram_ids:
        await message.answer("Белый список пуст.")
        return

    lines = []
    for telegram_id, nickname in telegram_ids:
        label = html.escape(nickname) if nickname else "ник не выбран"
        lines.append(f"• <code>{telegram_id}</code> — {label}")

    header = f"<b>Белый список</b> ({len(lines)}):\n"
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = header
        chunk += f"{line}\n"
    if chunk != header:
        await message.answer(chunk.rstrip())


@router.message(NicknameForm.waiting_for_nickname)
async def save_nickname(message: Message, state: FSMContext) -> None:
    if not await is_whitelisted(message.from_user.id):
        await state.clear()
        await show_access_message(message)
        return

    telegram_id = message.from_user.id
    nickname = (message.text or "").strip()
    if not MINECRAFT_NICKNAME.fullmatch(nickname):
        await update_nickname_prompt(
            message,
            state,
            "Такой ник не подойдёт. Введи от 3 до 16 латинских букв, "
            "цифр или подчёркиваний.",
        )
        return

    if await nickname_is_taken(nickname, telegram_id):
        await update_nickname_prompt(
            message,
            state,
            "Этот ник уже занят. Выбери другой.",
        )
        return

    profile = await get_account_profile(telegram_id)
    if not await store_nickname(telegram_id, nickname):
        await update_nickname_prompt(
            message,
            state,
            "Этот ник уже занят. Выбери другой.",
        )
        return

    if profile and profile[1]:
        await update_nickname_prompt(
            message,
            state,
            main_menu_text(nickname),
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return

    await state.set_state(PasswordForm.waiting_for_password)
    await update_nickname_prompt(
        message,
        state,
        f"✅ Никнейм <b>{nickname}</b> сохранён.\n\n"
        "Теперь придумайте пароль для входа на сервер. Отправьте его отдельным "
        "сообщением сюда. Бот удалит его после обработки.",
    )


@router.message(PasswordForm.waiting_for_password)
async def save_password(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    if not await is_whitelisted(telegram_id):
        await state.clear()
        await show_access_message(message)
        return

    password = message.text or ""
    password_message_deleted = True
    try:
        await message.delete()
    except TelegramAPIError:
        password_message_deleted = False
        logger.warning(
            "Не удалось удалить сообщение с паролем пользователя %s", telegram_id
        )

    if len(password) < 4 or len(password) > 128:
        text = "Пароль должен содержать от 4 до 128 символов. Попробуйте ещё раз."
        if not password_message_deleted:
            text += " Если сообщение осталось в чате, удалите его вручную."
        await update_nickname_prompt(
            message,
            state,
            text,
        )
        return

    encoded = await asyncio.to_thread(hash_password, password)
    if not await save_password_hash(telegram_id, encoded):
        await update_nickname_prompt(
            message,
            state,
            "Не удалось сохранить пароль. Напиши /start и попробуй ещё раз.",
        )
        await state.clear()
        return
    return_to_account = (await state.get_data()).get("return_to_account", False)
    if return_to_account:
        profile = await get_account_profile(telegram_id)
        text = "✅ Пароль обновлён.\n\n" + account_card_text(profile)
        if not password_message_deleted:
            text += "\n\nНе удалось удалить сообщение с паролем. Удали его вручную."
        await update_nickname_prompt(
            message, state, text, reply_markup=account_keyboard()
        )
        await state.clear()
        return

    text = "Пароль сохранён.\n\n" + main_menu_text(await get_nickname(telegram_id))
    if not password_message_deleted:
        text += "\n\nНе удалось удалить сообщение с паролем. Удали его вручную."
    await update_nickname_prompt(
        message,
        state,
        text,
        reply_markup=main_menu_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("mc:"))
async def menu_action(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    if not await require_private_callback(callback):
        return

    telegram_id = callback.from_user.id
    action = callback.data.removeprefix("mc:")

    if action == "request_access":
        if await is_whitelisted(telegram_id):
            nickname = await get_nickname(telegram_id)
            if nickname:
                await callback.answer()
                await edit_callback_message(
                    callback,
                    main_menu_text(nickname),
                    reply_markup=main_menu_keyboard(),
                )
            else:
                await prompt_for_nickname_from_callback(callback, state)
            return

        user = callback.from_user
        created = await create_access_request(
            telegram_id, user.username, user.full_name
        )
        if not created:
            await callback.answer()
            await edit_callback_message(
                callback,
                "Ты уже отправлял заявку. Она на рассмотрении.",
            )
            return

        await callback.answer()
        try:
            await send_access_request_to_admin(
                bot, telegram_id, user.username, user.full_name
            )
        except TelegramForbiddenError:
            logger.info(
                "Администратор ещё не открыл чат с ботом; заявка сохранена в базе"
            )
            await edit_callback_message(
                callback,
                "Заявка сохранена. Ответ появится здесь, когда её посмотрят.",
            )
        except TelegramAPIError:
            logger.exception("Не удалось отправить заявку администратору")
            await edit_callback_message(
                callback,
                "Заявка сохранена, но сообщение не отправилось. Попробуй позже.",
            )
        else:
            await edit_callback_message(
                callback, "Готово, заявку отправил. Жди ответа."
            )
        return

    if not await is_whitelisted(telegram_id):
        await state.clear()
        await callback.answer()
        await edit_callback_message(
            callback,
            access_message_text(telegram_id),
            reply_markup=access_keyboard(),
        )
        return

    if not await get_nickname(telegram_id):
        await prompt_for_nickname_from_callback(callback, state)
        return

    if action == "change_nickname":
        await state.clear()
        await callback.answer("Меню обновлено.")
        await edit_callback_message(
            callback,
            "Выберите действие:",
            reply_markup=main_menu_keyboard(),
        )
        return

    if action == "menu":
        await state.clear()
        nickname = await get_nickname(telegram_id)
        await callback.answer()
        await edit_callback_message(
            callback, main_menu_text(nickname), reply_markup=main_menu_keyboard()
        )
        return

    if action == "notifications":
        await state.clear()
        preferences = await get_notification_preferences(telegram_id)
        await callback.answer()
        await edit_callback_message(
            callback,
            notification_settings_text(preferences),
            reply_markup=notification_settings_keyboard(preferences),
        )
        return

    if action.startswith("notify:"):
        event_type = action.removeprefix("notify:")
        if event_type not in NOTIFICATION_LABELS:
            await callback.answer("Неизвестный тип оповещений.", show_alert=True)
            return

        preferences = await get_notification_preferences(telegram_id)
        enabled = not preferences[event_type]
        await set_notification_preference(telegram_id, event_type, enabled)
        preferences = await get_notification_preferences(telegram_id)
        status = "включены" if enabled else "выключены"
        await callback.answer(f"{NOTIFICATION_LABELS[event_type]} {status}.")
        await edit_callback_message(
            callback,
            notification_settings_text(preferences),
            reply_markup=notification_settings_keyboard(preferences),
        )
        return

    if action == "password":
        await state.clear()
        await state.set_state(PasswordForm.waiting_for_password)
        await callback.answer()
        if isinstance(callback.message, Message):
            await state.update_data(
                prompt_chat_id=callback.message.chat.id,
                prompt_message_id=callback.message.message_id,
                return_to_account=True,
            )
            await callback.message.edit_text(
                "Отправьте новый пароль отдельным сообщением сюда. "
                "Бот удалит его после обработки.",
                reply_markup=EMPTY_KEYBOARD,
            )
        return

    await state.clear()
    profile = await get_account_profile(telegram_id)
    nickname = profile[0] if profile else "не задан"
    if action in {"server", "status", "join"}:
        try:
            status = await query_server_status()
        except Exception as error:
            logger.warning("Не удалось получить статус Minecraft сервера: %s", error)
            text = (
                "🔴 <b>Айпи сервера</b>\n\n"
                "Состояние: недоступен\n"
                "Игроки: нет данных\n"
                f"Версия: {html.escape(MINECRAFT_SERVER_VERSION)}\n"
                f"Айпи: <code>{html.escape(MINECRAFT_SERVER_ADDRESS)}</code>"
            )
        else:
            text = (
                "🟢 <b>Айпи сервера</b>\n\n"
                "Состояние: онлайн\n"
                f"Игроки: {status.online_players}/{status.max_players}\n"
                f"Версия: {html.escape(status.version)}\n"
                f"Айпи: <code>{html.escape(MINECRAFT_SERVER_ADDRESS)}</code>"
            )
    elif action == "account":
        text = account_card_text(profile)
    elif action == "rules":
        text = (
            "📜 <b>Правила сервера</b>\n\n"
            "1. Гриферство запрещено.\n"
            "2. Запрещены читы, портящие атмосферу игры, например KillAura."
        )
    elif action == "help":
        text = (
            "🛟 <b>Помощь</b>\n\n"
            "Если возникла проблема или нужно сменить никнейм, "
            'напишите <a href="https://t.me/pluresque">@pluresque</a>.\n\n'
            "Личное сообщение игроку: <code>/msg ник текст</code>. "
            "Ответ на личное сообщение из игры отправится обратно в Minecraft."
        )
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    await callback.answer()
    keyboard = account_keyboard() if action == "account" else back_keyboard()
    await edit_callback_message(callback, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("v:"))
async def cast_server_poll_vote(callback: CallbackQuery) -> None:
    if not await require_private_callback(callback):
        return

    if not await is_whitelisted(callback.from_user.id):
        await callback.answer("У тебя нет доступа к этому голосованию.", show_alert=True)
        return

    try:
        _, raw_poll_id, raw_option_index = callback.data.split(":", 2)
        poll_id = int(raw_poll_id)
        option_index = int(raw_option_index)
    except (AttributeError, TypeError, ValueError):
        await callback.answer("Не удалось распознать голосование.", show_alert=True)
        return

    poll, changed = await cast_poll_vote(
        poll_id,
        callback.from_user.id,
        option_index,
    )
    if poll is None:
        await callback.answer(
            "Голосование завершено или недоступно.", show_alert=True
        )
        return

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                poll_message_text(poll, selected_option=option_index),
                reply_markup=poll_keyboard(
                    poll_id,
                    poll["options"],
                    selected_option=option_index,
                ),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.casefold():
                logger.info("Не удалось обновить сообщение голосования: %s", error)
        except TelegramAPIError:
            logger.exception("Не удалось обновить сообщение голосования")

    await callback.answer("Голос учтён." if changed else "Этот голос уже учтён.")
    if changed:
        try:
            await notify_admin_of_results(callback.bot, poll)
        except TelegramAPIError:
            logger.exception("Не удалось отправить результаты голосования админу")


@router.callback_query(F.data.startswith("admin:"))
async def review_access_request(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_TELEGRAM_ID:
        await callback.answer("Действие доступно только администратору.", show_alert=True)
        return
    if not await require_private_callback(callback):
        return

    try:
        _, decision, raw_telegram_id = callback.data.split(":", 2)
        telegram_id = int(raw_telegram_id)
    except (AttributeError, ValueError):
        await callback.answer("Некорректная заявка.", show_alert=True)
        return

    if decision not in {"approve", "deny"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    approved = decision == "approve"
    resolved = await resolve_access_request(
        telegram_id, approved, callback.from_user.id
    )
    if not resolved:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    if isinstance(callback.message, Message):
        outcome = "✅ Доступ одобрен." if approved else "❌ В доступе отказано."
        previous_text = callback.message.text or "Заявка доступа"
        await edit_callback_message(
            callback,
            f"{previous_text}\n\n{outcome}",
            reply_markup=EMPTY_KEYBOARD,
        )

    await callback.answer("Доступ одобрен." if approved else "В доступе отказано.")
    try:
        if approved:
            await bot.send_message(
                telegram_id,
                "✅ Доступ открыт. Напиши /start, чтобы начать.",
            )
        else:
            await bot.send_message(
                telegram_id,
                "Доступ пока не открыт. Можешь попробовать позже.",
            )
    except TelegramAPIError:
        logger.info(
            "Не удалось сообщить пользователю %s о решении по заявке", telegram_id
        )


def normalize_minecraft_chat_text(raw_text: str) -> str:
    normalized = raw_text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    normalized = normalized.replace("\t", " ")
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cc"
    ).strip()


def minecraft_chat_length(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


async def relay_message_to_minecraft(message: Message, raw_text: str | None) -> None:
    if message.from_user is None:
        return

    telegram_id = message.from_user.id
    if not await is_whitelisted(telegram_id):
        await message.answer("Напиши /start, чтобы запросить доступ к серверу.")
        return

    profile = await get_account_profile(telegram_id)
    if not profile or not profile[0]:
        await message.answer("Сначала выбери никнейм. Напиши /start.")
        return

    if raw_text is None:
        await message.answer(
            "Стикеры в Minecraft не отображаются. Отправь текст или эмодзи.",
        )
        return

    text = normalize_minecraft_chat_text(raw_text)
    if not text:
        return
    if minecraft_chat_length(text) > MAX_MINECRAFT_CHAT_UTF16_UNITS:
        await message.answer(
            "Сообщение длиннее лимита Minecraft: максимум 256 символов с учётом эмодзи."
        )
        return

    too_fast = False
    queued = False
    async with CHAT_RELAY_LOCK:
        now = time.monotonic()
        last_sent = CHAT_RELAY_LAST_SENT_AT.get(telegram_id, 0.0)
        if now - last_sent < CHAT_RELAY_COOLDOWN_SECONDS:
            too_fast = True
        else:
            try:
                queued = await enqueue_minecraft_chat_message(telegram_id, text)
            except Exception:
                logger.exception(
                    "Не удалось передать сообщение Minecraft от пользователя %s",
                    telegram_id,
                )
            if queued:
                CHAT_RELAY_LAST_SENT_AT[telegram_id] = time.monotonic()

    if too_fast:
        await message.answer("Отправлять сообщения можно раз в 2 секунды.")
    elif not queued:
        await message.answer("Не получилось отправить. Проверь доступ и ник через /start.")


async def relay_private_message_to_minecraft(
    message: Message, raw_text: str | None, recipient_nickname: str
) -> None:
    if message.from_user is None:
        return

    telegram_id = message.from_user.id
    if not await is_whitelisted(telegram_id):
        await message.answer("У тебя нет доступа к серверу.")
        return

    profile = await get_account_profile(telegram_id)
    if not profile or not profile[0]:
        await message.answer("Сначала выбери никнейм. Напиши /start.")
        return

    if not await private_message_bridge_enabled():
        await message.answer(
            "Связь личных сообщений включится после ближайшего рестарта сервера."
        )
        return

    if raw_text is None:
        await message.answer("Отправь текст или эмодзи.")
        return

    text = normalize_minecraft_chat_text(raw_text)
    if not text:
        return
    if minecraft_chat_length(text) > MAX_MINECRAFT_CHAT_UTF16_UNITS:
        await message.answer(
            "Сообщение длиннее лимита Minecraft: максимум 256 символов с учётом эмодзи."
        )
        return

    too_fast = False
    recipient = None
    async with CHAT_RELAY_LOCK:
        now = time.monotonic()
        last_sent = PRIVATE_RELAY_LAST_SENT_AT.get(telegram_id, 0.0)
        if now - last_sent < PRIVATE_RELAY_COOLDOWN_SECONDS:
            too_fast = True
        else:
            try:
                recipient = await enqueue_telegram_private_message(
                    telegram_id, recipient_nickname, text
                )
            except Exception:
                logger.exception(
                    "Не удалось передать личное сообщение Minecraft от пользователя %s",
                    telegram_id,
                )
            if recipient:
                PRIVATE_RELAY_LAST_SENT_AT[telegram_id] = time.monotonic()

    if too_fast:
        await message.answer("Отправлять личные сообщения можно раз в секунду.")
    elif not recipient:
        await message.answer(
            "Игрок не найден в списке доступа или ещё не настроил аккаунт."
        )


@router.message(F.sticker)
async def relay_sticker_to_minecraft(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state in {
        NicknameForm.waiting_for_nickname.state,
        PasswordForm.waiting_for_password.state,
    }:
        await message.answer("Сейчас бот ждёт текстовый ответ.")
        return

    sticker = message.sticker
    if message.from_user and message.reply_to_message:
        recipient = await get_private_message_reply_target(
            message.from_user.id, message.reply_to_message.message_id
        )
        if recipient:
            await relay_private_message_to_minecraft(
                message, sticker.emoji if sticker else None, recipient
            )
            return
        if (
            message.reply_to_message.from_user
            and message.reply_to_message.from_user.is_bot
        ):
            await message.answer("Не нашёл личный диалог. Используй /msg ник текст.")
            return
    await relay_message_to_minecraft(message, sticker.emoji if sticker else None)


@router.message(F.text & ~F.text.startswith("/"))
async def relay_text_to_minecraft(message: Message) -> None:
    if message.from_user and message.reply_to_message:
        recipient = await get_private_message_reply_target(
            message.from_user.id, message.reply_to_message.message_id
        )
        if recipient:
            await relay_private_message_to_minecraft(
                message, message.text, recipient
            )
            return
        if (
            message.reply_to_message.from_user
            and message.reply_to_message.from_user.is_bot
        ):
            await message.answer("Не нашёл личный диалог. Используй /msg ник текст.")
            return
    await relay_message_to_minecraft(message, message.text)
