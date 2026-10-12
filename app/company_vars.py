"""Settings that belong to a company rather than to the deployment.

WHY THIS EXISTS. Everything the engine needs arrived through Railway's
environment: one Airtable token, one Slash key, one set of STH seasons, one
CAD rate. That is exactly right with one company and wrong with two -- they
have different Airtable bases, different card programs, different seasons --
and changing any of it meant a redeploy that changed it for everybody at once.

HOW IT BEHAVES. `get()` reads the company's override and falls back to the
environment value. An install that never opens the Variables screen therefore
behaves precisely as it did before: the fallback IS the old behavior. Nothing
had to be migrated and nothing breaks if the table is empty.

WHAT IS NOT HERE. Deployment-level settings -- the database URL, the JWT
secret, SMTP, the QuickBooks and Plaid app credentials, the safety gates -- are
deliberately absent from the catalog. They are not per-company facts, and
putting a kill switch like qbo_write_enabled on a screen any admin can reach
would turn a deliberate two-step safety gate into a checkbox.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy.orm import Session

from .config import settings


# name -> (label, group, secret, help, settings attribute for the fallback)
#
# A CATALOG, not free text. A screen that lets people invent key names
# produces a company with AIRTABLE_TOKEN and another with airtable_api_token,
# and nothing reads either. Every entry here is something the engine actually
# looks up.
CATALOG: list[dict] = [
    # --- Airtable / STH -----------------------------------------------------
    # NOT YET PER COMPANY, and the help text says so rather than implying
    # otherwise. The STH mirror (HalRow) has no company column: every record
    # from every Airtable base lands in one table keyed by Airtable record id.
    # Until that table is company-keyed there is no such thing as "this
    # company's STH data", so a per-company token would pull a second base into
    # the same mirror and the two would overwrite each other by record id.
    # Making it company-scoped is a schema change and a migration of existing
    # records, which is its own piece of work.
    {"name": "airtable_token", "label": "Airtable token", "group": "Airtable",
     "secret": True, "connectors_only": True, "attr": "airtable_token",
     "help": "DEPLOYMENT-WIDE for now — the STH mirror is not yet split by "
             "company, so setting this here has no effect. Change it in "
             "Railway. Listed so the value is visible in one place."},

    # PER COMPANY, and they take effect.
    #
    # Each broker keeps their season tickets in their own Airtable base. One
    # token in the workspace reaches all of them, so the base id is the only
    # thing that differs -- and it is a per-company fact, which is why it lives
    # here rather than in Railway.
    #
    # Empty means the deployment default, so an installation that has never
    # been split keeps working with nothing set.
    {"name": "airtable_base_tickets", "label": "Airtable Base ID \u2014 Tickets",
     "group": "Airtable", "secret": True, "attr": "airtable_base_tickets",
     "help": "The base holding this company's STH and Address Book, e.g. "
             "appOEQZDCBXBZRiUv. Find it in the Airtable URL: the part after "
             "airtable.com/ that starts with 'app'. Leave empty to use the "
             "deployment default."},
    {"name": "airtable_base_accounting", "label": "Airtable Base ID \u2014 Accounting",
     "group": "Airtable", "secret": True, "attr": "airtable_base_accounting",
     "help": "The base holding team and venue aliases for this company. "
             "Leave empty to use the deployment default."},

    # WHICH OF THIS BROKER'S COLUMNS FEEDS WHICH FIELD.
    #
    # Real broker exports run 25 to 68 columns and no two agree on names: the
    # lookup is "Lookup", "Lookup Name", "Unique Lookup" or "Name"; the plan is
    # "Plan", "Plan Type" or "2026 Plan". The fields themselves are present in
    # nearly every one, so a mapping is enough -- no code per broker.
    #
    # The EMAIL line is the one that matters most. Identity is an address, and
    # only some brokers put it in the lookup name; the rest keep it in a column
    # of its own. Left unmapped, such a broker imports cleanly and then matches
    # nothing at all.
    #
    # Written as "field = Their Column Name", one per line, so it can be read
    # and corrected by whoever knows the broker's base.
    {"name": "airtable_field_map", "label": "Airtable field map",
     "group": "Airtable", "secret": False, "attr": "airtable_field_map",
     "multiline": True,
     "help": "One per line, field = their column name. Fields: lookup, team, "
             "email, plan, status, card, total, section, row, seats. Example: "
             "email = Email (from Profiles). Leave empty when the base uses "
             "the same layout as the default one."},

    # THE BROKER'S AIRTABLE SETUP, from Connectors: which tables, views and
    # columns hold its names and emails and its STH records. JSON, written by
    # the setup screen; empty means the default (Y&S) layout.
    {"name": "airtable_setup", "label": "Airtable setup",
     "group": "Airtable", "secret": False, "attr": "airtable_setup",
     "multiline": True, "help": "Written by the Airtable setup on Connectors."},

    # --- Status words: what this broker's season ticket statuses mean -------
    {"name": "sth_status_words", "label": "Season ticket status words", "group": "Broker Data",
     "secret": False, "connectors_only": True, "attr": "sth_status_words",
     "help": 'JSON: {"active": [...], "progress": [...], "dead": [...]}, added to the built-in words.'},

    {"name": "renewal_custom", "label": "Renewal statuses of this company's own", "group": "Broker Data",
     "secret": False, "connectors_only": True, "attr": "renewal_custom",
     "help": 'JSON [{key, name, kind, words, color}]: statuses added on Renewal Status Words.'},

    {"name": "season_conventions", "label": "Renewal year conventions", "group": "Broker Data",
     "secret": False, "connectors_only": True, "attr": "season_conventions",
     "help": 'JSON {LEAGUE: "starts" | "ends"}: what a single year like 2026 means for that league.'},

    # --- Descriptions on Connectors, edited with the row's pencil -----------
    {"name": "airtable_label", "label": "Airtable description", "group": "Connectors",
     "secret": False, "connectors_only": True, "attr": "airtable_label",
     "help": "What the Airtable row's Source says."},
    {"name": "slash_label", "label": "Slash description", "group": "Connectors",
     "secret": False, "connectors_only": True, "attr": "slash_label",
     "help": "What the Slash row's Source says."},

    # --- Google Sheets and OneDrive: broker data kept in spreadsheets --------
    {"name": "google_service_account", "label": "Google service account key",
     "group": "Google Sheets", "secret": True, "connectors_only": True,
     "attr": "google_service_account", "multiline": True,
     "help": "The JSON key of a Google Cloud service account with the Sheets and "
             "Drive APIs enabled. Share each spreadsheet with its email address."},
    {"name": "microsoft_client_id", "label": "Microsoft App (client) ID",
     "group": "OneDrive", "secret": False, "connectors_only": True,
     "attr": "microsoft_client_id",
     "help": "From an Azure app registration that allows public client flows, "
             "with the Files.Read.All and User.Read delegated permissions."},
    {"name": "microsoft_refresh_token", "label": "OneDrive sign-in",
     "group": "OneDrive", "secret": True, "connectors_only": True,
     "attr": "microsoft_refresh_token",
     "help": "Kept by Sign In on Connectors. Not typed in."},
    {"name": "microsoft_account", "label": "OneDrive account", "group": "OneDrive",
     "secret": False, "connectors_only": True, "attr": "microsoft_account",
     "help": "The account signed in on Connectors."},

    # --- Slash --------------------------------------------------------------
    {"name": "slash_api_key", "label": "Slash API key", "group": "Slash",
     "secret": True, "connectors_only": True, "attr": "slash_api_key",
     "help": "One key per Slash account. Leave blank if this company has no "
             "Slash program."},
    {"name": "slash_legal_entity_id", "label": "Slash legal entity id",
     "group": "Slash", "secret": False, "attr": "slash_legal_entity_id",
     "help": "Only needed for a USER-scoped key, which spans several legal "
             "entities and rejects any request that does not name one."},
    {"name": "slash_account_id", "label": "Slash account id", "group": "Slash",
     "secret": False, "attr": "slash_account_id",
     "help": "Restricts the pull to one account. Blank pulls every account the "
             "key can see -- right for one account, wrong when several belong "
             "to different QuickBooks files."},
    {"name": "slash_lookback_days", "label": "Slash lookback days",
     "group": "Slash", "secret": False, "attr": "slash_lookback_days",
     "help": "How far back a cold start reaches. Dedup makes an overlap free, "
             "so this bounds a first pull rather than a normal one."},

    # --- matching -----------------------------------------------------------
    #
    # auto_threshold and review_threshold are NOT here. They exist on Settings
    # and nothing reads them: the Matching Algorithm table replaced them, and
    # putting a dial on this screen that moves nothing is worse than not
    # offering it -- somebody would set it, watch nothing change, and stop
    # trusting the rest of the page.
    # cad_usd_rate is NOT here. Each company already sets its own rate on the
    # QBO Sync screen, stored in the database and audited when it changes, and
    # a second place to set the same thing is a second answer to "what rate did
    # this charge use". The value on Settings remains what a fresh install
    # starts from.

    # --- pulls --------------------------------------------------------------
    {"name": "plaid_days_requested", "label": "Plaid history requested (days)",
     "group": "Card programs", "secret": False, "attr": "plaid_days_requested",
     "help": "Asked for at link time only. Plaid honors it when Transactions "
             "is first added to an Item; more later means re-linking."},
]

BY_NAME = {c["name"]: c for c in CATALOG}


def _key(company: str, name: str) -> str:
    return f"{company}|{name}"


def get(db: Session, company: str, name: str):
    """This company's value, or the deployed default.

    Returns the value TYPED like its fallback: a threshold comes back as a
    float and a lookback as an int, because every caller of the old
    `settings.auto_threshold` expects a number and a screen can only store
    text. A value that will not convert is ignored rather than raising -- a
    typo in one setting must not take the pull down.
    """
    spec = BY_NAME.get(name)
    fallback = getattr(settings, spec["attr"], "") if spec else ""
    if not company:
        return fallback
    from .models_db import CompanyVariableRow
    if spec and spec.get("connectors_only"):
        fallback = ""
    row = db.get(CompanyVariableRow, _key(company, name))
    if row is None or row.value == "":
        return fallback
    if spec and spec.get("secret"):
        # Decrypted here, so every existing caller keeps asking for a value and
        # getting one. Empty means it cannot be read -- almost always
        # JWT_SECRET having changed -- and the deployment's own value is a
        # better answer than nothing.
        from .secrets_store import decrypt as _decrypt
        plain = _decrypt(row.value)
        return plain if plain else fallback
    if isinstance(fallback, bool):
        return str(row.value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(fallback, int) and not isinstance(fallback, bool):
        try:
            return int(float(row.value))
        except ValueError:
            return fallback
    if isinstance(fallback, float):
        try:
            return float(row.value)
        except ValueError:
            return fallback
    return row.value


def set_value(db: Session, company: str, name: str, value: str,
              actor: str = "") -> None:
    """Store one override. An empty value DELETES it rather than storing "".

    Storing an empty string would mean "this company has no Airtable token",
    which is a different statement from "this company has not been configured
    and should use the deployed one" -- and the second is what somebody
    clearing a box almost always means.
    """
    from .models_db import CompanyVariableRow
    spec = BY_NAME.get(name)
    if spec is None:
        raise KeyError(name)
    kid = _key(company, name)
    row = db.get(CompanyVariableRow, kid)
    if str(value or "").strip() == "":
        if row is not None:
            db.delete(row)
        return
    if row is None:
        row = CompanyVariableRow(id=kid, company=company, name=name)
    # A SECRET IS ENCRYPTED ON THE WAY IN. Marked secret in the catalog means
    # an API key or token, which must not be readable in a database backup or
    # a console session. Everything else -- a base id, a lookback in days --
    # is stored as written, because hiding it would only make it harder to
    # read for no gain.
    raw = str(value).strip()
    if spec.get("secret"):
        from .secrets_store import encrypt as _encrypt
        row.value = _encrypt(raw)
    else:
        row.value = raw
    row.secret = bool(spec["secret"])
    row.updated_by = actor
    row.updated_at = datetime.utcnow()
    db.add(row)


def _hint(stored: str) -> str:
    """The masked tail of a stored secret, for the screen."""
    from .secrets_store import decrypt as _d, mask as _m
    plain = _d(stored or "")
    # Unreadable means JWT_SECRET changed: say so rather than show a blank,
    # which reads as "no key set" and sends somebody looking in the wrong
    # place.
    return _m(plain) if plain else "cannot be read \u2014 re-enter"


def _hint_for(name: str, stored: str) -> str:
    """The masked tail shown for a stored secret. A Google service account key
    is a JSON file whose last four characters are its closing braces, so it is
    shown by its private_key_id instead -- the id Google lists the key under."""
    if name == "google_service_account":
        import json as _jh
        from .secrets_store import decrypt as _d, mask as _m
        try:
            kid = (_jh.loads(_d(stored or "") or "{}") or {}).get("private_key_id", "")
        except ValueError:
            kid = ""
        if kid:
            return _m(kid)
        # No key id: its last four would be the file's closing braces.
        return "\u2022" * 8
    return _hint(stored)


def _mask_plain(value: str) -> str:
    """The last four of a value that is not secret but is long and opaque, such
    as the Microsoft App (client) ID -- shown the way keys are, for a uniform
    column."""
    from .secrets_store import mask as _m
    return _m(value or "")


def listing(db: Session, company: str) -> list[dict]:
    """The catalog with each entry's current state, safe to send to a browser.

    A secret's VALUE never leaves the server once stored. What the screen gets
    is whether one is set, who set it and when -- which is what a person needs
    to know, and nothing an attacker does.
    """
    from .models_db import CompanyVariableRow
    rows = {r.name: r for r in db.query(CompanyVariableRow)
            .filter(CompanyVariableRow.company == company).all()}
    out = []
    for spec in CATALOG:
        r = rows.get(spec["name"])
        fallback = getattr(settings, spec["attr"], "")
        out.append({
            "name": spec["name"], "label": spec["label"],
            "group": spec["group"], "secret": spec["secret"],
            "help": spec["help"],
            "set_here": r is not None,
            "value": "" if (spec["secret"] or r is None) else r.value,
            # A SECRET'S LAST FOUR CHARACTERS, and nothing else. Enough to tell
            # which key is stored -- the point when you are checking whether
            # the right one was pasted -- and useless to anybody else.
            "hint": (_hint_for(spec["name"], r.value) if (spec["secret"] and r is not None)
                     else (_mask_plain(r.value) if r is not None and spec["name"] == "microsoft_client_id"
                           else "")),
            # The deployed default, shown so a person can see what they would
            # be overriding. Blanked for secrets for the same reason.
            "default": "" if spec["secret"] else str(fallback),
            "updated_by": r.updated_by if r else "",
            "updated_at": (r.updated_at.isoformat() + "Z") if r and r.updated_at else "",
        })
    return out


# --- the active company, for code too low-level to be handed one -------------
#
# The values above are read in places that have no database session and no
# company in scope: the Airtable HTTP helper that builds an Authorization
# header, the Slash client that sets X-API-Key. Threading a company and a
# session down through every one of those signatures would touch twenty
# functions whose job has nothing to do with configuration.
#
# So the caller that DOES know the company declares it, and the low-level code
# reads it. A ContextVar rather than a module global, because two companies can
# be syncing at once and a global would let one pull read the other's token --
# the exact failure this whole area exists to prevent.
#
# The fallback is what makes this safe to adopt piecemeal: code running outside
# any `for_company` block reads the deployed environment, which is precisely
# what it did before. A caller that forgets the wrapper gets the OLD behavior,
# not another company's credentials.
_ACTIVE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "company_vars_active", default=None)


def snapshot(db: Session, company: str) -> dict:
    """Every override this company has, read once.

    Taken as a snapshot rather than queried per lookup: a sync reads the same
    token dozens of times, and a value that changed halfway through a pull
    would make the first half and the second half of one run disagree.
    """
    from .models_db import CompanyVariableRow
    from .secrets_store import decrypt as _decrypt
    if not company:
        return {}
    out = {}
    for r in (db.query(CompanyVariableRow)
              .filter(CompanyVariableRow.company == company).all()):
        if not r.value:
            continue
        spec = BY_NAME.get(r.name)
        if spec and spec.get("secret"):
            # DECRYPTED HERE TOO. This path loads every value at once and
            # bypasses get(), so decrypting only there handed callers the
            # ciphertext -- which is what the Slash client would then have
            # sent as its API key. A value that cannot be read is dropped, so
            # the deployment's own falls through instead.
            plain = _decrypt(r.value)
            if not plain:
                continue
            out[r.name] = plain
        else:
            out[r.name] = r.value
    return out


@contextmanager
def for_company(db: Session, company: str):
    """Run this block with one company's settings in force."""
    token = _ACTIVE.set(snapshot(db, company))
    tco = _ACTIVE_CO.set(company or "")
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        _ACTIVE_CO.reset(tco)


# Which company is in force -- for the rare write made from inside a sync,
# such as storing the new refresh token Microsoft hands back.
_ACTIVE_CO: contextvars.ContextVar[str] = contextvars.ContextVar("_cv_active_co", default="")


def active_company() -> str:
    return _ACTIVE_CO.get()


def value(name: str):
    """This setting, for whichever company is in force, else the deployment.

    Typed like its fallback, for the same reason get() is: a lookback is an int
    everywhere it is used and a screen can only store text.
    """
    spec = BY_NAME.get(name)
    fallback = getattr(settings, spec["attr"], "") if spec else ""
    # CONNECTORS ONLY. A key moved to the Connectors screen must never be read
    # from the deployment's environment again -- otherwise a key cleared or
    # never entered there is silently replaced by an old Railway value, and a
    # pull looks healthy while using a credential nobody can see.
    if spec and spec.get("connectors_only"):
        fallback = ""
    active = _ACTIVE.get()
    if not active or name not in active or not str(active[name] or "").strip():
        return fallback
    raw = active[name]
    if isinstance(fallback, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(fallback, int) and not isinstance(fallback, bool):
        try:
            return int(float(raw))
        except ValueError:
            return fallback
    if isinstance(fallback, float):
        try:
            return float(raw)
        except ValueError:
            return fallback
    return raw


def active_company_set() -> bool:
    """Whether a company's settings are in force. For diagnostics only."""
    return _ACTIVE.get() is not None


# --- card-program connections ------------------------------------------------
#
# A company may have several Slash programs or several Divvy accounts. The
# active one is declared the same way the active company is, and for the same
# reason: the code that builds an X-API-Key header has no company and no
# connection in scope, and threading both down through every HTTP helper would
# touch functions whose job is not configuration.
_CONN: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "card_connection_active", default=None)


def connections(db: Session, company: str, program: str = "") -> list:
    """This company's connections, newest label order, active ones first."""
    from .models_db import CardConnectionRow
    qy = db.query(CardConnectionRow).filter(CardConnectionRow.company == company)
    if program:
        qy = qy.filter(CardConnectionRow.program == program)
    return sorted(qy.all(),
                  key=lambda r: (not r.is_active, (r.label or "").lower()))


@contextmanager
def for_connection(row):
    """Run this block against one connection's credentials.

    Nested inside for_company, not instead of it: the connection supplies the
    key and the entity, the company still supplies everything else.
    """
    fields = {} if row is None else {
        "api_key": row.api_key or "",
        "legal_entity_id": row.legal_entity_id or "",
        "account_id": row.account_id or "",
        "lookback_days": row.lookback_days or 0,
        "key_var": row.key_var or "",
        "label": row.label or "",
    }
    token = _CONN.set(fields or None)
    try:
        yield
    finally:
        _CONN.reset(token)


def conn_value(field: str, fallback):
    """A field of the connection in force, or the fallback.

    Empty is treated as absent, not as "cleared": a connection row that names
    a label but no legal entity should use the company's, exactly as a company
    with no override uses the deployment's. One rule, three levels.
    """
    active = _CONN.get()
    if not active:
        return fallback
    v = active.get(field)
    if field == "api_key" and v:
        # DECRYPTED on the way out, so every caller keeps asking for a key and
        # getting one. Unreadable means JWT_SECRET changed; the fallback is a
        # better answer than handing a sync the ciphertext to authenticate
        # with.
        from .secrets_store import decrypt as _dec_key
        v = _dec_key(v)
    return v if v not in ("", 0, None) else fallback


def field_map(name_or_blank: str = "") -> dict:
    """This company's column mapping, as {field: their column name}.

    Text rather than a table because it is edited by hand, rarely, by somebody
    reading the broker's base alongside it -- and a malformed line should be
    ignorable rather than fatal. Unknown fields are dropped; a missing field
    simply means "use the default layout for it".
    """
    raw = name_or_blank or value("airtable_field_map") or ""
    known = {"lookup", "team", "email", "plan", "status", "card", "total",
             "section", "row", "seats"}
    out = {}
    for line in str(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip().lower(), v.strip()
        if k in known and v:
            out[k] = v
    return out


def sth_scope(db, company: str) -> str:
    """Which STH records a company reads: its own, and only its own.

    Every company now connects its own Airtable on Connectors -- its token and
    its base -- and the sync stores what it finds under that company. There is
    no shared, deployment-wide base any more, so there is no shared pool to
    fall back to: a company that is not connected reads nothing.

    The shared pool this used to fall back on was Y&S's old mirror. Reading it
    for any company without a base of its own would have let another broker's
    charges match against Y&S's season tickets.
    """
    return company or ""



# --- Bank balances reported by card-program APIs -----------------------------
# Plaid's live on the Plaid connection. A card program's are kept here, keyed
# by company and card account, with what the figure IS -- each program reports
# a different kind of number, and the grid says which.

def set_api_balance(db, company: str, source: str, amount, label: str) -> None:
    import json as _j
    from datetime import datetime as _dt
    from .models_db import AppSettingRow
    try:
        amt = None if amount is None else round(float(amount), 2)
    except (TypeError, ValueError):
        return
    if amt is None:
        return
    db.merge(AppSettingRow(key=f"api_balance:{company}:{source}", value=_j.dumps(
        {"amount": amt, "label": label, "at": _dt.utcnow().isoformat() + "Z"})))
    db.commit()


def get_api_balance(db, company: str, source: str) -> dict:
    import json as _j
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, f"api_balance:{company}:{source}")
    try:
        return _j.loads(row.value) if row else {}
    except ValueError:
        return {}



def airtable_setup() -> dict:
    """This company's Airtable setup: {"book": {...}, "sth": {...}}, either part
    optional. Empty when the company uses the default layout."""
    import json as _ja
    raw = value("airtable_setup") or ""
    try:
        d = _ja.loads(raw) if raw.strip() else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}



# STH columns that are no longer offered. Dropped when a setup is read, so a
# setup saved with them does not keep asking Airtable for them.
_RETIRED_STH_FIELDS = {"section", "row", "seats"}


def airtable_sources(part: str) -> list:
    """This company's sources for one part -- "book" (Profiles / Address Book)
    or "sth" (Season Ticket Records) -- as a list, one per base and table.

    A broker can keep its data in several places: a table per league, one base
    per year. Each source names its own base. A setup saved before sources were
    lists holds one dict for the part, and the base beside it; that reads as a
    list of one, on that base.
    """
    raw = (airtable_setup() or {}).get(part)
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) and raw else [])
    fallback = value("airtable_base_tickets") or ""
    out = []
    for s in items:
        if not isinstance(s, dict) or not (s.get("table") or (s.get("file") and s.get("tab"))):
            continue
        s = dict(s)
        s["provider"] = s.get("provider") or "airtable"
        if s["provider"] == "airtable":
            s["base"] = s.get("base") or fallback
        if part == "sth":
            s["fields"] = {k: v for k, v in (s.get("fields") or {}).items()
                           if k not in _RETIRED_STH_FIELDS}
        out.append(s)
    return out


def encrypt_plain_secrets(db) -> int:
    """At start-up: any value now marked secret but still stored readable (the
    Airtable base IDs, which were not secret before) is encrypted in place."""
    from .models_db import CompanyVariableRow
    from .secrets_store import encrypt as _enc, _PREFIX
    secret = {n for n, v in BY_NAME.items() if v.get("secret")}
    n = 0
    for r in db.query(CompanyVariableRow).filter(CompanyVariableRow.name.in_(secret)).all():
        if r.value and not str(r.value).startswith(_PREFIX):
            r.value = _enc(r.value)
            r.secret = True
            n += 1
    if n:
        db.commit()
    return n


def status_words(db, company: str) -> dict:
    """What status_kind reads for this company: its own words for every
    renewal status (and its own statuses), per kind, and the Dashboard's
    Engine Notation per kind (Renewal Status Words)."""
    from .renewal_status import words_by_kind
    return words_by_kind(db, company)


ENGINE_WORDS_KEY = "sth_engine_words"      # the form before Renewal Status Words


def engine_status_words(db):
    """The Engine Notation per kind {active, progress, dead}."""
    from .renewal_status import engine_statuses
    out = {"active": [], "progress": [], "dead": []}
    for s in engine_statuses(db):
        out[s["kind"]] += s["words"]
    return out


# How a broker can write the season beginning in 2026 (the Engine's 26/27).
SEASON_NOTATIONS = ["same", "2026", "2027", "2026/27", "2026-27", "2026/2027", "2026-2027",
                    # A broker a year BEHIND on two-year seasons: what the
                    # engine calls 26/27 is written 2025/26 there.
                    "2025/26", "2025-26", "2025/2026", "2025-2026"]
BEHIND_NOTATIONS = ("2025/26", "2025-26", "2025/2026", "2025-2026")


def season_notations() -> dict:
    """{LEAGUE: notation} as chosen on Renewal Years, for the company in scope."""
    import json as _jc
    raw = value("season_conventions") or ""
    try:
        d = _jc.loads(raw) if raw.strip() else {}
    except ValueError:
        d = {}
    out = {}
    for k, v in d.items():
        v = {"starts": "2026", "ends": "2027"}.get(v, v)     # the earlier form
        if v in SEASON_NOTATIONS:
            out[str(k).upper()] = v
    return out


def season_conventions() -> dict:
    """{LEAGUE: "starts" | "ends"}: what a SINGLE year means for that league in
    this broker's data. Only the single-year notations change the reading --
    "2027" for a season beginning in 2026 means the year it ends. Two-year
    notations are read as written; absent leagues use the built-in reading."""
    # "behind": the broker's two-year seasons run a year behind the engine's
    # (2025/26 written for the season beginning in 2026); a single year is
    # still read as the season starting then.
    return {k: ("ends" if v == "2027" else "behind" if v in BEHIND_NOTATIONS else "starts")
            for k, v in season_notations().items()
            if v in ("2026", "2027") or v in BEHIND_NOTATIONS}
