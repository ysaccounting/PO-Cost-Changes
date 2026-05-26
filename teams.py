"""Major-league teams lookup from data/major_league_teams.xlsx.

Used by the team-rename vendor rule: if a row's Vendor is one of the
TEAM_RENAME_VENDORS (e.g. 'Ticketmaster AM', 'Tickets.com', 'Ballpark')
and Team/Performer is one of these teams, rename the Vendor to the
Team/Performer. Otherwise leave Vendor unchanged.

File schema: two columns 'League', 'Team'. We only care about Team.
Membership is case-insensitive — we lowercase both the lookup set and
the input.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data") / "major_league_teams.xlsx"

# Vendors that get rewritten to the Team/Performer when the performer is
# a known major-league team. Stored lowercased for case-insensitive match.
# To add a vendor to this rule, add its lowercased name here.
TEAM_RENAME_VENDORS: frozenset[str] = frozenset({
    "ticketmaster am",
    "tickets.com",
    "ballpark",
})


def load_teams(path: Path | str | None = None) -> set[str]:
    """Return the set of team names (lowercased) for fast lookup."""
    path = Path(path) if path else Path(os.getenv("TEAMS_PATH", DEFAULT_PATH))
    if not path.exists():
        log.warning("Teams file not found at %s; using empty set.", path)
        return set()

    df = pd.read_excel(path)
    if "Team" not in df.columns:
        log.warning("Teams file at %s missing 'Team' column; using empty set.", path)
        return set()

    teams = {
        str(t).strip().lower()
        for t in df["Team"].dropna().tolist()
        if str(t).strip()
    }
    log.info("Loaded %d teams from %s", len(teams), path)
    return teams


@lru_cache(maxsize=1)
def get_teams() -> set[str]:
    """Cached accessor used by the processor."""
    return load_teams()


def reset_cache() -> None:
    """Clear the cache (used in tests)."""
    get_teams.cache_clear()


def rename_team_vendor(
    vendor: str | None,
    team_performer: str | None,
    teams: set[str] | None = None,
) -> str | None:
    """Apply the team-vendor rename rule:
       - If Vendor is in TEAM_RENAME_VENDORS (e.g. 'Ticketmaster AM',
         'Tickets.com', 'Ballpark') AND Team/Performer matches a known
         major-league team → return Team/Performer.
       - Otherwise → return Vendor unchanged.

    Case-insensitive on both the vendor check and the team lookup.
    """
    if vendor is None or (isinstance(vendor, float) and pd.isna(vendor)):
        return vendor
    if str(vendor).strip().lower() not in TEAM_RENAME_VENDORS:
        return vendor
    if team_performer is None or (isinstance(team_performer, float) and pd.isna(team_performer)):
        return vendor
    if teams is None:
        teams = get_teams()
    if str(team_performer).strip().lower() in teams:
        return str(team_performer).strip()
    return vendor


# Backwards-compatible alias for the original single-vendor function.
rename_ticketmaster_am = rename_team_vendor
