from __future__ import annotations

import re
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message
from ..api_client import submissions_article, submissions_photo
from ..keyboards import kb_moderate
from ..config import ADMIN_CHAT_ID

router = Router()
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}(?:/[^\s]*)?", re.I)

def _mask_phone(p: str | None) -> str:
    if not p: return ""
    return re.sub(r"\d(?=\d{2})", "•", p)

@router.message(F.text.func(lambda s: bool(s and URL_RE.search(s))))
async def on_article(m: Message, bot: Bot):
    logging.info(f"Processing article submission from user {m.from_user.id}: {m.text}")
    
    url_match = URL_RE.search(m.text)
    if not url_match:
        logging.warning(f"No URL match found in text: {m.text}")
        return
    
    url = url_match.group(0)
    # Добавляем протокол если его нет
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    logging.info(f"Extracted URL: {url}")
    st, r = await submissions_article(tg_id=m.from_user.id, url=url, caption=None)
    logging.info(f"API response: status={st}, response={r}")
    
    if st == 404:
        return await m.answer("❌ Вы не зарегистрированы. Нажмите /reg для регистрации.")
    
    if st != 200 or not isinstance(r, dict):
        return await m.answer("❌ Не удалось принять ссылку, попробуй ещё раз.")
    
    if r.get("status") == "duplicate":
        return await m.answer("⚠️ Эта ссылка уже была отправлена ранее.")
    
    if r.get("status") != "ok":
        return await m.answer("❌ Не удалось принять ссылку, попробуй ещё раз.")

    sid = r.get("id") or r.get("submission_id")
    u = r.get("user", {})
    team_label = r.get("team_number") or f"Команда {r.get('team_id') or '?'}"
    caption = f"📰 <b>{team_label}</b>\nАвтор: {u.get('last_name','') or ''} {u.get('first_name','') or ''}\nТел: {_mask_phone(u.get('phone'))}\n\n{url}"
    kb = kb_moderate(sid)
    await bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, parse_mode="HTML", reply_markup=kb)
    await m.answer("✅ Ссылка отправлена на модерацию. Спасибо!")

@router.message(F.photo)
async def on_photo(m: Message, bot: Bot):
    file_id = m.photo[-1].file_id
    st, r = await submissions_photo(tg_id=m.from_user.id, tg_file_id=file_id, caption=m.caption)
    
    if st == 404:
        return await m.answer("❌ Вы не зарегистрированы. Нажмите /reg для регистрации.")
    
    if st != 200 or not isinstance(r, dict) or r.get("status") != "ok":
        return await m.answer("❌ Не удалось принять фото, попробуй ещё раз.")
    
    sid = r["id"]
    u = r.get("user", {})
    team_label = r.get("team_number") or f"Команда {r.get('team_id') or '?'}"
    cap = f"📷 <b>{team_label}</b>\nАвтор: {u.get('last_name','') or ''} {u.get('first_name','') or ''}\nТел: {_mask_phone(u.get('phone'))}"
    await bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=file_id, caption=cap, parse_mode="HTML", reply_markup=kb_moderate(sid))
    await m.answer("✅ Фото отправлено на модерацию. Спасибо!")