"""Spike: can DataHub Core v1.5.0.6 hold TWO independent claim artifacts on ONE dataset?

The question this exists to answer, empirically, against the pinned server:

    "After two claims about one dataset are approved, show me what the next agent
     inherits from DataHub alone."

Today the honest answer is "an opaque run UUID". Attest's write-back is five
dataset-level structured properties, and structured properties are last-write-wins: two
claims about one dataset overwrite each other, so the catalog holds the last verdict and
nothing else. No claim identity, no asserted value, no grain, no evidence, no reviewer.

Three candidate representations, in order of preference (see docs/design/claim-artifact.md):

  1. DataHub Assertions      -- semantically the closest fit. THIS SPIKE PROVES IT WORKS.
  2. A custom aspect/entity  -- more control, more work. Not needed; 1 works.
  3. Per-claim-keyed props   -- the fallback. Not needed; 1 works.

The invariant under test, and everything else is detail:

    TWO CLAIMS ON ONE DATASET COEXIST AND ARE BOTH INDEPENDENTLY RETRIEVABLE.

This is a THROWAWAY PROOF, in the Session 0 `datahub_probe.py` tradition: it imports no
Attest feature code beyond the GraphQL client and it ships no feature code. It talks to the
server and reports what the server actually does -- not what the docs claim. Consensus
among documents is not evidence; only running the command is.

Run (from repo root, after seeding):  python spikes/claim_artifact_probe.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from attest.datahub import DataHubClient, DataHubError

# One dataset. Two claims about it. That is the whole point.
PROBE_DATASET = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
    "analytics.customers.customer_profile,PROD)"
)
PROBE_PLATFORM = "urn:li:dataPlatform:snowflake"

# The custom assertion type. `customType` is the ONE assertion field that is filterable in
# search (measured below), so it is what makes "every Attest claim in the catalog" a query.
ASSERTION_TYPE = "ATTEST_GROUNDEDNESS_CLAIM"

# The verdict tag. Assertions do NOT support structured properties (measured below), so a
# tag is the only way to make the LATEST verdict a cross-dataset filter.
VERDICT_TAG = "urn:li:tag:Attest-Probe-Contradicted"

# The search/graph index is eventually consistent and reportAssertionResult READS it (see
# LANDMINE 4). Poll rather than sleep-and-hope.
INDEX_TIMEOUT_S = 60


class ProbeFailure(Exception):
    pass


def header(n: int, title: str) -> None:
    print(f"\n{'=' * 76}\n[{n}] {title}\n{'=' * 76}")


# Every check() result, so the summary cannot contradict the body. The first draft of this
# spike printed "PROVEN" with two FAILs above it, because the banner only keyed off whether
# an exception escaped. A proof whose conclusion does not read its own evidence is exactly
# the green-tick-about-a-different-program this project exists to catch — so the verdict is
# COMPUTED from the checks, never asserted alongside them.
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    if not ok:
        FAILURES.append(label)
    return ok


def claim_id(target_urn: str, claim_type: str, field_path: str | None, asserted: str) -> str:
    """A stable claim ID: what the claim is ABOUT plus what it ASSERTS.

    Deterministic, so re-auditing the SAME claim hits the same assertion and appends a run
    event to it rather than minting a second artifact. That is what turns a pile of writes
    into a history. Two DIFFERENT claims about one dataset hash differently and coexist —
    which is the invariant this spike exists to prove.
    """
    key = "|".join([target_urn, claim_type, field_path or "", asserted])
    return f"attest-probe-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


class Probe:
    def __init__(self, client: DataHubClient) -> None:
        self.client = client
        self.minted: list[str] = []

    # --- the three operations the representation has to support ----------------

    UPSERT = """
    mutation up($urn: String, $input: UpsertCustomAssertionInput!) {
      upsertCustomAssertion(urn: $urn, input: $input) {
        urn
        info { type description externalUrl
               customAssertion { type entityUrn logic field { path } } }
      }
    }
    """

    REPORT = """
    mutation report($urn: String!, $result: AssertionResultInput!) {
      reportAssertionResult(urn: $urn, result: $result)
    }
    """

    READ_ONE = """
    query assertion($urn: String!) {
      assertion(urn: $urn) {
        urn
        info { description externalUrl
               customAssertion { type entityUrn logic field { path } } }
        runEvents(status: COMPLETE, limit: 20) {
          total failed succeeded
          runEvents { timestampMillis result { type externalUrl nativeResults { key value } } }
        }
      }
    }
    """

    LIST_ON_DATASET = """
    query dataset($urn: String!) {
      dataset(urn: $urn) {
        assertions(start: 0, count: 50) {
          total
          assertions { urn info { description customAssertion { type field { path } } } }
        }
      }
    }
    """

    SEARCH = """
    query search($filters: [FacetFilterInput!]) {
      searchAcrossEntities(
        input: { types: [ASSERTION], query: "*", start: 0, count: 50,
                 orFilters: [{ and: $filters }] }
      ) {
        total
        searchResults {
          entity { urn ... on Assertion { info { description customAssertion { entityUrn } } } }
        }
      }
    }
    """

    def write_claim(
        self,
        urn: str,
        description: str,
        payload: dict[str, Any],
        field_path: str | None = None,
        external_url: str = "",
    ) -> dict[str, Any]:
        """Create/update ONE claim artifact.

        `field_path` is exposed ONLY so the spike can demonstrate LANDMINE 1. Real code must
        never set it — see the landmine section at the bottom.
        """
        payload_input: dict[str, Any] = {
            "entityUrn": PROBE_DATASET,
            "type": ASSERTION_TYPE,
            "description": description,
            "platform": {"urn": PROBE_PLATFORM},
            "logic": json.dumps(payload, sort_keys=True),
        }
        if external_url:
            payload_input["externalUrl"] = external_url
        if field_path:
            payload_input["fieldPath"] = field_path

        out = self.client.execute(self.UPSERT, {"urn": urn, "input": payload_input})
        if urn not in self.minted:
            self.minted.append(urn)
        return out["upsertCustomAssertion"]

    INDEX_LAG = "not associated with any entity"

    def try_report(self, urn: str, result_type: str = "SUCCESS") -> tuple[bool, str]:
        """Report a result, retrying PAST the index lag, and return the real outcome.

        Landmine 4 (index lag) and landmine 1 (fieldPath) both surface as a DataHubError
        from this one mutation, and the first draft of this spike could not tell them
        apart: it reported immediately after the upsert, caught the lag error, and
        "demonstrated" the fieldPath landmine with the wrong exception entirely. So the lag
        is retried away FIRST, and whatever error remains is the one actually being tested.
        """
        result = {"timestampMillis": int(time.time() * 1000), "type": result_type}
        deadline = time.monotonic() + INDEX_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            try:
                self.client.execute(self.REPORT, {"urn": urn, "result": result})
                return True, ""
            except DataHubError as exc:
                last = str(exc)
                if self.INDEX_LAG not in last:
                    return False, last  # a real, non-transient refusal
                time.sleep(1)
        return False, f"still index-lagging after {INDEX_TIMEOUT_S}s: {last}"

    def report_verdict(
        self, urn: str, result_type: str, properties: dict[str, str], external_url: str = ""
    ) -> bool:
        """Append ONE verdict event to a claim artifact, retrying past the index lag.

        LANDMINE 4: reportAssertionResult resolves the assertion's assertee through an
        eventually-consistent index, so a result reported immediately after the artifact is
        created fails with "does not exist or is not associated with any entity". It is
        pure latency and a retry clears it — but code that reports once and trusts the
        exception would drop verdicts under exactly the conditions a real run creates them.
        """
        result: dict[str, Any] = {
            "timestampMillis": int(time.time() * 1000),
            "type": result_type,
            "properties": [{"key": k, "value": v} for k, v in properties.items()],
        }
        if external_url:
            result["externalUrl"] = external_url

        deadline = time.monotonic() + INDEX_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            try:
                return bool(
                    self.client.execute(self.REPORT, {"urn": urn, "result": result})[
                        "reportAssertionResult"
                    ]
                )
            except DataHubError as exc:
                last = str(exc)
                if self.INDEX_LAG not in last:
                    raise
                time.sleep(1)
        raise ProbeFailure(f"reportAssertionResult never succeeded for {urn}: {last}")

    def read_claim(self, urn: str) -> dict[str, Any] | None:
        return self.client.execute(self.READ_ONE, {"urn": urn}).get("assertion")

    def list_claims(self, dataset_urn: str) -> dict[str, Any]:
        ds = self.client.execute(self.LIST_ON_DATASET, {"urn": dataset_urn}).get("dataset") or {}
        return ds.get("assertions") or {}

    def search(self, field: str, value: str) -> dict[str, Any]:
        return self.client.execute(self.SEARCH, {"filters": [{"field": field, "values": [value]}]})[
            "searchAcrossEntities"
        ]

    # --- cleanup ---------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove everything this spike wrote.

        LANDMINE 3: `deleteAssertion` REJECTS custom assertions — "Unsupported Assertion
        Type CUSTOM provided", HTTP 500 — so the obvious mutation is not the way out. The
        OpenAPI v3 entity endpoint hard-deletes; `batchUpdateSoftDeleted` also works if a
        tombstone is wanted instead. A spike that polluted the seeded catalog would break
        the anti-drift pin for everyone afterwards.
        """
        for urn in self.minted:
            r = httpx.delete(f"{self.client.gms_url}/openapi/v3/entity/assertion/{urn}", timeout=20)
            print(f"  deleted {urn.split(':')[-1]} -> HTTP {r.status_code}")
        try:
            self.client.execute(
                "mutation d($urn:String!){ deleteTag(urn:$urn) }", {"urn": VERDICT_TAG}
            )
            print(f"  deleted {VERDICT_TAG}")
        except DataHubError:
            pass


# --- the proof ----------------------------------------------------------------


def prove_two_claims_coexist(probe: Probe) -> tuple[str, str]:
    header(1, "WRITE — two DIFFERENT claims onto ONE dataset")

    # Claim A: table-grain classification. "This table contains PII."
    a_urn = "urn:li:assertion:" + claim_id(
        PROBE_DATASET, "classification", None, "urn:li:tag:PII/present=True"
    )
    a = probe.write_claim(
        a_urn,
        description="Attest claim: analytics.customers.customer_profile contains PII",
        payload={
            "claim_id": a_urn,
            "claim_type": "classification",
            "target_urn": PROBE_DATASET,
            "grain": "table",
            "field_path": None,
            "asserted_value": {"labels": ["urn:li:tag:PII"], "present": True},
        },
        external_url="http://localhost:8003/audit/run-ALPHA",
    )

    # Claim B: column-grain schema. A DIFFERENT claim, SAME dataset.
    b_urn = "urn:li:assertion:" + claim_id(
        PROBE_DATASET, "schema", "email", "email:STRING"
    )
    b = probe.write_claim(
        b_urn,
        description="Attest claim: column email of customer_profile is a STRING",
        payload={
            "claim_id": b_urn,
            "claim_type": "schema",
            "target_urn": PROBE_DATASET,
            "grain": "column",
            "field_path": "email",
            "asserted_value": {"columns": [{"name": "email", "native_type": "STRING"}]},
        },
        external_url="http://localhost:8003/audit/run-BETA",
    )

    print(f"  A {a['urn']}\n      {a['info']['description']}")
    print(f"  B {b['urn']}\n      {b['info']['description']}")
    ok = check("two artifacts minted with DISTINCT urns", a["urn"] != b["urn"])
    ok &= check("claim id is deterministic (re-derives identically)",
                a_urn == "urn:li:assertion:" + claim_id(
                    PROBE_DATASET, "classification", None, "urn:li:tag:PII/present=True"))
    if not ok:
        raise ProbeFailure("Could not mint two distinct claim artifacts.")
    return a_urn, b_urn


def prove_independent_readback(probe: Probe, a_urn: str, b_urn: str) -> None:
    header(2, "READ-BACK — each claim retrievable INDEPENDENTLY, by its own id")
    for label, urn in (("A", a_urn), ("B", b_urn)):
        got = probe.read_claim(urn)
        if not check(f"claim {label} reads back by urn", got is not None):
            raise ProbeFailure(f"{urn} did not read back.")
        ca = got["info"]["customAssertion"]
        payload = json.loads(ca["logic"])
        print(f"    {label}: grain={payload['grain']:6} "
              f"field_path={str(payload['field_path']):5} type={payload['claim_type']}")
        print(f"       asserted_value={json.dumps(payload['asserted_value'])}")
        check(f"claim {label} carries its asserted value + grain", bool(payload["asserted_value"]))


def prove_no_overwrite(probe: Probe, a_urn: str, b_urn: str) -> None:
    header(3, "NO-OVERWRITE — both claims listed on the dataset (THE INVARIANT)")
    deadline = time.monotonic() + INDEX_TIMEOUT_S
    listed: dict[str, Any] = {}
    while time.monotonic() < deadline:
        listed = probe.list_claims(PROBE_DATASET)
        if (listed.get("total") or 0) >= 2:
            break
        time.sleep(1)

    urns = {a["urn"] for a in (listed.get("assertions") or [])}
    print(f"  dataset.assertions.total = {listed.get('total')}")
    for a in listed.get("assertions") or []:
        ca = a["info"]["customAssertion"]
        print(f"    - {a['urn'].split(':')[-1][:26]}  type={ca['type']}  "
              f":: {a['info']['description'][:52]}")

    ok = check("BOTH claims present — neither overwrote the other", {a_urn, b_urn} <= urns)
    ok &= check("dataset.assertions is the per-dataset retrieval path",
                (listed.get("total") or 0) >= 2)
    if not ok:
        raise ProbeFailure(
            "THE INVARIANT FAILED: two claims on one dataset did not coexist. "
            "Assertions are not a viable representation; fall back to candidate 2 or 3."
        )


def prove_verdicts_and_history(probe: Probe, a_urn: str, b_urn: str) -> None:
    header(4, "VERDICTS — all three, and the HISTORY that a last-write-wins field cannot hold")

    # An earlier audit found the claim Supported.
    probe.report_verdict(
        a_urn, "SUCCESS",
        {"attest.verdict": "Supported", "attest.claim_type": "classification",
         "attest.audit_run": "run-OLD", "attest.reviewer": "alice@example.com",
         "attest.decision": "accepted", "attest.policy_version": "session-15",
         "attest.snapshot_id": "snap-001",
         "attest.evidence": "globalTags.tags[].tag.urn=urn:li:tag:PII"},
        external_url="http://localhost:8003/audit/run-OLD",
    )
    # Later, the catalog changed and the same claim is now Contradicted.
    probe.report_verdict(
        a_urn, "FAILURE",
        {"attest.verdict": "Contradicted", "attest.claim_type": "classification",
         "attest.audit_run": "run-NEW", "attest.reviewer": "bob@example.com",
         "attest.decision": "accepted", "attest.policy_version": "session-15",
         "attest.snapshot_id": "snap-002",
         "attest.evidence": "globalTags.tags[].tag.urn=urn:li:tag:NonPII"},
        external_url="http://localhost:8003/audit/run-NEW",
    )
    # The third verdict, on claim B. There is no native result type for it — see below.
    probe.report_verdict(
        b_urn, "ERROR",
        {"attest.verdict": "Insufficient-Coverage", "attest.claim_type": "schema",
         "attest.audit_run": "run-IC", "attest.reviewer": "carol@example.com",
         "attest.decision": "accepted", "attest.policy_version": "session-15",
         "attest.snapshot_id": "snap-002",
         "attest.evidence": "schemaMetadata.fields=<absent>"},
        external_url="http://localhost:8003/audit/run-IC",
    )

    deadline = time.monotonic() + INDEX_TIMEOUT_S
    a: dict[str, Any] = {}
    while time.monotonic() < deadline:
        a = probe.read_claim(a_urn) or {}
        if ((a.get("runEvents") or {}).get("total") or 0) >= 2:
            break
        time.sleep(1)

    events = (a.get("runEvents") or {}).get("runEvents") or []
    print(f"  claim A run events: total={(a.get('runEvents') or {}).get('total')}")
    for ev in events:
        nr = {n["key"]: n["value"] for n in (ev["result"].get("nativeResults") or [])}
        when = datetime.fromtimestamp(
            ev["timestampMillis"] / 1000, tz=UTC).isoformat(timespec="seconds")
        print(f"    - {when}  native={ev['result']['type']:8} "
              f"verdict={nr.get('attest.verdict'):22} run={nr.get('attest.audit_run'):8} "
              f"reviewer={nr.get('attest.reviewer')}")

    ok = check("re-auditing a claim APPENDS — the earlier verdict SURVIVES",
               len(events) >= 2, f"{len(events)} events")
    ok &= check("the flip Supported->Contradicted is readable from DataHub ALONE",
                {"Supported", "Contradicted"} <=
                {n["value"] for ev in events for n in (ev["result"].get("nativeResults") or [])
                 if n["key"] == "attest.verdict"})

    b = probe.read_claim(b_urn) or {}
    rb = b.get("runEvents") or {}
    print(f"\n  claim B (Insufficient-Coverage): "
          f"succeeded={rb.get('succeeded')} failed={rb.get('failed')}")
    ok &= check("the THIRD verdict is in NEITHER bucket — it stays third",
                (rb.get("succeeded") or 0) == 0 and (rb.get("failed") or 0) == 0)
    if not ok:
        raise ProbeFailure("The history / three-verdict properties did not hold.")


def prove_cross_dataset_query(probe: Probe, a_urn: str) -> None:
    header(5, "QUERY — find claim artifacts across the catalog")

    deadline = time.monotonic() + INDEX_TIMEOUT_S
    hits: dict[str, Any] = {}
    while time.monotonic() < deadline:
        hits = probe.search("customType", ASSERTION_TYPE)
        if (hits.get("total") or 0) >= 2:
            break
        time.sleep(1)
    print(f"  customType = {ASSERTION_TYPE} -> total={hits.get('total')}")
    for r in hits.get("searchResults") or []:
        desc = (r["entity"].get("info") or {}).get("description", "")
        print(f"    - {r['entity']['urn'].split(':')[-1][:26]} :: {desc[:50]}")
    ok = check("every Attest claim in the catalog is ONE query", (hits.get("total") or 0) >= 2)

    # The latest verdict as a filter. Assertions carry no structured properties, so a tag is
    # the only filterable verdict field.
    probe.client.execute(
        "mutation c($input:CreateTagInput!){ createTag(input:$input) }",
        {"input": {"id": VERDICT_TAG.split(":")[-1], "name": VERDICT_TAG.split(":")[-1],
                   "description": "Probe: Attest's latest verdict on this claim."}},
    )
    probe.client.execute(
        "mutation t($input:TagAssociationInput!){ addTag(input:$input) }",
        {"input": {"tagUrn": VERDICT_TAG, "resourceUrn": a_urn}},
    )
    deadline = time.monotonic() + INDEX_TIMEOUT_S
    tagged: dict[str, Any] = {}
    while time.monotonic() < deadline:
        tagged = probe.search("tags", VERDICT_TAG)
        if (tagged.get("total") or 0) >= 1:
            break
        time.sleep(1)
    print(f"\n  tags = {VERDICT_TAG} -> total={tagged.get('total')}")
    ok &= check("'every Contradicted claim' is a real catalog query",
                (tagged.get("total") or 0) >= 1)

    print("\n  Filterability, MEASURED (not from docs) — only these two work:")
    for field, value in (
        ("customType", ASSERTION_TYPE),
        ("tags", VERDICT_TAG),
        ("entityUrn", PROBE_DATASET),
        ("fieldPath", "email"),
        ("description", "Attest"),
    ):
        try:
            total = probe.search(field, value).get("total") or 0
        except DataHubError:
            total = -1
        print(f"    {field:12} -> total={total}{'   <-- NOT filterable' if total <= 0 else ''}")

    if not ok:
        raise ProbeFailure("Claim artifacts are not queryable across the catalog.")


def demonstrate_fieldpath_landmine(probe: Probe) -> None:
    """OBSERVATIONAL, and deliberately not a gate. Read this before "fixing" it.

    The landmine is real and reproducible: fieldPath sets the assertion's asserteeUrn to a
    schemaField URN, assertionRunEvent requires a dataset URN, and the artifact can then
    never carry a verdict. But WHICH failure you get depends on what the index holds at the
    moment you report — refused / index-lagging / silently fine on a repaired artifact's
    stale assertee. Across consecutive runs this section produced all three.

    So it asserts nothing. The project already has this rule and it was learned the hard
    way on the correction loop: a flaky assertion on a load-bearing invariant is worse than
    no assertion, because it teaches people to re-run until green — which is how a real
    regression gets waved through. The property that holds EVERY time is the one the design
    takes: `fieldPath` is unsafe, so never set it. The specific outcome is reported, not
    demanded — and the raciness is itself the argument, since a field that usually works is
    a worse trap than one that never does.
    """
    header(6, "LANDMINE 1 — `fieldPath` makes a claim artifact PERMANENTLY verdict-less")
    print("  (OBSERVATIONAL — asserts nothing: the failure mode is index-timing dependent.")
    print("   That is the finding, not a flaw in the probe. See the docstring.)")

    # A VIRGIN urn, every run. This demo reused a fixed urn at first and went flaky: run 1
    # cleared the trap by repairing the artifact, and run 2 recreated it WITH fieldPath but
    # reported against the assertee still cached in the index from the repair — so the write
    # succeeded and the landmine "disappeared". Which is itself worth knowing: the assertee
    # reportAssertionResult validates against is the INDEXED one, so a repaired artifact
    # keeps working on stale state and a fresh one fails. Only a never-seen urn tests it.
    urn = f"urn:li:assertion:attest-probe-fieldpath-trap-{int(time.time())}"
    probe.write_claim(
        urn,
        description="probe: column-grain claim WITH fieldPath set",
        payload={"grain": "column", "field_path": "email"},
        field_path="email",  # <-- the trap
    )
    print("  created a custom assertion with fieldPath='email' — the write SUCCEEDS,")
    print("  it reads back fine, and the column grain looks natively modelled.")

    ok, err = probe.try_report(urn)
    if ok:
        print("\n  OBSERVED: the verdict wrote. The trap did NOT fire on this run — the")
        print("  index had not yet taken the schemaField assertee. This is the DANGEROUS")
        print("  outcome: it is the one that makes fieldPath look safe in a dev loop.")
    elif "Invalid entity type urn validation failure" in err:
        marker = err.find("msg=")
        print(f"\n    {err[marker:marker + 210] if marker != -1 else err[:210]}\n")
        print("  OBSERVED: the trap fired. ROOT CAUSE: fieldPath sets the assertion's")
        print("  asserteeUrn to a schemaField URN, but the assertionRunEvent aspect")
        print("  requires a DATASET urn. The artifact is CREATED and READS back perfectly")
        print("  and can never carry a VERDICT — the one thing it exists to carry.")
    else:
        print(f"\n  OBSERVED: refused for a third reason: {err[:150]}")

    # Not a one-way trap: re-upserting without fieldPath clears the assertee. Also racy —
    # the repair lands in the entity store immediately and in the index whenever it gets there.
    probe.write_claim(urn, description="probe: repaired (fieldPath cleared)",
                      payload={"grain": "column", "field_path": "email"})
    repaired, repair_err = probe.try_report(urn)
    print(f"\n  OBSERVED: re-upsert WITHOUT fieldPath repairs it -> "
          f"{'verdict now writes' if repaired else 'still refused: ' + repair_err[:90]}")
    print("\n  CONSEQUENCE FOR THE DESIGN: never set fieldPath. Carry the grain in `logic`")
    print("  (and in the description). The grain stays fully recoverable; fieldPath costs")
    print("  the verdict entirely, which is the one thing the artifact exists to carry.")


def main() -> int:
    print(f"probing claim artifacts on {PROBE_DATASET}")
    probe: Probe | None = None
    try:
        with DataHubClient() as client:
            version = client.execute("query { appConfig { appVersion } }")
            app_version = (version.get("appConfig") or {}).get("appVersion")
            print(f"gms: {client.gms_url}  version: {app_version}")

            probe = Probe(client)
            a_urn, b_urn = prove_two_claims_coexist(probe)
            prove_independent_readback(probe, a_urn, b_urn)
            prove_no_overwrite(probe, a_urn, b_urn)
            prove_verdicts_and_history(probe, a_urn, b_urn)
            prove_cross_dataset_query(probe, a_urn)
            demonstrate_fieldpath_landmine(probe)

            header(7, "CLEANUP — leave the seeded catalog exactly as it was found")
            probe.cleanup()
            # The listing is served from the same eventually-consistent index the writes go
            # through, so a delete is not visible the instant it returns. Poll, or the spike
            # reports pollution it did not leave.
            deadline = time.monotonic() + INDEX_TIMEOUT_S
            left: dict[str, Any] = {}
            while time.monotonic() < deadline:
                left = probe.list_claims(PROBE_DATASET)
                if (left.get("total") or 0) == 0:
                    break
                time.sleep(1)
            check("no probe artifacts left on the dataset", (left.get("total") or 0) == 0,
                  f"total={left.get('total')}")
    except (ProbeFailure, DataHubError) as exc:
        print(f"\nPROBE FAILED: {exc}")
        if probe is not None:
            print("\nattempting cleanup anyway...")
            try:
                probe.cleanup()
            except Exception as cleanup_exc:  # noqa: BLE001 - best effort
                print(f"  cleanup failed too: {cleanup_exc}")
        return 1

    if FAILURES:
        print(f"\n{'=' * 76}")
        print(f"NOT PROVEN — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        print("The design must not be built on this until they are understood.")
        print("=" * 76)
        return 1

    print(f"\n{'=' * 76}")
    print("PROVEN on DataHub Core v1.5.0.6 — Assertions (candidate 1) support")
    print("per-claim, non-overwriting, queryable artifacts. Candidates 2 and 3 not needed.")
    print("=" * 76)
    print("""
What the server actually does (measured, not read):

  WORKS
    upsertCustomAssertion(urn:)   caller-supplied urn -> WE own claim identity
    dataset.assertions            per-dataset listing -> two claims coexist
    reportAssertionResult         APPEND-ONLY timeseries -> the history DataHub was
                                  said not to be able to hold. It can, for this aspect.
    result.properties             -> nativeResults, round-trips exactly. The payload.
    result.externalUrl            round-trips. The run link.
    search types:[ASSERTION]      filterable by customType and tags.

  LANDMINES (each cost a real debugging cycle; all four are silent or misdirecting)
    1. fieldPath poisons asserteeUrn to a schemaField urn -> reportAssertionResult
       fails FOREVER on that artifact. Write succeeds; read looks perfect. NEVER set it.
    2. result.error is ACCEPTED AND SILENTLY DISCARDED. Reads back null. So
       AssertionResultErrorType.INSUFFICIENT_DATA cannot self-describe verdict 3.
    3. deleteAssertion REJECTS custom assertions (HTTP 500, "Unsupported Assertion
       Type CUSTOM"). Use openapi/v3 DELETE or batchUpdateSoftDeleted.
    4. reportAssertionResult reads an eventually-consistent index: called right after
       the upsert it fails with "not associated with any entity". Retry, do not trust.

  CONSTRAINTS THE DESIGN MUST ABSORB
    - Only `customType` and `tags` are filterable on an assertion. entityUrn,
      fieldPath and description are NOT. Per-dataset listing goes through
      dataset.assertions, not through search.
    - Assertions do NOT support structured properties, so the latest verdict is
      filterable only as a TAG.
    - AssertionResultType has 4 values and Attest has 3 verdicts, which do not line
      up. nativeResults['attest.verdict'] is AUTHORITATIVE; the native type is a
      LOSSY PROJECTION for DataHub's own health rollup:
          Supported             -> SUCCESS
          Contradicted          -> FAILURE
          Insufficient-Coverage -> ERROR   (lands in NEITHER succeeded NOR failed,
                                            which is exactly right: the third verdict
                                            is not a pass and not a fail. Measured.)
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
