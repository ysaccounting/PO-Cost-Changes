"""
Airtable mirror — pulls the STH season-ticket records into local storage.

Why mirror rather than query live: STH is ~30,000 records and the rules run
against every charge in a daily batch. Hitting the API per charge would be slow
and rate-limited. Instead the worker pages the relevant slice into Postgres on a
schedule, and the classifier queries the local copy.

Field IDs are the real ones from the Y&S "Tickets" base. They're pinned here
because Airtable field *names* can be renamed by users while IDs are stable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterator

import time

import httpx

from ..config import settings
from .. import company_vars as _cv

log = logging.getLogger(__name__)


class AirtableAuthError(RuntimeError):
    pass


class AirtableQueryError(RuntimeError):
    pass

API = "https://api.airtable.com/v0"

def BASE_TICKETS_FOR() -> str:
    """This company's tickets base, or the deployment default.

    A function, not a constant: which base to read depends on which company is
    in force, and a module-level constant is fixed at import. Every use below
    calls this so a second broker's base is one Variables entry away.
    """
    return _cv.value("airtable_base_tickets") or "appOEQZDCBXBZRiUv"


# Kept for anything still importing the name; it is the DEFAULT, not "the"
# base, and reading it directly ignores whichever company is in force.
BASE_TICKETS = "appOEQZDCBXBZRiUv"
TABLE_HAL = "tblHVnNLLKSnvjoKr"
TABLE_ADDRESS_BOOK = "tblSBLNnEWyhjrdHn"
TABLE_TEAMS = "tblHjlJG913h29EnU"
# The Emails table. Read directly rather than through the address book's E-Mail
# column, because over the REST API a linked-record field returns bare record
# ids -- ["rec7SLm..."] -- and never the address itself. This table carries both
# the address and the profile name as plain text.
TABLE_EMAILS = "tblDBBSFfsl92GIip"
F_EM_ADDRESS = "fldfS8zFymh4kIt1N"     # "pendent_dairies8x@icloud.com"
F_EM_PERSON = "fld6Ka3DqaL0osGqS"      # "Jane Santiago"

# Address Book fields used to build the person's name
F_AB_FIRSTNAME = "fldBl388T6D83cAOx"
F_AB_LASTNAME = "fldQ9ylAmLdvCCnc5"
F_AB_LOOKUP = "fldCAmTJkqgLD3hZ2"      # primary: "Akeya Eleni (CA) San Francisco 94132"
# The address book's "E-Mail" column, and the ONLY place an address is read
# from. It links to the Emails table and allows several links per profile
# (prefersSingleRecordLink: false), so it already holds every address a profile
# buys under -- there is nothing to supplement it with.
#
# Deliberately not used: the neighboring lookup fields. They reach through a
# DIFFERENT link to pull recovery and forwarding addresses off related records,
# which are not addresses this person buys under, and matching on them would
# attach charges to the wrong buyer.
F_AB_EMAIL = "fldqXIrzfwuxEVgXs"

F_TEAM_NAME = "fldvPQkyhI69Pm0zp"      # Teams primary: "San Jose Sharks"

# --- STH field IDs -----------------------------------------------------------
F_LOOKUP = "fld4YB0f3SzM97YAs"      # "LA Clippers matthewjude86@outlook.com"
F_ADDRESS_BOOK = "fldxpdk4QRYmskwDX"  # "Matthew Jude (CA) Los Angeles 90004"
F_TEAM = "fldpUJf4MQ61kfYsZ"
F_FULL_PARTIAL = "fldWKMM4HFjzr0i3t"  # plan type; carries Wait List / DEPOSIT
F_TOTAL = "fldKTWBlOVClzGvav"
F_CARD = "fldKJyhEeOvropPHO"          # card PROGRAM name, not a number
F_PAYMENT_PLAN = "fldCtfLz5E4kqlT23"  # checkbox

# Season status fields, keyed by season label.
# IDs are used for FIELD SELECTION (stable across renames); names are required
# for filterByFormula, which does not accept field IDs.
STATUS_FIELDS = {
    "24/25": "fldrme0zedWhb3mUE",
    "25/26": "fldixaT5UFHyDaFkd",
    "26/27": "fldFiTzd1eig0eg0B",
    "27/28": "fldFqi6LIWFfpCYUQ",
}

STATUS_FIELD_NAMES = {
    "24/25": "24/25 Status",
    "25/26": "25/26 Status",
    "26/27": "26/27 Status",
    "27/28": "27/28 Status",
}


def _cell(rec: dict, field_id: str):
    return rec.get("cellValuesByFieldId", rec.get("fields", {})).get(field_id)


def _select_name(value) -> str | None:
    """A season status as text, whichever shape Airtable sent.

    Routed through the same reader as Full/Partial rather than handling only the
    object form: a status that arrived as a list would otherwise be stored as a
    list and rendered as one.
    """
    text = _choice_text(value)
    return text or None


def _multi_names(value) -> str:
    """A multipleSelects cell as text, whichever shape Airtable sent.

    The REST list endpoint returns plain STRINGS -- ["Full", "Partial"] -- while
    a webhook payload returns [{"id","name","color"}, ...]. Only the object form
    was handled, so every value read over REST collapsed to an empty string and
    Full/Partial never appeared on a STH record. Both are read now.
    """
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, dict):
                out.append(v.get("name", "") or "")
            elif v is not None:
                out.append(str(v))
        return ", ".join(p for p in out if p)
    return str(value or "")


def _choice_text(value) -> str:
    """A select value as plain text, whether Airtable sent one or many.

    Full/Partial is a select, and each of the four shapes below occurs in
    practice: a bare string (single select over REST), an object (single select
    in a webhook payload), a list of strings (multi select over REST) or a list
    of objects (multi select in a webhook payload).
    """
    if isinstance(value, list):
        return _multi_names(value)
    if isinstance(value, dict):
        return value.get("name", "") or ""
    return str(value or "")


def _linked_name(value) -> str:
    """Linked records come back as [{"id","name"}]."""
    if isinstance(value, list) and value:
        first = value[0]
        return first.get("name", "") if isinstance(first, dict) else str(first)
    return ""


def fetch_hal(seasons: list[str] | None = None,
              page_size: int = 100,
              modified_since_minutes: int | None = None,
              known_teams=None) -> Iterator[dict]:
    """Page the STH table, yielding normalized dicts.

    Filters to records that have a status set for at least one of `seasons`, so
    the mirror stays to the live population rather than all ~30k historical rows.
    """
    # Include 25/26. Plenty of records that are live today still have only a
    # 25/26 status set -- filtering to 26/27+ dropped them from the mirror
    # entirely, so their cardholders looked like they had no STH record at all.
    seasons = seasons or ["25/26", "26/27", "27/28"]
    # A season nobody has added a status field for cannot be queried, and
    # silently narrowing the pull is how a mirror ends up smaller than the table
    # with no explanation. Recorded so the Connectors screen can say so.
    global LAST_HAL_IGNORED_SEASONS
    LAST_HAL_IGNORED_SEASONS = [s for s in seasons if s not in STATUS_FIELD_NAMES]
    if LAST_HAL_IGNORED_SEASONS:
        log.warning("STH: no status field for season(s) %s — not queried",
                    ", ".join(LAST_HAL_IGNORED_SEASONS))
    fields = [F_LOOKUP, F_ADDRESS_BOOK, F_TEAM, F_FULL_PARTIAL, F_TOTAL,
              F_CARD, F_PAYMENT_PLAN] + [STATUS_FIELDS[s] for s in seasons
                                         if s in STATUS_FIELDS]

    # filterByFormula takes field NAMES. Records with any season status set.
    clauses = [f"{{{STATUS_FIELD_NAMES[s]}}} != ''"
               for s in seasons if s in STATUS_FIELD_NAMES]
    formula = f"OR({','.join(clauses)})" if clauses else ""

    if modified_since_minutes:
        # Incremental: only records touched recently. LAST_MODIFIED_TIME() with
        # no arguments is the record's own last-modified stamp, so this catches
        # any edit -- status, plan type, links -- not just one field.
        window = (f"IS_AFTER(LAST_MODIFIED_TIME(), "
                  f"DATEADD(NOW(), -{int(modified_since_minutes)}, 'minutes'))")
        formula = f"AND({formula},{window})" if formula else window

    # A MAPPED BROKER IS ASKED BY NAME, NOT BY FIELD ID.
    #
    # Airtable field ids are generated per base, so the ids above exist only in
    # the default one -- asking another base for them returns 422 on every
    # field. Names are human-chosen and the mapping says which name means what,
    # so a mapped company asks for its own columns by name instead.
    #
    # The season formula is skipped for a mapped base too: it is built from the
    # default base's status column names, which another broker does not have.
    # Everything comes back and the season filter happens in the normalizer.
    # A BROKER'S SETUP (Connectors) names its own bases, tables, views and
    # columns -- as many sources as it keeps its records in.
    _sources = _cv.airtable_sources("sth")
    if _sources:
        yield from _fetch_sth_sources(_sources, seasons, page_size,
                                      modified_since_minutes,
                                      known_teams if known_teams is not None else KNOWN_TEAMS)
        return
    fmap = _cv.field_map()
    headers = {"Authorization": f"Bearer {_cv.value('airtable_token')}"}
    if fmap:
        params: list = [("pageSize", page_size)]
        params += [("fields[]", n) for n in dict.fromkeys(fmap.values())]
    else:
        params = [("pageSize", page_size), ("returnFieldsByFieldId", "true")]
        if formula:
            params.append(("filterByFormula", formula))
        params += [("fields[]", f) for f in fields]

    # This company's base, resolved now rather than at import.
    url = f"{API}/{BASE_TICKETS_FOR()}/{TABLE_HAL}"
    offset = None
    pages = 0
    _read = 0
    with httpx.Client(timeout=30) as client:
        while True:
            q = list(params)
            if offset:
                q.append(("offset", offset))
            r = client.get(url, headers=headers, params=q)
            if r.status_code in (401, 403):
                raise AirtableAuthError(
                    f"Airtable returned {r.status_code}. Check that the token "
                    f"exists, has scopes data.records:read and schema.bases:read, "
                    f"AND has been granted access to the Tickets base "
                    f"({BASE_TICKETS_FOR()}). Adding scopes alone is not enough — the "
                    f"base must be added under the token's Access section. "
                    f"Airtable said: {r.text[:200]}")
            if r.status_code == 422:
                raise AirtableQueryError(
                    f"Airtable rejected the query (422). Usually a field name in "
                    f"filterByFormula that doesn't exist. Airtable said: {r.text[:300]}")
            if r.status_code == 429:
                time.sleep(2)          # backoff and retry this page
                continue
            r.raise_for_status()
            body = r.json()
            for rec in body.get("records", []):
                _read += 1
                yield _map_record(rec, seasons)
            pages += 1
            offset = body.get("offset")
            if not offset:
                break
            time.sleep(0.25)   # Airtable allows 5 req/sec per base
    log.info("fetched STH in %d pages", pages)
    global LAST_HAL_READ
    LAST_HAL_READ = {"pages": pages, "records": _read,
                     "seasons": [x for x in seasons if x in STATUS_FIELD_NAMES]}


def _fetch_table(table_id: str, fields: list[str] | None,
                 page_size: int = 100, base_id: str = "",
                 by_field_id: bool = True, view: str = "") -> Iterator[dict]:
    """Page any table, returning raw records.

    `base_id` because not everything lives in the tickets base -- the team and
    venue aliases are in Accounting. `by_field_id` off lets a caller ask for
    columns by NAME, which is what you want for a table whose field ids nobody
    has written down.
    """
    headers = {"Authorization": f"Bearer {_cv.value('airtable_token')}"}
    base_params = [("pageSize", page_size)]
    if by_field_id:
        base_params.append(("returnFieldsByFieldId", "true"))
    # None asks for EVERY field, which is what a caller wants when the
    # column headings are not known for certain. An empty list would mean
    # the same thing to Airtable, but None says it deliberately.
    base_params += [("fields[]", f) for f in (fields or [])]
    if view:
        base_params.append(("view", view))
    url = f"{API}/{base_id or BASE_TICKETS_FOR()}/{table_id}"
    offset = None
    with httpx.Client(timeout=30) as client:
        while True:
            q = list(base_params)
            if offset:
                q.append(("offset", offset))
            r = client.get(url, headers=headers, params=q)
            if r.status_code in (401, 403):
                raise AirtableAuthError(
                    f"Airtable returned {r.status_code} reading {table_id}. "
                    f"The token needs access to the Tickets base.")
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            body = r.json()
            for rec in body.get("records", []):
                yield rec
            offset = body.get("offset")
            if not offset:
                break
            time.sleep(0.25)


_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Set by the last fetch_person_emails() run: how many addresses the
# shared-address guard discarded, and a sample showing who shared them.
LAST_SHARED_DROPS = 0
LAST_SHARED_SAMPLE: list = []
# Stage-by-stage counts from the last fetch_person_emails() run.
LAST_PULL_STATS: dict = {}
# Seasons asked for that have no status field, so could not be queried.
LAST_HAL_READ: dict = {}
LAST_HAL_IGNORED_SEASONS: list = []
# [(team, league)] the company already names; set by the worker before a sync.
KNOWN_TEAMS: list = []


def _emails_from(value) -> set[str]:
    """Every address in a linked-record cell, whatever shape it arrives in."""
    if value is None:
        return set()
    if isinstance(value, list):
        text = " ".join(
            v.get("name", "") if isinstance(v, dict) else str(v) for v in value)
    elif isinstance(value, dict):
        # {"linkedRecordIds": [...], "valuesByLinkedRecordId": {...}}
        text = json.dumps(value)
    else:
        text = str(value)
    return {m.group(0).lower() for m in _EMAIL_IN_TEXT.finditer(text)}


def _cell_text(v) -> str:
    """A cell as text: plain, a linked record's name, or a list's first item."""
    if isinstance(v, list):
        return ", ".join(_cell_text(x) for x in v if _cell_text(x))
    if isinstance(v, dict):
        return str(v.get("name") or v.get("email") or "")
    return "" if v is None else str(v)


def _emails_from_setup(books) -> dict[str, set[str]]:
    """Names and emails from a broker's own tables, by the columns each source
    names: a name column (or first and last), and an email column that may
    hold several addresses -- typed in, or linked from another table. Every
    source is read and the results pooled, so a person listed in two places
    has the addresses from both. An address used by different people
    identifies none of them."""
    if isinstance(books, dict):
        books = [books]
    by_name: dict[str, set[str]] = {}
    for book in books:
        for f in iter_source(book, book_columns(book)):
            name = _cell_text(f.get(book["name"])) if book.get("name") else ""
            if not name:
                name = f"{_cell_text(f.get(book.get('first')))} {_cell_text(f.get(book.get('last')))}".strip()
            name = re.sub(r"\s+", " ", name).strip().lower()
            if not name:
                continue
            for addr in re.split(r"[,;\s]+", _cell_text(f.get(book.get("email")))):
                addr = addr.strip().lower()
                if "@" in addr:
                    by_name.setdefault(name, set()).add(addr)
    owners: dict[str, set[str]] = {}
    for n, addrs in by_name.items():
        for a in addrs:
            owners.setdefault(a, set()).add(n)
    return {n: {a for a in addrs if len(owners[a]) == 1} for n, addrs in by_name.items()}


# --- reading a broker's source -----------------------------------------------
#
# LINKED COLUMNS. A column that links to another table comes back over the
# REST API as record ids -- ["recnEcagiRm9HOwlU"] -- never the text you see in
# Airtable. That is what put rec... values in the Emails preview. So before a
# row is read, each linked column is swapped for the linked records' text: the
# column the setup chose to read from ("Read From"), or the linked table's
# primary column. Lookup and rollup columns already carry text and pass as is.

def _base_schema(base: str) -> dict:
    """{table id: {"name", "primary", "fields": {name: {"type", "linked"}}}}.
    Empty when the token cannot read the schema; links are then left as ids."""
    try:
        r = httpx.get(f"{API}/meta/bases/{base}/tables", timeout=30,
                      headers={"Authorization": f"Bearer {_cv.value('airtable_token')}"})
        if r.status_code >= 400:
            return {}
        out = {}
        for tb in r.json().get("tables", []):
            fields = {f["name"]: {"type": f.get("type", ""),
                                  "linked": (f.get("options") or {}).get("linkedTableId", "")}
                      for f in tb.get("fields", [])}
            prim = next((f["name"] for f in tb.get("fields", [])
                         if f.get("id") == tb.get("primaryFieldId")), "")
            out[tb["id"]] = {"name": tb.get("name", ""), "primary": prim, "fields": fields}
            out.setdefault(tb.get("name", ""), out[tb["id"]])
        return out
    except Exception:                                   # noqa: BLE001
        log.warning("could not read the schema of %s", base, exc_info=True)
        return {}


def _page(base: str, table: str, params: list, limit: int | None = None):
    """Raw records from one table, paged; at most `limit` when given."""
    headers = {"Authorization": f"Bearer {_cv.value('airtable_token')}"}
    url = f"{API}/{base}/{table}"
    offset, n = None, 0
    with httpx.Client(timeout=30) as client:
        while True:
            q = list(params) + ([("pageSize", min(100, limit))] if limit else [("pageSize", 100)])
            if offset:
                q.append(("offset", offset))
            r = client.get(url, headers=headers, params=q)
            if r.status_code in (401, 403):
                raise AirtableAuthError(
                    f"Airtable returned {r.status_code} reading {table} in {base}. "
                    f"The token needs the data.records:read and schema.bases:read "
                    f"scopes, and access to this base. Airtable said: {r.text[:200]}")
            if r.status_code == 422:
                raise AirtableQueryError(
                    f"Airtable rejected the query (422) on {table}: usually a column "
                    f"or view that has been renamed. Airtable said: {r.text[:300]}")
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            body = r.json()
            for rec in body.get("records", []):
                yield rec
                n += 1
                if limit and n >= limit:
                    return
            offset = body.get("offset")
            if not offset:
                return
            time.sleep(0.25)


def _link_targets(src: dict, columns: list, schema: dict) -> dict:
    """{column: (linked table id, column to read)} for each linked column."""
    table = schema.get(src.get("table")) or schema.get(src.get("table_name") or "") or {}
    out = {}
    for col in columns:
        meta = (table.get("fields") or {}).get(col) or {}
        if meta.get("type") != "multipleRecordLinks" or not meta.get("linked"):
            continue
        linked = schema.get(meta["linked"]) or {}
        read = (src.get("via") or {}).get(col) or linked.get("primary") or ""
        if read:
            out[col] = (meta["linked"], read)
    return out


def _link_maps(src: dict, columns: list, schema: dict, cache: dict) -> dict:
    """{column: {record id: text}} for each of `columns` that links elsewhere --
    the WHOLE linked table, read once per sync."""
    out = {}
    for col, (linked, read) in _link_targets(src, columns, schema).items():
        key = (src["base"], linked, read)
        if key not in cache:
            cache[key] = {rec["id"]: _cell_text((rec.get("fields") or {}).get(read))
                          for rec in _page(src["base"], linked, [("fields[]", read)])}
        out[col] = cache[key]
    return out


def _link_maps_for(src: dict, links: dict, ids_by_col: dict) -> dict:
    """{column: {record id: text}} for just the record ids given -- what a
    PREVIEW needs. Reading the whole linked table to show five rows took as
    long as a sync (a table of every email address), so the preview sat on
    "Reading..." for a long time."""
    out = {}
    for col, (linked, read) in links.items():
        ids = [i for i in ids_by_col.get(col, []) if isinstance(i, str) and i.startswith("rec")]
        m = {}
        for k in range(0, len(ids), 40):
            part = ids[k:k + 40]
            formula = "OR(" + ",".join(f"RECORD_ID()='{i}'" for i in part) + ")"
            for rec in _page(src["base"], linked, [("fields[]", read), ("filterByFormula", formula)]):
                m[rec["id"]] = _cell_text((rec.get("fields") or {}).get(read))
        out[col] = m
    return out


def _swap_links(fields: dict, links: dict) -> dict:
    if not links:
        return fields
    f = dict(fields)
    for col, m in links.items():
        v = f.get(col)
        if isinstance(v, list):
            f[col] = [m.get(x, x) if isinstance(x, str) else x for x in v]
    return f


def book_columns(book: dict) -> list:
    return [c for c in (book.get("name"), book.get("first"), book.get("last"),
                        book.get("email")) if c]


def iter_source(src: dict, columns: list, extra_params=None, limit=None,
                with_ids: bool = False, cache: dict | None = None):
    """Each row of one source as {column: text}, linked columns resolved.
    With `with_ids`, (record id, fields) pairs instead."""
    cache = {} if cache is None else cache
    if (src.get("provider") or "airtable") != "airtable":
        from . import sheets as _sh
        yield from _sh.iter_rows(src, columns, limit=limit, with_ids=with_ids, cache=cache)
        return
    schema = cache.get(("schema", src["base"]))
    if schema is None:
        schema = cache[("schema", src["base"])] = _base_schema(src["base"])
    params = [("fields[]", c) for c in columns] + list(extra_params or [])
    if src.get("view"):
        params.append(("view", src["view"]))
    if limit:
        # A PREVIEW: read the few rows first, then only the linked records
        # those rows point to.
        recs = list(_page(src["base"], src["table"], params, limit=limit))
        targets = _link_targets(src, columns, schema)
        ids = {col: [i for r in recs for i in ((r.get("fields") or {}).get(col) or [])
                     if isinstance((r.get("fields") or {}).get(col), list)] for col in targets}
        links = _link_maps_for(src, targets, ids)
        for rec in recs:
            f = _swap_links(rec.get("fields", {}) or {}, links)
            yield (rec["id"], f) if with_ids else f
        return
    links = _link_maps(src, columns, schema, cache)
    for rec in _page(src["base"], src["table"], params, limit=limit):
        f = _swap_links(rec.get("fields", {}) or {}, links)
        yield (rec["id"], f) if with_ids else f


def _fetch_sth_sources(sources, seasons, page_size, modified_since_minutes,
                       known_teams):
    """Every STH source, normalized; counts kept for the Connectors screen."""
    global LAST_HAL_READ, LAST_HAL_IGNORED_SEASONS
    LAST_HAL_IGNORED_SEASONS = []
    LAST_HAL_IGNORED_IDS.clear()
    extra = []
    if modified_since_minutes:
        extra.append(("filterByFormula",
                      f"IS_AFTER(LAST_MODIFIED_TIME(), DATEADD(NOW(), "
                      f"-{int(modified_since_minutes)}, 'minutes'))"))
    stats = {"records": 0, "pages": 0, "sources": len(sources), "skipped_season": 0,
             "other_seasons": 0, "teams_unread": 0, "unread_sample": []}
    cache: dict = {}
    for src in sources:
        if modified_since_minutes and (src.get("provider") or "airtable") != "airtable":
            continue                     # a spreadsheet has no "changed since"; the full pass reads it
        for rid, f in iter_source(src, setup_sth_columns(src), extra,
                                  with_ids=True, cache=cache):
            stats["records"] += 1
            # IGNORED by the source's own setting: never stored.
            if ignored_row(src, f):
                stats["ignored"] = stats.get("ignored", 0) + 1
                LAST_HAL_IGNORED_IDS.add(rid)
                continue
            m = _map_record_setup({"id": rid, "fields": f}, seasons, src, known_teams)
            # EVERY ROW IS KEPT. A row whose season cannot be read, or is one
            # the engine does not track, is still stored -- counted in the
            # summary, but no longer dropped.
            skip = m.pop("_skip", "")
            if skip == "season":
                stats["skipped_season"] += 1
            elif skip == "other":
                stats["other_seasons"] += 1
            if m.pop("_team_unread", False):
                stats["teams_unread"] += 1
                if len(stats["unread_sample"]) < 5 and m["team"] not in stats["unread_sample"]:
                    stats["unread_sample"].append(m["team"])
            yield m
        stats["pages"] += 1
    stats["seasons"] = list(seasons)
    LAST_HAL_READ = stats


def fetch_person_emails() -> dict[str, set[str]]:
    """Map a cardholder's name -> every email address on their profile.

    Deliberately simple, because the rule is simple: the Profile on a
    transaction is a person's FirstName + LastName in the Address Book, and the
    addresses that person buys under are the E-Mail records their row links to.
    Match the name, take their addresses.

    Earlier versions layered guessing on top of that -- a second attribution
    path through the Emails table's free-text Person column, and a guard that
    deleted any address it thought two people shared. Both went wrong on real
    data. The Person text is a rollup on many rows and arrives pre-concatenated
    ("gabaiegabaiegabaie..."), which invented people who do not exist; the guard
    then counted those inventions as rivals and discarded 5,495 addresses,
    including ordinary private ones. Neither mechanism is here now.

    An address genuinely used by several DIFFERENT people is still not allowed
    to identify one of them, but that is decided on the address-book names --
    which are real -- and never on parsed text.
    """
    # Address Book -> the person's name, and the E-Mail records they link to.
    _books = [b for b in _cv.airtable_sources("book") if b.get("email")]
    if _books:
        return _emails_from_setup(_books)
    people: dict[str, str] = {}
    ab_email_ids: dict[str, list] = {}
    ab_rows = 0
    unnamed = 0
    for rec in _fetch_table(TABLE_ADDRESS_BOOK,
                            [F_AB_FIRSTNAME, F_AB_LASTNAME, F_AB_LOOKUP,
                             F_AB_EMAIL]):
        cells = rec.get("fields", {})
        ab_rows += 1
        # FirstName + LastName, exactly as the Profile is written. Read through
        # _first_text so a cell that arrives wrapped in a list still yields one
        # value rather than a concatenation of every linked row.
        first = _first_text(cells.get(F_AB_FIRSTNAME)).strip()
        last = _first_text(cells.get(F_AB_LASTNAME)).strip()
        name = f"{first} {last}".strip()
        if not name:
            # Only when both are empty: the primary field, with the trailing
            # "(CA) Los Angeles 90046" removed.
            name = re.sub(r"\s*\(.*$", "",
                          _first_text(cells.get(F_AB_LOOKUP))).strip()
        if not name:
            unnamed += 1
            continue
        people[rec["id"]] = name
        eids = _link_ids(cells.get(F_AB_EMAIL))
        if eids:
            ab_email_ids[rec["id"]] = eids

    # E-Mail records -> the address on each.
    email_by_id: dict[str, str] = {}
    em_rows = 0
    for rec in _fetch_table(TABLE_EMAILS, [F_EM_ADDRESS]):
        em_rows += 1
        addr = _first_text(rec.get("fields", {}).get(F_EM_ADDRESS)).strip().lower()
        if addr and "@" in addr:
            email_by_id[rec["id"]] = addr

    # Join them: each person gets the addresses their own row points at.
    by_name: dict[str, set[str]] = {}
    linked = 0
    for ab_id, eids in ab_email_ids.items():
        name = people[ab_id].strip().lower()
        for eid in eids:
            addr = email_by_id.get(eid)
            if addr:
                by_name.setdefault(name, set()).add(addr)
                linked += 1

    # An address on several people's rows identifies none of them -- but only
    # when they are genuinely different people. The same person is routinely
    # entered more than once ("Jared Nisenfeld" and "Jared Nissenfeld", three
    # Sol Cohen rows for three cities), and treating those duplicates as rivals
    # is what removed working addresses.
    def _key(name: str) -> str:
        """Letters only, sorted -- so word order and punctuation do not matter."""
        return "".join(sorted(re.sub(r"[^a-z]", "", name.lower())))

    def _same_person(a: str, b: str) -> bool:
        import difflib
        ka, kb = _key(a), _key(b)
        if not ka or not kb:
            return False
        if ka == kb:
            return True
        if difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.90:
            return True
        # Shortened forenames: "Ben"/"Benjamin". Only when the other name part
        # matches exactly -- a shared surname alone joins nobody.
        pa = [p for p in re.split(r"[^a-z]+", a.lower()) if len(p) > 1]
        pb = [p for p in re.split(r"[^a-z]+", b.lower()) if len(p) > 1]
        if len(pa) >= 2 and len(pb) >= 2:
            sa, sb = set(pa), set(pb)
            common = sa & sb
            ra, rb = sorted(sa - common), sorted(sb - common)
            if common and len(ra) == 1 and len(rb) == 1:
                short, long_ = sorted((ra[0], rb[0]), key=len)
                if len(short) >= 3 and long_.startswith(short):
                    return True
        return False

    owners: dict[str, set[str]] = {}
    for nm, emails in by_name.items():
        for e in emails:
            owners.setdefault(e, set()).add(nm)

    shared, owner_groups = set(), {}
    for e, who in owners.items():
        if len(who) == 1:
            continue
        groups: list = []
        for n in sorted(who):
            if not any(_same_person(n, g) for g in groups):
                groups.append(n)
        if len(groups) > 1:
            shared.add(e)
            owner_groups[e] = groups

    out = {k: (v - shared) for k, v in by_name.items()}
    out = {k: v for k, v in out.items() if v}

    global LAST_SHARED_DROPS, LAST_SHARED_SAMPLE, LAST_PULL_STATS
    LAST_SHARED_DROPS = len(shared)
    LAST_SHARED_SAMPLE = [
        {"email": e, "people": owner_groups[e]}
        for e in sorted(shared, key=lambda x: -len(owner_groups[x]))[:25]]
    LAST_PULL_STATS = {
        "address_book_rows": ab_rows,
        "address_book_named": len(people),
        "address_book_unnamed": unnamed,
        "address_book_with_email_link": len(ab_email_ids),
        "email_rows": em_rows,
        "email_rows_usable": len(email_by_id),
        "name_to_address_links": linked,
        "people_before_shared_guard": len(by_name),
        "addresses_dropped_as_shared": len(shared),
        "people_after_shared_guard": len(out),
    }
    log.info("address book: %s", LAST_PULL_STATS)
    return out

def _first_text(value) -> str:
    """A cell's text, whatever wrapper Airtable put it in."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and v.get("name"):
                return v["name"]
        return ""
    if isinstance(value, dict):
        return value.get("name") or ""
    return str(value)


def fetch_person_names() -> dict:
    """Map Address Book record id -> person's name.

    Necessary because the REST API returns linked-record fields as bare record
    IDs (["recXXXX"]), NOT as {"id","name"} objects. Without this resolution,
    STH's Address Book link is an opaque ID and no cardholder ever matches.
    """
    out = {}
    for rec in _fetch_table(TABLE_ADDRESS_BOOK,
                            [F_AB_FIRSTNAME, F_AB_LASTNAME, F_AB_LOOKUP]):
        cells = rec.get("fields", {})
        first = (cells.get(F_AB_FIRSTNAME) or "").strip()
        last = (cells.get(F_AB_LASTNAME) or "").strip()
        if first or last:
            name = f"{first} {last}".strip()
        else:
            # fall back to the primary field, "Name (ST) City 12345"
            raw = cells.get(F_AB_LOOKUP) or ""
            name = re.sub(r"\s*\(.*$", "", str(raw)).strip()
        if name:
            out[rec["id"]] = name
    log.info("resolved %d address-book names", len(out))
    return out


def fetch_team_names() -> dict:
    """Map Teams record id -> team name."""
    out = {}
    for rec in _fetch_table(TABLE_TEAMS, [F_TEAM_NAME]):
        name = (rec.get("fields", {}).get(F_TEAM_NAME) or "").strip()
        if name:
            out[rec["id"]] = name
    log.info("resolved %d team names", len(out))
    return out


# The Accounting base, where the team/venue reference lives.
def BASE_ACCOUNTING_FOR() -> str:
    """This company's accounting base, or the deployment default."""
    return _cv.value("airtable_base_accounting") or "appohpGuROXFlSdVZ"


BASE_ACCOUNTING = "appohpGuROXFlSdVZ"
TABLE_TEAMS_VENUES = "tblVGnFhFnsmjWJi4"
# By NAME, not id: this table is maintained by people rather than by the app,
# and a renamed column is a far smaller problem than a field id nobody knows.
F_TV_TEAM = "Team"
F_TV_VENUE = "Venue"
F_TV_NOTES = "Bank Detail Notes"
F_TV_LEAGUE = "League"
# The payment-plan columns on the same Teams / Venues view. Matched by name
# with a few spellings tried, because these were added after the sync was
# written and the exact heading is easier to get wrong than to look up.
F_TV_DAY = ("Day of Month", "Day of month", "Day Of Month", "Charge Day")
F_TV_MONTHS = ("# months", "# Months", "Months", "Number of Months",
               "# of Months")


def fetch_team_aliases() -> list[dict]:
    """Every other name a team's charges arrive under.

    A card statement rarely says "Arizona Diamondbacks". It says the venue --
    CHASE FIELD -- or something the team has arranged with its processor, which
    is what the Bank Detail Notes column records. The bill, meanwhile, names the
    team. So the two sides describe the same purchase with no words in common,
    and Text Overlap scores nothing on a pair that is obviously right.

    Returns one row per team with its aliases already split and cleaned.
    """
    out = []
    def _first_present(f: dict, names) -> str:
        """The first of several spellings that the record actually carries."""
        for n in names:
            if f.get(n) not in (None, ""):
                return f.get(n)
        return ""

    def _as_int(v):
        try:
            return int(float(_first_text(v) if isinstance(v, list) else v))
        except (TypeError, ValueError):
            return None

    def _day_text(v) -> str:
        """The charge day exactly as entered, tidied of stray whitespace.

        A team listing two dates has two real answers, and reducing that to the
        first is a compromise the NUMBER has to make. The grid does not.
        """
        if isinstance(v, list):
            raw = " ".join(str(x).strip() for x in v if str(x).strip())
        else:
            raw = str(v or "").strip()
        return " ".join(raw.split())

    def _as_day(v):
        """The charge day, which is not always a number.

        Airtable holds these as free text because the real answers are not all
        days of the month: "Last Day", "Last Mon", "1st Mon", and rows with two
        options ("1  15"). A plain int() dropped every one of those to nothing,
        so the teams with the most distinctive schedules were the ones that
        synced blank.

        The FIRST option is taken where several are offered -- it is the one the
        charge usually lands on -- and the named ones are encoded as negatives
        so the queue can tell them apart from a numbered day:
            -1  last day of the month
            -2  last <weekday>       -3  first <weekday>
        """
        if isinstance(v, list):
            raw = " ".join(str(x).strip() for x in v if str(x).strip())
        else:
            raw = str(v or "").strip()
        if not raw:
            return None
        low = raw.lower()
        if low.startswith("last day"):
            return -1
        if low.startswith("last"):
            return -2
        if low.startswith("1st") or low.startswith("first"):
            return -3
        # "1  15" or "1, 15": take the first, which is the one it usually
        # charges on, rather than refusing the row outright.
        import re as _re
        m = _re.search(r"\d+", raw)
        if not m:
            return None
        try:
            d = int(m.group(0))
        except ValueError:
            return None
        return d if 1 <= d <= 31 else None

    # No field list: the payment-plan columns were added after this sync was
    # written, and asking for every field is cheaper than getting the heading
    # wrong and silently reading nothing.
    for rec in _fetch_table(TABLE_TEAMS_VENUES, None,
                            base_id=BASE_ACCOUNTING_FOR(), by_field_id=False):
        f = rec.get("fields", {})
        team = str(f.get(F_TV_TEAM) or "").strip()
        if not team:
            continue
        aliases = []
        venue = str(f.get(F_TV_VENUE) or "").strip()
        if venue:
            aliases.append((venue, "venue"))
        # Comma separated, as the column is maintained.
        for part in str(f.get(F_TV_NOTES) or "").split(","):
            part = part.strip()
            if part:
                aliases.append((part, "note"))
        # An alias identical to the team name teaches the matcher nothing.
        aliases = [(a, k) for a, k in aliases if a.lower() != team.lower()]
        # A team with no aliases still matters: the league mapping is what makes
        # its season readable, and classify() needs to know it is a team at all.
        out.append({"team": team, "aliases": aliases,
                    "league": _select_name(f.get(F_TV_LEAGUE))
                              or str(f.get(F_TV_LEAGUE) or "").strip(),
                    "day_of_month": _as_day(_first_present(f, F_TV_DAY)),
                    "day_text": _day_text(_first_present(f, F_TV_DAY)),
                    "plan_months": _as_int(_first_present(f, F_TV_MONTHS))})
    log.info("resolved aliases for %d teams", len(out))
    return out


def _link_ids(value) -> list:
    """Linked-record cell -> list of record ids (handles every shape).

    Over REST a link comes back either as a list (of bare ids or of
    {"id","name"} objects) or, on some field configs, as a dict carrying
    "linkedRecordIds". All three are flattened to a plain id list here.
    """
    if isinstance(value, dict):
        ids = value.get("linkedRecordIds") or value.get("ids") or []
        return [str(i) for i in ids if i]
    if not isinstance(value, list):
        return []
    ids = []
    for v in value:
        if isinstance(v, dict):
            ids.append(v.get("id") or v.get("name") or "")
        else:
            ids.append(str(v))
    return [i for i in ids if i]


# The STH columns a source can name. Section, Row and Seats were offered once
# and are no longer read.
STH_FIELDS = ("lookup", "first", "last", "email", "team", "league", "plan", "total", "card")


def _map_record_setup(rec: dict, seasons: list[str], sth: dict,
                      known_teams=None) -> dict:
    """Normalize a record read by a broker's SETUP (Connectors).

    Statuses come in one of three layouts:
      per_season  one status column per season ("25/26 Status", ...)
      single      one status column, true of every season
      rows        one ROW per season: a season column ("2026-2027", "2027",
                  "26/27"), and a status column -- or none, when being listed
                  for a season is what says the account is live
    The team is read through the team directory, with the league column when
    there is one, so "PHI" in an NBA row is stored as the Philadelphia 76ers.
    """
    from engine.team_directory import resolve_team, normalize_season
    cols = {k: v for k, v in (sth.get("fields") or {}).items() if v and k in STH_FIELDS}
    base = _map_record_mapped(rec, seasons, cols)
    f = rec.get("fields", {}) or {}
    league_raw = _first_text(f.get(cols.get("league", ""))) if cols.get("league") else ""
    t = resolve_team(base["team"], league_raw, known_teams)
    base["team_raw"] = base["team"]
    base["team"] = t["team"]
    base["league"] = t["league"]
    # Unread only when it looks like a TEAM: a venue (a concert at an
    # amphitheatre) is kept as written without being reported.
    from engine.team_directory import is_venue
    base["_team_unread"] = bool(base["team_raw"]) and not t["how"] and not is_venue(base["team_raw"])
    # The holder as written in the broker's column -- or, as on the address
    # book, a first and a last name column put together. The Y&S layout links a
    # person record instead, and the worker resolves that.
    if not base["lookup_name"] and (cols.get("first") or cols.get("last")):
        base["lookup_name"] = " ".join(x for x in (
            _first_text(f.get(cols.get("first", ""))) if cols.get("first") else "",
            _first_text(f.get(cols.get("last", ""))) if cols.get("last") else "") if x).strip()
    base["holder"] = base["lookup_name"]
    shape = sth.get("status_shape") or "single"
    if shape == "per_season":
        base["statuses"] = {s: _cell_text(f.get(col)) for s, col in
                            (sth.get("status_by_season") or {}).items()
                            if col and s in seasons and _cell_text(f.get(col))}
    elif shape == "rows":
        raw = f.get(sth.get("season_col") or "")
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        # WHAT A SINGLE YEAR MEANS: the source's own choice when it says
        # "ends"; otherwise this broker's convention for the league (Broker
        # Data > Renewal Years); otherwise the built-in reading.
        _conv = "auto"
        if sth.get("single_year") == "ends":
            _conv = "ends"
        else:
            try:
                _conv = _cv.season_conventions().get((t["league"] or "").upper(), "auto")
            except Exception:                           # noqa: BLE001
                _conv = "auto"
        label = normalize_season(raw if isinstance(raw, (int, float)) else _cell_text(raw),
                                 t["league"], _conv)
        base["season_raw"] = _cell_text(raw)
        base["season"] = label
        st = (_cell_text(f.get(sth["status_col"])) if sth.get("status_col") else "Active")
        if not label:
            base["_skip"] = "season"
            base["statuses"] = {}
        elif label not in seasons:
            # KEPT, with its own season's status: every record is kept, whether
            # or not the engine tracks that season.
            base["_skip"] = "other"
            base["statuses"] = {label: st} if st else {}
        else:
            base["statuses"] = {label: st} if st else {}
    else:
        st = _cell_text(f.get(sth.get("status_single") or ""))
        base["statuses"] = {s: st for s in seasons} if st else {}
    return base


# Record ids the last read skipped by a source's ignore setting -- removed
# from the mirror if an earlier sync stored them.
LAST_HAL_IGNORED_IDS: set = set()


def setup_sth_columns(sth: dict) -> list[str]:
    """Every column a broker's STH source reads."""
    cols = [c for k, c in (sth.get("fields") or {}).items() if c and k in STH_FIELDS]
    shape = sth.get("status_shape") or "single"
    if shape == "per_season":
        cols += [c for c in (sth.get("status_by_season") or {}).values() if c]
    elif shape == "rows":
        cols += [c for c in (sth.get("season_col"), sth.get("status_col")) if c]
    elif sth.get("status_single"):
        cols.append(sth["status_single"])
    cols += [r["column"] for r in ignore_rules(sth)]
    return list(dict.fromkeys(cols))


# IGNORE ROWS WHERE: as many conditions as needed, any one of which skips a
# row, with Airtable's own operators. Text: is, is not, is any of, is none of,
# is empty, is not empty. Amounts: = != > < >= <=.
IGNORE_TEXT_OPS = {"is": "is", "is_not": "is not", "any_of": "is any of", "none_of": "is none of",
                   "empty": "is empty", "not_empty": "is not empty"}
IGNORE_NUM_OPS = {"eq": "=", "neq": "\u2260", "gt": ">", "lt": "<", "gte": "\u2265", "lte": "\u2264"}
_NO_VALUE_OPS = ("empty", "not_empty")


def ignore_rules(src: dict) -> list:
    """The source's "Ignore rows where" conditions: [{column, op, value}]. A
    condition written before operators existed is "is"."""
    out = []
    for r in src.get("ignore") or []:
        if not isinstance(r, dict):
            continue
        col = str(r.get("column") or "").strip()
        op = str(r.get("op") or "is").strip()
        if op not in IGNORE_TEXT_OPS and op not in IGNORE_NUM_OPS:
            op = "is"
        val = r.get("value")
        val = ", ".join(str(x) for x in val) if isinstance(val, list) else str(val if val is not None else "").strip()
        if not col or (op not in _NO_VALUE_OPS and not val):
            continue
        out.append({"column": col, "op": op, "value": val})
    return out


def _num(x):
    try:
        t = str(x).replace("$", "").replace(",", "").strip()
        if t.startswith("(") and t.endswith(")"):
            t = "-" + t[1:-1]
        return float(t)
    except (TypeError, ValueError):
        return None


def _ignore_hit(rule: dict, v) -> bool:
    vals = [x for x in (v if isinstance(v, list) else [v]) if x is not None]
    texts = [_cell_text(x).strip().lower() for x in vals]
    texts = [t for t in texts if t != ""]
    op, want = rule["op"], rule["value"]
    if op == "empty":
        return not texts
    if op == "not_empty":
        return bool(texts)
    if op in IGNORE_NUM_OPS:
        w = _num(want)
        nums = [n for n in (_num(t) for t in texts) if n is not None]
        if w is None or not nums:
            return False
        n = nums[0]
        return {"eq": abs(n - w) < 0.005, "neq": abs(n - w) >= 0.005, "gt": n > w, "lt": n < w,
                "gte": n >= w - 0.005, "lte": n <= w + 0.005}[op]
    many = [x.strip().lower() for x in str(want).split(",") if x.strip()]
    one = str(want).strip().lower()
    if op == "is":
        return one in texts
    if op == "is_not":
        return one not in texts
    if op == "any_of":
        return any(t in many for t in texts)
    if op == "none_of":
        return not any(t in many for t in texts)
    return False


def ignored_row(src: dict, fields: dict) -> bool:
    """Whether a row meets ANY of the source's ignore conditions (text ignores
    case and spaces; a list cell matches on any of its items)."""
    for r in ignore_rules(src):
        if _ignore_hit(r, fields.get(r["column"])):
            return True
    return False


def _map_record_mapped(rec: dict, seasons: list[str], fmap: dict) -> dict:
    """Normalize a record from a MAPPED base, read by column name.

    A mapped base is another broker's, so nothing here can assume the default
    base's field ids, its linked-record structure, or its per-season status
    columns. Everything comes back as whatever that column holds: usually plain
    text, sometimes a linked record, so both shapes are unwrapped.

    Statuses: a mapped broker has one status column, not one per season, so its
    value is recorded against every season being mirrored. That is honest --
    the record is live or it is not -- and it keeps a mapped broker out of the
    per-season machinery built for the default base.
    """
    f = rec.get("fields", {}) or {}

    def get(field: str) -> str:
        col = fmap.get(field)
        if not col:
            return ""
        v = f.get(col)
        if isinstance(v, list) and v:
            first = v[0]
            return (first.get("name", "") if isinstance(first, dict)
                    else str(first))
        if isinstance(v, dict):
            return v.get("name", "") or ""
        return "" if v is None else str(v)

    status = get("status")
    total = get("total").replace("$", "").replace(",", "").strip()
    try:
        total_val = float(total) if total else 0
    except ValueError:
        total_val = 0
    return {
        "record_id": rec["id"],
        "lookup_name": get("lookup"),
        # No linked-record ids: these are plain values in the broker's own
        # columns, so the worker's id-to-name maps have nothing to resolve.
        "address_book_ids": [],
        "team_ids": [],
        "address_book": get("email") or get("lookup"),
        "team": get("team"),
        # STATED, not dug out of the lookup name. This is the whole point of
        # the mapping: most brokers keep the buyer address in a column.
        "email": get("email"),
        "plan_type": get("plan"),
        "total": total_val,
        "card_program": get("card"),
        # Nothing to read: a mapped base has no payment-plan flag of its own
        # yet, and guessing one from the plan text would invent a fact.
        "has_payment_plan": False,
        "statuses": {s: status for s in seasons} if status else {},
    }


def _map_record(rec: dict, seasons: list[str]) -> dict:
    fmap = _cv.field_map()
    if fmap:
        return _map_record_mapped(rec, seasons, fmap)
    statuses = {}
    for s in seasons:
        fid = STATUS_FIELDS.get(s)
        if fid:
            name = _select_name(_cell(rec, fid))
            if name:
                statuses[s] = name
    return {
        "record_id": rec["id"],
        "lookup_name": _cell(rec, F_LOOKUP) or "",
        # The default base keeps the address inside the lookup name, so there
        # is no separate column to read -- stated as empty rather than absent,
        # so both paths return the same shape.
        "email": "",
        # raw ids; resolved to names by the worker using the lookup maps
        "address_book_ids": _link_ids(_cell(rec, F_ADDRESS_BOOK)),
        "team_ids": _link_ids(_cell(rec, F_TEAM)),
        "address_book": _linked_name(_cell(rec, F_ADDRESS_BOOK)),
        "team": _linked_name(_cell(rec, F_TEAM)),
        "plan_type": _choice_text(_cell(rec, F_FULL_PARTIAL)),
        "total": _cell(rec, F_TOTAL) or 0,
        "card_program": _cell(rec, F_CARD) or "",
        "has_payment_plan": bool(_cell(rec, F_PAYMENT_PLAN)),
        "statuses": statuses,
    }
