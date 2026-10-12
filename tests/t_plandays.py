"""A payment plan's charge day, every way Payment Plans saves it: a date, the
last day, and the named weekdays ("second-tue", "last-fri"). A named weekday
used to crash the whole Bank Feed with "invalid literal for int()"."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET", "testsecret_testsecret_testsecret")

from app.review import _day_label, _tie_date, _tie_days  # noqa: E402

ok = True


def check(label, cond, got=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond or got == "" else f"  -> {got}"))


check("labels: second-tue, last-fri, 15, last",
      [_day_label(x) for x in ("second-tue", "last-fri", "15", "last")] == ["2nd Tu", "Last F", "15th", "Last day"],
      [_day_label(x) for x in ("second-tue", "last-fri", "15", "last")])
check("nothing unreadable crashes", _day_label("someday") == "" and _tie_days("someday", "") == [])
# October 2026: Tuesdays 6, 13, 20, 27; Fridays 2, 9, 16, 23, 30.
check("second Tuesday: the 13th ties out", _tie_date(date(2026, 10, 13), "second-tue") is True)
check("…the first Tuesday does not", _tie_date(date(2026, 10, 6), "second-tue") is False)
check("last Friday: the 30th ties out", _tie_date(date(2026, 10, 30), "last-fri") is True)
check("fourth Thursday: the 22nd ties out", _tie_date(date(2026, 10, 22), "fourth-thu") is True)
check("a date still works: the 15th", _tie_date(date(2026, 10, 15), "15") is True)
print("\nALL CHECKS PASSED" if ok else "\n*** FAILURES ***")
sys.exit(0 if ok else 1)
