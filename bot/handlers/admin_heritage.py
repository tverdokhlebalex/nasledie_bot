from __future__ import annotations
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from ..config import ADMIN_CHAT_ID
from ..api_client import admin_approve_submission, admin_reject_submission, admin_queue_register, admin_reject_by_reply, submission_get, leaderboard, get_all_users
from ..states import BroadcastStates

router = Router()

def _is_admin_chat(m: Message | CallbackQuery) -> bool:
    chat_id = m.message.chat.id if isinstance(m, CallbackQuery) else m.chat.id
    return chat_id == ADMIN_CHAT_ID

@router.callback_query(F.data.startswith("mod:"))
async def on_moderate(cq: CallbackQuery, bot: Bot):
    if not _is_admin_chat(cq):
        return await cq.answer("Только в админ-чате.", show_alert=True)
    parts = cq.data.split(":")  # mod:appr:123
    action, sid = parts[1], int(parts[2])
    if action == "appr":
        st, _ = await admin_approve_submission(sid, reviewer_tg=cq.from_user.id)
        if st == 200:
            await cq.message.edit_reply_markup(reply_markup=None)
            await cq.answer("Принято ✅")
            st2, s = await submission_get(sid)
            if st2 == 200 and s.get("user",{}).get("tg_id"):
                await bot.send_message(s["user"]["tg_id"], "Ваша отправка принята ✅")
        else:
            await cq.answer("Ошибка", show_alert=True)
    elif action == "rej":
        # Скрываем кнопки сразу при нажатии "Отклонить"
        await cq.message.edit_reply_markup(reply_markup=None)
        # Отправляем сообщение с просьбой написать причину
        reply_msg = await cq.message.reply("Напишите причину отказа ответом на сообщение выше.")
        # регистрируем связку для последующего reply (на сообщение с просьбой)
        await admin_queue_register(ADMIN_CHAT_ID, reply_msg.message_id, sid)
        await cq.answer("Жду причину…")

@router.message(F.chat.id == ADMIN_CHAT_ID, BroadcastStates.waiting_message, F.reply_to_message)
async def process_broadcast_reply(m: Message, state: FSMContext, bot: Bot):
    logging.info(f"=== BROADCAST REPLY HANDLER TRIGGERED ===")
    logging.info(f"Processing broadcast reply from admin {m.from_user.id}, text: '{m.text}'")
    message_text = m.text or "📢 Сообщение от администрации"
    logging.info(f"Processing broadcast message from admin {m.from_user.id}: {message_text[:100]}...")
    
    # Получаем список всех пользователей
    st, users = await get_all_users()
    if st != 200 or not users:
        await m.answer("❌ Не удалось получить список пользователей.")
        await state.clear()
        return
    
    await m.answer(f"📤 Начинаю рассылку сообщения {len(users)} участникам...")
    
    # Отправляем сообщение каждому пользователю
    sent_count = 0
    failed_count = 0
    
    for user in users:
        try:
            await bot.send_message(user["tg_id"], message_text)
            sent_count += 1
            logging.info(f"Sent broadcast to user {user['tg_id']} ({user.get('first_name', 'Unknown')})")
        except Exception as e:
            failed_count += 1
            logging.error(f"Failed to send broadcast to user {user['tg_id']}: {e}")
    
    await state.clear()
    await m.answer(
        f"✅ *Рассылка завершена*\n\n"
        f"📤 Отправлено: {sent_count}\n"
        f"❌ Ошибок: {failed_count}\n"
        f"👥 Всего участников: {len(users)}",
        parse_mode="Markdown"
    )

@router.message(F.chat.id == ADMIN_CHAT_ID, F.text.in_({"/leaderboard","/lb","/leaderbord"}))
async def cmd_lb(m: Message):
    st, rows = await leaderboard()
    if st != 200 or not rows:
        return await m.answer("📊 Пока пусто.")
    
    out = ["🏆 *Лидерборд*:"]
    for i, r in enumerate(rows, 1):
        # Эмодзи для позиций
        if i == 1:
            medal = "🥇"
        elif i == 2:
            medal = "🥈"
        elif i == 3:
            medal = "🥉"
        else:
            medal = f"{i}."
        
        out.append(f"{medal} *{r['team_name']}* — {r['total_points']} баллов (📰 {r['article_points']}, 📷 {r['photo_points']})")
    await m.answer("\n".join(out), parse_mode="Markdown")

@router.message(F.chat.id == ADMIN_CHAT_ID, F.text.in_({"/broadcast", "/рассылка"}))
async def cmd_broadcast(m: Message, state: FSMContext):
    logging.info(f"Broadcast command from admin {m.from_user.id}")
    await state.set_state(BroadcastStates.waiting_message)
    logging.info(f"Set state to BroadcastStates.waiting_message for admin {m.from_user.id}")
    await m.answer(
        "📢 *Рассылка сообщений*\n\n"
        "💬 *Ответьте на это сообщение* текстом, который хотите отправить всем участникам.\n\n"
        "❌ Для отмены используйте /cancel",
        parse_mode="Markdown"
    )

@router.message(F.chat.id == ADMIN_CHAT_ID, BroadcastStates.waiting_message, F.text == "/cancel")
async def cancel_broadcast(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Рассылка отменена.")

@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message)
async def on_reason(m: Message, bot: Bot, state: FSMContext):
    # Проверяем, что мы не в состоянии broadcast
    current_state = await state.get_state()
    if current_state == BroadcastStates.waiting_message:
        logging.info(f"Ignoring reply message in broadcast state: {m.text}")
        return
    logging.info(f"Admin reply message: chat_id={m.chat.id}, user_id={m.from_user.id}, text='{m.text}', reply_to={m.reply_to_message.message_id}")
    logging.info(f"Processing reject reason from admin {m.from_user.id}: {m.text}")
    
    try:
        reply_to = m.reply_to_message.message_id
        st, r = await admin_reject_by_reply(ADMIN_CHAT_ID, reply_to, reason=m.text or "", reviewer_tg=m.from_user.id)
        logging.info(f"Reject API response: status={st}, response={r}")
        
        if st == 200:
            await m.reply("Причина зафиксирована. Отправка отклонена ❌")
            # уведомим автора
            sid = r.get("submission_id")
            if sid:
                st2, s = await submission_get(sid)
                logging.info(f"Get submission response: status={st2}, submission={s}")
                
                if st2 == 200 and s and s.get("user") and s["user"].get("tg_id"):
                    user_tg_id = s["user"]["tg_id"]
                    reason_text = f"Отправка отклонена ❌\nПричина: {m.text or 'не указана'}"
                    logging.info(f"Sending rejection notification to user {user_tg_id}: {reason_text}")
                    try:
                        await bot.send_message(user_tg_id, reason_text)
                        logging.info(f"Successfully sent rejection notification to user {user_tg_id}")
                    except Exception as e:
                        logging.error(f"Failed to send message to user {user_tg_id}: {e}")
                else:
                    logging.warning(f"Could not send rejection notification: st2={st2}, user_tg_id={s.get('user',{}).get('tg_id') if s else 'None'}")
            else:
                logging.warning(f"No submission_id in response: {r}")
        else:
            logging.error(f"Failed to reject submission: status={st}, response={r}")
    except Exception as e:
        logging.error(f"Error processing reject reason: {e}")
        await m.reply("❌ Ошибка при обработке причины отклонения")

@router.message(F.chat.id == ADMIN_CHAT_ID)
async def on_admin_message_debug(m: Message, state: FSMContext):
    current_state = await state.get_state()
    logging.info(f"Admin message debug: chat_id={m.chat.id}, user_id={m.from_user.id}, text='{m.text}', state='{current_state}', reply_to={m.reply_to_message.message_id if m.reply_to_message else None}")