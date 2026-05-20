"""Shared email-normalization helper.

Every IdP integration and every intake endpoint needs the same notion of
"normalized email" so that ``identity_link.email_normalized``,
``contacts.email_normalized``, and rate-limit / idempotency keys all agree.

The canonical form is: lowercased, surrounding whitespace stripped, and a
trailing dot (FQDN literal form) removed. This is intentionally stricter than
``str.lower()`` so a domain typed as ``"User@Example.com."`` still collides
with the row already in the DB at ``"user@example.com"``.
"""

from __future__ import annotations


def normalize_email(raw: str) -> str:
    """Return the canonical form used everywhere the API persists an email.

    * ``strip()`` — trim ASCII whitespace and newlines.
    * ``rstrip('.')`` — collapse FQDN-literal trailing dots.
    * ``lower()`` — case-insensitive equality is the dedup contract.
    """
    return raw.strip().rstrip(".").lower()


__all__ = ["normalize_email"]
