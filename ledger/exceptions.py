class AccountingError(Exception):
    """Base exception for accounting engine errors."""
    pass


class UnbalancedJournalEntryError(AccountingError):
    """Raised when debit total does not equal credit total."""
    pass


class AccountingPeriodClosedError(AccountingError):
    """Raised when attempting to post into a closed or locked period."""
    pass


class InvalidJournalStateError(AccountingError):
    """Raised when an operation is invalid for the journal entry's current status."""
    pass


class AccountMismatchError(AccountingError):
    """Raised when an account belongs to a different organization or is inactive."""
    pass
