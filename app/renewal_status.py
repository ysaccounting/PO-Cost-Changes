"""RENEWAL STATUS WORDS: what a season ticket record's renewal status means.

Three statuses come built in -- Renewed, In Progress, Not Renewed -- each
treated a fixed way: Renewed is coded (and can be added automatically) with
its pill in its color; In Progress is coded but flagged; Not Renewed goes to
review. A status nobody listed goes to review as unrecognized.

The DASHBOARD owns the Engine Notation (the words every company's records are
read by) and each status's color; more statuses can be added there, each
treated as one of the three. A COMPANY adds its own words beside each one
(Your Notation), and can add statuses of its own, with a color.

Stored as:
  AppSetting "renewal_statuses"         [{key, name, kind, words, color}]  (Dashboard)
  company var "sth_status_words"        {status key: [words]}             (Your Notation)
  company var "renewal_custom"          [{key, name, kind, words, color}]  (the company's own)
Before this, the Engine Words were {active, dead} under "sth_engine_words";
they are read as the starting Engine Notation.
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

KEY = "renewal_statuses"
CUSTOM_VAR = "renewal_custom"
KINDS = {"active": "Renewed", "progress": "In Progress", "dead": "Not Renewed"}
COLORS = {"active": "#dcfce7", "progress": "#fef3c7", "dead": "#fee2e2"}
BUILT_IN = ("active", "progress", "dead")


def _clean_words(v) -> list:
    if isinstance(v, str):
        v = v.split(",")
    out, seen = [], set()
    for x in v or []:
        t = str(x).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _color(v, kind: str) -> str:
    v = str(v or "").strip()
    return v if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else COLORS.get(kind, "#e5e7eb")


def engine_statuses(db: Session) -> list:
    """The Dashboard's statuses, the three built in first."""
    from engine.hal import STATUS_ACTIVE, STATUS_DEAD, STATUS_IN_PROGRESS
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, KEY)
    saved = []
    if r is not None and (r.value or "").strip():
        try:
            saved = json.loads(r.value) or []
        except ValueError:
            saved = []
    by_key = {s.get("key"): s for s in saved if isinstance(s, dict) and s.get("key")}
    # The Engine Words as they were edited before this grid existed.
    old = {}
    o = db.get(AppSettingRow, "sth_engine_words")
    if o is not None and (o.value or "").strip():
        try:
            old = json.loads(o.value) or {}
        except ValueError:
            old = {}
    base = {"active": sorted(STATUS_ACTIVE), "progress": sorted(STATUS_IN_PROGRESS), "dead": sorted(STATUS_DEAD)}
    out = []
    for k in BUILT_IN:
        s = by_key.get(k) or {}
        words = s.get("words") if "words" in s else (old.get(k) if k in old else base[k])
        out.append({"key": k, "name": KINDS[k], "kind": k, "builtin": True,
                    "words": _clean_words(words), "color": _color(s.get("color"), k)})
    for s in saved:
        if not isinstance(s, dict) or s.get("key") in BUILT_IN or not s.get("key"):
            continue
        kind = s.get("kind") if s.get("kind") in KINDS else "dead"
        out.append({"key": str(s["key"]), "name": str(s.get("name") or "").strip() or "Status",
                    "kind": kind, "builtin": False, "words": _clean_words(s.get("words")),
                    "color": _color(s.get("color"), kind)})
    return out


def save_engine_statuses(db: Session, rows: list) -> list:
    from .models_db import AppSettingRow
    clean, used = [], {}
    n = 0
    for s in rows or []:
        key = str(s.get("key") or "").strip()
        if key in BUILT_IN:
            kind, name = key, KINDS[key]
        else:
            name = str(s.get("name") or "").strip()
            if not name:
                raise ValueError("every status needs a name")
            kind = s.get("kind") if s.get("kind") in KINDS else "dead"
            if not key or not key.startswith("e_"):
                n += 1
                key = "e_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or f"e_{n}"
        words = _clean_words(s.get("words"))
        for w in words:
            if w.lower() in used and used[w.lower()] != key:
                raise ValueError(f'"{w}" is listed under two statuses; a word means one thing')
            used[w.lower()] = key
        clean.append({"key": key, "name": name, "kind": kind, "words": words, "color": _color(s.get("color"), kind)})
    keys = [c["key"] for c in clean]
    for k in BUILT_IN:
        if k not in keys:
            raise ValueError(f"{KINDS[k]} can't be removed")
    if len(set(keys)) != len(keys):
        raise ValueError("two statuses have the same name")
    db.merge(AppSettingRow(key=KEY, value=json.dumps(clean)))
    db.flush()                       # a pending row is not found by db.get
    return engine_statuses(db)


def company_state(db: Session, company: str) -> dict:
    """{engine: [...], words: {key: [...]}, custom: [...]} for one company."""
    from . import company_vars as _cv
    with _cv.for_company(db, company):
        raw_w = _cv.value("sth_status_words") or ""
        raw_c = _cv.value("renewal_custom") or ""
    try:
        w = json.loads(raw_w) if raw_w.strip() else {}
    except ValueError:
        w = {}
    try:
        c = json.loads(raw_c) if raw_c.strip() else []
    except ValueError:
        c = []
    words = {str(k): _clean_words(v) for k, v in (w or {}).items()}
    custom = []
    for s in c or []:
        if isinstance(s, dict) and s.get("key"):
            kind = s.get("kind") if s.get("kind") in KINDS else "dead"
            custom.append({"key": str(s["key"]), "name": str(s.get("name") or "Status"), "kind": kind,
                           "words": _clean_words(s.get("words")), "color": _color(s.get("color"), kind)})
    return {"engine": engine_statuses(db), "words": words, "custom": custom}


def save_company(db: Session, company: str, words: dict, custom: list, actor: str) -> dict:
    from . import company_vars as _cv
    eng = engine_statuses(db)
    ekeys = {s["key"] for s in eng}
    cw = {k: _clean_words(v) for k, v in (words or {}).items() if k in ekeys}
    cc, n = [], 0
    for s in custom or []:
        name = str(s.get("name") or "").strip()
        if not name:
            raise ValueError("every status needs a name")
        key = str(s.get("key") or "").strip()
        if not key.startswith("c_"):
            n += 1
            key = "c_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or str(n))
        kind = s.get("kind") if s.get("kind") in KINDS else "dead"
        cc.append({"key": key, "name": name, "kind": kind, "words": _clean_words(s.get("words")),
                   "color": _color(s.get("color"), kind)})
    # A WORD MEANS ONE THING, across the Engine Notation and this company's.
    used = {}
    for s in eng:
        for w in s["words"] + cw.get(s["key"], []):
            if w.lower() in used and used[w.lower()] != s["key"]:
                raise ValueError(f'"{w}" is listed under two statuses; a word means one thing')
            used[w.lower()] = s["key"]
    for s in cc:
        for w in s["words"]:
            if w.lower() in used and used[w.lower()] != s["key"]:
                raise ValueError(f'"{w}" is listed under two statuses; a word means one thing')
            used[w.lower()] = s["key"]
    if len({s["key"] for s in cc}) != len(cc):
        raise ValueError("two statuses have the same name")
    _cv.set_value(db, company, "sth_status_words", json.dumps(cw), actor)
    _cv.set_value(db, company, CUSTOM_VAR, json.dumps(cc), actor)
    db.flush()
    return company_state(db, company)


def words_by_kind(db: Session, company: str) -> dict:
    """What status_kind reads: the company's words per kind (its notation for
    every status, and its own statuses), and the Engine Notation per kind."""
    st = company_state(db, company)
    out = {k: [] for k in KINDS}
    eng = {k: [] for k in KINDS}
    for s in st["engine"]:
        eng[s["kind"]] += s["words"]
        out[s["kind"]] += st["words"].get(s["key"], [])
    for s in st["custom"]:
        out[s["kind"]] += s["words"]
    res = dict(out)
    res["_engine_active"], res["_engine_progress"], res["_engine_dead"] = eng["active"], eng["progress"], eng["dead"]
    return res


def colors(db: Session, company: str) -> dict:
    """word (lower case) -> pill color, for this company's Bank Feed."""
    st = company_state(db, company)
    out = {}
    for s in st["engine"]:
        for w in s["words"] + st["words"].get(s["key"], []):
            out[w.lower()] = s["color"]
        out[s["name"].lower()] = s["color"]
    for s in st["custom"]:
        for w in s["words"]:
            out[w.lower()] = s["color"]
        out[s["name"].lower()] = s["color"]
    return out
