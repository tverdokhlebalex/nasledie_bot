# bot/handlers/registration.py
from __future__ import annotations

import logging, os, csv, io, re
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove

from ..states import RegStates                  # в стейтах должны быть как минимум: waiting_phone, waiting_phone_manual
from ..keyboards import kb_request_phone        # ReplyKeyboardMarkup с кнопкой request_contact
from ..api_client import register_user
from ..utils import norm_phone, load_participants
from ..config import STRICT_WHITELIST
from ..texts import ONBOARDING

router = Router()

# ────────────────────────────────────────────────────────────────────────────────
# helpers

PHONE_HINT = (
    "Введите номер вручную, например:\n"
    "<codРегистрация>+7XXXXXXXXXX</codРегистрация завершена! Теперь присылайте ссылки на статьи и фотографии.e> или <code>8XXXXXXXXXX</code>"
)

def _valid_e164(s: str | None) -> str | None:
    """Приводим номер к E.164. Возвращаем None, если не похоже на номер."""
    if not s:
        return None
    p = norm_phone(s)  # у тебя уже есть нормализатор; если его нет – раскомментируй ниже.
    # # простой нормализатор:
    # p = re.sub(r"[^\d+]", "", s)
    # if p.startswith("8") and len(p) == 11:
    #     p = "+7" + p[1:]
    # elif p.startswith("7") and len(p) == 11:
    #     p = "+" + p
    # elif not p.startswith("+"):
    #     p = "+" + p
    # if len(re.sub(r"\D", "", p)) < 10:
    #     return None
    return p

def _find_in_whitelist(phone_e164: str) -> dict | None:
    """
    Пытаемся взять из utils.load_participants(); если там None/ошибка — подгружаем CSV напрямую.
    Возвращаем запись или None.
    """
    try:
        wl = load_participants() or {}             # <-- главная защита от None
    except Exception as e:
        logging.warning("load_participants() failed: %r. Fallback to CSV.", e)
        wl = {}

    if not wl:  # запасной план: читаем CSV прямо в боте
        path = os.getenv("WHITELIST_PATH", "/code/data/participants_template.csv")
        try:
            text = open(path, "rb").read().decode("utf-8-sig", "replace")
            rdr = csv.DictReader(io.StringIO(text))
            # поддержим team_number и team
            for row in rdr:
                p = _valid_e164(row.get("phone"))
                if not p:
                    continue
                tn_raw = row.get("team_number") or row.get("team")
                m = re.findall(r"\d+", str(tn_raw or ""))
                tn = int(m[0]) if m else 0
                wl[p] = {
                    "first_name": (row.get("first_name") or "").strip(),
                    "last_name": (row.get("last_name") or "").strip(),
                    "team_number": tn,
                }
        except FileNotFoundError:
            logging.warning("Whitelist CSV not found at %s", path)
        except Exception as e:
            logging.exception("Failed to parse whitelist CSV: %r", e)

    return wl.get(phone_e164)

# ────────────────────────────────────────────────────────────────────────────────
# /start и /reg → просим поделиться контактом

@router.message(F.text.in_({"/start", "/reg"}))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    text = ONBOARDING or "Привет! Для участия подтвердите телефон."
    await m.answer(
        text + "\n\nНажмите кнопку ниже, чтобы поделиться номером из Telegram.",
        reply_markup=kb_request_phone(),
    )
    await state.set_state(RegStates.waiting_phone)

# ────────────────────────────────────────────────────────────────────────────────
# Пользователь поделился контактом

@router.message(RegStates.waiting_phone, F.contact)
async def got_contact(m: Message, state: FSMContext):
    raw = m.contact.phone_number if m.contact else None
    phone = _valid_e164(raw)
    if not phone:
        # вдруг контакт «пустой» или странный — уводим на ручной ввод
        await m.answer("Не удалось прочитать номер из контакта. " + PHONE_HINT, reply_markup=ReplyKeyboardRemove())
        return await state.set_state(RegStates.waiting_phone_manual)

    # проверяем whitelist — даже если STRICT_WHITELIST = False,
    # мы всё равно используем из него номер команды/ФИО.
    entry = _find_in_whitelist(phone)

    if STRICT_WHITELIST and not entry:
        await m.answer("Твоего номера нет в списке участников. " + PHONE_HINT, reply_markup=ReplyKeyboardRemove())
        return await state.set_state(RegStates.waiting_phone_manual)

    # регистрация
    first_name = (entry or {}).get("first_name") or m.from_user.first_name or ""
    st, payload = await register_user(m.from_user.id, phone, first_name)
    if st != 200:
        logging.error("register_user failed: %s %s", st, payload)
        return await m.answer("Сервис регистрации временно недоступен.", reply_markup=ReplyKeyboardRemove())

    await state.clear()
    try:
        team_name = (payload.get("team_name") if isinstance(payload, dict) else None) or "твоя команда"
        team_id = (payload.get("team_id") if isinstance(payload, dict) else None)
        suffix = f" (№{team_id})" if team_id else ""
        await m.answer(
            f"✅ {first_name}, регистрация завершена!\nТвоя команда: *{team_name}*.\n\nТеперь присылай ссылки на статьи и фотографии.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
    except Exception:
        pass

# ────────────────────────────────────────────────────────────────────────────────
# Пользователь не хочет/не может делиться контактом → ручной ввод

@router.message(RegStates.waiting_phone, F.text)
async def fallback_to_manual(m: Message, state: FSMContext):
    # Любой текст вместо контакта — показываем просьбу ввести номер вручную
    await m.answer("Ок, давай вручную.\n" + PHONE_HINT, reply_markup=ReplyKeyboardRemove())
    await state.set_state(RegStates.waiting_phone_manual)

@router.message(RegStates.waiting_phone_manual, F.text)
async def got_manual_phone(m: Message, state: FSMContext):
    phone = _valid_e164(m.text or "")
    if not phone:
        return await m.answer("Это не похоже на номер. " + PHONE_HINT)

    entry = _find_in_whitelist(phone)

    if STRICT_WHITELIST and not entry:
        return await m.answer("Этого номера нет в списке участников. Проверь ещё раз или обратись к координатору.\n" + PHONE_HINT)

    first_name = (entry or {}).get("first_name") or m.from_user.first_name or ""
    st, payload = await register_user(m.from_user.id, phone, first_name)
    if st != 200:
        logging.error("register_user failed: %s %s", st, payload)
        return await m.answer("Сервис регистрации временно недоступен.")

    await state.clear()
    try:
        team_name = (payload.get("team_name") if isinstance(payload, dict) else None) or "твоя команда"
        team_id = (payload.get("team_id") if isinstance(payload, dict) else None)
        suffix = f" (№{team_id})" if team_id else ""
        first_line = (entry or {}).get("first_name") or m.from_user.first_name or ""
        await m.answer(
            f"✅ {first_line}, регистрация завершена!\nТвоя команда: *{team_name}*{suffix}.\n\nТеперь присылай ссылки на статьи и фото.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
    except Exception:
        pass