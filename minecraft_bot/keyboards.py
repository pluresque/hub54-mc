from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def access_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Попросить доступ",
                    callback_data="mc:request_access",
                )
            ]
        ]
    )


def access_review_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Одобрить",
                    callback_data=f"admin:approve:{telegram_id}",
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"admin:deny:{telegram_id}",
                ),
            ]
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Айпи сервера", callback_data="mc:server"),
                InlineKeyboardButton(text="Мой аккаунт", callback_data="mc:account"),
            ],
            [
                InlineKeyboardButton(text="Правила", callback_data="mc:rules"),
                InlineKeyboardButton(
                    text="Оповещения", callback_data="mc:notifications"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Карта", url="https://map.hub54.space"
                ),
                InlineKeyboardButton(text="Помощь", callback_data="mc:help"),
            ],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад в меню", callback_data="mc:menu")]
        ]
    )


def account_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сменить пароль", callback_data="mc:password")],
            [InlineKeyboardButton(text="Назад в меню", callback_data="mc:menu")],
        ]
    )


def notification_settings_keyboard(
    preferences: dict[str, bool],
) -> InlineKeyboardMarkup:
    labels = {
        "chat": "Чат",
        "join": "Вход",
        "leave": "Выход",
        "kill": "Убийства",
        "death": "Смерти",
    }
    event_types = tuple(labels)
    rows = []
    for index in range(0, len(event_types), 2):
        row = []
        for event_type in event_types[index : index + 2]:
            enabled = preferences.get(event_type, False)
            icon = "🔔" if enabled else "🔕"
            row.append(
                InlineKeyboardButton(
                    text=f"{icon} {labels[event_type]}",
                    callback_data=f"mc:notify:{event_type}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Назад в меню", callback_data="mc:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
