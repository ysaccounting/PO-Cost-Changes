"""Vendor and category rules.

Rules used to be LEARNED: every coding decision was recorded against a merchant
and, once confirmed often enough, started applying itself. That is switched
off. One reviewer coding a merchant unusually could turn that into the
merchant's default, and the mechanism that promoted a rule to auto-apply was
never something anybody chose.

What replaces it is deliberate. A rule is written by hand, matches on
conditions a person can read, and does one of three things:

    off           nothing at all; the rule stays in the table
    prefill       fills Vendor and Category on For Review, and stops there
    prefill_post  codes the charge AND posts it to QuickBooks

There is no mode between the last two. Coding a charge without posting it left
a Categorized row with no QuickBooks transaction behind it -- a state the rest
of the app cannot represent, where Delete has nothing to reverse and the
QuickBooks scan has nothing to check.

Matching is first-past-the-post in creation order, so a narrow rule written
early beats a broad one written later. Blank conditions are not tested.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from .models_db import ChargeRow, LearnedRuleRow

log = logging.getLogger(__name__)

# Confirmations needed before a rule may auto-apply. Deliberately not 2 or 3:
# a handful of coincidental repeats shouldn't be enough to stop asking.
AUTO_THRESHOLD = 8



def _ref_by_name(*a, **k):
    """A vendor or account by name from the company's OWN QuickBooks file
    -- see review.ref_by_name. Imported late to avoid an import cycle."""
    from .review import ref_by_name
    return ref_by_name(*a, **k)

def normalize_merchant(merchant: str) -> str:
    """Collapse a merchant string to a stable key.

    Card descriptors carry order numbers, store numbers and varying spacing --
    "FIFAUS - 61667627" and "FIFAUS - 61701234" are the same merchant. Digits
    and punctuation are stripped so they land on one rule instead of hundreds.
    """
    s = str(merchant or "").upper()
    s = re.sub(r"\b\d[\d\-]*\b", " ", s)          # order/store numbers
    s = re.sub(r"[^A-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80]


def rule_key(company: str, merchant: str) -> str:
    return f"{company}|{normalize_merchant(merchant)}"


def record_decision(db: Session, charge: ChargeRow, category: str,
                    vendor: str, actor: str) -> LearnedRuleRow | None:
    """Learn from one coding decision."""
    key = normalize_merchant(charge.merchant)
    if not key or not category:
        return None

    rid = rule_key(charge.company, charge.merchant)
    rule = db.get(LearnedRuleRow, rid)
    if rule is None:
        # db.get() only looks at the database and rows already flushed, so
        # coding several charges from the SAME merchant in one batch created the
        # rule once per charge and the commit failed on the primary key --
        # taking the whole batch with it. Flushing makes the first insert
        # visible to the next lookup.
        db.flush()
        rule = db.get(LearnedRuleRow, rid)

    if rule is None:
        rule = LearnedRuleRow(
            id=rid, company=charge.company, merchant_key=key,
            sample_merchant=charge.merchant or "", category=category,
            vendor=vendor or "", confirmations=1, disagreements=0,
            auto_apply=False, last_actor=actor, updated_at=datetime.utcnow())
        db.add(rule)
        return rule

    same = (rule.category == category and (rule.vendor or "") == (vendor or ""))
    if same:
        rule.confirmations += 1
        # Consistent enough, and not being argued with -> allow auto-apply.
        if rule.confirmations >= AUTO_THRESHOLD and rule.disagreements == 0:
            if not rule.auto_apply:
                log.info("rule %s promoted to auto-apply after %d confirmations",
                         rid, rule.confirmations)
            rule.auto_apply = True
    else:
        # Someone coded this merchant differently. Trust the newer decision,
        # but stop auto-applying until it settles again.
        rule.disagreements += 1
        rule.category = category
        rule.vendor = vendor or ""
        rule.confirmations = 1
        rule.auto_apply = False
        log.info("rule %s changed by %s — auto-apply disabled", rid, actor)

    rule.last_actor = actor
    rule.updated_at = datetime.utcnow()
    return rule


def _amount_matches(rule, amount) -> bool:
    """Does the charge amount satisfy this rule's amount condition?

    Compared on the absolute value: a refund is the same size as the purchase
    it reverses, and a rule written as "over $500" plainly means the size of
    the transaction rather than its sign.
    """
    op = (rule.amount_op or "any").lower()
    if op == "any":
        return True
    if amount is None:
        return False
    a = abs(Decimal(str(amount)))
    v1 = rule.amount_value
    v2 = rule.amount_value2
    if v1 is None:
        return True                 # an amount rule with no figure tests nothing
    v1 = abs(Decimal(str(v1)))
    if op == "equals":
        return a == v1
    if op == "lt":
        return a < v1
    if op == "gt":
        return a > v1
    if op == "between":
        if v2 is None:
            return False
        lo, hi = sorted((v1, abs(Decimal(str(v2)))))
        return lo <= a <= hi
    return False


def rule_conditions(rule) -> list:
    """Which conditions this rule actually tests. Empty means it tests nothing.

    "Blank conditions are not tested" is the right principle and it has one
    catastrophic edge: a rule where EVERY condition is blank tests nothing at
    all, so it matches every charge in the company and stamps its vendor and
    category on all of them. That is how unrelated bank details -- charity
    donations, say -- ended up coded as a concert venue.
    """
    out = []
    if [p for p in (rule.detail_value or "").split(",") if p.strip()]:
        out.append("bank detail")
    if (rule.card_account or "").strip():
        out.append("card account")
    if (rule.amount_op or "any").lower() != "any" and rule.amount_value is not None:
        out.append("amount")
    return out


# EACH RULE PREPARED ONCE -- its phrases, whether it tests anything, its
# expanded phrases -- not again for every charge it is checked against (an
# export checks every charge against every rule: a million checks). Keyed on
# the rule's settings, so an edited rule is prepared afresh.
_RULE_PREP: dict = {}


def _prepared(rule) -> list:
    # KEPT ON THE RULE ITSELF for the life of the request: building the lookup
    # key below cost as much as the preparation, and an export checks every
    # rule against every row -- 1.6 million times for 11,000 rows. A rule
    # edited in the meantime is a fresh object, and its values are rechecked.
    sig = (rule.detail_value, rule.detail_op, rule.card_account, rule.amount_op, rule.mode)
    own = rule.__dict__.get("_eng_prep")
    if own is not None and own[0] == sig:
        return own[1]
    k = (rule.id, rule.detail_value, rule.detail_op, rule.card_account, rule.amount_op,
         str(rule.amount_value), str(getattr(rule, "amount_value2", None)), rule.mode)
    v = _RULE_PREP.get(k)
    if v is None:
        if len(_RULE_PREP) > 20000:
            _RULE_PREP.clear()
        v = [[p.strip().lower() for p in (rule.detail_value or "").split(",") if p.strip()],
             (rule.detail_op or "contains").lower() == "equals",
             bool(rule_conditions(rule)), None]
        _RULE_PREP[k] = v
    try:
        rule.__dict__["_eng_prep"] = (sig, v)
    except Exception:                                   # noqa: BLE001
        pass
    return v


def rule_matches(rule, charge, allow_payment: bool = False) -> bool:
    """Every condition that is SET must hold. Blank conditions are not tested.

    `allow_payment` is ONLY for reading a payment's vendor (see
    rule_vendor_for_payment); nothing that codes or posts ever passes it."""
    # NEVER A CARD PAYMENT. A payment onto a card is money moving between two
    # accounts the company owns; its category is always a bank account -- the
    # primary checking account it was paid from. A rule written for the
    # card's PURCHASES (an "AMEX" rule to Inventory Asset, say) matched the
    # payment too, because the payment's bank detail names the same program.
    # Rules are for purchases; payments are coded by the payment settings.
    if charge is not None and getattr(charge, "is_card_payment", False) \
            and not allow_payment:
        return False
    if (rule.mode or "off") == "off":
        return False
    # A rule with nothing to test matches nothing, rather than everything.
    # Silently, because a rule in this state is half-written rather than wrong,
    # and it is visible as inert in the Rules table and in /admin/why.
    prep = _prepared(rule)
    if not prep[2]:
        return False
    # Comma separated, and ANY of them qualifies. One rule per spelling of the
    # same merchant was the alternative, and a dozen near-identical rows with
    # the same vendor and category is a list nobody can check.
    phrases = prep[0]
    if phrases:
        detail = (charge.merchant or "").lower()
        equals = prep[1]
        hit = (detail.strip() in phrases if equals
               else any(p in detail for p in phrases))
        if not hit:
            # Second pass through the abbreviation table, so a rule written
            # "Los Angeles Clippers" still fires on "LA CLIPPERS LLC". Only as a
            # fallback: a rule that already matched literally is left exactly as
            # it behaved before, so nothing that works today changes.
            try:
                from engine.hal import expand_text as _x
                xd = _x(detail)
                if prep[3] is None:
                    prep[3] = [_x(p) for p in phrases]
                xp = prep[3]
                hit = (xd.strip() in xp if equals
                       else any(p in xd for p in xp))
            except Exception:               # noqa: BLE001
                hit = False
        if not hit:
            return False
    if (rule.card_account or "").strip():
        if (charge.source or "") != rule.card_account:
            return False
    return _amount_matches(rule, charge.amount)


# SHARED RULES: one rule for several companies, edited once (Multi-Company
# Mode). Stored once under this marker; the companies USING it are kept beside
# it -- a company ignores a shared rule by being left off that list.
SHARED = "__shared__"
_SHARED_KEY = "shared_rule:"           # + rule id -> {"companies": [...]}


def shared_companies(db: Session, rule_id: str) -> list:
    import json as _js
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, _SHARED_KEY + rule_id)
    try:
        return list((_js.loads(r.value) or {}).get("companies") or []) if r and r.value else []
    except ValueError:
        return []


def set_shared_companies(db: Session, rule_id: str, companies: list) -> list:
    import json as _js
    from .models_db import AppSettingRow
    cos = sorted({str(c).strip() for c in companies if str(c).strip()})
    db.merge(AppSettingRow(key=_SHARED_KEY + rule_id, value=_js.dumps({"companies": cos})))
    return cos


RULE_ORDER_KEY = "rule_order:"     # + company (or SHARED) -> [rule ids], top first


def rule_order(db: Session, scope: str) -> list:
    import json as _jro
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, RULE_ORDER_KEY + (scope or ""))
    try:
        return _jro.loads(r.value) if r and r.value else []
    except ValueError:
        return []


def ordered(db: Session, scope: str, rows: list) -> list:
    """The rules in the order dragged on Bank Feed Rules; any not yet placed
    (new since) after them, oldest first."""
    order = {rid: i for i, rid in enumerate(rule_order(db, scope))}
    return sorted(rows, key=lambda r: (order.get(r.id, len(order)), r.updated_at or datetime.min, r.id))


NO_SHARED_KEY = "no_shared_rules:"     # + company -> "true"


def no_shared_rules(db: Session, company: str) -> bool:
    """Configuration > Settings: this company ignores Multi-Company Bank Feed
    Rules, even when a rule's company picker includes it."""
    from .models_db import AppSettingRow
    r = db.get(AppSettingRow, NO_SHARED_KEY + (company or ""))
    return bool(r and (r.value or "") == "true")


def shared_rules(db: Session) -> list:
    return ordered(db, SHARED, db.query(LearnedRuleRow).filter(LearnedRuleRow.company == SHARED)
                   .order_by(LearnedRuleRow.updated_at, LearnedRuleRow.id).all())


def rules_for(db: Session, company: str) -> list:
    """Every rule for a company, oldest first -- the order they are tried in.
    The company's own rules come first, then the SHARED rules it uses, so a
    company-specific rule always wins. A shared rule whose vendor or category
    this company's QuickBooks file does not have does not apply here."""
    own = ordered(db, company, db.query(LearnedRuleRow)
                  .filter(LearnedRuleRow.company == company)
                  .order_by(LearnedRuleRow.updated_at, LearnedRuleRow.id).all())
    if not company or company == SHARED:
        return own
    # THIS COMPANY OPTED OUT of Multi-Company rules (Configuration > Settings):
    # none apply, even where the rule ticks it.
    if no_shared_rules(db, company):
        return own
    shared = []
    for r in shared_rules(db):
        if company not in shared_companies(db, r.id):
            continue
        if not (_ref_by_name(db, company, "account", r.category)
                and _ref_by_name(db, company, "vendor", r.vendor)):
            continue
        shared.append(r)
    return own + shared


def match_rule(db: Session, charge, rules=None):
    """The first rule this charge satisfies, or None.

    `rules` can be passed in when scoring a page of charges, so the table is
    read once rather than once per row.
    """
    for rule in (rules if rules is not None
                 else rules_for(db, charge.company)):
        try:
            if rule_matches(rule, charge):
                return rule
        except Exception:                   # noqa: BLE001
            # A malformed rule must not stop the queue rendering. It simply
            # never matches until somebody fixes it.
            continue
    return None


def lookup(db: Session, company: str, merchant: str) -> LearnedRuleRow | None:
    key = normalize_merchant(merchant)
    if not key:
        return None
    return db.get(LearnedRuleRow, rule_key(company, merchant))


def suggestion_for(db: Session, company: str, merchant: str,
                   charge=None, rules=None) -> dict | None:
    """What the rules say about this charge, if anything.

    `charge` is what the conditions are actually tested against; `merchant` is
    kept in the signature so older callers still work, and a bare merchant is
    matched as a charge with no card account and no amount.
    """
    if charge is None:
        charge = ChargeRow(company=company, merchant=merchant or "",
                           source="", amount=None)
    rule = match_rule(db, charge, rules)
    if rule is None:
        return None
    # The split, when the rule carries one. Read here rather than by every
    # caller, so a rule's answer is one shape whether it names one category or
    # divides across several.
    def _live(kind: str, ref_id, stored: str) -> str:
        """The current name behind an id, falling back to what was stored."""
        if not ref_id:
            return stored or ""
        try:
            from .models_db import QboRefRow
            hit = db.query(QboRefRow.name).filter(
                QboRefRow.kind == kind,
                QboRefRow.qbo_id == str(ref_id)).first()
            return (hit[0] if hit else "") or stored or ""
        except Exception:                   # noqa: BLE001
            return stored or ""

    split = []
    if getattr(rule, "split_json", None):
        try:
            import json as _json
            raw = _json.loads(rule.split_json) or []
            for ln in raw:
                cat = str((ln or {}).get("category") or "").strip()
                try:
                    pct = float((ln or {}).get("percent") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                if cat and pct > 0:
                    split.append({
                        "category": _live("account",
                                          (ln or {}).get("category_id"), cat),
                        "percent": pct})
        except Exception:                   # noqa: BLE001
            split = []
    return {
        "category": _live("account", getattr(rule, "category_id", None),
                          rule.category),
        "vendor": _live("vendor", getattr(rule, "vendor_id", None), rule.vendor),
        "split": split,
        "rule_id": rule.id,
        "rule_name": rule.name or rule.detail_value or "",
        "mode": rule.mode or "prefill",
        "source": "rule",
    }


def after_ingest(company: str, charge_ids: list, actor: str = "system") -> None:
    """The hook EVERY ingestion path calls once new charges are committed.

    One place, deliberately. Card transactions arrive by CSV today and will
    arrive by API from Amex, Citi, Slash and Divvy later; if each adapter had
    to remember to run the rules, the one that forgot would silently behave
    differently from the rest and nobody would notice for months.

    An adapter's whole obligation is: commit the charges, then call this with
    their ids. Everything else -- whether auto-posting is switched on, which
    rules apply, what happens when QuickBooks refuses -- is decided here.

    Runs on a background thread. Posting is a QuickBooks round trip per charge,
    and a three-hundred row upload would otherwise leave the browser waiting
    minutes for a response it does not need.
    """
    if not charge_ids:
        return

    def work():
        from .db import SessionLocal
        db = SessionLocal()
        try:
            # No separate switch. A rule's mode IS the switch: setting one to
            # Prefill & Post is the decision to post what it matches, and a
            # second global toggle on top of that only created a state where
            # the table said one thing and the app did another.
            # Payments first: they need no rule, and posting them before the
            # rules run keeps them out of a rule that happens to match their
            # bank detail.
            # PROFILES FIRST (Configuration > Profiles): a general card's charge
            # takes the profile of the first matching row, before anything
            # that reads the cardholder -- STH matching included.
            try:
                from .profiles import apply as _apply_profiles
                _apply_profiles(db, company, list(charge_ids))
            except Exception:                           # noqa: BLE001
                log.exception("profiles failed for %s", company)
                db.rollback()
            # RECOGNIZE PAYMENTS FIRST. A charge was only marked as a card
            # payment when it was rescored -- after this hook -- so the rules
            # below saw a newly arrived payment as an ordinary charge, and a
            # rule matching its bank detail (an "AMEX" rule to Inventory Asset)
            # coded and posted it. Marked here, with the same Payment Tags test
            # the rescoring uses, the rules never see it.
            from .review import is_payment as _is_pay, pay_autocode_for as _pac
            for c in db.query(ChargeRow).filter(
                    ChargeRow.company == company,
                    ChargeRow.charge_id.in_(list(charge_ids))).all():
                if _is_pay(c, db) and not c.is_card_payment:
                    c.is_card_payment = True
            db.commit()
            # AND POST THEM ONLY IF THIS COMPANY SAYS SO (Configuration >
            # Settings). This ran regardless of that setting.
            p = (post_card_payments(db, company, actor=actor,
                                    charge_ids=list(charge_ids))
                 if _pac(db, company) else {})
            r = apply_auto_rules(db, company, actor=actor,
                                 charge_ids=list(charge_ids))
            if any((p.get("posted"), p.get("failed"),
                    r.get("posted"), r.get("failed"))):
                log.info("auto-post after ingest for %s: payments=%s rules=%s",
                         company, p, r)
        except Exception:                           # noqa: BLE001
            # An ingestion must not fail because posting did. The charges are
            # already safely in For Review either way.
            log.exception("auto-post after ingest failed for %s", company)
        finally:
            db.close()

    threading.Thread(target=work, daemon=True,
                     name=f"auto-post-{company}").start()


def post_card_payments(db: Session, company: str, actor: str = "system",
                       limit: int = 500, charge_ids: list | None = None) -> dict:
    """Post every recognized card payment. No rule needed.

    A payment has exactly one correct treatment once the primary account is
    designated -- a Transfer between bank accounts, or a Credit Card Payment
    against a card -- and there is no category or vendor for a person to
    choose. So there is nothing for a rule to decide, and asking somebody to
    write one per card would be asking them to restate the obvious.

    Payment Tags already does the recognizing; this posts what it recognized.
    Anything QuickBooks refuses goes back to For Review with the reason on it,
    exactly as the rule runner does.
    """
    # QuickBooks' own entity names, in the words the rest of the app
    # uses. Imported here rather than at module scope: qbo_write
    # imports learning, so the reverse at import time is a cycle.
    from .integrations.qbo_write import entity_word as _entity_word
    from .models_db import AuditRow
    from .integrations.qbo_write import push_charge, assert_writes_allowed

    try:
        assert_writes_allowed()
    except Exception as e:                          # noqa: BLE001
        return {"posted": 0, "failed": 0, "errors": [str(e)], "blocked": True}

    q = (db.query(ChargeRow)
         .filter(ChargeRow.company == company,
                 ChargeRow.status == "for_review",
                 ChargeRow.qbo_txn_id == "",
                 # Provisional rows are never auto-posted. push_charge refuses
                 # them anyway, but excluding them here keeps the counts honest
                 # -- otherwise every pull reports a batch of "failures" that
                 # are really just charges waiting to settle.
                 ChargeRow.is_pending.is_(False),
                 ChargeRow.is_card_payment.is_(True)))
    if charge_ids is not None:
        q = q.filter(ChargeRow.charge_id.in_(charge_ids))
    charges = q.limit(limit).all()
    # LEFT TO MATCH TRANSFER: either side of a transfer it pairs (posted on its
    # own, each side would be a second entry), and anything on the PRIMARY
    # account itself -- a payment out of it is the other side of a card's
    # payment, and posting it as one would move money from and to the same
    # account (QuickBooks refuses; the row flickered out of For Review and back).
    if charges:
        from .models_db import SourceAccountRow as _SAp
        from .transfers import paired_ids as _paired
        _prim = {r.source for r in db.query(_SAp).filter(_SAp.company == company,
                                                         _SAp.is_primary.is_(True)).all()}
        _pairs = _paired(db, company)
        charges = [c for c in charges if c.source not in _prim and c.charge_id not in _pairs]

    posted, failed, errors = 0, 0, []
    for c in charges:
        before = c.status
        # No category and no vendor: the write path builds a Transfer or a
        # Credit Card Payment from the two ACCOUNTS, not from a coding.
        c.status, c.resolution = "categorized", "coded"
        c.rule_source = "payment"
        c.rule_detail = "card payment / transfer"
        c.resolved_by = "system-payment"
        c.resolved_at = datetime.utcnow()
        c.post_error = ""
        db.commit()

        # A RAISED ERROR IS A FAILURE TOO. Uncaught, it left the row marked
        # categorized with nothing in QuickBooks -- out of For Review, and every
        # later Match Transfer refused it as "no longer For Review".
        try:
            result = push_charge(db, c, actor)
        except Exception as _e:                         # noqa: BLE001
            db.rollback()
            result = {"status": "failed", "error": f"{type(_e).__name__}: {_e}"}
        if result.get("status") == "posted":
            posted += 1
            db.add(AuditRow(company=company, action="payment_posted",
                            charge_id=c.charge_id,
                            detail=f"{_entity_word(c.qbo_txn_type) or 'payment'} "
                                   f"{c.qbo_txn_id or ''}".strip(),
                            dry_run=False, actor=actor))
        else:
            failed += 1
            c.post_error = str(result.get("error", ""))[:1000]
            c.status, c.resolution = before, ""
            c.resolved_by, c.resolved_at = "", None
            errors.append(f"{c.merchant or c.charge_id}: {c.post_error[:120]}")
            db.add(AuditRow(company=company, action="payment_post_failed",
                            charge_id=c.charge_id,
                            detail=f"returned to For Review: {c.post_error[:300]}",
                            dry_run=False, actor=actor))
        db.commit()

    if posted or failed:
        log.info("payments posted %d failed %d for %s", posted, failed, company)
    return {"posted": posted, "failed": failed, "errors": errors[:10]}


def apply_auto_rules_async(company: str, actor: str, rule_id: str) -> None:
    """Sweep the backlog for one rule on a background thread.

    Posting is a QuickBooks round trip each, so eighty transactions is a couple
    of minutes. Holding the request open for that leaves the browser on a
    spinner with nothing to read, and someone reasonably concludes it did
    nothing -- which is exactly what happened.
    """
    def work():
        from .db import SessionLocal
        db = SessionLocal()
        try:
            r = apply_auto_rules(db, company, actor=actor, only_rule_id=rule_id)
            log.info("rule %s swept the backlog: %s", rule_id, r)
        except Exception:                           # noqa: BLE001
            log.exception("backlog sweep failed for rule %s", rule_id)
        finally:
            db.close()
    threading.Thread(target=work, daemon=True, name=f"rule-{rule_id}").start()


def apply_auto_rules(db: Session, company: str, actor: str = "system",
                     limit: int = 500, charge_ids: list | None = None,
                     only_rule_id: str | None = None) -> dict:
    """Code and POST every For Review charge matching a Prefill & Post rule.

    The old version of this coded charges to Categorized and stopped, which
    left rows with no QuickBooks transaction behind them -- Delete had nothing
    to reverse and the QuickBooks scan had nothing to check. It also happened
    to be unreachable, so it never ran. This one goes all the way to the
    ledger, which is the only version of the idea that leaves the app coherent.

    Charge by charge, using the same push_charge the reviewer's Add button
    uses, and reverting exactly the way that path reverts:

        posted   -> Categorized, with its QuickBooks id
        refused  -> back to For Review with the error on the row

    A failure must not leave a charge in Categorized pretending to be done.
    That would make the tab lie, and it is the one thing a person cannot see by
    looking.

    Bounded by `limit` because it can run unattended: a rule written too
    broadly should cost a few hundred transactions somebody can undo, not the
    whole queue.
    """
    from .models_db import AuditRow, QboRefRow
    from .integrations.qbo_write import push_charge, assert_writes_allowed

    rules = [r for r in rules_for(db, company)
             if (r.mode or "") == "prefill_post"
             and (r.vendor or "").strip() and (r.category or "").strip()]
    # One rule only, when a rule has just been written or switched on: it
    # should sweep the backlog it covers without every other rule doing the
    # same at the same moment.
    if only_rule_id is not None:
        rules = [r for r in rules if r.id == only_rule_id]
    if not rules:
        return {"posted": 0, "failed": 0, "rules_active": 0, "errors": []}

    # A rule pointing at an account QuickBooks no longer has would fail on
    # every charge it touched, so it is skipped until somebody fixes it rather
    # than being allowed to generate a hundred identical errors.
    known = {r.name for r in db.query(QboRefRow.name).filter(
        QboRefRow.kind == "account").all() if r.name}
    if known:
        rules = [r for r in rules if r.category in known]
    if not rules:
        return {"posted": 0, "failed": 0, "rules_active": 0, "errors": []}

    try:
        assert_writes_allowed()
    except Exception as e:                          # noqa: BLE001
        # DRY_RUN, or writes switched off. Say so rather than reporting a run
        # that quietly did nothing.
        return {"posted": 0, "failed": 0, "rules_active": len(rules),
                "errors": [str(e)], "blocked": True}

    from sqlalchemy import or_ as _or

    q = (db.query(ChargeRow)
         .filter(ChargeRow.company == company,
                 ChargeRow.status == "for_review",
                 ChargeRow.qbo_txn_id == "",
                 # Provisional rows are excluded from the POSTING pass, not
                 # merely refused by it. This function codes a charge, pushes
                 # it, and on anything other than "posted" counts a failure and
                 # REVERTS the coding -- so letting a pending row through would
                 # throw the coding away and report a failure that isn't one.
                 # It settles within days and is coded then, or inherits what a
                 # person coded by hand in the meantime.
                 ChargeRow.is_pending.is_(False)))
    # Scoped to one upload when called from ingestion, so a run triggered by a
    # ten-row file cannot reach back and post something that has been sitting
    # in For Review untouched for a fortnight.
    if charge_ids is not None:
        q = q.filter(ChargeRow.charge_id.in_(charge_ids))

    # THE BANK DETAIL CONDITION IS PUSHED INTO SQL. It used to be applied in
    # Python after `.limit(500)`, which meant the limit fell on the first 500
    # For Review rows in no particular order and the rule was tested only
    # against those. On a queue of three thousand that examined a sixth of it:
    # a rule matching eighty-one transactions posted the nineteen that happened
    # to land inside the slice and silently left the rest.
    #
    # Narrowing first makes the limit mean what it says -- up to `limit`
    # transactions the rule ACTUALLY matches. The remaining conditions (card
    # account, amount) are still checked in Python by rule_matches; they only
    # ever shrink the set further, so nothing is missed by not expressing them
    # here.
    phrases = []
    for r in rules:
        for p_ in (r.detail_value or "").split(","):
            p_ = p_.strip()
            if p_:
                phrases.append((r.detail_op or "contains", p_))
    if phrases and all(p_ for _, p_ in phrases):
        clauses = [ChargeRow.merchant.ilike(p_ if op == "equals" else f"%{p_}%")
                   for op, p_ in phrases]
        q = q.filter(_or(*clauses))

    charges = q.limit(limit).all()

    posted, failed, errors = 0, 0, []
    for c in charges:
        rule = match_rule(db, c, rules)
        if rule is None:
            continue
        # A refund reverses something already posted, so a rule can hold them
        # back while still adding its purchases. The charge stays in For Review
        # with the rule's coding prefilled -- it is a pause, not a rejection.
        if c.is_credit and not getattr(rule, "auto_add_refunds", True):
            continue

        before = c.status
        c.status, c.resolution = "categorized", "coded"
        c.coded_category, c.coded_vendor = rule.category, rule.vendor
        acct = _ref_by_name(db, company, "account", rule.category)
        ven = _ref_by_name(db, company, "vendor", rule.vendor)
        c.coded_category_id = acct.qbo_id if acct else ""
        c.coded_vendor_id = ven.qbo_id if ven else ""
        # WHICH rule, recorded on the charge.
        #
        # This used to be "system-rule" alone, on the reasoning that the User
        # column answers "who" and a rule name is not a who -- with the detail
        # left to the audit entry. But the audit entry is a separate screen and
        # a separate search, and the question "why is this coded this way" is
        # asked while looking at the row. The kind and the name are stored here
        # so the badge and the User column can both answer it without going
        # digging, and without re-deriving it from rules that may since have
        # changed.
        c.rule_source = "bank_rule"
        c.rule_detail = (rule.name or rule.detail_value or "").strip()
        c.resolved_by = "system-rule"
        c.resolved_at = datetime.utcnow()
        c.post_error = ""
        db.commit()

        # A RAISED ERROR IS A FAILURE TOO. Uncaught, it left the row marked
        # categorized with nothing in QuickBooks -- out of For Review, and every
        # later Match Transfer refused it as "no longer For Review".
        try:
            result = push_charge(db, c, actor)
        except Exception as _e:                         # noqa: BLE001
            db.rollback()
            result = {"status": "failed", "error": f"{type(_e).__name__}: {_e}"}
        if result.get("status") == "posted":
            posted += 1
            db.add(AuditRow(
                company=company, action="rule_posted", charge_id=c.charge_id,
                detail=f"rule '{rule.name or rule.detail_value}' -> "
                       f"{rule.category} / {rule.vendor}",
                dry_run=False, actor=actor))
        else:
            failed += 1
            c.post_error = str(result.get("error", ""))[:1000]
            c.status, c.resolution = before, ""
            c.coded_category = c.coded_vendor = ""
            c.coded_category_id = c.coded_vendor_id = ""
            c.resolved_by, c.resolved_at = "", None
            errors.append(f"{c.merchant or c.charge_id}: {c.post_error[:120]}")
            db.add(AuditRow(
                company=company, action="rule_post_failed",
                charge_id=c.charge_id,
                detail=f"rule '{rule.name or rule.detail_value}' could not "
                       f"post, returned to For Review: {c.post_error[:300]}",
                dry_run=False, actor=actor))
        db.commit()

    log.info("rules posted %d and failed %d for %s", posted, failed, company)
    return {"posted": posted, "failed": failed, "rules_active": len(rules),
            "errors": errors[:10]}



def rule_vendor_for_payment(db: Session, charge) -> str:
    """A payment's VENDOR from a rule -- only the vendor, never the category.

    A payment's vendor is normally the card program, found from the card
    account's name; "Cap One" does not find the vendor "Capital One". Rules are
    kept away from payments because a rule's category (Inventory Asset, say)
    must never land on one -- but its vendor is exactly what a person wrote the
    rule to supply. The category always comes from the payment settings.
    """
    for rule in rules_for(db, charge.company):
        try:
            if rule_matches(rule, charge, allow_payment=True) and (rule.vendor or "").strip():
                return rule.vendor.strip()
        except Exception:                               # noqa: BLE001
            continue
    return ""
