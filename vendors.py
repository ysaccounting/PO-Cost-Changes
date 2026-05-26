"""Open-vendors lookup from data/Vendors_Open.xlsx.

A row's expense pair uses "Due from Vendors - Open" as the offset Category
when the Vendor is in this list. Anything else uses the default "<Vendor> (TC)".

Membership is case-insensitive — we lowercase both the lookup set and the
input when checking.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data") / "Vendors_Open.xlsx"


def load_open_vendors(path: Path | str | None = None) -> set[str]:
    """Load the set of "open" vendor names, lowercased for case-insensitive
    matching at lookup time."""
    path = Path(path) if path else Path(os.getenv("OPEN_VENDORS_PATH", DEFAULT_PATH))
    if not path.exists():
        log.warning("Open-vendors file not found at %s; using empty set.", path)
        return set()

    df = pd.read_excel(path)
    if df.shape[1] == 0:
        return set()
    col = df.columns[0]  # single-column file
    vendors = {
        str(v).strip().lower()
        for v in df[col].dropna().tolist()
        if str(v).strip()
    }
    log.info("Loaded %d open vendors from %s", len(vendors), path)
    return vendors


@lru_cache(maxsize=1)
def get_open_vendors() -> set[str]:
    """Cached accessor used by the processor."""
    return load_open_vendors()


def reset_cache() -> None:
    """Clear the cache (used in tests)."""
    get_open_vendors.cache_clear()


def offset_category(vendor: str, open_vendors: set[str] | None = None) -> str:
    """Return the offset Category for an expense pair's second line.

    - If `vendor` is in the open-vendors list → 'Due from Vendors - Open'
    - Otherwise → '<vendor> (TC)'
    """
    if open_vendors is None:
        open_vendors = get_open_vendors()
    v = str(vendor).strip()
    if v.lower() in open_vendors:
        return "Due from Vendors - Open"
    return f"{v} (TC)"
