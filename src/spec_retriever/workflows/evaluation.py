"""Required-clause recall with explicit dataset provenance and missed addresses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from spec_retriever.retrieval.service import collect_evidence, multi_search, _address, _key


def evaluate(corpus, dataset_path: str | Path, *, mode: str = "collect", limit: int = 20,
             search_fn=None) -> dict:
    raw = Path(dataset_path).read_bytes()
    dataset = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(dataset, dict) or dataset.get("schema_version") != 1:
        raise ValueError("evaluation dataset schema_version must be 1")
    if not isinstance(dataset.get("label_source"), str) or not dataset["label_source"].strip():
        raise ValueError("dataset must describe label_source; generated labels are not human-reviewed")
    if mode not in {"single", "multi", "collect"}:
        raise ValueError("evaluation mode must be single, multi or collect")
    baseline = dataset.get("baseline")
    status = corpus.status(baseline)
    if dataset.get("manifest_sha256") and dataset["manifest_sha256"] != status["manifest_sha256"]:
        raise ValueError("evaluation dataset pins a different corpus manifest")
    for expected_source in dataset.get("documents", []):
        matching = [item for item in status["documents"] if item["spec"] == expected_source.get("spec")
                    and item["version"] == expected_source.get("version") and item["status"] == "indexed"]
        if not matching or matching[0]["sha256"] != expected_source.get("sha256"):
            raise ValueError("evaluation source document/version/checksum is not indexed")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 500:
        raise ValueError("evaluation cases must contain 1-500 cases")
    rows, seen_ids = [], set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not case["id"].strip() or case["id"] in seen_ids):
            raise ValueError("each case must have a unique string id")
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError("each evaluation case needs a nonempty query")
        seen_ids.add(case["id"])
        expected = case.get("expected")
        if not isinstance(expected, list) or not expected:
            raise ValueError("each case needs at least one expected clause address")
        expected_map = {_key(address): address for item in expected for address in [_address(item, baseline)]}
        if mode == "single":
            result = (search_fn or corpus.search)(case["query"], baseline, limit=limit)
            found = result["results"]
            gaps, truncated = not found, result["truncated"]
            full_addresses = set()
        elif mode == "multi":
            result = multi_search(corpus, case["query"], baseline, subqueries=case.get("subqueries"),
                                  limit=limit, search_fn=search_fn)
            found = result["results"]
            gaps, truncated = not found, result["truncated"]
            full_addresses = set()
        else:
            result = collect_evidence(corpus, case["query"], baseline, subqueries=case.get("subqueries"),
                dependencies=case.get("dependencies", dataset.get("dependencies")), limit=limit, search_fn=search_fn)
            found = [node["address"] for node in result["evidence"]["nodes"]]
            gaps, truncated = result["has_gaps"], result["truncated"]
            full_addresses = {_key(node["address"]) for node in result["evidence"]["nodes"]
                              if not node["content_truncated"]}
        found_addresses = list(dict.fromkeys(_key(_address(item, baseline)) for item in found))
        hits = set(found_addresses) & expected_map.keys()
        reciprocal_rank = next((1 / rank for rank, key in enumerate(found_addresses, 1) if key in expected_map), 0)
        rows.append({"id": case["id"], "query": case["query"], "expected_count": len(expected_map),
            "found_required_count": len(hits), "required_clause_recall": len(hits) / len(expected_map),
            "reciprocal_rank": reciprocal_rank, "retrieved_clause_count": len(found_addresses),
            "missing": [address for key, address in expected_map.items() if key not in hits],
            "fully_returned_required_count": len(full_addresses & expected_map.keys()) if mode == "collect" else None,
            "content_limited_required": [address for key, address in expected_map.items()
                                         if key in hits and key not in full_addresses] if mode == "collect" else [],
            "has_gaps": gaps, "truncated": truncated})
    required = sum(row["expected_count"] for row in rows)
    found = sum(row["found_required_count"] for row in rows)
    return {"dataset_sha256": hashlib.sha256(raw).hexdigest(), "label_source": dataset["label_source"],
        "baseline": baseline, "manifest_sha256": status["manifest_sha256"], "mode": mode, "limit": limit,
        "cases": rows, "case_count": len(rows), "micro_required_clause_recall": found / required,
        "macro_required_clause_recall": sum(row["required_clause_recall"] for row in rows) / len(rows),
        "mean_reciprocal_rank": sum(row["reciprocal_rank"] for row in rows) / len(rows),
        "micro_full_clause_evidence_recall": sum(row["fully_returned_required_count"] for row in rows) / required
            if mode == "collect" else None,
        "all_listed_requirements_found_cases": sum(not row["missing"] for row in rows),
        "interpretation": "recall_against_listed_labels_not_general_compliance_accuracy", "corpus_status": status}
