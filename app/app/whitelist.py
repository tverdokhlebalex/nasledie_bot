# app/app/whitelist.py
import csv
import io
import os
import re
import threading
import logging
from pathlib import Path
from typing import Optional, Dict

STRICT = os.getenv("STRICT_WHITELIST", "false").strip().lower() in {"1","true","yes","on"}
# В контейнере рабочая директория API — /code/app,
# поэтому по умолчанию укажем абсолютный путь к data в корне проекта
CSV_PATH = os.getenv("WHITELIST_PATH", "/code/data/participants_template.csv")

_lock = threading.RLock()
_data: Dict[str, Dict] = {}
_loaded = False

def _norm_phone(phone: str) -> Optional[str]:
    if not phone:
        return None
    p = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    # допускаем вход: 8ХХХ..., 7ХХХ..., +7ХХХ..., 9ХХХ...
    if p.startswith("+"):
        pass
    elif p.startswith("8") and len(p) == 11:
        p = "+7" + p[1:]
    elif p.startswith("7") and len(p) == 11:
        p = "+" + p
    elif p.startswith("9") and len(p) == 10:
        p = "+7" + p
    # быстрая валидация
    if not (p.startswith("+7") and len(p) == 12 and p[1:].isdigit()):
        return None
    return p

ENCODINGS = ("utf-8-sig", "utf-16", "cp1251", "utf-8")


def _open_text(path: str) -> str:
    with open(path, "rb") as fb:
        raw = fb.read()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_dialect(text: str) -> csv.Dialect:
    sample = text[:4096]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;")
    except Exception:
        return csv.excel


def _normalize_headers(fieldnames: list[str] | None) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for h in fieldnames or []:
        key = (h or "").strip().lower()
        if key:
            headers[key] = h
    return headers


def _team_number(val: str | None) -> int:
    if not val:
        return 0
    m = re.findall(r"\d+", str(val))
    return int(m[0]) if m else 0


def _load_locked() -> None:
    global _data, _loaded
    _data = {}
    path = Path(CSV_PATH)
    if not path.exists():
        _loaded = True
        return
    # Читаем с авто-детектом кодировки/разделителя
    text = _open_text(str(path)).lstrip("\ufeff")
    dialect = _detect_dialect(text)
    rd = csv.DictReader(io.StringIO(text), dialect=dialect)
    header_map = _normalize_headers(rd.fieldnames)

    # Ищем реальные имена колонок в файле
    def pick(*candidates: str) -> Optional[str]:
        for c in candidates:
            key = c.strip().lower()
            if key in header_map:
                return header_map[key]
        return None

    col_phone = pick("phone", "телефон")
    col_first = pick("first_name", "имя")
    col_last = pick("last_name", "фамилия")
    col_team = pick("team_number", "team", "team_id", "номер команды", "номер_команды", "команда")

    for row in rd:
        raw_phone = row.get(col_phone or "", "") if col_phone else ""
        phone = _norm_phone(str(raw_phone))
        if not phone:
            continue
        first_name = (row.get(col_first or "", "") if col_first else "").strip()
        last_name = (row.get(col_last or "", "") if col_last else "").strip() or None
        team_val = row.get(col_team or "", "") if col_team else ""
        team_number = _team_number(str(team_val)) if col_team else 0

        _data[phone] = {
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            # raw значение и распарсенный номер для надёжности
            "team": (str(team_val).strip() or None),
            "team_number": team_number,
        }
    _loaded = True
    try:
        sample = sorted({v.get("team_number", 0) for v in _data.values() if v.get("team_number")})[:10]
        logging.info("[WHITELIST] loaded %d rows from %s. Teams sample: %s", len(_data), str(path), sample)
    except Exception:
        pass

def ensure_loaded() -> None:
    with _lock:
        if not _loaded:
            _load_locked()

def reload() -> int:
    with _lock:
        _load_locked()
        return len(_data)

def lookup(phone: str) -> Optional[Dict]:
    ensure_loaded()
    p = _norm_phone(phone)
    if not p:
        return None
    return _data.get(p)

def is_allowed(phone: str) -> bool:
    """true в нон-строгом режиме всегда, в строгом — только если есть совпадение."""
    if not STRICT:
        return True
    return lookup(phone) is not None

def norm_phone(phone: str) -> Optional[str]:
    return _norm_phone(phone)

def stats() -> Dict:
    ensure_loaded()
    return {"strict": STRICT, "size": len(_data), "path": CSV_PATH}