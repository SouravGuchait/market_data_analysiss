"""Apply the rules to a row and tally rule-level pass/fail counts.

Validation is the seam between "what the file said" and "what we will store". It
merges the structural problems the normaliser found (a missing field, an
unparseable date) with the domain findings from
:mod:`apps.ingestion.etl.rules`, so a caller sees one list of reasons per row.

A row with no outcomes is accepted; a row with outcomes is rejected and its
outcomes become validation-error records. Nothing here touches the database.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .errors import ROW_RULES
from .rules import DOMAIN_RULES, RuleContext
from .schemas import NormalizedRow, Outcome


def validate_row(row: NormalizedRow, context: RuleContext) -> list[Outcome]:
    """Return every reason this row must be rejected, or ``[]`` if it is clean.

    Contract
    --------
    in : a normalised row and the batch context (today, known symbols, seen keys).
    out: the structural issues from normalisation followed by every domain
         finding, in the fixed order of :data:`DOMAIN_RULES`.

    Business purpose: one function decides a row's fate, so "is this row good?"
    has exactly one answer and exactly one place to test it.
    """
    outcomes = list(row.issues)
    for rule in DOMAIN_RULES:
        outcomes.extend(rule(row, context))
    return outcomes


@dataclass
class ValidationTally:
    """Running counts for the whole batch.

    ``failures`` counts, per rule name, how many rows produced a finding. The
    pass count for a rule is ``evaluated - failures``: a row that produced no
    finding for a rule passed it. A rule skipped because a prerequisite was
    missing also counts as a pass, because no violation was found.
    """

    evaluated: int = 0
    accepted: int = 0
    rejected: int = 0
    failures: Counter = field(default_factory=Counter)

    def observe(self, outcomes: Iterable[Outcome]) -> None:
        """Record one row's verdict."""
        outcomes = list(outcomes)
        self.evaluated += 1
        if outcomes:
            self.rejected += 1
            for outcome in outcomes:
                self.failures[outcome.rule] += 1
        else:
            self.accepted += 1

    def counts_for(self, rule: str) -> tuple[int, int]:
        """Return ``(passed, failed)`` for one rule."""
        failed = self.failures.get(rule, 0)
        return max(self.evaluated - failed, 0), failed

    def as_rule_counts(self) -> dict[str, tuple[int, int]]:
        """Return ``{rule: (passed, failed)}`` for every row rule."""
        return {rule: self.counts_for(rule) for rule in ROW_RULES}


def evaluate(
    rows: Iterable[NormalizedRow], context: RuleContext, tally: ValidationTally
) -> Iterator[tuple[NormalizedRow, list[Outcome]]]:
    """Stream rows through :func:`validate_row`, updating ``tally`` as it goes.

    Memory stays flat: only one row and its outcomes are alive at a time, which
    is what lets a large file be processed without buffering all of it.
    """
    for row in rows:
        outcomes = validate_row(row, context)
        tally.observe(outcomes)
        yield row, outcomes
