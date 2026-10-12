"""The Oct 12 late round: Renewal Status Words (Dashboard and company, with
In Progress and colors); the company settings for the Dashboard's Teams and
Multi-Company rules; Ignore Rows Where with Airtable's operators; Allow
Statements on a card already connected; the day labels; and the page."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./t_oct12c.db?timeout=30")
os.environ.setdefault("JWT_SECRET", "testsecret_testsecret_testsecret")
os.environ.setdefault("IN_APP_SCHEDULER", "false")
os.environ.setdefault("SEED_ADMIN_USER", "a")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw-test-123456")
os.environ.setdefault("REQUIRE_TWOFA", "false")

from fastapi.testclient import TestClient

from app.api import app
from app.db import SessionLocal
from app.models_db import CompanyRow, LearnedRuleRow, PlaidItemRow, TeamAliasRow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "app/static/app.js"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "app/static/app.css"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "app/static/index.html"), encoding="utf-8").read()
ok = True


def check(label, cond, got=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond or got == "" else f"  -> {str(got)[:300]}"))


with TestClient(app) as c:
    db = SessionLocal()
    for co in ("A1", "B1"):
        db.merge(CompanyRow(id=co, name=co, active=True))
    db.commit()
    A = {"Authorization": "Bearer " + c.post(
        "/auth/login", data={"username": "a", "password": "pw-test-123456"}).json()["access_token"]}

    # RENEWAL STATUS WORDS.
    j = c.get("/renewal-statuses", headers=A).json()
    check("three statuses built in: Renewed, In Progress, Not Renewed",
          [s["name"] for s in j["statuses"]] == ["Renewed", "In Progress", "Not Renewed"], j)
    st = j["statuses"]
    st[0]["words"] = ["Active", "Renewed"]; st[0]["color"] = "#00aa00"
    st[1]["words"] = ["Pending"]
    st.append({"key": "", "name": "Waitlist", "kind": "progress", "words": ["Wait List"], "color": "#123456"})
    r = c.post("/renewal-statuses", headers=A, data={"statuses": json.dumps(st)})
    check("the Dashboard sets the Engine Notation, colors and adds a status", r.status_code == 200
          and r.json()["statuses"][3]["name"] == "Waitlist", r.text)
    bad = [dict(x) for x in r.json()["statuses"]]; bad[2]["words"] = ["Renewed"]
    check("…a word under two statuses is refused",
          c.post("/renewal-statuses", headers=A, data={"statuses": json.dumps(bad)}).status_code == 400)
    check("…and a built-in status can't be removed",
          c.post("/renewal-statuses", headers=A, data={"statuses": json.dumps(r.json()["statuses"][1:])}).status_code == 400)
    r = c.post("/setup/sth-status-words", headers=A, data={
        "company": "A1", "words": json.dumps({"active": ["Yes"], "dead": ["Nope"]}),
        "custom": json.dumps([{"name": "Hold", "kind": "progress", "words": ["On Hold"], "color": "#abcdef"}])})
    check("a company adds its notation and a status of its own", r.status_code == 200
          and r.json()["notation"]["active"] == ["Yes"] and r.json()["custom"][0]["name"] == "Hold", r.text)
    from app.company_vars import status_words
    from engine.hal import status_kind
    w = status_words(db, "A1")
    check("…and they decide a status: Renewed and Yes renewed, Pending, Wait List and On Hold in progress, Nope not",
          status_kind("renewed", w) == "active" and status_kind("yes", w) == "active"
          and status_kind("pending", w) == "progress" and status_kind("wait list", w) == "progress"
          and status_kind("on hold", w) == "progress" and status_kind("nope", w) == "dead"
          and status_kind("yes", status_words(db, "B1")) == "", w)
    cols = c.get("/setup/sth-status-words?company=A1", headers=A).json()["colors"]
    check("…and each word carries its status's pill color",
          cols.get("yes") == "#00aa00" and cols.get("on hold") == "#abcdef" and cols.get("wait list") == "#123456", cols)

    # COMPANY SETTINGS.
    c.post("/setup/settings", headers=A, data={"company": "A1", "own_teams_only": "true"})
    c.post("/setup/settings", headers=A, data={"company": "A1", "no_shared_rules": "true"})
    s = c.get("/setup/settings?company=A1", headers=A).json()
    check("the two new company settings are saved", s["own_teams_only"] and s["no_shared_rules"], s)
    db.merge(TeamAliasRow(id="bulls-uc", alias="united center", team="Chicago Bulls", kind="venue", league="NBA"))
    db.commit()
    from app.review import team_claim_pairs
    pa = dict(team_claim_pairs(db, company="A1")); pb = dict(team_claim_pairs(db, company="B1"))
    check("ignoring the Dashboard's Teams: its venues no longer name a team for that company only",
          "united center" not in pa and pb.get("united center") == "Chicago Bulls", (len(pa), pb.get("united center")))
    from app.learning import SHARED, set_shared_companies, rules_for
    from datetime import datetime
    db.merge(LearnedRuleRow(id="S|1", company=SHARED, name="shared", detail_op="contains", detail_value="X",
                            updated_at=datetime(2026, 10, 1)))
    set_shared_companies(db, "S|1", ["A1", "B1"]); db.commit()
    check("an opted-out company gets no Multi-Company rules, even when ticked",
          not [r for r in rules_for(db, "A1") if r.id == "S|1"], [r.id for r in rules_for(db, "A1")])
    lst = [x["id"] for x in c.get("/rules?company=A1", headers=A).json()["rules"]]
    check("…and none are listed there", "S|1" not in lst, lst)

    # ALLOW STATEMENTS on a card connected through the same login.
    db.merge(PlaidItemRow(id="itm", company="A1", source="amex", access_token="tok", active=True))
    db.merge(PlaidItemRow(id="old:acct2", company="A1", source="qbo:349", access_token="tok", active=True))
    db.commit()
    import app.integrations.plaid_api as _pa
    _pa.get_accounts = lambda t: {"accounts": [{"account_id": "acct2", "mask": "1004", "name": "Amex"}]}
    from app.models_db import SourceAccountRow
    db.merge(SourceAccountRow(id="A1:qbo:349", company="A1", source="qbo:349", nickname="Amex 1004")); db.commit()
    r = c.post("/plaid/attach", headers=A, data={"item_id": "itm", "company": "A1",
                                                  "links": json.dumps([{"account_id": "acct2", "source": "qbo:349"}])})
    check("a card on the same login is not 'already connected to another bank login'",
          r.status_code == 200 and not r.json()["skipped"], r.text)
    check("Update mode (Reconnect / Allow Statements) no longer exchanges and attaches again",
          "if (tok.mode === 'update') {" in JS and "/plaid/statements/refresh" in JS)

# IGNORE ROWS WHERE.
from app.integrations.airtable import ignored_row
src = {"ignore": [{"column": "Type", "op": "any_of", "value": "Parking, Suite"},
                  {"column": "Cost", "op": "lt", "value": "10"},
                  {"column": "Email", "op": "empty", "value": ""},
                  {"column": "Old", "value": "X"}]}
check("is any of", ignored_row(src, {"Type": "parking", "Email": "a@b", "Cost": "50"}))
check("less than an amount", ignored_row(src, {"Type": "Seats", "Email": "a@b", "Cost": "$9.50"}))
check("is empty", ignored_row(src, {"Type": "Seats", "Cost": "50"}))
check("a condition from before operators is 'is'", ignored_row(src, {"Type": "Seats", "Email": "a@b", "Cost": "50", "Old": "x"}))
check("a row meeting none is kept", not ignored_row(src, {"Type": "Seats", "Email": "a@b", "Cost": "50"}))
check("is none of / is not / >=", ignored_row({"ignore": [{"column": "T", "op": "none_of", "value": "A, B"}]}, {"T": "C"})
      and not ignored_row({"ignore": [{"column": "T", "op": "is_not", "value": "A"}]}, {"T": "a"})
      and ignored_row({"ignore": [{"column": "N", "op": "gte", "value": "100"}]}, {"N": "100.00"}))

# DAY LABELS: as the Payment Plans window writes them.
from app.review import _day_label
check("Last M / Last Tu, 15th, Last day", [_day_label(x) for x in ("last-mon", "last-tue", "15", "last", "first-mon")]
      == ["Last M", "Last Tu", "15th", "Last day", "1st M"])

# THE PAGE.
check("5: removing a team asks with no subtext", "askConfirm(_ov ? 'Remove this override?' : 'Remove this team?', ''," in JS)
check("7: every grid Save rests gray: tracked rows, and any Save hidden until a change",
      "function grayRowSaves(root)" in JS and "button.isIdle" in CSS and '.graySave[style*="visibility:hidden"]' in CSS)
check("8: the quick search waits for a pause in typing and keeps what is typed",
      "t = setTimeout(run, 800);" in JS and "QS_TYPING" in JS)
check("9: weekdays read Last M, as 1st M", "out.push([`${nv}-${dv}`, `${nl} ${ds}`])" in JS)
check("10: Ignore Rows Where takes many conditions and Airtable's operators",
      "Add Condition" in JS and "['none_of', 'is none of']" in JS and "['gte', '\\u2265 (amount)']" in JS)
check("11: Change Amount is centered", "#billamt{text-align:center}" in CSS)
check("13: Renewal Years' line is centered over its grid", "display:inline-block;max-width:100%\">' :" in JS)
check("14/15: Renewal Status Words: tab renamed, row Saves, Add / Clear, colors, on the Dashboard after Payment Plans",
      'data-bk="words">Renewal Status Words' in HTML
      and HTML.index('data-tl="statuses"') > HTML.index('data-tl="plans"')
      and "How you notate your season ticket renewal statuses." in JS and 'class="rsAdd"' in JS
      and "Your Notation</th>" in JS and "Engine Notation</th>" in JS and "STH_COLORS[t]" in JS)
check("17: Bank Feed Rules number their rows", "data-col=\"rn\" style=\"width:36px\"" in JS)
check("18: Credit Card Profiles: a select column and Batch Actions",
      'class="pfPick"' in JS and 'id="pfBatch"' in JS and "pfbGo" in JS)

print("\nALL CHECKS PASSED" if ok else "\n*** FAILURES ***")
sys.exit(0 if ok else 1)
