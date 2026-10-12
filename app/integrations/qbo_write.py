"""
QuickBooks write-back.

This is the only code in the project that changes a real ledger, so it is
deliberately conservative:

  - **Idempotent.** Every charge records the QBO transaction id it created.
    A charge that already has one is skipped, so a re-run, a double-click or a
    retry after a timeout cannot post twice. This matters more than anything
    else here: a duplicated payment is far harder to notice and unwind than a
    failed one.
  - **Per-record.** One bad charge fails alone and is reported; the rest still
    post. An all-or-nothing batch would strand 200 good rows behind one bad one.
  - **Gated.** Both DRY_RUN=false and QBO_WRITE_ENABLED=true are required, plus
    a mapped QBO account for the card. Anything missing is a refusal, not a
    guess.
  - **Audited.** Before/after state, the QBO id, and who approved it are written
    to the audit log for every post.

Two shapes get written:

  matched -> BillPayment      pays down a specific bill from the card account
  coded   -> Purchase         an expense against a category account and vendor
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models_db import (AuditRow, BillRow, CardRow, ChargeRow, QboTokenRow,
                         SourceAccountRow)
from . import qbo_oauth, qbo_tokens

log = logging.getLogger(__name__)

MINOR_VERSION = "75"



def _ref_by_name(*a, **k):
    """A vendor or account by name from the company's OWN QuickBooks file
    -- see review.ref_by_name. Imported late to avoid an import cycle."""
    from ..review import ref_by_name
    return ref_by_name(*a, **k)

class WriteBlocked(RuntimeError):
    """Raised when the safety gates or configuration forbid a write."""


class WriteFailed(RuntimeError):
    pass


def assert_writes_allowed() -> None:
    if settings.dry_run:
        raise WriteBlocked("DRY_RUN is true — set it to false to post for real")
    if not settings.qbo_write_enabled:
        raise WriteBlocked("QBO_WRITE_ENABLED is false — set it to true to post")


def plain_fault(body: str, entity: str, status: int) -> str:
    """The sentence QuickBooks buried, rather than the whole JSON envelope.

    An Intuit fault is a nest of Fault/Error/Detail with the useful sentence at
    the bottom. Showing the raw payload makes an ordinary validation message --
    "subaccounts must match their parent's type" -- look like a crash.
    """
    import json as _json
    import re as _re
    try:
        err = (_json.loads(body).get("Fault", {}).get("Error") or [{}])[0]
        detail = (err.get("Detail") or "").strip()
        message = (err.get("Message") or "").strip()
        text = detail or message
        # Intuit prefixes the detail with its own label and appends a
        # timestamp; neither helps the reader.
        text = _re.sub(r"^[A-Za-z ]*Error:\s*", "", text).strip()
        text = _re.sub(r"\s*\d{4}-\d{2}-\d{2}T[\d:.\-+]+$", "", text).strip()
        text = text.rstrip(".")
        if text:
            return text
    except Exception:                       # noqa: BLE001
        pass
    return f"{entity} write failed ({status})"


def _post(db: Session, realm_id: str, entity: str, payload: dict,
          _restored: bool = False) -> dict:
    """Send one create or update to QuickBooks.

    A DELETED VENDOR is handled rather than shown. QuickBooks refuses to
    modify a transaction whose vendor has since been deleted -- "Before you
    can modify this transaction, you must restore <vendor> (deleted)". The
    engine does exactly that: restores the vendor, makes the change, and
    deletes the vendor again, so the vendor list ends as it was. Once only;
    if QuickBooks still refuses, the error is shown as before.
    """
    try:
        return _post_once(db, realm_id, entity, payload)
    except WriteFailed as e:
        name = _deleted_vendor_in(str(e))
        if _restored or not name:
            raise
        vid = _set_vendor_active(db, realm_id, name, True)
        if not vid:
            raise
        try:
            return _post(db, realm_id, entity, payload, _restored=True)
        finally:
            try:
                _set_vendor_active(db, realm_id, vid, False, by_id=True)
            except Exception:                               # noqa: BLE001
                log.exception("could not delete vendor %s again after the update",
                              name)


def _deleted_vendor_in(message: str) -> str:
    """The vendor QuickBooks says must be restored, or ""."""
    import re as _re
    if "has been deleted" not in message or "restore" not in message:
        return ""
    m = _re.search(r"restore\s+(.+?)\s*$", message.strip().rstrip("."))
    return m.group(1).strip() if m else ""


def _set_vendor_active(db: Session, realm_id: str, name_or_id: str,
                       active: bool, by_id: bool = False) -> str:
    """Restore or delete a vendor. Returns its id, or "" if not found."""
    from .qbo_bills import _query
    if by_id:
        res = _query(db, realm_id, f"SELECT * FROM Vendor WHERE Id = '{name_or_id}'")
    else:
        safe = name_or_id.replace("'", "\\'")
        res = _query(db, realm_id,
                     f"SELECT * FROM Vendor WHERE Active = false AND "
                     f"DisplayName = '{safe}'")
    # _query returns the QueryResponse already unwrapped; either shape is read.
    res = res or {}
    rows = (res.get("Vendor") or (res.get("QueryResponse") or {}).get("Vendor")
            or [])
    if not rows:
        return ""
    v = rows[0]
    _post_once(db, realm_id, "vendor", {"Id": v["Id"], "SyncToken": v["SyncToken"],
                                         "sparse": True, "Active": active})
    log.warning("QuickBooks vendor %s %s to allow an update", v.get("DisplayName"),
                "restored" if active else "deleted again")
    return v["Id"]


def _post_once(db: Session, realm_id: str, entity: str, payload: dict) -> dict:
    qbo_tokens.assert_realm_allowed(realm_id, db)
    token = qbo_tokens.get_access_token(db, realm_id)
    url = f"{qbo_oauth.api_base()}/v3/company/{realm_id}/{entity}"
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json",
                 "Content-Type": "application/json"},
        params={"minorversion": MINOR_VERSION},
        json=payload,
        timeout=60,
    )
    if r.status_code == 401:
        raise WriteFailed("401 from QuickBooks — token rejected; reconnect")
    if r.status_code >= 400:
        # Same reasoning as correct_bill_amount: a concurrency fault is not a
        # system failure, it's someone getting there first.
        if "5010" in r.text or "Stale Object" in r.text:
            raise WriteFailed("Bill already matched")
        raise WriteFailed(plain_fault(r.text, entity, r.status_code))
    return r.json()


def delete_transaction(db: Session, realm_id: str, entity: str,
                       txn_id: str, sync_token: str) -> bool:
    """Delete a transaction from QuickBooks.

    Used by undo. QuickBooks needs both the Id and the current SyncToken --
    the token is a concurrency guard, so a stale one means someone else edited
    the transaction and the delete is refused rather than clobbering them.
    """
    assert_writes_allowed()
    qbo_tokens.assert_realm_allowed(realm_id, db)
    token = qbo_tokens.get_access_token(db, realm_id)
    url = f"{qbo_oauth.api_base()}/v3/company/{realm_id}/{entity}"
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json",
                 "Content-Type": "application/json"},
        params={"operation": "delete", "minorversion": MINOR_VERSION},
        json={"Id": txn_id, "SyncToken": sync_token or "0"},
        timeout=60,
    )
    if r.status_code >= 400:
        body = r.text or ""
        # Already deleted in QuickBooks -- by someone working directly in QBO,
        # or a retry after a partial failure. The goal of undo is "not in the
        # ledger", and that's already true, so treat it as done rather than
        # stranding the charge in Categorized with no way back.
        if "Object Not Found" in body or '"code":"610"' in body:
            log.info("%s %s already absent from QuickBooks — treating as deleted",
                     entity, txn_id)
            return True
        raise WriteFailed(
            f"could not delete {entity} {txn_id} ({r.status_code}): {body[:300]}")
    return True


def undo_post(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Remove a charge's transaction from QuickBooks so it can be reworked.

    Deleting in QBO first, and only clearing local state if that succeeds, is
    what stops the app's view drifting from the ledger. A failed delete leaves
    everything as it was rather than pretending the transaction is gone.
    """
    if not charge.qbo_txn_id:
        return {"status": "nothing_posted"}

    # EVERY KIND THE ENGINE CREATES. Card payments and transfers were missing,
    # so they were deleted as a "purchase": QuickBooks answered "not found",
    # which counts as already deleted -- and the engine cleared its record while
    # the payment stayed in QuickBooks, ready to be posted twice.
    entity = {"BillPayment": "billpayment", "Deposit": "deposit",
              "CreditCardPayment": "creditcardpayment", "Transfer": "transfer",
              "Purchase": "purchase"}.get(charge.qbo_txn_type or "Purchase")
    if entity is None:
        return {"status": "failed",
                "error": f"don't know how to remove {entity_word(charge.qbo_txn_type)} from QuickBooks; "
                         f"delete {charge.qbo_txn_id} there, then undo here"}
    realm = _realm_for(db, charge.company)
    try:
        delete_transaction(db, realm, entity, charge.qbo_txn_id,
                           charge.qbo_sync_token)
    except (WriteBlocked, WriteFailed) as e:
        return {"status": "failed", "error": str(e)[:300]}

    db.add(AuditRow(
        company=charge.company, action="qbo_deleted",
        charge_id=charge.charge_id,
        detail=f"deleted {entity_word(charge.qbo_txn_type)} {charge.qbo_txn_id} by {actor}",
        dry_run=False, actor=actor))
    # The payment is gone from the ledger, so the bill is owed again. Without
    # this it would stay invisible until the next sync -- unmatchable for as
    # long as that takes, which is exactly when the reviewer is trying to redo it.
    if charge.qbo_txn_type == "BillPayment" and charge.matched_bill_id:
        paid_bill = db.get(BillRow, charge.matched_bill_id)
        if paid_bill is not None:
            before_bal = paid_bill.balance or Decimal("0")
            paid_bill.balance = min(paid_bill.amount or (before_bal + charge.amount),
                                    before_bal + charge.amount)
            db.add(AuditRow(
                company=charge.company, action="bill_balance_restored",
                charge_id=charge.charge_id, bill_id=paid_bill.bill_id,
                detail=f"balance {before_bal} -> {paid_bill.balance} "
                       f"after undoing payment of ${charge.amount}",
                dry_run=False, actor=actor))
    charge.qbo_txn_id = ""
    charge.qbo_txn_type = ""
    charge.qbo_sync_token = ""
    charge.posted_at = None
    db.commit()
    return {"status": "deleted"}


# QuickBooks keeps change history for 30 days. A gap wider than that means
# deletions happened that CDC can no longer report, so the sweep says so rather
# than reporting a clean run it cannot vouch for.
CDC_RETENTION_DAYS = 30


# Every kind of transaction the app writes, plus Bill, which it doesn't write
# but a payment can be orphaned by. Kept as a tuple so adding a fourth kind of
# post is one edit rather than a search for every place a type is listed.
WATCHED_ENTITIES = ("Bill", "BillPayment", "Purchase", "Deposit")


def fetch_changes_since(db: Session, realm_id: str, since: datetime,
                        entities: str = "") -> list[dict]:
    """Everything QuickBooks changed since `since`, deletions included.

    One request instead of one per posted transaction. Deleted entities come
    back with a status of "Deleted", which is the only way to learn about a
    deletion without re-reading everything we have ever written.
    """
    entities = entities or ",".join(WATCHED_ENTITIES)
    qbo_tokens.assert_realm_allowed(realm_id, db)
    token = qbo_tokens.get_access_token(db, realm_id)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm_id}"
    r = httpx.get(f"{base}/cdc",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"},
                  params={"entities": entities,
                          "changedSince": since.strftime("%Y-%m-%dT%H:%M:%S%z")
                                          or since.isoformat(),
                          "minorversion": MINOR_VERSION},
                  timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"change feed refused ({r.status_code}): {r.text[:200]}")

    out: list[dict] = []
    for block in (r.json().get("CDCResponse") or []):
        for qr in (block.get("QueryResponse") or []):
            for kind in WATCHED_ENTITIES:
                for row in (qr.get(kind) or []):
                    out.append({"type": kind, "id": str(row.get("Id") or ""),
                                "status": str(row.get("status")
                                              or row.get("Status") or ""),
                                "raw": row})
    return out


def find_orphaned_transactions(db: Session, company: str,
                               since: datetime) -> dict:
    """Flag charges whose QuickBooks side has changed underneath them.

    EVERY kind of transaction the app writes, not only bill payments. A coded
    expense is a Purchase and a bank refund is a Deposit, and either can be
    deleted in QuickBooks with exactly as little trace as a payment can.

    Three distinct failures, and they need different words because they need
    different fixes:

    - OUR TRANSACTION was deleted. The charge claims to be posted and isn't, so
      the work has to be done again.
    - OUR TRANSACTION was voided. It is still there, at zero, so the ledger has
      a record and no money. Not the same as deleted and not fixed the same way.
    - The BILL a payment settled was deleted. The payment still exists but pays
      nothing; it sits in QuickBooks as an unapplied amount against the vendor.

    Ids are only unique WITHIN an entity type -- Purchase 42 and Bill 42 are
    different transactions -- so the deleted set is keyed by type. Flattening it
    would have every charge whose id collided with an unrelated deletion
    flagged for no reason.

    Nothing is changed automatically. All three need a person to decide, and a
    sweep that quietly un-resolved a hundred charges at 3am would be worse than
    the problem it was fixing.
    """
    realm = _realm_for(db, company)
    changes = fetch_changes_since(db, realm, since)

    dead: dict[str, set] = {k: set() for k in WATCHED_ENTITIES}
    voided: dict[str, set] = {k: set() for k in WATCHED_ENTITIES}
    for c in changes:
        if c["type"] not in dead:
            continue
        if c["status"].lower() == "deleted":
            dead[c["type"]].add(c["id"])
            continue
        # QuickBooks does not report a void as a status -- a voided transaction
        # comes back as an ordinary update with its total zeroed. Read as a
        # heuristic, which is why it says "appears" when it reports one.
        try:
            if Decimal(str((c["raw"] or {}).get("TotalAmt", "-1"))) == 0:
                voided[c["type"]].add(c["id"])
        except Exception:                   # noqa: BLE001
            pass

    if not any(dead.values()) and not any(voided.values()):
        return {"checked": len(changes), "flagged": 0, "details": []}

    flagged, details = 0, []
    posted = db.query(ChargeRow).filter(
        ChargeRow.company == company,
        ChargeRow.qbo_txn_id != "").all()
    for ch in posted:
        kind = ch.qbo_txn_type or ""
        label = {"BillPayment": "bill payment", "Purchase": "expense",
                 "Deposit": "deposit"}.get(kind, "transaction")
        if ch.qbo_txn_id in dead.get(kind, ()):
            # The transaction is GONE. There is nothing left in QuickBooks for
            # this charge, so leaving it in Categorized would be the app
            # asserting something plainly untrue. It is returned to For Review
            # automatically -- unlike the bill case below, where the payment
            # still exists and a person has to decide what to do with it.
            ch.status, ch.resolution = "for_review", ""
            ch.matched_bill_id = ""
            ch.qbo_txn_id = ch.qbo_txn_type = ch.qbo_sync_token = ""
            ch.resolved_by, ch.resolved_at, ch.posted_at = "", None, None
            # WHAT HAPPENED, not what to do about it.
            #
            # This said "Delete and redo in the engine", which was instructions
            # for a job the sweep had already done -- the charge is back on For
            # Review by the two lines above. Somebody following it would delete
            # a charge that only needed coding again.
            ch.post_error = ("Deleted in QuickBooks — returned to For Review. "
                             "Categorize it again when you are ready.")
            flagged += 1
            details.append(f"{ch.charge_id}: returned to For Review")
            db.add(AuditRow(company=company, action="qbo_orphan_returned",
                            charge_id=ch.charge_id,
                            detail=f"{label} deleted in QuickBooks; charge "
                                   f"returned to For Review",
                            dry_run=False, actor="cron"))
            continue
        if (ch.qbo_txn_id in voided.get(kind, ())
                and (ch.amount or 0) != 0):
            note = (f"The {label} was voided in QBO — it is still there, at "
                    f"zero. Delete and redo in the engine.")
        elif (kind == "BillPayment" and ch.matched_bill_id
                and ch.matched_bill_id in dead.get("Bill", ())):
            note = ("Bill deleted in QBO. The payment still exists and pays "
                    "nothing — check it in QuickBooks.")
        else:
            continue
        if ch.post_error == note:
            continue                        # already flagged; don't re-log it
        ch.post_error = note
        flagged += 1
        details.append(f"{ch.charge_id}: {note}")
        db.add(AuditRow(company=company, action="qbo_orphan_detected",
                        charge_id=ch.charge_id, bill_id=ch.matched_bill_id or "",
                        detail=note, dry_run=False, actor="cron"))
    db.commit()
    return {"checked": len(changes), "flagged": flagged, "details": details}


# The old name, kept so nothing that imported it breaks mid-deploy.
find_orphaned_payments = find_orphaned_transactions


def delete_bill(db: Session, bill: BillRow, actor: str) -> dict:
    """Delete a bill from QuickBooks, and only if nothing has been paid on it.

    NOT undoable, and deliberately so: QuickBooks issues a new Id on re-create,
    so a restored bill is a different bill and anything referring to the old one
    is broken. The confirm says as much.

    The refusal is the important part. QuickBooks will happily delete a bill
    that has a payment against it -- the payment survives, unlinked, as an
    unapplied amount against the vendor, and nothing here would ever notice,
    because a deleted bill leaves the local mirror by the same route a fully
    paid one does. So the check happens before the delete, not after.

    Checked against the LIVE bill, not the mirror, which can be an hour stale --
    an hour is long enough for someone to pay it in QuickBooks.
    """
    assert_writes_allowed()
    realm = _realm_for(db, bill.company)
    live = _read_bill_live(db, realm, bill.bill_id)
    if not live:
        raise WriteFailed(f"bill {bill.doc_number or bill.bill_id} is not in "
                          f"QuickBooks — nothing to delete")

    total = Decimal(str(live.get("TotalAmt", "0")))
    label = live.get("DocNumber") or bill.doc_number or bill.bill_id
    # The same three checks merge makes. Shared deliberately: two copies would
    # eventually disagree, and the lenient one would be the way in.
    #
    # LinkedTxn is checked as well as the balance because a payment that nets to
    # zero -- a payment plus a credit, say -- leaves the balance looking
    # untouched. And our own record is checked because we may have posted a
    # payment QuickBooks has not yet reflected in the figures just read.
    _bill_is_untouched(db, bill.bill_id, live, label)

    delete_transaction(db, realm, "bill", bill.bill_id,
                       str(live.get("SyncToken", "0")))
    db.add(AuditRow(company=bill.company, action="bill_deleted",
                    bill_id=bill.bill_id,
                    detail=f"deleted bill {label} (${total}) — not reversible",
                    dry_run=False, actor=actor))
    db.delete(bill)
    _drop_all_bill(db, bill.bill_id)
    db.commit()
    return {"status": "deleted", "bill": label}


def _bill_is_untouched(db: Session, bill_id: str, live: dict, label: str) -> None:
    """Raise unless nothing has been paid against or linked to this bill.

    The same three checks delete_bill makes, factored out because merge deletes
    bills too and must not be the lenient way in.
    """
    total = Decimal(str(live.get("TotalAmt", "0")))
    balance = Decimal(str(live.get("Balance", "0")))
    if balance != total:
        raise WriteFailed(
            f"bill {label} has ${total - balance} paid against it — removing "
            f"it would leave that payment in QuickBooks applied to nothing")
    linked = [t for t in (live.get("LinkedTxn") or [])
              if str(t.get("TxnType", "")).lower() != "purchaseorder"]
    if linked:
        kinds = ", ".join(sorted({str(t.get("TxnType", "?")) for t in linked}))
        raise WriteFailed(f"bill {label} has transactions linked to it ({kinds})")
    ours = db.query(ChargeRow).filter(
        ChargeRow.matched_bill_id == bill_id,
        ChargeRow.qbo_txn_id != "").first()
    if ours is not None:
        raise WriteFailed(
            f"bill {label} is matched to a charge in this app "
            f"({ours.merchant or ours.charge_id}) — undo that first")


# Every merged line is booked here. Not a choice the dialog offers: these bills
# are ticket purchases, the combined bill is still a ticket purchase, and a
# per-merge category would be one more thing to get wrong on a write that
# cannot be undone.
MERGE_LINE_ACCOUNT = "Inventory Asset"


def _merge_account_ref(db: Session, company: str = "") -> dict:
    """The AccountRef every merged line is booked to.

    Resolved by id where the cached chart has it, so a renamed account still
    posts correctly; by name otherwise, which is what QuickBooks falls back to.
    """
    from ..models_db import QboRefRow
    row = _ref_by_name(db, company, "account", MERGE_LINE_ACCOUNT)
    if row is None:
        # Tolerated rather than fatal: QuickBooks resolves by name, and a chart
        # cache that has not been refreshed should not block a merge.
        log.warning("%s not in the cached chart of accounts; posting by name",
                    MERGE_LINE_ACCOUNT)
        return {"name": MERGE_LINE_ACCOUNT}
    return {"value": row.qbo_id, "name": row.name}


def merge_bills(db: Session, company: str, keep_id: str, drop_ids: list,
                actor: str) -> dict:
    """Fold one or more bills into another, in QuickBooks and here.

    The kept bill is UPDATED rather than replaced: it keeps its QuickBooks id,
    so anything already pointing at it -- a matched charge, a reference in
    somebody's records -- still points at the right thing. Creating a combined
    bill and deleting all the originals would have broken every one of those.

    Each dropped bill contributes its lines to the kept bill, carrying the
    description and the amount across, booked to Inventory Asset. Then the
    dropped bills are deleted.

    ORDER IS THE SAFETY ARGUMENT, as with deletion. The kept bill is written
    first and its new total checked before anything is deleted:

    - update fails      -> nothing deleted, nothing lost
    - total comes back wrong -> the original lines are written back and the
      merge abandoned
    - a later delete fails   -> the amount is on the kept bill AND still on the
      original, so it is named in the result as a duplicate to remove by hand

    Not undoable. QuickBooks issues new ids, so a deleted bill cannot be
    restored as the same bill.
    """
    assert_writes_allowed()
    drop_ids = [d for d in drop_ids if d and d != keep_id]
    if not drop_ids:
        raise WriteFailed("Choose at least one bill to merge into the one you keep")
    realm = _realm_for(db, company)

    keep_row = db.query(BillRow).filter(BillRow.company == company,
                                        BillRow.bill_id == keep_id).first()
    if keep_row is None:
        raise WriteFailed("the bill you are keeping is not in the bill list any more")
    keep_live = _read_bill_live(db, realm, keep_id)
    if not keep_live:
        raise WriteFailed("the bill you are keeping is not in QuickBooks")
    keep_label = keep_live.get("DocNumber") or keep_row.doc_number or keep_id

    drops, lives = [], {}
    for bid in drop_ids:
        row = db.query(BillRow).filter(BillRow.company == company,
                                       BillRow.bill_id == bid).first()
        if row is None:
            raise WriteFailed(f"bill {bid} is not in the bill list any more")
        live = _read_bill_live(db, realm, bid)
        if not live:
            raise WriteFailed(f"bill {row.doc_number or bid} is not in QuickBooks")
        label = live.get("DocNumber") or row.doc_number or bid
        # The full guard, on the bills being DELETED only. Deleting a bill with
        # a payment against it orphans that payment; adding a line to the kept
        # bill merely increases what is owed on it, which is recoverable and
        # visible.
        _bill_is_untouched(db, bid, live, label)
        drops.append(row)
        lives[bid] = live

    vendor_of = lambda lv: str((lv.get("VendorRef") or {}).get("value") or "")
    if {vendor_of(lives[b]) for b in drop_ids} != {vendor_of(keep_live)}:
        names = sorted({str((lv.get("VendorRef") or {}).get("name") or "?")
                        for lv in [keep_live] + [lives[b] for b in drop_ids]})
        raise WriteFailed(f"these bills are from different vendors "
                          f"({', '.join(names)}) — a bill can only have one")

    account_ref = _merge_account_ref(db, company)
    original_lines = [ln for ln in (keep_live.get("Line") or [])]
    added = []
    for bid in drop_ids:
        lv = lives[bid]
        label = lv.get("DocNumber") or bid
        for ln in (lv.get("Line") or []):
            if not ln.get("DetailType"):
                continue
            added.append({
                "Amount": ln.get("Amount", 0),
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": dict(account_ref)},
                # The description is what tells anyone reading the merged bill
                # where the second line came from, so it falls back rather than
                # being left blank.
                "Description": engine_memo_for(company, ln.get("Description")
                                           or lv.get("PrivateNote")
                                           or f"from bill {label}"),
            })
    if not added:
        raise WriteFailed("the bills being merged in have no lines to carry across")

    keep_total = Decimal(str(keep_live.get("TotalAmt", "0")))
    expected = keep_total + sum(
        (Decimal(str(lives[b].get("TotalAmt", "0"))) for b in drop_ids), Decimal("0"))

    def write_lines(lines, sync_token):
        """Sparse update of the kept bill's lines. Returns the new Bill."""
        payload = {
            "Id": keep_id,
            "SyncToken": str(sync_token),
            "sparse": True,
            "VendorRef": keep_live.get("VendorRef") or {},
            "Line": [{k: v for k, v in ln.items() if k != "LineNum"}
                     for ln in lines],
        }
        return (_post(db, realm, "bill", payload) or {}).get("Bill", {})

    updated = write_lines(original_lines + added,
                          keep_live.get("SyncToken", "0"))
    got = Decimal(str(updated.get("TotalAmt", "0")))
    if got != expected:
        # Put the bill back the way it was before giving up, so a failed merge
        # leaves nothing behind.
        try:
            write_lines(original_lines, updated.get("SyncToken",
                                                    keep_live.get("SyncToken", "0")))
            raise WriteFailed(
                f"bill {keep_label} would have totaled ${got}, not ${expected} "
                f"— it has been put back as it was and nothing was deleted")
        except WriteFailed:
            raise
        except Exception:                   # noqa: BLE001
            log.exception("could not restore bill %s after a bad merge", keep_id)
            raise WriteFailed(
                f"bill {keep_label} totaled ${got}, not ${expected}, and could "
                f"not be put back — check it in QuickBooks. Nothing was deleted.")

    db.add(AuditRow(company=company, action="bills_merged", bill_id=keep_id,
                    detail=f"merged {', '.join(r.doc_number or r.bill_id for r in drops)}"
                           f" into {keep_label}: ${keep_total} -> ${expected} "
                           f"— not reversible",
                    dry_run=False, actor=actor))

    stuck = []
    for row in drops:
        label = row.doc_number or row.bill_id
        try:
            delete_transaction(db, realm, "bill", row.bill_id,
                               str(lives[row.bill_id].get("SyncToken", "0")))
            db.delete(row)
            _drop_all_bill(db, row.bill_id)
        except Exception as e:              # noqa: BLE001
            stuck.append(f"{label}: {e}")
            db.add(AuditRow(company=company, action="bill_merge_leftover",
                            bill_id=row.bill_id,
                            detail=f"folded into {keep_label} but could not be "
                                   f"deleted — the amount is now on both: {e}",
                            dry_run=False, actor=actor))

    keep_row.amount = expected
    keep_row.balance = Decimal(str(updated.get("Balance", expected)))
    keep_row.line_count = len(original_lines) + len(added)
    db.commit()
    return {"bill_id": keep_id, "doc_number": keep_label,
            "amount": str(expected), "lines_added": len(added),
            "merged": len(drops), "stuck": stuck}


def split_bill(db: Session, company: str, bill_id: str, parts: list,
               actor: str) -> dict:
    """Divide one bill into several, in QuickBooks and here.

    The inverse of merge, and built the same way round: the ORIGINAL bill is
    kept and reduced to the first part, then a new bill is created for each
    remaining part. Keeping it means its QuickBooks id survives, so anything
    already pointing at the bill still points at something real.

    Single-line bills only. Splitting by amount when the bill has several lines
    is ambiguous -- which line shrinks? -- and guessing would quietly move money
    between accounts. Said plainly instead.

    Not undoable: the new bills are new transactions and the original's amount
    has changed. Nothing is deleted, though, so the failure mode is a bill in
    the wrong shape rather than a bill that is gone.
    """
    assert_writes_allowed()
    amounts = [Decimal(str(p)).quantize(Decimal("0.01")) for p in parts]
    if len(amounts) < 2:
        raise WriteFailed("A split needs at least two parts")
    if any(a <= 0 for a in amounts):
        raise WriteFailed("Every part needs an amount above zero")

    realm = _realm_for(db, company)
    row = db.query(BillRow).filter(BillRow.company == company,
                                   BillRow.bill_id == bill_id).first()
    if row is None:
        raise WriteFailed("that bill is not in the bill list any more")
    live = _read_bill_live(db, realm, bill_id)
    if not live:
        raise WriteFailed("that bill is not in QuickBooks")
    label = live.get("DocNumber") or row.doc_number or bill_id
    _bill_is_untouched(db, bill_id, live, label)

    lines = [ln for ln in (live.get("Line") or []) if ln.get("DetailType")]
    if len(lines) != 1:
        raise WriteFailed(
            f"bill {label} has {len(lines)} lines. Splitting by amount only "
            f"makes sense on a single-line bill — otherwise there is no saying "
            f"which line should shrink.")

    total = Decimal(str(live.get("TotalAmt", "0")))
    if sum(amounts, Decimal("0")) != total:
        raise WriteFailed(
            f"the parts total ${sum(amounts, Decimal('0'))} but bill {label} "
            f"is ${total} — nothing was split")

    template = lines[0]
    base_desc = template.get("Description") or live.get("PrivateNote") or ""

    def line_for(amount, part_no):
        ln = {k: v for k, v in template.items() if k not in ("Id", "LineNum")}
        ln["Amount"] = float(amount)
        # The part number goes INSIDE the signature, not after it -- the mark
        # belongs at the end of the description or it stops being a suffix you
        # can look for.
        ln["Description"] = engine_memo_for(company, 
            f"{_unsigned(base_desc)} (part {part_no} of {len(amounts)})".strip())
        return ln

    # The original FIRST. If this fails nothing has been created, and the bill
    # is exactly as it was.
    shrunk = (_post(db, realm, "bill", {
        "Id": bill_id, "SyncToken": str(live.get("SyncToken", "0")),
        "sparse": True, "VendorRef": live.get("VendorRef") or {},
        "Line": [line_for(amounts[0], 1)],
    }) or {}).get("Bill", {})
    got = Decimal(str(shrunk.get("TotalAmt", "0")))
    if got != amounts[0]:
        try:
            _post(db, realm, "bill", {
                "Id": bill_id,
                "SyncToken": str(shrunk.get("SyncToken", live.get("SyncToken", "0"))),
                "sparse": True, "VendorRef": live.get("VendorRef") or {},
                "Line": [{k: v for k, v in template.items() if k != "LineNum"}],
            })
        except Exception:                   # noqa: BLE001
            log.exception("could not restore bill %s after a bad split", bill_id)
            raise WriteFailed(
                f"bill {label} came back as ${got}, not ${amounts[0]}, and could "
                f"not be put back — check it in QuickBooks. Nothing was created.")
        raise WriteFailed(
            f"bill {label} would have become ${got}, not ${amounts[0]} — it has "
            f"been put back as it was and nothing was created")

    created, failed = [], []
    for i, amount in enumerate(amounts[1:], start=2):
        try:
            made = (_post(db, realm, "bill", {
                "VendorRef": live.get("VendorRef") or {},
                "TxnDate": live.get("TxnDate"),
                # Suffixed rather than repeated: two bills sharing a document
                # number is exactly the confusion this feature creates if the
                # numbers are not distinguishable.
                "DocNumber": f"{(live.get('DocNumber') or bill_id)[:15]}-{i}",
                "PrivateNote": engine_memo_for(company, live.get("PrivateNote") or ""),
                "Line": [line_for(amount, i)],
            }) or {}).get("Bill", {})
            new_id = str(made.get("Id") or "")
            if not new_id:
                raise WriteFailed("QuickBooks did not return the new bill")
            created.append(made.get("DocNumber") or new_id)
            db.merge(BillRow(
                bill_id=new_id, company=company, amount=amount,
                balance=Decimal(str(made.get("Balance", amount))),
                txn_date=row.txn_date, line_count=1, quantity=None,
                vendor=row.vendor, vendor_id=row.vendor_id,
                doc_number=str(made.get("DocNumber") or ""),
                email=row.email or "", order_number=row.order_number or "",
                memo=row.memo or ""))
        except Exception as e:              # noqa: BLE001
            failed.append(f"part {i} (${amount}): {e}")
            db.add(AuditRow(company=company, action="bill_split_incomplete",
                            bill_id=bill_id,
                            detail=f"bill {label} was reduced to ${amounts[0]} "
                                   f"but part {i} of ${amount} was not created: {e}",
                            dry_run=False, actor=actor))

    row.amount = amounts[0]
    row.balance = Decimal(str(shrunk.get("Balance", amounts[0])))
    db.add(AuditRow(company=company, action="bill_split", bill_id=bill_id,
                    detail=f"bill {label} (${total}) split into "
                           + ", ".join(f"${a}" for a in amounts)
                           + " — not reversible",
                    dry_run=False, actor=actor))
    db.commit()
    return {"bill_id": bill_id, "doc_number": label, "kept": str(amounts[0]),
            "created": created, "failed": failed}


def _primary_account(db: Session, company: str):
    """The account card payments are paid FROM, or None if none is set."""
    from ..models_db import SourceAccountRow
    return (db.query(SourceAccountRow)
            .filter(SourceAccountRow.company == company,
                    SourceAccountRow.is_primary.is_(True)).first())


def payment_payload(db: Session, charge: ChargeRow) -> tuple[str, dict]:
    """A payment onto a card, written as QuickBooks would write it.

    Which entity depends on what the card IS, because QuickBooks models the two
    differently and neither form is correct for the other:

      credit card  -> CreditCardPayment, which settles a card balance
      bank account -> Transfer, which just moves money between accounts

    Both need the other side: the operating account the money came from. That
    is the primary account designated on Card Accounts, and if none is set this
    refuses rather than guessing -- picking an account to move money out of is
    not a decision to make on somebody's behalf.
    """
    assert_writes_allowed()
    # (db, charge) -- it reads the company and source off the row itself, and
    # takes the per-card override into account, which passing the two fields
    # separately would have skipped even if the arity had matched.
    acct_id, acct_name = resolve_card_account(db, charge)
    if not acct_id:
        raise WriteFailed(f"'{charge.source}' is not mapped to a QuickBooks "
                          f"account")
    primary = _primary_account(db, charge.company)
    if primary is None or not primary.qbo_account_id:
        raise WriteFailed(
            "No primary operating account is set. Card Accounts → tick "
            "Primary on the account these payments are paid from.")
    if primary.qbo_account_id == acct_id:
        # THE CHECKING SIDE of a card payment: the card's own payment row is
        # where the entry is written from. Said in those words.
        raise WriteFailed(
            f"This is the {primary.nickname or primary.source} side of a card payment, so it can't be "
            f"added on its own: use Match Transfer (here or on the card's payment) to record it once "
            f"and clear both rows.")

    # The amount is written positive whichever sign the statement used: a
    # payment is a movement of a size, and QuickBooks takes the direction from
    # the entity and the two accounts rather than from the number.
    amount = float(abs(charge.amount))
    kind = account_type(db, acct_id).lower()

    if kind.startswith("bank"):
        return "transfer", {
            "FromAccountRef": {"value": primary.qbo_account_id},
            "ToAccountRef": {"value": acct_id, "name": acct_name},
            "Amount": amount,
            "TxnDate": charge.txn_date.isoformat(),
            "PrivateNote": engine_memo_for(charge.company, charge_memo_text(charge)),
        }
    return "creditcardpayment", {
        "CreditCardAccountRef": {"value": acct_id, "name": acct_name},
        "BankAccountRef": {"value": primary.qbo_account_id},
        "Amount": amount,
        "TxnDate": charge.txn_date.isoformat(),
        "PrivateNote": engine_memo_for(charge.company, charge_memo_text(charge)),
    }


def account_type(db: Session, qbo_account_id: str) -> str:
    """'Bank' or 'Credit Card' for a mapped account, from the cached chart.

    Determines which QuickBooks object a transaction becomes, so it can't be
    guessed: money returned to a bank account is a Deposit, money returned to a
    credit card is a Credit Card Credit, and they are not interchangeable.
    """
    from ..models_db import QboRefRow
    row = db.query(QboRefRow).filter(
        QboRefRow.kind == "account",
        QboRefRow.qbo_id == qbo_account_id).first()
    return (row.account_type if row else "") or "Credit Card"


def resolve_card_account(db: Session, charge: ChargeRow) -> tuple[str, str]:
    """Which QBO account this charge's card settles to.

    Per-card override first, then the card program default. A charge whose card
    isn't mapped is refused rather than posted to a guessed account.
    """
    override = db.get(CardRow, f"{charge.source}:{charge.card_last4}")
    if override is not None and override.qbo_account_id:
        return override.qbo_account_id, override.qbo_account_name

    prog = db.get(SourceAccountRow, f"{charge.company}:{charge.source}")
    if prog is not None and prog.qbo_account_id:
        return prog.qbo_account_id, prog.qbo_account_name

    raise WriteBlocked(
        f"card program '{charge.source}' has no QuickBooks account mapped — "
        f"set it under Cards before posting")


def _realm_for(db: Session, company: str = "") -> str:
    """Which QuickBooks file this company's writes go to.

    MULTI-COMPANY. This used to refuse outright the moment a second QuickBooks
    file was connected, because picking one arbitrarily would post one
    company's charges into another company's ledger -- the worst thing this
    application could do. The mapping it was waiting for is CompanyRow.realm_id,
    which has been on the model all along; this reads it.

    The company name is passed rather than inferred, and every caller that has
    a charge passes charge.company. Where a caller genuinely has no company in
    hand -- a token refresh, a connection test -- the single-file shortcut
    still applies, and the refusal still stands when there is more than one:
    "I don't know which" is the only safe answer to an ambiguous write.
    """
    rows = db.query(QboTokenRow).all()
    if not rows:
        raise WriteBlocked("QuickBooks is not connected")

    if company:
        from ..models_db import CompanyRow
        co = (db.query(CompanyRow)
              .filter(CompanyRow.name == company).first())
        if co is None:
            raise WriteBlocked(f"{company!r} is not a company in this engine")
        if co.realm_id:
            if not any(r.realm_id == co.realm_id for r in rows):
                raise WriteBlocked(
                    f"{company} is mapped to a QuickBooks file that is not "
                    f"connected — reconnect it on Configuration → Companies")
            return co.realm_id
        # No mapping yet. One connected file is unambiguous, so use it rather
        # than blocking a single-company install that never needed a mapping.
        if len(rows) == 1:
            return rows[0].realm_id
        raise WriteBlocked(
            f"{company} is not mapped to a QuickBooks company — "
            f"set it on Configuration → Companies before posting")

    if len(rows) > 1:
        raise WriteBlocked(
            "more than one QuickBooks company is connected and this operation "
            "did not say which — this is a bug, not a setup problem")
    return rows[0].realm_id


# Kept only so that anything still importing it does not break. Nothing reads
# it: the amount tolerances in the Matching Algorithm table are now the single
# answer to how far apart a charge and a bill may be.
MAX_ROUNDING_GAP = Decimal("0.25")


def _within_match_tolerance(charge_amount: Decimal, bill_amount: Decimal) -> bool:
    """Would the matcher have offered this pair at all?

    There is no separate rounding tolerance any more. There were two thresholds
    for one question -- how far apart may a charge and a bill be -- and the
    second one silently refused pairs the first had already offered, so a
    reviewer could see a suggestion, accept it, and be told it could not be
    posted.

    The amount tolerances are now the only answer. A bill close enough to be
    suggested is close enough to be corrected to the charge; a bill that is not
    is never offered in the first place.
    """
    try:
        from engine.suggest import current_limits
        lim = current_limits()
        pct = float(lim.get("gate_amount_pct", 0.0)) / 100.0
        abs_ = float(lim.get("gate_amount_abs", 0.0))
    except Exception:                       # noqa: BLE001
        return False
    gap = abs(Decimal(charge_amount) - Decimal(bill_amount))
    rel = float(gap) / max(float(abs(Decimal(bill_amount or 0))), 1.0)
    return rel <= pct or float(gap) <= abs_


def _overage_is_tolerated(charge_amount: Decimal, bill_amount: Decimal,
                          balance: Decimal) -> bool:
    """Is a charge above the balance a tolerance case rather than an overpayment?

    The tolerance is meant to work in both directions, but the balance guard
    made it one-sided: a charge UNDER its bill was waved through and corrected,
    while the same gap the other way was refused as an overpayment before the
    correction could run. It isn't an overpayment -- the bill is raised to the
    charge in the very next step, so the balance ends up covering it exactly.

    Two conditions, both required:

    - the gap is within the tolerance, so it would be corrected anyway;
    - NOTHING has been paid against the bill yet. On a partly-paid bill the
      shortfall may be real, and raising the amount to absorb an overage would
      quietly enlarge a liability someone has already part-settled. That case
      keeps the refusal.
    """
    if bill_amount is None or balance is None:
        return False
    if balance != bill_amount:
        return False                      # partly paid: not ours to widen
    return _within_match_tolerance(charge_amount, bill_amount)


def _rounding_gap_is_explained(charge_amount: Decimal, bill: BillRow) -> dict:
    """Is the charge-vs-bill difference a TicketVault rounding artifact?

    TicketVault builds a bill as round(round(total/qty) * qty), which loses a
    penny or two when the total doesn't divide evenly -- $100.00 over 3 tickets
    becomes $99.99. The charge is the true amount; the bill is the artifact.

    Proven case: the bill is EXACTLY what that arithmetic would produce from
    this charge. Plausible case: quantity is unknown but the gap is within the
    ceiling. Anything larger is a real discrepancy.

    The arithmetic is tried BEFORE the ceiling, because proof outranks a
    threshold. The loss is at most half a cent per ticket, so a 60-ticket bill
    can legitimately sit $0.30 out -- past the old flat ceiling, which ran
    first and threw the proof away.

    IN PRACTICE THAT BRANCH IS DORMANT. Quantity is read from a QuickBooks
    line's Qty field, and Y&S bills do not carry one, so bill.quantity is None
    on every row and the proven case has never once been reached. Every real
    decision is made by the ceiling below, on a gap nothing can corroborate.
    The branch is kept because it costs nothing and becomes correct the moment
    a ticket count reaches the ledger -- but do not read its presence as
    evidence that gaps are being verified today. They are not.
    """
    gap = (charge_amount - bill.amount).quantize(Decimal("0.01"))
    out = {"gap": gap, "explained": False, "proven": False, "reason": ""}

    if gap == 0:
        out["reason"] = "amounts already equal"
        return out

    if bill.line_count != 1:
        out["reason"] = (f"bill has {bill.line_count} lines — rounding "
                         f"correction only applies to single-line bills")
        return out

    qty = bill.quantity
    if qty and qty > 0:
        unit = (charge_amount / Decimal(qty)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        reconstructed = (unit * Decimal(qty)).quantize(Decimal("0.01"))
        # The arithmetic cannot lose more than half a cent a ticket, so a gap
        # beyond that didn't come from it however well the total reconciles.
        # Belt and braces against a coincidence on a very large quantity.
        arithmetic_max = (Decimal(qty) * Decimal("0.01")).quantize(Decimal("0.01"))
        if reconstructed == bill.amount and abs(gap) <= arithmetic_max:
            out.update(explained=True, proven=True,
                       reason=(f"bill = round({charge_amount}/{qty}) x {qty} "
                               f"= {bill.amount}; TicketVault rounding"))
            return out
        out["reason"] = (f"gap {gap} does not match TicketVault arithmetic "
                         f"for {qty} tickets (would be {reconstructed})")
        return out

    # No quantity, so there is nothing to check the gap against. The pair was
    # offered by the matcher, which is the whole test now -- but the reason
    # says plainly that it was assumed rather than shown, because an audit entry
    # reading "TicketVault rounding" would claim more than was established.
    if not _within_match_tolerance(charge_amount, bill.amount):
        out["reason"] = (f"gap {gap} is outside the amount tolerances "
                         f"— treated as a real discrepancy")
        return out

    out.update(explained=True, proven=False,
               reason=(f"gap {gap} assumed to be rounding: within the amount "
                       f"tolerances, no ticket count to verify it against"))
    return out


def match_is_postable(charge_amount: Decimal, bill: BillRow,
                      forced: str = "") -> dict:
    """Would a BillPayment for this charge/bill pair actually be accepted?

    The review queue used to offer candidates the write path then refused, so a
    reviewer could click Match, watch the row move to Categorized, and only then
    be told the gap was a real discrepancy. The verdict now lives in one place
    and both sides ask it.

    Note gap == 0 returns explained=False (there is nothing to explain), so it
    has to be admitted separately.
    """
    # A partly-paid bill can still look like a perfect amount match -- the gap
    # is measured against the bill's ORIGINAL amount, not what's left owed. The
    # write path refuses to overpay, so the queue must not offer it either.
    balance = bill.balance if bill.balance is not None else bill.amount

    # A FORCED match in "keep" mode is a deliberate part payment: the bill keeps
    # its amount and the charge pays what it covers. Overpaying is still refused
    # -- QuickBooks would reject it, and "pay more than is owed" is not a thing
    # a reviewer can mean.
    if forced == "keep":
        if balance is not None and charge_amount > balance:
            return {"ok": False, "gap": charge_amount - (bill.amount or 0),
                    "reason": f"only {balance} still owed — paying "
                              f"{charge_amount} would overpay the bill"}
        return {"ok": True, "gap": (charge_amount - (bill.amount or 0)),
                "reason": "forced match — bill amount left as it is, "
                          "paying only what the charge covers"}

    v = _rounding_gap_is_explained(charge_amount, bill)
    # ADJUST or OTHER FEES: the reviewer has said the bill grows to what the
    # charge pays -- so "more than is owed" is the point, not an overpayment.
    # (Answered BEFORE the overpay test, which refused these on a part-paid bill.)
    if forced in ("adjust", "fees"):
        return {"ok": True, "gap": v["gap"],
                "reason": ((f"forced match — difference added as an Other Fees line"
                            if forced == "fees" else f"forced match — bill adjusted to the charge")
                           + (f" (gap {v['gap']})" if v["gap"] else ""))}

    if (balance is not None and charge_amount > balance
            and not _overage_is_tolerated(charge_amount, bill.amount, balance)):
        return {"ok": False, "gap": charge_amount - (bill.amount or 0),
                "reason": f"only {balance} still owed — paying {charge_amount} "
                          f"would overpay the bill"}

    # "adjust" means the reviewer has said the bill is wrong and the charge is
    # right, so the tolerance question does not arise -- the bill is corrected
    # to the charge before the payment is written. Said plainly in the reason,
    # because this is somebody's decision and should never read as though the
    # engine established it.
    return {"ok": v["gap"] == 0 or v["explained"],
            "gap": v["gap"], "reason": v["reason"]}


OTHER_FEES_ACCOUNT = "Other Fees"


def _other_fees_ref(db: Session, realm_id: str) -> dict:
    """The "Other Fees" account in THIS company's QuickBooks file."""
    from ..models_db import QboRefRow
    row = (db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                      QboRefRow.realm_id == realm_id)
           .filter(QboRefRow.name.ilike(OTHER_FEES_ACCOUNT)).first())
    if row is None:
        raise WriteFailed(f'there is no "{OTHER_FEES_ACCOUNT}" account in this '
                          f"company's QuickBooks file -- add one, then Sync from QBO")
    return {"value": row.qbo_id, "name": row.name}


def correct_bill_amount(db: Session, realm_id: str, bill: BillRow,
                        new_amount: Decimal, actor: str,
                        mode: str = "amount") -> dict:
    """Rewrite a single-line bill's amount to the true charge amount.

    A sparse update against the bill's CURRENT SyncToken -- re-read first, since
    the TicketVault import may have touched the bill since it was synced, and a
    stale token would either fail or clobber that change.
    """
    assert_writes_allowed()
    qbo_tokens.assert_realm_allowed(realm_id, db)
    token = qbo_tokens.get_access_token(db, realm_id)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}

    r = httpx.get(f"{base}/bill/{bill.bill_id}", headers=headers,
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read bill {bill.bill_id} "
                          f"({r.status_code}): {r.text[:200]}")
    current = r.json().get("Bill", {})
    lines = current.get("Line", [])
    if len(lines) != 1:
        raise WriteFailed(f"bill {bill.bill_id} has {len(lines)} lines in "
                          f"QuickBooks — refusing to rewrite the amount")

    old_amount = Decimal(str(current.get("TotalAmt", bill.amount)))
    # AN "OTHER FEES" LINE instead of a new amount: the bill's own line is left
    # exactly as it was and the difference is added beside it. Only for a bill
    # SMALLER than its charge -- a line can add to a bill, not take from it --
    # so a larger bill has its amount corrected as before.
    # BOTH WAYS: a bill larger than its charge gets a NEGATIVE Other Fees line
    # (QuickBooks takes one while the bill's total stays positive). Only if
    # QuickBooks refuses it is the amount changed instead -- and then the
    # result says so (fees_refused), rather than claiming a line was added.
    add_fees = mode == "fees" and new_amount != old_amount and new_amount > 0
    fees_refused = ""
    if add_fees:
        fees = _other_fees_ref(db, realm_id)
        diff = (new_amount - old_amount).quantize(Decimal("0.01"))
        vendor_ref = current.get("VendorRef")
        if not vendor_ref:
            raise WriteFailed(f"bill {bill.bill_id} came back from QuickBooks without "
                              f"a VendorRef — cannot safely add a line to it")
        payload = {"Id": current["Id"], "SyncToken": current["SyncToken"],
                   "sparse": True, "VendorRef": vendor_ref,
                   "Line": [dict(lines[0]), {
                       "DetailType": "AccountBasedExpenseLineDetail",
                       # THE FIRST LINE'S MEMO, carried onto the new line.
                       "Amount": float(diff), "Description": (lines[0].get("Description") or OTHER_FEES_ACCOUNT),
                       "AccountBasedExpenseLineDetail": {"AccountRef": {
                           "value": fees["value"], "name": fees["name"]}}}],
                   "TotalAmt": float(new_amount)}
        for k in ("CurrencyRef", "APAccountRef", "TxnDate"):
            if current.get(k):
                payload[k] = current[k]
        u = httpx.post(f"{base}/bill", headers=headers,
                       params={"minorversion": MINOR_VERSION}, json=payload, timeout=60)
        if u.status_code >= 400:
            if "5010" in u.text or "Stale Object" in u.text:
                raise WriteFailed("Bill already matched")
            if diff < 0:
                # A negative line refused: fall through to changing the amount,
                # and say so.
                fees_refused = (f"QuickBooks would not take a negative {OTHER_FEES_ACCOUNT} line "
                                f"({u.status_code}); the bill's amount was changed instead")
                log.info("negative fee line refused on bill %s: %s", bill.bill_id, u.text[:200])
            else:
                raise WriteFailed(f"could not add the Other Fees line to bill "
                                  f"{bill.bill_id} ({u.status_code}): {u.text[:300]}")
        if fees_refused:
            add_fees = False
    if add_fees:
        updated = u.json().get("Bill", {})
        bill.amount = new_amount
        bill.balance = new_amount
        db.add(AuditRow(
            company=bill.company, action="bill_fees_line_added",
            bill_id=bill.bill_id,
            detail=f"{old_amount} -> {new_amount}: added a ${diff} "
                   f"{OTHER_FEES_ACCOUNT} line by {actor}",
            dry_run=False, actor=actor))
        db.commit()
        log.info("added %s %s line to bill %s", diff, OTHER_FEES_ACCOUNT, bill.bill_id)
        return {"bill_id": bill.bill_id, "from": str(old_amount),
                "to": str(new_amount), "fees_line": str(diff),
                "sync_token": updated.get("SyncToken")}
    line = dict(lines[0])
    line["Amount"] = float(new_amount)
    for detail_key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
        if detail_key in line and isinstance(line[detail_key], dict):
            d = dict(line[detail_key])
            # keep unit price consistent where the line carries one
            if "Qty" in d and d.get("Qty"):
                try:
                    d["UnitPrice"] = float(
                        (new_amount / Decimal(str(d["Qty"]))).quantize(
                            Decimal("0.000001")))
                except Exception:
                    pass
            line[detail_key] = d

    # A sparse update still has to carry the entity's REQUIRED fields, and for
    # a Bill that means VendorRef. Leaving it out returns a 2020 validation
    # fault ("Required parameter VendorRef is missing"), which reads like a bug
    # in our payload because it is one. We already re-read the bill above, so
    # carry its own refs straight back.
    vendor_ref = current.get("VendorRef")
    if not vendor_ref:
        raise WriteFailed(
            f"bill {bill.bill_id} came back from QuickBooks without a VendorRef "
            f"— cannot safely rewrite its amount")
    payload = {
        "Id": current["Id"],
        "SyncToken": current["SyncToken"],
        "sparse": True,
        "VendorRef": vendor_ref,
        "Line": [line],
        "TotalAmt": float(new_amount),
    }
    # Optional, but a bill in a non-home currency needs these preserved or the
    # update is rejected on the same "required field" grounds.
    for k in ("CurrencyRef", "APAccountRef", "TxnDate"):
        if current.get(k):
            payload[k] = current[k]
    u = httpx.post(f"{base}/bill", headers=headers,
                   params={"minorversion": MINOR_VERSION}, json=payload, timeout=60)
    if u.status_code >= 400:
        # 5010 is Intuit's Stale Object Error: the bill changed between our read
        # and our write, which in practice means someone matched it first. The
        # raw fault names both people and reads like a system failure, so say
        # what actually happened.
        if "5010" in u.text or "Stale Object" in u.text:
            raise WriteFailed("Bill already matched")
        raise WriteFailed(f"could not correct bill {bill.bill_id} "
                          f"({u.status_code}): {u.text[:300]}")

    updated = u.json().get("Bill", {})
    bill.amount = new_amount
    bill.balance = new_amount
    db.add(AuditRow(
        company=bill.company, action="bill_amount_corrected",
        bill_id=bill.bill_id,
        detail=f"{old_amount} -> {new_amount} (TicketVault rounding) by {actor}",
        dry_run=False, actor=actor))
    db.commit()
    log.info("corrected bill %s: %s -> %s", bill.bill_id, old_amount, new_amount)
    return {"bill_id": bill.bill_id, "from": str(old_amount),
            "to": str(new_amount), "sync_token": updated.get("SyncToken"), "note": fees_refused}


VENDORS_OPEN_ACCOUNT = "Due from Vendors - Open"


def _vendors_open_ref(db: Session, realm_id: str) -> dict:
    """The "Due from Vendors - Open" account in this company's file."""
    from ..models_db import QboRefRow
    q = db.query(QboRefRow).filter(QboRefRow.kind == "account", QboRefRow.realm_id == realm_id)
    row = (q.filter(QboRefRow.name.ilike(VENDORS_OPEN_ACCOUNT)).first()
           or q.filter(QboRefRow.name.ilike("%due from vendor%open%")).first())
    if row is None:
        raise WriteFailed(f'there is no "{VENDORS_OPEN_ACCOUNT}" account in this '
                          f"company's QuickBooks file -- add one, then Sync from QBO")
    return {"value": row.qbo_id, "name": row.name}


def adjust_for_vendors_open(db: Session, realm_id: str, bill: BillRow, actor: str) -> dict:
    """ADJUST FOR VENDORS OPEN: a negative line on "Due from Vendors - Open"
    for the bill's open balance, so the balance comes to zero. Every existing
    line stays as it is; the new line carries the first line's memo."""
    assert_writes_allowed()
    qbo_tokens.assert_realm_allowed(realm_id, db)
    current = _read_bill_live(db, realm_id, bill.bill_id)
    lines = [dict(l) for l in (current.get("Line") or []) if l.get("DetailType") != "SubTotalLineDetail"]
    if not lines:
        raise WriteFailed(f"bill {bill.doc_number or bill.bill_id} has no lines in QuickBooks")
    total = Decimal(str(current.get("TotalAmt", bill.amount or 0)))
    balance = Decimal(str(current.get("Balance", bill.balance or 0))).quantize(Decimal("0.01"))
    if balance <= 0:
        raise WriteFailed(f"bill {bill.doc_number or bill.bill_id} has no open balance")
    vendor_ref = current.get("VendorRef")
    if not vendor_ref:
        raise WriteFailed(f"bill {bill.bill_id} came back from QuickBooks without a VendorRef")
    acct = _vendors_open_ref(db, realm_id)
    new_total = (total - balance).quantize(Decimal("0.01"))
    payload = {"Id": current["Id"], "SyncToken": current["SyncToken"], "sparse": True,
               "VendorRef": vendor_ref,
               "Line": lines + [{
                   "DetailType": "AccountBasedExpenseLineDetail", "Amount": float(-balance),
                   "Description": lines[0].get("Description") or VENDORS_OPEN_ACCOUNT,
                   "AccountBasedExpenseLineDetail": {"AccountRef": acct}}],
               "TotalAmt": float(new_total)}
    for k in ("CurrencyRef", "APAccountRef", "TxnDate"):
        if current.get(k):
            payload[k] = current[k]
    body = _post(db, realm_id, "bill", payload)
    upd = body.get("Bill", {})
    bill.amount = Decimal(str(upd.get("TotalAmt", new_total)))
    bill.balance = Decimal(str(upd.get("Balance", 0)))
    db.add(AuditRow(company=bill.company, action="bill_vendors_open_line_added", bill_id=bill.bill_id,
                    detail=f"-{balance} {acct['name']} line added (balance to zero) by {actor}",
                    dry_run=False, actor=actor))
    db.commit()
    return {"bill_id": bill.bill_id, "line": str(-balance), "to": str(bill.amount)}


def _read_live(db: Session, realm_id: str, entity: str, obj_id: str) -> dict:
    """Fetch any one object from QuickBooks as it stands right now."""
    token = qbo_tokens.get_access_token(db, realm_id)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm_id}"
    r = httpx.get(f"{base}/{entity}/{obj_id}",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"},
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read {entity} {obj_id} from QuickBooks "
                          f"({r.status_code}): {r.text[:200]}")
    return r.json().get(entity.capitalize(), {})


def _read_bill_live(db: Session, realm_id: str, bill_id: str) -> dict:
    """Fetch one bill from QuickBooks as it stands right now."""
    token = qbo_tokens.get_access_token(db, realm_id)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm_id}"
    r = httpx.get(f"{base}/bill/{bill_id}",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"},
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read bill {bill_id} from QuickBooks "
                          f"({r.status_code}): {r.text[:200]}")
    return r.json().get("Bill", {})


def _refresh_bill_from_live(db: Session, bill: BillRow, live: dict) -> None:
    """Bring the local mirror in step with what QuickBooks just told us."""
    try:
        bill.amount = Decimal(str(live.get("TotalAmt", bill.amount)))
        bill.balance = Decimal(str(live.get("Balance", bill.balance)))
    except Exception:
        return


def post_bill_payment(db: Session, charge: ChargeRow, actor: str) -> dict:
    """ONE PAYMENT AT A TIME PER BILL. Two different charges paying the same bill
    at the same moment each held their own charge's claim, and the "already
    matched" check below only sees a payment once it has FINISHED -- so both
    could pay it. The bill is claimed too; the second is refused, by name."""
    key = f"posting:bill:{charge.matched_bill_id}"
    if charge.matched_bill_id and not _claim_post("", key=key):
        raise WriteFailed("Bill is being paid by another charge right now")
    try:
        return _post_bill_payment_claimed(db, charge, actor)
    finally:
        if charge.matched_bill_id:
            _release_post("", key=key)


def _post_bill_payment_claimed(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Pay down a matched bill from the card's account.

    If the bill is a penny or two under the charge because of TicketVault's
    rounding, the BILL is corrected first and then paid in full. Paying the
    charge amount against an uncorrected bill would leave it overpaid, and
    thousands of those would litter A/P with small credit balances.
    """
    assert_writes_allowed()
    realm = _realm_for(db, charge.company)
    acct_id, acct_name = resolve_card_account(db, charge)

    if charge.is_credit:
        raise WriteFailed(
            "this is a refund — money came back, so it can't pay down a bill. "
            "Code it as an expense/refund instead.")

    bill = db.get(BillRow, charge.matched_bill_id)
    if bill is None:
        raise WriteFailed(f"bill {charge.matched_bill_id} is not in the local pool")

    # Check the LIVE bill before doing anything to it. The local mirror is
    # refreshed on a cron, so it can be minutes stale -- and during rollout,
    # while people are still paying bills directly in QuickBooks, minutes is
    # long enough for someone to match a charge to a bill that is already
    # settled. Paying it again would create an overpayment sitting in A/P as a
    # vendor credit, which is far harder to spot and unwind than a refusal.
    #
    # One extra GET per payment. Payments happen at human speed, so the cost is
    # irrelevant next to what it prevents.
    # Checked BEFORE asking QuickBooks, because QuickBooks can lag: two tabs
    # matching different charges to the same bill seconds apart could both read
    # a positive balance and both post, leaving an overpayment nobody notices.
    # Our own draw-down is immediate, so it closes that window.
    other = db.query(ChargeRow).filter(
        ChargeRow.company == charge.company,
        ChargeRow.matched_bill_id == bill.bill_id,
        ChargeRow.charge_id != charge.charge_id,
        ChargeRow.qbo_txn_id != "").first()
    if other is not None:
        # Matched inside the app -- by anyone, in any tab.
        raise WriteFailed("Bill already matched")
    if (bill.balance or 0) <= 0:
        # Balance is zero but nothing here paid it, so it was settled directly
        # in QuickBooks. Saying "already matched" would send someone looking
        # for a match in this app that doesn't exist.
        raise WriteFailed("Bill already paid")

    live = _read_bill_live(db, realm, bill.bill_id)
    live_balance = Decimal(str(live.get("Balance", "0")))
    _refresh_bill_from_live(db, bill, live)

    if live_balance <= 0:
        # Self-healing: the mirror now says what QuickBooks says, so the bill
        # drops out of the candidate pool and the Bills Available tab straight
        # away rather than waiting for the next sync to prune it.
        db.add(AuditRow(
            company=charge.company, action="bill_already_paid",
            charge_id=charge.charge_id, bill_id=bill.bill_id,
            detail=f"bill {bill.doc_number or bill.bill_id} had balance "
                   f"{live_balance} in QuickBooks at post time; refused",
            dry_run=False, actor=actor))
        db.commit()
        # Deliberately terse. The specifics -- which bill, what balance QBO
        # reported, when -- are in the audit entry written just above.
        raise WriteFailed("Bill already paid")

    # Same tolerance, same both directions -- and against the LIVE figures, so
    # a bill edited in QuickBooks since the last sync is judged on what it says
    # now rather than on the mirror.
    # What this charge applies to THIS bill. On a multi-bill match the charge
    # deliberately exceeds any single bill -- that is the point of splitting it
    # -- so comparing the whole charge against the first bill's balance
    # rejected every multi-bill force match as an overpayment.
    _applied = charge.amount
    try:
        from ..models_db import ChargeBillMatchRow as _CBM2
        _line = (db.query(_CBM2)
                 .filter(_CBM2.charge_id == charge.charge_id,
                         _CBM2.bill_id == bill.bill_id).first())
        if _line is not None:
            _applied = Decimal(str(_line.amount))
    except Exception:                       # noqa: BLE001
        _applied = charge.amount

    _fm = (getattr(charge, "force_mode", "") or "") if getattr(charge, "force_matched", False) else ""
    if (_fm not in ("adjust", "fees") and _applied > live_balance
            and not _overage_is_tolerated(_applied, bill.amount, live_balance)):
        db.commit()
        raise WriteFailed(
            f"${_applied} applied to bill {bill.doc_number or bill.bill_id} is "
            f"more than the ${live_balance} still owed on it in QuickBooks — "
            f"paying it would overpay the bill. Nothing was posted.")

    correction = None
    # A reviewer's override, if there was one. "keep" means leave the bill and
    # pay only what the charge covers -- a deliberate part payment, so there is
    # no gap to explain and nothing to correct. "adjust" means the bill is wrong
    # and the charge is right, so it is corrected without asking the tolerances,
    # which the reviewer has already overruled.
    forced = (getattr(charge, "force_mode", "") or "") if getattr(
        charge, "force_matched", False) else ""
    # HOW A CORRECTION IS MADE: the bill's amount changed, or an "Other Fees"
    # line added for the difference. Force match can choose fees outright;
    # otherwise the company's setting decides.
    _how = "amount"
    if forced == "fees":
        _how, forced = "fees", "adjust"
    elif not forced:
        from ..models_db import AppSettingRow as _ASf
        _fs = db.get(_ASf, f"bill_fix_fees:{charge.company}")
        if _fs is not None and (_fs.value or "") == "true":
            _how = "fees"

    # Computed against the LIVE amount, so a bill edited in QuickBooks since
    # the last sync isn't "corrected" against a stale figure.
    finding = _rounding_gap_is_explained(charge.amount, bill)
    if forced == "keep":
        pass                    # bill untouched; the payment covers part of it
    elif finding["gap"] != 0:
        if forced != "adjust" and not finding["explained"]:
            raise WriteFailed(
                f"charge ${charge.amount} vs bill ${bill.amount}: "
                f"{finding['reason']}. Not posted — review this pair.")
        # ALREADY PAID + THIS CHARGE: on a part-paid bill, a total of just the
        # charge would still leave it overpaid; this way the charge settles it.
        _paid = max(Decimal("0"), Decimal(str(bill.amount or 0)) - Decimal(str(live_balance)))
        correction = correct_bill_amount(db, realm, bill, charge.amount + _paid, actor,
                                         mode=_how)
        correction["reason"] = (
            f"forced by {actor}: bill corrected to the charge"
            if forced == "adjust" else finding["reason"])
        # Never "proven" on an override: somebody decided this, the engine did
        # not establish it, and the audit entry has to say which.
        correction["proven"] = False if forced == "adjust" else finding["proven"]

    # One line per bill. A charge that settles several bills has rows in
    # charge_bill_matches; anything else has none and pays the single bill it is
    # matched to, exactly as before.
    #
    # QuickBooks allows ONE VendorRef per BillPayment, so every bill in a
    # multi-bill match has to belong to the same vendor. That is checked before
    # the match is stored, not here -- by this point it is too late to tell
    # anybody.
    lines = _bill_match_lines(db, charge, bill)
    payload = {
        "VendorRef": {"value": bill.vendor_id or ""} if getattr(bill, "vendor_id", "")
                     else {"name": bill.vendor or ""},
        "TxnDate": charge.txn_date.isoformat(),
        "PayType": "CreditCard",
        "CreditCardPayment": {"CCAccountRef": {"value": acct_id, "name": acct_name}},
        "TotalAmt": float(charge.amount),
        "Line": lines,
        # Just the source description. Provenance lives in the audit log; the
        # ledger memo should read the way the team writes it.
        "PrivateNote": engine_memo_for(charge.company, charge_memo_text(charge)),
    }
    body = _post(db, realm, "billpayment", payload)
    created = body.get("BillPayment", {})
    if correction:
        created["_bill_correction"] = correction
    return created


def _bill_match_lines(db: Session, charge: ChargeRow, bill: BillRow) -> list:
    """The BillPayment lines for this charge: one per bill it settles.

    Falls back to a single line for the matched bill, which is every charge that
    predates multi-bill matching. The lines must total the charge -- QuickBooks
    rejects a payment whose lines disagree with its TotalAmt -- so the remainder
    from rounding is put on the last line rather than left to drift.
    """
    from ..models_db import ChargeBillMatchRow
    rows = (db.query(ChargeBillMatchRow)
            .filter(ChargeBillMatchRow.charge_id == charge.charge_id)
            .order_by(ChargeBillMatchRow.position, ChargeBillMatchRow.id).all())
    if len(rows) < 2:
        return [{"Amount": float(charge.amount),
                 "LinkedTxn": [{"TxnId": bill.bill_id, "TxnType": "Bill"}]}]
    total = Decimal(str(charge.amount))
    acc = Decimal("0")
    out = []
    for i, r in enumerate(rows):
        amt = (total - acc) if i == len(rows) - 1 else Decimal(str(r.amount))
        acc += amt
        out.append({"Amount": float(amt),
                    "LinkedTxn": [{"TxnId": r.bill_id, "TxnType": "Bill"}]})
    return out


def post_expense(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Record a coded charge in QuickBooks."""
    entity, payload = _expense_payload(db, charge)
    realm = _realm_for(db, charge.company)
    body = _post(db, realm, entity, payload)
    return body.get("Deposit" if entity == "deposit" else "Purchase", {})


def _split_lines(db: Session, charge: ChargeRow) -> list:
    """The charge's split rows, in order, or an empty list if it isn't split.

    Read here rather than passed in so that every path that builds a payload --
    the first post and every later correction -- sees the same split. An edit
    that silently dropped the lines would post the whole amount to one
    category and look like it had worked.
    """
    from ..models_db import ChargeSplitRow
    try:
        return (db.query(ChargeSplitRow)
                .filter(ChargeSplitRow.charge_id == charge.charge_id)
                .order_by(ChargeSplitRow.position, ChargeSplitRow.id).all())
    except Exception:                       # noqa: BLE001
        return []                           # table not there yet: unsplit


def _splits_are_whole(charge: ChargeRow, splits: list) -> None:
    """Refuse to post a split that doesn't add up.

    QuickBooks would accept lines summing to less than TotalAmt and quietly
    balance the difference somewhere nobody looks, so the check happens here
    rather than being left to the API.
    """
    if not splits:
        return
    total = sum((Decimal(s.amount) for s in splits), Decimal("0"))
    if total != Decimal(charge.amount):
        raise WriteFailed(
            f"split lines total ${total} but the charge is ${charge.amount} — "
            f"nothing was posted. Re-open the split and correct it.")


def _expense_payload(db: Session, charge: ChargeRow) -> tuple[str, dict]:
    """The QuickBooks object for a coded charge.

    Shared by the initial post and by later edits, so a corrected category
    lands the same way it would have if it had been right the first time.

    Four combinations, and they map to different QuickBooks objects:

        credit card + spend   Purchase (PaymentType CreditCard) -> Expense
        credit card + refund  Purchase with Credit=true -> Credit Card Credit
        bank + spend          Purchase (PaymentType Cash) -> Expense
        bank + refund         Deposit

    PaymentType Cash rather than Check for bank spending: QuickBooks renders a
    Check-type Purchase as a check (expecting a check number), while Cash
    renders as an Expense, which is what these card charges actually are.

    Posting a bank refund as a credit-card credit would put it on the wrong kind
    of account and wouldn't reconcile, so the account type decides the shape.
    """
    assert_writes_allowed()
    realm = _realm_for(db, charge.company)
    acct_id, acct_name = resolve_card_account(db, charge)

    # Read before the category check: a split charge carries its categories on
    # its lines, and demanding one on the charge as well would refuse a split
    # that is completely specified.
    splits = _split_lines(db, charge)
    _splits_are_whole(charge, splits)
    if not charge.coded_category and not splits:
        raise WriteFailed("no category set on this charge")
    # An id, not just a name: QuickBooks will not accept an AccountRef without
    # one, and the error it returns names the missing field rather than the
    # split line that caused it.
    missing = [s.position + 1 for s in splits if not s.category_id]
    if missing:
        raise WriteFailed(
            "split line(s) " + ", ".join(str(m) for m in missing)
            + " have no QuickBooks account. Re-open the split and choose a "
              "category from the list.")

    is_bank = account_type(db, acct_id).lower().startswith("bank")
    # CUSTOM NOTES in each line's description too, as in the memo: after the
    # text, in brackets, before {Engine}.
    fallback_desc = charge_memo_text(charge)
    _note = str(getattr(charge, "custom_note", "") or "").strip()

    def _line_desc(desc):
        d = str(desc or "").strip()
        if not d:
            return fallback_desc
        return f"{d} ({_note})" if _note and f"({_note})" not in d else d
    # QUICKBOOKS NEEDS IDS. A charge coded by name only -- from a rule or a
    # suggestion -- sent {"name": ...}, which QuickBooks does not look up, and
    # the write failed with a cryptic missing-field error. The ID is found by
    # name in THIS company's file; if it isn't there, the post is refused with
    # a plain reason before anything is sent.
    from ..review import ref_by_name as _rbn

    def _id_for(kind, name, have):
        if have:
            return have
        hit = _rbn(db, charge.company, kind, name) if name else None
        if hit is None:
            raise WriteFailed(f"'{name}' isn't {'an account' if kind == 'account' else 'a vendor'} "
                              f"in this company's QuickBooks file")
        return hit.qbo_id

    # --- money coming back into a bank account: a Deposit ---
    if charge.is_credit and is_bank:
        def deposit_line(amount, cat, cat_id, desc):
            ln: dict = {
                "Amount": float(amount),
                "DetailType": "DepositLineDetail",
                "DepositLineDetail": {
                    "AccountRef": {"value": _id_for("account", cat, cat_id)},
                },
                "Description": engine_memo_for(charge.company, _line_desc(desc)),
            }
            if charge.coded_vendor_id or charge.coded_vendor:
                ln["DepositLineDetail"]["Entity"] = {
                    "value": _id_for("vendor", charge.coded_vendor, charge.coded_vendor_id), "type": "Vendor"}
            return ln

        lines = ([deposit_line(s.amount, s.category, s.category_id, s.description)
                  for s in splits] if splits else
                 [deposit_line(charge.amount, charge.coded_category,
                               charge.coded_category_id, "")])
        payload = {
            "DepositToAccountRef": {"value": acct_id, "name": acct_name},
            "TxnDate": charge.txn_date.isoformat(),
            "TotalAmt": float(charge.amount),
            "Line": lines,
            "PrivateNote": engine_memo_for(charge.company, charge_memo_text(charge)),
        }
        return "deposit", payload

    # Prefer the id: QuickBooks resolves by `value` when present, so a category
    # renamed since coding still posts correctly. Name is a fallback for
    # charges coded before ids were captured.
    def expense_line(amount, cat, cat_id, desc):
        return {
            "Amount": float(amount),
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": _id_for("account", cat, cat_id)},
            },
            "Description": engine_memo_for(charge.company, _line_desc(desc)),
        }

    lines = ([expense_line(s.amount, s.category, s.category_id, s.description)
              for s in splits] if splits else
             [expense_line(charge.amount, charge.coded_category,
                           charge.coded_category_id, "")])
    payload = {
        # Cash -> QuickBooks shows an Expense; Check would show a check.
        "PaymentType": "Cash" if is_bank else "CreditCard",
        "AccountRef": {"value": acct_id, "name": acct_name},
        "TxnDate": charge.txn_date.isoformat(),
        "TotalAmt": float(charge.amount),
        "Line": lines,
        "PrivateNote": engine_memo_for(charge.company, charge_memo_text(charge)),
    }
    if charge.coded_vendor_id:
        payload["EntityRef"] = {"value": charge.coded_vendor_id, "type": "Vendor"}
    elif charge.coded_vendor:
        payload["EntityRef"] = {"value": _id_for("vendor", charge.coded_vendor, ""), "type": "Vendor"}
    if charge.is_credit:
        # Credit card only -- the bank case returned a Deposit above.
        payload["Credit"] = True

    return "purchase", payload


def update_posted_charge(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Push an edited charge to the transaction QuickBooks already holds.

    A correction made here has to reach the ledger, or the app quietly becomes
    a second set of books that disagrees with the first.

    Sparse update, so only what we send changes -- but QuickBooks still
    requires an entity's mandatory fields in a sparse write, which is why the
    whole payload is rebuilt from _expense_payload rather than patched. The
    SyncToken is re-read first: a stale one is refused, and ours can be stale
    if anyone touched the transaction in QuickBooks since we wrote it.
    """
    assert_writes_allowed()
    if not charge.qbo_txn_id:
        return {"status": "skipped", "reason": "not in QuickBooks"}

    realm = _realm_for(db, charge.company)
    entity = {"Purchase": "purchase", "Deposit": "deposit",
              "BillPayment": "billpayment"}.get(charge.qbo_txn_type or "")
    if not entity:
        return {"status": "skipped",
                "reason": f"cannot edit {entity_word(charge.qbo_txn_type)}"}

    token = qbo_tokens.get_access_token(db, realm)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm}"
    r = httpx.get(f"{base}/{entity}/{charge.qbo_txn_id}",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"},
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read the QuickBooks transaction "
                          f"({r.status_code}): {r.text[:200]}")
    key = {"purchase": "Purchase", "deposit": "Deposit",
           "billpayment": "BillPayment"}[entity]
    current = r.json().get(key, {})
    sync = str(current.get("SyncToken", charge.qbo_sync_token or "0"))

    if entity == "billpayment":
        # A bill payment's amount and linked bill are not ours to rewrite --
        # only the note that explains it.
        payload = {"Id": charge.qbo_txn_id, "SyncToken": sync, "sparse": True,
                   "PrivateNote": engine_memo_for(charge.company, charge_memo_text(charge)),
                   "VendorRef": current.get("VendorRef"),
                   "TotalAmt": current.get("TotalAmt")}
    else:
        # A FULL update, built by overlaying our changes onto the document
        # QuickBooks just gave us.
        #
        # Sparse looked right and wasn't: a sparse Purchase update applies the
        # fields it recognizes but leaves the payee as it was, so editing the
        # vendor changed our copy and nothing in the ledger. A full update
        # replaces the object, so the payee actually moves -- and echoing the
        # current document back means nothing we don't model (class, department,
        # attachments, doc number) is lost in the process.
        _, built = _expense_payload(db, charge)
        payload = {k: v for k, v in current.items()
                   if k not in ("MetaData", "domain", "sparse")}
        payload["PrivateNote"] = built.get("PrivateNote", "")

        # The payee. Removing it entirely is meaningful too, so an absent
        # EntityRef clears rather than silently keeping the old one.
        if "EntityRef" in built:
            payload["EntityRef"] = built["EntityRef"]
        else:
            payload.pop("EntityRef", None)

        # Line by line, by position, keeping each existing line's Id: without
        # the Id a full update deletes the original line and adds a new one,
        # which reshuffles the transaction for no reason.
        #
        # POSITIONALLY, and for as many lines as we are sending. An earlier
        # version merged only the FIRST line and kept the rest of the document's
        # lines as they were, which was fine while every charge was one line and
        # silently wrong the moment splits arrived: the extra lines never
        # reached QuickBooks, so a split saved here left a single-line
        # transaction over there at the old amount.
        new_lines = built.get("Line") or []
        old_lines = payload.get("Line") or []
        merged_lines = []
        for i, new_line in enumerate(new_lines):
            if i < len(old_lines):
                merged = dict(old_lines[i])
                # Merge the detail object rather than replacing it. A flat
                # update would swap out AccountBasedExpenseLineDetail wholesale
                # and take ClassRef, CustomerRef and BillableStatus with it --
                # fields the app never sets but the ledger relies on.
                for k, v in new_line.items():
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        sub = dict(merged[k])
                        sub.update(v)
                        merged[k] = sub
                    else:
                        merged[k] = v
                merged_lines.append(merged)
            else:
                # A line the transaction did not have. No Id: QuickBooks
                # assigns one.
                merged_lines.append(dict(new_line))
        # Lines beyond what we are sending are dropped deliberately -- a split
        # reduced from three parts to two must lose the third over there too.
        payload["Line"] = merged_lines

        payload.update({"Id": charge.qbo_txn_id, "SyncToken": sync})

    body = _post(db, realm, entity, payload)
    updated = body.get(key, {})
    charge.qbo_sync_token = str(updated.get("SyncToken", sync))
    db.add(AuditRow(company=charge.company, action="qbo_updated",
                    charge_id=charge.charge_id,
                    detail=f"updated {entity_word(charge.qbo_txn_type)} {charge.qbo_txn_id} "
                           f"in QuickBooks by {actor}",
                    dry_run=False, actor=actor))
    db.commit()
    return {"status": "updated", "id": charge.qbo_txn_id}


# ONE POSTING AT A TIME PER CHARGE. "Already posted?" was checked and then the
# write made, with nothing between them -- two posts of one charge arriving
# together (a double click, two people, two processes) could both pass the
# check and write it twice. A claim is taken first, through the database's own
# uniqueness rule, so it holds across requests and processes alike; a claim
# left by a crash expires.
_CLAIM_MINUTES = 10


def _claim_post(charge_id: str, key: str = "") -> bool:
    from datetime import datetime, timedelta
    from sqlalchemy.exc import IntegrityError
    from ..db import SessionLocal
    from ..models_db import AppSettingRow
    key = key or f"posting:{charge_id}"
    for _ in range(2):
        s = SessionLocal()
        try:
            s.add(AppSettingRow(key=key, value=datetime.utcnow().isoformat()))
            s.commit()
            return True
        except IntegrityError:
            s.rollback()
            row = s.get(AppSettingRow, key)
            try:
                stale = row is not None and datetime.fromisoformat(row.value) < (
                    datetime.utcnow() - timedelta(minutes=_CLAIM_MINUTES))
            except (TypeError, ValueError):
                stale = True
            if not stale:
                return False
            s.delete(row)
            s.commit()
        finally:
            s.close()
    return False


def _release_post(charge_id: str, key: str = "") -> None:
    from ..db import SessionLocal
    from ..models_db import AppSettingRow
    s = SessionLocal()
    try:
        row = s.get(AppSettingRow, key or f"posting:{charge_id}")
        if row is not None:
            s.delete(row)
            s.commit()
    finally:
        s.close()


def push_charge(db: Session, charge: ChargeRow, actor: str) -> dict:
    """Post one charge. Returns a result dict; never raises for a normal failure."""
    if charge.qbo_txn_id:
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": "already posted", "qbo_txn_id": charge.qbo_txn_id}
    if not _claim_post(charge.charge_id):
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": "already being posted"}
    try:
        # Re-read: another post may have finished just before the claim.
        try:
            db.refresh(charge)
        except Exception:                               # noqa: BLE001
            pass
        return _push_charge_claimed(db, charge, actor)
    finally:
        _release_post(charge.charge_id)


def _push_charge_claimed(db: Session, charge: ChargeRow, actor: str) -> dict:
    if charge.qbo_txn_id:
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": "already posted", "qbo_txn_id": charge.qbo_txn_id}
    # A pending charge is provisional: the bank can still change its amount or
    # its merchant, and can withdraw it altogether. Writing one to the ledger
    # risks an entry with no transaction behind it, and an amount nobody can
    # tie out. So it is refused HERE rather than at each caller -- auto-post,
    # manual push and batch push all funnel through this function, and a guard
    # on any one of them would leave the other two open.
    #
    # This is not a delay in the work. The row is reviewable and codable the
    # moment it lands; only the ledger write waits. When the settled copy
    # arrives it inherits the coding and posts by itself.
    if charge.is_pending:
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": "still pending at the bank — will post when it settles",
                "pending": True}
    if charge.status != "categorized":
        return {"charge_id": charge.charge_id, "status": "skipped",
                "reason": f"status is {charge.status}, not categorized"}

    try:
        if charge.resolution == "matched":
            created = post_bill_payment(db, charge, actor)
            kind = "BillPayment"
        elif charge.is_card_payment:
            # Settling a card balance is not an expense -- nothing was bought.
            # QuickBooks has its own entities for it, and which one depends on
            # what the card is: a Transfer between bank accounts, or a
            # CreditCardPayment against a card.
            entity, payload = payment_payload(db, charge)
            realm = _realm_for(db, charge.company)
            created = (_post(db, realm, entity, payload) or {}).get(
                "Transfer" if entity == "transfer" else "CreditCardPayment", {})
            kind = "Transfer" if entity == "transfer" else "CreditCardPayment"
        elif charge.resolution == "coded":
            created = post_expense(db, charge, actor)
            # A bank refund comes back as a Deposit, which needs the right
            # entity name for a later undo to find it.
            kind = "Deposit" if (charge.is_credit and
                                 account_type(db, resolve_card_account(db, charge)[0])
                                 .lower().startswith("bank")) else "Purchase"
        else:
            return {"charge_id": charge.charge_id, "status": "skipped",
                    "reason": "no resolution recorded"}

        txn_id = created.get("Id", "")
        # A paid bill has to leave the candidate pool NOW, not at the next
        # sync. Otherwise it keeps being suggested for up to an hour after it
        # was settled, and two reviewers can match two different charges to the
        # same bill. Drawing the balance down (rather than deleting the row)
        # keeps the record around so Undo can put it back.
        if kind == "BillPayment" and charge.matched_bill_id:
            paid_bill = db.get(BillRow, charge.matched_bill_id)
            if paid_bill is not None:
                before_bal = paid_bill.balance
                paid_bill.balance = max(Decimal("0"),
                                        (before_bal or Decimal("0")) - charge.amount)
                db.add(AuditRow(
                    company=charge.company, action="bill_balance_drawn_down",
                    charge_id=charge.charge_id, bill_id=paid_bill.bill_id,
                    detail=f"balance {before_bal} -> {paid_bill.balance} "
                           f"after payment of ${charge.amount}",
                    dry_run=False, actor=actor))
        charge.qbo_txn_id = txn_id
        charge.qbo_txn_type = kind
        # needed to delete it again if the reviewer undoes
        charge.qbo_sync_token = str(created.get("SyncToken", "0"))
        charge.posted_at = datetime.utcnow()
        db.add(AuditRow(
            company=charge.company, action=f"qbo_post_{kind.lower()}",
            charge_id=charge.charge_id, bill_id=charge.matched_bill_id or None,
            detail=f"created {entity_word(kind)} {txn_id} for ${charge.amount} by {actor}",
            dry_run=False, actor=actor))
        db.commit()
        return {"charge_id": charge.charge_id, "status": "posted",
                "type": kind, "qbo_txn_id": txn_id}

    except (WriteBlocked, WriteFailed) as e:
        db.rollback()
        db.add(AuditRow(
            company=charge.company, action="qbo_post_failed",
            charge_id=charge.charge_id, detail=str(e)[:1500],
            dry_run=False, actor=actor))
        db.commit()
        return {"charge_id": charge.charge_id, "status": "failed",
                "error": str(e)[:300]}
    except Exception as e:                      # unexpected: log loudly, keep going
        db.rollback()
        log.exception("unexpected error posting %s", charge.charge_id)
        return {"charge_id": charge.charge_id, "status": "failed",
                "error": f"{type(e).__name__}: {e}"[:300]}


def push_batch(db: Session, company: str, actor: str,
               charge_ids: list[str] | None = None, limit: int = 50) -> dict:
    """Post categorized charges. Bounded by `limit` on purpose.

    Defaulting to a small batch means the first live run is small enough to
    check by hand in QuickBooks before doing more.
    """
    assert_writes_allowed()

    q = db.query(ChargeRow).filter(
        ChargeRow.company == company,
        ChargeRow.status == "categorized",
        ChargeRow.qbo_txn_id == "")
    if charge_ids:
        q = q.filter(ChargeRow.charge_id.in_(charge_ids))
    charges = q.limit(limit).all()

    results = [push_charge(db, c, actor) for c in charges]
    return {
        "company": company,
        "attempted": len(results),
        "posted": sum(1 for r in results if r["status"] == "posted"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "results": results,
    }


# --- creating reference records -----------------------------------------
#
# Coding a charge needs a vendor and a category that already exist in
# QuickBooks. When one doesn't, the old answer was "go and make it in QBO, then
# come back and refresh" -- a dead end in the middle of a task. These create it
# in place.
#
# All three follow the same shape: refuse a name that already exists (QuickBooks
# would either error or make a confusing near-duplicate), create, then hand back
# the id so the caller can cache it and use it immediately.

def _existing_name(db: Session, realm: str, entity: str, name: str) -> dict | None:
    """The record with this name, if QuickBooks already has one."""
    from .qbo_bills import _query
    safe = (name or "").replace("'", "\\'")
    field = "DisplayName" if entity == "Vendor" else "Name"
    resp = _query(db, realm,
                  f"SELECT * FROM {entity} WHERE {field} = '{safe}' MAXRESULTS 5")
    rows = resp.get(entity, [])
    return rows[0] if rows else None


def _existing_account(db: Session, realm: str, leaf: str, full: str) -> dict | None:
    """The account at exactly this path, if QuickBooks already has one.

    QuickBooks allows the SAME leaf name under different parents -- "Blue Jays"
    can sit under both "Prepaid Inventory:MLB" and "Ticket Credits:MLB". The
    old lookup asked for up to five accounts named "Blue Jays" and then read
    rows[0], so which one it got depended on the order QuickBooks happened to
    return. When that first row was the wrong branch the path check failed, and
    the engine CREATED a second account at the path it wanted -- a duplicate
    with the same name, differing only in its parent.

    Every match is examined here, and one is returned only if its
    FullyQualifiedName is the path being asked for.
    """
    from .qbo_bills import _query
    safe = (leaf or "").replace("'", "\\'")
    resp = _query(db, realm,
                  f"SELECT * FROM Account WHERE Name = '{safe}' MAXRESULTS 100")
    want = (full or "").strip().lower()
    rows = resp.get("Account", [])
    for a in rows:
        if (a.get("FullyQualifiedName") or "").strip().lower() == want:
            return a
    # Also try the path as a literal Name -- some files store it that way.
    if ":" in (full or ""):
        alt = _existing_name(db, realm, "Account", full)
        if alt and (alt.get("FullyQualifiedName") or "").strip().lower() == want:
            return alt
    return None


def create_vendor(db: Session, name: str, actor: str,
                  company: str = "") -> dict:
    """Add a vendor to QuickBooks and return {id, name}."""
    assert_writes_allowed()
    name = (name or "").strip()
    if not name:
        raise WriteFailed("A vendor name is required")
    realm = _realm_for(db, company)

    existing = _existing_name(db, realm, "Vendor", name)
    if existing:
        # Not an error worth stopping for: the person wanted this vendor to
        # exist, and it does. Hand back the one already there.
        return {"id": existing.get("Id"), "name": existing.get("DisplayName"),
                "created": False}

    body = _post(db, realm, "vendor", {"DisplayName": name})
    v = body.get("Vendor", {})
    db.add(AuditRow(company="", action="qbo_vendor_created",
                    detail=f"created vendor '{name}' ({v.get('Id')})",
                    dry_run=False, actor=actor))
    db.commit()
    return {"id": v.get("Id"), "name": v.get("DisplayName"), "created": True}


def create_category(db: Session, name: str, actor: str,
                    account_type: str = "Expense",
                    parent_id: str = "",
                    company: str = "") -> dict:
    """Add an expense account -- a 'category' -- to QuickBooks.

    `name` may be a colon path ("Ticket Credits:MLB:Blue Jays"), which is how
    the team writes categories. QuickBooks stores the leaf name plus a parent
    link, so the path is split and each missing level is created in turn.
    """
    assert_writes_allowed()
    name = (name or "").strip().strip(":")
    if not name:
        raise WriteFailed("A category name is required")
    # Every type QuickBooks accepts. A sub-account must match its parent's type,
    # so this cannot be limited to the expense kinds.
    allowed = {"Expense", "Cost of Goods Sold", "Other Expense", "Income",
               "Other Income", "Bank", "Accounts Receivable",
               "Other Current Asset", "Fixed Asset", "Other Asset",
               "Accounts Payable", "Credit Card", "Other Current Liability",
               "Long Term Liability", "Equity"}
    if account_type not in allowed:
        raise WriteFailed(f"'{account_type}' isn't a QuickBooks account type")
    realm = _realm_for(db, company)

    parent = parent_id
    created_any = False
    leaf = None
    path = []
    for part in [p.strip() for p in name.split(":") if p.strip()]:
        path.append(part)
        full = ":".join(path)
        existing = _existing_account(db, realm, part, full)
        if existing:
            parent = existing.get("Id")
            leaf = existing
            continue

        payload = {"Name": part, "AccountType": account_type}
        if parent:
            payload["SubAccount"] = True
            payload["ParentRef"] = {"value": parent}
        body = _post(db, realm, "account", payload)
        leaf = body.get("Account", {})
        parent = leaf.get("Id")
        created_any = True

    if leaf is None:
        raise WriteFailed(f"could not create '{name}'")
    if created_any:
        db.add(AuditRow(company="", action="qbo_category_created",
                        detail=f"created category '{name}' ({leaf.get('Id')})",
                        dry_run=False, actor=actor))
        db.commit()
    return {"id": leaf.get("Id"),
            "name": leaf.get("FullyQualifiedName") or leaf.get("Name"),
            "created": created_any}


MERGE_IN_QBO = (
    "That name already belongs to another account. "
    "Merging accounts can only be done in Quickbooks"
)

# The shapes QuickBooks refuses a name-clash in. It has more than one
# wording and the one you get depends on which check fires first, so all
# of them are matched rather than the sentence we happened to see.
def _is_merge_refusal(exc) -> bool:
    t = str(exc).lower()
    return ("accountsubtype" in t or "merge" in t
            or "different name" in t or "already using this name" in t
            or "duplicate name" in t)


def rename_account(db: Session, qbo_id: str, name: str, actor: str,
                   company: str = "") -> dict:
    """Rename a bank or credit-card account, in QuickBooks and in the mirror.

    QuickBooks is the source of truth for these names now, so renaming here
    without renaming there would put the two out of step until the next
    refresh -- and the next refresh would silently undo it.
    """
    assert_writes_allowed()
    name = (name or "").strip()
    if not name:
        raise WriteFailed("An account name is required")
    realm = _realm_for(db, company)

    current = _read_live(db, realm, "account", qbo_id)
    if not current:
        raise WriteFailed("That account is not in QuickBooks")
    if (current.get("Name") or "") == name:
        return {"id": qbo_id, "name": name, "changed": False}

    # THE SAME SENTENCE AS THE OTHER RENAME PATH. The Bank / Card Accounts grid
    # posts to /qbo/accounts/rename, which lands here -- a different function
    # from rename_reference, so guarding only that one left QuickBooks' own
    # wording coming straight through on the screen people rename from.
    try:
        body = _post(db, realm, "account", {
            "Id": qbo_id, "SyncToken": str(current.get("SyncToken", "0")),
            "Name": name, "AccountType": current.get("AccountType"),
            "sparse": True,
        })
    except Exception as exc:                            # noqa: BLE001
        if _is_merge_refusal(exc):
            raise WriteFailed(MERGE_IN_QBO) from exc
        raise
    a = body.get("Account", {})
    new_name = a.get("FullyQualifiedName") or a.get("Name") or name

    from ..models_db import QboRefRow, SourceAccountRow
    ref = db.query(QboRefRow).filter(QboRefRow.realm_id == realm,
                                     QboRefRow.kind == "account",
                                     QboRefRow.qbo_id == qbo_id).first()
    if ref:
        ref.name = new_name
    for row in db.query(SourceAccountRow).filter(
            SourceAccountRow.qbo_account_id == qbo_id).all():
        row.qbo_account_name = new_name
    db.add(AuditRow(company="", action="qbo_account_renamed",
                    detail=f"{current.get('Name')} -> {new_name}",
                    dry_run=False, actor=actor))
    db.commit()
    return {"id": qbo_id, "name": new_name, "changed": True}


def retype_account(db: Session, qbo_id: str, account_type: str,
                   actor: str,
                   company: str = "", subtype: str = "") -> dict:
    """Switch an account between Bank and Credit Card.

    QuickBooks refuses the change once the account has transactions, which is
    the right answer -- the two are posted to differently -- so the refusal is
    passed through rather than dressed up.
    """
    assert_writes_allowed()
    # ANY type QuickBooks accepts, not just Bank and Credit Card. This existed
    # to flip a card account between those two; the chart of accounts now edits
    # type and subtype on every account, and refusing the rest here meant the
    # picker offered choices that could not be saved.
    realm = _realm_for(db, company)
    current = _read_live(db, realm, "account", qbo_id)
    if not current:
        raise WriteFailed("That account is not in QuickBooks")

    want_sub = (subtype or "").strip()
    # THE SUBTYPE COUNTS AS A CHANGE. This returned early whenever the type
    # matched, so changing only the subtype -- which is what you do to make two
    # accounts mergeable -- did nothing at all and reported success.
    same_type = (current.get("AccountType") or "") == account_type
    same_sub = (not want_sub
                or (current.get("AccountSubType") or "") == want_sub)
    if same_type and same_sub:
        return {"id": qbo_id, "type": account_type, "changed": False}

    payload = {"Id": qbo_id, "SyncToken": str(current.get("SyncToken", "0")),
               "Name": current.get("Name"), "AccountType": account_type,
               "sparse": True}
    # The caller's subtype, not a hardcoded "Checking". That default was right
    # for the one job this had and wrong for every account whose subtype is
    # something else -- it would have silently retyped a savings account.
    if want_sub:
        payload["AccountSubType"] = want_sub
    elif account_type == "Bank":
        payload["AccountSubType"] = "Checking"
    a = (_post(db, realm, "account", payload) or {}).get("Account", {})

    from ..models_db import QboRefRow
    # SCOPED BY REALM. QuickBooks ids are only unique within one file, so
    # matching on qbo_id alone can update another company's row and leave this
    # one untouched -- the write reaches QuickBooks, the local mirror does not
    # change, and the grid redraws the old value. That is the "saves and then
    # reverts" you saw.
    ref = db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                     QboRefRow.realm_id == realm,
                                     QboRefRow.qbo_id == qbo_id).first()
    if ref:
        ref.account_type = a.get("AccountType") or account_type
        ref.subtype = a.get("AccountSubType") or want_sub or ref.subtype
    db.add(AuditRow(company="", action="qbo_account_retyped",
                    detail=f"{current.get('Name')}: "
                           f"{current.get('AccountType')} -> {account_type}",
                    dry_run=False, actor=actor))
    db.commit()
    return {"id": qbo_id, "type": account_type, "changed": True}


def create_bank_account(db: Session, name: str, actor: str,
                        account_type: str = "Credit Card",
                        company: str = "") -> dict:
    """Add a bank or credit-card account, the kind a card program settles to."""
    assert_writes_allowed()
    name = (name or "").strip()
    if not name:
        raise WriteFailed("An account name is required")
    if account_type not in ("Bank", "Credit Card"):
        raise WriteFailed("Account type must be Bank or Credit Card")
    realm = _realm_for(db, company)

    existing = _existing_name(db, realm, "Account", name)
    if existing:
        return {"id": existing.get("Id"),
                "name": existing.get("FullyQualifiedName") or existing.get("Name"),
                "type": existing.get("AccountType"), "created": False}

    # THE SUBTYPE IS ALWAYS STATED. Bank already was; Credit Card was left for
    # QuickBooks to default, and an unstated value is one we cannot promise is
    # the same twice. Two accounts created here must be mergeable with each
    # other, and a merge compares the subtype as well as the type.
    payload = {"Name": name, "AccountType": account_type}
    if account_type == "Bank":
        payload["AccountSubType"] = "Checking"
    elif account_type == "Credit Card":
        payload["AccountSubType"] = "CreditCard"
    body = _post(db, realm, "account", payload)
    a = body.get("Account", {})
    db.add(AuditRow(company="", action="qbo_account_created",
                    detail=f"created {account_type} account '{name}' ({a.get('Id')})",
                    dry_run=False, actor=actor))
    db.commit()
    return {"id": a.get("Id"),
            "name": a.get("FullyQualifiedName") or a.get("Name"),
            "type": a.get("AccountType"), "created": True}


# --- editing bills -------------------------------------------------------

def edit_bill(db: Session, bill: BillRow, actor: str,
              vendor_name: str | None = None,
              new_amount: Decimal | None = None) -> dict:
    """Change a bill's vendor and/or amount, in QuickBooks and in our mirror.

    Reads the bill live first. The SyncToken is a concurrency guard and ours
    goes stale the moment anyone touches the bill in QuickBooks or the
    TicketVault import runs, so a cached one would either be rejected or
    overwrite someone else's change.

    Refuses two things outright:

      * more than one line -- splitting a new total across lines is an
        accounting decision, not one to guess at;
      * an amount below what has already been paid against the bill, which
        QuickBooks would leave over-applied.
    """
    assert_writes_allowed()
    realm = _realm_for(db, bill.company)
    qbo_tokens.assert_realm_allowed(realm, db)
    token = qbo_tokens.get_access_token(db, realm)
    base = f"{qbo_oauth.api_base()}/v3/company/{realm}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}

    r = httpx.get(f"{base}/bill/{bill.bill_id}", headers=headers,
                  params={"minorversion": MINOR_VERSION}, timeout=60)
    if r.status_code >= 400:
        raise WriteFailed(f"could not read bill {bill.doc_number or bill.bill_id} "
                          f"({r.status_code}): {r.text[:160]}")
    current = r.json().get("Bill", {})
    lines = current.get("Line", [])

    total = Decimal(str(current.get("TotalAmt", bill.amount or 0)))
    balance = Decimal(str(current.get("Balance", bill.balance or 0)))
    paid = total - balance

    payload = {k: v for k, v in current.items()
               if k not in ("MetaData", "domain", "sparse")}
    payload["Id"] = bill.bill_id
    payload["SyncToken"] = str(current.get("SyncToken", "0"))
    changed = []

    if vendor_name is not None and vendor_name.strip():
        from ..models_db import QboRefRow
        ref = _ref_by_name(db, bill.company, "vendor", vendor_name)
        if ref is None:
            raise WriteFailed(f"'{vendor_name}' isn't a vendor in QuickBooks")
        was = (current.get("VendorRef") or {}).get("name") or bill.vendor
        payload["VendorRef"] = {"value": ref.qbo_id}
        changed.append(f"vendor: '{was or '(blank)'}' changed to "
                       f"'{vendor_name or '(blank)'}'")

    if new_amount is not None:
        new_amount = Decimal(new_amount).quantize(Decimal("0.01"))
        if new_amount <= 0:
            raise WriteFailed("A bill's amount must be more than zero")
        if len(lines) != 1:
            raise WriteFailed(
                f"bill {bill.doc_number or bill.bill_id} has {len(lines)} lines "
                f"in QuickBooks — change the amount there instead")
        if new_amount < paid:
            raise WriteFailed(
                f"bill {bill.doc_number or bill.bill_id} already has "
                f"${paid} paid against it — the amount can't go below that")
        line = dict(lines[0])
        line["Amount"] = float(new_amount)
        # The detail block carries its own copy of the amount on some line
        # types; leaving it behind makes QuickBooks reject the update.
        for key in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
            if isinstance(line.get(key), dict) and "Amount" in line[key]:
                line[key] = {**line[key], "Amount": float(new_amount)}
        payload["Line"] = [line]
        payload["TotalAmt"] = float(new_amount)
        changed.append(f"amount ${total} -> ${new_amount}")

    if not changed:
        return {"status": "unchanged", "changed": []}

    body = _post(db, realm, "bill", payload)
    updated = body.get("Bill", {})

    # Mirror what QuickBooks now holds rather than what we asked for.
    if "TotalAmt" in updated:
        newtotal = Decimal(str(updated["TotalAmt"]))
        bill.balance = newtotal - paid
        bill.amount = newtotal
    # ONLY WHEN THE VENDOR WAS CHANGED. The payload always carries the bill's
    # VendorRef (copied from QuickBooks), so testing it set the vendor to None
    # on every amount-only edit -- the Open Bills row lost its vendor.
    if vendor_name is not None and vendor_name.strip():
        bill.vendor = vendor_name
    db.add(AuditRow(company=bill.company, action="bill_edited",
                    bill_id=bill.bill_id, detail="; ".join(changed),
                    dry_run=False, actor=actor))
    db.commit()
    return {"status": "updated", "changed": changed}


# --- renaming a reference ----------------------------------------------------
#
# The dangerous edit on this screen, and the reason Chart of Accounts and
# Vendors are not just Card Accounts with a different query.
#
# QuickBooks identifies an account or a vendor by id, but this engine stores the
# NAME as a string in a dozen places: on every coded charge, in every match
# rule's vendor and category, on each card account's QuickBooks account name.
# Rename "Ticketmaster" to "Ticketmaster Inc" in QuickBooks alone and the rules
# that fill it keep writing the old string, which no longer resolves -- and
# nothing errors, because a name that matches nothing simply matches nothing.
#
# So a rename here is one operation with two halves: the QuickBooks update, and
# the local rewrite of everything that referred to the old name. The rewrite
# reports what it touched, because "renamed, and 412 charges and 9 rules now say
# something different" is a materially different action from "renamed".

# --- every memo the engine writes is signed ---------------------------------
ENGINE_TAG = " {Engine}"


# QuickBooks' own entity names, in the words the rest of the app uses.
#
# "Purchase" is what the API calls an expense and "BillPayment" what it calls a
# payment against a bill. Neither appears anywhere else in this application, so
# an audit note saying "created Purchase 2704" was asking the reader to know
# QuickBooks' vocabulary to understand their own log.
ENTITY_WORDS = {
    "Purchase": "expense",
    "BillPayment": "bill payment",
    "Bill": "bill",
    "Deposit": "deposit",
    "JournalEntry": "journal entry",
    "CreditCardCredit": "refund",
}


def entity_word(kind: str) -> str:
    return ENTITY_WORDS.get(str(kind or ""), str(kind or "").lower())


def engine_memo(text: str) -> str:
    """The memo, with the engine's mark on the end.

    Anyone reading a transaction in QuickBooks can tell at a glance whether it
    came from here or was typed there, which matters most in exactly the case
    that has been causing trouble: somebody edits a transaction over there and
    the two records diverge. Without the mark, "who wrote this" is a question
    the ledger cannot answer.

    Applied through ONE function rather than at each of the nine places that
    build a PrivateNote -- nine copies of a suffix is nine chances for the
    tenth to be written without it, and a mark that is only mostly there tells
    you nothing when it is missing.

    Idempotent: re-posting or updating an existing transaction runs its memo
    through here again, and a memo that already ends in the tag is left alone
    rather than accumulating "{Engine} {Engine} {Engine}".
    """
    body = str(text or "").rstrip()
    if body.endswith(ENGINE_TAG.strip()):
        return body
    # SWITCHABLE, on Configuration > Settings. Off means memos are written as
    # they are, with no mark added; a memo that already carries it keeps it.
    if not engine_tag_on(_TAG_COMPANY.get("")):
        return body
    return (body + ENGINE_TAG) if body else ENGINE_TAG.strip()


import contextvars as _cvars
_TAG_COMPANY = _cvars.ContextVar("engine_tag_company", default="")


def charge_memo_text(charge) -> str:
    """The memo a charge is written to QuickBooks with: its memo (or bank
    detail), then its Custom Notes in brackets -- before {Engine}."""
    body = str(getattr(charge, "memo", "") or getattr(charge, "merchant", "") or "").rstrip()
    tag = ENGINE_TAG.strip()
    if body.endswith(tag):
        body = body[: -len(tag)].rstrip()
    note = str(getattr(charge, "custom_note", "") or "").strip()
    if note and f"({note})" not in body:
        body = f"{body} ({note})" if body else f"({note})"
    return body


def engine_memo_for(company: str, text: str) -> str:
    """engine_memo, deciding by THIS company's setting (Configuration >
    Settings), which is per company."""
    tok = _TAG_COMPANY.set(company or "")
    try:
        return engine_memo(text)
    finally:
        _TAG_COMPANY.reset(tok)


ENGINE_TAG_KEY = "qbo_engine_tag"
_TAG_CACHE: dict = {}


def engine_tag_key(company: str) -> str:
    return f"{ENGINE_TAG_KEY}:{company}" if company else ENGINE_TAG_KEY


def engine_tag_on(company: str = "") -> bool:
    """Whether memos written to QuickBooks for THIS company end in {Engine}.

    The company's own setting if it has one; otherwise the engine-wide value
    from before settings were per company, so turning this into a per-company
    switch changed nothing for anybody. On by default. Cached for a minute
    per company: it is read on every memo written.
    """
    import time as _t
    hit = _TAG_CACHE.get(company)
    if hit and _t.time() - hit[0] < 60:
        return hit[1]
    on = True
    try:
        from ..db import SessionLocal
        from ..models_db import AppSettingRow
        with SessionLocal() as _db:
            row = _db.get(AppSettingRow, engine_tag_key(company)) if company else None
            if row is None:
                row = _db.get(AppSettingRow, ENGINE_TAG_KEY)
            on = not (row is not None and (row.value or "").strip() == "off")
    except Exception:                                   # noqa: BLE001
        on = True
    _TAG_CACHE[company] = (_t.time(), on)
    return on

def _unsigned(text: str) -> str:
    """The text with the engine's mark taken back off.

    Needed wherever a description is REBUILT from one that already carries the
    mark -- a split, a merge -- so the result reads "... (part 2 of 3) {Engine}"
    rather than "... {Engine} (part 2 of 3) {Engine}".
    """
    body = str(text or "").rstrip()
    tag = ENGINE_TAG.strip()
    return body[:-len(tag)].rstrip() if body.endswith(tag) else body


def _sparse_update(db: Session, realm_id: str, entity: str,
                   obj_id: str, fields: dict) -> dict:
    """Read the object for its SyncToken, then update only the named fields.

    QuickBooks rejects an update carrying a stale SyncToken, which is what
    stops two people renaming the same account into different things.
    """
    live = _read_live(db, realm_id, entity, obj_id)
    if not live:
        raise WriteFailed(f"{entity} {obj_id} not found in QuickBooks")
    payload = {"Id": obj_id, "SyncToken": live.get("SyncToken", "0"),
               "sparse": True}
    payload.update(fields)
    return _post(db, realm_id, entity, payload)


def rename_reference(db: Session, kind: str, qbo_id: str, new_name: str,
                     actor: str,
                     company: str = "") -> dict:
    """Rename an account or vendor in QuickBooks, and everywhere here."""
    from ..models_db import (AuditRow, ChargeRow, LearnedRuleRow, QboRefRow,
                             SourceAccountRow)

    assert_writes_allowed()
    kind = (kind or "").strip().lower()
    if kind not in ("account", "vendor"):
        raise WriteFailed("kind must be 'account' or 'vendor'")
    new_name = (new_name or "").strip()
    if not new_name:
        raise WriteFailed("the new name cannot be empty")

    realm = _realm_for(db, company)
    row = db.query(QboRefRow).filter(
        QboRefRow.realm_id == realm,
        QboRefRow.kind == kind, QboRefRow.qbo_id == str(qbo_id)).first()
    if row is None:
        raise WriteFailed(f"no {kind} with id {qbo_id} is mirrored here")
    old_name = row.name or ""
    if old_name == new_name:
        return {"renamed": False, "reason": "the name is unchanged"}

    # LOWERCASE. QuickBooks routes on the path segment, and every other caller
    # in this file passes "bill", "billpayment", "purchase". Passing "Account"
    # produced /v3/company/{realm}/Account/307, which QuickBooks answers with
    # "Unsupported Operation — could not find resource for relative". The
    # RESPONSE key is capitalized, which is what _read_live's .capitalize()
    # handles, and the two are easy to conflate.
    entity = "account" if kind == "account" else "vendor"
    key = entity.capitalize()
    field = {"Name": new_name} if kind == "account" else {"DisplayName": new_name}
    try:
        res = _sparse_update(db, realm, entity, str(qbo_id), field)
    except Exception as exc:                                    # noqa: BLE001
        # MERGE REFUSALS SAY WHAT THE TWO ACCOUNTS ACTUALLY ARE.
        #
        # QuickBooks answers a merge it will not do with "You can merge
        # accounts only when both their AccountType and AccountSubType match"
        # -- and nothing about what either one is. The screen shows OUR mirror,
        # which can be out of date, so the pair can look identical here while
        # QuickBooks disagrees. Reading both live turns that into a fact.
        # QuickBooks refuses the rename ITSELF when the name is taken, whether
        # or not our mirror knew about the other account -- so this arrives as
        # its own error rather than as our 409, and the client never sees the
        # merge flag. Both roads now end at the same sentence.
        if kind == "account" and _is_merge_refusal(exc):
            raise WriteFailed(MERGE_IN_QBO) from exc
        raise
    live = (res.get(key) or {})
    confirmed = live.get("Name") or live.get("DisplayName") or new_name

    # QuickBooks accepted it. Now the local strings, in one transaction with
    # the mirror, so a failure here cannot leave the two disagreeing.
    touched = {"charges_coded": 0, "charges_suggested": 0, "rules": 0,
               "card_accounts": 0}
    if kind == "vendor":
        touched["charges_coded"] = db.query(ChargeRow).filter(
            ChargeRow.coded_vendor == old_name).update(
            {ChargeRow.coded_vendor: confirmed}, synchronize_session=False)
        touched["charges_suggested"] = db.query(ChargeRow).filter(
            ChargeRow.suggested_vendor == old_name).update(
            {ChargeRow.suggested_vendor: confirmed}, synchronize_session=False)
        touched["rules"] = db.query(LearnedRuleRow).filter(
            LearnedRuleRow.vendor == old_name).update(
            {LearnedRuleRow.vendor: confirmed}, synchronize_session=False)
    else:
        touched["charges_coded"] = db.query(ChargeRow).filter(
            ChargeRow.coded_category == old_name).update(
            {ChargeRow.coded_category: confirmed}, synchronize_session=False)
        touched["charges_suggested"] = db.query(ChargeRow).filter(
            ChargeRow.suggested_category == old_name).update(
            {ChargeRow.suggested_category: confirmed}, synchronize_session=False)
        touched["rules"] = db.query(LearnedRuleRow).filter(
            LearnedRuleRow.category == old_name).update(
            {LearnedRuleRow.category: confirmed}, synchronize_session=False)
        touched["card_accounts"] = db.query(SourceAccountRow).filter(
            SourceAccountRow.qbo_account_name == old_name).update(
            {SourceAccountRow.qbo_account_name: confirmed},
            synchronize_session=False)

    row.name = confirmed
    row.synced_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(company="", action=f"qbo_{kind}_renamed",
                    detail=f"{old_name!r} -> {confirmed!r}; "
                           + ", ".join(f"{k}={v}" for k, v in touched.items()),
                    actor=actor))
    db.commit()

    from ..review import reset_account_cache
    reset_account_cache()
    return {"renamed": True, "kind": kind, "qbo_id": str(qbo_id),
            "old_name": old_name, "name": confirmed, "updated": touched}


def reference_usage(db: Session, kind: str, name: str) -> dict:
    """How many things here refer to this account or vendor BY NAME.

    Deactivating is not a local edit either. A rule that fills a retired vendor
    keeps filling it, and the charges it codes will fail to post; a card
    account mapped to a retired QuickBooks account has nowhere to send its
    spending. Counted before the change so the confirmation can say what it
    will break rather than asking for a blind yes.
    """
    from ..models_db import ChargeRow, LearnedRuleRow, SourceAccountRow
    if kind == "vendor":
        return {
            "coded_charges": db.query(ChargeRow).filter(
                ChargeRow.coded_vendor == name).count(),
            "rules": db.query(LearnedRuleRow).filter(
                LearnedRuleRow.vendor == name).count(),
            "card_accounts": 0,
        }
    return {
        "coded_charges": db.query(ChargeRow).filter(
            ChargeRow.coded_category == name).count(),
        "rules": db.query(LearnedRuleRow).filter(
            LearnedRuleRow.category == name).count(),
        "card_accounts": db.query(SourceAccountRow).filter(
            SourceAccountRow.qbo_account_name == name).count(),
    }


def set_reference_active(db: Session, kind: str, qbo_id: str, active: bool,
                         actor: str,
                         company: str = "") -> dict:
    """Retire or restore an account or vendor in QuickBooks.

    QuickBooks does not delete these -- it deactivates them, and an object with
    history cannot be removed at all. So this is the only "delete" there is,
    and it is reversible, which is why restoring is the same call with the flag
    the other way.

    Nothing local is rewritten. A retired account keeps its name so charges
    already coded to it still resolve; what changes is that it stops being
    offered. The counts returned say what is still pointing at it.
    """
    from ..models_db import AuditRow, QboRefRow

    assert_writes_allowed()
    kind = (kind or "").strip().lower()
    if kind not in ("account", "vendor"):
        raise WriteFailed("kind must be 'account' or 'vendor'")
    row = db.query(QboRefRow).filter(
        QboRefRow.kind == kind, QboRefRow.qbo_id == str(qbo_id)).first()
    if row is None:
        raise WriteFailed(f"no {kind} with id {qbo_id} is mirrored here")

    realm = _realm_for(db, company)
    entity = "account" if kind == "account" else "vendor"
    # NAME travels with the update even though only Active is changing.
    #
    # QuickBooks calls this a sparse update, but Account rejects one that omits
    # Name -- "Required parameter Name is missing in the request" -- while
    # Vendor accepts the same shape happily. So the two are not symmetrical and
    # sending the name for both is simpler than remembering which is which.
    fields: dict = {"Active": bool(active)}
    if kind == "account":
        fields["Name"] = row.name or ""
    else:
        fields["DisplayName"] = row.name or ""
    _sparse_update(db, realm, entity, str(qbo_id), fields)

    uses = reference_usage(db, kind, row.name or "")
    row.active = bool(active)
    row.synced_at = datetime.utcnow()
    db.add(row)
    db.add(AuditRow(
        company="", actor=actor,
        action=f"qbo_{kind}_{'restored' if active else 'retired'}",
        detail=f"{row.name!r}; still referenced by "
               + ", ".join(f"{k}={v}" for k, v in uses.items())))
    db.commit()

    from ..review import reset_account_cache
    reset_account_cache()
    return {"kind": kind, "qbo_id": str(qbo_id), "name": row.name,
            "active": bool(active), "still_referenced_by": uses}


def merge_numbered_duplicates(db: Session, actor: str,
                              dry_run: bool = True,
                              company: str = "") -> dict:
    """Fold "Office Supplies (1)" back into "Office Supplies".

    These are the accounts the old path-matching bug created: it asked
    QuickBooks for accounts by leaf name, read the first of up to five, and
    when that was the wrong branch it created a second account at the path it
    wanted. That bug is fixed, but the accounts it already made are in
    QuickBooks and only a write can remove them.

    THREE STEPS PER DUPLICATE, in this order:

      1. Repoint everything here that names the duplicate -- coded charges,
         suggested categories, learned rules, card-account mappings -- at the
         original. Done FIRST, because after step 3 the duplicate stops being
         offered and anything still pointing at it would resolve to a retired
         account.
      2. Confirm nothing is left pointing at it. If something is, the duplicate
         is skipped and named rather than retired underneath it.
      3. Retire it in QuickBooks. NOT delete: QuickBooks does not delete an
         account, and one with any history cannot be removed at all. Retiring
         is the only "delete" there is and it is reversible, which is the right
         property for a bulk operation.

    A duplicate that QuickBooks has POSTED TRANSACTIONS against is left alone
    regardless. Repointing the engine's copy would not move the transaction in
    QuickBooks, so the two would disagree -- and that is the exact failure this
    whole area exists to prevent. Those have to be merged in QuickBooks, where
    merging actually moves the ledger.

    `dry_run` reports what would happen and writes nothing. It is the default
    deliberately: this touches the chart of accounts, and the first answer to
    "what will this do" should never be "it already did it".
    """
    import re as _re
    from ..models_db import (AuditRow, ChargeRow, LearnedRuleRow, QboRefRow,
                             SourceAccountRow)

    if not dry_run:
        assert_writes_allowed()

    suffix = _re.compile(r"\s*\((\d+)\)\s*$")
    rows = db.query(QboRefRow).filter(QboRefRow.kind == "account",
                                      QboRefRow.active.isnot(False)).all()

    # The originals: an unsuffixed name is the account somebody meant.
    originals: dict = {}
    for r in rows:
        if not suffix.search(r.name or ""):
            originals.setdefault(suffix.sub("", r.name or "").strip().lower(), r)

    merged, skipped = [], []
    for r in rows:
        if not suffix.search(r.name or ""):
            continue
        keep = originals.get(suffix.sub("", r.name or "").strip().lower())
        if keep is None:
            # A numbered name with no plain twin is not a duplicate of
            # anything -- it may simply be what the account is called.
            skipped.append({"name": r.name, "why": "no account by the base name"})
            continue

        posted = db.query(ChargeRow).filter(
            ChargeRow.coded_category == r.name,
            ChargeRow.qbo_txn_id != "").count()
        if posted:
            skipped.append({"name": r.name, "keep": keep.name,
                            "why": f"{posted} charge(s) already posted to "
                                   f"QuickBooks against it — merge this one in "
                                   f"QuickBooks so the ledger moves too"})
            continue

        moves = {
            "charges": db.query(ChargeRow).filter(
                ChargeRow.coded_category == r.name).count(),
            "suggestions": db.query(ChargeRow).filter(
                ChargeRow.suggested_category == r.name).count(),
            "rules": db.query(LearnedRuleRow).filter(
                LearnedRuleRow.category == r.name).count(),
            "card_accounts": db.query(SourceAccountRow).filter(
                SourceAccountRow.qbo_account_name == r.name).count(),
        }
        entry = {"name": r.name, "keep": keep.name, "qbo_id": r.qbo_id,
                 "moves": moves}
        if dry_run:
            merged.append(entry)
            continue

        db.query(ChargeRow).filter(ChargeRow.coded_category == r.name).update(
            {ChargeRow.coded_category: keep.name,
             ChargeRow.coded_category_id: keep.qbo_id},
            synchronize_session=False)
        db.query(ChargeRow).filter(
            ChargeRow.suggested_category == r.name).update(
            {ChargeRow.suggested_category: keep.name}, synchronize_session=False)
        db.query(LearnedRuleRow).filter(
            LearnedRuleRow.category == r.name).update(
            {LearnedRuleRow.category: keep.name}, synchronize_session=False)
        db.query(SourceAccountRow).filter(
            SourceAccountRow.qbo_account_name == r.name).update(
            {SourceAccountRow.qbo_account_name: keep.name},
            synchronize_session=False)
        db.flush()

        left = reference_usage(db, "account", r.name)
        if any(left.values()):
            db.rollback()
            skipped.append({"name": r.name, "keep": keep.name,
                            "why": "something still refers to it after the "
                                   "repoint: "
                                   + ", ".join(f"{k}={v}" for k, v in left.items()
                                               if v)})
            continue

        try:
            _sparse_update(db, _realm_for(db, company), "account", str(r.qbo_id),
                           {"Active": False, "Name": r.name or ""})
        except (WriteFailed, WriteBlocked) as e:
            db.rollback()
            skipped.append({"name": r.name, "keep": keep.name,
                            "why": f"QuickBooks refused to retire it: {e}"})
            continue

        r.active = False
        r.synced_at = datetime.utcnow()
        db.add(r)
        db.add(AuditRow(company="", actor=actor, action="qbo_account_merged",
                        detail=f"{r.name!r} folded into {keep.name!r}; "
                               + ", ".join(f"{k}={v}" for k, v in moves.items()
                                           if v)))
        db.commit()
        merged.append(entry)

    if not dry_run:
        db.commit()
        from ..review import reset_account_cache
        reset_account_cache()
    return {"dry_run": dry_run, "merged": merged, "skipped": skipped,
            "merged_count": len(merged), "skipped_count": len(skipped)}


def _drop_all_bill(db, bill_id: str) -> None:
    """A bill deleted in QuickBooks leaves All Bills too."""
    try:
        from ..models_db import AllBillRow
        r = db.get(AllBillRow, bill_id)
        if r is not None:
            db.delete(r)
    except Exception:                      # noqa: BLE001
        log.warning("could not drop bill %s from All Bills", bill_id, exc_info=True)

