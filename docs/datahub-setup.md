# DataHub setup

Attest treats DataHub's catalog as ground truth. This document records which server
version that ground truth sits on, why it's pinned there, and the incompatibility that
made us build our own seed data — so nobody repeats the investigation.

## The pinned version

**DataHub Core v1.5.0.6**, via Docker quickstart. GMS on `:8080`, UI on `:9002`.
Metadata auth is disabled locally, so no token is needed; `.datahubenv` points at
`http://localhost:8080` with an empty token.

Verify what's actually running — don't trust the CLI's exit code (see landmines below):

```powershell
docker ps
(Invoke-RestMethod http://localhost:8080/config).versions.'acryldata/datahub'.version   # -> v1.5.0.6
```

## Why it's pinned

**For a reproducible base, not as a fallback from something better.**

This distinction matters. v1.5.0.6 is not a downgrade we settled for — it supports every
aspect Attest needs, verified directly against the running server:

| Aspect | Used for |
| --- | --- |
| `schemaMetadata` | schema claims; column-level tags and terms |
| `ownership` | ownership claims |
| `globalTags` | classification claims |
| `glossaryTerms` | classification claims |
| `datasetProperties.lastModified` | freshness claims |
| `structuredProperties` | writing Attest's verdicts back |

The alternative was master-head (which reports `v1.6.0rc1`). We pinned instead, because a
benchmark's ground truth cannot sit on a moving branch: if the server changes underneath
it, a verdict regression becomes indistinguishable from a server change. A tagged release
is the only base where "the catalog says X" means the same thing next month.

The seed catalog ingests against v1.5.0.6 with **0 failures and 0 warnings**
(64 records), and `spikes/datahub_probe.py` proves the full read/write round-trip.

## The `dataQualityCheck` incompatibility

Loading DataHub's showcase-ecommerce sample datapack against Core fails with ~2000 errors
of the form:

```
Failed to find entity with name dataQualityCheck in EntityRegistry
```

`dataQualityCheck` is a **DataHub Cloud** entity type. It does not exist in Core's
EntityRegistry at any version, so no choice of quickstart version fixes this. The failures
cascade: they take down the schemas, owners, tags, and glossary links that follow them in
the datapack, which is why the symptom looks like a broadly broken catalog rather than one
missing entity type.

**We do not emit `dataQualityCheck`, and we do not fake it as custom properties.** Attest's
four claim types — freshness, ownership, classification, schema — never touch data-quality
entities, so the entire error class disappears by construction. Faking the shape would mean
building verification logic against an invented structure that corresponds to nothing real
in DataHub; that collapses the moment it meets a real catalog.

This is why `seed/generate_seed.py` exists. It's not a workaround — Attest verifies claims
against *known* ground truth, so its benchmark needs entities where we control exactly
what's true. See the emitter's docstring for the verdict-bucket design.

> Scope note: the ~2000-error datapack run predates this repo and was not reproduced here.
> What *is* verified is the half that matters: v1.5.0.6 supports every aspect Attest needs,
> and our own seed data ingests against it cleanly. The cascade explanation above is the
> best reading of the original errors, not something this session re-ran.

## Rebuilding the stack from scratch

```powershell
datahub docker nuke                  # destroys containers AND volumes — catalog is gone
datahub docker quickstart            # see landmines before trusting this
python seed\generate_seed.py
datahub ingest -c ./seed/recipe.yml  # expect failures: [], 64 records
python spikes\datahub_probe.py       # expect ALL FOUR OPERATIONS PASSED
```

## Landmines

Each of these cost real time. They're worked around in code; this is why the code looks
the way it does.

- **`quickstart` can't fetch its compose file** behind a TLS-inspecting network. Python's
  certifi bundle lacks the root CA, so it dies with `CERTIFICATE_VERIFY_FAILED` — and
  leaves a **zero-byte** `docker-compose.yml` cached in `~/.datahub/quickstart/`, which
  then poisons the next run. Fetch it with PowerShell (which uses the Windows cert store)
  and pass it explicitly:

  ```powershell
  Invoke-WebRequest -UseBasicParsing `
    -Uri "https://raw.githubusercontent.com/datahub-project/datahub/v1.5.0.6/docker/quickstart/docker-compose.quickstart-profile.yml" `
    -OutFile "$env:USERPROFILE\.datahub\quickstart\docker-compose.yml"
  datahub docker quickstart --no-pull-images `
    --quickstart-compose-file "$env:USERPROFILE\.datahub\quickstart\docker-compose.yml"
  ```

- **`quickstart` exits non-zero even when it succeeds** on a cp1252 console: it boots the
  whole stack, then crashes printing its `✔` success checkmark (`UnicodeEncodeError`).
  Check `docker ps`, not `$LASTEXITCODE`.

- **Absolute Windows paths break the DataHub CLI.** Its filesystem registry parses `D:\...`
  as a URI and reads `d:` as a scheme: `Did not find a registered class for d`. This hits
  recipe paths *and* `write_metadata_file`. Use relative `./` paths.

- **Never write YAML with PowerShell's `Out-File`.** It emits a UTF-8 BOM that the YAML
  parser chokes on. `seed/recipe.yml` is written from Python with an explicit no-BOM
  encoding, and is regenerated rather than hand-edited.

- **Structured property values are objects, not scalars.** `upsertStructuredProperties`
  takes `values: [{stringValue: "..."}]`. A bare string fails with `Expected type 'Map'`.

- **The search index is eventually consistent, and lags under load.** A structured property
  written to an entity reads back immediately via `dataset(urn:)`, but becomes *searchable*
  anywhere from ~2s to >10s later — the slow case observed right after a seed ingest, when
  the index is busy. Don't read a `total: 0` from search as a failed write: check the entity
  directly first. `spikes/datahub_probe.py` polls for up to 60s and reports the round-trip
  and the searchability separately, because they are separate claims.

- **DataHub rejects multi-`__type` introspection queries** as `BadFaithIntrospection`.
  Introspect one type per request.
