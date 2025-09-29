# bot/handlers/registration.py
import logging

from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

from ..states import RegStates
from ..keyboards import kb_request_phone
from ..api_client import register_user
from ..utils import norm_phone, KNOWN, load_participants
from ..config import STRICT_WHITELIST
from ..texts import ONBOARDING

router = Router()


@router.message(F.text.in_({"/start", "start"}))
async def onboarding(m: Message):
    await m.answer(ONBOARDING, parse_mode="Markdown")


@router.message(F.text == "/reg")
async def reg_begin(m: Message, state: FSMContext):
    await state.set_state(RegStates.waiting_phone)
    await m.answer(
        "Шаг 1/2: пришли номер телефона в формате +7XXXXXXXXXX",
        reply_markup=kb_request_phone()
    )


@router.message(RegStates.waiting_phone, F.contact)
async def reg_phone_contact(m: Message, state: FSMContext):
    me = m.contact
    if me and me.user_id and m.from_user and me.user_id != m.from_user.id:
        return await m.answer("Нужен *твой* номер.", parse_mode="Markdown")
    phone = norm_phone(me.phone_number if me else "")
    if not phone:
        return await m.answer("Не распознал номер. Пришли ещё раз.")
    
    # Завершаем регистрацию сразу после получения телефона
    first_name = m.from_user.first_name or "Участник"
    await _complete_registration(m, state, phone, first_name)


@router.message(RegStates.waiting_phone, F.text)
async def reg_phone_text(m: Message, state: FSMContext):
    phone = norm_phone(m.text or "")
    if not phone:
        return await m.answer("Формат телефона не распознан. Пример: +79991234567")
    
    # Завершаем регистрацию сразу после получения телефона
    first_name = m.from_user.first_name or "Участник"
    await _complete_registration(m, state, phone, first_name)


async def _complete_registration(m: Message, state: FSMContext, phone: str, first_name: str):
    """Завершение регистрации пользователя"""
    if STRICT_WHITELIST:
        load_participants()
        if phone not in KNOWN:
            await state.clear()
            return await m.answer("Не нашёл тебя в списке. Обратись к координатору.")

    st, payload = await register_user(m.from_user.id, phone, first_name)
    await state.clear()
    
    if st != 200:
        logging.error("register_user failed: %s %s", st, payload)
        return await m.answer("Сервис регистрации временно недоступен.")

    # Убираем клавиатуру и завершаем
    try:
        await m.answer("✅ Регистрация завершена! Теперь вы можете отправлять ссылки на статьи и фотографии.", reply_markup=ReplyKeyboardRemove())
    except Exception:
        pass