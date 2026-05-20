"""Shared mapping from state-machine exceptions to FastAPI HTTPExceptions.

Both ``routers/matches.py`` and ``routers/workflow.py`` invoke
``transition_adopter`` / ``transition_facilitator`` and need to render the
documented domain exceptions as structured HTTP errors. The mapping is
identical between the two routers — extract once so the response shapes can't
drift apart.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from jp_adopt_api.domain.state_machine import (
    ConcurrentModificationError,
    IllegalTransitionError,
    InvalidReasonCodeError,
    ReasonRequiredError,
    RoleNotPermittedError,
)


def map_state_machine_exception(e: Exception) -> HTTPException:
    """Translate a domain state-machine exception into an HTTPException.

    The ``except`` clauses that invoke this function enumerate exactly the
    five exception types below, so no fallback path is needed; if a non-
    enumerated exception ever reaches here it should propagate as a 500
    naturally via FastAPI's default handling rather than being silently
    rewritten.
    """
    if isinstance(e, ReasonRequiredError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "reason_required", "message": str(e)},
        )
    if isinstance(e, InvalidReasonCodeError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_reason_code", "message": str(e)},
        )
    if isinstance(e, RoleNotPermittedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "role_not_permitted", "message": str(e)},
        )
    if isinstance(e, IllegalTransitionError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "illegal_transition", "message": str(e)},
        )
    if isinstance(e, ConcurrentModificationError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "concurrent_modification", "message": str(e)},
        )
    # The enumerated except clauses guarantee we never hit this branch; raise
    # so a future caller that broadens the except list above doesn't silently
    # get an opaque 500.
    raise TypeError(
        f"map_state_machine_exception() got unmapped exception: {type(e).__name__}"
    )


__all__ = ["map_state_machine_exception"]
