"""Last statement date and balance for credit cards, from Plaid's Liabilities
product -- for bank reconciliations on Bank / Card Accounts.

Off until PLAID_LIABILITIES is set: Liabilities must first be enabled in the
Plaid dashboard (it is billed separately), and asking Plaid for a product that
is not enabled would make connecting cards fail. Plaid reports statements for
CREDIT accounts only; checking and savings show none. A card connected before
Liabilities was enabled needs the cardholder to reconnect once, to agree to it.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)
KEY = "stmt:"                  # + company|source -> {"date","balance","at"} or {"note"}
CHECKED = "stmt_checked"       # the day statements were last read
HISTORY = 2                    # earlier statements kept for the hover


def clear(db: Session, company: str, source: str) -> None:
    """Forget a card's statements -- on disconnecting it."""
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, f"{KEY}{company}|{source}")
    if r is not None:
        db.delete(r)


def enabled() -> bool:
    from .config import settings
    return bool(getattr(settings, "plaid_liabilities", False))


def get(db: Session, company: str, source: str) -> dict:
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, f"{KEY}{company}|{source}")
    try:
        return json.loads(r.value) if r and r.value else {}
    except ValueError:
        return {}


def _put(db: Session, company: str, source: str, value: dict) -> None:
    from .models_db import AppSettingRow
    db.merge(AppSettingRow(key=f"{KEY}{company}|{source}", value=json.dumps(value)))


NOTES = {
    "ADDITIONAL_CONSENT_REQUIRED": "Reconnect this card once to allow statements",
    "PRODUCTS_NOT_SUPPORTED": "This bank doesn't report statements to Plaid",
    "PRODUCT_NOT_SUPPORTED": "This bank doesn't report statements to Plaid",
    "INVALID_PRODUCT": "Liabilities isn't enabled in the Plaid dashboard",
    "PRODUCT_NOT_ENABLED": "Liabilities isn't enabled in the Plaid dashboard",
    "NO_LIABILITY_ACCOUNTS": "No credit card statements on this connection",
    "ITEM_LOGIN_REQUIRED": "Reconnect this card (the bank needs a sign-in)",
}


# Answers worth asking again within the day: Plaid is still preparing the
# connection's data, which is usual for an hour or so after connecting.
RETRY = ("PRODUCT_NOT_READY",)


def refresh(db: Session, company: str = "", only_missing: bool = False) -> dict:
    """Read the latest statement for every Plaid-connected card. One call per
    bank login; each card's figures go to its own card account.

    only_missing: just the cards with nothing read yet (or a not-ready answer)
    -- a card connected after the day's read used to wait until the next day,
    showing no statement date or balance in the meantime."""
    from .integrations import plaid_api
    from .models_db import PlaidItemRow
    # CONNECTED CARDS ONLY: a card disconnected on Bank / Card Accounts is not
    # read again (and its statement was cleared when it was disconnected).
    q = db.query(PlaidItemRow).filter(PlaidItemRow.active.is_(True))
    if company:
        q = q.filter(PlaidItemRow.company == company)
    by_token: dict = {}
    for r in q.all():
        # A connection whose account was never chosen is read too (it used to
        # be skipped, and so stayed "not read yet" for good): its card is the
        # one credit account on the login, or the one with its last four.
        if r.access_token and r.source:
            if only_missing:
                have = get(db, r.company, r.source)
                if have and not have.get("retry"):
                    continue
            by_token.setdefault(r.access_token, []).append(r)
    if only_missing and not by_token:
        return {"statements": 0, "without": 0}
    done = noted = 0
    for token, rows in by_token.items():
        try:
            res = plaid_api.get_liabilities(token)
        except plaid_api.PlaidError as e:
            code = getattr(e, "error_code", "") or str(e)
            again = any(k in code for k in RETRY)
            # Consent or a sign-in still owed: asked again by the new-connection
            # read too, so a card whose cardholder has just granted it is read
            # without waiting for the next day.
            again = again or any(k in code for k in ("ADDITIONAL_CONSENT_REQUIRED", "ITEM_LOGIN_REQUIRED"))
            note = ("Plaid is still preparing this card\u2019s statements \u2014 read again within the hour"
                    if any(k in code for k in RETRY) else next((v for k, v in NOTES.items() if k in code), f"Plaid said: {code[:120]}"))
            for r in rows:
                _put(db, r.company, r.source, {"note": note, "retry": again,
                                               "at": datetime.utcnow().isoformat() + "Z"})
                noted += 1
            continue
        credit = {c.get("account_id"): c for c in ((res.get("liabilities") or {}).get("credit") or [])}
        masks = {a.get("account_id"): str(a.get("mask") or "") for a in (res.get("accounts") or [])}
        for r in rows:
            c = credit.get(r.account_id) if r.account_id else None
            if c is None and not r.account_id:
                mine = [v for k, v in credit.items() if getattr(r, "mask", "") and masks.get(k) == str(r.mask)]
                c = mine[0] if len(mine) == 1 else (next(iter(credit.values())) if len(credit) == 1 else None)
            if c and (c.get("last_statement_issue_date") or c.get("last_statement_balance") is not None):
                # EARLIER STATEMENTS, for the hover: Plaid reports only the
                # latest, so each one is kept as the next replaces it (the two
                # before the current one).
                _old = get(db, r.company, r.source)
                _new_date = c.get("last_statement_issue_date") or ""
                _hist = list(_old.get("history") or [])
                if _old.get("date") and _old["date"] != _new_date:
                    _hist = [{"date": _old["date"], "balance": _old.get("balance")}] + _hist
                _put(db, r.company, r.source, {
                    "date": _new_date,
                    "balance": c.get("last_statement_balance"),
                    "history": _hist[:HISTORY],
                    "at": datetime.utcnow().isoformat() + "Z"})
                done += 1
            else:
                _put(db, r.company, r.source, {"note": "No statement reported for this account",
                                               "at": datetime.utcnow().isoformat() + "Z"})
                noted += 1
    from .models_db import AppSettingRow
    if not only_missing:
        db.merge(AppSettingRow(key=CHECKED, value=date.today().isoformat()))
    db.commit()
    return {"statements": done, "without": noted}


def due(db: Session) -> bool:
    from .models_db import AppSettingRow
    if not enabled():
        return False
    r = db.get(AppSettingRow, CHECKED)
    return not (r and r.value == date.today().isoformat())


def refresh_soon(company: str, delay: float = 45.0) -> None:
    """A newly connected card's statement, read shortly after connecting (in
    the background: the account is chosen, and Plaid prepares the data,
    first) rather than at the next day's read."""
    import threading
    import time as _t

    def run():
        _t.sleep(delay)
        from .db import SessionLocal
        try:
            with SessionLocal() as db:
                refresh(db, company, only_missing=True)
        except Exception:                           # noqa: BLE001
            log.exception("statements for a new connection (%s)", company)
    if enabled():
        threading.Thread(target=run, name="stmt-new", daemon=True).start()
