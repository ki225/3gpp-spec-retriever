# 3GPP Spec Retriever

## Introduction

An MCP server and CLI that give agents traceable evidence from 3GPP specifications. It builds a local Release 17 corpus from official archives, pins document versions and hashes, and lets agents search clauses, read tables, and follow explicit cross-references. SQLite is the default search backend; Elasticsearch with BGE-M3 is optional.

```mermaid
flowchart LR
    A["Official 3GPP archives"] --> B["Version-pinned local corpus"]
    B --> C["Search and cross-reference index"]
    C --> D["MCP server / CLI"]
    D --> E["Agent evidence review"]
```

## Usage

Requires Python 3.11+ and Node.js 18+. Legacy `.doc` sources also require LibreOffice or Microsoft Word on Windows.

1. Install the project:

   ```bash
   python -m venv .venv
   .venv/bin/python -m pip install -e ".[mcp]"
   ```

   On Windows, use `.venv/Scripts/python.exe` in place of `.venv/bin/python`. Use that environment's Python for the remaining commands.

2. Create `%APPDATA%/3gpp-spec-retriever/config.yaml` on Windows or `~/.config/3gpp-spec-retriever/config.yaml` on Linux/macOS:

   ```yaml
   release: 17
   scope: all
   storage_root: /absolute/path/to/3gpp-data
   retrieval: sqlite
   ```

3. Build the corpus and start the MCP server:

   ```bash
   python -m spec_retriever build --dry-run
   python -m spec_retriever build
   python -m spec_retriever serve
   ```

   The build selects the latest `17.x.y` archive of every specification with a Release 17 version. An interrupted build resumes automatically. `current.json` is published only after all selected documents and their cross-references are indexed. If the build fails, inspect `pending.json` and the run's `report.json` under `storage_root`.

4. Point your MCP client at the virtual environment's Python executable:

   ```json
   {
     "mcpServers": {
       "3gpp-spec-retriever": {
         "command": "/absolute/path/to/.venv/bin/python",
         "args": ["-m", "spec_retriever", "serve"]
       }
     }
   }
   ```

   On Windows, use `.venv/Scripts/python.exe`. Pass `baseline: "current"` to the MCP tools. The [agent skill](skills/3gpp-spec-retriever/SKILL.md) describes the evidence workflow.

For CLI retrieval, run `python -m spec_retriever search "PFCP Session Modification" --db /absolute/path/to/3gpp-data/corpus.sqlite3 --baseline @/absolute/path/to/3gpp-data/current.json`. Use `python -m spec_retriever --help` for clause lookup, corpus status, reference tracing, document checks and updates, and import repair. Results report provenance, incomplete coverage, and pagination; an agent must still assess which clauses apply.
