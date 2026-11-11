"""The one place that decides when two email addresses are the same address."""


def normalise_key(addr: str) -> str:
    """Comparison key for an address: trimmed and lower-cased.

    Used for duplicate detection, ``unsubscribed.txt`` matching and unsubscribe tokens, so an
    address that opted out is recognised however it was capitalised.
    """
    return addr.strip().lower()
