# Version-pinned cross-reference retrieval

The retriever stores explicit specification references as directed edges between
versioned clauses. An edge records its source clause, original text location,
reference kind, exact resolution result, and target clause when resolvable.
Document-only and ambiguous references remain visible without a fabricated
clause target. Repeated printed clause IDs in one document are unresolved until
the intended occurrence can be established.

The SQLite index is built after all documents in a baseline have been ingested
and before an official build or document update is published. Each document is
committed separately; an interrupted build resumes from committed documents.
Changing the extraction version or re-ingesting documents invalidates the index.
The MCP server never builds it during a query.

`get_references` and the CLI `references` command page through outgoing and
incoming edges of an exact `baseline/spec/version/clause` address. When the
index is complete, `collect_evidence` follows forward references and includes
up to four incoming citations per search seed before broader context. It keeps
depth, node, edge, and character budgets. An incomplete index causes a live
forward-parser fallback, with the missing reverse coverage reported as a gap.

This index represents explicit parsed citations, not all semantically applicable
requirements. Source parser warnings, unresolved references, and search or
expansion limits remain visible to the caller. An agent must review clause text
and any remaining gaps before a compliance conclusion.
