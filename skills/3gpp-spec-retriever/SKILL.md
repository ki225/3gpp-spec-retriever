---
name: 3gpp-spec-retriever
description: Retrieve version-pinned 3GPP evidence and trace related clauses when assessing a feature or implementation against a specified Release. Use the configured retriever MCP tools or CLI; do not treat retrieval alone as proof of compliance.
---

# Retrieve evidence for a 3GPP assessment

Use the baseline named by the user or project. A Release is not a unique document
version. Inspect `get_corpus_status` before searching: it lists pinned versions,
missing imports and unsupported content. If no suitable baseline exists, report
the missing input rather than silently choosing another Release.

Use `collect_evidence` for the feature question. Supply concrete subquestions
for its trigger, prerequisites, successful flow, rejection/error paths and
optional conditions when those aspects apply. Automatic query expansion is
limited to explicit delimiters and known acronyms; supply domain-specific
synonyms or English terminology yourself when useful.

Inspect the returned full clauses, ordered tables, annex type, reference edges,
`unresolved`, `pending`, omitted blocks and search truncation. Use `get_clause`
with `include_children=true` when the relevant parent delegates requirements to
subclauses. Use `list_clauses` with pagination to navigate document-only references.
`expand_references` accepts exact seed addresses and explicit dependency baselines
when a reference points to an older Release. It never authorizes changing source
versions or downloading missing documents without the surrounding task's scope.

For requests to find all definitions behind an issue, build an explicit feature
audit case. Identify architecture, Stage 2 procedures, Stage 3 API/resources,
datatypes, security/identity and relevant upstream/downstream protocols. Inspect
each required document's pinned version and hash, including dependency baselines.
Fetch missing public specifications when the task authorizes building that corpus;
record transformations of legacy files and retain both input and output hashes.

Use `scan_corpus` to scan the full supported corpus and follow every `next_offset`.
Literal terms alone are insufficient: add reviewed section subtrees where the
definition does not repeat the feature name. Use CLI `audit CASE --db CORPUS
--output NEW_FILE` to export every matching block, complete source clauses and
resolved reference-target evidence. Inspect required-document and section gaps.
Do not describe `scope_ready` as proof that applicability discovery is complete.

Write a definition and analysis, or a specific exclusion reason, for every candidate
clause. Review referenced targets and record remaining gaps. Keep raw evidence
separate from paraphrased analysis. Use CLI `review BUNDLE REVIEW_JSON` to verify
the analysis refers to the exact case and evidence bytes and to find missing
reviews. Its coverage result checks presence, not semantic truth or human approval.
Treat normative requirements, implementation inferences and issue allegations as
distinct. In particular, a suggested fix is not automatically a standard's exact
requirement; optional fields, cardinalities and similar-sounding definitions need
their original scope checked.

Keep dated references exact. Treat an unresolved or ambiguous reference, a missing
document, an unexpanded child, a budget omission or an uninterpreted figure as
an evidence gap when it matters to the requirement. A false `truncated` value
only means the configured retrieval budget was not hit; it does not prove all
applicable specifications were found.

For an implementation assessment, map each applicable normative requirement to
both its source evidence and implementation/test evidence. Distinguish mandatory,
conditional and optional provisions from informative explanations; account for
exceptions and applicability conditions. Prefer a compact table of requirement,
specification/version/clause, implementation/test evidence and finding. Report
findings as supported, contradicted or insufficient evidence, explaining remaining
gaps. Retrieval rank and ColBERT scores are relevance signals, not confidence in
compliance. Never infer that an absent search hit means no requirement exists.

If MCP is unavailable, use equivalent JSON CLI operations from the project:

```text
spec-retriever status --db CORPUS --baseline BASELINE
spec-retriever collect "FEATURE QUESTION" --db CORPUS --baseline BASELINE --subquery "ERROR CONDITION"
spec-retriever list-clauses --db CORPUS --baseline BASELINE --spec 29.571 --version 17.9.0
spec-retriever clause --db CORPUS --baseline BASELINE --spec 29.571 --version 17.9.0 --clause 5.2.4.1 --include-children
spec-retriever expand --db CORPUS --baseline BASELINE --seed 29.571@17.9.0:5.2.4.1
spec-retriever scan --db CORPUS --baseline BASELINE --term "FEATURE TERM" --offset 0 --limit 100
spec-retriever audit CASE_JSON --db CORPUS --output NEW_EVIDENCE_JSON
spec-retriever review EVIDENCE_JSON REVIEW_JSON
```

Resolve the actual corpus path and baseline from the project configuration or
user context. Do not fabricate a local path, installed tool or successful result.

## Freshness and inventory

When a managed corpus is configured, use `check-documents @PATH/current.json --db DB` before claiming freshness.
Compare versions only inside the requested Release. Inspect `missing_references`, local hashes and index state separately.
Use `--required-spec` for expected documents, `--include-references` for one-hop same-Release acquisition, or explicit `--series` for catalog comparison.
Use `sync-documents @PATH/current.json --db DB --directory DIRECTORY --model MODEL` to download and rebuild embeddings before publication.
Only use `--lexical-only` when explicitly permitted; `not_requested` is not successful embedding. Follow the returned baseline for MCP queries.
An unchanged managed generation skips embedding; `--force` rebuilds. Dated references retain their exact version gaps.
Read the [document update guide](../../README.md#5-check-and-update-documents) for command examples and failure behavior. These maintenance actions use CLI; MCP remains read-only.
