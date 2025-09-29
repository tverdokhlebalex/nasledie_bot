from aiogram import Router, F
from aiogram.types import Message
from ..api_client import roster_by_tg, leaderboard, team_by_tg
from ..utils import format_roster
from ..config import API_BASE

router = Router()

@router.message(F.text == "/team")
async def cmd_team(m: Message):
    st, r = await roster_by_tg(m.from_user.id)
    if st != 200 or not r:
        return await m.answer("Ты ещё не зарегистрирован. Набери /reg.")
    await m.answer(format_roster(r), parse_mode="Markdown")

# /lb остаётся на всякий случай
@router.message(F.text.in_({"/lb","/leaderboard","Лидерборд"}))
async def cmd_lb(m: Message):
    st, rows = await leaderboard()
    if st != 200 or not isinstance(rows, list) or not rows:
        return await m.answer("Лидерборд пока пуст.")
    out = ["*Лидерборд*:"]
    for i, r in enumerate(rows[:10], 1):
        name = r.get("team_name", f"Команда #{r.get('team_id')}")
        done, total = r.get("tasks_done", 0), r.get("total_tasks", 0)
        if r.get("finished_at"):
            out.append(f"{i}. *{name}* — {done}/{total} ✅")
        elif r.get("started_at"):
            out.append(f"{i}. *{name}* — {done}/{total} (в процессе)")
        else:
            out.append(f"{i}. *{name}* — не стартовали")
    await m.answer("\n".join(out), parse_mode="Markdown")