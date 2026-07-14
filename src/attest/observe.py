"""The run's own audit trail: what ran, in what order, on what, for how long, at what cost.

Attest audits other systems for making claims it cannot back up. A README that says "12
claims in 4.1s for $0.0007" is itself a claim about data, and it would be embarrassing —
and disqualifying — for that number to be an estimate. So it is measured here, from the
clock and from the API's own token counts, and every step of every run is recorded whether
anyone reads it or not.

**Every step carries a `kind`, and the kind is load-bearing.** It is not a logging nicety
or a colour in a dashboard. `DETERMINISTIC`, `LLM`, and `IO` are what make the central
architectural claim of this project *checkable after the fact*: the verdict path contains
no model call. trajectory.py asserts exactly that against this trace, so a future edit
that quietly slips an LLM into a checker fails a test rather than a code review. A trace
that recorded only step names could not catch it, and would be the "log line, not an
assertion" version of trajectory verification that this design exists to avoid.

The trace is append-only and never reset mid-run. A step that raised is recorded with its
error and re-raised — a stage that failed still ran, and hiding it would put a hole in the
trajectory exactly where something went wrong.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from attest.cost import Cost
from attest.llm import LLM

log = logging.getLogger(__name__)


class StepKind(StrEnum):
    """What a step is made of. The whole point of trajectory verification.

    DETERMINISTIC — code only: date math, set membership, string comparison, the guard.
    LLM           — a model was called. Permitted ONLY to decompose and to phrase.
    IO            — a read from DataHub. No decision, just a fetch.
    """

    DETERMINISTIC = "deterministic"
    LLM = "llm"
    IO = "io"


@dataclass
class StepRecord:
    """One node's execution: its inputs, its outputs, its latency, its tokens."""

    name: str
    kind: StepKind
    # Which claim this step was working on. None for run-level steps (sanitize,
    # decompose, assemble) that happen once and are not about any single claim.
    claim_index: int | None = None
    latency_ms: float = 0.0
    # Deliberately summaries, not payloads: a trace that embeds every snapshot is a
    # memory leak wearing a debugging hat. What is needed is enough to see the path.
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    # The models this step called, if any. Empty for deterministic and IO steps.
    models: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_llm(self) -> bool:
        return self.kind is StepKind.LLM

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __str__(self) -> str:
        where = "" if self.claim_index is None else f"[claim {self.claim_index}]"
        cost = "" if not self.total_tokens else f" {self.total_tokens}tok"
        failed = "" if self.error is None else f" ERROR: {self.error}"
        return f"{self.name}{where} ({self.kind.value}) {self.latency_ms:.0f}ms{cost}{failed}"


@dataclass
class Trace:
    """Every step of one audit run, in order."""

    steps: list[StepRecord] = field(default_factory=list)

    def __iter__(self) -> Iterator[StepRecord]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.steps]

    def for_claim(self, index: int) -> list[StepRecord]:
        """Every step that ran on one claim, in order. The claim's own trajectory."""
        return [s for s in self.steps if s.claim_index == index]

    def named(self, name: str) -> list[StepRecord]:
        return [s for s in self.steps if s.name == name]

    @property
    def latency_ms(self) -> float:
        """Summed step latency. Sequential graph, so this is the wall clock, near enough."""
        return sum(s.latency_ms for s in self.steps)

    @property
    def cost(self) -> Cost:
        """The run's total spend, or an explicit unknown. Never a silent zero."""
        input_tokens = sum(s.input_tokens for s in self.steps)
        output_tokens = sum(s.output_tokens for s in self.steps)

        # A step that called a model but could not be priced poisons the total, on
        # purpose. See cost.py: a partial sum printed as a receipt is worse than none.
        unpriced = tuple(
            sorted(
                {
                    m
                    for s in self.steps
                    if s.is_llm and s.cost_usd is None and s.total_tokens
                    for m in s.models
                }
            )
        )
        usd: float | None
        if unpriced:
            usd = None
        else:
            usd = sum(s.cost_usd or 0.0 for s in self.steps)

        return Cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
            unpriced_models=unpriced,
        )

    @contextmanager
    def step(
        self,
        name: str,
        kind: StepKind,
        *,
        claim_index: int | None = None,
        llm: LLM | None = None,
        **inputs: Any,
    ) -> Iterator[StepRecord]:
        """Record one step. Times it, bills it, and records it even if it raises.

        `llm` is passed on steps that may call a model. The usage list is sliced from
        the mark, so this bills whatever the step actually spent — including a retry the
        step made internally, which a caller counting its own calls would miss.
        """
        record = StepRecord(name=name, kind=kind, claim_index=claim_index, inputs=inputs)
        mark = len(llm.usage) if llm is not None else 0
        started = time.perf_counter()

        try:
            yield record
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.latency_ms = (time.perf_counter() - started) * 1000

            if llm is not None:
                spent = llm.usage[mark:]
                record.input_tokens = sum(u.input_tokens for u in spent)
                record.output_tokens = sum(u.output_tokens for u in spent)
                record.models = tuple(dict.fromkeys(u.model for u in spent))
                # None if any call in this step was unpriceable — same rule as Cost.
                if spent and all(u.cost_usd is not None for u in spent):
                    record.cost_usd = sum(u.cost_usd or 0.0 for u in spent)

            self.steps.append(record)
            log.info("%s", record)
