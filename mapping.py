"""Company mapping from the Master Mapping List.

The master list has a row per TicketVault company. The 'QBO Company' column
gives the canonical name to use in QuickBooks output. We only map rows where
QBO Company is populated — others (e.g. "-Fee" entries, "Not Found",
"Due from/to ...") get passed through unchanged.

The master file lives at the path given by MASTER_MAPPING_PATH env var,
defaulting to ./data/Master_Mapping_List.xlsx. Loaded once at startup.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data") / "Master_Mapping_List.xlsx"

# Header names in the master file. They contain newlines in the source, which
# is annoying — we normalize by stripping whitespace before matching.
QBO_COL = "QBO Company"
TV_COL_SUBSTRING = "TicketVault"  # column header starts with this


def _find_columns(df: pd.DataFrame) -> tuple[str, str]:
    """Locate the QBO and TicketVault columns regardless of newline quirks."""
    cols = {c: c.replace("\n", " ").replace("  ", " ").strip() for c in df.columns}
    qbo = next((orig for orig, clean in cols.items() if clean.startswith(QBO_COL)), None)
    tv  = next((orig for orig, clean in cols.items() if TV_COL_SUBSTRING in clean), None)
    if not qbo or not tv:
        raise ValueError(
            f"Master mapping file missing expected columns. "
            f"Found: {list(df.columns)}"
        )
    return qbo, tv


def load_mapping(path: Path | str | None = None) -> dict[str, str]:
    """Load the TicketVault → QBO company mapping.

    Returns a dict like {"ysa 2": "YS Asher Tickets", "jacks ys": "YS Chase Tickets", ...}
    Keys are LOWERCASED for case-insensitive matching at lookup time.
    Values keep their canonical casing (used directly in output).
    Only rows where both source and target are non-empty are included.
    """
    path = Path(path) if path else Path(os.getenv("MASTER_MAPPING_PATH", DEFAULT_PATH))
    if not path.exists():
        log.warning("Master mapping file not found at %s; using empty mapping.", path)
        return {}

    df = pd.read_excel(path)
    qbo_col, tv_col = _find_columns(df)

    mapping: dict[str, str] = {}
    for _, row in df.iterrows():
        src = row[tv_col]
        dst = row[qbo_col]
        if pd.isna(src) or pd.isna(dst):
            continue
        src_clean = str(src).strip().lower()
        dst_clean = str(dst).strip()
        if not src_clean or not dst_clean:
            continue
        mapping[src_clean] = dst_clean
    log.info("Loaded %d company mappings from %s", len(mapping), path)
    return mapping


@lru_cache(maxsize=1)
def get_mapping() -> dict[str, str]:
    """Cached accessor used by the transform pipeline."""
    return load_mapping()


def reset_cache() -> None:
    """Clear the cache (used in tests)."""
    get_mapping.cache_clear()
