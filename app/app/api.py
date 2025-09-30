# app/app/api.py
from __future__ import annotations

import os
import csv
import io
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import (
    APIRouter, Depends, UploadFile, File, HTTPException,
    Header, Path, Form, Body, Query
)
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from .database import get_db
from . import models
from .schemas import (
    # public
    RegisterIn, RegisterOut, ImportReport, TeamOut, TeamRosterOut,
    # team structs / admin
    TeamMemberInfo, TeamAdminOut, SetCaptainIn, MoveMemberIn,
    # tasks / game (совместимость со старым API)
    TaskOut, TaskCreateIn, TaskUpdateIn, GameScanIn, GameScanOut,
    # rename
    TeamRenameIn, TeamRenameOut,
)
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

def canonical_url(raw: str) -> str:
    """
    Нормализация ссылки: схема/хост/путь, чистим UTM и фрагмент.
    """
    try:
        u = urlsplit(raw.strip())
        if u.scheme not in ("http", "https"):
            return raw
        qs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if not k.lower().startswith("utm_")]
        return urlunsplit((u.scheme, u.netloc.lower(), u.path, urlencode(qs), ""))  # без #fragment
    except Exception:
        return raw.strip()
# -----------------------------------------------------------------------------
# Root router: /api
# -----------------------------------------------------------------------------
router = APIRouter(prefix="/api", tags=["api"])

APP_SECRET = os.getenv("APP_SECRET", "change-me-please")
TEAM_SIZE = int(os.getenv("TEAM_SIZE") or 7)
PROOFS_DIR = os.getenv("PROOFS_DIR", "/code/data/proofs")
os.makedirs(PROOFS_DIR, exist_ok=True)

# --- security ---------------------------------------------------------------
def require_secret(x_app_secret: str | None = Header(default=None, alias="x-app-secret")):
    if not x_app_secret or x_app_secret != APP_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# Админ-саброутер защищён заголовком x-app-secret
admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_secret)])


# --- helpers ----------------------------------------------------------------
def now_utc() -> datetime:
    # проект везде использует naive UTC (без tzinfo)
    return datetime.utcnow()


def norm_phone(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[^\d+]", "", s.strip())
    if s.startswith("8") and len(s) == 11:
        s = "+7" + s[1:]
    if s.isdigit() and len(s) == 11 and s[0] == "7":
        s = "+" + s
    return s


def dump_team_admin(db: Session, team: models.Team) -> TeamAdminOut:
    rows = (
        db.query(models.TeamMember, models.User)
        .join(models.User, models.User.id == models.TeamMember.user_id)
        .filter(models.TeamMember.team_id == team.id)
        .order_by(models.TeamMember.id.asc())
        .all()
    )
    members: List[TeamMemberInfo] = []
    captain: Optional[TeamMemberInfo] = None
    for m, u in rows:
        item = TeamMemberInfo(
            user_id=u.id,
            role=m.role,
            first_name=u.first_name,
            last_name=u.last_name,
            phone=u.phone,
            tg_id=u.tg_id,
        )
        members.append(item)
        if (m.role or "").upper() == "CAPTAIN":
            captain = item
    return TeamAdminOut(
        team_id=team.id,
        team_name=team.name,
        is_locked=bool(team.is_locked),
        captain=captain,
        members=members,
        color=getattr(team, "color", None),
        route_id=getattr(team, "route_id", None),
    )


def _team_member_count(db: Session, team_id: int) -> int:
    return (
        db.query(func.count(models.TeamMember.id))
        .filter(models.TeamMember.team_id == team_id)
        .scalar()
    ) or 0


def _team_is_full(db: Session, team_id: int) -> bool:
    return _team_member_count(db, team_id) >= TEAM_SIZE


def _ensure_captain_if_full(db: Session, team_id: int) -> None:
    rows = (
        db.query(models.TeamMember)
        .filter(models.TeamMember.team_id == team_id)
        .order_by(models.TeamMember.id.asc())
        .all()
    )
    if not rows or len(rows) < TEAM_SIZE:
        return
    if any((m.role or "").upper() == "CAPTAIN" for m in rows):
        return
    rows[0].role = "CAPTAIN"
    db.commit()


def _next_open_team(db: Session) -> models.Team:
    # незаполнённая разблокированная команда по возрастанию id
    candidates = (
        db.query(models.Team.id)
        .filter(models.Team.is_locked == False)  # noqa: E712
        .order_by(models.Team.id.asc())
        .all()
    )
    for (tid,) in candidates:
        if _team_member_count(db, tid) < TEAM_SIZE:
            return db.get(models.Team, tid)

    # создать новую «Команда №N»
    base_n = (db.query(func.count(models.Team.id)).scalar() or 0) + 1
    n = base_n
    while True:
        name = f"Команда №{n}"
        exists = db.query(models.Team).filter(models.Team.name == name).first()
        if not exists:
            break
        n += 1

    team = models.Team(name=name, is_locked=False)
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


def _require_team_started(team: models.Team):
    if not getattr(team, "started_at", None):
        raise HTTPException(409, "Team has not started yet")


# ---------- routes helpers ----------
def _routes_with_checkpoints(db: Session) -> list[models.Route]:
    """Вернёт только маршруты, у которых есть хотя бы один чекпоинт."""
    routes = db.query(models.Route).order_by(models.Route.id.asc()).all()
    out: list[models.Route] = []
    for r in routes:
        cnt = (
            db.query(func.count(models.Checkpoint.id))
            .filter(models.Checkpoint.route_id == r.id)
            .scalar()
        ) or 0
        if cnt > 0:
            out.append(r)
    return out


def _auto_assign_route_if_needed(db: Session, team: models.Team) -> bool:
    """
    Если у команды ещё не выбран маршрут — выбрать маршрут
    с минимальным числом уже привязанных команд (среди маршрутов с чекпоинтами).
    Возвращает True, если назначили (или уже есть).
    """
    if getattr(team, "route_id", None):
        return True

    routes = _routes_with_checkpoints(db)
    if not routes:
        return False

    counts: Dict[int, int] = {}
    for r in routes:
        counts[r.id] = (
            db.query(func.count(models.Team.id))
            .filter(models.Team.route_id == r.id)
            .scalar()
        ) or 0

    chosen = min(routes, key=lambda r: counts.get(r.id, 0))
    team.route_id = chosen.id
    db.commit()
    return True


# ---- Маршруты / чекпойнты / доказательства ---------------------------------
def _route_total_checkpoints(db: Session, route_id: int | None) -> int:
    if not route_id:
        return 0
    return (
        db.query(func.count(models.Checkpoint.id))
        .filter(models.Checkpoint.route_id == route_id)
        .scalar()
    ) or 0


def _approved_count_cp(db: Session, team_id: int) -> int:
    return (
        db.query(func.count(models.Proof.id))
        .filter(models.Proof.team_id == team_id, models.Proof.status == "APPROVED")
        .scalar()
    ) or 0


def _current_checkpoint(db: Session, team: models.Team) -> models.Checkpoint | None:
    if not getattr(team, "route_id", None) or not getattr(team, "current_order_num", None):
        return None
    return (
        db.query(models.Checkpoint)
        .filter(
            models.Checkpoint.route_id == team.route_id,
            models.Checkpoint.order_num == team.current_order_num,
        )
        .one_or_none()
    )


def _is_last_checkpoint(db: Session, team: models.Team) -> bool:
    total = _route_total_checkpoints(db, getattr(team, "route_id", None))
    return bool(total and int(getattr(team, "current_order_num", 0)) >= total)


def _advance_team_to_next_checkpoint(db: Session, team: models.Team) -> None:
    team.current_order_num = int(team.current_order_num or 1) + 1
    db.commit()


def _progress_tuple(db: Session, team: models.Team) -> Dict[str, int]:
    done = _approved_count_cp(db, team.id)
    total = _route_total_checkpoints(db, getattr(team, "route_id", None))
    return {"done": int(done), "total": int(total)}


# =============================================================================
# PUBLIC (requires x-app-secret) — под основным роутером /api
# =============================================================================


@router.post("/users/register", response_model=RegisterOut, dependencies=[Depends(require_secret)])
def register_or_assign(payload: RegisterIn, db: Session = Depends(get_db)):
    phone = norm_phone(payload.phone)

    # 1) user by tg_id
    user = db.query(models.User).filter(models.User.tg_id == payload.tg_id).one_or_none()

    # 2) create/match by phone
    if not user:
        user = db.query(models.User).filter(models.User.phone == phone).one_or_none()
        if user:
            user.tg_id = payload.tg_id
            user.first_name = payload.first_name
            # last_name из payload опционально, если не пришла — не перетираем
            if getattr(payload, "last_name", None):
                user.last_name = payload.last_name
        else:
            user = models.User(
                tg_id=payload.tg_id,
                phone=phone,
                first_name=payload.first_name,
                last_name=(payload.last_name or None),
            )
            db.add(user)
        db.flush()

    # 3) membership
    member = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).one_or_none()
    if not member:
        # Попробуем назначить из whitelist (если есть номер команды у телефона)
        from .whitelist import lookup as wl_lookup
        wl = wl_lookup(phone)
        preferred_team_id = None
        if wl:
            # приоритет: распарсенный team_number, затем raw team
            num = 0
            try:
                num = int(wl.get("team_number") or 0)
            except Exception:
                num = 0
            if not num and wl.get("team"):
                import re
                m = re.search(r"(\d+)", str(wl["team"]))
                if m:
                    num = int(m.group(1))
            if num:
                # Сначала ищем по имени вида "Команда №N"
                team_name = f"Команда №{num}"
                t = db.query(models.Team).filter(models.Team.name == team_name).one_or_none()
                if not t:
                    # На всякий случай попробуем по id == num (если заранее заведены как id=N)
                    t = db.query(models.Team).filter(models.Team.id == num).one_or_none()
                if not t:
                    # Если такой команды ещё нет — создаём её с нужным именем
                    t = models.Team(name=team_name)
                    db.add(t)
                    db.flush()
                preferred_team_id = t.id

        if preferred_team_id is None:
            team = _next_open_team(db)
        else:
            team = db.query(models.Team).get(preferred_team_id)  # type: ignore

        db.add(models.TeamMember(team_id=team.id, user_id=user.id, role="PLAYER"))
        db.commit()
        _ensure_captain_if_full(db, team.id)
    else:
        team = db.get(models.Team, member.team_id)

    # Если команда полная и маршрута нет — назначим автоматически
    if _team_is_full(db, team.id) and not getattr(team, "route_id", None):
        _auto_assign_route_if_needed(db, team)

    return RegisterOut(user_id=user.id, team_id=team.id, team_name=team.name)


@router.post("/participants/import", response_model=ImportReport, dependencies=[Depends(require_secret)])
def import_participants(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        content = file.file.read().decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to read CSV as UTF-8")

    reader = csv.DictReader(io.StringIO(content))
    total = loaded = skipped = 0

    for row in reader:
        total += 1
        phone = norm_phone(row.get("phone", ""))
        first_name = (row.get("first_name") or "").strip()
        if not (phone and first_name):
            skipped += 1
            continue

        exists = db.query(models.User).filter(models.User.phone == phone).first()
        if exists:
            skipped += 1
            continue

        db.add(models.User(
            tg_id=f"pending:{phone}",
            phone=phone,
            first_name=first_name,
            last_name=first_name,
        ))
        loaded += 1

    db.commit()
    return ImportReport(total=total, loaded=loaded, skipped=skipped)

@router.post("/submissions/article", dependencies=[Depends(require_secret)])
def submit_article(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    tg_id = str(payload.get("tg_id") or "").strip()
    url = (payload.get("url") or "").strip()
    caption = (payload.get("caption") or "").strip() or None
    if not tg_id or not url:
        raise HTTPException(400, "tg_id and url are required")

    user = db.query(models.User).filter(models.User.tg_id == tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "user_not_found")

    # берём первую команду пользователя (по ТЗ — фиксированные команды 1/2/3)
    tm = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).order_by(models.TeamMember.id.asc()).first()
    team_id = tm.team_id if tm else None

    can_url = canonical_url(url)

    # мягкая дедупликация
    dup = (
        db.query(models.Submission)
        .filter(models.Submission.type == "article",
                models.Submission.canonical_url == can_url,
                models.Submission.status.in_(("pending", "approved")))
        .first()
    )
    if dup:
        return {"status": "duplicate", "submission_id": dup.id}

    s = models.Submission(
        user_id=user.id, team_id=team_id, type="article",
        url=url, canonical_url=can_url, caption=caption, status="pending",
    )
    db.add(s)
    db.commit()

    # ответ с данными для карточки
    team = db.query(models.Team).filter(models.Team.id == team_id).one_or_none()
    return {
        "status": "ok",
        "id": s.id,
        "team_number": team.name if team and team.name else (f"Команда {team_id}" if team_id else "Без команды"),
        "team_id": team_id,
        "user": {"id": user.id, "tg_id": user.tg_id, "first_name": user.first_name, "last_name": user.last_name, "phone": user.phone},
        "kind": "article",
        "url": url,
        "caption": caption,
    }

@router.post("/submissions/photo", dependencies=[Depends(require_secret)])
def submit_photo(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    tg_id = str(payload.get("tg_id") or "").strip()
    file_id = (payload.get("tg_file_id") or "").strip()
    caption = (payload.get("caption") or "").strip() or None
    if not tg_id or not file_id:
        raise HTTPException(400, "tg_id and tg_file_id are required")

    user = db.query(models.User).filter(models.User.tg_id == tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "user_not_found")

    tm = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).order_by(models.TeamMember.id.asc()).first()
    team_id = tm.team_id if tm else None

    s = models.Submission(
        user_id=user.id, team_id=team_id, type="photo",
        tg_file_id=file_id, caption=caption, status="pending",
    )
    db.add(s)
    db.commit()

    team = db.query(models.Team).filter(models.Team.id == team_id).one_or_none()
    return {
        "status": "ok",
        "id": s.id,
        "team_number": team.name if team and team.name else (f"Команда {team_id}" if team_id else "Без команды"),
        "team_id": team_id,
        "user": {"id": user.id, "tg_id": user.tg_id, "first_name": user.first_name, "last_name": user.last_name, "phone": user.phone},
        "kind": "photo",
        "tg_file_id": file_id,
        "caption": caption,
    }



@admin.post("/submissions/{sid}/approve")
def admin_approve_submission(
    sid: int = Path(...),
    reviewer_tg: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    s = db.query(models.Submission).filter(models.Submission.id == sid).one_or_none()
    if not s:
        raise HTTPException(404, "not_found")
    s.status = "approved"
    s.reviewed_at = now_utc()
    s.reviewed_by_tg = int(reviewer_tg) if reviewer_tg and str(reviewer_tg).isdigit() else None
    db.commit()
    return {"status": "ok"}

@admin.post("/submissions/{sid}/reject")
def admin_reject_submission(
    sid: int = Path(...),
    reason: Optional[str] = Body(None, embed=True),
    reviewer_tg: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    s = db.query(models.Submission).filter(models.Submission.id == sid).one_or_none()
    if not s:
        raise HTTPException(404, "not_found")
    s.status = "rejected"
    s.reject_reason = reason or s.reject_reason
    s.reviewed_at = now_utc()
    s.reviewed_by_tg = int(reviewer_tg) if reviewer_tg and str(reviewer_tg).isdigit() else None
    db.commit()
    return {"status": "ok"}

@admin.post("/queue/register")
def admin_queue_register(
    admin_chat_id: int = Body(..., embed=True),
    message_id: int = Body(..., embed=True),
    submission_id: int = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    row = models.AdminQueueMessage(
        admin_chat_id=admin_chat_id, message_id=message_id, submission_id=submission_id, state="awaiting_reason"
    )
    db.add(row)
    db.commit()
    return {"status": "ok"}

@admin.post("/queue/reject-by-reply")
def admin_reject_by_reply(
    admin_chat_id: int = Body(..., embed=True),
    reply_to_message_id: int = Body(..., embed=True),
    reason: str = Body(..., embed=True),
    reviewer_tg: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    link = (
        db.query(models.AdminQueueMessage)
        .filter(models.AdminQueueMessage.admin_chat_id == admin_chat_id,
                models.AdminQueueMessage.message_id == reply_to_message_id,
                models.AdminQueueMessage.state == "awaiting_reason")
        .one_or_none()
    )
    if not link:
        raise HTTPException(404, "link_not_found")
    s = db.query(models.Submission).filter(models.Submission.id == link.submission_id).one_or_none()
    if not s:
        raise HTTPException(404, "submission_not_found")

    s.status = "rejected"
    s.reject_reason = reason
    s.reviewed_at = now_utc()
    s.reviewed_by_tg = int(reviewer_tg) if reviewer_tg and str(reviewer_tg).isdigit() else None
    link.state = "done"
    db.commit()
    return {"status": "ok", "submission_id": s.id}


@admin.get("/submissions/pending", response_model=list)
def admin_pending_submissions(db: Session = Depends(get_db)):
    """
    Получить все pending submissions для модерации в админ-чате.
    """
    q = (
        db.query(models.Submission, models.User, models.Team)
        .join(models.User, models.User.id == models.Submission.user_id, isouter=True)
        .join(models.Team, models.Team.id == models.Submission.team_id, isouter=True)
        .filter(models.Submission.status == "pending")
        .order_by(models.Submission.created_at.asc())
        .all()
    )
    
    out = []
    for submission, user, team in q:
        out.append({
            "id": submission.id,
            "type": submission.type,
            "url": submission.url,
            "canonical_url": submission.canonical_url,
            "tg_file_id": submission.tg_file_id,
            "caption": submission.caption,
            "created_at": submission.created_at.isoformat() if submission.created_at else None,
            "user": {
                "id": user.id if user else None,
                "tg_id": user.tg_id if user else None,
                "first_name": user.first_name if user else None,
                "last_name": user.last_name if user else None,
                "phone": user.phone if user else None,
            },
            "team": {
                "id": team.id if team else None,
                "name": team.name if team else None,
            },
        })
    return out


@router.get("/submissions/{sid}", dependencies=[Depends(require_secret)])
def get_submission(sid: int = Path(...), db: Session = Depends(get_db)):
    s = db.query(models.Submission).filter(models.Submission.id == sid).one_or_none()
    if not s:
        raise HTTPException(404, "not_found")
    
    user = None
    if s.user_id:
        user = db.query(models.User).filter(models.User.id == s.user_id).one_or_none()
        if not user:
            raise HTTPException(404, "user_not_found")
    
    team = None
    if s.team_id:
        team = db.query(models.Team).filter(models.Team.id == s.team_id).one_or_none()
        if not team:
            raise HTTPException(404, "team_not_found")
    
    return {
        "id": s.id, "type": s.type, "status": s.status,
        "url": s.url, "tg_file_id": s.tg_file_id, "caption": s.caption,
        "user": {"tg_id": user.tg_id if user else None, "first_name": user.first_name if user else None, "last_name": user.last_name if user else None, "phone": user.phone if user else None},
        "team": {"id": team.id if team else None, "name": team.name if team else None},
    }

@router.get("/teams/by-tg/{tg_id}", response_model=TeamOut, dependencies=[Depends(require_secret)])
def get_team_by_tg(tg_id: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.tg_id == tg_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    member = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team not assigned")

    team = db.get(models.Team, member.team_id)
    return TeamOut(
        team_id=team.id,
        team_name=team.name,
        role=member.role,
        is_captain=(member.role or "").upper() == "CAPTAIN",
        color=getattr(team, "color", None),
        route_id=getattr(team, "route_id", None),
    )


@router.get("/teams/roster/by-tg/{tg_id}", response_model=TeamRosterOut, dependencies=[Depends(require_secret)])
def get_roster_by_tg(tg_id: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.tg_id == tg_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    member = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Team not assigned")

    team = db.get(models.Team, member.team_id)

    cap_row = (
        db.query(models.TeamMember, models.User)
        .join(models.User, models.User.id == models.TeamMember.user_id)
        .filter(models.TeamMember.team_id == team.id, models.TeamMember.role == "CAPTAIN")
        .one_or_none()
    )
    captain = None
    if cap_row:
        m, u = cap_row
        captain = TeamMemberInfo(
            user_id=u.id, role=m.role, first_name=u.first_name, last_name=u.last_name,
            phone=u.phone, tg_id=u.tg_id,
        )

    rows = (
        db.query(models.TeamMember, models.User)
        .join(models.User, models.User.id == models.TeamMember.user_id)
        .filter(models.TeamMember.team_id == team.id)
        .order_by(models.TeamMember.id.asc())
        .all()
    )
    members = [
        TeamMemberInfo(
            user_id=u.id, role=m.role, first_name=u.first_name, last_name=u.last_name, phone=u.phone, tg_id=u.tg_id
        )
        for m, u in rows
    ]

    return TeamRosterOut(
        team_id=team.id,
        team_name=team.name,
        is_locked=bool(team.is_locked),
        captain=captain,
        members=members,
        color=getattr(team, "color", None),
        route_id=getattr(team, "route_id", None),
        can_rename=getattr(team, "can_rename", True),
    )


# ---------- TEAM: одноразовое переименование ----------
def _rename_core(data: TeamRenameIn, db: Session) -> TeamRenameOut:
    user = db.query(models.User).filter_by(tg_id=data.tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    member = db.query(models.TeamMember).filter_by(user_id=user.id).one_or_none()
    if not member:
        raise HTTPException(409, "User has no team")

    if (member.role or "").upper() != "CAPTAIN":
        raise HTTPException(403, "Only captain can rename")

    team = db.get(models.Team, member.team_id)

    if not _team_is_full(db, team.id):
        raise HTTPException(409, "Team is not full yet")
    if getattr(team, "started_at", None):
        raise HTTPException(409, "Team already started")
    if not getattr(team, "can_rename", True):
        raise HTTPException(409, "Rename already used")

    new_name = (data.new_name or "").strip()
    if len(new_name) < 2:
        raise HTTPException(400, "New name is too short")

    exists = (
        db.query(models.Team)
        .filter(models.Team.name == new_name, models.Team.id != team.id)
        .one_or_none()
    )
    if exists:
        raise HTTPException(409, "Team name already exists")

    team.name = new_name
    team.can_rename = False
    db.commit()

    return TeamRenameOut(ok=True, team_id=team.id, team_name=team.name, renamed=True)


@router.post("/team/rename", response_model=TeamRenameOut, dependencies=[Depends(require_secret)])
def team_rename_single(data: TeamRenameIn, db: Session = Depends(get_db)):
    return _rename_core(data, db)


@router.post("/teams/rename", response_model=TeamRenameOut, dependencies=[Depends(require_secret)])
def team_rename_plural(data: TeamRenameIn, db: Session = Depends(get_db)):
    return _rename_core(data, db)


# ---------- GAME: старт капитаном ----------
@router.post("/game/start", response_model=dict, dependencies=[Depends(require_secret)])
def game_start(
    tg_id: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter_by(tg_id=tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    member = db.query(models.TeamMember).filter_by(user_id=user.id).one_or_none()
    if not member:
        raise HTTPException(409, "User has no team")

    if (member.role or "").upper() != "CAPTAIN":
        raise HTTPException(403, "Only captain can start")

    team = db.get(models.Team, member.team_id)

    if getattr(team, "started_at", None):
        return {"ok": True, "message": "Already started", "team_id": team.id, "team_name": team.name}

    if not _team_is_full(db, team.id):
        raise HTTPException(409, "Team is not full yet")

    # Гарантируем маршрут: если ещё не назначен — назначим
    if not getattr(team, "route_id", None):
        ok = _auto_assign_route_if_needed(db, team)
        if not ok:
            raise HTTPException(409, "Route is not assigned for this team")

    # Нельзя стартовать с именем по умолчанию, если переименование ещё доступно
    is_default = bool(re.match(r"^Команда №\d+$", team.name or ""))
    if is_default and getattr(team, "can_rename", True):
        raise HTTPException(409, "Set custom team name first")

    team.started_at = now_utc()
    if not getattr(team, "current_order_num", None):
        team.current_order_num = 1
    db.commit()
    return {
        "ok": True,
        "message": "Started",
        "team_id": team.id,
        "team_name": team.name,
        "started_at": team.started_at.isoformat(),
    }


# ---------- GAME: текущая точка ----------
@router.get("/game/current", response_model=dict, dependencies=[Depends(require_secret)])
def game_current(tg_id: str = Query(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(tg_id=tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    member = db.query(models.TeamMember).filter_by(user_id=user.id).one_or_none()
    if not member:
        raise HTTPException(409, "User has no team")

    team = db.get(models.Team, member.team_id)

    # если финиш — сразу говорим об этом
    if getattr(team, "finished_at", None):
        return {"finished": True, "checkpoint": None}

    _require_team_started(team)

    cp = _current_checkpoint(db, team)
    if not cp:
        return {"finished": True, "checkpoint": None}

    total = _route_total_checkpoints(db, team.route_id)
    return {
        "finished": False,
        "checkpoint": {
            "id": cp.id,
            "order_num": cp.order_num,
            "title": cp.title,
            "riddle": cp.riddle,
            "photo_hint": getattr(cp, "photo_hint", None),
            "total": total,
        },
    }


# ---------- GAME: QR отключён (только фото) ----------
@router.post("/game/scan", response_model=GameScanOut, dependencies=[Depends(require_secret)])
def game_scan(_: GameScanIn, __: Session = Depends(get_db)):
    raise HTTPException(status_code=410, detail="QR flow disabled: answers are photos only")


# ---------- Фото: JSON — Proof(PENDING) на текущую точку ----------
@router.post("/game/photo", response_model=dict, dependencies=[Depends(require_secret)])
def submit_photo_json(
    data: Dict[str, Any] = Body(..., example={"tg_id": "123", "tg_file_id": "<file_id>"}),
    db: Session = Depends(get_db),
):
    tg_id = str(data.get("tg_id") or "")
    tg_file_id = str(data.get("tg_file_id") or "")

    if not (tg_id and tg_file_id):
        raise HTTPException(400, "tg_id and tg_file_id are required")

    user = db.query(models.User).filter_by(tg_id=tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    member = db.query(models.TeamMember).filter_by(user_id=user.id).one_or_none()
    if not member:
        raise HTTPException(409, "User has no team")

    if (member.role or "").upper() != "CAPTAIN":
        raise HTTPException(403, "Only captain can submit")

    team = db.get(models.Team, member.team_id)
    _require_team_started(team)

    cp = _current_checkpoint(db, team)
    if not cp:
        return {"ok": False, "message": "Route already finished"}

    # Если уже есть PENDING — не спамим
    pending_exists = db.query(models.Proof).filter(
        models.Proof.team_id == team.id,
        models.Proof.checkpoint_id == cp.id,
        models.Proof.status == "PENDING",
    ).first()
    if pending_exists:
        return {"ok": True, "message": "Already queued for moderation", "proof_id": pending_exists.id}

    # Если последний по этой точке был REJECTED — переоткроем его
    rejected = db.query(models.Proof).filter(
        models.Proof.team_id == team.id,
        models.Proof.checkpoint_id == cp.id,
        models.Proof.status == "REJECTED",
    ).order_by(models.Proof.id.desc()).first()

    if rejected:
        rejected.status = "PENDING"
        rejected.photo_file_id = tg_file_id
        rejected.submitted_by_user_id = user.id
        rejected.judged_by = None
        rejected.judged_at = None
        rejected.comment = None
        # гарантируем обновление updated_at
        if hasattr(rejected, "updated_at"):
            rejected.updated_at = now_utc()
        db.commit()
        db.refresh(rejected)
        return {"ok": True, "message": "Re-queued for moderation", "proof_id": rejected.id}

    # Первичная подача для этого чекпоинта
    proof = models.Proof(
        team_id=team.id,
        route_id=team.route_id,
        checkpoint_id=cp.id,
        photo_file_id=tg_file_id,         # Telegram file_id
        status="PENDING",
        submitted_by_user_id=user.id,
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return {"ok": True, "message": "Queued for moderation", "proof_id": proof.id}


# ---------- Фото: multipart — сохраняем файл локально и тоже Proof ----------
@router.post("/game/submit-photo", response_model=dict, dependencies=[Depends(require_secret)])
def submit_photo_file(
    tg_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter_by(tg_id=tg_id).one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    member = db.query(models.TeamMember).filter_by(user_id=user.id).one_or_none()
    if not member:
        raise HTTPException(409, "User has no team")

    if (member.role or "").upper() != "CAPTAIN":
        raise HTTPException(403, "Only captain can submit")

    team = db.get(models.Team, member.team_id)
    _require_team_started(team)

    cp = _current_checkpoint(db, team)
    if not cp:
        return {"ok": False, "message": "Route already finished"}

    ts = int(now_utc().timestamp())
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", file.filename or f"proof_{ts}.jpg")
    fname = f"team{team.id}_cp{cp.id}_{ts}_{safe_name}"
    path = os.path.join(PROOFS_DIR, fname)
    with open(path, "wb") as out:
        out.write(file.file.read())

    # Если уже есть PENDING — не спамим
    pending_exists = db.query(models.Proof).filter(
        models.Proof.team_id == team.id,
        models.Proof.checkpoint_id == cp.id,
        models.Proof.status == "PENDING",
    ).first()
    if pending_exists:
        return {"ok": True, "message": "Already queued for moderation", "proof_id": pending_exists.id, "file": fname}

    # Если был REJECTED — переоткроем
    rejected = db.query(models.Proof).filter(
        models.Proof.team_id == team.id,
        models.Proof.checkpoint_id == cp.id,
        models.Proof.status == "REJECTED",
    ).order_by(models.Proof.id.desc()).first()

    if rejected:
        rejected.status = "PENDING"
        rejected.photo_file_id = path
        rejected.submitted_by_user_id = user.id
        rejected.judged_by = None
        rejected.judged_at = None
        rejected.comment = None
        if hasattr(rejected, "updated_at"):
            rejected.updated_at = now_utc()
        db.commit()
        db.refresh(rejected)
        return {"ok": True, "message": "Re-queued for moderation", "proof_id": rejected.id, "file": fname}

    # Первичная подача
    proof = models.Proof(
        team_id=team.id,
        route_id=team.route_id,
        checkpoint_id=cp.id,
        photo_file_id=path,                     # локальный путь
        status="PENDING",
        submitted_by_user_id=user.id,
    )
    db.add(proof)
    db.commit()
    db.refresh(proof)
    return {"ok": True, "message": "Queued for moderation", "proof_id": proof.id, "file": fname}


# ---------- ЛИДЕРБОРД по маршруту ----------
@router.get("/leaderboard", response_model=list, dependencies=[Depends(require_secret)])
def leaderboard(db: Session = Depends(get_db)):
    # Баллы из ENV (или 1/1 по умолчанию)
    art_pts = int(os.getenv("ARTICLE_POINTS", "1") or "1")
    photo_pts = int(os.getenv("PHOTO_POINTS", "1") or "1")

    # Сначала получаем все команды
    teams = db.query(models.Team).all()
    
    # Затем для каждой команды считаем баллы
    rows = []
    for team in teams:
        # Считаем одобренные статьи
        article_points = db.query(func.count(models.Submission.id)).filter(
            models.Submission.team_id == team.id,
            models.Submission.type == "article",
            models.Submission.status == "approved"
        ).scalar() or 0
        
        # Считаем одобренные фото
        photo_points = db.query(func.count(models.Submission.id)).filter(
            models.Submission.team_id == team.id,
            models.Submission.type == "photo", 
            models.Submission.status == "approved"
        ).scalar() or 0
        
        # Считаем общее количество одобренных
        approved_total = db.query(func.count(models.Submission.id)).filter(
            models.Submission.team_id == team.id,
            models.Submission.status == "approved"
        ).scalar() or 0
        
        rows.append({
            "team_id": team.id,
            "team_name": team.name or f"Команда {team.id}",
            "article_points": article_points * art_pts,
            "photo_points": photo_points * photo_pts,
            "approved_total": approved_total,
        })

    # Сортируем по общему количеству баллов
    rows.sort(key=lambda x: (-(x["article_points"] + x["photo_points"]), -x["approved_total"], x["team_id"] or 0))
    
    # Добавляем total_points для совместимости
    for r in rows:
        r["total_points"] = r["article_points"] + r["photo_points"]
    
    return rows

@router.get("/users/all", response_model=list, dependencies=[Depends(require_secret)])
def get_all_users(db: Session = Depends(get_db)):
    """Получить всех зарегистрированных пользователей для рассылки"""
    users = db.query(models.User).filter(models.User.tg_id.isnot(None)).all()
    return [
        {
            "id": user.id,
            "tg_id": user.tg_id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "team_id": user.teams[0].team_id if user.teams else None,
        }
        for user in users
    ]

# =============================================================================
# ADMIN (под /api/admin, защищён require_secret)
# =============================================================================
@admin.get("/teams/search", response_model=list[dict])
def admin_search_teams(
    q: str = Query(..., min_length=1, description="Substring search (case-insensitive)"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    # Postgres: ILIKE / SQLite: LOWER(name) LIKE LOWER(:q)
    # Если у тебя Postgres — оставь .ilike; для SQLite замени на func.lower(...)
    rows = (
        db.query(models.Team.id, models.Team.name, models.Team.started_at)
        .filter(models.Team.name.ilike(f"%{q}%"))  # для SQLite: func.lower(models.Team.name).like(func.lower(f"%{q}%"))
        .order_by(models.Team.name.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "team_id": r.id,
            "team_name": r.name,
            "started_at": r.started_at.isoformat() if r.started_at else None,
        }
        for r in rows
    ]
@admin.get("/teams/{team_id}", response_model=TeamAdminOut)
def admin_get_team(team_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
    team = db.get(models.Team, team_id)
    if not team:
        raise HTTPException(404, "Team not found")
    return dump_team_admin(db, team)


@admin.get("/teams", response_model=List[TeamAdminOut])
def admin_list_teams(db: Session = Depends(get_db)):
    teams = db.query(models.Team).order_by(models.Team.id.asc()).all()
    return [dump_team_admin(db, t) for t in teams]


@admin.post("/teams/lock", response_model=List[TeamAdminOut])
def admin_lock_all(db: Session = Depends(get_db)):
    teams = db.query(models.Team).all()
    for t in teams:
        t.is_locked = True
        _ensure_captain_if_full(db, t.id)
    db.commit()
    teams = db.query(models.Team).order_by(models.Team.id.asc()).all()
    return [dump_team_admin(db, t) for t in teams]


@admin.post("/teams/unlock", response_model=List[TeamAdminOut])
def admin_unlock_all(db: Session = Depends(get_db)):
    db.execute(update(models.Team).values(is_locked=False))
    db.commit()
    teams = db.query(models.Team).order_by(models.Team.id.asc()).all()
    return [dump_team_admin(db, t) for t in teams]


@admin.post("/teams/{team_id}/set-captain", response_model=TeamAdminOut)
def admin_set_captain(
    data: SetCaptainIn,
    team_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
):
    if not data.user_id and not data.tg_id:
        raise HTTPException(400, "Provide user_id or tg_id")

    team = db.get(models.Team, team_id)
    if not team:
        raise HTTPException(404, "Team not found")

    q = db.query(models.User)
    q = q.filter(models.User.id == data.user_id) if data.user_id else q.filter(models.User.tg_id == str(data.tg_id))
    user = q.one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    member = (
        db.query(models.TeamMember)
        .filter(models.TeamMember.team_id == team_id, models.TeamMember.user_id == user.id)
        .one_or_none()
    )
    if not member:
        raise HTTPException(409, "User is not a member of this team")

    # снять прежнего капитана → назначить нового
    db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id, models.TeamMember.role == "CAPTAIN"
    ).update({models.TeamMember.role: "PLAYER"})
    member.role = "CAPTAIN"
    db.commit()

    return dump_team_admin(db, team)

@admin.post("/teams/{team_id}/unset-captain", response_model=TeamAdminOut)
def admin_unset_captain(team_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
    db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id, models.TeamMember.role == "CAPTAIN"
    ).update({models.TeamMember.role: "PLAYER"})
    db.commit()
    team = db.get(models.Team, team_id)
    return dump_team_admin(db, team)


@admin.post("/members/move", response_model=TeamAdminOut)
def admin_move_member(data: MoveMemberIn, db: Session = Depends(get_db)):
    if not data.user_id and not data.tg_id:
        raise HTTPException(400, "Provide user_id or tg_id")

    q = db.query(models.User)
    q = q.filter(models.User.id == data.user_id) if data.user_id else q.filter(models.User.tg_id == str(data.tg_id))
    user = q.one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    member = db.query(models.TeamMember).filter(models.TeamMember.user_id == user.id).one_or_none()
    if not member:
        raise HTTPException(409, "User has no team membership")

    dest = db.get(models.Team, data.dest_team_id)
    if not dest:
        raise HTTPException(404, "Destination team not found")

    member.team_id = dest.id
    member.role = "CAPTAIN" if data.make_captain else "PLAYER"
    db.commit()

    return dump_team_admin(db, dest)


# ---------- admin: tasks CRUD (совместимость со старым UI) ----------
@admin.get("/tasks", response_model=List[TaskOut])
def admin_tasks_list(db: Session = Depends(get_db)):
    items = (
        db.query(models.Task)
        .order_by(func.coalesce(models.Task.order, 10**9), models.Task.id.asc())
        .all()
    )
    return items


@admin.post("/tasks", response_model=TaskOut)
def admin_tasks_create(data: TaskCreateIn, db: Session = Depends(get_db)):
    exists = db.query(models.Task).filter(models.Task.code == data.code).one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="Task code already exists")

    obj = models.Task(
        code=data.code.strip(),
        title=data.title.strip(),
        description=data.description,
        points=int(data.points) if data.points is not None else 1,
        is_active=True if data.is_active is None else bool(data.is_active),
        order=data.order,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@admin.patch("/tasks/{task_id}", response_model=TaskOut)
def admin_tasks_update(
    task_id: int = Path(..., ge=1),
    data: TaskUpdateIn | None = Body(None),
    db: Session = Depends(get_db),
):
    obj = db.get(models.Task, task_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Task not found")

    if data is None:
        data = TaskUpdateIn()

    if data.code is not None:
        exists = (
            db.query(models.Task)
            .filter(models.Task.code == data.code, models.Task.id != obj.id)
            .one_or_none()
        )
        if exists:
            raise HTTPException(status_code=409, detail="Task code already exists")
        obj.code = data.code.strip()

    if data.title is not None:
        obj.title = data.title.strip()
    if data.description is not None:
        obj.description = data.description
    if data.points is not None:
        obj.points = int(data.points)
    if data.is_active is not None:
        obj.is_active = bool(data.is_active)
    if data.order is not None:
        obj.order = data.order

    db.commit()
    db.refresh(obj)
    return obj


@admin.delete("/tasks/{task_id}", response_model=dict)
def admin_tasks_delete(task_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
    obj = db.get(models.Task, task_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(obj)
    db.commit()
    return {"ok": True}


@admin.post("/tasks/reset-progress", response_model=dict)
def admin_tasks_reset_progress(db: Session = Depends(get_db)):
    # Старый прогресс больше не используется, но ручку оставляем no-op совместимой
    db.query(models.TeamTaskProgress).delete()
    db.commit()
    return {"ok": True}


# ---------- МОДЕРАЦИЯ ФОТО (Proof) ----------
@admin.get("/proofs/pending", response_model=list)
def admin_pending(db: Session = Depends(get_db)):
    q = (
        db.query(models.Proof, models.Team, models.Checkpoint, models.Route, models.User)
        .join(models.Team, models.Team.id == models.Proof.team_id)
        .join(models.Checkpoint, models.Checkpoint.id == models.Proof.checkpoint_id)
        .join(models.Route, models.Route.id == models.Proof.route_id)
        .join(models.User, models.User.id == models.Proof.submitted_by_user_id, isouter=True)
        .filter(models.Proof.status == "PENDING")
        .order_by(models.Proof.created_at.asc())
        .all()
    )
    out = []
    for proof, team, cp, route, user in q:
        out.append({
            "id": proof.id,
            "team_id": team.id,
            "team_name": team.name,
            "route": route.code,
            "checkpoint_id": cp.id,
            "order_num": cp.order_num,
            "checkpoint_title": cp.title,
            "photo_file_id": proof.photo_file_id,
            "submitted_by_user_id": getattr(proof, "submitted_by_user_id", None),
            "submitted_by_tg_id": getattr(user, "tg_id", None),
            "created_at": proof.created_at.isoformat() if getattr(proof, "created_at", None) else None,
            "updated_at": getattr(proof, "updated_at", None).isoformat() if getattr(proof, "updated_at", None) else None,  # <-- важно для вотчера
        })
    return out


@admin.post("/proofs/{proof_id}/approve", response_model=dict)
def admin_approve(proof_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
    proof = db.get(models.Proof, proof_id)
    if not proof:
        raise HTTPException(404, "Proof not found")
    if proof.status != "PENDING":
        return {"ok": False, "message": "Already processed"}

    proof.status = "APPROVED"
    proof.judged_by = 0
    proof.judged_at = now_utc()
    if hasattr(proof, "updated_at"):
        proof.updated_at = now_utc()
    db.commit()

    team = db.get(models.Team, proof.team_id)

    if _is_last_checkpoint(db, team):
        if not getattr(team, "finished_at", None):
            team.finished_at = now_utc()
            db.commit()
    else:
        _advance_team_to_next_checkpoint(db, team)

    return {"ok": True, "progress": _progress_tuple(db, team)}


@admin.post("/proofs/{proof_id}/reject", response_model=dict)
def admin_reject(proof_id: int = Path(..., ge=1), db: Session = Depends(get_db)):
    proof = db.get(models.Proof, proof_id)
    if not proof:
        raise HTTPException(404, "Proof not found")
    if proof.status != "PENDING":
        return {"ok": False, "message": "Already processed"}

    proof.status = "REJECTED"
    proof.judged_by = 0
    proof.judged_at = now_utc()
    if hasattr(proof, "updated_at"):
        proof.updated_at = now_utc()
    db.commit()
    team = db.get(models.Team, proof.team_id)
    return {"ok": True, "progress": _progress_tuple(db, team)}


# Подключаем ТОЛЬКО админский саброутер
router.include_router(admin)