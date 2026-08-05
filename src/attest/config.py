"""Configuration for Attest.

Model selection is per-step and never hardcoded. Each pipeline step resolves its
model through `settings.model_for(step)`, which falls back to `model_default`
when that step has no explicit override. This exists so a single step (most
likely semantic entailment) can be moved to a stronger model without touching
code or dragging the cheap steps up with it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to the repo, not to the working directory. A bare env_file=".env" is resolved
# relative to CWD, so `python somewhere/else/script.py` silently loaded no key at all and
# failed as "missing credentials" — which reads like an unset key rather than a wrong CWD.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Step(StrEnum):
    """Steps that make an LLM call. Each resolves its own model."""

    CLAIM_EXTRACTION = "claim_extraction"
    EVIDENCE_SELECTION = "evidence_selection"
    ENTAILMENT = "entailment"
    VERDICT = "verdict"
    # NOT a pipeline step. The benchmark's independent second labeler (benchmark/labeler.py),
    # which exists to keep GPT from grading GPT's homework — see model_calibration. It never
    # runs inside an audit, it cannot reach a verdict, and it is the ONE place a second model
    # family is legitimate. It is a Step so that it resolves its model through the same
    # config path as everything else, rather than hardcoding one in a script.
    CALIBRATION = "calibration"


class Settings(BaseSettings):
    # protected_namespaces=() so our `model_*` fields don't collide with
    # pydantic's reserved `model_` prefix.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    datahub_gms_url: str = Field(default="http://localhost:8080", alias="DATAHUB_GMS_URL")
    datahub_token: str = Field(default="", alias="DATAHUB_TOKEN")

    # The DataHub *UI* origin, distinct from the GMS URL above. GMS (:8080) is the metadata
    # API Attest reads and writes; the UI (:9002) is where a human clicks through to a
    # dataset. The frontend deep-links to it, but the built SPA has no runtime env — so the
    # backend hands it this value over /health rather than the frontend hardcoding :9002,
    # which is the one host that would silently break behind a real URL. Local default;
    # override for a deployed instance where the UI is not on localhost:9002.
    datahub_ui_url: str = Field(default="http://localhost:9002", alias="DATAHUB_UI_URL")

    # Repair a TLS failure by falling back to the OS certificate store (see llm.py).
    # Only ever attempted AFTER certifi's bundle has actually failed, so it costs a
    # normal network nothing. Set false to keep certifi as the only trust source.
    ssl_os_truststore: bool = Field(default=True, alias="ATTEST_SSL_TRUSTSTORE")

    # Attest's own audit history: every run, its receipts, its evidence, and every human
    # decision. Since Session 15 DataHub DOES hold a per-claim verdict history (the claim
    # artifact's append-only run events); what it cannot hold is the RUN-level trail — the
    # receipts, the decision log, who was shown what — and the dataset-level structured
    # property (the glance badge) is still last-write-wins. See store.py.
    store_path: str = Field(default="attest.db", alias="ATTEST_STORE_PATH")

    # LangGraph's checkpoints: the PAUSED GRAPH of a run parked at the human checkpoint.
    # A separate file from the audit history on purpose. LangGraph owns these tables and
    # their shape moves with its release cadence, not ours; the audit history is Attest's
    # own schema and nothing outside store.py may alter it. Sharing one file would put a
    # dependency's migrations in the same blast radius as the evidence trail.
    checkpoint_path: str = Field(
        default="attest-checkpoints.db", alias="ATTEST_CHECKPOINT_PATH"
    )

    # The port the API binds to. Pinned explicitly rather than left to a framework
    # default: DataHub already owns 8080 (GMS) and 9002 (UI) on the same machine, and a
    # collision surfaces as an audit that cannot reach the catalog, which reads like a
    # DataHub outage rather than a port clash.
    api_port: int = Field(default=8003, alias="ATTEST_API_PORT")

    # --- catalog DISCOVERY, over the DataHub MCP Server (§22) --------------------------
    #
    # Discovery answers "which dataset did you mean?" for a HUMAN. It is not the catalog
    # read: verdicts come from GraphQL, and §12 measured why. The only value that crosses
    # from here into a run is a URN a person selected, which must then appear verbatim in
    # the agent's text like any other. See src/attest/discovery/.
    discovery_enabled: bool = Field(default=True, alias="ATTEST_DISCOVERY_ENABLED")

    # The MCP server, launched exactly as the Session 17 probe launched it — the recipe that
    # is already measured against Core v1.5.0.6. `--native-tls` is uv's system-CA opt-in,
    # this network's trap one runtime further out than truststore and NODE_USE_SYSTEM_CA.
    # Overridable so a deployment can point at an already-installed server instead of paying
    # uvx's resolve; parsed with shlex, so it is one env var rather than an argv encoding.
    #
    # THE VERSION IS PINNED (`==0.6.0`), and it is the same argument as the Core pin and the
    # `langgraph-checkpoint-sqlite` pin: **every claim Attest makes about this server is
    # version-bounded.** The parity finding is 130 mismatches on 0.6.0 (docs/mcp-evaluation.md),
    # `just spike-mcp` exits non-zero BY DESIGN and is the tripwire that says the day that
    # finding expires, and PR #182 proposes a fix to this very codebase. Unpinned, `uvx`
    # resolves whatever is newest at first use — so a judge running the demo next week could
    # get a server none of those receipts describe, and if our own upstream fix lands, the
    # tripwire and `just discover` start behaving differently than documented, SILENTLY. A
    # tripwire wired to a moving target is a green light wired to nothing.
    #
    # Pinning is a FREEZE, not a downgrade: 0.6.0 is the current latest on PyPI (checked
    # 2026-08-05), so this changes nothing today and holds it that way. Bump it deliberately,
    # with `just spike-mcp` and `just discover` re-run and the measured figures re-taken, or
    # not at all.
    mcp_command: str = Field(
        default=(
            "uvx --native-tls --from mcp-server-datahub==0.6.0 mcp-server-datahub "
            "--transport stdio"
        ),
        alias="ATTEST_MCP_COMMAND",
    )
    # MEASURED on Core v1.5.0.6: spawn + initialize is 3.33s warm, and a search is
    # 125-344ms. The budgets are ~10x each, so a timeout means something is wrong rather
    # than that the server was busy — the same reasoning as llm.READ_TIMEOUT_SECONDS.
    mcp_startup_timeout_seconds: float = Field(
        default=30.0, alias="ATTEST_MCP_STARTUP_TIMEOUT"
    )
    mcp_call_timeout_seconds: float = Field(default=10.0, alias="ATTEST_MCP_CALL_TIMEOUT")

    # --- the benchmark's second labeler, and the only legitimate second family ---------
    #
    # CLAUDE.md has said since Session 0 that a second provider has exactly one honest use:
    # cross-model-family calibration of the benchmark labels, so that GPT is not grading
    # GPT's homework. This is that, and it is scoped to it. Nemotron is a LLAMA-family
    # model, which is the point — a different provider serving similar weights would break
    # nothing. It never touches the verdict path; the pipeline default stays gpt-4o-mini.
    nvidia_api_key: str = Field(default="", alias="NVIDIA_API_KEY")
    nvidia_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1", alias="NVIDIA_BASE_URL"
    )
    model_calibration: str = Field(
        default="nvidia/llama-3.3-nemotron-super-49b-v1.5",
        alias="ATTEST_MODEL_CALIBRATION",
    )

    model_default: str = Field(default="gpt-4o-mini", alias="ATTEST_MODEL_DEFAULT")
    model_claim_extraction: str | None = Field(
        default=None, alias="ATTEST_MODEL_CLAIM_EXTRACTION"
    )
    model_evidence_selection: str | None = Field(
        default=None, alias="ATTEST_MODEL_EVIDENCE_SELECTION"
    )
    model_entailment: str | None = Field(default=None, alias="ATTEST_MODEL_ENTAILMENT")
    model_verdict: str | None = Field(default=None, alias="ATTEST_MODEL_VERDICT")

    def model_for(self, step: Step) -> str:
        """Resolve the model for a step, falling back to the default."""
        override = getattr(self, f"model_{step.value}", None)
        return override or self.model_default


settings = Settings()
