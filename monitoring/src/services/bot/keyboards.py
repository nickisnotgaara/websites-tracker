from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🚀 Начать парсинг")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="👥 Пользователи")])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
    )


def back_button() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Назад")]],
        resize_keyboard=True,
    )


def users_list_inline_keyboard(users_full: list[dict]) -> InlineKeyboardMarkup:
    """
    Generates inline list of users with delete buttons.
    users_full: [{'id': 123, 'name': 'Bob'}, ...]
    """
    buttons = []

    # Sort by ID or Name
    users_full.sort(key=lambda x: x.get("id", 0))

    for u in users_full:
        # u is expected to be dict from get_users_full
        uid = u["id"]
        # Handle case where name might be missing (legacy safety)
        name = u.get("name", str(uid))

        # Button: "❌ Name (ID)" -> callback "remove_ask:123"
        # Truncate name if too long
        if len(name) > 15:
            name = name[:12] + "..."

        btn_text = f"❌ {name} ({uid})"
        buttons.append(
            [InlineKeyboardButton(text=btn_text, callback_data=f"remove_ask:{uid}")]
        )

    # Add "Add User" button at the bottom
    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить пользователя", callback_data="add_user_start"
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_delete_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить", callback_data=f"remove_confirm:{user_id}"
                ),
                InlineKeyboardButton(text="❌ Отмена", callback_data="remove_cancel"),
            ]
        ]
    )
