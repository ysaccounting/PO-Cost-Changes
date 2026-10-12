"""A broker's own status words: Ticket Guy's "Yes" is active in Ticket Guy, not
elsewhere; a word cannot mean two things; the company's STH index carries them."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./t_statuswords.db?timeout=30")
os.environ.setdefault("JWT_SECRET", "testsecret_testsecret_testsecret")
os.environ.setdefault("IN_APP_SCHEDULER", "false")
os.environ.setdefault("SEED_ADMIN_USER", "a")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw-test-123456")
os.environ.setdefault("REQUIRE_TWOFA", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models_db import CompanyRow, HalRow  # noqa: E402
from engine.hal import status_kind  # noqa: E402

ok = True


def check(label, cond, got=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond or got == "" else f"  -> {got}"))


check("built-in: Active is active, whatever the capitals", status_kind(" active ") == "active")
check("built-in: Canceled is not active", status_kind("Canceled") == "dead")
check("Yes is unknown without the broker's words", status_kind("Yes") == "")
check("…and active with them", status_kind("yes", {"active": ["Yes"]}) == "active")

with TestClient(app) as c:
    db = SessionLocal()
    for co in ("TG", "YS"):
        db.merge(CompanyRow(id=co, name=co, active=True))
    db.add(HalRow(company="TG", record_id="r1", holder_name="Pat Doe", team="Philadelphia 76ers",
                  statuses_json=json.dumps({"26/27": "Yes"})))
    db.commit()
    A = {"Authorization": "Bearer " + c.post("/auth/login", data={"username": "a", "password": "pw-test-123456"}).json()["access_token"]}
    r = c.post("/setup/sth-status-words", headers=A, data={"company": "TG", "words": json.dumps(
        {"active": ["Yes", "Renewed"], "dead": ["No"]})})
    check("Ticket Guy's words save", r.status_code == 200, r.text[:200])
    r = c.post("/setup/sth-status-words", headers=A, data={"company": "TG", "words": json.dumps(
        {"active": ["Yes"], "dead": ["yes"]})})
    check("a word in two lists is refused, in words", r.status_code == 400 and "two statuses" in r.text, r.text[:200])
    got = c.get("/setup/sth-status-words?company=TG", headers=A).json()
    check("…and read back, with the built-ins beside them",
          sorted(got["notation"]["active"]) == ["Renewed", "Yes"] and "Active" in got["engine_words"]["active"], got)
    from app.hal_cache import get_index, invalidate
    invalidate()
    idx = get_index(db, "TG")
    check("Ticket Guy's STH index carries its words",
          idx is not None and "Yes" in idx.status_words.get("active", []), getattr(idx, "status_words", None))
    check("Y&S has none of them", c.get("/setup/sth-status-words?company=YS", headers=A).json()["words"]["active"] == [])

    # RENEWAL YEARS: Ticket Guy writes the 2026-27 NBA season as "2027".
    r = c.post("/setup/season-conventions", headers=A, data={"company": "TG", "conventions": json.dumps({"nba": "2027"})})
    check("renewal years save", r.status_code == 200 and r.json()["notations"] == {"NBA": "2027"}, r.text[:200])
    from app import company_vars as _cvx
    from app.integrations import airtable as _atx
    SRC = {"fields": {"lookup": "H", "team": "T", "league": "L"}, "status_shape": "rows", "season_col": "S"}
    rec = {"id": "x", "fields": {"H": "Pat", "T": "PHI", "L": "NBA", "S": "2027"}}
    with _cvx.for_company(db, "TG"):
        tg = _atx._map_record_setup(rec, ["26/27", "27/28"], SRC)
    with _cvx.for_company(db, "YS"):
        ys = _atx._map_record_setup(rec, ["26/27", "27/28"], SRC)
    check("…in Ticket Guy an NBA 2027 is the 26/27 season", tg["season"] == "26/27", tg["season"])
    check("…in Y&S, built in, it is 27/28", ys["season"] == "27/28", ys["season"])

print("\nALL CHECKS PASSED" if ok else "\n*** FAILURES ***")
sys.exit(0 if ok else 1)
