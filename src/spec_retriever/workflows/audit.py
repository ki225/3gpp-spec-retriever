"""Exhaustive block scans and explicit, version-pinned feature audit bundles.

Scanning completeness is a mechanical property; applicability remains an agent
review task. No ranking cutoff or character budget drops a matching paragraph.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unicodedata

from spec_retriever.retrieval.references import ReferenceResolver


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _feature(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
        raise ValueError("each feature needs an id")
    feature = {"id": raw["id"], "any_terms": [], "all_terms": [], "sections": []}
    for field in ("any_terms", "all_terms"):
        values = raw.get(field, [])
        if not isinstance(values, list) or len(values) > 100 or any(
                not isinstance(term, str) or not term.strip() or len(term) > 256 for term in values):
            raise ValueError(f"{field} must contain at most 100 nonempty strings of <=256 characters")
        feature[field] = [_normalize(term) for term in values]
    sections = raw.get("sections", [])
    if not isinstance(sections, list) or len(sections) > 200:
        raise ValueError("sections must be a list of at most 200 exact clause selectors")
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("spec"), str) or not re.fullmatch(r"\d{2,3}\.\d{3}", section["spec"]):
            raise ValueError("section needs a specification number")
        if not isinstance(section.get("clause"), str) or not section["clause"].strip():
            raise ValueError("section needs an exact clause id")
        if type(section.get("include_children", True)) is not bool:
            raise ValueError("include_children must be boolean")
        feature["sections"].append(dict(section))
    if not any(feature[field] for field in ("any_terms", "all_terms", "sections")):
        raise ValueError("provide literal terms or explicit clause selectors")
    return feature


def _matches(feature: dict, spec: str, clause: str, text: str) -> dict | None:
    sections = [item["clause"] for item in feature["sections"] if item["spec"] == spec and (
        item["clause"] == clause or (item.get("include_children", True) and clause.startswith(item["clause"] + ".")))]
    hits = [term for term in feature["any_terms"] if term in text]
    all_hits = feature["all_terms"] and all(term in text for term in feature["all_terms"])
    lexical = (bool(hits) if feature["any_terms"] else bool(all_hits)) and (
        bool(all_hits) if feature["all_terms"] else True)
    if sections or lexical:
        return {"feature": feature["id"], "section_selectors": sections,
                "literal_terms": list(dict.fromkeys([*hits, *(feature["all_terms"] if all_hits else [])]))}
    return None


def _scan(corpus, baseline: str, features: list[dict], *, offset: int, limit: int | None,
          spec: str | None = None) -> dict:
    status = corpus.status(baseline)
    results, total, scanned_blocks, scanned_clauses = [], 0, 0, 0
    documents = [item for item in status["documents"] if spec is None or item["spec"] == spec]
    for document in documents:
        if document["status"] != "indexed":
            continue
        for clause in corpus.clause_records(baseline, document["spec"], document["version"]):
            scanned_clauses += 1
            for ordinal, block in enumerate(clause["blocks"]):
                scanned_blocks += 1
                text = _normalize(block["text"])
                matches = [match for feature in features if (match := _matches(
                    feature, document["spec"], clause["clause_id"], text))]
                if not matches:
                    continue
                index = total
                total += 1
                if index < offset or (limit is not None and len(results) >= limit):
                    continue
                identity = [baseline, document["spec"], document["version"], clause["clause_id"], ordinal,
                            document["sha256"], block["location"]]
                evidence_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                results.append({"evidence_id": evidence_id, "baseline": baseline,
                    "spec": document["spec"], "version": document["version"], "clause": clause["clause_id"],
                    "title": clause["title"], "title_path": clause["title_path"],
                    "annex_type": clause["annex_type"], "block_ordinal": ordinal,
                    "source_url": document["source_url"], "sha256": document["sha256"],
                    "block": block, "matches": matches, "relevance": "requires_review"})
    next_offset = offset + len(results) if offset + len(results) < total else None
    return {"baseline": baseline, "manifest_sha256": status["manifest_sha256"],
        "mode": "exhaustive_literal_and_section_scan", "ranking_cutoff": None,
        "scan_complete": True, "scanned_clause_count": scanned_clauses, "scanned_block_count": scanned_blocks,
        "total_matches": total, "offset": offset, "next_offset": next_offset,
        "page_complete": next_offset is None, "results": results, "corpus_status": status,
        "applicable_spec_coverage": "not_assessed",
        "interpretation": "all_matching_supported_blocks_in_pinned_scope_not_all_applicable_requirements"}


def scan_corpus(corpus, baseline: str, *, any_terms: list[str] | None = None,
                all_terms: list[str] | None = None, sections: list[dict] | None = None,
                spec: str | None = None, offset: int = 0, limit: int = 100) -> dict:
    """Scan the entire corpus, then paginate complete matching source blocks."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("offset must be >=0; limit must be 1..500")
    feature = _feature({"id": "query", "any_terms": [] if any_terms is None else any_terms,
                        "all_terms": [] if all_terms is None else all_terms,
                        "sections": [] if sections is None else sections})
    return _scan(corpus, baseline, [feature], offset=offset, limit=limit, spec=spec)


def audit_case(corpus, case_path: str | Path) -> dict:
    """Export every matching block, complete source clauses and reference targets.

    The case's selected subtrees supplement keywords when definitions omit the
    feature's name. Reference targets form a review queue, not guessed relevance.
    """
    path = Path(case_path)
    raw_bytes = path.read_bytes()
    case = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(case, dict) or case.get("schema_version") != 1:
        raise ValueError("audit case schema_version must be 1")
    if not isinstance(case.get("case_id"), str) or not case["case_id"].strip():
        raise ValueError("case needs a case_id")
    if not isinstance(case.get("features"), list) or not 1 <= len(case["features"]) <= 100:
        raise ValueError("case needs 1..100 features")
    features = [_feature(item) for item in case["features"]]
    if len({item["id"] for item in features}) != len(features):
        raise ValueError("feature ids must be unique")
    required = case.get("required_documents")
    if not isinstance(required, list) or not required:
        raise ValueError("case needs required_documents pinned by spec, version and SHA-256")
    baseline = case.get("baseline")
    if not isinstance(baseline, str) or not baseline.strip():
        raise ValueError("case needs a baseline")
    status = corpus.status(baseline)
    if case.get("release", status["release"]) != status["release"]:
        raise ValueError("case Release differs from pinned baseline Release")
    if case.get("manifest_sha256", status["manifest_sha256"]) != status["manifest_sha256"]:
        raise ValueError("case manifest digest differs from pinned baseline")
    dependencies = case.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > 19 or any(
            not isinstance(name, str) or not name.strip() for name in dependencies):
        raise ValueError("dependencies must be at most 19 explicit baseline names")
    scopes = list(dict.fromkeys([baseline, *dependencies]))
    statuses = [corpus.status(name) for name in scopes]
    documents = [doc for snapshot in statuses for doc in snapshot["documents"]]
    gaps = []
    seen_specs = set()
    for item in required:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("spec", "version", "sha256")) or not re.fullmatch(r"\d{2,3}\.\d{3}", item["spec"]) or not re.fullmatch(
                r"\d+\.\d+\.\d+", item.get("version", "")) or not re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", "")):
            raise ValueError("required documents need exact spec, version and SHA-256")
        if item["spec"] in seen_specs:
            raise ValueError("required_documents contains a duplicate specification")
        seen_specs.add(item["spec"])
        candidates = [doc for doc in documents if doc["spec"] == item["spec"]]
        exact = [doc for doc in candidates if all(doc[key] == item[key] for key in ("version", "sha256")) and doc["status"] == "indexed"]
        found = exact[0] if len(exact) == 1 else (candidates[0] if candidates else None)
        if found is None or any(found[field] != item[field] for field in ("version", "sha256")) or found["status"] != "indexed":
            gaps.append({"required": item, "actual": found, "reason": "required_document_not_available_exactly"})
        elif len(exact) != 1:
            gaps.append({"required": item, "actual": exact, "reason": "required_document_ambiguous_baseline"})
    selector_gaps = []
    for feature in features:
        for selector in feature["sections"]:
            found = next((doc for doc in documents if doc["spec"] == selector["spec"] and doc["status"] == "indexed"), None)
            try:
                if found is None:
                    raise ValueError("selector document not indexed")
                corpus.get_clause(found["baseline"], selector["spec"], found["version"], selector["clause"])
            except ValueError:
                selector_gaps.append({"feature": feature["id"], "selector": selector, "reason": "section_not_available_exactly"})
    scans = [_scan(corpus, name, features, offset=0, limit=None) for name in scopes]
    result = scans[0]
    result["results"] = [item for scan in scans for item in scan["results"]]
    for field in ("total_matches", "scanned_clause_count", "scanned_block_count"):
        result[field] = sum(scan[field] for scan in scans)
    result["dependency_status"] = statuses[1:]
    addresses = list(dict.fromkeys((item["baseline"], item["spec"], item["version"], item["clause"]) for item in result["results"]))
    resolver = ReferenceResolver(corpus, baseline, dependencies)
    clauses, references, targets = [], [], {}
    selected = set(addresses)
    for source_baseline, spec, version, clause_id in addresses:
        address = {"baseline": source_baseline, "spec": spec, "version": version, "clause": clause_id}
        clause = corpus.get_clause(source_baseline, spec, version, clause_id)
        clauses.append({key: value for key, value in clause.items() if key != "corpus_status"})
        for reference in resolver.references_for(address, clause["clause"]):
            resolution = resolver.resolve(reference, address)
            references.append({"source": address, "reference": reference, "resolution": resolution})
            target = resolution.get("target")
            if target and resolution["status"] == "resolved" and (
                    target["baseline"], target["spec"], target["version"], target["clause"]) not in selected:
                targets[(target["baseline"], target["spec"], target["version"], target["clause"])] = target
    reference_evidence = []
    for target in targets.values():
        full = corpus.get_clause(target["baseline"], target["spec"], target["version"], target["clause"])
        reference_evidence.append({key: value for key, value in full.items() if key != "corpus_status"})
    return {**result, "case_id": case["case_id"], "case_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "source_issue": case.get("source_issue"), "required_document_gaps": gaps,
        "required_documents_ready": not gaps, "section_selector_gaps": selector_gaps, "scope_ready": not gaps and not selector_gaps,
        "full_clauses": clauses,
        "references": references, "reference_targets_to_review": list(targets.values()),
        "reference_target_evidence": reference_evidence,
        "review_status": "agent_review_required", "evidence_character_truncation": False,
        "feature_counts": {feature["id"]: sum(any(match["feature"] == feature["id"] for match in item["matches"])
            for item in result["results"]) for feature in features}}
