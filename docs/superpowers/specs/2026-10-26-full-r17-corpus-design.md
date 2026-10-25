# Full Release 17 corpus build

Status: proposed design; implementation pending.

## Goal and scope

Build a local corpus from the official 3GPP specification archive for **every
specification with an available `17.x.y` archive version**, across all archive
series. Both Technical Specifications and Technical Reports are in scope. For
each specification, select its highest available `17.x.y` version. Do not
silently substitute an older or newer Release when no Release 17 version exists.
This defines archive-version coverage, not a guarantee that every document
applicable to a Release 17 implementation has a `17.x.y` version.

Discovery must enumerate series and specification directories from the official
archive root, rather than a static list. The inventory records every discovered
series/specification, selected version and URL, and exclusions with reasons.
An inventory can be inspected before downloading. The known archive parsing
issue where directory links omit their trailing slash must be corrected.

## Configuration and interface

A user-level YAML configuration selects the desired Release and storage path.
The default search path is platform-specific (`%APPDATA%` on Windows and
`~/.config` on Unix); `--config` can select another file. The minimal
configuration is:

```yaml
release: 17
scope: all
storage_root: /absolute/path/to/3gpp-data
retrieval: sqlite
```

`scope: all` means every official archive series with a Release 17 version.
The user may later choose explicit series without changing the Release filter;
named RAN/Core profiles are optional convenience and must expose their resolved
series. The builder and MCP server read the same configuration. Command-line
paths may override it for a single invocation. `build --dry-run` outputs the
selected inventory and estimated document count; `build` performs acquisition
and indexing. The MCP server remains a read-only retrieval interface and opens
the active database from the configured storage root.

## Build flow

1. Discover official series and specification directories, select the latest
   `17.x.y` archive for each, and save a timestamped inventory snapshot.
2. Download ZIPs to a staging generation with bounded concurrency and retries;
   validate archive paths and record URL, archive hash, source DOCX hash, and
   any legacy `.doc` conversion provenance. Reuse verified downloads on resume.
3. Parse and index all selected documents using the existing corpus schema.
   SQLite full-text search is the default; optional hybrid embeddings run only
   when explicitly configured. Parsing errors are recorded per document.
4. Validate that every selected document has the expected version, hash and
   successful ingestion. Write a machine-readable gap report. Only after this
   validation succeeds, atomically switch the active generation pointer.

An interrupted run resumes against its saved inventory and staging generation.
Refreshing the official inventory is an explicit new build, so updates cannot
change the selected set midway through a run. A failed build leaves the prior
active generation available. It must not report `complete` when any selected
document failed download, conversion, parsing or indexing. Staging progress and
failure reasons remain inspectable, and failed documents can be retried after a
parser fix.

## MCP and verification

The existing FastMCP stdio server continues to expose search, clause reading,
corpus status, reference expansion, evidence collection and corpus scanning.
`get_corpus_status` must distinguish the configured inventory scope, selected
count, indexed count, failures and active-generation completeness. Query results
retain source specification, exact version and clause location; unresolved
cross-references outside the built corpus remain visible.

Verification covers official directory discovery (including slashless links),
Release 17 selection and exclusions, interrupted-run resume, corrupt/failed
documents, atomic publication, and a real MCP client handshake followed by
tool discovery and a successful query against a built corpus. A small official
subset can validate the full pipeline locally; a claim of **complete all-series
R17 coverage** requires the actual all-series inventory and every selected
document to pass, with counts and failures published as evidence.
