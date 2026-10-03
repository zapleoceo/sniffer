"""Карточка фильтра: одно сообщение поиска с чипами условий (R1 §9.4, R6 фаза 1).

Рисует и разбирает кнопки; в базу не ходит. Правка идёт через `filter_flow` и тот же
реестр полей (`domain/field_spec`), что и проверка правки: чип — это поле реестра, а не
набор строк, собранных здесь.

Провод: `flt:<корень>:<версия>:<поле>:<действие>:<вариант>`, влезает в 64 байта при самом
длинном ключе поля. Версия едет с кнопкой: правка применяется только к той версии, которую
человек видел (`base_version`), иначе нажатие на старое сообщение затёрло бы свежее условие.

Действия: `o` — открыть поле (выбор значений или приглашение написать), `s` — задать
вариант, `x` — снять поле, `p` — выбрать, какое условие добавить, `b` — назад к карточке.
Всё, что пришло от человека или из чужого текста, проходит `escape`: карточка идёт с
`parse_mode=HTML`.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sniffer.bot.threads import bold_title
from sniffer.domain.field_spec import FieldSpec, Kind, Monitor, specs_for
from sniffer.domain.passport import Passport
from sniffer.domain.passport_edit import read

OPEN, SET, CLEAR, PICK, BACK = "o", "s", "x", "p", "b"
NOT_MONITORED = "слежение это условие не учитывает"
STALE = "Поиск уже изменился — показываю свежие условия."
CURSOR_NOTE = (
    "Слежение уже просмотрело часть объявлений и к ним не вернётся: "
    "расширенный фильтр поймает только новые."
)
_STATE_LINE = {
    "active": "🟢 Слежу за новыми объявлениями",
    "paused": "⏸ Слежение на паузе",
    "expired": "⌛ Слежение закончилось",
    "off": "▫️ Слежения нет",
}
_YES_NO = (("true", "да"), ("false", "нет"))


class FilterCallback(CallbackData, prefix="flt"):
    root: int
    v: int
    f: str = ""
    a: str = BACK
    o: str = ""


@dataclass(frozen=True, slots=True)
class CardView:
    root: int
    version: int
    passport: Passport
    monitoring: str = "off"


def shown(spec: FieldSpec, value: Any, passport: Passport) -> str:
    """Значение поля глазами человека: «автомат», «да», «15 000 000 VND», «Hoi An, My Khe»."""
    if spec.kind is Kind.CHOICE:
        return dict(spec.options).get(str(value), str(value))
    if spec.kind is Kind.FLAG:
        return "да" if value else "нет"
    if spec.kind is Kind.LIST:
        return ", ".join(str(item) for item in value)
    if spec.kind is Kind.NUMBER:
        number = f"{value:,.0f}".replace(",", " ") if float(value).is_integer() else f"{value:g}"
        currency = passport.budget.currency
        return (
            f"{number} {currency.value}" if spec.path.startswith("budget") and currency else number
        )
    return str(value)


def filled(view: CardView) -> list[tuple[FieldSpec, Any]]:
    """Условия, которые заданы: по ним рисуются чипы. Порядок — порядок реестра."""
    pairs = [(spec, read(view.passport, spec)) for spec in specs_for(view.passport.category)]
    return [(spec, value) for spec, value in pairs if value not in (None, "", [], {})]


def card_text(view: CardView) -> str:
    lines = [bold_title(view.passport), _STATE_LINE.get(view.monitoring, _STATE_LINE["off"])]
    rows = filled(view)
    if not rows:
        lines.append("Условий пока нет — добавьте первое кнопкой ниже.")
    for spec, value in rows:
        note = "" if spec.monitor is not Monitor.NO else f" — <i>{NOT_MONITORED}</i>"
        lines.append(f"• {escape(spec.label)}: {escape(shown(spec, value, view.passport))}{note}")
    return "\n".join(lines)


def card_markup(
    view: CardView, *, footer: list[list[InlineKeyboardButton]]
) -> InlineKeyboardMarkup:
    """Чипы и кнопка «+ условие»; `footer` — ряды слежения, их собирает панель."""
    rows: list[list[InlineKeyboardButton]] = []
    for spec, value in filled(view):
        chip = InlineKeyboardButton(
            text=f"✎ {spec.label}: {shown(spec, value, view.passport)}"[:60],
            callback_data=_pack(view, spec.key, OPEN),
        )
        row = [chip]
        if not spec.required:
            row.append(InlineKeyboardButton(text="✕", callback_data=_pack(view, spec.key, CLEAR)))
        rows.append(row)
    if _addable(view):
        rows.append([InlineKeyboardButton(text="＋ условие", callback_data=_pack(view, "", PICK))])
    return InlineKeyboardMarkup(inline_keyboard=[*rows, *footer])


def field_text(spec: FieldSpec) -> str:
    return f"<b>{escape(spec.label)}</b>: выберите значение."


def prompt_text(spec: FieldSpec) -> str:
    return f"<b>{escape(spec.label)}</b>: напишите новое значение сообщением."


def field_markup(view: CardView, spec: FieldSpec) -> InlineKeyboardMarkup:
    """Кнопки значений для поля с конечным выбором; «любое» снимает условие."""
    options = _YES_NO if spec.kind is Kind.FLAG else spec.options
    buttons = [
        InlineKeyboardButton(text=label, callback_data=_pack(view, spec.key, SET, value))
        for value, label in options
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if not spec.required:
        rows.append(
            [InlineKeyboardButton(text="любое", callback_data=_pack(view, spec.key, CLEAR))]
        )
    rows.append([InlineKeyboardButton(text="← К условиям", callback_data=_pack(view, "", BACK))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def addable_markup(view: CardView) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=spec.label, callback_data=_pack(view, spec.key, OPEN))]
        for spec in _addable(view)
    ]
    rows.append([InlineKeyboardButton(text="← К условиям", callback_data=_pack(view, "", BACK))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_option(spec: FieldSpec, raw: str) -> Any:
    """Значение кнопки обратно в значение поля: флаг приходит строкой «true» / «false»."""
    return raw == "true" if spec.kind is Kind.FLAG else raw


def is_choice(spec: FieldSpec) -> bool:
    return spec.kind in (Kind.CHOICE, Kind.FLAG)


def _addable(view: CardView) -> list[FieldSpec]:
    have = {spec.key for spec, _ in filled(view)}
    return [spec for spec in specs_for(view.passport.category) if spec.key not in have]


def _pack(view: CardView, field: str, action: str, option: str = "") -> str:
    return FilterCallback(root=view.root, v=view.version, f=field, a=action, o=option).pack()
