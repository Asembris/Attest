# Draft issue 1 — schema field data loss

**Repo:** `acryldata/mcp-server-datahub`
**Title:** `get_entities`/`list_schema_fields` drop schema-field `type` and return tag/term display names instead of URNs

---

### Summary

The schema fields returned by `get_entities` and `list_schema_fields` lose two pieces of
structured data that GMS returns and that the server's own GraphQL query is otherwise shaped
to carry:

1. **`type` is commented out** of the `entitySchemaFieldFields` fragment, so
   `SchemaFieldDataType` never reaches the response — even though the cleaning code still
   reads it.
2. **Field-level tags and glossary terms are flattened to display names**, so
   `urn:li:tag:PII` arrives as `"PII"` and `urn:li:glossaryTerm:CustomerIdentifier` arrives
   as `"Customer Identifier"`. The URN is dropped.

Table-level tags and terms **do** keep their URNs (`tags.tags[].tag.urn`), so a consumer sees
identifiers at one grain and display strings at the other, for the same concepts.

### Environment

- `mcp-server-datahub` 0.6.0 (via `uvx`)
- DataHub Core **v1.5.0.6**, self-hosted quickstart (`is_oss=True`, `is_cloud=False`)
- Python 3.12

### Reproduction

Call `get_entities` (or `list_schema_fields`) on any dataset with a tagged, glossary-termed
column:

```jsonc
// get_entities -> schemaMetadata.fields[0]
{
  "fieldPath": "customer_id",
  "nativeDataType": "VARCHAR(36)",
  "description": "Surrogate key.",
  "nullable": false,
  "tags": ["NonPII"],                     // display name; urn:li:tag:NonPII is gone
  "glossaryTerms": ["Customer Identifier"] // display name; urn:li:glossaryTerm:CustomerIdentifier is gone
}
// note: no "type"
```

The same GMS, same instant, over GraphQL:

```graphql
{
  dataset(urn: "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)") {
    schemaMetadata { fields { fieldPath type tags { tags { tag { urn } } } } }
  }
}
```

```jsonc
{ "fieldPath": "customer_id",
  "type": "STRING",
  "tags": { "tags": [ { "tag": { "urn": "urn:li:tag:NonPII" } } ] } }
```

### Root cause

**`type`** — `gql/entity_details.gql`:

```graphql
fragment entitySchemaFieldFields on SchemaField {
    fieldPath
    label
    jsonPath
    nullable
    description
    # type
    nativeDataType
    ...
}
```

It is commented out, but `graphql_helpers._clean_schema_fields` still reads it:

```python
# Add type if present (essential for SQL)
if field_type := f.get("type"):
    field_dict["type"] = field_type
```

so that branch is now dead code — which suggests the comment may have been a temporary
measure rather than an intentional removal. `SchemaField.type` is
`NON_NULL SchemaFieldDataType` in the GraphQL schema, so it is always available and never
null. The comment `# Add type if present (essential for SQL)` states the case for it.

**Tags/terms** — `graphql_helpers._clean_schema_fields`:

```python
if tags := f.get("tags"):
    if tag_list := tags.get("tags"):
        # Keep just tag names for context
        field_dict["tags"] = [
            t["tag"]["properties"]["name"]
            for t in tag_list
            if t.get("tag", {}).get("properties") and t["tag"]["properties"].get("name")
        ]
```

The fragment already fetches the full `globalTagsFields`, so the URN is present in the raw
response and discarded during cleaning. Note the filter also **drops a tag entirely** when it
has no `properties.name`, rather than falling back to the URN.

### Impact

For an LLM reading context, display names are fine — arguably better. For any consumer doing
**structured** work they are lossy in ways that do not degrade gracefully:

- **Display names are not identifiers.** They are not unique, they are mutable, and they are
  not resolvable. `"Customer Identifier"` cannot be looked up, joined, or compared against a
  URN a caller already holds.
- **The glossary hierarchy becomes unusable at column grain.** `parentNodes` is keyed by term
  URN, so a column's term cannot be resolved to the node it sits under. Any rule of the form
  "this term is filed under the PII node, therefore this column is PII" is unimplementable
  from the column-grain response — which is the main reason to read column-grain terms at all.
- **The two grains disagree.** Table-level terms keep URNs; column-level ones do not. A
  consumer must special-case identity by grain for the same concept.
- **It is silently lossy.** Nothing in the response indicates a URN existed.

We hit this building a governance tool that verifies claims against catalog metadata: a claim
naming `urn:li:tag:PII` on a column cannot be matched against a response that says `"PII"`,
and the column-level PII signal is lost. But nothing about the problem is specific to that use
case — it applies to any caller that needs to join, filter, or resolve against catalog
identifiers.

### Suggested fix

Both are cheap and neither changes what an LLM sees today, if the display name is retained:

1. **Uncomment `type`** in `entitySchemaFieldFields`. Its reader already exists.
2. **Include the URN alongside the name** for field tags/terms. Minimal change:

```python
field_dict["tags"] = [
    {"urn": t["tag"]["urn"], "name": (t["tag"].get("properties") or {}).get("name")}
    for t in tag_list if t.get("tag", {}).get("urn")
]
```

If the token cost of the object form is a concern, an alternative that keeps the response
flat is to emit the URN and let the consumer derive the name — or to gate the richer shape
behind an env var, as `DESCRIPTION_LENGTH_LIMIT` was for #88.

Keying off `urn` rather than `properties.name` would also stop tags with no name being
dropped.

Happy to open a PR for the `type` uncomment, and for whichever tag/term shape you prefer.
