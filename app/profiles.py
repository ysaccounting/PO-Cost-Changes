"""Profiles: which person a general card's charges belong to.

A grid per company (Configuration > Profiles), read in order -- the FIRST row
that matches a charge sets its Profile. A row names a card account, a bank
detail condition (Contains one of several comma-separated values, or Any),
the teams it applies to (or Any), and the profile to set. The Profile is the
charge's cardholder name, which is also what STH matching uses to find the
season-ticket holder -- so a regular Amex or Citi card used for many accounts
has each charge matched to the right person.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

KEY = "profile_rules:"


def rules(db: Session, company: str) -> list:
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, KEY + company)
    try:
        return json.loads(row.value) if row and row.value else []
    except ValueError:
        return []


def clean(rows: list) -> list:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        op = (r.get("op") or "any").strip().lower()
        op = "contains" if op == "contains" else "any"
        teams = [t.strip() for t in (r.get("teams") or []) if str(t).strip()]
        if not teams or any(t.lower() == "any" for t in teams):
            teams = ["Any"]
        prof = (r.get("profile") or "").strip()
        # ONE OR SEVERAL CARD ACCOUNTS (the picker is multi-select): kept as
        # their keys, comma separated.
        _s = r.get("source") or ""
        _s = _s if isinstance(_s, list) else str(_s).split(",")
        src = ",".join(dict.fromkeys(x.strip() for x in _s if str(x).strip()))
        if not prof or not src:
            continue                    # a row needs a card account and a profile
        out.append({"name": str(r.get("name") or "").strip()[:80], "source": src, "op": op,
                    "value": (r.get("value") or "").strip() if op == "contains" else "",
                    "teams": teams, "profile": prof})
    return out


def save(db: Session, company: str, rows: list) -> list:
    from .models_db import AppSettingRow
    rows = clean(rows)
    db.merge(AppSettingRow(key=KEY + company, value=json.dumps(rows)))
    db.commit()
    return rows


class _TeamFinder:
    """The team a bank detail names -- the same alias table and abbreviation
    expansion the Bank Feed uses, longest alias first."""

    def __init__(self, db: Session, company: str = ""):
        from engine.hal import expand_text
        from .review import team_claim_pairs
        self._x = expand_text
        try:
            self.table = {expand_text(k): v for k, v in team_claim_pairs(db, company=company)}
        except Exception:                               # noqa: BLE001
            self.table = {}
        self.order = sorted(self.table, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        low = self._x(text or "")
        if not low:
            return ""
        if low.strip() in self.table:
            return self.table[low.strip()]
        for a in self.order:
            if len(a) > 3 and a in low:
                return self.table[a]
        return ""


def profile_for(charge, rows: list, team_of) -> str:
    """The profile the FIRST matching row gives this charge, or ""."""
    detail = f"{charge.merchant or ''} {charge.raw_description or ''}".lower()
    team = None
    for r in rows:
        if charge.source not in {x.strip() for x in str(r["source"]).split(",")}:
            continue
        if r["op"] == "contains":
            vals = [v.strip().lower() for v in r["value"].split(",") if v.strip()]
            if not vals or not any(v in detail for v in vals):
                continue
        if r["teams"] != ["Any"]:
            if team is None:
                team = team_of(charge.merchant or charge.raw_description or "")
            if not team or team.lower() not in {t.lower() for t in r["teams"]}:
                continue
        return r["profile"]
    return ""


SET_KEY = "profiles_set:"      # + company -> {charge_id: {"was": name, "set": name}}


def _setmap(db: Session, company: str) -> dict:
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, SET_KEY + company)
    try:
        return json.loads(r.value) if r and r.value else {}
    except ValueError:
        return {}


def apply(db: Session, company: str, charge_ids: list | None = None) -> int:
    """Set the Profile of charges in For Review from the grid -- the given
    charges (as they arrive), or every one in For Review. Returns how many
    changed.

    A PROFILE A RULE SET FOLLOWS THE RULE. Each one is remembered with the
    name it replaced; when the rule changes or goes, the charge takes the new
    rule's profile or gets its own name back. A profile a person typed in
    since is theirs and is left alone."""
    from .models_db import AppSettingRow, ChargeRow
    rows = rules(db, company)
    seen = _setmap(db, company)
    if not rows and not seen:
        return 0
    q = db.query(ChargeRow).filter(ChargeRow.company == company,
                                   ChargeRow.status == "for_review")
    if charge_ids is not None:
        q = q.filter(ChargeRow.charge_id.in_(list(charge_ids)))
    finder = _TeamFinder(db, company)
    n = 0
    live = set()
    for c in q.all():
        live.add(c.charge_id)
        cur = c.cardholder_name or ""
        rec = seen.get(c.charge_id)
        if rec and cur != rec.get("set"):
            seen.pop(c.charge_id, None)          # a person changed it since
            rec = None
        base = rec.get("was", "") if rec else cur
        p = profile_for(c, rows, finder) if rows else ""
        if p:
            if p != cur:
                c.cardholder_name = p
                n += 1
            if p != base:
                seen[c.charge_id] = {"was": base, "set": p}
            else:
                seen.pop(c.charge_id, None)
        elif rec:
            c.cardholder_name = base
            seen.pop(c.charge_id, None)
            n += 1
    if charge_ids is None:
        seen = {k: v for k, v in seen.items() if k in live}   # only charges still in For Review
    db.merge(AppSettingRow(key=SET_KEY + company, value=json.dumps(seen)))
    db.commit()
    return n


def backfill_blank(db: Session, company: str, source: str = "", cards: dict | None = None,
                   run_rules: bool = True) -> int:
    """FILL IN CHARGES WITH NO PROFILE. A charge's card is known by its last
    four; when exactly one name goes with those four -- on the program's card
    list, or on the same card account's other charges -- a blank Profile takes
    it. Then the Credit Card Profiles rules run over For Review as usual.
    Returns how many were filled."""
    from collections import defaultdict
    from .models_db import ChargeRow
    q = db.query(ChargeRow).filter(ChargeRow.company == company)
    if source:
        q = q.filter(ChargeRow.source == source)
    rows = q.all()
    names = defaultdict(set)          # (source, last4) -> names seen
    for r in rows:
        if (r.cardholder_name or "").strip() and r.card_last4:
            names[(r.source, r.card_last4)].add(r.cardholder_name.strip())
    by_card = defaultdict(set)        # last4 -> names on the card list
    for c in (cards or {}).values():
        if isinstance(c, dict) and c.get("last4") and c.get("name"):
            by_card[str(c["last4"])].add(c["name"].strip())
    n = 0
    for r in rows:
        if (r.cardholder_name or "").strip() or not r.card_last4:
            continue
        cand = by_card.get(r.card_last4) or names.get((r.source, r.card_last4)) or set()
        if len(cand) == 1:
            r.cardholder_name = next(iter(cand))
            n += 1
    if n:
        db.commit()
    if run_rules:
        try:
            apply(db, company)
        except Exception:                 # noqa: BLE001
            pass
    return n
