"""Atomic baseline imports and exact evidence retrieval using SQLite/FTS5."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from spec_retriever.spec_store.docx_parser import parse_docx
from spec_retriever.spec_store.manifest import Manifest, sha256_file
from spec_retriever.spec_store.models import make_chunks


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id(*parts: str) -> str:
    return hashlib.sha256(_json(parts).encode()).hexdigest()


class Corpus:
    def __init__(self, database: str | Path):
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS baselines (
                name TEXT PRIMARY KEY, release INTEGER NOT NULL,
                digest TEXT NOT NULL, manifest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, baseline TEXT NOT NULL REFERENCES baselines(name),
                spec TEXT NOT NULL, version TEXT NOT NULL, source_url TEXT NOT NULL,
                filename TEXT NOT NULL, sha256 TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, warnings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clauses (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                clause_id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
                metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, clause_pk TEXT NOT NULL REFERENCES clauses(id),
                payload TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text, chunk_id UNINDEXED, tokenize='unicode61'
            );
            CREATE INDEX IF NOT EXISTS documents_baseline ON documents(baseline, spec, version);
            CREATE INDEX IF NOT EXISTS clauses_document ON clauses(document_id, clause_id);
            CREATE INDEX IF NOT EXISTS chunks_clause ON chunks(clause_pk);
            CREATE TABLE IF NOT EXISTS reference_edges (
                id INTEGER PRIMARY KEY, source_clause_pk TEXT NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
                source_baseline TEXT NOT NULL, target_clause_pk TEXT,
                kind TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                reference_json TEXT NOT NULL, resolution_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reference_edges_source ON reference_edges(source_clause_pk);
            CREATE INDEX IF NOT EXISTS reference_edges_target ON reference_edges(target_clause_pk);
            CREATE TABLE IF NOT EXISTS reference_index_documents (
                document_id TEXT PRIMARY KEY, baseline TEXT NOT NULL, sha256 TEXT NOT NULL,
                extractor_version INTEGER NOT NULL, edge_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reference_index_state (
                baseline TEXT PRIMARY KEY, manifest_digest TEXT NOT NULL,
                extractor_version INTEGER NOT NULL, complete INTEGER NOT NULL
            );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(clauses)")}
        if "metadata" not in columns:
            self.connection.execute("ALTER TABLE clauses ADD COLUMN metadata TEXT")
        if self.connection.execute("SELECT 1 FROM clauses WHERE metadata IS NULL LIMIT 1").fetchone():
            with self.connection:
                self.connection.execute("""UPDATE clauses SET metadata=json_object(
                    'title',json_extract(payload,'$.title'),
                    'title_path',json_extract(payload,'$.title_path'),
                    'annex_type',json_extract(payload,'$.annex_type')) WHERE metadata IS NULL""")

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def ingest(self, manifest: Manifest) -> dict:
        existing = self.connection.execute("SELECT digest FROM baselines WHERE name=?", (manifest.baseline,)).fetchone()
        if existing and existing["digest"] != manifest.digest:
            raise ValueError("baseline already pins different manifest contents; choose a new baseline name")
        parsed = []
        for item in manifest.documents:
            if item.sha256 is None:
                raise ValueError("ingest requires a locked manifest")
            path = manifest.document_path(item)
            status, error, result = "indexed", None, None
            try:
                if sha256_file(path) != item.sha256:
                    status, error = "checksum_mismatch", "source bytes do not match lockfile"
                else:
                    result = parse_docx(path, item)
            except FileNotFoundError:
                status, error = "missing", "source DOCX does not exist"
            except Exception as exception:
                # Keep the failure visible and continue other manifest entries.
                status, error = "parse_failed", f"{type(exception).__name__}: {exception}"
            parsed.append((item, status, error, result))

        with self.connection:
            self.connection.execute("DELETE FROM reference_index_state WHERE baseline=?", (manifest.baseline,))
            self.connection.execute("DELETE FROM reference_index_documents WHERE baseline=?", (manifest.baseline,))
            self.connection.execute("INSERT OR IGNORE INTO baselines VALUES (?,?,?,?)",
                                    (manifest.baseline, manifest.release, manifest.digest, _json(manifest.payload())))
            # Remove previous evidence even for a document that now fails import.
            self.connection.execute("""DELETE FROM chunks_fts WHERE chunk_id IN (
                SELECT k.id FROM chunks k JOIN clauses c ON c.id=k.clause_pk
                JOIN documents d ON d.id=c.document_id WHERE d.baseline=?)""", (manifest.baseline,))
            self.connection.execute("""DELETE FROM chunks WHERE clause_pk IN (
                SELECT c.id FROM clauses c JOIN documents d ON d.id=c.document_id WHERE d.baseline=?)""", (manifest.baseline,))
            self.connection.execute("DELETE FROM clauses WHERE document_id IN (SELECT id FROM documents WHERE baseline=?)", (manifest.baseline,))
            self.connection.execute("DELETE FROM documents WHERE baseline=?", (manifest.baseline,))
            for item, status, error, result in parsed:
                document_id = _id(manifest.baseline, item.spec, item.version)
                warnings = result.warnings if result else []
                self.connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    document_id, manifest.baseline, item.spec, item.version, item.source_url,
                    Path(item.path).name, item.sha256, status, error, _json(warnings)))
                if result is None:
                    continue
                for clause in result.clauses:
                    clause_pk = _id(document_id, clause.clause_id)
                    metadata = {name: getattr(clause, name) for name in ("title", "title_path", "annex_type")}
                    self.connection.execute("INSERT INTO clauses VALUES (?,?,?,?,?,?)", (
                        clause_pk, document_id, clause.clause_id, clause.ordinal, _json(asdict(clause)), _json(metadata)))
                    for index, chunk in enumerate(make_chunks(clause)):
                        chunk_id = _id(clause_pk, str(index))
                        self.connection.execute("INSERT INTO chunks VALUES (?,?,?)", (chunk_id, clause_pk, _json(chunk)))
                        self.connection.execute("INSERT INTO chunks_fts VALUES (?,?)", (chunk["search_text"], chunk_id))
        return self.status(manifest.baseline)

    def ingest_resumable(self, manifest: Manifest) -> dict:
        """Commit documents one at a time so a large build can resume safely."""
        existing = self.connection.execute("SELECT digest FROM baselines WHERE name=?", (manifest.baseline,)).fetchone()
        if existing and existing["digest"] != manifest.digest:
            raise ValueError("baseline already pins different manifest contents; choose a new baseline name")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO baselines VALUES (?,?,?,?)",
                                    (manifest.baseline, manifest.release, manifest.digest, _json(manifest.payload())))
        for item in manifest.documents:
            if item.sha256 is None:
                raise ValueError("ingest requires a locked manifest")
            document_id = _id(manifest.baseline, item.spec, item.version)
            path = manifest.document_path(item)
            status, error, result = "indexed", None, None
            try:
                if sha256_file(path) != item.sha256:
                    status, error = "checksum_mismatch", "source bytes do not match lockfile"
                else:
                    previous = self.connection.execute("SELECT status,sha256 FROM documents WHERE id=?",
                                                       (document_id,)).fetchone()
                    if previous and previous["status"] == "indexed" and previous["sha256"] == item.sha256:
                        count = self.connection.execute("SELECT count(*) FROM clauses WHERE document_id=?",
                                                        (document_id,)).fetchone()[0]
                        if count:
                            continue
                    result = parse_docx(path, item)
                    if not result.clauses:
                        status, error, result = "parse_failed", "parser produced no clauses", None
                    if result is not None:
                        metadata_path = path.with_name("source.json")
                        if metadata_path.is_file():
                            source = json.loads(metadata_path.read_text(encoding="utf-8"))
                            members = source.get("archive_members", [])
                            for clause in result.clauses:
                                for block in clause.blocks:
                                    location = block["location"]
                                    index = location["body_index"]
                                    matches = [part for part in members
                                               if part["body_start"] <= index < part["body_end"]]
                                    if members and len(matches) != 1:
                                        raise ValueError(f"multi-part source location is ambiguous at body index {index}")
                                    if matches:
                                        part = matches[0]
                                        location["archive_member"] = part["name"]
                                        location["archive_member_sha256"] = part["sha256"]
                                        location["member_body_index"] = index - part["body_start"]
            except FileNotFoundError:
                status, error = "missing", "source DOCX does not exist"
            except Exception as exception:
                status, error = "parse_failed", f"{type(exception).__name__}: {exception}"
            with self.connection:
                self.connection.execute("DELETE FROM reference_index_state WHERE baseline=?", (manifest.baseline,))
                self.connection.execute("DELETE FROM reference_index_documents WHERE document_id=?", (document_id,))
                self.connection.execute("""DELETE FROM chunks_fts WHERE chunk_id IN (
                    SELECT k.id FROM chunks k JOIN clauses c ON c.id=k.clause_pk
                    WHERE c.document_id=?)""", (document_id,))
                self.connection.execute("DELETE FROM chunks WHERE clause_pk IN (SELECT id FROM clauses WHERE document_id=?)",
                                        (document_id,))
                self.connection.execute("DELETE FROM clauses WHERE document_id=?", (document_id,))
                self.connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
                warnings = result.warnings if result else []
                self.connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    document_id, manifest.baseline, item.spec, item.version, item.source_url,
                    Path(item.path).name, item.sha256, status, error, _json(warnings)))
                if result is None:
                    continue
                for clause in result.clauses:
                    clause_pk = _id(document_id, clause.clause_id)
                    metadata = {name: getattr(clause, name) for name in ("title", "title_path", "annex_type")}
                    self.connection.execute("INSERT INTO clauses VALUES (?,?,?,?,?,?)", (
                        clause_pk, document_id, clause.clause_id, clause.ordinal, _json(asdict(clause)), _json(metadata)))
                    for index, chunk in enumerate(make_chunks(clause)):
                        chunk_id = _id(clause_pk, str(index))
                        self.connection.execute("INSERT INTO chunks VALUES (?,?,?)", (chunk_id, clause_pk, _json(chunk)))
                        self.connection.execute("INSERT INTO chunks_fts VALUES (?,?)", (chunk["search_text"], chunk_id))
        return self.status(manifest.baseline)

    def status(self, baseline: str) -> dict:
        row = self.connection.execute("SELECT * FROM baselines WHERE name=?", (baseline,)).fetchone()
        if row is None:
            raise ValueError(f"unknown baseline: {baseline}")
        documents = []
        for document in self.connection.execute("SELECT * FROM documents WHERE baseline=? ORDER BY spec", (baseline,)):
            item = dict(document)
            item["warnings"] = json.loads(item["warnings"])
            del item["id"]
            item["clause_count"] = self.connection.execute("SELECT count(*) FROM clauses WHERE document_id=?", (document["id"],)).fetchone()[0]
            documents.append(item)
        expected_count = len(json.loads(row["manifest"])["documents"])
        ready = (bool(documents) and len(documents) == expected_count
                 and all(item["status"] == "indexed" for item in documents))
        return {
            "baseline": baseline, "release": row["release"], "manifest_sha256": row["digest"],
            "ingestion_complete": ready,
            "has_parser_limitations": any(item["warnings"] for item in documents),
            "applicable_spec_coverage": "not_assessed",
            "documents": documents,
        }

    def _evidence(self, row: sqlite3.Row) -> dict:
        clause = json.loads(row["clause_payload"])
        chunk = json.loads(row["chunk_payload"])
        return {
            "chunk_id": row["chunk_id"], "baseline": row["baseline"],
            "spec": row["spec"], "version": row["version"], "clause": row["clause_id"],
            "title": clause["title"], "title_path": clause["title_path"],
            "annex_type": clause["annex_type"], "source_url": row["source_url"],
            "filename": row["filename"], "sha256": row["sha256"], **chunk,
        }

    def search(self, query: str, baseline: str, *, limit: int = 20, spec: str | None = None) -> dict:
        status = self.status(baseline)
        if not isinstance(query, str) or not query.strip() or len(query) > 4096:
            raise ValueError("query must contain 1-4096 characters")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", query, re.UNICODE)))
        if not tokens:
            raise ValueError("query contains no searchable terms")
        if len(tokens) > 128:
            raise ValueError("query exceeds 128 searchable terms")
        expression = " OR ".join('"' + token + '"' for token in tokens)
        parameters = [expression, baseline]
        filter_sql = ""
        if spec is not None:
            filter_sql = " AND d.spec=?"
            parameters.append(spec)
        joins = """FROM chunks_fts f JOIN chunks k ON k.id=f.chunk_id
                   JOIN clauses c ON c.id=k.clause_pk JOIN documents d ON d.id=c.document_id
                   WHERE chunks_fts MATCH ? AND d.baseline=?""" + filter_sql
        matched = self.connection.execute("SELECT count(*) " + joins, parameters).fetchone()[0]
        candidate_limit = max(1000, limit * 10)
        rows = self.connection.execute("""SELECT k.id AS chunk_id, k.payload AS chunk_payload,
            c.metadata AS clause_payload, c.clause_id, d.*, bm25(chunks_fts) AS rank
            """ + joins + " ORDER BY rank, k.id LIMIT ?", [*parameters, candidate_limit]).fetchall()
        results, seen = [], set()
        for row in rows:
            address = (row["spec"], row["version"], row["clause_id"])
            if address in seen:
                continue
            seen.add(address)
            evidence = self._evidence(row)
            evidence.pop("search_text")
            evidence["score"] = -row["rank"]
            results.append(evidence)
        truncated = matched > len(rows) or len(results) > limit
        return {"query": query, "backend": "sqlite_fts5", "score_meaning": "lexical_rank_not_confidence",
                "results": results[:limit], "matched_chunks": matched,
                "candidates_examined": len(rows), "truncated": truncated,
                "candidate_limit": candidate_limit, "corpus_status": status}

    def get_clause(self, baseline: str, spec: str, version: str, clause_id: str,
                   *, include_children: bool = False) -> dict:
        status = self.status(baseline)
        row = self.connection.execute("""SELECT c.*, d.source_url, d.filename, d.sha256
            FROM clauses c JOIN documents d ON d.id=c.document_id
            WHERE d.baseline=? AND d.spec=? AND d.version=? AND c.clause_id=?""",
                                      (baseline, spec, version, clause_id)).fetchone()
        if row is None:
            raise ValueError(f"exact clause not found: {baseline} / {spec} / {version} / {clause_id}")
        all_rows = self.connection.execute("SELECT * FROM clauses WHERE document_id=? ORDER BY ordinal", (row["document_id"],)).fetchall()
        all_clauses = [json.loads(item["payload"]) for item in all_rows]
        printed_id = clause_id.split("~", 1)[0]
        same_printed = [item["clause_id"] for item in all_clauses
                        if item["clause_id"].split("~", 1)[0] == printed_id]
        descendants = []
        parents = {clause_id}
        for clause in all_clauses:
            if clause["parent"] in parents:
                parents.add(clause["clause_id"])
                descendants.append(clause)
        selected = json.loads(row["payload"])
        return {"baseline": baseline, "spec": spec, "version": version, "clause": selected,
                "printed_clause_id": printed_id,
                "same_printed_clause_ids": same_printed,
                "source_url": row["source_url"], "filename": row["filename"], "sha256": row["sha256"],
                "children_included": include_children,
                "descendant_clause_ids": [item["clause_id"] for item in descendants],
                "descendants": descendants if include_children else [],
                "corpus_status": status}

    def export_chunks(self, baseline: str):
        self.status(baseline)
        rows = self.connection.execute("""SELECT k.id AS chunk_id, k.payload AS chunk_payload,
            c.metadata AS clause_payload, c.clause_id, d.* FROM chunks k
            JOIN clauses c ON c.id=k.clause_pk JOIN documents d ON d.id=c.document_id
            WHERE d.baseline=? ORDER BY k.id""", (baseline,))
        for row in rows:
            yield self._evidence(row)

    def document_candidates(self, baselines: list[str], spec: str) -> list[dict]:
        """Return pinned entries, including failed imports, without substituting versions."""
        entries = []
        for baseline in dict.fromkeys(baselines):
            for row in self.connection.execute("SELECT * FROM documents WHERE baseline=? AND spec=?",
                                               (baseline, spec)):
                item = dict(row)
                item["warnings"] = json.loads(item["warnings"])
                item["clause_count"] = self.connection.execute(
                    "SELECT count(*) FROM clauses WHERE document_id=?", (item["id"],)).fetchone()[0]
                del item["id"]
                entries.append(item)
        return entries

    def clause_records(self, baseline: str, spec: str, version: str):
        self.status(baseline)
        rows = self.connection.execute("""SELECT c.payload FROM clauses c JOIN documents d ON d.id=c.document_id
            WHERE d.baseline=? AND d.spec=? AND d.version=? ORDER BY c.ordinal""", (baseline, spec, version))
        for row in rows:
            yield json.loads(row["payload"])

    def list_clauses(self, baseline: str, spec: str, version: str, *, offset: int = 0, limit: int = 100) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("offset must be >= 0 and limit must be between 1 and 500")
        status = self.status(baseline)
        candidates = [item for item in status["documents"] if item["spec"] == spec and item["version"] == version]
        if not candidates or candidates[0]["status"] != "indexed":
            raise ValueError("exact document is not indexed in this baseline")
        rows = self.connection.execute("""SELECT c.payload FROM clauses c JOIN documents d ON d.id=c.document_id
            WHERE d.baseline=? AND d.spec=? AND d.version=? ORDER BY c.ordinal LIMIT ? OFFSET ?""",
                                       (baseline, spec, version, limit + 1, offset)).fetchall()
        results = []
        for row in rows[:limit]:
            item = json.loads(row["payload"])
            results.append({key: item[key] for key in ("clause_id", "title", "title_path", "parent", "annex_type")})
        return {"baseline": baseline, "spec": spec, "version": version, "clauses": results,
                "next_offset": offset + limit if len(rows) > limit else None, "corpus_status": status}

    def context_addresses(self, baseline: str, spec: str, version: str, clause_id: str,
                          *, neighbours: int = 1, include_parent: bool = True) -> list[dict]:
        if type(neighbours) is not int or not 0 <= neighbours <= 3:
            raise ValueError("neighbours must be an integer from 0 to 3")
        rows = self.connection.execute("""SELECT c.clause_id, c.ordinal, c.payload FROM clauses c
            JOIN documents d ON d.id=c.document_id WHERE d.baseline=? AND d.spec=? AND d.version=? ORDER BY c.ordinal""",
                                       (baseline, spec, version)).fetchall()
        position = next((index for index, row in enumerate(rows) if row["clause_id"] == clause_id), None)
        if position is None:
            raise ValueError("exact clause not found for context lookup")
        parent = json.loads(rows[position]["payload"])["parent"]
        selected = []
        if include_parent and parent:
            selected.append((parent, "parent"))
        for row in rows[max(0, position-neighbours):position+neighbours+1]:
            if row["clause_id"] != clause_id:
                selected.append((row["clause_id"], "neighbour"))
        return [{"baseline": baseline, "spec": spec, "version": version, "clause": item,
                 "relationship": relationship} for item, relationship in dict(selected).items()]

    def definition_matches(self, baseline: str, symbols: list[str], spec: str | None = None) -> list[dict]:
        """Exact definition names are useful anchors for technical identifiers."""
        found = []
        for symbol in symbols:
            parameters = [baseline, "%" + symbol + "%"]
            restriction = ""
            if spec is not None:
                restriction = " AND d.spec=?"
                parameters.append(spec)
            rows = self.connection.execute("""SELECT k.id AS chunk_id, k.payload AS chunk_payload,
                c.metadata AS clause_payload,c.clause_id,d.* FROM clauses c
                JOIN documents d ON d.id=c.document_id JOIN chunks k ON k.clause_pk=c.id
                WHERE d.baseline=? AND json_extract(c.metadata,'$.title') LIKE ?
                AND json_extract(k.payload,'$.kind')='heading'""" + restriction + " ORDER BY c.id", parameters)
            for row in rows:
                metadata = json.loads(row["clause_payload"])
                match = re.fullmatch(r"(?:Type|Enumeration)(?::\s*|\s+)" + re.escape(symbol), metadata["title"])
                if match:
                    item = self._evidence(row)
                    item.pop("search_text")
                    item["match_reason"] = "exact_definition_symbol"
                    item["identifier"] = symbol
                    found.append(item)
        return found
