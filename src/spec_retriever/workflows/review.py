"""Validate an agent's clause-by-clause review against an exact evidence bundle."""

import hashlib
import json
from pathlib import Path


def _key(address):
    return tuple(address[field] for field in ("baseline", "spec", "version", "clause"))


def review_bundle(bundle_path, review_path):
    payload = Path(bundle_path).read_bytes()
    bundle = json.loads(payload.decode("utf-8-sig"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8-sig"))
    if review.get("schema_version") != 1 or review.get("case_sha256") != bundle["case_sha256"]:
        raise ValueError("review must match evidence case SHA-256 and schema_version 1")
    if review.get("evidence_sha256") != hashlib.sha256(payload).hexdigest():
        raise ValueError("review does not match the exact evidence bundle bytes")
    decisions = {}
    for item in review.get("decisions", []):
        if not isinstance(item, dict) or not all(isinstance(item.get(field), str) and item[field].strip()
                for field in ("baseline", "spec", "version", "clause", "definition", "analysis")):
            raise ValueError("each review decision needs an exact address, definition and analysis")
        if item.get("classification") not in {"direct", "supporting", "excluded"}:
            raise ValueError("review classification must be direct, supporting or excluded")
        key = _key(item)
        if key in decisions:
            raise ValueError("duplicate clause review decision")
        decisions[key] = item
    expected = {_key({**item, "clause": item["clause"]["clause_id"]}) for item in bundle["full_clauses"]}
    if set(decisions) - expected:
        raise ValueError("review references clauses absent from full source evidence")
    missing = sorted(expected - set(decisions))
    references = {}
    expected_targets = {_key(item) for item in bundle["reference_targets_to_review"]}
    available_targets = {_key({**item, "clause": item["clause"]["clause_id"]}) for item in bundle.get("reference_target_evidence", [])}
    for item in review.get("reference_decisions", []):
        if not isinstance(item, dict) or not all(isinstance(item.get(field), str) and item[field].strip()
                for field in ("baseline", "spec", "version", "clause", "reason")):
            raise ValueError("reference review needs an exact target and reason")
        key = _key(item)
        if key in references or key not in expected_targets:
            raise ValueError("duplicate or unknown reference review target")
        if item.get("disposition") not in {"outside_issue_scope", "needs_followup", "supporting_definition"}:
            raise ValueError("reference needs supporting_definition, outside_issue_scope or needs_followup")
        if item["disposition"] == "supporting_definition" and (key not in available_targets or not all(
                isinstance(item.get(field), str) and item[field].strip() for field in ("definition", "analysis"))):
            raise ValueError("supporting reference needs full evidence, definition and analysis")
        references[key] = item
    missing_targets = sorted(expected_targets - set(references))
    followups = [item for item in references.values() if item["disposition"] == "needs_followup"]
    return {"case_id": bundle["case_id"], "case_sha256": bundle["case_sha256"],
        "evidence_sha256": hashlib.sha256(payload).hexdigest(), "reviewed_clauses": len(decisions),
        "unreviewed_clauses": missing, "unreviewed_reference_targets": missing_targets,
        "reference_followups": followups,
        "review_coverage_complete": not missing and not missing_targets,
        "scope_ready": bundle["scope_ready"],
        "classifications": {name: sum(item["classification"] == name for item in decisions.values())
                            for name in ("direct", "supporting", "excluded")},
        "semantic_accuracy": "agent_reviewed_not_human_validated",
        "global_applicability": "not_proven",
        "interpretation": "checks_review_presence_and_provenance_not_truth_or_exhaustive_3gpp_coverage"}
