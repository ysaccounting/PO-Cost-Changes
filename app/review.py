"""
Review queue — what the frontend renders.

Takes the charges and bills already in the database, runs each charge through
the classifier, and for bill-payment charges ranks candidate bills. This is the
read model behind the review screens: nothing here writes.
"""

from __future__ import annotations


import json
import logging
import re
from collections import Counter

from sqlalchemy import select, func, case, literal
from sqlalchemy.orm import Session

from engine import Bill, Charge, ChargeClass, classify, suggest, set_hal_index
from engine.hal import HalIndex, HalRecord
from .models_db import BillRow, ChargeRow, HalRow

MAX_SUGGESTIONS = 10   # UI lets the reviewer show top 3 / 5 / 10


# name -> email, from the address-book mirror. Loaded once per request batch:
# a few hundred rows, consulted for every charge on the page.
_EMAIL_BY_NAME: dict[str, set[str]] | None = None


_EMAIL_BY_LOOSE: dict | None = None
# company -> (by name, by loose name). One address book per company.
_EMAIL_CACHE: dict = {}


# This module had no logger. Twice now a bare `except` in here has swallowed a
# NameError and left a feature silently absent -- once for the CAD conversion,
# once for the API staleness badge -- because there was nothing to log with and
# the handler just passed. One logger, defined once.
log = logging.getLogger("ys.review")


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


def ref_by_name(db, company: str, kind: str, name, realm: str = ""):
    """A QuickBooks vendor or account BY NAME, from THIS company's file only.

    QuickBooks numbers vendors and accounts separately in each company file, and
    the same names recur across files -- every broker has a "Buffalo Sabres"
    vendor and a "Prepaid Inventory:NHL:Buffalo Sabres (TC)" account, each with
    its own id. Looked up by name alone, the first match could be ANOTHER
    company's, and its id means something else here: a Sabres charge was sent
    to QuickBooks with an id that, in this file, belongs to a deleted
    "Philadelphia Flyers (DEP)" -- and QuickBooks refused it with a message that
    named neither.

    With no file known for the company, nothing is returned: a missing id fails
    visibly, a wrong one files things in the wrong place.
    """
    from .models_db import QboRefRow as _Qn
    name = (name or "").strip()
    if not name:
        return None
    realm = realm or _company_realm(db, company)
    if not realm:
        return None
    return (db.query(_Qn).filter(_Qn.kind == kind, _Qn.name == name,
                                 _Qn.realm_id == realm)
            .order_by(_Qn.active.desc()).first())


def _company_realm(db, company: str) -> str:
    """Which QuickBooks file this company reads from, or "".

    Local to this module so review.py does not import api.py, which imports it.
    An empty answer means "not mapped", and every caller treats that as "show
    nothing" rather than "show everything" -- the difference between a company
    that is not connected yet and one wearing another company's accounts.
    """
    from .models_db import CompanyRow, QboTokenRow
    if company:
        co = db.query(CompanyRow).filter(CompanyRow.name == company).first()
        if co is not None and co.realm_id:
            return co.realm_id
        return ""
    rows = db.query(QboTokenRow).all()
    return rows[0].realm_id if len(rows) == 1 else ""


def _loose_name(name: str) -> str:
    """A name reduced to what both sides agree on: first and last, letters only.

    The address book and the card statement write the same person differently
    -- "Moshe Sochaczewsky" against "Moshe A. Sochaczewsky", a trailing
    "(CA) Los Angeles 90004", a hyphen, a double space. Keyed on the raw string,
    every one of those is a miss, which is why roughly two thirds of cardholder
    names resolved to nothing and the email signal could not fire for them.

    First and last only, because a middle name is the part most likely to
    appear on one side and not the other.
    """
    import re as _re
    # Everything from the first bracket onward is decoration -- the address
    # book writes "Matthew Jude (CA) Los Angeles 90004", and taking the last
    # word of THAT makes the surname "angeles".
    n = (name or "").lower().split("(")[0]
    parts = [p for p in _re.split(r"[^a-z]+", n) if len(p) > 1]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    # Sorted, so "Sochaczewsky, Moshe" and "Moshe Sochaczewsky" are the same
    # key. Which order a name is written in is not information about who it is.
    a, b = parts[0], parts[-1]
    return " ".join(sorted((a, b)))


def _emails_for(db: Session, name: str, company: str = "") -> set[str]:
    """Every address on file for a cardholder.

    The cache is a module global that build_queue clears at the start of every
    request. FastAPI serves sync endpoints from a threadpool, so a concurrent
    request can null it between the None-check and the .get below -- which showed
    up as an intermittent "'NoneType' object has no attribute 'get'" while
    paging. Reading the globals into locals (and rebuilding locally when either
    is missing) makes this read independent of a concurrent reset: the reset
    reassigns the globals, but the locals already in hand are untouched.
    """
    # ONE COMPANY'S ADDRESS BOOK: a name in Ticket Guy's book is no evidence
    # about a Y&S cardholder.
    hit = _EMAIL_CACHE.get(company)
    by_name, by_loose = hit if hit else (None, None)
    if by_name is None or by_loose is None:
        from .models_db import ProfileEmailRow
        by_name = {}
        loose: dict = {}
        rows = db.query(ProfileEmailRow).filter(ProfileEmailRow.company == company).all()
        for r in rows:
            if not r.email:
                continue
            by_name.setdefault(r.name_key, set()).add(r.email)
            lk = _loose_name(r.name_key)
            if lk:
                loose.setdefault(lk, set()).add(r.email)
        # A loose key that lands on more than one PERSON is ambiguous, and a
        # wrong address is worse than none -- it scores as certainty. Those are
        # dropped; the exact key still works for them.
        owners: dict = {}
        for r in rows:
            lk = _loose_name(r.name_key)
            if lk:
                owners.setdefault(lk, set()).add(r.name_key)
        by_loose = {k: v for k, v in loose.items() if len(owners.get(k, ())) == 1}
        # Publish both together so a reader sees a consistent pair.
        _EMAIL_CACHE[company] = (by_name, by_loose)
    key = (name or "").strip().lower()
    hit = by_name.get(key)
    if hit:
        return hit
    return by_loose.get(_loose_name(key), set())


def reset_email_cache() -> None:
    global _EMAIL_BY_NAME, _EMAIL_BY_LOOSE
    _EMAIL_BY_NAME = None
    _EMAIL_BY_LOOSE = None
    _EMAIL_CACHE.clear()


def _to_charge(r: ChargeRow, db: Session | None = None) -> Charge:
    # The charge's own email wins; the address book fills the gap. Slash and WEX
    # exports name the cardholder but never their address, so without this the
    # email signal is dead for two of the three card programs.
    #
    # A profile usually has more than one address and a bill may name any of
    # them, so all are carried and any one counts as a match.
    known = _emails_for(db, r.cardholder_name, r.company or "") if db is not None else set()
    email = r.email or (sorted(known)[0] if known else "")
    if r.email:
        known = known | {r.email.strip().lower()}
    return Charge(
        charge_id=r.charge_id, company=r.company, source=r.source,
        amount=r.amount, txn_date=r.txn_date, card_last4=r.card_last4,
        cardholder_name=r.cardholder_name, email=email or None,
        alt_emails=known,
        order_number=r.order_number, raw_description=r.raw_description or "",
        merchant=(r.merchant or r.raw_description or "").strip(),
        is_credit=bool(r.is_credit),
    )


def _to_bill(r: BillRow) -> Bill:
    return Bill(
        bill_id=r.bill_id, company=r.company, amount=r.amount,
        txn_date=r.txn_date, balance=r.balance, line_count=r.line_count,
        vendor=r.vendor, doc_number=r.doc_number or "",
        name=r.name, email=r.email,
        order_number=r.order_number, memo=r.memo or "",
    )


_TEAM_ACCOUNTS: dict = {}
_TEAM_VENDORS: dict = {}


# Seeded, not hardcoded: these are the ones every ticket feed uses, and the
# table is editable so the next one does not need a code change.
DEFAULT_SYNONYMS = {
    "la": "los angeles", "ny": "new york", "nyc": "new york city",
    "sf": "san francisco", "sd": "san diego", "kc": "kansas city",
    "tb": "tampa bay", "no": "new orleans", "okc": "oklahoma city",
    "phi": "philadelphia", "philly": "philadelphia",
}


# Set per request from the Payment Tags tab.
# Off unless somebody turns it on. It was defaulting to on, so payments filed
# themselves into Categorized before anyone had asked for it -- and the checkbox
# showed unticked while the behavior was running, which is the worst of both.
_PAY_AUTOCODE = False


CC_VENDORS_KEY = "cc_vendors"
CC_VENDORS_DEFAULT = [{"match": "Cap One", "vendor": "Capital One"},
                      {"match": "BOA", "vendor": "Bank of America"}]


def cc_vendors(db: Session) -> list:
    """[{match, vendor}]: a card account whose name contains `match` pays
    `vendor`. Engine-wide; starts with the two cases that prompted it."""
    import json as _jcv
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, CC_VENDORS_KEY)
    if row is None:
        return [dict(x) for x in CC_VENDORS_DEFAULT]
    try:
        v = _jcv.loads(row.value or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def cc_vendor_for(db: Session, account_name: str) -> str:
    """The vendor the CC Vendors list gives a card account, or "". The longest
    matching name wins, so "Cap One Spark" can be told apart from "Cap One"."""
    low = (account_name or "").lower()
    best = ("", "")
    for x in cc_vendors(db):
        m = (x.get("match") or "").strip().lower()
        if m and re.search(r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", low) and len(m) > len(best[0]):
            best = (m, (x.get("vendor") or "").strip())
    return best[1]


def _vendor_for_program(db: Session, account_name: str) -> str:
    """The QuickBooks vendor for a card program, however the account is named.

    Exact first, then the longest vendor whose name STARTS the program name:
    "SlashPlat" and "Slash Prefund" are both the Slash vendor, and "Divvy
    (Credit)" is Divvy. Prefix rather than substring, so "Amex" cannot be
    matched by a vendor called "ex".
    """
    from .models_db import QboRefRow
    # CC VENDORS FIRST (Dashboard > Tags > CC Vendors): a card named "Cap One"
    # pays the vendor "Capital One", "BOA" pays "Bank of America" -- names that
    # matching by the account's own name could never connect.
    _cv = cc_vendor_for(db, account_name)
    if _cv:
        return _cv
    bare = _program_name(account_name)
    hit = _known_vendor(db, bare)
    if hit:
        return hit
    low = re.sub(r"[^a-z0-9]", "", bare.lower())
    if not low:
        return ""
    best = ""
    for r in db.query(QboRefRow.name).filter(
            QboRefRow.kind == "vendor").all():
        name = r[0] or ""
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key and len(key) >= 4 and low.startswith(key) and len(key) > len(best):
            best = name
    return best


def _program_name(account_name: str) -> str:
    """The card PROGRAM behind an account name.

    "Divvy (Credit)" and "Divvy Prefund" are two accounts belonging to one
    program, and the ledger has a single vendor for it. Everything from the
    first bracket or a known account-kind word onwards is dropped.
    """
    n = re.sub(r"\s*\(.*$", "", str(account_name or "")).strip()
    n = re.sub(r"\s+(credit|prefund|pre-fund|debit|charge|card|plat|platinum)$",
               "", n, flags=re.I).strip()
    return n


# --- tie-out ----------------------------------------------------------------
#
# A season charge is expected on a given day for a given amount, but neither
# lands exactly. Processors post a day or two late, a charge due on a Saturday
# arrives on the Monday, and an amount can be a cent or two out from dividing a
# total by the months. The tolerances are settings rather than constants
# because every team behaves slightly differently.
_TIE = {"days": 3, "weekend": True, "pct": 1.0}


def set_tie_settings(d: dict) -> None:
    global _TIE
    _TIE = {"days": int(d.get("days", 3) or 0),
            "weekend": bool(d.get("weekend", True)),
            "pct": float(d.get("pct", 1.0) or 0)}


def current_tie_settings() -> dict:
    return dict(_TIE)


# A PLAN'S CHARGE DAY, however it was saved: a date ("15"), "last", one of the
# old named schedules (-1, -2, -3), or a named weekday from Payment Plans --
# "second-tue", "last-fri". The named weekdays were read with int() and broke
# the whole Bank Feed for any company with a plan set to one.
_NTH = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
_WD = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_day(day):
    """An int (a date, or -1/-2/-3), ("wd", n, weekday) for a named weekday,
    or None when it cannot be read."""
    if day is None:
        return None
    if isinstance(day, (tuple, int)) and not isinstance(day, bool):
        return day                    # already read
    t = str(day).strip().lower()
    if not t:
        return None
    if t == "last":
        return -1
    if "-" in t[1:]:
        n, _, w = t.partition("-")
        if n in _NTH and w[:3] in _WD:
            return ("wd", _NTH[n], _WD[w[:3]])
    try:
        return int(float(t))
    except ValueError:
        return None


def _day_label(day) -> str:
    """How the charge day reads on screen. The negatives are named schedules."""
    d = _parse_day(day)
    if d is None:
        return ""
    if isinstance(d, tuple):
        # As Payment Plans writes them: "1st M", "2nd Tu"; "Last Friday".
        # "Last M", "Last Tu" -- the same short day as "1st M".
        names = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", -1: "Last"}
        return f"{names[d[1]]} {['M', 'Tu', 'W', 'Th', 'F', 'Sa', 'Su'][d[2]]}"
    # EXACTLY AS THE PAYMENT PLANS WINDOW WRITES THEM: "15th", "Last day".
    if d >= 1:
        n = int(d)
        suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suf}"
    return {-1: "Last day", -2: "Last wkdy", -3: "1st wkdy"}.get(d, str(d))


def _days_label(plan: dict) -> str:
    """Every charge day of a plan, as the Payment Plans window shows them
    ("1st M, 15th")."""
    days = plan.get("days") or ([plan.get("day")] if plan.get("day") else [])
    out = [x for x in (_day_label(d) for d in days) if x]
    return ", ".join(out)


def _tie_days(day, text) -> list:
    """Every day this team might charge on.

    A team listing "1  15" has two real answers and a charge lands on one of
    them, so testing only the first reported half its charges as late. The
    stored number stays the first -- it is what the grid sorts on -- but the
    text is the full picture, so tie-out reads that when it has it.
    """
    import re as _re
    out = []
    raw = str(text or "").strip()
    if raw:
        low = raw.lower()
        if low.startswith("last day"):
            out.append(-1)
        elif low.startswith("last"):
            out.append(-2)
        elif low.startswith("1st") or low.startswith("first"):
            out.append(-3)
        for n in _re.findall(r"\d+", raw):
            v = int(n)
            if 1 <= v <= 31 and v not in out:
                out.append(v)
    if not out and day is not None:
        d = _parse_day(day)
        if d is not None:
            out.append(d)
    return out


def _tie_date(txn_date, day, text=""):
    """True if the charge ties out to ANY of the team's charge days."""
    days = _tie_days(day, text)
    if not txn_date or not days:
        return None
    seen = [_tie_one(txn_date, d) for d in days]
    if all(v is None for v in seen):
        return None
    return any(v is True for v in seen)


def _tie_one(txn_date, day):
    """Did the charge land near enough to the expected day of the month?"""
    if not txn_date or not day:
        return None
    try:
        import calendar
        from datetime import date as _d, timedelta as _td
        last = calendar.monthrange(txn_date.year, txn_date.month)[1]
        d = _parse_day(day)
        if d is None:
            return None
        if isinstance(d, tuple):
            # A NAMED WEEKDAY: the nth (or last) such weekday of the charge's
            # own month.
            _, n, wd = d
            if n > 0:
                due = _d(txn_date.year, txn_date.month, 1)
                due += _td(days=(wd - due.weekday()) % 7 + 7 * (n - 1))
                if due.month != txn_date.month:
                    return None
            else:
                due = _d(txn_date.year, txn_date.month, last)
                due -= _td(days=(due.weekday() - wd) % 7)
        elif d == -1:
            # Last day of the month.
            due = _d(txn_date.year, txn_date.month, last)
        elif d == -2:
            # Last weekday of the month -- the last day that is not a weekend.
            due = _d(txn_date.year, txn_date.month, last)
            while due.weekday() >= 5:
                due -= _td(days=1)
        elif d == -3:
            # First weekday of the month.
            due = _d(txn_date.year, txn_date.month, 1)
            while due.weekday() >= 5:
                due += _td(days=1)
        elif isinstance(d, int) and d < 1:
            return None
        elif isinstance(d, int):
            # Clamped into the charge's own month: the 31st of February is the
            # 28th.
            due = _d(txn_date.year, txn_date.month, min(d, last))
        # A weekend due date is left alone by default: plenty of teams do
        # charge on a Saturday, and moving the start of the window forward made
        # those look late. With the setting on, a weekend due date simply gets
        # the extra days rather than a new start date -- which is the thing
        # people actually mean by "allow for weekends".
        _extra = 0
        if _TIE["weekend"] and due.weekday() >= 5:
            _extra = 2
        gap = (txn_date - due).days
        # The gap can straddle a month end: a charge on the 1st against a due
        # date of the 30th is one day late, not twenty-nine early.
        if gap < -20:
            gap += last
        elif gap > 20:
            gap -= last
        # Late is tolerated, early is not: a processor running behind is
        # ordinary, a charge arriving before it was due is a different event.
        return 0 <= gap <= _TIE["days"] + _extra
    except Exception:                       # noqa: BLE001
        return None


def _variance_pct(amount, total, months, share=None):
    """Actual against expected, as a signed percentage. Negative is under.

    Same expectation as _tie_amount -- the plan's share for the charge's own
    month where there is one, an even split otherwise. The two must agree or
    the grid says "out of tolerance" beside a variance of zero.
    """
    if amount is None or total is None:
        return None
    if not months and not share:
        return None
    try:
        from decimal import Decimal as _D
        expected = (_D(str(total)) * _D(str(share)) / _D("100") if share
                    else _D(str(total)) / _D(str(months)))
        if expected == 0:
            return None
        return float(((_D(str(amount)) - expected) / expected * 100)
                     .quantize(_D("0.1")))
    except Exception:                       # noqa: BLE001
        return None


def _expected_fields(row, grand, plan, grand_cad=None, cad_rate=None) -> dict:
    """expected_amount and expected_calc for the STH line. A Canadian team's
    season total is in CAD, so its working shows the third factor: the CAD to
    USD rate on the charge's date."""
    if row.is_credit or grand is None or not plan:
        return {"expected_amount": "", "expected_calc": ""}
    from decimal import Decimal as _D
    money = lambda v: f"${_D(str(v)).quantize(_D('0.01')):,}"
    cad = grand_cad is not None and cad_rate
    base = _D(str(grand_cad)) if cad else _D(str(grand))
    rate = _D(str(cad_rate)) if cad else _D("1")
    tot_txt = (f"Total cost CA{money(grand_cad)}" if cad else f"Total cost {money(grand)}")
    rate_txt = (f" \u00d7 {cad_rate} (CAD \u2192 USD on {row.txn_date:%-m/%-d/%Y})" if cad else "")
    try:
        share = _plan_share(plan, row.txn_date)
        if share:
            exp = (base * _D(str(share)) / _D("100") * rate).quantize(_D("0.01"))
            pct = f"{_D(str(share)).normalize():f}"
            return {"expected_amount": str(exp),
                    "expected_calc": f"{tot_txt} \u00d7 {pct}% ({row.txn_date:%B}){rate_txt} = {money(exp)}"}
        if plan.get("months"):
            exp = (base / _D(str(plan["months"])) * rate).quantize(_D("0.01"))
            return {"expected_amount": str(exp),
                    "expected_calc": f"{tot_txt} \u00f7 {plan['months']} months{rate_txt} = {money(exp)}"}
    except Exception:                       # noqa: BLE001
        pass
    return {"expected_amount": "", "expected_calc": ""}


def _pick_plan(plans, amount, total, txn_date):
    """Which of a team's plans this charge belongs to, judged on amount alone.

    A team may offer a six-month and a ten-month schedule for the same seats,
    and nothing about the charge says which one the buyer took -- not the
    vendor, not the memo, not the plan type on the record. The installment size
    is the only signal, so each plan is asked what it would expect in this
    month and the closest is chosen.

    One plan means no choice to make, which is every team today. None of them
    landing close means no plan is claimed, rather than the least-wrong one
    being forced: a wrong plan produces a confident wrong expectation, which is
    worse than none.
    """
    live = [p for p in (plans or []) if p]
    if not live:
        return None
    if len(live) == 1:
        return live[0]
    if not amount or not total:
        return live[0]

    best, best_gap = None, None
    for p in live:
        share = _plan_share(p, txn_date)
        if not share:
            continue
        expected = float(total) * float(share) / 100.0
        gap = abs(float(amount) - expected)
        if best_gap is None or gap < best_gap:
            best, best_gap = p, gap
    if best is None:
        return live[0]
    # Within a quarter of the expected installment. Beyond that the charge is
    # not recognizably either plan, and claiming one would invent a fact.
    share = _plan_share(best, txn_date) or 0
    expected = float(total) * float(share) / 100.0
    if expected and best_gap > expected * 0.25:
        return None
    return best


def _plan_share(plan, txn_date):
    """The percentage of the season due in the charge's own month, or None.

    None where the team has no payment plan, or where the plan charges nothing
    in that month -- in which case the caller falls back to an even split
    rather than treating the expectation as zero, because a charge arriving in
    a month the plan does not cover is a question about the plan, not proof
    that nothing was due.
    """
    if not plan or not txn_date:
        return None
    shares = plan.get("shares")
    if not shares:
        return None
    own = shares.get(txn_date.month - 1) or None
    if own:
        return own

    # THE FIRST CHARGE MAY LAND THE MONTH BEFORE THE PLAN STARTS.
    #
    # Renewals are billed ahead of the season: a plan running January to
    # October is commonly charged first in December. That charge falls in a
    # month the plan does not cover, so this returned None and the caller fell
    # back to an even split -- an expectation that is wrong by exactly the
    # amount the first installment differs from the average.
    #
    # Only the month immediately before the FIRST month of the plan, and only
    # when that month itself charges nothing. Anything looser would let a
    # charge in any empty month claim its neighbor's share.
    live = sorted(i for i, v in shares.items() if v)
    if not live:
        return None
    first = live[0]
    prior = (first - 1) % 12
    if (txn_date.month - 1) == prior and not shares.get(prior):
        return shares.get(first) or None
    return None


def _tie_amount(amount, total, months, share=None):
    """Is the charge within tolerance of the expected installment?

    `share` is the percentage of the season falling in the charge's own month,
    from the team's payment plan. Where there is one the installment is that
    share of the total; where there is not it falls back to an even split
    across `months`, which is all the Airtable arrangement could express.

    That difference matters on any plan that is not even -- a season charged
    40/30/30 has no month equal to a third of it, so every charge on it read
    as out of tolerance under the old arithmetic.
    """
    if amount is None or total is None:
        return None
    if not months and not share:
        return None
    try:
        from decimal import Decimal as _D
        expected = (_D(str(total)) * _D(str(share)) / _D("100") if share
                    else _D(str(total)) / _D(str(months)))
        if expected == 0:
            return None
        gap = abs(_D(str(amount)) - expected) / expected * 100
        return gap <= _D(str(_TIE["pct"]))
    except Exception:                       # noqa: BLE001
        return None


# Shorter than this and an alias is too easy to hit by accident: "att" sits
# inside "seattle", and a three-letter venue code inside half the merchants on
# a statement.
_ALIAS_MIN = 8

# A mascot has to carry the team on its own, so it needs more length than an
# alias somebody wrote deliberately. Six lets through "Chargers", "Buccaneers"
# and "Cowboys" while keeping out "Stars", "Bulls", "Jets" and "Kings".
_MASCOT_MIN = 6


# Names that are never a team, whatever the Airtable column says. Live Nation
# is a promoter: it arrives down the Teams/Venues column because its charges
# are ticket charges, and every variant -- LIVE NATION GLEN HELEN, LIVE NATION
# 713 -- was claiming rows it has no business claiming.
#
# Matched as a SUBSTRING of the team name rather than by equality, because
# there is no one name to exclude: the source carries a row per venue. One
# entry here removes all of them, from the matcher and from the screen, and no
# new row can reintroduce the problem.
# Nothing is excluded by TEAM NAME any more.
#
# "live nation" was here because promoter venues under the z-LIVE NATION league
# were claiming charges that belonged to the club playing the venue. Those rows
# are wanted again -- and the mechanism that made them harmful has since been
# fixed properly: aliases are keyed by league, so a venue can no longer lend
# itself across leagues, and a venue claimed by two teams settles nothing
# rather than picking one.
NEVER_TEAM_SUBSTRINGS: tuple = ()

# Whole LEAGUES that are not leagues. Airtable files promoter venues under
# "z-LIVE NATION", so the exclusion belongs on the league rather than on each
# venue: one entry removes every row filed under it, including ones added
# later, and it does not depend on the venue names following any pattern.
NEVER_LEAGUE_SUBSTRINGS = (
    # "z-LIVE NATION", not "Live Nation". They are two different leagues in the
    # Teams Table: the plain one carries real promoter rows that should claim,
    # and the z- prefixed one is the parked set that should not.
    #
    # The prefix is what separates them, and it does so exactly: "z-live
    # nation" does not occur in "live nation", so excluding one leaves the
    # other untouched. Matching on "live nation" alone caught both.
    "z-live nation",
    # NCAAF and NCAAB duplicate the NCAA rows: the same university appears
    # under all three, so a bank detail naming "Ohio State Buckeyes" matched
    # two or three teams at once and the claim was thrown away as ambiguous.
    # NCAA carries the team; which SPORT it is comes from the season record,
    # resolved in _sport_hint below rather than from a second alias row.
    #
    # Exact enough as substrings: "ncaaf" does not occur in "ncaa", so the
    # league that should survive is untouched.
    "ncaaf",
    "ncaab",
)


# Words that name a college sport, and the league prefix its accounts carry.
#
# One university has a football account and a basketball account -- "NCAAF:Ohio
# State Buckeyes (TC)" and "NCAAB:..." -- and the team name alone cannot tell
# them apart. The STH record can: a season ticket account is for one sport, and
# says so in its team or plan text.
_SPORT_WORDS = (
    ("NCAAF", ("football", "fball", "fb ", " fb", "gridiron", "bowl")),
    ("NCAAB", ("basketball", "bball", "hoops", "mbb", "wbb", "hockey east")),
)


# A team row that names a PROMOTER rather than a team.
#
# "Live Nation (Generic)" and "Broadway (Generic)" exist so a bank detail like
# "LN PREMIUM" or "BROADWAYACROSSAMERICA" is recognized at all -- but they are
# not teams. There is no Live Nation vendor and no Live Nation ticket account,
# so resolving one and then looking up its vendor and category produced
# whatever happened to match first, which is where the random codings came from.
#
# The promoter says WHERE the ticket was bought, not what it was for. The
# season account says what it was for, and is looked up by cardholder instead.
def is_generic_team(team: str) -> bool:
    return "(generic)" in (team or "").strip().lower()


def holdings_for(db: Session, cardholder_name: str = "", email: str = "") -> list:
    """The season-account teams this cardholder holds, bare names.

    ONE answer to "who is this person and what do they hold", used everywhere
    that question is asked.

    It existed twice: the STH block resolved a cardholder through the index,
    which normalizes names and keys by email, while the disambiguation beside
    it ran its own `holder_name == cardholder_name` query. Same intent, and
    only one of them survived a holder name like "Finley Frazier (MN) Mankato
    56001" against a card that says "Finley Frazier". Every ambiguous bank
    detail therefore stayed ambiguous.

    Both now call this. Two copies that must agree is a bug waiting to happen
    again; one copy cannot disagree with itself.
    """
    try:
        idx = load_hal_index(db)
        if idx is None:
            return []
        # THE INDEX'S OWN lookup(), not a hand-rolled one.
        #
        # I reached into by_name and by_squash directly, which misses every
        # holder whose STH name carries a qualifier -- "Finley Frazier (MN)
        # Mankato 56001" against a card that says "Finley Frazier". The index
        # has by_loose for exactly that case, and lookup() tries the forms in
        # the right order: exact, card-suffix stripped, squashed, loose.
        #
        # Reimplementing half of it is what left Broadway and Live Nation
        # unresolved after the first fix appeared to work in a test whose
        # fixture happened to match on email.
        recs = list(idx.lookup(cardholder_name or "",
                               emails=[email] if email else None) or [])
        if not recs and email:
            recs = list(idx.by_email.get(email.strip().lower(), []) or [])
        return [_bare_team(getattr(h, "team", "") or "") for h in recs]
    except Exception:                       # noqa: BLE001
        log.exception("could not read the cardholder's STH holdings")
        return []


def _sport_hint(*texts: str) -> str:
    """The league prefix a college account should carry, or "".

    Read from the season record's own words. Returns "" when nothing says --
    and "" must mean "do not narrow", never a guess, because picking football
    for a basketball charge files it against the wrong account entirely.
    """
    blob = " ".join((t or "") for t in texts).lower()
    if not blob.strip():
        return ""
    for league, words in _SPORT_WORDS:
        if any(w in blob for w in words):
            return league
    return ""


def is_never_row(team: str = "", alias: str = "", league: str = "") -> bool:
    """Is this alias row excluded outright, whichever column carries the name?

    A promoter turns up in all three: as its own team row, as a bank-detail
    note written against a real club, and as the league every one of its venues
    is filed under. Checking one column and not the others left the rows the
    other two carried still matching.
    """
    if any(sub in (league or "").lower() for sub in NEVER_LEAGUE_SUBSTRINGS):
        return True
    return any(sub in (team or "").lower() or sub in (alias or "").lower()
               for sub in NEVER_TEAM_SUBSTRINGS)


CAD_RATE_KEY = "cad_usd_rate"


def amex_parents(db: Session, company: str) -> dict:
    """card account -> its parent card's five-digit ending ("" when not
    known), for this company's American Express card accounts."""
    import re as _rx
    from .models_db import SourceAccountRow as _SAx, PlaidItemRow as _PIx
    out = {}
    try:
        masks = {r.source: str(r.mask or "") for r in db.query(_PIx).filter(_PIx.company == company).all()}
        for r in db.query(_SAx).filter(_SAx.company == company).all():
            names = " ".join(str(x or "") for x in (r.nickname, r.qbo_account_name, r.source))
            if not _rx.search(r"\bamex\b|american\s+express", names, _rx.I):
                continue
            m = _rx.search(r"(?<!\d)(\d{5})(?!\d)", names)
            mk = masks.get(r.source, "")
            out[r.source] = m.group(1) if m else (mk if len(mk) == 5 and mk.isdigit() else "")
    except Exception:                                           # noqa: BLE001
        pass
    return out


def card_ending(row, parents: dict) -> str:
    """CC Last 4/5: an American Express card's five-digit ending -- from the
    bank detail or memo (a five-digit group ending in its last four), or else
    its parent card's fifth-from-last digit (the account digit every card on
    the account shares) before its last four. Every other card: its last four."""
    import re as _rx
    l4 = str(getattr(row, "card_last4", "") or "")
    src = getattr(row, "source", "")
    if src not in parents or len(l4) != 4:
        return l4
    for text in (getattr(row, "merchant", ""), getattr(row, "memo", ""), getattr(row, "raw_description", "")):
        for m in _rx.finditer(r"(?<!\d)(\d{5})(?!\d)", str(text or "")):
            if m.group(1).endswith(l4):
                return m.group(1)
    p5 = parents.get(src) or ""
    return (p5[0] + l4) if p5 else l4


def sth_costs_in_usd(db: Session, company: str) -> bool:
    """Whether this broker lists Canadian teams' costs in USD already -- its
    Season Ticket Records sources' "Canadian team costs are in" answer. Records
    don't carry their source, so the answer is the company's: USD when a
    source says USD and none says CAD."""
    try:
        from . import company_vars as _cv
        with _cv.for_company(db, company):
            srcs = _cv.airtable_sources("sth") or []
        said = {str(x.get("cad_costs") or "").lower() for x in srcs if isinstance(x, dict)}
        return "usd" in said and "cad" not in said
    except Exception:                                           # noqa: BLE001
        return False


def cad_rate(db: Session) -> float:
    """One Canadian dollar in US dollars.

    Read from the database, falling back to the environment default. Canadian
    teams hold their season totals in CAD in Airtable while the charge posts to
    a US card in USD, so this is what makes the two comparable -- without it
    every Canadian plan reads about 37% short, consistently enough to look like
    a real underpayment rather than an arithmetic error.
    """
    from .models_db import AppSettingRow
    row = db.get(AppSettingRow, CAD_RATE_KEY)
    try:
        v = float((row.value if row else "") or 0)
    except Exception:                                           # noqa: BLE001
        v = 0
    from .config import settings as _cfg
    return v if v > 0 else float(_cfg.cad_usd_rate or 0.73)


OWN_TEAMS_KEY = "own_teams_only:"      # + company -> "true"


def own_teams_only(db, company: str) -> bool:
    """Configuration > Settings: this company ignores the Dashboard's Teams
    (their bank details, venues and names) and uses only its own Teams tab."""
    from .models_db import AppSettingRow as _ASo
    r = db.get(_ASo, OWN_TEAMS_KEY + (company or ""))
    return bool(r and (r.value or "") == "true")


def team_claim_pairs(db, _with_why: bool = False, company: str = ""):
    """Every (text, team) pair that identifies a team, from one place.

    THE source of truth for "which team does this bank detail name". Three
    copies of this used to exist -- one for coding, one for the STH hint, one
    registered with STH's own resolver -- each built separately from the same
    table. They drifted apart three times in a day, and each drift looked like
    a different bug: a team that coded but found no record, a record belonging
    to the wrong team, a rule that worked on one screen and not another.

    Three kinds of claim, in descending order of how deliberate they are:

      1. a written alias -- a venue or bank-detail note somebody maintained,
      2. the team's own name, which is not an alias of itself and was the
         omission behind "DALLAS STARS TICKETS E-" finding nothing, and
      3. the mascot, so "BUCCANEERS TICKETS" resolves without anyone having to
         write an alias for all five hundred teams.

    Two guards apply throughout, and they are why this is safe to widen:
    a claim used by more than one team is dropped rather than guessed at, and
    a claim too short to be distinctive is left out.
    """
    from .models_db import TeamAliasRow as _TA
    rows = [(a, t, k) for a, t, k, lg
            in db.query(_TA.alias, _TA.team, _TA.kind, _TA.league).all()
            if not is_never_row(t, a, lg)]
    # THIS COMPANY IGNORES THE DASHBOARD'S TEAMS (Configuration > Settings):
    # only the teams on its own Teams tab name a team in its bank details.
    if company and own_teams_only(db, company):
        rows = []
    # THIS COMPANY'S OWN TEAMS (Configuration > Teams): its added teams join
    # the list, and a team it overrides has its bank details and venues
    # replaced by the company's -- for this company only.
    if company:
        try:
            from .company_teams import claim_rows as _crows
            _extra, _over = _crows(db, company)
            rows = [(a, t, k) for a, t, k in rows
                    if not (t and t.lower() in _over and k in ("note", "venue"))] + _extra
        except Exception:                               # noqa: BLE001
            pass
    teams = sorted({t for _, t, _k in rows if t})

    # Words too common in ordinary commerce to identify a team, however well
    # they match. "Athletics" is a gym, a school department, a sportswear shop
    # and an MLB club, and the first three vastly outnumber the fourth in a
    # card feed. Applies to the TEAM NAME too, not only to aliases -- the
    # club's own name is the whole problem here. Reaching it needs a written
    # alias or a rule, where somebody has stated the intent.
    # Two maintained lists, both editable on the Teams Table screen.
    #
    #   NO ALGORITHM  the team stays on the screen and can still be reached by
    #                 a written alias or a rule, but nothing about its name is
    #                 inferred. "Athletics" belongs here: it is a gym, a school
    #                 department and a sportswear shop far more often than it
    #                 is the MLB club.
    #   HIDDEN        the team is not a team for this purpose at all and should
    #                 not occupy a row. Live Nation is a promoter that arrived
    #                 through the same Airtable column.
    #
    # Hidden implies no algorithm -- a team nobody can see should not be
    # quietly steering the matcher.
    _NEVER_CLAIM = set()
    try:
        from .models_db import AppSettingRow as _AS
        import json as _json
        for _key in ("team_no_algorithm", "team_hidden"):
            _r = db.get(_AS, _key)
            for _t in _json.loads((_r.value if _r else "") or "[]"):
                _NEVER_CLAIM.add(str(_t).strip().lower())
    except Exception:                       # noqa: BLE001
        pass

    # An excluded team contributes nothing: not its name, not its mascot, not
    # its aliases. Filtered here rather than at each claim site so there is one
    # place to look when a team stops matching.
    def _excluded(team: str) -> bool:
        low = _bare_team(team).lower()
        if low in _NEVER_CLAIM:
            return True
        return any(sub in low for sub in NEVER_TEAM_SUBSTRINGS)

    rows = [(a, t, k) for a, t, k in rows if not _excluded(t)]
    teams = [t for t in teams if not _excluded(t)]

    claims: dict = {}
    # (key -> team -> (outcome, extra)). Kept so the Teams Table screen can
    # say WHY an alias does nothing, rather than showing a row somebody wrote
    # that silently has no effect. Every outcome that ends in "this claims
    # nothing" is recorded here.
    why: dict = {}
    # Which tier settled each key. A later, weaker tier must not disturb an
    # answer an earlier one already gave -- including an earlier one that
    # deliberately gave no answer.
    settled: dict = {}

    def _claim(text: str, team: str, minimum: int, tier: str = "") -> None:
        key = (text or "").strip().lower()
        if not team or len(key) < minimum:
            if key:
                why.setdefault(key, {})[team] = ("short", minimum)
            return
        if key in _NEVER_CLAIM:
            why.setdefault(key, {})[team] = ("off", "")
            return
        if tier and settled.get(key, tier) != tier:
            # A stronger tier has already spoken. Recorded rather than dropped
            # silently: this is the case where a venue looks like it does
            # nothing because a note elsewhere outranks it.
            # Not if THIS team is the one that already claimed it at the
            # stronger tier. The Red Wings hold Little Caesars Arena as both a
            # note and a venue; the venue pass arriving second must not report
            # the team as outranked by its own note.
            if why.get(key, {}).get(team, ("",))[0] != "active":
                why.setdefault(key, {})[team] = ("outranked", settled[key])
            return
        if tier:
            settled[key] = tier
        # Already claimed by somebody else AT THIS TIER: neither of them gets it.
        if key in claims and claims[key] != team:
            claims[key] = None
            why.setdefault(key, {})[team] = ("shared", tier)
            for _t in list(why.get(key, {})):
                if why[key][_t][0] == "active":
                    why[key][_t] = ("shared", tier)
        else:
            claims[key] = team
            why.setdefault(key, {})[team] = ("active", tier)

    # NOTES BEFORE VENUES, and a note is not canceled by a venue.
    #
    # The two columns are different kinds of statement, which is why the model
    # keeps them apart: a venue is a fact about where a team plays, a note is
    # something somebody read off a statement and wrote down on purpose.
    #
    # Little Caesars Arena is the venue for BOTH the Red Wings and the Pistons,
    # so as equal claims they canceled and the string identified neither. But
    # when it is written as a NOTE on the Red Wings, that is a person saying
    # "charges reading this are hockey" -- knowledge the venue field does not
    # contain and cannot contradict. Treating the two as equal threw the
    # deliberate statement away and kept the incidental one.
    #
    # Within a tier the old rule stands: two teams both claiming a string as a
    # note is a real ambiguity, and a venue cannot break that tie either.
    for tier in ("note", "venue"):
        for alias, team, kind in rows:
            if (kind or "note") == tier:
                _claim(alias, team, _ALIAS_MIN, tier=tier)
    for team in teams:
        # The team's own name has no minimum -- it is the most deliberate
        # claim there is, and a short one like "FC Dallas" is still exact.
        key = _bare_team(team).lower()
        if key and key not in _NEVER_CLAIM:
            claims[key] = None if (key in claims and claims[key] != team) else team
    # Words that serve as a CITY or a club prefix somewhere in the league, so a
    # mascot claim never hands one out.
    #
    # The mascot rule assumes a team is named "City Mascot" and takes the last
    # word. Soccer clubs are named the other way round -- FC Dallas, Inter
    # Miami, Sporting Kansas City, Real Salt Lake -- so the last word is the
    # CITY, and "dallas" was claimed as FC Dallas's mascot. Any bank detail
    # containing the word then resolved to that club: DALLAS SUMMER MUSICALS
    # pulled FC Dallas season-ticket records, once per cardholder, all wrong.
    #
    # No city list is needed to see this. A word used ahead of another team's
    # mascot is a place, not a mascot -- "dallas" leads Dallas Stars and Dallas
    # Cowboys, "city" leads Kansas City Chiefs -- so the league's own names say
    # which words are geography.
    place_words = set()
    for team in teams:
        w = _bare_team(team).lower().split()
        place_words.update(w[:-1])          # everything but the final word

    for team in teams:
        words = _bare_team(team).lower().split()
        if len(words) < 2:
            continue
        # Last two words before the last one: "Red Wings" is far more
        # distinctive than "Wings", so it gets the chance to claim first.
        for cand in ([" ".join(words[-2:])] if len(words) > 2 else []) + [words[-1]]:
            # A single word that is geography elsewhere claims nothing. Pairs
            # are left alone: "salt lake" and "kansas city" are distinctive
            # even though both words are places.
            if " " not in cand and cand in place_words:
                continue
            _claim(cand, team, _MASCOT_MIN)

    if _with_why:
        return [(k, v) for k, v in claims.items() if v], why
    return [(k, v) for k, v in claims.items() if v]


def alias_status(db) -> dict:
    """For every written alias: does it do anything, and if not, why not?

    An alias that claims nothing looks exactly like one that works -- the row
    is there, the text is right, and the only way to find out was to ask. Every
    dead end in this session was one of these:

      shared     two teams claim the string at the same strength, so it names
                 neither. Little Caesars Arena as a venue on both Detroit teams.
      outranked  a note elsewhere claims it, and a note beats a venue. Usually
                 fine, and worth seeing so a later note on the other team is not
                 a mystery when it silently switches the answer.
      off        the team is excluded from matching.
      short      below the minimum length, so it is never tested at all.

    Keyed "team\x00alias" because both are needed and neither is unique alone.
    """
    from .models_db import TeamAliasRow as _TA
    pairs, why = team_claim_pairs(db, _with_why=True)
    # Excluded teams are filtered out before any claim is attempted, so nothing
    # is recorded against them and their aliases would read as merely "unused".
    # The reason is knowable and worth saying: somebody switched the team off.
    off = set()
    try:
        from .models_db import AppSettingRow as _AS
        for k in ("team_no_algorithm", "team_hidden"):
            r = db.get(_AS, k)
            for t in json.loads((r.value if r else "") or "[]"):
                off.add(str(t).strip().lower())
    except Exception:                       # noqa: BLE001
        pass
    out = {}
    for alias, team in db.query(_TA.alias, _TA.team).all():
        key = (alias or "").strip().lower()
        if not key or not team:
            continue
        if _bare_team(team).lower() in off:
            out[f"{team}\x00{key}"] = {"status": "off"}
            continue
        outcome, extra = (why.get(key, {}).get(team)
                          or ("active" if pairs and dict(pairs).get(key) == team
                              else "unused", ""))
        entry = {"status": outcome}
        if outcome == "shared":
            entry["with"] = sorted({t for t, (o, _e) in why.get(key, {}).items()
                                    if o == "shared" and t != team})
        elif outcome == "outranked":
            entry["by"] = str(extra)
        elif outcome == "short":
            entry["min"] = extra
        out[f"{team}\x00{key}"] = entry
    return out


def team_claim_candidates(db) -> dict:
    """Keys that name SEVERAL teams, and which ones.

    team_claim_pairs drops these, and that is right for coding: "KSE" is the
    Kroenke company and it bills for both the Nuggets and the Avalanche, so the
    string alone cannot say which. But dropping it also throws away the fact
    that the field has been narrowed from thirty teams to two.

    This returns that narrowing, so a second stage can finish the job with
    evidence the bank detail does not carry -- which team the cardholder
    actually holds, which day of the month the plan bills, what the installment
    comes to. MLSE (Maple Leafs and Raptors) is the same shape, as is a college
    whose football and basketball tickets bill identically.

    Deliberately NOT merged into team_claim_pairs. A candidate set is a weaker
    kind of answer and has to stay visibly weaker, or it gets used as though
    somebody had stated it.
    """
    from .models_db import TeamAliasRow as _TA
    rows = db.query(_TA.alias, _TA.team).all()

    skip = set()
    try:
        from .models_db import AppSettingRow as _AS
        for key in ("team_no_algorithm", "team_hidden"):
            r = db.get(_AS, key)
            for t in json.loads((r.value if r else "") or "[]"):
                skip.add(str(t).strip().lower())
    except Exception:                       # noqa: BLE001
        pass

    by_key: dict = {}
    for alias, team in rows:
        key = (alias or "").strip().lower()
        if not key or len(key) < _ALIAS_MIN or not team:
            continue
        if _bare_team(team).lower() in skip:
            continue
        by_key.setdefault(key, set()).add(_bare_team(team))
    # Only the genuinely shared ones; a key naming one team is already handled.
    return {k: sorted(v) for k, v in by_key.items() if len(v) > 1}


def _bare_team(team: str) -> str:
    """The team name without a trailing venue.

    Airtable records some teams as "New York Islanders (UBS Arena)". That
    string is a label, not a vendor and not an account, and using it verbatim
    put a vendor in the field that QuickBooks has never heard of.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", team or "").strip()


def _same_team(want: str, got: str) -> bool:
    """Do a hint and a STH record's team name refer to the same team?

    They are compared as word sequences, not as strings, because the two sides
    come from different systems and are spelled differently on purpose:

        STH / Airtable   "Florida Panthers"
        QuickBooks       "Florida Panthers Hockey Club"

    The hint is whichever of those the row happens to be carrying. On a charge
    the engine has never touched it is the TEAM, read from the alias table. On
    a charge a previous pass has already coded, the row carries the persisted
    QuickBooks VENDOR instead -- so the same charge produced a match before a
    resync and none afterwards, and the difference looked like the alias had
    stopped working.

    Exact equality is therefore the wrong test. But so is plain containment:
    "dallas" appears in both "Dallas Stars" and "FC Dallas", and those are two
    teams one person may well hold accounts with. So one name must be a
    CONTIGUOUS RUN OF WHOLE WORDS inside the other:

        florida panthers  <  florida panthers hockey club     same team
        dallas stars      ?  fc dallas                        different teams
        ny islanders      ?  new york islanders               different -- see below

    The last one is deliberate. Abbreviations are somebody's decision, not
    something to infer, and the Abbreviations table is where that decision is
    recorded -- so both sides are expanded through it first and "ny" becomes
    "new york" only because a person said so.
    """
    from engine.hal import expand_text as _ex
    a = (_ex(want) or want or "").split()
    b = (_ex(got) or got or "").split()
    if not a or not b:
        return False
    if len(a) > len(b):
        a, b = b, a
    n = len(a)
    return any(b[i:i + n] == a for i in range(len(b) - n + 1))


# Words that say what KIND of club a team is, not which one. Stripped from
# both sides before a name is compared, so the same club spelled "SC",
# "S.C.", "Soccer" or "Soccer Club" still resolves to one vendor.
_CLUB_WORDS = {"fc", "sc", "cf", "afc", "soccer", "club", "football",
               "athletic", "the", "inc", "llc", "ltd", "co"}


def _vendor_words(name: str) -> frozenset:
    # Single characters go too: "S.C." tokenizes to "s" and "c", which are the
    # same suffix written with full stops.
    return frozenset(w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
                     if len(w) > 1 and w not in _CLUB_WORDS)


def _team_vendor(db: Session, team: str, sport: str = "") -> str:
    """The QuickBooks vendor for a team, or "" if there isn't one.

    Checked rather than assumed: a vendor prefilled into the field has to be
    one Add can actually post, and the server refuses an unknown name -- so an
    unchecked guess turns into a failure at the last step instead of a blank
    somebody would have filled in.
    """
    from .models_db import QboRefRow
    bare = _bare_team(team)
    if not bare:
        return ""
    # Keyed with the sport, for the same reason the account cache is: one
    # university can have a football vendor and a basketball vendor, and a
    # shared key would serve whichever was looked up first to both.
    vkey = f"{bare}|{sport}"
    if vkey in _TEAM_VENDORS:
        return _TEAM_VENDORS[vkey]
    low = bare.lower()
    hit = ""
    for r in db.query(QboRefRow.name).filter(QboRefRow.kind == "vendor").all():
        name = r[0] or ""
        if name.lower() == low:
            hit = name
            break
        # A vendor naming the sport wins outright where one exists.
        #
        # Matched on the sport WORD, not the league code: accounts are named
        # "NCAAB:Ohio State Buckeyes (TC)" but vendors are named "Ohio State
        # Buckeyes Basketball". Looking for "ncaab" in a vendor name found
        # nothing, so the sport never narrowed the vendor at all.
        if sport and low in name.lower() and _sport_hint(name) == sport:
            hit = name
            break
        if not hit and low in name.lower():
            hit = name
    if not hit:
        # Substring alone is too literal. QuickBooks spells a club however the
        # bookkeeper typed it -- "Orlando City Soccer Club", "Orlando City
        # S.C." -- and none of those contain "orlando city sc", because after
        # "orlando city s" comes an "o" or a full stop. The account lookup
        # happened to succeed for the same team because "MLS:Orlando City SC
        # (TC)" does contain it, which is why the category filled in and the
        # vendor did not.
        #
        # So compare on words, with the club suffix stripped from BOTH sides,
        # and require the remainder to match EXACTLY. Equality rather than
        # containment is the safety: "San Diego FC" reduces to {san, diego},
        # which is a subset of "San Diego Padres" but not equal to it, so a
        # club can never borrow another team's vendor.
        want = _vendor_words(bare)
        # At least two words must survive. "FC Dallas" reduces to {dallas} --
        # one place word -- and matching any vendor that also reduces to
        # {dallas} would hand the club whatever "Dallas Club" or "The Dallas
        # Co" happened to be. A club named only by its city is reachable by the
        # exact substring test above, or by a written rule.
        if len(want) >= 2:
            for r in db.query(QboRefRow.name).filter(
                    QboRefRow.kind == "vendor").all():
                if _vendor_words(r[0] or "") == want:
                    hit = r[0]
                    break
    _TEAM_VENDORS[vkey] = hit
    return hit


def _team_account(db: Session, team: str, short: str, sport: str = "") -> str:
    """The chart-of-accounts name for a team's ticket-credit account.

    Matched on the team name and the "(TC)" / "(DEP)" suffix rather than built
    from a template: the prefix carries the league and is not something this
    code should be reconstructing, and an account renamed in QuickBooks should
    still be found.
    """
    from .models_db import QboRefRow
    # The SPORT is part of the key. Without it the first lookup for a team
    # cached its answer for every later one, so a basketball charge resolved to
    # whatever the football charge before it had already put in the cache.
    key = f"{team}|{short}|{sport}"
    if key in _TEAM_ACCOUNTS:
        return _TEAM_ACCOUNTS[key]
    tail = f"({short.lower()})"
    # Without the venue suffix: "New York Islanders (UBS Arena)" appears in no
    # account name, so the search found nothing for exactly the teams whose
    # Airtable label carries a venue.
    t = _bare_team(team).lower()
    # THE SPORT NARROWS A COLLEGE TEAM, when the season record named one.
    #
    # "Ohio State Buckeyes" has a football account and a basketball account and
    # the team name matches both, so whichever sorted first used to win. With a
    # sport named, the account carrying that prefix is taken; with none, the
    # first match still wins -- guessing would file a basketball charge against
    # the football account, which is worse than not narrowing at all.
    hint = (sport or "").strip().lower()
    hit = ""
    for r in db.query(QboRefRow.name).filter(QboRefRow.kind == "account").all():
        name = (r[0] or "")
        low = name.lower()
        if not (low.endswith(tail) and t in low):
            continue
        if hint and hint in low:
            hit = name
            break
        if not hit:
            # Kept as the fallback, but NOT returned yet when a sport is named:
            # a later row may carry the matching prefix, and breaking here
            # would take the wrong sport's account for being earlier in the list.
            hit = name
            if not hint:
                break
    _TEAM_ACCOUNTS[key] = hit
    return hit


def _hal_banner_state(db: Session, company: str, idx) -> bool | None:
    """For the "STH is not synced yet" banner: False only for ONE company that
    has Season Ticket Records set up on Broker Data and none read yet. None
    (no banner) for Multi-Company, a company without a source, or the
    Dashboard -- where "not synced" isn't true and isn't fixable by syncing."""
    if idx is not None:
        return True
    if not company or company == "__all__":
        return None
    try:
        from . import company_vars as _cvh
        with _cvh.for_company(db, company):
            return False if _cvh.airtable_sources("sth") else None
    except Exception:                                   # noqa: BLE001
        return None


def load_hal_index(db: Session, company: str = "") -> HalIndex | None:
    """One company's STH index. Empty company means the unsplit, shared rows.

    Cached -- see app/hal_cache.py. Rebuilding ~18k records per request was a
    second or two of latency on every click. The cache is keyed by company for
    the same reason the table is: two brokers' records must not answer each
    other's lookups.
    """
    from .hal_cache import get_index
    return get_index(db, company)


from datetime import date as _dt_date

_NULL_DATE = _dt_date.min


def _order_by(sort: str, direction: str, nick: dict, db, company: str):
    """SQL ORDER BY clauses matching what the Python sort used to produce.

    Two things need care:

    * **Nulls.** The old code keyed a NULL date as date.min and a NULL amount as
      0, so nulls sorted first ascending. COALESCE reproduces that on both
      SQLite and Postgres, whose native NULL ordering differs from each other's.

    * **Card.** That column shows the account NICKNAME, which lives in
      source_accounts, not on the charge. Ordering by ChargeRow.source instead
      would be wrong whenever a nickname doesn't sort like its source key, so we
      rank the sources by nickname and order by that rank explicitly.

    A charge_id tiebreaker is appended to every ordering. Without a fully
    deterministic sort, OFFSET paging can show the same row twice and skip
    another -- invisible until someone resolves a charge they saw twice.
    """
    desc_ = direction != "asc"

    def d(col):
        return col.desc() if desc_ else col.asc()

    if sort == "account":
        sources = [r for (r,) in db.query(ChargeRow.source)
                   .filter(ChargeRow.company == company).distinct().all()]
        for k in nick:
            if k not in sources:
                sources.append(k)
        ranked = sorted(sources, key=lambda x: (nick.get(x, x) or "").lower())
        if ranked:
            rank = case({src: i for i, src in enumerate(ranked)},
                        value=ChargeRow.source, else_=len(ranked))
            return [d(rank), ChargeRow.charge_id.asc()]
        return [d(ChargeRow.source), ChargeRow.charge_id.asc()]

    if sort == "category":
        shown = case((ChargeRow.resolution == "matched", literal("Bill Payment")),
                     else_=ChargeRow.coded_category)
        return [d(func.lower(func.coalesce(shown, ""))), ChargeRow.charge_id.asc()]

    if sort == "cvendor":
        # The column falls back to the bill's vendor for a payment, so the sort
        # has to use the same value the eye sees.
        shown = case((ChargeRow.resolution == "matched", ChargeRow.matched_bill_vendor),
                     else_=ChargeRow.coded_vendor)
        return [d(func.lower(func.coalesce(shown, ""))), ChargeRow.charge_id.asc()]

    if sort == "status":
        # Strong first by default -- the STORED score, so the ordering covers
        # every matching charge rather than the page in front of you.
        return [d(ChargeRow.score), ChargeRow.charge_id.asc()]

    if sort == "user":
        return [d(func.lower(func.coalesce(ChargeRow.resolved_by, ""))),
                ChargeRow.charge_id.asc()]

    if sort == "ts":
        return [d(ChargeRow.resolved_at), ChargeRow.charge_id.asc()]

    if sort == "kind":
        # Bill payment, then expense, then refund -- the order they appear in
        # the filter, so the column and the control agree.
        rank = case(
            (ChargeRow.resolution == "matched", 0),
            (ChargeRow.is_credit.is_(True), 2),
            else_=1)
        return [d(rank), ChargeRow.charge_id.asc()]

    if sort == "details":
        # Sort on whatever the column leads with: the bill number for a
        # payment, the category for a coding.
        lead = case((ChargeRow.resolution == "matched", ChargeRow.matched_bill_no),
                    else_=ChargeRow.coded_category)
        return [d(func.lower(func.coalesce(lead, ""))), ChargeRow.charge_id.asc()]

    zero = literal(0)
    cols = {
        "amount": [func.coalesce(ChargeRow.amount, zero)],
        "vendor": [func.lower(func.coalesce(ChargeRow.merchant, ""))],
        "profile": [func.lower(func.coalesce(ChargeRow.cardholder_name, ""))],
        "card": [func.coalesce(ChargeRow.card_last4, "")],
        "memo": [func.lower(func.coalesce(ChargeRow.memo, ""))],
        "note": [func.lower(func.coalesce(ChargeRow.custom_note, ""))],
        # Sorted as text, not as a number: QuickBooks ids are opaque strings
        # and an unposted charge has none, which coalesces to the empty string
        # and gathers at one end rather than being scattered as zeroes.
        "qbo": [func.coalesce(ChargeRow.qbo_txn_id, "")],
        "ord": [func.coalesce(ChargeRow.order_number, "")],
        "txnid": [func.coalesce(ChargeRow.source_txn_id, "")],
        "ingest": [func.coalesce(ChargeRow.ingest_method, "csv")],
        "upts": [ChargeRow.uploaded_at],
        "scur": [func.coalesce(ChargeRow.source_currency, "")],
        "samt": [ChargeRow.source_amount],
        "date": [func.coalesce(ChargeRow.txn_date, _NULL_DATE),
                 func.coalesce(ChargeRow.amount, zero)],
    }.get(sort, [func.coalesce(ChargeRow.txn_date, _NULL_DATE),
                 func.coalesce(ChargeRow.amount, zero)])
    return [d(c) for c in cols] + [ChargeRow.charge_id.asc()]


def _candidate_postable(charge_amount, bill_row) -> bool:
    """Would matching this charge to this bill survive the write path?

    Asks the same question qbo_write asks at post time, so the queue never
    offers a Match it would then refuse.
    """
    if bill_row is None:
        return False
    from .integrations.qbo_write import match_is_postable
    try:
        return bool(match_is_postable(charge_amount, bill_row)["ok"])
    except Exception:
        # A verdict we can't compute shouldn't hide the candidate outright --
        # the reviewer still sees it, and the write path remains the backstop.
        return True


CHARGE_FILTER_KEYS = ("status", "card", "source", "date_from", "date_to",
                      "amt_min", "amt_max", "samt_min", "samt_max", "drift",
                      "rules_applied",
                      "vendor", "profile", "last4",
                      "memo", "details", "cvendor", "category", "user",
                      "ts_from", "ts_to", "currency", "txn_type", "kinds",
                      "tier", "matchable", "q", "record", "ingest")


def apply_charge_filters(qy, f: dict):
    """Every charge filter, in one place.

    The grid, the tab counts and the per-card counts all have to narrow the
    same way. When this logic was duplicated, the header said "Divvy (172)"
    above a grid showing one row, because only the grid knew about the date
    filter.
    """
    from sqlalchemy import (or_ as _or, and_ as _and, false as _false,
                            cast as _cast, String as _String)

    status = f.get("status")
    if status:
        qy = qy.where(ChargeRow.status == status)
    # `card` is one or more card ACCOUNTS (programs like slash/divvy/wex),
    # comma-separated -- not individual virtual cards, of which there are
    # hundreds and nobody filters by one.
    if f.get("card"):
        wanted = [c.strip() for c in str(f["card"]).split(",") if c.strip()]
        if wanted:
            qy = qy.where(ChargeRow.source.in_(wanted))
    if f.get("source"):
        qy = qy.where(ChargeRow.source == f["source"])
    if f.get("date_from"):
        qy = qy.where(ChargeRow.txn_date >= f["date_from"])
    if f.get("date_to"):
        qy = qy.where(ChargeRow.txn_date <= f["date_to"])
    if f.get("amt_min") is not None:
        qy = qy.where(ChargeRow.amount >= f["amt_min"])
    # THE SOURCE amount -- what the merchant charged, which on a foreign
    # purchase is a different number from what the card settled. A range over
    # one cannot answer a question about the other.
    #
    # Compared on the ABSOLUTE value, as the grid displays it: source_amount is
    # stored unsigned for a purchase and a refund alike, and a reviewer typing
    # 500 means five hundred either way.
    # Only the charges QuickBooks disagrees with. Without this the badge is
    # findable only by scrolling, which for 29,000 rows is not findable.
    if f.get("drift"):
        qy = qy.where(func.coalesce(ChargeRow.qbo_drift, "") != "")
    if f.get("samt_min") is not None:
        qy = qy.where(func.abs(ChargeRow.source_amount) >= f["samt_min"])
    if f.get("samt_max") is not None:
        qy = qy.where(func.abs(ChargeRow.source_amount) <= f["samt_max"])
    if f.get("amt_max") is not None:
        qy = qy.where(ChargeRow.amount <= f["amt_max"])
    # Separate fields rather than one search box: reviewers look for a specific
    # vendor OR a specific person, and a combined search matches neither well.
    if f.get("vendor"):
        qy = qy.where(ChargeRow.merchant.ilike(f"%{f['vendor']}%"))
    if f.get("profile"):
        qy = qy.where(ChargeRow.cardholder_name.ilike(f"%{f['profile']}%"))
    if f.get("last4"):
        # An Amex ending is five digits; its last four are what is stored.
        _l4 = str(f["last4"]).strip()
        if len(_l4) == 5 and _l4.isdigit():
            _l4 = _l4[1:]
        qy = qy.where(ChargeRow.card_last4.ilike(f"%{_l4}%"))
    if f.get("memo"):
        qy = qy.where(ChargeRow.memo.ilike(f"%{f['memo']}%"))
    if f.get("order_no"):
        qy = qy.where(ChargeRow.order_number.ilike(f"%{str(f['order_no']).strip()}%"))
    if f.get("qbo_id"):
        # Partial, like every other text filter here: the id is usually pasted
        # out of QuickBooks and may arrive with stray whitespace around it.
        qy = qy.where(ChargeRow.qbo_txn_id.ilike(f"%{str(f['qbo_id']).strip()}%"))
    # The Vendor column shows whichever applies: the coded vendor once resolved,
    # the bill's vendor for a payment, the engine's suggestion while still in
    # For Review. The filter looks at all three or it misses what's on screen.
    # SEVERAL VENDORS, comma separated.
    #
    # One at a time meant "show me the three Broadway venues" was three
    # separate looks at the grid, and any comparison between them had to be
    # held in the head. The separator is a comma because a vendor name can
    # contain almost anything else.
    if f.get("cvendor"):
        _terms = [t.strip() for t in str(f["cvendor"]).split(",") if t.strip()]
        _any = []
        for _t in _terms:
            _cv = f"%{_t}%"
            _any += [ChargeRow.coded_vendor.ilike(_cv),
                     ChargeRow.matched_bill_vendor.ilike(_cv),
                     ChargeRow.suggested_vendor.ilike(_cv)]
        if _any:
            qy = qy.where(_or(*_any))
    # Same for Category. "Bill Payment" is not stored anywhere -- it is what the
    # column prints for a matched charge -- so it is matched explicitly.
    # Several categories too, same reasoning as the vendor above.
    if f.get("category"):
        _cl = []
        for _t in [t.strip() for t in str(f["category"]).split(",") if t.strip()]:
            _ct = f"%{_t}%"
            _cl += [ChargeRow.coded_category.ilike(_ct),
                    ChargeRow.suggested_category.ilike(_ct)]
            # "Bill Payment" is not an account: it is what a matched charge
            # shows in that column, so the name has to reach the resolution.
            if "bill payment".startswith(_t.lower()[:12]):
                _cl.append(ChargeRow.resolution == "matched")
        if _cl:
            qy = qy.where(_or(*_cl))

    # WAS THIS CODED BY A RULE, or by a person?
    #
    # The badge says so per row; this asks the question of the whole grid --
    # "what did the engine do on its own" and "what still needed hands" are
    # both worth being able to see at once.
    #
    # A rule is anything the engine decided: a written bank-feed rule, a team
    # alias, or a season account. `rule_source` is set when a charge is coded
    # automatically, and `category_from_rule` covers the suggestions that have
    # not been committed yet.
    # WHAT "a rule applied" MEANS DEPENDS ON THE TAB.
    #
    # On Categorized a rule CODED the charge, which shows in rule_source or a
    # "system-" resolver. On For Review nothing is coded yet -- a rule there
    # has produced a SUGGESTION, which is what the RULE badge beside the vendor
    # is reporting. Asking only the first question returned nothing on For
    # Review, because no charge in that queue has been coded by anything.
    _ra = str(f.get("rules_applied") or "").strip().lower()
    if _ra in ("yes", "no"):
        # rule_applied is the badge's own answer, stored. Rows never rendered
        # or rescored have NULL, and are treated as "not known to have one" --
        # the background pass fills them in.
        # NULL is "not worked out yet", not "no".
        #
        # Treating it as no put every un-flagged row into "No" -- including
        # rows plainly showing a badge, which is what made the filter look
        # broken. Unknown rows appear in neither answer until the background
        # pass has been over them, which is a smaller and more honest error
        # than asserting something false.
        if _ra == "yes":
            qy = qy.where(ChargeRow.rule_applied.is_(True))
        else:
            qy = qy.where(ChargeRow.rule_applied.is_(False))

    # How the charge arrived. A comma-separated set rather than one value, so
    # "everything that did NOT come from a file" is one filter rather than two
    # passes -- which is the question asked after a switchover.
    _ing = [v.strip().lower() for v in (f.get("ingest") or "").split(",")
            if v.strip()]
    if _ing:
        qy = qy.where(func.coalesce(ChargeRow.ingest_method, "csv").in_(_ing))

    _cur = (f.get("currency") or "").upper()
    if _cur == "CAD":
        qy = qy.where(_or(ChargeRow.source_currency == "CAD",
                          ChargeRow.is_canadian.is_(True)))
    elif _cur == "USD":
        qy = qy.where(_or(ChargeRow.source_currency == "USD",
                          ChargeRow.source_currency.is_(None),
                          ChargeRow.source_currency == ""))
    elif _cur == "OTHER":
        qy = qy.where(_and(ChargeRow.source_currency.isnot(None),
                           ChargeRow.source_currency != "",
                           ChargeRow.source_currency != "USD",
                           ChargeRow.source_currency != "CAD"))
    if f.get("user"):
        # A comma-separated set, so several people can be selected at once, and
        # "system" matches the whole family -- system-rule, system-payment,
        # system-teams. Those are the engine's own internal names for which
        # part of it acted; the filter's job is only to separate automatic from
        # human.
        _users = [u.strip() for u in str(f["user"]).split(",") if u.strip()]
        if _users:
            _clauses = []
            for _u in _users:
                if _u.lower() == "system":
                    _clauses.append(ChargeRow.resolved_by.ilike("system%"))
                else:
                    _clauses.append(ChargeRow.resolved_by == _u)
            qy = qy.where(_or(*_clauses))
    if f.get("ts_from"):
        qy = qy.where(ChargeRow.resolved_at >= f["ts_from"])
    if f.get("ts_to"):
        qy = qy.where(ChargeRow.resolved_at <= f["ts_to"])
    if f.get("details"):
        _d = f"%{f['details']}%"
        qy = qy.where(_or(ChargeRow.matched_bill_no.ilike(_d),
                          ChargeRow.matched_bill_vendor.ilike(_d),
                          ChargeRow.matched_bill_date.ilike(_d),
                          ChargeRow.matched_bill_memo.ilike(_d),
                          ChargeRow.coded_vendor.ilike(_d),
                          ChargeRow.coded_category.ilike(_d)))
    # Amounts are stored absolute with is_credit carrying the sign, so an amount
    # range can't express "refunds only" -- this is the only way to ask.
    # Multi-select: a comma list. Selecting everything means the same as
    # selecting nothing, and the UI sends neither.
    if f.get("txn_type"):
        if str(f["txn_type"]) == "\u0000none":
            return qy.where(_false())
        _want = {t.strip() for t in str(f["txn_type"]).split(",") if t.strip()}
        _tc = []
        if "charges" in _want:
            _tc.append(ChargeRow.is_credit.is_(False))
        if "refunds" in _want:
            _tc.append(_and(ChargeRow.is_credit.is_(True),
                            ChargeRow.is_card_payment.is_(False)))
        if "payments" in _want:
            _tc.append(_and(ChargeRow.is_credit.is_(True),
                            ChargeRow.is_card_payment.is_(True)))
            # THE BANK SIDE TOO: a bank debit that pays a card shows PAYMENT
            # because it pairs with the card's payment -- worked out by the
            # pairing, not stored -- so the caller passes those rows' ids.
            if f.get("_payment_ids"):
                _tc.append(ChargeRow.charge_id.in_(list(f["_payment_ids"])))
        qy = qy.where(_or(*_tc) if _tc else _false())
    # On Categorized the question is what the charge was resolved AS, which is
    # a different axis: a bill payment, an expense, or money coming back.
    if f.get("kinds"):
        if str(f["kinds"]) == "\u0000none":
            return qy.where(_false())
        want = {k.strip() for k in f["kinds"].split(",") if k.strip()}
        clauses = []
        if "bill_payment" in want:
            clauses.append(ChargeRow.resolution == "matched")
        if "expense" in want:
            clauses.append(_and(ChargeRow.resolution == "coded",
                                ChargeRow.is_credit.is_(False)))
        # Money coming back splits two ways, as it does on For Review: a refund
        # reverses a purchase, a payment settles the card.
        if "refund" in want:
            clauses.append(_and(ChargeRow.resolution == "coded",
                                ChargeRow.is_credit.is_(True),
                                ChargeRow.is_card_payment.is_(False)))
        if "payment" in want:
            clauses.append(_and(ChargeRow.resolution == "coded",
                                ChargeRow.is_credit.is_(True),
                                ChargeRow.is_card_payment.is_(True)))
            # THE BANK SIDE OF A MATCHED TRANSFER is a payment too. Resolved
            # as "transfer", it fell in no kind at all, so with any kind
            # ticked the checking row vanished from Categorized after Match
            # Transfer -- while its card side showed.
            clauses.append(ChargeRow.resolution == "transfer")
        qy = qy.where(_or(*clauses) if clauses else _false())
    # "Match" means a bill candidate exists; "Add" means there isn't one, so
    # coding is the only route. Derived from the stored tier: "none" is exactly
    # the no-candidate case.
    if f.get("matchable") == "match":
        qy = qy.where(ChargeRow.tier != "none")
    elif f.get("matchable") == "add":
        qy = qy.where(ChargeRow.tier == "none")
    # RECORD FOUND / NOT FOUND (the Strengths / Records picker). Sent by the
    # screen all along and never applied, so "Record Found" still listed
    # charges showing No Records Found. Read from hal_matched, which the queue
    # writes as it looks each charge's STH record up; a charge not looked up
    # yet (NULL) is in neither.
    _rec = (f.get("record") or "").strip().lower()
    if _rec == "found":
        qy = qy.where(ChargeRow.hal_matched.is_(True))
    elif _rec == "none":
        qy = qy.where(ChargeRow.hal_matched.is_(False))
    elif _rec == "unknown":
        # Not looked up yet -- for the background pass that fills them in.
        qy = qy.where(ChargeRow.hal_matched.is_(None))
    # NOTHING TICKED in Strengths / Records: the charges with neither a bill
    # pill nor an STH pill -- no candidate bill, and no STH record looked for.
    if str(f.get("tier") or "") == "\u0000none" and not _rec:
        return qy.where(ChargeRow.tier == "none", ChargeRow.hal_matched.is_(None))
    if str(f.get("tier") or "") == "\u0000none":
        return qy.where(_false())
    if str(f.get("matchable") or "") == "\u0000none":
        return qy.where(_false())
    if f.get("tier"):
        wanted = [t.strip() for t in str(f["tier"]).split(",") if t.strip()]
        if wanted:
            qy = qy.where(ChargeRow.tier.in_(wanted))
    if f.get("q"):
        # Every column the grid can show. The box says "quick search", so
        # searching a value that is visible on screen and getting nothing back
        # reads as broken -- Order # in particular, which used to have its own
        # filter and now relies on this.
        like = f"%{f['q']}%"
        # The search term as a number, if it is one. Currency symbols, commas
        # and spaces are stripped, because "$1,712.00" is what somebody copies
        # off the screen and it is the same number.
        _amount_term = None
        _bare = (f["q"] or "").strip().lstrip("$").replace(",", "").replace(" ", "")
        try:
            from decimal import Decimal as _Dec2
            if _bare:
                _amount_term = abs(_Dec2(_bare))
        except Exception:                   # noqa: BLE001
            _amount_term = None
        # The CARD ACCOUNT's name too, as the grid shows it -- searching "Amex"
        # found nothing, though the column on screen said Amex.
        from sqlalchemy import and_ as _and_q, exists as _exists_q
        from .models_db import SourceAccountRow as _SAq
        _card_named = _exists_q().where(_and_q(
            _SAq.company == ChargeRow.company, _SAq.source == ChargeRow.source,
            _or(_SAq.nickname.ilike(like), _SAq.qbo_account_name.ilike(like))))
        qy = qy.where(_or(
            _card_named,
            ChargeRow.merchant.ilike(like),
            ChargeRow.raw_description.ilike(like),
            ChargeRow.cardholder_name.ilike(like),
            ChargeRow.card_last4.ilike(like),
            ChargeRow.memo.ilike(like),
            ChargeRow.custom_note.ilike(like),
            ChargeRow.order_number.ilike(like),
            ChargeRow.source_txn_id.ilike(like),
            ChargeRow.source_currency.ilike(like),
            ChargeRow.coded_vendor.ilike(like),
            ChargeRow.coded_category.ilike(like),
            ChargeRow.suggested_vendor.ilike(like),
            ChargeRow.suggested_category.ilike(like),
            ChargeRow.matched_bill_id.ilike(like),
            ChargeRow.matched_bill_no.ilike(like),
            ChargeRow.matched_bill_vendor.ilike(like),
            ChargeRow.matched_bill_memo.ilike(like),
            ChargeRow.qbo_txn_id.ilike(like),
            ChargeRow.resolved_by.ilike(like),
            ChargeRow.source.ilike(like),
            # AMOUNT MATCHES EXACTLY, everything else by substring.
            #
            # The amount was cast to text and matched with %400%, so searching
            # 400 returned 2400, 4004 and 1400.75 as well -- and on a grid of
            # ticket charges those are plentiful enough to bury the one row
            # being looked for. A number is a value, not a fragment of one.
            #
            # Only when the term IS a number: "400" searches amounts exactly
            # AND still matches an order number containing 400, because the
            # other columns are unchanged.
            *( [ChargeRow.amount == _amount_term] if _amount_term is not None
               else [_cast(ChargeRow.amount, _String).ilike(like)] ),
        ))
    return qy


_ACCOUNT_NAMES: set[str] | None = None


_VENDOR_NAMES: set[str] | None = None


def _known_vendor(db: Session, name: str) -> str:
    """The name back if QuickBooks has that vendor, otherwise blank.

    Same reasoning as categories: the engine composes vendor names from the
    event and team, and not every one it can build exists. A suggestion the
    ledger will reject looks decided, so it gets committed and fails at post
    time -- blank at least asks the question.
    """
    global _VENDOR_NAMES
    if not name:
        return ""
    # Read the cache into a local: reset_account_cache() can null the global from
    # another request's thread mid-call, and `name in None` would raise.
    known = _VENDOR_NAMES
    if known is None:
        from .models_db import QboRefRow
        known = {r.name for r in db.query(QboRefRow.name).filter(
            QboRefRow.kind == "vendor").all() if r.name}
        _VENDOR_NAMES = known
    if not known:             # nothing synced yet -- don't blank everything
        return name
    return name if name in known else ""


def _season_account(category: str, team: str, from_rule: bool = False) -> str:
    """Turn the classifier's "TC" or "DEP" into the account it means.

    The engine decides WHICH KIND of season account a charge belongs to; the
    chart of accounts holds one per team, named "Chicago Cubs (TC)". Passing
    the bare code to the account lookup found nothing, so the category simply
    never prefilled -- the vendor did, which made it look like half a rule.

    A value that came from a WRITTEN RULE always resolves to the TC account.
    The DEP variant is derived from the STH record's Full/Partial value, so two
    charges to the same team under the same rule landed on different accounts --
    and where the (DEP) account did not exist in QuickBooks the category was
    blanked, which is why a rule looked like it applied to some rows and not
    others. A rule is an instruction, not a guess, and it should not change its
    answer because of a field on a different record.
    """
    code = (category or "").strip().upper()
    if code in ("TC", "DEP") and (team or "").strip():
        if from_rule:
            code = "TC"
        return f"{team.strip()} ({code})"
    return category


def _known_account(db: Session, name: str) -> str:
    """The name back if QuickBooks has that account, otherwise blank.

    Cached per request-batch: the chart of accounts is a few hundred rows and
    this runs once per charge on the page.
    """
    global _ACCOUNT_NAMES
    if not name:
        return ""
    # Local copy: a concurrent reset_account_cache() nulling the global between
    # the check and `name in ...` would otherwise raise.
    known = _ACCOUNT_NAMES
    if known is None:
        from .models_db import QboRefRow
        known = {r.name for r in db.query(QboRefRow.name).filter(
            QboRefRow.kind == "account").all() if r.name}
        _ACCOUNT_NAMES = known
    # No accounts synced yet: don't suppress every suggestion on a fresh setup.
    if not known:
        return name
    return name if name in known else ""


def reset_account_cache() -> None:
    """Called after a QuickBooks refresh, so new records are picked up.

    The team->vendor and team->account maps are cleared too. They were not, and
    they cache MISSES as well as hits: the first render after a deploy, or any
    render before QuickBooks had been synced, would store "" for a team and
    every later render in that process returned the empty string without ever
    looking again. A vendor added in QuickBooks would not appear until the
    server restarted, while the account -- looked up through a different map
    that happened to be populated later -- filled in normally. That is exactly
    the shape of "the category came through but the vendor is blank".
    """
    global _ACCOUNT_NAMES, _VENDOR_NAMES
    _ACCOUNT_NAMES = None
    _VENDOR_NAMES = None
    _TEAM_VENDORS.clear()
    _TEAM_ACCOUNTS.clear()


def _fmt_central(dt):
    from .export_xlsx import fmt_central
    return fmt_central(dt)


# A credit that's a payment onto the card, not a refund from a merchant.
# Statement wording is remarkably consistent about this.
# Money moved onto the card, or between our own accounts -- not a merchant
# returning a purchase. Wires and ACH belong here: they're funding movements,
# and calling them refunds puts them in the wrong bucket for coding.
_CARD_PAYMENT_RE = re.compile(
    r"\b(payment\s+(thank\s+you|received|-\s*thank)|autopay|auto\s*pay|"
    r"online\s+payment|electronic\s+payment|pmt\s+thank|"
    r"cardmember\s+payment|direct\s+debit|"
    r"wire(\s+transfer)?|ach(\s+(transfer|credit|debit|payment))?|"
    r"bank\s+transfer|funds\s+transfer|transfer\s+from|eft)\b", re.I)


def looks_like_card_payment(*parts: str) -> bool:
    return bool(_CARD_PAYMENT_RE.search(" ".join(p or "" for p in parts)))


_PAY_RULES = None
_SOURCE_NAMES = None


def reset_payment_rules() -> None:
    global _PAY_RULES, _SOURCE_NAMES
    _PAY_RULES = None
    _SOURCE_NAMES = None


def is_payment(row, db=None) -> bool:
    """Is this credit a payment onto the card rather than a merchant refund?

    The rules table decides, and nothing else. The wire/ACH wording used to be
    checked first, in code -- so the Payment Tags grid could be empty while
    badges still appeared, with no way to see why or to change it. Those
    phrases are seeded as rows instead.

    The built-in matcher survives only as a fallback for when the table can't
    be read at all, so a database problem doesn't silently reclassify every
    credit as a refund.
    """
    global _PAY_RULES
    if not row.is_credit:
        return False        # money going out is never a payment onto the card
    # MONEY ARRIVING IN THE PRIMARY CHECKING ACCOUNT is never a payment onto a
    # card: that account is where payments are made FROM. Flagged, a deposit
    # there would be posted as a card payment from the account into itself.
    if db is not None and getattr(row, "source", "") and getattr(row, "company", ""):
        from .models_db import SourceAccountRow as _SAp
        _prim = db.query(_SAp.source).filter(_SAp.company == row.company,
                                             _SAp.is_primary.is_(True)).first()
        if _prim and _prim[0] == row.source:
            return False
    if db is None:
        return looks_like_card_payment(row.merchant, row.raw_description, row.memo)
    # Local copies: reset_payment_rules() runs at the start of every queue build,
    # so a concurrent request can null these globals mid-call -- `for r in None`
    # and `None.get` would both raise.
    rules = _PAY_RULES
    if rules is None:
        from .models_db import PaymentRuleRow
        rules = db.query(PaymentRuleRow).filter(
            PaymentRuleRow.active.is_(True)).all()
        _PAY_RULES = rules
    if not rules:
        # No rules at all means nothing is a payment. That is a real answer, and
        # an empty grid should look like an empty grid.
        return False
    from .canada import matches_rules
    # Card Account is matched against BOTH the internal key ("wex") and the
    # name on the Cards screen ("Wex (Credit)"). Someone writing a rule reads
    # the second and has no reason to know the first exists.
    global _SOURCE_NAMES
    src_names = _SOURCE_NAMES
    if src_names is None:
        from .models_db import SourceAccountRow
        src_names = {r.source: (r.nickname or r.qbo_account_name or r.source)
                     for r in db.query(SourceAccountRow).all()}
        _SOURCE_NAMES = src_names
    account = f"{row.source or ''} {src_names.get(row.source, '')}".strip()
    return matches_rules(
        row.coded_vendor or row.matched_bill_vendor or row.suggested_vendor or "",
        row.merchant or row.raw_description or "",
        rules, card_account=account)


def tier_for(top_score: float, has_candidates: bool) -> str:
    """The one place the score-to-tier thresholds live -- now tunable."""
    from engine.suggest import current_limits
    if not has_candidates:
        return "none"
    # THE BANDS AS EDITED (Algorithms > Strengths): the highest whose start the
    # score reaches. Held to 3 places, so a "100%" match is not kept out of the
    # top band by floating-point dust (0.99999...).
    from .bands import tier_of
    return tier_of(top_score)


def load_match_weights(db: Session) -> None:
    """Push the tuned scorecard into the scorer before it runs.

    Weights, limits and match rules together, because all three are read on
    every scored pair and loading them separately invites the state where one
    is current and another is stale.
    """
    from .models_db import MatchWeightRow, MatchRuleRow
    from engine.suggest import (set_weights, set_limits, set_match_rules,
                                set_stopwords, set_team_aliases, DEFAULT_WEIGHTS)
    try:
        saved = {r.key: r.weight for r in db.query(MatchWeightRow).all()}
        # One table, two kinds of setting -- split by which name it is.
        set_weights({k: v for k, v in saved.items() if k in DEFAULT_WEIGHTS})
        set_limits({k: v for k, v in saved.items() if k not in DEFAULT_WEIGHTS})
    except Exception:                       # noqa: BLE001
        pass                                # defaults stand
    try:
        rows = db.query(MatchRuleRow).filter(MatchRuleRow.active.is_(True)).all()
        set_match_rules([{"bank_detail": r.bank_detail,
                          "item": getattr(r, "item", "bank_detail"),
                          "op": getattr(r, "op", "contains"),
                          "bill_vendor": r.bill_vendor,
                          "ignore_email": r.ignore_email,
                          "ignore_text": r.ignore_text} for r in rows])
    except Exception:                       # noqa: BLE001
        # A missing table (an older database, mid-migration) must not stop the
        # queue rendering -- no rules is the same as no exceptions.
        set_match_rules([])
    try:
        from .models_db import AppSettingRow
        row = db.get(AppSettingRow, "match_stopwords")
        saved = (row.value or "").strip() if row else ""
        # Empty means "use the built-in list", which is what None restores.
        set_stopwords([w.strip() for w in saved.split(",") if w.strip()]
                      if saved else None)
    except Exception:                       # noqa: BLE001
        set_stopwords(None)
    try:
        from .models_db import AppSettingRow as _AS2
        from engine.suggest import set_never_direct
        _nd = db.get(_AS2, "never_direct")
        _v = (_nd.value or "").strip() if _nd else ""
        set_never_direct([p.strip() for p in _v.split(",") if p.strip()]
                         if _v else None)
    except Exception:                       # noqa: BLE001
        from engine.suggest import set_never_direct
        set_never_direct(None)
    try:
        from .models_db import AppSettingRow as _AS3
        from engine.suggest import (set_virtual_programs,
                                    DEFAULT_VIRTUAL_PROGRAMS)
        _vp = db.get(_AS3, "virtual_programs")
        # Absent means "never configured" -> the built-in list. A row that IS
        # present and empty means somebody cleared it deliberately, and is
        # honored as empty.
        if _vp is None:
            set_virtual_programs(list(DEFAULT_VIRTUAL_PROGRAMS))
        else:
            set_virtual_programs([p.strip() for p in (_vp.value or "").split(",")
                                  if p.strip()])
    except Exception:                       # noqa: BLE001
        from engine.suggest import (set_virtual_programs,
                                    DEFAULT_VIRTUAL_PROGRAMS)
        set_virtual_programs(list(DEFAULT_VIRTUAL_PROGRAMS))
    try:
        from .models_db import AppSettingRow as _AS5
        global _PAY_AUTOCODE
        import json as _tj
        _t = db.get(_AS5, "tie_settings")
        set_tie_settings(_tj.loads(_t.value) if _t and _t.value else {})
        _a = db.get(_AS5, "payment_autocode")
        _PAY_AUTOCODE = (_a.value or "false") == "true" if _a else False
    except Exception:                       # noqa: BLE001
        _PAY_AUTOCODE = False
    try:
        from .models_db import AppSettingRow as _AS4
        from engine.hal import set_synonyms
        import json as _json
        _syn = db.get(_AS4, "word_synonyms")
        set_synonyms(_json.loads(_syn.value) if _syn and _syn.value
                     else DEFAULT_SYNONYMS)
    except Exception:                       # noqa: BLE001
        from engine.hal import set_synonyms
        set_synonyms(DEFAULT_SYNONYMS)
    try:
        from .models_db import TeamAliasRow
        _rows = db.query(TeamAliasRow).all()
        # Written aliases, PLUS the team's own name and its mascot. Only rows
        # carrying an alias were registered, so STH's resolver could not answer
        # "DALLAS STARS TICKETS E-" -- the team name is not an alias of itself.
        # With no answer it fell through to word comparison, where "dallas"
        # matched an FC Dallas record held by the same person.
        set_team_aliases(team_claim_pairs(db))
        # The same list decides whether a merchant is a TEAM at all, which
        # classify() used to answer from a hardcoded set of about forty names.
        # classify() asks "is this merchant a team at all", which decides TC
        # coding -- so it gets the team names only, not the venues. A venue in
        # this list is what let UNITED CENTER be coded as a team payment.
        from engine.classify import set_season_teams
        set_season_teams([(r.team, r.team) for r in _rows])
        # STH uses the same mapping to decide WHICH team a charge is for,
        # rather than comparing the merchant string against each record's team
        # name and accepting a single shared word.
        # The STRICT resolver: this decides which season account a charge
        # belongs to, and from there what gets written into Vendor and
        # Category. The loose one stays where it belongs, in scoring.
        from engine.hal import set_team_resolver
        from engine.suggest import team_for_prefill as _tfp
        set_team_resolver(_tfp)
    except Exception:                       # noqa: BLE001
        # Table not there yet: no aliases is the same as no venue knowledge,
        # which is where the matcher was before this existed.
        set_team_aliases([])


def refresh_flags(db: Session, company: str) -> int:
    """Recompute the descriptive flags on every charge in a company.

    Cheap -- string tests, no bill scan -- so it can run over the whole table
    rather than only what is still in the queue.
    """
    from .canada import looks_canadian, reset_rules
    reset_rules()
    n = 0
    for row in db.scalars(select(ChargeRow).where(ChargeRow.company == company)):
        pay = is_payment(row, db)
        cad = looks_canadian(row.merchant, row.raw_description, row.memo, db=db,
            vendor=row.coded_vendor or row.matched_bill_vendor
                   or row.suggested_vendor or "")
        # A charge the FILE said was in Canadian dollars stays flagged. The
        # rules read text, and on these rows the bank detail is a phone number
        # -- so recomputing from text alone would clear a flag the import had
        # already established from the amounts.
        if (row.source_currency or "").upper() == "CAD":
            cad = True
        if bool(row.is_card_payment) != pay or bool(row.is_canadian) != cad:
            row.is_card_payment, row.is_canadian = pay, cad
            n += 1
    if n:
        db.commit()
    return n


def refresh_rule_flags(db: Session, company: str, page_size: int = 500) -> int:
    """Store, for every unresolved charge, whether a rule applies to it.

    BY RUNNING THE GRID, page by page, and letting its own write-back do the
    storing.

    The first attempt asked the rules engine directly and got it wrong: the
    badge is also raised by a team alias and by a season account, neither of
    which that engine knows about. So it overwrote the render's correct answer
    with a wrong one -- a second implementation of a question that already had
    an implementation, which is the same mistake as the two holdings lookups.

    This is slower than a bespoke query and it cannot disagree with the badge,
    because it IS the badge's own code. It runs in the background, where slow
    is acceptable and wrong is not.
    """
    total = 0
    # BOTH QUEUES. The filter is offered on Categorized as well, and a pass
    # that only walked For Review left every categorized row unflagged --
    # which the filter then had to guess about.
    for _status in ("for_review", "categorized"):
        offset = 0
        for _ in range(200):                # 100,000 rows per queue
            out = build_queue(db, company, status=_status,
                              limit=page_size, offset=offset)
            got = len(out.get("items") or [])
            total += got
            if got < page_size:
                break
            offset += page_size
    return total


def pay_autocode_for(db, company: str) -> bool:
    """Whether this company's card payments file themselves into Categorized,
    coded to the primary checking account.

    PER COMPANY, on Configuration > Settings. A company that has not set it
    falls back to the old engine-wide switch (Payment Tags), so nothing
    changes for anyone until they choose.
    """
    from .models_db import AppSettingRow as _ASp
    row = db.get(_ASp, f"payment_autocode:{company}") if company else None
    if row is not None:
        return (row.value or "") == "true"
    return _PAY_AUTOCODE


AUTO_STH_KEY = "auto_sth:"                # + company -> "true"


def auto_sth_on(db, company: str) -> bool:
    """Configuration > Settings: automatically categorize STH charges."""
    from .models_db import AppSettingRow as _ASs
    row = db.get(_ASs, AUTO_STH_KEY + company) if company else None
    return bool(row and (row.value or "") == "true")


def start_auto_post(company: str, charge_ids: list, actor: str) -> None:
    """Post charges already categorized, in the background, as a person's
    categorizing would -- and put back in For Review, coding cleared, with the
    reason, any QuickBooks refuses."""
    import threading as _thrp

    def work():
        from .db import SessionLocal
        from .models_db import ChargeRow
        from .integrations.qbo_write import push_charge
        db = SessionLocal()
        try:
            for cid in charge_ids:
                row = db.get(ChargeRow, cid)
                if row is None or row.status != "categorized" or row.qbo_txn_id:
                    continue
                res = push_charge(db, row, actor)
                if res.get("status") == "failed":
                    row.status, row.resolution = "for_review", ""
                    row.coded_vendor = row.coded_category = ""
                    row.post_error = (res.get("error") or "")[:1000]
                    db.commit()
        except Exception:                               # noqa: BLE001
            log.exception("auto-post failed for %s", company)
        finally:
            db.close()

    _thrp.Thread(target=work, daemon=True, name=f"auto-post-{company}").start()


AUTO_STRONG_KEY = "auto_strong:"          # + company -> "true"


def auto_strong_level(db, company: str) -> str:
    """Configuration > Settings: which matches are categorized automatically --
    "strong" (Strong & Above), "very_strong" (Very Strong & Above), "perfect",
    or "" for none. The setting was once on/off; on means Strong & Above."""
    from .models_db import AppSettingRow as _ASa
    row = db.get(_ASa, AUTO_STRONG_KEY + company) if company else None
    v = (row.value or "").strip() if row else ""
    # "true" (the old on/off) is Strong & Above; so is MODERATE, which is no
    # longer offered -- what is posted always matches what Settings shows.
    if v in ("true", "moderate"):
        return "strong"
    from .bands import auto_levels
    return v if v in {k for k, _ in auto_levels()} else ""


def auto_strong_on(db, company: str) -> bool:
    return bool(auto_strong_level(db, company))


def choose_auto_strong(db, rows, ranked: dict, level: str = "strong") -> list:
    """The (charge_id, bill_id) pairs safe to categorize with nobody looking.

    ALL of these, or the charge is left for a person:
      - its best bill is graded STRONG;
      - that best bill is clearly ahead of its second-best (no tie);
      - NO OTHER charge scores that same bill as high or higher -- "no stronger
        match for that particular bill";
      - it is an ordinary charge: not a refund, a card payment or pending, and
        not already in QuickBooks.
    """
    from .models_db import BillRow as _BRa
    from engine.suggest import current_limits
    lim = current_limits()          # the same threshold that grades "strong"
    eligible = {}
    # BOTH A STRONG MATCH AND AN STH CANDIDATE: left for a person. When the
    # company auto-adds STH charges, a charge with an STH record could go
    # either way -- to that bill, or as an STH charge -- so neither automatic
    # route takes it (the STH auto-add steps aside for a strong match too).
    try:
        _cos = {getattr(r, "company", None) for r in rows} - {None}
        _sth_on = {c for c in _cos if auto_sth_on(db, c)}
    except Exception:                                           # noqa: BLE001
        _sth_on = set()
    for row in rows:
        if (row.status != "for_review" or row.is_credit or row.is_card_payment
                or row.is_pending or (row.qbo_txn_id or "")):
            continue
        if getattr(row, "company", None) in _sth_on and getattr(row, "hal_matched", None) is True:
            continue
        sg = ranked.get(row.charge_id) or []
        # The chosen level's starting score: Strong, Very Strong or Perfect.
        from .bands import start_of as _band_start
        floor = _band_start(level, lim["tier_strong"])
        if not sg or round(sg[0][1], 3) < floor:
            continue
        if len(sg) > 1 and sg[1][1] >= sg[0][1]:
            continue                                  # a tie between two bills
        eligible[row.charge_id] = sg[0]
    # Every charge's claim on every bill, to find a competing claim.
    best_for_bill = {}
    for cid, sg in ranked.items():
        for bid, sc in sg:
            best_for_bill.setdefault(bid, []).append((sc, cid))
    pairs = []
    for cid, (bid, sc) in eligible.items():
        rivals = [s2 for s2, c2 in best_for_bill.get(bid, []) if c2 != cid]
        if rivals and max(rivals) >= sc:
            continue                                  # another charge wants it as much
        if db.get(_BRa, bid) is None:
            continue
        pairs.append((cid, bid))
    return pairs


def start_auto_strong(company: str, pairs: list) -> None:
    """Match and post each pair in the background, as a person's match would:
    categorized, matched to the bill, posted to QuickBooks -- and returned to
    For Review with the reason if QuickBooks refuses it."""
    import threading as _thra

    def work():
        from datetime import datetime as _dta
        from .db import SessionLocal
        from .models_db import AuditRow, BillRow, ChargeRow
        from .integrations.qbo_write import push_charge
        db = SessionLocal()
        try:
            for cid, bid in pairs:
                row, b = db.get(ChargeRow, cid), db.get(BillRow, bid)
                if row is None or b is None or row.status != "for_review" or row.qbo_txn_id:
                    continue
                row.status, row.resolution = "categorized", "matched"
                row.matched_bill_id = bid
                row.coded_category = row.coded_vendor = ""
                row.matched_bill_no = b.doc_number or b.bill_id or ""
                row.matched_bill_vendor = b.vendor or ""
                row.matched_bill_date = str(b.txn_date or "")
                row.matched_bill_memo = (b.memo or "")[:500]
                row.rule_source = "auto_strong"
                row.rule_detail = "strong match, no competing match for the bill"
                row.resolved_by = "system-auto-match"
                row.resolved_at = _dta.utcnow()
                row.post_error = ""
                db.commit()
                res = push_charge(db, row, "system-auto-match")
                if res.get("status") == "failed":
                    # Back to For Review, and nothing left to suggest it was
                    # matched -- the bill's details go too.
                    row.status, row.resolution = "for_review", ""
                    row.matched_bill_id = row.matched_bill_no = ""
                    row.matched_bill_vendor = row.matched_bill_date = ""
                    row.matched_bill_memo = ""
                    row.post_error = (res.get("error") or "")[:1000]
                    db.commit()
                db.add(AuditRow(company=company, action="auto_matched",
                                charge_id=cid,
                                detail=(f"auto-matched to bill {row.matched_bill_no or bid}"
                                        if res.get("status") != "failed"
                                        else f"auto-match to bill {bid} refused by "
                                             f"QuickBooks: {row.post_error[:200]}"),
                                dry_run=False, actor="system-auto-match"))
                db.commit()
        except Exception:                               # noqa: BLE001
            log.exception("auto-match run failed for %s", company)
        finally:
            db.close()

    _thra.Thread(target=work, daemon=True, name=f"auto-strong-{company}").start()


def refresh_scores(db: Session, company: str) -> int:
    """Recompute stored match strength for every unresolved charge.

    Runs in the worker rather than on a page load: it is the full
    charges x bills scan, which is exactly the cost we keep off the request
    path. Roughly a second for a few hundred charges against a few thousand
    bills, and it only has to run when bills or charges actually change.
    """
    idx = load_hal_index(db, company)
    set_hal_index(idx)
    load_match_weights(db)

    bill_rows = list(db.scalars(select(BillRow).where(BillRow.company == company)))
    bills = [_to_bill(b) for b in bill_rows]
    by_id = {b.bill_id: b for b in bill_rows}

    # Flags first, over EVERY charge -- not just the unresolved ones.
    # is_card_payment and is_canadian describe what a charge IS, which doesn't
    # stop being true once it's coded. Computing them only in the For Review
    # loop meant an already-categorized wire transfer kept showing as a refund.
    refresh_flags(db, company)

    rows = list(db.scalars(select(ChargeRow).where(
        ChargeRow.company == company, ChargeRow.status == "for_review")))
    n = 0
    _ranked = {}          # charge_id -> [(bill_id, score)], for auto-matching
    for row in rows:
        if row.is_credit or not bills:
            tier, score = "none", 0
        else:
            sg = [x for x in suggest(_to_charge(row, db), bills)
                  if _candidate_postable(row.amount, by_id.get(x.bill_id))]
            top = sg[0].score if sg else 0
            tier, score = tier_for(top, bool(sg)), int(round(top * 100))
            _ranked[row.charge_id] = [(x.bill_id, x.score) for x in sg]
        # The proposed coding, stored alongside the strength so both are
        # filterable. Computed from the same classification the queue shows.
        try:
            k = classify(_to_charge(row, db),
                         team_hint=(row.coded_vendor or row.suggested_vendor or ""))
            sv = _known_vendor(db, k.coding.get("vendor") or "")[:200]
            # Resolved to the real account name, as the queue does. Passing the
            # bare "TC"/"DEP" to the account lookup matched nothing, so the
            # stored suggestion was always blank for season charges while the
            # grid showed one -- the two disagreed about the same row.
            sc = _known_account(db, _season_account(
                k.coding.get("category") or "",
                k.coding.get("vendor") or ""))[:200]
        except Exception:
            sv = sc = ""
        # Cleared on purpose, per field. Proposing it again would overwrite a
        # decision -- but only for the field that was actually cleared.
        if row.vendor_cleared:
            sv = ""
        if row.category_cleared:
            sc = ""
        pay = is_payment(row, db)
        from .canada import looks_canadian
        cad = looks_canadian(row.merchant, row.raw_description, row.memo, db=db,
            vendor=row.coded_vendor or row.matched_bill_vendor
                   or row.suggested_vendor or "")
        # A charge the FILE said was in Canadian dollars stays flagged. The
        # rules read text, and on these rows the bank detail is a phone number
        # -- so recomputing from text alone would clear a flag the import had
        # already established from the amounts.
        if (row.source_currency or "").upper() == "CAD":
            cad = True

        if (row.tier != tier or row.score != score
                or row.suggested_vendor != sv or row.suggested_category != sc
                or bool(row.is_card_payment) != pay
                or bool(row.is_canadian) != cad):
            row.tier, row.score = tier, score
            row.suggested_vendor, row.suggested_category = sv, sc
            row.is_card_payment = pay
            row.is_canadian = cad
            n += 1

        # Auto-file, when that is switched on. A payment is fully described by
        # the card it settled and the bank it came from, so there is nothing for
        # a person to decide -- but only where nothing has been decided already,
        # and never for a row somebody has touched.
        if (pay_autocode_for(db, company) and pay and row.status == "for_review"
                and not (row.resolved_by or "")
                and not row.coded_vendor and not row.coded_category
                and not row.vendor_cleared and not row.category_cleared
                and sv and sc):
            row.coded_vendor, row.coded_category = sv, sc
            row.status, row.resolution = "categorized", "expense"
            from datetime import datetime as _dt
            row.resolved_at = _dt.utcnow()
            row.resolved_by = "auto: card payment"
            n += 1
    db.commit()
    # AUTO-CATEGORIZE STRONG MATCHES, if this company has it switched on
    # (Configuration > Settings). Chosen here, from the ranking just made;
    # matched and posted in the background, exactly as a person's match is.
    try:
        _lvl = auto_strong_level(db, company)
        if _lvl:
            pairs = choose_auto_strong(db, rows, _ranked, _lvl)
            if pairs:
                start_auto_strong(company, pairs)
    except Exception:                                   # noqa: BLE001
        log.exception("auto-categorizing strong matches failed for %s", company)
    return n


def virtual_sources(db, company: str) -> set:
    """The card accounts this company counts as VIRTUAL, by one rule.

    Virtual either by the account's own flag, or by name: any account whose
    name contains a word on the Virtual Cards list (Tags). The Bank / Card
    Accounts grid already applied both; the Bank Feed's card picker applied
    only the flag, so accounts that are virtual by name never counted there and
    "All Virtual" did not appear at all. One function, so the two cannot
    disagree again.
    """
    from .models_db import AppSettingRow as _AS, SourceAccountRow as _SA
    from engine.suggest import DEFAULT_VIRTUAL_PROGRAMS
    row = db.get(_AS, "virtual_programs")
    programs = (list(DEFAULT_VIRTUAL_PROGRAMS) if row is None else
                [w.strip().lower() for w in (row.value or "").split(",")
                 if w.strip()])
    out = set()
    for r in db.query(_SA).filter(_SA.company == company).all():
        text = (f"{r.nickname or ''} {r.qbo_account_name or ''} "
                f"{r.source or ''}").lower()
        if r.is_virtual or any(w.lower() in text for w in programs):
            out.add(r.source)
    return out


def api_fed_sources(db, company: str) -> tuple[set, dict]:
    """Which card accounts a program API feeds, and how stale each one is.

    Read from the CONNECTION ROWS in our own database -- not from the
    providers' status endpoints. That is what lets the Bank / Card Accounts
    grid use it: the grid cannot spend three calls to somebody else's API to
    draw a table, which is why those calls moved to the API Card Connections
    tab -- and why the badges vanished from the grid when they did.

    Returns (sources, ages-in-hours). An age of None means "configured but
    never pulled", a different problem from "pulled three weeks ago".
    """
    _api_age: dict = {}
    # EVERY program-fed account, from the connection lists.
    #
    # This read one Slash source out of the legacy single-value map, and knew
    # nothing about Taekus. So the card picker showed the API badge on the
    # first Slash account and on no other -- the second Slash program and every
    # Taekus account looked like ordinary uploads.
    #
    # Built from the same lists the panels use, so an account that is fed shows
    # as fed wherever it appears.
    _api_srcs: set = set()
    try:
        from .slash_sync import (connections as _sconn, source_map as _smap,
                                 _migrated as _smig)
        from .integrations import slash_api as _sapi
        from . import company_vars as _cvb
        # AS THE COMPANY: its Slash key is the one entered on Connectors for
        # it. Asked with no company in force, configured() could only find
        # the deployment's key -- none, now -- and every Slash account lost its
        # API badge on the card pickers and the Bank / Card Accounts grid.
        with _cvb.for_company(db, company):
            _slash_on = _sapi.configured()
        if _slash_on:
            _api_srcs |= {c.get("source") for c in _sconn(db, company)}
            # The legacy map only before migration -- it is never cleared, and
            # after a disconnect it names an account nothing feeds.
            if not _smig(db):
                _api_srcs.add(_smap(db).get(company) or "")
    except Exception:                       # noqa: BLE001
        pass
    try:
        from .taekus_sync import connections as _tconn
        _api_srcs |= {c.get("source") for c in _tconn(db, company)}
    except Exception:                       # noqa: BLE001
        pass
    try:
        # DIVVY TOO. Its connections were read a few lines below for the
        # staleness loop and never added here, so a Divvy account was the one
        # program-fed account that showed no API badge -- and was still
        # offered Connect, which would have pointed a second feed at it.
        from .divvy_sync import connections as _dconn0
        _api_srcs |= {c.get("source") for c in _dconn0(db, company)}
    except Exception:                       # noqa: BLE001
        pass
    try:
        # GLOBAL REWARDS: an account with its card account chosen is API-fed.
        from .gr_sync import connections as _gconn
        _api_srcs |= {c.get("source") for c in _gconn(db, company) if c.get("source")}
    except Exception:                       # noqa: BLE001
        pass

    # How long since each program-fed account last pulled anything.
    #
    # A Slash or Divvy account whose token has expired returns nothing, which
    # looks exactly like a card nobody used -- the same silent failure the
    # Plaid connections already guard against, and the reason those show an age.
    try:
        from datetime import datetime as _dtm
        from .divvy_sync import connections as _dconn
        from .models_db import UploadRow as _UR
        _srcs = {c.get("source") for c in _dconn(db, company)} | _api_srcs
        for _s in {x for x in _srcs if x}:
            r = (db.query(_UR).filter(_UR.company == company,
                                      _UR.method == "pull", _UR.source == _s)
                 .order_by(_UR.at.desc()).first())
            # None means "configured but never pulled", a different problem
            # from "pulled three weeks ago".
            _api_age[_s] = (
                round((_dtm.utcnow() - r.at).total_seconds() / 3600, 1)
                if r and r.at else None)
    except Exception:                       # noqa: BLE001
        log.exception("could not compute API feed staleness")
        _api_age = {}
    return {x for x in _api_srcs if x}, _api_age


def build_queue(db: Session, company: str, status: str = "for_review",
                card: str | None = None, source: str | None = None,
                limit: int = 200, offset: int = 0, stored_scores: bool = False,
                date_from=None, date_to=None,
                amt_min=None, amt_max=None, samt_min=None, samt_max=None,
                drift=None, rules_applied=None,
                vendor: str | None = None, profile: str | None = None,
                last4: str | None = None,
                memo: str | None = None,
                details: str | None = None,
                txn_type: str | None = None,
                kinds: str | None = None,
                cvendor: str | None = None,
                category: str | None = None,
                user: str | None = None,
                currency: str | None = None,
                ts_from=None, ts_to=None,
                tier: str | None = None,
                matchable: str | None = None,
                record: str | None = None,
                # A BACKGROUND PASS (filling STH flags) builds pages only to
                # learn the answers: it must not auto-add or post anything.
                no_autofile: bool = False,
                qbo_id: str | None = None,
                order_no: str | None = None,
                ingest: str | None = None,
                q: str | None = None,
                sort: str = "date", direction: str = "desc") -> dict:
    """One unified queue, mirroring the QuickBooks bank feed.

    Every charge appears once and can be resolved either way -- matched to a
    bill, or coded as an expense/refund. The engine offers both: ranked bill
    candidates AND a suggested category. The reviewer picks; nothing forces a
    charge into one lane before a human has looked at it.
    """
    idx = load_hal_index(db, company)
    set_hal_index(idx)

    # keep the validator's season list in step with what was actually mirrored
    from .models_db import AppSettingRow
    from engine.hal import set_tracked_seasons
    _s = db.get(AppSettingRow, "hal_seasons")
    if _s and _s.value:
        set_tracked_seasons([x.strip() for x in _s.value.split(",") if x.strip()])

    _f = dict(status=status, card=card, source=source,
              date_from=date_from, date_to=date_to,
              amt_min=amt_min, amt_max=amt_max,
              samt_min=samt_min, samt_max=samt_max, drift=drift,
              rules_applied=rules_applied,
              vendor=vendor, profile=profile,
              last4=last4, memo=memo, details=details, cvendor=cvendor,
              category=category, user=user, currency=currency,
              ts_from=ts_from, ts_to=ts_to,
              txn_type=txn_type, kinds=kinds, tier=tier,
              matchable=matchable, record=record,
              qbo_id=qbo_id, order_no=order_no, ingest=ingest, q=q)
    if txn_type and "payments" in str(txn_type):
        try:
            from .api import transfer_matches as _tm
            _f["_payment_ids"] = list(_tm(db, company).keys())
        except Exception:                                       # noqa: BLE001
            pass
    qy = apply_charge_filters(
        select(ChargeRow).where(ChargeRow.company == company), _f)

    # nickname per program, so rows show the account name your team chose
    from .models_db import SourceAccountRow
    # THE NICKNAME LEADS, not the QuickBooks account.
    #
    # These were the other way round, on the reasoning that the QuickBooks
    # account is what a card account "is". That is true of where its charges
    # POST and false of what the thing is called: once cards were mapped, the
    # picker stopped listing card accounts and started listing expense
    # accounts -- "Prepaid Inventory:NHL", "Interest Expense" -- which is not a
    # list of cards anybody recognizes.
    #
    # The card is "Divvy (Credit)". Where it posts belongs on the mapping
    # screen, which is where it is set.
    nick = {s_.source: (s_.nickname or s_.qbo_account_name or s_.source)
            for s_ in db.query(SourceAccountRow)
            .filter(SourceAccountRow.company == company).all()}
    # Charges imported under a bare "qbo:874" key, before that account was given
    # a SourceAccountRow. Named from QuickBooks so the picker never shows a raw
    # id, whatever order things were set up in.
    #
    # Keyed off the QuickBooks accounts themselves rather than off the sources
    # in play: this runs before the source list is built, and reaching forward
    # to a name that does not exist yet is what broke it the first time.
    try:
        from .models_db import QboRefRow as _QR
        # BANK AND CREDIT CARD ACCOUNTS ONLY.
        #
        # The picker pads its list from this map -- any key named here becomes
        # a selectable card account -- so dropping the usable_for filter put
        # the entire chart of accounts into the card picker. Naming and
        # eligibility are the same decision here, which is easy to miss because
        # the code reads as if it were only naming.
        # SKIP any QuickBooks account a SourceAccountRow already maps.
        #
        # Naming every bank account as "qbo:874" listed the SAME account twice
        # whenever it also had a mapped source key -- once under its own key,
        # carrying the API and VIRTUAL badges, and once under the raw id
        # without them. Two entries for one card account, one of them looking
        # like a different account with no badges.
        _mapped_ids = {r.qbo_account_id for r in db.query(SourceAccountRow)
                       .filter(SourceAccountRow.company == company).all()
                       if r.qbo_account_id}
        # THIS COMPANY'S QuickBooks file only. Without the realm filter every
        # connected file's bank accounts were injected into every company's
        # card list as `qbo:<id>` sources -- which is how a company with no
        # cards and no transactions still looked populated.
        for _r in db.query(_QR).filter(
                _QR.kind == "account", _QR.usable_for == "bank",
                _eq_or_none(_QR.realm_id, _company_realm(db, company))).all():
            if _r.qbo_id in _mapped_ids:
                continue
            nick.setdefault(f"qbo:{_r.qbo_id}", _r.name or f"qbo:{_r.qbo_id}")
    except Exception:                       # noqa: BLE001
        pass

    # Count and page in the DATABASE. This used to load every matching row into
    # Python, sort there and slice afterwards, so asking for 200 rows out of
    # 110,000 still materialized all 110,000 -- Categorized grew without bound
    # and took the page load with it.
    total = db.scalar(select(func.count()).select_from(qy.subquery())) or 0
    qy = qy.order_by(*_order_by(sort, direction, nick, db, company))
    if limit:
        qy = qy.offset(offset).limit(limit)
    elif offset:
        qy = qy.offset(offset)
    page = list(db.scalars(qy))
    # The payment-plan facts per team, read once for the page.
    _autofiled: list = []
    _to_post: list = []           # STH charges categorized automatically, to post
    # An export reads; it never posts.
    _auto_sth = auto_sth_on(db, company) and not stored_scores
    # Rows whose STH verdict changed on this pass, committed together at the
    # end rather than one at a time.
    _hal_flag_changed: list = []

    # Bank detail -> team, built once for the page so the STH block can name a
    # team before the coding block below has run.
    # One builder, one answer -- see team_claim_pairs.
    try:
        from engine.hal import expand_text as _exp0
        _early_map = {_exp0(k): v for k, v in team_claim_pairs(db)}
    except Exception:                       # noqa: BLE001
        _early_map = {}

    def _early_team(text: str) -> str:
        """The team a bank detail names, using the same rules as the coding
        block: exactly one claimant, and long enough not to collide."""
        try:
            from engine.hal import expand_text as _exp1
            low = _exp1(text)
        except Exception:                   # noqa: BLE001
            return ""
        if not low:
            return ""
        if low.strip() in _early_map:
            return _early_map[low.strip()]
        for _al in sorted(_early_map, key=len, reverse=True):
            if _al and _al in low:
                return _early_map[_al]
        return ""
    # Charges this has already filed once. Sending one back from Categorized
    # clears resolved_by, so that could not be the marker -- the audit trail is
    # the only record that survives the round trip.
    _autofiled_before: set = set()
    try:
        from .models_db import AuditRow as _AR2
        _ids = [r.charge_id for r in page]
        if _ids:
            _autofiled_before = {
                a.charge_id for a in db.query(_AR2.charge_id).filter(
                    _AR2.charge_id.in_(_ids)).all() if a.charge_id}
    except Exception:                       # noqa: BLE001
        _autofiled_before = set()
    from decimal import Decimal as _Dec
    _team_plan: dict = {}
    # Which teams have been trusted to add their own charges.
    _team_auto: dict = {}
    try:
        from .models_db import PaymentPlanRow as _PP, TeamAliasRow as _TA3

        # THE PAYMENT PLAN, where there is one.
        #
        # The plan used to be two columns on the team row -- a day and a count
        # of months -- copied from Airtable. Those are still read below for
        # teams that have not been given a plan yet, because dropping them
        # would stop matching for every such team the day this shipped. Where a
        # plan exists it wins, and it says more: which months, and what share
        # falls in each.
        _plans = {}
        # Plans are installation-wide now, like the teams they belong to; a
        # row still carrying a company from before that is read as well.
        # A COMPANY'S OWN PLAN for a team (Configuration > Payment Plans,
        # marked "own:") replaces the Dashboard's for that company -- when it
        # has anything set; a blank one defers to the Dashboard's.
        _pp_rows = [p for p in db.query(_PP).filter(_PP.company.in_(("", company))).all()
                    if not ((p.id or "").startswith("own:") and p.company != company)]
        _own_teams = {(p.team or "").lower() for p in _pp_rows if (p.id or "").startswith("own:")
                      and ((p.months or "").replace(",", "").strip() or p.days)}
        _pp_rows = [p for p in _pp_rows if (p.id or "").startswith("own:")
                    or (p.team or "").lower() not in _own_teams]
        for _pp in _pp_rows:
            _months = [x.strip() for x in (_pp.months or "").split(",")]
            _live = [i for i, v in enumerate(_months) if v and float(v or 0) > 0]
            if not _live and not _pp.days:
                continue                    # a row with nothing set is no plan
            # EVERY plan a team has, keyed by team. A team may run a
            # six-month and a ten-month schedule for the same seats, and
            # assigning into the dict kept only whichever came last -- so half
            # the charges were measured against a plan their buyer never took.
            _plans.setdefault(_bare_team(_pp.team).lower(), []).append({
                # `day` and `months` keep the names the matcher already uses, so
                # nothing downstream has to know where they came from.
                "day": (_pp.days or "").split(",")[0].strip() or None,
                "days": [d.strip() for d in (_pp.days or "").split(",") if d.strip()],
                "months": len(_live) or None,
                "shares": {i: float(_months[i] or 0) for i in _live},
                # EVERY charge day, not just the first. _tie_days reads the
                # text form and finds all of them; leaving it None meant a plan
                # charging on the 1st and the 15th was tested against the 1st
                # alone, and half its charges read as late.
                "day_text": (_pp.days or "").replace(",", " "),
            })

        # THE PAYMENT PLAN, AND NOTHING ELSE.
        #
        # day_of_month, plan_months and day_text are the Airtable columns. They
        # were read as a fallback for teams with no plan yet, so that switching
        # to Payment Plans did not stop the installment check on day one. That
        # was right for the changeover and is wrong now: a team with no plan
        # was still showing a due-date and an expected amount derived from a
        # source the engine no longer syncs, which cannot be corrected from any
        # screen -- the Payment Plans row is blank and the Bank Feed disagrees.
        #
        # No plan now means no installment expectation, which is true and is
        # fixable in one place.
        for _r in db.query(_TA3.team, _TA3.auto_add,
                           _TA3.auto_add_refunds).all():
            if not _r[0]:
                continue
            _k = _bare_team(_r[0]).lower()
            if _k in _plans:
                _team_plan[_k] = _plans[_k]
            if _r[1]:
                _team_auto[_k] = bool(_r[2])

        # A plan for a team with no alias row still counts -- the two tables
        # are edited separately and one can exist without the other.
        for _k, _v in _plans.items():
            _team_plan.setdefault(_k, _v)
    except Exception:                       # noqa: BLE001
        _team_plan, _team_auto = {}, {}



    # The extra bills behind any forced multi-bill match on this page, fetched
    # once rather than per row.
    _bill_lines: dict = {}
    try:
        from .models_db import ChargeBillMatchRow as _CBM, BillRow as _BR2
        _ids = [r.charge_id for r in page]
        if _ids:
            _rows = (db.query(_CBM)
                     .filter(_CBM.charge_id.in_(_ids))
                     .order_by(_CBM.position, _CBM.id).all())
            _bill_ids = {r.bill_id for r in _rows}
            _bills = {b.bill_id: b for b in db.query(_BR2).filter(
                _BR2.bill_id.in_(_bill_ids)).all()} if _bill_ids else {}
            for r in _rows:
                b = _bills.get(r.bill_id)
                _bill_lines.setdefault(r.charge_id, []).append({
                    "bill_id": r.bill_id,
                    "bill_no": (b.doc_number or b.bill_id) if b else r.bill_id,
                    "vendor": (b.vendor or "") if b else "",
                    "date": str(b.txn_date or "") if b else "",
                    "amount": str(r.amount),
                })
    except Exception:                       # noqa: BLE001
        _bill_lines = {}

    # WHAT HAPPENED TO EACH CHARGE AFTER IT WAS CATEGORIZED.
    #
    # The User and Categorized Time Stamp columns report the FIRST decision and
    # nothing else, so a charge coded by one person on Monday and recoded by
    # another on Friday reads on screen as though Monday were the whole story.
    # The audit log knew; the grid did not.
    #
    # One query for the page rather than one per row, and only where the
    # columns that would carry it are on screen -- For Review has no
    # categorization to be subsequent to.
    _later = {}
    if status in ("categorized", "excluded"):
        try:
            from .models_db import AuditRow as _AR
            from . import audit_labels as _al2
            _res = {r.charge_id: r.resolved_at for r in page if r.resolved_at}
            if _res:
                _ev = (db.query(_AR)
                       .filter(_AR.charge_id.in_(list(_res)),
                               _AR.action.in_(list(_al2.EDIT_ACTIONS)))
                       .order_by(_AR.at.asc()).all())
                for e in _ev:
                    _at = _res.get(e.charge_id)
                    # STRICTLY after, with a second of slack: the coding action
                    # and the resolved_at stamp are written in the same
                    # transaction and can land on the same microsecond, which
                    # would report every categorized charge as edited once.
                    if _at is None or (e.at - _at).total_seconds() <= 1:
                        continue
                    _later.setdefault(e.charge_id, []).append({
                        "at": e.at.isoformat() + "Z",
                        "actor": e.actor or "",
                        "what": _al2.label_for(e.action),
                        "detail": _al2.strip_actor(e.detail or "", e.actor or "",
                                                   company),
                    })
        except Exception:                   # noqa: BLE001
            log.exception("could not read later edits for the page")
            _later = {}

    # Candidate bills are only ever shown for an unresolved charge, so the
    # Categorized and Excluded tabs were paying for a scan whose result was
    # thrown away. Load the pool only when something on this page needs it.
    needs_suggestions = any(r.status == "for_review" and not r.is_credit
                            for r in page)
    if needs_suggestions:
        # Keep the ORM rows as well as the engine objects: the postability
        # check needs line_count and quantity, which live on the row.
        bill_rows = list(db.scalars(
            select(BillRow).where(BillRow.company == company)))
        bills = [_to_bill(b) for b in bill_rows]
    else:
        bill_rows, bills = [], []
    bill_by_id = {b.bill_id: b for b in bills}
    bill_row_by_id = {b.bill_id: b for b in bill_rows}

    from .learning import suggestion_for, rules_for

    # Read once for the page. Matching is per charge, but the table is not --
    # a hundred rows would otherwise be a hundred identical queries.
    _rules = rules_for(db, company)

    load_match_weights(db)  # tunable in the UI, so read per request
    try:
        _cad_rate = cad_rate(db)
        # Each day's rate, so a charge converts at its OWN date's rate.
        try:
            from .fx import history as _fxh
            _cad_hist = _fxh(db)
        except Exception:                           # noqa: BLE001
            _cad_hist = {}
    except Exception:                       # noqa: BLE001
        from .config import settings as _cfg0
        _cad_rate = float(_cfg0.cad_usd_rate or 0.73)
        _cad_hist = {}
    # CANADIAN TEAMS' COSTS: in CAD (converted at the charge date's rate) unless
    # the broker's Season Ticket Records say they are already in USD.
    _sth_usd = sth_costs_in_usd(db, company)
    _amex = amex_parents(db, company)
    reset_account_cache()   # accounts can change between requests
    reset_email_cache()
    from .canada import reset_rules
    reset_rules()           # both rule tables are editable in the UI
    reset_payment_rules()
    items, _drifted = [], []
    _flagged = 0
    for row in page:
        # Self-heal the descriptive flags for the rows actually on screen.
        # They were only recomputed during a bill sync, so a wire transfer kept
        # showing as a refund until the next one -- and on an already-resolved
        # charge, never. This is a regex over 200 rows; the cost is nothing and
        # the grid is right the moment you look at it.
        from .canada import looks_canadian as _lc
        _pay = is_payment(row, db)
        _cad = _lc(row.merchant, row.raw_description, row.memo, db=db,
            vendor=row.coded_vendor or row.matched_bill_vendor
                   or row.suggested_vendor or "")
        if bool(row.is_card_payment) != _pay or bool(row.is_canadian) != _cad:
            row.is_card_payment, row.is_canadian = _pay, _cad
            _flagged += 1

        c = _to_charge(row, db)
        # A written rule beats the engine's default: it is what somebody
        # decided for this case, not a guess from a hardcoded list. Matched
        # against the ROW, since a rule can test the card account and the
        # amount as well as the bank detail.
        #
        # Resolved BEFORE classify so its vendor can say which team this is.
        # That is what lets a bank detail the alias table has never seen reach
        # the season-ticket record: the rule already named the team.
        # A row already coded doesn't use a rule's suggestion -- its own vendor
        # comes first below -- so the rules aren't run for it. On Categorized
        # that was 150 rules against every row, 95% of an export's time.
        # Nor for a bill payment (its vendor is the bill's) or an excluded
        # row: a suggestion is only ever for a row still to be decided.
        learned = (None if (row.status != "for_review"
                            and (row.coded_vendor or row.matched_bill_id or row.status == "excluded"))
                   else suggestion_for(db, company, row.merchant, charge=row, rules=_rules))
        k = classify(c, team_hint=(row.coded_vendor
                                   or (learned or {}).get("vendor") or ""))
        # A refund is money returned; it can't pay down a bill, so don't
        # offer candidates that would only lead to a rejected post.
        # SAVED SCORES (exports): the band and score the background rescoring
        # already saved, rather than scoring every charge against every bill
        # again -- millions of comparisons for a full export.
        sg = ([] if (stored_scores or row.is_credit or row.status != "for_review" or not bills)
              else suggest(c, bills))
        # Drop anything the write path would refuse. Offering a candidate that
        # cannot be posted wastes a click and teaches reviewers to distrust the
        # suggestions; a charge whose only candidate is unpostable is honestly
        # "no candidate" and belongs in coding instead.
        sg = [s_ for s_ in sg
              if _candidate_postable(c.amount, bill_row_by_id.get(s_.bill_id))
              ][:MAX_SUGGESTIONS]
        top = sg[0].score if sg else 0
        if row.status == "for_review" and not row.is_credit and bills and not stored_scores:
            tier, score_pct = tier_for(top, bool(sg)), int(round(top * 100))
            # Self-healing: if the stored value drifted since the last refresh,
            # correct it now rather than letting the filter and the badge argue.
            if row.tier != tier or row.score != score_pct:
                row.tier, row.score = tier, score_pct
                _drifted.append(row)
        else:
            tier = row.tier or "none"

        items.append({
            "id": c.charge_id, "src": c.source,
            "account": nick.get(c.source, c.source),
            "amt": str(c.amount or 0), "date": str(c.txn_date or ""),
            "merchant": c.merchant, "memo": row.memo or "", "custom_note": row.custom_note or "",
            # Amounts are stored absolute, so without this a refund is
            # indistinguishable from a charge in the grid.
            "is_credit": bool(row.is_credit),
            # A payment onto the card, not a refund from a merchant -- the
            # Amount column colors and badges them differently.
            # What QuickBooks now holds that this does not agree with. Shown
            # rather than resolved: see app/qbo_drift.py.
            "qbo_drift": (row.qbo_drift or ""),
            "is_card_payment": bool(row.is_card_payment),
            "is_canadian": bool(row.is_canadian),
            "card": card_ending(row, _amex), "holder": c.cardholder_name or "",
            # None, not "", so the grid can tell "not stated" from a value.
            "order_number": row.order_number or "",
            # The portal's own identifier for the transaction, captured at
            # upload. Useful when reconciling against the source export.
            "txn_id": row.source_txn_id or "",
            "ingest_method": (row.ingest_method or "csv"),
            # WITH the UTC marker. These are stored naive-UTC, and an ISO
            # string carrying no offset is read by the browser as LOCAL time --
            # so a charge ingested at 20:00 UTC displayed as 20:00 Eastern,
            # five hours in the future. `resolved_at_utc` a few lines below has
            # always appended the Z; this one never did, which is why only the
            # uploaded column was wrong.
            "uploaded_at": (row.uploaded_at.isoformat(timespec="minutes") + "Z"
                            if row.uploaded_at else ""),
            # A forced match can settle SEVERAL bills. matched_bill_* describes
            # only the first, so the rest are carried here -- otherwise the row
            # reports one bill for a payment that covered two.
            "bill_lines": _bill_lines.get(row.charge_id, []),
            # Everything done to this charge SINCE it was categorized. Empty
            # for the overwhelming majority; the grid shows a marker only where
            # there is something to show.
            "later_edits": _later.get(row.charge_id, [])[-8:],
            "later_edit_count": len(_later.get(row.charge_id, [])),
            "force_matched": bool(getattr(row, "force_matched", False)),
            "source_currency": getattr(row, "source_currency", None),
            "source_amount": (str(row.source_amount)
                              if getattr(row, "source_amount", None) is not None
                              else None),
            "status": row.status, "resolution": row.resolution,
            "matched_bill_id": row.matched_bill_id,
            "coded_category": row.coded_category,
            "coded_vendor": row.coded_vendor,
            # Snapshot taken at match time; the bill itself may since have been
            # pruned, so these are the only record of what it was.
            "matched_bill_no": row.matched_bill_no or "",
            "matched_bill_vendor": row.matched_bill_vendor or "",
            "matched_bill_date": row.matched_bill_date or "",
            "matched_bill_memo": row.matched_bill_memo or "",
            "resolved_by": row.resolved_by,
            # Chicago wall time: the team is there, and a UTC stamp on a
            # spreadsheet gets misread as local every time.
            # UTC, ISO 8601 with a Z. The browser renders it in whatever zone
            # the person's machine is set to -- a fixed zone is wrong for
            # anyone outside it, and the server has no way to know.
            "resolved_at_utc": (row.resolved_at.isoformat() + "Z"
                                if row.resolved_at else ""),
            "qbo_txn_id": row.qbo_txn_id, "qbo_txn_type": row.qbo_txn_type,
            "post_error": row.post_error,
            "tier": tier,
            # Resolved rows aren't rescored on view, so fall back to what was
            # stored when they were still in the queue.
            "score": ((row.score or 0) if stored_scores
                      else (round(top * 100) if sg else 0) if bills else (row.score or 0)),
            # The engine composes category names from the season and team
            # ("Ticket Credits:CFL:Calgary Stampeders (TC)"), and not every
            # combination it can build exists in the chart of accounts. A
            # suggestion QuickBooks will reject is worse than none: it looks
            # decided, so it gets committed, and fails at post time.
            # Cleared on purpose means cleared. The engine still HAS an opinion
            # here -- it recomputes one on every request -- and returning it
            # anyway is what made Clear look like it had done nothing.
            # Stored first, computed second. The stored value is what a clear
            # writes to and what the filters search, so preferring the live
            # computation meant the grid could show something the database did
            # not agree with -- which is how a cleared vendor came back.
            # A written rule's category is kept AS WRITTEN. Passing it through
            # the QuickBooks check blanked it whenever the account did not
            # exist there -- so a correct rule showed its RULE badge beside an
            # empty cell, with nothing to say why. It is flagged NOT IN QBO
            # further down instead, which is the actionable version.
            "suggested_category": "" if row.category_cleared else (
                row.suggested_category
                or (_season_account(learned["category"], k.coding.get("vendor") or "",
                                    from_rule=True) if learned
                    else _known_account(
                        db, _season_account(k.coding.get("category") or "",
                                            k.coding.get("vendor") or "")))),
            "suggested_vendor": "" if row.vendor_cleared else (
                row.suggested_vendor
                or (learned["vendor"] if learned
                    else _known_vendor(db, k.coding.get("vendor", "")))),
            # A rule that divides the charge says so here. The grid badges it
            # SPLIT and the Add path writes the lines instead of one category.
            "rule_split": (learned or {}).get("split") or [],
            "coding_rule": "rule" if learned else k.rule,
            "coding_why": (
                f"rule: {learned['rule_name']}" if learned
                else "; ".join(k.reasons)),
            "learned": bool(learned),
            # Whether the value ON SCREEN came from a rule. Only true while the
            # charge still carries the rule's answer: once somebody types over
            # it, coded_vendor holds their choice and the badge has to go, or
            # it would be crediting the rule with a decision a person made.
            # Also true once the transaction is CODED, when what was coded is
            # exactly what the rule said. On Categorized the value has moved
            # into coded_vendor, so the old test -- "no coded value yet" --
            # made the badge vanish the moment the rule's answer was accepted,
            # which is the one time it is most worth seeing.
            "vendor_from_rule": bool(
                learned and not row.vendor_cleared and (learned["vendor"] or "")
                and (not row.coded_vendor
                     or row.coded_vendor == learned["vendor"])),
            "category_from_rule": bool(
                learned and not row.category_cleared
                and (learned["category"] or "")
                and (not row.coded_category
                     or row.coded_category == learned["category"])),
            # The STORED provenance wins over the recomputed one. A charge
            # coded last month by a rule since edited or deleted should say
            # what actually decided it, not what today's tables would decide.
            "rule_name": (row.rule_detail or
                          (learned["rule_name"] if learned else "")),
            "rule_source": (row.rule_source or ""),
            "rule_detail": (row.rule_detail or ""),
            "hal_status": k.coding.get("hal_status", ""),
            "season": k.coding.get("season", ""),
            "is_expense_like": k.charge_class != ChargeClass.BILL_PAYMENT,
            # ranked bill candidates, always offered
            "candidates": [{
                "bill": (bill_by_id[s.bill_id].doc_number
                         or bill_by_id[s.bill_id].bill_id),
                "bill_id": s.bill_id,
                "amt": str(bill_by_id[s.bill_id].amount),
                "date": str(bill_by_id[s.bill_id].txn_date),
                "vendor": bill_by_id[s.bill_id].vendor or "",
                "memo": (bill_by_id[s.bill_id].memo or "")[:90],
                "email": bill_by_id[s.bill_id].email or "",
                "score": round(s.score, 2),
                # Per-candidate, so the expanded list can show each one's own
                # strength rather than borrowing the top candidate's.
                "tier": tier_for(s.score, True),
                "why": "; ".join(s.reasons),
            } for s in sg],
        })

    # One query for the page, not one per row: a hundred charges would
    # otherwise be a hundred round trips to learn that most aren't split.
    try:
        from .models_db import ChargeSplitRow
        ids = [it["id"] for it in items]
        if ids:
            counts: dict = {}
            for r in (db.query(ChargeSplitRow)
                      .filter(ChargeSplitRow.charge_id.in_(ids)).all()):
                counts[r.charge_id] = counts.get(r.charge_id, 0) + 1
            # The lines themselves, not just how many. Without them the grid
            # said "Split 2" and there was nowhere to find out into what.
            detail: dict = {}
            for r in (db.query(ChargeSplitRow)
                      .filter(ChargeSplitRow.charge_id.in_(ids))
                      .order_by(ChargeSplitRow.position, ChargeSplitRow.id).all()):
                detail.setdefault(r.charge_id, []).append(
                    {"category": r.category, "amount": str(r.amount)})
            for it in items:
                it["split_lines"] = counts.get(it["id"], 0)
                it["split_detail"] = detail.get(it["id"], [])
    except Exception:                       # noqa: BLE001
        for it in items:
            it.setdefault("split_lines", 0)

    # --- nothing is suggested that QuickBooks does not have ------------------
    #
    # A suggestion is an answer somebody will accept with one click, so it has
    # to be postable. The engine was offering vendors and categories built from
    # the bank detail -- "UNITED CENTER", "UNITED CENTER (TC)" -- which exist
    # nowhere, and Add then failed at the last step. A blank field is a smaller
    # problem than a confident wrong one: it invites the reviewer to choose,
    # which is what they would have had to do anyway.
    try:
        from .models_db import QboRefRow as _QR
        _known_cats = {r[0] for r in db.query(_QR.name).filter(
            _QR.kind == "account").all() if r[0]}
        _known_vens = {r[0] for r in db.query(_QR.name).filter(
            _QR.kind == "vendor").all() if r[0]}
    except Exception:                       # noqa: BLE001
        _known_cats = _known_vens = set()
    if _known_cats or _known_vens:
        for it in items:
            # A written RULE is an instruction, not a guess, so it is kept even
            # when the account does not exist yet -- and flagged instead, so the
            # reason is visible. Silently blanking it made one rule look like it
            # applied to some rows and not others: the same Anaheim Ducks rule
            # showed on rows whose season account existed and vanished on the
            # rest, which reads as the rule being broken rather than an account
            # being missing.
            missing = []
            _by_rule = (it.get("coding_rule") == "rule"
                        or it.get("category_from_rule")
                        or it.get("vendor_from_rule"))
            sc = it.get("suggested_category")
            if sc and _known_cats and sc not in _known_cats:
                if _by_rule:
                    missing.append(f"account '{sc}' is not in QuickBooks")
                else:
                    it["suggested_category"] = ""
            sv = it.get("suggested_vendor")
            if sv and _known_vens and sv not in _known_vens:
                if _by_rule:
                    missing.append(f"vendor '{sv}' is not in QuickBooks")
                else:
                    it["suggested_vendor"] = ""
            if not it.get("suggested_category"):
                it["category_from_rule"] = False
            if not it.get("suggested_vendor"):
                it["vendor_from_rule"] = False
            if missing:
                # Add will refuse until it exists, so say so here rather than
                # letting the click fail at the last step.
                it["needs_qbo"] = "; ".join(missing)

    # --- the season-ticket account behind a charge ---------------------------
    #
    # Many charges are routine monthly payments against a season-ticket
    # account. They are ADDED rather than matched, so the bill candidate list
    # says nothing about them -- and the reviewer had no way to see which STH
    # record a payment belonged to without leaving the app for Airtable.
    #
    # Resolved with the SAME function that classifies them, so the panel can
    # never disagree with the decision the engine made.
    try:
        from engine.hal import validate as _hal_validate
        from .models_db import TeamAliasRow as _TA, AppSettingRow as _AS
        _idx = load_hal_index(db, company)
        # Still needed: the panel SHOWS the league beside the team, even though
        # the renewal years no longer come from it. Kept for the label, not for
        # the lookup it used to serve.
        _league_of = {r.team: (r.league or "")
                      for r in db.query(_TA.team, _TA.league).distinct().all()}
        # This company's own leagues for its teams (Configuration > Teams).
        try:
            from .company_teams import league_overrides as _lov, teams as _cteams
            _lo = _lov(db, company)
            for _t in list(_league_of):
                if _t.lower() in _lo:
                    _league_of[_t] = _lo[_t.lower()]
            for _ct in _cteams(db, company):
                _league_of.setdefault(_ct["team"], _ct.get("league", ""))
        except Exception:                               # noqa: BLE001
            pass
        # PER TEAM, from the Payment Plans grid.
        #
        # This read "league_seasons" -- one set of years for a whole league --
        # which is the arrangement the Renewal Years column replaced. Left
        # here, a STH lookup showed whatever years the league was set to, not
        # the years chosen for the team, and nothing on any screen could change
        # it: the grid that set league_seasons is gone.
        try:
            from .models_db import PaymentPlanRow as _PP4
            _seasons = {}
            # This company's plans only, its own ("own:") winning.
            for _r4 in sorted(db.query(_PP4).filter(_PP4.company.in_(("", company))).all(),
                              key=lambda r: (r.id or "").startswith("own:")):
                if (_r4.id or "").startswith("own:") and _r4.company != company:
                    continue
                if _r4.seasons:
                    _seasons[_r4.team] = [x.strip() for x in
                                          _r4.seasons.split(",") if x.strip()]
        except Exception:                   # noqa: BLE001
            _seasons = {}
        _totals = {}
    except Exception:                       # noqa: BLE001
        _idx = None

    try:
        from .models_db import TeamAliasRow as _TA2
        # Same builder as the STH hint and STH's own resolver, so all
        # three agree by construction rather than by being kept in step.
        _alias_team = {k: v for k, v in team_claim_pairs(db, company=company)}
    except Exception:                       # noqa: BLE001
        _alias_team = {}

    from engine.hal import expand_text as _expand
    _alias_team = {_expand(k): v for k, v in _alias_team.items()}

    def _team_from_detail(text: str) -> str:
        """The team a bank detail names, via the alias table. Longest first, so
        a specific alias beats a shorter one that happens to be inside it.

        Both sides go through the abbreviation table, so an alias written
        "Los Angeles Clippers" still matches a bank detail reading
        "LA CLIPPERS LLC"."""
        low = _expand(text)
        if not low:
            return ""
        if low.strip() in _alias_team:
            return _alias_team[low.strip()]
        for alias in sorted(_alias_team, key=len, reverse=True):
            if len(alias) > 3 and alias in low:
                return _alias_team[alias]
        # Then the same strict test STH's own resolver uses: every significant
        # word of the team name present in the detail.
        #
        # The two tests answer different questions and neither covers the
        # other. Substring finds a WRITTEN ALIAS -- "arena operating" -- which
        # word matching would never invent. Word matching finds a team named in
        # words the string test cannot see through: "ORLANDO CITY SOCCER"
        # contains every significant word of "Orlando City SC", but not the
        # name as a substring, because after "orlando city s" comes an "o".
        #
        # That gap is why a charge could show a STH record and still leave
        # Vendor and Category blank: STH was reached through the resolver,
        # coding was reached through the substring, and only one of them
        # matched. Consulting both makes the two agree.
        try:
            from engine.suggest import team_for_prefill as _tfp
            return _tfp(text) or ""
        except Exception:                   # noqa: BLE001
            return ""


    # --- shared billing entities: KSE, MLSE, one college for two sports -----
    #
    # These bank details name a candidate SET rather than a team. Narrowed here
    # with evidence the string does not carry, in the order the evidence
    # deserves to be trusted:
    #
    #   1. which of the candidates this cardholder actually holds a season
    #      account for -- decisive when they hold exactly one;
    #   2. the plan's billing day, when the candidates bill on different days;
    #   3. the installment amount, when the plans differ in size.
    #
    # A guess is made ONLY when one candidate survives and at least one piece
    # of evidence positively chose it. Two survivors, or none, leaves the row
    # blank -- which is what happens today, so nothing gets worse.
    #
    # Everything filled this way is marked `likely`, and the badge is a
    # different color. It is a deduction from circumstance, not somebody's
    # stated intent, and it must not look like one.
    try:
        _cands = team_claim_candidates(db)
    except Exception:                       # noqa: BLE001
        _cands = {}
    _cands = {_expand(k): v for k, v in _cands.items()}

    def _narrow(text: str, row) -> tuple:
        """(team, why) for an ambiguous bank detail, or ("", "")."""
        low = _expand(text)
        if not low:
            return "", ""
        hit = ""
        for key in sorted(_cands, key=len, reverse=True):
            if len(key) > 3 and key in low:
                hit = key
                break
        if not hit:
            return "", ""
        names = [t for t in _cands[hit]]

        # 1. season accounts this cardholder holds
        # See holdings_for: one answer to "what does this cardholder hold",
        # shared with the STH block rather than reimplemented here.
        held = holdings_for(db, row.cardholder_name or "", row.email or "")
        by_hold = [t for t in names if t in held]
        if len(by_hold) == 1:
            return by_hold[0], f"only {by_hold[0]} account held"
        pool = by_hold or names

        # A team's PLANS, plural. A team may run a six-month and a ten-month
        # schedule, so each entry is a list -- and treating it as one dict is
        # what threw "'list' object has no attribute 'get'" on the Bank Feed.
        # A team matches when ANY of its plans does.
        def _plans_of(t):
            v = _team_plan.get(t.lower())
            if not v:
                return []
            return v if isinstance(v, list) else [v]

        # 2. billing day
        by_day = [t for t in pool
                  if any(_tie_date(row.txn_date, pl.get("day"),
                                   pl.get("day_text"))
                         for pl in _plans_of(t))]
        if len(by_day) == 1:
            return by_day[0], f"billing day matches {by_day[0]}"
        pool = by_day or pool

        # 3. installment amount
        by_amt = []
        for t in pool:
            plans_t = [pl for pl in _plans_of(t) if pl.get("months")]
            if not plans_t:
                continue
            try:
                from .models_db import HalRow as _HR3
                tot = sum((h.total or 0) for h in db.query(_HR3).filter(
                    _HR3.company == row.company,
                    _HR3.holder_name == row.cardholder_name).all()
                    if _bare_team(h.team or "") == t)
            except Exception:               # noqa: BLE001
                continue
            if tot and any(_tie_amount(row.amount, tot, pl.get("months"),
                                       _plan_share(pl, row.txn_date))
                           for pl in plans_t):
                by_amt.append(t)
        if len(by_amt) == 1:
            return by_amt[0], f"installment matches {by_amt[0]}"
        return "", ""


    for it in items:
        it["hal"] = None
    if _idx is not None:
        from .models_db import HalRow as _HR
        # `page` is the rows this render is actually about; `rows` is a much
        # earlier query in this function and pairing against it would have
        # attached each charge's account to a different charge.
        for it, row in zip(items, page):
            if not row.cardholder_name:
                continue
            # THE BANK DETAIL MUST NAME A TEAM BEFORE STH IS CONSULTED.
            #
            # STH answers "which season account does this cardholder hold for
            # THIS team". With no team it degrades into "which season account
            # does this person hold", and since most cardholders hold exactly
            # one, it returned that one for any charge at all -- an AT&T bill
            # showed Dallas Cowboys, "MLB" showed the Padres, "Bluejackets"
            # showed Blue Jackets records with the vendor column empty.
            #
            # The tell was always visible on screen: a STH record beside a
            # blank Vendor. The vendor is empty precisely when no team was
            # identified, so a record shown there was answering a question
            # nobody had asked. The same resolver now gates both, so a STH
            # record can no longer appear without the vendor that justifies it.
            _team = _team_from_detail(row.merchant or row.raw_description or "")
            if not _team:
                # A shared billing entity narrowed to one team counts. The
                # cardholder's holdings are part of how it was narrowed, so
                # looking the record up is reading the same evidence back --
                # but the record is what makes the deduction checkable on
                # screen, and a `likely` vendor with no record beside it is the
                # same silent mismatch, inverted.
                _team, _ = _narrow(row.merchant or row.raw_description or "", row)
            if not _team:
                continue
            try:
                # The SESSION is passed now, so the charge carries the
                # cardholder's known addresses. STH identity is the name on the
                # card and the addresses that name is known by -- the card
                # number is no longer consulted, so skipping the email lookup
                # here would remove the very evidence a differently-written name
                # is resolved by. The lookup reads a per-request cache, so it
                # costs nothing per row.
                #
                # The vendor already established for this row is passed as the
                # team hint. For "SEATTLE HOCKEY" a rule has already said the
                # vendor is the Seattle Kraken; without that, the bank detail
                # and the team name share only a stopword and STH was never
                # consulted at all.
                # The alias table is consulted HERE too, not only in the coding
                # block below. That block runs after this one, so its vendor was
                # not set yet when the hint was read -- which meant exactly the
                # teams whose bank detail does not name them ("ARENA OPERATING
                # COMPANY" for the Panthers) gave STH no hint and found nothing,
                # while teams named outright in the detail resolved inside STH
                # and worked. That is the team-by-team split.
                # THE ALIAS TABLE FIRST, then the vendor. Order matters and
                # used to be the other way round.
                #
                # This hint is passed into STH to say which team's record to
                # look for, so it has to be a TEAM. The alias table answers in
                # team names; the vendor fields answer in QuickBooks vendor
                # names, which are spelled differently on purpose ("Florida
                # Panthers Hockey Club" for "Florida Panthers").
                #
                # On a charge nothing had touched yet, the vendor fields were
                # empty and the hint fell through to the alias table, so STH
                # got a team name and worked. Once a pass had written the
                # vendor onto the row, that vendor won the `or` chain and STH
                # was handed a name no record carries -- so a charge that
                # matched before a resync found nothing after one, and only
                # for teams whose bank detail does not name them outright.
                # Teams named in the detail kept working, because STH could
                # still score the merchant text directly. That split is what
                # made it look like particular aliases had stopped working.
                #
                # The vendor stays as the fallback for the opposite case: when
                # the bank detail claims no team ("SEATTLE HOCKEY"), a rule's
                # vendor is the only evidence there is.
                # The team the bank detail named, and nothing else. The
                # vendor fields used to serve as a fallback here; they are the
                # same answer arriving by a longer route when they are right,
                # and a stale value from an earlier pass when they are not.
                # A PROMOTER IS NOT A TEAM HINT.
                #
                # "Live Nation (Generic)" and "Broadway (Generic)" exist so a
                # bank detail like "LN PREMIUM" is recognized at all, but no
                # season record carries those names -- so passing one as the
                # hint found nothing, and the coding block then guessed a
                # vendor from the promoter name. Dropping the hint asks STH the
                # right question instead: which season account does THIS
                # CARDHOLDER hold? For a promoter charge that is the whole
                # answer, because the promoter sold a ticket to a show the
                # holder has an account for.
                _hint = "" if is_generic_team(_team) else _team
                v = _hal_validate(_to_charge(row, db), _idx, team_hint=_hint)
            except Exception:               # noqa: BLE001
                continue
            rec = getattr(v, "record", None)
            if rec is not None and _hint:
                try:
                    _want = _bare_team(_hint).lower()
                    _got = _bare_team(getattr(rec, "team", "") or "").lower()
                    if _want and _got and not _same_team(_want, _got):
                        rec = None
                except Exception:           # noqa: BLE001
                    pass
            if not v.matched or rec is None:
                # Looked and found nothing. Written down as well as a match --
                # "no record" is an answer, and the filter has to be able to
                # ask for it.
                if row.hal_matched is not False:
                    row.hal_matched = False
                    _hal_flag_changed.append(row)
                continue
            if rec.record_id not in _totals:
                # Composite key: the record id alone is no longer unique,
                # because each broker's base numbers its own records.
                from .company_vars import sth_scope as _scope2
                h = db.get(_HR, (_scope2(db, company), rec.record_id))
                _totals[rec.record_id] = (h.total, h.lookup_name) if h else (None, "")
            # NB: a distinct name from the paging `total` (the row count set
            # above and returned to the client). Reusing `total` here overwrote
            # that count with this STH account's dollar total, so the pager read
            # "1-200 of 24375" -- a season total, changing with whichever STH row
            # happened to be last on the page.
            hal_total, lookup = _totals[rec.record_id]
            league = _league_of.get(rec.team, "")
            # Keyed by TEAM. With no years chosen for it the panel shows
            # none, rather than a year nobody asked for -- which is the honest
            # answer and points at the Renewal Years column, where it is fixed.
            want = _seasons.get(rec.team) or []
            statuses = [{"season": s, "status": rec.statuses.get(s) or "—"}
                        for s in want]
            # STH sets NO coding. It answers "which season-ticket record does
            # this charge belong to", and that is all it is for.
            #
            # It used to prefill the vendor and the category as well, deriving
            # TC or DEP from the record's Full/Partial value. That is why one
            # team's rows disagreed with each other: two charges to the same
            # team, under the same rule, landed on different accounts because
            # somebody had marked one account "Mini Plan" and another "Partial
            # Deposit" -- and where the (DEP) account did not exist the field
            # simply went blank. Coding now comes from the team aliases and the
            # rules built on them, which are maintained for exactly this
            # purpose; the STH record is evidence shown beside it.
            it["rule_name"] = it.get("rule_name") or f"season account: {rec.team}"
            # WHICH COLLEGE SPORT, read from the season record.
            #
            # NCAAF and NCAAB rows are excluded from the alias table because
            # they duplicate the NCAA row and made every college team
            # ambiguous. The sport still has to come from somewhere, and the
            # season account is the one thing that knows: a ticket account is
            # for one sport and its own text says which.
            # Carried across for the coding block: when the bank detail named
            # only a promoter, this is the team the cardholder's season account
            # actually belongs to.
            it["_hal_team"] = rec.team
            _sp = _sport_hint(rec.team, rec.plan_type or "",
                              getattr(rec, "address_book", "") or "")
            if _sp:
                it["_sport"] = _sp

            # A HOLDER WHOSE ONLY RECORDS ARE DEPOSITS BELONGS IN (DEP).
            #
            # The paragraph above is why STH stopped setting the category at
            # all: deriving TC or DEP per record made two charges to the same
            # team disagree with each other. This is the narrow case that
            # survives that reasoning -- not "this record says Deposit" but
            # "EVERY record this holder has for this team is a deposit", which
            # cannot disagree with itself.
            #
            # Three guards, and all three matter:
            #   * every plan must be a deposit, not merely one of them;
            #   * a category set by a WRITTEN RULE is left alone -- a rule is an
            #     instruction, and this is an inference;
            #   * the (DEP) account must exist in QuickBooks, or the field would
            #     be blanked and the row would look worse than before.

            # One season-ticket account routinely spans SEVERAL STH rows -- the
            # same holder and team, one row per seat location. Showing whichever
            # row matched first reported a fraction of the account and made two
            # charges to the same account look like different accounts. All the
            # holder's rows for this team are added up, grouped by plan type,
            # since that is the unit somebody thinks in.
            from sqlalchemy import func as _fn
            _bare_t = _bare_team(rec.team).lower()
            # WHICH of the team's plans, chosen on amount. One plan is the
            # usual case and picks itself; where a team runs two, the
            # installment size is the only thing that says which the buyer took.
            # `grand` is not known yet here, so the choice is deferred until it
            # is -- see below, where _pick_plan is called with the season total.
            _plan_options = _team_plan.get(_bare_t) or []
            _plan = _plan_options[0] if _plan_options else {}
            # Settled once `grand` is known, further down -- there is nothing
            # to compare an amount against until the season total exists.
            _chosen = _plan
            plans: dict = {}
            plan_status: dict = {}
            grand = None
            seat_rows = 0
            # Reset per charge: a value left from the previous row must never
            # describe this one.
            _all_rows, _pick = [], None
            try:
                # THIS COMPANY'S RECORDS ONLY. Gathered by name and team alone,
                # another broker's records for a holder of the same name were
                # mixed into the line and its totals.
                _rows = [h for h in db.query(_HR).filter(
                            _HR.company == company,
                            _HR.holder_name == rec.holder_name).all()
                         if _bare_team(h.team or "").lower() == _bare_t]
                _all_rows = list(_rows)       # every season's, for the status pills
                def _st(h) -> dict:
                    try:
                        return json.loads(h.statuses_json or "{}") or {}
                    except Exception:       # noqa: BLE001
                        return {}
                # THIS SEASON'S RECORDS. A broker with a row (or a tab) per
                # season has one record per season for the same seats; adding
                # every season's together put several years' costs in one
                # total. Only records with a status for the charge's season
                # count, when there are any.
                # ONE SEASON, not every chosen one. With two renewal years
                # chosen (or a tab per season), the records of BOTH seasons
                # have a status, so both were added together -- the totals
                # read about double. The charge's own season is used when
                # records carry it, otherwise the newest chosen season that
                # has records. With no years chosen, the charge's season.
                _cands = list(want) or ([v.season] if getattr(v, "season", None) else [])
                _by = {x: [h for h in _rows if (_st(h).get(x) or "").strip()] for x in _cands}
                _pick = getattr(v, "season", None)
                if not _by.get(_pick):
                    _pick = next((x for x in sorted(_cands, reverse=True) if _by.get(x)), None)
                _season = _by.get(_pick) or []
                if _season:
                    _rows = _season
                    _one = [_pick]
                else:
                    _one = list(want)
                # An account with a live plan is described by that plan; only
                # when nothing is live do the other rows speak for it. Live by
                # THIS BROKER'S status words (Ticket Guy's "Yes"), not only
                # the word "Active".
                from engine.hal import status_kind as _sk
                from .company_vars import status_words as _swq
                _words = _swq(db, company)
                _live = [h for h in _rows if any(_sk(_st(h).get(x), _words) == "active" for x in _one)]
                if _live:
                    _rows = _live
                for h in _rows:
                    seat_rows += 1
                    key = (h.plan_type or "").strip() or "—"
                    amt = h.total
                    if amt is not None:
                        plans[key] = (plans.get(key) or 0) + amt
                        grand = (grand or 0) + amt
                    else:
                        plans.setdefault(key, plans.get(key) or 0)
                    # Each seat row carries its OWN season status, and they do
                    # differ -- one plan renewed, another canceled. Showing the
                    # matched row's status beside a total covering every row
                    # said "Active" over an amount that included a canceled
                    # plan, which is worse than saying nothing.
                    try:
                        st = json.loads(h.statuses_json or "{}")
                    except Exception:       # noqa: BLE001
                        st = {}
                    for _s in want:
                        plan_status.setdefault(key, {}).setdefault(
                            _s, set()).add(st.get(_s) or "—")
            except Exception:               # noqa: BLE001
                seat_rows = 0
            # Only when NO record was read: one record of the chosen season is
            # that season's figure -- the matched record may be another
            # season's (a tab per season), and used to replace it.
            if seat_rows == 0:
                plans = ({(rec.plan_type or "—"): hal_total}
                         if hal_total is not None else {})
                grand = hal_total

                plan_status = {(rec.plan_type or "—"):
                               {s: {rec.statuses.get(s) or "—"} for s in want}}
            # EACH SEASON'S PILL FROM THAT SEASON'S OWN RECORDS. The total is
            # the charge's season; the pills for the other chosen years were
            # read from that season's records too, which carry no status for
            # them -- so they showed a red "--" where each year alone is green.
            try:
                if want and locals().get("_all_rows"):
                    def _st2(h):
                        try:
                            return json.loads(h.statuses_json or "{}") or {}
                        except Exception:   # noqa: BLE001
                            return {}
                    for _k in list(plan_status or {}):
                        for _s in want:
                            vals = {(_st2(h).get(_s) or "").strip() for h in _all_rows
                                    if ((h.plan_type or "").strip() or "—") == _k and (_st2(h).get(_s) or "").strip()}
                            if vals:
                                plan_status[_k][_s] = vals
            except Exception:               # noqa: BLE001
                pass
            # EACH CHOSEN RENEWAL YEAR ON ITS OWN, oldest first: its plans,
            # their totals and statuses from that year's own records -- for the
            # Bank Feed to show a line per year.
            _by_season = []
            try:
                if want and len(want) > 1 and locals().get("_all_rows"):
                    def _st3(h):
                        try:
                            return json.loads(h.statuses_json or "{}") or {}
                        except Exception:   # noqa: BLE001
                            return {}
                    for _s in sorted(want):
                        _rs = [h for h in _all_rows if (_st3(h).get(_s) or "").strip()]
                        _pl: dict = {}
                        for h in _rs:
                            k = (h.plan_type or "").strip() or "—"
                            d = _pl.setdefault(k, {"total": None, "status": set()})
                            if h.total is not None:
                                d["total"] = (d["total"] or 0) + h.total
                            d["status"].add((_st3(h).get(_s) or "").strip())
                        _by_season.append({"season": _s, "rows": len(_rs), "plans": [
                            {"plan_type": k, "total": str(d["total"]) if d["total"] is not None else "",
                             "status": ", ".join(sorted(d["status"])) or "\u2014"}
                            for k, d in sorted(_pl.items())] or [
                            {"plan_type": "", "total": "", "status": "\u2014"}]})
            except Exception:               # noqa: BLE001
                _by_season = []
            try:
                _ptypes = [k for k in (plans or {}) if k and k != "—"]

                # A DEPOSIT WHOSE TOTAL IS THIS CHARGE.
                #
                # "Every record is a deposit" was too narrow. A holder commonly
                # has a season plan AND a deposit, and the deposit is paid as a
                # single charge for its exact amount -- so the charge that
                # matches a deposit total is that deposit, whatever else the
                # holder owns. The amount is what identifies it; without that
                # test a mixed holder could never be coded DEP at all.
                _amt_dep = any(
                    v is not None and row.amount is not None
                    and abs(_Dec(str(v)) - _Dec(str(row.amount))) < _Dec("0.01")
                    for k, v in (plans or {}).items() if "deposit" in k.lower())

                # A WRITTEN RULE is left alone; a TEAM ALIAS is not.
                #
                # Both set category_from_rule, so refusing to override it
                # blocked every alias-coded charge -- which is nearly all of
                # them -- and the deposit rule could never fire. A written rule
                # is somebody's instruction and outranks this; the alias is the
                # engine's own default for the team, and "this exact amount is
                # the deposit" is better information about the same charge.
                _from_written_rule = str(it.get("rule_name") or "").startswith("rule:")
                # ONLY on an exact amount match.
                #
                # "every record is a deposit" was still too loose: a holder
                # whose single record is a $500 deposit can be charged $80 for
                # something else entirely, and coding that to (DEP) because of
                # what else the holder owns is a guess about this charge from
                # evidence about a different one. The amount matching a deposit
                # total is the only thing that identifies THIS charge as that
                # deposit; without it, (TC) is the honest answer.
                if (_amt_dep
                        and not _from_written_rule
                        and not row.coded_category):
                    # _team_account, not _known_account: the account is named
                    # "Prepaid Inventory:NHL:San Jose Sharks (DEP)" and the
                    # prefix carries the league. Looking up the bare
                    # "San Jose Sharks (DEP)" matched nothing, so the branch
                    # ran correctly and then silently found no account.
                    _dep = _team_account(db, _bare_team(rec.team), "DEP",
                                        it.get("_sport", ""))
                    if _dep:
                        # RECORDED, not applied here. The team-alias coding
                        # block runs after this one and sets (TC)
                        # unconditionally -- so writing the category here was
                        # overwritten a few hundred lines later, and the only
                        # visible trace was the rule name this block had also
                        # set. The flag is read there instead.
                        it["_dep_account"] = _dep
                        it["rule_name"] = (
                            f"season account: {rec.team} — "
                            + "deposit amount")
                    else:
                        # The (DEP) account does not exist in QuickBooks. Said
                        # out loud rather than silently leaving TC: the coding
                        # is wrong and the reason is one account away.
                        log.info("no (DEP) account for %s — left as coded",
                                 rec.team)
            except Exception:                   # noqa: BLE001
                log.exception("could not apply the deposit category")

            # CANADIAN PLANS ARE HELD IN CAD; THE CHARGE POSTED IN USD.
            #
            # Airtable records a Toronto or Montreal season total in Canadian
            # dollars, because that is what the team billed. By the time the
            # charge reaches a US card it has been converted. Dividing the CAD
            # total by the installment count and comparing it to a USD charge
            # compares two different currencies, and every Canadian plan came
            # out looking about 37% short -- consistently enough to look like a
            # real underpayment rather than an arithmetic error.
            #
            # Which teams are Canadian comes from the Canada rules table, the
            # same maintained list the Canadian-account routing already uses,
            # so there is no second copy to keep in step.
            _fx_note = ""
            _grand_cad, _cad_used = None, None     # for the Exp Amt working
            if grand is not None:
                try:
                    from .canada import looks_canadian as _looks_ca
                    if not _sth_usd and _looks_ca(rec.team or "", db=db, vendor=rec.team or ""):
                        # The rate people maintain on the Canada Tags
                        # screen, not the environment default -- it moves,
                        # and it must not need a deploy to move.
                        from .fx import rate_on as _rate_on
                        _r_day = _rate_on(_cad_hist, getattr(row, "txn_date", None), _cad_rate)
                        _rate = _Dec(str(_r_day))
                        if _rate > 0:
                            _grand_cad, _cad_used = grand, _r_day
                            grand = (grand * _rate).quantize(_Dec("0.01"))
                            _fx_note = f"CAD converted at {_r_day}"
                except Exception:           # noqa: BLE001
                    _fx_note = ""

            # WHICH PLAN THIS CHARGE IS ON. Settled here because the expected
            # installment is a share OF the season total, so there was nothing
            # to compare an amount against until `grand` was final -- including
            # any currency conversion just applied above.
            #
            # None means it matches neither plan. The row falls back to the
            # first for display, and its amount check simply will not tie --
            # the honest answer rather than a confident wrong one.
            if not row.is_credit and len(_plan_options) > 1:
                _chosen = _pick_plan(_plan_options, row.amount, grand,
                                     row.txn_date) or _plan

            if row.hal_matched is not True:
                row.hal_matched = True
                _hal_flag_changed.append(row)
            it["hal"] = {
                "lookup_name": lookup or rec.holder_name,
                # Shown as "team · holder" on the STH line, whatever the source.
                "holder_name": rec.holder_name or "",
                "team": rec.team, "league": league,
                "plan_type": rec.plan_type or "",
                # The matched row's own total, kept so nothing that read this
                # before starts reading a different number.
                "total": str(hal_total) if hal_total is not None else "",
                # The account as a whole: every seat row added up.
                # Per plan: its total AND its status for each chosen season.
                # Several rows of one plan type can disagree, in which case all
                # the values are listed rather than one being picked.
                "plans": [{
                    "plan_type": k, "total": str(v),
                    "statuses": [
                        {"season": _s,
                         "status": ", ".join(sorted(
                             plan_status.get(k, {}).get(_s, {"—"})))}
                        for _s in want],
                } for k, v in sorted(plans.items())],
                "account_total": str(grand) if grand is not None else "",
                "by_season": _by_season,
                "charge_season": locals().get("_pick") or "",
                "seat_rows": seat_rows,
                # When the team charges, and what one installment should be.
                # The expected amount is the account total over the number of
                # months the plan runs -- both come from the Teams / Payment
                # Plans view, so nothing is inferred from the charges.
                # NONE OF THE PLAN COMPARISON APPLIES TO A REFUND.
                #
                # A credit reverses a charge; it is not an installment. Asking
                # whether it landed on the plan's billing day, or whether it
                # matches one twelfth of the season, compares it against
                # something it was never meant to equal -- so a $273 refund
                # against a $3,270 plan reported a -90% variance and a failed
                # date check, in red, on a row where nothing is wrong. Three
                # false alarms per refund, on every refund.
                #
                # The season account, its total and its statuses still show:
                # those say which account the money came back from, which is
                # the useful part.
                # "??" WHEN THE PLAN DOES NOT SAY, rather than nothing at all.
                #
                # A team with no billing day rendered an empty string, and an
                # empty string hides the whole pill -- so a plan whose day is
                # simply unrecorded looked identical to a charge with no plan
                # behind it. Those are different problems: one is missing data
                # in Airtable, the other is a charge nobody expects to tie out.
                # The marker is amber because it is neither a pass nor a fail.
                "day_of_month": ("" if row.is_credit else
                                 (_days_label(_plan) or _plan.get("day_text") or "??")),
                # Whether the charge ties out to the plan. Two separate
                # questions, because they fail for different reasons and the
                # fix is different: the wrong DAY usually means a weekend or a
                # slow processor, the wrong AMOUNT means the plan changed.
                "date_ok": (None if row.is_credit else
                            _tie_date(row.txn_date, _plan.get("day"),
                                      _plan.get("day_text"))),
                # The share for the MONTH THIS CHARGE LANDED IN, not an
                # average: that is the whole point of recording twelve figures
                # rather than a count.
                # The plan is settled here, where `grand` finally exists: the
                # expected installment is a share OF the season total, so there
                # is nothing to compare amounts against until it is known.
                "amount_ok": (None if row.is_credit else
                              _tie_amount(row.amount, grand,
                                          _chosen.get("months"),
                                          _plan_share(_chosen, row.txn_date))),
                # Signed, so under and over read differently at a glance.
                "variance_pct": (None if row.is_credit else
                                 _variance_pct(row.amount, grand,
                                               _chosen.get("months"),
                                               _plan_share(_chosen, row.txn_date))),
                # The rate applied, or "" when none was. Shown beside the
                # figure so a converted number is never mistaken for a raw one.
                "fx_note": "" if row.is_credit else _fx_note,
                # Same for the installment: "??" when the plan has no month
                # count, so an unrecorded plan length reads as unrecorded
                # rather than as nothing to check.
                "expected_unknown": (not row.is_credit
                                     and grand is not None
                                     and not _plan.get("months")),
                # THE SAME EXPECTATION THE AMOUNT CHECK USES: the plan's share
                # for the charge's month of the total, or an even split. It
                # showed the even split while the check used the share, so
                # the two could disagree. `expected_calc` is the working,
                # shown on hover: "$3,270.00 x 10% = $327.00".
                **_expected_fields(row, grand, _chosen if _chosen else _plan, _grand_cad, _cad_used),
                "statuses": statuses,
                "seasons_chosen": bool(want),
            }

    # --- team aliases drive the vendor AND its ticket-credit account ---------
    #
    # The alias table maps a bank detail to a team; that team is the vendor, and
    # the vendor implies the account. Both come from the same maintained fact,
    # resolved from the BANK DETAIL -- never from a STH record.
    #
    # STH used to supply the vendor, and its team names carry a venue on some
    # rows: "New York Islanders (UBS Arena)". That string is a label, not a
    # QuickBooks vendor, so those rows were blanked while rows whose STH record
    # happened to omit the venue filled in -- the same team disagreeing with
    # itself depending on which season account the charge belonged to.
    #
    # Only fills fields that are EMPTY: anything coded, cleared, or set by a
    # written rule is somebody's decision and stands.
    # The TEAM NAME only -- not the venue and note aliases beside it. Those
    # exist for the matching algorithm, where a near miss costs a suggestion;
    # here they would silently code a charge, and "AT&T STADIUM TICKET OFF" is
    # not reliably the Cowboys the way "Dallas Cowboys" is. Exceptions belong in
    # a written rule, where somebody has stated the intent.
    _team_names = {_bare_team(t).lower() for t in _alias_team.values()}
    if _alias_team:
        for it, row in zip(items, page):
            team = _team_from_detail(row.merchant or row.raw_description or "")
            _likely = ""
            if not team:
                # No single team named. Before giving up, see whether the
                # detail names a SHARED billing entity that can be narrowed.
                team, _likely = _narrow(
                    row.merchant or row.raw_description or "", row)
                if team:
                    it["vendor_confidence"] = "likely"
                    it["likely_reason"] = _likely
            if not team:
                # No alias matches this bank detail, so nothing here is a team
                # charge. A team vendor still sitting on the row came from the
                # STH era, when the vendor was taken from whichever season
                # account the CARDHOLDER held -- which is how a Mavericks
                # charge ("DALLAS BASKETBALL LIMIT") came to be coded FC Dallas.
                # Cleared unless somebody actually chose it.
                if not row.coded_vendor and not it.get("learned"):
                    v = (it.get("suggested_vendor") or "").strip()
                    if v and _bare_team(v).lower() in _team_names:
                        it["suggested_vendor"] = ""
                        it["vendor_from_rule"] = False
                if not row.coded_category and not it.get("learned"):
                    c = (it.get("suggested_category") or "")
                    if c.endswith("(TC)") or c.endswith("(DEP)"):
                        it["suggested_category"] = ""
                        it["category_from_rule"] = False
                continue
            bare = _bare_team(team)
            # The alias is the answer, so it REPLACES an existing suggestion
            # rather than deferring to it. A suggestion stored by an earlier
            # pass is not a decision -- and while STH was still supplying the
            # vendor, those stored values are exactly the wrong ones: a
            # "DALLAS BASKETBALL LIMIT" charge carrying "FC Dallas" because the
            # cardholder happened to hold an FC Dallas account. Deferring also
            # left the vendor unbadged while the category next to it was
            # badged, which read as the rule half-applying.
            if is_generic_team(team):
                # A PROMOTER, not a team. Whatever the cardholder's season
                # account says is the answer; the promoter name is not.
                alt = it.get("_hal_team") or ""
                if alt and not is_generic_team(alt):
                    team = alt
                    bare = _bare_team(alt)
                    it["rule_name"] = f"season account: {alt} (via promoter)"
                else:
                    # Nothing better to go on. Left BLANK rather than coded to
                    # whatever matched first -- an empty field asks a question,
                    # a wrong one answers it incorrectly.
                    it["rule_name"] = (it.get("rule_name")
                                       or "promoter — no season account found")
                    team = ""

            if not row.coded_vendor and not row.vendor_cleared:
                vend = _team_vendor(db, team, it.get("_sport", ""))
                if vend:
                    it["suggested_vendor"] = vend
                    it["vendor_from_rule"] = True
                    it["rule_name"] = (it.get("rule_name")
                                       or f"team alias: {bare}")
            if team and not row.coded_category and not row.category_cleared:
                # (TC) by default: a charge to a team is a ticket credit. The
                # (DEP) variant used to come from a STH record's plan type,
                # which is what made two charges to one team code differently.
                #
                # It comes back only where it cannot disagree with itself: the
                # STH block above sets _dep_account when EVERY record for this
                # holder and team is a deposit, or when this charge's amount IS
                # a deposit total. Both are facts about this charge rather than
                # a property of whichever record happened to match first.
                acct = (it.get("_dep_account")
                        or _team_account(db, team, "TC", it.get("_sport", "")))
                if acct:
                    it["suggested_category"] = acct
                    it["category_from_rule"] = True
                    it["rule_name"] = (it.get("rule_name")
                                       or f"team alias: {bare}")

            # A team trusted to add its own charges files them here, the same
            # way a card payment does -- coded, moved, and recorded. Refunds
            # only when that has been ticked too, and never a charge somebody
            # has already touched.
            # AUTOMATICALLY CATEGORIZE STH CHARGES (Configuration > Settings),
            # replacing the per-team auto-add. Only when EVERY condition holds,
            # each read from the same figures the screen shows:
            #   1. the corresponding STH record is found;
            #   2. the status for the team's designated renewal year(s) is
            #      Active;
            #   3. the charge date matches the team's expected day of the month;
            #   4. the amount matches the expected charge for that account.
            # Never a refund (the plan checks do not apply to one), never a
            # charge somebody has touched.
            _h = it.get("hal") or {}
            _st = [x.get("status", "") for x in (_h.get("statuses") or [])]
            if (_auto_sth and not no_autofile and status == "for_review" and not row.is_credit
                    and _h
                    and _st and all(str(x).strip().lower() == "active" for x in _st)
                    and _h.get("date_ok") is True and _h.get("amount_ok") is True
                    and row.charge_id not in _autofiled_before
                    and not row.coded_vendor and not row.coded_category
                    and not row.vendor_cleared and not row.category_cleared
                    and it.get("suggested_vendor")
                    and it.get("suggested_category")
                    # A BILL WINS. A charge that also matches a bill at Strong
                    # or better is paying that bill: filing it as an STH
                    # expense too would count it twice. It waits for the bill
                    # match (or a person) instead.
                    and not __import__("app.bands", fromlist=["x"]).strong_or_better(it.get("tier") or "")):
                from datetime import datetime as _dt3
                from .models_db import AuditRow as _AR4
                row.coded_vendor = it["suggested_vendor"]
                row.coded_category = it["suggested_category"]
                row.status, row.resolution = "categorized", "expense"
                row.resolved_at = _dt3.utcnow()
                row.resolved_by = "system-auto-sth"
                db.add(_AR4(company=row.company, action="sth_autofiled",
                            charge_id=row.charge_id,
                            detail=f"STH charge categorized automatically for {bare}: "
                                   f"record found, Active, on its day, expected amount",
                            dry_run=False, actor="system"))
                _autofiled.append(row)
                _to_post.append(row.charge_id)

    # --- card payments prefill from the card, not from a guess --------------
    #
    # A payment onto the card is money moving between two accounts you own. The
    # vendor is the card program it paid off, and the category is the bank it
    # came from -- both known, neither derivable from the bank detail, which
    # usually reads "PAYMENT THANK YOU". Left blank, every payment in the queue
    # had to be coded by hand into the same two values.
    # A card payment always shows its badge and always prefills: the vendor is
    # the card program it settled and the category is the primary checking
    # account. Both are facts about the payment, not guesses, so there is
    # nothing to opt out of. The toggle on Payment Tags decides only whether
    # these rows FILE THEMSELVES into Categorized or wait to be reviewed.
    if True:
        # The pay-from account comes from the Card Accounts screen, where the
        # primary checking account is already flagged. There is no second place
        # to set it.
        _bank = ""
        try:
            from .models_db import SourceAccountRow as _SA5, QboRefRow as _QR3
            _prim = db.query(_SA5).filter(
                _SA5.company == company, _SA5.is_primary.is_(True)).first()
            if _prim:
                _bank = (_prim.qbo_account_name or "").strip()
            if not _bank:
                # This company's own bank accounts -- the query had no file
                # filter, so it could offer another company's checking account.
                _realm_p = _company_realm(db, company)
                _banks = [r.name for r in db.query(_QR3).filter(
                    _QR3.kind == "account", _QR3.usable_for == "bank",
                    _QR3.realm_id == _realm_p).all()
                    if r.name and "credit" not in (r.account_type or "").lower()] \
                    if _realm_p else []
                _pref = [b for b in _banks if "checking" in b.lower()]
                _bank = (sorted(_pref) or sorted(_banks) or [""])[0]
        except Exception:                   # noqa: BLE001
            _bank = ""
        for it, row in zip(items, page):
            if not row.is_card_payment:
                continue
            # OVER ANY RULE'S LEFTOVERS. A payment's vendor is the program and
            # its category the account it was paid from; a rule's answer stored
            # on it before rules stopped matching payments is replaced, unless
            # a person coded it themselves.
            if not row.coded_vendor and not row.vendor_cleared:
                # The PROGRAM, not the card account: "Divvy", not "Divvy
                # (Credit)" or "Divvy Prefund". Those are two accounts under one
                # vendor, and the ledger has the vendor.
                _prog = _vendor_for_program(db, nick.get(row.source)
                                            or row.source or "")
                # A RULE'S VENDOR when the program is not found by name -- a
                # "Cap One" card whose vendor is "Capital One". The vendor only:
                # the category below always comes from the payment settings.
                _rname = "card payment"
                if not _prog:
                    from .learning import rule_vendor_for_payment as _rvp
                    _prog = _rvp(db, row)
                    _rname = "card payment (vendor from a rule)"
                if _prog:
                    it["suggested_vendor"] = _prog
                    it["vendor_from_rule"] = True
                    it["rule_name"] = it.get("rule_name") or _rname
            if _bank and not row.coded_category and not row.category_cleared:
                it["suggested_category"] = _bank
                it["category_from_rule"] = True
                it["rule_name"] = it.get("rule_name") or "card payment"
            # Filed here as well as in the rescore, so ticking the box takes
            # effect on the next render instead of waiting for whatever else
            # happens to trigger a rescore. Untouched rows only.
            # Never a SECOND time. A charge sent back from Categorized has been
            # resolved once already, and re-filing it on the next render meant
            # deleting a payment simply bounced it straight back.
            if (not no_autofile and pay_autocode_for(db, company) and status == "for_review"
                    and row.charge_id not in _autofiled_before
                    and not row.coded_vendor and not row.coded_category
                    and not row.vendor_cleared and not row.category_cleared
                    and it.get("suggested_vendor") and it.get("suggested_category")):
                from datetime import datetime as _dt2
                row.coded_vendor = it["suggested_vendor"]
                row.coded_category = it["suggested_category"]
                row.status, row.resolution = "categorized", "expense"
                row.resolved_at = _dt2.utcnow()
                row.resolved_by = "auto: card payment"
                from .models_db import AuditRow as _AR3
                db.add(_AR3(company=row.company, action="payment_autofiled",
                            charge_id=row.charge_id,
                            detail="categorized automatically as a card payment",
                            dry_run=False, actor="system"))
                _autofiled.append(row)

    if _hal_flag_changed:
        db.commit()
    if _autofiled:
        db.commit()
        # Posted straight away, as a person's categorizing is.
        if _to_post:
            start_auto_post(company, list(_to_post), "system-auto-sth")
        # They have left this tab, so they must leave this page too -- otherwise
        # the grid shows rows that are no longer in it until the next reload.
        _gone = {r.charge_id for r in _autofiled}
        items = [i for i in items if i["id"] not in _gone]
        total = max(0, total - len(_gone))

    # --- charges competing for the same bill --------------------------------
    #
    # Scoring is per charge: each row ranks the open bills without knowing what
    # any other row wants. Two charges can therefore both be shown a strong
    # match to ONE bill, and whoever clicks first takes it -- the second
    # reviewer sees the bill vanish, or matches it first and leaves the better
    # pairing impossible.
    #
    # Nothing is reordered here. The engine has no basis for deciding which
    # charge should win, and quietly demoting one would hide the conflict
    # rather than resolve it. It is named instead, on both rows, with the score
    # the rival gets -- so the person choosing can see the whole picture the
    # engine can.
    by_bill: dict = {}
    for it in items:
        top = (it.get("candidates") or [None])[0]
        if top:
            by_bill.setdefault(top["bill_id"], []).append({
                "charge_id": it["id"], "score": top["score"],
                # Enough to FIND the other transaction: what it says, whose
                # card, which day, how much. A merchant name alone sent people
                # hunting through a page of identical descriptions.
                "merchant": it.get("merchant") or it["id"],
                "date": it.get("date", ""), "amount": it.get("amt", "0"),
                "card": it.get("account", ""),
                "last4": it.get("card", ""), "holder": it.get("holder", ""),
            })
    for it in items:
        it["contested_by"] = None
        top = (it.get("candidates") or [None])[0]
        if not top:
            continue
        rivals = [r for r in by_bill.get(top["bill_id"], [])
                  if r["charge_id"] != it["id"]]
        if not rivals:
            continue
        best = max(rivals, key=lambda r: r["score"])
        it["contested_by"] = {
            **best, "bill": top["bill"], "others": len(rivals),
            # True when the rival scores HIGHER on this bill, which is the case
            # worth interrupting for: taking it here is probably the wrong call.
            "stronger": best["score"] > top["score"],
        }

    # THE SUGGESTION, SELF-HEALED like the tier above.
    #
    # The RULE badge is computed during this render; the Rules Applied filter
    # runs in SQL and reads the stored suggested_* columns, which only the
    # hourly rescore was writing. So a charge could show the badge and be
    # absent from "Rules Applied: Yes" -- the filter and the badge arguing
    # about the same fact, which the comment on the tier heal above already
    # names as the thing to avoid.
    #
    # `by_row` is built here rather than earlier because only the finished
    # items know what was actually suggested, after STH and the coding block
    # have had their say.
    # FROM HISTORY, last of all: rows nothing else suggested for, filled from
    # how people have coded the same merchant before. Suggestions only.
    if status == "for_review":
        try:
            from .history import suggest as _hist_suggest
            _hist_suggest(db, company, items, page)
        except Exception:                           # noqa: BLE001
            log.exception("history suggestions failed for %s", company)
    # PROFILE FROM A CREDIT CARD PROFILES RULE: the row whose rule gives the
    # profile it holds is marked, for the blue RULE badge beside the name.
    try:
        from . import profiles as _pfm
        _prules = _pfm.rules(db, company)
        if _prules:
            _finder = _pfm._TeamFinder(db, company)
            _pby = {r.charge_id: r for r in page}
            for it in items:
                _c = _pby.get(it.get("id"))
                if _c is None or not (_c.cardholder_name or ""):
                    continue
                for _n, _r in enumerate(_prules):
                    if _pfm.profile_for(_c, [_r], _finder) == _c.cardholder_name:
                        it["profile_rule"] = (f"Credit Card Profiles rule {_n + 1}: {_r['source']}"
                                              + (f", bank detail contains {_r['value']}" if _r.get("op") == "contains" else "")
                                              + ("" if _r.get("teams") == ["Any"] else f", teams {', '.join(_r['teams'])}"))
                        break
                    if _pfm.profile_for(_c, [_r], _finder):
                        break                       # an earlier row decides, and gives another name
    except Exception:                               # noqa: BLE001
        log.exception("profile rule marks failed for %s", company)
    _sugg = 0
    # `page` is the list of ChargeRow objects this render loaded -- there is no
    # `charge_rows`, and guarding on a name that does not exist would have
    # skipped every row while appearing to work.
    _by_id = {r.charge_id: r for r in page}
    for _it in items:
        _r = _by_id.get(_it.get("id"))
        if _r is None:
            continue
        _sv, _sc = _it.get("suggested_vendor") or "", _it.get("suggested_category") or ""
        # EXACTLY what the badge shows, stored beside the suggestion.
        # THE BADGE'S OWN SUPPRESSIONS TOO, not just its inputs.
        #
        # ruleBadge returns nothing for a MATCHED charge -- it took its vendor
        # and category from the bill it settled, so a rule badge would claim
        # credit for something the rule did not do. The flag was storing the
        # raw inputs, so bill payments were stored as rule-applied and showed
        # under "Yes" with no badge anywhere on the row.
        _ra = bool((_it.get("vendor_from_rule") or _it.get("category_from_rule"))
                   and _it.get("resolution") != "matched")
        if ((_r.suggested_vendor or "") != _sv
                or (_r.suggested_category or "") != _sc
                or bool(_r.rule_applied) != _ra or _r.rule_applied is None):
            _r.suggested_vendor, _r.suggested_category = _sv, _sc
            _r.rule_applied = _ra
            db.add(_r)
            _sugg += 1
    if _sugg:
        db.commit()

    if _flagged:
        db.commit()
    if _drifted:
        db.commit()      # stored strength corrected while rendering the page

    return {"company": company, "status": status,
            "hal_connected": _hal_banner_state(db, company, idx),
            "bills_loaded": len(bills), "total": total,
            "offset": offset, "limit": limit,
            "sort": sort, "direction": direction,
            # The deposit marker is internal plumbing between two blocks in
            # this function, not something the client should see or come to
            # depend on.
            "items": [{k: v for k, v in i.items()
                       if k not in ("_dep_account", "_sport", "_hal_team")}
                      for i in items]}


def queue_counts(db: Session, company: str, card: str | None = None,
                 filters: dict | None = None) -> dict:
    """Counts per state, and per card, under the SAME filters as the grid.

    Two different questions, so two different uses of `card`:
      * the tab counts respect the card selection, because that's what the
        grid is showing;
      * the per-card counts deliberately ignore it, or an unticked card would
        report zero and you could never tell what ticking it would give you.
    """
    from sqlalchemy import func
    f = dict(filters or {})
    status = f.get("status") or "for_review"

    base = apply_charge_filters(
        db.query(ChargeRow.status, func.count(ChargeRow.charge_id)).filter(
            ChargeRow.company == company),
        {**f, "status": None, "card": card})
    counts = dict(base.group_by(ChargeRow.status).all())

    from .models_db import SourceAccountRow
    # qbo_account_name FIRST, then the nickname.
    #
    # A rename in QuickBooks writes qbo_account_name and leaves the nickname
    # as it was, so reading the nickname alone showed the OLD name -- the grid
    # said "Slash Prefund" while the card picker beside it still said
    # "SlashPlat". The other nick map in this file already had the right
    # precedence; this one did not, which is the same two-copies problem as
    # the holdings lookup.
    nick = {s.source: (s.nickname or s.qbo_account_name or s.source)
            for s in db.query(SourceAccountRow)
            .filter(SourceAccountRow.company == company).all()}
    # A source like "qbo:874" is a QuickBooks account nothing has mapped yet --
    # a program can import into one before anybody names it. Named from
    # QuickBooks so the picker shows "Divvy (Prefund)" rather than a raw id.
    #
    # There are TWO nick maps in this file, one per function, and the first fix
    # went into the other one: the picker reads this one.
    try:
        from .models_db import QboRefRow as _QR
        # BANK AND CREDIT CARD ACCOUNTS ONLY.
        #
        # The picker pads its list from this map -- any key named here becomes
        # a selectable card account -- so dropping the usable_for filter put
        # the entire chart of accounts into the card picker. Naming and
        # eligibility are the same decision here, which is easy to miss because
        # the code reads as if it were only naming.
        # SKIP any QuickBooks account a SourceAccountRow already maps.
        #
        # Naming every bank account as "qbo:874" listed the SAME account twice
        # whenever it also had a mapped source key -- once under its own key,
        # carrying the API and VIRTUAL badges, and once under the raw id
        # without them. Two entries for one card account, one of them looking
        # like a different account with no badges.
        _mapped_ids = {r.qbo_account_id for r in db.query(SourceAccountRow)
                       .filter(SourceAccountRow.company == company).all()
                       if r.qbo_account_id}
        # THIS COMPANY'S QuickBooks file only. Without the realm filter every
        # connected file's bank accounts were injected into every company's
        # card list as `qbo:<id>` sources -- which is how a company with no
        # cards and no transactions still looked populated.
        for _r in db.query(_QR).filter(
                _QR.kind == "account", _QR.usable_for == "bank",
                _eq_or_none(_QR.realm_id, _company_realm(db, company))).all():
            if _r.qbo_id in _mapped_ids:
                continue
            nick.setdefault(f"qbo:{_r.qbo_id}", _r.name or f"qbo:{_r.qbo_id}")
    except Exception:                       # noqa: BLE001
        pass

    per_card = dict(apply_charge_filters(
        db.query(ChargeRow.source, func.count(ChargeRow.charge_id)).filter(
            ChargeRow.company == company),
        {**f, "status": status, "card": None}).group_by(ChargeRow.source).all())

    # Every card account the company has, not just the ones with matching rows.
    # GROUP BY drops a source with no matches, so under a Refunds filter a card
    # with no refunds vanished from the picker -- the list got shorter than the
    # selection, "all selected" stopped being true, and the button flipped from
    # "All Cards" to "3 card accounts" while nothing had been deselected.
    all_sources = [r for (r,) in db.query(ChargeRow.source).filter(
        ChargeRow.company == company).distinct().all() if r]
    # Every source with ANY charge behind it, before the picker's own list is
    # padded out with named-but-empty accounts. Used below to decide whether a
    # retired account still has to be reachable.
    sources_with_charges = set(all_sources)
    for src in nick:
        if src not in all_sources:
            all_sources.append(src)

    from .models_db import SourceAccountRow as _SA
    virtual = virtual_sources(db, company)
    # Retired accounts stay in the filter -- their charges are still in the
    # ledger and have to be findable -- but they say so, so nobody wonders why
    # a card that no longer exists is on the list.
    try:
        retired = {r.source for r in db.query(_SA).filter(
            _SA.company == company, _SA.is_active.is_(False)).all()}
    except Exception:                       # noqa: BLE001
        retired = set()                     # column not there yet
    # Counted against the tab being viewed, not against For Review -- the query
    # above already filters on the current status. The old name said otherwise
    # and the number was quietly right all along, so the picker showed a
    # correct count under a label that made it look wrong.
    # Which card accounts are fed by a live bank connection rather than by
    # uploads. Surfaced here so the picker can badge them without a second
    # request -- and so a BROKEN connection is visible at the moment somebody
    # is looking at the queue and wondering why a card is quiet.
    try:
        from .models_db import PlaidItemRow as _PI
        linked = {r.source: r.status for r in db.query(_PI).filter(
            _PI.company == company, _PI.active.is_(True)).all()}
    except Exception:                       # noqa: BLE001
        linked = {}                         # table not there yet

    # Which card account this company's Slash pull lands in. Derived from the
    # mapping rather than stored a second time, so the badge cannot disagree
    # with where the charges actually go.
    # Same source of truth as the Bank / Card Accounts grid, so an account
    # that is fed shows as fed on both screens.
    _api_srcs, _api_age = api_fed_sources(db, company)
    _api_src = ""

    # Accounts RETIRED IN QUICKBOOKS, and whether anything is filed under them.
    #
    # This picker is a filter, not a chooser: hiding an account outright makes
    # its charges unreachable, and a retired card whose charges are still in
    # the ledger has to stay findable. So the rule is by consequence rather
    # than by status --
    #
    #   retired AND holds charges  -> stays, marked, so it can be filtered to
    #   retired AND holds nothing  -> dropped; it is a dead name on a list
    #                                 people scan every day
    #
    # An account still In Use in the engine is untouched by any of this; that
    # tick and QuickBooks' Active flag are different questions.
    qbo_retired = set()
    try:
        from .models_db import QboRefRow as _QR
        # THIS COMPANY'S FILE ONLY. QuickBooks numbers accounts per file, so
        # an id retired in another company's file -- an old inventory account
        # numbered 874 -- marked THIS company's live card account 874 retired,
        # and a card with no charges yet was then dropped from the picker
        # entirely. With no file mapped, nothing is treated as retired.
        _realm_r = _company_realm(db, company)
        _dead = ({r.qbo_id for r in db.query(_QR).filter(
            _QR.kind == "account", _QR.active.is_(False),
            _QR.realm_id == _realm_r).all()} if _realm_r else set())
        if _dead:
            qbo_retired = {r.source for r in db.query(_SA).filter(
                _SA.company == company).all()
                if r.qbo_account_id and r.qbo_account_id in _dead}
            qbo_retired |= {f"qbo:{i}" for i in _dead}
    except Exception:                       # noqa: BLE001
        log.exception("could not read retired QuickBooks accounts")
        qbo_retired = set()

    # ANY charge, not just one on the tab being viewed. per_card counts within
    # the current status, so keying on it would drop a retired card from the
    # For Review picker while its charges sat in Categorized -- unfilterable
    # from the one screen where somebody would go looking for them.
    all_sources = [s for s in all_sources
                   if s not in qbo_retired or s in sources_with_charges]

    # VIRTUAL BY THE NAME THE PICKER SHOWS, for every card on it -- not only
    # the ones with a card-account record. A card program routed straight to a
    # QuickBooks account ("qbo:874", named "Divvy (WH)") has no such record,
    # so the rule never looked at it: no card counted as virtual, and "All
    # Virtual" did not appear even with several virtual cards on the list.
    try:
        from .models_db import AppSettingRow as _ASv
        from engine.suggest import DEFAULT_VIRTUAL_PROGRAMS as _DVP
        _vrow = db.get(_ASv, "virtual_programs")
        _words = (list(_DVP) if _vrow is None else
                  [w.strip().lower() for w in (_vrow.value or "").split(",")
                   if w.strip()])
        virtual |= {src for src in all_sources
                    if any(w in f"{nick.get(src, src)} {src}".lower()
                           for w in _words)}
    except Exception:                       # noqa: BLE001
        log.exception("could not apply the virtual card list by name")

    per_source = [{"source": src, "name": nick.get(src, src),
                   "qbo_retired": src in qbo_retired,
                   "api_fed": src in _api_age,
                   "api_stale_hours": _api_age.get(src),
                   "virtual": src in virtual,
                   "active": src not in retired,
                   "linked": src in linked,
                   "link_status": linked.get(src, ""),
                   "count": per_card.get(src, 0),
                   # Kept so a client that has not reloaded still reads a number.
                   "for_review": per_card.get(src, 0)}
                  for src in all_sources]
    # TYPE, THEN NAME -- the order the Bank / Card Accounts grid uses.
    #
    # It sorted by charge count, so the list reshuffled as charges arrived and
    # a card people look for daily moved position from one visit to the next.
    # A picker somebody uses by muscle memory has to be stable, and matching
    # the grid means one mental map for both screens.
    _types = {}
    try:
        from .models_db import QboRefRow as _QR2
        # THIS COMPANY'S FILE. QuickBooks ids are only unique within a realm,
        # so an unfiltered lookup lets one company's account id answer for
        # another's -- a card takes the wrong TYPE and sorts into the wrong
        # block, which is why this picker's order stopped matching the grid
        # once a second file was connected.
        _realm2 = _company_realm(db, company)
        _by_id = {r.qbo_id: (r.account_type or "") for r in db.query(_QR2)
                  .filter(_QR2.kind == "account",
                          _eq_or_none(_QR2.realm_id, _realm2)).all()}
        for _sa in db.query(SourceAccountRow).filter(
                SourceAccountRow.company == company).all():
            if _sa.qbo_account_id:
                _types[_sa.source] = _by_id.get(_sa.qbo_account_id, "")
        for _qid, _t in _by_id.items():
            _types.setdefault(f"qbo:{_qid}", _t)
    except Exception:                       # noqa: BLE001
        log.exception("could not read account types for the picker")
    per_source.sort(key=lambda s: ((_types.get(s["source"]) or "").lower(),
                                   (s["name"] or "").lower()))

    from .models_db import BillRow as _B
    # Open bills only -- the same set the Bills Available tab lists and the
    # matcher scores against.
    bills_n = db.query(_B).filter(_B.company == company, _B.balance > 0).count()

    return {
        "bills": bills_n,
        "for_review": counts.get("for_review", 0),
        "categorized": counts.get("categorized", 0),
        "excluded": counts.get("excluded", 0),
        "sources": per_source,
    }


def build_review(db: Session, company: str, limit: int = 200,
                 tier: str | None = None, source: str | None = None) -> dict:
    """Legacy two-bucket view, kept for the older endpoint."""
    idx = load_hal_index(db, company)
    set_hal_index(idx)          # None is fine: classifier falls back gracefully

    charges = [_to_charge(r, db) for r in db.scalars(
        select(ChargeRow).where(ChargeRow.company == company))]
    bills = [_to_bill(r) for r in db.scalars(
        select(BillRow).where(BillRow.company == company))]
    bill_by_id = {b.bill_id: b for b in bills}

    from .learning import suggestion_for
    payments, expenses = [], []
    tiers = Counter()

    for c in charges:
        if source and c.source != source:
            continue
        k = classify(c)

        if k.charge_class == ChargeClass.BILL_PAYMENT:
            sg = suggest(c, bills)[:MAX_SUGGESTIONS]
            top = sg[0].score if sg else 0
            t = tier_for(top, bool(sg))
            tiers[t] += 1
            if tier and t != tier:
                continue
            payments.append({
                "id": c.charge_id, "src": c.source, "amt": str(c.amount),
                "date": str(c.txn_date), "merchant": c.merchant,
                "card": c.card_last4 or "", "holder": c.cardholder_name or "",
                "tier": t,
                "candidates": [{
                    "bill": (bill_by_id[s.bill_id].doc_number
                         or bill_by_id[s.bill_id].bill_id),
                "bill_id": s.bill_id,
                    "amt": str(bill_by_id[s.bill_id].amount),
                    "date": str(bill_by_id[s.bill_id].txn_date),
                    "vendor": bill_by_id[s.bill_id].vendor or "",
                    "memo": (bill_by_id[s.bill_id].memo or "")[:90],
                    "score": round(s.score, 2),
                    "tier": tier_for(s.score, True),
                    "why": "; ".join(s.reasons),
                } for s in sg],
            })
        else:
            expenses.append({
                "id": c.charge_id, "src": c.source, "amt": str(c.amount),
                "date": str(c.txn_date), "merchant": c.merchant,
                "card": c.card_last4 or "", "holder": c.cardholder_name or "",
                "category": k.coding.get("category") or "—",
                "vendor": k.coding.get("vendor", ""),
                "season": k.coding.get("season", ""),
                "hal": k.coding.get("hal_status", ""),
                "rule": k.rule,
                "why": "; ".join(k.reasons),
            })

    from .bands import order as _band_order
    order = _band_order()
    payments.sort(key=lambda p: (order.get(p["tier"], 99), -float(p["amt"])))
    expenses.sort(key=lambda e: -float(e["amt"]))

    return {
        "company": company,
        "hal_connected": idx is not None,
        "stats": {
            "charges": len(charges), "bills": len(bills),
            "bill_payments": sum(tiers.values()), "expenses": len(expenses),
            "perfect": tiers.get("perfect", 0), "very_strong": tiers.get("very_strong", 0),
            "strong": tiers["strong"], "moderate": tiers["moderate"],
            "weak": tiers["weak"], "none": tiers["none"],
            "tiers": dict(tiers),
        },
        "bill_payments": payments[:limit],
        "expenses": expenses[:limit],
    }


def list_companies(db: Session) -> list[str]:
    rows = db.execute(select(ChargeRow.company).distinct()).scalars().all()
    return sorted(r for r in rows if r)
