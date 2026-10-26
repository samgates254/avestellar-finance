import uuid
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from ledger.exceptions import (
    AccountMismatchError,
    AccountingPeriodClosedError,
    InvalidJournalStateError,
    UnbalancedJournalEntryError,
)
from ledger.models import JournalEntry, JournalLine, JournalStatus, PeriodStatus


@transaction.atomic
def post_journal_entry(entry_id: uuid.UUID | str, user=None) -> JournalEntry:
    """Posts a draft journal entry to the general ledger atomically.

    Enforces mandatory accounting invariants:
    1. Journal entry must be in DRAFT status.
    2. Accounting period must be OPEN.
    3. Must contain at least two lines.
    4. Total debit must strictly equal total credit (debit == credit > 0).
    5. All line accounts must belong to the same organization and be active.
    6. Atomically commits changes and publishes a 'journal.posted' outbox event.
    """
    entry = (
        JournalEntry.objects.select_for_update()
        .select_related("organization", "period")
        .get(id=entry_id)
    )

    if entry.status != JournalStatus.DRAFT:
        raise InvalidJournalStateError(
            f"Cannot post journal entry {entry.entry_number}: current status is '{entry.status}', expected 'DRAFT'."
        )

    if entry.period.status != PeriodStatus.OPEN:
        raise AccountingPeriodClosedError(
            f"Cannot post into closed or locked accounting period '{entry.period.name}' (status: {entry.period.status})."
        )

    lines = list(entry.lines.select_related("account").all())
    if len(lines) < 2:
        raise UnbalancedJournalEntryError(
            f"Journal entry {entry.entry_number} must have at least two lines to satisfy double-entry accounting rules."
        )

    total_debit = Decimal("0.0000")
    total_credit = Decimal("0.0000")

    for line in lines:
        if line.account.organization_id != entry.organization_id:
            raise AccountMismatchError(
                f"Account {line.account.code} belongs to a different organization."
            )
        if not line.account.is_active:
            raise AccountMismatchError(
                f"Account {line.account.code} is inactive and cannot accept postings."
            )
        total_debit += line.debit
        total_credit += line.credit

    # Enforce debit == credit
    if total_debit != total_credit or total_debit <= Decimal("0.0000"):
        raise UnbalancedJournalEntryError(
            f"Journal entry {entry.entry_number} is unbalanced: Debits ({total_debit}) != Credits ({total_credit})."
        )

    # Commit posted state
    now = timezone.now()
    entry.total_debit = total_debit
    entry.total_credit = total_credit
    entry.status = JournalStatus.POSTED
    entry.posted_at = now
    entry.posted_by = user
    entry.save(update_fields=["status", "total_debit", "total_credit", "posted_at", "posted_by", "updated_at"])

    # Transactional outbox event
    publish_outbox_event(
        event_type="journal.posted",
        organization=entry.organization,
        payload={
            "journal_entry_id": str(entry.id),
            "entry_number": entry.entry_number,
            "date": str(entry.date),
            "amount": str(entry.total_debit),
            "currency": entry.currency,
            "reference": entry.reference,
            "period_id": str(entry.period_id),
            "line_count": len(lines),
        },
    )

    # Audit log
    record_audit_log(
        action="POST_JOURNAL",
        target_model="JournalEntry",
        target_id=str(entry.id),
        organization=entry.organization,
        user=user,
        details={
            "entry_number": entry.entry_number,
            "amount": str(entry.total_debit),
            "currency": entry.currency,
        },
    )

    return entry


@transaction.atomic
def reverse_journal_entry(
    entry_id: uuid.UUID | str,
    reversal_date=None,
    reason: str = "Correction of posted entry",
    user=None,
) -> JournalEntry:
    """Executes a formal accounting reversal for a previously posted journal entry.

    Creates an inverted mirror journal entry, posts it, and marks original entry as REVERSED.
    """
    original = (
        JournalEntry.objects.select_for_update()
        .select_related("organization", "period")
        .get(id=entry_id)
    )

    if original.status != JournalStatus.POSTED:
        raise InvalidJournalStateError(
            f"Cannot reverse journal entry {original.entry_number}: status is '{original.status}', expected 'POSTED'."
        )

    if hasattr(original, "reversal_of") and original.reversal_of:
        raise InvalidJournalStateError(
            f"Journal entry {original.entry_number} has already been reversed."
        )

    rev_date = reversal_date or timezone.now().date()
    reversal_number = f"REV-{original.entry_number}"

    # Create reversing entry
    reversing_entry = JournalEntry.objects.create(
        organization=original.organization,
        period=original.period,
        entry_number=reversal_number,
        date=rev_date,
        description=f"Reversal of {original.entry_number}: {reason}",
        reference=f"REV:{original.reference}" if original.reference else f"REV:{original.entry_number}",
        status=JournalStatus.DRAFT,
        currency=original.currency,
        created_by=user,
        is_auto_reversal=True,
    )

    # Invert the lines: swap debit and credit
    for line in original.lines.all():
        JournalLine.objects.create(
            journal_entry=reversing_entry,
            account=line.account,
            description=f"Reversal: {line.description}" if line.description else "Reversal line",
            debit=line.credit,  # swapped
            credit=line.debit,  # swapped
            sequence=line.sequence,
        )

    # Post reversing entry
    reversing_entry = post_journal_entry(reversing_entry.id, user=user)

    # Link reversal to original
    original.status = JournalStatus.REVERSED
    original.reversed_entry = reversing_entry
    original.save(update_fields=["status", "reversed_entry", "updated_at"])

    # Outbox event
    publish_outbox_event(
        event_type="journal.reversed",
        organization=original.organization,
        payload={
            "original_journal_id": str(original.id),
            "reversing_journal_id": str(reversing_entry.id),
            "entry_number": original.entry_number,
            "reversal_number": reversing_entry.entry_number,
            "reason": reason,
        },
    )

    # Audit log
    record_audit_log(
        action="REVERSE_JOURNAL",
        target_model="JournalEntry",
        target_id=str(original.id),
        organization=original.organization,
        user=user,
        details={
            "reversal_entry_id": str(reversing_entry.id),
            "reversal_number": reversing_entry.entry_number,
            "reason": reason,
        },
    )

    return reversing_entry
