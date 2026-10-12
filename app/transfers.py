"""MATCH TRANSFER, both sides still in For Review: one QuickBooks entry, both
rows cleared, in one click -- or a plain reason why not, with neither row moved.

Why this is its own module. The first versions handed the card side to
post_card_payments (the automatic poster) and then POLLED for the result. That
poster skips a row without saying so in several cases (already being posted by
another thread, pending, an empty-vs-missing QuickBooks id) and puts it back
in For Review when it does, so the endpoint waited for an outcome that never
came -- "QuickBooks didn't confirm the payment in time". The automatic poster
also posted BOTH sides of a pair on its own (the checking side always fails:
it is the primary account, so the payment would go from and to the same
account), which is how both rows briefly left For Review.

Here, instead, the pair is posted directly and synchronously:
  1. the card side's posting claim is taken (waiting briefly if another post
     of it is running, and using that post's result if it lands);
  2. the card side is written to QuickBooks once -- a Credit Card Payment or a
     Transfer out of the primary account;
  3. the checking side is linked to that same entry.
A failure at 2 puts the card side back in For Review with QuickBooks' own
reason, and the checking side is never touched.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

WAIT_FOR_OTHER_POST = 45          # seconds


class PairError(Exception):
    pass


def _link_bank(db: Session, bank, card, actor: str, other_account: str, other_date: str) -> None:
    from .models_db import AuditRow
    from .integrations.qbo_write import entity_word
    bank.status, bank.resolution = "categorized", "transfer"
    bank.qbo_txn_id, bank.qbo_txn_type = card.qbo_txn_id, card.qbo_txn_type
    bank.rule_source, bank.rule_detail = "transfer_match", card.charge_id
    bank.resolved_by, bank.resolved_at = actor, datetime.utcnow()
    bank.post_error = ""
    db.add(AuditRow(company=bank.company, action="transfer_matched", charge_id=bank.charge_id,
                    detail=f"matched to {entity_word(card.qbo_txn_type) or 'payment'} {card.qbo_txn_id} posted from "
                           f"{other_account} ({other_date}) -- one entry for both sides",
                    dry_run=False, actor=actor))


def post_pair(db: Session, company: str, card_id: str, bank_id: str, actor: str,
              other_account: str = "", other_date: str = "") -> dict:
    """Post the card side once and link the checking side to it."""
    from .models_db import AuditRow, ChargeRow
    from .integrations.qbo_write import (_claim_post, _release_post, _push_charge_claimed,
                                         assert_writes_allowed)
    assert_writes_allowed()
    card, bank = db.get(ChargeRow, card_id), db.get(ChargeRow, bank_id)
    if card is None or bank is None:
        raise PairError("one side of the transfer is no longer in the Engine")
    if bank.status != "for_review" or bank.qbo_txn_id:
        raise PairError("the checking side is no longer in For Review")
    if card.is_pending:
        raise PairError("the card payment is still pending at the bank -- it can be matched once it settles")

    # ALREADY IN QUICKBOOKS (an earlier click, or the card side was added on
    # its own): just link the checking side.
    if card.qbo_txn_id:
        _link_bank(db, bank, card, actor, other_account, other_date)
        db.commit()
        return {"posted": False, "qbo_txn_id": card.qbo_txn_id, "qbo_txn_type": card.qbo_txn_type}

    # THE CARD SIDE'S POSTING CLAIM. Held by another post of the same payment
    # (a pull's automatic post, someone's Add): wait for it, and use its entry.
    deadline = time.time() + WAIT_FOR_OTHER_POST
    while not _claim_post(card.charge_id):
        if time.time() > deadline:
            raise PairError("another post of this payment is still running -- try again in a minute")
        time.sleep(1)
        db.expire_all()
        card = db.get(ChargeRow, card_id)
        if card is not None and card.qbo_txn_id:
            bank = db.get(ChargeRow, bank_id)
            _link_bank(db, bank, card, actor, other_account, other_date)
            db.commit()
            return {"posted": False, "qbo_txn_id": card.qbo_txn_id, "qbo_txn_type": card.qbo_txn_type}
    try:
        db.expire_all()
        card = db.get(ChargeRow, card_id)
        if card.qbo_txn_id:                       # landed just before the claim
            result = {"status": "posted"}
        else:
            before = (card.status, card.resolution)
            card.status, card.resolution = "categorized", "coded"
            card.rule_source, card.rule_detail = "payment", "card payment / transfer"
            card.resolved_by, card.resolved_at = actor, datetime.utcnow()
            card.post_error = ""
            db.commit()
            try:
                result = _push_charge_claimed(db, card, actor)
            except Exception as e:                # noqa: BLE001
                db.rollback()
                result = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
            db.expire_all()
            card = db.get(ChargeRow, card_id)
            if result.get("status") != "posted" or not card.qbo_txn_id:
                why = result.get("error") or result.get("reason") or "QuickBooks did not return the payment"
                card.status, card.resolution = "for_review", ""
                card.resolved_by, card.resolved_at = "", None
                card.post_error = str(why)[:1000]
                db.add(AuditRow(company=company, action="payment_post_failed", charge_id=card.charge_id,
                                detail=f"Match Transfer: returned to For Review: {str(why)[:300]}",
                                dry_run=False, actor=actor))
                db.commit()
                raise PairError(str(why))
        bank = db.get(ChargeRow, bank_id)
        _link_bank(db, bank, card, actor, other_account, other_date)
        db.commit()
        return {"posted": True, "qbo_txn_id": card.qbo_txn_id, "qbo_txn_type": card.qbo_txn_type}
    finally:
        _release_post(card_id)


def paired_ids(db: Session, company: str) -> set:
    """Every For Review row that is one side of a transfer Match Transfer
    offers. The automatic payment poster leaves these to Match Transfer: posted
    on its own each side becomes a second entry (or, for the checking side,
    an error)."""
    try:
        from .api import transfer_matches
        out = set()
        for cid, m in (transfer_matches(db, company) or {}).items():
            out.add(cid)
            if m.get("other_id"):
                out.add(m["other_id"])
        return out
    except Exception:                             # noqa: BLE001
        log.warning("could not read transfer pairs for %s", company, exc_info=True)
        return set()
