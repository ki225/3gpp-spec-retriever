"""Persistent, version-pinned explicit references and reverse lookups."""

from __future__ import annotations

import json
import sys

from .references import ReferenceResolver, bibliography, extract_references

EXTRACTOR_VERSION = 2


def index_status(corpus, baseline: str) -> dict:
    row = corpus.connection.execute(
        "SELECT manifest_digest, extractor_version, complete FROM reference_index_state WHERE baseline=?",
        (baseline,)).fetchone()
    current = corpus.connection.execute("SELECT digest FROM baselines WHERE name=?", (baseline,)).fetchone()
    if current is None:
        raise ValueError(f"unknown baseline: {baseline}")
    indexed = corpus.connection.execute(
        "SELECT count(*) FROM reference_index_documents WHERE baseline=?", (baseline,)).fetchone()[0]
    expected = corpus.connection.execute(
        "SELECT count(*) FROM documents WHERE baseline=? AND status='indexed'", (baseline,)).fetchone()[0]
    complete = bool(row and row["complete"] and row["manifest_digest"] == current["digest"]
                    and row["extractor_version"] == EXTRACTOR_VERSION and indexed == expected)
    return {"baseline": baseline, "complete": complete, "indexed_documents": indexed,
            "expected_documents": expected, "extractor_version": EXTRACTOR_VERSION}


def build_reference_index(corpus, baseline: str, *, progress: bool = False) -> dict:
    """Build per document; a stopped run can resume without publishing partial edges."""
    status = corpus.status(baseline)
    if not status["ingestion_complete"]:
        raise ValueError("reference indexing requires a fully ingested baseline")
    digest = status["manifest_sha256"]
    connection = corpus.connection
    state = connection.execute(
        "SELECT manifest_digest,extractor_version,complete FROM reference_index_state WHERE baseline=?",
        (baseline,)).fetchone()
    if state and state["manifest_digest"] == digest and state["extractor_version"] == EXTRACTOR_VERSION and state["complete"]:
        ready = index_status(corpus, baseline)
        if ready["complete"]:
            return ready
    if not state or state["manifest_digest"] != digest or state["extractor_version"] != EXTRACTOR_VERSION:
        with connection:
            connection.execute("DELETE FROM reference_edges WHERE source_baseline=?", (baseline,))
            connection.execute("DELETE FROM reference_index_documents WHERE baseline=?", (baseline,))
            connection.execute("REPLACE INTO reference_index_state VALUES (?,?,?,0)",
                               (baseline, digest, EXTRACTOR_VERSION))
    resolver = ReferenceResolver(corpus, baseline)
    documents = connection.execute(
        "SELECT id,spec,version,sha256 FROM documents WHERE baseline=? AND status='indexed' ORDER BY spec",
        (baseline,)).fetchall()
    for position, document in enumerate(documents, 1):
        existing = connection.execute(
            "SELECT sha256,extractor_version FROM reference_index_documents WHERE document_id=?",
            (document["id"],)).fetchone()
        if existing and existing["sha256"] == document["sha256"] and existing["extractor_version"] == EXTRACTOR_VERSION:
            continue
        rows = connection.execute(
            "SELECT id,payload FROM clauses WHERE document_id=? ORDER BY ordinal", (document["id"],)).fetchall()
        records = [json.loads(row["payload"]) for row in rows]
        reference_labels = bibliography(records)
        address = {"baseline": baseline, "spec": document["spec"], "version": document["version"]}
        edges = []
        for row, clause in zip(rows, records):
            source = {**address, "clause": clause["clause_id"]}
            for reference in extract_references(clause, source_spec=document["spec"],
                                                source_version=document["version"], references=reference_labels):
                resolution = resolver.resolve(reference, source)
                target_pk = None
                if resolution["status"] == "resolved":
                    target = resolution["target"]
                    target_row = connection.execute("""SELECT c.id FROM clauses c JOIN documents d ON d.id=c.document_id
                        WHERE d.baseline=? AND d.spec=? AND d.version=? AND c.clause_id=?""",
                        (target["baseline"], target["spec"], target["version"], target["clause"])).fetchone()
                    target_pk = target_row["id"] if target_row else None
                    if target_pk is None:
                        resolution = {"status": "unresolved", "reason": "target_clause_missing_during_index"}
                edges.append((row["id"], baseline, target_pk, reference["kind"], resolution["status"],
                              resolution.get("reason"), json.dumps(reference, ensure_ascii=False),
                              json.dumps(resolution, ensure_ascii=False)))
        with connection:
            connection.execute("DELETE FROM reference_edges WHERE source_clause_pk IN "
                               "(SELECT id FROM clauses WHERE document_id=?)", (document["id"],))
            connection.executemany("""INSERT INTO reference_edges
                (source_clause_pk,source_baseline,target_clause_pk,kind,status,reason,reference_json,resolution_json)
                VALUES (?,?,?,?,?,?,?,?)""", edges)
            connection.execute("REPLACE INTO reference_index_documents VALUES (?,?,?,?,?)",
                               (document["id"], baseline, document["sha256"], EXTRACTOR_VERSION, len(edges)))
        resolver._documents.clear()  # Table-owner lookups need not retain every document in R17.
        resolver._table_owners.clear()
        if progress and position % 25 == 0:
            print(f"reference index: {position}/{len(documents)} documents", file=sys.stderr, flush=True)
    with connection:
        connection.execute("UPDATE reference_index_state SET complete=1 WHERE baseline=?", (baseline,))
    result = index_status(corpus, baseline)
    result["edge_count"] = connection.execute(
        "SELECT count(*) FROM reference_edges WHERE source_baseline=?", (baseline,)).fetchone()[0]
    return result


def references_for_clause(corpus, address: dict, *, direction: str = "both", offset: int = 0,
                          limit: int = 100) -> dict:
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("direction must be outgoing, incoming or both")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    baseline = address["baseline"]
    state = index_status(corpus, baseline)
    if not state["complete"]:
        return {"address": address, "index_status": state, "edges": [], "next_offset": None, "truncated": False,
                "has_gaps": True, "reason": "reference_index_incomplete"}
    source = corpus.connection.execute("""SELECT c.id FROM clauses c JOIN documents d ON d.id=c.document_id
        WHERE d.baseline=? AND d.spec=? AND d.version=? AND c.clause_id=?""",
        (baseline, address["spec"], address["version"], address["clause"])).fetchone()
    if source is None:
        raise ValueError("exact source clause not found")
    clauses = []
    if direction in {"outgoing", "both"}:
        clauses.append("source_clause_pk=?")
    if direction in {"incoming", "both"}:
        clauses.append("target_clause_pk=?")
    condition = " OR ".join(clauses)
    params = (source["id"],) * len(clauses)
    rows = corpus.connection.execute(f"""SELECT e.*, sd.spec source_spec,sd.version source_version,
        sc.clause_id source_clause,sc.metadata source_metadata,td.baseline target_baseline,td.spec target_spec,
        td.version target_version,tc.clause_id target_clause
        FROM reference_edges e JOIN clauses sc ON sc.id=e.source_clause_pk
        JOIN documents sd ON sd.id=sc.document_id
        LEFT JOIN clauses tc ON tc.id=e.target_clause_pk
        LEFT JOIN documents td ON td.id=tc.document_id
        WHERE ({condition}) ORDER BY e.id LIMIT ? OFFSET ?""", (*params, limit + 1, offset)).fetchall()
    edges = []
    for row in rows[:limit]:
        reference = json.loads(row["reference_json"])
        raw = reference.get("raw", "")
        reference["raw"] = raw[:800]
        reference["raw_excerpt_truncated"] = len(raw) > 800
        edge = {"source": {"baseline": row["source_baseline"], "spec": row["source_spec"],
                           "version": row["source_version"], "clause": row["source_clause"]},
                "source_title": json.loads(row["source_metadata"] or "{}").get("title", ""),
                "direction": "outgoing" if row["source_clause_pk"] == source["id"] else "incoming",
                "reference": reference,
                "resolution": json.loads(row["resolution_json"])}
        if row["target_clause_pk"]:
            edge["target"] = {"baseline": row["target_baseline"], "spec": row["target_spec"],
                              "version": row["target_version"], "clause": row["target_clause"]}
        edges.append(edge)
    return {"address": address, "index_status": state, "edges": edges,
            "next_offset": offset + limit if len(rows) > limit else None,
            "truncated": len(rows) > limit, "has_gaps": len(rows) > limit or any(
                edge["resolution"]["status"] != "resolved" for edge in edges)}
