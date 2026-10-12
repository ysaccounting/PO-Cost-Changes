"""
HTTP API. The dashboard (separate frontend) talks to these endpoints; the
scheduled worker calls the same reconciliation path directly.

Endpoints:
  GET  /health                  liveness + which safety gates are set
  POST /reconcile/{company}     run matching for a company, store results
  GET  /results/{company}       the day's results, grouped by decision
  GET  /audit/{company}         the audit trail
  POST /push/{company}          push approved matches (DRY-RUN by default)
"""

from __future__ import annotations

from collections import defaultdict

from datetime import datetime, timedelta, timezone

from . import roles as _roles
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, Query, Request, UploadFile)
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
import re as _re
import time
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from .config import settings
from . import company_vars as _cv
from .db import init_db, get_session
from .models_db import MatchRow, AuditRow, OAuthStateRow
from .persistence import run_reconciliation
from .models_db import UserRow, UploadRow
from .review import CAD_RATE_KEY, cad_rate, is_never_row
from . import auth as _auth
from .auth import (current_user, require_admin, require_reviewer, require_owner,
                   require_write, ROLES, ROLE_USER, ROLE_OWNER)
from .ingestion import ingest_csv, SOURCES, PULLABLE
from .integrations.quickbooks import push_company
from .integrations import qbo_oauth, qbo_tokens

import json
import pathlib

import logging
import hashlib
import os
import urllib.parse as _q
import re
import threading

log = logging.getLogger("ys.api")

app = FastAPI(title="YS Reconciliation Engine", version="0.1.0")

STATIC = pathlib.Path(__file__).parent / "static"



def _ref_by_name(*a, **k):
    """A vendor or account by name from the company's OWN QuickBooks file
    -- see review.ref_by_name. Imported late to avoid an import cycle."""
    from .review import ref_by_name
    return ref_by_name(*a, **k)

@app.get("/", include_in_schema=False)
def index():
    """The reviewer UI. Auth happens client-side against /auth/login.

    Never cached. The whole front end is this one file, so a cached copy means a
    deploy appears to have changed nothing -- the new code is on the server and
    the browser is still running last week's. An ETag alone was not enough:
    revalidation is the browser's choice, and an already-open tab will not make
    it at all.
    """
    # The asset versions are stamped in at REQUEST time, not baked into the
    # file. Written into the markup once, they would be whatever they were when
    # the split happened -- so the next edit to app.js would ship behind a
    # cached copy of the old one, which is the exact failure the no-store
    # header above exists to prevent.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.css", "app.js"):
        f = STATIC / name
        if f.exists():
            v = hashlib.md5(f.read_bytes()).hexdigest()[:8]
            html = re.sub(rf"/static/{re.escape(name)}\?v=[0-9a-f]*",
                          f"/static/{name}?v={v}", html)
    return Response(
        html, media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"})


@app.get("/static/{name}")
def static_asset(name: str):
    """The split-out stylesheet and script.

    Served by an explicit route rather than a mounted directory: these are a
    few known files, and a mount would expose whatever else ever lands in that
    folder. The name is checked against a list rather than sanitized, which is
    the difference between "no traversal today" and "no traversal ever".

    racecar.png is the sign-in artwork. It has to be named here or it 404s --
    which is the whole point of the allow-list, and the reason adding an asset
    is a deliberate act rather than a matter of dropping a file in a directory.

    Cached hard and for a long time. Safe for app.css and app.js because their
    URLs carry a content hash; the image has no hash in its URL, so it is
    served with a shorter, revalidating cache instead. A year-long immutable
    cache on a URL that can change its contents is how a replaced image stays
    replaced for nobody.
    """
    # signin-844.jpg is the sign-in photograph. Added here with the file
    # itself: shipped without this line it 404'd and the panel showed only
    # its gray fill.
    allowed = {"app.css": "text/css", "app.js": "application/javascript",
               "racecar.png": "image/png", "signin-844.jpg": "image/jpeg",
               # The User Guide page and its PDF (tools/build_guide.py).
               "guide.html": "text/html; charset=utf-8",
               # Connectors icons for OneDrive and Google Sheets: the official
               # files from Microsoft's and Google's brand pages, added by hand.
               # Until a file is there the screen shows a plain grid instead.
               "icon-onedrive.png": "image/png", "icon-google-sheets.png": "image/png",
               "user-guide.pdf": "application/pdf"}
    if name not in allowed:
        raise HTTPException(404, "not found")
    hashed = name in ("app.css", "app.js")
    cache = ("public, max-age=31536000, immutable" if hashed
             else "public, max-age=86400, must-revalidate")
    return FileResponse(STATIC / name, media_type=allowed[name],
                        headers={"Cache-Control": cache})


def _seed_profile_emails(db) -> None:
    """Fill the cardholder-email mirror if it is empty -- in the BACKGROUND.

    It is what lets a card's PROFILE NAME identify the buyer on a bill, and it
    only populated during the hourly STH sync, so between a deploy and the next
    sync the email signal was dead and every candidate scored the same.

    On a thread, because it is a paged HTTP fetch over the whole address book.
    Run inline it delayed the startup event, and the server does not answer
    /health until that returns -- so the healthcheck timed out and Railway
    marked a perfectly good deploy as failed. Nothing else waits on this: an
    empty mirror costs match quality, not correctness.
    """
    from .models_db import ProfileEmailRow
    from .worker import airtable_companies
    # EACH COMPANY'S OWN, for each company with broker data whose address book
    # is empty -- which, after the address book became per company, is every
    # one of them once. It used to fill one shared list, under no company.
    try:
        have = {c for (c,) in db.query(ProfileEmailRow.company).distinct().all()}
        todo = [c for c in airtable_companies(db) if c not in have]
    except Exception:                           # noqa: BLE001
        return                                  # table not there yet; the daily job will
    if not todo:
        return

    def _fill() -> None:
        from .worker import sync_profile_emails_all
        from .review import reset_email_cache
        for co in todo:
            try:
                sync_profile_emails_all(company=co)
            except Exception as e:              # noqa: BLE001
                log.warning("could not seed %s's cardholder emails: %s", co, e)
        reset_email_cache()
        log.info("seeded the address books of %d company(ies) in the background", len(todo))

    import threading
    threading.Thread(target=_fill, name="seed-emails", daemon=True).start()


def rescope_coded_ids_once(db) -> int:
    """Re-resolve coded vendor and category ids from each company's OWN file.

    Codings used to look vendors and accounts up by name across every company's
    QuickBooks file, so a charge could carry another file's id -- which here
    meant something else (a deleted "Philadelphia Flyers (DEP)" in place of
    Buffalo Sabres). Every charge not yet in QuickBooks is re-resolved by its
    names against its own file. Once, remembered.
    """
    from .models_db import AppSettingRow, ChargeRow, CompanyRow, QboRefRow
    key = "coded_ids_rescoped_v1"
    if db.get(AppSettingRow, key) is not None:
        return 0
    realms = {c.name: c.realm_id for c in db.query(CompanyRow).all() if c.realm_id}
    ids: dict = {}
    for r in db.query(QboRefRow).filter(QboRefRow.kind.in_(("vendor", "account"))):
        k = (r.realm_id, r.kind, r.name)
        if k not in ids or (r.active and not ids[k][1]):
            ids[k] = (r.qbo_id, bool(r.active))
    fixed = 0
    q = db.query(ChargeRow).filter(
        (ChargeRow.qbo_txn_id.is_(None)) | (ChargeRow.qbo_txn_id == ""))
    for ch in q.yield_per(2000):
        realm = realms.get(ch.company)
        if not realm:
            continue
        for kind, name_f, id_f in (("vendor", "coded_vendor", "coded_vendor_id"),
                                   ("account", "coded_category", "coded_category_id")):
            name = (getattr(ch, name_f, "") or "").strip()
            if not name:
                continue
            right = ids.get((realm, kind, name), (None,))[0]
            if right and getattr(ch, id_f, "") != right:
                setattr(ch, id_f, right)
                fixed += 1
    db.merge(AppSettingRow(key=key, value=f"done: {fixed}"))
    db.commit()
    if fixed:
        log.warning("re-resolved %d coded id(s) from each company's own file", fixed)
    return fixed


def reread_plaid_once(db) -> int:
    """Re-read every Plaid connection's history ONCE, after the one-account fix.

    Pulls used to import every account on a bank login into the connected card.
    Re-reading from the start lets the corrected pull keep only the right
    account's transactions and take the leaked ones back out of For Review.
    Remembered, so it happens once and not at every start.
    """
    from .models_db import AppSettingRow, PlaidItemRow
    key = "plaid_one_account_reread_v1"
    if db.get(AppSettingRow, key) is not None:
        return 0
    n = 0
    for it in db.query(PlaidItemRow).all():
        it.cursor = ""
        n += 1
    db.merge(AppSettingRow(key=key, value="done"))
    db.commit()
    if n:
        log.warning("Plaid: %d connection(s) will re-read their history once", n)
    return n


def mark_interrupted_syncs(db) -> int:
    """At startup: any STH or address book sync still marked RUNNING died
    with the engine's previous run (a deploy or restart). Said so plainly,
    instead of leaving the screen to report "interrupted" with no reason."""
    from .models_db import SyncStatusRow
    n = 0
    for r in db.query(SyncStatusRow).filter(SyncStatusRow.state == "running").all():
        if (r.name or "").startswith(("hal:", "emails:")):
            r.state = "interrupted"
            r.detail = ("The engine restarted while this sync was running "
                        "(a deploy, usually). It starts again on its own.")
            r.finished_at = datetime.utcnow()
            n += 1
    if n:
        db.commit()
    return n


def repair_negative_amounts(db) -> int:
    """Correct charges stored as NEGATIVE amounts. Safe to repeat.

    The importer always stores the size of an amount and keeps charge-or-refund
    in is_credit, so a negative amount can only have come from the Plaid update
    path, which wrote Plaid's value negated straight in -- and it only did that
    to charges (money out). Each is set back to its size, as a charge.
    """
    from .models_db import ChargeRow
    fixed = 0
    for r in db.query(ChargeRow).filter(ChargeRow.amount < 0).all():
        r.amount = abs(r.amount)
        r.is_credit = False
        fixed += 1
    if fixed:
        db.commit()
        log.warning("corrected %d charge(s) stored as negative amounts", fixed)
    return fixed


def repair_card_account_files(db) -> int:
    """Re-read card accounts that were named from ANOTHER company's QuickBooks
    file. Safe to run any number of times; returns how many it corrected.

    QuickBooks numbers accounts per file, so an id like 874 exists in several.
    Routing used to look the id up without checking whose file it was in, and
    saved a Divvy card account as "Prepaid Inventory:NFL:Washington Commanders
    (TC)" -- the other company's account. Listed under that name, it could not
    be found as "Divvy (WH)" in either card picker.

    A migration does the same repair once; this runs at every start as well,
    so it does not depend on that migration having been deployed, and it also
    catches a card account created with the bad name AFTER the migration ran.
    A nickname somebody typed is left alone -- only a name copied from the
    wrong account is replaced.
    """
    from .models_db import CompanyRow, QboRefRow, SourceAccountRow
    fixed = 0
    realms = {c.name: c.realm_id for c in db.query(CompanyRow).all()
              if c.realm_id}
    for r in db.query(SourceAccountRow).filter(
            SourceAccountRow.qbo_account_id.isnot(None)).all():
        want = realms.get(r.company)
        if not want or not r.qbo_account_id or (r.realm_id or "") == want:
            continue
        ref = db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.realm_id == want,
            QboRefRow.qbo_id == r.qbo_account_id).first()
        if ref is None:
            continue
        right = ref.name or r.source
        if (r.nickname or "") in ("", r.qbo_account_name or "", r.source):
            r.nickname = right
        r.qbo_account_name = right
        r.realm_id = want
        fixed += 1
    # A NICKNAME LEFT BEHIND. An account whose file and QuickBooks name were
    # put right (above, or by an earlier repair), or that was pointed at a
    # different QuickBooks account, could keep a nickname copied from the
    # WRONG account -- "FFB Chkg" listed everywhere as "Inventory Asset", its
    # charges filed under that name and the account itself missing from the
    # card picker. A nickname that is exactly the name of a DIFFERENT
    # QuickBooks account was copied, not typed: it becomes the account's own.
    all_names = {}
    for ref in db.query(QboRefRow).filter(QboRefRow.kind == "account").all():
        all_names.setdefault((ref.name or "").strip().lower(), set()).add(
            (ref.realm_id or "", ref.qbo_id or ""))
    renamed = 0
    for r in db.query(SourceAccountRow).filter(
            SourceAccountRow.qbo_account_id.isnot(None)).all():
        realm = r.realm_id or realms.get(r.company) or ""
        if not r.qbo_account_id or not realm:
            continue
        own = db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.realm_id == realm,
            QboRefRow.qbo_id == r.qbo_account_id).first()
        if own is None or not own.name:
            continue
        nick = (r.nickname or "").strip()
        others = all_names.get(nick.lower(), set()) - {(realm, r.qbo_account_id)}
        if nick and nick.lower() != own.name.strip().lower() and others:
            log.warning("card account %s:%s nickname %r was another account's name; now %r",
                        r.company, r.source, nick, own.name)
            r.nickname = own.name
            renamed += 1
        if (r.qbo_account_name or "") != own.name:
            r.qbo_account_name = own.name
            renamed += 1
    fixed += renamed
    if fixed:
        db.commit()
        log.warning("corrected %d card account name(s)", fixed)
    return fixed


@app.on_event("startup")
def _startup():
    init_db()
    from .db import SessionLocal
    db = SessionLocal()
    try:
        _auth.ensure_seed_admin(db)
        _seed_profile_emails(db)
        try:
            repair_card_account_files(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not check card accounts' QuickBooks files")
            db.rollback()
        try:
            from .company_vars import encrypt_plain_secrets as _eps
            _eps(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not encrypt stored values")
            db.rollback()
        # AFTER A JWT_SECRET CHANGE: keys stored under the previous secret are
        # re-encrypted under the new one (JWT_SECRET_PREVIOUS set).
        try:
            from .secrets_store import reencrypt_all as _rea
            _rea(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not re-encrypt stored keys")
            db.rollback()
        try:
            repair_negative_amounts(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not check for negative charge amounts")
            db.rollback()
        try:
            mark_interrupted_syncs(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not mark interrupted syncs")
            db.rollback()
        try:
            reread_plaid_once(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not schedule the Plaid re-read")
            db.rollback()
        try:
            rescope_coded_ids_once(db)
        except Exception:                       # noqa: BLE001
            log.exception("could not re-resolve coded ids")
            db.rollback()
    finally:
        db.close()
    # The scheduled syncs. They used to depend on Railway cron services created
    # by hand, so a deployment that skipped that step never synced at all and
    # said nothing about it.
    try:
        from . import scheduler
        scheduler.start()
    except Exception:                           # noqa: BLE001
        log.exception("could not start the scheduler")


# --- auth -------------------------------------------------------------------

def _issue_twofa(db: Session, user: UserRow) -> None:
    """Email a fresh sign-in code, invalidating any earlier one."""
    from .models_db import AuthTokenRow
    from . import mailer
    db.query(AuthTokenRow).filter(
        AuthTokenRow.username == user.username,
        AuthTokenRow.kind == "twofa",
        AuthTokenRow.used_at.is_(None)).delete(synchronize_session=False)
    code = _auth.new_code()
    db.add(AuthTokenRow(
        username=user.username, kind="twofa",
        token_hash=_auth.hash_token(code),
        expires_at=datetime.utcnow() + timedelta(minutes=settings.twofa_code_minutes)))
    db.commit()
    target = user.email or user.username
    mailer.send_twofa(target, code, settings.twofa_code_minutes)


def _device_trusted(db: Session, user: UserRow, device: str) -> bool:
    from .models_db import TrustedDeviceRow
    if not device:
        return False
    row = db.get(TrustedDeviceRow, _auth.hash_token(device))
    return bool(row and row.username == user.username
                and row.expires_at > datetime.utcnow())


@app.post("/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(),
          device: str = Form(""),
          db: Session = Depends(get_session)):
    """Password check, then 2FA unless this browser was trusted recently."""
    try:
        user = _auth.authenticate(db, form.username, form.password)
    except _auth.LockedOut as e:
        # 429, not 401: the password may well have been right. Saying so, with
        # the wait, is the difference between "try again shortly" and somebody
        # assuming the account is broken.
        raise HTTPException(429, f"Too many failed attempts. Try again in "
                                 f"{e.minutes} minute(s).")
    if user is None:
        raise HTTPException(401, "incorrect username or password")

    needs_2fa = (settings.require_twofa and user.twofa_enabled
                 and not _device_trusted(db, user, device))
    if needs_2fa:
        _issue_twofa(db, user)
        from . import mailer
        return {"twofa_required": True, "username": user.username,
                "email_sent": mailer.is_configured(),
                "note": ("a code was emailed to you" if mailer.is_configured()
                         else "email isn't configured — the code is in the "
                              "server logs")}

    return {"access_token": _auth.create_token(user.username, user.role),
            "token_type": "bearer", "role": user.role,
            "idle_minutes": _roles.idle_minutes()}


@app.post("/auth/verify-2fa")
def verify_twofa(username: str = Form(...), code: str = Form(...),
                 trust_device: bool = Form(True),
                 db: Session = Depends(get_session)):
    """Exchange an emailed code for a session."""
    from .models_db import AuthTokenRow, TrustedDeviceRow
    import secrets as _s

    username = username.lower().strip()
    row = db.query(AuthTokenRow).filter(
        AuthTokenRow.username == username, AuthTokenRow.kind == "twofa",
        AuthTokenRow.used_at.is_(None)).order_by(
        AuthTokenRow.created_at.desc()).first()

    if row is None or row.expires_at <= datetime.utcnow():
        raise HTTPException(400, "that code has expired — sign in again")
    # Rate-limit guessing: six digits is only 10^6, so unlimited tries is weak.
    if row.attempts >= 5:
        raise HTTPException(429, "too many attempts — sign in again")
    row.attempts += 1
    db.commit()

    if row.token_hash != _auth.hash_token(code.strip()):
        raise HTTPException(400, "that code isn't right")

    row.used_at = datetime.utcnow()
    user = db.get(UserRow, username)
    if user is None or not user.is_active:
        raise HTTPException(401, "account unavailable")

    device_token = ""
    if trust_device:
        device_token = _s.token_urlsafe(32)
        db.add(TrustedDeviceRow(
            id=_auth.hash_token(device_token), username=username,
            expires_at=datetime.utcnow() + timedelta(hours=settings.twofa_trust_hours)))
    db.commit()

    return {"access_token": _auth.create_token(user.username, user.role),
            "token_type": "bearer", "role": user.role,
            "device": device_token,
            "trust_hours": settings.twofa_trust_hours,
            "idle_minutes": _roles.idle_minutes()}


@app.post("/auth/profile")
def update_profile(full_name: str = Form(""), current_password: str = Form(""),
                   new_password: str = Form(""), db: Session = Depends(get_session),
                   user: UserRow = Depends(_auth.current_user)):
    """PROFILE: a person's own name, and their password -- which needs the
    current one, so an unattended browser cannot be used to change it."""
    row = db.get(UserRow, user.username)
    changed = []
    if full_name.strip() != (row.full_name or ""):
        row.full_name = full_name.strip()[:120]
        changed.append("name")
    if new_password:
        if not _auth.verify_password(current_password, row.password_hash or ""):
            raise HTTPException(400, "Your current password isn't right")
        if len(new_password) < 12:
            raise HTTPException(400, "The new password must be at least 12 characters")
        if new_password == current_password:
            raise HTTPException(400, "The new password must be different from the current one")
        row.password_hash = _auth.hash_password(new_password)
        changed.append("password")
    if changed:
        db.add(AuditRow(company="", action="setting_saved", detail=f"{user.username} updated their {' and '.join(changed)}",
                        dry_run=False, actor=user.username))
        db.commit()
    return {"ok": True, "changed": changed, "full_name": row.full_name or ""}


@app.post("/auth/forgot")
def forgot_password(username: str = Form(...),
                    db: Session = Depends(get_session)):
    """Email a reset link.

    Always reports success. Saying "no such account" would let anyone probe
    which email addresses exist.
    """
    from .models_db import AuthTokenRow
    from . import mailer
    import secrets as _s

    username = username.lower().strip()
    user = db.get(UserRow, username)
    if user is None:
        user = db.query(UserRow).filter(UserRow.email == username).first()
    if user is not None and user.is_active:
        username = user.username
        token = _s.token_urlsafe(32)
        db.add(AuthTokenRow(
            username=username, kind="reset",
            token_hash=_auth.hash_token(token),
            expires_at=datetime.utcnow() + timedelta(minutes=settings.reset_token_minutes)))
        db.commit()
        base = settings.app_base_url.rstrip("/") or ""
        link = f"{base}/?reset={token}&u={username}"
        mailer.send_reset(user.email or user.username, link,
                          settings.reset_token_minutes)
    return {"sent": True,
            "note": "if that account exists, a reset link has been emailed"}


@app.post("/auth/reset")
def reset_password(username: str = Form(...), token: str = Form(...),
                   new_password: str = Form(...),
                   db: Session = Depends(get_session)):
    from .models_db import AuthTokenRow, TrustedDeviceRow
    username = username.lower().strip()
    if len(new_password) < 12:
        raise HTTPException(400, "password must be at least 12 characters")

    row = db.query(AuthTokenRow).filter(
        AuthTokenRow.username == username, AuthTokenRow.kind == "reset",
        AuthTokenRow.token_hash == _auth.hash_token(token),
        AuthTokenRow.used_at.is_(None)).first()
    if row is None or row.expires_at <= datetime.utcnow():
        raise HTTPException(400, "that reset link has expired or been used")

    user = db.get(UserRow, username)
    if user is None:
        raise HTTPException(404, "no such account")
    user.password_hash = _auth.hash_password(new_password)
    row.used_at = datetime.utcnow()
    # A reset usually means "someone else may have had access" -- drop trusted
    # devices so 2FA is required again everywhere.
    db.query(TrustedDeviceRow).filter(
        TrustedDeviceRow.username == username).delete(synchronize_session=False)
    db.commit()
    return {"reset": True}


@app.get("/auth/me")
def me(user: UserRow = Depends(current_user)):
    """Also refreshes the session — the client calls this periodically, which
    is what makes the 30-minute window slide with activity."""
    # Deliberately no email here: it's set by an admin when inviting and is
    # never shown to or edited by the person themselves.
    return {"username": user.username, "full_name": user.full_name,
            "role": user.role, "last_login": user.last_login,
            "access_token": _auth.refreshed_token(user),
            "idle_minutes": _roles.idle_minutes()}


@app.post("/auth/users")
def create_user(username: str = Form(...), email: str = Form(...),
                password: str = Form(""),
                full_name: str = Form(""), role: str = Form(ROLE_USER),
                send_invite: bool = Form(True),
                # WHICH COMPANIES, chosen at the same moment as the account.
                # An account with no companies can sign in and see nothing,
                # which looks like a broken invite rather than an unfinished
                # one -- and the person who set it up has already moved on.
                companies: str = Form(""),
                company_role: str = Form("user"),
                db: Session = Depends(get_session),
                admin: UserRow = Depends(require_admin)):
    """Add a user. The username IS their email address.

    With no password given, a random one is set and an invite emailed — so an
    admin never handles someone else's password.
    """
    from .models_db import AuthTokenRow
    from . import mailer
    import secrets as _s

    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {sorted(ROLES)}")
    username = username.lower().strip()
    email = email.lower().strip()
    if not username:
        raise HTTPException(400, "username is required")
    if not _auth.valid_email(email):
        raise HTTPException(
            400, "a valid email address is required — it's where sign-in codes "
                 "and password resets are sent")
    if db.get(UserRow, username):
        raise HTTPException(409, "that username is taken")
    if db.query(UserRow).filter(UserRow.email == email).first():
        raise HTTPException(409, "that email is already on another account")
    if password and len(password) < 12:
        raise HTTPException(400, "password must be at least 12 characters")

    pw = password or _s.token_urlsafe(24)
    db.add(UserRow(username=username, email=email, full_name=full_name.strip(),
                   password_hash=_auth.hash_password(pw), role=role))
    db.commit()

    invited = False
    if send_invite:
        token = _s.token_urlsafe(32)
        db.add(AuthTokenRow(
            username=username, kind="reset",
            token_hash=_auth.hash_token(token),
            expires_at=datetime.utcnow() + timedelta(hours=48)))
        db.commit()
        base = settings.app_base_url.rstrip("/") or ""
        invited = mailer.send(
            email, "You've been added to Y&S Reconciliation",
            f"{admin.username} set up an account for you.\n\n"
            f"Choose your password here (valid 48 hours):\n"
            f"{base}/?reset={token}&u={username}\n")

    # Membership, granted here rather than as a second job afterwards.
    granted = []
    if role != ROLE_OWNER:
        from datetime import datetime as _dt
        from .models_db import CompanyRow as _CR, UserCompanyRow as _UC
        from . import membership as _mb
        mine = _mb.companies_for(db, admin)
        want = [x.strip() for x in (companies or "").split(",") if x.strip()]
        crole = company_role if company_role in ("admin", "user", "viewer") else "user"
        for co in want:
            if not db.query(_CR).filter(_CR.name == co).first():
                continue
            # An admin cannot hand out access to a company they are not in
            # themselves -- otherwise inviting is a way around the membership
            # check rather than a use of it.
            if mine is not None and co not in mine:
                raise HTTPException(
                    403, f"you are not a member of {co}, so you cannot add "
                         f"somebody to it")
            db.merge(_UC(id=f"{username}|{co}", username=username, company=co,
                         role=crole, invited_by=admin.username,
                         created_at=_dt.utcnow()))
            granted.append(co)
        if granted:
            db.commit()

    return {"created": username, "role": role, "by": admin.username,
            "invite_emailed": invited, "companies": granted,
            "password_set_manually": bool(password)}


@app.get("/auth/users")
def list_users(db: Session = Depends(get_session),
               admin: UserRow = Depends(require_admin)):
    return [{"username": u.username, "email": u.email, "role": u.role,
             "active": u.is_active, "last_login": u.last_login}
            for u in db.query(UserRow).all()]


@app.post("/auth/users/{username}/deactivate")
def deactivate_user(username: str, db: Session = Depends(get_session),
                    admin: UserRow = Depends(require_admin)):
    u = db.get(UserRow, username.lower().strip())
    if u is None:
        raise HTTPException(404, "no such user")
    if u.username == admin.username:
        raise HTTPException(400, "cannot deactivate yourself")
    u.is_active = False
    db.commit()
    return {"deactivated": u.username}


# --- charge ingestion -------------------------------------------------------

@app.post("/charges/upload")
async def upload_charges(source: str = Form(...), company: str = Form(...),
                         date_start: str = Form(""), date_end: str = Form(""),
                         file: UploadFile = File(...),
                         override_name_check: bool = Form(False),
                         preflight: bool = Form(False),
                         amount_direction: str = Form(""),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_write)):
    """Upload a card-portal CSV export. Idempotent: re-uploads are deduped."""
    # Registered card accounts, not a hardcoded three. The layout comes from
    # the account's file_format, so a program can be called anything.
    from .models_db import SourceAccountRow as _SA
    acct = db.query(_SA).filter(_SA.company == company,
                                _SA.source == source.lower()).first()
    # An account listed straight from QuickBooks has no settings row until
    # somebody changes something about it, and is keyed "qbo:<id>" until then.
    # Uploading IS the first thing somebody does with a new account, so the row
    # is created here the same way every other setting creates it -- otherwise
    # the account is offered in the dropdown and then refused as "not a card
    # account", which is the account QuickBooks itself gave us.
    if acct is None and source.lower().startswith("qbo:"):
        acct = _row_for_source(db, company, source)
        if acct is not None:
            db.commit()
    if acct is None and source.lower() not in SOURCES:
        raise HTTPException(
            400, f"'{source}' is not a card account for {company}. "
                 f"Add it under Cards first.")
    # Said plainly rather than accepted quietly: rows filed against a retired
    # account are rows nobody is looking at, and a silent success is the worst
    # possible answer to "why aren't my transactions showing up".
    if acct is not None and not acct.is_active:
        raise HTTPException(
            400, f"'{acct.nickname or source}' is no longer in use. Its history "
                 f"is still here, but it can't take new transactions — "
                 f"re-activate it under Card Accounts if this is right.")
    # RETIRED IN QUICKBOOKS: refused here too -- its charges could never be
    # posted ("no longer active" on Add).
    if acct is not None and getattr(acct, "qbo_account_id", None):
        from .models_db import QboRefRow as _QRu
        _q = db.query(_QRu).filter(_QRu.kind == "account", _QRu.qbo_id == acct.qbo_account_id,
                                   _eq_or_none(_QRu.realm_id, __import__("app.review", fromlist=["x"])._company_realm(db, company))).first()
        if _q is not None and _q.active is False:
            raise HTTPException(
                400, f"'{acct.nickname or acct.qbo_account_name or source}' is retired in QuickBooks, so its "
                     f"transactions can't be posted. Restore it on Bank / Card Accounts first.")
    # Only an explicitly configured format is passed through; otherwise the
    # layout is read from the file's own columns. Falling back to the source
    # name here is what made "amexcredit" look like a file format.
    _fmt = (acct.file_format if acct else "") or ""

    # The company must be one the admin registered. A typed name that differs
    # by so much as a space would file the charges where nothing can find them.
    from .models_db import CompanyRow
    known = db.query(CompanyRow).filter(CompanyRow.name == company).first()
    if known is None:
        registered = [c.name for c in db.query(CompanyRow).filter(
            CompanyRow.active.is_(True)).all()]
        raise HTTPException(
            400,
            f"'{company}' is not a registered company. "
            f"Pick one of: {registered or 'none set up yet — an admin must add one'}")

    content = await file.read()
    # Excel in, CSV out. The card portals hand people .xlsx as often as .csv,
    # and asking somebody to re-save it is a step that only exists because the
    # parser could not read it.
    if (file.filename or "").lower().endswith((".xlsx", ".xlsm", ".xls")):
        try:
            import io as _io
            import csv as _csv
            from openpyxl import load_workbook
            wb = load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            buf = _io.StringIO()
            w = _csv.writer(buf)
            for r in ws.iter_rows(values_only=True):
                w.writerow(["" if c is None else str(c) for c in r])
            wb.close()
            content = buf.getvalue().encode("utf-8")
        except Exception as e:                  # noqa: BLE001
            raise HTTPException(
                400, f"Could not read that Excel file: {e}. "
                     f"Saving it as CSV will also work.")
    try:
        from datetime import date as _d
        def _parse(v, label):
            if not v:
                return None
            try:
                return _d.fromisoformat(v)
            except ValueError:
                raise HTTPException(400, f"{label} is not a date (YYYY-MM-DD)")
        ds, de = _parse(date_start, "Start date"), _parse(date_end, "End date")
        # OPTIONAL, either or both: none takes the whole file; one bounds that
        # side only.
        if ds and de and ds > de:
            raise HTTPException(400, "The start date is after the end date")
        result = ingest_csv(db, source, content, company,
                            filename=file.filename or "", actor=user.username,
                            override_name_check=override_name_check,
                            preflight=preflight, file_format=_fmt,
                            date_start=ds, date_end=de,
                            amount_direction=amount_direction)
    except ValueError as e:
        # A QUESTION, not a refusal: which way a file's amounts go. Asked on
        # the upload screen, then the file is sent again with the answer.
        from .ingestion import NeedsDirection
        if isinstance(e, NeedsDirection):
            raise HTTPException(409, str(e))
        # Record rejected uploads too -- a file that was refused is exactly the
        # kind of thing someone asks about later.
        from .models_db import AuditRow
        db.add(AuditRow(
            company=company, action="upload_rejected",
            detail=f"{file.filename or '(no name)'} as {source}: {e}",
            dry_run=True, actor=user.username))
        db.commit()
        raise HTTPException(400, str(e))

    if preflight:
        return result           # nothing imported, nothing to record

    from .models_db import AuditRow
    db.add(AuditRow(
        company=company, action="charges_uploaded",
        detail=(f"{result['filename'] or '(no name)'} as {source}: "
                f"{result['new']} new, {result['duplicates']} already loaded, "
                f"{result['skipped']} skipped, of {result['rows_seen']} rows"),
        dry_run=True, actor=user.username))
    db.commit()
    return result


@app.get("/charges/uploads")
def upload_history(company: str | None = None,
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    q = db.query(UploadRow).order_by(UploadRow.at.desc())
    if company:
        q = q.filter(UploadRow.company == company)
    return [{"at": u.at, "source": u.source, "method": u.method,
             "file": u.filename, "company": u.company, "seen": u.rows_seen,
             "rescored": _rescore_after_upload(db, company),
             "new": u.rows_new, "duplicate": u.rows_duplicate,
             "by": u.actor} for u in q.limit(50).all()]


@app.get("/charges/sources")
def charge_sources(user: UserRow = Depends(require_reviewer)):
    return {"sources": list(SOURCES), "api_pull_supported": sorted(PULLABLE),
            "upload_only": sorted(set(SOURCES) - PULLABLE)}


SLOW_REQUEST_MS = 1500


@app.middleware("http")
async def download_done_cookie(request, call_next):
    """AN EXPORT'S SPINNER STOPS WHEN ITS FILE ARRIVES. Exports download from
    a link (so the browser saves them straight away), which leaves the page no
    way to know when the file is ready -- the Excel icon stopped turning. The
    link carries a token (dlt); the file's response sets a cookie holding it,
    which the page watches for."""
    response = await call_next(request)
    tok = request.query_params.get("dlt", "")
    if tok and response.status_code < 400 and _re.fullmatch(r"[A-Za-z0-9]{6,40}", tok):
        response.headers.append("set-cookie", f"engine_dl={tok}; Max-Age=300; Path=/; SameSite=Lax")
    return response


@app.middleware("http")
async def enforce_company_membership(request, call_next):
    """Refuse a request naming a company this person is not a member of.

    Central on purpose -- see app/membership.py. The alternative is a check on
    fifty endpoints, which is fifty chances to miss one and no protection at
    all for the fifty-first.

    Unauthenticated requests fall straight through: they are the sign-in and
    static routes, and refusing them here would be an authorization answer to
    an authentication question.
    """
    from . import membership as _mb

    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    # A DOWNLOAD PASS signs a download in too (export links carry it in the
    # address): an export is checked against its company like anything else.
    if not token and request.method == "GET" and request.url.path.startswith(_auth.DOWNLOAD_PATHS):
        token = request.query_params.get("dl", "")
    if not token:
        return await call_next(request)

    query = dict(request.query_params)
    form: dict = {}
    body = b""
    ctype = request.headers.get("content-type", "")
    if request.method in ("POST", "PUT", "PATCH") and (
            "form-" in ctype or "x-www-form-urlencoded" in ctype):
        # The body is READ AND PUT BACK. Consuming it here without replaying it
        # would leave every form endpoint receiving an empty request, which is
        # the sort of breakage that looks like the endpoint's fault.
        body = await request.body()
        try:
            from urllib.parse import parse_qs
            if "x-www-form-urlencoded" in ctype:
                form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            else:
                # multipart: pull out just the company field rather than
                # parsing the whole thing, which may carry an uploaded file.
                m = re.search(rb'name="company"\r?\n\r?\n([^\r\n]*)', body)
                if m:
                    form = {"company": m.group(1).decode("utf-8", "replace")}
        except Exception:                                       # noqa: BLE001
            form = {}

        async def _replay():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _replay

    from .db import SessionLocal
    db = SessionLocal()
    try:
        company = (_mb.company_of_route(db, request.scope)
                   or _mb.company_of(request.url.path, query, form))
    except Exception:                                           # noqa: BLE001
        company = _mb.company_of(request.url.path, query, form)
    finally:
        db.close()
    if company:
        db = SessionLocal()
        try:
            user = _auth.user_from_token(db, token)
            if user is not None and not _mb.may_reach(db, user, company):
                from starlette.responses import JSONResponse
                log.warning("blocked %s %s: %s is not a member of %r",
                            request.method, request.url.path,
                            user.username, company)
                return JSONResponse(
                    {"detail": f"you do not have access to {company}"},
                    status_code=403)
        except Exception:                                       # noqa: BLE001
            # A token this middleware cannot read is not this middleware's
            # problem -- the endpoint's own auth dependency will reject it with
            # the right status and message.
            pass
        finally:
            db.close()

    return await call_next(request)


@app.middleware("http")
async def enforce_custom_roles(request, call_next):
    """USER MANAGEMENT > ROLES: an ability switched off for a person's role is
    refused here, for every request that needs it -- central, like company
    membership above, so no screen or endpoint can miss it."""
    need = _roles.ability_of(request.method, request.url.path)
    if not need:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token and request.method == "GET" and request.url.path.startswith(_auth.DOWNLOAD_PATHS):
        token = request.query_params.get("dl", "")
    if token:
        from .db import SessionLocal
        db = SessionLocal()
        try:
            user = _auth.user_from_token(db, token)
            if user is not None and need in _roles.denied_for(db, user.username):
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": f"your role does not allow {_roles.label(need)}"},
                                    status_code=403)
        except Exception:                                       # noqa: BLE001
            pass
        finally:
            db.close()
    return await call_next(request)


@app.middleware("http")
async def log_slow_requests(request, call_next):
    """Log anything slower than SLOW_REQUEST_MS, with a timing header always.

    The threshold that matters for this app is queue latency creeping past
    about 1.5s. Without this you only learn about it when somebody complains,
    by which point it has been getting worse for weeks.
    """
    import time as _time
    started = _time.perf_counter()
    response = await call_next(request)
    ms = (_time.perf_counter() - started) * 1000
    response.headers["X-Response-Time-ms"] = f"{ms:.0f}"
    if ms >= SLOW_REQUEST_MS:
        log.warning("SLOW %s %s -> %d in %.0fms", request.method,
                    request.url.path, response.status_code, ms)
    return response


# SECURITY HEADERS on every response. No other site may frame the engine
# (clickjacking); browsers load scripts, frames and connections only from the
# engine itself and Plaid (whose Link window connects cards); files are not
# second-guessed as another type; and the address isn't passed to other sites.
_SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.plaid.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; font-src 'self' data:; "
        "connect-src 'self' https://*.plaid.com; "
        "frame-src https://cdn.plaid.com https://*.plaid.com; "
        "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"),
}


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    for k, v in _SEC_HEADERS.items():
        response.headers.setdefault(k, v)
    # HTTPS only once on HTTPS (Railway terminates TLS and says so).
    if request.headers.get("x-forwarded-proto", request.url.scheme) == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def strict_transport(request, call_next):
    """HSTS: once a browser has reached the engine over https, it uses https
    for it from then on. Sent only over https (directly or behind the host's
    proxy), as the standard requires."""
    response = await call_next(request)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.middleware("http")
async def export_quick_search(request, call_next):
    """An export carries its grid's quick search as ?qs= (see export_xlsx.QS)."""
    qs = request.query_params.get("qs", "") if request.url.path.startswith(
        ("/export/", "/teams/plans/export")) else ""
    if not qs:
        return await call_next(request)
    from .export_xlsx import QS as _QS
    tok = _QS.set(qs)
    try:
        return await call_next(request)
    finally:
        _QS.reset(tok)


@app.middleware("http")
async def sliding_session(request, call_next):
    """Extend the session on any authenticated request.

    Relying on a timer to call /auth/me meant a throttled background tab could
    miss its refresh and the token would expire mid-session -- which looked
    like being signed out at random while working.
    """
    response = await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and response.status_code < 400:
        import jwt as _jwt
        from jwt import PyJWTError as JWTError
        try:
            payload = _jwt.decode(auth[7:], _auth._secret(),
                                  algorithms=[_auth.ALGORITHM])
            if payload.get("scope") == "download":
                return response          # a download pass is never renewed
            fresh = _auth.create_token(payload["sub"], payload.get("role", ""))
            response.headers["X-Refresh-Token"] = fresh
            response.headers["X-Download-Token"] = _auth.create_download_token(
                payload["sub"], payload.get("role", ""))
        except (JWTError, KeyError, Exception):
            pass
    return response


@app.exception_handler(Exception)
async def unhandled(request, exc):
    """Return the actual error instead of a bare 500.

    A generic "Internal Server Error" in the UI means digging through deploy
    logs to learn anything. The type and message are enough to act on and
    contain no secrets; the traceback stays in the logs.
    """
    from fastapi.responses import JSONResponse
    import logging as _log
    _log.getLogger(__name__).exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"[:500],
                 "path": str(request.url.path)})


@app.get("/health")
def health():
    # ui_build is a hash of the front-end file. "The deploy did not take" and
    # "my browser is showing an old copy" look identical from the outside;
    # comparing this with the value the page reports tells them apart.
    try:
        import hashlib
        # ALL THREE static files, not just index.html. The markup is now a
        # tenth of the front end; hashing it alone would report a matching
        # ui_build while the script it loads was from another deploy.
        _h = hashlib.md5()
        for _n in ("index.html", "app.css", "app.js"):
            _f = STATIC / _n
            if _f.exists():
                _h.update(_n.encode())
                _h.update(_f.read_bytes())
        ui = _h.hexdigest()[:8]
    except Exception:                       # noqa: BLE001
        ui = "unknown"
    # A hash of every PYTHON file, not just the front end.
    #
    # ui_build only ever proved index.html shipped. Deploying a subset of the
    # files -- which is what happens when changes are copied folder by folder --
    # leaves a front end from one point in time calling a backend from another,
    # and ui_build reports success. The failure then shows up as a button that
    # 404s rather than as a deploy that visibly did not take.
    #
    # Sorted by path so the value depends on the contents and not on the order
    # the filesystem hands them back.
    try:
        import hashlib
        # Only the application: app/, engine/ and the migrations. NOT the whole
        # tree -- a scratch script sitting in the root would change the value
        # without changing a line of what runs, and a verification number that
        # moves for irrelevant reasons stops being checked.
        root = pathlib.Path(__file__).parent.parent
        h = hashlib.md5()
        for sub in ("app", "engine", "migrations"):
            for f in sorted((root / sub).rglob("*.py")):
                if "__pycache__" in f.parts:
                    continue
                h.update(str(f.relative_to(root)).encode())
                h.update(f.read_bytes())
        code = h.hexdigest()[:8]
    except Exception:                       # noqa: BLE001
        code = "unknown"
    return {
        # THE ACTUAL FILES THIS SERVER WOULD SEND, so "is my browser stale or
        # is the deployment stale?" can be answered without guessing. Compare
        # these with what the page asks for (view source) -- if they differ,
        # the browser is holding an old copy; if they differ from the build you
        # deployed, the deployment is the old one.
        "assets": {
            n: __import__("hashlib").md5(
                (STATIC / n).read_bytes()).hexdigest()[:8]
            for n in ("app.js", "app.css", "index.html")
            if (STATIC / n).exists()
        },
        
        "status": "ok",
        "env": settings.app_env,
        "dry_run": settings.dry_run,
        "qbo_write_enabled": settings.qbo_write_enabled,
        "safe": settings.dry_run or not settings.qbo_write_enabled,
        "ui_build": ui,
        # Both must match what a package reports, or the deploy was partial.
        "code_build": code,
    }


# --- learned rules ----------------------------------------------------------

def _split_lines(rule, ref_name) -> list:
    """A rule's split lines, with each name resolved from its id.

    Defensive about what is stored: split_json predates the id, and a value
    that is not a list of objects must not take the whole rules screen down --
    which is exactly what an unguarded .get() did.
    """
    import json as _j
    raw = getattr(rule, "split_json", None)
    if not raw:
        return []
    try:
        parsed = _j.loads(raw)
    except Exception:                       # noqa: BLE001
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for ln in parsed:
        if not isinstance(ln, dict):
            continue
        out.append({**ln, "category": ref_name("account", ln.get("category_id"),
                                               ln.get("category", ""))})
    return out


def _ref_name_factory(db):
    """id -> the name that account or vendor has RIGHT NOW."""
    from .models_db import QboRefRow
    cache: dict = {}

    def _name(kind: str, ref_id, stored: str) -> str:
        if not ref_id:
            return stored or ""
        key = (kind, ref_id)
        if key not in cache:
            hit = db.query(QboRefRow.name).filter(
                QboRefRow.kind == kind, QboRefRow.qbo_id == str(ref_id)).first()
            cache[key] = hit[0] if hit else ""
        # An id that no longer resolves means the account was DELETED, not
        # renamed. The stored name is kept so the rule still reads as something
        # rather than emptying itself.
        return cache[key] or stored or ""
    return _name


@app.get("/rules")
def list_rules(company: str = Query(""),
               companies: str = Query(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Every vendor-and-category rule, in the order they are tried."""
    if company == ALL_COMPANIES:
        # Every reachable company's rules, each tagged with its company.
        _rules, _broken = [], 0
        _seen = set()
        for _co in _reachable(db, user, companies):
            r = list_rules(company=_co, companies="", db=db, user=user)
            for x in r.get("rules") or []:
                if x.get("shared"):
                    # ONE ROW for a shared rule, not one per company.
                    if x["id"] not in _seen:
                        _seen.add(x["id"])
                        _rules.append({**x, "company": "Shared"})
                    continue
                _rules.append({**x, "company": _co})
            _broken += int(r.get("broken_refs") or 0)
        return {"company": ALL_COMPANIES, "rules": _rules, "broken_refs": _broken}
    from .learning import SHARED, shared_companies, shared_rules
    from .models_db import LearnedRuleRow
    # The company's own rules, then EVERY shared rule -- the ones it ignores
    # too, marked so, so it can start using one again.
    from .learning import ordered as _ordered
    rows = _ordered(db, company, db.query(LearnedRuleRow).filter(LearnedRuleRow.company == company)
                    .order_by(LearnedRuleRow.updated_at, LearnedRuleRow.id).all())
    # SHARED RULES THIS PERSON'S COMPANIES COULD USE: one made only for
    # companies they can't reach is not listed, and a rule's company list
    # names only companies they can reach.
    _reach_l = set(_reachable(db, user))
    # ONLY THE SHARED RULES THIS COMPANY USES: one it was taken off (in
    # Multi-Company, or by its Ignore tick) is not listed here at all -- it
    # used to show grayed out.
    # Nor any, when the company opted out of Multi-Company rules.
    from .learning import no_shared_rules as _nsr_l
    if not _nsr_l(db, company):
        rows += [r for r in shared_rules(db) if company in shared_companies(db, r.id)]
    _ref_name = _ref_name_factory(db)
    # How many rules point at something QuickBooks no longer has. Reported so a
    # broken rule is a number on the screen rather than a blank cell somebody
    # has to notice.
    # The company's own rules only, BOTH tests inside the exclusion. Written as
    # "own and category, or vendor", a shared rule (vendor named, resolved per
    # company) counted on its vendor while its row was never marked -- the
    # notice said "marked ! below" with no ! to be found.
    _broken = sum(1 for r in rows if r.company != SHARED
                  and (((r.category or "") and not r.category_id)
                       or ((r.vendor or "") and not r.vendor_id)))
    return {
        "broken_refs": _broken,
        "company": company,
        "rules": [{
            "id": r.id, "name": r.name or "",
            "detail_op": r.detail_op or "contains",
            "detail_value": r.detail_value or "",
            "card_account": r.card_account or "",
            "amount_op": r.amount_op or "any",
            "amount_value": (str(r.amount_value)
                             if r.amount_value is not None else ""),
            "amount_value2": (str(r.amount_value2)
                              if r.amount_value2 is not None else ""),
            # Resolved from the id, so a rename in QuickBooks shows here
            # immediately. The stored name is the fallback for rules that have
            # no id -- those are the ones the health check flags.
            "vendor": _ref_name("vendor", r.vendor_id, r.vendor),
            "category": _ref_name("account", r.category_id, r.category),
            "auto_add_refunds": bool(getattr(r, "auto_add_refunds", True)),
            "vendor_ok": bool(r.vendor_id) or not (r.vendor or ""),
            "category_ok": bool(r.category_id) or not (r.category or ""),
            # The split, so the grid can show it and the dialog can reopen on it.
            # Names resolved from the id, so a renamed account shows its
            # current name here. Lines saved before ids existed keep the stored
            # name -- re-saving the split records the id.
            "split": _split_lines(r, _ref_name),
            "mode": r.mode or "prefill",
            # SHARED: one rule for several companies; "using" says whether
            # this company uses it.
            **({"shared": True, "companies": [c for c in shared_companies(db, r.id) if c in _reach_l],
                "using": company in shared_companies(db, r.id),
                "vendor_ok": True, "category_ok": True}
               if r.company == SHARED else {}),
        } for r in rows],
    }


VALID_DETAIL_OPS = ("contains", "equals")
VALID_AMOUNT_OPS = ("any", "equals", "lt", "gt", "between")
VALID_MODES = ("off", "prefill", "prefill_post")


@app.post("/rules/{rule_id}/mode")
def set_rule_mode(rule_id: str, mode: str = Form(...),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_write)):
    """Switch one rule between Off, Prefill and Prefill & Post."""
    from .models_db import LearnedRuleRow
    if mode not in VALID_MODES:
        raise HTTPException(400, f"'{mode}' is not a mode")
    r = db.get(LearnedRuleRow, rule_id)
    if r is None:
        raise HTTPException(404, "no such rule")
    if mode == "prefill_post" and not ((r.vendor or "").strip()
                                       and (r.category or "").strip()):
        # Posting needs both, and a rule that fired and then failed every time
        # would be worse than one that never fired.
        raise HTTPException(400, "A rule needs both a vendor and a category "
                                 "before it can post")
    r.mode = mode
    db.add(AuditRow(company=r.company, action="rule_mode",
                    detail=f"'{r.name or r.detail_value}' set to {mode}",
                    dry_run=False, actor=user.username))
    db.commit()
    # Switching a rule ON is the same decision as writing one, so it does the
    # same thing: sweeps what is already in For Review.
    pending = 0
    if mode == "prefill_post":
        from .learning import rule_matches, apply_auto_rules_async
        from .models_db import ChargeRow as _CR
        candidates = (db.query(_CR)
                      .filter(_CR.company == r.company,
                              _CR.status == "for_review",
                              _CR.qbo_txn_id == "").all())
        pending = sum(1 for c in candidates if rule_matches(r, c))
        apply_auto_rules_async(r.company, user.username, r.id)
    return {"id": rule_id, "mode": r.mode, "pending": pending}


def _rule_amount(op: str, v1: str, v2: str):
    """Read the amount condition, or explain why it can't be."""
    from decimal import Decimal, InvalidOperation
    if op not in VALID_AMOUNT_OPS:
        raise HTTPException(400, f"'{op}' is not an amount rule")
    if op == "any":
        return None, None
    def num(x, which):
        try:
            return abs(Decimal(str(x).strip()))
        except (InvalidOperation, TypeError, ValueError):
            raise HTTPException(400, f"{which} isn't an amount")
    if not str(v1 or "").strip():
        raise HTTPException(400, "That amount rule needs a figure")
    a = num(v1, "The amount")
    if op != "between":
        return a, None
    if not str(v2 or "").strip():
        raise HTTPException(400, "Between needs two figures")
    b = num(v2, "The second amount")
    # Stored in order, so a rule written 600-400 still reads 400-600 later.
    return min(a, b), max(a, b)


@app.post("/rules/add")
def add_rule(company: str = Form(...),
             name: str = Form(""),
             detail_op: str = Form("contains"), detail_value: str = Form(""),
             card_account: str = Form(""),
             amount_op: str = Form("any"), amount_value: str = Form(""),
             amount_value2: str = Form(""),
             category: str = Form(...), vendor: str = Form(...),
             split: str = Form(""),
             auto_add_refunds: str = Form("true"),
             mode: str = Form("prefill"), rule_id: str = Form(""),
             companies: str = Form(""),
             db: Session = Depends(get_session),
             user: UserRow = Depends(require_write)):
    """Create or edit one vendor-and-category rule.

    Nothing is inferred from behavior any more, so this is the only way a rule
    comes into existence.
    """
    if company == ALL_COMPANIES:
        # ONE SHARED RULE for the companies ticked -- edited once, used by
        # each; a company ignores it by being left off its list. A card account
        # belongs to one company, so a shared rule applies to any card.
        from .learning import SHARED as _SH, set_shared_companies as _ssc
        _targets = [c for c in _reachable(db, user, companies)] if companies.strip() else []
        if not _targets:
            raise HTTPException(400, "Choose the companies this rule is for")
        _ok = [c for c in _targets if _ref_by_name(db, c, "account", (category or "").strip())
               and _ref_by_name(db, c, "vendor", (vendor or "").strip())]
        if not _ok:
            raise HTTPException(400, "None of those companies' QuickBooks files has that "
                                     "vendor and category")
        r = add_rule(company=_SH, name=name, detail_op=detail_op, detail_value=detail_value,
                     card_account="", amount_op=amount_op, amount_value=amount_value,
                     amount_value2=amount_value2, category=category, vendor=vendor,
                     split=split, auto_add_refunds=auto_add_refunds, mode=mode,
                     rule_id="", companies=",".join(_targets), db=db, user=user)
        return {"added_to": _ok, "shared": True,
                "failed": {c: "its QuickBooks file doesn't have that vendor or category, "
                              "so the rule won't apply there until it does"
                           for c in _targets if c not in _ok}}
    from .models_db import LearnedRuleRow, QboRefRow
    import uuid as _uuid

    if detail_op not in VALID_DETAIL_OPS:
        raise HTTPException(400, f"'{detail_op}' is not a bank detail rule")
    if mode not in VALID_MODES:
        raise HTTPException(400, f"'{mode}' is not a mode")

    # Lowercased on store, like the payment, Canada and match-rule tables, so a
    # bank-detail rule is case-insensitive end to end -- the matcher already
    # lowercases at read time, this keeps the stored/displayed value consistent
    # with it and with the other three rule tables.
    detail_value = (detail_value or "").strip().lower()
    amt1, amt2 = _rule_amount(amount_op, amount_value, amount_value2)
    # A rule with no conditions at all would claim every charge in the company.
    if not detail_value and not (card_account or "").strip() and amount_op == "any":
        raise HTTPException(400, "A rule needs at least one condition — "
                                 "otherwise it matches every transaction")

    category, vendor = (category or "").strip(), (vendor or "").strip()
    if not category or not vendor:
        raise HTTPException(400, "A category and a vendor are both required")
    # Checked against QuickBooks now rather than at post time, where the failure
    # would land on whoever happened to click Add.
    if company != "__shared__":
        if not _ref_by_name(db, company, "account", category):
            raise HTTPException(400, f"'{category}' isn't in the chart of accounts")
        if not _ref_by_name(db, company, "vendor", vendor):
            raise HTTPException(400, f"'{vendor}' isn't a vendor in QuickBooks")

    row = db.get(LearnedRuleRow, rule_id) if rule_id else None
    _shared_rule_guard(db, user, row)
    if row is None:
        # A generated id, not one derived from the bank detail: two rules can
        # legitimately share a phrase and differ on card account or amount.
        row = LearnedRuleRow(id=f"{company}|{_uuid.uuid4().hex[:12]}",
                             company=company)
        db.add(row)
    row.name = (name or "").strip() or detail_value or "Rule"
    row.detail_op, row.detail_value = detail_op, detail_value
    row.card_account = (card_account or "").strip()
    row.amount_op, row.amount_value, row.amount_value2 = amount_op, amt1, amt2
    row.category, row.vendor, row.mode = category, vendor, mode
    row.auto_add_refunds = auto_add_refunds != "false"
    # The id behind each name, so a later rename in QuickBooks flows through
    # instead of orphaning the rule. Unknown names keep a null id and are
    # reported by the health check rather than being rejected here -- a rule
    # may legitimately be written before the account exists.
    def _ref_id(kind: str, name: str) -> str | None:
        from .models_db import QboRefRow
        if not (name or "").strip():
            return None
        hit = _ref_by_name(db, company, kind, name)
        return hit.qbo_id if hit else None
    # A shared rule has no one file to take ids from: matched by name in each.
    row.category_id = _ref_id("account", category) if company != "__shared__" else None
    row.vendor_id = _ref_id("vendor", vendor) if company != "__shared__" else None
    if company == "__shared__" and companies.strip():
        from .learning import set_shared_companies as _ssc2
        # ONLY companies this person can reach.
        _reach2 = set(_reachable(db, user))
        _ssc2(db, row.id, [c for c in companies.split(",") if c.strip() and c.strip() in _reach2])
    # A split chosen while the rule was being written, saved in the same step.
    # Validated the same way the dedicated endpoint validates it: two lines
    # minimum, totaling 100, or the rule codes part of a charge and leaves the
    # rest silently uncoded.
    if split:
        import json as _sj
        try:
            raw = _sj.loads(split) or []
        except ValueError:
            raise HTTPException(400, "Could not read the split lines")
        lines = []
        for ln in raw if isinstance(raw, list) else []:
            cat = str((ln or {}).get("category") or "").strip()
            try:
                pct = round(float((ln or {}).get("percent") or 0), 4)
            except (TypeError, ValueError):
                pct = 0.0
            if cat and pct > 0:
                _h = _ref_by_name(db, company, "account", cat)
                lines.append({"category": cat, "percent": pct,
                              "category_id": _h.qbo_id if _h else None})
        if lines:
            if len(lines) < 2:
                raise HTTPException(400, "A split needs at least two categories")
            if abs(round(sum(l["percent"] for l in lines), 4) - 100.0) > 0.01:
                raise HTTPException(400, "The percentages have to total 100%")
        row.split_json = _sj.dumps(lines) if lines else None
    row.last_actor = user.username
    # Only on creation: the sort order IS the match order, so editing a rule
    # must not quietly move it to the end of the queue.
    if row.updated_at is None:
        row.updated_at = datetime.utcnow()
    db.add(AuditRow(company=company,
                    action="rule_saved" if rule_id else "rule_added",
                    detail=f"'{row.name}' set to {category} / {vendor} [{mode}]",
                    dry_run=False, actor=user.username))
    db.commit()

    # A Prefill & Post rule applies to what is ALREADY waiting, not only to
    # what arrives next. Writing a rule is the decision; making it take effect
    # only for future uploads would leave the queue holding transactions the
    # rule plainly covers, and no way to say so.
    #
    # Scoped to the rule just saved, so saving one rule cannot set another
    # loose over the backlog.
    # Counted BEFORE running, so the response can say how much work is starting
    # rather than leaving the browser on a spinner for two minutes with nothing
    # to read. The sweep itself runs in the background.
    pending = 0
    if mode == "prefill_post":
        from .learning import rule_matches, apply_auto_rules_async
        from .models_db import ChargeRow as _CR
        candidates = (db.query(_CR)
                      .filter(_CR.company == company, _CR.status == "for_review",
                              _CR.qbo_txn_id == "").all())
        pending = sum(1 for c in candidates if rule_matches(row, c))
        apply_auto_rules_async(company, user.username, row.id)
    return {"id": row.id, "pending": pending}


@app.post("/rules/shared/companies")
def set_shared_rule_companies(rule_id: str = Form(...), companies: str = Form(""),
                              company: str = Form(""), use: str = Form(""),
                              db: Session = Depends(get_session),
                              user: UserRow = Depends(require_write)):
    """Which companies use a shared rule: the whole list (Multi-Company),
    or one company starting or stopping (Ignore / Use, inside a company)."""
    from .learning import SHARED, shared_companies, set_shared_companies
    from .models_db import LearnedRuleRow
    row = db.get(LearnedRuleRow, rule_id)
    if row is None or row.company != SHARED:
        raise HTTPException(404, "No such shared rule")
    reach = set(_reachable(db, user))
    have = set(shared_companies(db, rule_id))
    if company:
        if company not in reach:
            raise HTTPException(403, f"You don't have access to {company}")
        on = str(use).lower() in ("1", "true", "yes", "on")
        have = (have | {company}) if on else (have - {company})
        detail = f"'{row.name}': {company} {'uses' if on else 'ignores'} it"
    else:
        want = {c.strip() for c in companies.split(",") if c.strip()}
        # Only the companies this person can reach change; others are kept.
        have = (have - reach) | (want & reach)
        detail = f"'{row.name}': used by {', '.join(sorted(have)) or 'no company'}"
    cos = set_shared_companies(db, rule_id, list(have))
    db.add(AuditRow(company=company or "", action="rule_saved", detail=detail,
                    dry_run=False, actor=user.username))
    db.commit()
    return {"companies": cos}


@app.post("/rules/bulk")
def bulk_edit_rules(ids: str = Form(...), field: str = Form(...),
                    value: str = Form(""),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_write)):
    """Set one field across several rules.

    One field at a time on purpose. A dialog that changed four things at once
    would need four "leave as they are" options, and the difference between
    "set this to All" and "don't touch it" is exactly the thing nobody would
    notice getting wrong.
    """
    from .models_db import LearnedRuleRow, SourceAccountRow

    allowed = {"detail_op": VALID_DETAIL_OPS, "amount_op": VALID_AMOUNT_OPS,
               "mode": VALID_MODES, "card_account": None,
               "auto_add_refunds": ("true", "false")}
    if field not in allowed:
        raise HTTPException(400, f"'{field}' is not something this can change")
    if allowed[field] is not None and value not in allowed[field]:
        raise HTTPException(400, f"'{value}' is not a valid {field}")
    if field == "card_account" and value:
        known = {r.source for r in db.query(SourceAccountRow.source).all()}
        if value not in known:
            raise HTTPException(400, f"'{value}' isn't a card account")

    wanted = [i.strip() for i in (ids or "").split(",") if i.strip()]
    rows = db.query(LearnedRuleRow).filter(LearnedRuleRow.id.in_(wanted)).all()

    changed, skipped = 0, []
    for r in rows:
        if field == "mode" and value == "prefill_post" and not (
                (r.vendor or "").strip() and (r.category or "").strip()):
            skipped.append(f"{r.name or r.detail_value}: needs a vendor and a "
                           f"category before it can post")
            continue
        # Switching the amount rule to something that needs a figure, on a rule
        # that has none, would save a condition that can never be true.
        # Only meaningful on a rule that adds at all; on any other it would be
        # a setting that quietly does nothing.
        if field == "auto_add_refunds" and (r.mode or "") != "prefill_post":
            skipped.append(f"{r.name or r.detail_value}: only applies when Auto "
                           f"is Fill & Add")
            continue
        if field == "amount_op" and value != "any" and r.amount_value is None:
            skipped.append(f"{r.name or r.detail_value}: has no amount to "
                           f"compare against")
            continue
        # The boolean field arrives as text, like every other form value.
        setattr(r, field,
                value == "true" if field == "auto_add_refunds" else value)
        changed += 1
    if changed:
        db.add(AuditRow(company=rows[0].company if rows else "",
                        action="rules_bulk_edit",
                        detail=f"{field} set to '{value}' on "
                               f"{changed} rule(s)",
                        dry_run=False, actor=user.username))
        db.commit()
    return {"changed": changed, "skipped": skipped}


@app.get("/rules/template/{company}")
def rules_template(company: str = "", db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    """An empty sheet with the columns the importer expects.

    Carries two example rows rather than a bare header: the columns that take
    a fixed set of words -- Contains/Equals, the amount rules, the three Auto
    modes -- are much easier to get right from an example than from a guess.
    Both are marked so nobody mistakes them for real rules.
    """
    from .export_xlsx import build_workbook, filename
    headers = ["Rule Name", "Bank Detail Rule", "Bank Detail Value",
               "Card Account", "Amount Rule", "Amount Value", "Amount Value 2",
               "Vendor", "Category", "Auto"]
    rows = [
        ["EXAMPLE — delete this row", "Contains", "blue jays", "", "Any",
         None, None, "Toronto Blue Jays",
         "Ticket Credits:MLB:Toronto Blue Jays", "Prefill"],
        ["EXAMPLE — delete this row", "Equals", "TM *BULLS", "divvy", "Between",
         100, 500, "Chicago Bulls", "Auto Expense", "Off"],
    ]
    applied = [
        ("Bank Detail Rule", "Contains or Equals"),
        ("Card Account", "The card account key, or blank for all"),
        ("Amount Rule", "Any, Equals, Less Than, Greater Than or Between"),
        ("Amount Value 2", "Only used by Between"),
        ("Auto", "Off, Prefill, or Prefill & Post"),
        ("Vendor and Category", "Must already exist in QuickBooks"),
        ("Note", "A rule needs at least one condition, or it would match "
                 "every transaction"),
    ]
    blob = build_workbook("Rules", headers, rows, applied, money_cols=[5, 6], filters_sheet=False)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("rules", "template")}"'})


@app.post("/rules/import")
async def import_rules(company: str = Form(...), file: UploadFile = File(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_write)):
    """Load rules from a spreadsheet with the same columns the export writes.

    Validated row by row and applied ALL OR NOTHING. A half-loaded rule set is
    worse than none: the rules that landed would start prefilling while the
    ones that did not are invisible, and nobody could tell which was which by
    looking. Every problem is reported with its row number instead.
    """
    from io import BytesIO
    from openpyxl import load_workbook
    from .models_db import LearnedRuleRow, QboRefRow, SourceAccountRow
    import uuid as _uuid

    raw = await file.read()
    try:
        ws = load_workbook(BytesIO(raw), data_only=True).active
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(400, f"That file could not be read as a "
                                 f"spreadsheet: {e}")

    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        raise HTTPException(400, "That sheet has no rows under its header")

    def norm(x):
        return str(x).strip() if x is not None else ""

    head = [norm(h).lower() for h in rows[0]]
    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None

    ix = {
        "name": col("rule name", "name"),
        "dop": col("bank detail rule", "bank detail op"),
        "dval": col("bank detail value", "bank detail"),
        "card": col("card account"),
        "aop": col("amount rule", "amount op"),
        "a1": col("amount value", "amount"),
        "a2": col("amount value 2", "amount value2"),
        "vendor": col("vendor"),
        "category": col("category"),
        "mode": col("auto", "mode"),
    }
    if ix["vendor"] is None or ix["category"] is None:
        raise HTTPException(400, "That sheet needs at least Vendor and "
                                 "Category columns")

    DOP = {"contains": "contains", "equals": "equals", "": "contains"}
    AOP = {"any": "any", "equals": "equals", "less than": "lt", "lt": "lt",
           "greater than": "gt", "gt": "gt", "between": "between", "": "any"}
    MODE = {"off": "off", "prefill": "prefill",
            "prefill & post": "prefill_post", "prefill and post": "prefill_post",
            "prefill_post": "prefill_post", "": "prefill",
            # A sheet exported before this change carries Yes/No.
            "yes": "prefill_post", "no": "prefill"}

    accounts = {r.name for r in db.query(QboRefRow.name).filter(
        QboRefRow.kind == "account").all() if r.name}
    vendors = {r.name for r in db.query(QboRefRow.name).filter(
        QboRefRow.kind == "vendor").all() if r.name}
    sources = {r.source for r in db.query(SourceAccountRow.source).filter(
        SourceAccountRow.company == company).all()}

    staged, problems = [], []
    for n, raw_row in enumerate(rows[1:], start=2):
        get = lambda k: norm(raw_row[ix[k]]) if ix[k] is not None \
            and ix[k] < len(raw_row) else ""
        if not any(get(k) for k in ix if ix[k] is not None):
            continue                                # a blank spacer row
        vendor, category = get("vendor"), get("category")
        dop = DOP.get(get("dop").lower())
        aop = AOP.get(get("aop").lower())
        mode = MODE.get(get("mode").lower())
        card = get("card")
        if dop is None:
            problems.append(f"row {n}: '{get('dop')}' is not Contains or Equals")
            continue
        if aop is None:
            problems.append(f"row {n}: '{get('aop')}' is not an amount rule")
            continue
        if mode is None:
            problems.append(f"row {n}: '{get('mode')}' is not Off, Prefill or "
                            f"Prefill & Post")
            continue
        if not vendor or not category:
            problems.append(f"row {n}: both a vendor and a category are needed")
            continue
        if category not in accounts:
            problems.append(f"row {n}: '{category}' isn't in the chart of accounts")
            continue
        if vendor not in vendors:
            problems.append(f"row {n}: '{vendor}' isn't a vendor in QuickBooks")
            continue
        if card and card not in sources:
            problems.append(f"row {n}: '{card}' isn't a card account")
            continue
        try:
            a1, a2 = _rule_amount(aop, get("a1"), get("a2"))
        except HTTPException as e:
            problems.append(f"row {n}: {e.detail}")
            continue
        if not get("dval") and not card and aop == "any":
            problems.append(f"row {n}: no conditions — that would match every "
                            f"transaction")
            continue
        staged.append(dict(name=get("name") or get("dval") or "Rule",
                           detail_op=dop, detail_value=get("dval").lower(),
                           card_account=card, amount_op=aop,
                           amount_value=a1, amount_value2=a2,
                           vendor=vendor, category=category, mode=mode))

    if problems:
        raise HTTPException(400, "Nothing was imported. "
                            + f"{len(problems)} problem(s): "
                            + " | ".join(problems[:8]))
    if not staged:
        raise HTTPException(400, "No rules found in that sheet")

    for r in staged:
        db.add(LearnedRuleRow(id=f"{company}|{_uuid.uuid4().hex[:12]}",
                              company=company, last_actor=user.username,
                              updated_at=datetime.utcnow(), **r))
    db.add(AuditRow(company=company, action="rules_imported",
                    detail=f"{len(staged)} rule(s) from {file.filename}",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"imported": len(staged)}


@app.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, company: str = Query(""), db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    from .models_db import LearnedRuleRow
    from .learning import SHARED
    r = db.get(LearnedRuleRow, rule_id)
    if r is None:
        raise HTTPException(404, "no such rule")
    # A SHARED RULE IS NOT REMOVED FROM ONE COMPANY: it belongs to all of them.
    # A company ignores it instead (Ignore (Shared Rules)); only Multi-Company,
    # where it is managed for everyone, removes it.
    if r.company == SHARED and company != ALL_COMPANIES:
        raise HTTPException(403, "A shared rule is removed in Multi-Company. Here, tick Ignore (Shared Rules) "
                                 "to stop this company using it.")
    _shared_rule_guard(db, user, r)
    db.delete(r); db.commit()
    return {"deleted": rule_id}


@app.post("/rules/apply")
def apply_rules(company: str = Form(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_admin)):
    """Run every Prefill & Post rule over For Review, posting what matches.

    Admin only: this writes to QuickBooks without anybody looking at the
    individual transactions, which is a different thing from the rest of the
    app and should be reachable by fewer people.
    """
    from .learning import apply_auto_rules
    return apply_auto_rules(db, company, actor=user.username)


# --- unified queue ----------------------------------------------------------

# --- ALL COMPANIES (Multi-Company) ----------------------------------------
# The company picker's "All Companies" sends this in place of a company. The
# Bank Feed endpoints then ask each company the person can reach exactly as
# they would one company, tag every row with its company, and merge -- so a
# row's actions still act within its own company.
ALL_COMPANIES = "__all__"


def _shared_rule_guard(db, user, row) -> None:
    """A SHARED RULE belongs to the companies using it: only someone who can
    reach every one of them may change or remove it for all of them."""
    from .learning import SHARED, shared_companies
    if row is None or row.company != SHARED:
        return
    reach = set(_reachable(db, user))
    if set(shared_companies(db, row.id)) - reach:
        raise HTTPException(403, "This shared rule is also used by companies you don't have access to, "
                                 "so it can't be changed or removed here.")


def _reachable(db, user, only: str = "") -> list:
    """The companies this person can reach -- narrowed to `only` (the
    Companies picker, comma separated) when it names any."""
    from .membership import companies_for
    from .models_db import CompanyRow as _CRr
    allowed = companies_for(db, user)
    names = [c.name for c in db.query(_CRr).filter(_CRr.active.is_(True))
             .order_by(_CRr.name).all() if c.name]
    pick = {x.strip() for x in (only or "").split(",") if x.strip()}
    return [n for n in names if (allowed is None or n in allowed)
            and (not pick or n in pick)]


def _split_cards(card):
    """ "Company|source,Company|source" -> {company: "source,source"}, or None
    when no card is chosen (every card of every company)."""
    if not card:
        return None
    out = {}
    for part in str(card).split(","):
        co, _, src = part.partition("|")
        if co and src:
            out.setdefault(co, []).append(src)
    return {co: ",".join(v) for co, v in out.items()}


def _merge_key(sort: str):
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    keys = {"amount": lambda i: num(i.get("amt", i.get("amount"))),
            "amt": lambda i: num(i.get("amt", i.get("amount"))),
            "balance": lambda i: num(i.get("balance")),
            "vendor": lambda i: str(i.get("merchant") or i.get("vendor") or "").lower(),
            "account": lambda i: str(i.get("account") or "").lower(),
            "status": lambda i: num(i.get("score")),
            "company": lambda i: str(i.get("company") or "").lower()}
    return keys.get((sort or "date").lower(),
                    lambda i: str(i.get("date") or i.get("txn_date") or ""))


def _each_company(cos: list, work) -> dict:
    """Run work(company, db) for every company AT THE SAME TIME, each on its
    own database connection, and return {company: result}. Multi-Company
    built each company's Bank Feed one after another, so five companies took
    five times as long as one. A company that fails is left out and logged."""
    from concurrent.futures import ThreadPoolExecutor
    from .db import SessionLocal as _SLp

    def one(co):
        db = _SLp()
        try:
            return co, work(co, db)
        except Exception:                               # noqa: BLE001
            log.exception("multi-company: %s failed", co)
            return co, None
        finally:
            db.close()
    if len(cos) <= 1:
        return dict(one(c) for c in cos)
    with ThreadPoolExecutor(max_workers=min(6, len(cos))) as ex:
        return dict(ex.map(one, cos))


def _fan_rows(fn, kw: dict, rows_key: str):
    """Run a list endpoint for every reachable company and merge its rows."""
    db, user = kw["db"], kw["user"]
    lim, off = int(kw.get("limit") or 200), int(kw.get("offset") or 0)
    cards = _split_cards(kw.get("card")) if "card" in kw else None
    rows, total, first = [], 0, None
    cos = [co for co in _reachable(db, user, kw.get("companies") or "")
           if cards is None or co in cards]

    def work(co, cdb):
        k2 = {**kw, "company": co, "limit": off + lim, "offset": 0, "db": cdb}
        if cards is not None:
            k2["card"] = cards[co]
        return fn(**k2)
    res = _each_company(cos, work)
    for co in cos:                      # merged in company order, as before
        r = res.get(co)
        if r is None:
            continue
        first = first or r
        total += int(r.get("total") or 0)
        for it in r.get(rows_key) or []:
            it["company"] = co
            rows.append(it)
    rows.sort(key=_merge_key(kw.get("sort")),
              reverse=(str(kw.get("direction") or "desc").lower() == "desc"))
    out = dict(first or {})
    out.update(company=ALL_COMPANIES, total=total, offset=off, limit=lim)
    out[rows_key] = rows[off:off + lim]
    # NO "STH not synced" BANNER in Multi-Company: it came from whichever
    # company answered first, and says nothing true about all of them.
    if "hal_connected" in out:
        out["hal_connected"] = None
    return out


@app.get("/queue/{company}")
def queue(company: str, status: str = Query("for_review"),
          card: str | None = Query(None), source: str | None = Query(None),
          limit: int = Query(200), offset: int = Query(0),
          date_from: str = Query(""), date_to: str = Query(""),
          amt_min: str = Query(""), amt_max: str = Query(""),
               samt_min: str = Query(""), samt_max: str = Query(""),
               drift: str = Query(""),
               rules_applied: str = Query(""),
          vendor: str = Query(""), profile: str = Query(""),
          last4: str = Query(""), memo: str = Query(""),
          details: str = Query(""), txn_type: str = Query(""),
          category: str = Query(""), user_f: str = Query("", alias="user"),
          ts_from: str = Query(""), ts_to: str = Query(""),
          currency: str = Query(""), qbo_id: str = Query(""),
          order_no: str = Query(""), ingest: str = Query(""),
          kinds: str = Query(""), cvendor: str = Query(""),
          tier: str = Query(""), matchable: str = Query(""),
          record: str = Query(""),
          q: str = Query(""), sort: str = Query("date"),
          direction: str = Query("desc"),
          companies: str = Query(""),
          db: Session = Depends(get_session),
          user: UserRow = Depends(require_reviewer)):
    if company == ALL_COMPANIES:
        return _fan_rows(queue, dict(locals()), "items")
    from datetime import date as _date
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    from .review import build_queue
    # THE RULES FILTER NEEDS CURRENT SUGGESTIONS, BUT NOT AT THE COST OF THE
    # REQUEST.
    #
    # The badge is computed as the grid renders; the filter reads the stored
    # suggestion. Running the rescore inline fixed the disagreement and hung
    # the screen: 29,000 charges against 2,233 bills is a minute of work, and
    # the grid simply never came back.
    #
    # So it runs in the BACKGROUND and the grid answers immediately from what
    # is stored. The first use of the filter may be a little stale; by the time
    # anyone reads the page it has been corrected, and the response says so
    # rather than leaving the reader to wonder.
    rescoring = False
    if rules_applied.strip() and offset == 0:
        rescoring = _kick_rescore(company)

    _out = build_queue(db, company, status=status, card=card, source=source,
                       limit=limit, offset=offset,
                       date_from=_d(date_from), date_to=_d(date_to),
                       amt_min=_m(amt_min), amt_max=_m(amt_max),
                       samt_min=_m(samt_min), samt_max=_m(samt_max),
                       drift=(drift.strip().lower() in ("1", "true", "yes")),
                       rules_applied=rules_applied,
                       vendor=vendor or None, profile=profile or None,
                       last4=last4 or None, memo=memo or None,
                       details=details or None, txn_type=txn_type or None,
                       category=category or None, user=user_f or None,
                       qbo_id=qbo_id or None, order_no=order_no or None,
                       ingest=ingest or None,
                       ts_from=_dt_from(ts_from), ts_to=_dt_to(ts_to),
                       currency=currency or None,
                       kinds=kinds or None, cvendor=cvendor or None,
                       tier=tier or None, matchable=matchable or None,
                       record=record or None,
                       q=q or None, sort=sort, direction=direction)
    if rescoring:
        # Read by the grid, which says so rather than showing a filtered list
        # that may be a few rows short without explanation.
        _out["rescoring"] = True
    # THE OTHER SIDE OF A TRANSFER already in QuickBooks, offered on its row.
    # The Records filter, or nothing ticked in Strengths / Records: charges not
    # yet looked up are looked up now, in the background.
    if status == "for_review" and offset == 0 and (record or (tier or "") == "\u0000none"):
        try:
            _w = _kick_hal_fill(db, company)
            if _w:
                _out["checking_records"] = _w
        except Exception:                                       # noqa: BLE001
            log.exception("could not start the STH record lookup pass")
    if status == "for_review":
        try:
            _tm = transfer_matches(db, company)
            _tw = transfer_why(db, company, _tm)
            for _it in (_out.get("items") or []):
                if _it.get("id") in _tm:
                    _it["transfer_match"] = _tm[_it["id"]]
                elif _it.get("id") in _tw:
                    _it["transfer_why"] = _tw[_it["id"]]
        except Exception:                               # noqa: BLE001
            log.exception("transfer matching failed for %s", company)
    return _out



@app.get("/bills/{company}")
def list_bills(company: str, limit: int = Query(200), offset: int = Query(0),
               q: str = Query(""), vendor: str = Query(""), memo: str = Query(""),
               date_from: str = Query(""), date_to: str = Query(""),
               amt_min: str = Query(""), amt_max: str = Query(""),
               samt_min: str = Query(""), samt_max: str = Query(""),
               drift: str = Query(""),
               rules_applied: str = Query(""),
               bal_min: str = Query(""), bal_max: str = Query(""),
               qbo_id: str = Query(""), aging: str = Query(""),
               klass: str = Query(""), scope: str = Query("open"),
               sort: str = Query("date"),
               direction: str = Query("desc"),
               companies: str = Query(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Open bills available to match against, as rows. `scope=all` lists every
    bill, open or paid (All Bills). `klass` narrows to Classes (comma
    separated; "__none" is a bill with no class).

    Same shape as the charge queue so the UI can render it with the same table.
    """
    if company == ALL_COMPANIES:
        return _fan_rows(list_bills, dict(locals()), "bills")
    from sqlalchemy import or_ as _or
    from .models_db import BillRow

    from datetime import date as _date
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    # Open bills only, matching what the matcher treats as a candidate. A bill
    # paid inside the app is drawn down immediately, so it leaves this tab at
    # the same moment it stops being suggested -- the count in the tab header
    # and the candidates in the queue can't disagree.
    if scope == "all":
        from .models_db import AllBillRow as BillRow        # noqa: F811
        qy = db.query(BillRow).filter(BillRow.company == company)
    else:
        qy = db.query(BillRow).filter(BillRow.company == company,
                                      BillRow.balance > 0)
    # THE CLASSES on this company's bills, for the Class filter's choices.
    _classes = sorted({c.strip() for (v,) in db.query(BillRow.class_name)
                       .filter(BillRow.company == company).distinct()
                       for c in (v or "").split(",") if c.strip()}, key=str.lower)
    if klass:
        _want = [k.strip() for k in klass.split(",") if k.strip()]
        _conds = []
        for k in _want:
            if k == "__none":
                _conds += [BillRow.class_name.is_(None), BillRow.class_name == ""]
            else:
                _conds += [BillRow.class_name == k, BillRow.class_name.like(f"{k}, %"),
                           BillRow.class_name.like(f"%, {k}"), BillRow.class_name.like(f"%, {k}, %")]
        if _conds:
            qy = qy.filter(_or(*_conds))
    # `q` stays as the bill-number / catch-all box; the rest are the separate
    # fields reviewers actually think in -- a combined search matches none of
    # them well.
    if q:
        like = f"%{q}%"
        qy = qy.filter(_or(BillRow.vendor.ilike(like), BillRow.memo.ilike(like),
                           BillRow.doc_number.ilike(like),
                           BillRow.email.ilike(like)))
    if vendor:
        qy = qy.filter(BillRow.vendor.ilike(f"%{vendor}%"))
    if memo:
        qy = qy.filter(BillRow.memo.ilike(f"%{memo}%"))
    if qbo_id:
        # A bill IS the QuickBooks transaction, so its id is the thing you would
        # have copied out of QuickBooks to come looking.
        qy = qy.filter(BillRow.bill_id.ilike(f"%{qbo_id.strip()}%"))
    _df, _dt = _d(date_from), _d(date_to)
    if _df:
        qy = qy.filter(BillRow.txn_date >= _df)
    if _dt:
        qy = qy.filter(BillRow.txn_date <= _dt)
    _amin, _amax = _m(amt_min), _m(amt_max)
    if _amin is not None:
        qy = qy.filter(BillRow.amount >= _amin)
    if _amax is not None:
        qy = qy.filter(BillRow.amount <= _amax)
    # Balance is what's still owed, which is what makes a bill a candidate at
    # all -- worth filtering separately from the original amount.
    _bmin, _bmax = _m(bal_min), _m(bal_max)
    if _bmin is not None:
        qy = qy.filter(BillRow.balance >= _bmin)
    if _bmax is not None:
        qy = qy.filter(BillRow.balance <= _bmax)
    total = qy.count()

    # Every column the bills grid offers, so clicking any heading sorts --
    # QBO Memo was the one that did nothing, which reads as a broken grid
    # rather than a column that happens not to sort.
    col = {"date": BillRow.txn_date, "amount": BillRow.amount,
           "vendor": BillRow.vendor, "bill": BillRow.doc_number,
           "balance": BillRow.balance, "memo": BillRow.memo,
           "class": BillRow.class_name,
           "qbo": BillRow.bill_id}.get(sort, BillRow.txn_date)
    qy = qy.order_by(col.desc() if direction != "asc" else col.asc())
    # Aging is derived, not stored, so it is filtered after the rows are read.
    # The bill pool is small enough that this costs nothing, and storing a
    # bucket would go stale the moment the date changed.
    if aging:
        # Several buckets at once ("Current,1 - 30"), as the picker sends them.
        _ages = {a.strip() for a in aging.split(",") if a.strip()}
        allrows = qy.all()
        keep = [b for b in allrows if _aging_bucket(b.txn_date) in _ages]
        total = len(keep)
        rows = keep[offset:offset + limit] if limit else keep[offset:]
    else:
        rows = (qy.offset(offset).limit(limit).all() if limit
                else qy.offset(offset).all())

    return {"company": company, "total": total, "offset": offset, "limit": limit,
            "scope": "all" if scope == "all" else "open", "classes": _classes,
            "bills": [{
                "bill": b.doc_number or b.bill_id, "bill_id": b.bill_id,
                "class": b.class_name or "",
                "amt": str(b.amount), "balance": str(b.balance),
                "date": str(b.txn_date), "vendor": b.vendor or "",
                "email": b.email or "", "memo": (b.memo or "")[:120],
                "quantity": b.quantity, "lines": b.line_count,
                "aging": _aging_bucket(b.txn_date),
                "aging_days": _aging_days(b.txn_date),
            } for b in rows]}


# --- ap aging ---------------------------------------------------------------
#
# QuickBooks ages a bill from its DUE date. No due date is mirrored here, so it
# is taken as net 30 from the bill date -- the terms these bills actually carry.
# "Current" therefore means not yet due, and the buckets count days past due,
# which is what the A/P Aging report shows.
_AGING_TERMS_DAYS = 30


def _aging_days(txn_date) -> int | None:
    """Days past due, negative when it is not due yet."""
    if not txn_date:
        return None
    from datetime import date as _d
    try:
        due = txn_date + timedelta(days=_AGING_TERMS_DAYS)
        return (_d.today() - due).days
    except Exception:                       # noqa: BLE001
        return None


def _aging_bucket(txn_date) -> str:
    d = _aging_days(txn_date)
    if d is None:
        return ""
    if d <= 0:
        return "Current"
    if d <= 30:
        return "1 - 30"
    if d <= 60:
        return "31 - 60"
    if d <= 90:
        return "61 - 90"
    return "91 and over"


@app.get("/prefs/{key}")
def get_pref(key: str, db: Session = Depends(get_session),
             user: UserRow = Depends(require_reviewer)):
    """One preference blob for the signed-in user. Absent is not an error."""
    from .models_db import UserPrefRow
    row = db.query(UserPrefRow).filter(UserPrefRow.username == user.username,
                                       UserPrefRow.key == key).first()
    return {"key": key, "value": row.value if row else ""}


@app.post("/prefs/{key}")
def set_pref(key: str, value: str = Form(...),
             db: Session = Depends(get_session),
             user: UserRow = Depends(require_reviewer)):
    """Save a preference blob. Available to view-only users on purpose --
    changing which columns YOU see isn't a write to anyone's data."""
    from .models_db import UserPrefRow
    row = db.query(UserPrefRow).filter(UserPrefRow.username == user.username,
                                       UserPrefRow.key == key).first()
    if row is None:
        row = UserPrefRow(username=user.username, key=key)
        db.add(row)
    # NOT CUT SHORT: a layout cut mid-way is not JSON, and the whole of it --
    # every grid's widths and order -- would be lost on the next sign-in.
    if len(value) > 500000:
        raise HTTPException(400, "That preference is too large to save")
    row.value = value
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"key": key, "saved": True}


@app.get("/queue/{company}/counts")
def queue_count(company: str, card: str | None = Query(None),
                status: str = Query("for_review"),
                txn_type: str = Query(""), kinds: str = Query(""),
                date_from: str = Query(""), date_to: str = Query(""),
                amt_min: str = Query(""), amt_max: str = Query(""),
               samt_min: str = Query(""), samt_max: str = Query(""),
               drift: str = Query(""),
               rules_applied: str = Query(""),
                vendor: str = Query(""), profile: str = Query(""),
                last4: str = Query(""), memo: str = Query(""),
                cvendor: str = Query(""), q: str = Query(""),
                qbo_id: str = Query(""), order_no: str = Query(""),
                ingest: str = Query(""),
                companies: str = Query(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    if company == ALL_COMPANIES:
        # Every company's counts, added up; each card listed with its company.
        _kw = dict(locals()); _db, _u = _kw["db"], _kw["user"]
        _cards = _split_cards(_kw.get("card"))
        _tot = {"bills": 0, "for_review": 0, "categorized": 0, "excluded": 0, "sources": []}
        _cos = [c for c in _reachable(_db, _u, _kw.get("companies") or "")
                if _cards is None or c in _cards]

        def _w(co, cdb):
            k2 = {**_kw, "company": co, "db": cdb}
            if _cards is not None:
                k2["card"] = _cards[co]
            return queue_count(**k2)
        _res = _each_company(_cos, _w)
        for _co in _cos:
            _r = _res.get(_co)
            if _r is None:
                continue
            for _n in ("bills", "for_review", "categorized", "excluded"):
                _tot[_n] += int(_r.get(_n) or 0)
            for _s in _r.get("sources") or []:
                _tot["sources"].append({**_s, "source": f"{_co}|{_s.get('source')}",
                                        "name": f"{_co} \u00b7 {_s.get('name') or _s.get('source')}",
                                        "company": _co})
        return _tot
    from datetime import date as _date
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    from .review import queue_counts
    return queue_counts(db, company, card=card, filters=dict(
        status=status or None, txn_type=txn_type or None, kinds=kinds or None,
        date_from=_d(date_from), date_to=_d(date_to),
        amt_min=_m(amt_min), amt_max=_m(amt_max),
                       samt_min=_m(samt_min), samt_max=_m(samt_max),
                       drift=(drift.strip().lower() in ("1", "true", "yes")),
                       rules_applied=rules_applied,
        vendor=vendor or None, profile=profile or None, last4=last4 or None,
        memo=memo or None, cvendor=cvendor or None, q=q or None))


def _rescore_after_upload(db: Session, company: str) -> int:
    """New charges arrive with no strength; give them one before they're seen."""
    from .review import refresh_scores
    try:
        return refresh_scores(db, company)
    except Exception:
        return 0     # a scoring hiccup must not fail the upload itself


# --- restored: these four routes were removed by accident when the counts
# --- endpoint was rewritten, and the UI never stopped calling them.

@app.post("/queue/purge")
def purge_excluded(company: str = Form(...), ids: str = Form(""),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Permanently delete excluded charges. Admin only, irreversible.

    Excluded means the charge does not belong in the ledger, so by definition
    there is no QuickBooks transaction behind it -- excluding a posted charge is
    refused, and undoing one deletes its transaction first. Deleting from here
    therefore never touches QuickBooks and never asks about it.

    A handful of rows predate that rule and still carry a transaction id. They
    are deleted like any other, with the id recorded in the audit entry so the
    reference isn't lost silently.
    """
    from .models_db import ChargeRow, AuditRow, DeletedTxnRow

    q = db.query(ChargeRow).filter(ChargeRow.company == company,
                                   ChargeRow.status == "excluded")
    id_list = [i for i in ids.split(",") if i.strip()]
    if id_list:
        q = q.filter(ChargeRow.charge_id.in_(id_list))
    rows = q.all()

    # Counted for the audit note only. Nothing here calls QuickBooks: an
    # excluded charge has no transaction behind it, so there is nothing to
    # delete there and nothing to ask about.
    stale = [r for r in rows if r.qbo_txn_id]

    deleted = 0
    for r in rows:
        from .usdates import us_date as _usd2
        note = f"${r.amount} {_usd2(r.txn_date)} {r.merchant[:60]}"
        if r.qbo_txn_id:
            # Predates the rule that a posted charge can't be excluded. Noted
            # rather than blocked, so the id survives in the log.
            note += f" (carried a stale {_txn_word(r.qbo_txn_type)} id {r.qbo_txn_id})"
        db.add(AuditRow(company=company, action="charge_purged",
                        charge_id=r.charge_id, detail=note,
                        dry_run=False, actor=admin.username))
        # REMEMBERED, so the nightly pull cannot bring it back. Only the
        # provider's own id: enough to recognize it, and nothing about the
        # charge itself kept after somebody asked for it to be gone.
        if r.source_txn_id:
            db.merge(DeletedTxnRow(company=r.company, source=r.source,
                                   source_txn_id=r.source_txn_id,
                                   deleted_by=admin.username))
        db.delete(r)
        deleted += 1
        if deleted % 300 == 0:
            db.commit()
    db.commit()
    return {"deleted": deleted, "company": company,
            "stale_refs": len(stale)}


@app.get("/audit/log/{company}")
def audit_log(company: str, limit: int = Query(200), offset: int = Query(0),
              actor: str = Query(""), action: str = Query(""),
              group: str = Query(""),
              date_from: str = Query(""), date_to: str = Query(""),
              card: str = Query(""), vendor: str = Query(""),
              category: str = Query(""), profile: str = Query(""),
              last4: str = Query(""), txn: str = Query(""),
              qbo_id: str = Query(""),
              q: str = Query(""),
              companies: str = Query(""),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_reviewer)):
    """Every consequential action, newest first.

    Readable by anyone signed in on purpose: an audit trail only one person can
    see isn't much of a control.
    """
    from datetime import datetime as _dt
    from sqlalchemy import or_ as _or, func
    from .models_db import AuditRow
    # Imported at the TOP of the function. The group filter below reads it, and
    # the only import was further down beside the row builder -- so Python
    # treated `_al` as a local assigned later and the filter raised
    # UnboundLocalError on every request that used it.
    from . import audit_labels as _al

    # "*" is EVERY company: the dashboard's Audit Log, which belongs to no one
    # company. A named company still sees only its own rows.
    # ALL COMPANIES: every company this person can reach (or those ticked in
    # the Companies picker) -- unlike "*", which is the Dashboard's own log.
    if company == ALL_COMPANIES:
        qy = db.query(AuditRow).filter(
            AuditRow.company.in_(_reachable(db, user, companies)))
    else:
        qy = (db.query(AuditRow) if company == "*"
              else db.query(AuditRow).filter(AuditRow.company == company))
    # THE MULTI-SELECT FILTERS arrive as comma-separated lists.
    #
    # One value or twenty, the query is the same shape: an IN over whatever was
    # ticked. Splitting here rather than at each use means "one action" and
    # "six actions" are not two different code paths that can disagree.
    def _many(v):
        return [x.strip() for x in str(v or "").split(",") if x.strip()]

    if actor:
        # Exact now, not `contains`: these come from a tick list of real user
        # names, so a substring match would be a way to match names nobody
        # chose -- "jo" quietly pulling in "jones".
        qy = qy.filter(AuditRow.actor.in_(_many(actor)))
    if action:
        qy = qy.filter(AuditRow.action.in_(_many(action)))
    if group:
        # A group is a set of actions, resolved here rather than stored on the
        # row: the grouping is a presentation decision and putting a copy of it
        # in the database would mean migrating every time it changed.
        want = set(_many(group))
        codes = [k for k, (g, _) in _al.ACTIONS.items() if g in want]
        qy = qy.filter(AuditRow.action.in_(codes) if codes else _false())
    if date_from:
        try:
            qy = qy.filter(AuditRow.at >= _dt.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            qy = qy.filter(AuditRow.at <= _dt.fromisoformat(date_to + "T23:59:59"))
        except ValueError:
            pass
    if q:
        like = f"%{q}%"
        # EVERY COLUMN THE GRID SHOWS, not the three the audit row happens to
        # hold. Seventeen columns are on screen and typing a vendor name into
        # the search box found nothing, because vendor lives on the CHARGE --
        # so the search looked like it was broken rather than scoped.
        #
        # The charge-side columns are matched through a subquery, the same way
        # the vendor and card filters below do it, so a row with no charge
        # behind it (a settings change, a sync) is excluded cleanly rather than
        # dropped by a join it was never eligible for.
        from .models_db import ChargeRow as _CQ, SourceAccountRow as _SQ
        _srcs = [r.source for r in db.query(_SQ)
                 .filter(_SQ.company == company).all()
                 if q.strip().lower() in
                 ((r.nickname or r.qbo_account_name or r.source) or "").lower()]
        _charges = db.query(_CQ.charge_id).filter(
            _CQ.company == company,
            _or(_CQ.coded_vendor.ilike(like),
                _CQ.suggested_vendor.ilike(like),
                _CQ.coded_category.ilike(like),
                _CQ.suggested_category.ilike(like),
                _CQ.merchant.ilike(like),
                _CQ.cardholder_name.ilike(like),
                _CQ.card_last4.ilike(like),
                _CQ.source_txn_id.ilike(like),
                _CQ.qbo_txn_id.ilike(like),
                _CQ.source.in_(_srcs) if _srcs else _false()))

        # The action's LABEL, not its code: the grid shows "Coded" and the row
        # stores "queue_code", so searching for what is on screen matched
        # nothing.
        _codes = [k for k, (_g, lab) in _al.ACTIONS.items()
                  if q.strip().lower() in lab.lower()]
        _groups = [k for k, (g, _l) in _al.ACTIONS.items()
                   if q.strip().lower() in
                   dict(_al.GROUPS).get(g, "").lower()]

        qy = qy.filter(_or(AuditRow.detail.ilike(like),
                           AuditRow.charge_id.ilike(like),
                           AuditRow.bill_id.ilike(like),
                           AuditRow.actor.ilike(like),
                           AuditRow.action.in_(_codes + _groups)
                           if (_codes or _groups) else _false(),
                           AuditRow.charge_id.in_(_charges)))

    # THE CHARGE FILTERS.
    #
    # The audit row itself holds only a charge_id, so filtering on vendor or
    # card account means asking which charges match and restricting to those.
    # Done as one subquery rather than a join, because an audit row with no
    # charge -- a settings change, a sync -- must still be excluded cleanly
    # rather than dropped by an inner join it was never eligible for.
    from .models_db import ChargeRow as _CRF, SourceAccountRow as _SAF
    # Vendor and category are tick lists now, so they match exactly and allow
    # several. Profile, last four and the two ids are still free text, where a
    # substring match is what a person typing half an id actually wants.
    _lists = [(vendor, _CRF.coded_vendor), (category, _CRF.coded_category)]
    _text = [(profile, _CRF.cardholder_name), (last4, _CRF.card_last4),
             (txn, _CRF.source_txn_id), (qbo_id, _CRF.qbo_txn_id)]
    _need = [(v, col) for v, col in _text if v and v.strip()]
    _picked = [(v, col) for v, col in _lists if v and v.strip()]
    if _need or _picked or (card and card.strip()):
        cq = db.query(_CRF.charge_id).filter(_CRF.company == company)
        for v, col in _need:
            cq = cq.filter(col.ilike(f"%{v.strip()}%"))
        for v, col in _picked:
            cq = cq.filter(col.in_(_many(v)))
        if card and card.strip():
            # Matched on the NICKNAME a person sees, not the internal source
            # key -- "Divvy (Credit)" is what the column shows, and filtering
            # on `divvy` would be filtering on something not on screen.
            wanted = {c.lower() for c in _many(card)}
            srcs = [r.source for r in db.query(_SAF).filter(
                _SAF.company == company).all()
                if ((r.nickname or r.qbo_account_name or r.source)
                    or "").lower() in wanted]
            cq = cq.filter(_CRF.source.in_(srcs) if srcs else _false())
        qy = qy.filter(AuditRow.charge_id.in_(cq.subquery().select()))

    total = qy.count()
    # limit=0 means "all of it" -- the export asks for the whole filtered log.
    _q = qy.order_by(AuditRow.at.desc()).offset(offset)
    rows = (_q.limit(limit).all() if limit else _q.all())

    actors = [r[0] for r in db.query(AuditRow.actor).filter(
        AuditRow.company == company).distinct().all() if r[0]]
    # Code AND label, from the whole log rather than the page. The dropdown
    # used to be built client-side from the rows on screen, which cannot offer
    # anything the current filter has already excluded -- so narrowing once
    # emptied the list you would narrow with next.
    _codes = sorted({r[0] for r in db.query(AuditRow.action).filter(
        AuditRow.company == company).distinct().all() if r[0]})
    actions = sorted(({"code": c, "label": _al.label_for(c)} for c in _codes),
                     key=lambda a: a["label"])

    # One lookup for the page rather than one per row.
    from .models_db import BillRow as _BR
    _ids = {r.bill_id for r in rows if r.bill_id}
    bill_no = ({b.bill_id: (b.doc_number or "")
                for b in db.query(_BR).filter(_BR.bill_id.in_(_ids)).all()}
               if _ids else {})
    # One query for the page, not one per row.
    # ChargeRow and SourceAccountRow are NOT module-level names in this file --
    # every endpoint that needs them imports them locally. Three separate
    # NameErrors in this session have come from assuming otherwise.
    from .models_db import (ChargeRow as _CR4, SourceAccountRow as _SA4)
    _ids = {r.charge_id for r in rows if r.charge_id}
    charges = {c.charge_id: c for c in db.query(_CR4).filter(
        _CR4.charge_id.in_(list(_ids))).all()} if _ids else {}
    # Card names by (company, card): in Multi-Company each entry's card is
    # named from its own company.
    _nq = (db.query(_SA4) if company in (ALL_COMPANIES, "*")
           else db.query(_SA4).filter(_SA4.company == company))
    nick = {(s_.company, s_.source): (s_.nickname or s_.qbo_account_name or s_.source)
            for s_ in _nq.all()}

    return {
        "company": company, "total": total, "offset": offset, "limit": limit,
        # Code and label both: the filter sends the code, the dropdown shows
        # the label. Sending labels only meant the client had to map back.
        "groups": [{"code": c, "label": g} for c, g in _al.GROUPS],
        # `actions` is a list of {code, label} dicts and is ALREADY ordered by
        # label above. It used to be a list of bare strings, and the sorted()
        # that was right for those raised TypeError the moment two dicts had to
        # be compared -- so the whole endpoint 500'd, and the audit screen sat
        # on "Loading…" forever.
        "actors": sorted(actors), "actions": actions,
        "entries": [{
            "at": r.at.isoformat(),
            "at_utc": r.at.isoformat() + "Z",
            "action": r.action, "actor": r.actor, "company": r.company or "",
            # Plain English and a group, from the one table that names all of
            # them. The screen had wording for 17 of 77 actions, so most rows
            # read as raw keys like `bill_split_incomplete`.
            "action_label": _al.label_for(r.action),
            "action_group": _al.group_label(_al.group_for(r.action)),
            "charge_id": r.charge_id or "", "bill_id": r.bill_id or "",
            # What the entry was ABOUT, so the log can be read without opening
            # each row against the grid. Empty for settings and integration
            # entries, which are not about a charge.
            **(lambda c: {
                "txn_id": (c.source_txn_id if c else ""),
                "qbo_txn_id": (c.qbo_txn_id if c else ""),
                "card_account": (nick.get((c.company, c.source), c.source) if c else ""),
                "card_last4": (c.card_last4 or "") if c else "",
                "date": (c.txn_date.isoformat() if c and c.txn_date else ""),
                "vendor": (c.coded_vendor or "") if c else "",
                "category": (c.coded_category or "") if c else "",
                "bank_detail": (c.merchant or c.raw_description or "") if c else "",
                "profile": (c.cardholder_name or "") if c else "",
                "amount": (str(c.amount) if c and c.amount is not None else ""),
                "kind": ("" if not c else
                         ("Expense" if not c.is_credit
                          else "Payment" if c.is_card_payment else "Refund")),
            })(charges.get(r.charge_id or "")),
            # The document number a person would recognize, alongside the
            # QuickBooks id. The column used to show only the id under a
            # heading reading "Bill", which looked like the bill number.
            "bill_no": bill_no.get(r.bill_id or "", ""),
            # Cleaned HERE rather than on screen, so the grid and the export
            # read identically. The client used to strip the user with its own
            # regex, which meant two implementations of one rule and only one
            # of them applied to the spreadsheet.
            # humanize FIRST, then strip the actor: the legacy note ends
            # "...} by jsmith", so taking the name off before the dict is
            # translated would leave the dict and lose the name.
            # Blank unless it records a change from one value to another --
            # see audit_labels.worth_showing. Applied on the way out so the
            # grid and the export agree without either deciding for itself.
            # EXCEPT on the dashboard ("*"), which has no Vendor, Amount or
            # QBO columns for the detail to repeat -- there it is the only
            # place the information lives, so it is served whole. A company's
            # log is unchanged.
            "detail": (_al.strip_actor(_al.humanize(r.detail or ""),
                                       r.actor or "", company)
                       if company == "*" else
                       _al.worth_showing(r.action or "", _al.strip_actor(
                           _al.humanize(r.detail or ""), r.actor or "",
                           company))),
            "dry_run": r.dry_run,
        } for r in rows],
    }


@app.post("/queue/memo")
def set_memo(charge_id: str = Form(...), memo: str = Form(""),
             db: Session = Depends(get_session),
             user: UserRow = Depends(require_write)):
    """Edit the memo that will be written to QuickBooks."""
    from .models_db import ChargeRow, AuditRow
    row = db.get(ChargeRow, charge_id)
    if row is None:
        raise HTTPException(404, "no such charge")
    if row.qbo_txn_id:
        raise HTTPException(
            409, "this charge is already posted — undo it first to change the memo")
    before = row.memo or ""
    row.memo = memo.strip()
    db.add(AuditRow(company=row.company, action="memo_edited",
                    charge_id=charge_id,
                    detail=f"memo: '{before or '(blank)'}' changed to "
                           f"'{row.memo or '(blank)'}'", dry_run=True,
                    actor=user.username))
    db.commit()
    return {"charge_id": charge_id, "memo": row.memo}


@app.get("/queue/{company}/version")
def queue_version(company: str, status: str = Query("for_review"),
                  companies: str = Query(""),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """Cheap poll: counts plus the latest resolution time.

    The UI hits this every few seconds. Returning a fingerprint rather than the
    rows keeps it light enough to poll while several people work at once.
    """
    from sqlalchemy import func
    from .models_db import ChargeRow
    # All Companies: the same fingerprint across every company reachable.
    _cos = _reachable(db, user, companies) if company == ALL_COMPANIES else [company]
    counts = dict(db.query(ChargeRow.status, func.count(ChargeRow.charge_id))
                  .filter(ChargeRow.company.in_(_cos))
                  .group_by(ChargeRow.status).all())
    latest = db.query(func.max(ChargeRow.resolved_at)).filter(
        ChargeRow.company.in_(_cos)).scalar()
    return {"for_review": counts.get("for_review", 0),
            "categorized": counts.get("categorized", 0),
            "excluded": counts.get("excluded", 0),
            "latest_change": latest.isoformat() if latest else ""}


def _dt_from(v):
    """A date string as the first instant of that day, in UTC."""
    from datetime import datetime as _dt, date as _d
    try:
        return _dt.combine(_d.fromisoformat(str(v)[:10]), _dt.min.time()) if v else None
    except ValueError:
        return None


def _dt_to(v):
    """...and as the last, so a single-day range includes the whole day."""
    from datetime import datetime as _dt, date as _d, timedelta as _td
    try:
        if not v:
            return None
        return _dt.combine(_d.fromisoformat(str(v)[:10]), _dt.min.time()) + _td(days=1)
    except ValueError:
        return None


def _was(before: dict) -> str:
    """Where the charge stood before this action, in words.

    "was on For Review, uncoded" said the same thing twice: everything on For
    Review is uncoded, that is what the tab means. The only cases worth a
    sentence are the ones that are NOT obvious -- a charge that already had a
    suggestion, or one that was already categorized and is being changed. When
    the previous state is exactly what the tab implies, this returns nothing
    and the action's own name ("Coded", "Excluded") is the whole entry.
    """
    tabs = {"for_review": "For Review", "categorized": "Categorized",
            "excluded": "Excluded", "bills": "Bills"}
    status = before.get("status") or ""
    res = before.get("resolution") or ""
    cat = before.get("category") or ""
    bill = before.get("bill") or ""

    if status == "for_review" and not res and not cat and not bill:
        return ""                       # the ordinary case: nothing to add
    if status == "for_review":
        # It had a suggestion sitting on it, which is worth knowing: this
        # action either accepted that suggestion or overrode it.
        return f"was suggested as {cat}" if cat else "was on For Review"

    bits = ["was already " + (tabs.get(status, status) or "somewhere else").lower()]
    if res == "matched" and bill:
        bits.append(f"matched to bill {bill}")
    elif cat:
        bits.append(f"as {cat}")
    return ", ".join(bits)


def _txn_word(kind: str) -> str:
    """QuickBooks' entity name in the words this app uses everywhere else.

    "Purchase" and "BillPayment" are the API's vocabulary and appear nowhere on
    any screen, so showing them asked the reader to know QuickBooks internals
    to understand a sentence about their own transaction.
    """
    from .integrations.qbo_write import entity_word
    return entity_word(kind)


def _eq_or_none(column, value):
    """`column == value`, or a condition matching NOTHING when value is empty.

    The empty case used to be written as `column == "\\x00"` -- a realm id no
    row could hold. SQLite stores that happily; PostgreSQL refuses a NUL byte
    in a string literal outright, so on the deployed database every one of
    these raised "A string literal cannot contain NUL (0x00) characters" and
    the screen it was on failed to load.

    A company with no QuickBooks file mapped must see no accounts, which is
    what `false()` says without smuggling an impossible value through the
    driver.
    """
    from sqlalchemy import false as _f
    return column == value if value else _f()


def _false():
    """A condition that matches nothing.

    `sqlalchemy.false()` rather than `1 = 0` so the dialect writes whatever it
    prefers; wrapped so the call sites read as what they mean.
    """
    from sqlalchemy import false as _f
    return _f()


def _as_naive(v):
    """ISO-with-Z to a naive datetime, so openpyxl writes a real timestamp."""
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(str(v).replace("Z", "")) if v else None
    except ValueError:
        return None


def _as_date(v):
    """ISO string to a real date, so Excel formats and sorts it as one."""
    from datetime import date as _dd
    try:
        return _dd.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return v


def _last_edit_words(it) -> str:
    """The most recent post-coding edit as one readable cell.

    Newest, not the whole list: the grid's hover shows every one, and a cell
    holding eight of them is unreadable and unsortable. The count in the column
    beside says how many there were.
    """
    ev = it.get("later_edits") or []
    if not ev:
        return ""
    e = ev[-1]
    from datetime import datetime as _dt
    # The SAME clock as the Time Stamp column beside it. This used Central
    # while every other stamp in the workbook is written as the naive UTC value
    # the rest of the exports carry -- so one row read 12:00 in one column and
    # 07:00 in the next, for the same event.
    try:
        when = _dt.fromisoformat(
            str(e.get("at", "")).replace("Z", "")).strftime("%-m/%-d/%Y %-I:%M %p")
    except ValueError:
        when = str(e.get("at", ""))
    bits = [when, e.get("actor") or "system", e.get("what") or ""]
    line = " — ".join(b for b in bits if b)
    return f"{line}: {e['detail']}" if e.get("detail") else line


def _fmt_central_ts(dt):
    """UTC stamp as Chicago wall time — the team reads it as local either way."""
    from .export_xlsx import fmt_central
    return fmt_central(dt)


def _multi_export(fn, kw: dict, prefix: str):
    """An export in Multi-Company: the same export for every company this
    person can reach (or those ticked in the Companies picker), combined into
    one workbook with a Company column first, named Multi-Company."""
    from .export_xlsx import _CAPTURE, build_workbook, filename
    db, user = kw["db"], kw["user"]
    merged, rows = None, []
    for co in _reachable(db, user, kw.get("companies") or ""):
        cap = []
        tok = _CAPTURE.set(cap)
        try:
            fn(**{**kw, "company": co, "companies": ""})
        except HTTPException:
            continue
        finally:
            _CAPTURE.reset(tok)
        if not cap:
            continue
        merged = merged or cap[-1]
        rows += [[co] + list(r) for r in cap[-1]["rows"]]
    if merged is None:
        raise HTTPException(404, "Nothing to export")
    sh = lambda cols: [c + 1 for c in cols]
    blob = build_workbook(merged["title"], ["Company"] + merged["headers"], rows, merged["filters"],
                          date_cols=sh(merged["date_cols"]), money_cols=sh(merged["money_cols"]),
                          stamp_cols=sh(merged["stamp_cols"]), pct_cols=sh(merged["pct_cols"]))
    return Response(content=blob,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename(prefix, ALL_COMPANIES)}"'})


@app.get("/export/bills/{company}")
def export_bills(company: str, q: str = Query(""), vendor: str = Query(""),
                 memo: str = Query(""), date_from: str = Query(""),
                 date_to: str = Query(""), amt_min: str = Query(""),
                 amt_max: str = Query(""), bal_min: str = Query(""),
                 bal_max: str = Query(""), aging: str = Query(""),
                 qbo_id: str = Query(""), sort: str = Query("date"),
                 direction: str = Query("desc"),
                 companies: str = Query(""),
                 klass: str = Query(""), scope: str = Query("open"),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Open bills matching the current filters, all of them (every bill with
    `scope=all`)."""
    if company == ALL_COMPANIES:
        return _multi_export(export_bills, dict(locals()), "bills")
    from .export_xlsx import build_workbook, filename
    # EVERY argument list_bills takes, passed explicitly.
    #
    # This export was CRASHING on every call. list_bills is an endpoint as well
    # as a function, so its unpassed parameters keep their FastAPI `Query("")`
    # default -- and calling `.strip()` on a Query object raises. `aging` and
    # `qbo_id` were the two the Bills grid sends and this signature never
    # declared, so the moment somebody set an aging or bill-number filter and
    # pressed Export they got a 500; and because `qbo_id` is read
    # unconditionally, the export failed even with no filters set at all.
    #
    # The four charge-only parameters are passed as empty strings rather than
    # left to default, for the same reason: a sentinel that reaches a string
    # operation is a crash waiting for whoever adds the next filter.
    data = list_bills(company=company, limit=0, offset=0, q=q, vendor=vendor,
                      memo=memo, date_from=date_from, date_to=date_to,
                      amt_min=amt_min, amt_max=amt_max, bal_min=bal_min,
                      bal_max=bal_max, aging=aging, qbo_id=qbo_id,
                      samt_min="", samt_max="", drift="", rules_applied="",
                      klass=klass, scope=scope,
                      sort=sort, direction=direction,
                      db=db, user=user)
    # THE GRID'S COLUMNS, all of them. Aging and QBO Txn ID are on the Bills
    # tab and were missing here, so the spreadsheet somebody exports to chase
    # overdue bills arrived without the ageing, and the id needed to find one
    # in QuickBooks was absent too.
    #
    # Aging goes out as a NUMBER of days as well as the label: "31-60" cannot
    # be sorted or filtered numerically, which is most of what a bills
    # spreadsheet gets used for.
    headers = ["Bill No.", "Vendor", "Date", "Amount", "Balance", "Memo", "Class",
               "Aging", "Days Past Due", "QBO Txn ID"]
    rows = [[b["bill"], b["vendor"], _as_date(b["date"]), float(b["amt"] or 0),
             float(b["balance"] or 0), b["memo"], b.get("class") or "", b.get("aging") or "",
             (b.get("aging_days") if b.get("aging_days") is not None else None),
             b.get("bill_id") or ""] for b in data["bills"]]
    applied = [("Company", company), ("Tab", "All Bills" if scope == "all" else "Open Bills"),
               ("Vendor", vendor or "—"), ("Memo", memo or "—"),
               ("Bill no. / search", q or "—"),
               ("Date from", date_from or "—"), ("Date to", date_to or "—"),
               ("Amount min", amt_min or "—"), ("Amount max", amt_max or "—"),
               ("Balance min", bal_min or "—"), ("Balance max", bal_max or "—"),
               ("Aging", aging or "—"), ("QBO Bill Id", qbo_id or "—"),
               ("Sorted by", f"{sort} {direction}"),
               ("Exported by", user.username)]
    blob = build_workbook("Bills", headers, rows, applied,
                          date_cols=[2], money_cols=[3, 4])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("bills", company)}"'})


@app.get("/export/audit/{company}")
def export_audit(company: str, action: str = Query(""), actor: str = Query(""),
                 group: str = Query(""),
                 date_from: str = Query(""), date_to: str = Query(""),
                 card: str = Query(""), vendor: str = Query(""),
                 category: str = Query(""), profile: str = Query(""),
                 last4: str = Query(""), txn: str = Query(""),
                 qbo_id: str = Query(""),
                 q: str = Query(""),
                 companies: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """The audit log matching the current filters."""
    if company == ALL_COMPANIES:
        return _multi_export(export_audit, dict(locals()), "audit")
    from .export_xlsx import build_workbook, filename, fmt_central
    from . import audit_labels as _al
    # EVERY filter the dialog offers, passed straight through. The export took
    # four of them, so exporting a log narrowed by vendor or card account
    # quietly produced the unfiltered log instead -- a spreadsheet that does
    # not match the screen it came from is worse than no spreadsheet.
    data = audit_log(company=company, limit=0, offset=0, action=action,
                     actor=actor, group=group,
                     date_from=date_from, date_to=date_to,
                     card=card, vendor=vendor, category=category,
                     profile=profile, last4=last4, txn=txn, qbo_id=qbo_id,
                     q=q, db=db, user=user)
    # The charge behind each entry, so the export can say WHAT was acted on
    # rather than only that something was. One query for the whole page rather
    # than one per row.
    from .models_db import (ChargeRow as _CR2, BillRow as _BR2,
                            SourceAccountRow as _SA3)
    ids = {e.get("charge_id") for e in data["entries"] if e.get("charge_id")}
    charges = {c.charge_id: c for c in db.query(_CR2).filter(
        _CR2.charge_id.in_(list(ids))).all()} if ids else {}
    bids = {e.get("bill_id") for e in data["entries"] if e.get("bill_id")}
    bills = {b.bill_id: b for b in db.query(_BR2).filter(
        _BR2.bill_id.in_(list(bids))).all()} if bids else {}
    nick = {s_.source: (s_.nickname or s_.qbo_account_name or s_.source)
            for s_ in db.query(_SA3).filter(_SA3.company == company).all()}

    def _kind(c):
        if c is None:
            return ""
        if not c.is_credit:
            return "Expense"
        return "Payment" if c.is_card_payment else "Refund"

    # ORDER AND NAMES MATCH THE SCREEN, column for column.
    #
    # Group before Action because the group is the coarser question -- you ask
    # "was this a coding change?" before "which one" -- and reading it the
    # other way round meant scanning 77 action names to work out the handful of
    # families they fall into. "Action Group" and "Action Details" rather than
    # "Group" and "Action", because "Group" beside a transaction row reads as a
    # grouping OF transactions.
    #
    # The three identifiers sit at the END. They are what you copy when you
    # already know which row you want, not what you scan to find it, and three
    # opaque id columns between the action and the money pushed everything a
    # person actually reads off the first screen.
    headers = ["Time Stamp", "User", "Action Group", "Action Details",
               "Transaction Type", "Card Account", "Date", "Vendor",
               "Category", "Bank Detail", "Profile", "Amount", "CC Last 4/5",
               "Additional Details", "Platform TXN ID", "QBO TXN ID", "Bill No."]
    rows = []
    for e in data["entries"]:
        c = charges.get(e.get("charge_id") or "")
        b = bills.get(e.get("bill_id") or "")
        rows.append([
            _as_naive(e.get("at_utc") or e.get("at", "")),
            e.get("actor", ""),
            _al.group_label(_al.group_for(e.get("action", ""))),
            _al.label_for(e.get("action", "")),
            _kind(c),
            nick.get(c.source, c.source) if c is not None else "",
            # A real date, not its ISO text. Written as a string it ignored the
            # m/d/yyyy number format and sorted alphabetically, so 12/1 came
            # before 2/1 in a column that looked like dates.
            (c.txn_date if c is not None else None),
            (c.coded_vendor or "") if c is not None else "",
            (c.coded_category or "") if c is not None else "",
            (c.merchant or c.raw_description or "") if c is not None else "",
            (c.cardholder_name or "") if c is not None else "",
            (float(c.amount) if c is not None and c.amount is not None else None),
            # Beside the amount, where a person checking a charge against a
            # statement is already looking.
            (c.card_last4 or "") if c is not None else "",
            _al.worth_showing(e.get("action", ""), _al.strip_actor(
                _al.humanize(e.get("detail", "")), e.get("actor", ""), company)),
            # The source's own transaction id, NOT the engine's compound key --
            # the compound key begins with the company name, which the column
            # beside it already says.
            (c.source_txn_id if c is not None else "")
            or (e.get("charge_id") or "").split(":")[-1],
            (c.qbo_txn_id if c is not None else ""),
            # doc_number is QuickBooks' "Bill no." -- there is no bill_no
            # attribute, and reading one would have thrown on every export.
            (b.doc_number if b is not None else "") or (e.get("bill_id") or ""),
        ])
    applied = [("Company", company), ("Tab", "Audit log"),
               ("Action group", _al.group_label(group) if group
                else "All action groups"),
               ("Action", _al.label_for(action) if action else "All actions"),
               ("User", actor or "All users"),
               ("Date from", date_from or "—"), ("Date to", date_to or "—"),
               ("Card account", card or "—"), ("Vendor", vendor or "—"),
               ("Category", category or "—"), ("Profile", profile or "—"),
               ("CC Last 4/5", last4 or "—"),
               ("Platform TXN ID", txn or "—"), ("QBO Txn ID", qbo_id or "—"),
               ("Quick search", q or "—"),
               ("Exported by", user.username)]
    # CC Last 4/5 sits at 12, so Detail is 13 and the three ids run 14-16. The
    # money column did not move.
    blob = build_workbook("Audit log", headers, rows, applied, stamp_cols=[0],
                          date_cols=[6], money_cols=[11])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("audit", company)}"'})


@app.get("/export/queue/{company}")
def export_queue(company: str, status: str = Query("for_review"),
                 card: str = Query(""), txn_type: str = Query(""),
                 kinds: str = Query(""), tier: str = Query(""),
                 date_from: str = Query(""), date_to: str = Query(""),
                 amt_min: str = Query(""), amt_max: str = Query(""),
               samt_min: str = Query(""), samt_max: str = Query(""),
               drift: str = Query(""),
               rules_applied: str = Query(""),
                 vendor: str = Query(""), profile: str = Query(""),
                 last4: str = Query(""), memo: str = Query(""),
                 cvendor: str = Query(""), category: str = Query(""),
                 user_f: str = Query("", alias="user"),
                 ts_from: str = Query(""), ts_to: str = Query(""),
                 qbo_id: str = Query(""),
                 q: str = Query(""),
                 sort: str = Query("date"), direction: str = Query("desc"),
                 columns: str = Query(""),
                 companies: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """The whole filtered grid as a spreadsheet, page size ignored.

    `columns` is the visible column keys in the order the person arranged them,
    so the export matches the screen rather than some canonical layout.
    """
    if company == ALL_COMPANIES:
        return _multi_export(export_queue, dict(locals()), status)
    from .review import build_queue
    from .export_xlsx import build_workbook, filename, fmt_central
    from datetime import date as _date, datetime as _dt
    from decimal import Decimal as _D, InvalidOperation

    def _d(v):
        try:
            return _date.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v not in ("", None) else None
        except (InvalidOperation, TypeError):
            return None

    # limit=0 means every matching row: the page size is a screen constraint,
    # not part of what was asked for.
    # Saved scores: the rescoring's results, not millions of comparisons again.
    data = build_queue(db, company, status=status, limit=0, offset=0, stored_scores=True,
                       card=card or None, date_from=_d(date_from),
                       date_to=_d(date_to), amt_min=_m(amt_min),
                       amt_max=_m(amt_max), vendor=vendor or None,
                       profile=profile or None, last4=last4 or None,
                       memo=memo or None, cvendor=cvendor or None,
                       txn_type=txn_type or None, kinds=kinds or None,
                       tier=tier or None, category=category or None,
                       user=user_f or None, qbo_id=qbo_id or None,
                       ts_from=_dt_from(ts_from), ts_to=_dt_to(ts_to),
                       q=q or None,
                       sort=sort, direction=direction)

    # Every column the grid can show, in the grid's own order. The export used
    # to follow the VISIBLE columns, so anything hidden -- or anything added to
    # the grid but never given an entry here, which is what happened to the
    # QuickBooks id -- was silently absent from the spreadsheet. A spreadsheet
    # is where the hidden columns are wanted most.
    from . import export_rich as _rich
    LABELS = {"card": "Card Account", "date": "Date", "cven": "Vendor",
              "ccat": "Category", "ven": "Bank Detail", "prof": "Profile",
              "amt": "Amount", "l4": "CC Last 4/5", "memo": "QBO Memo", "note": "Custom Notes",
              "ord": "Order #",
              "scur": "Source Currency", "samt": "Source Amount",
              "strength": "Strength", "strength_pct": "Strength %",
              "kind": "Transaction Type", "details": "Additional Details",
              "user": "User", "ts": "Time Stamp", "qbo": "QBO Txn ID",
              # These two are on the grid and were missing here, so a
              # spreadsheet exported to hunt for duplicate transaction ids
              # arrived without the transaction id. The assertion below now
              # makes that a startup failure rather than a silent gap.
              "txnid": "Platform TXN ID", "upts": "Ingestion Time Stamp",
              "ingest": "Ingestion Method",
              # The STH comparison, as values. A spreadsheet cannot carry the
              # tick and the cross, but the figures behind them are the point --
              # a variance column is what somebody sorts on to find the plans
              # that are out.

              # WHAT THE RED ! ON THE GRID SAYS, as values.
              #
              # A tooltip cannot survive the trip to a spreadsheet, and the
              # marker's whole point is that the User and Time Stamp columns
              # beside it are not the whole story -- so the export would repeat
              # the same half-answer the grid used to give. A count and the
              # most recent edit are what a person filters and sorts on.
              "edited": "Edited After Coding", "last_edit": "Last Edit",
              "split": "Split Lines"}

    # Every column key the queue grid can show. Kept beside LABELS so the two
    # are read together, and checked rather than trusted: a column added to the
    # grid and forgotten here vanishes from the export with no error anywhere,
    # which is how TXN ID went missing.
    def _yn(v):
        return "" if v is None else ("Yes" if v else "No")

    GRID_KEYS = {"card", "date", "cven", "ccat", "ven", "prof", "amt", "l4",
                 "memo", "ord", "scur", "samt", "txnid", "upts", "status",
                 "kind", "details", "user", "ts", "qbo", "ingest"}
    _absent = GRID_KEYS - set(LABELS)
    if _absent:
        log.error("export/queue is missing columns the grid shows: %s",
                  sorted(_absent))

    def kind_of(it):
        # ONE word, not "Refund/Payment".
        #
        # A credit is either money coming back from a merchant or a payment
        # against the card, and the engine already knows which -- the grid
        # colors them differently. A cell reading "Refund/Payment" makes the
        # column unfilterable and unpivotable, which is most of what a
        # spreadsheet is for.
        if it["resolution"] == "matched":
            return "Bill Payment"
        if not it["is_credit"]:
            return "Expense"
        return "Payment" if it.get("is_card_payment") else "Refund"

    def details_of(it):
        # Same three cases as the More Details column on screen: the bill for a
        # payment, the STH season-ticket account for a charge added against one,
        # and nothing for anything else.
        if it["resolution"] == "matched":
            parts = [f"Bill {it['matched_bill_no']}" if it["matched_bill_no"] else "",
                     it["matched_bill_vendor"], it["matched_bill_date"],
                     it["matched_bill_memo"]]
            return "  ·  ".join(p for p in parts if p)
        h = it.get("hal") or {}
        if h:
            # The COLORED STH line, in this column rather than a separate one.
            # It is the same content the screen shows here, so a reader
            # comparing the two is looking at one column in both places -- and
            # the six flat STH columns it replaced put the same facts at the
            # far right of the sheet, where nobody joined them back up.
            return _rich.hal_cell(h, bool(it.get("is_credit")))
        return ""

    VALUES = {
        "card": lambda it: it["account"],
        "cven": lambda it: (it["matched_bill_vendor"] if it["resolution"] == "matched"
                            else it["coded_vendor"]),
        "date": lambda it: _as_date(it["date"]),
        # Signed, so a refund reads as money back rather than another charge.
        "amt": lambda it: -float(it["amt"] or 0) if it["is_credit"] else float(it["amt"] or 0),
        "ven": lambda it: it["merchant"],
        "prof": lambda it: it["holder"],
        "l4": lambda it: it["card"],
        "memo": lambda it: it["memo"],
        "note": lambda it: it.get("custom_note") or "",
        "kind": kind_of,
        "ccat": lambda it: ("Bill Payment" if it["resolution"] == "matched"
                            else it["coded_category"] or it.get("suggested_category", "")),
        # Two columns, not one string. "strong 78%" cannot be sorted, filtered
        # or averaged in Excel; a word and a number can.
        # Still the tier word, still sortable -- just colored the way the pill
        # is. The comment above stands: the word and the number stay separate
        # so both can be sorted, and color is added to the word rather than
        # merged into it.
        # The band's NAME, as edited ("Very Strong"), not its key ("Very_Strong").
        "strength": lambda it: _rich.strength_cell(
            next((b["name"] for b in __import__("app.bands", fromlist=["x"]).get()
                  if b["key"] == (it.get("tier") or "")),
                 "No Candidate" if (it.get("tier") or "") == "none"
                 else (it.get("tier") or "").replace("_", " ").title()),
            it.get("tier") or ""),
        "strength_pct": lambda it: (it["score"] / 100 if it.get("score") else None),
        "details": details_of,
        "user": lambda it: it.get("resolved_by", ""),
        # Exports carry a real datetime; Excel formats it in the reader's own
        # locale, which is the same principle as the grid.
        "ts": lambda it: _as_naive(it.get("resolved_at_utc", "")),
        "qbo": lambda it: it.get("qbo_txn_id", ""),
        "ord": lambda it: it.get("order_number") or "",
        "scur": lambda it: it.get("source_currency") or "",
        "samt": lambda it: (float(it["source_amount"])
                            if it.get("source_amount") else None),
        "split": lambda it: (it.get("split_lines") or 0) or None,
        # The source's own transaction id -- the field somebody exports the
        # grid to check for duplicates against, which is exactly what was
        # absent.
        "txnid": lambda it: it.get("txn_id") or "",
        "upts": lambda it: _as_naive(it.get("uploaded_at", "")),
        # ONE CELL, colored -- the tick, the day, the expected amount and the
        # variance are one statement about one plan, and splitting them into
        # columns loses that. The six flat STH columns below stay as well: a
        # colored cell reads well and sorts badly, and a variance column is
        # what somebody sorts on to find the plans that are out.
        # Yes / No / blank rather than true / false: a spreadsheet column of
        # TRUE and FALSE reads as data, and this is a verdict.
        "ingest": lambda it: {"csv": "CSV", "api": "API",
                              "plaid": "Plaid"}.get(
            it.get("ingest_method") or "csv", it.get("ingest_method") or ""),
        # Blank rather than 0 where nothing happened: a column of zeroes reads
        # as a measurement, and the answer here is "nothing to report".
        "edited": lambda it: (it.get("later_edit_count") or 0) or None,
        "last_edit": lambda it: _last_edit_words(it),
    }
    _no_value = set(LABELS) - set(VALUES) - {"strength", "strength_pct"}
    if _no_value:
        log.error("export/queue has labels with no value extractor: %s",
                  sorted(_no_value))

    # Everything, always. `columns` still arrives from the screen and is used
    # only to ORDER what the person has arranged; anything they have hidden is
    # appended rather than dropped. The old behavior exported the visible
    # columns only, which made the export strictly worse than the screen.
    wanted = [k for k in (columns.split(",") if columns else []) if k in LABELS]
    keys = wanted + [k for k in LABELS if k not in wanted]
    # STRENGTH AFTER TRANSACTION TYPE, matching the grid.
    #
    # The order here came from the order the LABELS dict happened to be
    # written in, which put strength in the middle of the coding columns. A
    # spreadsheet whose columns are in a different order from the screen makes
    # every comparison a hunt.
    for _k in ("strength_pct", "strength"):
        if _k in keys and "kind" in keys:
            keys.remove(_k)
            keys.insert(keys.index("kind") + 1, _k)
    # Columns that say nothing on this tab are left out rather than exported as
    # a column of blanks.
    # Only where there is a categorization for an edit to come after. On For
    # Review these would be two empty columns describing a state that has not
    # happened yet.
    if status != "categorized":
        keys = [k for k in keys if k not in ("edited", "last_edit")]
    if status == "for_review":
        # "details" is KEPT here now. It used to hold only the matched bill,
        # which is a Categorized concept, so on For Review it was a column of
        # blanks. It now carries the STH line -- which is the most-read thing on
        # this tab -- so dropping it would export the one screen where season
        # accounts matter without any of them.
        keys = [k for k in keys if k not in ("user", "ts", "qbo")]

    headers = [LABELS[k] for k in keys]
    rows = [[VALUES[k](it) for k in keys] for it in data["items"]]

    TAB = {"for_review": "For Review", "categorized": "Categorized",
           "excluded": "Excluded"}
    applied = [("Company", company), ("Tab", TAB.get(status, status)),
               ("Quick search", q or "—"),
               ("Card accounts", card or "All cards"),
               ("Transaction type", kinds or txn_type or "All"),
               ("Strength", tier or "All strengths"),
               ("Date from", date_from or "—"), ("Date to", date_to or "—"),
               ("Amount min", amt_min or "—"), ("Amount max", amt_max or "—"),
               ("Vendor", cvendor or "—"), ("Bank detail", vendor or "—"),
               ("Profile", profile or "—"), ("CC Last 4/5", last4 or "—"),
               ("QBO memo", memo or "—"),
               ("Sorted by", f"{sort} {direction}"),
               ("Exported by", user.username)]

    date_cols = [i for i, k in enumerate(keys) if k == "date"]
    stamp_cols = [i for i, k in enumerate(keys) if k == "ts"]
    money_cols = [i for i, k in enumerate(keys) if k in ("amt", "samt")]
    pct_cols = [i for i, k in enumerate(keys) if k == "strength_pct"]
    blob = build_workbook(TAB.get(status, "Export"), headers, rows, applied,
                          date_cols=date_cols, money_cols=money_cols,
                          stamp_cols=stamp_cols, pct_cols=pct_cols)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 # The tab, not "transactions" -- For Review and Categorized
                 # are different exports and a folder of them should say which
                 # is which.
                 f'attachment; filename="{filename(status, company)}"'})


def _convert_payment_to_expense(db, row, category: str, actor: str) -> dict:
    """Turn a matched bill payment into a coded expense, ledger included.

    QuickBooks has no way to change a BillPayment into a Purchase, so the
    payment is deleted and an expense posted in its place. The order matters:
    the deletion goes first, and only once QuickBooks confirms it does the
    charge get rewritten -- otherwise a failure would leave the app calling it
    an expense while the payment still sits in the ledger.
    """
    from .models_db import QboRefRow, ChargeRow
    from .integrations.qbo_write import (undo_post, push_charge, WriteBlocked,
                                         WriteFailed)

    was_posted = bool(row.qbo_txn_id)
    acct = _ref_by_name(db, row.company, "account", category)
    if acct is None and was_posted:
        raise HTTPException(
            400, f"'{category}' isn't in the QuickBooks chart of accounts. "
                 f"Pick an existing category, or refresh accounts first.")

    # The bill's vendor carries over as the expense's payee -- it's the best
    # answer available, and it's editable once the charge is an expense.
    vendor = (row.coded_vendor or row.matched_bill_vendor or "").strip()
    ven = _ref_by_name(db, row.company, "vendor", vendor) if vendor else None

    if was_posted:
        try:
            res = undo_post(db, row, actor)
        except (WriteBlocked, WriteFailed) as e:
            raise HTTPException(400, f"Could not remove the bill payment from "
                                     f"QuickBooks: {e}")
        if res.get("status") not in ("deleted", "nothing_posted"):
            raise HTTPException(
                400, f"Could not remove the bill payment from QuickBooks: "
                     f"{res.get('error', 'refused')}. Nothing was changed.")

    was_bill = row.matched_bill_no or row.matched_bill_id
    row.resolution = "coded"
    row.status = "categorized"
    row.coded_category = category
    row.coded_category_id = acct.qbo_id if acct else ""
    row.coded_vendor = vendor
    row.coded_vendor_id = ven.qbo_id if ven else ""
    row.matched_bill_id = ""
    row.matched_bill_no = row.matched_bill_vendor = ""
    row.matched_bill_date = row.matched_bill_memo = ""
    row.resolved_by, row.resolved_at = actor, datetime.utcnow()

    changed = [f"bill payment on bill {was_bill} recategorized as "
               f"expense '{category}'"]
    db.add(AuditRow(company=row.company, action="payment_to_expense",
                    charge_id=row.charge_id, detail=changed[0],
                    dry_run=False, actor=actor))
    db.commit()

    qbo = "not in QuickBooks"
    if was_posted:
        result = push_charge(db, row, actor)
        if result["status"] == "failed":
            # The payment is gone and the expense didn't land. Say so plainly:
            # this charge now needs attention in QuickBooks.
            row.post_error = result.get("error", "")[:1000]
            db.commit()
            raise HTTPException(
                400, f"The bill payment was removed, but the expense could not "
                     f"be created: {result.get('error')}. The charge is in "
                     f"Categorized with nothing posted — fix and retry.")
        qbo = "replaced with an expense"
    return {"changed": changed, "qbo": qbo}


# --- undo ---------------------------------------------------------------

# Fields that together describe what a charge WAS. Restoring these is what an
# undo does; anything not listed here is either derived (tier, suggestions) or
# not something a click changes.
_UNDO_FIELDS = (
    "status", "resolution", "coded_category", "coded_category_id",
    "coded_vendor", "coded_vendor_id", "matched_bill_id", "matched_bill_no",
    "matched_bill_vendor", "matched_bill_date", "matched_bill_memo",
    "memo", "cardholder_name", "merchant", "custom_note",
    "qbo_txn_id", "qbo_txn_type", "qbo_sync_token", "post_error",
    "resolved_by",
)

# One step, not a history. Undo is for "that click was wrong", and a deeper
# stack invites walking back through work that QuickBooks has since accepted.
UNDO_DEPTH = 1


def _snapshot(rows) -> str:
    """What these charges look like right now, as JSON."""
    import json as _json
    out = []
    for r in rows:
        snap = {f: getattr(r, f, None) for f in _UNDO_FIELDS}
        snap["charge_id"] = r.charge_id
        snap["resolved_at"] = r.resolved_at.isoformat() if r.resolved_at else None
        out.append(snap)
    return _json.dumps(out)


def _record_action(db, company: str, actor: str, action: str,
                   summary: str, before: str) -> None:
    """Remember one click, and forget anything past the depth limit."""
    from .models_db import ActionRow
    db.add(ActionRow(company=company, actor=actor, action=action,
                     summary=summary, state=before))
    db.flush()
    # Only the last few are keepable. Older ones are deleted outright rather
    # than left to accumulate: an undo stack you cannot reach is just a table
    # holding stale copies of charges.
    keep = [a.id for a in db.query(ActionRow)
            .filter(ActionRow.company == company, ActionRow.actor == actor,
                    ActionRow.undone_at.is_(None))
            .order_by(ActionRow.at.desc()).limit(UNDO_DEPTH).all()]
    if keep:
        db.query(ActionRow).filter(
            ActionRow.company == company, ActionRow.actor == actor,
            ActionRow.undone_at.is_(None),
            ~ActionRow.id.in_(keep)).delete(synchronize_session=False)


@app.get("/actions/undoable/{company}")
def undoable(company: str, db: Session = Depends(get_session),
             user: UserRow = Depends(require_reviewer)):
    """What this person's Undo button would reverse, and how deep it goes."""
    from .models_db import ActionRow
    # ALL COMPANIES: this person's latest action in any company they can reach.
    _cos = _reachable(db, user) if company == ALL_COMPANIES else [company]
    rows = (db.query(ActionRow)
            .filter(ActionRow.company.in_(_cos), ActionRow.actor == user.username,
                    ActionRow.undone_at.is_(None))
            .order_by(ActionRow.at.desc()).limit(UNDO_DEPTH).all())
    return {"depth": len(rows),
            "next": ({"id": rows[0].id, "company": rows[0].company,
                      "summary": (f"{rows[0].company}: {rows[0].summary}"
                                  if company == ALL_COMPANIES else rows[0].summary),
                      "at": rows[0].at.isoformat() + "Z"} if rows else None)}


@app.post("/actions/undo")
def undo_last(company: str = Form(...), db: Session = Depends(get_session),
              user: UserRow = Depends(require_write)):
    """Reverse this person's most recent click, QuickBooks included.

    Only their own: with several people working the queue, a shared stack means
    undoing a colleague's match you never saw.

    A charge that has been touched SINCE the action is left alone. Restoring an
    old snapshot over someone else's later work would discard it silently, so
    the honest answer is to say which ones and stop.
    """
    if company == ALL_COMPANIES:
        # The latest action in whichever company it was taken.
        _nx = undoable(company=ALL_COMPANIES, db=db, user=user).get("next")
        if not _nx:
            raise HTTPException(409, "Nothing to undo")
        return undo_last(company=_nx["company"], db=db, user=user)
    import json as _json
    from .models_db import ActionRow, ChargeRow
    from .integrations.qbo_write import undo_post, WriteBlocked, WriteFailed
    from datetime import datetime as _dt

    act = (db.query(ActionRow)
           .filter(ActionRow.company == company, ActionRow.actor == user.username,
                   ActionRow.undone_at.is_(None))
           .order_by(ActionRow.at.desc()).first())
    if act is None:
        raise HTTPException(400, "Nothing of yours left to undo.")

    snaps = _json.loads(act.state or "[]")
    restored, skipped, failed = 0, [], []

    # Bills take a different route: reversing one means writing to QuickBooks
    # again, so it goes back through the same edit path rather than restoring
    # fields locally and leaving the ledger out of step.
    if act.action == "bill_edit":
        from .models_db import BillRow
        from .integrations.qbo_write import edit_bill, WriteBlocked, WriteFailed
        from decimal import Decimal as _D
        for snap in snaps:
            row = db.get(BillRow, snap["bill_id"])
            if row is None:
                skipped.append(f"{snap['bill_id']}: no longer here")
                continue
            try:
                edit_bill(db, row, user.username,
                          vendor_name=snap.get("vendor") or None,
                          new_amount=_D(snap["amount"]))
                restored += 1
            except (WriteBlocked, WriteFailed) as e:
                failed.append(f"{row.doc_number or row.bill_id}: {e}")
        act.undone_at = _dt.utcnow()
        db.add(AuditRow(company=company, action="undo",
                        detail=f"undid '{act.summary}' — {restored} restored",
                        dry_run=False, actor=user.username))
        db.commit()
        return {"summary": act.summary, "restored": restored,
                "skipped": skipped, "failed": failed}

    for snap in snaps:
        row = db.get(ChargeRow, snap["charge_id"])
        if row is None:
            skipped.append(f"{snap['charge_id']}: no longer here")
            continue
        # Changed since? Whoever did that gets to keep it.
        now_resolved = row.resolved_at.isoformat() if row.resolved_at else None
        if row.resolved_by and row.resolved_by != user.username:
            skipped.append(f"{row.merchant or row.charge_id}: "
                           f"changed since by {row.resolved_by}")
            continue

        # The ledger first. If this action put something in QuickBooks and the
        # snapshot says there was nothing there before, take it back out --
        # otherwise the transaction is orphaned.
        if row.qbo_txn_id and not snap.get("qbo_txn_id"):
            try:
                res = undo_post(db, row, user.username)
                if res.get("status") not in ("deleted", "nothing_posted", "skipped"):
                    failed.append(f"{row.merchant or row.charge_id}: "
                                  f"{res.get('error', 'QuickBooks refused')}")
                    continue
            except (WriteBlocked, WriteFailed) as e:
                failed.append(f"{row.merchant or row.charge_id}: {e}")
                continue

        for f in _UNDO_FIELDS:
            setattr(row, f, snap.get(f))
        row.resolved_at = (_dt.fromisoformat(snap["resolved_at"])
                           if snap.get("resolved_at") else None)
        restored += 1

    act.undone_at = _dt.utcnow()
    db.add(AuditRow(company=company, action="undo",
                    detail=f"undid '{act.summary}' — {restored} restored",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"summary": act.summary, "restored": restored,
            "skipped": skipped, "failed": failed}


WEIGHT_LABELS = {
    "amount": ("Amount", "How close the charge and bill amounts are."),
    "email": ("Buyer Email",
              "The email address associated with the profile matches the email "
              "address in the bill's memo."),
    "order": ("Order Number",
              "The order number on the transaction matches the one on the "
              "bill. Compared on digits only, so the same reference written "
              "differently still matches; needs six digits or more, and scores "
              "nothing when either side has no order number."),
    "vendor": ("Vendor", "Words shared between the bank detail and the bill's "
                         "vendor."),
    "text": ("Text Overlap", "Words shared between the bank detail and the "
                             "bill's performer."),
    "name_email": ("Name in Email",
                   "Profile name matches the email address in the bill's memo. "
                   "N/A when Buyer Email signal finds a match."),
    "date": ("Date Proximity", "How close the charge and bill dates are."),
}

# The limits, which behave differently from the weights: these are thresholds
# and ceilings rather than contributions, so they get their own units.
LIMIT_LABELS = {
    "gate_days_before": ("Date Window - Before", "days",
        "How many days the charge occurs before the bill date."),
    "gate_days_after": ("Date Window - After", "days",
        "How many days the charge occurs after the bill date."),
    "gate_amount_pct": ("Amount Tolerance (%)", "%",
        "The % difference between the charge amount and the bill amount."),
    "gate_amount_abs": ("Amount Tolerance ($)", "$",
        "The $ difference between the charge amount and the bill amount."),
    # --- Signal Detail: how much of a signal's weight a partial answer earns
    "amt_exact_f": ("Amount — exact", "of weight",
        "Share of the Amount weight when the two amounts are identical."),
    "amt_near_gap": ("Amount — 'rounding' within", "$",
        "A gap up to this counts as rounding rather than a difference."),
    "amt_near_f": ("Amount — rounding earns", "of weight",
        "Share of the Amount weight for a gap inside the rounding figure."),
    "amt_close_gap": ("Amount — 'close' within", "$",
        "A gap up to this is close but not rounding."),
    "amt_close_f": ("Amount — close earns", "of weight",
        "Share of the Amount weight for a gap inside the close figure."),
    "amt_far_f": ("Amount — anything else earns", "of weight",
        "Share of the Amount weight for any gap that still cleared the "
        "tolerances above."),
    "date_full_days": ("Date — full marks within", "days",
        "A bill this many days from the charge earns the whole Date weight. "
        "Beyond it the score falls away across the rest of the window."),
    "text_fuzzy_floor": ("Text — fuzzy match floor", "0-1",
        "How alike two run-together merchant strings must be before a fuzzy "
        "match counts at all. Below this the signal scores nothing."),
    "text_min_word": ("Text — shortest word compared", "letters",
        "Words shorter than this are ignored when comparing text, so 'us' and "
        "'co' cannot carry a match on their own."),
    "name_both_f": ("Name in Email — both names", "of weight",
        "Share of the Name in Email weight when forename and surname are both "
        "found in the address."),
    "name_surname_f": ("Name in Email — surname only", "of weight",
        "Share when only the surname is found."),
    "name_other_f": ("Name in Email — forename only", "of weight",
        "Share when only the forename is found. A forename alone identifies "
        "far less than a surname alone."),
    "tier_perfect": ("Perfect (Floor)", "score",
        "A match at or above this score is shown as Perfect."),
    "tier_very_strong": ("Very Strong (Floor)", "score",
        "At or above this and below Perfect is shown as Very Strong."),
    "tier_strong": ("Strong (Floor)", "score",
        "At or above this and below Very Strong is shown as Strong."),
    "tier_moderate": ("Moderate (Floor)", "score",
        "At or above this and below Strong is shown as Moderate. Below it, Weak."),
}


@app.get("/match/weights")
def list_match_weights(db: Session = Depends(get_session),
                       user: UserRow = Depends(require_reviewer)):
    """The scorecard: each signal, its weight, and what it means."""
    from .models_db import MatchWeightRow
    from engine.suggest import DEFAULT_WEIGHTS
    saved = {r.key: r.weight for r in db.query(MatchWeightRow).all()}
    # Heaviest first, alphabetical within a tie: the order tells you what the
    # scorer actually leans on.
    rows = [{"key": k, "label": WEIGHT_LABELS[k][0], "help": WEIGHT_LABELS[k][1],
             "weight": saved.get(k, d), "default": d, "tuned": k in saved}
            for k, d in DEFAULT_WEIGHTS.items()]
    rows.sort(key=lambda r: (-r["weight"], r["label"]))

    from engine.suggest import DEFAULT_LIMITS
    # gate_date_days is deliberately absent from LIMIT_LABELS: the single
    # window it set has been replaced by the two below, and it survives only so
    # that a table saved before the split still means what it meant.
    limits = [{"key": k, "label": LIMIT_LABELS[k][0], "unit": LIMIT_LABELS[k][1],
               "help": LIMIT_LABELS[k][2], "value": saved.get(k, d), "default": d,
               # Gates decide which bills are offered at all; strengths decide
               # what a score is called once it has one. Different questions,
               # so they are asked in different places.
               "section": ("strength" if k.startswith("tier_")
                           else "signal" if (k.startswith("amt_")
                                             or k.startswith("name_")
                                             or k.startswith("text_")
                                             or k == "date_full_days")
                           else "limit")}
              for k, d in DEFAULT_LIMITS.items() if k in LIMIT_LABELS]
    # The two gate switches ride alongside the numeric limits but are rendered as
    # on/off toggles, not number boxes, so they are handed over separately.
    _GATE_META = [
        ("gate_days_before_on", "Date Window - Before",
         "Off: no limit on how far the charge can precede the bill."),
        ("gate_days_after_on", "Date Window - After",
         "Off: no limit on how far the charge can follow the bill."),
        ("gate_amount_pct_on", "Percentage tolerance",
         "Off: the percentage test stops rejecting anything."),
        ("gate_amount_abs_on", "Dollar tolerance",
         "Off: the dollar test stops rejecting anything."),
    ]
    gates = [{"key": k, "label": lab, "help": h,
              "on": float(saved.get(k, DEFAULT_LIMITS.get(k, 1.0))) >= 0.5}
             for k, lab, h in _GATE_META]
    return {"weights": rows, "limits": limits, "gates": gates}


@app.post("/match/weights")
def save_match_weights(payload: str = Form(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Save tuned weights. An empty payload restores the defaults.

    Changing these re-ranks every suggestion, so the stored strengths are
    recomputed straight away rather than drifting until the next bill sync.
    """
    import json as _json
    from .models_db import MatchWeightRow
    from engine.suggest import DEFAULT_WEIGHTS

    try:
        vals = _json.loads(payload or "{}")
    except ValueError:
        raise HTTPException(400, "Could not read those weights")

    from engine.suggest import DEFAULT_LIMITS
    bounds = {"gate_date_days": (0, 400, "Date Window is 0 to 400 days"),
              "gate_days_before": (0, 400, "Date Window is 0 to 400 days"),
              "gate_days_after": (0, 400, "Date Window is 0 to 400 days"),
              "gate_amount_pct": (0, 100, "Amount Tolerance is 0 to 100%"),
              "gate_amount_abs": (0, 100000,
                                  "Dollar tolerance is 0 to 100,000"),
              "gate_days_before_on": (0, 1, "A gate switch is 0 or 1"),
              "gate_days_after_on": (0, 1, "A gate switch is 0 or 1"),
              "gate_amount_pct_on": (0, 1, "A gate switch is 0 or 1"),
              "gate_amount_abs_on": (0, 1, "A gate switch is 0 or 1"),
              "amt_near_gap": (0, 1000, "Rounding figure is 0 to 1,000"),
              "amt_close_gap": (0, 10000, "Close figure is 0 to 10,000"),
              "amt_exact_f": (0, 1, "A share is 0 to 1"),
              "amt_near_f": (0, 1, "A share is 0 to 1"),
              "amt_close_f": (0, 1, "A share is 0 to 1"),
              "amt_far_f": (0, 1, "A share is 0 to 1"),
              "date_full_days": (0, 400, "Full-marks window is 0 to 400 days"),
              "name_both_f": (0, 1, "A share is 0 to 1"),
              "name_surname_f": (0, 1, "A share is 0 to 1"),
              "name_other_f": (0, 1, "A share is 0 to 1"),
              "text_fuzzy_floor": (0, 1, "The fuzzy floor is 0 to 1"),
              "text_min_word": (1, 12, "Shortest word is 1 to 12 letters"),
              "tier_perfect": (0, 1, "Perfect From is 0 to 1"),
              "tier_very_strong": (0, 1, "Very Strong From is 0 to 1"),
              "tier_strong": (0, 1, "Strong From is 0 to 1"),
              "tier_moderate": (0, 1, "Moderate From is 0 to 1"),
              "score_cap": (0.1, 1, "Score Ceiling is 0.1 to 1")}

    # MERGE, not replace. This used to delete every row and write back only
    # what the form sent -- fine when one screen held every setting, and
    # destructive once they were split across tabs: saving on Gates sent no
    # weights, so the six weights were deleted and silently fell back to
    # defaults. Nothing on screen said so, because the tab being saved looked
    # exactly right afterwards.
    #
    # An explicit reset still restores the defaults; it sends "{}" and is
    # handled below.
    if not vals:
        db.query(MatchWeightRow).delete()
    for k, v in vals.items():
        if k not in DEFAULT_WEIGHTS and k not in DEFAULT_LIMITS:
            continue
        try:
            f = round(float(v), 4)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{v}' isn't a number")
        if k in DEFAULT_WEIGHTS:
            if not 0 <= f <= 1:
                raise HTTPException(400, "Weights run from 0 to 1")
        else:
            lo, hi, msg = bounds[k]
            if not lo <= f <= hi:
                raise HTTPException(400, msg)
        db.merge(MatchWeightRow(key=k, weight=f))

    # Compared against what is STORED, not against the defaults: the two bands
    # can now be saved from the same tab or different ones, and defaulting the
    # absent side would let a valid pair look invalid.
    _stored = {r.key: float(r.weight) for r in db.query(MatchWeightRow).all()}
    _mod = vals.get("tier_moderate",
                    _stored.get("tier_moderate", DEFAULT_LIMITS["tier_moderate"]))
    _str = vals.get("tier_strong",
                    _stored.get("tier_strong", DEFAULT_LIMITS["tier_strong"]))
    if float(_mod) > float(_str):
        raise HTTPException(400, "Moderate From can't be above Strong From")
    _vs = float(vals.get("tier_very_strong", _stored.get(
        "tier_very_strong", DEFAULT_LIMITS["tier_very_strong"])))
    _pf = float(vals.get("tier_perfect", _stored.get(
        "tier_perfect", DEFAULT_LIMITS["tier_perfect"])))
    if float(_str) > _vs:
        raise HTTPException(400, "Strong From can't be above Very Strong From")
    if _vs > _pf:
        raise HTTPException(400, "Very Strong From can't be above Perfect From")
    db.add(AuditRow(company="", action="match_weights_saved",
                    detail=", ".join(f"{k}={v}" for k, v in sorted(vals.items())),
                    dry_run=False, actor=user.username))
    db.commit()

    from .review import load_match_weights
    load_match_weights(db)
    # Band starts not yet saved as bands come from these limits: re-read.
    from .bands import reset_cache as _brc
    _brc()
    return {"saved": len(vals)}


@app.post("/match/weights/preview")
def preview_match_weights(company: str = Form(...), payload: str = Form(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_reviewer)):
    """What these weights would do, without saving them.

    Re-scores the queue under the proposed scorecard and reports how many
    charges change tier and how many top-ranked bills change. Tuning blind --
    save, then hunt for what moved -- is how a scorecard quietly gets worse.
    """
    import json as _json
    from engine.suggest import current_weights, set_weights
    from .review import build_queue, load_match_weights

    try:
        vals = _json.loads(payload or "{}")
    except ValueError:
        raise HTTPException(400, "Could not read those weights")

    load_match_weights(db)
    before = build_queue(db, company, status="for_review", limit=0)
    was = {i["id"]: (i["tier"], (i["candidates"] or [{}])[0].get("bill_id"))
           for i in before["items"]}

    saved = current_weights()
    try:
        set_weights({**saved, **vals})
        after = build_queue(db, company, status="for_review", limit=0)
    finally:
        set_weights(saved)          # never leak a preview into live scoring

    tier_moves, top_moves = 0, 0
    for i in after["items"]:
        old = was.get(i["id"])
        if not old:
            continue
        if old[0] != i["tier"]:
            tier_moves += 1
        if old[1] != (i["candidates"] or [{}])[0].get("bill_id"):
            top_moves += 1
    return {"charges": len(after["items"]), "tier_changes": tier_moves,
            "top_match_changes": top_moves}


SPLIT_MAX_LINES = 20


def _split_rows(db: Session, charge_id: str) -> list:
    from .models_db import ChargeSplitRow
    return (db.query(ChargeSplitRow)
            .filter(ChargeSplitRow.charge_id == charge_id)
            .order_by(ChargeSplitRow.position, ChargeSplitRow.id).all())


@app.get("/queue/{company}/split/{charge_id}")
def get_charge_split(company: str, charge_id: str,
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """The split on one charge, for reopening the dialog on what was saved."""
    rows = _split_rows(db, charge_id)
    return {"charge_id": charge_id,
            "lines": [{"position": r.position, "category": r.category,
                       "category_id": r.category_id,
                       "amount": str(r.amount),
                       "percent": (str(r.percent) if r.percent is not None
                                   else None),
                       "description": r.description} for r in rows]}


@app.post("/queue/split")
def save_charge_split(charge_id: str = Form(...), payload: str = Form("[]"),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_write)):
    """Replace the split on one charge. An empty payload removes it.

    The lines must total the charge exactly. The dialog already makes them, but
    it is the server's job to insist: a split that doesn't add up posts a
    transaction QuickBooks balances silently.
    """
    import json as _json
    from decimal import Decimal, InvalidOperation
    from .models_db import ChargeRow, ChargeSplitRow

    charge = db.query(ChargeRow).filter(ChargeRow.charge_id == charge_id).first()
    if charge is None:
        raise HTTPException(404, "No such charge")

    try:
        lines = _json.loads(payload or "[]")
    except ValueError:
        raise HTTPException(400, "Could not read those lines")
    if not isinstance(lines, list):
        raise HTTPException(400, "Could not read those lines")

    if not lines:
        db.query(ChargeSplitRow).filter(
            ChargeSplitRow.charge_id == charge_id).delete()
        db.add(AuditRow(company=charge.company, action="charge_split_cleared",
                        charge_id=charge_id, dry_run=False, actor=user.username))
        db.commit()
        _resync_split(db, charge, user.username)
        return {"lines": 0}

    if len(lines) < 2:
        raise HTTPException(400, "A split needs at least two lines")
    if len(lines) > SPLIT_MAX_LINES:
        raise HTTPException(400, f"At most {SPLIT_MAX_LINES} lines")

    total = Decimal("0")
    rows = []
    for i, ln in enumerate(lines):
        try:
            amt = Decimal(str(ln.get("amount", "0"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise HTTPException(400, f"Line {i + 1}: '{ln.get('amount')}' isn't an amount")
        if amt <= 0:
            raise HTTPException(400, f"Line {i + 1}: every line needs an amount above zero")
        cat = str(ln.get("category") or "").strip()
        if not cat:
            raise HTTPException(400, f"Line {i + 1}: choose a category")
        pct = ln.get("percent")
        try:
            pct = Decimal(str(pct)) if pct not in (None, "") else None
        except (InvalidOperation, TypeError):
            pct = None
        total += amt
        # Resolve the account id HERE, from the name the dialog sent. Without
        # it the payload carries AccountRef by name only, and QuickBooks
        # rejects the whole request with "Required parameter
        # Line.AccountBasedExpenseLineDetail.AccountRef is missing" -- which
        # names the field it wants but not the reason it is empty.
        cat_id = str(ln.get("category_id") or "").strip()
        if not cat_id:
            from .models_db import QboRefRow
            ref = _ref_by_name(db, charge.company, "account", cat)
            if ref is None:
                raise HTTPException(
                    400, f"Line {i + 1}: '{cat}' is not a QuickBooks account. "
                         f"Pick one from the list.")
            cat_id = ref.qbo_id
        rows.append(ChargeSplitRow(
            charge_id=charge_id, company=charge.company, position=i,
            category=cat, category_id=cat_id,
            amount=amt, percent=pct,
            description=str(ln.get("description") or "").strip()))

    if total != Decimal(charge.amount):
        raise HTTPException(
            400, f"The lines total ${total} but the charge is ${charge.amount}")

    # Only now, with every line accepted. Deleting first meant a rejected save
    # took the existing split with it -- the reviewer fixed one typo and lost
    # the split they were editing.
    db.query(ChargeSplitRow).filter(
        ChargeSplitRow.charge_id == charge_id).delete()
    for r in rows:
        db.add(r)
    # The charge keeps a category so the grid, the filters and the exports have
    # something to show. "Split" would be a lie to any filter on it, so it
    # shows the first line and the split badge says the rest.
    charge.coded_category = rows[0].category
    charge.coded_category_id = rows[0].category_id
    db.add(AuditRow(company=charge.company, action="charge_split_saved",
                    charge_id=charge_id,
                    detail="; ".join(f"{r.category} ${r.amount}" for r in rows),
                    dry_run=False, actor=user.username))
    db.commit()
    _resync_split(db, charge, user.username)
    return {"lines": len(rows), "total": str(total)}


def _resync_split(db: Session, charge, actor: str) -> None:
    """Push a changed split to a charge already in QuickBooks.

    Editing the split of a posted charge and leaving QuickBooks holding the old
    lines is exactly the "second set of books" problem the update path exists
    to prevent, so the same path runs here.
    """
    if not charge.qbo_txn_id:
        return
    try:
        from .integrations.qbo_write import update_posted_charge
        update_posted_charge(db, charge, actor)
        db.commit()
    except Exception as e:                  # noqa: BLE001
        # Reported, not raised: the split IS saved, and losing that because the
        # ledger write failed would leave the two further apart, not closer.
        charge.post_error = f"split saved here but not in QuickBooks: {e}"
        db.commit()


@app.post("/bills/delete")
def delete_bills(bill_ids: str = Form(...), company: str = Form(...),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Delete bills in QuickBooks. Not reversible, and refused if paid.

    Admin only and one bill at a time on the wire, so a failure part-way
    through a selection leaves an exact record of which ones went.
    """
    from .models_db import BillRow
    from .integrations.qbo_write import delete_bill, WriteFailed, WriteBlocked

    ids = [b for b in (bill_ids or "").split(",") if b.strip()]
    deleted, refused = [], []
    for bid in ids:
        bill = db.query(BillRow).filter(BillRow.company == company,
                                        BillRow.bill_id == bid.strip()).first()
        if bill is None:
            refused.append(f"{bid}: not in the bill list any more")
            continue
        label = bill.doc_number or bill.bill_id
        try:
            delete_bill(db, bill, user.username)
            deleted.append(label)
        except (WriteFailed, WriteBlocked) as e:
            db.rollback()
            refused.append(f"{label}: {e}")
    return {"deleted": deleted, "refused": refused}


@app.post("/bills/merge")
def merge_bills_endpoint(keep_id: str = Form(...), drop_ids: str = Form(...),
                         company: str = Form(...),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_write)):
    """Fold one or more bills into the one being kept, here and in QuickBooks.

    Open to anyone who may change data, not admins only: merging duplicate
    bills is ordinary reviewing work, and routing it through an admin would
    mean it either waited or was done in QuickBooks where nothing records it.
    """
    from .integrations.qbo_write import merge_bills, WriteFailed, WriteBlocked

    drops = [b.strip() for b in (drop_ids or "").split(",") if b.strip()]
    try:
        return merge_bills(db, company, keep_id.strip(), drops, user.username)
    except (WriteFailed, WriteBlocked) as e:
        db.rollback()
        raise HTTPException(400, str(e))


@app.post("/bills/split")
def split_bill_endpoint(bill_id: str = Form(...), company: str = Form(...),
                        parts: str = Form(...),
                        db: Session = Depends(get_session),
                        user: UserRow = Depends(require_write)):
    """Divide one bill into several. `parts` is a comma-separated amount list."""
    from decimal import Decimal, InvalidOperation
    from .integrations.qbo_write import split_bill, WriteFailed, WriteBlocked
    try:
        amounts = [Decimal(p.strip()) for p in parts.split(",") if p.strip()]
    except InvalidOperation:
        raise HTTPException(400, "Those amounts could not be read")
    try:
        return split_bill(db, company, bill_id.strip(), amounts, user.username)
    except (WriteFailed, WriteBlocked) as e:
        db.rollback()
        raise HTTPException(400, str(e))


@app.get("/export/scan/{company}")
def export_scan(company: str, companies: str = Query(""), db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    """The flagged list as a spreadsheet."""
    if company == ALL_COMPANIES:
        return _multi_export(export_scan, dict(locals()), "scan")
    from .export_xlsx import build_workbook, filename
    data = qbo_scan_state(company=company, db=db, user=user)
    # Column for column with the grid, in the same order.
    headers = ["Time Stamp", "Finding", "Card Account", "Vendor", "Category",
               "Date", "Bank Detail", "Profile", "Amount", "What happened",
               "Platform TXN ID", "QBO Txn ID", "QBO Type", "Bill", "Status"]
    rows = [[_as_naive(i.get("at", "")), i.get("kind", ""),
             i.get("card_account", ""), i.get("vendor", ""),
             i.get("category", ""), _as_date(i["date"]), i["merchant"],
             i["holder"], float(i["amount"]), i["problem"],
             i.get("txn_id", ""), i["qbo_txn_id"], i["qbo_txn_type"],
             i["bill_id"], i["status"]] for i in data["items"]]
    applied = [("Company", company), ("Tab", "Deletion / Edit scan"),
               ("Last scan", data["last_run"] or "never run"),
               ("Scan state", data["state"]),
               ("Exported by", user.username)]
    # The columns moved: Time Stamp is 0, Date is 3, Amount is 6. Left at the
    # old indices the date format would have landed on the timestamp and the
    # currency format on the date.
    blob = build_workbook("Deletion / Edit scan", headers, rows, applied,
                          # Vendor and Category pushed Date to 5 and Amount to 8.
                          stamp_cols=[0], date_cols=[5], money_cols=[8])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename("scan", company)}"'})


@app.get("/qbo/scan/{company}")
def qbo_scan_state(company: str, companies: str = Query(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    """What the hourly sweep last found, and everything still flagged.

    The sweep writes its findings onto the charge and its own health into
    sync_status. Both are needed to read the screen honestly: an empty list
    means nothing is wrong ONLY if the sweep has actually run.
    """
    if company == ALL_COMPANIES:
        # Every reachable company's findings, tagged; the latest run of any.
        _items, _last, _flag = [], None, 0
        for _co in _reachable(db, user, companies):
            _r = qbo_scan_state(company=_co, companies="", db=db, user=user)
            for _x in _r.get("items") or []:
                _x["company"] = _co
                _items.append(_x)
            if _r.get("last_run") and (not _last or _r["last_run"] > _last):
                _last = _r["last_run"]
            _flag += int(_r.get("flagged_last_run") or 0)
        return {"company": ALL_COMPANIES, "last_run": _last,
                "state": "ok" if _last else "never run", "detail": "",
                "flagged_last_run": _flag, "items": _items}
    from datetime import datetime as _dt, timedelta as _td
    from . import audit_labels as _al
    from .models_db import ChargeRow, SourceAccountRow, SyncStatusRow
    from sqlalchemy import or_ as _or
    # The card account's NICKNAME, which is what the column shows everywhere
    # else -- "Divvy (Credit)", not the internal source key.
    _nick = {a.source: (a.nickname or a.qbo_account_name or a.source)
             for a in db.query(SourceAccountRow)
             .filter(SourceAccountRow.company == company).all()}
    # THIS company's last scan, falling back to the deployment-wide row for
    # installs that predate per-company status. A company that has never
    # been scanned must not inherit another's timestamp -- "last scan:
    # yesterday, nothing orphaned" is a clean bill of health for a company
    # nothing has ever looked at.
    st = db.get(SyncStatusRow, f"orphans:{company}")
    # BOTH kinds of finding, because one sweep produces both.
    #
    # The sweep reads QuickBooks' change feed for deletions AND edits, but this
    # screen only ever read post_error -- which only deletions and failed posts
    # set. So a charge somebody re-categorized in QuickBooks was detected,
    # badged on the Categorized grid and written to the audit log, and was
    # missing from the one screen whose entire subject is "what did the
    # QuickBooks scan find". Two answers to that question disagreed.
    rows = (db.query(ChargeRow)
            .filter(ChargeRow.company == company,
                    _or(ChargeRow.post_error.isnot(None) & (ChargeRow.post_error != ""),
                        ChargeRow.qbo_drift.isnot(None) & (ChargeRow.qbo_drift != "")))
            .order_by(ChargeRow.txn_date.desc()).limit(500).all())

    # EDITS THAT WERE COPIED ACROSS belong on this screen too.
    #
    # An adopted edit leaves no marker on the charge -- that is the point, it
    # needed nothing from anybody -- so nothing above finds it, and the tab
    # named after the deletion-and-edit scan was listing only the half that
    # went wrong. They come from the audit log instead, which is where the
    # adoption is recorded.
    from .models_db import AuditRow as _AR
    seen = {r.charge_id for r in rows}
    _cut = _dt.utcnow() - _td(days=30)          # the change feed's own horizon
    edits = (db.query(_AR)
             .filter(_AR.company == company, _AR.action == "qbo_updated",
                     _AR.at >= _cut)
             .order_by(_AR.at.desc()).limit(500).all())
    by_charge = {}
    for e in edits:
        if e.charge_id and e.charge_id not in seen:
            by_charge.setdefault(e.charge_id, e)
    if by_charge:
        extra = (db.query(ChargeRow)
                 .filter(ChargeRow.charge_id.in_(list(by_charge))).all())
    else:
        extra = []

    def _drift_words(raw: str) -> str:
        """The stored diff as a sentence. Names BOTH readings, deliberately:
        "changed" without saying from what to what is half an answer."""
        import json as _js
        try:
            diffs = _js.loads(raw or "[]")
        except Exception:                   # noqa: BLE001
            return "Changed in QuickBooks by someone else."
        parts = []
        for d in diffs:
            # The keys the drift sweep actually writes are `engine` and
            # `quickbooks` -- see app/qbo_drift.py. Reading `ours`/`theirs`
            # would have produced "this says (blank), QuickBooks says (blank)"
            # on every row, which reads as a bug in the comparison rather than
            # a mistake in this sentence.
            # "changed amount from X to Y", in the same shape as every other
            # change note. The old wording named both systems in one clause,
            # which is a sentence about the engine rather than about the
            # transaction.
            parts.append(f"changed {d.get('field', 'value')} from "
                         f"{d.get('engine') or '(blank)'} to "
                         f"{d.get('quickbooks') or '(blank)'}")
        # Only amounts reach this now: everything else is adopted rather
        # than reported, so a row here is a difference somebody has to settle.
        return "; ".join(parts) if parts else "Differs from QuickBooks."
    return {
        "company": company,
        "last_run": st.finished_at.isoformat() + "Z" if st and st.finished_at else None,
        "state": st.state if st else "never run",
        "detail": st.detail if st else "",
        "flagged_last_run": st.records if st else 0,
        "items": [{"charge_id": r.charge_id, "date": str(r.txn_date),
                   "merchant": r.merchant or "", "amount": str(r.amount),
                   "holder": r.cardholder_name or "",
                   # The same three the audit log carries, so the two screens
                   # can be read side by side: when the scan saw it, which card
                   # account it is on, and the source's own transaction id.
                   "at": (r.qbo_drift_at or r.posted_at).isoformat() + "Z"
                         if (r.qbo_drift_at or r.posted_at) else "",
                   "card_account": _nick.get(r.source, r.source),
                   # The grid grew Vendor and Category columns and the payload
                   # never grew the fields, so both rendered blank on every row.
                   "vendor": r.coded_vendor or r.suggested_vendor or "",
                   "category": r.coded_category or r.suggested_category or "",
                   "txn_id": r.source_txn_id or "",
                   "qbo_txn_id": r.qbo_txn_id or "",
                   "qbo_txn_type": _txn_word(r.qbo_txn_type) or "",
                   "bill_id": r.matched_bill_id or "",
                   "status": r.status,
                   # Named, so the screen can say which sweep found it and
                   # offer the action that fits. Returning an edited charge to
                   # For Review would throw away a decision somebody made in
                   # QuickBooks; that button belongs on deletions only.
                   "kind": ("deleted or failed" if (r.post_error or "")
                            else "amount differs"),
                   "problem": (r.post_error
                               or _drift_words(r.qbo_drift or ""))}
                  for r in rows]
                 + [{"charge_id": r.charge_id, "date": str(r.txn_date),
                     "merchant": r.merchant or "", "amount": str(r.amount),
                     "holder": r.cardholder_name or "",
                     "at": by_charge[r.charge_id].at.isoformat() + "Z",
                     "card_account": _nick.get(r.source, r.source),
                     "vendor": r.coded_vendor or r.suggested_vendor or "",
                     "category": r.coded_category or r.suggested_category or "",
                     "txn_id": r.source_txn_id or "",
                     "qbo_txn_id": r.qbo_txn_id or "",
                     "qbo_txn_type": _txn_word(r.qbo_txn_type) or "",
                     "bill_id": r.matched_bill_id or "",
                     "status": r.status,
                     "kind": "edited in QuickBooks",
                     # The audit entry's own words. It already names each field
                     # with both readings, and writing a second sentence here
                     # would be a second description of one event that could
                     # drift from the first.
                     "problem": _al.humanize(
                         by_charge[r.charge_id].detail or "")}
                    for r in extra],
    }


@app.post("/qbo/scan/{company}")
def qbo_scan_run(company: str, companies: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Run the sweep now rather than waiting for the hour.

    Looks back a day, which is the same window the first scheduled run uses.
    """
    if company == ALL_COMPANIES:
        # Scan Now for every reachable company, one after another.
        _res = {}
        for _co in _reachable(db, user, companies):
            try:
                _res[_co] = qbo_scan_run(company=_co, companies="", db=db, user=user)
            except HTTPException as _e:
                _res[_co] = {"error": _e.detail}
        return {"company": ALL_COMPANIES, "results": _res}
    from datetime import datetime as _dt, timedelta as _td
    from .integrations.qbo_write import (find_orphaned_transactions, WriteFailed,
                                         WriteBlocked)
    from .qbo_drift import scan_for_edits
    try:
        since = _dt.utcnow() - _td(days=1)
        r = find_orphaned_transactions(db, company, since)
        # EDITS TOO. This button ran the deletion sweep only, so pressing "Scan
        # now" after changing a category in QuickBooks reported nothing and
        # looked like the detector was broken -- while the hourly job, which
        # runs both, would have found it an hour later. A button labeled "Scan
        # now" has to do what the scan does.
        d = scan_for_edits(db, company, since)

        # RECORD THAT THIS COMPANY WAS SCANNED. Only the hourly worker wrote
        # the status row, so pressing "Scan now" ran the sweep, reported what
        # it found, and left the screen still saying "Last scan: never — the
        # hourly job may not be running". The button did its job and then said
        # it had not.
        from .models_db import SyncStatusRow as _SS
        flagged = r["flagged"] + d.get("flagged", 0)
        st = db.get(_SS, f"orphans:{company}")
        if st is None:
            st = _SS(name=f"orphans:{company}")
        st.state = "warn" if flagged else "ok"
        st.detail = (f"{flagged} flagged" if flagged else "nothing orphaned")
        st.records = flagged
        st.started_at = since
        st.finished_at = _dt.utcnow()
        db.add(st)
        db.commit()

        return {"checked": r["checked"] + d.get("checked", 0),
                "flagged": flagged,
                "deleted": r["flagged"], "changed": d.get("flagged", 0),
                "cleared": d.get("cleared", 0)}
    except (WriteFailed, WriteBlocked) as e:
        raise HTTPException(400, str(e))
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")


@app.post("/queue/convert-to-expense")
def convert_payments_to_expenses(ids: str = Form(...), category: str = Form(...),
                                 vendor: str = Form(""),
                                 db: Session = Depends(get_session),
                                 user: UserRow = Depends(require_write)):
    """Turn several matched bill payments into coded expenses.

    The single-charge path already does this properly -- the BillPayment is
    deleted, which puts the bill back on the books, and a Purchase is created
    in its place. This is that, in a loop, so the two can never drift apart.

    Charge by charge rather than as one transaction: each one is an independent
    pair of QuickBooks writes, and a failure on the fortieth should not undo the
    thirty-nine that worked. What failed is named instead.
    """
    from .models_db import ChargeRow

    cat = (category or "").strip()
    if not cat or cat.lower() == "bill payment":
        raise HTTPException(400, "Choose the category the expenses should take")

    done, skipped, failed = 0, [], []
    for cid in [c.strip() for c in (ids or "").split(",") if c.strip()]:
        row = db.query(ChargeRow).filter(ChargeRow.charge_id == cid).first()
        if row is None:
            continue
        if row.resolution != "matched":
            skipped.append(f"{row.merchant or cid}: not a bill payment")
            continue
        try:
            # The vendor is set BEFORE the conversion, so the expense is
            # created with it. Applied only when one was given, so an older
            # client that sends no vendor keeps the bill's own.
            if (vendor or "").strip():
                from .models_db import QboRefRow
                row.coded_vendor = vendor.strip()
                ref = _ref_by_name(db, row.company, "vendor", vendor)
                row.coded_vendor_id = ref.qbo_id if ref else ""
            _convert_payment_to_expense(db, row, cat, user.username)
            done += 1
        except HTTPException as e:
            db.rollback()
            failed.append(f"{row.merchant or cid}: {e.detail}")
        except Exception as e:                      # noqa: BLE001
            db.rollback()
            failed.append(f"{row.merchant or cid}: {e}")
    return {"converted": done, "skipped": skipped, "failed": failed}


@app.post("/queue/release")
def release_charge(charge_id: str = Form(...),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Return a charge to For Review WITHOUT touching QuickBooks.

    For the one case Undo cannot handle: the transaction Undo would delete is
    already gone, so Undo fails and the charge is stuck looking resolved. This
    lets go of a QuickBooks record that no longer exists.

    Deliberately separate from Undo, and deliberately not automatic. It is the
    only action that drops the link to QuickBooks without checking QuickBooks,
    so it should only ever be reached knowingly, from a charge the sweep has
    already flagged.
    """
    from .models_db import ChargeRow
    row = db.query(ChargeRow).filter(ChargeRow.charge_id == charge_id).first()
    if row is None:
        raise HTTPException(404, "No such charge")
    was = row.qbo_txn_id or "—"
    db.add(AuditRow(company=row.company, action="charge_released",
                    charge_id=charge_id, bill_id=row.matched_bill_id or "",
                    detail=f"released from {_txn_word(row.qbo_txn_type) or 'QuickBooks'} "
                           f"{was}; returned to For Review without a QuickBooks "
                           f"write", dry_run=False, actor=user.username))
    row.status, row.resolution = "for_review", ""
    row.matched_bill_id = ""
    row.qbo_txn_id = row.qbo_txn_type = row.qbo_sync_token = ""
    row.posted_at = row.resolved_at = None
    row.resolved_by = ""
    row.post_error = ""
    db.commit()
    return {"released": charge_id}


# The seasons offered as renewal years. A DEFAULT now, not the list: seasons
# roll over every year, and editing source to add one is a deploy for a fact
# that changes on a schedule everybody already knows.
SEASON_YEARS_DEFAULT = ("24/25", "25/26", "26/27", "27/28")
SEASON_YEARS_KEY = "season_years"


def season_years(db) -> list:
    """The renewal years on offer, as set in the UI, else the default."""
    from .models_db import AppSettingRow as _AS5
    row = db.get(_AS5, SEASON_YEARS_KEY)
    if row and (row.value or "").strip():
        out = [y.strip() for y in row.value.split(",") if y.strip()]
        if out:
            return out
    return list(SEASON_YEARS_DEFAULT)


# Kept so anything still importing the name gets the default rather than an
# ImportError; it is NOT the live list, which needs a session to read.
SEASON_YEARS = SEASON_YEARS_DEFAULT
SEASONS_SETTING = "league_seasons"


def league_seasons(db: Session) -> dict:
    """Which seasons to read, per league.

    Per league rather than one global list: an NHL account and an MLB account
    are not on the same calendar, and reading a status for the wrong year is
    worse than reading none at all.
    """
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, SEASONS_SETTING)
    if not row or not (row.value or "").strip():
        return {}
    try:
        return json.loads(row.value)
    except Exception:                               # noqa: BLE001
        return {}


@app.post("/setup/season-years")
def save_season_years(years: str = Form(...),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Replace the list of renewal years offered across the engine."""
    from .models_db import AppSettingRow as _AS6, PaymentPlanRow as _PP6
    clean, seen = [], set()
    for y in years.split(","):
        y = y.strip()
        # Same shape as the ones already in use, so a typo cannot quietly
        # become a season nobody can match against.
        if not y or y in seen:
            continue
        if not re.fullmatch(r"\d{2}/\d{2}", y):
            raise HTTPException(400, f"{y!r} is not a season like 25/26")
        seen.add(y)
        clean.append(y)
    if not clean:
        raise HTTPException(400, "At least one renewal year is needed")

    # A YEAR IN USE CANNOT JUST VANISH. Removing one that teams are assigned to
    # would leave those rows pointing at a season the engine no longer offers,
    # which shows as a blank pill and matches nothing -- so say which teams,
    # and let somebody decide.
    gone = set(season_years(db)) - seen
    if gone:
        rows = db.query(_PP6).filter(_PP6.seasons != "").all()
        # Only the years actually blocking, not every year being removed: a
        # message naming a season nobody uses sends people looking for it.
        blocked = {}
        for r in rows:
            for y in (r.seasons or "").split(","):
                y = y.strip()
                if y in gone:
                    blocked.setdefault(y, set()).add(r.team)
        if blocked:
            parts = []
            for y in sorted(blocked):
                teams = sorted(blocked[y])
                shown = ", ".join(teams[:5])
                more = f" and {len(teams) - 5} more" if len(teams) > 5 else ""
                parts.append(f"{y} is still set on {shown}{more}")
            raise HTTPException(400, {
                "detail": "; ".join(parts) + ". Clear it from those teams "
                                             "first.",
                "in_use": sorted({t for ts in blocked.values() for t in ts})})

    row = db.get(_AS6, SEASON_YEARS_KEY)
    if row:
        row.value = ",".join(clean)
    else:
        db.add(_AS6(key=SEASON_YEARS_KEY, value=",".join(clean)))
    db.add(AuditRow(company="", action="season_years_saved",
                    detail="renewal years on offer are now "
                           + ", ".join(clean), dry_run=False,
                    actor=user.username))
    db.commit()
    return {"years": clean}


@app.get("/setup/seasons")
def get_seasons(db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    """The leagues known from the alias sync, and the seasons chosen for each."""
    from .models_db import TeamAliasRow
    chosen = league_seasons(db)
    leagues = sorted({r[0] for r in db.query(TeamAliasRow.league).distinct().all()
                      if r[0]})
    # A league nobody has chosen seasons for reads none, rather than guessing.
    return {"years": season_years(db),
            "leagues": [{"league": lg, "seasons": chosen.get(lg, [])}
                        for lg in leagues]}


@app.post("/setup/seasons")
def save_seasons(seasons: str = Form(...),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Save the per-league season selection. `seasons` is JSON."""
    from .models_db import AppSettingRow
    try:
        payload = json.loads(seasons)
        assert isinstance(payload, dict)
    except Exception:                               # noqa: BLE001
        raise HTTPException(400, "That selection could not be read")
    _live = set(season_years(db))
    clean = {str(k): [y for y in v if y in _live]
             for k, v in payload.items() if isinstance(v, list)}
    row = db.get(AppSettingRow, SEASONS_SETTING) or AppSettingRow(
        key=SEASONS_SETTING)
    row.value = json.dumps(clean)
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="seasons_saved",
                    detail=", ".join(f"{k}: {'/'.join(v)}"
                                     for k, v in sorted(clean.items()) if v),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"seasons": clean}


def _team_rows(rows, astat, noalgo):
    """The Teams grid, built in one pass over the alias rows.

    Grouped rather than filtered per team: the previous version ran eight
    separate generators over the full list for each team, which is quadratic in
    the number of teams and was the whole of the delay on that tab.
    """
    by_team: dict = {}
    for x in rows:
        by_team.setdefault(x.team, []).append(x)

    out = []
    for team in sorted(by_team):
        mine = by_team[team]
        venue = [x for x in mine if x.kind == "venue" and x.alias]
        note = [x for x in mine if x.kind != "venue" and x.alias]

        def item(x):
            return dict(alias=x.alias,
                        **astat.get(f"{team}\x00{(x.alias or '').lower()}",
                                    {"status": "active"}))

        from .canada import looks_canadian as _lc_t
        out.append({
            "team": team,
            # Canadian: the flag after the team's name, as on the Bank Feed.
            "canadian": bool(_lc_t(team, *[x.alias for x in venue])),
            "league": next((x.league for x in mine if x.league), ""),
            "venue": ", ".join(x.alias for x in venue),
            "notes": ", ".join(x.alias for x in note),
            # The same aliases, one per entry, each carrying whether it
            # actually claims anything. An alias that does nothing looks
            # identical to one that works until it is asked about.
            "venue_items": [item(x) for x in venue],
            "note_items": [item(x) for x in note],
            # Whether this team's name and aliases are inferred from at all.
            # Off means the row stays visible and reachable by a written rule,
            # but nothing about it is guessed.
            "matching": (team or "").strip().lower() not in noalgo,
            # Per team, so a team can be trusted to add its own charges without
            # every other team being trusted at the same time.
            "auto_add": any(bool(getattr(x, "auto_add", False)) for x in mine),
            "auto_add_refunds": any(
                bool(getattr(x, "auto_add_refunds", False)) for x in mine),
        })
    return out


@app.get("/match/aliases")
def list_team_aliases(db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    """Team aliases, newest sync first, so the screen can show what is known."""
    from .models_db import TeamAliasRow, SyncStatusRow
    rows = (db.query(TeamAliasRow)
            .order_by(TeamAliasRow.team, TeamAliasRow.alias).all())
    # Hidden teams leave the grid entirely. They arrive through the same
    # Airtable column as real teams -- Live Nation is a promoter, not a team --
    # and a row nobody wants to see is a row somebody has to skip past on every
    # pass through the screen.
    from .models_db import AppSettingRow as _AS
    _h = db.get(_AS, "team_hidden")
    try:
        _hidden = {str(t).strip().lower()
                   for t in json.loads((_h.value if _h else "") or "[]")}
    except Exception:                                           # noqa: BLE001
        _hidden = set()
    # Never-teams leave the list entirely, alongside anything hidden by hand.
    # One substring covers every Live Nation venue row rather than needing each
    # to be hidden individually.
    # Both columns. A promoter appears as its own team row AND as a note
    # written against a real team, and filtering only the team name left the
    # notes sitting in the Bank Details column of clubs that do play there.
    rows = [r for r in rows
            if (r.team or "").strip().lower() not in _hidden
            and not is_never_row(r.team, r.alias, r.league)]
    _n = db.get(_AS, "team_no_algorithm")
    try:
        _noalgo = {str(t).strip().lower()
                   for t in json.loads((_n.value if _n else "") or "[]")}
    except Exception:                                           # noqa: BLE001
        _noalgo = set()
    st = db.get(SyncStatusRow, "aliases")
    # The hidden teams themselves, so the screen can offer them back. A list
    # you can add to but not see is a trap.
    _hid_names = sorted(json.loads((_h.value if _h else "") or "[]")) if _h else []
    try:
        from .review import alias_status as _alias_status
        _astat = _alias_status(db)
    except Exception:                                           # noqa: BLE001
        _astat = {}
    by_team: dict = {}
    for r in rows:
        by_team.setdefault(r.team, []).append(r.alias)
    from .company_teams import leagues as _dash_leagues
    return {
        # Leagues added with Add League, so the picker offers them before any
        # team uses one.
        "extra_leagues": _dash_leagues(db),
        # The order leagues are shown in, when one has been chosen.
        "league_order": _league_order(db),
        "league_colors": _league_colors(db),
        # Whether this table is the engine's own yet. The button that imports
        # it reads this to decide whether it is offering a takeover or a
        # re-import -- two different promises, and the wrong wording on either
        # is a surprise somebody only notices afterwards.
        "owned": db.query(TeamAliasRow).filter(
            TeamAliasRow.locked.is_(True)).first() is not None,
        # One row per alias, not per team: the grid is a list of what the
        # matcher will actually test, and a team with four aliases is four
        # things that can be right or wrong.
        # Grouped per team, with venue and notes in their own lists -- they are
        # maintained in different Airtable columns and read differently.
        "hidden_teams": _hid_names,
        # GROUPED ONCE, not re-scanned per team.
        #
        # Each of these fields used a generator over the whole row list, so
        # every team walked all of them -- eight times over. At 300 teams and
        # four aliases each that is roughly three million comparisons to build
        # one grid, which is why the Teams tab took half a second on the server
        # before the browser drew anything.
        #
        # One pass into a dict, then a lookup per team.
        "rows": _team_rows(rows, _astat, _noalgo),
        "teams": [{"team": t, "aliases": a} for t, a in sorted(by_team.items())],
        "count": len(rows),
        "last_sync": st.finished_at.isoformat() + "Z" if st and st.finished_at else None,
        "detail": st.detail if st else "",
    }


@app.post("/match/aliases/edit")
def edit_team(team: str = Form(...), rename_to: str = Form(""),
              league: str = Form(""), add_alias: str = Form(""),
              alias_kind: str = Form("note"), drop_alias: str = Form(""),
              # The whole list at once, from the inline editor. An explicit
              # marker rather than "is the value None": an Optional[str] form
              # field arrives as None when it is sent EMPTY, so emptying the
              # box and saving looked identical to not touching it -- the one
              # case where the difference matters.
              set_aliases: str = Form(""), set_list: str = Form(""),
              delete_team: str = Form("false"),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_write)):
    """Edit one team: rename it, change its league, add or drop an alias.

    ONLY ONCE THE TABLE IS OWNED. While the hourly Airtable sync is still
    running it deletes every row and re-inserts them, so an edit made here
    would survive until the next pass and then vanish -- which is worse than
    being told no, because nobody would know it had gone.
    """
    from datetime import datetime as _dt
    from .models_db import TeamAliasRow
    owned = db.query(TeamAliasRow).filter(
        TeamAliasRow.locked.is_(True)).first() is not None
    if not owned:
        raise HTTPException(
            409, "this table is still synced from Airtable — use 'Import from "
                 "Airtable' to take it over before editing here")

    rows = db.query(TeamAliasRow).filter(TeamAliasRow.team == team).all()
    if not rows:
        raise HTTPException(404, f"no team called {team!r}")

    if str(delete_team).lower() in ("1", "true", "yes"):
        # The aliases go with it. A team's aliases are only meaningful as ways
        # of spelling that team, so leaving them would leave rules matching a
        # team that no longer exists.
        for r in rows:
            db.delete(r)
        # And its plans. A plan for a team that no longer exists cannot be
        # reached from any screen and would be silently re-adopted if the same
        # name were created again later.
        from .models_db import PaymentPlanRow as _PP3
        for _pl in db.query(_PP3).filter(_PP3.team == team).all():
            db.delete(_pl)
        db.add(AuditRow(company="", action="team_deleted", actor=user.username,
                        dry_run=False,
                        detail=f"{team} and its {len(rows)} alias(es) deleted"))
        db.commit()
        return {"ok": True, "deleted": len(rows)}

    changed = []
    new_name = rename_to.strip()
    if new_name and new_name != team:
        if db.query(TeamAliasRow).filter(TeamAliasRow.team == new_name).first():
            raise HTTPException(409, f"{new_name!r} already exists")
        for r in rows:
            r.team = new_name
            db.add(r)
        # The PLAN moves with the team. Keyed by name, so a rename without this
        # orphans the plan and the team silently loses its installment check.
        from .models_db import PaymentPlanRow as _PP2
        for _pl in db.query(_PP2).filter(_PP2.team == team).all():
            _pl.team = new_name
            _pl.id = f"{_pl.company}|{new_name}"
            db.add(_pl)
        changed.append(f"renamed from {team} to {new_name}")
        team = new_name

    if league.strip() and rows[0].league != league.strip():
        changed.append(f"league changed from {rows[0].league or '(none)'} "
                       f"to {league.strip()}")
        for r in rows:
            r.league = league.strip()
            db.add(r)

    if drop_alias.strip():
        gone = [r for r in rows if r.alias == drop_alias.strip()]
        for r in gone:
            db.delete(r)
        if gone:
            changed.append(f"alias '{drop_alias.strip()}' removed")

    # THE WHOLE LIST AT ONCE, replacing rather than diffing: the grid sends
    # what is in the box, and working out which entries are new and which have
    # gone would be a second model of the same field, kept in the client, able
    # to disagree with it.
    if set_list and alias_kind in ("venue", "note"):
        want = [a.strip() for a in set_aliases.split(",") if a.strip()]
        have = [r for r in rows if r.kind == alias_kind]
        for r in have:
            if r.alias not in want:
                db.delete(r)
                changed.append(f"{alias_kind} '{r.alias}' removed")
        for a in want:
            if any(r.alias == a for r in have):
                continue
            kid = f"{team}|{alias_kind}|{a.lower()}"
            if db.get(TeamAliasRow, kid) is None:
                db.add(TeamAliasRow(
                    id=kid, team=team, league=league.strip() or rows[0].league,
                    alias=a, kind=alias_kind, source="engine", locked=True,
                    updated_at=_dt.utcnow(),
                    auto_add=bool(rows[0].auto_add),
                    auto_add_refunds=bool(rows[0].auto_add_refunds)))
                changed.append(f"{alias_kind} '{a}' added")

    if add_alias.strip():
        if alias_kind not in ("venue", "note"):
            raise HTTPException(400, "alias kind must be venue or note")
        kid = f"{team}|{alias_kind}|{add_alias.strip().lower()}"
        if db.get(TeamAliasRow, kid) is None:
            db.add(TeamAliasRow(
                id=kid, team=team, league=league.strip() or rows[0].league,
                alias=add_alias.strip(), kind=alias_kind, source="engine",
                locked=True, updated_at=_dt.utcnow(),
                auto_add=bool(rows[0].auto_add),
                auto_add_refunds=bool(rows[0].auto_add_refunds)))
            changed.append(f"alias '{add_alias.strip()}' added as {alias_kind}")

    if changed:
        db.add(AuditRow(company="", action="team_edited", actor=user.username,
                        dry_run=False, detail=f"{team}: " + "; ".join(changed)))
    db.commit()
    # TOO SHORT TO MATCH: saved, but reported, so the person knows it won't
    # identify the team (the grid also shows it in amber).
    from .review import _ALIAS_MIN
    _short = [a for a in (locals().get("want") or [])
              if alias_kind in ("venue", "note") and len(" ".join(a.lower().split())) < _ALIAS_MIN]
    return {"ok": True, "changed": changed, "short": _short, "short_min": _ALIAS_MIN}


@app.post("/match/aliases/add")
def add_team(team: str = Form(...), league: str = Form(""),
             db: Session = Depends(get_session),
             user: UserRow = Depends(require_write)):
    """Create a team with no aliases yet."""
    from datetime import datetime as _dt
    from .models_db import TeamAliasRow
    team = team.strip()
    if not team:
        raise HTTPException(400, "a team name is required")
    if not db.query(TeamAliasRow).filter(TeamAliasRow.locked.is_(True)).first():
        raise HTTPException(
            409, "this table is still synced from Airtable — use 'Import from "
                 "Airtable' to take it over before adding here")
    if db.query(TeamAliasRow).filter(TeamAliasRow.team == team).first():
        raise HTTPException(409, f"{team!r} already exists")
    # A team with no alias matches nothing, which is a fine state to create it
    # in -- the aliases are added on the row afterwards, and refusing until one
    # exists would mean typing the team name twice to get started.
    db.add(TeamAliasRow(
        id=f"{team}|note|{team.lower()}", team=team, league=league.strip(),
        alias=team, kind="note", source="engine", locked=True,
        updated_at=_dt.utcnow()))
    db.add(AuditRow(company="", action="team_added", actor=user.username,
                    dry_run=False, detail=f"{team} added"))
    db.commit()
    return {"ok": True}


def _review_day_label(day):
    from .review import _day_label
    return _day_label(day) or None


@app.post("/match/aliases/auto-add")
def set_team_auto_add(team: str = Form(...), auto_add: str = Form("false"),
                      auto_add_refunds: str = Form("false"),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Trust a team to add its own charges, or stop trusting it.

    Set on every alias row for the team, since any of them can be the one that
    matched a given charge.
    """
    from .models_db import TeamAliasRow
    rows = db.query(TeamAliasRow).filter(TeamAliasRow.team == team).all()
    if not rows:
        raise HTTPException(404, f"No aliases for {team}")
    on = auto_add == "true"
    ref = on and auto_add_refunds == "true"
    for r in rows:
        r.auto_add = on
        # Refunds can only be on if adding is on at all -- otherwise the pair
        # says "do not add, but do add refunds", which means nothing.
        r.auto_add_refunds = ref
        db.merge(r)
    db.add(AuditRow(company="", action="team_auto_add",
                    detail=f"{team}: auto_add={on}, refunds={ref}",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"team": team, "auto_add": on, "auto_add_refunds": ref}


# ALSO WITHOUT A COMPANY IN THE PATH.
#
# Algorithms is an installation-wide screen, so no company is selected while
# you are on it and the client builds "/export/aliases/" with nothing after the
# slash. That is a different route, and it 404'd -- the Excel button on Teams
# and Ignored Words reported "Not Found" and looked broken.
#
# Neither export is company-scoped anyway: team aliases and ignored words
# belong to the installation, like the screen they sit on. So the segment is
# genuinely optional rather than being defaulted away.
@app.get("/export/aliases/")
@app.get("/export/aliases/{company}")
def export_aliases(company: str = "", db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    """The alias list as a spreadsheet."""
    from .export_xlsx import build_workbook, filename
    from .models_db import TeamAliasRow
    rows = (db.query(TeamAliasRow)
            .order_by(TeamAliasRow.team, TeamAliasRow.alias).all())
    blob = build_workbook(
        "Teams Table", ["Team", "Bank detail contains", "Source"],
        [[r.team, r.alias, r.source] for r in rows],
        [("Source", "Synced from the Teams / Venues view in Accounting")])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 # "team" used to be passed as the COMPANY here, so the name came out
                 # "Engine - team - Teams Table". Teams are installation-wide,
                 # so there is no company to name and the helper says
                 # "Dashboard" instead.
                 f'attachment; filename="{filename("aliases", company)}"'})


# The Airtable team sync endpoint is gone.
#
# The Teams table is the engine's own: it is edited here, and the hourly job
# that used to replace it is guarded off. Leaving a route that re-reads
# Airtable over the top of it meant one POST -- from a stale browser tab, a
# bookmark, a script -- could discard every edit since the takeover, with no
# button anywhere to explain where it came from.
#
# The one-time import lives on /teams/take-ownership, which is what a fresh
# install uses and which refuses to run twice destructively.
@app.get("/export/stopwords/")
@app.get("/export/stopwords/{company}")
def export_stopwords(company: str = "", db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    from .export_xlsx import build_workbook, filename
    from .models_db import AppSettingRow
    from engine.suggest import STOPWORDS
    row = db.get(AppSettingRow, "match_stopwords")
    saved = (row.value or "") if row else ""
    words = [w.strip() for w in (saved or ", ".join(sorted(STOPWORDS))).split(",")
             if w.strip()]
    blob = build_workbook("Ignored Words", ["Word"], [[w] for w in words],
                          [("List", "your own" if saved else "built-in")])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("ignored", "words")}"'})


@app.post("/slash/pull")
def slash_pull(company: str = Form(""), since: str = Form(""),
               entity: str = Form(""), account: str = Form(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_write)):
    """Pull Slash now rather than waiting for the schedule.

    `since` is an optional YYYY-MM-DD. Re-pulling an overlapping window is
    free -- dedup is on Slash's own transaction id -- so widening it is the
    right move whenever something looks missing.
    """
    from .ingestion import pull_source
    d = None
    if since.strip():
        try:
            d = datetime.strptime(since.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "since must be YYYY-MM-DD")
    # IN THE BACKGROUND, like Divvy's. Run inside the request, a pull longer
    # than Railway's limit came back as "upstream error" -- although the pull
    # carried on and its transactions arrived. Started on a thread instead:
    # the answer is immediate, and the row shows the result when it is done.
    key = (company, entity.strip(), account.strip())
    if key in _SLASH_RUNNING:
        return {"started": False, "note": "That Slash program is already pulling."}
    _SLASH_RUNNING.add(key)
    actor = user.username

    def _run():
        from .db import SessionLocal
        from .slash_sync import pull_slash as _ps
        s2 = SessionLocal()
        try:
            with _cv.for_company(s2, company):
                # pull_slash, not pull_source: `entity` names ONE program, and
                # the generic entry point has no idea a company can have several.
                _ps(s2, company, d, actor=actor, entity=key[1], account=key[2])
        except Exception:                                       # noqa: BLE001
            log.exception("Slash pull failed for %s", company)
        finally:
            s2.close()
            _SLASH_RUNNING.discard(key)
    threading.Thread(target=_run, daemon=True, name=f"slash-pull-{company}").start()
    return {"started": True, "note": "Slash is pulling in the background \u2014 the row updates when it finishes."}


_SLASH_RUNNING: set = set()


@app.get("/slash/status")
def slash_status(company: str = Query(""), db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Whether the key works, which entities it reaches, and which one this
    company is mapped to.

    Run AS the company: its Slash key is the one entered on Connectors for it.
    Without a company in force the only key reachable was the deployment's,
    so API Card Connections would have shown Slash as not configured the
    moment that stopped being read.
    """
    from . import company_vars as _cvs
    with _cvs.for_company(db, company):
        return _slash_status(company, db)


def _slash_status(company: str, db: Session):
    from .integrations import slash_api
    from .slash_sync import entity_map
    if not slash_api.configured():
        return {"configured": False, "entities": []}
    try:
        ents = sorted(
            ({"id": e.get("id"), "name": e.get("name") or e.get("id")}
             for e in slash_api.list_legal_entities_cached()),
            key=lambda e: (e["name"] or "").lower())
        # TWO ENTITIES CAN SHARE A NAME -- "YSP Tickets LLC" twice in the Slash
        # portal -- and as two identical options there is no telling which is
        # which. Those get the end of their id, which is what the portal
        # itself shows to tell them apart.
        from collections import Counter as _Ctr
        _dup = _Ctr(e["name"] for e in ents)
        for e in ents:
            if _dup[e["name"]] > 1 and e.get("id"):
                e["name"] = f'{e["name"]} \u00b7 \u2026{str(e["id"])[-6:]}'
        # WHO ELSE USES IT. An entity with a program connected to another
        # company says so in the picker, before anybody tries to connect it
        # here (which is refused -- see slash_config).
        from .slash_sync import connections as _sconns
        _used = {}
        _test_here = _is_test_company(db, company)
        for _r in _sconns(db):
            if (_r.get("company") and _r.get("company") != company
                    and not _test_here
                    and not _is_test_company(db, _r["company"])):
                _used.setdefault(_r.get("entity"), set()).add(_r["company"])
        for e in ents:
            if e.get("id") in _used:
                e["used_by"] = sorted(_used[e["id"]])
                e["name"] = f'{e["name"]} \u2014 used by {", ".join(e["used_by"])}'
    except Exception as e:                                      # noqa: BLE001
        return {"configured": True, "error": str(e)[:300], "entities": []}
    from .slash_sync import source_for, source_map
    from .models_db import SourceAccountRow as _SA
    mapping = entity_map(db)
    # The same list the Card Accounts grid shows -- built from QuickBooks bank
    # and credit-card accounts, not only from ones already mapped.
    accounts = _card_account_choices(db, company) if company else []
    # When this company's Slash pull last ran. Read from the upload log rather
    # than stored separately, so it cannot drift from what actually happened.
    last = ""
    if company:
        from .models_db import UploadRow as _UR
        r = (db.query(_UR).filter(_UR.company == company, _UR.method == "pull",
                                  _UR.source == source_for(db, company))
             .order_by(_UR.at.desc()).first())
        if r and r.at:
            last = r.at.isoformat() + "Z"
    _abe = _slash_accounts(db, ents, {c.get("entity") for c in _ss_conns(db, company)})
    # REMEMBER EACH PROGRAM'S NAMES, for Bank / Card Accounts: it shows each
    # API card's connection ("Y&S Tickets Inc · Business Platinum") without
    # asking Slash itself on every open.
    try:
        import json as _jl2
        from .models_db import AppSettingRow
        _en = {e.get("id"): (e.get("name") or "").split(" \u2014 used by")[0] for e in ents}
        for c in _ss_conns(db, company):
            if not c.get("source"):
                continue
            _an = next((x.get("name") for x in (_abe.get(c.get("entity")) or [])
                        if x.get("id") == c.get("account")), "")
            _lab = " \u00b7 ".join(x for x in (_en.get(c.get("entity"), ""), _an) if x)
            if _lab:
                db.merge(AppSettingRow(key=f"api_label:{company}:{c['source']}",
                                       value=_jl2.dumps({"label": _lab})))
        db.commit()
    except Exception:                                   # noqa: BLE001
        db.rollback()
    return {"configured": True, "entities": ents, "mapping": mapping,
            "company": company, "last_pull_at": last,
            "connections": _slash_connections_with_status(db, company),
            # The card ACCOUNTS under each entity -- the secured and unsecured
            # programs. The entity picker alone showed one company and no way
            # to say which program this connection covers.
            "accounts_by_entity": _abe,
            # Free accounts, plus the one Slash already uses so changing
            # something else does not silently drop its own target.
            "card_accounts": ([a for a in _free_card_accounts(
                db, company, ignore=(("slash", ""),))] if company else accounts),
            # The legacy single-value map, ONLY before migration.
            #
            # It is never cleared, so after connections were migrated and one
            # was removed it still named a card account -- and the panel's API
            # badge and the free-account list both believed it. Prefund kept
            # its badge after being disconnected, and could not be reconnected.
            "imports_into": (source_for(db, company)
                             if company and not _ss_migrated(db) else ""),
            "imports_into_set": (bool(source_map(db).get(company))
                                 if company and not _ss_migrated(db) else False),
            "mapped_to": (mapping.get(company) or "") if company else "",
            "fallback_entity_id": _cv.value("slash_legal_entity_id") or "",
            "account_id": _cv.value("slash_account_id") or ""}


@app.post("/slash/entity")
def slash_set_entity(company: str = Form(...), legal_entity_id: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Map one company to one Slash legal entity.

    Per company rather than a single environment variable, because a
    user-scoped key spans every entity its owner can reach and each is a
    separate set of books. An empty id clears the mapping.
    """
    from .models_db import AppSettingRow
    from .slash_sync import ENTITY_MAP_KEY, entity_map
    m = entity_map(db)
    was = m.get(company, "")
    if legal_entity_id.strip():
        m[company] = legal_entity_id.strip()
    else:
        m.pop(company, None)
    row = db.get(AppSettingRow, ENTITY_MAP_KEY) or AppSettingRow(key=ENTITY_MAP_KEY)
    row.value = json.dumps(m, sort_keys=True)
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company=company, action="slash_entity_mapped",
                    detail=f"legal entity changed from {was or '(none)'} "
                           f"to {legal_entity_id or '(none)'}",
                    actor=user.username))
    db.commit()
    return {"company": company, "legal_entity_id": m.get(company, ""),
            "mapping": m}


@app.post("/slash/target")
def slash_set_target(company: str = Form(...), source: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Which card account this company's Slash charges import under.

    Stated rather than assumed. The adapter defaulted to the card account
    literally named "slash", which was right for the first company and is a
    coin flip for the rest.
    """
    from .models_db import AppSettingRow, SourceAccountRow
    from .slash_sync import ACCOUNT_MAP_KEY, source_map
    src = source.strip()
    if src and src not in {a["source"] for a in _card_account_choices(db, company)}:
        raise HTTPException(400, f"'{company}' has no card account '{src}'")
    # One card account, one feed -- across every program, not just this one.
    if src:
        _owner = _feed_owner(db, company, src, ignore=(("slash", ""),))
        if _owner:
            raise HTTPException(
                400, f"{_owner} already imports into that card account — give "
                     f"Slash its own, or the two feeds' charges cannot be told "
                     f"apart")
    # Same as Divvy: an unmapped QuickBooks account becomes a real card account
    # the moment something is set to import into it, rather than staying a bare
    # "qbo:874" key that the picker has no name for.
    if src.startswith("qbo:"):
        from .models_db import QboRefRow as _QR, SourceAccountRow as _SA2
        # THIS COMPANY'S QuickBooks file only. Account ids are numbered per
        # file, so "874" exists in several; looked up by id alone it took the
        # first match -- another company's -- and named a Divvy card account
        # "Prepaid Inventory:NFL:Washington Commanders (TC)", which is why the
        # card picker could not find it by searching "divvy".
        from .review import _company_realm as _crealm
        _realm = _crealm(db, company)
        ref = (db.query(_QR).filter(_QR.kind == "account",
                                    _QR.qbo_id == src.split(":", 1)[1],
                                    _QR.realm_id == _realm).first()
               if _realm else None)
        if ref is not None and not db.query(_SA2).filter(
                _SA2.company == company, _SA2.source == src).first():
            db.add(_SA2(id=f"{company}:{src}", company=company, source=src,
                        nickname=ref.name or src, qbo_account_id=ref.qbo_id,
                        qbo_account_name=ref.name or "",
                        realm_id=ref.realm_id or ""))
            db.commit()
    m = source_map(db)
    was = m.get(company, "")
    if src:
        m[company] = src
    else:
        m.pop(company, None)
    row = db.get(AppSettingRow, ACCOUNT_MAP_KEY) or AppSettingRow(key=ACCOUNT_MAP_KEY)
    row.value = json.dumps(m, sort_keys=True)
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company=company, action="slash_target_set",
                    detail=f"changed from {was or '(default)'} "
                           f"to {src or '(default)'}",
                    actor=user.username))
    db.commit()
    return {"company": company, "source": m.get(company, "")}


@app.get("/teams/exclusions")
def get_team_exclusions(db: Session = Depends(get_session),
                        user: UserRow = Depends(require_reviewer)):
    """Teams excluded from the matcher, from the grid, or both.

    Both lists are maintained here rather than in Airtable, because they are
    decisions about how THIS engine should behave, not facts about the team.
    Airtable stays the record of who the teams are.
    """
    from .models_db import AppSettingRow
    out = {}
    for key in ("team_no_algorithm", "team_hidden"):
        row = db.get(AppSettingRow, key)
        try:
            out[key] = json.loads((row.value if row else "") or "[]")
        except Exception:                                       # noqa: BLE001
            out[key] = []
    return {"no_algorithm": out["team_no_algorithm"],
            "hidden": out["team_hidden"]}


@app.post("/teams/exclusions")
def set_team_exclusions(no_algorithm: str = Form(""), hidden: str = Form(""),
                        db: Session = Depends(get_session),
                        user: UserRow = Depends(require_admin)):
    """Replace both lists. Comma-separated team names, matched case-insensitively."""
    from .models_db import AppSettingRow
    for key, raw in (("team_no_algorithm", no_algorithm), ("team_hidden", hidden)):
        names = [t.strip() for t in (raw or "").split(",") if t.strip()]
        row = db.get(AppSettingRow, key) or AppSettingRow(key=key)
        row.value = json.dumps(names)
        row.updated_at = datetime.utcnow()
        db.add(row)
    db.add(AuditRow(company="", action="team_exclusions_saved",
                    detail=f"no_algorithm={no_algorithm!r} hidden={hidden!r}",
                    actor=user.username))
    db.commit()
    return get_team_exclusions(db=db, user=user)


@app.post("/teams/exclusion")
def set_one_team_exclusion(team: str = Form(...), which: str = Form(...),
                           on: str = Form("true"),
                           db: Session = Depends(get_session),
                           user: UserRow = Depends(require_admin)):
    """Add or remove ONE team from one of the two lists.

    Per team rather than replacing the whole list, because the grid toggles one
    row at a time and a wholesale save would let two people editing different
    rows overwrite each other's change without either of them seeing it.

    `which` is "no_algorithm" or "hidden"; `on` true means excluded.
    """
    from .models_db import AppSettingRow
    key = {"no_algorithm": "team_no_algorithm",
           "hidden": "team_hidden"}.get((which or "").strip())
    if not key:
        raise HTTPException(400, "which must be 'no_algorithm' or 'hidden'")
    row = db.get(AppSettingRow, key) or AppSettingRow(key=key)
    try:
        names = json.loads(row.value or "[]")
    except Exception:                                           # noqa: BLE001
        names = []
    team = (team or "").strip()
    low = team.lower()
    names = [n for n in names if str(n).strip().lower() != low]
    if str(on).strip().lower() in ("true", "1", "yes"):
        names.append(team)
    row.value = json.dumps(sorted(names))
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company="", action=f"team_{which}",
                    detail=f"{team} turned {'on' if on else 'off'}",
                    actor=user.username))
    db.commit()
    return {"team": team, "which": which, "on": str(on).lower() == "true",
            "list": json.loads(row.value)}


@app.get("/canada/rate")
def get_cad_rate(db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, CAD_RATE_KEY)
    from . import fx as _fx
    _src = _fx._get(db, _fx.SOURCE_KEY, {}) or {}
    return {"rate": cad_rate(db),
            # The daily update from the Bank of Canada, and the day it gave.
            "auto": _fx.auto_on(db),
            "source": _src.get("source", "") if isinstance(_src, dict) else "",
            "rate_date": _src.get("date", "") if isinstance(_src, dict) else "",
            "using_default": row is None or not (row.value or "").strip(),
            # Straight from Settings: the rate in use is the one on this
            # screen, stored per company in the database. This is only the
            # figure a fresh install starts from.
            "default": float(settings.cad_usd_rate or 0.73),
            "updated_at": (row.updated_at.isoformat()
                           if row and row.updated_at else "")}


@app.get("/canada/rate/history")
def cad_rate_history(db: Session = Depends(get_session), user: UserRow = Depends(require_reviewer)):
    """Every daily CAD rate kept, newest first -- for the Rate History window."""
    from . import fx as _fx
    h = _fx.history(db) or {}
    rows = []
    for d, r in h.items():
        try:
            rows.append({"date": str(d), "rate": float(r)})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x["date"], reverse=True)
    return {"rates": rows}


@app.post("/canada/rate/history/earlier")
def cad_rate_history_earlier(db: Session = Depends(get_session), user: UserRow = Depends(require_reviewer)):
    """Load every earlier daily rate the Bank of Canada has (Rate History)."""
    from . import fx as _fx
    try:
        return _fx.backfill(db, actor=user.username)
    except Exception as e:                                      # noqa: BLE001
        raise HTTPException(502, f"The Bank of Canada could not be reached: {str(e)[:200]}")


@app.post("/canada/rate/auto")
def set_cad_rate_auto(on: str = Form(...), db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Turn the daily Bank of Canada update on or off; turning it on brings
    the rate up to date straight away."""
    from . import fx as _fx
    want = str(on).lower() in ("1", "true", "yes", "on")
    _fx.set_auto(db, want)
    db.add(AuditRow(company="", action="setting_saved",
                    detail=f"Daily CAD rate from the Bank of Canada: {'on' if want else 'off'}",
                    dry_run=False, actor=user.username))
    db.commit()
    if want:
        try:
            _fx.update(db, actor=user.username, force=True)
        except Exception as e:                          # noqa: BLE001
            raise HTTPException(502, f"The Bank of Canada could not be reached: {e}")
    return get_cad_rate(db=db, user=user)


@app.post("/canada/rate/refresh")
def refresh_cad_rate(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Update Now: the latest Bank of Canada rate."""
    from . import fx as _fx
    try:
        r = _fx.update(db, actor=user.username, force=True)
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(502, f"The Bank of Canada could not be reached: {e}")
    if r.get("error"):
        raise HTTPException(502, r["error"])
    return {**get_cad_rate(db=db, user=user), "changed": r.get("changed", False)}


@app.post("/canada/rate")
def set_cad_rate(rate: str = Form(...), db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Set the rate. Deliberately manual, and range-checked.

    Manual because a rate that moved on its own would make the same charge tie
    out one day and not the next, with nothing in the engine to explain the
    difference -- somebody should be able to say when it changed and to what.

    Range-checked because the realistic failure is not a bad rate but a
    misplaced decimal: typing 73 for 73 cents would multiply every Canadian
    plan by seventy-three and turn a tidy variance into nonsense that still
    renders. CAD/USD has not left 0.60-1.10 in living memory, so anything
    outside a generous band around that is a typo, not a market move.
    """
    try:
        v = float(str(rate).strip().replace(",", ""))
    except Exception:                                           # noqa: BLE001
        raise HTTPException(400, "the rate must be a number, like 0.73")
    if not (0.3 <= v <= 2.0):
        raise HTTPException(
            400, f"{v} is not a plausible CAD/USD rate — expected something "
                 f"near 0.73. One Canadian dollar in US dollars, not cents.")
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, CAD_RATE_KEY) or AppSettingRow(key=CAD_RATE_KEY)
    was = row.value
    row.value = f"{v:.4f}"
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company="", action="cad_rate_set",
                    detail=f"changed from {was or '(default)'} to {row.value}",
                    actor=user.username))
    db.commit()
    # SET BY HAND: the daily update is turned off, so this rate is not
    # replaced overnight; ticking the box again resumes it.
    from . import fx as _fx
    _fx.set_auto(db, False)
    _fx._put(db, _fx.SOURCE_KEY, {"source": f"set by {user.username}", "date": ""})
    db.commit()
    return get_cad_rate(db=db, user=user)


@app.get("/match/never-direct")
def get_never_direct(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """Prefixes that mean a bank detail is a marketplace, not a team."""
    from .models_db import AppSettingRow
    from engine.suggest import DEFAULT_NEVER_DIRECT
    row = db.get(AppSettingRow, "never_direct")
    saved = (row.value or "") if row else ""
    return {"prefixes": saved or ", ".join(DEFAULT_NEVER_DIRECT),
            "using_default": not saved,
            "default": ", ".join(DEFAULT_NEVER_DIRECT)}


@app.post("/match/never-direct")
def save_never_direct(prefixes: str = Form(""),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Replace the list. Empty restores the built-in one."""
    from .models_db import AppSettingRow
    cleaned = ", ".join(sorted({p.strip().lower()
                                for p in (prefixes or "").split(",") if p.strip()}))
    row = db.get(AppSettingRow, "never_direct") or AppSettingRow(key="never_direct")
    row.value = cleaned
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="never_direct_saved",
                    detail="never matched directly: " + (cleaned or "none"),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"prefixes": cleaned, "using_default": not cleaned}


@app.get("/force-match/bills")
def force_bills(company: str = Query(...), q: str = Query(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    """Every open bill, for matching by hand.

    Not the scored candidates -- the whole list. The point of a forced match is
    to reach a bill the matcher ruled out, so filtering it by the same rules
    would defeat it.
    """
    from .models_db import BillRow
    # NOTHING LEFT TO PAY, NOT LISTED: a bill whose balance is $0.00 cannot take
    # a charge. (A bill with no balance recorded is listed, on its amount.)
    qy = db.query(BillRow).filter(BillRow.company == company,
                                  or_(BillRow.balance.is_(None), BillRow.balance != 0))
    needle = (q or "").strip().lower()
    if needle:
        # EVERY COLUMN the window shows: bill number, vendor, memo, date (as
        # written, 10/8/2026, or 2026-10-08), amount and balance ($1,134 or 1134).
        from sqlalchemy import String as _Str, cast as _cast
        like = f"%{needle}%"
        num = needle.replace("$", "").replace(",", "").strip()
        conds = [BillRow.doc_number.ilike(like), BillRow.vendor.ilike(like), BillRow.memo.ilike(like),
                 _cast(BillRow.txn_date, _Str).ilike(like)]
        m = _re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", needle)
        if m:
            mm, dd, yy = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3) or ""
            yy = ("20" + yy) if len(yy) == 2 else yy
            conds.append(_cast(BillRow.txn_date, _Str).ilike(f"%{yy}-{mm}-{dd}%" if yy else f"%-{mm}-{dd}%"))
        if _re.fullmatch(r"\d+(\.\d*)?", num):
            conds += [_cast(BillRow.amount, _Str).ilike(f"%{num}%"), _cast(BillRow.balance, _Str).ilike(f"%{num}%")]
        qy = qy.filter(or_(*conds))
    rows = qy.order_by(BillRow.txn_date.desc()).limit(200).all()
    return {"bills": [{
        "bill_id": b.bill_id,
        "doc_number": b.doc_number or "",
        "vendor": b.vendor or "",
        "date": str(b.txn_date or ""),
        "amount": str(b.amount or ""),
        "balance": str(b.balance if b.balance is not None else (b.amount or "")),
        "memo": (b.memo or "")[:200],
    } for b in rows]}


@app.post("/queue/force-match")
def force_match(charge_id: str = Form(...), bill_id: str = Form(""),
                bills: str = Form(""), mode: str = Form("adjust"),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    """Match a charge to a bill the matcher did not offer.

    `mode` says what to do about the amount difference:
      adjust -- correct the bill to the charge (the ordinary rounding case)
      keep   -- leave the bill and pay only what the charge covers

    Recorded on the charge, because the WRITE path checks the same tolerances
    again: without the flag this match would be accepted here and refused at
    post time.
    """
    # "fees": like adjust, but the difference goes on as an "Other Fees" line
    # rather than changing the bill's amount.
    if mode not in ("adjust", "keep", "fees"):
        raise HTTPException(400, "mode must be 'adjust', 'keep' or 'fees'")
    from .models_db import ChargeRow
    row = db.query(ChargeRow).filter(ChargeRow.charge_id == charge_id).first()
    if row is None:
        raise HTTPException(404, "No such charge")
    if row.is_credit:
        raise HTTPException(
            400, "That is a refund — code it as an expense instead of matching "
                 "it to a bill.")
    if row.qbo_txn_id:
        raise HTTPException(
            400, "Already posted to QuickBooks. Undo it first.")
    from decimal import Decimal
    from .models_db import BillRow, ChargeBillMatchRow
    import json as _bj
    # One bill or several. `bills` is [{"bill_id": ..., "amount": ...}, ...];
    # `bill_id` is the single-bill form and still works.
    picked = []
    if bills:
        try:
            raw = _bj.loads(bills) or []
        except ValueError:
            raise HTTPException(400, "Could not read the selected bills")
        for ln in raw if isinstance(raw, list) else []:
            bid = str((ln or {}).get("bill_id") or "").strip()
            if not bid:
                continue
            try:
                amt = Decimal(str((ln or {}).get("amount") or "0"))
            except (ArithmeticError, ValueError):
                raise HTTPException(400, f"Bad amount for bill {bid}")
            picked.append((bid, amt))
    elif bill_id:
        picked = [(bill_id, Decimal(str(row.amount)))]
    if not picked:
        raise HTTPException(400, "Pick at least one bill")

    bill_rows = []
    for bid, amt in picked:
        b = db.get(BillRow, bid)
        if b is None:
            raise HTTPException(404, f"No such bill: {bid}")
        bill_rows.append((b, amt))

    # QuickBooks allows ONE vendor per bill payment, so a selection spanning two
    # vendors cannot be posted as a single payment. Refused here, while the
    # reviewer is still looking at the list -- at post time it is too late to
    # tell anybody, and the row would already have moved.
    vendors = {(b.vendor or "").strip().lower() for b, _ in bill_rows}
    if len(vendors) > 1:
        raise HTTPException(
            400, "Those bills belong to different vendors ("
                 + ", ".join(sorted({(b.vendor or "?") for b, _ in bill_rows}))
                 + "). A single payment can only cover one vendor.")

    if len(bill_rows) > 1:
        # The lines have to total the charge: QuickBooks rejects a payment whose
        # lines disagree with its total, and a silent shortfall would leave a
        # bill looking part-paid for reasons nobody recorded.
        allocated = sum((a for _, a in bill_rows), Decimal("0"))
        if abs(allocated - Decimal(str(row.amount))) > Decimal("0.01"):
            raise HTTPException(
                400, f"The amounts add up to {allocated}, but the charge is "
                     f"{row.amount}.")
        for b, amt in bill_rows:
            bal = b.balance if b.balance is not None else b.amount
            if bal is not None and amt > bal + Decimal("0.01"):
                raise HTTPException(
                    400, f"Bill {b.doc_number or b.bill_id} only has {bal} "
                         f"owing — {amt} would overpay it.")

    bill = bill_rows[0][0]
    # Refused here as well as at post time, so the reviewer is told now rather
    # than after the row has moved. Only meaningful for a single bill: across
    # several, the per-bill checks above are the test.
    from .integrations.qbo_write import match_is_postable
    if len(bill_rows) == 1:
        verdict = match_is_postable(row.amount, bill, forced=mode)
        if not verdict["ok"]:
            raise HTTPException(400, verdict["reason"])
    else:
        verdict = {"ok": True, "gap": Decimal("0"),
                   "reason": f"forced match across {len(bill_rows)} bills"}

    db.query(ChargeBillMatchRow).filter(
        ChargeBillMatchRow.charge_id == charge_id).delete()
    if len(bill_rows) > 1:
        for i, (b, amt) in enumerate(bill_rows):
            db.add(ChargeBillMatchRow(charge_id=charge_id, bill_id=b.bill_id,
                                      amount=amt, position=i))

    row.status, row.resolution = "categorized", "matched"
    row.matched_bill_id = bill.bill_id
    row.coded_category = row.coded_vendor = ""
    row.matched_bill_no = bill.doc_number or bill.bill_id or ""
    row.matched_bill_vendor = bill.vendor or ""
    row.matched_bill_date = str(bill.txn_date or "")
    row.matched_bill_memo = (bill.memo or "")[:500]
    row.force_matched = True
    row.force_mode = mode
    row.resolved_at = datetime.utcnow()
    row.resolved_by = user.username
    db.add(AuditRow(
        company=row.company, action="force_match",
        detail=(f"{charge_id} -> "
                + (", ".join(f"bill {b.doc_number or b.bill_id} ${a}"
                             for b, a in bill_rows)
                   if len(bill_rows) > 1
                   else f"bill {row.matched_bill_no} "
                        f"(${row.amount} vs ${bill.amount})")
                + f"; mode={mode}: "
                + ("bill will be adjusted to the charge" if mode == "adjust"
                   else "bill left as is, part payment")),
        dry_run=False, actor=user.username))
    db.commit()

    # Post straight away, exactly as /queue/resolve does for an ordinary match.
    # Without this the charge moved to Categorized and stopped -- nothing else
    # posts it, so a forced match never reached QuickBooks at all. Categorized
    # is meant to mean "this exists in the ledger"; a row that never posted has
    # no business sitting there.
    posted = None
    try:
        from .integrations.qbo_write import push_charge, WriteBlocked
        if not settings.dry_run and settings.qbo_write_enabled:
            try:
                res = push_charge(db, row, user.username)
                posted = res.get("status")
                if res.get("status") == "failed":
                    # Same rule as a normal match: nothing reached QuickBooks,
                    # so the charge goes back rather than pretending to be done.
                    row.status, row.resolution = "for_review", ""
                    row.matched_bill_id = ""
                    row.force_matched = False
                    row.force_mode = None
                    db.query(ChargeBillMatchRow).filter(
                        ChargeBillMatchRow.charge_id == charge_id).delete()
                    db.add(AuditRow(
                        company=row.company, action="force_match_post_failed",
                        detail=str(res.get("reason") or res.get("error") or "")[:500],
                        dry_run=False, actor=user.username))
                    db.commit()
                    raise HTTPException(
                        400, "Matched, but QuickBooks refused it: "
                             + str(res.get("reason") or res.get("error") or ""))
            except WriteBlocked as e:
                posted = f"not posted ({e})"
    except HTTPException:
        raise
    except Exception as e:                  # noqa: BLE001
        log.exception("force match posted the match but the write failed")
        posted = f"error: {e}"
    db.commit()
    return {"ok": True, "gap": str(verdict.get("gap") or ""),
            "posted": posted,
            "reason": verdict.get("reason") or ""}


@app.post("/rules/split")
def save_rule_split(rule_id: str = Form(...), payload: str = Form("[]"),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_admin)):
    """Set (or clear) the split on a coding rule.

    Percentages, not amounts: a rule applies to charges of different sizes, so
    a fixed dollar split would only ever be right for one of them. They must
    total 100, because a rule that codes 90% of a charge leaves the rest
    silently uncoded.
    """
    import json as _json
    from .models_db import LearnedRuleRow
    rule = db.get(LearnedRuleRow, rule_id)
    if rule is None:
        raise HTTPException(404, "No such rule")
    try:
        raw = _json.loads(payload or "[]")
    except ValueError:
        raise HTTPException(400, "Could not read those lines")
    from .models_db import QboRefRow as _QR
    lines = []
    for ln in raw if isinstance(raw, list) else []:
        cat = str((ln or {}).get("category") or "").strip()
        try:
            pct = round(float((ln or {}).get("percent") or 0), 4)
        except (TypeError, ValueError):
            pct = 0.0
        if cat and pct > 0:
            # The id as well as the name, so a rename in QuickBooks flows
            # through here as it now does for the rule's own category.
            hit = _ref_by_name(db, rule.company, "account", cat)
            lines.append({"category": cat, "percent": pct,
                          "category_id": hit.qbo_id if hit else None})
    if lines:
        if len(lines) < 2:
            raise HTTPException(400, "A split needs at least two categories")
        total = round(sum(l["percent"] for l in lines), 4)
        if abs(total - 100.0) > 0.01:
            raise HTTPException(
                400, f"The percentages total {total:g}%, not 100%")
    rule.split_json = _json.dumps(lines) if lines else None
    db.merge(rule)
    db.add(AuditRow(company=rule.company, action="rule_split_saved",
                    detail=f"{rule.name or rule.detail_value}: "
                           + (", ".join(f"{l['category']} {l['percent']:g}%"
                                        for l in lines) or "(cleared)"),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"split": lines}


@app.get("/match/tie-settings")
def get_tie_settings(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """What counts as tying out to the expected charge."""
    from .models_db import AppSettingRow
    from .review import current_tie_settings
    import json as _j
    row = db.get(AppSettingRow, "tie_settings")
    if row and row.value:
        try:
            return {"tie": {**current_tie_settings(), **_j.loads(row.value)}}
        except ValueError:
            pass
    return {"tie": current_tie_settings()}


@app.post("/match/tie-settings")
def save_tie_settings(days: str = Form("3"), weekend: str = Form("true"),
                      pct: str = Form("1.0"),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    from .models_db import AppSettingRow
    from .review import set_tie_settings
    import json as _j
    try:
        d = max(0, min(31, int(float(days))))
        p = max(0.0, min(100.0, float(pct)))
    except ValueError:
        raise HTTPException(400, "Days and percentage have to be numbers")
    val = {"days": d, "weekend": weekend == "true", "pct": p}
    row = db.get(AppSettingRow, "tie_settings") or AppSettingRow(key="tie_settings")
    row.value = _j.dumps(val)
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="tie_settings_saved",
                    detail=f"{d} day(s), weekend={val['weekend']}, {p}%",
                    dry_run=False, actor=user.username))
    db.commit()
    set_tie_settings(val)
    return {"tie": val}


@app.get("/match/payment-settings")
def get_payment_settings(db: Session = Depends(get_session),
                         user: UserRow = Depends(require_reviewer)):
    """Whether card payments are coded automatically."""
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, "payment_autocode")
    return {"autocode": (row.value or "false") == "true" if row else False}


@app.post("/match/payment-settings")
def save_payment_settings(autocode: str = Form("true"),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    from .models_db import AppSettingRow
    from .review import load_match_weights
    row = db.get(AppSettingRow, "payment_autocode") or AppSettingRow(
        key="payment_autocode")
    row.value = "true" if autocode == "true" else "false"
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="payment_settings_saved",
                    detail=f"autocode={row.value}", dry_run=False,
                    actor=user.username))
    db.commit()
    load_match_weights(db)
    return {"ok": True}


@app.get("/match/synonyms")
def get_synonyms(db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """City and league abbreviations treated as the same word."""
    from .models_db import AppSettingRow
    from .review import DEFAULT_SYNONYMS
    import json as _json
    row = db.get(AppSettingRow, "word_synonyms")
    if row is None or not row.value:
        return {"synonyms": DEFAULT_SYNONYMS, "using_default": True,
                "default": DEFAULT_SYNONYMS}
    try:
        pairs = _json.loads(row.value) or {}
    except Exception:                       # noqa: BLE001
        pairs = {}
    return {"synonyms": pairs, "using_default": False,
            "default": DEFAULT_SYNONYMS}


@app.post("/match/synonyms")
def save_synonyms(pairs: str = Form(""), restore: str = Form(""),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    """Replace the table. `pairs` is "short=long" per line."""
    from .models_db import AppSettingRow
    from .review import DEFAULT_SYNONYMS, load_match_weights
    import json as _json
    if restore == "true":
        db.query(AppSettingRow).filter(
            AppSettingRow.key == "word_synonyms").delete()
        db.commit()
        load_match_weights(db)
        return {"synonyms": DEFAULT_SYNONYMS, "using_default": True}
    out = {}
    for line in (pairs or "").replace(",", "\n").splitlines():
        if "=" not in line:
            continue
        a, b = line.split("=", 1)
        a, b = a.strip().lower(), b.strip().lower()
        # A short form that is not shorter is a typo, and one that equals its
        # expansion would rewrite text to itself forever.
        if a and b and a != b:
            out[a] = b
    row = db.get(AppSettingRow, "word_synonyms") or AppSettingRow(key="word_synonyms")
    row.value = _json.dumps(out)
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="synonyms_saved",
                    detail=f"{len(out)} abbreviation(s)", dry_run=False,
                    actor=user.username))
    db.commit()
    load_match_weights(db)
    return {"synonyms": out, "using_default": False}


@app.get("/match/virtual-programs")
def get_virtual_programs(company: str = Query(""),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_reviewer)):
    """Name fragments that earn a card account the Virtual badge.

    Also returns what the current list actually badges, because the answer that
    matters is "which of my accounts is this calling virtual", not the list of
    words in the abstract.
    """
    from .models_db import AppSettingRow, SourceAccountRow
    from engine.suggest import DEFAULT_VIRTUAL_PROGRAMS, is_virtual_program
    row = db.get(AppSettingRow, "virtual_programs")
    if row is None:
        programs = list(DEFAULT_VIRTUAL_PROGRAMS)
        using_default = True
    else:
        programs = [p.strip() for p in (row.value or "").split(",") if p.strip()]
        using_default = False

    # Judged against the list as SAVED, not the engine's live copy, so the
    # preview matches the table above it even before a reload.
    def _by_name(text: str) -> bool:
        t = (text or "").lower()
        return any(p in t for p in programs)

    qy = db.query(SourceAccountRow)
    if company:
        qy = qy.filter(SourceAccountRow.company == company)
    accounts = []
    for r in qy.order_by(SourceAccountRow.source).all():
        text = f"{r.nickname or ''} {r.qbo_account_name or ''} {r.source or ''}"
        hit = _by_name(text)
        accounts.append({
            "source": r.source,
            "name": (r.nickname or r.qbo_account_name or r.source),
            "virtual": bool(hit or r.is_virtual),
            # Which of the two reasons applied, so a badge nobody expected can
            # be traced to the row that caused it.
            "why": ("name" if hit else "ticked" if r.is_virtual else ""),
        })
    return {"programs": programs, "using_default": using_default,
            "default": list(DEFAULT_VIRTUAL_PROGRAMS), "accounts": accounts}


@app.post("/match/virtual-programs")
def save_virtual_programs(programs: str = Form(""),
                          restore: str = Form(""),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    """Replace the list. `restore=true` puts the built-in list back.

    An empty list is saved as empty and honored: "no program is virtual by
    name" is a real answer, and the per-account tick on Card Accounts is still
    there for the exceptions.
    """
    from .models_db import AppSettingRow
    from engine.suggest import DEFAULT_VIRTUAL_PROGRAMS
    from .review import load_match_weights

    if restore == "true":
        db.query(AppSettingRow).filter(
            AppSettingRow.key == "virtual_programs").delete()
        db.add(AuditRow(company="", action="virtual_programs_restored",
                        detail=", ".join(DEFAULT_VIRTUAL_PROGRAMS),
                        dry_run=False, actor=user.username))
        db.commit()
        load_match_weights(db)
        return {"programs": list(DEFAULT_VIRTUAL_PROGRAMS), "using_default": True}

    cleaned = sorted({p.strip().lower()
                      for p in (programs or "").split(",") if p.strip()})
    row = (db.get(AppSettingRow, "virtual_programs")
           or AppSettingRow(key="virtual_programs"))
    row.value = ", ".join(cleaned)
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="virtual_programs_saved",
                    detail=row.value or "(none)",
                    dry_run=False, actor=user.username))
    db.commit()
    # Push straight into the engine so the badge changes on the next render
    # rather than waiting for whatever else happens to reload the settings.
    load_match_weights(db)
    return {"programs": cleaned, "using_default": False}


@app.get("/match/stopwords")
def get_stopwords(db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """The words ignored when comparing text, and the built-in default."""
    from .models_db import AppSettingRow
    from engine.suggest import STOPWORDS
    row = db.get(AppSettingRow, "match_stopwords")
    saved = (row.value or "") if row else ""
    return {"words": saved or ", ".join(sorted(STOPWORDS)),
            "using_default": not saved,
            "default": ", ".join(sorted(STOPWORDS))}


@app.post("/match/stopwords")
def set_stopwords_endpoint(words: str = Form(""),
                           db: Session = Depends(get_session),
                           user: UserRow = Depends(require_admin)):
    """Replace the ignored-word list. Empty restores the built-in one."""
    from .models_db import AppSettingRow
    cleaned = ", ".join(sorted({w.strip().lower()
                                for w in (words or "").split(",") if w.strip()}))
    row = db.get(AppSettingRow, "match_stopwords") or AppSettingRow(
        key="match_stopwords")
    row.value = cleaned
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.merge(row)
    db.add(AuditRow(company="", action="stopwords_saved",
                    detail=f"{len(cleaned.split(',')) if cleaned else 0} word(s)",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"words": cleaned, "using_default": not cleaned}


@app.get("/match/rules")
def list_match_rules(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """Standing exceptions to how a bank detail is read when matching bills."""
    from .models_db import MatchRuleRow
    rows = db.query(MatchRuleRow).order_by(MatchRuleRow.bank_detail).all()
    return {"rules": [{"id": r.id, "bank_detail": r.bank_detail,
                       "item": getattr(r, "item", "bank_detail"),
                       "op": getattr(r, "op", "contains"),
                       "bill_vendor": r.bill_vendor,
                       "ignore_email": bool(r.ignore_email),
                       "ignore_text": bool(r.ignore_text),
                       "active": bool(r.active), "note": r.note} for r in rows]}


@app.post("/match/rules")
def save_match_rule(bank_detail: str = Form(""), bill_vendor: str = Form(""),
                    item: str = Form("bank_detail"), op: str = Form("contains"),
                    ignore_email: str = Form("false"),
                    ignore_text: str = Form("false"),
                    rule_id: str = Form(""), active: str = Form("true"),
                    remove: str = Form(""),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_admin)):
    """Add, change or remove one match rule."""
    from .models_db import MatchRuleRow
    from .review import load_match_weights

    if remove and rule_id:
        db.query(MatchRuleRow).filter(MatchRuleRow.id == int(rule_id)).delete()
        db.commit()
        load_match_weights(db)
        return {"removed": rule_id}

    phrase = (bank_detail or "").strip().lower()
    if not phrase:
        raise HTTPException(400, "A value is required")
    vendor = (bill_vendor or "").strip()
    ig_email = ignore_email == "true"
    ig_text = ignore_text == "true"
    # A rule that names no vendor and switches nothing off would sit in the
    # table looking meaningful and do nothing at all.
    if not vendor and not ig_email and not ig_text:
        raise HTTPException(400, "Name a vendor or switch a signal off — "
                                 "otherwise the rule has no effect")

    row = (db.query(MatchRuleRow).filter(MatchRuleRow.id == int(rule_id)).first()
           if rule_id else None)
    if row is None:
        row = MatchRuleRow(bank_detail=phrase)
        db.add(row)
    if item not in ("bank_detail", "vendor"):
        raise HTTPException(400, "Item must be Bank Detail or Vendor")
    if op not in ("contains", "equals"):
        raise HTTPException(400, "Rule must be Contains or Equals")
    row.bank_detail, row.bill_vendor = phrase, vendor
    row.item, row.op = item, op
    row.ignore_email, row.ignore_text = ig_email, ig_text
    row.active = active != "false"
    db.add(AuditRow(company="", action="match_rule_saved",
                    detail=(f"'{phrase}'"
                            + (f" -> {vendor}" if vendor else "")
                            + (" | no buyer email" if ig_email else "")
                            + (" | no performer text" if ig_text else "")),
                    dry_run=False, actor=user.username))
    db.commit()
    load_match_weights(db)
    return {"saved": row.id}


@app.get("/payments/rules")
def list_payment_rules(db: Session = Depends(get_session),
                       user: UserRow = Depends(require_reviewer)):
    """The rules that mark a credit as a payment rather than a refund."""
    from .models_db import PaymentRuleRow
    rows = db.query(PaymentRuleRow).order_by(PaymentRuleRow.item,
                                             PaymentRuleRow.phrase).all()
    return {"rules": [{"id": r.id, "phrase": r.phrase, "item": r.item,
                       "rule": r.rule, "active": bool(r.active),
                       "note": r.note} for r in rows]}


@app.post("/payments/rules")
def save_payment_rule(phrase: str = Form(...), item: str = Form("bank_detail"),
                      rule: str = Form("contains"), rule_id: str = Form(""),
                      active: str = Form("true"), remove: str = Form(""),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Add, change or remove one payment rule."""
    from .models_db import PaymentRuleRow
    from .review import reset_payment_rules

    if remove and rule_id:
        db.query(PaymentRuleRow).filter(PaymentRuleRow.id == int(rule_id)).delete()
        db.commit()
        reset_payment_rules()
        return {"removed": rule_id}

    phrase = (phrase or "").strip().lower()
    if not phrase:
        raise HTTPException(400, "An input is required")
    if item not in ("vendor", "bank_detail", "card_account"):
        raise HTTPException(400, "Item must be Vendor, Bank Detail or Card Account")
    if rule not in ("equals", "contains"):
        raise HTTPException(400, "Rule must be Equals or Contains")

    row = (db.query(PaymentRuleRow).filter(PaymentRuleRow.id == int(rule_id)).first()
           if rule_id else None)
    if row is None:
        row = PaymentRuleRow(phrase=phrase)
        db.add(row)
    row.phrase, row.item, row.rule = phrase, item, rule
    row.active = active != "false"
    db.add(AuditRow(company="", action="payment_rule_saved",
                    detail=f"{item} {rule} '{phrase}'", dry_run=False,
                    actor=user.username))
    db.commit()
    reset_payment_rules()
    return {"saved": row.id}


@app.get("/export/cards/{company}")
def export_cards(company: str, companies: str = Query(""),
                 q: str = Query(""), kinds: str = Query(""), retired: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """The Card Accounts grid as a workbook."""
    if company == ALL_COMPANIES:
        return _multi_export(export_cards, dict(locals()), "cards")
    # THE GRID'S OWN COLUMNS, from the same data the screen draws from.
    from decimal import Decimal as _Dc
    from . import export_xlsx as X
    m = account_mapping(company=company, companies="", db=db, user=user)
    try:
        plaid = plaid_status(company=company, companies="", db=db, user=user).get("connected") or {}
    except Exception:                                   # noqa: BLE001
        plaid = {}
    fed = set(m.get("api_fed") or [])
    money = lambda v: _Dc(str(v)) if v not in (None, "") else None
    rows = []
    for p_ in m.get("programs") or []:
        src = p_.get("source", "")
        feed = "API" if src in fed else "Plaid" if src in plaid else "None"
        rows.append([p_.get("nickname") or p_.get("qbo_account_name") or src,
                     p_.get("qbo_account_type") or "", p_.get("qbo_account_subtype") or "",
                     money(p_.get("qbo_balance")), money(p_.get("bank_balance")),
                     p_.get("statement_date") or "", money(p_.get("statement_balance")),
                     feed, "Retired" if p_.get("qbo_active") is False else "Active"])
    rows.sort(key=lambda r: (str(r[1]), str(r[0]).lower()))
    # THE GRID'S OWN FILTERS: retired hidden unless shown; card types as the
    # picker reads them; the quick search on the name.
    want = {k for k in kinds.split(",") if k}
    show_retired = retired.lower() in ("1", "true", "yes")
    kept = []
    for p_, r in zip(sorted(m.get("programs") or [], key=lambda p_: (
            str(p_.get("qbo_account_type") or ""),
            str(p_.get("nickname") or p_.get("qbo_account_name") or p_.get("source", "")).lower())), rows):
        api, pl, virt = r[7] == "API", r[7] == "Plaid", bool(p_.get("is_virtual"))
        if r[8] == "Retired" and not show_retired:
            continue
        # CONNECTION ISSUES ONLY: an API feed quiet over a day (or never
        # pulled), or a bank feed that needs signing in again or is erroring.
        if "issues" in want:
            _src = p_.get("source", "")
            _h = (m.get("api_stale_hours") or {}).get(_src)
            _pst = (plaid.get(_src) or {}).get("status") if isinstance(plaid.get(_src), dict) else None
            if not ((api and (_h is None or _h > 24)) or (_pst and _pst != "ok")):
                continue
        _kinds = want - {"issues"}
        if _kinds and not (("api" in _kinds and api) or ("plaid" in _kinds and pl) or ("virtual" in _kinds and virt)
                           or ("physical" in _kinds and not virt) or ("none" in _kinds and not api and not pl)):
            continue
        if q.strip() and q.strip().lower() not in str(r[0]).lower():
            continue
        kept.append(r)
    rows = kept
    names = {"api": "API", "plaid": "Plaid", "virtual": "Virtuals", "physical": "Non-Virtuals", "none": "None",
             "issues": "Connection Issues Only"}
    applied_extra = ([("Card Types", ", ".join(names.get(k, k) for k in sorted(want)))] if want else []) \
        + [("Retired accounts", "shown" if show_retired else "hidden")] \
        + ([("Search", q.strip())] if q.strip() else [])
    blob = X.build_workbook(
        "Bank & Card Accounts",
        ["Bank / Card Accounts", "Type", "Subtype", "QBO Balance", "Bank Balance",
         "Last Statement Date", "Last Statement Balance", "Bank Feed", "Status"],
        rows, [("Company", company), ("Exported by", user.username)] + applied_extra, money_cols=[3, 4, 6])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{X.filename("cards", company)}"'})


# kind -> (the function that already serves that grid, the key holding its
# rows, the sheet title).
#
# Built from the SAME source the screen reads rather than from the models
# directly. These six grids are served by /match/* endpoints that assemble
# their rows from more than one place, so a second query written here would be
# a second definition of "what is in this table" -- and the export would drift
# from the grid the first time either changed.
def _simple_tables():
    return {
        "ignored":     (get_stopwords,    "words",    "Ignored Words"),
        "marketplace": (get_never_direct, "names",    "Marketplaces"),
        "abbrev":      (get_synonyms,     "synonyms", "Abbreviations"),
        "tie":         (get_tie_settings, "tie",      "Tie Breakers"),
        # Takes a company, unlike the four above, so the caller passes one.
        "virtual":     (get_virtual_programs, "programs", "Virtual Cards"),
    }


def _rows_from(payload):
    """A list of dicts, or of strings, as headers and rows.

    The /match endpoints return whichever shape suited them -- a list of words,
    a list of pairs, a dict of settings. Rather than teach each one a table
    format, this reads whatever came back.
    """
    if isinstance(payload, dict):
        return ["Setting", "Value"], [[k, str(v)] for k, v in payload.items()]
    rows = list(payload or [])
    if rows and isinstance(rows[0], dict):
        head = list(rows[0].keys())
        return ([h.replace("_", " ").title() for h in head],
                [[r.get(h, "") for h in head] for r in rows])
    return ["Value"], [[r] for r in rows]


@app.get("/export/tables/{kind}")
def export_tables(kind: str, company: str = Query(""),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """Whichever Tables grid is on screen, as a workbook."""
    from . import export_xlsx as X
    from .models_db import (CanadaRuleRow, PaymentRuleRow, LearnedRuleRow,
                             MatchRuleRow)

    label = {"vendor": "Vendor", "bank_detail": "Bank Detail",
             "card_account": "Card Account"}
    if kind == "matchrules":
        rows = [[r.bank_detail, r.bill_vendor or "",
                 "Off" if r.ignore_email else "On",
                 "Off" if r.ignore_text else "On",
                 "Yes" if r.active else "No", r.note or ""]
                for r in db.query(MatchRuleRow).order_by(
                    MatchRuleRow.bank_detail).all()]
        head = ["Bank Detail contains", "Treat as Vendor", "Buyer Email",
                "Performer Text", "Active", "Note"]
        title = "Match Rules"
    elif kind == "coding":
        # The same columns the grid shows, in the same order, so an export can
        # be edited and uploaded straight back.
        _mode = {"off": "Off", "prefill": "Prefill",
                 "prefill_post": "Prefill & Post"}
        _dop = {"contains": "Contains", "equals": "Equals"}
        _aop = {"any": "Any", "equals": "Equals", "lt": "Less Than",
                "gt": "Greater Than", "between": "Between"}
        from .learning import rules_for
        rows = [[r.name or "", _dop.get(r.detail_op or "contains", "Contains"),
                 r.detail_value or "", r.card_account or "",
                 _aop.get(r.amount_op or "any", "Any"),
                 float(r.amount_value) if r.amount_value is not None else None,
                 float(r.amount_value2) if r.amount_value2 is not None else None,
                 r.vendor or "", r.category or "",
                 _mode.get(r.mode or "prefill", "Prefill")]
                for r in rules_for(db, company)]
        head = ["Rule Name", "Bank Detail Rule", "Bank Detail Value",
                "Card Account", "Amount Rule", "Amount Value", "Amount Value 2",
                "Vendor", "Category", "Auto"]
        title = "Vendor & Category Rules"
    elif kind in ("canada", "payment"):
        model = CanadaRuleRow if kind == "canada" else PaymentRuleRow
        rows = []
        for r in db.query(model).all():
            if getattr(r, "kind", "") == "city_province":
                rows.append(["Bank Detail", f"Contains · {r.provinces}", r.phrase])
            else:
                rows.append([label.get(r.item, "Bank Detail"),
                             "Equals" if r.rule == "equals" else "Contains",
                             r.phrase])
        head = ["Item", "Rule", "Input"]
        title = "Canada Tags" if kind == "canada" else "Payment Tags"
    elif kind == "weights":
        from engine.suggest import DEFAULT_WEIGHTS
        from .models_db import MatchWeightRow
        saved = {r.key: r.weight for r in db.query(MatchWeightRow).all()}
        rows = [[WEIGHT_LABELS[k][0], WEIGHT_LABELS[k][1], saved.get(k, d), d]
                for k, d in DEFAULT_WEIGHTS.items()]
        rows.sort(key=lambda r: (-r[2], r[0]))
        head = ["Signal", "What it means", "Weight", "Default"]
        title = "Matching Algorithm"
    elif kind == "ccvendors":
        from .review import cc_vendors as _ccv
        rows = [[x.get("match", ""), x.get("vendor", "")] for x in _ccv(db)]
        head = ["Card Name Contains", "Vendor"]
        title = "CC Vendors"
    elif kind in _simple_tables():
        # THE ONE-COLUMN AND TWO-COLUMN TABLES.
        #
        # Six kinds reached this endpoint from their own Excel buttons and fell
        # straight through to the 400 below: ignored words, marketplaces,
        # abbreviations, virtual cards, tie breakers and team aliases. Every
        # one of those grids draws an export button, so every one of them had a
        # button that reported "not found" when pressed. The buttons were
        # right; the endpoint only ever knew about the kinds that happened to
        # be written first.
        fn, key, title = _simple_tables()[kind]
        # Only some of these take a company; passing one to a handler that does
        # not would fail, and leaving it off one that does would export another
        # company's rows.
        import inspect as _inspect
        _kw = {"db": db, "user": user}
        if "company" in _inspect.signature(fn).parameters:
            _kw["company"] = company
        payload = fn(**_kw)
        head, rows = _rows_from(payload.get(key, payload))
    else:
        raise HTTPException(400, "Unknown table")

    blob = X.build_workbook(title, head, rows,
                            [("Company", company), ("Exported by", user.username)])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{X.filename(kind, company)}"'})


@app.get("/canada/rules")
def list_canada_rules(db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    """The phrases that mark a charge as Canadian."""
    from .models_db import CanadaRuleRow
    rows = db.query(CanadaRuleRow).order_by(CanadaRuleRow.kind,
                                            CanadaRuleRow.phrase).all()
    return {"rules": [{"id": r.id, "phrase": r.phrase, "kind": r.kind,
                       "item": r.item, "rule": r.rule,
                       "provinces": r.provinces, "active": bool(r.active),
                       "note": r.note} for r in rows]}


@app.post("/canada/rules")
def save_canada_rule(phrase: str = Form(...), kind: str = Form("phrase"),
                     item: str = Form("bank_detail"), rule: str = Form("contains"),
                     provinces: str = Form(""), rule_id: str = Form(""),
                     active: str = Form("true"), remove: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Add, change or remove one rule, then rescore.

    A city rule needs its provinces: several Canadian city names are more
    commonly US ones in this data, so a bare city would mislabel every
    'ONTARIO CA' and 'VANCOUVER WA' as Canadian.
    """
    from .models_db import CanadaRuleRow
    from .canada import reset_rules

    if remove and rule_id:
        db.query(CanadaRuleRow).filter(CanadaRuleRow.id == int(rule_id)).delete()
        db.commit()
        reset_rules()
        return {"removed": rule_id}

    phrase = (phrase or "").strip().lower()
    if not phrase:
        raise HTTPException(400, "A phrase is required")
    if kind not in ("phrase", "city_province"):
        raise HTTPException(400, "Kind must be phrase or city_province")
    if kind == "city_province" and not provinces.strip():
        raise HTTPException(
            400, "A city rule needs its province codes — 'Vancouver' alone "
                 "would also match Vancouver, Washington.")

    row = (db.query(CanadaRuleRow).filter(CanadaRuleRow.id == int(rule_id)).first()
           if rule_id else None)
    if row is None:
        row = CanadaRuleRow(phrase=phrase)
        db.add(row)
    if item not in ("vendor", "bank_detail"):
        raise HTTPException(400, "Item must be Vendor or Bank Detail")
    if rule not in ("equals", "contains"):
        raise HTTPException(400, "Rule must be Equals or Contains")
    row.phrase = phrase
    row.item = item
    row.rule = rule
    row.kind = kind
    row.provinces = provinces.strip().upper()
    row.active = active != "false"
    db.add(AuditRow(company="", action="canada_rule_saved",
                    detail=f"{item} {rule} '{phrase}'",
                    dry_run=False, actor=user.username))
    db.commit()
    reset_rules()
    return {"saved": row.id}


@app.post("/rules/rescore")
@app.post("/canada/rescore")
def rescore_canada(company: str = Form(...),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Re-apply the rules to every charge, so a change takes effect now."""
    from .review import refresh_flags
    return {"changed": refresh_flags(db, company)}


@app.post("/bills/edit")
def edit_bills(bill_ids: str = Form(...), vendor: str = Form(""),
               amount_mode: str = Form(""), amount_value: str = Form(""),
               amount_how: str = Form("amount"),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_write)):
    """Edit one or many bills: vendor, amount, or both.

    Three ways to say what the amount becomes:

      set      the figure given
      delta    add the figure (negative to subtract)
      percent  add that percentage (negative to subtract)

    Percentages round to the cent -- a bill is a real invoice, and rounding to
    the dollar would silently invent a discrepancy the reconciler then has to
    explain.

    Each bill is read and written on its own, so a batch is a sequence of
    independent edits. One failure doesn't stop the rest; it's named in the
    result instead.
    """
    from .models_db import BillRow
    from .integrations.qbo_write import edit_bill, WriteBlocked, WriteFailed
    from decimal import Decimal as _D, InvalidOperation, ROUND_HALF_UP

    ids = [b.strip() for b in bill_ids.split(",") if b.strip()]
    if not ids:
        raise HTTPException(400, "No bills selected")
    if amount_mode and amount_mode not in ("set", "delta", "percent", "cad", "vendors_open"):
        raise HTTPException(400, "Amount change must be set, delta, percent, cad or vendors_open")

    value = None
    # ADJUST FOR CAD TO USD: each bill times the CAD rate on its own date (the
    # Bank of Canada's for that day, or the latest before it).
    _fxh, _fxnow = None, None
    if amount_mode == "cad":
        from . import fx as _fxm
        _fxh, _fxnow = _fxm.history(db), cad_rate(db)
    if amount_mode and amount_mode not in ("cad", "vendors_open"):
        try:
            value = _D(str(amount_value).strip())
        except (InvalidOperation, TypeError):
            raise HTTPException(400, "That amount isn't a number")
        if amount_mode == "set" and value <= 0:
            raise HTTPException(400, "A bill's amount must be more than zero")

    rows = db.query(BillRow).filter(BillRow.bill_id.in_(ids)).all()
    # What the bills looked like first, so this click can be undone like any
    # other. Bills are not charges, so they carry their own snapshot shape.
    import json as _json
    _before_bills = _json.dumps([
        {"bill_id": r.bill_id, "vendor": r.vendor or "",
         "amount": str(r.amount or 0), "balance": str(r.balance or 0)}
        for r in rows])
    found = {r.bill_id for r in rows}
    updated, failed = [], [{"bill": i, "error": "no longer in the bill list"}
                           for i in ids if i not in found]

    for row in rows:
        new_amount = None
        if amount_mode == "vendors_open":
            # ADJUST FOR VENDORS OPEN: a negative "Due from Vendors - Open"
            # line for the open balance.
            from .integrations.qbo_write import adjust_for_vendors_open as _afvo
            from .models_db import CompanyRow as _CoV
            _cov = (db.get(_CoV, row.company) or db.query(_CoV).filter(_CoV.name == row.company).first())
            try:
                _r = _afvo(db, (_cov.realm_id if _cov else "") or "", row, user.username)
                updated.append({"bill": row.doc_number or row.bill_id,
                                "changed": [f"Due from Vendors - Open line {_r['line']}"]})
            except WriteBlocked as e:
                failed.append({"bill": row.doc_number or row.bill_id,
                               "error": f"writing to QuickBooks is turned off ({e})"})
            except WriteFailed as e:
                failed.append({"bill": row.doc_number or row.bill_id, "error": str(e)})
            continue
        if amount_mode:
            base = _D(str(row.amount or 0))
            if amount_mode == "cad":
                from . import fx as _fxm2
                _rate = _D(str(_fxm2.rate_on(_fxh, row.txn_date, _fxnow)))
                new_amount = base * _rate
            elif amount_mode == "set":
                new_amount = value
            elif amount_mode == "delta":
                new_amount = base + value
            else:
                new_amount = base + (base * value / _D(100))
            # To the cent, never the dollar.
            new_amount = new_amount.quantize(_D("0.01"), rounding=ROUND_HALF_UP)
        try:
            # AN OTHER FEES LINE, when chosen -- as Force Match offers -- for an
            # increase or a decrease (a negative line). If QuickBooks will not
            # take a negative line, the amount is changed and that is said.
            if amount_how == "fees" and new_amount is not None and new_amount != _D(str(row.amount or 0)):
                from .integrations.qbo_write import correct_bill_amount as _cba
                from .models_db import CompanyRow as _CoB
                # THE COMPANY'S FILE, by its id OR its name. Read as
                # db.get(CompanyRow, name) alone, a company whose name is not
                # its id came back with no file -- "realm  is not connected",
                # on the Other Fees path only.
                _co = (db.get(_CoB, row.company)
                       or db.query(_CoB).filter(_CoB.name == row.company).first())
                _realm = (_co.realm_id if _co else "") or ""
                _diff = new_amount - _D(str(row.amount or 0))
                if vendor:
                    edit_bill(db, row, user.username, vendor_name=vendor, new_amount=None)
                _res = _cba(db, _realm, row, new_amount, user.username, mode="fees") or {}
                updated.append({"bill": row.doc_number or row.bill_id,
                                "changed": [_res.get("note") or f"Other Fees line for {_diff}"]})
                continue
            res = edit_bill(db, row, user.username,
                            vendor_name=vendor or None, new_amount=new_amount)
            if res["status"] == "updated":
                updated.append({"bill": row.doc_number or row.bill_id,
                                "changed": res["changed"]})
        except WriteBlocked as e:
            failed.append({"bill": row.doc_number or row.bill_id,
                           "error": f"writing to QuickBooks is turned off ({e})"})
        except WriteFailed as e:
            failed.append({"bill": row.doc_number or row.bill_id, "error": str(e)})

    if updated:
        what = []
        if vendor:
            what.append("vendor")
        if amount_mode:
            what.append("amount")
        _record_action(db, rows[0].company if rows else "", user.username,
                       "bill_edit",
                       f"Updated {' and '.join(what)} on {len(updated)} bill(s)",
                       _before_bills)
        db.commit()

    return {"updated": len(updated), "failed": failed, "details": updated}


@app.post("/qbo/vendors/create")
def create_vendor_endpoint(name: str = Form(...),
                           db: Session = Depends(get_session),
                           user: UserRow = Depends(require_write),
                           company: str = Form("")):
    """Create a vendor in QuickBooks and cache it for immediate use."""
    from .integrations.qbo_write import create_vendor, WriteBlocked, WriteFailed
    from .models_db import QboRefRow
    try:
        res = create_vendor(db, name, user.username, company=company)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))
    # Cached now rather than waiting for the hourly reference sync -- the point
    # of creating it here is to use it in the next click.
    realm = _current_realm(db, company)
    db.merge(QboRefRow(id=f"vendor:{realm}:{res['id']}", kind="vendor",
                       realm_id=realm, qbo_id=res["id"], name=res["name"],
                       synced_at=datetime.utcnow()))
    db.commit()
    return res


@app.post("/qbo/categories/create")
def create_category_endpoint(name: str = Form(...),
                             account_type: str = Form("Expense"),
                             parent_id: str = Form(""),
                             db: Session = Depends(get_session),
                             user: UserRow = Depends(require_write),
                             company: str = Form("")):
    """Create an expense account -- a category -- in QuickBooks.

    `parent_id` nests it under an existing account. The colon path in `name`
    still works and still creates each missing level, but typing
    "Ticket Credits:MLB:Blue Jays" means knowing the exact spelling of two
    parents and silently creating a near-duplicate when one is wrong. Choosing
    the parent removes that whole failure.
    """
    from .integrations.qbo_write import create_category, WriteBlocked, WriteFailed
    from .models_db import QboRefRow
    if parent_id.strip():
        # A sub-account must share its parent's type; QuickBooks refuses
        # otherwise, with a message that does not say so plainly.
        from .review import _company_realm as _crp
        _rp = _crp(db, company) if company else ""
        _pq = db.query(QboRefRow).filter(
            QboRefRow.kind == "account",
            QboRefRow.qbo_id == parent_id.strip())
        # This company's file: a parent id alone can match another company's.
        par = (_pq.filter(QboRefRow.realm_id == _rp) if _rp else _pq).first()
        if par is None:
            raise HTTPException(400, "that parent account is not mirrored here")
        if par.account_type and par.account_type != account_type:
            raise HTTPException(
                400, f"a sub-account of '{par.name}' must be type "
                     f"'{par.account_type}', not '{account_type}'")
    try:
        res = create_category(db, name, user.username, company=company,
                              account_type=account_type,
                              parent_id=parent_id.strip())
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))
    realm = _current_realm(db, company)
    db.merge(QboRefRow(id=f"account:{realm}:{res['id']}", kind="account",
                       realm_id=realm, qbo_id=res["id"], name=res["name"],
                       account_type=account_type, usable_for="category",
                       synced_at=datetime.utcnow()))
    db.commit()
    from .review import reset_account_cache
    reset_account_cache()      # suggestions may now resolve to it
    return res


@app.post("/qbo/accounts/type")
def retype_qbo_account(qbo_id: str = Form(...), account_type: str = Form(...),
                       # Sent with the type, because QuickBooks validates the
                       # PAIR: a subtype that does not belong to the type is
                       # refused, and so is a merge between two accounts whose
                       # subtypes differ. Optional, so an older caller that
                       # sends only a type still works.
                       subtype: str = Form(""),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin),
                       company: str = Form("")):
    """Switch an account between Bank and Credit Card, in QuickBooks and here."""
    from .integrations.qbo_write import retype_account, WriteBlocked, WriteFailed
    try:
        return retype_account(db, qbo_id.strip(), account_type, user.username,
                          subtype=subtype.strip(),
                              company=company)
    except (WriteFailed, WriteBlocked) as e:
        db.rollback()
        raise HTTPException(400, str(e))


@app.post("/qbo/accounts/rename")
def rename_qbo_account(qbo_id: str = Form(...), name: str = Form(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin),
                       company: str = Form("")):
    """Rename a bank or credit-card account, in QuickBooks and here."""
    from .integrations.qbo_write import rename_account, WriteBlocked, WriteFailed
    try:
        return rename_account(db, qbo_id.strip(), name, user.username,
                             company=company)
    except (WriteFailed, WriteBlocked) as e:
        db.rollback()
        raise HTTPException(400, str(e))


@app.post("/qbo/accounts/create")
def create_bank_account_endpoint(name: str = Form(...),
                                 account_type: str = Form("Credit Card"),
                                 db: Session = Depends(get_session),
                                 user: UserRow = Depends(require_admin),
                                 company: str = Form("")):
    """Create the bank or credit-card account a card program settles to."""
    from .integrations.qbo_write import (create_bank_account, WriteBlocked,
                                         WriteFailed)
    from .models_db import QboRefRow
    try:
        res = create_bank_account(db, name, user.username, company=company,
                                  account_type=account_type)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))
    realm = _current_realm(db, company)
    db.merge(QboRefRow(id=f"account:{realm}:{res['id']}", kind="account",
                       realm_id=realm, qbo_id=res["id"], name=res["name"],
                       account_type=res.get("type") or account_type,
                       usable_for="bank", synced_at=datetime.utcnow()))
    db.commit()
    return res


def _current_realm(db: Session, company: str = "") -> str:
    """The realm to READ from, for a given company.

    The write side has its own resolver in qbo_write._realm_for, which raises
    rather than guessing. This one is for reads and returns "" when it cannot
    tell -- a read with no realm shows nothing, which is recoverable; a write
    with the wrong realm puts one company's transaction in another company's
    ledger, which is not.
    """
    from .models_db import CompanyRow, QboTokenRow
    if company:
        co = db.query(CompanyRow).filter(CompanyRow.name == company).first()
        if co is not None and co.realm_id:
            return co.realm_id
    rows = db.query(QboTokenRow).all()
    # A NAMED COMPANY WITH NO MAPPING GETS NOTHING.
    #
    # This used to fall back to the single connected file whenever there was
    # only one -- which is right for an install that has never had a second
    # company, and wrong the moment somebody adds one: the new company was
    # handed the first company's chart of accounts, vendor list and card
    # accounts before it had been mapped to anything.
    #
    # The single-file shortcut now applies only when NO company was named at
    # all, which is the deployment-wide case it was written for.
    if company:
        return ""
    return rows[0].realm_id if len(rows) == 1 else ""


def _drop_split(db: Session, row) -> None:
    """Remove a transaction's split lines when its category is set or cleared.

    A split IS the category -- several of them. Choosing a single category, or
    clearing it, is a statement that the transaction is no longer split, so the
    lines have to go with it. Leaving them meant the Category column kept
    reading "Split" after somebody had plainly said otherwise, and the split
    would still have been posted.
    """
    from .models_db import ChargeSplitRow
    n = (db.query(ChargeSplitRow)
         .filter(ChargeSplitRow.charge_id == row.charge_id).delete())
    if n:
        db.add(AuditRow(company=row.company, action="charge_split_cleared",
                        charge_id=row.charge_id,
                        detail=f"category changed; {n} split line(s) removed",
                        dry_run=False, actor="system"))


@app.post("/queue/edit")
def edit_charge(charge_id: str = Form(...), profile: str = Form(None),
                merchant: str = Form(None), memo: str = Form(None),
               custom_note: str | None = Form(None),
                category: str = Form(None), vendor: str = Form(None),
                # An empty form field arrives as None, which is the same thing
                # FastAPI gives for "not sent at all" -- so clearing a field
                # could not be told apart from leaving it alone. The client
                # names what it means to blank.
                clear: str = Form(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    """Correct a charge's details, and carry the correction into QuickBooks.

    Which fields are editable depends on what the charge is:

      * bank detail, profile and the QBO memo -- always. The memo can be set on
        its own, and is ALSO rebuilt from the other two whenever either changes
        ("Bank Detail - Profile - 1234"). Editing what a memo describes should
        move the memo; anything else leaves a stale description in the ledger.
      * category and vendor -- only for a coded expense or refund. A bill
        payment's category is the bill it paid, and rewriting that would mean
        unpicking the payment rather than editing it.

    Anything already in QuickBooks is updated there too. An edit that only
    changed our copy would make this app a second set of books.
    """
    from .models_db import ChargeRow
    row = db.get(ChargeRow, charge_id)
    if row is None:
        raise HTTPException(404, "charge not found")

    # Compare against what the COLUMN shows, not just the coded field: on For
    # Review the cell displays the engine's suggestion, so clearing it looked
    # like "no change" and did nothing.
    _before_edit = _snapshot([row])
    before = {"profile": row.cardholder_name or "", "memo": row.memo or "",
              "merchant": row.merchant or "",
              "category": row.coded_category or row.suggested_category or "",
              "vendor": row.coded_vendor or row.suggested_vendor or ""}
    changed, needs_push = [], False

    if custom_note is not None and custom_note != (row.custom_note or ""):
        # WRITTEN TO QUICKBOOKS TOO, in brackets after the memo (before
        # {Engine}): a posted transaction's memo is updated with it.
        changed.append(f"custom note changed from '{row.custom_note or '(blank)'}' to '{custom_note or '(blank)'}'")
        row.custom_note = custom_note[:2000]
        needs_push = True
    if memo is not None and memo != before["memo"]:
        row.memo = memo
        changed.append(f"memo changed from '{before['memo'] or '(blank)'}' "
                       f"to '{memo or '(blank)'}'")
        needs_push = True

    if profile is not None and profile != before["profile"]:
        row.cardholder_name = profile
        changed.append(f"profile changed from '{before['profile'] or '(blank)'}' "
                       f"to '{profile or '(blank)'}'")
        # THE SAME CARD'S BLANK PROFILES FOLLOW: filling one in fills every
        # other For Review charge on that card (same card account, same last
        # four) whose profile is still blank.
        if profile.strip() and (row.card_last4 or "").strip():
            from sqlalchemy import or_ as _or_pf
            _same = db.query(ChargeRow).filter(
                ChargeRow.company == row.company, ChargeRow.source == row.source,
                ChargeRow.card_last4 == row.card_last4, ChargeRow.status == "for_review",
                ChargeRow.charge_id != row.charge_id,
                _or_pf(ChargeRow.cardholder_name.is_(None), ChargeRow.cardholder_name == "")).all()
            for _o in _same:
                _o.cardholder_name = profile.strip()
            if _same:
                changed.append(f"{len(_same)} other blank profile(s) on the same card filled")
    if merchant is not None and merchant != before["merchant"]:
        row.merchant = merchant
        changed.append(f"bank detail changed from "
                       f"'{before['merchant'] or '(blank)'}' "
                       f"to '{merchant or '(blank)'}'")

    # The memo follows from them. Rebuilding it here rather than asking for it
    # keeps the ledger's description and the row it came from in step, and it
    # is the memo -- not the profile -- that QuickBooks actually shows.
    if profile is not None or merchant is not None:
        from .ingestion import compose_memo, memo_tail
        # Bank detail and profile own their own segments and nothing else.
        # Anything typed after the card number is a note somebody added on
        # purpose, so it survives a correction to either of them.
        recognized, tail = memo_tail(before["memo"], row.card_last4 or "")
        new_memo = (compose_memo(row.merchant, row.cardholder_name,
                                 row.card_last4 or "", tail)
                    if recognized else (row.memo or ""))
        if new_memo != (row.memo or ""):
            was = row.memo or ""
            row.memo = new_memo
            changed.append(f"memo changed from '{was or '(blank)'}' "
                           f"to '{new_memo or '(blank)'}'")
            needs_push = True

    # --- a bill payment recategorized becomes an expense ------------------
    #
    # There is no way to "edit" a BillPayment into a Purchase: they're
    # different objects. So the payment is deleted (which puts the bill back on
    # the books) and an expense is created in its place. Both sides move
    # together or neither does.
    if row.resolution == "matched" and category is not None \
            and category.strip() and category.strip().lower() != "bill payment":
        if vendor is not None and vendor.strip() != (row.matched_bill_vendor or "").strip():
            raise HTTPException(
                400, "Change the category first — a bill payment's vendor "
                     "comes from the bill it paid.")
        result = _convert_payment_to_expense(db, row, category.strip(),
                                             user.username)
        return {"charge_id": charge_id, "changed": result["changed"],
                "qbo": result["qbo"]}

    _clear = {c.strip() for c in (clear or "").split(",") if c.strip()}
    if _clear:
        # Applied straight to the row, not routed through the "did the value
        # change" comparison below. The value on screen can come from a live
        # computation that was never written to the row, so `before` reads as
        # blank and the comparison decides nothing changed -- which is how a
        # visible vendor refused to clear.
        if row.qbo_txn_id:
            raise HTTPException(
                400, "This charge is in QuickBooks, so it needs a vendor and "
                     "a category.")
        if "category" in _clear:
            row.coded_category = row.coded_category_id = ""
            row.suggested_category = ""
            row.category_cleared = True
            category = None
            _drop_split(db, row)
        if "vendor" in _clear:
            row.coded_vendor = row.coded_vendor_id = ""
            row.suggested_vendor = ""
            row.vendor_cleared = True
            vendor = None
        changed.append(f"{' and '.join(sorted(_clear))} cleared")
        needs_push = False

    if category is not None or vendor is not None:
        if row.resolution == "matched":
            # Vendor, or a no-op "Bill Payment" category: neither is editable
            # on a payment.
            raise HTTPException(
                400, "A bill payment's vendor comes from the bill it paid. "
                     "Change the category to recategorize it as an expense.")
        from .models_db import QboRefRow
        if category is not None and category != before["category"]:
            # Blank is allowed while a charge is still unresolved -- clearing a
            # wrong suggestion is a normal step, and Add already refuses to
            # commit without one. Only a POSTED charge needs a value, because
            # QuickBooks is holding it.
            if not category.strip():
                if row.qbo_txn_id:
                    raise HTTPException(
                        400, "This charge is in QuickBooks, so it needs a category.")
                row.coded_category = row.coded_category_id = ""
                row.suggested_category = ""
                row.suggestion_cleared = True
                changed.append(f"category '{before['category']}' cleared")
                category = None          # nothing further to resolve
                _drop_split(db, row)
            # One direction only. Going the other way means picking WHICH bill
            # this pays -- a decision with candidates, amounts and a rounding
            # check behind it, not a category you can type. Undo it back to For
            # Review and match it there.
            elif category.strip().lower() in ("bill payment", "billpayment"):
                raise HTTPException(
                    400, "To pay a bill with this charge, undo it back to For "
                         "Review and match it to the bill.")
            else:
                # Re-resolve to a QuickBooks id. The old id belongs to the old
                # category, and clearing it is not enough: QuickBooks accepts an
                # AccountRef by name when CREATING, but rejects an update with
                # "Required parameter ... AccountRef is missing" without a value.
                # Choosing ONE category ends a split, whatever it was split
                # into before.
                _drop_split(db, row)
                acct = _ref_by_name(db, row.company, "account", category)
                if acct is None and row.qbo_txn_id:
                    raise HTTPException(
                        400, f"'{category}' isn't in the QuickBooks chart of "
                             f"accounts. Pick an existing category, or refresh "
                             f"accounts from QuickBooks first.")
                row.coded_category = category
                row.coded_category_id = acct.qbo_id if acct else ""
                row.category_cleared = False
                changed.append(
                    f"category changed from "
                    f"'{before['category'] or '(blank)'}' "
                    f"to '{category or '(blank)'}'")
                needs_push = True
        if vendor is not None and vendor != before["vendor"]:
            if not vendor.strip():
                # Blank is fine while the charge is unresolved -- clearing a
                # wrong suggestion is a normal step, and Add refuses to commit
                # without one anyway. A POSTED charge needs a value, because
                # QuickBooks is holding it.
                if row.qbo_txn_id:
                    raise HTTPException(
                        400, "This charge is in QuickBooks, so it needs a vendor.")
                row.coded_vendor = row.coded_vendor_id = row.suggested_vendor = ""
                row.suggestion_cleared = True
                changed.append(f"vendor '{before['vendor']}' cleared")
            else:
                ven = _ref_by_name(db, row.company, "vendor", vendor)
                if ven is None and row.qbo_txn_id:
                    raise HTTPException(
                        400, f"'{vendor}' isn't a vendor in QuickBooks. Pick an "
                             f"existing vendor, or refresh from QuickBooks first.")
                row.coded_vendor = vendor
                row.coded_vendor_id = ven.qbo_id if ven else ""
                row.vendor_cleared = False
                changed.append(
                    f"vendor changed from '{before['vendor'] or '(blank)'}' "
                    f"to '{vendor or '(blank)'}'")
                needs_push = True

    if not changed:
        return {"charge_id": charge_id, "changed": [], "qbo": "unchanged"}

    db.add(AuditRow(company=row.company, action="charge_edited",
                    charge_id=charge_id, detail="; ".join(changed),
                    dry_run=False, actor=user.username))
    # UNDOABLE ONLY BEFORE IT IS IN QUICKBOOKS: undo restores the row here,
    # and a posted transaction's QuickBooks copy would not follow.
    if not row.qbo_txn_id:
        _record_action(db, row.company, user.username, "edit",
                       changed[0] if len(changed) == 1
                       else f"Edited {len(changed)} fields", _before_edit)
    db.commit()

    qbo = "not in QuickBooks"
    if needs_push and row.qbo_txn_id:
        from .integrations.qbo_write import (update_posted_charge, WriteBlocked,
                                             WriteFailed)
        try:
            qbo = (update_posted_charge(db, row, user.username) or {}).get("status", "")
        except WriteBlocked as e:
            qbo = f"not pushed ({e})"
        except WriteFailed as e:
            # The local edit stands; the ledger didn't take it. Say so rather
            # than rolling back an edit the person can see on screen.
            db.add(AuditRow(company=row.company, action="qbo_update_failed",
                            charge_id=charge_id, detail=str(e)[:400],
                            dry_run=False, actor=user.username))
            db.commit()
            raise HTTPException(
                400, f"Saved here, but QuickBooks refused the update: {e}")
    return {"charge_id": charge_id, "changed": changed, "qbo": qbo}


def _selection_totals(db, ids: list) -> dict:
    """What a selection adds up to -- charges, refunds and payments apart,
    each with its count and total -- across EVERY selected row, not only the
    page on screen."""
    from decimal import Decimal as _Dst
    from .models_db import ChargeRow as _CRst
    out = {"charge": [0, _Dst("0")], "refund": [0, _Dst("0")], "payment": [0, _Dst("0")]}
    for k in range(0, len(ids), 500):
        for amt, credit, pay in db.query(_CRst.amount, _CRst.is_credit, _CRst.is_card_payment) \
                .filter(_CRst.charge_id.in_(ids[k:k + 500])).all():
            key = "charge" if not credit else ("payment" if pay else "refund")
            out[key][0] += 1
            out[key][1] += abs(amt or 0)
    return {k: {"n": v[0], "amount": str(v[1])} for k, v in out.items()}


@app.post("/queue/select-all")
def select_all_ids(company: str = Form(...), status: str = Form("for_review"),
                   # Used below and sent by the grid's filters, but never
                   # declared -- so "select all N matching" failed outright.
                   qbo_id: str = Form(""), order_no: str = Form(""),
                   ingest: str = Form(""),
                   card: str = Form(""), source: str = Form(""),
                   vendor: str = Form(""), profile: str = Form(""),
                   last4: str = Form(""), memo: str = Form(""),
                   cvendor: str = Form(""), details: str = Form(""),
                   category: str = Form(""), user_f: str = Form("", alias="user"),
                   currency: str = Form(""),
                   ts_from: str = Form(""), ts_to: str = Form(""),
                   txn_type: str = Form(""), kinds: str = Form(""),
                   tier: str = Form(""), matchable: str = Form(""),
                   record: str = Form(""),
                   q: str = Form(""),
                   date_from: str = Form(""), date_to: str = Form(""),
                   amt_min: str = Form(""), amt_max: str = Form(""),
                   companies: str = Form(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Every charge id matching the current filters, not just the page shown.

    The queue renders a capped slice, so selecting the visible rows would miss
    most of them -- this returns the full set so a bulk action means what the
    person expects.

    Uses the same apply_charge_filters as the grid and the counts: "all" has to
    mean all of what's on screen, and three copies of the filter logic is how
    they drift apart.
    """
    if company == ALL_COMPANIES:
        # Every matching row in every company reachable, each company asked with
        # its own cards.
        _kw = dict(locals()); _cards = _split_cards(_kw.get("card"))
        _ids = []
        for _co in _reachable(_kw["db"], _kw["user"], _kw.get("companies") or ""):
            _k2 = {**_kw, "company": _co}
            if _cards is not None:
                if _co not in _cards:
                    continue
                _k2["card"] = _cards[_co]
            _ids += select_all_ids(**_k2)["ids"]
        return {"ids": _ids, "count": len(_ids), "totals": _selection_totals(_kw["db"], _ids)}
    from .models_db import ChargeRow
    from .review import apply_charge_filters
    from datetime import date as _dt
    from decimal import Decimal as _D, InvalidOperation as _IO

    def _d(v):
        try:
            return _dt.fromisoformat(v) if v else None
        except ValueError:
            return None

    def _m(v):
        try:
            return _D(v) if v else None
        except (_IO, TypeError):
            return None

    qy = apply_charge_filters(
        db.query(ChargeRow.charge_id).filter(ChargeRow.company == company),
        dict(status=status, card=card or None, source=source or None,
             vendor=vendor or None, profile=profile or None, last4=last4 or None,
             memo=memo or None, cvendor=cvendor or None, details=details or None,
             txn_type=txn_type or None, kinds=kinds or None, tier=tier or None,
             category=category or None, user=user_f or None,
             currency=currency or None,
             ts_from=_dt_from(ts_from), ts_to=_dt_to(ts_to),
             matchable=matchable or None, record=record or None,
             qbo_id=qbo_id or None,
             order_no=order_no or None, ingest=ingest or None,
             q=q or None, date_from=_d(date_from), date_to=_d(date_to),
             amt_min=_m(amt_min), amt_max=_m(amt_max)))
    ids = [r[0] for r in qy.all()]
    return {"ids": ids, "count": len(ids), "totals": _selection_totals(db, ids)}


def transfer_matches(db, company: str) -> dict:
    """For Review rows that are the OTHER SIDE of a transfer already in
    QuickBooks, as QuickBooks' own bank feed would match them.

    A card payment or a wire into a card platform is posted from the card or
    platform side, as a Transfer or Credit Card Payment out of the primary
    checking account. The checking account's own feed then shows the same
    money leaving -- recorded again, it would be counted twice. Paired here:
    a withdrawal on the primary checking account's feed, still in For Review,
    with a posted payment on another account of this company, for the same
    amount, within 5 days. Only a pair with exactly one candidate on each
    side is offered. Nothing is matched without a person pressing Match.
    """
    from .models_db import SourceAccountRow as _SAt, ChargeRow as _CRt
    prim = (db.query(_SAt).filter(_SAt.company == company,
                                  _SAt.is_primary.is_(True)).first())
    if prim is None or not prim.source:
        return {}
    S = prim.source
    names = {r.source: (r.nickname or r.qbo_account_name or r.source)
             for r in db.query(_SAt).filter(_SAt.company == company).all()}
    linked = {r.rule_detail for r in db.query(_CRt.rule_detail).filter(
        _CRt.company == company, _CRt.rule_source == "transfer_match").all()}
    posted = [r for r in db.query(_CRt).filter(
        _CRt.company == company, _CRt.source != S, _CRt.is_card_payment.is_(True),
        _CRt.qbo_txn_id != "", _CRt.qbo_txn_type.in_(("Transfer", "CreditCardPayment"))).all()
        if r.charge_id not in linked]
    waiting = db.query(_CRt).filter(
        _CRt.company == company, _CRt.source == S, _CRt.status == "for_review",
        _CRt.is_credit.is_(False), _CRt.qbo_txn_id == "").all()

    def close(a, b):
        return (abs(abs(a.amount) - abs(b.amount)) < 0.005
                and a.txn_date and b.txn_date
                # FIVE DAYS (as asked): one exact amount, one candidate on each
                # side, within five days of each other.
                and abs((a.txn_date - b.txn_date).days) <= 5)
    cand = {w.charge_id: [p for p in posted if close(w, p)] for w in waiting}
    back = {}
    for wid, ps in cand.items():
        for p in ps:
            back.setdefault(p.charge_id, []).append(wid)
    out = {}
    for w in waiting:
        ps = cand[w.charge_id]
        if len(ps) == 1 and len(back.get(ps[0].charge_id, [])) == 1:
            p = ps[0]
            out[w.charge_id] = {"other_id": p.charge_id,
                                "other_account": names.get(p.source, p.source),
                                "other_date": str(p.txn_date or ""),
                                "qbo_txn_id": p.qbo_txn_id,
                                "qbo_txn_type": p.qbo_txn_type}

    # THE REVERSE: the checking side was recorded first, and the payment is
    # still waiting on the CARD's feed. The card's own feed says which card was
    # paid; the checking withdrawal must have been recorded TO THAT CARD'S
    # ACCOUNT, which confirms it -- one coded to another card or to an expense
    # is never offered.
    acct_of = {r.source: {(r.qbo_account_name or "").strip().lower(),
                          (r.nickname or "").strip().lower()} - {""}
               for r in db.query(_SAt).filter(_SAt.company == company).all()}
    bank_posted = [r for r in db.query(_CRt).filter(
        _CRt.company == company, _CRt.source == S, _CRt.is_credit.is_(False),
        _CRt.qbo_txn_id != "", _CRt.coded_category != "").all()
        if r.charge_id not in linked]
    card_waiting = db.query(_CRt).filter(
        _CRt.company == company, _CRt.source != S, _CRt.status == "for_review",
        _CRt.is_card_payment.is_(True), _CRt.qbo_txn_id == "").all()

    def to_card(w, p):
        cat = (w.coded_category or "").strip().lower()
        return any(cat == n or cat.endswith(":" + n) for n in acct_of.get(p.source, set()))
    cand2 = {p.charge_id: [w for w in bank_posted if close(p, w) and to_card(w, p)]
             for p in card_waiting}
    back2 = {}
    for pid, ws in cand2.items():
        for w in ws:
            back2.setdefault(w.charge_id, []).append(pid)
    for p in card_waiting:
        ws = cand2[p.charge_id]
        if len(ws) == 1 and len(back2.get(ws[0].charge_id, [])) == 1:
            w = ws[0]
            out[p.charge_id] = {"other_id": w.charge_id,
                                "other_account": names.get(S, S),
                                "other_date": str(w.txn_date or ""),
                                "qbo_txn_id": w.qbo_txn_id,
                                "qbo_txn_type": w.qbo_txn_type}
    # BOTH SIDES STILL IN FOR REVIEW -- as QuickBooks' own bank feed pairs
    # them: a card payment waiting on its card account, and the checking
    # withdrawal that paid it. Match posts the payment ONCE (from the card
    # side, the way a card payment is always posted) and clears both rows.
    taken = set(out) | {m["other_id"] for m in out.values()}
    w_free = [w for w in waiting if w.charge_id not in taken]
    c_free = [p for p in card_waiting if p.charge_id not in taken and not p.is_pending]
    cand3 = {w.charge_id: [p for p in c_free if close(w, p)] for w in w_free}
    back3 = {}
    for wid, ps in cand3.items():
        for p in ps:
            back3.setdefault(p.charge_id, []).append(wid)
    for w in w_free:
        ps = cand3[w.charge_id]
        if len(ps) == 1 and len(back3.get(ps[0].charge_id, [])) == 1:
            p = ps[0]
            out[w.charge_id] = {"other_id": p.charge_id, "pair": "both", "bank_id": w.charge_id,
                                "card_id": p.charge_id,
                                "other_account": names.get(p.source, p.source),
                                "other_date": str(p.txn_date or ""),
                                "qbo_txn_id": "", "qbo_txn_type": ""}
            out[p.charge_id] = {"other_id": w.charge_id, "pair": "both", "bank_id": w.charge_id,
                                "card_id": p.charge_id,
                                "other_account": names.get(S, S),
                                "other_date": str(w.txn_date or ""),
                                "qbo_txn_id": "", "qbo_txn_type": ""}
    return out



def transfer_why(db, company: str, matched: dict) -> dict:
    """For a checking withdrawal with no match offered, but SOMETHING close on
    another account (same amount, within 5 days): why it isn't offered. Only
    near-misses get a reason -- an ordinary withdrawal gets nothing."""
    from .models_db import SourceAccountRow as _SAw, ChargeRow as _CRw
    prim = (db.query(_SAw).filter(_SAw.company == company,
                                  _SAw.is_primary.is_(True)).first())
    if prim is None or not prim.source:
        return {}
    S = prim.source
    names = {r.source: (r.nickname or r.qbo_account_name or r.source)
             for r in db.query(_SAw).filter(_SAw.company == company).all()}
    waiting = [w for w in db.query(_CRw).filter(
        _CRw.company == company, _CRw.source == S, _CRw.status == "for_review",
        _CRw.is_credit.is_(False), _CRw.qbo_txn_id == "").all() if w.charge_id not in matched]
    if not waiting:
        return {}
    others = db.query(_CRw).filter(_CRw.company == company, _CRw.source != S,
                                   _CRw.status != "excluded").all()
    out = {}
    for w in waiting:
        near = [o for o in others if o.txn_date and w.txn_date
                and abs(abs(o.amount) - abs(w.amount)) < 0.005
                and abs((o.txn_date - w.txn_date).days) <= 5]
        if not near:
            continue
        amt = f"${abs(w.amount):,.2f}"
        if len(near) > 1:
            out[w.charge_id] = (f"{len(near)} transactions of {amt} within 5 days on other accounts, "
                                "so none is matched automatically. Force Match can pair one by hand.")
            continue
        o = near[0]
        from .usdates import us_date as _usd
        where = f"{names.get(o.source, o.source)} on {_usd(o.txn_date)}"
        if not o.is_card_payment:
            out[w.charge_id] = (f"{amt} on {where} isn't marked as a card payment, so it isn't paired. "
                                "Check its CC Payments / Wires pattern.")
        elif o.qbo_txn_id and o.qbo_txn_type not in ("Transfer", "CreditCardPayment"):
            out[w.charge_id] = (f"The card payment of {amt} on {where} was posted to QuickBooks as "
                                f"{_txn_word(o.qbo_txn_type) if o.qbo_txn_type else 'something else'}, "
                                "not a transfer, so it can't be paired.")
        elif o.status == "for_review" and o.is_pending:
            out[w.charge_id] = f"The card payment of {amt} on {where} is still pending."
        else:
            out[w.charge_id] = f"A card payment of {amt} on {where} is already matched elsewhere."
    return out


@app.get("/match/bands")
def get_bands(db: Session = Depends(get_session), user: UserRow = Depends(require_reviewer)):
    """The match strength bands, highest first, each with the text color that
    reads on it, and the auto-categorize choices they make."""
    from . import bands as _b
    return {"bands": [{**x, "text": _b.text_color(x["color"])} for x in _b.get(db)],
            "auto_levels": [{"key": k, "label": lab} for k, lab in _b.auto_levels()]}


@app.post("/match/bands")
def save_bands(rows: str = Form(...), db: Session = Depends(get_session),
               user: UserRow = Depends(require_admin)):
    """Replace the bands: names, starting scores and colors. Charges are
    regraded at their next rescoring."""
    import json as _jb
    from . import bands as _b
    try:
        saved = _b.save(db, _jb.loads(rows))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(AuditRow(company="", action="bands_saved",
                    detail=", ".join(f"{x['name']} from {x['start'] * 100:g}%" for x in saved),
                    dry_run=False, actor=user.username))
    db.commit()
    return get_bands(db=db, user=user)


@app.get("/tags/cc-vendors")
def get_cc_vendors(db: Session = Depends(get_session), user: UserRow = Depends(require_reviewer)):
    from .review import cc_vendors
    return {"rows": cc_vendors(db)}


@app.post("/tags/cc-vendors")
def save_cc_vendors(rows: str = Form(...), db: Session = Depends(get_session),
                    user: UserRow = Depends(require_admin)):
    """Replace the CC Vendors list: how a card account's name maps to the
    vendor its payments use."""
    import json as _jv
    from .models_db import AppSettingRow
    from .review import CC_VENDORS_KEY
    try:
        lst = _jv.loads(rows)
    except ValueError:
        raise HTTPException(400, "rows must be a JSON list")
    clean, seen = [], set()
    for x in lst if isinstance(lst, list) else []:
        m, v = str((x or {}).get("match") or "").strip(), str((x or {}).get("vendor") or "").strip()
        if m and v and m.lower() not in seen:
            seen.add(m.lower())
            clean.append({"match": m, "vendor": v})
    db.merge(AppSettingRow(key=CC_VENDORS_KEY, value=_jv.dumps(clean)))
    db.add(AuditRow(company="", action="cc_vendors_saved",
                    detail=", ".join(f"{x['match']} -> {x['vendor']}" for x in clean)[:500],
                    dry_run=False, actor=user.username))
    db.commit()
    return {"rows": clean}


@app.post("/queue/transfer-match")
def transfer_match(charge_id: str = Form(...), company: str = Form(...),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Match one row to the transfer already in QuickBooks. Nothing is written
    to QuickBooks: the row is linked to that entry and leaves For Review."""
    from .models_db import ChargeRow
    m = transfer_matches(db, company).get(charge_id)
    if not m:
        raise HTTPException(409, "That transfer is no longer a clear match -- "
                                 "it may have been matched or changed.")
    if m.get("pair") == "both":
        # BOTH STILL IN FOR REVIEW: the card side is written to QuickBooks once,
        # directly, and the checking side linked to that entry -- see
        # app/transfers.py for why this no longer goes through the automatic
        # poster and waits.
        from . import transfers as _tr
        from .integrations.qbo_write import WriteBlocked as _WB
        try:
            r = _tr.post_pair(db, company, m["card_id"], m["bank_id"], user.username,
                              other_account=m.get("other_account", ""), other_date=m.get("other_date", ""))
        except _WB as e:
            raise HTTPException(409, f"QuickBooks writes are off: {e}")
        except _tr.PairError as e:
            raise HTTPException(409, f"The payment couldn't be posted: {e}")
        return {"ok": True, **m, "qbo_txn_id": r["qbo_txn_id"], "qbo_txn_type": r["qbo_txn_type"]}
    row = db.get(ChargeRow, charge_id)
    row.status, row.resolution = "categorized", "transfer"
    row.qbo_txn_id, row.qbo_txn_type = m["qbo_txn_id"], m["qbo_txn_type"]
    row.rule_source, row.rule_detail = "transfer_match", m["other_id"]
    row.resolved_by, row.resolved_at = user.username, datetime.utcnow()
    row.post_error = ""
    db.add(AuditRow(company=company, action="transfer_matched", charge_id=charge_id,
                    detail=f"matched to {_txn_word(m['qbo_txn_type'])} {m['qbo_txn_id']} already "
                           f"recorded from {m['other_account']} ({m['other_date']})",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True, **m}


@app.post("/queue/resolve")
def resolve(ids: str = Form(...), action: str = Form(...),
            bill_id: str = Form(""), category: str = Form(""),
            vendor: str = Form(""), db: Session = Depends(get_session),
            user: UserRow = Depends(require_write)):
    """Resolve one or many charges.

    action: match | code | exclude | undo

    A coding must carry a vendor. QuickBooks will accept a Purchase without an
    EntityRef, so nothing downstream would reject it -- it just lands in the
    ledger with a blank payee, and finding those again afterwards means going
    charge by charge. Cheaper to insist up front.

    Accepts a comma-separated list of ids so the UI can act on a whole
    selection at once. Every change is written to the audit log with the
    previous state, which is what makes undo possible and what an auditor
    would ask for.
    """
    from .models_db import ChargeRow, AuditRow
    id_list = [i for i in ids.split(",") if i.strip()]
    if not id_list:
        raise HTTPException(400, "no charge ids given")
    if action not in ("match", "code", "exclude", "undo"):
        raise HTTPException(400, "action must be match, code, exclude or undo")
    if action == "match" and not bill_id and len(id_list) > 1:
        raise HTTPException(400, "matching several charges needs one bill each")

    from .models_db import BillRow
    from .integrations.qbo_write import (push_charge, undo_post, WriteBlocked,
                                         assert_writes_allowed)
    try:
        assert_writes_allowed()
        live = True
    except WriteBlocked:
        live = False          # gates closed: resolve locally only

    changed = 0
    if action == "code":
        # Both are required by the write path anyway -- post_expense refuses a
        # charge with no category, and QuickBooks takes a Purchase with no
        # payee but leaves it blank in the ledger. Catching it here saves the
        # charge a pointless trip to Categorized and back.
        #
        # EXCEPT for card payments and split transactions, neither of which is
        # an ordinary expense:
        #   a payment posts as a Transfer or Credit Card Payment between two
        #   accounts, so there is no vendor and no category to ask for;
        #   a split carries its categories on its lines, and demanding one on
        #   the charge as well refuses a transaction that is fully specified.
        from .models_db import ChargeSplitRow as _CS
        _rows = db.query(ChargeRow).filter(ChargeRow.charge_id.in_(id_list)).all()
        _split = {r[0] for r in db.query(_CS.charge_id).filter(
            _CS.charge_id.in_(id_list)).distinct().all()}
        _needs = [r for r in _rows
                  if not r.is_card_payment and r.charge_id not in _split]
        if _needs:
            missing = [n for n, v in (("Category", category), ("Vendor", vendor))
                       if not (v or "").strip()]
            if missing:
                raise HTTPException(
                    400, f"{' and '.join(missing)} "
                         f"{'are' if len(missing) > 1 else 'is'} required")

    # What these charges looked like BEFORE the click, captured while it is
    # still true. One snapshot for the whole batch: pressing Add over forty
    # charges is one action, so one Undo puts all forty back.
    _rows_before = [r for r in (db.get(ChargeRow, c) for c in id_list)
                    if r is not None]
    _before = _snapshot(_rows_before)
    # Every charge in a batch belongs to one company; take it from the rows
    # rather than asking the caller for something it already implies.
    _company = _rows_before[0].company if _rows_before else ""

    posted, post_errors, conflicts = [], [], []
    for cid in id_list:
        row = db.get(ChargeRow, cid)
        if row is None:
            continue

        # Someone else may have resolved this since the page was loaded. Silently
        # overwriting would leave the app disagreeing with QuickBooks -- their
        # transaction stays in the ledger while our record says something else.
        # Deliberately not "someone ELSE resolved it". One person with the app
        # open in two tabs hit this constantly: both tabs passed the guard, the
        # second resolve was a no-op because the write path is idempotent, and
        # nothing was reported -- so it looked like the click did nothing.
        # Already resolved is already resolved, whoever did it.
        if (action in ("match", "code", "exclude")
                and row.status != "for_review"
                and row.resolved_by
                and not row.resolved_by.startswith("auto:")):
            conflicts.append({
                "charge_id": cid,
                "resolved_by": row.resolved_by,
                "as": (f"bill {row.matched_bill_id}" if row.resolution == "matched"
                       else row.coded_category or row.status),
                # Lets the UI name the state rather than describing it.
                "status": row.status,
                "posted": bool(row.qbo_txn_id),
            })
            continue

        before = {"status": row.status, "resolution": row.resolution,
                  "bill": row.matched_bill_id, "category": row.coded_category}

        if action == "match":
            if row.is_credit:
                conflicts.append({
                    "charge_id": cid, "resolved_by": "—",
                    "as": "refund — code it as an expense/refund instead",
                    "posted": False})
                continue
            row.status, row.resolution = "categorized", "matched"
            row.matched_bill_id = bill_id
            row.coded_category = row.coded_vendor = ""
            # Snapshot the bill now -- see models_db: the BillRow is pruned once
            # QuickBooks stops returning it as open, which this match causes.
            _b = db.get(BillRow, bill_id) if bill_id else None
            if _b is not None:
                row.matched_bill_no = _b.doc_number or _b.bill_id or ""
                row.matched_bill_vendor = _b.vendor or ""
                row.matched_bill_date = str(_b.txn_date or "")
                row.matched_bill_memo = (_b.memo or "")[:500]
        elif action == "code":
            row.status, row.resolution = "categorized", "coded"
            row.coded_category, row.coded_vendor = category, vendor
            # Resolve to QuickBooks ids now, so a later rename in QBO doesn't
            # orphan the coding.
            from .models_db import QboRefRow
            acct = _ref_by_name(db, row.company, "account", category)
            row.coded_category_id = acct.qbo_id if acct else ""
            ven = _ref_by_name(db, row.company, "vendor", vendor) if vendor else None
            row.coded_vendor_id = ven.qbo_id if ven else ""
            row.matched_bill_id = ""
            # A rule that divides this charge writes its lines now, so Add on a
            # split rule produces the split rather than a single-category
            # coding somebody then has to break apart by hand. Percentages are
            # turned into amounts here, with the rounding remainder put on the
            # LAST line so the lines total the charge exactly.
            try:
                from decimal import Decimal
                from .learning import suggestion_for as _sfor
                from .models_db import ChargeSplitRow as _CSR
                _sug = _sfor(db, row.company, row.merchant, charge=row)
                _lines = (_sug or {}).get("split") or []
                if len(_lines) >= 2 and row.amount is not None:
                    db.query(_CSR).filter(_CSR.charge_id == row.charge_id).delete()
                    _total = Decimal(str(row.amount))
                    _acc = Decimal("0")
                    for _i, _ln in enumerate(_lines):
                        if _i == len(_lines) - 1:
                            _amt = _total - _acc
                        else:
                            _amt = (_total * Decimal(str(_ln["percent"]))
                                    / Decimal("100")).quantize(Decimal("0.01"))
                            _acc += _amt
                        _a = _ref_by_name(db, row.company, "account", _ln["category"])
                        db.add(_CSR(charge_id=row.charge_id, position=_i,
                                    category=_ln["category"],
                                    category_id=_a.qbo_id if _a else "",
                                    amount=_amt))
            except Exception:               # noqa: BLE001
                # A split that cannot be written must not stop the coding: the
                # charge is still categorized, just not divided.
                log.exception("could not apply rule split to %s", row.charge_id)
            # Nothing is learned from this. Coding a charge used to write a
            # rule for the merchant, which meant one unusual decision quietly
            # became the default for every later charge from it. Rules are now
            # only what somebody deliberately entered under Rules & Tags.
        elif action == "exclude":
            # Excluding is a statement that the charge does not belong in the
            # ledger. If it is already there, that has to be undone first --
            # otherwise the app records "excluded" while a live transaction
            # sits in QuickBooks with nothing pointing at it.
            if row.qbo_txn_id:
                raise HTTPException(
                    409, f"This charge is in QuickBooks as {_txn_word(row.qbo_txn_type)} "
                         f"{row.qbo_txn_id}. Undo it first, then exclude it.")
            row.status, row.resolution = "excluded", ""
        elif action == "undo":
            # A MATCHED TRANSFER is the other side of an entry the card side
            # recorded: undoing it UNLINKS it and leaves that entry alone.
            # Deleting it from QuickBooks would take the card side's payment
            # with it.
            if row.resolution == "transfer":
                row.qbo_txn_id = row.qbo_txn_type = ""
                row.rule_source = row.rule_detail = ""
            if row.qbo_txn_id:
                # Remove it from the ledger first. Only if QuickBooks confirms
                # the delete do we clear local state -- otherwise the app would
                # claim the charge is unposted while the transaction lives on.
                res = undo_post(db, row, user.username)
                if res["status"] == "failed":
                    raise HTTPException(
                        409,
                        f"could not remove {_txn_word(row.qbo_txn_type)} {row.qbo_txn_id} "
                        f"from QuickBooks: {res['error']}")
            row.status, row.resolution = "for_review", ""
            row.matched_bill_id = row.coded_category = row.coded_vendor = ""
            row.matched_bill_no = row.matched_bill_vendor = ""
            row.matched_bill_date = row.matched_bill_memo = ""
            row.post_error = ""
            # ITS MATCHED OTHER SIDE, if any, goes back too: the entry they
            # shared has just been removed from QuickBooks.
            for _mt in db.query(ChargeRow).filter(
                    ChargeRow.company == row.company,
                    ChargeRow.rule_source == "transfer_match",
                    ChargeRow.rule_detail == row.charge_id).all():
                _mt.status, _mt.resolution = "for_review", ""
                _mt.qbo_txn_id = _mt.qbo_txn_type = ""
                _mt.rule_source = _mt.rule_detail = ""

        row.resolved_by = "" if action == "undo" else user.username
        row.resolved_at = None if action == "undo" else datetime.utcnow()
        # A SENTENCE, not a Python dict.
        #
        # This wrote the raw snapshot: "from {'status': 'for_review',
        # 'resolution': '', 'bill': '', 'category': ''} by jsmith". Every row on
        # the audit screen was mostly braces and quotes, three of the four keys
        # were usually empty, and the one fact worth reading -- what the charge
        # was before -- was buried in the middle of it.
        db.add(AuditRow(company=row.company, action=f"queue_{action}",
                        charge_id=cid, bill_id=bill_id or None,
                        detail=_was(before) or "", dry_run=not live,
                        actor=user.username))
        changed += 1
    db.commit()

    # Post straight away rather than queueing: a reviewer's decision reaches
    # the ledger immediately, and Undo removes it again.
    if live and action in ("match", "code"):
        for cid in id_list:
            row = db.get(ChargeRow, cid)
            if row is None or row.qbo_txn_id:
                continue
            result = push_charge(db, row, user.username)
            if result["status"] == "posted":
                posted.append(result)
            elif result["status"] == "skipped" and result.get("reason") in ("already being posted", "already posted"):
                # SOMEONE ELSE GOT THERE FIRST, at the same moment: told, not silent.
                conflicts.append({"charge_id": cid, "resolved_by": "", "as": "",
                                  "status": "posting" if result.get("reason") == "already being posted" else row.status,
                                  "posted": True})
            elif result["status"] == "failed":
                # Nothing reached QuickBooks, so the charge has not been
                # resolved -- it goes back to For Review rather than sitting in
                # Categorized pretending to be done. Categorized is meant to
                # mean "this exists in the ledger"; a failed post that stayed
                # there made the tab lie, and left the reviewer to notice the
                # error and manually Undo before they could retry.
                row.post_error = result.get("error", "")[:1000]
                row.status, row.resolution = "for_review", ""
                row.matched_bill_id = ""
                row.coded_category = row.coded_vendor = ""
                row.coded_category_id = row.coded_vendor_id = ""
                row.matched_bill_no = row.matched_bill_vendor = ""
                row.matched_bill_date = row.matched_bill_memo = ""
                row.resolved_by, row.resolved_at = "", None
                db.add(AuditRow(
                    company=row.company, action="post_failed_reverted",
                    charge_id=cid,
                    detail=f"post failed, returned to For Review: "
                           f"{row.post_error[:300]}",
                    dry_run=not live, actor=user.username))
                db.commit()
                post_errors.append(result)

    # Recorded only if something actually changed -- an action that did nothing
    # is not worth a slot in a five-deep stack.
    # SUGGESTIONS FOLLOW AT ONCE: what was just coded counts on the next
    # Bank Feed load, not after the five-minute cache runs out.
    if changed:
        try:
            from . import history as _hcl
            _hcl._CACHE.clear()
        except Exception:                                       # noqa: BLE001
            pass
    # NOT A DELETE: undoing one would mark the rows posted again while their
    # QuickBooks entries stay deleted.
    if changed and action != "undo":
        verb = {"match": "Matched", "code": "Added", "exclude": "Excluded",
                "undo": "Undone"}.get(action, action.title())
        _record_action(db, _company, user.username, action,
                       f"{verb} {changed} transaction(s)", _before)
        db.commit()

    return {"action": action, "changed": changed, "live": live,
            "posted": len(posted), "post_failed": len(post_errors),
            "errors": post_errors[:5],
            "conflicts": len(conflicts), "conflict_detail": conflicts[:5]}


@app.get("/qbo/companies")
def qbo_companies(db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """Connected QuickBooks files, by their own company names."""
    from .models_db import QboTokenRow
    from .integrations.qbo_bills import company_name
    out = []
    for t in db.query(QboTokenRow).all():
        name = t.company_name
        if not name:
            try:
                name = company_name(db, t.realm_id)
                t.company_name = name
                db.commit()
            except Exception:
                name = t.realm_id
        out.append({"realm_id": t.realm_id, "name": name or t.realm_id})
    return out


# --- card registry ----------------------------------------------------------

def _account_type_of(db: Session, qbo_id: str, company: str = "") -> str:
    """One account's type, within this company's QuickBooks file -- like its
    subtype below. Ids repeat across files."""
    from .models_db import QboRefRow
    from .review import _company_realm as _crt
    if not qbo_id:
        return ""
    realm = _crt(db, company) if company else ""
    if company and not realm:
        return ""
    q = db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                   QboRefRow.qbo_id == qbo_id)
    if realm:
        q = q.filter(QboRefRow.realm_id == realm)
    r = q.first()
    return (r.account_type or "") if r else ""


def _is_virtual(name: str, row=None) -> bool:
    """Virtual-card program, by name -- with a stored flag still honored.

    The six programs are known, so asking somebody to tick a box for each is
    asking them to restate something the account name already says. An account
    explicitly flagged in the past still counts, so nothing that was true
    yesterday becomes false today.
    """
    from engine.suggest import is_virtual_program
    if row is not None and getattr(row, "is_virtual", False):
        return True
    return is_virtual_program(name)


def _account_subtype_of(db, qbo_id: str, company: str) -> str:
    """One account's subtype, within this company's QuickBooks file.

    Scoped by realm: QuickBooks ids are only unique within one, so an
    unfiltered lookup lets another company's account answer.
    """
    if not qbo_id:
        return ""
    from .models_db import QboRefRow as _QR3
    row = (db.query(_QR3)
           .filter(_QR3.kind == "account", _QR3.qbo_id == str(qbo_id),
                   _eq_or_none(_QR3.realm_id, _current_realm(db, company)))
           .first())
    return (row.subtype or "") if row else ""


@app.get("/accounts/mapping")
def account_mapping(company: str = Query(""),
                    companies: str = Query(""),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_reviewer)):
    """Card programs and their QBO accounts, plus any per-card overrides.

    Resolution order for a charge: per-card override, else the program default.
    """
    if company == ALL_COMPANIES:
        # EVERY COMPANY'S CARD ACCOUNTS, each keyed "Company|id" -- card and
        # QuickBooks ids repeat from company to company. The screen's actions
        # send those keys back, and the client splits them into the company and
        # the id before they reach the server (see api() in app.js).
        out = {"company": ALL_COMPANIES, "programs": [], "overrides": [],
               "api_fed": [], "api_stale_hours": {}, "api_details": {},
               "unmapped_programs": 0, "synced_at": "", "file_formats": []}
        for _co in _reachable(db, user, companies):
            r = account_mapping(company=_co, companies="", db=db, user=user)
            k = lambda v: f"{_co}|{v}" if v else ""
            for p_ in r.get("programs") or []:
                out["programs"].append({**p_, "company": _co, "source": k(p_.get("source")),
                                        "qbo_account_id": k(p_.get("qbo_account_id")),
                                        "qbo_ref_id": k(p_.get("qbo_ref_id"))})
            for o_ in r.get("overrides") or []:
                out["overrides"].append({**o_, "company": _co,
                                         **({"source": k(o_["source"])} if o_.get("source") else {})})
            out["api_fed"] += [k(x) for x in r.get("api_fed") or []]
            out["api_stale_hours"].update({k(x): v for x, v in (r.get("api_stale_hours") or {}).items()})
            out["api_details"].update({k(x): v for x, v in (r.get("api_details") or {}).items()})
            out["unmapped_programs"] += int(r.get("unmapped_programs") or 0)
            out["synced_at"] = max(out["synced_at"], r.get("synced_at") or "")
            out["file_formats"] = r.get("file_formats") or out["file_formats"]
        return out
    from .models_db import SourceAccountRow, CardRow, ChargeRow
    from sqlalchemy import func
    from .ingestion import SOURCE_SIGNATURES

    counts = dict(db.query(ChargeRow.source, func.count(ChargeRow.charge_id))
                  .filter(ChargeRow.company == company)
                  .group_by(ChargeRow.source).all())
    cards = dict(db.query(ChargeRow.source, func.count(func.distinct(ChargeRow.card_last4)))
                 .filter(ChargeRow.company == company)
                 .group_by(ChargeRow.source).all())

    mapped = {m.source: m for m in db.query(SourceAccountRow)
              .filter(SourceAccountRow.company == company).all()}

    # THE LIST IS QUICKBOOKS' BANK AND CREDIT-CARD ACCOUNTS, not a set of card
    # programs that point at them. An account IS a card account; the mapping in
    # between bought nothing and had to be maintained.
    #
    # `source` survives as the internal key, because every charge already
    # carries one and thirty-odd places read it. A row that has never been used
    # takes the account id as its key, so nothing has to be invented.
    from .models_db import QboRefRow
    from .ingestion import SOURCES as _SOURCES

    # Retired accounts are KEPT on this grid, unlike in the pickers.
    #
    # Hiding one would take an account off the screen that thousands of coded
    # charges still resolve through, and the reconciliation would simply go
    # quiet with nothing to explain it. Shown, marked, and not offered for
    # anything new -- which is what "retired" should look like.
    # THIS COMPANY'S QuickBooks file, not every connected one. The bank
    # accounts are injected into the card-account list as `qbo:<id>` sources,
    # so without the realm filter a company with no cards of its own was shown
    # another company's clearing accounts as if they were its own -- which is
    # what made an unmapped company look populated even with no transactions.
    accounts = db.query(QboRefRow).filter(
        QboRefRow.kind == "account", QboRefRow.usable_for == "bank",
        _eq_or_none(QboRefRow.realm_id, _current_realm(db, company))).all()
    by_qbo = {m.qbo_account_id: m for m in mapped.values() if m.qbo_account_id}

    keys, seen = [], set()
    for a in sorted(accounts, key=lambda x: (x.name or "").lower()):
        m = by_qbo.get(a.qbo_id)
        src = m.source if m else f"qbo:{a.qbo_id}"
        keys.append((src, m, a))
        seen.add(src)
    # Anything with HISTORY but no QuickBooks account behind it stays on the
    # list -- dropping it would hide the account those charges resolve through.
    # The built-in program names are deliberately NOT included: they were the
    # old mapping's placeholders, and listing an account that does not exist in
    # QuickBooks and has never been used is exactly what this change removes.
    for src in sorted(set(list(counts) + list(mapped))):
        if src not in seen:
            keys.append((src, mapped.get(src), None))

    programs = []
    _bal_cache = _balance_cache(db, company)
    for src, m, acct in keys:
        programs.append({
            "source": src,
            # False only when QuickBooks says so. An account with no mirror row
            # behind it is not retired, it is unmapped.
            "qbo_active": (acct.active is not False) if acct is not None else True,
            # Needed to retire it. An account with no mirror row behind it is
            # not in QuickBooks at all, so there is nothing to retire.
            "qbo_ref_id": (acct.qbo_id if acct is not None else ""),
            "nickname": m.nickname if m else "",
            "qbo_account_id": (acct.qbo_id if acct
                               else (m.qbo_account_id if m else "")),
            "qbo_account_name": (acct.name if acct
                                 else (m.qbo_account_name if m else "")),
            # So the settings picker can offer bank accounts only -- a credit
            # card cannot be the account a payment is made FROM.
            "qbo_account_type": (acct.account_type if acct
                                 else _account_type_of(
                                     db, m.qbo_account_id, company) if m else ""),
            # QuickBooks compares the SUBTYPE too when deciding whether a
            # rename may merge two accounts, so the grid has to show it -- and
            # these are the accounts people actually try to merge.
            "qbo_account_subtype": _account_subtype_of(
                # `acct` is a QboRefRow (its id field is qbo_id); `m` is a
                # SourceAccountRow (qbo_account_id). Two row types, two names
                # for the same thing.
                db, (acct.qbo_id if acct
                     else (m.qbo_account_id if m else "")), company),
            "mapped": bool(acct or (m and m.qbo_account_id)),
            # QBO BALANCE and BANK BALANCE, side by side, as QuickBooks' own
            # bank feed shows them. QuickBooks' from the last reference sync;
            # the bank's from the card's Plaid connection, if it has one.
            "qbo_balance": _qbo_balance(db, company, acct, m, _bal_cache),
            **_bank_balance(db, company, src, _bal_cache),
            # Blank means "same as source", which is true of the original three.
            "file_format": (m.file_format if m else "") or src,
            # Derived from the name rather than stored: these programs are
            # known, and a box per account was one more thing to keep true.
            "is_virtual": _is_virtual(
                f"{(m.nickname if m else '')} "
                f"{(acct.name if acct else (m.qbo_account_name if m else ''))} {src}",
                m),
            # Unmapped programs have no row yet, so they are active by default.
            "is_active": bool(m.is_active) if m else True,
            "is_primary": bool(m.is_primary) if m else False,
            "charges": counts.get(src, 0),
            "distinct_cards": cards.get(src, 0),
        })

    overrides = [{
        "id": c.id, "source": c.source, "last4": c.last4,
        "nickname": c.nickname, "qbo_account_name": c.qbo_account_name,
    } for c in db.query(CardRow).filter(
        CardRow.company == company, CardRow.qbo_account_id != "").all()]

    # WHICH ACCOUNTS A PROGRAM API ALREADY FEEDS.
    #
    # From our own connection rows, so the grid can have this without calling
    # Slash, Divvy and Taekus to draw a table. Those calls moved to the API
    # Card Connections tab, and the grid built its badges from them -- so the
    # badge vanished here, and Connect was offered on accounts a program feeds.
    # Connecting Plaid to one of those would point two feeds at one ledger.
    from .review import api_fed_sources as _afs
    _fed, _fed_age = _afs(db, company)

    # LAST STATEMENT, for bank reconciliations: Plaid Liabilities, when on.
    try:
        from . import plaid_statements as _pst
        from .models_db import PlaidItemRow as _PIst
        # CONNECTED CARDS ONLY. A card disconnected before statements were
        # cleared on disconnect kept its last statement on show; its stored
        # statement is dropped here the first time the screen is opened.
        _plaid_src = {r.source for r in db.query(_PIst.source).filter(
            _PIst.company == company, _PIst.active.is_(True)).all()}
        _stale = False
        for _p in programs:
            _s = _pst.get(db, company, _p.get("source", ""))
            if _s and _p.get("source") not in _plaid_src:
                _pst.clear(db, company, _p.get("source", ""))
                _stale, _s = True, {}
            _p["statement_date"] = _s.get("date", "")
            _p["statement_balance"] = _s.get("balance")
            _p["statement_history"] = _s.get("history") or []
            if _s.get("note"):
                _p["statement_note"] = _s["note"]
            elif not _pst.enabled():
                _p["statement_note"] = "Statements come from Plaid Liabilities, which is off"
            elif not _s and _p.get("source") in _plaid_src:
                # Connected, not read yet: said, rather than a blank cell --
                # and read now, in the background (at most every 10 minutes).
                _p["statement_note"] = "Not read yet \u2014 being read now; reopen this screen in a minute"
                _stmt_kick(company)
            else:
                _p["statement_note"] = ""
        if _stale:
            db.commit()
    except Exception:                                   # noqa: BLE001
        log.exception("statements for %s", company)
    return {"api_fed": sorted(_fed), "api_stale_hours": dict(_fed_age),"company": company, "programs": programs, "overrides": overrides,
            "file_formats": sorted(SOURCE_SIGNATURES),
            "unmapped_programs": sum(1 for p in programs if not p["mapped"]),
            # WHEN THE BANK AND CARD ACCOUNTS WERE LAST READ FROM QUICKBOOKS,
            # for the "last synced" line -- as Chart of Accounts and Vendors
            # show theirs. The newest refresh among this company's accounts.
            "synced_at": _accounts_synced_at(_bal_cache),
            # How each API card's connection reads (see _api_details).
            "api_details": _api_details(db, company)}


_STMT_KICKED: dict = {}


def _stmt_kick(company: str) -> None:
    """Read missing card statements for a company now, in the background --
    at most once every 10 minutes."""
    import time as _tk
    if _tk.time() - _STMT_KICKED.get(company, 0) < 600:
        return
    _STMT_KICKED[company] = _tk.time()
    try:
        from . import plaid_statements as _psk
        _psk.refresh_soon(company, delay=1)
    except Exception:                                   # noqa: BLE001
        log.exception("could not start the statement read for %s", company)


def _assert_account_unique(db, company: str, source: str, nickname: str,
                           qbo_account_id: str) -> None:
    """Two card accounts must not share a name or a QuickBooks account.

    A duplicate name makes the upload picker ambiguous -- two identical entries
    and no way to tell which is which. Two programs pointing at one QuickBooks
    account is worse: every charge from either lands in the same place, so the
    mapping stops meaning anything and nothing downstream can tell them apart.
    """
    from .models_db import SourceAccountRow
    others = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source != source).all()

    want = (nickname or "").strip().lower()
    if want:
        clash = next((o for o in others
                      if (o.nickname or o.source or "").strip().lower() == want), None)
        if clash:
            raise HTTPException(
                400, f"Another card account is already called "
                     f"'{clash.nickname or clash.source}'.")

    acct = (qbo_account_id or "").strip()
    if acct:
        clash = next((o for o in others if (o.qbo_account_id or "") == acct), None)
        if clash:
            raise HTTPException(
                400, f"'{clash.nickname or clash.source}' is already mapped to "
                     f"that QuickBooks account.")


@app.post("/accounts/mapping")
def save_account_mapping(source: str = Form(...), qbo_account_id: str = Form(...),
                         qbo_account_name: str = Form(...), nickname: str = Form(""),
                         company: str = Form(""),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_write)):
    """Point a whole card program at one QuickBooks account."""
    from .models_db import SourceAccountRow
    _assert_account_unique(db, company, source, nickname, qbo_account_id)
    # POINTED ELSEWHERE: a nickname that was only the old account's name
    # follows the account to its new name, rather than staying behind.
    _prev = db.get(SourceAccountRow, f"{company}:{source}")
    if (_prev is not None and (_prev.qbo_account_id or "") != qbo_account_id.strip()
            and nickname.strip() in ("", (_prev.qbo_account_name or "").strip())):
        nickname = qbo_account_name
    db.merge(SourceAccountRow(
        id=f"{company}:{source}", company=company, source=source,
        nickname=nickname.strip(), qbo_account_id=qbo_account_id.strip(),
        qbo_account_name=qbo_account_name.strip(), updated_at=datetime.utcnow()))
    db.commit()
    return {"source": source, "account": qbo_account_name,
            "note": "all cards in this program now resolve to this account, "
                    "including ones not yet seen"}


@app.get("/cards")
def list_cards(company: str = Query(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Every card seen in the charge data, with its QBO mapping if set.

    Cards are discovered from the charges themselves -- upload an export and any
    new last-4 shows up here as unmapped, so nothing has to be entered by hand
    before it appears.
    """
    from .models_db import CardRow, ChargeRow
    from sqlalchemy import func

    seen = db.query(
        ChargeRow.source, ChargeRow.card_last4,
        func.count(ChargeRow.charge_id).label("n"),
        func.max(ChargeRow.cardholder_name).label("holder"),
    ).filter(ChargeRow.company == company,
             ChargeRow.card_last4 != "").group_by(
        ChargeRow.source, ChargeRow.card_last4).all()

    known = {c.id: c for c in db.query(CardRow).all()}
    out = []
    for src, last4, n, holder in seen:
        cid = f"{src}:{last4}"
        c = known.get(cid)
        out.append({
            "id": cid, "source": src, "last4": last4, "charges": n,
            "holder_seen": holder or "",
            "nickname": c.nickname if c else "",
            "qbo_account_id": c.qbo_account_id if c else "",
            "qbo_account_name": c.qbo_account_name if c else "",
            "mapped": bool(c and c.qbo_account_id),
            "active": c.active if c else True,
        })
    out.sort(key=lambda x: (x["mapped"], -x["charges"]))
    return {"company": company, "cards": out,
            "unmapped": sum(1 for c in out if not c["mapped"])}


@app.post("/cards")
def save_card(id: str = Form(...), nickname: str = Form(""),
              qbo_account_id: str = Form(""), qbo_account_name: str = Form(""),
              company: str = Form(""), active: bool = Form(True),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_write)):
    """Name a card and map it to a QBO account. Any signed-in user may do this."""
    from .models_db import CardRow
    src, _, last4 = id.partition(":")
    db.merge(CardRow(
        id=id, source=src, last4=last4, nickname=nickname.strip(),
        company=company, qbo_account_id=qbo_account_id.strip(),
        qbo_account_name=qbo_account_name.strip(), active=active,
        updated_at=datetime.utcnow(),
    ))
    db.commit()
    return {"saved": id, "nickname": nickname, "qbo_account": qbo_account_name}


@app.post("/accounts/source/virtual")
def set_source_virtual(company: str = Form(...), source: str = Form(...),
                       virtual: str = Form("true"),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Mark a card program as issuing virtual cards."""
    from .models_db import SourceAccountRow
    row = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == source).first()
    if row is None:
        row = SourceAccountRow(id=f"{company}:{source}", company=company,
                               source=source, nickname=source)
        db.add(row)
    row.is_virtual = virtual != "false"
    db.commit()
    return {"source": source, "is_virtual": row.is_virtual}


@app.post("/accounts/source/active")
def set_source_active(company: str = Form(...), source: str = Form(...),
                      active: str = Form("true"),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Retire a card account, or bring it back.

    Deliberately not a delete. The charges stay, the grid stays, the exports
    stay; only the places that OFFER the account change.
    """
    from .models_db import SourceAccountRow
    row = _row_for_source(db, company, source)
    if row is None:
        row = SourceAccountRow(id=f"{company}:{source}", company=company,
                               source=source, nickname=source)
        db.add(row)
    row.is_active = active != "false"
    db.add(AuditRow(company=company,
                    action="card_account_active" if row.is_active
                           else "card_account_retired",
                    detail=row.nickname or source, dry_run=False,
                    actor=user.username))
    db.commit()
    return {"source": source, "is_active": row.is_active}


def _row_for_source(db: Session, company: str, source: str):
    """The settings row for an account, created if this is its first setting.

    An account listed from QuickBooks has no row until somebody changes
    something about it. Keyed "qbo:<id>" in that case, so the key is derived
    from the account rather than invented.
    """
    from .models_db import SourceAccountRow, QboRefRow
    row = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == source).first()
    if row is not None:
        return row
    if not source.startswith("qbo:"):
        return None
    # THIS COMPANY'S QuickBooks file only. Account ids are small numbers that
    # repeat from one file to the next, and looked up by id alone this took
    # another company's account with the same number -- a checking account
    # was created named "Inventory Asset", and an upload into it was filed
    # under that name.
    from .review import _company_realm as _crr
    realm = _crr(db, company)
    if not realm:
        return None
    acct = db.query(QboRefRow).filter(
        QboRefRow.kind == "account", QboRefRow.realm_id == realm,
        QboRefRow.qbo_id == source.split(":", 1)[1]).first()
    if acct is None:
        return None
    row = SourceAccountRow(id=f"{company}:{source}", company=company,
                           source=source, nickname=acct.name,
                           qbo_account_id=acct.qbo_id, realm_id=realm,
                           qbo_account_name=acct.name)
    db.add(row)
    return row


@app.post("/accounts/source/primary")
def set_source_primary(company: str = Form(...), source: str = Form(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Designate the operating account card payments are paid from.

    Exactly one per company: setting a new one clears the last, because two
    primary accounts would leave the write path choosing between them and it
    has no basis to.
    """
    from .models_db import SourceAccountRow
    row = _row_for_source(db, company, source)
    if row is None:
        raise HTTPException(404, "No such card account")
    if not row.qbo_account_id:
        raise HTTPException(400, "Map this account to QuickBooks first — a "
                                 "payment has to come from a real account")
    db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.is_primary.is_(True)).update({"is_primary": False})
    row.is_primary = True
    db.add(AuditRow(company=company, action="primary_account",
                    detail=f"payments now paid from {row.nickname or source}",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"source": source, "is_primary": True}


@app.post("/accounts/source/add")
def add_source_account(company: str = Form(""), source: str = Form(""),
                       nickname: str = Form(""), file_format: str = Form(""),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_admin)):
    """Register a card program before any of its charges have been uploaded.

    The list is otherwise derived from charges, which is a chicken-and-egg
    problem: a new program can't be mapped to a QuickBooks account until
    someone has already uploaded a file for it.
    """
    from .models_db import SourceAccountRow
    # Validated here rather than by FastAPI's required-field check, so an empty
    # box gets a sentence instead of a 422 the UI can only render as a blob.
    if not (company or "").strip():
        raise HTTPException(400, "Company is required")
    key = (source or "").strip().lower()
    if not key:
        raise HTTPException(400, "Card account key is required")
    if not key.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "Use letters, numbers, dashes or underscores only")
    existing = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == key).first()
    if existing:
        raise HTTPException(
            400, f"A card account called "
                 f"'{existing.nickname or existing.source}' already exists.")
    _assert_account_unique(db, company, key, nickname, "")
    # id is the composite "{company}:{source}", matching save_account_mapping.
    from .ingestion import SOURCE_SIGNATURES
    fmt = (file_format or "").strip().lower()
    if fmt and fmt not in SOURCE_SIGNATURES:
        raise HTTPException(
            400, f"File format must be one of {', '.join(sorted(SOURCE_SIGNATURES))}"
                 f", or blank for the pre-formatted export")
    db.add(SourceAccountRow(id=f"{company}:{key}", company=company, source=key,
                            nickname=(nickname or key).strip(),
                            file_format=fmt,
                            qbo_account_id="", qbo_account_name=""))
    db.add(AuditRow(company=company, action="card_account_added",
                    detail=f"{key} ({nickname or key})", dry_run=False,
                    actor=user.username))
    db.commit()
    return {"added": key}


@app.post("/accounts/source/delete")
def delete_source_account(company: str = Form(...), source: str = Form(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    """Remove a card program's mapping.

    Refuses while charges still reference it: deleting the mapping would leave
    those charges unpostable with no obvious cause, and the charges themselves
    are the record of what happened. Clear or exclude them first.
    """
    from .models_db import SourceAccountRow, ChargeRow, CardRow
    from sqlalchemy import func as _f
    by_status = dict(db.query(ChargeRow.status, _f.count(ChargeRow.charge_id))
                     .filter(ChargeRow.company == company,
                             ChargeRow.source == source)
                     .group_by(ChargeRow.status).all())
    total = sum(by_status.values())
    if total:
        # Any status blocks, including excluded and categorized: the charges are
        # the record of what happened, and removing the mapping they resolve
        # through would leave that record unexplainable later. The counts are
        # deliberately not quoted -- they sent the reader to a tab to go and
        # delete things, which is the wrong instinct when retiring is right there.
        raise HTTPException(
            400, "Transactions have been coded on this account. "
                 "Consider unchecking the In Use box instead.")
    # The name people know it by, read BEFORE it is deleted: the audit entry
    # used to record only the internal key, like "divvy".
    _gone = db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == source).first()
    _gone_name = _gone.nickname if _gone and _gone.nickname else source
    db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company,
        SourceAccountRow.source == source).delete()
    db.query(CardRow).filter(CardRow.company == company,
                             CardRow.source == source).delete()
    db.add(AuditRow(company=company, action="card_account_removed",
                    detail=f"{_gone_name} removed ({source})",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"removed": source}


@app.post("/cards/map-source")
def map_source(source: str = Form(...), qbo_account_id: str = Form(...),
               qbo_account_name: str = Form(...), company: str = Form(""),
               only_unmapped: bool = Form(True),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_write)):
    """Map every card from one program to a QBO account in one go.

    Virtual-card programs issue a card per buyer -- there are ~1,000 distinct
    last-4s in a single month -- but they all settle to the same QuickBooks
    account. Mapping them individually isn't realistic, so the default is to
    map by program and override the exceptions afterwards.
    """
    from .models_db import CardRow, ChargeRow
    from sqlalchemy import func

    seen = db.query(ChargeRow.card_last4, func.max(ChargeRow.cardholder_name)).filter(
        ChargeRow.company == company, ChargeRow.source == source,
        ChargeRow.card_last4 != "").group_by(ChargeRow.card_last4).all()

    n = 0
    for last4, holder in seen:
        cid = f"{source}:{last4}"
        existing = db.get(CardRow, cid)
        if only_unmapped and existing and existing.qbo_account_id:
            continue
        db.merge(CardRow(
            id=cid, source=source, last4=last4,
            nickname=existing.nickname if existing else "",
            holder=holder or "", company=company,
            qbo_account_id=qbo_account_id.strip(),
            qbo_account_name=qbo_account_name.strip(),
            active=True, updated_at=datetime.utcnow()))
        n += 1
        if n % 300 == 0:
            db.commit()
    db.commit()
    return {"source": source, "mapped": n, "account": qbo_account_name}


_DUP_SUFFIX = _re.compile(r"\s*\((\d+)\)\s*$")


def _base_name(name: str) -> str:
    """An account name with a trailing "(2)" taken off.

    This is the shape the duplicates actually take: "Office Supplies" and
    "Office Supplies (1)" side by side in the chart of accounts. They are two
    different names, so collapsing on the name alone did nothing about them --
    which is the whole point of normalizing here instead.
    """
    return _DUP_SUFFIX.sub("", str(name or "")).strip()


def _duplicate_map(rows: list) -> dict:
    """row id -> the account this one appears to duplicate.

    Two shapes, because both turn up in the same chart of accounts:
      * a NUMBERED near-duplicate -- "Office Supplies (1)" beside "Office
        Supplies", which is what a duplicate creation leaves behind; and
      * an EXACT repeat -- the same name twice under different QuickBooks ids,
        which is indistinguishable on screen and impossible to choose between.

    The unsuffixed one, or the first by sort order, is treated as the original.
    Nothing is hidden here; the caller decides whether to badge or collapse.
    """
    out: dict = {}
    plain: dict = {}
    for r in rows:                      # originals first, in sort order
        if not _DUP_SUFFIX.search(r.name or ""):
            plain.setdefault(_base_name(r.name).lower(), r)
    for r in rows:
        key = _base_name(r.name).lower()
        first = plain.get(key)
        if first is None or first.id == r.id:
            continue
        out[r.id] = first.name
    return out


def _one_per_name(rows: list) -> list:
    """One row per account name, newest sync wins.

    A name is what a person picks and what a rule stores, so two rows that
    resolve to the same name are indistinguishable on screen -- choosing
    between them is a choice nobody can make correctly. Where the mirror holds
    both, the most recently synced is kept; it is the one QuickBooks confirmed
    last.

    This hides a symptom rather than curing it, and deliberately so: the causes
    are fixed upstream (paged account queries, path-exact account lookup), but
    a file that already contains duplicates should not keep showing them until
    somebody merges them in QuickBooks.
    """
    from datetime import datetime as _dt
    best: dict = {}
    for r in rows:
        # Same key as _duplicate_map: whatever that calls a duplicate, this
        # collapses. Two different answers to "are these the same account?"
        # would put a badge on the chart of accounts for a pair the picker
        # still offered twice.
        key = _base_name(r.name).lower()
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        # THE UNSUFFIXED ONE WINS. "Office Supplies" is the account somebody
        # meant; "Office Supplies (1)" is what a duplicate creation left
        # behind. Only when both or neither carry a suffix does the newest
        # sync decide.
        r_plain = not _DUP_SUFFIX.search(r.name or "")
        c_plain = not _DUP_SUFFIX.search(cur.name or "")
        if r_plain != c_plain:
            if r_plain:
                best[key] = r
            continue
        if (r.synced_at or _dt.min) > (cur.synced_at or _dt.min):
            best[key] = r
    return list(best.values())


def _category_order(r):
    """Assets, then expenses, then liabilities, then bank accounts.

    Ordered by how often each is reached rather than alphabetically: season
    accounts are assets and carry most of the coding, and the bank accounts
    exist for card payments, which are a small share of the queue.
    """
    t = (r.account_type or "").strip().lower()
    if (r.usable_for or "") == "bank":
        rank = 3
    elif "asset" in t:
        rank = 0
    elif "expense" in t or "cost of goods" in t:
        rank = 1
    elif "liab" in t or "payable" in t or "credit card" in t:
        rank = 2
    else:
        rank = 2                        # anything unrecognized sits with the rest
    return (rank, (r.name or "").lower())


@app.get("/qbo/categories")
def qbo_categories(company: str = Query(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    """Cached expense/asset accounts. Refresh via /admin/sync-qbo."""
    from .models_db import QboRefRow
    # Bank accounts are included. A card payment is coded to the bank it was
    # paid FROM, and that account is not an expense category -- so the picker
    # that had only categories in it could not express the most common coding
    # in the queue. Flagged, so the UI can mark them.
    # ACTIVE ONLY, and each name once.
    #
    # Two faults met here. The picker never filtered on `active`, so an account
    # retired in QuickBooks stayed on offer -- and QuickBooks renames a
    # deactivated account, which is where a second entry with a suffix on the
    # end came from. And nothing collapsed by name, so wherever the mirror held
    # two rows resolving to the same name, the list showed it twice with no way
    # to tell which was which.
    #
    # The realm is pinned too: with more than one QuickBooks file connected the
    # picker was concatenating both charts of accounts.
    realm = _current_realm(db, company)
    qy = db.query(QboRefRow).filter(
        QboRefRow.kind == "account",
        QboRefRow.active.isnot(False),
        QboRefRow.usable_for.in_(("category", "bank")))
    # AN UNRESOLVED REALM MEANS NOTHING, NOT EVERYTHING.
    #
    # `if realm:` skipped the filter when the realm came back empty, so a
    # company mapped to no QuickBooks file was handed every file's references.
    # An empty result says "this company is not connected yet", which is true
    # and fixable; a full one says "these are your accounts", which is a lie
    # about somebody else's ledger.
    qy = qy.filter(_eq_or_none(QboRefRow.realm_id, realm))
    rows = _one_per_name(qy.all())
    return {"categories": [{"id": r.qbo_id, "name": r.name,
                            "fully_qualified": r.name, "type": r.account_type,
                            "subtype": r.subtype,
                            "is_bank": r.usable_for == "bank"}
                           for r in sorted(rows, key=_category_order)]}


@app.get("/qbo/bank-accounts")
def qbo_bank_accounts(company: str = Query(""),
                      companies: str = Query(""),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    """Bank and credit-card accounts -- what a card program settles to.

    Separate from /qbo/categories, which lists expense accounts. Offering the
    expense list here would let someone point a card program at an expense
    account, which QuickBooks accepts and then posts wrongly.
    """
    if company == ALL_COMPANIES:
        # Each company's bank and card accounts, keyed "Company|id".
        _acc = []
        for _co in _reachable(db, user, companies):
            for x in qbo_bank_accounts(company=_co, companies="", db=db, user=user).get("accounts") or []:
                _acc.append({**x, "id": f"{_co}|{x['id']}", "company": _co,
                             "name": f"{_co} \u00b7 {x['name']}"})
        return {"accounts": _acc}
    from .models_db import QboRefRow
    # ONE COMPANY'S BANK ACCOUNTS. This had no realm filter at all, so the
    # card-account mapping picker offered every connected QuickBooks file's
    # accounts -- and a company mapped to none was offered all of them, which
    # is how an unmapped company still looked like it had a chart of accounts.
    rows = db.query(QboRefRow).filter(
        QboRefRow.kind == "account", QboRefRow.usable_for == "bank",
        _eq_or_none(QboRefRow.realm_id, _current_realm(db, company))).all()
    return {"accounts": [{"id": r.qbo_id, "name": r.name,
                          "type": r.account_type}
                         for r in sorted(rows, key=lambda x: (x.name or "").lower())]}


@app.get("/qbo/vendors")
def qbo_vendors(company: str = Query(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    from .models_db import QboRefRow
    # ONE COMPANY'S VENDORS. Every connected file's vendors share this table,
    # so without the realm filter the picker offered another company's vendor
    # list -- and coding a charge to a vendor that does not exist in its own
    # QuickBooks file fails at post time, long after the mistake was made.
    qy = db.query(QboRefRow).filter(QboRefRow.kind == "vendor",
                                    QboRefRow.active.isnot(False))
    realm = _current_realm(db, company)
    # AN UNRESOLVED REALM MEANS NOTHING, NOT EVERYTHING.
    #
    # `if realm:` skipped the filter when the realm came back empty, so a
    # company mapped to no QuickBooks file was handed every file's references.
    # An empty result says "this company is not connected yet", which is true
    # and fixable; a full one says "these are your accounts", which is a lie
    # about somebody else's ledger.
    qy = qy.filter(_eq_or_none(QboRefRow.realm_id, realm))
    rows = _one_per_name(qy.all())
    return {"vendors": [{"id": r.qbo_id, "name": r.name}
                        for r in sorted(rows, key=lambda x: (x.name or "").lower())]}


@app.get("/qbo/accounts")
def qbo_accounts(db: Session = Depends(get_session),
                 company: str = Query(""),
                 user: UserRow = Depends(require_reviewer)):
    """Cached bank / credit-card accounts. Refresh via /admin/sync-qbo."""
    from .models_db import QboRefRow
    # ONE COMPANY'S BANK ACCOUNTS. No realm filter at all here, so the
    # card-account mapping picker offered every connected QuickBooks file's
    # accounts -- and an unmapped company was offered all of them.
    rows = db.query(QboRefRow).filter(
        QboRefRow.kind == "account", QboRefRow.usable_for == "bank",
        _eq_or_none(QboRefRow.realm_id, _current_realm(db, company))).all()
    return {"accounts": [{"id": r.qbo_id, "name": r.name, "type": r.account_type,
                          "subtype": r.subtype, "realm_id": r.realm_id}
                         for r in sorted(rows, key=lambda x: (x.account_type, x.name))]}


# --- admin actions ----------------------------------------------------------

@app.get("/admin/settings")
def get_settings(db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Runtime settings, with the env-var value as the fallback."""
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, "hal_seasons")
    return {
        "hal_seasons": (row.value if row else _cv.value("hal_seasons")),
        "hal_seasons_source": "database" if row else "environment",
        "available_seasons": ["24/25", "25/26", "26/27", "27/28", "28/29"],
        "updated_by": row.updated_by if row else "",
        "updated_at": row.updated_at if row else None,
    }


@app.post("/admin/settings")
def save_settings(hal_seasons: str = Form(...),
                  db: Session = Depends(get_session),
                  admin: UserRow = Depends(require_write)):
    """Change which STH season columns are mirrored, without a redeploy."""
    from .models_db import AppSettingRow
    valid = {"24/25", "25/26", "26/27", "27/28", "28/29"}
    chosen = [s.strip() for s in hal_seasons.split(",") if s.strip()]
    bad = [s for s in chosen if s not in valid]
    if bad:
        raise HTTPException(400, f"unknown season(s): {bad}")
    if not chosen:
        raise HTTPException(400, "pick at least one season")

    db.merge(AppSettingRow(key="hal_seasons", value=",".join(chosen),
                           updated_by=admin.username, updated_at=datetime.utcnow()))
    db.commit()
    return {"hal_seasons": ",".join(chosen),
            "note": "takes effect on the next STH sync"}


def _need_airtable(db, company: str) -> None:
    """Refuse, plainly, a sync that has nothing to run with.

    The token and base are entered on Connectors now; the deployment's own are
    not read. Said here rather than left to fail inside a background job,
    where nobody would see why.
    """
    from .worker import airtable_companies
    if company and company not in airtable_companies(db):
        raise HTTPException(
            400, "Enter this company's Airtable token and base on Connectors "
                 "first, then sync")
    if not company and not airtable_companies(db):
        raise HTTPException(
            400, "No company has an Airtable token and base on Connectors yet")


@app.post("/admin/sync-hal/stop")
def admin_stop_sync_hal(company: str = Query(""),
                        admin: UserRow = Depends(require_write)):
    """Stop a running STH sync at its next checkpoint (every 500 records).

    What it has stored so far stays stored; the next sync carries on from the
    full list again.
    """
    from .worker import request_stop, sth_status_key
    request_stop(sth_status_key(company))
    return {"stopping": True,
            "note": "the sync will stop within a few hundred records"}


@app.post("/admin/sync-emails/stop")
def admin_stop_sync_emails(company: str = Query(""), admin: UserRow = Depends(require_write)):
    """Stop an address book sync before it saves; what was there stays."""
    from .worker import request_stop
    request_stop(f"emails:{company}" if company else "emails")
    return {"stopping": True, "note": "the sync will stop before it saves"}


@app.post("/admin/sync-emails")
def admin_sync_emails(company: str = Query(""),
                      db: Session = Depends(get_session),
                      admin: UserRow = Depends(require_write)):
    """Mirror cardholder name -> email from the Airtable address book.

    Runs inline: it is one small table, unlike the STH sync. Returns the counts
    so the result is verifiable rather than a hopeful "started". Run as the
    company whose Sync button was pressed, with its token from Connectors.
    """
    from .worker import sync_profile_emails_all, airtable_companies
    from .review import reset_email_cache
    _need_airtable(db, company)
    n = sync_profile_emails_all(company=company)
    reset_email_cache()
    return {"addresses": n}


@app.get("/admin/email-diagnose")
def diagnose_cardholder(name: str = Query(""), company: str = Query(""),
                        db: Session = Depends(get_session),
                        user: UserRow = Depends(require_reviewer)):
    """Why does this Profile name resolve to an address, or not?

    The mirror either has a person or it doesn't, and when it doesn't there are
    several quite different reasons -- the name is spelled differently, the
    address book has no address for them, or their address was discarded for
    being shared. They need different fixes, and the resolve count alone cannot
    tell them apart.
    """
    from .models_db import ProfileEmailRow
    from .review import _emails_for, _loose_name

    key = (name or "").strip().lower()
    if not key:
        raise HTTPException(400, "Give a Profile name to look up")

    # One company's address book: another broker's people are not evidence.
    _mine = db.query(ProfileEmailRow).filter(ProfileEmailRow.company == company)
    exact = [r.email for r in _mine.filter(ProfileEmailRow.name_key == key).all()]
    lk = _loose_name(key)
    loose_rows = []
    if lk:
        # Compared in Python: the loose key is computed, not stored, so there is
        # nothing to filter on in SQL.
        for r in _mine.all():
            if _loose_name(r.name_key) == lk:
                loose_rows.append(r)
    loose_owners = sorted({r.name_key for r in loose_rows})

    resolved = sorted(_emails_for(db, name, company))
    if exact:
        why = "exact name match"
    elif len(loose_owners) == 1:
        why = f"matched loosely to '{loose_owners[0]}'"
    elif len(loose_owners) > 1:
        why = ("the loose name is ambiguous — it matches "
               + ", ".join(f"'{o}'" for o in loose_owners[:6])
               + " — so no address is used")
    else:
        why = "no person in the mirror has this name"

    # Near misses, so a spelling difference is visible rather than guessed at.
    # Fuzzy, not substring: the cases that matter most are a letter apart --
    # "Nisenfeld" on the card against "Nissenfeld" in the address book -- and a
    # substring test finds neither of them.
    near = []
    if not exact and not loose_owners:
        import difflib
        by_key: dict = {}
        for r in _mine.all():
            by_key.setdefault(r.name_key, []).append(r.email)
        for k in difflib.get_close_matches(key, list(by_key), n=8, cutoff=0.75):
            near.append({"name_key": k, "email": ", ".join(sorted(by_key[k])[:3])})
    return {"name": name, "key": key, "loose_key": lk,
            "resolves": bool(resolved), "emails": resolved,
            "why": why, "exact_emails": exact,
            "loose_matches": loose_owners, "near_misses": near}


@app.get("/admin/email-shared-drops")
def email_shared_drops(user: UserRow = Depends(require_reviewer)):
    """What the last address-book pull did, stage by stage."""
    from .integrations.airtable import (LAST_SHARED_DROPS, LAST_SHARED_SAMPLE,
                                        LAST_PULL_STATS)
    return {"dropped": LAST_SHARED_DROPS, "sample": LAST_SHARED_SAMPLE,
            "stats": LAST_PULL_STATS}


@app.get("/admin/email-status")
def email_status(company: str = Query(""), db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """How much of the address book is mirrored, and whether it is being used.

    'matched' is the number that actually helps: cardholder names on charges
    that resolve to an address. A big mirror and a small match count means the
    names on the cards are written differently from the names in Airtable.
    """
    from .models_db import ProfileEmailRow, ChargeRow
    from sqlalchemy import func as _f
    from .review import _loose_name
    # THIS COMPANY'S COUNTS. Counting the whole table showed every company the
    # same 17,695 -- whichever company had synced last.
    _pe = ProfileEmailRow.company == company
    addresses = db.query(_f.count(ProfileEmailRow.email)).filter(_pe).scalar() or 0
    people = db.query(_f.count(_f.distinct(ProfileEmailRow.name_key))).filter(_pe).scalar() or 0
    known = {k for (k,) in db.query(ProfileEmailRow.name_key).filter(_pe).distinct().all()}
    names = {(n or "").strip().lower()
             for (n,) in db.query(ChargeRow.cardholder_name).distinct().all() if n}
    # Count resolution the way the engine actually resolves it: an exact name_key
    # hit OR an unambiguous loose-name hit (first+last, letters only, order-free).
    # Reporting only exact matches understated the real figure badly -- the
    # engine already falls back to the loose key, so a status that ignored it
    # made a working mirror look broken.
    loose_owners: dict = {}
    for k in known:
        lk = _loose_name(k)
        if lk:
            loose_owners.setdefault(lk, set()).add(k)
    loose_known = {lk for lk, owners in loose_owners.items() if len(owners) == 1}

    def _resolves(n: str) -> bool:
        if n in known:
            return True
        lk = _loose_name(n)
        return bool(lk) and lk in loose_known

    matched = sum(1 for n in names if _resolves(n))
    from .models_db import SyncStatusRow
    st = db.get(SyncStatusRow, f"emails:{company}" if company else "emails")
    # THIS COMPANY'S OWN RUN. The address book is still one list for every
    # company, so its size is only shown to a company whose own sync of it has
    # worked -- a company with a made-up key showed Y&S's 17,704 addresses.
    ok = bool(st and st.state == "ok") or not company
    return {"addresses": addresses if ok else None,
            "people": people if ok else None,
            "cardholders": len(names), "matched": matched,
            "state": st.state if st else "never run",
            "error": (st.detail if st and st.state == "error" else ""),
            "last_sync": st.finished_at if st and st.finished_at else None,
            "unmatched": sorted(n for n in names if not _resolves(n))[:12]}


@app.get("/admin/sync-state/{name}")
def sync_state(name: str, db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """One sync's state, for a screen waiting on a background job."""
    from .models_db import SyncStatusRow
    st = db.get(SyncStatusRow, name)
    if st is None:
        return {"name": name, "state": "never run", "detail": "", "records": 0}
    # A "RUNNING" THAT NEVER ENDED. A sync that dies partway -- most often
    # because a deploy restarted the engine under it -- leaves its record
    # saying running for ever, and every screen waiting on it said "still
    # running from earlier" indefinitely. Past 45 minutes it is reported as
    # interrupted: no sync here legitimately takes that long, and the next
    # scheduled run starts it again anyway.
    state = st.state
    if (state == "running" and st.started_at
            and datetime.utcnow() - st.started_at > timedelta(minutes=45)):
        state = "interrupted"
    # A company's STH sync is only running while it holds its lock here.
    if state == "running" and name.startswith("hal:"):
        from .worker import sth_running as _sr
        if not _sr(name.split(":", 1)[1]):
            state = "interrupted"
    return {"name": name, "state": state, "detail": st.detail or "",
            "records": st.records or 0,
            "started_at": st.started_at.isoformat() + "Z" if st.started_at else None,
            "finished_at": st.finished_at.isoformat() + "Z" if st.finished_at else None}


@app.post("/admin/sync-hal")
def admin_sync_hal(background: BackgroundTasks, company: str = Query(""),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Kick off the Airtable STH mirror refresh, for one company.

    Runs in the background: ~18k records over paged requests takes minutes,
    far longer than an HTTP request should hang. As the company whose Sync
    button was pressed, so it uses that company's token and base.
    """
    from .worker import sync_hal_all, start_full_sth
    _need_airtable(db, company)
    if company:
        # The same background full run the scheduler uses -- one per company
        # at a time, so pressing Sync while one runs does not start a second.
        if not start_full_sth(company):
            return {"started": False,
                    "note": "a sync is already running for this company"}
        return {"started": True,
                "note": "syncing in the background; this row shows progress"}
    background.add_task(sync_hal_all, None, company)
    return {"started": True,
            "note": "syncing in the background; refresh the page in a few minutes"}


@app.post("/admin/sync-bills")
def admin_sync_bills(background: BackgroundTasks, company: str = Query(""),
                     days: int = Query(120),
                     db: Session = Depends(get_session),
                     admin: UserRow = Depends(require_write)):
    """Pull open bills (Balance > 0) from every connected QBO company file."""
    from .models_db import QboTokenRow, CompanyRow
    realms = [r.realm_id for r in db.query(QboTokenRow).all()]
    if not realms:
        raise HTTPException(400, "QuickBooks is not connected yet")
    if not company:
        reg = db.query(CompanyRow).filter(CompanyRow.active.is_(True)).first()
        if reg is None:
            raise HTTPException(400, "no company registered yet")
        company = reg.name

    from datetime import date as _d, timedelta as _td
    from .db import SessionLocal
    from .persistence import sync_bills_from_qbo

    def _run():
        s = SessionLocal()
        try:
            for realm in realms:
                sync_bills_from_qbo(s, company, realm,
                                    since=_d.today() - _td(days=days))
        finally:
            s.close()

    background.add_task(_run)
    return {"started": True, "realms": realms,
            "note": "pulling open bills in the background; reopen Setup shortly"}


@app.get("/admin/bills-status")
def bills_status(company: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    from .models_db import BillRow
    return {"company": company,
            "bills": db.query(BillRow).filter(BillRow.company == company).count()}


@app.post("/admin/sync-qbo")
def admin_sync_qbo(background: BackgroundTasks, days: int = Query(120),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Refresh everything from QuickBooks: open bills, chart of accounts, vendors.

    This is the button to press after changing something in QBO and wanting the
    app to reflect it immediately, rather than waiting for the hourly run.
    """
    from .models_db import QboTokenRow
    if not db.query(QboTokenRow).count():
        raise HTTPException(400, "QuickBooks is not connected yet")
    from .worker import sync_qbo_all
    background.add_task(sync_qbo_all, days)
    return {"started": True,
            "note": "refreshing bills, accounts and vendors in the background"}


@app.get("/admin/qbo-status")
def qbo_sync_status(company: str = Query(""),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_reviewer)):
    from .models_db import BillRow, QboRefRow, SyncStatusRow
    st = db.get(SyncStatusRow, "qbo")
    return {
        "bills": db.query(BillRow).filter(BillRow.company == company).count(),
        "categories": db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.usable_for == "category").count(),
        "bank_accounts": db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.usable_for == "bank").count(),
        "vendors": db.query(QboRefRow).filter(QboRefRow.kind == "vendor").count(),
        "state": st.state if st else "never run",
        "detail": st.detail if st else "",
        "last_synced": st.finished_at if st else None,
    }


@app.get("/admin/schema")
def schema_status(admin: UserRow = Depends(require_admin)):
    """Which columns the database has, for diagnosing a bad deploy."""
    from sqlalchemy import inspect as _inspect
    from .db import engine as _e
    insp = _inspect(_e)
    from .alembic_boot import current_revision
    return {
        "alembic_revision": current_revision(),
        "tables": {t: sorted(c["name"] for c in insp.get_columns(t))
                   for t in sorted(insp.get_table_names())},
    }


@app.get("/admin/hal-status")
def hal_status(company: str = Query(""), db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """One company's STH mirror: ITS records, ITS last sync, ITS state.

    It used to count every STH record in the database and report one shared
    status, so every company's Airtable row showed Y&S's numbers -- even a
    company whose key and base were made up.
    """
    from .models_db import HalRow, SyncStatusRow
    from .worker import sth_status_key, sth_running
    q = db.query(HalRow)
    if company:
        q = q.filter(HalRow.company == company)
    n = q.count()
    latest = q.order_by(HalRow.synced_at.desc()).first()
    st = db.get(SyncStatusRow, sth_status_key(company))
    from .hal_cache import stats as _cache_stats
    return {"records": n, "cache": _cache_stats(),
            "last_synced": latest.synced_at if latest else None,
            "configured": bool(_cv.value("airtable_token")),
            # RUNNING ONLY WHILE IT RUNS. A status left saying "running" by a
            # sync that died -- a deploy restarting the engine under it, or a
            # final status write that failed -- is reported as interrupted,
            # because in this process nothing holds the company's sync lock.
            "state": ("running" if company and sth_running(company)
                      else ("interrupted" if (company and st and st.state == "running")
                            else (st.state if st else "never run"))),
            "detail": st.detail if st else "",
            "error": (st.detail if st and st.state == "error" else ""),
            "status_key": sth_status_key(company),
            "finished_at": st.finished_at if st else None}


# --- QuickBooks OAuth -------------------------------------------------------

@app.get("/qbo/connect")
def qbo_connect(token: str | None = Query(None),
                db: Session = Depends(get_session)):
    """Start the OAuth flow.

    Browser navigation can't send an Authorization header, so this accepts the
    token as a query parameter instead. Admin is still required -- the token is
    verified the same way, just carried differently.
    """
    import jwt as _jwt
    from jwt import PyJWTError as JWTError
    if not token:
        raise HTTPException(401, "pass ?token=<your access token>")
    try:
        payload = _jwt.decode(token, _auth._secret(), algorithms=[_auth.ALGORITHM])
    except JWTError:
        raise HTTPException(401, "invalid or expired token")
    # OWNER OR ADMIN. This compared against "admin" alone, so an owner -- the
    # role that outranks admin and is the only one that can see every company
    # -- was refused the one action that connects a new QuickBooks file.
    if payload.get("role") not in ("owner", "admin"):
        raise HTTPException(403, "admin required")
    try:
        url, state = qbo_oauth.build_authorize_url()
    except qbo_oauth.OAuthError as e:
        raise HTTPException(400, str(e))
    db.add(OAuthStateRow(state=state))
    db.commit()
    return RedirectResponse(url)


@app.get("/qbo/callback")
def qbo_callback(code: str = Query(...), realmId: str = Query(...),
                 state: str = Query(...), db: Session = Depends(get_session)):
    """Intuit redirects here with the authorization code."""
    row = db.get(OAuthStateRow, state)
    if row is None:
        raise HTTPException(400, "unknown or reused state — restart the flow")
    if row.created_at < datetime.utcnow() - timedelta(minutes=15):
        db.delete(row); db.commit()
        raise HTTPException(400, "state expired — restart the flow")
    db.delete(row); db.commit()

    try:
        # NOT gated here. This is the callback that CREATES the token, and the
        # gate asks whether a token exists for the realm -- so checking it
        # first meant a new QuickBooks file could never be connected, which is
        # the "No QuickBooks file is connected" you hit after clearing
        # QBO_ALLOWED_REALMS. save_tokens still honors a pinned list.
        tokens = qbo_oauth.exchange_code(code, realmId)
        qbo_tokens.save_tokens(db, tokens)
        # store QBO's own company name so the picker matches what the team sees
        try:
            from .integrations.qbo_bills import company_name as _cn
            from .models_db import QboTokenRow as _Q
            row = db.get(_Q, realmId)
            if row is not None:
                row.company_name = _cn(db, realmId)
                db.commit()
        except Exception:
            pass
    except qbo_tokens.RealmNotAllowed as e:
        raise HTTPException(403, str(e))
    except qbo_oauth.OAuthError as e:
        raise HTTPException(400, str(e))

    # WHAT THE GATES ACTUALLY SAY, not a fixed sentence.
    #
    # This always read "read-only until the write gates are flipped", whether
    # or not they were already flipped -- so a deployment that could post said
    # it could not, and the only way to find out which was true was to try a
    # post and see. A status line that is right half the time is worse than no
    # status line, because it gets believed.
    from .models_db import CompanyRow as _CR
    _mapped = db.query(_CR).filter(_CR.realm_id == realmId).first()
    if settings.dry_run:
        note = "DRY_RUN is on — nothing will be written to QuickBooks"
    elif not settings.qbo_write_enabled:
        note = "QBO_WRITE_ENABLED is off — nothing will be written to QuickBooks"
    elif _mapped is None:
        note = ("connected, but no company is mapped to this file yet — "
                "map it on Companies before posting")
    else:
        note = f"ready to post for {_mapped.name}"
    # BACK TO THE APP, not a page of JSON.
    #
    # This is the end of a browser redirect chain, so whatever it returns is
    # what the person sees -- and they saw a raw payload and had to navigate
    # back by hand. They came from Companies to connect a file; they are
    # returned there, with the outcome in the address so the screen can say it.
    from starlette.responses import RedirectResponse
    return RedirectResponse(
        "/?connected=" + _q.quote(realmId)
        + "&note=" + _q.quote(note),
        status_code=303)


def _connections_with_company(db: Session) -> list[dict]:
    """The QuickBooks connections, each carrying the company it serves."""
    from .models_db import CompanyRow as _CR2
    by_realm = {c.realm_id: c.name for c in db.query(_CR2).all() if c.realm_id}
    out = []
    for c in qbo_tokens.connection_status(db):
        c = dict(c)
        c["company"] = by_realm.get(c.get("realm_id"), "")
        out.append(c)
    return out


@app.get("/qbo/status")
def qbo_status(db: Session = Depends(get_session),
               admin: UserRow = Depends(require_owner)):
    return {
        "environment": settings.qbo_environment,
        "allowed_realms": sorted(qbo_tokens.allowed_realms(db)),
        # WHICH COMPANY EACH FILE IS MAPPED TO, so a screen can pick the one
        # it is showing. Without it the Connectors screen matched on a field
        # that was never sent, fell through to the first connection, and named
        # the same QuickBooks file under every company.
        "connections": _connections_with_company(db),
        "dry_run": settings.dry_run,
        "write_enabled": settings.qbo_write_enabled,
    }


@app.post("/qbo/disconnect/{realm_id}")
def qbo_disconnect(realm_id: str, db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_owner)):
    from .models_db import QboTokenRow
    row = db.get(QboTokenRow, realm_id)
    if row is None:
        raise HTTPException(404, "not connected")
    qbo_oauth.revoke(row.refresh_token)
    db.delete(row); db.commit()
    return {"disconnected": realm_id}


@app.get("/qbo/company/{realm_id}")
def qbo_company(realm_id: str, db: Session = Depends(get_session),
                admin: UserRow = Depends(require_admin)):
    """Cheapest possible check that a connection actually works."""
    from .integrations.qbo_bills import company_name, count_open_bills, QboReadError
    try:
        return {"realm_id": realm_id, "company_name": company_name(db, realm_id),
                "open_bills": count_open_bills(db, realm_id)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/bills/status/{company}")
def bills_status(company: str, scope: str = Query("open"), db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """THE OPEN BILLS LINE: how many open bills are mirrored from this
    company's QuickBooks file, and when they were last read (hourly).
    `scope=all`: every bill, for All Bills."""
    from sqlalchemy import func as _fb
    from .models_db import BillRow as _BR, SyncStatusRow as _SS
    if scope == "all":
        from .models_db import AllBillRow as _AB
        q = db.query(_fb.count(_AB.bill_id))
        if company != ALL_COMPANIES:
            q = q.filter(_AB.company == company)
        _last = db.query(_fb.max(_AB.updated_at))
        if company != ALL_COMPANIES:
            _last = _last.filter(_AB.company == company)
        _st = _last.scalar()
        return {"count": int(q.scalar() or 0), "synced_at": (_st.isoformat() + "Z") if _st else ""}
    q = db.query(_fb.count(_BR.bill_id)).filter(_BR.balance > 0)
    if company != ALL_COMPANIES:
        q = q.filter(_BR.company == company)
    n = q.scalar() or 0
    stamp = None
    if company == ALL_COMPANIES:
        rows = db.query(_SS).filter(_SS.name.like("bills:%")).all()
        stamp = min([r.finished_at for r in rows if r.finished_at], default=None)
    else:
        r = db.get(_SS, f"bills:{company}")
        stamp = r.finished_at if r else None
    if stamp is None:
        r = db.get(_SS, "qbo")
        stamp = r.finished_at if r and r.state == "ok" else None
    return {"count": int(n), "synced_at": (stamp.isoformat() + "Z") if stamp else ""}


@app.post("/bills/sync/{company}")
def bills_sync_now(company: str, scope: str = Query("open"), db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """SYNC FROM QBO on the Open Bills tab: this company's open bills, now.
    On All Bills (`scope=all`) every bill is read again in full as well."""
    from .worker import sync_bills_all
    if company == ALL_COMPANIES:
        raise HTTPException(400, "Choose a company to sync its bills")
    try:
        n = sync_bills_all(only=company, all_full=(scope == "all"))
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(400, f"QuickBooks: {e}")
    return {"ok": True, "synced": n, **bills_status(company=company, scope=scope, db=db, user=user)}


@app.post("/qbo/sync-bills/{company}")
def qbo_sync_bills(company: str, realm_id: str = Query(...),
                   days: int = Query(120),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_write)):
    """Pull open bills from QBO into the local candidate pool."""
    from datetime import date as _date, timedelta as _td
    from .persistence import sync_bills_from_qbo
    try:
        # Bills changed, so every unresolved charge's match strength may have
        # too -- refresh it here rather than on the next page load.
        n = sync_bills_from_qbo(db, company, realm_id,
                                since=_date.today() - _td(days=days))
    except Exception as e:
        raise HTTPException(400, str(e))
    from .review import refresh_scores
    rescored = refresh_scores(db, company)
    return {"company": company, "realm_id": realm_id, "open_bills_synced": n,
            "rescored": rescored}


# --- review queue (read model for the UI) -----------------------------------

@app.get("/companies/registry")
def company_registry(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """The companies this engine services. Everyone reads; only admins write."""
    from .models_db import CompanyRow, ChargeRow, BillRow, QboTokenRow
    from sqlalchemy import func
    charges = dict(db.query(ChargeRow.company, func.count(ChargeRow.charge_id))
                   .group_by(ChargeRow.company).all())
    bills = dict(db.query(BillRow.company, func.count(BillRow.bill_id))
                 .group_by(BillRow.company).all())
    rows = db.query(CompanyRow).filter(CompanyRow.active.is_(True)).all()
    # ONLY THE COMPANIES THIS PERSON BELONGS TO. The picker and the landing
    # page both read this, so filtering here is what makes another company
    # invisible rather than merely refused -- being told "no access" to a
    # client you were never meant to know about is itself a disclosure.
    from . import membership as _mb
    _mine = _mb.companies_for(db, user)
    if _mine is not None:
        rows = [r for r in rows if r.name in set(_mine)]
    return {"companies": [{
        "id": c.id, "name": c.name, "realm_id": c.realm_id,
        "qbo_company_name": c.qbo_company_name,
        "charges": charges.get(c.name, 0), "bills": bills.get(c.name, 0),
    } for c in sorted(rows, key=lambda x: x.name)]}


@app.get("/companies/available-realms")
def available_realms(db: Session = Depends(get_session),
                     admin: UserRow = Depends(require_owner)):
    """Connected QuickBooks files, and whether each is already claimed."""
    from .models_db import QboTokenRow, CompanyRow
    taken = {c.realm_id: c.name for c in db.query(CompanyRow).all() if c.realm_id}
    out = []
    for t in db.query(QboTokenRow).all():
        name = t.company_name
        if not name:
            try:
                from .integrations.qbo_bills import company_name as _cn
                name = _cn(db, t.realm_id)
                t.company_name = name
                db.commit()
            except Exception:
                name = t.realm_id
        out.append({"realm_id": t.realm_id, "qbo_name": name,
                    "linked_to": taken.get(t.realm_id)})
    return {"realms": out}


@app.post("/companies")
def create_company(name: str = Form(...), realm_id: str = Form(...),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_owner)):
    """Onboard a company and link it to a QuickBooks file. Admin only."""
    from .models_db import CompanyRow, QboTokenRow
    name = name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    if db.get(QboTokenRow, realm_id) is None:
        raise HTTPException(400, f"realm {realm_id} is not connected")
    existing = db.query(CompanyRow).filter(CompanyRow.realm_id == realm_id).first()
    if existing is not None:
        raise HTTPException(409,
            f"that QuickBooks file is already linked to '{existing.name}'")
    if db.query(CompanyRow).filter(CompanyRow.name == name).first():
        raise HTTPException(409, f"a company named '{name}' already exists")

    tok = db.get(QboTokenRow, realm_id)
    db.add(CompanyRow(id=realm_id, name=name, realm_id=realm_id,
                      qbo_company_name=tok.company_name or "", active=True))
    db.commit()
    return {"created": name, "realm_id": realm_id}


@app.post("/companies/{company_id}/rename")
def rename_registered(company_id: str, new_name: str = Form(...),
                      db: Session = Depends(get_session),
                      admin: UserRow = Depends(require_owner)):
    """Rename a company and move all its data to the new name."""
    from .models_db import CompanyRow
    c = db.get(CompanyRow, company_id)
    if c is None:
        raise HTTPException(404, "no such company")
    old, new = c.name, new_name.strip()
    if not new or old == new:
        raise HTTPException(400, "give a different name")
    result = rename_company(old=old, new=new, db=db, admin=admin)
    c.name = new
    db.commit()
    return result


@app.get("/companies")
def companies(db: Session = Depends(get_session),
              user: UserRow = Depends(require_reviewer)):
    """Every company name that appears in the data, and where it came from.

    Charges carry whatever name was typed at upload; the QuickBooks file has
    its own name. If they differ, the queue filters on one and finds nothing --
    so surface both rather than silently showing an empty list.
    """
    from sqlalchemy import func, select as _sel
    from .models_db import ChargeRow, BillRow, QboTokenRow

    charge_counts = dict(db.query(ChargeRow.company, func.count(ChargeRow.charge_id))
                         .group_by(ChargeRow.company).all())
    bill_counts = dict(db.query(BillRow.company, func.count(BillRow.bill_id))
                       .group_by(BillRow.company).all())
    qbo = [{"realm_id": t.realm_id, "name": t.company_name or t.realm_id}
           for t in db.query(QboTokenRow).all()]

    names = sorted(set(charge_counts) | set(bill_counts) | {q["name"] for q in qbo})
    return {
        "companies": [{
            "name": n,
            "charges": charge_counts.get(n, 0),
            "bills": bill_counts.get(n, 0),
            "in_quickbooks": any(q["name"] == n for q in qbo),
        } for n in names],
        "quickbooks": qbo,
    }


@app.get("/companies/orphans")
def orphan_data(db: Session = Depends(get_session),
                admin: UserRow = Depends(require_owner)):
    """Data filed under a company name that isn't registered.

    Happens when charges were uploaded before the registry existed, or under a
    different spelling. Without surfacing it, the rows are invisible: the queue
    filters by the selected company and simply shows nothing.
    """
    from sqlalchemy import func
    from .models_db import ChargeRow, BillRow, CompanyRow

    registered = {c.name for c in db.query(CompanyRow).all()}
    charges = dict(db.query(ChargeRow.company, func.count(ChargeRow.charge_id))
                   .group_by(ChargeRow.company).all())
    bills = dict(db.query(BillRow.company, func.count(BillRow.bill_id))
                 .group_by(BillRow.company).all())

    orphans = []
    for name in sorted(set(charges) | set(bills)):
        if name in registered:
            continue
        orphans.append({"name": name, "charges": charges.get(name, 0),
                        "bills": bills.get(name, 0)})
    return {"orphans": orphans, "registered": sorted(registered)}


@app.post("/companies/adopt")
def adopt_orphans(orphan_name: str = Form(...), company: str = Form(...),
                  db: Session = Depends(get_session),
                  admin: UserRow = Depends(require_owner)):
    """Move data from an unregistered name onto a registered company."""
    from .models_db import CompanyRow
    if db.query(CompanyRow).filter(CompanyRow.name == company).first() is None:
        raise HTTPException(400, f"'{company}' is not a registered company")
    result = rename_company(old=orphan_name, new=company, db=db, admin=admin)

    # charge_ids are company-scoped, so rewrite the keys to match their new home
    from .models_db import ChargeRow
    moved = 0
    for row in db.query(ChargeRow).filter(ChargeRow.company == company).all():
        parts = row.charge_id.split(":", 2)
        if len(parts) == 3 and parts[0] != company:
            row.charge_id = f"{company}:{parts[1]}:{parts[2]}"
            moved += 1
        elif len(parts) == 2:                     # pre-scoping id
            row.charge_id = f"{company}:{parts[0]}:{parts[1]}"
            moved += 1
    db.commit()
    result["charge_ids_rekeyed"] = moved
    return result


@app.post("/companies/rename")
def rename_company(old: str = Form(...), new: str = Form(...),
                   db: Session = Depends(get_session),
                   admin: UserRow = Depends(require_owner)):
    """Move all data from one company name to another.

    For when charges were uploaded under a name that doesn't match the
    QuickBooks company, which leaves the queue looking empty.
    """
    from .models_db import (ChargeRow, BillRow, MatchRow, AuditRow, UploadRow,
                            SourceAccountRow, CardRow, LearnedRuleRow)
    old, new = old.strip(), new.strip()
    if not old or not new or old == new:
        raise HTTPException(400, "give two different names")

    moved = {}
    # CompanyRow keys on `name`, not `company`, and the caller updates it.
    for model in (ChargeRow, BillRow, MatchRow, AuditRow, UploadRow, CardRow):
        n = db.query(model).filter(model.company == old).update(
            {model.company: new}, synchronize_session=False)
        if n:
            moved[model.__tablename__] = n

    # these are keyed "{company}:..." / "{company}|...", so rebuild the ids
    for row in db.query(SourceAccountRow).filter(
            SourceAccountRow.company == old).all():
        db.delete(row)
        db.add(SourceAccountRow(
            id=f"{new}:{row.source}", company=new, source=row.source,
            nickname=row.nickname, qbo_account_id=row.qbo_account_id,
            qbo_account_name=row.qbo_account_name, realm_id=row.realm_id))
        moved["source_accounts"] = moved.get("source_accounts", 0) + 1

    for row in db.query(LearnedRuleRow).filter(
            LearnedRuleRow.company == old).all():
        db.delete(row)
        db.add(LearnedRuleRow(
            id=f"{new}|{row.id.split('|', 1)[-1]}", company=new,
            name=row.name, detail_op=row.detail_op,
            detail_value=row.detail_value, card_account=row.card_account,
            amount_op=row.amount_op, amount_value=row.amount_value,
            amount_value2=row.amount_value2, mode=row.mode,
            merchant_key=row.merchant_key, sample_merchant=row.sample_merchant,
            category=row.category, vendor=row.vendor,
            last_actor=row.last_actor, updated_at=row.updated_at))
        moved["learned_rules"] = moved.get("learned_rules", 0) + 1

    db.commit()
    return {"from": old, "to": new, "moved": moved}


@app.get("/review/companies")
def review_companies(db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    from .review import list_companies
    return list_companies(db)


@app.get("/review/{company}")
def review(company: str, limit: int = Query(200),
           db: Session = Depends(get_session),
           user: UserRow = Depends(require_reviewer)):
    from .review import build_review
    return build_review(db, company, limit=limit)


@app.post("/reconcile/{company}")
def reconcile_company(company: str, db: Session = Depends(get_session),
                      user: UserRow = Depends(require_write)):
    rows = run_reconciliation(db, company)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.decision] += 1
    return {"company": company, "charges": len(rows), "counts": dict(counts)}


@app.get("/results/{company}")
def results(company: str, db: Session = Depends(get_session),
            user: UserRow = Depends(require_reviewer)):
    rows = db.scalars(select(MatchRow).where(MatchRow.company == company)).all()
    grouped: dict[str, list] = defaultdict(list)
    for r in rows:
        grouped[r.decision].append({
            "charge_id": r.charge_id,
            "bill_id": r.bill_id,
            "score": round(r.score, 2),
            "overwrite_to": str(r.overwrite_bill_amount) if r.overwrite_bill_amount else None,
            "reasons": r.reasons.split(" | ") if r.reasons else [],
        })
    return {"company": company, "results": grouped}


@app.get("/audit/{company}")
def audit(company: str, db: Session = Depends(get_session),
          user: UserRow = Depends(require_reviewer)):
    rows = db.scalars(
        select(AuditRow).where(AuditRow.company == company).order_by(AuditRow.at)
    ).all()
    return {"company": company, "entries": [
        {"action": a.action, "charge_id": a.charge_id, "bill_id": a.bill_id,
         "detail": a.detail, "dry_run": a.dry_run, "at": a.at.isoformat()}
        for a in rows
    ]}


@app.post("/qbo/post/{company}")
def qbo_post(company: str, ids: str = Form(""), limit: int = Form(25),
             db: Session = Depends(get_session),
             admin: UserRow = Depends(require_write)):
    """Write categorized charges to QuickBooks. Admin only.

    Defaults to a small batch so the first live run can be checked by hand in
    QuickBooks before doing more. Already-posted charges are skipped, so a
    repeat call cannot duplicate anything.
    """
    from .integrations.qbo_write import push_batch, WriteBlocked
    id_list = [i for i in ids.split(",") if i.strip()] or None
    try:
        return push_batch(db, company, admin.username,
                          charge_ids=id_list, limit=limit)
    except WriteBlocked as e:
        raise HTTPException(400, str(e))


@app.get("/qbo/post-status/{company}")
def qbo_post_status(company: str, db: Session = Depends(get_session),
                    user: UserRow = Depends(require_reviewer)):
    """What is ready to post, and what already has been."""
    from .models_db import ChargeRow
    ready = db.query(ChargeRow).filter(
        ChargeRow.company == company, ChargeRow.status == "categorized",
        ChargeRow.qbo_txn_id == "").count()
    posted = db.query(ChargeRow).filter(
        ChargeRow.company == company, ChargeRow.qbo_txn_id != "").count()
    return {"ready_to_post": ready, "already_posted": posted,
            "dry_run": settings.dry_run,
            "write_enabled": settings.qbo_write_enabled,
            "writes_possible": (not settings.dry_run) and settings.qbo_write_enabled}


@app.post("/push/{company}")
def push(company: str, db: Session = Depends(get_session),
         admin: UserRow = Depends(require_write)):
    return push_company(db, company)


# --- Plaid: connecting a personal card to the feed ---------------------------
#
# A card account on the Card Accounts screen is normally fed by uploads. These
# endpoints let one be fed by a live bank connection instead, which is the
# whole point for Amex, Citi and Chase: they have no card-program API, so the
# only way in is for the cardholder to authenticate through Plaid Link.
#
# Two things make this flow different from every other connector:
#
#   * Only the CARDHOLDER can complete it. Link opens in their browser and
#     they type their own bank credentials, which nobody here ever sees.
#   * A broken connection is a normal state, not an error. Repair goes through
#     Link again in "update mode", which fixes the SAME connection rather than
#     creating a second one -- important on Plaid's Trial plan, where the
#     ten-connection cap does not recycle.

@app.get("/plaid/status")
def plaid_status(company: str = Query(""),
                 companies: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Which card accounts are bank-connected, and whether each is healthy.

    Keyed by source so the Card Accounts grid and the queue's card picker can
    both badge a row without a second round trip.
    """
    if company == ALL_COMPANIES:
        # Each company's bank connections, keyed "Company|card account".
        _con, _conf, _env = {}, False, ""
        for _co in _reachable(db, user, companies):
            r = plaid_status(company=_co, companies="", db=db, user=user)
            _con.update({f"{_co}|{s_}": v for s_, v in (r.get("connected") or {}).items()})
            _conf = _conf or bool(r.get("configured"))
            _env = r.get("environment") or _env
        return {"company": ALL_COMPANIES, "connected": _con, "configured": _conf,
                "environment": _env}
    from .models_db import PlaidItemRow
    from .integrations import plaid_api
    out = {}
    for it in db.query(PlaidItemRow).filter(
            PlaidItemRow.company == company,
            PlaidItemRow.active.is_(True)).all():
        from .plaid_sync import start_date as _psd
        _sd = _psd(db, it.id)
        out[it.source] = {
            "item_id": it.id,
            "start_date": _sd.isoformat() if _sd else "",
            "institution": it.institution_name,
            "mask": it.mask,
            "cardholder": it.cardholder,
            "status": it.status,
            "last_error": it.last_error,
            "last_success_at": (it.last_success_at.isoformat()
                                if it.last_success_at else ""),
            # Distinguishes "nothing was spent" from "this stopped working
            # three weeks ago" -- the failure that is otherwise invisible.
            "stale_hours": (
                round((datetime.utcnow() - it.last_success_at).total_seconds() / 3600, 1)
                if it.last_success_at else None),
        }
    return {"company": company, "connected": out,
            "configured": plaid_api.configured(),
            "environment": settings.plaid_environment}


@app.post("/plaid/link-token")
def plaid_link_token(source: str = Form(...), company: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Open Link for this card account — to connect it, or to repair it.

    If a connection already exists the token is issued in update mode against
    its access token, so the cardholder re-authenticates the connection they
    have instead of creating a new one beside it.
    """
    from .models_db import PlaidItemRow
    from .integrations import plaid_api
    existing = db.query(PlaidItemRow).filter(
        PlaidItemRow.company == company, PlaidItemRow.source == source,
        PlaidItemRow.active.is_(True)).first()
    try:
        res = plaid_api.create_link_token(
            user_id=f"{company}:{source}",
            access_token=(existing.access_token if existing else ""),
            days_requested=_cv.value("plaid_days_requested"),
            webhook=(f"{settings.app_base_url.rstrip('/')}/plaid/webhook"
                     if settings.app_base_url else ""))
    except plaid_api.PlaidError as e:
        raise HTTPException(400, str(e))
    return {"link_token": res.get("link_token", ""),
            "mode": "update" if existing else "new",
            "expiration": res.get("expiration", "")}


def _ensure_card_account(db, company: str, source: str) -> bool:
    """Make sure `source` is a card account of this company; True if it is.

    A "qbo:702" source is a QuickBooks bank account nobody has set up as a card
    account yet. Divvy and Slash routing create one on the spot; Plaid's final
    step did not, and refused with "no card account called 'qbo:702'" after
    the bank login had already succeeded. Created here from THIS company's own
    QuickBooks file -- never by the id alone, since ids repeat across files.
    """
    from .models_db import QboRefRow as _QRe, SourceAccountRow as _SAe
    if db.query(_SAe).filter(_SAe.company == company,
                             _SAe.source == source).first():
        return True
    if not source.startswith("qbo:"):
        return False
    from .review import _company_realm as _cre
    realm = _cre(db, company)
    if not realm:
        return False
    qid = source.split(":", 1)[1]
    ref = db.query(_QRe).filter(_QRe.kind == "account", _QRe.qbo_id == qid,
                                _QRe.realm_id == realm).first()
    if ref is None:
        return False
    db.add(_SAe(id=f"{company}:{source}", company=company, source=source,
                nickname=ref.name or source, qbo_account_id=ref.qbo_id,
                qbo_account_name=ref.name or "", realm_id=realm))
    db.commit()
    return True


@app.post("/plaid/start-date")
def plaid_start_date(item_id: str = Form(...), start: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Set (or clear) the date a Plaid connection imports from.

    Plaid decides how much history it shares when the card is connected, and
    that cannot be changed afterwards -- so the engine applies its own start
    date: nothing earlier is imported, and anything earlier still in For
    Review from this connection is taken back out. Anything already coded or
    posted is left alone.
    """
    from .models_db import PlaidItemRow, ChargeRow, AppSettingRow
    it = db.get(PlaidItemRow, item_id)
    if it is None:
        raise HTTPException(404, "No such bank connection")
    d = None
    if start.strip():
        try:
            d = datetime.strptime(start.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "The start date must be YYYY-MM-DD")
    key = f"plaid_start:{it.id}"
    removed = 0
    if d is None:
        row = db.get(AppSettingRow, key)
        if row is not None:
            db.delete(row)
    else:
        db.merge(AppSettingRow(key=key, value=d.isoformat()))
        for r in db.query(ChargeRow).filter(
                ChargeRow.company == it.company, ChargeRow.source == it.source,
                ChargeRow.status == "for_review", ChargeRow.txn_date < d).all():
            if getattr(r, "qbo_txn_id", None):
                continue
            db.delete(r)
            removed += 1
    db.add(AuditRow(company=it.company, action="plaid_start_date",
                    detail=f"{it.source}: import from "
                           f"{__import__('app.usdates', fromlist=['x']).us_date(d) if d else 'the start of what Plaid shares'}"
                           + (f"; {removed} earlier charge(s) removed from For Review"
                              if removed else ""),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True, "start_date": d.isoformat() if d else "",
            "removed": removed}


@app.post("/plaid/statements/refresh")
def plaid_statements_refresh(company: str = Form(...), source: str = Form(""),
                             db: Session = Depends(get_session),
                             user: UserRow = Depends(require_admin)):
    """READ STATEMENTS NOW, after Allow Statements: the cardholder has just
    granted the consent, so this company's cards are read again at once (the
    daily read and the new-connection read both skip a card that already has a
    note such as "Reconnect this card once to allow statements")."""
    from . import plaid_statements as _psr
    if not _psr.enabled():
        return {"statements": 0, "without": 0, "enabled": False}
    try:
        r = _psr.refresh(db, company, only_missing=False)
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(400, f"Could not read statements: {e}")
    # A just-granted consent can take Plaid a little while to apply: read once
    # more shortly in the background.
    _psr.refresh_soon(company, delay=60)
    st = _psr.get(db, company, source) if source else {}
    return {**r, "enabled": True, "statement": st}


@app.post("/plaid/attach")
def plaid_attach(item_id: str = Form(...), links: str = Form(...),
                 company: str = Form(""), start: str = Form(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Connect MORE cards from a bank login that has just been connected.

    One login often carries several cards. Plaid's one-time token can only be
    exchanged once -- the old flow tried a second exchange for a second card,
    which Plaid refuses -- so the extra cards reuse the login already stored.
    `links` is [{"account_id": ..., "source": ...}]: which card account each
    of the login's accounts is. Each becomes its own connection, pulling only
    its own account.
    """
    import json as _jl
    from .integrations import plaid_api
    from .models_db import PlaidItemRow, SourceAccountRow, AppSettingRow
    base = db.get(PlaidItemRow, item_id)
    if base is None or base.company != company:
        raise HTTPException(404, "That bank login is not connected here")
    try:
        want = [x for x in _jl.loads(links or "[]")
                if x.get("account_id") and x.get("source")]
    except ValueError:
        raise HTTPException(400, "links must be a JSON list")
    try:
        accts = {a.get("account_id"): a for a in
                 (plaid_api.get_accounts(base.access_token) or {}).get("accounts") or []}
    except plaid_api.PlaidError as e:
        raise HTTPException(400, str(e))
    _sd = None
    if start.strip():
        try:
            _sd = datetime.strptime(start.strip(), "%Y-%m-%d").date()
        except ValueError:
            _sd = None
    done, skipped = [], []
    for x in want:
        acct = accts.get(x["account_id"])
        src = x["source"]
        if acct is None:
            skipped.append(f"{src}: that account is not on this login")
            continue
        if not _ensure_card_account(db, company, src):
            skipped.append(f"{src}: no such card account")
            continue
        # A card already fed by ANOTHER login is left alone -- two feeds on
        # one card would import the same spending twice.
        other = (db.query(PlaidItemRow).filter(
            PlaidItemRow.company == company, PlaidItemRow.source == src,
            PlaidItemRow.active.is_(True)).first())
        # THE SAME LOGIN under another row (a card reconnected, or allowing
        # statements) is not "another bank login": same access token, same login.
        if other is not None and not (other.id == item_id
                                      or other.id.startswith(item_id + ":")
                                      or (other.access_token and other.access_token == base.access_token)):
            skipped.append(f"{src}: already connected to another bank login")
            continue
        if other is not None and other.access_token == base.access_token and other.id != item_id \
                and not other.id.startswith(item_id + ":"):
            # Already connected through this same login: nothing to add.
            done.append(src)
            continue
        rid = item_id if src == base.source else f"{item_id}:{acct['account_id']}"
        row = db.get(PlaidItemRow, rid) or PlaidItemRow(id=rid)
        row.company, row.source = company, src
        row.access_token = base.access_token
        row.institution_id = base.institution_id
        row.institution_name = (acct.get("official_name") or acct.get("name")
                                or base.institution_name or "")
        row.account_id = acct.get("account_id", "")
        row.mask = acct.get("mask", "") or ""
        row.cardholder = base.cardholder
        row.status, row.last_error, row.active = "ok", "", True
        row.updated_at = datetime.utcnow()
        db.add(row)
        if _sd is not None:
            db.merge(AppSettingRow(key=f"plaid_start:{rid}", value=_sd.isoformat()))
        db.add(AuditRow(company=company, action="plaid_connected",
                        detail=f"{src} linked to {row.institution_name} ••{row.mask}"
                               f" (same bank login as {base.source})",
                        actor=user.username))
        done.append(src)
    db.commit()
    return {"connected": done, "skipped": skipped}


@app.post("/plaid/exchange")
def plaid_exchange(public_token: str = Form(...), source: str = Form(...),
                   company: str = Form(""), start: str = Form(""),
                   cardholder: str = Form(""),
                   account_id: str = Form(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_admin)):
    """Finish a link: swap the one-time token and record the connection.

    `account_id` matters for Amex in particular, where several cards can sit on
    one login and each arrives as its own sub-account with its own last four.
    Left empty, the first credit or depository account is taken, which is right
    for the single-card case and wrong to assume for the rest -- so the UI asks
    when there is more than one.
    """
    from .models_db import PlaidItemRow, SourceAccountRow
    from .integrations import plaid_api

    if not _ensure_card_account(db, company, source):
        raise HTTPException(400, f"no card account called '{source}'")

    try:
        ex = plaid_api.exchange_public_token(public_token)
        access = ex.get("access_token", "")
        item_id = ex.get("item_id", "")
        accts = (plaid_api.get_accounts(access) or {})
    except plaid_api.PlaidError as e:
        raise HTTPException(400, str(e))

    cards = accts.get("accounts") or []
    # WHICH account on the login this card is: the one named, else the one
    # whose last four the card's name carries (Citi #8625 -> 8625), and only
    # then the first. Taking the first every time connected a Citi #8625 card
    # to account 0645.
    from .plaid_sync import pick_account as _pick
    _sa = db.query(SourceAccountRow).filter(SourceAccountRow.company == company,
                                            SourceAccountRow.source == source).first()
    chosen = _pick(cards, ((_sa.nickname or _sa.qbo_account_name) if _sa else "")
                   or source, account_id)
    inst = (accts.get("item") or {}).get("institution_id", "")

    row = db.get(PlaidItemRow, item_id) or PlaidItemRow(id=item_id)
    row.company = company
    row.source = source
    row.access_token = access
    row.institution_id = inst
    row.institution_name = (chosen.get("official_name")
                            or chosen.get("name") or inst or "")
    row.account_id = chosen.get("account_id", "")
    row.mask = chosen.get("mask", "") or ""
    row.cardholder = cardholder.strip()
    row.status = "ok"
    row.last_error = ""
    row.active = True
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company=company, action="plaid_connected",
                    detail=f"{source} linked to {row.institution_name} "
                           f"••{row.mask} ({cardholder or 'unnamed'})",
                    actor=user.username))
    # THE START DATE chosen beside Connect, saved before the first pull so
    # that pull already starts from it (see plaid_sync.start_date).
    if start.strip():
        try:
            _sd = datetime.strptime(start.strip(), "%Y-%m-%d").date()
            from .models_db import AppSettingRow as _ASx
            db.merge(_ASx(key=f"plaid_start:{row.id}", value=_sd.isoformat()))
        except ValueError:
            pass
    db.commit()
    # ITS STATEMENT straight away (shortly), not at the next day's read.
    try:
        from . import plaid_statements as _pst_new
        _pst_new.refresh_soon(row.company or company)
    except Exception:                                   # noqa: BLE001
        log.exception("could not schedule the statement read")
    return {"item_id": item_id, "source": source, "mask": row.mask,
            "institution": row.institution_name,
            # So the UI can offer a picker when the login carried several cards.
            "accounts": [{"account_id": a.get("account_id"),
                          "name": a.get("name"), "mask": a.get("mask"),
                          "type": a.get("type"), "subtype": a.get("subtype")}
                         for a in cards]}


@app.post("/plaid/disconnect")
def plaid_disconnect(source: str = Form(...), company: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Stop pulling this card. The charges already imported are untouched.

    Deactivated rather than deleted, and the Plaid Item is deliberately NOT
    removed: on a Trial plan /item/remove does not free the slot, so deleting
    would spend a connection permanently and gain nothing.
    """
    from .models_db import PlaidItemRow
    row = db.query(PlaidItemRow).filter(
        PlaidItemRow.company == company, PlaidItemRow.source == source,
        PlaidItemRow.active.is_(True)).first()
    if row is None:
        raise HTTPException(404, "that card account is not connected")
    row.active = False
    row.updated_at = datetime.utcnow()
    # ITS STATEMENT GOES TOO: a disconnected card's last statement date and
    # balance went on showing as though still read.
    from . import plaid_statements as _pst_d
    _pst_d.clear(db, company, source)
    db.add(AuditRow(company=company, action="plaid_disconnected",
                    detail=f"{source} ({row.institution_name} ••{row.mask})",
                    actor=user.username))
    db.commit()
    return {"source": source, "disconnected": True}


@app.post("/plaid/sync")
def plaid_sync_now(company: str = Form(""), source: str = Form(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write)):
    """Pull now rather than waiting for the schedule."""
    from .models_db import PlaidItemRow
    from . import plaid_sync as _ps
    if source:
        row = db.query(PlaidItemRow).filter(
            PlaidItemRow.company == company, PlaidItemRow.source == source,
            PlaidItemRow.active.is_(True)).first()
        if row is None:
            raise HTTPException(404, "that card account is not connected")
        return _ps.sync_item(db, row, actor=user.username)
    with _cv.for_company(db, company):
        return _ps.sync_all(db, company, actor=user.username)


@app.post("/plaid/webhook")
async def plaid_webhook(request: Request, db: Session = Depends(get_session)):
    """Plaid tells us something changed. Public by necessity, verified always.

    This is what turns the feed from "hourly" into "minutes": rather than the
    schedule asking whether anything is new, Plaid says so when it is.

    Two rules govern the shape of this handler.

    **Verify before reading.** The endpoint cannot require a session token --
    Plaid has none -- so anything on the internet can reach it. A forged
    ITEM_ERROR would mark a healthy card as broken and quietly stop its charges
    arriving, which is worse than a forged pull. So the signature is checked
    against the RAW body first, and the parsed payload comes out of that check
    rather than out of FastAPI.

    **Answer immediately, work afterwards.** Plaid expects a fast response and
    retries on timeout; a sync that walks a card's history would blow through
    that and be re-delivered while still running. So the sync goes on a thread
    and this returns at once. Losing a webhook costs nothing anyway -- the
    hourly pass is the backstop, and the cursor means a repeated pull imports
    nothing twice.
    """
    from .integrations import plaid_webhook as _verify
    from .models_db import PlaidItemRow

    raw = await request.body()
    try:
        payload = _verify.verify(raw, request.headers.get("Plaid-Verification", ""))
    except _verify.VerificationError as e:
        log.warning("rejected plaid webhook: %s", e)
        # 401 rather than 400: this is an authentication failure, and Plaid
        # does not retry it into a loop.
        raise HTTPException(401, "webhook verification failed")

    wtype = str(payload.get("webhook_type") or "")
    code = str(payload.get("webhook_code") or "")
    item_id = str(payload.get("item_id") or "")
    # EVERY CARD ON THE LOGIN. Several cards can share one bank login, each
    # its own connection: the first under the login's id, the rest under
    # "<login id>:<account id>". A notice about the login concerns them all.
    _all = ([r for r in db.query(PlaidItemRow).filter(
        (PlaidItemRow.id == item_id) | PlaidItemRow.id.like(item_id + ":%")).all()
             if r.active] if item_id else [])
    item = _all[0] if _all else None
    if item is None or not item.active:
        # A connection we no longer pull, or one belonging to another
        # environment. Acknowledged so Plaid stops retrying.
        return {"ok": True, "ignored": "unknown or inactive connection"}

    handled = ""

    if wtype == "TRANSACTIONS" and code in (
            "SYNC_UPDATES_AVAILABLE", "DEFAULT_UPDATE",
            "INITIAL_UPDATE", "HISTORICAL_UPDATE"):
        handled = "sync"

        def _pull(ids=tuple(r.id for r in _all)):
            from .db import SessionLocal
            from . import plaid_sync as _ps
            for iid in ids:
                s = SessionLocal()
                try:
                    row = s.get(PlaidItemRow, iid)
                    if row is not None and row.active:
                        _ps.sync_item(s, row, actor="plaid-webhook")
                except Exception:                       # noqa: BLE001
                    log.exception("webhook-triggered sync failed for %s", iid)
                finally:
                    s.close()

        threading.Thread(target=_pull, daemon=True).start()

    elif wtype == "ITEM":
        err = (payload.get("error") or {}).get("error_code", "")
        if code == "ERROR" and err == "ITEM_LOGIN_REQUIRED":
            for _r in _all:
                _r.status, _r.last_error = "login_required", err
            handled = "login_required"
        elif code == "PENDING_EXPIRATION":
            # The consent is about to lapse. Flagged NOW rather than when it
            # lapses, because the fix needs the cardholder and they are easier
            # to reach before their card goes quiet than after.
            item.status, item.last_error = "login_required", "PENDING_EXPIRATION"
            handled = "expiring"
        elif code == "USER_PERMISSION_REVOKED":
            item.status = "error"
            item.last_error = "USER_PERMISSION_REVOKED"
            item.active = False
            handled = "revoked"
        elif code == "LOGIN_REPAIRED":
            item.status, item.last_error = "ok", ""
            handled = "repaired"
        if handled:
            item.updated_at = datetime.utcnow()
            db.commit()

    return {"ok": True, "webhook": f"{wtype}/{code}", "handled": handled or "noted"}


@app.get("/admin/why")
def admin_why(charge_id: str = Query(...),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_reviewer)):
    """Why did this charge get this vendor, category and STH record?

    A wrong prefill is nearly impossible to argue with from the queue: the row
    says RULE, and which rule -- out of a table of hundreds -- is not on screen.
    This answers it directly, naming the rule, the condition that matched, and
    every rule that would have matched had the first not won.

    It also shows the STH side: the team the bank detail was read as claiming,
    where that claim came from, and whether the record was accepted or thrown
    away by the team check.
    """
    from .models_db import ChargeRow
    from .learning import rules_for, rule_matches, rule_conditions
    from .review import (_to_charge, _bare_team, _same_team, load_hal_index,
                         team_claim_pairs, load_match_weights)
    from engine.hal import expand_text, validate as hal_validate

    row = db.get(ChargeRow, charge_id)
    if row is None:
        raise HTTPException(404, "no such charge")

    # Same registration the queue does, so this answers about the live
    # configuration rather than whatever a previous request left behind.
    load_match_weights(db)

    charge = _to_charge(row, db)

    # --- rules -------------------------------------------------------------
    matches, inert = [], []
    for r in rules_for(db, row.company):
        conds = rule_conditions(r)
        entry = {
            "id": r.id,
            "mode": r.mode,
            "vendor": r.vendor,
            "category": r.category,
            "tests": conds,
            "bank_detail_phrase": r.detail_value or "",
            "detail_op": r.detail_op or "contains",
            "card_account": r.card_account or "",
            "amount_op": r.amount_op or "any",
            "amount_value": (str(r.amount_value)
                             if r.amount_value is not None else ""),
        }
        if (r.mode or "off") != "off" and not conds:
            # The dangerous shape: nothing to test. Inert now, but it was
            # matching everything before, so it is called out rather than
            # merely omitted.
            entry["warning"] = ("this rule tests NOTHING — it matched every "
                                "charge in the company until it was made inert")
            inert.append(entry)
            continue
        try:
            if rule_matches(r, charge):
                matches.append(entry)
        except Exception as e:                              # noqa: BLE001
            entry["error"] = str(e)[:120]
            inert.append(entry)

    # --- the team the bank detail claims ------------------------------------
    text = expand_text(f"{row.merchant or ''} {row.raw_description or ''}")
    claims = []
    for k, v in team_claim_pairs(db):
        if k and k in text:
            claims.append({"claimed_by": k, "team": v})
    claims.sort(key=lambda c: -len(c["claimed_by"]))
    early = claims[0]["team"] if claims else ""

    # The SAME two-step the coding path uses: the alias table by substring,
    # then the strict word test. Reporting only the first would have this
    # screen say "no alias matched" for a charge the engine resolves happily --
    # a diagnostic that disagrees with the code it explains is worse than none.
    if not early:
        try:
            from engine.suggest import team_for_prefill as _tfp
            early = _tfp(f"{row.merchant or ''} {row.raw_description or ''}") or ""
            if early:
                claims.append({"claimed_by": "(whole-word match on the team name)",
                               "team": early})
        except Exception:                                       # noqa: BLE001
            pass

    hint = (early or row.coded_vendor or row.suggested_vendor or "")

    # --- STH ----------------------------------------------------------------
    hal = {"hint": hint, "hint_from": ("bank detail claim" if early
                                       else "vendor already on the row" if hint
                                       else "nothing")}
    if not row.cardholder_name:
        hal["result"] = "not looked up — the charge has no cardholder name"
    else:
        idx = load_hal_index(db)
        if idx is None:
            hal["result"] = "no STH records mirrored yet"
        else:
            try:
                v = hal_validate(charge, idx, team_hint=hint)
                rec = getattr(v, "record", None)
                if rec is None:
                    hal["result"] = "STH found no record for this cardholder"
                else:
                    want = _bare_team(hint).lower()
                    got = _bare_team(getattr(rec, "team", "") or "").lower()
                    kept = (not want or not got or _same_team(want, got))
                    hal["found_team"] = rec.team
                    hal["found_record"] = rec.record_id
                    hal["team_check"] = f"{want or '(no hint)'} vs {got}"
                    hal["result"] = ("shown" if kept else
                                     "DISCARDED — the record's team does not "
                                     "match the hint")
            except Exception as e:                          # noqa: BLE001
                hal["result"] = f"STH raised: {str(e)[:160]}"

    # --- shared billing entities -------------------------------------------
    # Why an amber badge did or did not appear. The narrowing needs a candidate
    # set AND something to choose within it, and either can be missing.
    from .review import team_claim_candidates
    shared = {}
    try:
        cands = team_claim_candidates(db)
        low = expand_text(f"{row.merchant or ''} {row.raw_description or ''}")
        hit = next((k for k in sorted(cands, key=len, reverse=True)
                    if len(k) > 3 and k in low), "")
        if not hit:
            shared["matched"] = ("no shared alias in this bank detail — either "
                                 "the alias is on one team only (so it should "
                                 "resolve outright) or it is not written at all")
        else:
            names = cands[hit]
            shared["alias"] = hit
            shared["candidates"] = names
            from .models_db import HalRow as _HR
            held = sorted({_bare_team(h.team or "") for h in db.query(_HR).filter(
                _HR.company == row.company,
                _HR.holder_name == row.cardholder_name).all()}) if row.cardholder_name else []
            shared["cardholder_holds"] = held
            overlap = [t for t in names if t in held]
            shared["candidates_held"] = overlap
            if len(overlap) == 1:
                shared["result"] = f"narrowed to {overlap[0]} — only account held"
            elif len(overlap) > 1:
                shared["result"] = ("holds BOTH candidates, so holdings decide "
                                    "nothing; falls through to billing day, "
                                    "then installment amount")
            else:
                shared["result"] = ("holds NEITHER candidate, so there is "
                                    "nothing to narrow with — this is why no "
                                    "badge appeared")
    except Exception as e:                                      # noqa: BLE001
        shared["error"] = str(e)[:160]

    # --- why the vendor field is what it is --------------------------------
    # Two lookups fill this row, against the same team, from the same table.
    # When the category arrives and the vendor does not, the difference is in
    # how each one matched -- and that is only visible from here.
    from .review import _team_vendor, _team_account, _vendor_words
    from .models_db import QboRefRow
    vend = {}
    try:
        _t = early or ""
        vend["team_resolved"] = _t or "(none — no alias matched the bank detail)"
        if _t:
            vend["vendor_found"] = _team_vendor(db, _t) or "(none)"
            vend["account_found"] = _team_account(db, _t, "TC") or "(none)"
            want = _vendor_words(_t)
            vend["team_words"] = sorted(want)
            # Every QuickBooks vendor sharing a word with the team, so a name
            # spelled differently is visible rather than guessed at.
            near = []
            for r in db.query(QboRefRow.name).filter(
                    QboRefRow.kind == "vendor").all():
                nm = r[0] or ""
                w = _vendor_words(nm)
                if w & want:
                    near.append({"name": nm, "words": sorted(w),
                                 "exact_word_match": w == want})
            vend["vendors_sharing_a_word"] = near[:25]
            vend["vendor_rows_in_table"] = db.query(QboRefRow).filter(
                QboRefRow.kind == "vendor").count()
    except Exception as e:                                      # noqa: BLE001
        vend["error"] = str(e)[:200]

    return {
        "charge_id": charge_id,
        "vendor_lookup": vend,
        "shared_entity": shared,
        "bank_detail": row.merchant or "",
        "card_account": row.source,
        "cardholder": row.cardholder_name or "",
        "amount": str(row.amount),
        "vendor_now": row.coded_vendor or row.suggested_vendor or "",
        "category_now": row.coded_category or row.suggested_category or "",
        # First one wins; the rest are shown so an overlap is visible.
        "rule_applied": matches[0] if matches else None,
        "other_rules_that_would_match": matches[1:],
        "rules_that_test_nothing": inert,
        "team_claims_in_bank_detail": claims,
        "hal": hal,
    }


# --- QBO Sync: chart of accounts and vendors --------------------------------

@app.get("/qbo/refs/{kind}")
def list_qbo_refs(kind: str, q: str = Query(""), limit: int = Query(500),
                  include_inactive: bool = Query(False),
                  company: str = Query(""),
                  companies: str = Query(""),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """The mirrored chart of accounts, or the vendor list.

    Read from the local mirror rather than QuickBooks. The mirror is what every
    suggestion and rule actually resolves against, so showing QuickBooks
    directly would display something the engine is not using -- and the gap
    between the two is exactly what somebody comes to this screen to find.
    `synced_at` says how old the answer is.
    """
    if company == ALL_COMPANIES:
        # Every reachable company's list, each row tagged with its company.
        _rows, _newest = [], ""
        for _co in _reachable(db, user, companies):
            _r = list_qbo_refs(kind=kind, q=q, limit=limit,
                               include_inactive=include_inactive, company=_co,
                               companies="", db=db, user=user)
            for _x in _r.get("rows") or []:
                _x["company"] = _co
                _rows.append(_x)
            _newest = max(_newest, _r.get("synced_at") or "")
        return {"kind": kind, "count": len(_rows), "synced_at": _newest, "rows": _rows}
    from .models_db import QboRefRow
    kind = (kind or "").strip().lower()
    if kind not in ("account", "vendor"):
        raise HTTPException(400, "kind must be 'account' or 'vendor'")
    # Inactive references are mirrored but never offered: a charge already
    # coded to a retired account still needs its name to resolve, and nothing
    # new should be coded to it.
    qy = db.query(QboRefRow).filter(
        QboRefRow.kind == kind,
        # ONE COMPANY'S REFERENCES. This screen had no realm filter at all, so
        # the Chart of Accounts and Vendors tabs showed every connected
        # QuickBooks file's records interleaved -- and a company mapped to none
        # showed all of them. An unresolved realm matches nothing on purpose:
        # "not connected yet" is true and fixable, "here are your accounts" is
        # a lie about another company's ledger.
        _eq_or_none(QboRefRow.realm_id, _current_realm(db, company)))
    if not include_inactive:
        qy = qy.filter(QboRefRow.active.isnot(False))
    if kind == "account":
        # Bank and credit card accounts live on the Card Accounts tab, where
        # they are mapped to card programs. Listing them here too would show
        # the same account under two headings with different meanings -- one a
        # category to code to, the other a feed to map.
        qy = qy.filter(QboRefRow.usable_for != "bank")
    if q.strip():
        qy = qy.filter(QboRefRow.name.ilike(f"%{q.strip()}%"))
    # Ordered as the category picker orders it: account type first, then name.
    # A chart of accounts sorted purely alphabetically interleaves expenses
    # with assets and reads as a jumble to anyone who knows the chart.
    # Capped at 50,000, not 2,000: the Vendors grid asked for its whole list
    # and got the first 2,000 alphabetically, so it stopped partway through
    # the T's with nothing to say the rest existed.
    rows = qy.order_by(QboRefRow.account_type, QboRefRow.name).limit(
        min(int(limit), 50000)).all()
    # WHICH ROWS ARE NUMBERED NEAR-DUPLICATES of another row here.
    #
    # Not collapsed on this screen, deliberately. "Office Supplies (1)" exists
    # in QuickBooks and may hold transactions; hiding it from the one screen
    # somebody opens to CHECK the chart of accounts would conceal exactly what
    # they came to find. It is badged instead, so the pairs are visible and can
    # be merged in QuickBooks. The coding picker is where they are collapsed --
    # that is a list of things to choose, and offering both is the harm.
    _dupmap = _duplicate_map(rows) if kind == "account" else {}
    # The server order is the DEFAULT the grid starts from; the grid sorts on
    # its own after that. Named here so both agree from the first render --
    # the grid was re-sorting by name alone and discarding the type grouping
    # the moment it drew.
    newest = max((r.synced_at for r in rows if r.synced_at), default=None)

    from sqlalchemy import func as _f
    from .models_db import ChargeRow as _CR, LearnedRuleRow as _LR
    col = _CR.coded_vendor if kind == "vendor" else _CR.coded_category
    rcol = _LR.vendor if kind == "vendor" else _LR.category

    # COUNTED ONLY FOR THE NAMES ON THIS PAGE.
    #
    # These were two unrestricted GROUP BYs over every charge in the database,
    # run on every load of this screen -- a full scan of a table with tens of
    # thousands of rows, grouping by an unindexed text column, to produce a
    # number shown against at most a few hundred accounts. Everything outside
    # `rows` was aggregated and then thrown away.
    #
    # Restricted to the page's own names, the same answer comes back from an
    # IN list the planner can work with. That is the whole of why this screen
    # had become slow to open.
    _names = [r.name for r in rows if r.name]
    _uses: dict = {}
    if _names:
        for name, n in (db.query(col, _f.count())
                        .filter(col.in_(_names)).group_by(col).all()):
            if name:
                _uses[name] = _uses.get(name, 0) + n
        for name, n in (db.query(rcol, _f.count())
                        .filter(rcol.in_(_names)).group_by(rcol).all()):
            if name:
                _uses[name] = _uses.get(name, 0) + n
    return {
        "kind": kind,
        "count": len(rows),
        "synced_at": (newest.isoformat() + "Z") if newest else "",
        # How many things still point at each row BY NAME. Two grouped queries
        # rather than one per row: a chart of accounts is hundreds of rows and
        # this renders on every visit.
        "rows": [{"qbo_id": r.qbo_id, "name": r.name,
                  "active": r.active is not False,
                  "used_by": _uses.get(r.name or "", 0),
                  "account_type": r.account_type or "",
                  "subtype": r.subtype or "",
                  "usable_for": r.usable_for or "",
                  # Badged on screen rather than hidden: an account that
                  # exists in QuickBooks and may hold transactions should not
                  # vanish from the screen somebody opens to CHECK the chart of
                  # accounts. The coding picker is where these collapse.
                  "dup_of": _dupmap.get(r.id, ""),
                  "synced_at": (r.synced_at.isoformat() + "Z")
                               if r.synced_at else ""}
                 for r in rows],
    }


@app.get("/export/refs/{kind}/{company}")
def export_refs(kind: str, company: str, q: str = Query(""),
                include_inactive: bool = Query(False),
                companies: str = Query(""),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    """The mirrored chart of accounts or vendor list, as a workbook.

    THE SAME ROWS THE SCREEN SHOWS. This exported every row in the mirror
    regardless of what the grid was displaying: inactive references the screen
    hides, bank accounts it excludes, and the search box ignored entirely. A
    spreadsheet that disagrees with the grid it was taken from is the one thing
    an export must not be, so it now takes the same arguments and applies the
    same rules.
    """
    if company == ALL_COMPANIES:
        return _multi_export(export_refs, dict(locals()), kind)
    from .export_xlsx import build_workbook, filename
    from .models_db import QboRefRow
    kind = (kind or "").strip().lower()
    if kind not in ("account", "vendor"):
        raise HTTPException(400, "kind must be 'account' or 'vendor'")
    qy = db.query(QboRefRow).filter(QboRefRow.kind == kind)
    if not include_inactive:
        qy = qy.filter(QboRefRow.active.isnot(False))
    if kind == "account":
        qy = qy.filter(QboRefRow.usable_for != "bank")
    if q.strip():
        qy = qy.filter(QboRefRow.name.ilike(f"%{q.strip()}%"))
    rows = qy.order_by(QboRefRow.account_type, QboRefRow.name).all()
    if kind == "account":
        # Base name first, so a numbered duplicate sits directly under its
        # original in the sheet exactly as it does on the screen.
        rows = sorted(rows, key=lambda r: (r.account_type or "",
                                           _base_name(r.name).lower(),
                                           r.name or ""))
    if kind == "account":
        # The DUPLICATE? badge as a column: a spreadsheet cannot carry a pill,
        # and the list of pairs to merge in QuickBooks is the main reason to
        # export this sheet at all.
        dup = _duplicate_map(rows)
        headers = ["Name", "Type", "Detail Type", "Used For", "QBO Id",
                   "Active", "Possible Duplicate Of", "Last Synced"]
        data = [[r.name, r.account_type or "", r.subtype or "",
                 r.usable_for or "", r.qbo_id,
                 "No" if r.active is False else "Yes",
                 dup.get(r.id, ""), r.synced_at]
                for r in rows]
        stamp = [7]
    else:
        headers = ["Name", "QBO Id", "Active", "Last Synced"]
        data = [[r.name, r.qbo_id, "No" if r.active is False else "Yes",
                 r.synced_at] for r in rows]
        stamp = [3]
    applied = [("Company", company), ("Tab", f"{kind.title()}s"),
               ("Search", q or "—"),
               ("Includes inactive", "Yes" if include_inactive else "No"),
               ("Exported by", user.username)]
    # A real datetime, so Last Synced carries the m/d/yyyy h:mm format the
    # other sheets use instead of arriving as text that sorts alphabetically.
    blob = build_workbook(f"{kind.title()}s", headers, data, applied,
                          stamp_cols=stamp)
    return Response(
        blob, media_type="application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename(kind, company)}"'})


@app.post("/qbo/refs/rename")
def rename_qbo_ref(kind: str = Form(...), qbo_id: str = Form(...),
                   name: str = Form(...), db: Session = Depends(get_session),
                   user: UserRow = Depends(require_write),
                   company: str = Form(""), confirm_merge: str = Form("")):
    """Rename an account or vendor in QuickBooks and everywhere here.

    The local rewrite is not a convenience. Rules, coded charges and card
    account mappings all store the NAME, so renaming in QuickBooks alone leaves
    them pointing at a string that no longer exists -- silently, because a name
    matching nothing simply matches nothing.
    """
    from .integrations.qbo_write import (rename_reference, WriteBlocked,
                                         WriteFailed)
    from .models_db import QboRefRow as _QR

    # RENAMING ONTO AN EXISTING NAME IS A MERGE, and QuickBooks performs it
    # without asking. Two accounts become one and the charges of both end up
    # under it -- correct, usually intended, and not something to discover
    # afterwards from a chart of accounts that is a row shorter.
    #
    # So it is refused once, with what would happen, and repeated with
    # confirm_merge set if that is what was meant. The check is here rather
    # than in the browser because the browser cannot see the other realms'
    # rows and this is the only place that knows which one is in play.
    realm = _current_realm(db, company)
    clash = (db.query(_QR)
             .filter(_QR.kind == kind, _QR.realm_id == realm,
                     _QR.name == name.strip(), _QR.qbo_id != qbo_id.strip(),
                     _QR.active.isnot(False)).first())
    if clash is not None and str(confirm_merge).lower() not in ("1", "true", "yes"):
        noun = "account" if kind == "account" else "vendor"
        article = "An" if noun == "account" else "A"
        raise HTTPException(409, {
            "merge": True,
            "detail": f"{article} {noun} called {name.strip()!r} already exists. "
                      f"Renaming this one onto it MERGES the two in "
                      f"QuickBooks: they become a single {noun} and every "
                      f"transaction coded to either ends up under it. This "
                      f"cannot be undone from here.",
        })
    try:
        return rename_reference(db, kind, qbo_id, name, user.username,
                                company=company)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))


@app.post("/charge/last4")
def set_charge_last4(charge_id: str = Form(...), last4: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_write)):
    """Correct the card last four on one charge.

    Some feeds carry no card number at all, and a charge with the wrong four
    resolves to the wrong cardholder -- which then drives the profile and the
    STH lookup. It is the one field on a charge worth being able to fix by hand,
    because everything downstream of it is inferred from it.

    Refused once the charge is in QuickBooks: the ledger entry was built from
    what this said, and changing it afterwards would leave the two disagreeing
    with nothing to show for it.
    """
    from .models_db import ChargeRow as _CR
    row = db.get(_CR, charge_id)
    if row is None:
        raise HTTPException(404, "no such charge")
    if row.qbo_txn_id:
        raise HTTPException(
            400, "this charge is already in QuickBooks — undo the posting first")
    v = "".join(ch for ch in (last4 or "") if ch.isdigit())[-4:]
    if v and len(v) != 4:
        raise HTTPException(400, "give four digits, or leave it blank")
    was = row.card_last4
    row.card_last4 = v
    db.add(row)
    db.add(AuditRow(company=row.company, action="charge_last4_set",
                    charge_id=charge_id, actor=user.username,
                    detail=f"changed from {was or '(blank)'} "
                           f"to {v or '(blank)'}"))
    db.commit()
    return {"charge_id": charge_id, "card_last4": v}


@app.get("/users/list")
def users_list(db: Session = Depends(get_session),
               user: UserRow = Depends(require_reviewer)):
    """Everyone who could have resolved a charge, for the User filter.

    The stored values are usernames plus the "system-" forms the engine writes
    when it codes something itself, so both are offered -- filtering to "what
    did the engine do on its own" is the common question and typing
    "system-rule" from memory is not a filter anybody uses.
    """
    from .models_db import ChargeRow as _CR
    people = sorted({u for (u,) in db.query(UserRow.username).all() if u})
    # ONE "system", not one per internal identity.
    #
    # The engine writes system-rule, system-payment and system-teams depending
    # on what coded the charge, and offering all three asks the reader to know
    # the engine's internals to answer "what did it do on its own". The
    # distinction is already in the User column, which spells out the rule; the
    # filter only needs the one bucket.
    has_system = bool(db.query(_CR).filter(
        _CR.resolved_by.ilike("system%")).first())
    return {"users": people, "system": ["system"] if has_system else []}


@app.post("/qbo/refs/type")
def set_qbo_ref_type(qbo_id: str = Form(...), account_type: str = Form(...),
                     company: str = Form(""),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_write)):
    """Change an account's type in QuickBooks.

    Whatever QuickBooks permits, this permits. An earlier version of this note
    asserted that QuickBooks refuses the change once an account has
    transactions; that was my assumption and not something I verified. The call
    is made and its answer passed through, so the rule -- whatever it actually
    is -- comes from QuickBooks rather than from a guess reimplemented here.

    A sub-account does have to match its parent's type; that one is documented.
    """
    from .integrations.qbo_write import (_sparse_update, _realm_for,
                                         assert_writes_allowed, WriteBlocked,
                                         WriteFailed)
    from .models_db import QboRefRow
    from .review import _company_realm as _crs
    _rs = _crs(db, company) if company else ""
    _sq = db.query(QboRefRow).filter(
        QboRefRow.kind == "account", QboRefRow.qbo_id == qbo_id.strip())
    # This company's file: an id alone can match another company's account.
    row = (_sq.filter(QboRefRow.realm_id == _rs) if _rs else _sq).first()
    if row is None:
        raise HTTPException(404, "no such account is mirrored here")
    if (row.account_type or "") == account_type.strip():
        return {"changed": False, "reason": "the type is unchanged"}
    try:
        assert_writes_allowed()
        _sparse_update(db, _realm_for(db, company), "account", qbo_id.strip(),
                       {"Name": row.name or "",
                        "AccountType": account_type.strip()})
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        # QuickBooks says why -- usually that the account has transactions.
        raise HTTPException(400, str(e))
    was = row.account_type
    row.account_type = account_type.strip()
    row.synced_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company="", action="qbo_account_type", actor=user.username,
                    detail=f"{row.name}: type changed from {was} "
                           f"to {account_type}"))
    db.commit()
    from .review import reset_account_cache
    reset_account_cache()
    return {"changed": True, "name": row.name, "account_type": row.account_type}


@app.post("/qbo/refs/active")
def set_qbo_ref_active(kind: str = Form(...), qbo_id: str = Form(...),
                       active: str = Form("false"),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_write),
                       company: str = Form("")):
    """Retire or restore an account or vendor in QuickBooks.

    QuickBooks deactivates rather than deletes, and an object with history
    cannot be removed at all -- so this is the only "delete" there is, and it
    is reversible. Nothing local is renamed: a retired account keeps its name
    so charges already coded to it still resolve.
    """
    from .integrations.qbo_write import (set_reference_active, WriteBlocked,
                                         WriteFailed)
    want = str(active).strip().lower() in ("true", "1", "yes")
    try:
        return set_reference_active(db, kind, qbo_id, want, user.username,
                                    company=company)
    except WriteBlocked as e:
        raise HTTPException(400, f"Writing to QuickBooks is turned off ({e}).")
    except WriteFailed as e:
        raise HTTPException(400, str(e))


@app.post("/qbo/accounts/dedupe")
def dedupe_qbo_accounts(company: str = Query(""),
                        dry_run: bool = Query(True),
                        db: Session = Depends(get_session),
                        user: UserRow = Depends(require_admin)):
    """Fold numbered duplicate accounts back into their originals.

    Admin only, and a dry run unless asked otherwise: this writes to the chart
    of accounts, and the first answer to "what will this do" should not be
    "it already did it".
    """
    from .integrations.qbo_write import (merge_numbered_duplicates, WriteFailed,
                                         WriteBlocked)
    try:
        return merge_numbered_duplicates(db, user.username, dry_run=dry_run,
                                         company=company)
    except (WriteFailed, WriteBlocked) as e:
        db.rollback()
        raise HTTPException(400, str(e))


@app.get("/users/overview")
def users_overview(db: Session = Depends(get_session),
                   user: UserRow = Depends(require_admin)):
    """Every account, with the companies each one reaches.

    One call rather than a list of users and then a membership lookup per row:
    the screen shows both together, and fetching them separately means a grid
    that fills in a column at a time.
    """
    from .models_db import UserCompanyRow
    from . import membership as _mb
    by_user: dict = {}
    for r in db.query(UserCompanyRow).all():
        by_user.setdefault(r.username, []).append({"company": r.company,
                                                   "role": r.role})
    mine = _mb.companies_for(db, user)
    _assigned = _roles.assignments(db)
    rows = []
    for u in db.query(UserRow).order_by(UserRow.username).all():
        cos = sorted(by_user.get(u.username, []), key=lambda c: c["company"])
        # An admin who is not an owner sees only the companies they share with
        # this person -- listing the rest would tell them about clients they
        # have no access to.
        if mine is not None:
            cos = [c for c in cos if c["company"] in set(mine)]
        rows.append({
            "username": u.username, "email": u.email or "",
            "full_name": u.full_name or "", "role": u.role or "",
            "custom_role": _assigned.get(u.username, ""),
            "active": bool(u.is_active), "twofa": bool(u.twofa_enabled),
            "last_login": (u.last_login.isoformat() + "Z") if u.last_login else "",
            "companies": cos,
            # An owner reaches everything without any rows, so saying "no
            # companies" about one would be false.
            "all_companies": (u.role or "") == "owner",
        })
    return {"users": rows}


@app.post("/users/active")
def set_user_active(username: str = Form(...), active: str = Form("true"),
                    db: Session = Depends(get_session),
                    admin: UserRow = Depends(require_admin)):
    """Turn an account off, or back on.

    DEACTIVATED, not deleted. A user is named on every audit row they ever
    wrote, and removing the account would leave those rows attributed to
    somebody who, as far as the database is concerned, never existed. Turning
    it off stops the sign-in, which is the thing actually being asked for.
    """
    target = db.get(UserRow, username)
    if target is None:
        raise HTTPException(404, f"{username!r} is not a user of this engine")
    want = str(active).lower() in ("1", "true", "yes", "on")
    if not want and target.username == admin.username:
        raise HTTPException(400, "you cannot deactivate your own account")
    if not want and (target.role or "") == "owner" and db.query(UserRow).filter(
            UserRow.role == "owner", UserRow.is_active.is_(True)).count() <= 1:
        # The last owner is the only account that can reach every company and
        # reactivate anybody. Turning it off is not recoverable from inside the
        # application.
        raise HTTPException(400, "this is the last active owner")
    target.is_active = want
    db.add(target)
    db.add(AuditRow(company="", action="user_active_set", actor=admin.username,
                    dry_run=False,
                    detail=f"{username} {'reactivated' if want else 'deactivated'}"))
    db.commit()
    return {"ok": True, "active": want}


# --- User Management > Settings and Roles -----------------------------------

@app.get("/users/settings")
def users_settings(db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """User Management > Settings."""
    from .config import settings as _st
    return {"idle_minutes": _roles.idle_minutes(db), "default_idle_minutes": _st.session_idle_minutes}


@app.post("/users/settings")
def save_users_settings(idle_minutes: str = Form(...), db: Session = Depends(get_session),
                        user: UserRow = Depends(require_admin)):
    try:
        n = int(str(idle_minutes).strip())
    except ValueError:
        raise HTTPException(400, "Minutes must be a whole number")
    if n < 5 or n > 720:
        raise HTTPException(400, "Minutes must be between 5 and 720")
    n = _roles.set_idle_minutes(db, n)
    db.add(AuditRow(company="", action="setting_saved", actor=user.username, dry_run=False,
                    detail=f"Sign out after {n} minutes of inactivity"))
    db.commit()
    return {"idle_minutes": n}


@app.get("/users/roles")
def users_roles(db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """User Management > Roles: the built-in roles, the roles added, what each
    can do, and who has an added role."""
    return {"abilities": [[k, l] for k, l, _p in _roles.ABILITIES],
            "builtin": [[k, l] for k, l in _roles.BUILTIN],
            "bases": [[k, l] for k, l in _roles.BASES.items()],
            "roles": _roles.custom_roles(db), "assigned": _roles.assignments(db)}


@app.post("/users/roles")
def save_users_roles(roles: str = Form(...), db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    import json as _jr
    try:
        data = _jr.loads(roles)
        assert isinstance(data, list)
    except Exception:                                   # noqa: BLE001
        raise HTTPException(400, "roles must be a JSON list")
    try:
        clean = _roles.save_roles(db, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(AuditRow(company="", action="setting_saved", actor=user.username, dry_run=False,
                    detail=("Roles saved: " + ", ".join(
                        f"{r['name']} ({_roles.BASES[r['base']]}"
                        + (f"; no {', '.join(_roles.label(d) for d in r['deny'])}" if r['deny'] else "") + ")"
                        for r in clean))[:1000] if clean else "Roles saved: none added"))
    db.commit()
    return users_roles(db=db, user=user)


@app.post("/users/role")
def set_user_role(username: str = Form(...), role: str = Form(...),
                  db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """Give a person a role: Admin, Reviewer, Viewer or one added on Roles."""
    try:
        base = _roles.assign(db, username, role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(AuditRow(company="", action="user_role_set", actor=user.username, dry_run=False,
                    detail=f"{username}: {_roles.BASES.get(role, role)}"))
    db.commit()
    return {"ok": True, "role": base, "custom": _roles.assignments(db).get(username, "")}


@app.post("/teams/take-ownership")
def take_team_ownership(db: Session = Depends(get_session),
                        user: UserRow = Depends(require_admin)):
    """Mirror what Airtable has now, then stop syncing from it.

    ONE WAY, on purpose. After this the Teams table is edited here and the
    hourly sync leaves it alone -- which it must, because that sync deletes
    every row and re-inserts it, and would wipe an edit somewhere between two
    page loads.

    Running it again re-imports and stays owned; it is not a toggle back. To go
    back you would clear `locked`, and that is deliberately not a button: it
    means discarding whatever has been edited here since, and nothing on a
    screen should make that look routine.
    """
    from .models_db import TeamAliasRow
    from .worker import sync_team_aliases

    before = db.query(TeamAliasRow).count()
    locked = db.query(TeamAliasRow).filter(TeamAliasRow.locked.is_(True)).count()
    if not locked:
        # Nothing owned yet: pull a fresh copy first, so the snapshot taken
        # over is today's rather than whenever the last sync happened to run.
        try:
            sync_team_aliases()
        except Exception as e:                      # noqa: BLE001
            raise HTTPException(
                502, f"could not read Airtable: {type(e).__name__}: {e}")

    n = db.query(TeamAliasRow).update({TeamAliasRow.locked: True},
                                      synchronize_session=False)
    db.add(AuditRow(company="", action="teams_owned", actor=user.username,
                    dry_run=False,
                    detail=f"{n} team rows imported from Airtable and taken "
                           f"over; Airtable sync stopped"))
    db.commit()
    return {"ok": True, "rows": n, "was": before}


@app.post("/teams/plans/seed")
def seed_payment_plans(company: str = Form(""),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_write)):
    """Create a plan row for every team that has none, from what Airtable said.

    The old arrangement recorded a charge day and a COUNT of months, which is
    an even split over the first N months and nothing more -- so that is what
    this writes. It is a starting point to correct, not a translation: a team
    whose real plan is 40/30/30 will come through as three equal thirds and
    has to be edited.

    Written that way rather than left blank because blank means "no plan", and
    a blank table on the day this ships would stop the installment check for
    every team at once -- a silent loss of a check people rely on.
    """
    from datetime import datetime as _dt
    from .models_db import PaymentPlanRow, TeamAliasRow
    seen: dict = {}
    for r in db.query(TeamAliasRow).all():
        if r.team and r.team not in seen:
            seen[r.team] = r
    made = 0
    for team, r in seen.items():
        kid = f"|{team}"
        if (db.get(PaymentPlanRow, kid) is not None
                or db.query(PaymentPlanRow)
                     .filter(PaymentPlanRow.team == team).first() is not None):
            continue                        # never overwrite an edited plan
        n = int(r.plan_months or 0)
        months = [""] * 12
        if n > 0:
            # An even split to two decimals, with the rounding put on the LAST
            # month so the twelve add to exactly 100 -- 100/3 three times is
            # 99.99, which the save would then refuse.
            each = round(100.0 / n, 2)
            for i in range(n):
                months[i] = f"{each:g}"
            months[n - 1] = f"{round(100.0 - each * (n - 1), 2):g}"
        days = str(r.day_text or r.day_of_month or "").strip()
        db.add(PaymentPlanRow(
            id=kid, company="", team=team, league=r.league or "",
            days=days, months=",".join(months),
            note="from the Airtable plan — an even split, correct it if it is not",
            updated_by=user.username, updated_at=_dt.utcnow()))
        made += 1
    if made:
        db.add(AuditRow(company=company, action="payment_plans_seeded",
                        actor=user.username, dry_run=False,
                        detail=f"{made} plan(s) created from the Airtable "
                               f"month counts"))
        db.commit()
    return {"ok": True, "created": made, "teams": len(seen)}


_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _describe_months(csv: str) -> str:
    """'Jan 10%, Feb 10%, ...' from the stored twelve comma-separated shares.

    Collapses an even split to 'Jan-Oct, 10% each', which is how a plan is
    actually thought of and keeps the entry short enough to read.
    """
    vals = [(v or "").strip() for v in (csv or "").split(",")]
    vals = (vals + [""] * 12)[:12]
    on = [(i, v) for i, v in enumerate(vals) if v and float(v) > 0]
    if not on:
        return "none"
    idx = [i for i, _ in on]
    run = idx == list(range(idx[0], idx[0] + len(idx)))
    first = round(float(on[0][1]), 2)
    # EVEN means every month but the last is exactly the same share, and the
    # last differs only by rounding (it carries the leftover so the plan totals
    # 100). Allowing any two values let 40/20/20/20 read as "40% each".
    body_even = all(round(float(v), 2) == first for _, v in on[:-1])
    last_ok = abs(float(on[-1][1]) - first) <= 0.05
    if run and len(on) > 1 and body_even and last_ok:
        return (f"{_MONTH_NAMES[idx[0]]}-{_MONTH_NAMES[idx[-1]]}, "
                f"{first:g}% each")
    return ", ".join(f"{_MONTH_NAMES[i]} {float(v):g}%" for i, v in on)


def _plan_changes(m0, m1, d0, d1, y0, y1) -> list:
    """Each field of a payment plan that changed, as a readable phrase."""
    out = []
    if (m0 or "").strip(",") != (m1 or "").strip(","):
        out.append(f"months {_describe_months(m0)} \u2192 "
                   f"{_describe_months(m1)}")
    if (d0 or "") != (d1 or ""):
        out.append(f"charge days {d0 or 'none'} \u2192 {d1 or 'none'}")
    if (y0 or "") != (y1 or ""):
        out.append(f"renewal years {(y0 or 'none').replace(',', ', ')} "
                   f"\u2192 {(y1 or 'none').replace(',', ', ')}")
    return out


@app.post("/teams/plans/delete")
def delete_payment_plan(team: str = Form(...), slot: str = Form(...),
                        company: str = Form(""), own: str = Form(""),
                        db: Session = Depends(get_session),
                        user: UserRow = Depends(require_write)):
    """Remove ONE of a team's plans."""
    try:
        n = int(str(slot).strip() or 0)
    except ValueError:
        n = 0
    # SLOT 1 IS NOT DELETABLE. Every team has a first plan -- an empty one is
    # how you say "no plan yet" -- so removing it would leave the team with no
    # row to edit and no way to get one back. Clearing it is what Reset is for.
    if n < 2:
        raise HTTPException(400, "The first plan cannot be removed. Use Reset "
                                 "to clear it instead.")
    # Imported here, like every other handler in this file: the model is not a
    # module-level name and assuming it was is what made this 500 rather than
    # answer.
    from .models_db import PaymentPlanRow
    mine = company if (str(own).lower() in ("1", "true", "yes") and company) else ""
    row = db.get(PaymentPlanRow, f"own:{mine}|{team}|{n}" if mine else f"|{team}|{n}")
    if row is None and mine:
        row = db.get(PaymentPlanRow, f"{mine}|{team}|{n}")
    if row is None and not mine:
        row = (db.query(PaymentPlanRow)
               .filter(PaymentPlanRow.team == team, ~PaymentPlanRow.id.like("own:%"),
                       PaymentPlanRow.slot == n).first())
    if row is None:
        raise HTTPException(404, "No such plan")
    db.delete(row)
    db.add(AuditRow(company="", action="payment_plan_deleted",
                    detail=f"{team}: Plan {n} removed "
                           f"({_describe_months(row.months)})",
                    dry_run=False,
                    actor=user.username))
    db.commit()
    return {"team": team, "slot": n, "deleted": True}


@app.get("/teams/plans/export")
def export_payment_plans(company: str = Query(""), own: str = Query(""),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_reviewer)):
    """The Payment Plans grid, column for column."""
    from datetime import datetime as _dt
    from . import export_xlsx as X
    data = list_payment_plans(company=company, own=own, db=db, user=user)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    # THE SAME COLUMNS AS THE GRID, in the same order. The export had neither
    # the renewal years nor the month count, so the file and the screen
    # disagreed about what a plan is -- and the file is what gets sent on.
    headers = (["Team", "League", "Renewal Years", "Charged on"] + months
               + ["Total", "# Months"])
    rows = []
    for p in data["plans"]:
        n_months = sum(1 for m in p["months"]
                       if str(m).strip() and float(m or 0) > 0)
        rows.append([p["team"], p["league"],
                     (p.get("seasons") or "").replace(",", ", "), p["days"]]
                    + [float(m) if str(m).strip() else None for m in p["months"]]
                    + [p["total"] or None, n_months or None])
    # The fourth argument is the APPLIED-FILTERS list, pairs of (label, value)
    # printed above the table -- not a subtitle. Passing a string made the
    # builder iterate its characters.
    blob = X.build_workbook("Payment Plans", headers, rows,
                            [("Company", company)],
                            money_cols=[])
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        # Through the shared helper, like every other export. Built by hand,
        # this one came out "payment-plans 9-15-2026 2128.xlsx" while the rest
        # said "Engine - Company - Grid - date", which is the inconsistency in
        # your downloads folder.
        headers={"Content-Disposition":
                 f'attachment; filename="{X.filename("plans", company)}"'})


def _default_start(months: list) -> int:
    """Which month a plan starts, read from the months themselves.

    A season that runs November to June has an obvious start: the first month
    after the gap. One that runs all twelve has no gap and therefore no obvious
    answer, so the stored choice is used and January is only the fallback.

    Returned as a month number, 1-12.
    """
    live = [i for i, v in enumerate(months) if str(v).strip()
            and float(v or 0) > 0]
    if not live:
        return 1
    if len(live) == 12:
        return 1                       # no gap to read; the user chooses
    # The first month whose predecessor (wrapping December to January) is not
    # itself charged. That is the far side of the break.
    for i in live:
        if (i - 1) % 12 not in live:
            return i + 1
    return live[0] + 1


@app.get("/teams/plans")
def list_payment_plans(company: str = Query(""), own: str = Query(""),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_reviewer)):
    from .canada import looks_canadian as _lc_p
    _ca_seen: dict = {}

    def _ca_team(team):
        if team not in _ca_seen:
            _ca_seen[team] = bool(_lc_p(team, db=db))
        return _ca_seen[team]
    """Every team, with its payment plan if it has one.

    Teams without a plan are listed too, with empty months -- a plan screen
    that only showed teams already set up would hide exactly the ones that
    still need doing.
    """
    from .models_db import PaymentPlanRow, TeamAliasRow
    # A PLAN BELONGS TO THE TEAM, not to a company.
    #
    # Teams are installation-wide -- team_aliases has no company column -- so
    # keying their plans by company was a mismatch I introduced: it put the
    # Payment Plans screen on the installation-wide Algorithms navigation while
    # requiring a company that is not selected there, and saving failed with
    # "company field required".
    #
    # Rows written under a company before this are still read, so nothing
    # already set up is lost; new ones are written without one.
    # EVERY plan a team has, not just the first. A team may run a six-month and
    # a ten-month schedule for the same seats; keeping only one meant the grid
    # showed one and the matcher could only ever see one.
    plans: dict = {}
    for p in db.query(PaymentPlanRow).all():
        if (p.id or "").startswith("own:"):
            continue                      # a company's own plan, on its own grid
        if p.company in ("", company) or not company:
            plans.setdefault(p.team, []).append(p)
    for _lst in plans.values():
        _lst.sort(key=lambda r: (r.slot or 1))
    teams: dict = {}
    # THE SAME TEAMS THE TEAMS TAB SHOWS.
    #
    # Teams hidden there -- the accounting-base rows excluded from that grid --
    # were still listed here, so Payment Plans carried teams that do not exist
    # as far as the rest of the app is concerned and there was no way to get
    # rid of them from this screen.
    #
    # Read from the same setting rather than a copy of the list: two places
    # deciding which teams are real is how they came to disagree.
    from .models_db import AppSettingRow as _AS2
    _h2 = db.get(_AS2, "team_hidden")
    try:
        _hidden2 = {str(t).strip().lower()
                    for t in json.loads((_h2.value if _h2 else "") or "[]")}
    except Exception:                                   # noqa: BLE001
        _hidden2 = set()
    for r in db.query(TeamAliasRow).all():
        # BOTH of the Teams grid's filters, not just one.
        #
        # Hidden teams were skipped here already; never-rows -- the promoter
        # rows, Live Nation venues and the like -- were not, which is the whole
        # of "Payment Plans shows far more teams than Teams". One filter each
        # is how two lists of the same thing drift apart.
        if (r.team or "").strip().lower() in _hidden2:
            continue
        if is_never_row(r.team, r.alias, r.league):
            continue
        teams.setdefault(r.team, r.league or "")
    # A COMPANY'S OWN PAYMENT PLANS (Configuration > Payment Plans): every
    # team on the company's Teams tab -- its own teams AND its overrides of
    # Dashboard teams -- each with the plan saved for that company. An
    # override left blank uses the Dashboard's plan (marked so on the grid).
    _own_mode = str(own).lower() in ("1", "true", "yes") and bool(company)
    _dash_names: set = set()
    if _own_mode:
        from . import company_teams as _ctp
        _dash_names = {(r.team or "").strip().lower() for r in db.query(TeamAliasRow.team).all()}
        teams = {t["team"]: t.get("league") or "" for t in _ctp.teams(db, company)}
        plans = {}
        for p in db.query(PaymentPlanRow).filter(PaymentPlanRow.company == company).all():
            if (p.id or "").startswith(f"own:{company}|") or p.team.strip().lower() not in _dash_names:
                plans.setdefault(p.team, []).append(p)
        for _lst in plans.values():
            _lst.sort(key=lambda r: (r.slot or 1))
    # SORTED LIKE THE TEAMS GRID: league tier, then league, then team. Two
    # screens listing the same teams in two different orders means finding a
    # team twice, once per screen.
    _lo = [str(x).strip().upper() for x in _league_order(db)]

    def _tier(lg: str) -> int:
        """The same ranking LEAGUE_TIER uses in app.js.

        Copied rather than approximated: two orderings that are nearly the same
        are worse than two that are obviously different, because the difference
        only shows up on the few teams that fall between them.
        """
        lg = str(lg or "").strip().upper()
        # The order chosen in the Leagues window first, as the grid shows it.
        if _lo:
            if lg in _lo:
                return _lo.index(lg)
            return len(_lo) + _builtin(lg)
        return _builtin(lg)

    def _builtin(lg: str) -> int:
        if lg in ("MLB", "NBA", "NFL", "NHL", "MLS"):
            return 0
        if lg.startswith("NCAA"):
            return 1
        if lg == "WNBA":
            return 2
        if lg.startswith("BROADWAY"):
            return 3
        if "FOREST HILLS" in lg:
            return 4
        if "LIVE NATION" in lg:
            return 5
        return 6 if lg else 7

    out = []
    for team in sorted(teams, key=lambda t: (_tier(teams[t]),
                                             (teams[t] or "").lower(),
                                             t.lower())):
        # ONE ROW PER PLAN. A team with no plan still gets a row -- that is how
        # you give it one -- and a team with two gets two, in slot order.
        for p in (plans.get(team) or [None]):
            months = [x.strip() for x in (p.months or "").split(",")] if p else []
            months = (months + [""] * 12)[:12]
            out.append({
                "team": team, "league": teams[team],
                "canadian": _ca_team(team),
                # Which of this team's plans the row is. The grid shows it only
                # where a team has more than one, since "plan 1 of 1" is noise.
                "slot": (p.slot or 1) if p else 1,
                "days": p.days if p else "",
                "months": months,
                "total": round(sum(float(m) for m in months if m.strip()), 4),
                "note": p.note if p else "",
                # NOT derived. A season charged January to October can renew in
                # December, and December may carry the first charge rather than
                # January -- so the first month of the plan is a guess, and a
                # guess here is indistinguishable from a setting somebody
                # chose. 0 means "not set", which is the truth until it is.
                "start_month": (p.start_month if p and p.start_month else 0),
                "seasons": (p.seasons or "") if p else "",
                "updated_by": p.updated_by if p else "",
                # AN OVERRIDE WITH NO PLAN OF ITS OWN uses the Dashboard's.
                "override": _own_mode and team.strip().lower() in _dash_names,
                "uses_dashboard": (_own_mode and team.strip().lower() in _dash_names
                                   and not any((x.months or "").replace(",", "").strip() or x.days
                                               for x in (plans.get(team) or []))),
            })
    return {"company": company, "plans": out,
            # The years the picker offers. Sent with the rows so the grid does
            # not need a second call to know what is choosable.
            "years": season_years(db)}


@app.post("/teams/plans")
def save_payment_plan(company: str = Form(""), team: str = Form(...),
                      league: str = Form(""), days: str = Form(""),
                      months: str = Form(""), note: str = Form(""),
                      start_month: str = Form(""), seasons: str = Form(""),
                      slot: str = Form("1"),
                      set_seasons: str = Form(""), own: str = Form(""),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_write)):
    """Save one team's plan.

    The months must total 100, and that is checked HERE rather than only in the
    browser: a plan that charges 90% of a season is a plan that quietly
    under-bills every customer on it, and the cost of catching it late is a
    year of corrections.

    An entirely empty row is allowed and means "no plan", which is different
    from a plan that adds up to nothing.
    """
    from datetime import datetime as _dt
    from .models_db import PaymentPlanRow
    parts = [x.strip() for x in (months or "").split(",")]
    parts = (parts + [""] * 12)[:12]
    if any(p for p in parts):
        try:
            total = sum(float(p) for p in parts if p)
        except ValueError:
            raise HTTPException(400, "every month must be a number or blank")
        # A hundredth of a point of slack: percentages that come from dividing
        # a season by 7 do not land on exact hundredths, and refusing 99.999
        # would make a correct plan unsaveable.
        if abs(total - 100.0) > 0.01:
            raise HTTPException(
                400, f"the months add up to {total:g}%, not 100%")

    # Keyed by team AND slot. A team may run a six-month and a ten-month
    # schedule for the same seats, so "the plan for this team" is no longer a
    # single thing -- and keying on the team alone meant saving the second one
    # overwrote the first.
    #
    # Slot 1 keeps the old bare key, so every row written before this is found
    # and updated in place rather than being shadowed by a new one.
    try:
        n_slot = int(str(slot or 1).strip() or 1)
    except ValueError:
        n_slot = 1
    n_slot = max(1, min(n_slot, 9))
    # A COMPANY'S OWN TEAM's plan is that company's, keyed with it: two
    # companies may each add a team of the same name.
    mine = company if (str(own).lower() in ("1", "true", "yes") and company) else ""
    # A COMPANY'S OWN PLAN is marked "own:" -- for one of its own teams, or
    # an override of a Dashboard team's plan -- so it never shows on the
    # Dashboard and wins over the Dashboard's plan for that company.
    kid = (f"own:{mine}|{team}" if mine else f"|{team}") + ("" if n_slot == 1 else f"|{n_slot}")
    row = db.get(PaymentPlanRow, kid)
    if row is None and mine:
        # Saved before the marker (own teams only): updated in place.
        row = db.get(PaymentPlanRow, f"{mine}|{team}" + ("" if n_slot == 1 else f"|{n_slot}"))
    if row is None and n_slot == 1 and not mine:
        row = (db.query(PaymentPlanRow)
               .filter(PaymentPlanRow.team == team, ~PaymentPlanRow.id.like("own:%"),
                       PaymentPlanRow.slot == 1).first())
    if row is None:
        row = PaymentPlanRow(id=kid, company=mine, team=team, slot=n_slot)
    row.slot = n_slot
    was = row.months or ""
    # Every field as it stood, so the audit can say what actually changed.
    was_days = row.days or ""
    was_seasons = row.seasons or ""
    row.league = league.strip()
    row.days = days.strip()
    row.months = ",".join(parts)
    row.note = note.strip()
    # Only when the caller says it sent the field. An empty string means "no
    # renewal years", which is a real answer and different from "not sent" --
    # the same distinction the alias editor needed.
    if set_seasons:
        row.seasons = ",".join(
            x.strip() for x in (seasons or "").split(",") if x.strip())
    try:
        m = int(start_month or 0)
        row.start_month = m if 1 <= m <= 12 else 0
    except ValueError:
        row.start_month = 0
    row.updated_by = user.username
    row.updated_at = _dt.utcnow()
    db.add(row)
    # SAID IN WORDS: which team, which plan, and each field that moved. The
    # old entry was the months as raw comma-separated numbers and nothing else,
    # so a charge-day or renewal-year change looked like no change at all.
    what = _plan_changes(was, row.months, was_days, row.days,
                         was_seasons, row.seasons)
    label = team + (f" (Plan {n_slot})" if n_slot > 1 else "")
    db.add(AuditRow(company=company, action="payment_plan_saved",
                    actor=user.username, dry_run=False,
                    detail=f"{label}: " + ("; ".join(what) if what
                                           else "saved with no changes")))
    db.commit()
    return {"ok": True}


@app.get("/setup/members")
def list_members(company: str = Query(...),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Who may work in this company, and as what."""
    from .models_db import UserCompanyRow
    from . import membership as _mb
    rows = (db.query(UserCompanyRow)
            .filter(UserCompanyRow.company == company).all())
    users = {u.username: u for u in db.query(UserRow).all()}
    return {
        "company": company,
        # Whether the person reading may change this list. An owner always may;
        # a company admin may for their own company and no other.
        "may_manage": _mb.role_in(db, user, company) in ("owner", "admin"),
        "members": sorted([{
            "username": r.username,
            "full_name": (users[r.username].full_name
                          if r.username in users else ""),
            "email": users[r.username].email if r.username in users else "",
            "role": r.role, "invited_by": r.invited_by or "",
            "active": (users[r.username].is_active
                       if r.username in users else False),
        } for r in rows], key=lambda m: m["username"].lower()),
        # Everyone who could be added. Owners are absent because they already
        # reach every company -- offering to "add" one would imply it does
        # something, and removing them again would imply it took it away.
        "candidates": sorted(
            u.username for u in users.values()
            if u.is_active and (u.role or "") != "owner"
            and not db.get(UserCompanyRow, f"{u.username}|{company}")),
    }


@app.post("/setup/members")
def save_member(company: str = Form(...), username: str = Form(...),
                role: str = Form("user"), remove: str = Form("false"),
                db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    """Add, change or remove one person's access to this company.

    NOT require_admin. The point of this screen is that a company's own admin
    manages their own people without an owner in the loop -- so the guard is
    the role held IN THIS COMPANY, which the middleware has already confirmed
    the caller is a member of.
    """
    from datetime import datetime as _dt
    from .models_db import UserCompanyRow
    from . import membership as _mb
    if _mb.role_in(db, user, company) not in ("owner", "admin"):
        raise HTTPException(403, "only an admin of this company can do that")
    if role not in ("admin", "user", "viewer"):
        raise HTTPException(400, "role must be admin, user or viewer")

    target = db.get(UserRow, username)
    if target is None:
        raise HTTPException(404, f"{username!r} is not a user of this engine")
    if (target.role or "") == "owner":
        raise HTTPException(
            400, "owners already reach every company; there is nothing to add")

    kid = f"{username}|{company}"
    row = db.get(UserCompanyRow, kid)
    if str(remove).lower() in ("1", "true", "yes"):
        if row is None:
            raise HTTPException(404, "that person is not in this company")
        # The LAST admin cannot be removed. A company whose only admin has been
        # taken out of it has nobody who can put one back, and recovering from
        # that needs an owner and a support conversation.
        if row.role == "admin" and db.query(UserCompanyRow).filter(
                UserCompanyRow.company == company,
                UserCompanyRow.role == "admin").count() <= 1:
            raise HTTPException(
                400, "this is the last admin of this company — make somebody "
                     "else an admin first")
        db.delete(row)
        db.add(AuditRow(company=company, action="member_removed",
                        actor=user.username, dry_run=False,
                        detail=f"{username} removed"))
        db.commit()
        return {"ok": True, "removed": True}

    was = row.role if row else ""
    if row is None:
        row = UserCompanyRow(id=kid, username=username, company=company,
                             invited_by=user.username, created_at=_dt.utcnow())
    elif was == "admin" and role != "admin" and db.query(UserCompanyRow).filter(
            UserCompanyRow.company == company,
            UserCompanyRow.role == "admin").count() <= 1:
        raise HTTPException(
            400, "this is the last admin of this company — make somebody else "
                 "an admin first")
    row.role = role
    db.add(row)
    db.add(AuditRow(
        company=company, action="member_added" if not was else "member_changed",
        actor=user.username, dry_run=False,
        detail=f"{username} as {role}" + (f", was {was}" if was else "")))
    db.commit()
    return {"ok": True}


def _conn_key_hint(stored: str) -> str:
    """A connection key as the screen shows it: bullets and its last four."""
    from .secrets_store import decrypt as _d, mask as _m
    if not stored:
        return ""
    plain = _d(stored)
    return _m(plain) if plain else "cannot be read \u2014 re-enter"


def _api_details(db, company: str) -> dict:
    """card source -> how its API connection reads, for Bank / Card Accounts:
    Divvy and Taekus by the description given on Connectors (Taekus with its
    username), Slash by its entity and program names as last seen."""
    import json as _jd
    from .models_db import CardConnectionRow as _CCd2, AppSettingRow as _ASd
    from . import divvy_sync as _dvd, taekus_sync as _tkd, gr_sync as _grd
    out = {}
    for prog, mod in (("divvy", _dvd), ("taekus", _tkd), ("globalrewards", _grd)):
        for c in mod.connections(db, company):
            src, kv = c.get("source"), c.get("key_var")
            if not src:
                continue
            row = db.query(_CCd2).filter(_CCd2.program == prog, _CCd2.key_var == kv).first()
            lab = (row.label if row and row.label else "") or kv or ""
            if prog == "taekus" and c.get("username"):
                lab = f"{lab} as {c['username']}"
            out[src] = lab
    prefix = f"api_label:{company}:"
    for r in db.query(_ASd).filter(_ASd.key.like(prefix + "%")).all():
        try:
            out.setdefault(r.key[len(prefix):], _jd.loads(r.value).get("label", ""))
        except ValueError:
            pass
    return out


def _accounts_synced_at(cache: dict) -> str:
    stamps = [r.synced_at for r in (cache or {}).get("refs", {}).values()
              if getattr(r, "synced_at", None) and (r.usable_for or "") == "bank"]
    return (max(stamps).isoformat() + "Z") if stamps else ""


def _balance_cache(db, company: str) -> dict:
    """Everything the two balance columns need, read ONCE per screen.

    They used to query the database for every row -- the QuickBooks account,
    the Plaid connection, the program balance -- which is what made Bank /
    Card Accounts noticeably slower to open.
    """
    import json as _j
    from .models_db import QboRefRow as _QRc, PlaidItemRow as _PIc, AppSettingRow as _ASc
    from .review import _company_realm as _crc
    realm = _crc(db, company)
    refs = ({r.qbo_id: r for r in db.query(_QRc).filter(
        _QRc.kind == "account", _QRc.realm_id == realm).all()} if realm else {})
    plaid = {p.source: p for p in db.query(_PIc).filter(
        _PIc.company == company, _PIc.active.is_(True)).all()}
    prefix = f"api_balance:{company}:"
    api = {}
    for r in db.query(_ASc).filter(_ASc.key.like(prefix + "%")).all():
        try:
            api[r.key[len(prefix):]] = _j.loads(r.value)
        except ValueError:
            pass
    return {"refs": refs, "plaid": plaid, "api": api}


def _qbo_balance(db, company: str, acct, m, cache: dict | None = None):
    """QuickBooks' balance for a card account, as a string, or None.

    SIGNED THE WAY A PERSON READS IT. QuickBooks reports what is owed on a
    credit card as a negative number; shown here it is positive, and only a
    card in credit (overpaid) is negative. A bank account is shown as
    QuickBooks has it: positive, negative only when overdrawn.
    """
    ref = acct
    if ref is None and m is not None and m.qbo_account_id:
        if cache is not None:
            ref = cache["refs"].get(m.qbo_account_id)
        else:
            from .models_db import QboRefRow as _QRb
            from .review import _company_realm as _crb
            realm = _crb(db, company)
            ref = (db.query(_QRb).filter(_QRb.kind == "account",
                                         _QRb.qbo_id == m.qbo_account_id,
                                         _QRb.realm_id == realm).first()
                   if realm else None)
    bal = getattr(ref, "balance", None) if ref is not None else None
    if bal is None:
        return None
    if "credit" in (getattr(ref, "account_type", "") or "").lower():
        bal = -bal
    return str(bal)


def _bank_balance(db, company: str, source: str, cache: dict | None = None) -> dict:
    """The bank's balance -- Plaid's, or the card program's -- and when."""
    if cache is not None:
        it = cache["plaid"].get(source)
    else:
        from .models_db import PlaidItemRow as _PIb
        it = (db.query(_PIb).filter(_PIb.company == company, _PIb.source == source,
                                    _PIb.active.is_(True)).first())
    if it is not None and it.bank_balance is not None:
        return {"bank_balance": str(it.bank_balance),
                "bank_balance_at": (it.bank_balance_at.isoformat() + "Z"
                                    if it.bank_balance_at else "")}
    if cache is not None:
        ab = cache["api"].get(source) or {}
    else:
        from . import company_vars as _cvg
        ab = _cvg.get_api_balance(db, company, source)
    # SLASH'S BALANCES are left out for now: none of its figures matched the
    # Slash portal. The balance is still read and kept; only not shown.
    if ab.get("amount") is not None and "(Slash:" not in (ab.get("label") or ""):
        return {"bank_balance": str(ab["amount"]),
                "bank_balance_at": ab.get("at", ""),
                "bank_balance_label": ab.get("label", "")}
    return {"bank_balance": None, "bank_balance_at": ""}


# THE TESTING COMPANY. Its card connections do not block other companies,
# and it may connect programs others already use. One company, by name --
# it is unique to this installation, so it is not a setting.
TEST_COMPANIES = {"y&s tickets engine"}


def _is_test_company(db, company: str) -> bool:
    return (company or "").strip().lower() in TEST_COMPANIES


# --- Profiles (Configuration > Profiles) ------------------------------------
PROFILE_HEADERS = ["Card Account", "Bank Detail Rule",
                   "Bank Detail Value (Comma Separated)", "Team", "Profile"]


def _card_accounts_by_name(db, company: str) -> dict:
    """card account name (lower) -> source, for the grid and its upload: the
    full bank and card account list, as Bank / Card Accounts shows it."""
    from .models_db import SourceAccountRow
    out = {}
    for r in db.query(SourceAccountRow).filter(SourceAccountRow.company == company).all():
        for n in (r.nickname, r.qbo_account_name, r.source):
            if n:
                out.setdefault(n.strip().lower(), r.source)
    return out


@app.get("/profiles/rules")
def get_profile_rules(company: str = Query(...), db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    from . import profiles as _pf
    from .models_db import SourceAccountRow, TeamAliasRow
    # EVERY CARD ACCOUNT, connected or not: the accounts already set up here,
    # and every Credit Card account in this company's QuickBooks file -- as
    # Bank / Card Accounts lists them (an unconnected one as "qbo:<id>").
    from .models_db import CompanyRow as _CRp, QboRefRow as _QRp
    have = {r.source: (r.nickname or r.qbo_account_name or r.source)
            for r in db.query(SourceAccountRow).filter(SourceAccountRow.company == company).all()}
    mapped = {r.qbo_account_id for r in db.query(SourceAccountRow).filter(
        SourceAccountRow.company == company).all() if r.qbo_account_id}
    _co = db.get(_CRp, company)
    if _co and _co.realm_id:
        for q in db.query(_QRp).filter(_QRp.kind == "account", _QRp.realm_id == _co.realm_id,
                                       _QRp.account_type == "Credit Card").all():
            if q.qbo_id not in mapped and q.active is not False:
                have.setdefault(f"qbo:{q.qbo_id}", q.name)
    accts = sorted(have.items(), key=lambda x: str(x[1]).lower())
    teams = sorted({r.team for r in db.query(TeamAliasRow.team).all() if r.team},
                   key=str.lower)
    return {"rules": _pf.rules(db, company),
            "accounts": [{"source": s_, "name": n} for s_, n in accts],
            "teams": teams}


@app.post("/rules/order")
def save_rule_order(company: str = Form(...), ids: str = Form(...),
                    db: Session = Depends(get_session), user: UserRow = Depends(require_write)):
    """The order Bank Feed Rules are tried in, as dragged: a company's own
    rules (or, in Multi-Company, the shared rules). The first matching rule
    wins."""
    import json as _jo
    from .learning import RULE_ORDER_KEY, SHARED
    from .models_db import AppSettingRow, LearnedRuleRow
    try:
        lst = [str(x) for x in (_jo.loads(ids) or [])]
    except ValueError:
        raise HTTPException(400, "ids must be a JSON list")
    scope = SHARED if company == ALL_COMPANIES else company
    have = {r.id for r in db.query(LearnedRuleRow.id).filter(LearnedRuleRow.company == scope).all()}
    lst = [x for x in lst if x in have]
    db.merge(AppSettingRow(key=RULE_ORDER_KEY + scope, value=_jo.dumps(lst)))
    db.add(AuditRow(company=company if company != ALL_COMPANIES else "", action="rules_reordered",
                    detail=f"{len(lst)} rule(s) reordered", dry_run=False, actor=user.username))
    db.commit()
    return {"order": lst}


@app.post("/profiles/rules")
def save_profile_rules(company: str = Form(...), rules: str = Form(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_write)):
    """Replace the grid, in its order -- the first matching row wins."""
    import json as _jp
    from . import profiles as _pf
    try:
        rows = _jp.loads(rules)
    except ValueError:
        raise HTTPException(400, "rules must be a JSON list")
    saved = _pf.save(db, company, rows)
    db.add(AuditRow(company=company, action="profiles_saved",
                    detail=f"{len(saved)} profile row(s)", dry_run=False,
                    actor=user.username))
    db.commit()
    return {"rules": saved}


@app.post("/profiles/apply")
def apply_profile_rules(company: str = Form(...), db: Session = Depends(get_session),
                        user: UserRow = Depends(require_write)):
    """Fill the Profile of every charge in For Review from the grid."""
    from . import profiles as _pf
    # Blank profiles first, from the same card's other charges (every source).
    filled = _pf.backfill_blank(db, company, run_rules=False)
    n = _pf.apply(db, company) + filled
    db.add(AuditRow(company=company, action="profiles_applied",
                    detail=f"{n} charge(s) given a profile", dry_run=False,
                    actor=user.username))
    db.commit()
    return {"changed": n}


@app.get("/profiles/template")
def profile_template(company: str = "", db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    from .export_xlsx import build_workbook, filename
    rows = [["EXAMPLE — delete this row", "Contains", "TICKETMASTER, TM *",
             "Any", "Pat Doe"]]
    applied = [
        ("Card Account", "The card account's name, as on Bank / Card Accounts"),
        ("Bank Detail Rule", "Contains or Any"),
        ("Bank Detail Value", "One or more values, comma separated -- only for Contains"),
        ("Team", "Any, or one or more teams, comma separated"),
        ("Order", "Rows are read top to bottom; the first that matches sets the profile"),
    ]
    blob = build_workbook("Profiles", PROFILE_HEADERS, rows, applied, money_cols=[], filters_sheet=False)
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("profiles", "template")}"'})


@app.post("/profiles/import")
async def import_profile_rules(company: str = Form(...), file: UploadFile = File(...),
                               db: Session = Depends(get_session),
                               user: UserRow = Depends(require_write)):
    """Add the rows of a spreadsheet (same columns as the template) to the end
    of the grid. A row naming a card account this company doesn't have is
    reported and skipped."""
    import io as _io
    from openpyxl import load_workbook
    from . import profiles as _pf
    data = await file.read()
    try:
        ws = load_workbook(_io.BytesIO(data), read_only=True, data_only=True).active
    except Exception:                                   # noqa: BLE001
        raise HTTPException(400, "That file could not be read as an Excel workbook")
    it = ws.iter_rows(values_only=True)
    head = [str(h or "").strip().lower() for h in next(it, [])]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h.startswith(n):
                    return i
        return None
    ci = {"acct": col("card account"), "op": col("bank detail rule"),
          "val": col("bank detail value"), "team": col("team"), "prof": col("profile")}
    if ci["acct"] is None or ci["prof"] is None:
        raise HTTPException(400, "The sheet needs Card Account and Profile columns "
                                 "-- download the template for the layout")
    names = _card_accounts_by_name(db, company)
    new, skipped = [], []
    for n, r in enumerate(it, start=2):
        get = lambda k: (str(r[ci[k]]).strip() if ci[k] is not None and ci[k] < len(r)
                         and r[ci[k]] is not None else "")
        acct, prof = get("acct"), get("prof")
        if not acct and not prof:
            continue
        if acct.upper().startswith("EXAMPLE"):
            continue
        # One card account, or several separated by commas.
        _parts = [a.strip() for a in acct.split(",") if a.strip()]
        _miss = [a for a in _parts if not names.get(a.lower())]
        src = ",".join(names[a.lower()] for a in _parts if names.get(a.lower()))
        if _miss or not src:
            skipped.append(f"row {n}: no card account named \"{(_miss or [acct])[0]}\"")
            continue
        new.append({"source": src, "op": get("op") or "Any", "value": get("val"),
                    "teams": [t for t in get("team").split(",")] or ["Any"],
                    "profile": prof})
    saved = _pf.save(db, company, _pf.rules(db, company) + new)
    db.add(AuditRow(company=company, action="profiles_imported",
                    detail=f"{len(new)} row(s) added from {file.filename}",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"added": len(_pf.clean(new)), "skipped": skipped, "rules": saved}


# --- A company's own teams and leagues (Configuration > Teams) ---------------
def _dashboard_teams(db) -> list:
    """The Dashboard's teams, each with its league, venues and bank details."""
    from .models_db import TeamAliasRow
    by = {}
    for r in db.query(TeamAliasRow).all():
        if not r.team:
            continue
        d = by.setdefault(r.team, {"team": r.team, "league": r.league or "",
                                   "venue": [], "notes": []})
        if r.league and not d["league"]:
            d["league"] = r.league
        if (r.kind or "") == "venue" and r.alias:
            d["venue"].append(r.alias)
        elif (r.kind or "") == "note" and r.alias:
            d["notes"].append(r.alias)
    return [{**d, "venue": ", ".join(d["venue"]), "notes": ", ".join(d["notes"])}
            for d in sorted(by.values(), key=lambda x: x["team"].lower())]


@app.get("/teams/company")
def get_company_teams(company: str = Query(...), db: Session = Depends(get_session),
                      user: UserRow = Depends(require_reviewer)):
    from . import company_teams as _ct
    dash = _dashboard_teams(db)
    # THE SAME LEAGUES THE ADD / EDIT LEAGUES WINDOW LISTS -- the visible
    # Dashboard teams' leagues, those added, and this company's own. Reading
    # every Dashboard team's league offered leagues of hidden teams, which the
    # window doesn't list.
    leagues = sorted(set(_visible_league_teams(db)) | set(_ct.leagues(db))
                     | set(_ct.leagues(db, company)), key=str.lower)
    return {"teams": _ct.teams(db, company), "dashboard": dash, "leagues": leagues,
            "company_leagues": _ct.leagues(db, company),
            # The Dashboard's order, so the picker reads as its Leagues window does.
            "league_order": _league_order(db)}


@app.post("/teams/company")
def save_company_teams(company: str = Form(...), teams: str = Form(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_write)):
    import json as _jt
    from . import company_teams as _ct
    try:
        rows = _jt.loads(teams)
    except ValueError:
        raise HTTPException(400, "teams must be a JSON list")
    saved = _ct.save_teams(db, company, rows)
    db.add(AuditRow(company=company, action="company_teams_saved",
                    detail=f"{len(saved)} team(s) added or overridden", dry_run=False,
                    actor=user.username))
    db.commit()
    return {"teams": saved}


TEAM_TEMPLATE_HEADERS = ["Team", "League", "Venue", "Bank Details"]


@app.get("/teams/template/")
@app.get("/teams/template/{company}")
def teams_template(company: str = "", db: Session = Depends(get_session),
                   user: UserRow = Depends(require_reviewer)):
    """The columns Upload Teams reads: Team, League, Venue, Bank Details --
    the Teams grid's own columns. Venue and Bank Details may each hold
    several, comma separated. Same sheet for the Dashboard and a company."""
    from .export_xlsx import build_workbook, filename
    rows = [["EXAMPLE \u2014 Alabama Crimson Tide Football", "NCAAF", "Bryant-Denny Stadium",
             "ALABAMA ATHLETICS, ROLL TIDE TICKETS"]]
    notes = [("Team", "Required. A team already on the grid is updated: its venues and bank details are added to"),
             ("League", "A league on the grid, or a new one (it is added)"),
             ("Venue and Bank Details", "Several allowed, comma separated"),
             ("Note", "Rows starting with EXAMPLE are skipped. Every tab with a Team column is read."),
             ("Note ", "Nothing is loaded unless every row is valid")]
    blob = build_workbook("Teams", TEAM_TEMPLATE_HEADERS, rows, notes, filters_sheet=False)
    return Response(content=blob,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename("teams", "template")}"'})


def _read_team_sheet(raw: bytes) -> tuple[list, list]:
    """(rows, problems) from every tab of an uploaded sheet with a Team
    column. Each row is {team, league, venue, notes, where}."""
    from io import BytesIO
    from openpyxl import load_workbook
    try:
        wb = load_workbook(BytesIO(raw), data_only=True, read_only=True)
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(400, f"That file could not be read as a spreadsheet: {e}")
    norm = lambda x: " ".join(str(x).split()) if x is not None else ""
    split = lambda v: [x.strip() for x in str(v or "").split(",") if x.strip()]
    out, problems, saw_team = [], [], False
    for ws in wb.worksheets:
        grid = list(ws.iter_rows(values_only=True))
        if not grid:
            continue
        head = [norm(h).lower() for h in grid[0]]
        if "team" not in head:
            continue
        saw_team = True
        col = lambda *names: next((head.index(n) for n in names if n in head), None)
        ix = {"team": col("team"), "league": col("league"),
              "venue": col("venue", "venues"),
              "notes": col("bank details", "bank details (comma separated)", "bank detail", "notes")}
        for n, r in enumerate(grid[1:], start=2):
            get = lambda k: norm(r[ix[k]]) if ix[k] is not None and ix[k] < len(r) else ""
            if not any(get(k) for k in ix):
                continue
            team = get("team")
            if team.upper().startswith("EXAMPLE"):
                continue
            where = f"{ws.title} row {n}"
            if not team:
                problems.append(f"{where}: no team name")
                continue
            out.append({"team": team, "league": get("league"), "venue": split(get("venue")),
                        "notes": split(get("notes")), "where": where})
    if not saw_team:
        raise HTTPException(400, "That sheet needs a Team column (Download Template shows the columns)")
    return out, problems


@app.post("/teams/import")
async def import_teams(company: str = Form(""), file: UploadFile = File(...),
                       db: Session = Depends(get_session),
                       user: UserRow = Depends(require_write)):
    """UPLOAD TEAMS, on the Dashboard's Teams grid (no company) or a
    company's Configuration > Teams. A new team is added; one already there
    gets the sheet's venues and bank details added to its own, and its league
    if it had none. All or nothing: any bad row and nothing is loaded."""
    from datetime import datetime as _dt
    from .models_db import TeamAliasRow
    rows, problems = _read_team_sheet(await file.read())
    if problems:
        raise HTTPException(400, f"Nothing was imported. {len(problems)} problem(s): " + " | ".join(problems[:8]))
    if not rows:
        raise HTTPException(400, "No teams found in that sheet")
    # One row per team: the same team twice in the sheet is merged.
    merged = {}
    for r in rows:
        m = merged.setdefault(r["team"].lower(), {"team": r["team"], "league": "", "venue": [], "notes": []})
        m["league"] = m["league"] or r["league"]
        for k in ("venue", "notes"):
            m[k] += [x for x in r[k] if x.lower() not in {y.lower() for y in m[k]}]
    added = updated = 0
    if company and company != ALL_COMPANIES:
        from . import company_teams as _ct
        have = _ct.teams(db, company)
        by = {t["team"].lower(): t for t in have}
        dash = {d["team"].lower(): d for d in _dashboard_teams(db)}
        known = {l.lower() for l in _ct.leagues(db, company)} | {l.lower() for l in _ct.leagues(db)} \
            | {d["league"].lower() for d in _dashboard_teams(db) if d["league"]}
        for key, m in merged.items():
            t = by.get(key)
            if t is None:
                # A DASHBOARD TEAM starts from the Dashboard's values, as typing
                # its name on the grid does, so the override only adds to them.
                d = dash.get(key) or {}
                t = {"team": d.get("team") or m["team"], "league": d.get("league") or "",
                     "venue": d.get("venue") or "", "notes": d.get("notes") or ""}
                have.append(t); by[key] = t; added += 1
            else:
                updated += 1
            t["league"] = t.get("league") or m["league"]
            for k in ("venue", "notes"):
                cur = [x.strip() for x in (t.get(k) or "").split(",") if x.strip()]
                cur += [x for x in m[k] if x.lower() not in {y.lower() for y in cur}]
                t[k] = ", ".join(cur)
            if m["league"] and m["league"].lower() not in known:
                _ct.add_league(db, m["league"], company)
                known.add(m["league"].lower())
        _ct.save_teams(db, company, have)
        where = company
    else:
        if not db.query(TeamAliasRow).filter(TeamAliasRow.locked.is_(True)).first():
            raise HTTPException(409, "this table is still synced from Airtable \u2014 take it over before adding here")
        now = _dt.utcnow()
        for key, m in merged.items():
            cur = db.query(TeamAliasRow).filter(TeamAliasRow.team.ilike(m["team"])).all()
            team = cur[0].team if cur else m["team"]
            league = (cur[0].league if cur and cur[0].league else m["league"]) or ""
            if cur:
                updated += 1
                if league and not cur[0].league:
                    for r in cur:
                        r.league = league
            else:
                added += 1
                db.add(TeamAliasRow(id=f"{team}|note|{team.lower()}", team=team, league=league, alias=team,
                                    kind="note", source="engine", locked=True, updated_at=now))
            ids = {r.id for r in cur} | {f"{team}|note|{team.lower()}"}
            for kind, field in (("venue", "venue"), ("note", "notes")):
                for a in m[field]:
                    kid = f"{team}|{kind}|{a.lower()}"
                    if kid in ids:
                        continue
                    ids.add(kid)
                    db.add(TeamAliasRow(id=kid, team=team, league=league, alias=a, kind=kind, source="engine",
                                        locked=True, updated_at=now,
                                        auto_add=bool(cur[0].auto_add) if cur else False,
                                        auto_add_refunds=bool(cur[0].auto_add_refunds) if cur else False))
        where = ""
    db.add(AuditRow(company=where, action="teams_imported", actor=user.username, dry_run=False,
                    detail=f"{added} team(s) added, {updated} updated from {file.filename}"))
    db.commit()
    return {"added": added, "updated": updated}


LEAGUE_ORDER_KEY = "league_order"
# Each league's own color, chosen in Add / Edit Leagues: {LEAGUE: "#rrggbb"}.
# A league without one keeps its color from the built-in palette.
LEAGUE_COLORS_KEY = "league_colors"


def _league_colors(db) -> dict:
    import json as _jc
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, LEAGUE_COLORS_KEY)
    try:
        return _jc.loads(r.value) if r and r.value else {}
    except ValueError:
        return {}


def _league_order(db) -> list:
    import json as _jo
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, LEAGUE_ORDER_KEY)
    try:
        return _jo.loads(r.value) if r and r.value else []
    except ValueError:
        return []


def _save_league_order(db, order: list) -> None:
    import json as _jo
    from .models_db import AppSettingRow
    seen, out = set(), []
    for x in order:
        x = str(x).strip()
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    db.merge(AppSettingRow(key=LEAGUE_ORDER_KEY, value=_jo.dumps(out)))


@app.post("/teams/leagues/order")
def save_league_order(order: str = Form(...), db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """The order leagues are listed in on the Teams grid, as chosen."""
    import json as _jo
    try:
        lst = _jo.loads(order)
    except ValueError:
        raise HTTPException(400, "order must be a JSON list")
    _save_league_order(db, lst)
    db.add(AuditRow(company="", action="league_order_saved", detail=", ".join(map(str, lst))[:500],
                    dry_run=False, actor=user.username))
    db.commit()
    return {"order": _league_order(db)}


def _visible_league_teams(db) -> dict:
    """league -> sorted team names, for the teams the Teams grid SHOWS: not
    hidden, and not the promoter / venue rows that never count as teams. The
    Leagues window counted every row, so a league kept a count of teams nobody
    could see -- and could not be deleted."""
    import json as _jv
    from .models_db import TeamAliasRow as _TAv, AppSettingRow as _ASv
    _h = db.get(_ASv, "team_hidden")
    try:
        hidden = {str(t).strip().lower() for t in _jv.loads((_h.value if _h else "") or "[]")}
    except Exception:                                   # noqa: BLE001
        hidden = set()
    out: dict = {}
    for r in db.query(_TAv).filter(_TAv.league.isnot(None), _TAv.league != "").all():
        if (r.team or "").strip().lower() in hidden or is_never_row(r.team, r.alias, r.league):
            continue
        out.setdefault(r.league, set()).add(r.team)
    return {k: sorted(v, key=str.lower) for k, v in out.items()}


@app.get("/teams/leagues/all")
def list_all_leagues(company: str = Query(""), db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """Every league on the Dashboard -- those teams use and those added -- with
    how many teams use each."""
    from sqlalchemy import func as _fl
    from .models_db import TeamAliasRow
    from . import company_teams as _ct
    vis = _visible_league_teams(db)
    used = {lg: len(t) for lg, t in vis.items()}
    names = sorted(set(used) | set(_ct.leagues(db)), key=str.lower)
    # INSIDE A COMPANY: which leagues are that company's own. The rest are the
    # Dashboard's -- shown there, but not renamed, recolored or removed.
    from . import company_teams as _ctl
    own = [x for x in (_ctl.leagues(db, company) or [])] if company and company != ALL_COMPANIES else []
    names = names + [x for x in own if x not in names]
    return {"leagues": [{"name": n, "teams": int(used.get(n, 0)), "team_names": vis.get(n, [])[:50],
                         "own": n in own} for n in names],
            "order": _league_order(db), "colors": _league_colors(db),
            "company": company if own or company else ""}


@app.post("/teams/leagues/color")
def save_league_color(league: str = Form(...), color: str = Form(""),
                      db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """A league's color on every grid; an empty color returns it to the
    built-in palette."""
    import json as _jc
    from .models_db import AppSettingRow
    lg = league.strip().upper()
    c = color.strip().lower()
    if c and not _re.fullmatch(r"#[0-9a-f]{6}", c):
        raise HTTPException(400, "A color is written #rrggbb")
    cur = _league_colors(db)
    if c:
        cur[lg] = c
    else:
        cur.pop(lg, None)
    db.merge(AppSettingRow(key=LEAGUE_COLORS_KEY, value=_jc.dumps(cur)))
    db.add(AuditRow(company="", action="league_color_saved", detail=f"{lg}: {c or 'built-in'}",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"colors": cur}


@app.post("/teams/leagues/rename")
def rename_league(old: str = Form(...), new: str = Form(...),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    """Rename a league everywhere it is used: the Dashboard's teams, payment
    plans, every company's own teams, and the added list."""
    import json as _jl
    from .models_db import TeamAliasRow, PaymentPlanRow, AppSettingRow
    from . import company_teams as _ct
    old, new = old.strip(), new.strip()
    if not old or not new:
        raise HTTPException(400, "Give the league its new name")
    n = db.query(TeamAliasRow).filter(TeamAliasRow.league == old).update(
        {TeamAliasRow.league: new}, synchronize_session=False)
    db.query(PaymentPlanRow).filter(PaymentPlanRow.league == old).update(
        {PaymentPlanRow.league: new}, synchronize_session=False)
    for r in db.query(AppSettingRow).filter(AppSettingRow.key.like(_ct.TEAMS_KEY + "%")).all():
        try:
            rows = _jl.loads(r.value or "[]")
        except ValueError:
            continue
        if any(x.get("league") == old for x in rows):
            for x in rows:
                if x.get("league") == old:
                    x["league"] = new
            r.value = _jl.dumps(rows)
    for r in db.query(AppSettingRow).filter(AppSettingRow.key.like(_ct.LEAGUES_KEY + "%")).all():
        try:
            lst = _jl.loads(r.value or "[]")
        except ValueError:
            continue
        if old in lst:
            r.value = _jl.dumps(sorted({new if x == old else x for x in lst}, key=str.lower))
    _save_league_order(db, [new if x == old else x for x in _league_order(db)])
    db.add(AuditRow(company="", action="league_renamed", detail=f"{old} -> {new} ({n} alias row(s))",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True}


@app.post("/teams/leagues/delete")
def delete_league(name: str = Form(...), db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    """Delete a league no team uses. One still in use is refused, with the
    count -- rename it, or move those teams, first."""
    import json as _jl
    from sqlalchemy import func as _fd
    from .models_db import TeamAliasRow, AppSettingRow
    from . import company_teams as _ct
    name = name.strip()
    teams_in = list(_visible_league_teams(db).get(name, []))
    # A COMPANY'S OWN TEAMS in it count too.
    for _r in db.query(AppSettingRow).filter(AppSettingRow.key.like(_ct.TEAMS_KEY + "%")).all():
        try:
            for _t in _jl.loads(_r.value or "[]"):
                if (_t.get("league") or "").strip().lower() == name.lower() and _t.get("team") not in teams_in:
                    teams_in.append(_t["team"])
        except (ValueError, AttributeError):
            continue
    if teams_in:
        raise HTTPException(409, f"{len(teams_in)} team(s) are in {name}: {', '.join(teams_in[:8])}"
                                 f"{' and more' if len(teams_in) > 8 else ''}. Rename the league, "
                                 f"or move those teams to another league, first.")
    for r in db.query(AppSettingRow).filter(AppSettingRow.key.like(_ct.LEAGUES_KEY + "%")).all():
        try:
            lst = _jl.loads(r.value or "[]")
        except ValueError:
            continue
        if name in lst:
            r.value = _jl.dumps([x for x in lst if x != name])
    _save_league_order(db, [x for x in _league_order(db) if x != name])
    db.add(AuditRow(company="", action="league_deleted", detail=name, dry_run=False,
                    actor=user.username))
    db.commit()
    return {"ok": True}


@app.post("/teams/leagues")
def add_team_league(name: str = Form(...), company: str = Form(""),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_write)):
    """Add a league -- to the Dashboard's list (no company, admins only) or to
    one company's own list."""
    from . import company_teams as _ct
    if not company and getattr(user, "role", "") not in ("owner", "admin"):
        raise HTTPException(403, "Only an admin can add a league to the Dashboard")
    if not name.strip():
        raise HTTPException(400, "Give the league a name")
    out = _ct.add_league(db, name, company)
    db.add(AuditRow(company=company or "", action="league_added", detail=name.strip(),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"leagues": out}


# --- Airtable setup, per broker (Connectors) -----------------------------------
def _at_get(company: str, path: str, params=None) -> dict:
    """A GET to Airtable as this company, with its own token."""
    import httpx as _hx
    from .db import SessionLocal as _SLa
    with _SLa() as _db:
        with _cv.for_company(_db, company):
            tok = _cv.value("airtable_token")
    if not tok:
        raise HTTPException(400, "Enter this company's Airtable token on Connectors first")
    r = _hx.get(f"https://api.airtable.com/v0/{path}", params=params or [],
                headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if r.status_code in (401, 403):
        raise HTTPException(400, "Airtable refused the token here. It needs the "
                                 "schema.bases:read and data.records:read scopes, "
                                 "and access to this base, in Airtable's Builder Hub.")
    if r.status_code >= 400:
        raise HTTPException(400, f"Airtable said {r.status_code}: {r.text[:200]}")
    return r.json()


@app.get("/airtable/bases")
def airtable_bases(company: str = Query(...), user: UserRow = Depends(require_admin)):
    """The bases this company's token can reach."""
    out, offset = [], None
    while True:
        j = _at_get(company, "meta/bases", [("offset", offset)] if offset else [])
        out += [{"id": b["id"], "name": b.get("name", b["id"])} for b in j.get("bases", [])]
        offset = j.get("offset")
        if not offset:
            break
    return {"bases": sorted(out, key=lambda b: b["name"].lower())}


@app.get("/airtable/schema")
def airtable_schema(company: str = Query(...), base: str = Query(...),
                    user: UserRow = Depends(require_admin)):
    """A base's tables, each with its columns and views."""
    j = _at_get(company, f"meta/bases/{base}/tables")
    # A linked column says which table it links to, and every table which of
    # its columns is primary, so the setup can offer "Read From" -- the column
    # of the linked table to read instead of record ids.
    return {"tables": [{"id": tb["id"], "name": tb.get("name", tb["id"]),
                        "primary": next((f["name"] for f in tb.get("fields", [])
                                         if f.get("id") == tb.get("primaryFieldId")), ""),
                        "fields": [{"name": f["name"], "type": f.get("type", ""),
                                    "linked": (f.get("options") or {}).get("linkedTableId", "")}
                                   for f in tb.get("fields", [])],
                        "views": [{"id": v["id"], "name": v.get("name", v["id"])}
                                  for v in tb.get("views", [])]}
                       for tb in j.get("tables", [])]}


@app.get("/algo/suggestions")
def get_suggest_params(db: Session = Depends(get_session), user: UserRow = Depends(require_reviewer)):
    """What drives a suggestion from past coding, and the choices."""
    from . import history as _h
    return {"params": _h.params(db), "fields": _h.KEY_FIELDS, "defaults": _h.DEFAULTS}


@app.post("/algo/suggestions")
def save_suggest_params(min_times: int = Form(...), min_share: float = Form(...),
                        keys: str = Form("merchant"), people_only: str = Form("true"),
                        db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """Save what drives suggestions, for every company."""
    import json as _js
    from . import history as _h
    from .models_db import AppSettingRow
    if min_times < 1 or min_times > 500:
        raise HTTPException(400, "Similar past codings must be between 1 and 500")
    if min_share < 0.5 or min_share > 1:
        raise HTTPException(400, "Agreement must be between 50% and 100%")
    ks = [k for k in keys.split(",") if k in _h.KEY_FIELDS]
    val = {"min_times": int(min_times), "min_share": round(float(min_share), 3),
           "keys": ks or ["merchant"], "people_only": people_only.strip().lower() in ("1", "true", "yes", "on")}
    db.merge(AppSettingRow(key=_h.PARAMS_KEY, value=_js.dumps(val)))
    db.add(AuditRow(company="", action="suggest_params_saved", detail=_js.dumps(val)[:500],
                    dry_run=False, actor=user.username))
    db.commit()
    _h._CACHE.clear()
    return {"params": _h.params(db)}


@app.get("/history/suggestions")
def list_history_suggestions(company: str = Query(...), db: Session = Depends(get_session),
                             user: UserRow = Depends(require_reviewer)):
    """CONFIGURATION > SUGGESTIONS: the vendor and category this company's
    past coding suggests, by bank detail (and whatever else the Algorithms
    settings match on), and those removed."""
    from . import history as _h
    if company == ALL_COMPANIES:
        raise HTTPException(400, "Choose a company")
    out = _h.listing(db, company)
    out["enabled"] = _h.enabled(db, company)
    return out


@app.post("/history/suggestions/remove")
def remove_history_suggestion(company: str = Form(...), key: str = Form(...), restore: str = Form(""),
                              action: str = Form("ignore"),
                              db: Session = Depends(get_session), user: UserRow = Depends(require_write)):
    """Bank Feed > Suggestions: IGNORE a suggestion (listed, ticked, not
    offered; restore unticks it) or DELETE it (gone from the list and never
    offered again)."""
    import json as _jh
    from . import history as _h
    try:
        k = tuple(str(x) for x in _jh.loads(key))
    except (ValueError, TypeError):
        raise HTTPException(400, "key must be a JSON list")
    if not k:
        raise HTTPException(400, "key must not be empty")
    back = restore.strip().lower() in ("1", "true", "yes")
    if action == "delete":
        gone = _h.deleted(db, company); gone.add(k); _h.set_deleted(db, company, gone)
        ign = _h.ignored(db, company); ign.discard(k); _h.set_ignored(db, company, ign)
        what = "deleted"
    else:
        ign = _h.ignored(db, company)
        (ign.discard if back else ign.add)(k)
        _h.set_ignored(db, company, ign)
        what = "no longer ignored" if back else "ignored"
    # THE ROWS IT FILLED are worked out again on the next render: ignored, they
    # lose the vendor and category it gave them; unticked, they get it back,
    # with its badge.
    try:
        _row = (_h._table(db, company, include_ignored=True) or {}).get(k)
        _h.reset_stored(db, company, k, *((_row[0], _row[1]) if _row else ("", "")))
    except Exception:                               # noqa: BLE001
        log.warning("could not reset the stored suggestions", exc_info=True)
    db.add(AuditRow(company=company, action="setting_saved", dry_run=False, actor=user.username,
                    detail=f"Suggestion {what}: {' / '.join(k)}"[:500]))
    db.commit()
    return {"ok": True}


@app.get("/setup/season-conventions")
def get_season_conventions(company: str = Query(...), db: Session = Depends(get_session),
                           user: UserRow = Depends(require_reviewer)):
    """How this broker writes a single season year, per league, and the leagues
    to list: the major ones, college, and any the company's teams use."""
    # EVERY LEAGUE THE DASHBOARD LISTS (those its teams use and those added
    # there), then this company's own -- added on its Teams tab or used by its
    # own teams -- in the order the Leagues window sets. It listed only the
    # majors plus added leagues, so leagues that came with the teams were missing.
    from sqlalchemy import func as _fsl
    from .models_db import TeamAliasRow as _TAsl
    from . import company_teams as _ct
    names, seen = [], set()
    def _add(l):
        l = str(l or "").strip()
        if l and l.upper() not in seen:
            seen.add(l.upper()); names.append(l.upper())
    for l in ["NFL", "NBA", "MLB", "NHL", "MLS", "WNBA", "NCAAF", "NCAAB"]:
        _add(l)
    for (l,) in db.query(_TAsl.league).filter(_TAsl.league.isnot(None), _TAsl.league != "").distinct().all():
        _add(l)
    for l in (_ct.leagues(db) or []) + (_ct.leagues(db, company) or []):
        _add(l)
    for t in _ct.teams(db, company):
        _add(t.get("league"))
    order = [str(x).strip().upper() for x in _league_order(db)]
    def _rank(l):
        if l in order:
            return (0, order.index(l), "")
        tier = 0 if l in ("MLB", "NBA", "NFL", "NHL", "MLS") else 1 if l.startswith("NCAA") else 2 if l == "WNBA" else 3
        return (1, tier, l)
    with _cv.for_company(db, company):
        notes = _cv.season_notations()
    return {"leagues": sorted(names, key=_rank), "notations": notes, "choices": _cv.SEASON_NOTATIONS}


@app.post("/setup/season-conventions")
def save_season_conventions(company: str = Form(...), conventions: str = Form(...),
                            db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """Save what a single year means per league for this broker. Applied on
    the next sync of its season ticket records."""
    import json as _jc
    try:
        d = _jc.loads(conventions) or {}
    except ValueError:
        raise HTTPException(400, "conventions must be JSON")
    clean = {str(k).upper(): v for k, v in d.items() if v in _cv.SEASON_NOTATIONS and v != "same"}
    _cv.set_value(db, company, "season_conventions", _jc.dumps(clean), user.username)
    db.add(AuditRow(company=company, action="season_conventions_saved",
                    detail=", ".join(f"{k}: {v}" for k, v in clean.items()) or "all built in",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"notations": clean}


@app.get("/setup/sth-status-words")
def get_sth_status_words(company: str = Query(...), db: Session = Depends(get_session),
                         user: UserRow = Depends(require_reviewer)):
    """RENEWAL STATUS WORDS for one company: the Dashboard's statuses (with
    their Engine Notation and colors), this company's notation for each, its
    own statuses, and the pill color of every word."""
    from . import renewal_status as _rs
    st = _rs.company_state(db, company)
    by_kind = _rs.words_by_kind(db, company)
    return {**st, "kinds": _rs.KINDS, "colors": _rs.colors(db, company),
            # The earlier shape, still read by the Bank Feed's pill colors.
            "words": {**st["words"], **{k: by_kind[k] for k in _rs.KINDS}},
            "notation": st["words"],
            "engine_words": {k: by_kind["_engine_" + k] for k in _rs.KINDS}}


@app.post("/setup/sth-status-words")
def save_sth_status_words(company: str = Form(...), words: str = Form("{}"), custom: str = Form("[]"),
                          db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """Save this company's Renewal Status Words: its notation for each status
    ({status key: [words]}) and its own statuses. In use at once."""
    import json as _jw
    from . import renewal_status as _rs
    try:
        w = _jw.loads(words or "{}") or {}
        c = _jw.loads(custom or "[]") or []
    except ValueError:
        raise HTTPException(400, "words and custom must be JSON")
    try:
        st = _rs.save_company(db, company, w, c, user.username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(AuditRow(company=company, action="sth_status_words_saved",
                    detail=("; ".join(f"{s['name']}: {', '.join(st['words'].get(s['key'], [])) or '-'}" for s in st["engine"])
                            + ("; own: " + ", ".join(f"{s['name']} ({_rs.KINDS[s['kind']]})" for s in st["custom"])
                               if st["custom"] else ""))[:500],
                    dry_run=False, actor=user.username))
    db.commit()
    try:
        from .hal_cache import invalidate
        invalidate()
    except Exception:                                   # noqa: BLE001
        pass
    return get_sth_status_words(company=company, db=db, user=user)


@app.get("/renewal-statuses")
def get_renewal_statuses(db: Session = Depends(get_session), user: UserRow = Depends(require_reviewer)):
    """DASHBOARD > RENEWAL STATUS WORDS: every company's statuses, their Engine
    Notation and colors."""
    from . import renewal_status as _rs
    return {"statuses": _rs.engine_statuses(db), "kinds": _rs.KINDS}


@app.post("/renewal-statuses")
def save_renewal_statuses(statuses: str = Form(...), db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    import json as _jw
    from . import renewal_status as _rs
    try:
        rows = _jw.loads(statuses) or []
        assert isinstance(rows, list)
    except Exception:                                   # noqa: BLE001
        raise HTTPException(400, "statuses must be a JSON list")
    try:
        out = _rs.save_engine_statuses(db, rows)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(AuditRow(company="", action="sth_engine_words_saved",
                    detail="; ".join(f"{s['name']}: {', '.join(s['words']) or '-'}" for s in out)[:500],
                    dry_run=False, actor=user.username))
    db.commit()
    try:
        from .hal_cache import invalidate
        invalidate()
    except Exception:                                   # noqa: BLE001
        pass
    return {"statuses": out, "kinds": _rs.KINDS}


@app.get("/airtable/setup")
def get_airtable_setup(company: str = Query(...), db: Session = Depends(get_session),
                       user: UserRow = Depends(require_reviewer)):
    from .worker import _configured_seasons
    with _cv.for_company(db, company):
        return {"base": _cv.value("airtable_base_tickets") or "",
                # Each part as a LIST of sources, each naming its own base.
                "setup": {"book": _cv.airtable_sources("book"),
                          "sth": _cv.airtable_sources("sth"),
                          # Parts whose last source was removed: no built-in
                          # layout either, until a source is added.
                          "off": list((_cv.airtable_setup() or {}).get("off") or [])},
                # The seasons the engine mirrors: one status column each, when
                # a broker keeps a column per season.
                "seasons": _configured_seasons()}


def _check_source(part: str, s: dict) -> str:
    """What a source is missing, in words; "" when it is complete."""
    what = "Profiles / Address Book" if part == "book" else "Season Ticket Records"
    prov = s.get("provider") or "airtable"
    if prov not in ("airtable", "gsheets", "onedrive"):
        return f"{what}: unknown source type {prov}"
    if prov == "airtable" and (not s.get("base") or not s.get("table")):
        return f"{what}: every Airtable source needs a base and a table"
    if prov != "airtable" and (not s.get("file") or not s.get("tab")):
        return f"{what}: every spreadsheet source needs a file and a tab"
    s.setdefault("table_name", s.get("tab", ""))
    s.setdefault("table", s.get("table") or "")
    if part == "book":
        if not s.get("email") or not (s.get("name") or s.get("first")):
            return f"{what} ({s.get('table_name') or s.get('tab')}): choose a name column and an email column"
        return ""
    if not (s.get("fields") or {}).get("team"):
        return f"{what} ({s.get('table_name') or s.get('tab')}): choose the Team column"
    # WHO HOLDS IT. Without a name or an email, no charge can ever be matched to
    # these records -- a Y&S source saved without one left 30,811 records with
    # no holder, and every one of its STH matches gone.
    _f = s.get("fields") or {}
    # NAME IS REQUIRED (its red asterisk): the Name column, or both First and
    # Last Name. An email alone no longer satisfies it.
    if not (_f.get("lookup") or (_f.get("first") and _f.get("last"))):
        return (f"{what} ({s.get('table_name') or s.get('tab')}): choose the Name column (or both First Name "
                f"and Last Name) -- without one no charge can be matched to these records")
    shape = s.get("status_shape") or "single"
    if shape == "rows" and not s.get("season_col"):
        return f"{what} ({s.get('table_name') or s.get('tab')}): choose the Season column"
    if shape == "single" and not s.get("status_single"):
        return f"{what} ({s.get('table_name') or s.get('tab')}): choose the Status column"
    return ""


@app.post("/airtable/setup")
def save_airtable_setup(company: str = Form(...), part: str = Form(""),
                        sources: str = Form(""), base: str = Form(""),
                        setup: str = Form(""), db: Session = Depends(get_session),
                        user: UserRow = Depends(require_admin)):
    """Save one part's sources -- "book" (Profiles / Address Book) or "sth"
    (Season Ticket Records) -- leaving the other part as it is. Each source
    names its own base, table and columns."""
    import json as _js
    try:
        with _cv.for_company(db, company):
            cur = {"book": _cv.airtable_sources("book"), "sth": _cv.airtable_sources("sth")}
        if setup:                                   # the whole setup, as before
            whole = _js.loads(setup) or {}
            parts = {k: (v if isinstance(v, list) else [v] if v else [])
                     for k, v in whole.items() if k in ("book", "sth")}
            for lst in parts.values():
                for x in lst:
                    x.setdefault("base", base.strip())
        else:
            if part not in ("book", "sth"):
                raise HTTPException(400, "part must be book or sth")
            lst = _js.loads(sources or "[]")
            if not isinstance(lst, list):
                raise HTTPException(400, "sources must be a list")
            parts = {part: lst}
    except ValueError:
        raise HTTPException(400, "sources must be JSON")
    with _cv.for_company(db, company):
        _off = list((_cv.airtable_setup() or {}).get("off") or [])
    for k, lst in parts.items():
        for x in lst:
            why = _check_source(k, x)
            if why:
                raise HTTPException(400, why)
        cur[k] = lst
        if lst and k in _off:
            _off.remove(k)              # a source added: the part is on again
    cur["off"] = _off
    _cv.set_value(db, company, "airtable_setup", _js.dumps(cur), user.username)
    # The company's "own base" marker, which Connectors and the default layout
    # read: the first base this setup names, unless one is already set.
    # The company's OWN stored value: value() would fall back to the
    # deployment's default base, and a new broker would never be marked as
    # having one -- so its syncs would never run.
    from .models_db import CompanyVariableRow as _CVR
    _own = db.get(_CVR, _cv._key(company, "airtable_base_tickets"))
    have = bool(_own and (_own.value or "").strip())
    first = next((x["base"] for k in ("sth", "book") for x in cur.get(k) or [] if x.get("base")), "")
    if first and (not have or setup):
        _cv.set_value(db, company, "airtable_base_tickets", first, user.username)
    db.add(AuditRow(company=company, action="airtable_setup_saved",
                    detail="; ".join(
                        f"{'Profiles / Address Book' if k == 'book' else 'Season Ticket Records'}: "
                        + (", ".join(f"{x.get('base_name') or x.get('file_name') or x.get('base') or x.get('file')} \u203a "
                                     f"{x.get('table_name') or x.get('tab') or x.get('table')}" for x in lst)
                           or "none")
                        for k, lst in parts.items()),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True}


def _wipe_part(db, company: str, part: str) -> int:
    """Everything a Broker Data part holds for a company: its records and its
    sync details. Used when its last source goes, by either red x."""
    from .models_db import HalRow as _HRx, ProfileEmailRow as _PEx, SyncStatusRow as _SSx
    if part == "sth":
        n = db.query(_HRx).filter(_HRx.company == company).delete()
        key = f"hal:{company}"
    else:
        n = db.query(_PEx).filter(_PEx.company == company).delete()
        key = f"emails:{company}"
    st = db.get(_SSx, key)
    if st:
        st.records, st.detail, st.state = 0, "no source: removed", "idle"
        st.finished_at = None
    try:
        from .review import reset_email_cache
        from .hal_cache import invalidate
        reset_email_cache(); invalidate()
    except Exception:                                           # noqa: BLE001
        pass
    return n


@app.post("/broker-data/remove-source")
def remove_broker_source(company: str = Form(...), part: str = Form(...), index: int = Form(...),
                         db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """Remove one source. Removing a part's LAST source wipes the part: its
    records, its sync details, and the built-in layout that would otherwise
    take over -- the red x left all three behind."""
    import json as _jr
    from .models_db import HalRow as _HRx, ProfileEmailRow as _PEx, SyncStatusRow as _SSx
    if part not in ("book", "sth"):
        raise HTTPException(400, "part must be book or sth")
    with _cv.for_company(db, company):
        cur = {"book": _cv.airtable_sources("book"), "sth": _cv.airtable_sources("sth"),
               "off": list((_cv.airtable_setup() or {}).get("off") or [])}
    # index -1: THE BUILT-IN LAYOUT, which has no source of its own -- removing
    # it wipes the part and switches it off, as removing a last source does.
    if index == -1 and not cur[part]:
        gone = {"table_name": "the default Tickets layout"}
    elif not (0 <= index < len(cur[part])):
        raise HTTPException(404, "no such source")
    else:
        gone = cur[part].pop(index)
    wiped = 0
    if not cur[part]:
        if part not in cur["off"]:
            cur["off"].append(part)
        wiped = _wipe_part(db, company, part)
    _cv.set_value(db, company, "airtable_setup", _jr.dumps(cur), user.username)
    db.add(AuditRow(company=company, action="broker_source_removed",
                    detail=f"{part}: {gone.get('base_name') or gone.get('file_name') or ''} \u203a "
                           f"{gone.get('table_name') or gone.get('tab') or ''}"
                           + (f"; {wiped:,} record(s) removed with the last source" if wiped else ""),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True, "wiped": wiped}


@app.post("/airtable/preview")
def airtable_preview(company: str = Form(...), part: str = Form(...),
                     source: str = Form(""), base: str = Form(""),
                     setup: str = Form(""), db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """The first few rows a DRAFT source would read -- before anything is
    saved, so a wrong column is seen at once. Linked columns are shown as the
    text they link to, and teams and seasons as the engine will read them."""
    import json as _jp
    from .integrations import airtable as _at
    from .worker import _configured_seasons, _known_teams
    try:
        s = _jp.loads(source) if source else ((_jp.loads(setup) or {}).get(part) or {})
    except ValueError:
        raise HTTPException(400, "source must be JSON")
    if isinstance(s, list):
        s = s[0] if s else {}
    s = dict(s)
    from .integrations import sheets as _sh
    s["provider"] = s.get("provider") or "airtable"
    if s["provider"] == "airtable":
        s["base"] = s.get("base") or base
        if not s.get("base") or not s.get("table"):
            raise HTTPException(400, "Choose a base and a table first")
    elif not s.get("file") or not s.get("tab"):
        raise HTTPException(400, "Choose a file and a tab first")
    rows = []
    with _cv.for_company(db, company):
        if s["provider"] == "airtable" and not _cv.value("airtable_token"):
            raise HTTPException(400, "Enter this company's Airtable token on Connectors first")
        try:
            if part == "book":
                for f in _at.iter_source(s, _at.book_columns(s), limit=5):
                    name = _at._cell_text(f.get(s.get("name"))) if s.get("name") else \
                        f"{_at._cell_text(f.get(s.get('first')))} {_at._cell_text(f.get(s.get('last')))}".strip()
                    rows.append({"Name": name, "Emails": _at._cell_text(f.get(s.get("email")))})
            else:
                from engine import hal as _hal
                seasons = _configured_seasons() or list(_hal.TRACKED_SEASONS)
                if (s.get("status_shape") or "single") == "per_season":
                    seasons = list((s.get("status_by_season") or {}).keys()) or seasons
                known = _known_teams(db, company)
                for rid, f in _at.iter_source(s, _at.setup_sth_columns(s), limit=5,
                                              with_ids=True):
                    m = _at._map_record_setup({"id": rid, "fields": f}, seasons, s, known)
                    team = m["team"] if m["team"] == m["team_raw"] or not m["team_raw"] \
                        else f"{m['team_raw']} \u2192 {m['team']}"
                    if m.get("_team_unread"):
                        team += " (not recognized)"
                    row = {"Holder": m["lookup_name"], "Email": m["email"], "Team": team,
                           "League": m.get("league") or ""}
                    if (s.get("status_shape") or "") == "rows":
                        sk = m.get("_skip")
                        row["Season"] = (f"{m.get('season_raw')} \u2192 {m['season']}" if m.get("season")
                                         else f"{m.get('season_raw') or '(blank)'} (not read)")
                        if sk == "other":
                            row["Season"] += " (not mirrored)"
                    row.update({"Plan": m["plan_type"],
                                # As money: $6,000.00, not 6000.0.
                                "Total Cost": (f"${float(m['total']):,.2f}" if m["total"] not in (None, "")
                                               else "") if (s.get("fields") or {}).get("total") else "",
                                "Credit Card": m["card_program"],
                                "Statuses": ", ".join(f"{k}: {v}" for k, v in m["statuses"].items())})
                    rows.append(row)
        except (_at.AirtableAuthError, _at.AirtableQueryError, _sh.SheetsError) as e:
            raise HTTPException(400, str(e))
    return {"rows": rows}


# --- Google Sheets and OneDrive -------------------------------------------------
def _sheets_call(db, company: str, fn):
    from .integrations import sheets as _sh
    with _cv.for_company(db, company):
        try:
            return fn(_sh)
        except _sh.SheetsError as e:
            raise HTTPException(400, str(e))


# ONEDRIVE IS LISTED IN THE BACKGROUND. Walking a whole drive takes longer
# than Railway's proxy waits for a request, which answered with "upstream
# error" while the listing was still going. So the walk runs on a thread, the
# request returns at once with how far it has got, and the screen asks again
# until it is done. A finished list is kept for ten minutes per company.
_OD_SCANS: dict = {}


def _od_note(sh, files) -> str:
    if files:
        return ""
    sc = sh.LAST_OD_SCAN
    note = (f"Looked through {sc.get('own_items', 0):,} item(s) in this account's OneDrive"
            f" and {sc.get('shared_items', 0):,} shared with it, and found no Excel or CSV files.")
    if sc.get("other_types"):
        note += (f" {sc['other_types']} spreadsheet(s) in other formats (.ods, .numbers) "
                 "need saving as Excel first.")
    if sc.get("shared_error"):
        note += " Files shared with this account could not be listed."
    return note + " Files in SharePoint, Teams or another company's OneDrive: paste the file's link below."


def _od_scan(company: str, full: bool = False) -> None:
    import threading
    st = {"state": "running", "seen": 0, "files": [], "note": "", "at": time.time()}
    _OD_SCANS[company] = st

    def run():
        from .db import SessionLocal as _SLo
        from .integrations import sheets as _sh
        try:
            with _SLo() as _db, _cv.for_company(_db, company):
                files = _sh.onedrive_files(progress=st, full=full)
                st.update(files=files, note=_od_note(_sh, files), state="done", at=time.time())
        except Exception as e:                          # noqa: BLE001
            st.update(state="error", note=str(e)[:400], at=time.time())
    threading.Thread(target=run, daemon=True, name=f"onedrive-list-{company}").start()


@app.get("/sheets/files")
def sheets_files(company: str = Query(...), provider: str = Query(...), refresh: int = Query(0),
                 full: int = Query(0),
                 db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """The spreadsheets this company's Google key or OneDrive sign-in can open,
    and -- when there are none -- what was looked through, so the screen can
    say why rather than show an empty list. OneDrive answers {"scanning": true,
    "seen": n} while its listing runs."""
    if provider == "onedrive":
        from .integrations.sheets import LAST_OD_SCAN as _sh_scan
        with _cv.for_company(db, company):
            if not (_cv.value("microsoft_client_id") and _cv.value("microsoft_refresh_token")):
                raise HTTPException(400, "Sign in to OneDrive for this company on Connectors first.")
        st = _OD_SCANS.get(company)
        stale = not st or (st["state"] != "running" and time.time() - st["at"] > 600)
        if refresh or full or stale:
            if not (st and st["state"] == "running"):
                _od_scan(company, full=bool(full))
                st = _OD_SCANS[company]
        if st["state"] == "running":
            return {"scanning": True, "seen": st.get("seen", 0), "phase": st.get("phase", "search"),
                    "files": list(st.get("files") or [])}
        if st["state"] == "error":
            _OD_SCANS.pop(company, None)
            raise HTTPException(400, st["note"])
        return {"files": st["files"], "note": st["note"],
                "searched_only": bool(_sh_scan.get("searched")) and not _sh_scan.get("walked")}

    def go(sh):
        files = sh.files(provider)
        note = ""
        if not files and provider == "onedrive":
            sc = sh.LAST_OD_SCAN
            note = (f"Looked through {sc.get('own_items', 0):,} item(s) in this account's OneDrive"
                    f" and {sc.get('shared_items', 0):,} shared with it, and found no .xlsx or .csv files.")
            if sc.get("other_types"):
                note += (f" {sc['other_types']} spreadsheet(s) in other formats (.xls, .ods, .numbers) "
                         "need saving as .xlsx first.")
            if sc.get("shared_error"):
                note += " Files shared with this account could not be listed."
            note += " Files in SharePoint, Teams or another company's OneDrive: paste the file's link below."
        elif not files:
            # The service account's address is never shown (by request).
            note = ("No spreadsheets are shared with the Engine yet. Share them with the appropriate "
                    "Engine email address, or paste a sheet's link below.")
        return {"files": files, "note": note}
    return _sheets_call(db, company, go)


@app.get("/sheets/from-link")
def sheets_from_link(company: str = Query(...), provider: str = Query(...), url: str = Query(...),
                     db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """A file chosen by pasting its link: {"id", "name"}."""
    return _sheets_call(db, company, lambda sh: sh.from_link(provider, url))


@app.get("/sheets/tabs")
def sheets_tabs(company: str = Query(...), provider: str = Query(...), file: str = Query(...),
                db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    return {"tabs": _sheets_call(db, company, lambda sh: sh.tabs(provider, file))}


@app.get("/sheets/columns")
def sheets_columns(company: str = Query(...), provider: str = Query(...), file: str = Query(...),
                   tab: str = Query(...), header_row: int = Query(0),
                   db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """A tab's headings, and which row they were found on."""
    def go(sh):
        cols, found = sh.tab_columns(provider, file, tab, header_row)
        return {"columns": cols, "header_row": found}
    return _sheets_call(db, company, go)


@app.get("/sheets/status")
def sheets_status(company: str = Query(...), db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    """Who the Google key reads as -- the address to share sheets with -- and
    which Microsoft account is signed in."""
    from .integrations import sheets as _sh
    with _cv.for_company(db, company):
        _icons = pathlib.Path(__file__).parent / "static"
        return {"icons": {"onedrive": (_icons / "icon-onedrive.png").is_file(),
                          "gsheets": (_icons / "icon-google-sheets.png").is_file()},
                # Never the address itself (by request): only whether a key is set.
                "google_email": bool(_sh.google_email()),
                # The descriptions the Airtable and Slash rows show (pencil).
                "labels": {"airtable": _cv.value("airtable_label") or "",
                           "slash": _cv.value("slash_label") or ""},
                # Which apps this company has connected, so the setup window
                # starts a new source in one of them, and says so in the window
                # -- rather than a pop-up -- when one chosen is not connected.
                "airtable": bool(_cv.value("airtable_token")),
                "google": bool(_cv.value("google_service_account")),
                "microsoft_account": _cv.value("microsoft_account") or "",
                "microsoft_signed_in": bool(_cv.value("microsoft_refresh_token"))}


@app.post("/microsoft/signin-start")
def microsoft_signin_start(company: str = Form(...), db: Session = Depends(get_session),
                           user: UserRow = Depends(require_admin)):
    """Start Microsoft's device sign-in for this company's OneDrive."""
    import json as _jm
    from .integrations import sheets as _sh
    from .models_db import AppSettingRow
    with _cv.for_company(db, company):
        cid = _cv.value("microsoft_client_id") or ""
    if not cid:
        raise HTTPException(400, "Enter the Microsoft App (client) ID in the OneDrive row first")
    try:
        j = _sh.ms_device_start(cid)
    except _sh.SheetsError as e:
        raise HTTPException(400, str(e))
    db.merge(AppSettingRow(key=f"ms_device:{company}", value=_jm.dumps(
        {"device_code": j["device_code"], "expires": time.time() + int(j.get("expires_in", 900))})))
    db.commit()
    return {"user_code": j.get("user_code"), "verification_uri": j.get("verification_uri"),
            "interval": int(j.get("interval", 5)), "message": j.get("message", "")}


@app.post("/microsoft/signin-poll")
def microsoft_signin_poll(company: str = Form(...), db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    """Has the person finished signing in? Stores the sign-in when they have."""
    import json as _jm
    from .integrations import sheets as _sh
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, f"ms_device:{company}")
    d = _jm.loads(row.value) if row and row.value else {}
    if not d or d.get("expires", 0) < time.time():
        return {"state": "error", "message": "That sign-in code has expired. Start again."}
    with _cv.for_company(db, company):
        cid = _cv.value("microsoft_client_id") or ""
    res = _sh.ms_device_poll(cid, d["device_code"])
    if res["state"] == "ok":
        _cv.set_value(db, company, "microsoft_refresh_token", res["refresh_token"], user.username)
        _cv.set_value(db, company, "microsoft_account", res.get("account", ""), user.username)
        db.delete(row)
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"OneDrive signed in as {res.get('account') or 'an account'}",
                        dry_run=False, actor=user.username))
        db.commit()
        return {"state": "ok", "account": res.get("account", "")}
    return {k: v for k, v in res.items() if k in ("state", "message")}


@app.get("/setup/connectors-added")
def connectors_added(company: str = Query(...), db: Session = Depends(get_session),
                     user: UserRow = Depends(require_reviewer)):
    """Connectors this company has added on Connectors (Airtable, QuickBooks,
    Slash, WEX); an account program's accounts are listed on their own."""
    import json as _jc
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, f"connectors_added:{company}")
    try:
        return {"added": _jc.loads(row.value) if row else []}
    except ValueError:
        return {"added": []}


@app.post("/setup/connectors-added")
def add_connector(company: str = Form(...), connector: str = Form(...),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    import json as _jc
    from .models_db import AppSettingRow
    if connector not in ("airtable", "quickbooks", "slash", "wex", "google", "onedrive"):
        raise HTTPException(400, "Not a connector that is added this way")
    row = db.get(AppSettingRow, f"connectors_added:{company}")
    have = _jc.loads(row.value) if row and row.value else []
    if connector not in have:
        have.append(connector)
    db.merge(AppSettingRow(key=f"connectors_added:{company}", value=_jc.dumps(have)))
    db.add(AuditRow(company=company, action="connector_added", detail=connector,
                    dry_run=False, actor=user.username))
    db.commit()
    return {"added": have}


@app.get("/setup/settings")
def get_engine_settings(company: str = Query(""),
                        user: UserRow = Depends(require_reviewer)):
    """This company's settings, for Configuration > Settings."""
    from .integrations.qbo_write import engine_tag_on, _TAG_CACHE
    from .db import SessionLocal as _SLs
    from .review import pay_autocode_for
    _TAG_CACHE.pop(company, None)            # read fresh for the screen
    with _SLs() as _db:
        auto = pay_autocode_for(_db, company)
    from .review import auto_strong_level, auto_sth_on
    with _SLs() as _db:
        strong = auto_strong_level(_db, company)
        sth = auto_sth_on(_db, company)
        _bf = _db.get(__import__("app.models_db", fromlist=["x"]).AppSettingRow,
                      f"bill_fix_fees:{company}")
        fees = bool(_bf and (_bf.value or "") == "true")
        from .history import enabled as _hist_on
        hist_on = _hist_on(_db, company)
        _fz = _db.get(__import__("app.models_db", fromlist=["x"]).AppSettingRow, f"freeze_cols:{company}")
        freeze = bool(_fz and (_fz.value or "") == "true")
        from .review import own_teams_only as _oto
        from .learning import no_shared_rules as _nsr
        own_teams = _oto(_db, company)
        no_shared = _nsr(_db, company)
    return {"company": company, "engine_tag": engine_tag_on(company),
            "payment_autocode": auto, "auto_strong": strong, "auto_sth": sth,
            "bill_fix_fees": fees, "history_suggest": hist_on, "freeze_cols": freeze,
            "own_teams_only": own_teams, "no_shared_rules": no_shared}


@app.post("/setup/settings")
def save_engine_settings(company: str = Form(""),
                         engine_tag: str | None = Form(None),
                         payment_autocode: str | None = Form(None),
                         auto_strong: str | None = Form(None),
                         auto_sth: str | None = Form(None),
                         bill_fix_fees: str | None = Form(None),
                         history_suggest: str | None = Form(None),
                         freeze_cols: str | None = Form(None),
                         own_teams_only: str | None = Form(None),
                         no_shared_rules: str | None = Form(None),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_admin)):
    """Save one of THIS COMPANY's settings -- whichever was sent. Admins only:
    both change what is written to the company's QuickBooks."""
    from .models_db import AppSettingRow
    from .integrations.qbo_write import engine_tag_key, _TAG_CACHE
    if not company:
        raise HTTPException(400, "Which company? Settings are per company.")
    yes = lambda v: str(v).lower() in ("1", "true", "yes", "on")
    out = {"company": company}
    if engine_tag is not None:
        on = yes(engine_tag)
        db.merge(AppSettingRow(key=engine_tag_key(company),
                               value="on" if on else "off"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Append {{Engine}} to QuickBooks memos: "
                               f"{'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        _TAG_CACHE.pop(company, None)
        out["engine_tag"] = on
    if payment_autocode is not None:
        on = yes(payment_autocode)
        db.merge(AppSettingRow(key=f"payment_autocode:{company}",
                               value="true" if on else "false"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Code card payments automatically to the primary "
                               f"checking account: {'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["payment_autocode"] = on
    if auto_strong is not None:
        # A LEVEL now: Strong & Above, Very Strong & Above, Perfect, or off.
        # "true" (the old on/off) still means Strong & Above.
        v = str(auto_strong).strip().lower()
        # Moderate is not offered for automatic categorizing: Strong is the floor.
        v = "strong" if v in ("true", "1", "yes", "on", "moderate") else v
        from .bands import auto_levels as _alv
        _names = dict(_alv())
        v = v if v in _names else ""
        _names[""] = "off"
        from .review import AUTO_STRONG_KEY
        db.merge(AppSettingRow(key=AUTO_STRONG_KEY + company, value=v))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Automatically categorize matches: {_names[v]}",
                        dry_run=False, actor=user.username))
        out["auto_strong"] = v
    if auto_sth is not None:
        on = yes(auto_sth)
        from .review import AUTO_STH_KEY
        db.merge(AppSettingRow(key=AUTO_STH_KEY + company,
                               value="true" if on else "false"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Automatically categorize STH charges: "
                               f"{'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["auto_sth"] = on
    if bill_fix_fees is not None:
        on = yes(bill_fix_fees)
        db.merge(AppSettingRow(key=f"bill_fix_fees:{company}",
                               value="true" if on else "false"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Correct bills with an Other Fees line: "
                               f"{'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["bill_fix_fees"] = on
    if history_suggest is not None:
        on = yes(history_suggest)
        from .history import SETTING as _HS, _CACHE as _HC
        db.merge(AppSettingRow(key=_HS + company, value="true" if on else "false"))
        _HC.pop(company, None)
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Suggest vendor and category from past coding: {'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["history_suggest"] = on
    if freeze_cols is not None:
        on = yes(freeze_cols)
        db.merge(AppSettingRow(key=f"freeze_cols:{company}", value="true" if on else "false"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Freeze Action and Card Account columns: {'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["freeze_cols"] = on
    if own_teams_only is not None:
        on = yes(own_teams_only)
        from .review import OWN_TEAMS_KEY as _OTK
        db.merge(AppSettingRow(key=_OTK + company, value="true" if on else "false"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Ignore the Dashboard's Teams (use only this company's): {'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["own_teams_only"] = on
    if no_shared_rules is not None:
        on = yes(no_shared_rules)
        from .learning import NO_SHARED_KEY as _NSK
        db.merge(AppSettingRow(key=_NSK + company, value="true" if on else "false"))
        db.add(AuditRow(company=company, action="setting_saved",
                        detail=f"Exclude from Multi-Company Bank Feed Rules: {'on' if on else 'off'}",
                        dry_run=False, actor=user.username))
        out["no_shared_rules"] = on
    db.commit()
    return out


@app.get("/setup/program-keys")
def list_program_keys(company: str = Query(...),
                      db: Session = Depends(get_session),
                      user: UserRow = Depends(require_admin)):
    """Every Divvy and Taekus ACCOUNT this company has, with its key's status.

    The accounts are the ones API Card Connections set up -- which company,
    which card source -- and those lists stay the authority on routing. The
    key for each one is kept here, matched by the account's key_var. The key
    itself never leaves the server: only whether one is set, and its last four.
    """
    from . import divvy_sync as _dv, taekus_sync as _tk, gr_sync as _grs
    from .models_db import CardConnectionRow as _CC
    # PUT BACK any account that has a key here but has fallen off its
    # program's list -- which is what disconnecting on API Card Connections
    # used to do, taking the account off this screen with it. It returns
    # unrouted, description and key intact, ready to be routed again.
    for prog, mod in (("divvy", _dv), ("taekus", _tk), ("globalrewards", _grs)):
        listed = {c.get("key_var") for c in mod.connections(db)}
        lost = [r for r in db.query(_CC).filter(_CC.company == company,
                                                _CC.program == prog).all()
                if (r.key_var or "").strip() and r.key_var not in listed
                and r.api_key]
        if lost:
            rows = mod.connections(db)
            rows += [{"company": company, "key_var": r.key_var, "source": ""}
                     for r in lost]
            mod.save_connections(db, rows)
            db.commit()
    out = []
    for prog, mod in (("divvy", _dv), ("taekus", _tk), ("globalrewards", _grs)):
        for c in mod.connections(db, company):
            kv = (c.get("key_var") or "").strip()
            if not kv:
                continue
            row = (db.query(_CC).filter(_CC.program == prog,
                                        _CC.key_var == kv).first())
            out.append({
                "program": prog, "key_var": kv,
                "source": c.get("source") or "",
                "label": (row.label if row else "") or "",
                "has_key": bool(row and row.api_key),
                "key_hint": _conn_key_hint(row.api_key) if row else "",
            })
    return {"company": company, "accounts": out}


@app.post("/setup/connectors/remove")
def remove_connector(company: str = Form(...), kind: str = Form(""), program: str = Form(""),
                     key_var: str = Form(""), db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """THE RED X on a Connectors row: remove the connector -- what that means
    depends on its kind. Recorded in the Audit Log."""
    from .models_db import AppSettingRow, CardConnectionRow as _CC, CompanyVariableRow as _CV
    import json as _jr
    what = kind or program
    def drop_added(k):
        row = db.get(AppSettingRow, f"connectors_added:{company}")
        try:
            have = _jr.loads(row.value) if row and row.value else []
        except ValueError:
            have = []
        if k in have:
            db.merge(AppSettingRow(key=f"connectors_added:{company}", value=_jr.dumps([x for x in have if x != k])))
    def clear_vars(*names):
        for n in names:
            r = db.query(_CV).filter(_CV.company == company, _CV.name == n).first()
            if r is not None:
                db.delete(r)
    if program in ("divvy", "globalrewards", "taekus") and key_var:
        from . import divvy_sync as _dv, gr_sync as _gr, taekus_sync as _tk
        mod = {"divvy": _dv, "globalrewards": _gr, "taekus": _tk}[program]
        mod.save_connections(db, [c for c in mod.connections(db)
                                  if not (c.get("company") == company and c.get("key_var") == key_var)])
        for r in db.query(_CC).filter(_CC.program == program, _CC.key_var == key_var).all():
            db.delete(r)
        what = f"{program} account {key_var}"
    elif kind in ("airtable", "google", "onedrive"):
        # THAT APP'S SOURCES GO WITH IT; the others stay. The setup used to be
        # cleared whole, which would now also wipe a broker's Google Sheets or
        # OneDrive sources when only Airtable was removed.
        import json as _jr
        prov = {"airtable": "airtable", "google": "gsheets", "onedrive": "onedrive"}[kind]
        with _cv.for_company(db, company):
            before = {k: _cv.airtable_sources(k) for k in ("book", "sth")}
            off = list((_cv.airtable_setup() or {}).get("off") or [])
        keep = {k: [x for x in before[k] if x.get("provider") != prov] for k in ("book", "sth")}
        # A PART LEFT WITH NO SOURCE IS WIPED, as the Broker Data x does: its
        # records and sync details go, and no built-in layout steps in.
        # Removing Airtable used to leave all of that behind.
        for k in ("book", "sth"):
            if not keep[k] and (before[k] or (kind == "airtable" and k not in off)):
                _wipe_part(db, company, k)
                if k not in off:
                    off.append(k)
        keep["off"] = off
        _cv.set_value(db, company, "airtable_setup", _jr.dumps(keep), user.username)
        if kind == "airtable":
            clear_vars("airtable_token", "airtable_base_tickets", "airtable_base_accounting")
        elif kind == "google":
            clear_vars("google_service_account")
        else:
            clear_vars("microsoft_client_id", "microsoft_refresh_token", "microsoft_account")
        drop_added(kind)
    elif kind == "slash":
        clear_vars("slash_api_key")
        drop_added("slash")
    elif kind == "wex":
        drop_added("wex")
    elif kind == "quickbooks":
        # NOT REMOVABLE: the company's QuickBooks file is what everything posts
        # to, so the Connectors row has no red x and the request is refused.
        raise HTTPException(403, "QuickBooks cannot be removed from Connectors")
    else:
        raise HTTPException(400, "Nothing to remove")
    db.add(AuditRow(company=company, action="setting_saved", detail=f"Connector removed: {what}",
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True, "removed": what}


@app.post("/setup/program-keys")
def save_program_key(company: str = Form(...), program: str = Form(...),
                     key_var: str = Form(""), label: str | None = Form(None),
                     api_key: str = Form(""), clear_key: str = Form("false"),
                     db: Session = Depends(get_session),
                     user: UserRow = Depends(require_admin)):
    """Set, replace or clear the key for one Divvy or Taekus account."""
    from .models_db import CardConnectionRow as _CC
    from .secrets_store import encrypt as _enc
    if program not in ("divvy", "taekus", "globalrewards"):
        raise HTTPException(400, "Unknown program")
    kv = key_var.strip()
    if not kv:
        # A NEW ACCOUNT, added here first. Connectors is where an account
        # starts -- its description and its key -- and API Card Connections
        # then routes it to a card account. So it is registered in the
        # program's own list now, with no card account yet; the pulls leave
        # it alone until one is chosen, rather than pouring its charges into
        # a default account alongside another's.
        if not api_key.strip():
            raise HTTPException(400, "Paste the new account's key first")
        from . import divvy_sync as _dv2, taekus_sync as _tk2, gr_sync as _gr2
        import uuid as _u2
        mod = {"divvy": _dv2, "taekus": _tk2, "globalrewards": _gr2}[program]
        kv = f"{program.upper()}_{_u2.uuid4().hex[:8].upper()}"
        rows = mod.connections(db)          # every company's, not just this one
        rows.append({"company": company, "key_var": kv, "source": ""})
        mod.save_connections(db, rows)
    row = (db.query(_CC).filter(_CC.program == program,
                                _CC.key_var == kv).first())
    if row is None:
        import uuid as _uuid
        row = _CC(id=str(_uuid.uuid4()), company=company, program=program,
                  key_var=kv, is_active=True)
        db.add(row)
    if label is not None:
        row.label = label.strip()
    cleared = str(clear_key).lower() in ("1", "true", "yes", "on")
    if cleared:
        row.api_key = ""
    elif api_key.strip():
        row.api_key = _enc(api_key.strip())
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.add(AuditRow(company=company, action="card_connection_saved",
                    actor=user.username, dry_run=False,
                    # Never the key itself, only what happened to it.
                    detail=f"{program}: {row.label or kv} "
                           + ("key cleared" if cleared
                              else "key set" if api_key.strip()
                              else "description updated")))
    db.commit()
    return {"ok": True}


@app.get("/setup/connections")
def list_card_connections(company: str = Query(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_reviewer)):
    """This company's card-program connections.

    The key never comes back, for the same reason a variable's does not: the
    screen needs to know one is set and to be able to replace it, and nothing
    more.
    """
    from . import company_vars
    rows = company_vars.connections(db, company)
    return {"company": company, "connections": [{
        "id": r.id, "program": r.program, "label": r.label,
        "has_key": bool(r.api_key), "key_var": r.key_var or "",
        # The last four characters, masked -- enough to tell which key is
        # stored, and useless to anybody else. Never the key itself.
        "key_hint": _conn_key_hint(r.api_key),
        "legal_entity_id": r.legal_entity_id or "",
        "account_id": r.account_id or "",
        "lookback_days": r.lookback_days or 0,
        "is_active": r.is_active is not False,
        "updated_by": r.updated_by or "",
    } for r in rows]}


@app.post("/setup/connections")
def save_card_connection(company: str = Form(...), program: str = Form(...),
                         label: str = Form(""), api_key: str = Form(""),
                         # None means "not sent": leave the stored value
                         # alone. See the assignments below.
                         legal_entity_id: str | None = Form(None),
                         account_id: str | None = Form(None),
                         lookback_days: str | None = Form(None),
                         key_var: str | None = Form(None),
                         is_active: str | None = Form(None),
                         clear_key: str = Form("false"),
                         conn_id: str = Form(""),
                         delete: str = Form("false"),
                         db: Session = Depends(get_session),
                         user: UserRow = Depends(require_admin)):
    """Add, change or remove one connection. Admin only.

    These decide which Slash account is pulled into which company, so a wrong
    one imports another company's charges -- not something a reviewer should be
    able to do in passing.
    """
    import uuid as _uuid
    from .models_db import CardConnectionRow, CompanyRow
    if not db.query(CompanyRow).filter(CompanyRow.name == company).first():
        raise HTTPException(404, f"{company!r} is not a company in this engine")
    if program not in ("slash", "divvy", "taekus"):
        raise HTTPException(400, "program must be slash, divvy or taekus")

    row = db.get(CardConnectionRow, conn_id) if conn_id else None
    if str(delete).lower() in ("1", "true", "yes"):
        if row is None:
            raise HTTPException(404, "no such connection")
        db.add(AuditRow(company=company, action="card_connection_removed",
                        actor=user.username, dry_run=False,
                        detail=f"{row.program}: {row.label or '(unnamed)'}"))
        db.delete(row)
        db.commit()
        return {"ok": True, "deleted": True}

    created = row is None
    if created:
        row = CardConnectionRow(id=str(_uuid.uuid4()), company=company,
                                program=program)
    row.label = label.strip()
    # An EMPTY key leaves the stored one alone. The screen sends nothing for a
    # key it did not touch, and treating that as "clear it" would wipe the
    # credential every time somebody renamed a connection.
    if api_key.strip():
        # ENCRYPTED, like every other key the engine holds. A connection's key
        # reads card data and moves money exactly as the company-wide ones do,
        # and it must not be readable in a database backup either.
        from .secrets_store import encrypt as _enc_key
        row.api_key = _enc_key(api_key.strip())
    # AN EXPLICIT CLEAR, separate from "no key sent". The red X on Connectors
    # sends this; an empty key box sends nothing and leaves the key alone.
    if str(clear_key).lower() in ("1", "true", "yes", "on"):
        row.api_key = ""
    # ONLY WHAT WAS SENT CHANGES. Two screens save connections: API Card
    # Connections sends every field, Connectors sends only the description and
    # key. When every field was assigned unconditionally, saving a description
    # on Connectors wiped the Slash entity and account, wiped key_var -- which
    # is how a pull finds this connection -- and switched a retired connection
    # back on. A field that was not sent keeps its value.
    if legal_entity_id is not None:
        row.legal_entity_id = legal_entity_id.strip()
    if account_id is not None:
        row.account_id = account_id.strip()
    if key_var is not None:
        row.key_var = key_var.strip()
    if lookback_days is not None:
        try:
            row.lookback_days = int(float(lookback_days or 0))
        except ValueError:
            row.lookback_days = 0
    if is_active is not None:
        row.is_active = str(is_active).lower() in ("1", "true", "yes", "on")
    elif created:
        row.is_active = True
    row.updated_by = user.username
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(
        company=company,
        action="card_connection_saved", actor=user.username, dry_run=False,
        # The key is never written to the audit log, only the fact of it.
        detail=f"{program}: {row.label or '(unnamed)'}"
               + (" created" if created else " updated")
               + (", key set" if api_key.strip() else "")))
    db.commit()
    return {"ok": True, "id": row.id}


@app.get("/setup/variables")
def list_company_variables(company: str = Query(...),
                           db: Session = Depends(get_session),
                           user: UserRow = Depends(require_reviewer)):
    """This company's settings, and what each would fall back to."""
    from . import company_vars
    return {"company": company, "variables": company_vars.listing(db, company)}


@app.post("/setup/variables")
def save_company_variables(company: str = Form(...), values: str = Form(...),
                           db: Session = Depends(get_session),
                           user: UserRow = Depends(require_admin)):
    """Save one company's overrides.

    Admin only. These decide which Airtable base is mirrored and which Slash
    account is pulled, so getting them wrong pulls another company's data into
    this one -- not a thing a reviewer should be able to do in passing.

    A blank value CLEARS the override rather than storing an empty string: a
    person emptying a box means "use the deployed default", not "this company
    has no Airtable token".
    """
    import json as _json
    from . import company_vars
    from .models_db import CompanyRow
    if not db.query(CompanyRow).filter(CompanyRow.name == company).first():
        raise HTTPException(404, f"{company!r} is not a company in this engine")
    try:
        incoming = _json.loads(values)
    except ValueError:
        raise HTTPException(400, "values must be a JSON object")
    if not isinstance(incoming, dict):
        raise HTTPException(400, "values must be a JSON object")

    changed = []
    for name, val in incoming.items():
        spec = company_vars.BY_NAME.get(name)
        if spec is None:
            # Ignored rather than refused: a stale browser sending a setting
            # that has since been removed should not fail the whole save.
            continue
        before = db.get(__import__("app.models_db", fromlist=["x"])
                        .CompanyVariableRow, f"{company}|{name}")
        was = "" if before is None else before.value
        company_vars.set_value(db, company, name, val, user.username)
        if str(val or "").strip() != was:
            # The VALUE of a secret is never written to the audit log, for the
            # same reason it is never sent to the browser. That it changed, who
            # changed it and when is the whole of what an audit needs.
            changed.append(name if spec["secret"]
                           else f"{name}: '{was or '(default)'}' changed to "
                                f"'{str(val).strip() or '(default)'}'")
    if changed:
        db.add(AuditRow(company=company, action="company_variable_set",
                        actor=user.username, dry_run=False,
                        detail="; ".join(changed)[:900]))
    db.commit()
    return {"saved": len(changed),
            "variables": company_vars.listing(db, company)}


@app.get("/setup/companies")
def list_companies_admin(db: Session = Depends(get_session),
                         user: UserRow = Depends(require_reviewer)):
    """Every company, with the QuickBooks file each is mapped to.

    The realms actually CONNECTED are listed alongside, because the mapping is
    only meaningful against them -- a company pointed at a realm with no token
    is a company that cannot post, and saying so here is cheaper than finding
    out at post time.
    """
    from .models_db import ChargeRow, CompanyRow, QboTokenRow
    from sqlalchemy import func as _f
    toks = {t.realm_id: t for t in db.query(QboTokenRow).all()}
    counts = dict(db.query(ChargeRow.company, _f.count())
                  .group_by(ChargeRow.company).all())
    rows = db.query(CompanyRow).order_by(CompanyRow.name).all()
    from . import membership as _mb
    _mine = _mb.companies_for(db, user)
    if _mine is not None:
        rows = [r for r in rows if r.name in set(_mine)]
    return {
        "companies": [{
            "id": c.id, "name": c.name, "realm_id": c.realm_id or "",
            "qbo_company_name": c.qbo_company_name or "",
            "active": c.active is not False,
            "connected": bool(c.realm_id) and c.realm_id in toks,
            "charges": counts.get(c.name, 0),
        } for c in rows],
        "realms": [{"realm_id": r, "name": t.company_name or r}
                   for r, t in sorted(toks.items())],
    }


@app.post("/setup/companies")
def save_company(name: str = Form(...), realm_id: str = Form(""),
                 active: str = Form("true"), rename_to: str = Form(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Create a company, map it to a QuickBooks file, or retire it.

    The NAME is the key every charge, bill, card mapping and rule is filed
    under, so renaming is a real operation rather than an edit: everything
    filed under the old name has to move with it, or the queue silently shows
    nothing. It is done here, in one transaction, rather than left to whoever
    notices.
    """
    from .models_db import (AuditRow as _AR, BillRow, ChargeRow, CompanyRow,
                            LearnedRuleRow, SourceAccountRow)
    name = name.strip()
    if not name:
        raise HTTPException(400, "a company name is required")

    row = db.query(CompanyRow).filter(CompanyRow.name == name).first()
    created = row is None
    if created:
        row = CompanyRow(id=name, name=name)

    if realm_id.strip():
        # Two companies pointing at one QuickBooks file would post both sets of
        # charges into the same ledger, which is the exact failure the realm
        # mapping exists to prevent.
        clash = (db.query(CompanyRow)
                 .filter(CompanyRow.realm_id == realm_id.strip(),
                         CompanyRow.name != name).first())
        if clash is not None:
            raise HTTPException(
                409, f"that QuickBooks file is already mapped to {clash.name}")
        from .models_db import QboTokenRow
        tok = db.get(QboTokenRow, realm_id.strip())
        row.realm_id = realm_id.strip()
        row.qbo_company_name = (tok.company_name if tok else "") or ""
    elif realm_id == "":
        row.realm_id = ""
        row.qbo_company_name = ""

    row.active = str(active).lower() in ("1", "true", "yes", "on")
    db.add(row)
    db.flush()

    moved = 0
    new_name = rename_to.strip()
    if new_name and new_name != name:
        if db.query(CompanyRow).filter(CompanyRow.name == new_name).first():
            raise HTTPException(409, f"{new_name!r} already exists")
        for model in (ChargeRow, BillRow, SourceAccountRow, LearnedRuleRow, _AR):
            moved += (db.query(model).filter(model.company == name)
                      .update({model.company: new_name},
                              synchronize_session=False))
        from .models_db import CompanyVariableRow
        for v in db.query(CompanyVariableRow).filter(
                CompanyVariableRow.company == name).all():
            v.company = new_name
            v.id = f"{new_name}|{v.name}"
            db.add(v)
        row.name = new_name
        row.id = new_name
        db.add(row)

    db.add(_AR(company=row.name, action="company_saved", actor=user.username,
               dry_run=False,
               detail=("created" if created else "updated")
                      + (f"; renamed from {name}, {moved} rows moved"
                         if new_name and new_name != name else "")
                      + (f"; mapped to QuickBooks {row.qbo_company_name or row.realm_id}"
                         if row.realm_id else "; not mapped to QuickBooks")))
    db.commit()
    return {"ok": True, "name": row.name, "moved": moved}


@app.post("/qbo/refs/sync")
def sync_qbo_refs_now(db: Session = Depends(get_session),
                      user: UserRow = Depends(require_write)):
    """Re-read the chart of accounts and the vendor list from QuickBooks."""
    from .worker import sync_qbo_reference
    try:
        n = sync_qbo_reference()
    except Exception as e:                                      # noqa: BLE001
        raise HTTPException(400, str(e)[:300])
    from .review import reset_account_cache
    reset_account_cache()
    return {"synced": n}


@app.get("/admin/build")
def admin_build(db: Session = Depends(get_session),
                user: UserRow = Depends(require_reviewer)):
    """A per-file hash of the deployed application.

    code_build answers "does the whole tree match" with one number, which is
    the right question most of the time and useless when the answer is no: it
    says something is stale without saying what. This lists every file, so a
    partial deploy can be diffed against the package that was meant to be
    installed instead of narrowed down by guesswork.

    Content only -- no source, no configuration, nothing that is not already
    determined by which release is running.
    """
    import hashlib
    root = pathlib.Path(__file__).parent.parent
    files, folders = {}, {}
    for sub in ("app", "engine", "migrations"):
        h = hashlib.md5()
        for f in sorted((root / sub).rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            rel = str(f.relative_to(root))
            body = f.read_bytes()
            files[rel] = hashlib.md5(body).hexdigest()[:8]
            h.update(rel.encode())
            h.update(body)
        folders[sub] = h.hexdigest()[:8]
    try:
        # ALL THREE static files, not just index.html. The markup is now a
        # tenth of the front end; hashing it alone would report a matching
        # ui_build while the script it loads was from another deploy.
        _h = hashlib.md5()
        for _n in ("index.html", "app.css", "app.js"):
            _f = STATIC / _n
            if _f.exists():
                _h.update(_n.encode())
                _h.update(_f.read_bytes())
        ui = _h.hexdigest()[:8]
    except Exception:                                           # noqa: BLE001
        ui = "unknown"
    return {"ui_build": ui, "folders": folders,
            "file_count": len(files), "files": files}


# --- Divvy / BILL Spend & Expense -------------------------------------------
#
# One API key PER COMPANY, unlike Slash's single key across many entities. What
# is stored here is the NAME of the environment variable holding each company's
# token -- never the token. A secret in the database is a secret in every
# backup and every export; a variable name is not a secret at all.

def _card_account_choices(db: Session, company: str) -> list:
    """The card accounts a pull can import into — the SAME list the grid shows.

    The grid is built from QuickBooks BANK and credit-card accounts, plus
    anything with history behind it. The Slash and Divvy pickers were built
    from SourceAccountRow instead, which only holds accounts somebody has
    already mapped -- so a bank account added in QuickBooks appeared on the
    grid and was missing from the picker, with nothing to explain the gap.
    """
    from .models_db import QboRefRow, SourceAccountRow
    mapped = {m.source: m for m in db.query(SourceAccountRow)
              .filter(SourceAccountRow.company == company).all()}
    by_qbo = {m.qbo_account_id: m for m in mapped.values() if m.qbo_account_id}
    out, seen = [], set()
    # THIS COMPANY'S QUICKBOOKS FILE. No realm filter here meant the picker
    # walked every connected file's bank accounts, so linking a Slash or Taekus
    # key in one company offered card accounts belonging to another -- and
    # picking one would have pointed a pull at a different company's ledger.
    _realm = _current_realm(db, company)
    for a in db.query(QboRefRow).filter(
            QboRefRow.kind == "account", QboRefRow.usable_for == "bank",
            _eq_or_none(QboRefRow.realm_id, _realm),
            QboRefRow.active.isnot(False)).all():
        m = by_qbo.get(a.qbo_id)
        src = m.source if m else f"qbo:{a.qbo_id}"
        # THE NICKNAME FIRST, so this picker reads the same as the grid and
        # the Bank Feed. It used to lead with the QuickBooks account name, on
        # the reasoning that a rename there should show through -- which is
        # true, and is why the grid shows both. Leading with it here meant the
        # picker offered "Prepaid Inventory:NHL" where every other list said
        # "Divvy (Credit)".
        # A stored name that came from ANOTHER company's file (same account
        # number, different file) is not this account's name; this file's
        # own name is used instead.
        _foreign = bool(m and m.realm_id and _realm and m.realm_id != _realm)
        out.append({"source": src,
                    "name": ((m.nickname if (m and not _foreign) else "")
                             or a.name or src),
                    "type": a.account_type or ""})
        seen.add(src)
    # Sources with history but no ACTIVE QuickBooks account behind them.
    #
    # Retired accounts leak in here rather than through the loop above: that
    # one walks active accounts only, so a mapped source whose account has
    # since been retired never reaches `seen`, and this fallback -- which
    # exists to keep sources that have history -- put it straight back.
    #
    # SourceAccountRow carries no account TYPE, only the id and the name, so
    # the type is left blank rather than read from a column that is not there.
    retired_ids = {r.qbo_id for r in db.query(QboRefRow).filter(
        QboRefRow.kind == "account",
        _eq_or_none(QboRefRow.realm_id, _realm),
        QboRefRow.active.is_(False)).all()}
    for src, m in mapped.items():
        if src in seen:
            continue
        if m.qbo_account_id and m.qbo_account_id in retired_ids:
            continue
        out.append({"source": src,
                    "name": m.nickname or m.qbo_account_name or src,
                    "type": ""})
    # Type first, then name -- the order the grid uses.
    return sorted(out, key=lambda x: ((x["type"] or "").lower(),
                                      (x["name"] or "").lower()))


_DIVVY_OK: dict = {}


@app.get("/divvy/status")
def divvy_status(company: str = Query(""), db: Session = Depends(get_session),
                 user: UserRow = Depends(require_reviewer)):
    """Every Divvy account configured for this company, and whether each works.

    A LIST, because one company can hold several Divvy accounts -- Y&S runs
    two. Each has its own token variable and its own card account, and one
    expired token must be visible as that account's problem rather than as the
    company's.
    """
    from .integrations import divvy_api
    from .divvy_sync import connections
    from .models_db import SourceAccountRow as _SA, UploadRow as _UR

    accounts = _card_account_choices(db, company) if company else []
    # Names only, never values, so somebody can pick the right variable without
    # typing it from memory.
    candidates = _unrouted_accounts(db, company, "divvy",
                                    connections(db, company))

    out = []
    for c in connections(db, company):
        token = divvy_api.token_for(c["key_var"])
        live, err = False, ""
        if token:
            try:
                # Remembered for ten minutes per token: a key that answered a
                # moment ago does not need asking again on every open.
                import hashlib as _hd, time as _td
                _ck = _hd.sha256(token.encode()).hexdigest()[:16]
                _hit = _DIVVY_OK.get(_ck)
                if not (_hit and _td.time() - _hit < 600):
                    divvy_api.whoami(token)
                    _DIVVY_OK[_ck] = _td.time()
                live = True
            except Exception as e:                              # noqa: BLE001
                err = _readable_error(str(e))
        else:
            # Keys live on Connectors; the environment is no longer read.
            err = "no key entered on Connectors for this account"
        last = ""
        r = (db.query(_UR).filter(_UR.company == company, _UR.method == "pull",
                                  _UR.source == c.get("source", ""))
             .order_by(_UR.at.desc()).first())
        if r and r.at:
            last = r.at.isoformat() + "Z"
        out.append({"key_var": c["key_var"], "source": c.get("source", ""),
                    "label": _account_label(db, "divvy", c["key_var"]),
                    "source_name": next(
                        (a["name"] for a in accounts
                         if a["source"] == c.get("source")), c.get("source", "")),
                    "has_token": bool(token), "token_works": live,
                    "error": err, "last_pull_at": last})
    from .divvy_sync import progress_for
    from .models_db import AppSettingRow as _ASF
    prog = progress_for(company)
    for c in out:
        c["progress"] = prog.get(c["key_var"], {})
        # The last run's field list, from settings rather than memory -- see
        # divvy_sync. A deploy between the pull and the question used to erase
        # it, which is every time.
        try:
            _r = db.get(_ASF, f"divvy_fields:{company}:{c['key_var']}")
            if _r and _r.value:
                c["fields_seen"] = json.loads(_r.value)
        except Exception:                                       # noqa: BLE001
            pass
    return {"company": company, "connections": out,
            "running": any(p.get("running") for p in prog.values()),
            "key_var_choices": candidates,
            # Only accounts nothing else feeds — see _free_card_accounts. The
            # connections already listed above keep their own account, which is
            # shown on their row rather than in the picker.
            "card_accounts": _free_card_accounts(db, company) if company
            else accounts}


def _unrouted_accounts(db, company: str, program: str, conns: list) -> list:
    """Accounts added on Connectors that still need a card account.

    What API Card Connections offers to route. It used to list Railway
    variable names containing DIVVY or TAEKUS; keys live on Connectors now, so
    it offers the accounts created there, by the description given to them.
    """
    from .models_db import CardConnectionRow as _CCu
    waiting = [c for c in conns if not c.get("source")]
    out = []
    for c in waiting:
        row = (db.query(_CCu).filter(_CCu.program == program,
                                     _CCu.key_var == c["key_var"]).first())
        out.append({"value": c["key_var"],
                    "label": (row.label if row and row.label else c["key_var"])})
    return out


def _account_label(db, program: str, key_var: str) -> str:
    """The description given to an account on Connectors, or "".

    Shown in place of its handle: an account added on Connectors gets one like
    DIVVY_6F50A82E, which tells nobody which account it is.
    """
    from .models_db import CardConnectionRow as _CCl
    row = (db.query(_CCl).filter(_CCl.program == program,
                                 _CCl.key_var == key_var).first())
    return (row.label or "") if row else ""


# --- Global Rewards ----------------------------------------------------------
@app.get("/gr/status")
def gr_status(company: str = Query(...), db: Session = Depends(get_session),
              user: UserRow = Depends(require_reviewer)):
    """This company's Global Rewards accounts: routing, cards, last run."""
    import json as _jg
    from . import gr_sync as _g
    from .models_db import AppSettingRow as _ASg
    out = []
    for c in _g.connections(db, company):
        lr = db.get(_ASg, f"gr_last_run:{company}:{c['key_var']}")
        try:
            lr = _jg.loads(lr.value) if lr else {}
        except ValueError:
            lr = {}
        out.append({"key_var": c["key_var"], "source": c.get("source", ""),
                    "label": _account_label(db, "globalrewards", c["key_var"]),
                    "has_key": bool(_g.token_for(c["key_var"])),
                    "cards": c.get("cards") or [],
                    "cards_text": "\n".join(", ".join(x for x in (k.get("id", ""), k.get("last4", ""),
                                                             k.get("holder", "")) if x)
                                            for k in (c.get("cards") or [])),
                    "last_run": lr})
    return {"company": company, "accounts": out,
            "card_accounts": _free_card_accounts(db, company) if company else []}


@app.post("/gr/config")
def gr_config(company: str = Form(...), key_var: str = Form(...),
              source: str = Form(""), cards: str = Form(""),
              keep_cards: str = Form(""),
              db: Session = Depends(get_session),
              user: UserRow = Depends(require_admin)):
    """Route one Global Rewards account, and set its card list."""
    from . import gr_sync as _g
    rows = _g.connections(db)
    hit = next((r for r in rows if r.get("company") == company
                and r.get("key_var") == key_var), None)
    if hit is None:
        raise HTTPException(404, "No such Global Rewards account here")
    if source.strip():
        if not _ensure_card_account(db, company, source.strip()):
            raise HTTPException(400, "No such card account")
        hit["source"] = source.strip()
    # A large list is kept as it is (it is changed by uploading a card list);
    # otherwise the box's contents are the list.
    if keep_cards.lower() not in ("1", "true", "yes"):
        hit["cards"] = _g.parse_cards(cards)
    _g.save_connections(db, rows)
    db.add(AuditRow(company=company, action="gr_configured",
                    detail=f"{key_var}: {len(hit['cards'])} card(s)"
                           + (f", into {hit.get('source')}" if hit.get("source") else ""),
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True, "cards": len(hit["cards"])}


@app.post("/gr/pull")
def gr_pull(company: str = Form(...), key_var: str = Form(""), since: str = Form(""),
            db: Session = Depends(get_session), user: UserRow = Depends(require_write)):
    """Pull one Global Rewards account now (or every one, with no key_var)."""
    from . import gr_sync as _g
    d = None
    if since.strip():
        try:
            d = datetime.strptime(since.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "since must be YYYY-MM-DD")
    conns = [c for c in _g.connections(db, company)
             if not key_var or c.get("key_var") == key_var]
    if not conns:
        raise HTTPException(404, "No such Global Rewards account here")
    # A LARGE CARD LIST is pulled in the background: one request per card, and
    # thousands of them would outlast the request itself.
    if sum(len(c.get("cards") or []) for c in conns) > 200:
        if d is None:
            from .scheduler import _start_gr_pull
            started = _start_gr_pull(company)
        else:
            # WITH A PULL FROM DATE TOO: it used to run inside the request,
            # every card in turn -- Y&S's 6,000 outlasted Railway's limit and
            # came back "upstream error". On a thread, like the plain pull.
            key = ("gr", company, key_var)
            started = key not in _SLASH_RUNNING
            if started:
                _SLASH_RUNNING.add(key)
                actor = user.username

                def _run():
                    from .db import SessionLocal
                    s2 = SessionLocal()
                    try:
                        with _cv.for_company(s2, company):
                            for c in conns:
                                _g.pull_one(s2, c, d, actor=actor)
                    except Exception:                           # noqa: BLE001
                        log.exception("Global Rewards pull failed for %s", company)
                    finally:
                        s2.close()
                        _SLASH_RUNNING.discard(key)
                threading.Thread(target=_run, daemon=True, name=f"gr-pull-{company}").start()
        return {"started": started, "new": 0, "results": [],
                "message": ("Pulling in the background -- the last pull line updates when it finishes."
                            if started else "A pull for this company is already running.")}
    results = [_g.pull_one(db, c, d, actor=user.username) for c in conns]
    return {"new": sum(r.get("new", 0) for r in results), "results": results}


@app.post("/gr/cards/preview")
async def gr_cards_preview(file: UploadFile = File(...), db: Session = Depends(get_session),
                           user: UserRow = Depends(require_admin)):
    """What a Global Rewards card list holds: how many cards per company in
    it, and the Global Rewards accounts they can go to -- each group's last
    choice remembered."""
    from . import gr_sync as _g
    try:
        cards = _g.read_card_sheet(await file.read())
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(400, f"That file could not be read: {e}")
    groups = {}
    for c in cards:
        groups[c["label"]] = groups.get(c["label"], 0) + 1
    reach = set(_reachable(db, user))
    accounts = [{"value": f"{c['company']}|{c['key_var']}",
                 "name": f"{c['company']} \u00b7 {_account_label(db, 'globalrewards', c['key_var'])}"}
                for c in _g.connections(db) if c.get("company") in reach]
    saved = _g.label_map(db)
    # Nothing saved for a group yet: the company Global Rewards' name means,
    # and that company's (first) Global Rewards account.
    for k in groups:
        if k not in saved:
            co = _g.DEFAULT_LABELS.get(k.strip().lower())
            acct = next((x["value"] for x in accounts if x["value"].split("|")[0] == co), "")
            if acct:
                saved[k] = acct
    return {"groups": [{"label": k, "cards": n, "to": saved.get(k, "")}
                       for k, n in sorted(groups.items(), key=lambda x: -x[1])],
            "accounts": sorted(accounts, key=lambda x: x["name"].lower())}


@app.post("/gr/cards/import")
async def gr_cards_import(file: UploadFile = File(...), assign: str = Form(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    """Load a Global Rewards card list: each of its companies' cards into the
    account chosen for it, merged by GR ID. The choices are remembered."""
    import json as _jc
    from . import gr_sync as _g
    from .models_db import AppSettingRow
    try:
        to = _jc.loads(assign)
        cards = _g.read_card_sheet(await file.read())
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(400, f"That file could not be read: {e}")
    reach = set(_reachable(db, user))
    result, skipped = {}, []
    for label in sorted({c["label"] for c in cards}):
        dest = (to.get(label) or "").strip()
        if not dest:
            skipped.append(label)
            continue
        co, _, kv = dest.partition("|")
        if co not in reach:
            raise HTTPException(403, f"You don't have access to {co}")
        try:
            r = _g.merge_cards(db, co, kv, [c for c in cards if c["label"] == label])
        except ValueError as e:
            raise HTTPException(400, str(e))
        result[label] = {"company": co, **r}
        db.add(AuditRow(company=co, action="gr_cards_loaded",
                        detail=f"{r['added']} added, {r['updated']} updated from "
                               f"\"{label}\" in {file.filename}",
                        dry_run=False, actor=user.username))
    saved = _g.label_map(db)
    saved.update({k: v for k, v in to.items() if v})
    db.merge(AppSettingRow(key=_g.LABEL_MAP_KEY, value=_jc.dumps(saved)))
    db.commit()
    return {"loaded": result, "skipped": skipped}


@app.post("/gr/remove")
def gr_remove(company: str = Form(...), key_var: str = Form(...),
              db: Session = Depends(get_session), user: UserRow = Depends(require_admin)):
    """Stop pulling one account. Like Divvy: the routing goes, the account and
    its key stay on Connectors."""
    from . import gr_sync as _g
    rows = _disconnect_rows(db, "globalrewards", company, key_var, _g.connections(db))
    _g.save_connections(db, rows)
    db.add(AuditRow(company=company, action="gr_removed", detail=key_var,
                    dry_run=False, actor=user.username))
    db.commit()
    return {"ok": True}


@app.post("/divvy/config")
def divvy_config(company: str = Form(...), key_var: str = Form(...),
                 source: str = Form(...), db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Add or update ONE Divvy account. Keyed by the variable name.

    Per account rather than per company, because a company can have several and
    saving the whole set would let two people editing different accounts
    overwrite each other.
    """
    from .models_db import SourceAccountRow
    from .divvy_sync import connections, save_connections, CONNECTIONS_KEY
    var, src = key_var.strip(), source.strip()
    if not var or not src:
        raise HTTPException(400, "both a key variable and a card account are needed")
    # Validated against the SAME list the picker offers. A QuickBooks bank
    # account nobody has mapped yet has no SourceAccountRow, so checking that
    # table alone rejected exactly the accounts this was meant to allow.
    if src not in {a["source"] for a in _card_account_choices(db, company)}:
        raise HTTPException(400, f"'{company}' has no card account '{src}'")
    # Refused here rather than at pull time, when whoever could fix it has
    # moved on to something else.
    # The key on CONNECTORS, not a Railway variable: this was the one
    # check left reading the environment, so routing an account added
    # on Connectors was refused as if it had no key.
    from .integrations import divvy_api as _dvf
    if not _dvf.token_for(var):
        raise HTTPException(
            400, "No key has been entered for this account on Connectors "
                 "\u2014 enter it there first, then choose it here")
    # A source like "qbo:874" is a QuickBooks account nobody has mapped yet.
    # Left as that string, every charge imports under a key with no name, and
    # the card picker shows "qbo:874" where a card account should be. Give it a
    # real SourceAccountRow now, named from QuickBooks, so it behaves like every
    # other card account from its first charge onward.
    if src.startswith("qbo:"):
        from .models_db import QboRefRow as _QR
        # THIS COMPANY'S QuickBooks file only. Account ids are numbered per
        # file, so "874" exists in several; looked up by id alone it took the
        # first match -- another company's -- and named a Divvy card account
        # "Prepaid Inventory:NFL:Washington Commanders (TC)", which is why the
        # card picker could not find it by searching "divvy".
        from .review import _company_realm as _crealm
        _realm = _crealm(db, company)
        ref = (db.query(_QR).filter(_QR.kind == "account",
                                    _QR.qbo_id == src.split(":", 1)[1],
                                    _QR.realm_id == _realm).first()
               if _realm else None)
        if ref is not None and not db.query(SourceAccountRow).filter(
                SourceAccountRow.company == company,
                SourceAccountRow.source == src).first():
            db.add(SourceAccountRow(
                id=f"{company}:{src}", company=company, source=src,
                nickname=ref.name or src, qbo_account_id=ref.qbo_id,
                qbo_account_name=ref.name or "", realm_id=ref.realm_id or ""))
            db.commit()

    rows = [r for r in connections(db)
            if not (r.get("company") == company and r.get("key_var") == var)]
    # Two accounts on one company must not both import into the SAME card
    # account: their charges would be indistinguishable afterwards, and the
    # last four is not enough to tell two Divvy programs apart.
    clash = next((r for r in rows if r.get("company") == company
                  and r.get("source") == src), None)
    owner = _feed_owner(db, company, src, ignore=(("divvy", var),))
    if owner:
        raise HTTPException(
            400, f"{owner} already imports into that card account — give this "
                 f"one its own, or the two feeds' charges cannot be told apart")
    rows.append({"company": company, "key_var": var, "source": src})
    save_connections(db, rows)
    db.add(AuditRow(company=company, action="divvy_configured",
                    detail=f"{var} assigned to {src}", actor=user.username))
    db.commit()
    return divvy_status(company=company, db=db, user=user)


def _readable_error(raw: str) -> str:
    """A provider's error as a sentence. Divvy answers with a list of records
    -- [{'timestamp': '2026-10-05T20:41...', 'message': 'Unauthorized', ...}] --
    and that whole block was shown in Last Result. Its message (and status, if
    given) is what a person can act on."""
    import re as _re_e
    raw = str(raw or "")
    msgs = _re_e.findall(r"""['"](?:message|error_description|detail|error)['"]\s*:\s*['"]([^'"]{2,200})['"]""", raw)
    code = _re_e.search(r"""['"](?:status|code|statusCode)['"]\s*:\s*['"]?(\d{3})""", raw)
    if msgs:
        out = "; ".join(dict.fromkeys(m.strip() for m in msgs))
        if code and code.group(1) not in out:
            out = f"{out} ({code.group(1)})"
        if code and code.group(1) in ("401", "403") or _re_e.search(r"unauthori|forbidden|invalid.*(key|token)", out, _re_e.I):
            out += " \u2014 the key was refused. Check it on Configuration \u203a Connectors."
        return out[:300]
    return raw[:400]


def _disconnect_rows(db, program: str, company: str, key_var: str,
                     rows: list) -> list:
    """The program's account list after disconnecting ONE account.

    Disconnecting on API Card Connections undoes the ROUTING -- which card
    account the charges land in -- not the account itself. An account whose key
    lives on Connectors stays in the list, unrouted, so it stays on Connectors
    with its description and key and can be routed again. Deleting it outright
    also wiped it off Connectors, where it had been created and keyed.

    An account with no key on Connectors (one that predates it) is removed, as
    before: there is nothing on Connectors for it to stay as.
    """
    from .models_db import CardConnectionRow as _CCd
    kv = key_var.strip()
    has_key = db.query(_CCd).filter(_CCd.program == program,
                                    _CCd.key_var == kv).first() is not None
    out = []
    for r in rows:
        if r.get("company") == company and r.get("key_var") == kv:
            if has_key:
                out.append({**r, "source": ""})
            continue
        out.append(r)
    return out


@app.post("/divvy/remove")
def divvy_remove(company: str = Form(...), key_var: str = Form(...),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Stop pulling one Divvy account. Charges already imported are untouched."""
    from .divvy_sync import connections, save_connections
    rows = _disconnect_rows(db, "divvy", company, key_var, connections(db))
    save_connections(db, rows)
    db.add(AuditRow(company=company, action="divvy_removed",
                    detail=key_var.strip(), actor=user.username))
    db.commit()
    return divvy_status(company=company, db=db, user=user)


@app.post("/divvy/pull")
def divvy_pull(company: str = Form(""), since: str = Form(""),
               key_var: str = Form(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_write)):
    """Start a pull and return at once.

    BILL pages 50 records at a time, so a wide window is minutes of sequential
    round trips. Held open, that request outlives whatever proxy sits in front
    of it and the browser shows a button stuck on "Pulling…" with nothing to
    read. Started on a thread instead: the caller gets an acknowledgement, and
    /divvy/status reports what the run is doing.
    """
    from . import divvy_sync as _ds
    d = None
    if since.strip():
        try:
            d = datetime.strptime(since.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "since must be YYYY-MM-DD")

    conns = _ds.connections(db, company)
    if key_var.strip():
        conns = [c for c in conns if c.get("key_var") == key_var.strip()]
    if not conns:
        raise HTTPException(400, f"'{company}' has no Divvy account configured")
    # A run only blocks a new one while it is DEMONSTRABLY ALIVE.
    #
    # `running` was trusted on its own, so a thread that died -- an exception
    # before its cleanup, a worker recycled mid-run -- left the flag set
    # permanently and refused every later pull. The only thing that cleared it
    # was the Stop button, which is why pulls appeared to work only after
    # pressing Stop.
    #
    # A heartbeat is written as the run progresses; ten minutes without one
    # means nothing is there to protect.
    from datetime import timezone as _tz
    _stale_after = 600.0
    running = []
    for v in _ds.progress_for(company).values():
        if not v.get("running"):
            continue
        beat = v.get("heartbeat") or v.get("started") or ""
        try:
            age = (datetime.now(_tz.utc)
                   - datetime.fromisoformat(beat)).total_seconds()
        except Exception:                                       # noqa: BLE001
            age = _stale_after + 1
        if age < _stale_after:
            running.append(v)
    if running:
        raise HTTPException(400, "a pull is already running for this company")

    who = user.username

    def _run(co=company, kv=key_var.strip(), d=d):
        from .db import SessionLocal
        s2 = SessionLocal()
        try:
            with _cv.for_company(s2, co):
                _ds.pull_divvy(s2, co, d, actor=who, key_var=kv)
        except Exception:                                       # noqa: BLE001
            log.exception("divvy pull thread failed for %s", co)
        finally:
            s2.close()

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "accounts": len(conns),
            "since": d.isoformat() if d else "(default window)"}

@app.post("/queue/{company}/purge-excluded")
def purge_excluded(company: str, confirm: str = Form(""),
                   db: Session = Depends(get_session),
                   user: UserRow = Depends(require_admin)):
    """Delete EVERY excluded charge for a company, not just the page on screen.

    Excluding is how a mis-imported batch gets set aside, so the excluded tab
    fills up with thousands of rows that exist only to be forgotten. Clearing
    them a page at a time is not a workflow, and the bulk actions on that grid
    operate on the current page by design.

    Deletes only rows that are actually excluded and never posted -- an
    excluded charge with a QuickBooks id is a contradiction worth leaving alone
    for somebody to look at rather than quietly resolving here.
    """
    from .models_db import ChargeRow as _CR
    if confirm.strip().upper() != "DELETE":
        # Without confirmation this is a count, so the number is known before
        # the decision rather than after it.
        n = db.query(_CR).filter(_CR.company == company,
                                 _CR.status == "excluded",
                                 _CR.qbo_txn_id == "").count()
        return {"preview": True, "company": company, "would_delete": n,
                "note": "re-send with confirm=DELETE"}
    rows = db.query(_CR).filter(_CR.company == company,
                                _CR.status == "excluded",
                                _CR.qbo_txn_id == "").all()
    n = len(rows)
    for r in rows:
        db.delete(r)
    db.add(AuditRow(company=company, action="purge_excluded", actor=user.username,
                    detail=f"deleted {n} excluded charges"))
    db.commit()
    return {"company": company, "deleted": n}


def _slash_accounts(db: Session, ents: list, wanted: set | None = None) -> dict:
    """entity id -> its card accounts, for the entities this company USES.

    It used to fetch the first twelve entities in alphabetical order and no
    more -- so entities late in the alphabet (YSP, YSS, YSTL, YSW) never had
    their accounts asked for, and the picker offered nothing but "all
    accounts". Now only the entities already connected are fetched here, with
    no cap, so their rows can name their account; any other entity's accounts
    are fetched the moment it is picked (see /slash/accounts).
    """
    from .integrations import slash_api
    out = {}
    for e in (ents or []):
        eid = e.get("id")
        if not eid or (wanted is not None and eid not in wanted):
            continue
        try:
            out[eid] = slash_api.accounts_cached(eid)
        except Exception:                                       # noqa: BLE001
            out[eid] = []
    return out


@app.get("/slash/accounts")
def slash_entity_accounts(company: str = Query(""), entity: str = Query(...),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_reviewer)):
    """One entity's card accounts, fetched when it is picked.

    An error is RETURNED rather than swallowed into an empty list: an empty
    list reads as "this entity has no accounts", which is a different and
    wrong answer when the real one is "Slash did not respond".
    """
    from .integrations import slash_api
    from . import company_vars as _cva
    with _cva.for_company(db, company):
        try:
            return {"entity": entity, "accounts": slash_api.accounts(entity)}
        except Exception as e:                                  # noqa: BLE001
            return {"entity": entity, "accounts": [], "error": str(e)[:300]}


def _ss_conns(db: Session, company: str) -> list:
    from .slash_sync import connections
    return connections(db, company)


def _ss_migrated(db: Session) -> bool:
    from .slash_sync import _migrated
    return _migrated(db)


_TAEKUS_PROBE: dict = {}
# One pull per company at a time: the button clicked twice would otherwise
# start a second walk over the same cards and double the rate-limit pressure.
_TAEKUS_RUNNING: set = set()


def _taekus_alive(key: str, username: str) -> tuple:
    """Is this key working? Cheap, and remembered for five minutes.

    The probe is a live API call on a screen people open constantly. Without a
    cache every visit spent a request -- and a rate-limit slot -- to re-answer
    a question whose answer changes about never.
    """
    from .integrations import taekus_api
    ck = f"{key[:8]}|{username}"
    # A SUCCESS is worth remembering for five minutes; a FAILURE for thirty
    # seconds. A key that works keeps working, so re-asking is waste -- but a
    # failure is usually a passing throttle, and caching it for five minutes
    # made one bad moment look like a broken key for the rest of it.
    hit = _TAEKUS_PROBE.get(ck)
    if hit:
        age = (datetime.utcnow() - hit[0]).total_seconds()
        if age < (600 if hit[1] else 30):
            return hit[1], hit[2]
    try:
        taekus_api.accounts(key, username, retries=0)
        out = (True, "")
    except Exception as e:                                      # noqa: BLE001
        msg = str(e)[:400]
        # THROTTLED IS NOT BROKEN.
        #
        # The probe makes one call with no retries, so any rate limit reads as
        # a failure -- and the panel then said "key not working" about a key
        # that works perfectly and was merely asked too often. That sends
        # somebody to check the key, the username and the dashboard, none of
        # which is wrong.
        #
        # Reported as unknown instead, and NOT cached: the next look, a few
        # seconds later, will usually succeed.
        if "rate-limit" in msg.lower() or "throttl" in msg.lower():
            return (None, "Taekus is rate-limiting right now — the key is "
                          "probably fine; a pull will retry")
        out = (False, msg)
    _TAEKUS_PROBE[ck] = (datetime.utcnow(), out[0], out[1])
    return out


def _slash_conns(db: Session, company: str) -> list:
    from . import slash_sync as _ss
    return _ss.connections(db, company)


def _slash_connections_with_status(db: Session, company: str) -> list:
    """Each configured program with its entity, account and last pull."""
    from .models_db import UploadRow as _UR
    out = []
    for c in _slash_conns(db, company):
        last = ""
        r = (db.query(_UR).filter(_UR.company == company, _UR.method == "pull",
                                  _UR.source == c.get("source", ""))
             .order_by(_UR.at.desc()).first())
        if r and r.at:
            last = r.at.isoformat() + "Z"
        # A program with no account set, where the entity has more than one,
        # would fetch every program's charges into this one's card account.
        # The pull refuses it; the panel should say so before somebody waits
        # for a run that is not going to happen.
        sibs = [x for x in _slash_conns(db, company)
                if x.get("entity") == c.get("entity")]
        needs_account = (not (c.get("account") or "")) and len(sibs) > 1
        # The latest outcome, failure included -- see slash_sync._note_outcome.
        from .models_db import AppSettingRow as _ASo
        import json as _jo
        _oc = db.get(_ASo, f"slash_outcome:{company}:{c.get('source', '')}")
        try:
            _oc = _jo.loads(_oc.value) if _oc else {}
        except ValueError:
            _oc = {}
        out.append({**c, "last_pull_at": last,
                    "last_attempt_at": _oc.get("at", ""),
                    "last_error": _oc.get("error", ""),
                    "needs_account": needs_account})
    return out


def _free_card_accounts(db: Session, company: str, ignore: tuple = ()) -> list:
    """Card accounts with no feed attached yet.

    An account already fed by another program is not a choice -- offering it
    only lets somebody pick it and be refused a moment later, which reads as a
    bug rather than as a rule. The `ignore` tuple is the connection being
    edited, so its own account stays selectable when changing something else
    about it.
    """
    return [a for a in _card_account_choices(db, company)
            if not _feed_owner(db, company, a["source"], ignore=ignore)]


_RESCORE_RUNNING: set = set()


def _kick_hal_fill(db, company: str) -> int:
    """See hal_flags.kick: the background STH lookup."""
    from .hal_flags import kick
    return kick(company, db=db, automatic=False)


def _kick_rescore(company: str) -> bool:
    """Rescore this company on a thread. True if one started.

    One at a time per company: a filter clicked twice must not start a second
    pass over the same rows, and the second would only repeat the first.
    """
    if company in _RESCORE_RUNNING:
        return True
    def _run():
        from .db import SessionLocal
        from .review import refresh_scores
        s2 = SessionLocal()
        try:
            from .review import refresh_rule_flags
            # The flag the filter reads, for every row -- not just the page.
            k = refresh_rule_flags(s2, company)
            n = refresh_scores(s2, company)
            if n or k:
                log.info("rescored %d, rule-flagged %d for %s (rules filter)",
                         n, k, company)
        except Exception:                                       # noqa: BLE001
            log.exception("background rescore failed for %s", company)
            s2.rollback()
        finally:
            s2.close()
            _RESCORE_RUNNING.discard(company)
    _RESCORE_RUNNING.add(company)
    threading.Thread(target=_run, daemon=True,
                     name=f"rescore-{company}").start()
    return True


def _feed_owner(db: Session, company: str, source: str,
                ignore: tuple = ()) -> str:
    """What already imports into this card account, or "".

    ONE card account, ONE feed. Two programs pointed at the same account
    produce charges that are indistinguishable afterwards -- same source key,
    two origins -- and nothing downstream can separate them again. Worse, each
    would re-import what the other already brought in under a different
    transaction id, so the duplicates would not even dedup.

    Checked across EVERY program rather than each one guarding itself: Divvy
    and Taekus each refused a second account of their own kind, which left
    Divvy and Slash free to collide, and either free to collide with Plaid.

    `ignore` is the (kind, key) being edited, so saving a connection over
    itself is not reported as a clash with itself.
    """
    from . import divvy_sync as _ds, taekus_sync as _ts
    from .models_db import SourceAccountRow as _SA

    for c in _ds.connections(db, company):
        if c.get("source") == source and ("divvy", c.get("key_var")) not in ignore:
            return f"Divvy ({c.get('key_var')})"
    for c in _ts.connections(db, company):
        if c.get("source") == source and ("taekus", c.get("key_var")) not in ignore:
            return f"Taekus ({c.get('key_var')})"
    try:
        from . import slash_sync as _ss2
        # The CONNECTIONS list, not the old single map. Slash holds several
        # programs per company now, and checking only the legacy map let a
        # second program claim an account a first one already had.
        for c in _ss2.connections(db, company):
            if c.get("source") == source \
                    and ("slash", (c.get("entity", "") + "|"
                                   + (c.get("account") or ""))) not in ignore:
                return f"Slash ({c.get('entity') or 'entity'})"
        # The legacy single-entity map, ONLY before migration.
        #
        # It is never cleared, so after the connections were migrated and then
        # removed it still named a card account -- and the clash check believed
        # it. Removing a Slash program and adding it back reported "Slash
        # already imports into that card account" against a connection that no
        # longer existed anywhere except that stale map.
        from .slash_sync import source_for as _slash_src, _migrated
        if not _migrated(db) and not _ss2.connections(db, company) \
                and _slash_src(db, company) == source \
                and ("slash", "") not in ignore:
            return "Slash"
    except Exception:                                           # noqa: BLE001
        pass
    # Plaid attaches to the card account row itself.
    row = db.query(_SA).filter(_SA.company == company,
                               _SA.source == source).first()
    if row is not None and getattr(row, "plaid_item_id", "") \
            and ("plaid", "") not in ignore:
        return "a Plaid bank feed"
    return ""


@app.get("/admin/team-league-conflicts")
def team_league_conflicts(db: Session = Depends(get_session),
                          user: UserRow = Depends(require_reviewer)):
    """Team names that appear under more than one league.

    A genuine team belongs to one league. The same name under two is almost
    always a record whose Team field was edited while the rest of it stayed
    with the old team -- so its venue, schedule and platform are being read as
    though they belonged to the new one. That is how Chase Field ended up on a
    college program.

    Lists them with their leagues and venues so the wrong record is obvious.
    """
    from .models_db import TeamAliasRow
    by_team: dict = {}
    for r in db.query(TeamAliasRow).all():
        by_team.setdefault(r.team, {}).setdefault(r.league or "", set())
        if r.alias and r.kind == "venue":
            by_team[r.team][r.league or ""].add(r.alias)
    out = []
    for team, leagues in sorted(by_team.items()):
        if len(leagues) < 2:
            continue
        out.append({"team": team,
                    "leagues": [{"league": lg or "(none)",
                                 "venues": sorted(v)}
                                for lg, v in sorted(leagues.items())]})
    return {"conflicts": len(out), "teams": out}


@app.get("/admin/airtable-team")
def admin_airtable_team(name: str = Query(...),
                        user: UserRow = Depends(require_reviewer)):
    """The RAW Airtable record for a team, exactly as Airtable returns it.

    The venue on a team row can only come from that row -- team and venue are
    read from the same record and keyed by team, so there is no path in this
    code that could attach one team's stadium to another. When the screen
    disagrees with Airtable anyway, the question is which field the record
    actually carries, and that is answerable only by looking at the record.

    Returns every field, unmodified, plus the record id so it can be opened in
    Airtable directly.
    """
    from .integrations.airtable import (_fetch_table, TABLE_TEAMS_VENUES,
                                        BASE_ACCOUNTING_FOR, F_TV_TEAM)
    want = (name or "").strip().lower()
    hits = []
    for rec in _fetch_table(TABLE_TEAMS_VENUES, None,
                            base_id=BASE_ACCOUNTING_FOR(), by_field_id=False):
        f = rec.get("fields", {})
        team = str(f.get(F_TV_TEAM) or "").strip()
        if team.lower() == want or (want and want in team.lower()):
            hits.append({"record_id": rec.get("id"), "fields": f})
        if len(hits) >= 5:
            break
    return {"team": name, "matches": len(hits), "records": hits}


@app.get("/admin/taekus-last-run")
def taekus_last_run(company: str = Query(...),
                    db: Session = Depends(get_session),
                    user: UserRow = Depends(require_admin)):
    """What the last Taekus pull did, and whether one is running now.

    The pull is a background thread; everything it knows is written to
    settings. This reads it back without needing the screen, so a pull that
    appears to do nothing can be diagnosed in one call instead of a round trip
    per guess.
    """
    from . import taekus_sync as _ts
    from .models_db import AppSettingRow as _ASL
    out = {"company": company, "running": company in _TAEKUS_RUNNING,
           "connections": []}
    for c in _ts.connections(db, company):
        rec = {}
        r = db.get(_ASL, f"taekus_last_run:{company}:{c['key_var']}")
        if r and r.value:
            try:
                rec = json.loads(r.value)
            except Exception:                                   # noqa: BLE001
                rec = {"raw": r.value[:400]}
        key = _ts.token_for(c["key_var"])
        out["connections"].append({
            "key_var": c["key_var"], "username": c.get("username", ""),
            "source": c.get("source", ""),
            "key_present": bool(key), "key_shape": _ts.key_shape(key),
            "last_run": rec,
        })
    return out


@app.get("/admin/taekus-sample")
def taekus_sample(company: str = Query(...), since: str = Query(""),
                  merchant: str = Query(""),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    """Raw Taekus transactions, exactly as they send them.

    The same tool as the Divvy sample, for the same reason: guessing at why a
    charge has not arrived costs a deploy cycle each time, and asking costs one
    call. Reports what the ACCOUNT call returned as well, because "no accounts"
    and "no transactions" look identical from the outside and have different
    fixes.
    """
    from . import taekus_sync as _ts
    from .integrations import taekus_api

    conns = _ts.connections(db, company)
    if not conns:
        raise HTTPException(400, f"'{company}' has no Taekus account configured")
    conn = conns[0]
    key = _ts.token_for(conn["key_var"])
    shape = _ts.key_shape(key)
    if shape:
        raise HTTPException(400, f"{conn['key_var']}: {shape}")

    start = (datetime.strptime(since.strip(), "%Y-%m-%d").date()
             if since.strip()
             else (datetime.now(timezone.utc).date() - timedelta(days=45)))
    end = datetime.now(timezone.utc).date() + timedelta(days=1)

    out: dict = {"company": company, "key_var": conn["key_var"],
                 "username": conn.get("username", ""),
                 "window": [start.isoformat(), end.isoformat()]}
    try:
        accts = taekus_api.accounts(key, conn["username"])
    except Exception as e:                                      # noqa: BLE001
        out["accounts_error"] = str(e)[:300]
        return out
    out["accounts"] = accts
    if not accts:
        out["message"] = ("the key lists no card accounts — transactions are "
                          "fetched per account, so nothing can be pulled")
        return out

    want = merchant.strip().lower()
    seen, first, hit = 0, None, None
    for a in accts:
        try:
            for txn in taekus_api.transactions(
                    key, conn["username"], a["uuid"],
                    start=f"{start.isoformat()}T00:00:00Z",
                    end=f"{end.isoformat()}T00:00:00Z"):
                seen += 1
                if first is None:
                    first = {"account": a.get("name"), "txn": txn}
                blob = json.dumps(txn).lower()
                if want and want in blob:
                    hit = {"account": a.get("name"), "txn": txn}
                    break
                if seen >= 300:
                    break
        except Exception as e:                                  # noqa: BLE001
            out.setdefault("errors", []).append(
                f"{a.get('name')}: {str(e)[:200]}")
        if hit:
            break

    out["scanned"] = seen
    # Which endpoint answered, so a fallback that worked is visible rather
    # than looking like the first one having quietly started to work.
    out["note"] = ("if `errors` is empty and `scanned` is above zero, the "
                   "transactions call succeeded — possibly via the "
                   "payment-cards fallback; check the log line")
    out["matched_merchant"] = bool(hit)
    out["sample"] = hit or first
    if seen == 0:
        out["message"] = ("the accounts answered, but returned no transactions "
                          "in this window")
    return out


@app.get("/admin/divvy-sample")
def divvy_sample(company: str = Query(...), key_var: str = Query(""),
                 since: str = Query(""), merchant: str = Query(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """One RAW transaction from BILL, exactly as they send it.

    Every attempt to answer "does Divvy give us the original currency" has gone
    through a pull: run one, hope nothing restarts, read a diagnostic. Three
    rounds of that have produced nothing, because the answer depends on a
    sequence of events rather than on a question anyone can ask.

    This asks directly. It fetches a page, picks a transaction -- the first one
    matching `merchant` if given, so a Toronto charge can be named -- and
    returns its entire JSON body unaltered. Whatever BILL holds for currency is
    then visible, whatever they happen to call it.
    """
    from . import divvy_sync as _ds
    from .integrations import divvy_api

    conns = _ds.connections(db, company)
    if key_var.strip():
        conns = [c for c in conns if c.get("key_var") == key_var.strip()]
    if not conns:
        raise HTTPException(400, f"'{company}' has no Divvy account configured")

    conn = conns[0]
    token = divvy_api.token_for(conn["key_var"])
    if not token:
        raise HTTPException(400, f"{conn['key_var']} is not set here")

    start = (datetime.strptime(since.strip(), "%Y-%m-%d").date()
             if since.strip()
             else (datetime.now(timezone.utc).date() - timedelta(days=45)))
    since_txt = datetime(start.year, start.month, start.day,
                         tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    want = merchant.strip().lower()
    first, hit, seen = None, None, 0
    for txn in divvy_api.transactions(token, since=since_txt):
        seen += 1
        if first is None:
            first = txn
        name = " ".join(str(txn.get(k) or "") for k in
                        ("merchantName", "rawMerchantName")).lower()
        if want and want in name:
            hit = txn
            break
        if seen >= 400:
            # Enough to find a named merchant if it is there at all, and few
            # enough not to spend the rate limit on a diagnostic.
            break

    txn = hit or first
    if txn is None:
        return {"company": company, "key_var": conn["key_var"],
                "scanned": seen, "message": "no transactions in that window"}
    return {"company": company, "key_var": conn["key_var"], "scanned": seen,
            "matched_merchant": bool(hit),
            # The whole thing. A field list would answer "what is it called";
            # the values answer "is the number even here", which is the
            # question that has actually been open.
            "transaction": txn}


@app.post("/divvy/repair-currency")
def divvy_repair_currency(company: str = Form(...), key_var: str = Form(""),
                          since: str = Form(""),
                          db: Session = Depends(get_session),
                          user: UserRow = Depends(require_admin)):
    """Correct the currency on Divvy charges already imported.

    Runs in the foreground rather than on a thread: it is a one-off, the caller
    wants the counts, and there is nothing to watch while it works.
    """
    from .divvy_sync import repair_currency
    d = None
    if since.strip():
        try:
            d = datetime.strptime(since.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "since must be YYYY-MM-DD")
    try:
        return repair_currency(db, company, d, key_var=key_var.strip(),
                               actor=user.username)
    except Exception as e:                                      # noqa: BLE001
        raise HTTPException(400, str(e)[:300])


@app.post("/pull/stop")
def pull_stop_any(company: str = Form(...), program: str = Form(...), ident: str = Form(""),
                  user: UserRow = Depends(require_write)):
    """The red Stop of a running Pull Now: stops after the page (or card) it
    is on, keeping what it has imported."""
    if program == "divvy":
        from .divvy_sync import request_cancel
        return {"signaled": request_cancel(company, ident.strip())}
    if program not in ("slash", "taekus", "globalrewards"):
        raise HTTPException(400, "unknown program")
    from . import pull_stop
    pull_stop.request(program, company, ident.strip())
    return {"signaled": 1}


@app.post("/divvy/stop")
def divvy_stop(company: str = Form(...), key_var: str = Form(""),
               user: UserRow = Depends(require_write)):
    """Ask a running pull to stop after the page it is on."""
    from .divvy_sync import request_cancel
    return {"signaled": request_cancel(company, key_var.strip())}


@app.post("/admin/purge-pull")
def purge_pull(company: str = Form(...), source: str = Form(""),
               method: str = Form("api"), confirm: str = Form(""),
               db: Session = Depends(get_session),
               user: UserRow = Depends(require_admin)):
    """Delete charges that came in through an API pull, for one card account.

    For undoing a bad import -- a window that fetched far more than intended,
    a mapping that sent one company's spending into another's books. Deleting
    charges is not something this engine does anywhere else, so the guards are
    deliberate and not negotiable:

      * Only rows whose ingest_method is the one named. An upload sitting in
        the same card account is untouched.
      * Never a row with a QuickBooks transaction id. Those are in the ledger,
        and removing the engine's copy would leave an entry nothing here knows
        about -- silently, which is the worst version of it.
      * Never a row somebody has coded. That is human work, and a bad import is
        not a reason to throw it away without being asked.

    Anything refused is COUNTED and reported, so the caller learns the import
    is entangled rather than believing it is gone.
    """
    from sqlalchemy import func as _f
    from .models_db import ChargeRow as _CR

    q = db.query(_CR).filter(
        _CR.company == company,
        _f.coalesce(_CR.ingest_method, "csv") == method)
    if source.strip():
        q = q.filter(_CR.source == source.strip())
    rows = q.all()

    # NO source and NO confirm: a survey. Nobody should have to know the card
    # account key before they can find out what a bad import left behind, and
    # the key for an unmapped QuickBooks account ("qbo:874") is not something
    # anybody has seen written down.
    if not confirm.strip():
        by_source: dict = {}
        for r in rows:
            b = by_source.setdefault(r.source, {"total": 0, "deletable": 0,
                                                "posted": 0, "coded": 0})
            b["total"] += 1
            if r.qbo_txn_id:
                b["posted"] += 1
            elif r.status in ("categorized", "resolved"):
                b["coded"] += 1
            else:
                b["deletable"] += 1
        return {"preview": True, "company": company, "method": method,
                "by_source": by_source,
                "note": "re-send with confirm=DELETE (and a source) to delete"}

    if confirm.strip().upper() != "DELETE":
        raise HTTPException(400, "type DELETE to confirm")
    if not source.strip():
        # A confirmed delete has to name ONE card account. "Everything this
        # company ever pulled" is not a thing anybody means to type.
        raise HTTPException(400, "name a source to delete from")
    posted = [r for r in rows if r.qbo_txn_id]
    coded = [r for r in rows if not r.qbo_txn_id
             and r.status in ("categorized", "resolved")]
    deletable = [r for r in rows if not r.qbo_txn_id
                 and r.status not in ("categorized", "resolved")]
    for r in deletable:
        db.delete(r)
    db.add(AuditRow(company=company, action="purge_pull", actor=user.username,
                    detail=f"{source} {method}: deleted {len(deletable)}, "
                           f"kept {len(posted)} posted and {len(coded)} coded"))
    db.commit()
    return {"company": company, "source": source, "method": method,
            "deleted": len(deletable),
            "kept_posted_to_quickbooks": len(posted),
            "kept_already_coded": len(coded)}


# --- Taekus ------------------------------------------------------------------
#
# Two headers, not one: Authorization: Api-Key <key> AND Username: <username>.
# The username is per account and is stored alongside the key VARIABLE NAME --
# the key itself stays in the environment, as with Divvy.

@app.get("/taekus/status")
def taekus_status(company: str = Query(""), db: Session = Depends(get_session),
                  user: UserRow = Depends(require_reviewer)):
    from . import taekus_sync as _ts
    accounts = _card_account_choices(db, company) if company else []
    candidates = _unrouted_accounts(db, company, "taekus",
                                    _ts.connections(db, company))
    from .models_db import UploadRow as _UR
    out = []
    for c in _ts.connections(db, company):
        key = _ts.token_for(c["key_var"])
        live, err = False, ""
        shape = _ts.key_shape(key)
        if not key:
            # Keys live on Connectors; the environment is no longer read.
            err = "no key entered on Connectors for this account"
        elif shape:
            # Said BEFORE the call. A malformed key fails as "Unauthorized",
            # which sends people to check the username and the dashboard --
            # neither of which is wrong.
            err = f"{c['key_var']}: {shape}"
        elif not (c.get("username") or "").strip():
            err = "no username configured"
        else:
            # NO RETRIES on a probe somebody is waiting for, and the answer is
            # cached: this ran on every load of the QuickBooks Sync screen, so
            # a throttled or slow Taekus made the whole screen hang rather than
            # one line of it reading "unknown".
            live, err = _taekus_alive(key, c["username"])
        last = ""
        r = (db.query(_UR).filter(_UR.company == company, _UR.method == "pull",
                                  _UR.source == c.get("source", ""))
             .order_by(_UR.at.desc()).first())
        if r and r.at:
            last = r.at.isoformat() + "Z"
        # How the last pull went, from settings -- see taekus_sync._record_run.
        # A pull whose page was navigated away from still reports itself here.
        _lr = {}
        try:
            from .models_db import AppSettingRow as _ASR
            _r2 = db.get(_ASR, f"taekus_last_run:{company}:{c['key_var']}")
            if _r2 and _r2.value:
                _lr = json.loads(_r2.value)
        except Exception:                                       # noqa: BLE001
            _lr = {}
        out.append({"key_var": c["key_var"], "username": c.get("username", ""),
                    "label": _account_label(db, "taekus", c["key_var"]),
                    "last_run": _lr,
                    "source": c.get("source", ""),
                    "source_name": next((a["name"] for a in accounts
                                         if a["source"] == c.get("source")),
                                        c.get("source", "")),
                    "has_token": bool(key), "token_works": live,
                    "error": err, "last_pull_at": last})
    return {"company": company, "connections": out,
            "key_var_choices": candidates,
            # Only accounts nothing else feeds. See _free_card_accounts.
            "card_accounts": _free_card_accounts(db, company) if company
            else accounts}


@app.post("/taekus/config")
def taekus_config(company: str = Form(...), key_var: str = Form(...),
                  username: str = Form(...), source: str = Form(...),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    """Add or update one Taekus account."""
    from . import taekus_sync as _ts
    var, src, un = key_var.strip(), source.strip(), username.strip()
    if not (var and src and un):
        raise HTTPException(400, "key variable, username and card account are "
                                 "all required")
    # The key on CONNECTORS, not a Railway variable: this was the one
    # check left reading the environment, so routing an account added
    # on Connectors was refused as if it had no key.
    from . import taekus_sync as _tkf
    if not _tkf.token_for(var):
        raise HTTPException(
            400, "No key has been entered for this account on Connectors "
                 "\u2014 enter it there first, then choose it here")
    if src not in {a["source"] for a in _card_account_choices(db, company)}:
        raise HTTPException(400, f"'{company}' has no card account '{src}'")
    rows = [r for r in _ts.connections(db)
            if not (r.get("company") == company and r.get("key_var") == var)]
    # Two accounts must not share a card account, for the same reason Divvy's
    # must not: their charges would be indistinguishable afterwards.
    owner = _feed_owner(db, company, src, ignore=(("taekus", var),))
    if owner:
        raise HTTPException(
            400, f"{owner} already imports into that card account. One card "
                 f"account takes one feed — give this one its own.")
    rows.append({"company": company, "key_var": var, "username": un,
                 "source": src})
    _ts.save_connections(db, rows)
    db.add(AuditRow(company=company, action="taekus_configured",
                    detail=f"{var} assigned to {src}", actor=user.username))
    db.commit()
    return taekus_status(company=company, db=db, user=user)


@app.post("/taekus/remove")
def taekus_remove(company: str = Form(...), key_var: str = Form(...),
                  db: Session = Depends(get_session),
                  user: UserRow = Depends(require_admin)):
    from . import taekus_sync as _ts
    rows = _disconnect_rows(db, "taekus", company, key_var, _ts.connections(db))
    _ts.save_connections(db, rows)
    db.add(AuditRow(company=company, action="taekus_removed",
                    detail=key_var.strip(), actor=user.username))
    db.commit()
    return taekus_status(company=company, db=db, user=user)


@app.post("/taekus/pull")
def taekus_pull(company: str = Form(...), key_var: str = Form(""),
                since: str = Form(""), db: Session = Depends(get_session),
                user: UserRow = Depends(require_write)):
    d = None
    if since.strip():
        try:
            d = datetime.strptime(since.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "since must be YYYY-MM-DD")

    # ON A THREAD, returning at once.
    #
    # The pull ran inside the request. When it falls back to walking cards it
    # takes minutes -- and Railway's proxy gives up long before that, which is
    # the "upstream error" seen on the screen. The pull itself was fine and
    # carried on; only the connection died, so there was no way to learn what
    # it had done.
    #
    # The outcome is recorded per account (see taekus_sync._record_run) and the
    # panel reports it, so returning immediately loses nothing.
    if company in _TAEKUS_RUNNING:
        return {"started": False, "already_running": True,
                "message": "a pull is already running for this company"}

    # THE NAME, not the row.
    #
    # `user` is a SQLAlchemy object bound to THIS request's session. By the
    # time a thread reads user.username the session is closed and the read
    # raises DetachedInstanceError -- before the pull starts, inside a thread
    # whose exception nobody sees. So the button said "started", the thread
    # died at once, and nothing was recorded: exactly the symptom.
    #
    # The Divvy pull already captures the name first; this did not.
    who = user.username
    _kv = key_var.strip()

    def _run():
        from .db import SessionLocal
        from .taekus_sync import pull_taekus
        s2 = SessionLocal()
        try:
            with _cv.for_company(s2, company):
                r = pull_taekus(s2, company, d, actor=who, key_var=_kv)
            log.info("taekus pull for %s: %s fetched, %s new",
                     company, r.get("fetched"), r.get("new"))
        except Exception as exc:                                # noqa: BLE001
            # RECORDED WHERE IT CAN BE SEEN.
            #
            # A background thread's exception goes to the server log and
            # nowhere else. Everything after it -- the pull, the upload row,
            # the per-account record -- is skipped, so the screen shows exactly
            # what it showed before: an old timestamp and no error. That is
            # indistinguishable from the pull never having been asked for.
            log.exception("taekus pull failed for %s", company)
            s2.rollback()
            try:
                from .models_db import AppSettingRow as _ASE
                from . import taekus_sync as _ts2
                import traceback as _tb
                detail = f"{type(exc).__name__}: {exc}"[:400]
                trace = "".join(_tb.format_exception_only(type(exc), exc))[:200]
                for _c in _ts2.connections(s2, company):
                    k = f"taekus_last_run:{company}:{_c['key_var']}"
                    row = s2.get(_ASE, k) or _ASE(key=k)
                    row.value = json.dumps({
                        "at": datetime.now(timezone.utc).isoformat(),
                        "fetched": 0, "new": 0, "no_cardholder": 0,
                        "error": detail or trace,
                    })
                    row.updated_at = datetime.utcnow()
                    s2.add(row)
                s2.commit()
            except Exception:                                   # noqa: BLE001
                log.exception("could not record the taekus thread failure")
                s2.rollback()
        finally:
            s2.close()
            _TAEKUS_RUNNING.discard(company)

    _TAEKUS_RUNNING.add(company)
    threading.Thread(target=_run, daemon=True,
                     name=f"taekus-{company}").start()
    return {"started": True,
            "message": "Pulling in the background — the result appears on this "
                       "panel when it finishes"}


@app.post("/slash/config")
def slash_config(company: str = Form(...), entity: str = Form(...),
                 account: str = Form(""), source: str = Form(...),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Add or update one Slash program for this company.

    Slash held a single entity per company until they opened an unsecured card
    alongside the secured one -- the same shape as Divvy Credit and Prefund.
    Each program now gets its own card account, and one card account still
    takes exactly one feed.
    """
    from . import slash_sync as _ss
    ent, src = entity.strip(), source.strip()
    if not (ent and src):
        raise HTTPException(400, "entity and card account are both required")
    if src not in {a["source"] for a in _card_account_choices(db, company)}:
        raise HTTPException(400, f"'{company}' has no card account '{src}'")
    owner = _feed_owner(db, company, src,
                        ignore=(("slash", ent + "|" + account.strip()),))
    if owner:
        raise HTTPException(
            400, f"{owner} already imports into that card account — give this "
                 f"program its own, or the two feeds' charges cannot be told "
                 f"apart")
    # ONE COMPANY PER SLASH PROGRAM. A Slash key is a person's, and reaches
    # every entity that person can -- so on one company's screen the picker
    # offered other companies' entities too, and connecting one would pull
    # that company's charges into these books. A program already connected to
    # another company is refused, naming it. (A program with no account set
    # covers the whole entity, so it clashes with any program of that entity.)
    acct = account.strip()
    for r in _ss.connections(db) if not _is_test_company(db, company) else []:
        if r.get("company") == company or r.get("entity") != ent:
            continue
        # A testing company's connections block nobody.
        if _is_test_company(db, r.get("company") or ""):
            continue
        other = (r.get("account") or "")
        if not acct or not other or other == acct:
            raise HTTPException(
                409, f"That Slash program is already connected to "
                     f"{r.get('company')}. A program can belong to one "
                     f"company only -- remove it there first if it has moved.")
    # Keyed on entity AND account: two programs share an entity, so keying
    # on the entity alone let the second replace the first.
    rows = [r for r in _ss.connections(db)
            if not (r.get("company") == company and r.get("entity") == ent
                    and (r.get("account") or "") == acct)]
    rows.append({"company": company, "entity": ent, "account": acct,
                 "source": src})
    _ss.save_connections(db, rows)
    db.add(AuditRow(company=company, action="slash_configured",
                    detail=f"{ent} assigned to {src}", actor=user.username))
    db.commit()
    return {"ok": True, "connections": _ss.connections(db, company)}


@app.post("/slash/remove")
def slash_remove(company: str = Form(...), entity: str = Form(...),
                 account: str = Form(""),
                 db: Session = Depends(get_session),
                 user: UserRow = Depends(require_admin)):
    """Remove ONE Slash program.

    Matched on entity AND account. Two programs share an entity, so matching
    on the entity alone either removed both or -- because the migrated
    connection carries no account and the request sent one -- matched neither,
    which is why removing a connection appeared to do nothing.
    """
    from . import slash_sync as _ss
    ent, acct = entity.strip(), account.strip()
    rows = [r for r in _ss.connections(db)
            if not (r.get("company") == company
                    and r.get("entity") == ent
                    and (r.get("account") or "") == acct)]
    _ss.save_connections(db, rows)
    db.add(AuditRow(company=company, action="slash_removed",
                    detail=entity.strip(), actor=user.username))
    db.commit()
    return {"ok": True, "connections": _ss.connections(db, company)}
