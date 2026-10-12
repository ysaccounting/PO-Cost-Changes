"""
Suggestion engine — ranked match candidates for human review.

This is NOT auto-matching. For each charge it returns the top-N most likely
bills, each with a score and plain-English reasons, so a reviewer picks from a
short list instead of searching. The bar is "is the right bill in the top 3-5",
not "did we get it right unattended".

Signals available on real Y&S data (measured, not assumed):
  - date proximity      charge date vs bill date
  - amount closeness    exact, rounding-tolerance, or near
  - text overlap        charge bank detail (merchant + cardholder) vs bill
                        vendor name + memo

Deliberately NOT used: order number (only 12% of bills) and card last-4 (absent
from bills entirely). The engine leans on what the data actually carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

# Words that appear in nearly every merchant/vendor string and carry no
# discriminating power. Stripped before text comparison.
# The built-in list, used until the table is populated. set_stopwords replaces
# it wholesale, so the UI is the source of truth once anyone has saved there.
_STOPWORDS_OVERRIDE: set | None = None


def _stopwords() -> set:
    return STOPWORDS if _STOPWORDS_OVERRIDE is None else _STOPWORDS_OVERRIDE


# alias (lowercase) -> team name. Pushed in from the table before scoring, the
# same way weights and rules are.
_ALIASES: dict = {}
_ALIAS_TOKENS: dict = {}


# Programs that issue a fresh card number per purchase. A "card" from one of
# these is one of thousands rather than something somebody carries, which
# changes what a CC Last 4 identifies -- so it is worth flagging, and worth
# deriving from the name rather than asking somebody to tick a box per account.
#
# Editable under Rules & Tags -> Virtual Cards: which programs issue virtual
# cards is a fact about the world, and a new one shouldn't need a code change.
# An account can still be marked virtual individually on Card Accounts; that
# tick wins regardless of what this list says.
DEFAULT_VIRTUAL_PROGRAMS = ("wex", "divvy", "slash", "taekus",
                            "global rewards", "flex")
_VIRTUAL_PROGRAMS: tuple = DEFAULT_VIRTUAL_PROGRAMS


def set_virtual_programs(names) -> None:
    """Replace the list of name fragments that mean 'virtual card program'.

    An empty list is honored as empty -- "no program is virtual by name" is a
    real answer here, unlike the never-direct prefixes, and someone who clears
    the table means it. The per-account tick is the way back.
    """
    global _VIRTUAL_PROGRAMS
    _VIRTUAL_PROGRAMS = tuple(str(p).strip().lower() for p in (names or [])
                              if str(p).strip())


def current_virtual_programs() -> list:
    return list(_VIRTUAL_PROGRAMS)


def is_virtual_program(name: str) -> bool:
    n = (name or "").lower()
    return any(p in n for p in _VIRTUAL_PROGRAMS)


def _venue_words(text: str) -> set:
    """Words of a venue name, WITHOUT the stopword filter.

    tokens() drops "arena", "field", "park", "center" -- correct for performer
    text, where they are noise on half the bills, and wrong here: they are
    precisely what makes "Chase Field" a ballpark rather than a bank. Only very
    short words are dropped.
    """
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= 3}


def _expand(text: str) -> str:
    """The abbreviation table, shared with STH so both sides agree."""
    try:
        from .hal import expand_text
        return expand_text(text)
    except Exception:                       # noqa: BLE001
        return str(text or "").lower()


# Words that are a CITY or a club prefix somewhere in the league. Rebuilt with
# the alias map, because it is derived from the same team names.
_PLACE_WORDS: set = set()


def set_team_aliases(pairs) -> None:
    """Replace the alias map. `pairs` is (alias, team)."""
    global _ALIASES, _ALIAS_TOKENS, _PLACE_WORDS
    _ALIASES = {_expand(a): str(t).strip()
                for a, t in (pairs or []) if str(a).strip() and str(t).strip()}
    # Tokenized once here rather than per candidate: this runs on every scored
    # pair, and there are hundreds of aliases.
    _ALIAS_TOKENS = {a: _venue_words(a) for a in _ALIASES}
    # Every word that leads another team's name is geography, not identity.
    # "dallas" leads Dallas Stars and Dallas Cowboys; "seattle" leads Seattle
    # Kraken and Seattle Mariners. Derived from the league's own names, so no
    # city list has to be kept anywhere.
    places = set()
    for team in {t for t in _ALIASES.values()}:
        w = [x for x in _venue_words(_bare_team_name(team))]
        # Order is lost by _venue_words (it returns a set), so the team name is
        # re-split here to know which word is last.
        parts = re.findall(r"[a-z0-9]+", _bare_team_name(team).lower())
        if len(parts) > 1:
            places.update(parts[:-1])
    _PLACE_WORDS = places


def current_team_aliases() -> dict:
    return dict(_ALIASES)


def team_for_detail(text: str) -> str:
    """The team a bank detail names, through a venue or a known alias.

    Matched on SIGNIFICANT WORDS, in either direction. Substring containment
    only worked when the alias was shorter than the bank detail -- it caught
    "CHASE FIELD PHOENIX AZ" against "chase field" and missed "Xfinity Mobile"
    against "Xfinity Mobile Arena", which is the same relationship the other way
    up. A statement abbreviating the venue is at least as common as one padding
    it.

    Stopwords are dropped first, so "arena", "field" and "park" -- which appear
    in half the venues in the league -- cannot carry a match on their own.

    Longest alias first, so a specific venue beats a general one when both fit.
    """
    if not text or not _ALIASES:
        return ""
    # Through the abbreviation table first, so a bank detail written
    # "LA CLIPPERS LLC" reaches an alias stored as "Los Angeles Clippers".
    text = _expand(text)
    detail = _venue_words(text)
    if not detail:
        return ""
    for alias in sorted(_ALIASES, key=len, reverse=True):
        at = _ALIAS_TOKENS.get(alias)
        if not at:
            continue
        # Either side may be the abbreviation, so a subset in either direction
        # is a match -- but never on a single shared word, which would let one
        # city or sponsor claim every venue that mentions it.
        if len(at & detail) < min(len(at), len(detail)):
            continue
        # A ONE-WORD CLAIM inside a longer detail ("PAYPAL *ACESTICKETS 4421",
        # "BUCCANEERS TICKETS") counts when the word is distinctive: claims
        # only reach this list once long enough and held by one team alone
        # (team_claim_pairs), so a short everyday word never gets here.
        if (len(at & detail) >= 2 or at == detail
                or (len(at) == 1 and len(next(iter(at))) >= 6)):
            return _ALIASES[alias]
    return ""


# Words that are part of a club's legal name but not of how anyone refers to
# it. Dropped when deciding whether a bank detail NAMES a team, so "Atlanta
# United FC" needs "atlanta" and "united" and not "fc".
TEAM_FILLER = {"fc", "cf", "sc", "afc", "club", "tickets", "ticket", "the"}

# A bank detail starting with one of these is a MARKETPLACE purchase, not money
# paid to a team. "TM *JACKSON BROWNE" is Ticketmaster; "TM" without the star is
# the team billing directly. Editable under Algorithms, because which prefixes
# a processor uses is a fact about the world rather than about this code.
DEFAULT_NEVER_DIRECT = ("tm *", "tm*")
_NEVER_DIRECT: tuple = DEFAULT_NEVER_DIRECT


def set_never_direct(prefixes) -> None:
    """Replace the list of prefixes that mean 'marketplace, not the team'."""
    global _NEVER_DIRECT
    cleaned = tuple(str(p).strip().lower() for p in (prefixes or [])
                    if str(p).strip())
    _NEVER_DIRECT = cleaned or DEFAULT_NEVER_DIRECT


def current_never_direct() -> list:
    return list(_NEVER_DIRECT)


def is_marketplace_detail(text: str) -> bool:
    """Does this bank detail say a marketplace rather than a team?"""
    t = (text or "").strip().lower()
    return any(t.startswith(p) for p in _NEVER_DIRECT)


def team_for_prefill(text: str) -> str:
    """The team a bank detail NAMES outright, or "".

    Stricter than team_for_detail on purpose. That one is used for SCORING,
    where a venue or a partial hit is evidence to be weighed against everything
    else. This one decides what to write into somebody's Vendor and Category
    fields, and a wrong answer there is a wrong posting rather than a lower
    score -- so it requires the team's own name, every significant word of it,
    to be present in the bank detail.

    "ATLANTA UNITED FC TICKETS" names Atlanta United FC. "UNITED CENTER" does
    not, and neither does "BOX OFFICE KNICKS" name the New York Knicks. Those
    are real cases and they belong in Vendor & Category Rules, where somebody
    states the mapping deliberately, rather than in a guess that is right often
    enough to be trusted and wrong often enough to hurt.
    """
    if not text or is_marketplace_detail(text):
        return ""
    detail = _venue_words(text)
    if not detail:
        return ""
    # The detail as a flat word sequence, for the phrase test below. Word
    # membership alone cannot tell "FC DALLAS" from "DALLAS ... FC SOMETHING".
    _flat = " " + " ".join(re.findall(
        r"[a-z0-9]+", _expand(text).lower())) + " "
    best = ""
    for team in {t for t in _ALIASES.values()}:
        words = {w for w in _venue_words(_bare_team_name(team))
                 if w not in TEAM_FILLER}
        if not words or not words <= detail:
            continue
        # A team whose whole significant name is ONE word that is geography
        # elsewhere cannot be named this way. TEAM_FILLER strips "fc", so
        # "FC Dallas" reduces to {"dallas"} -- and the subset test then said
        # that any detail containing the word names the club. DALLAS SUMMER
        # MUSICALS resolved to FC Dallas, and because this resolver's answer
        # beats every other signal, it pulled that person's FC Dallas season
        # record onto a theater charge.
        #
        # The club is still reachable: "FC DALLAS TICKETS" matches through the
        # alias table, and a rule can state the mapping outright. What is
        # refused is inferring a club from a bare city name.
        # EVERY significant word being geography, not just one. "San Diego FC"
        # loses "fc" to the filler list and reduces to {san, diego} -- both of
        # which lead San Diego Padres -- so "BROADWAY SAN DIEGO" resolved to
        # the soccer club. A theater is not a football team, and the only thing
        # the two share is a city. The mascot is what identifies a team; a
        # place name never does, however many words of it there are.
        # "San Diego FC" and "Orlando City SC" are structurally identical --
        # a city plus a competition suffix the filler list strips -- so no rule
        # about the TEAM name can tell them apart. The discriminator is in the
        # DETAIL: a charge that means the club says so, with the suffix itself
        # or the word it stands for.
        #
        #   BROADWAY SAN DIEGO     no fc / sc / soccer   -> a theater
        #   SAN DIEGO FC TICKETS   "fc"                  -> the club
        #   ORLANDO CITY SOCCER    "soccer"              -> the club
        # Split raw, not through _venue_words: that drops two-letter tokens, so
        # the very suffix being tested for would vanish before it was seen.
        _club = {"fc", "sc", "cf", "sporting", "soccer", "football", "united"}
        _tparts = set(re.findall(r"[a-z0-9]+", _bare_team_name(team).lower()))
        _dparts = set(re.findall(r"[a-z0-9]+", _expand(text).lower()))
        if (words and words <= _PLACE_WORDS
                and not ((_tparts & _club) and (_dparts & _club))):
            # Unless the club's FULL name is present as a phrase. "FC DALLAS
            # TICKETS" does name the club -- the filler word is what makes it
            # unambiguous, so it is worth testing even though it is dropped
            # from the significant-word set. "DALLAS SUMMER MUSICALS" does not
            # contain "fc dallas" and is still refused.
            _full = " ".join(re.findall(
                r"[a-z0-9]+", _bare_team_name(team).lower()))
            if not _full or f" {_full} " not in _flat:
                continue
        # The longest team name that fits, so "New York Rangers" beats
        # "Rangers" when a detail contains both.
        if len(team) > len(best):
            best = team
    return best


def _bare_team_name(team: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", team or "").strip()


def set_stopwords(words) -> None:
    """Replace the ignored-word list. None restores the built-in one."""
    global _STOPWORDS_OVERRIDE
    _STOPWORDS_OVERRIDE = ({str(w).strip().lower() for w in words if str(w).strip()}
                           if words is not None else None)


def current_stopwords() -> list:
    return sorted(_stopwords())


STOPWORDS = {
    "tickets", "ticket", "tm", "the", "inc", "llc", "co", "com", "arena",
    "stadium", "center", "center", "field", "park", "sports", "entertainment",
    "ent", "operations", "mlb", "nba", "nhl", "nfl", "us", "usa", "i",
}

DATE_WINDOW_DAYS = 10       # generous: suggestions, not auto-match
MAX_SUGGESTIONS = 5


def tokens(text: str) -> set[str]:
    """Normalize free text to comparable word tokens."""
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", str(text).lower())
    return {w for w in words if w not in _stopwords()
            and len(w) >= int(_L["text_min_word"])}


# Marketplace prefixes a bank feed puts in front of a purchase. The bill's
# VENDOR names the marketplace ("Ticketmaster", "Gotickets"), so this is the
# most reliable bridge between the two sides -- far more so than the performer,
# which frequently doesn't reach the bank detail at all.
#
# "TM *" is Ticketmaster. Bare "TM" is NOT: a charge reading "TM CHICAGO BULLS"
# is the team billing directly, and matching it to a Ticketmaster bill would be
# wrong. The asterisk is the whole distinction, so it is matched literally.
MARKETPLACE_PREFIXES = [
    (re.compile(r"\bTM\s*\*", re.I), "ticketmaster"),
    (re.compile(r"\bTICKETMASTER\b", re.I), "ticketmaster"),
    (re.compile(r"\bSEATGEEK\b", re.I), "seatgeek"),
    (re.compile(r"\bSTUBHUB\b", re.I), "stubhub"),
    (re.compile(r"\bVIVID\s*SEATS\b", re.I), "vivid seats"),
    (re.compile(r"\bGOTICKETS\b", re.I), "gotickets"),
    (re.compile(r"\bTICKPICK\b", re.I), "tickpick"),
    (re.compile(r"\bAXS\b", re.I), "axs"),
    (re.compile(r"\bLIVE\s*NATION\b", re.I), "live nation"),
]


def marketplace_of(bank_detail: str) -> str:
    """Which ticketing marketplace this bank detail names, if any."""
    text = bank_detail or ""
    for pattern, name in MARKETPLACE_PREFIXES:
        if pattern.search(text):
            return name
    return ""


def vendor_match(bank_detail: str, bill_vendor: str) -> float:
    """1.0 when the bank detail names the same marketplace as the bill's vendor.

    Deliberately strict. The point of this signal is that "TM *" identifies
    Ticketmaster with near-certainty; loosening it to a fuzzy comparison would
    let "TM CHICAGO BULLS" -- a team charge -- match Ticketmaster bills, which
    is exactly the confusion it exists to prevent.
    """
    mk = marketplace_of(bank_detail)
    if mk and bill_vendor:
        if mk in bill_vendor.strip().lower():
            return 1.0
        # A named marketplace that ISN'T this bill's vendor is a mismatch, not
        # an absence -- "TM *" against a Gotickets bill means the money went
        # somewhere else. Falling through to a word comparison here would let a
        # coincidental token rescue it.
        return 0.0
    # No marketplace prefix: fall back to plain word overlap, so a bank detail
    # naming the vendor directly still counts.
    return text_overlap(bank_detail, bill_vendor)


def text_overlap(charge_text: str, bill_text: str) -> float:
    """How much of the charge's bank detail appears in the bill's vendor/memo.

    Uses token overlap first (robust to word order and extra noise), then falls
    back to fuzzy string similarity for near-misses like
    'BROADWAYACROSSAMERICA' vs 'Broadway Across America'.
    """
    ct, bt = tokens(charge_text), tokens(bill_text)
    if not ct or not bt:
        return 0.0

    shared = ct & bt
    if shared:
        return len(shared) / min(len(ct), len(bt))

    # No shared tokens: merchant strings are often run together with no spaces,
    # so compare the squashed forms fuzzily.
    a = "".join(sorted(ct))
    b = "".join(sorted(bt))
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if ratio >= _L["text_fuzzy_floor"] else 0.0


def _digits(value) -> str:
    """Just the digits, so the same reference written three ways compares equal."""
    return re.sub(r"\D", "", str(value or ""))


@dataclass
class Suggestion:
    bill_id: str
    score: float
    reasons: list[str] = field(default_factory=list)
    date_gap_days: int = 0
    amount_gap: Decimal = Decimal("0.00")


# Weights: amount and text carry the most information; date narrows the field.
W_AMOUNT = 0.38
W_TEXT = 0.27
W_DATE = 0.15
W_NAME_EMAIL = 0.20     # cardholder name found inside the bill's buyer email
# An EXACT email match between the charge's cardholder and the bill's buyer.
# Weighted above the fuzzy name-in-email signal because it is a different kind
# of evidence: "kaylsbentley@gmail.com == kaylsbentley@gmail.com" identifies the
# person, where "surname appears in the address" only suggests it. Most charges
# could never produce this -- only Divvy's export carries an email -- until the
# address book supplied one for every cardholder.
W_EMAIL = 0.32
# The bank detail names the same marketplace as the bill's vendor. Separate from
# W_TEXT because it is a different question: text overlap asks "do these
# describe the same event", this asks "did the money go to the same place".
W_VENDOR = 0.22
# An order number appearing on BOTH sides is about as close to a shared
# identifier as this data gets -- far stronger evidence than a name or a
# performer, which is why it starts high. Zero when either side lacks one,
# which is most of the time, so it never penalizes.
W_ORDER = 0.45


# The weights above are defaults. They can be overridden at runtime from the
# Match Weights table, so tuning does not need a deploy.
DEFAULT_WEIGHTS = {
    "amount": W_AMOUNT, "text": W_TEXT, "date": W_DATE,
    "name_email": W_NAME_EMAIL, "email": W_EMAIL, "vendor": W_VENDOR,
    "order": W_ORDER,
}

# The rest of the scorecard: what disqualifies a candidate before scoring, where
# the strength bands fall, and the ceiling. These mattered as much as the
# weights and were the only part still requiring a deploy to change.
DEFAULT_LIMITS = {
    # Kept as the single-window setting older stored configurations wrote. It
    # is no longer read when scoring -- set_limits copies it into both sides
    # below -- but removing the key would silently widen the window for anyone
    # whose table still holds only this one.
    "gate_date_days": float(DATE_WINDOW_DAYS),
    # The window is not symmetric in practice: a card is charged on or after
    # the day the bill is raised far more often than before it, so the two
    # sides are set independently.
    #   before = the charge is EARLIER than the bill
    #   after  = the charge is LATER than the bill
    "gate_days_before": float(DATE_WINDOW_DAYS),
    "gate_days_after": float(DATE_WINDOW_DAYS),
    # A candidate survives on EITHER tolerance, not both. A flat percentage is
    # pennies on a small bill and hundreds on a large one; the absolute figure
    # holds the small end open without loosening the large.
    "gate_amount_pct": 2.0,
    "gate_amount_abs": 1.00,
    # Each gate can be switched off entirely (1 = enforced, 0 = off). Off means
    # the gate stops rejecting: with the date gate off a bill of any age still
    # competes; with the amount gate off any amount does. The pair is still
    # scored, so a reviewer can see everything and judge, rather than the engine
    # deciding what is even allowed to appear. On by default.
    # One switch per gate, because they are four separate tests. Tying the two
    # halves of the date window together meant you could not, say, allow a bill
    # of any age BEFORE the charge while still refusing one long after it.
    "gate_days_before_on": 1.0,
    "gate_days_after_on": 1.0,
    "gate_amount_pct_on": 1.0,
    "gate_amount_abs_on": 1.0,
    # --- how each signal grades WITHIN itself -----------------------------
    # The gates decide what competes and the weights decide how much each
    # signal is worth. These decide how much of its weight a signal awards for
    # a partial answer -- which used to be fixed in the code, so the only way
    # to say "a gap under a dollar is nearly as good as exact" was to edit it.
    #
    # Amount: two thresholds and four fractions, in descending order.
    "amt_near_gap": 0.05,
    "amt_close_gap": 1.00,
    "amt_exact_f": 1.00,
    "amt_near_f": 0.95,
    "amt_close_f": 0.70,
    "amt_far_f": 0.40,
    # Date: a grace period that scores full marks. Zero means only a same-day
    # bill is worth the whole weight; 3 means anything inside three days is.
    "date_full_days": 0.0,
    # Name in Email: both names found, surname only, forename only.
    "name_both_f": 1.00,
    "name_surname_f": 0.60,
    "name_other_f": 0.40,
    # Text Overlap's own two constants. The first is how similar two squashed
    # merchant strings must be before a fuzzy match counts at all; the second
    # is the shortest word worth comparing. Both shape every text comparison
    # and both used to need a deploy to change.
    "text_fuzzy_floor": 0.60,
    "text_min_word": 3.0,
    # WHERE EACH BAND STARTS -- editable in the Strengths grid (Algorithms).
    "tier_perfect": 1.00,
    "tier_very_strong": 0.95,
    "tier_strong": 0.84,
    "tier_moderate": 0.50,
    "score_cap": 1.0,
}

_W = dict(DEFAULT_WEIGHTS)
_L = dict(DEFAULT_LIMITS)


def set_limits(overrides: dict) -> None:
    """Replace the live limits. Unknown keys and impossible values are ignored."""
    global _L
    merged = dict(DEFAULT_LIMITS)
    bounds = {"gate_date_days": (0, 400), "gate_days_before": (0, 400),
              "gate_days_after": (0, 400), "gate_amount_pct": (0, 100),
              "gate_amount_abs": (0, 100000),
              "gate_days_before_on": (0, 1), "gate_days_after_on": (0, 1),
              "gate_amount_pct_on": (0, 1), "gate_amount_abs_on": (0, 1),
              "amt_near_gap": (0, 1000), "amt_close_gap": (0, 10000),
              "amt_exact_f": (0, 1), "amt_near_f": (0, 1),
              "amt_close_f": (0, 1), "amt_far_f": (0, 1),
              "date_full_days": (0, 400),
              "name_both_f": (0, 1), "name_surname_f": (0, 1),
              "name_other_f": (0, 1),
              "text_fuzzy_floor": (0, 1), "text_min_word": (1, 12),
              "tier_perfect": (0, 1), "tier_very_strong": (0, 1),
              "tier_strong": (0, 1), "tier_moderate": (0, 1), "score_cap": (0.1, 1)}
    for k, v in (overrides or {}).items():
        if k not in merged:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        lo, hi = bounds[k]
        if lo <= f <= hi:
            merged[k] = f
    # A configuration written before the window was split carries only the old
    # single key. Honor it on both sides rather than falling back to a default
    # that may be wider than what was set.
    given = set((overrides or {}).keys())
    if "gate_date_days" in given and not ({"gate_days_before", "gate_days_after"} & given):
        merged["gate_days_before"] = merged["gate_days_after"] = merged["gate_date_days"]
    # Moderate can't sit above strong, or every moderate match would also be
    # strong and the band would be empty.
    # Each band starts no lower than the one below it.
    if merged["tier_moderate"] > merged["tier_strong"]:
        merged["tier_moderate"] = merged["tier_strong"]
    if merged["tier_very_strong"] < merged["tier_strong"]:
        merged["tier_very_strong"] = merged["tier_strong"]
    if merged["tier_perfect"] < merged["tier_very_strong"]:
        merged["tier_perfect"] = merged["tier_very_strong"]
    _L = merged


def current_limits() -> dict:
    return dict(_L)


# Standing exceptions to how a bank detail is read. Same shape as the weights:
# pushed in from the table before scoring, empty by default, and an empty list
# scores exactly as the code did before the table existed.
_R: list[dict] = []


def set_match_rules(rules) -> None:
    """Replace the live match rules. Malformed entries are dropped."""
    global _R
    clean = []
    for r in rules or []:
        phrase = str((r or {}).get("bank_detail") or "").strip().lower()
        if not phrase:
            continue
        clean.append({
            "bank_detail": phrase,
            "item": (r.get("item") or "bank_detail"),
            "op": (r.get("op") or "contains"),
            "bill_vendor": str(r.get("bill_vendor") or "").strip().lower(),
            "ignore_email": bool(r.get("ignore_email")),
            "ignore_text": bool(r.get("ignore_text")),
        })
    _R = clean


def current_match_rules() -> list[dict]:
    return [dict(r) for r in _R]


def rules_for_charge(charge, bill=None) -> list[dict]:
    """Every active exception that applies to this charge, and this bill.

    A rule now names WHICH field it tests -- the bank detail on the statement,
    or the vendor on the bill -- and HOW: contains, or equals exactly. A vendor
    rule needs the bill to judge, so it is skipped when scoring without one.
    """
    detail = str(getattr(charge, "merchant", "") or "").lower()
    if not _R:
        return []
    out = []
    for r in _R:
        value = r["bank_detail"]
        if not value:
            continue
        item = r.get("item") or "bank_detail"
        op = r.get("op") or "contains"
        if item == "vendor":
            if bill is None:
                continue
            hay = str(getattr(bill, "vendor", "") or "").lower()
        else:
            hay = detail
        if not hay:
            continue
        if (hay.strip() == value if op == "equals" else value in hay):
            out.append(r)
    return out


def set_weights(overrides: dict) -> None:
    """Replace the live weights. Unknown keys and bad values are ignored."""
    global _W
    merged = dict(DEFAULT_WEIGHTS)
    for k, v in (overrides or {}).items():
        if k in merged:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if 0 <= f <= 1:
                merged[k] = f
    _W = merged


def current_weights() -> dict:
    return dict(_W)


def name_in_email(cardholder: str, email: str) -> float:
    """How strongly the cardholder's name appears in the bill's buyer email.

    Y&S bill memos carry the buyer's email, and it very often encodes their
    name -- "Shannon Tanner" -> shannontanner@outlook.com. That's the closest
    thing to a shared identifier between the card side (which knows names) and
    the ledger side (which knows emails).

    It is NOT always true, so this is a strong positive signal when present and
    costs nothing when absent -- never a penalty, or legitimate matches on
    cards bought for someone else would be pushed down.

    MATCHED AT WORD BOUNDARIES, not anywhere inside the address. Plain
    containment read "ann" inside "joanna" and "lee" inside "leeds", so
    "Ann Lee" scored a full 1.0 against joanna.leeds@ -- a different person
    entirely, at the signal's maximum confidence. Three shapes count, and
    nothing else does:

      exact segment      shannon.tanner  ->  shannon, tanner
      initial + surname  stanner         ->  tanner
      run-together names shannontanner   ->  shannon, tanner

    Parts under three characters are ignored outright: they are too short to
    identify anyone and are what made the coincidences possible.
    """
    if not cardholder or not email:
        return 0.0
    local = email.split("@")[0].lower()
    segments = [seg for seg in re.split(r"[^a-z]+", local) if seg]
    if not segments:
        return 0.0

    parts = [p for p in re.split(r"[^a-zA-Z]+", cardholder.lower()) if len(p) > 2]
    if not parts:
        return 0.0

    def decomposes(seg: str) -> set:
        """Which name parts a run-together segment is built from, if any.

        Only counts when the parts consume the WHOLE segment -- "leeds" starting
        with "lee" leaves "ds" over and is not a match, which is the case that
        used to slip through.
        """
        found, rest = set(), seg
        progress = True
        while rest and progress:
            progress = False
            for p in parts:
                if rest.startswith(p):
                    found.add(p)
                    rest = rest[len(p):]
                    progress = True
                    break
        return found if not rest else set()

    hits: set = set()
    for seg in segments:
        for p in parts:
            if seg == p:
                hits.add(p)
            elif seg.endswith(p) and len(seg) - len(p) == 1:
                hits.add(p)          # an initial in front of the surname
        hits |= decomposes(seg)

    if len(hits) >= 2:
        return float(_L["name_both_f"])
    if hits:
        # A surname alone is more distinctive than a forename alone.
        only = next(iter(hits))
        return float(_L["name_surname_f"] if only == parts[-1]
                     else _L["name_other_f"])
    return 0.0


def score_pair(charge, bill) -> Suggestion | None:
    """Score one charge against one bill. Returns None if implausible."""
    # Signed, so the two sides of the window can differ. Positive means the
    # charge is LATER than the bill, which is the ordinary case.
    delta_days = (charge.txn_date - bill.txn_date).days
    gap_days = abs(delta_days)
    window = _L["gate_days_after"] if delta_days >= 0 else _L["gate_days_before"]
    # The side that applies has its own switch: a bill dated BEFORE the charge
    # is a different question from one dated after it.
    _date_on = _L.get("gate_days_after_on" if delta_days >= 0
                      else "gate_days_before_on", 1.0)
    if _date_on >= 0.5 and gap_days > window:
        return None

    reasons: list[str] = []
    score = 0.0

    # --- amount ---
    # THE GATE, and only the gate. It runs before the grading ladder below,
    # because the ladder used to sit in front of it: gaps under $1 were scored
    # and kept without either tolerance ever being consulted, so tightening
    # them below a dollar did nothing at all. One place now decides what
    # survives; the ladder only decides how good a survivor looks.
    #
    # Either tolerance is enough. A flat percentage is pennies on a small bill
    # and hundreds on a large one, and the dollar figure holds the small end
    # open without loosening the large.
    amount_gap = charge.amount - bill.amount
    abs_gap = abs(amount_gap)
    rel = float(abs_gap) / max(float(bill.amount), 1.0)
    within_pct = rel <= (_L["gate_amount_pct"] / 100.0)
    within_abs = float(abs_gap) <= _L["gate_amount_abs"]
    # Each tolerance is its own gate. A candidate survives if EITHER an enabled
    # tolerance accepts it -- and if both are switched off, the amount stops
    # disqualifying anything at all.
    _pct_on = _L.get("gate_amount_pct_on", 1.0) >= 0.5
    _abs_on = _L.get("gate_amount_abs_on", 1.0) >= 0.5
    if (_pct_on or _abs_on) and not ((_pct_on and within_pct)
                                     or (_abs_on and within_abs)):
        return None          # too far apart to be worth suggesting

    # The ladder. Graded rather than proportional because the steps mean
    # something: an exact match is a different kind of evidence from a close
    # one, and a cent of drift is not the same as a dollar of it.
    if abs_gap == 0:
        score += _W["amount"] * _L["amt_exact_f"]
        reasons.append("amount matches exactly")
    elif abs_gap <= Decimal(str(_L["amt_near_gap"])):
        score += _W["amount"] * _L["amt_near_f"]
        reasons.append(f"amount off by {amount_gap:+} (rounding)")
    elif abs_gap <= Decimal(str(_L["amt_close_gap"])):
        score += _W["amount"] * _L["amt_close_f"]
        reasons.append(f"amount off by {amount_gap:+}")
    else:
        score += _W["amount"] * _L["amt_far_f"]
        reasons.append(
            f"amount within {_L['gate_amount_pct']:g}% ({amount_gap:+})"
            if within_pct
            else f"amount within ${_L['gate_amount_abs']:g} ({amount_gap:+})"
            if within_abs
            else f"amount off by {amount_gap:+} (amount gate off)")

    # --- text: charge bank detail vs bill vendor + memo ---
    charge_text = f"{charge.merchant or ''} {charge.cardholder_name or ''}"
    # The marketplace first. "TM *JACKSON BROWNE" tells you the money went to
    # Ticketmaster even though the bill's memo names Jackson Browne and the bank
    # detail never mentions Ticketmaster by name -- and the performer often
    # doesn't reach the bank detail at all, so the event comparison alone misses
    # these entirely.
    # The team this bank detail names, through its venue or a Bank Detail Note.
    # Worked out once and used twice below: it can settle the VENDOR outright
    # when the bill is billed by the team, and it feeds the performer
    # comparison when the bill is billed by a marketplace.
    aliased_team = team_for_detail(charge.merchant or "")

    rules = rules_for_charge(charge, bill)
    # A rule that names this bill's vendor asserts the link the text never
    # states, and counts as fully as reading it there would have. Scored INSTEAD
    # of the text comparison, not on top of it -- both are the same claim.
    bill_vendor_l = (bill.vendor or "").lower()
    named = next((r for r in rules if r["bill_vendor"] and bill_vendor_l
                  and (r["bill_vendor"] in bill_vendor_l
                       or bill_vendor_l in r["bill_vendor"])), None)
    # An alias is the same kind of claim as a rule, and a stronger one than
    # reading words out of a string: somebody maintains the mapping from
    # XFINITY MOBILE to the Philadelphia Flyers, and when the bill is billed BY
    # the Flyers that settles the vendor. Diluting it through the text
    # comparison -- the smallest weight there is -- wasted the one piece of
    # curated knowledge in the whole scorecard.
    alias_is_vendor = bool(
        aliased_team and bill_vendor_l
        and (aliased_team.lower() in bill_vendor_l
             or bill_vendor_l in aliased_team.lower()))
    if named:
        score += _W["vendor"]
        reasons.append(f"rule: bank detail is {bill.vendor}")
    elif alias_is_vendor:
        score += _W["vendor"]
        reasons.append(f"bank detail is {aliased_team}'s venue")
    else:
        mk = vendor_match(charge.merchant or "", bill.vendor or "")
        if mk:
            score += _W["vendor"] * mk
            reasons.append(f"bank detail names {marketplace_of(charge.merchant or '')}")

    # Then the PERFORMER -- always the first segment of the bill memo -- for the
    # cases where the act or team does reach the bank detail.
    #
    # Compared against the performer alone, not the vendor or the whole memo.
    # Folding the vendor in here double-counted it against the signal above, and
    # including the rest of the memo let an email address or an order number
    # produce an overlap that means nothing.
    # A statement that says CHASE FIELD is talking about the Diamondbacks, and
    # the bill says so in words. Adding the team to the charge's text gives the
    # comparison below something to find; the original text is kept, so a bank
    # detail that ALSO names the performer still scores on its own terms.
    if aliased_team:
        charge_text = f"{charge_text} {aliased_team}"

    performer = (bill.memo or "").split("/")[0].strip()
    # Switched off rather than scored down: a signal known to mislead for this
    # merchant should abstain, not vote quietly.
    overlap = 0.0 if any(r["ignore_text"] for r in rules) else \
        text_overlap(charge_text, performer)
    if overlap > 0:
        score += _W["text"] * overlap
        if overlap >= 0.5:
            reasons.append("bank detail matches the performer")
        else:
            reasons.append("partial performer match")

    # --- buyer identity ---
    #
    # An exact email match supersedes the fuzzy one rather than stacking with
    # it: they are two readings of the same evidence, and counting both would
    # let one signal contribute half the score.
    # Membership, not equality: a profile buys under several addresses and the
    # bill names whichever was used, so any one of them identifies the buyer.
    # Both readings of buyer identity go together: trusting one and not the
    # other would make no sense, since they are the same evidence.
    # A VENDOR EXCEPTION (Algorithms > Exceptions: the bill's vendor contains
    # or equals a value) TREATS THE BUYER EMAIL AS A PERFECT MATCH -- a
    # marketplace whose bills carry its own address, not the buyer's. A bank
    # detail exception that switched the email off (the earlier form) still
    # leaves it out.
    _perfect = any(r["ignore_email"] and r.get("item") == "vendor" for r in rules)
    _ignore = any(r["ignore_email"] and r.get("item") != "vendor" for r in rules)
    if _perfect:
        score += _W["email"]
        reasons.append("rule: buyer email treated as a perfect match")
        b_email, known = "", set()
    elif _ignore:
        reasons.append("rule: buyer email not considered")
        b_email, known = "", set()
    else:
        b_email = (bill.email or "").strip().lower()
        known = {e.strip().lower()
                 for e in (getattr(charge, "alt_emails", None) or set())}
    if b_email and charge.email:
        known.add(charge.email.strip().lower())
    if b_email and b_email in known:
        score += _W["email"]
        reasons.append("same buyer email")
    elif not _perfect and (b_email or not _ignore):
        ne = name_in_email(charge.cardholder_name or "", bill.email or "")
        if ne > 0:
            score += _W["name_email"] * ne
            reasons.append("cardholder name matches buyer email"
                           if ne >= 1.0 else "cardholder surname matches buyer email")

    # --- order number ---
    #
    # Compared on digits alone. The two sides write the same reference
    # differently -- "Order #12345", "ORD-12345", "12345" -- and comparing the
    # strings verbatim would miss every one of those. Six digits or more, so a
    # quantity or a year cannot masquerade as a reference.
    c_order = _digits(getattr(charge, "order_number", None))
    b_order = _digits(getattr(bill, "order_number", None))
    if c_order and b_order and len(c_order) >= 6 and c_order == b_order:
        score += _W["order"]
        reasons.append(f"same order number ({c_order})")

    # --- date proximity ---
    #
    # Scaled against the WIDEST configured window, not the side this candidate
    # happened to fall on. The windows are gates: they decide what is eligible.
    # Once a bill is eligible, this signal answers one question -- how close are
    # the dates -- and the answer must not depend on which direction it fell or
    # on how much slack was allowed in that direction.
    #
    # Scaling per side meant a bill one day out scored 0.1364 with a ten-day
    # window and 0.0750 with a one-day window, on identical evidence. Since
    # scores are read against absolute tier floors, tightening a gate silently
    # demoted matches across the whole queue -- and widening one would have
    # promoted them. A gate should change what competes, not what things are
    # worth.
    #
    # max() rather than a constant so the falloff still spans whatever range is
    # actually in use: with a 30-day window, everything past day 10 would
    # otherwise tie at zero and stop being ordered at all.
    scale = max(_L["gate_days_before"], _L["gate_days_after"], 1.0)
    # Anything inside the grace period is as close as close gets; beyond it the
    # falloff runs across whatever range is left.
    full = min(float(_L["date_full_days"]), scale)
    if gap_days <= full:
        closeness = 1.0
    else:
        closeness = max(0.0, 1 - ((gap_days - full) / (scale - full + 1)))
    score += _W["date"] * closeness
    if gap_days == 0:
        reasons.append("same date")
    else:
        reasons.append(f"{gap_days}d apart")

    return Suggestion(
        bill_id=bill.bill_id,
        score=round(min(score, _L["score_cap"]), 4),
        reasons=reasons,
        date_gap_days=gap_days,
        amount_gap=amount_gap,
    )


def suggest(charge, bills, limit: int = MAX_SUGGESTIONS) -> list[Suggestion]:
    """Return the top-N candidate bills for a charge, best first."""
    out = []
    for bill in bills:
        if bill.company != charge.company or not bill.is_open:
            continue
        s = score_pair(charge, bill)
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: s.score, reverse=True)
    return out[:limit]
