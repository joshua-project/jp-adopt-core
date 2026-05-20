"""Matching algorithm tunable parameters (U6).

Everything in this module is intentionally a single-file change so Amy can
retune after Monday's walkthrough without code review on the algorithm itself.
See ``docs/runbooks/matching-algorithm-v1.md`` for the rationale behind each
weight and the procedure for re-tuning + re-running matches.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MatchingWeights:
    """Weighted-sum coefficients for ``score(contact, facilitator)``.

    Weights MUST sum to 1.0 (asserted at module import below). The score is
    bounded in [0, 1] because each component is normalized into that range.
    """

    capacity_headroom: float
    geography: float
    language: float
    fpg_affinity: float
    theological: float

    def sum(self) -> float:  # noqa: A003 — natural name for a weight tuple
        return (
            self.capacity_headroom
            + self.geography
            + self.language
            + self.fpg_affinity
            + self.theological
        )


# v1 weights — inferred from Amy's wishlist language (A2 "wants help
# selecting" + A5 "auto-routing" emphasis on facilitator capacity and
# geographic fit). Treated as a hypothesis until Amy validates post-Monday;
# the cut-order in the plan lets us retune by editing this file.
DEFAULT_WEIGHTS = MatchingWeights(
    capacity_headroom=0.40,
    geography=0.30,
    language=0.15,
    fpg_affinity=0.10,
    theological=0.05,
)

# Top-N candidates promoted to ``match.status='recommended'``. The remaining
# scored facilitators only show up as MatchAttempt audit rows, not as Match
# rows in Amy's queue.
TOP_N_RECOMMENDED = 3

# Minimum score floor: candidates scoring below this are NOT promoted, even
# if they pass the hard filter and slot into top-N. Catches "no-one is a
# good fit" cases so the queue isn't padded with weak matches.
#
# 0.0 in v1 (= no floor) so the queue is never silently empty when something
# exists. Tighten in v2 once Amy has data on what a "weak" match looks like.
MIN_SCORE_FLOOR = 0.0

# Safety floor for capacity_headroom: zero-capacity facilitators are removed
# by the hard filter, so this value is only used for the smallest non-zero
# capacity_total (defends against divide-by-zero in the score function).
CAPACITY_DIVIDE_BY_ZERO_FALLBACK = 1


# Sanity-check at import time. A weight bug shouldn't show up only at runtime.
def _assert_weights_sum_to_one() -> None:
    total = DEFAULT_WEIGHTS.sum()
    if abs(total - 1.0) > 1e-6:
        raise RuntimeError(
            f"matching_config: DEFAULT_WEIGHTS must sum to 1.0, got {total:.6f}"
        )


_assert_weights_sum_to_one()


__all__ = [
    "CAPACITY_DIVIDE_BY_ZERO_FALLBACK",
    "DEFAULT_WEIGHTS",
    "MIN_SCORE_FLOOR",
    "TOP_N_RECOMMENDED",
    "MatchingWeights",
]
