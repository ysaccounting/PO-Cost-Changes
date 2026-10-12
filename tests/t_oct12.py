"""The Oct 12 round: the one-time cleanup of For Review before 9/1/2026;
Bank Feed Rules in the order dragged; a profile typed in fills the same card's
blank ones; statements of cards disconnected earlier are dropped; Engine Words
for Renewed / Not Renewed; each renewal year's seat rows; the company league
picker in the Dashboard's order; Custom Notes in line descriptions; Match
Transfer never leaves a card payment half-posted; and the page changes."""
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./t_oct12.db?timeout=30")
os.environ.setdefault("JWT_SECRET", "testsecret_testsecret_testsecret")
os.environ.setdefault("IN_APP_SCHEDULER", "false")
os.environ.setdefault("SEED_ADMIN_USER", "a")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw-test-123456")
os.environ.setdefault("REQUIRE_TWOFA", "false")

from fastapi.testclient import TestClient

from app.api import app
from app.db import SessionLocal
from app.models_db import (AppSettingRow, ChargeRow, CompanyRow, LearnedRuleRow, PlaidItemRow,
                           SourceAccountRow)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "app/static/app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "app/static/app.css"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "app/static/index.html"), encoding="utf-8").read()
ok = True


def check(label, cond, got=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond or got == "" else f"  -> {str(got)[:300]}"))


def charge(cid, co, src, d, status="for_review", **kw):
    return ChargeRow(charge_id=cid, source_txn_id=cid.split(":")[-1], company=co, source=src,
                     amount=Decimal(kw.pop("amount", "10")), txn_date=d, merchant=kw.pop("merchant", "SHOP"),
                     memo="", status=status, **kw)


with TestClient(app) as c:
    db = SessionLocal()
    for co in ("A1", "B1"):
        db.merge(CompanyRow(id=co, name=co, active=True))
    db.commit()
    A = {"Authorization": "Bearer " + c.post(
        "/auth/login", data={"username": "a", "password": "pw-test-123456"}).json()["access_token"]}

    # ONE-TIME CLEANUP: run the migration's upgrade against rows put back.
    db.add_all([charge("A1:x:old", "A1", "x", date(2026, 8, 20)),
                charge("B1:y:old", "B1", "y", date(2026, 8, 31)),
                charge("A1:x:new", "A1", "x", date(2026, 9, 1)),
                charge("A1:x:cat", "A1", "x", date(2026, 8, 1), status="categorized"),
                charge("A1:x:posted", "A1", "x", date(2026, 8, 1), qbo_txn_id="Q1")])
    db.commit()
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location(
        "mig", os.path.join(ROOT, "migrations/versions/c1e0a9f7b312_clear_old_for_review.py"))
    mig = importlib.util.module_from_spec(spec); spec.loader.exec_module(mig)
    from app.db import engine
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
    db.expire_all()
    left = {r.charge_id for r in db.query(ChargeRow).filter(ChargeRow.charge_id.like("%:x:%") | ChargeRow.charge_id.like("%:y:%")).all()}
    check("cleanup: For Review before 9/1/2026 removed in every company",
          "A1:x:old" not in left and "B1:y:old" not in left, left)
    check("…9/1 itself, categorized and posted rows are kept",
          {"A1:x:new", "A1:x:cat", "A1:x:posted"} <= left, left)
    from app.models_db import DeletedTxnRow
    check("…and a pull will not bring them back", db.get(DeletedTxnRow, ("A1", "x", "old")) is not None)

    # BANK FEED RULES IN THE ORDER DRAGGED.
    from app.learning import rules_for
    for rid, t in (("A1|r1", 1), ("A1|r2", 2), ("A1|r3", 3)):
        db.merge(LearnedRuleRow(id=rid, company="A1", name=rid, detail_op="contains", detail_value=rid,
                                updated_at=datetime(2026, 10, t)))
    db.commit()
    r = c.post("/rules/order", headers=A, data={"company": "A1", "ids": json.dumps(["A1|r3", "A1|r1", "A1|r2"])})
    db.expire_all()
    check("rules are tried in the order dragged", r.status_code == 200
          and [x.id for x in rules_for(db, "A1")][:3] == ["A1|r3", "A1|r1", "A1|r2"], [x.id for x in rules_for(db, "A1")])
    lst = [x["id"] for x in c.get("/rules?company=A1", headers=A).json()["rules"]][:3]
    check("…and listed in that order", lst == ["A1|r3", "A1|r1", "A1|r2"], lst)

    # A PROFILE TYPED IN fills the same card's blanks.
    db.add_all([charge("A1:dv:1", "A1", "divvy", date(2026, 10, 1), card_last4="4699", cardholder_name=""),
                charge("A1:dv:2", "A1", "divvy", date(2026, 10, 2), card_last4="4699", cardholder_name=""),
                charge("A1:dv:3", "A1", "divvy", date(2026, 10, 3), card_last4="1111", cardholder_name="")])
    db.commit()
    c.post("/queue/edit", headers=A, data={"charge_id": "A1:dv:1", "profile": "Pat Doe"})
    db.expire_all()
    check("a profile typed in fills the same card's blank profiles",
          db.get(ChargeRow, "A1:dv:2").cardholder_name == "Pat Doe"
          and db.get(ChargeRow, "A1:dv:3").cardholder_name == "", (db.get(ChargeRow, "A1:dv:2").cardholder_name,))

    # STATEMENTS of a card disconnected before clearing existed.
    from app import plaid_statements as ps
    db.merge(SourceAccountRow(id="A1:amex", company="A1", source="amex", nickname="Amex"))
    db.merge(PlaidItemRow(id="it9", company="A1", source="amex", access_token="t", active=False))
    ps._put(db, "A1", "amex", {"date": "2026-09-15", "balance": 100.0})
    db.commit()
    progs = c.get("/accounts/mapping?company=A1", headers=A).json()["programs"]
    p = next((x for x in progs if x["source"] == "amex"), {})
    db.expire_all()
    check("a disconnected card's old statement is no longer shown, and is cleared",
          not p.get("statement_date") and ps.get(db, "A1", "amex") == {}, p.get("statement_date"))

    # ENGINE WORDS (now the Dashboard's Engine Notation): every company's.
    st = c.get("/renewal-statuses", headers=A).json()["statuses"]
    st[0]["words"] = ["Active", "Renewed"]; st[2]["words"] = ["Canceled", "Not Renewed"]
    r0 = c.post("/renewal-statuses", headers=A, data={"statuses": json.dumps(st)})
    r = c.post("/setup/sth-status-words", headers=A, data={
        "company": "A1", "words": json.dumps({"active": ["Yes"], "dead": []})})
    j = c.get("/setup/sth-status-words?company=B1", headers=A).json()
    check("Engine Words are saved for every company", r0.status_code == 200 and r.status_code == 200
          and "Renewed" in j["engine_words"]["active"], j)
    from app.company_vars import status_words
    from engine.hal import status_kind
    w = status_words(db, "A1")
    check("…and decide a status: Renewed is renewed, Yes (this company's) too, Not Renewed is not",
          status_kind("renewed", w) == "active" and status_kind("yes", w) == "active"
          and status_kind("Not Renewed", w) == "dead" and status_kind("yes", status_words(db, "B1")) == "", w)

    # THE COMPANY LEAGUE PICKER carries the Dashboard's order.
    db.merge(AppSettingRow(key="league_order", value=json.dumps(["NHL", "MLB"]))); db.commit()
    check("the company Teams grid gets the Dashboard's league order",
          c.get("/teams/company?company=A1", headers=A).json().get("league_order") == ["NHL", "MLB"])

    # CUSTOM NOTES in line descriptions.
    from types import SimpleNamespace as NS
    from app.integrations import qbo_write as qw
    ch = NS(memo="TM *BULLS", merchant="", custom_note="Playoffs")
    check("a note goes after the memo in brackets (lines use the same text)",
          qw.charge_memo_text(ch) == "TM *BULLS (Playoffs)")
    check("…and split lines get it too", "_line_desc(desc)" in open(os.path.join(ROOT, "app/integrations/qbo_write.py")).read())

# THE PAGE.
check("1: row Saves stay on screen, gray until there is something to save",
      '.ruleSave[style*="visibility:hidden"]' in CSS and "pointer-events:none" in CSS)
check("2: each renewal year's line carries its own seat rows", "y.rows > 1" in JS)
check("3: removing a profiles row asks with no subtext and saves at once",
      "askConfirm('Remove this row?', '', 'Remove', 'Cancel')) return;\n    PF.rows.splice(Number(el.closest('tr').dataset.i), 1); PF.dirty = true; PF.sel = new Set(); await saveAll();" in JS)
check("4: red x's are drawn, centered", 'M2 2l6 6M8 2L2 8' in JS)
check("5: the rate history date picker is narrow", 'aria-label="Go to date" style="font-size:12px;padding:2px 6px;width:150px"' in JS)
check("6: Ignore Rows' column picker leaves room for the value", 'class="atIgnCol" data-i="${i}" data-n="${n}" style="flex:0 1 40%' in JS)
check("7: Force Match's total sits on the line of Apply oldest first", 'align-items:baseline;gap:18px' in HTML)
check("8: Change Amount is wider, with the memo on its own line",
      'id="billamt" style="max-width:600px;width:600px"' in HTML and "['Memo', _b.memo]" in JS)
check("9: the red choice is on the left in confirmations", "if (goRed) go.parentElement.insertBefore(go, back);" in JS)
check("14: a company plan saved empty redraws at once", "box.dataset.own === '1' && _empty" in JS)
check("16: Status Words is a grid (now Renewal Status Words: Engine Notation / Your Notation)",
      "Engine Notation</th>" in JS and "Your Notation</th>" in JS)
check("19: Credit Card Profiles and Bank Feed Rules reorder by a drag handle",
      "wireRowDrag(box.querySelector('table.gridtable'), 'tr.pfRow'" in JS
      and "wireRowDrag(box.querySelector('table.rulegrid'), 'tr.ruleDrag'" in JS and "pfUpRow" not in JS)
check("20: row Clear buttons are red-outlined", 'id="ruleClear" title="Clear this row"\n                  style="background:#fff;color:var(--bad)' in JS)

check("23: the Dashboard's Audit Log card ends where its columns end",
      "#auditGrid.audit-dash #auditRows{border-radius:8px;width:max-content;max-width:100%}" in CSS)
check("24: the Bank Feed's loading ring never shows over another screen",
      "!document.body.classList.contains('screen-open');\n    if (empty) gridBusyStart('Loading Bank Feed');" in JS
      and "LEAVING THE BANK FEED takes its loading ring with it" in JS)
check("25B: a shared rule's company ticks wait for the row's Save", "btn.dataset.pending = [...cur].join('|');" in JS
      and "_cosBtn.dataset.pending !== undefined" in JS)
check("15: the account menu is as wide as its words", ".avatarmenu button{width:auto !important" in CSS)
check("27: Add Connection is narrower; bottom buttons are centered in every window",
      "max-width:400px;width:92vw" in JS and 'display:flex;justify-content:flex-end;' not in JS)
check("28: connectors already added are gray but readable", "color:#6b7280;background:#f3f4f6;cursor:not-allowed" in JS)
check("29: the WEX mark is drawn whole", 'const WEX_LOGO = `<svg viewBox="0 0 34 20"' in JS)
check("30: Add New Connector lists icons and is only as wide as its items",
      "_ico(AT_LOGO) + 'Airtable'" in JS and "_ico(WEX_LOGO)" in JS and "width:max-content" in JS)

print("\nALL CHECKS PASSED" if ok else "\n*** FAILURES ***")
sys.exit(0 if ok else 1)
