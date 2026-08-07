"""What a claim IS, and which extracted claim answers which labeled one.

The benchmark asks two questions of the full pipeline and they are not the same question:
*did Attest reach the right verdict?* and *did the decomposer transcribe the sentence into
the claim the label is about?* Answering the second one needs a definition of sameness, and
answering the first one needs to know WHICH extracted claim to read the verdict off.

Until this module existed both were done by one line — `next(a for a in report.audits if
a.claim.target_urn == case.target_urn)` — which is neither. It bound the FIRST claim
touching the case's URN, so a wrong-family claim answered a question nobody asked, a claim
extracted second could not win against a wrong one extracted first, and everything the
selector did not pick was discarded unexamined: a duplicate read as a perfect extraction and
an unintended extra was structurally invisible. Five defects, one expression.

--------------------------------------------------------------------------------
The canonical subject
--------------------------------------------------------------------------------

`subject()` is what a claim is ABOUT, normalized: its family, its target, and the fields
that family asserts. `raw_text` is excluded because it is the model's quotation of the agent
and varies in whitespace and length without meaning anything.

**Precision is part of the subject, and this is deliberately STRICTER than the checker.**
checkers/schema.py matches "VARCHAR" against "VARCHAR(36)" on purpose: leniency there is
correct, because nobody claimed the 36 and flagging it would be crying wolf. Fidelity asks a
different question — did the decomposer transcribe the sentence the human labeled? The
sentence said `NUMBER(12,2)`. So `norm_type` normalizes FORMATTING only (case, whitespace)
and never semantics: `NUMBER(12,2)` == `number(12, 2)`, and `NUMBER(12,2)` != `NUMBER`.

A consequence, stated rather than discovered later: a case can now be PARTIAL while its
verdict is unaffected, because the checker forgives what fidelity does not. That is a
finding — a partially correct subject — and not a false alarm. Weakening this to make a
number go back to 100% would be the benchmark author's characteristic failure.

**`None` is not a type.** A column asserted with no type ("has a payload column") and one
asserted as `VARCHAR` are different claims, and `norm_type(None) is None` keeps them apart.

**Multisets, not sets.** Columns and labels are compared as SORTED TUPLES, so a claim that
names the same column twice is not silently equal to one that names it once. Collapsing
duplicates inside a subject would hide, one level down, exactly the duplicate this module
exists to count one level up.

--------------------------------------------------------------------------------
Four outcomes, not a boolean
--------------------------------------------------------------------------------

Same reason report.CorrectionOutcome names six outcomes: a success flag hides the failure
modes that matter. EXACT and PARTIAL both bound a claim the case can be scored against;
WRONG_FAMILY and MISSING did not, and the caller must not read a verdict off either.

--------------------------------------------------------------------------------
The matching is ONE-TO-ONE, tier-ordered, and deterministic
--------------------------------------------------------------------------------

Three passes — exact, then same-family-and-URN, then same-URN — and every extracted claim is
claimed by at most one expected claim, so no single audit can satisfy two labels.

Tier-greedy is OPTIMAL here, not merely convenient, and the argument is short enough to
keep: pass 1 completes before pass 2 begins, so no partial match can consume an extracted
claim that some other expected claim matched exactly; and if two expected claims both match
one extracted claim exactly then their subjects are equal and they are interchangeable. The
same holds inside pass 2 keyed on (family, URN). That is why there is no assignment solver
here and no SciPy: an optimal answer is already reachable in three ordered sweeps.

Determinism is by construction. Candidates are ranked by `subject_distance` and ties are
broken by POSITION in the extracted list — never by set or dict iteration order — so the
same report scores identically every time, which a benchmark's own arithmetic must.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from attest.claims import Claim, ClaimType

# The benchmark contract: one Case carries exactly ONE labeled claim (`Case.claim` is a
# single mapping). `match()` is written for the general N-to-M case anyway — it is a pure
# function and the general form is no harder — but the caller asserts this, so that a
# future multi-claim case cannot quietly start relying on the tie-break below without
# somebody deciding it should.
EXPECTED_CLAIMS_PER_CASE = 1


class Fidelity(StrEnum):
    """How well the decomposer's output answers one labeled claim."""

    # The extracted claim IS the labeled claim's subject. The only clean outcome.
    EXACT = "exact"
    # Same family, same target, different assertion — the right question about the right
    # entity, transcribed wrong. Scorable: there IS a verdict about this subject.
    PARTIAL = "partial-subject"
    # Something was extracted about this entity, but it is a different KIND of claim. Its
    # verdict answers a question the case did not ask, and must never be scored as one.
    WRONG_FAMILY = "wrong-family"
    # Nothing at all was extracted about this entity.
    MISSING = "missing"

    @property
    def scorable(self) -> bool:
        """May a verdict be read off the bound audit for this outcome?"""
        return self in (Fidelity.EXACT, Fidelity.PARTIAL)


def norm_type(native: str | None) -> str | None:
    """FORMATTING only. `NUMBER(12,2)` == `number(12, 2)`; `NUMBER(12,2)` != `NUMBER`."""
    if native is None:
        return None
    return "".join(native.split()).casefold()


def _column_key(name: str, native: str | None) -> tuple[str, bool, str]:
    """A total order over (name, type) pairs where the type may be None.

    `sorted()` on raw pairs raises comparing None to str, and a benchmark that crashes on a
    typeless column is not an option. None sorts first and stays DISTINCT from "".
    """
    return (name, native is not None, native or "")


def _family_fields(claim: Claim) -> tuple[object, ...]:
    """The fields this claim's family asserts, normalized. Never includes raw_text."""
    match claim.claim_type:
        case ClaimType.FRESHNESS:
            return (float(claim.max_age_hours),)  # type: ignore[union-attr]
        case ClaimType.OWNERSHIP:
            return (claim.owner_urn,)  # type: ignore[union-attr]
        case ClaimType.CLASSIFICATION:
            return (
                tuple(sorted(claim.labels)),  # type: ignore[union-attr]
                claim.present,  # type: ignore[union-attr]
                claim.field_path,  # type: ignore[union-attr]
            )
        case ClaimType.SCHEMA:
            return (
                tuple(
                    sorted(
                        ((c.name, norm_type(c.native_type)) for c in claim.columns),  # type: ignore[union-attr]
                        key=lambda pair: _column_key(*pair),
                    )
                ),
            )
    raise AssertionError(f"unhandled claim family: {claim.claim_type!r}")


def subject(claim: Claim) -> tuple[object, ...]:
    """The canonical subject: family, target, and what the family asserts."""
    return (claim.claim_type.value, claim.target_urn, *_family_fields(claim))


def fingerprint(claims: Sequence[Claim]) -> tuple[str, ...]:
    """A sorted, stringified MULTISET of subjects — what pass@k compares across runs.

    Sorted so extraction order does not read as instability; a multiset (a sorted tuple, not
    a set) so a run that extracted one claim twice is distinguishable from one that
    extracted it once. Comparing only the SELECTED claim, as pass@k used to, made run-to-run
    variance in the extras invisible — the same blind spot this module closes one level up.
    """
    return tuple(sorted(repr(subject(c)) for c in claims))


def subject_distance(expected: Claim, extracted: Claim) -> int:
    """How far apart two subjects of the SAME family are. Deterministic, integer.

    Only ever used to rank PARTIAL candidates. It is a ranking key, not a measurement, and
    nothing is reported in these units.
    """
    if expected.claim_type is not extracted.claim_type:
        raise ValueError("subject_distance compares claims of one family")

    match expected.claim_type:
        case ClaimType.FRESHNESS | ClaimType.OWNERSHIP:
            return int(_family_fields(expected) != _family_fields(extracted))
        case ClaimType.CLASSIFICATION:
            want, got = expected, extracted
            labels = Counter(want.labels) - Counter(got.labels)  # type: ignore[union-attr]
            labels += Counter(got.labels) - Counter(want.labels)  # type: ignore[union-attr]
            return (
                sum(labels.values())
                + int(want.present != got.present)  # type: ignore[union-attr]
                + int(want.field_path != got.field_path)  # type: ignore[union-attr]
            )
        case ClaimType.SCHEMA:
            want_cols = Counter(
                (c.name, norm_type(c.native_type)) for c in expected.columns  # type: ignore[union-attr]
            )
            got_cols = Counter(
                (c.name, norm_type(c.native_type)) for c in extracted.columns  # type: ignore[union-attr]
            )
            diff = (want_cols - got_cols) + (got_cols - want_cols)
            return sum(diff.values())
    raise AssertionError(f"unhandled claim family: {expected.claim_type!r}")


def subject_diff(expected: Claim, extracted: Claim) -> tuple[str, ...]:
    """Field-level differences, for a human. A count alone does not say what moved."""
    if expected.claim_type is not extracted.claim_type:
        return (f"claim_type: {expected.claim_type.value} -> {extracted.claim_type.value}",)
    if expected.target_urn != extracted.target_urn:
        return (f"target_urn: {expected.target_urn} -> {extracted.target_urn}",)

    out: list[str] = []
    match expected.claim_type:
        case ClaimType.FRESHNESS:
            if expected.max_age_hours != extracted.max_age_hours:  # type: ignore[union-attr]
                out.append(
                    f"max_age_hours: {expected.max_age_hours} -> {extracted.max_age_hours}"  # type: ignore[union-attr]
                )
        case ClaimType.OWNERSHIP:
            if expected.owner_urn != extracted.owner_urn:  # type: ignore[union-attr]
                out.append(f"owner_urn: {expected.owner_urn} -> {extracted.owner_urn}")  # type: ignore[union-attr]
        case ClaimType.CLASSIFICATION:
            if sorted(expected.labels) != sorted(extracted.labels):  # type: ignore[union-attr]
                out.append(
                    f"labels: {sorted(expected.labels)} -> {sorted(extracted.labels)}"  # type: ignore[union-attr]
                )
            if expected.present != extracted.present:  # type: ignore[union-attr]
                out.append(f"present: {expected.present} -> {extracted.present}")  # type: ignore[union-attr]
            if expected.field_path != extracted.field_path:  # type: ignore[union-attr]
                out.append(
                    f"field_path: {expected.field_path!r} -> {extracted.field_path!r}"  # type: ignore[union-attr]
                )
        case ClaimType.SCHEMA:
            want = {c.name: c.native_type for c in expected.columns}  # type: ignore[union-attr]
            got = {c.name: c.native_type for c in extracted.columns}  # type: ignore[union-attr]
            for name in sorted(set(want) | set(got)):
                if name not in got:
                    out.append(f"column {name!r}: dropped")
                elif name not in want:
                    out.append(f"column {name!r}: added")
                elif norm_type(want[name]) != norm_type(got[name]):
                    out.append(f"column {name!r} native_type: {want[name]!r} -> {got[name]!r}")
    return tuple(out)


@dataclass(frozen=True)
class Pair:
    """One labeled claim, and the extracted claim (if any) bound to it."""

    expected_index: int
    extracted_index: int | None
    fidelity: Fidelity
    distance: int = 0
    diff: tuple[str, ...] = ()


@dataclass(frozen=True)
class Matching:
    """The whole assignment, plus everything it could not account for.

    `extras` and `duplicates` are DISJOINT and together are every unmatched extracted claim.
    A duplicate is an unmatched claim whose subject equals one that WAS bound — the
    decomposer said the same thing twice. An extra is anything else the decomposer produced
    and nothing asked for, including a claim about an entirely different URN: each benchmark
    case is audited on its own prose, so there is nothing else it could belong to.
    """

    pairs: tuple[Pair, ...]
    extras: tuple[int, ...] = ()
    duplicates: tuple[int, ...] = ()

    @property
    def unmatched(self) -> tuple[int, ...]:
        return tuple(sorted((*self.extras, *self.duplicates)))


def match(expected: Sequence[Claim], extracted: Sequence[Claim]) -> Matching:
    """Bind labeled claims to extracted ones, one-to-one, best tier first."""
    exp_subjects = [subject(c) for c in expected]
    ext_subjects = [subject(c) for c in extracted]

    taken: set[int] = set()
    bound: dict[int, Pair] = {}

    # Pass 1 — EXACT. Completed for every expected claim before any weaker tier runs, which
    # is what makes the greedy assignment optimal rather than merely convenient.
    for i in range(len(expected)):
        for j in range(len(extracted)):
            if j not in taken and ext_subjects[j] == exp_subjects[i]:
                taken.add(j)
                bound[i] = Pair(i, j, Fidelity.EXACT)
                break

    # Pass 2 — PARTIAL: the right question about the right entity, asserted differently.
    # Ranked by subject distance, ties broken by POSITION. Never by set iteration order.
    for i, want in enumerate(expected):
        if i in bound:
            continue
        candidates = [
            (subject_distance(want, extracted[j]), j)
            for j in range(len(extracted))
            if j not in taken
            and extracted[j].claim_type is want.claim_type
            and extracted[j].target_urn == want.target_urn
        ]
        if candidates:
            distance, j = min(candidates)
            taken.add(j)
            bound[i] = Pair(
                i, j, Fidelity.PARTIAL, distance, subject_diff(want, extracted[j])
            )

    # Pass 3 — WRONG_FAMILY: something about this entity, but not this kind of claim. Bound
    # so it can be REPORTED, never so a verdict can be read off it.
    for i, want in enumerate(expected):
        if i in bound:
            continue
        for j in range(len(extracted)):
            if j not in taken and extracted[j].target_urn == want.target_urn:
                taken.add(j)
                bound[i] = Pair(
                    i, j, Fidelity.WRONG_FAMILY, diff=subject_diff(want, extracted[j])
                )
                break

    for i in range(len(expected)):
        bound.setdefault(i, Pair(i, None, Fidelity.MISSING))

    matched_subjects = Counter(
        ext_subjects[p.extracted_index]
        for p in bound.values()
        if p.extracted_index is not None
    )
    leftover = [j for j in range(len(extracted)) if j not in taken]
    duplicates = tuple(j for j in leftover if ext_subjects[j] in matched_subjects)
    extras = tuple(j for j in leftover if ext_subjects[j] not in matched_subjects)

    return Matching(
        pairs=tuple(bound[i] for i in range(len(expected))),
        extras=extras,
        duplicates=duplicates,
    )
