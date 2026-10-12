"""Card payment and checking withdrawal BOTH still in For Review: offered on
both rows, Match posts the payment to QuickBooks once and clears both. A
withdrawal with only a near miss gets the reason."""
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./t_pairboth.db?timeout=30")
os.environ.setdefault("JWT_SECRET", "testsecret_testsecret_testsecret")
os.environ.setdefault("IN_APP_SCHEDULER", "false")
os.environ.setdefault("SEED_ADMIN_USER", "a")
os.environ.setdefault("SEED_ADMIN_PASSWORD", "pw-test-123456")
os.environ.setdefault("REQUIRE_TWOFA", "false")

from fastapi.testclient import TestClient

from app import learning
from app.api import app, transfer_matches, transfer_why
from app.db import SessionLocal
from app.models_db import ChargeRow, CompanyRow, SourceAccountRow

ok = True
posted = []


def check(label, cond, got=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}"
          + ("" if cond or got == "" else f"  -> {got}"))


from app.integrations import qbo_write as _qw
_qw.assert_writes_allowed = lambda *a, **k: None
FAIL_NEXT = []


def fake_push(db, r, actor):
    """The card side written to QuickBooks: one entry, or QuickBooks' refusal."""
    if FAIL_NEXT:
        return {"status": "failed", "error": FAIL_NEXT.pop()}
    posted.append(r.charge_id)
    r.qbo_txn_id, r.qbo_txn_type = f"Q{len(posted)}", "CreditCardPayment"
    db.commit()
    return {"status": "posted", "qbo_txn_id": r.qbo_txn_id}


_qw._push_charge_claimed = fake_push
CO = "YS"


def ch(cid, src, amt, day, **k):
    return ChargeRow(charge_id=cid, source_txn_id=cid, company=CO, source=src, amount=Decimal(amt),
                     txn_date=date(2026, 9, day), card_last4="", cardholder_name="",
                     merchant=k.pop("merchant", "X"), memo="", status=k.pop("status", "for_review"),
                     is_credit=k.pop("credit", False), **k)


with TestClient(app) as c:
    db = SessionLocal()
    db.merge(CompanyRow(id=CO, name=CO, active=True))
    db.merge(SourceAccountRow(id=f"{CO}:ffb", company=CO, source="ffb", nickname="FFB Chkg", is_primary=True))
    db.merge(SourceAccountRow(id=f"{CO}:cap1", company=CO, source="cap1", nickname="Capital One"))
    db.merge(SourceAccountRow(id=f"{CO}:citi", company=CO, source="citi", nickname="Citi"))
    db.add(ch("cardpay", "cap1", "868.26", 3, credit=True, is_card_payment=True, merchant="PAYMENT THANK YOU"))
    db.add(ch("wd", "ffb", "868.26", 3, merchant="autopay"))
    db.add(ch("wd2", "ffb", "500.00", 4, merchant="autopay"))
    db.add(ch("notflag", "citi", "500.00", 4, credit=True, merchant="STORE CREDIT 4411"))
    db.add(ch("wd3", "ffb", "77.00", 5, merchant="autopay"))
    db.add(ch("asexp", "citi", "77.00", 5, credit=True, is_card_payment=True, status="categorized",
              qbo_txn_id="E9", qbo_txn_type="Purchase"))
    db.commit()
    A = {"Authorization": "Bearer " + c.post(
        "/auth/login", data={"username": "a", "password": "pw-test-123456"}
    ).json()["access_token"]}

    m = transfer_matches(db, CO)
    check("both still waiting: the pair is offered on both rows",
          m.get("wd", {}).get("pair") == "both" and m.get("cardpay", {}).get("pair") == "both", m)
    items = {i["id"]: i for i in c.get(f"/queue/{CO}?status=for_review&limit=50", headers=A).json()["items"]}
    check("the checking row carries the match", bool(items["wd"].get("transfer_match")))
    why = transfer_why(db, CO, m)
    check("a near miss not marked as a card payment says so",
          "isn't marked as a card payment" in why.get("wd2", ""), why)
    check("a near miss posted as an expense says so",
          "not a transfer" in why.get("wd3", ""), why)

    r = c.post("/queue/transfer-match", headers=A, data={"charge_id": "wd", "company": CO})
    db.expire_all()
    w, p = db.get(ChargeRow, "wd"), db.get(ChargeRow, "cardpay")
    check("Match posts the payment once and clears both rows",
          r.status_code == 200 and posted == ["cardpay"]
          and w.status == "categorized" and p.status == "categorized"
          and w.qbo_txn_id == p.qbo_txn_id == "Q1", (r.text[:200], posted, w.status, p.status))
    check("…and the pair is no longer offered", "wd" not in transfer_matches(db, CO))

    # QUICKBOOKS REFUSES: the card side goes back with QuickBooks' own reason,
    # the checking side never moves, and no "didn't confirm in time".
    db.add(ch("cardpay2", "cap1", "250.00", 10, credit=True, is_card_payment=True, merchant="PAYMENT"))
    db.add(ch("wd4", "ffb", "250.00", 10, merchant="autopay"))
    db.commit()
    FAIL_NEXT.append("Account is inactive")
    r = c.post("/queue/transfer-match", headers=A, data={"charge_id": "cardpay2", "company": CO})
    db.expire_all()
    check("a refusal names QuickBooks' reason and leaves both rows in For Review",
          r.status_code == 409 and "Account is inactive" in r.text
          and db.get(ChargeRow, "cardpay2").status == "for_review" and db.get(ChargeRow, "wd4").status == "for_review",
          r.text)
    # ANOTHER POST OF IT FINISHES while this one waits: its entry is used.
    from app.integrations.qbo_write import _claim_post, _release_post
    import threading, time as _t
    _claim_post("cardpay2")
    def _other():
        _t.sleep(2)
        s2 = SessionLocal(); x = s2.get(ChargeRow, "cardpay2")
        x.qbo_txn_id, x.qbo_txn_type, x.status = "Q99", "CreditCardPayment", "categorized"; s2.commit(); s2.close()
        _release_post("cardpay2")
    threading.Thread(target=_other).start()
    n_before = len(posted)
    r = c.post("/queue/transfer-match", headers=A, data={"charge_id": "wd4", "company": CO})
    db.expire_all()
    check("another post of the same payment is waited for, and its entry used (no second entry)",
          r.status_code == 200 and len(posted) == n_before and db.get(ChargeRow, "wd4").qbo_txn_id == "Q99", r.text)

    # THE AUTOMATIC POSTER leaves pairs, and the primary account, to Match Transfer.
    db.add(ch("cardpay3", "cap1", "40.00", 20, credit=True, is_card_payment=True, merchant="PAYMENT"))
    db.add(ch("wd5", "ffb", "40.00", 20, merchant="autopay", is_card_payment=True))
    db.add(ch("solo", "citi", "33.00", 22, credit=True, is_card_payment=True, merchant="PAYMENT"))
    db.commit()
    import app.integrations.qbo_write as _qw2
    seen = []
    _orig = _qw2.push_charge
    _qw2.push_charge = lambda db, c, actor: (seen.append(c.charge_id) or {"status": "posted"})
    learning.post_card_payments(db, CO, actor="cron")
    _qw2.push_charge = _orig
    check("the automatic poster posts a lone payment but leaves both sides of a pair alone",
          seen == ["solo"], seen)

print("\nALL CHECKS PASSED" if ok else "\n*** FAILURES ***")
sys.exit(0 if ok else 1)
