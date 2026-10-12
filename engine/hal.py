"""
STH validation — resolves a charge's cardholder to a season-ticket record and
decides the expense category.

Data source (Airtable, base "Tickets" appOEQZDCBXBZRiUv):
    STH            tblHVnNLLKSnvjoKr   season-ticket records
      25/26 Status fldixaT5UFHyDaFkd   singleSelect
      26/27 Status fldFiTzd1eig0eg0B   singleSelect
      24/25 Status fldrme0zedWhb3mUE   singleSelect
      Full/Partial fldWKMM4HFjzr0i3t   plan type (multipleSelects)
      Year         fldEW82npcMjMRfqm   season starting year
      Team / Address Book / Default card / Payment Plans  (linked records)
    Cards          tblYVHB8AqUthH6ST   has Last 4 (formula), links to STH
    Payment Plans  tblkko9IoGyAs6S49   per-installment Date + Amount

The category is driven by the STH season status, per the accounting rule:

    TC   Team charge — a live season-ticket holder paying into their plan.
    DEP  One-time deposit to get on a waiting list.

Refunds follow the same categorization as the charge they reverse: a refund on
a waitlist deposit codes DEP, a refund on a plan payment codes TC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# --- category constants ------------------------------------------------------
TC = "TC"
DEP = "DEP"


# --- STH season status buckets ----------------------------------------------
# Exact option names read from the Airtable field config.

STATUS_ACTIVE = {"Active"}

# NOTE (corrected against real data): the TC/DEP split is NOT driven by status.
# It is driven by the Full/Partial field, which carries "Wait List" and
# "DEPOSIT" as options alongside the real plan types. Status answers "is this
# record live for the season"; Full/Partial answers "is this a plan or a
# waitlist deposit". The two are independent -- a record can be Active AND
# Wait List (e.g. San Diego Padres / willieoherty1).
DEP_PLAN_TYPES = {"wait list", "waitlist", "deposit", "partial deposit",
                  "flex deposit", "playoff deposit", "add-on deposit",
                  "ticket bank deposit", "partial plan deposit", "half deposit"}

# No live record for the season — a team charge here is an anomaly worth review.
STATUS_DEAD = {
    "Canceled", "Declined", "Not Renewed", "DO NOT RENEW", "Opted Out",
    "Emailed/Opted Out", "Forfeited", "To Be Refunded", "No Invoice",
    "Locked Out", "Disabled Gmail",
}

# In-progress admin states: validate, but surface the status so a reviewer can
# see the account wasn't settled.
STATUS_IN_PROGRESS = {
    "Pending", "Under Review", "Relocated", "Credit Card", "Form Sent",
    "Check Back", "Contact AM", "Phone Call", "Emailed", "Password",
}


def status_kind(status, words: dict | None = None) -> str:
    """What a status word MEANS: "active", "progress", "dead", or "" when it
    is not known.

    Brokers write status their own way -- Y&S "Active", Ticket Guy "Yes", others
    "Renewed" or "Current". The built-in lists are Y&S's; a company adds its
    own words on Broker Data > Status Words ({"active": [...], "progress":
    [...], "dead": [...]}), which this reads alongside them. Compared without
    regard to capitals or spaces at either end."""
    t = str(status or "").strip().lower()
    if not t:
        return ""
    w = words or {}
    # THE ENGINE'S OWN WORDS can be edited (Broker Data > Status Words, Engine
    # Words): when the settings carry them, they replace the built-in lists.
    _act = w["_engine_active"] if "_engine_active" in w else STATUS_ACTIVE
    _dead = w["_engine_dead"] if "_engine_dead" in w else STATUS_DEAD
    _prog = w["_engine_progress"] if "_engine_progress" in w else STATUS_IN_PROGRESS
    def has(builtin, key):
        return (t in {str(x).strip().lower() for x in builtin}
                or t in {str(x).strip().lower() for x in (w.get(key) or [])})
    if has(_act, "active"):
        return "active"
    if has(_prog, "progress"):
        return "progress"
    if has(_dead, "dead"):
        return "dead"
    return ""


def season_for(txn_date: date, start_month: int = 7) -> str:
    """Which season a charge belongs to.

    Seasons are labeled by starting year, e.g. '26/27'. Charges from the
    start month onward belong to the upcoming season -- a July 2026 charge is
    paying into 26/27. `start_month` is overridable per team from the Teams
    table's Start Month, so leagues that straddle differently can be driven
    from data rather than hardcoded.
    """
    y = txn_date.year if txn_date.month >= start_month else txn_date.year - 1
    return f"{str(y)[2:]}/{str(y + 1)[2:]}"


STATUS_FIELD_BY_SEASON = {
    "25/26": "25/26 Status",
    "26/27": "26/27 Status",
    "27/28": "27/28 Status",
}

# Seasons the mirror carries, newest last. A charge is checked against its own
# season first, then the others -- renewals mean a record can be live for the
# upcoming season while the current one is still blank, or vice versa.
# Newest first: a charge is checked against its own season, then these in turn.
# 25/26 is included because records that are live today often have only that
# status filled in, and treating them as "no record" is worse than validating
# against a slightly stale season and saying so.
TRACKED_SEASONS = ["26/27", "27/28", "25/26"]


def set_tracked_seasons(seasons: list[str]) -> None:
    """Point the validator at whatever the admin chose to mirror."""
    global TRACKED_SEASONS
    if seasons:
        TRACKED_SEASONS = list(seasons)


@dataclass
class HalRecord:
    """A season-ticket record, as loaded from Airtable."""
    record_id: str
    holder_name: str                      # from Address Book
    team: str
    statuses: dict                        # {"26/27": "Active", ...}
    card_last4: Optional[str] = None
    plan_type: str = ""                   # Full/Partial
    has_payment_plan: bool = False
    plan_installments: list = field(default_factory=list)   # [(date, amount)]
    # The Airtable "Lookup Name" -- "LA Clippers matthewjude86@outlook.com".
    # Carried because the buyer's address is inside it, and that address is the
    # other half of identifying whose account this is.
    lookup_name: str = ""
    # THE BUYER'S ADDRESS, stated rather than inferred.
    #
    # Only some brokers put the email inside the lookup name; most keep it in
    # a column of its own, under a name that differs per broker ("Email",
    # "Profiles", "Email (from Profiles)"). Where the mapping names that
    # column, its value lands here -- and identity stops depending on an
    # address happening to be embedded in a display string.
    email: str = ""

    def status_for(self, season: str) -> Optional[str]:
        return self.statuses.get(season)

    @property
    def emails(self) -> set:
        """Every address this record names.

        The explicit field first, then anything inside the lookup name. Both,
        not either: a record can carry one address in its own column and name a
        second in the lookup, and dropping either loses a match.
        """
        found = {e.lower() for e in _EMAIL_RE.findall(self.lookup_name or "")}
        found |= {e.lower() for e in _EMAIL_RE.findall(self.email or "")}
        return found


@dataclass
class HalValidation:
    matched: bool
    category: Optional[str]               # TC | DEP | None
    season: str
    status: Optional[str] = None
    holder: Optional[str] = None
    team: Optional[str] = None
    needs_review: bool = False
    reasons: list = field(default_factory=list)
    # WHICH record was matched. The validation could say a charge belonged to a
    # season account and name the holder and team, but not which of that
    # person's five records it was -- so nothing downstream could show the
    # lookup name, the plan or the total.
    record: object = None


# STH's Address Book values look like "Matthew Jude (CA) Los Angeles 90004".
# The person's name is everything before the state parenthetical -- and that
# name is what shows up as the cardholder on the credit-card charge.
_AB_NAME = re.compile(r"^(.*?)\s*\(")


def person_name(address_book_value: str) -> str:
    m = _AB_NAME.match(str(address_book_value or ""))
    return (m.group(1) if m else str(address_book_value or "")).strip()


# Card programs let people name their cards, and the name they choose is almost
# always their own with a word bolted on: "Jeffrey Rosenberg's Card",
# "Alan Blaustein's Card", "Eli Mann Card". The person is in there; the suffix
# is not part of it, and left in place it stopped every one of those charges
# from finding a STH record.
_CARD_SUFFIX = re.compile(
    r"[\s'’]*s?\s*(card|cards|virtual card|credit card|cc)\s*$", re.I)


def strip_card_suffix(name: str) -> str:
    """"Jeffrey Rosenberg's Card" -> "Jeffrey Rosenberg"."""
    out = _CARD_SUFFIX.sub("", str(name or "").strip())
    # Leave a bare "Card" alone rather than returning nothing at all.
    return out.strip(" '’") or str(name or "").strip()


def _norm_name(name: str) -> str:
    return " ".join(sorted(str(name or "").lower().replace(",", " ").split()))


def _category_for(rec: "HalRecord") -> str:
    """TC or DEP, decided by the Full/Partial plan type."""
    types = [t.strip().lower() for t in str(rec.plan_type or "").split(",")]
    return DEP if any(t in DEP_PLAN_TYPES for t in types) else TC


def _squash(name: str) -> str:
    """Letters only, lowercased -- so "Akeya Eleni" == "akeyaeleni"."""
    return re.sub(r"[^a-z]", "", str(name or "").lower())


_EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")


def _loose_name(name: str) -> str:
    """First and last name, letters only, order independent.

    The same key the address-book email mirror uses, for the same reason: one
    person is written several ways -- "Moshe A. Sochaczewsky", "Sochaczewsky,
    Moshe", a trailing "(CA) Los Angeles 90004" -- and an exact key misses every
    variant. The middle name is dropped because it is the part most likely to
    appear on one side and not the other.
    """
    n = str(name or "").lower().split("(")[0]
    parts = [p for p in re.split(r"[^a-z]+", n) if len(p) > 1]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " ".join(sorted((parts[0], parts[-1])))


class HalIndex:
    """In-memory mirror of the STH records, keyed for fast lookup.

    Production: refreshed on a schedule from Airtable so rules run against a
    local copy rather than hitting the API per charge.
    """

    def __init__(self, records: list[HalRecord], status_words: dict | None = None):
        # This company's own status words (see status_kind).
        self.status_words = status_words or {}
        self.by_name: dict[str, list[HalRecord]] = {}
        self.by_squash: dict[str, list[HalRecord]] = {}
        self.by_loose: dict[str, list[HalRecord]] = {}
        self.by_email: dict[str, list[HalRecord]] = {}
        for r in records:
            self.by_name.setdefault(_norm_name(r.holder_name), []).append(r)
            # Squashed key catches holders whose name came from an email local
            # part ("akeyaeleni"), which has no space to tokenize on.
            sq = _squash(r.holder_name)
            if sq:
                self.by_squash.setdefault(sq, []).append(r)
            lk = _loose_name(r.holder_name)
            if lk:
                self.by_loose.setdefault(lk, []).append(r)
            # The buyer address inside the record's Lookup Name. A cardholder
            # resolves to their addresses through the address-book mirror, so
            # this is what lets the two meet.
            for e in r.emails:
                self.by_email.setdefault(e, []).append(r)

    def lookup(self, cardholder_name: str, card_last4: Optional[str] = None,
               emails=None) -> list[HalRecord]:
        """Resolve a cardholder to their STH records.

        Identity is the NAME on the card and the addresses that name is known by
        -- the same evidence the bill matcher uses -- not the card number.

        The card's last four used to be tried first and won outright. On a
        virtual-card program a number is one of thousands and belongs to a
        purchase rather than a person, so it identifies nobody; worse, when it
        did hit it overrode the name and attached the charge to whoever else
        that number had been issued to. It is no longer consulted. The argument
        stays in the signature so existing callers keep working.
        """
        # A CARD NAMED BY AN EMAIL ADDRESS. Some brokers name their cards by the
        # holder's address rather than first and last name. That address is
        # then evidence in its own right, alongside any the address book knows,
        # and is matched to the records carrying it -- the record's Email
        # column, or the address book's addresses for its holder.
        if "@" in (cardholder_name or ""):
            _addr = cardholder_name.strip().lower()
            emails = [_addr] + [e for e in (emails or ()) if str(e).strip().lower() != _addr]
        hit = self.by_name.get(_norm_name(cardholder_name), [])
        if hit:
            return hit
        # The same name with the card wording removed. Tried before the looser
        # forms below, since it is an exact match once the suffix is gone.
        _bare = strip_card_suffix(cardholder_name)
        if _bare and _bare != cardholder_name:
            for _idx, _key in ((self.by_name, _norm_name(_bare)),
                               (self.by_squash, _squash(_bare)),
                               (self.by_loose, _loose_name(_bare))):
                hit = _idx.get(_key, [])
                if hit:
                    return hit
        # "Akeya Eleni" -> "akeyaeleni", matching an email-derived holder name
        hit = self.by_squash.get(_squash(cardholder_name), [])
        if hit:
            return hit
        # Written differently on the two sides -- a middle initial, a comma, a
        # trailing city and ZIP.
        hit = self.by_loose.get(_loose_name(cardholder_name), [])
        if hit:
            return hit
        # Nothing matched on the name. The cardholder's known addresses are the
        # remaining evidence: STH names the buyer's address on the record, and
        # the address book says which addresses belong to this person.
        #
        # Only when the address identifies ONE person, though. A buying address
        # can appear on records held by several different people, and attaching
        # a charge to whichever of them happens to hold the right team would be
        # filing it against a stranger's season account -- with a confident STH
        # badge on it. Where the address is ambiguous this abstains, and the
        # charge is treated as having no STH record, which is the honest answer.
        out, seen = [], set()
        for e in (emails or ()):
            for r in self.by_email.get(str(e).strip().lower(), []):
                if r.record_id not in seen:
                    seen.add(r.record_id)
                    out.append(r)
        holders = {_loose_name(r.holder_name) or r.holder_name for r in out}
        if len(holders) > 1:
            return []
        return out


# City names are shared across teams ("San Francisco Giants" vs "San Francisco
# 49ers"), so overlapping on those alone is not a match -- the distinguishing
# word is the nickname.
_TEAM_STOPWORDS = {
    "san", "new", "los", "las", "fort", "saint", "city", "north", "south",
    "east", "west", "football", "baseball", "basketball", "hockey", "club",
    "the", "and", "tickets", "ticket",
}


# Set from the Team Aliases table, so venues and bank-detail notes resolve to
# the team they name. None means the table has not been loaded.
_TEAM_RESOLVER = None


def set_team_resolver(fn) -> None:
    global _TEAM_RESOLVER
    _TEAM_RESOLVER = fn


def _bare(team: str) -> str:
    """Team name without a trailing venue, lowercased."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", team or "").strip().lower()


# City and league abbreviations, configured under Rules & Tags -> Abbreviations.
# "LA CLIPPERS LLC" and "Los Angeles Clippers" are the same team, and no amount
# of word comparison can know that -- "la" is not a shortening of "los angeles"
# by any rule, it is a convention somebody has to state.
_SYNONYMS: dict = {}
_EXPAND_CACHE: dict = {}


def set_synonyms(pairs) -> None:
    """Replace the abbreviation table. {"la": "los angeles", ...}, both ways."""
    global _SYNONYMS
    out: dict = {}
    for short, full in (pairs or {}).items():
        a = str(short or "").strip().lower()
        b = str(full or "").strip().lower()
        if a and b:
            out[a] = b
    _SYNONYMS = out
    _EXPAND_CACHE.clear()              # results depended on the old list
    try:                               # rules' expanded phrases did too
        from app.learning import _RULE_PREP
        _RULE_PREP.clear()
    except Exception:                  # noqa: BLE001
        pass


def current_synonyms() -> dict:
    return dict(_SYNONYMS)


def expand_text(text: str) -> str:
    """Rewrite abbreviations to their long form, whole words only.

    Whole words, so "la" does not fire inside "atlanta" or "dallas" -- which is
    exactly the kind of substring accident that put a Mavericks charge on an FC
    Dallas account.
    """
    # REMEMBERED: the same few thousand texts are expanded millions of times
    # (every rule against every charge), and each expansion runs one pattern
    # per abbreviation. The result depends only on the text and the list, and
    # the memory is cleared whenever the list changes (set_synonyms).
    key = str(text or "")
    hit = _EXPAND_CACHE.get(key)
    if hit is not None:
        return hit
    low = " " + re.sub(r"[^a-z0-9]+", " ", key.lower()).strip() + " "
    for short, full in _SYNONYMS.items():
        low = re.sub(rf"(?<= ){re.escape(short)}(?= )", full, low)
    out = low.strip()
    if len(_EXPAND_CACHE) > 200_000:
        _EXPAND_CACHE.clear()
    _EXPAND_CACHE[key] = out
    return out


def _team_score(charge_merchant: str, team: str) -> float:
    """How strongly a charge's merchant refers to a team. 0 means no match.

    Scored rather than boolean because one person holds records for several
    teams in the same city, and picking the first overlap gets the wrong one.
    """
    charge_merchant = expand_text(charge_merchant)
    team = expand_text(team)
    m, t = _squash(charge_merchant), _squash(team)
    if not m or not t:
        return 0.0
    if m == t:
        return 1.0
    # Containment, but only when the contained side is long enough for the
    # overlap to mean something. Squashing removes the spaces, so short strings
    # collide with letter runs inside longer ones: "AT&T" squashes to "att",
    # which is sitting inside "se-ATT-leseahawks" -- and that scored 0.9,
    # attaching an AT&T charge to a Seattle Seahawks season account. Six
    # characters is past the length where that happens by accident, and the word
    # comparison below still handles the genuinely short names.
    _short = m if len(m) <= len(t) else t
    if len(_short) >= 6 and (t in m or m in t):
        return 0.9

    def words(x):
        return {w for w in re.findall(r"[a-z0-9]+", str(x).lower())
                if len(w) > 2 and w not in _TEAM_STOPWORDS}

    mw, tw = words(charge_merchant), words(team)
    if not mw or not tw:
        return 0.0
    shared = mw & tw
    if not shared:
        return 0.0
    # ONE shared word is not a team match. "UNITED CENTER" and "Atlanta United
    # FC" share "united", which was enough to attach a Chicago arena charge to
    # an Atlanta season account -- and to do it inconsistently, because whether
    # it won depended on what else the cardholder held. Either the whole team
    # name is present, or at least two words are.
    #
    # "The whole team name is present" is only meaningful when the name is more
    # than one word. "FC Dallas" reduces to {"dallas"} -- "fc" is too short to
    # count -- so that exemption let it match anything containing "dallas",
    # including "DALLAS BASKETBALL LIMIT", which is the Mavericks. A one-word
    # team therefore has to be essentially the whole merchant, not a word buried
    # in it.
    if len(shared) < 2:
        if shared != tw:
            return 0.0
        if len(tw) == 1 and len(mw) > 1:
            return 0.0
    return 0.5 * (len(shared) / min(len(mw), len(tw)))


def validate(charge, hal: HalIndex, start_month: int = 7,
             team_hint: str = "") -> HalValidation:
    """Resolve a team charge to a STH record and decide TC vs DEP.

    A person typically holds records for SEVERAL teams -- five is common. So
    matching on the cardholder alone picks an arbitrary one, and a charge to
    San Jose Sharks can end up validated against a San Diego FC record that
    says "Opted Out". The team has to be part of the match.

    Which team a charge is for is decided from three things, in order:

      1. the alias table, when it recognizes the bank detail;
      2. `team_hint` -- the team already established for this row by other
         means, chiefly the vendor a Vendor & Category Rule set;
      3. comparing the bank detail with the team name directly.

    The hint exists because the alias table was effectively the only driver for
    anything the string comparison could not see. "SEATTLE HOCKEY" and "Seattle
    Kraken" share one word, and "hockey" is a stopword, so they score zero and
    the charge was ruled "not a team charge" -- no STH lookup at all -- even
    though a rule had already named the vendor as the Kraken. Aliases are for
    the matching algorithm; they should not be the only way STH can be reached.
    """
    season = season_for(charge.txn_date, start_month)
    # Identity is the name on the card plus every address that name is known by.
    # The addresses come from the address-book mirror via the charge, and are
    # what resolve a holder whose name is written differently in STH.
    _emails = {str(e).strip().lower()
               for e in (getattr(charge, "alt_emails", None) or ())}
    if getattr(charge, "email", None):
        _emails.add(str(charge.email).strip().lower())
    candidates = hal.lookup(charge.cardholder_name or "", charge.card_last4,
                            emails=_emails)

    # Narrow to the team the charge was actually paid to. Keep only the best
    # scoring team(s) -- not everything that shares a city name.
    #
    # If NOTHING matches, this isn't a team charge for this person: a theater
    # ticket on the card of someone who happens to hold Blue Jays seats must not
    # be validated against the Blue Jays record. Return no match and let it be
    # treated as an ordinary purchase.
    merchant = getattr(charge, "merchant", "") or ""

    # The curated mapping first. "UNITED CENTER" is a venue and "TRUE NORTH" is
    # a bank-detail note somebody wrote down; both name a team that no amount
    # of word comparison would find. When the alias table answers, it is the
    # answer -- a maintained fact beats a guess from overlapping strings.
    named = _TEAM_RESOLVER(merchant) if (_TEAM_RESOLVER and merchant) else ""
    if named and candidates:
        bare = _bare(named)
        exact = [r for r in candidates if _bare(r.team) == bare]
        if exact:
            candidates = exact
        else:
            # The alias says which team this is, and this person holds no
            # record for it. That is a clear answer, not a reason to fall
            # through to guessing.
            return HalValidation(
                matched=False, category=None, season=season, needs_review=False,
                reasons=[f"'{merchant}' is {named}, and "
                         f"'{charge.cardholder_name}' holds no season account "
                         f"for them"],
            )
    elif candidates:
        # The team already established for this row, when it names one this
        # person actually holds. Strictly additive: where it does not resolve,
        # the bank-detail comparison below runs exactly as it did before.
        hinted = ([r for r in candidates if _bare(r.team) == _bare(team_hint)]
                  if team_hint else [])
        if hinted:
            candidates = hinted
        elif not merchant.strip():
            # Nothing to compare. Falling through left every record the holder
            # owns in play and matched one of them arbitrarily, so a charge with
            # no bank detail acquired somebody's season account.
            return HalValidation(
                matched=False, category=None, season=season, needs_review=False,
                reasons=["no bank detail to identify a team"],
            )
        else:
            scored = [(_team_score(merchant, r.team), r) for r in candidates]
            best = max((s for s, _ in scored), default=0.0)
            if best <= 0:
                return HalValidation(
                    matched=False, category=None, season=season,
                    needs_review=False,
                    reasons=[f"'{charge.cardholder_name}' holds season tickets, "
                             f"but none for '{merchant}' — not a team charge"],
                )
            candidates = [r for s, r in scored if s == best]

    if not candidates:
        return HalValidation(
            matched=False, category=None, season=season, needs_review=True,
            reasons=[f"'{charge.cardholder_name}' has no STH record — cannot "
                     f"confirm season-ticket holder"],
        )

    # Prefer a record with a status for the charge's own season; if none has
    # one, accept another tracked season. Mid-renewal, a holder can be live for
    # 27/28 while 26/27 is still blank (or the reverse).
    rec = next((r for r in candidates if r.status_for(season)), None)
    used_season = season
    if rec is None:
        for alt in TRACKED_SEASONS:
            rec = next((r for r in candidates if r.status_for(alt)), None)
            if rec is not None:
                used_season = alt
                break
    if rec is None:
        rec = candidates[0]
    status = rec.status_for(used_season)
    stale = used_season != season
    season = used_season

    v = HalValidation(matched=True, category=None, season=season, status=status,
                      holder=rec.holder_name, team=rec.team, record=rec)

    if status is None:
        v.needs_review = True
        v.reasons.append(f"STH record found but no {season} status set")
        return v

    _kind = status_kind(status, getattr(hal, "status_words", None))
    if _kind == "active":
        v.category = _category_for(rec)
        v.reasons.append(f"active STH record for {season} — code as {v.category}"
                         + (" (no status set for the charge's own season)"
                            if stale else ""))

    elif _kind == "progress":
        v.category = _category_for(rec)
        v.reasons.append(f"STH status '{status}' (in progress) — code as "
                         f"{v.category}, account not yet settled")

    elif _kind == "dead":
        v.needs_review = True
        v.reasons.append(f"STH status '{status}' for {season} — no live record; "
                         f"team charge on this card is an anomaly")

    else:
        v.needs_review = True
        v.reasons.append(f"unrecognized STH status '{status}' — review")

    if rec.plan_type:
        v.reasons.append(f"plan: {rec.plan_type}")
    return v
