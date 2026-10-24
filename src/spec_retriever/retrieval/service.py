"""Bounded multiquery retrieval and version-aware evidence graph collection."""

from __future__ import annotations

from collections import deque
import re

from spec_retriever.retrieval.references import ReferenceResolver

ACRONYMS = {
    "AMF": "Access and Mobility Management Function", "SMF": "Session Management Function",
    "UPF": "User Plane Function", "UE": "User Equipment", "PDU": "Protocol Data Unit",
    "NRF": "Network Repository Function", "AUSF": "Authentication Server Function",
    "UDM": "Unified Data Management", "SBI": "Service Based Interface",
    "SUPI": "Subscription Permanent Identifier", "SUCI": "Subscription Concealed Identifier",
}


def _identifiers(query: str) -> list[str]:
    words = re.findall(r"\b[A-Za-z][A-Za-z0-9_]{1,63}\b", query)
    return list(dict.fromkeys(word for word in words if re.search(r"[a-z][A-Z]", word)))[:3]


def plan_queries(query: str, subqueries: list[str] | None = None) -> list[str]:
    if not isinstance(query, str) or not query.strip() or len(query) > 4096:
        raise ValueError("query must contain 1-4096 characters")
    if subqueries is not None and (not isinstance(subqueries, list) or len(subqueries) > 8):
        raise ValueError("subqueries must be a list with at most 8 items")
    queries = [query.strip()]
    for item in subqueries or []:
        if not isinstance(item, str) or not item.strip() or len(item) > 4096:
            raise ValueError("each subquery must contain 1-4096 characters")
        queries.append(item.strip())
    # Explicit delimiters only; splitting every 'and' changes technical phrases.
    fragments = [part.strip(" -•\t") for part in re.split(r"[;；\n]+", query) if part.strip(" -•\t")]
    if 1 < len(fragments) <= 4:
        queries.extend(fragments)
    expanded = query
    for acronym, phrase in ACRONYMS.items():
        expanded = re.sub(r"\b" + acronym + r"\b", phrase, expanded)
    if expanded != query:
        queries.append(expanded)
    queries.extend(_identifiers(query))
    unique = list(dict.fromkeys(queries))
    if len(unique) > 14:
        raise ValueError("query plan exceeds 14 variants; use fewer subqueries")
    return unique


def multi_search(corpus, query: str, baseline: str, *, subqueries: list[str] | None = None,
                 limit: int = 20, spec: str | None = None, search_fn=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer from 1 to 200")
    queries = plan_queries(query, subqueries)
    search_fn = search_fn or corpus.search
    scores, items, matched, summaries = {}, {}, {}, []
    for index, variant in enumerate(queries):
        result = search_fn(variant, baseline, limit=limit, spec=spec)
        summaries.append({"query": variant, "backend": result["backend"],
                          "returned_clauses": len(result["results"]), "truncated": result["truncated"]})
        for rank, item in enumerate(result["results"], 1):
            key = (item["baseline"], item["spec"], item["version"], item["clause"])
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            items.setdefault(key, dict(item))
            matched.setdefault(key, []).append(index)
    anchors = []
    for item in corpus.definition_matches(baseline, _identifiers(query), spec):
        key = (item["baseline"], item["spec"], item["version"], item["clause"])
        if key not in anchors:
            anchors.append(key)
        items[key] = {**item, **{name: items.get(key, {}).get(name) for name in ("rerank_score",)
                              if name in items.get(key, {})}}
        scores.setdefault(key, 0)
        matched.setdefault(key, [])
    keys = [*anchors, *(key for key in sorted(scores, key=lambda key: (-scores[key], key)) if key not in anchors)]
    results = [{**items[key], "score": scores[key], "matched_query_indexes": matched[key]} for key in keys[:limit]]
    return {"query": query, "query_plan": queries, "backend": "multiquery_rrf",
            "ordering": "exact_definition_anchors_then_rrf",
            "score_meaning": "rank_fusion_not_compliance_confidence", "results": results,
            "query_status": summaries, "truncated": len(keys) > limit or any(item["truncated"] for item in summaries),
            "corpus_status": corpus.status(baseline)}


def _address(value: dict, default_baseline: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("each seed must be an address object")
    result = {key: value.get(key) for key in ("spec", "version", "clause")}
    result["baseline"] = value.get("baseline", default_baseline)
    if not all(isinstance(item, str) and item.strip() for item in result.values()):
        raise ValueError("each address requires nonempty baseline, spec, version and clause")
    return result


def _key(address: dict) -> tuple:
    return tuple(address[key] for key in ("baseline", "spec", "version", "clause"))


def _validate_budgets(max_depth, max_nodes, max_chars, max_edges=400, neighbours=1):
    for name, value, lower, upper in [("max_depth", max_depth, 0, 8), ("max_nodes", max_nodes, 1, 200),
            ("max_chars", max_chars, 1000, 2000000), ("max_edges", max_edges, 1, 5000),
            ("neighbours", neighbours, 0, 3)]:
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer from {lower} to {upper}")


def expand_references(corpus, baseline: str, seeds: list[dict], *, dependencies: list[str] | None = None,
                      max_depth: int = 2, max_nodes: int = 40, max_chars: int = 200000,
                      max_edges: int = 400, include_context: bool = False, neighbours: int = 1) -> dict:
    _validate_budgets(max_depth, max_nodes, max_chars, max_edges, neighbours)
    if not isinstance(seeds, list) or not seeds or len(seeds) > 200:
        raise ValueError("seeds must contain 1-200 clause addresses")
    resolver = ReferenceResolver(corpus, baseline, dependencies)
    addresses = [_address(item, baseline) for item in seeds]
    if any(item["baseline"] not in resolver.baselines for item in addresses):
        raise ValueError("seed baseline must be the primary or an explicit dependency baseline")
    queue, scheduled, visited = deque(), set(), set()
    edges, unresolved, pending, nodes = [], [], [], []
    chars_used = 0
    truncated = False
    per_node_chars = max(1000, max_chars // min(len(addresses), max_nodes))

    def enqueue(address, depth, relationship):
        key = _key(address)
        if key not in scheduled and key not in visited:
            scheduled.add(key)
            queue.append((address, depth, relationship))

    for address in addresses:
        enqueue(address, 0, "seed")
    if include_context:
        for address in addresses:
            try:
                for context in corpus.context_addresses(address["baseline"], address["spec"], address["version"],
                                                        address["clause"], neighbours=neighbours):
                    relationship = context.pop("relationship")
                    enqueue(context, 0, relationship)
            except ValueError:
                pass  # The main seed lookup reports the exact failure below.
    while queue:
        if len(nodes) >= max_nodes:
            pending.extend({"target": item, "reason": "node_budget"} for item, _, _ in queue)
            truncated = True
            break
        address, depth, relationship = queue.popleft()
        key = _key(address)
        scheduled.discard(key)
        if key in visited:
            continue
        visited.add(key)
        try:
            evidence = corpus.get_clause(address["baseline"], address["spec"], address["version"], address["clause"])
        except ValueError as error:
            unresolved.append({"source": address, "reason": "seed_or_target_not_found", "message": str(error)})
            continue
        clause = evidence["clause"]
        included, omitted = [], []
        node_chars = 0
        for index, block in enumerate(clause["blocks"]):
            count = len(block["text"])
            if chars_used + count <= max_chars and node_chars + count <= per_node_chars:
                included.append(block)
                chars_used += count
                node_chars += count
            else:
                omitted.append({"block_index": index, "kind": block["kind"], "chars": count,
                                "location": block["location"], "reason": "character_budget"})
        if omitted:
            truncated = True
        nodes.append({"address": address, "relationship": relationship, "depth": depth,
                      "title": clause["title"], "title_path": clause["title_path"],
                      "parent": clause["parent"], "annex_type": clause["annex_type"],
                      "blocks": included, "omitted_blocks": omitted[:50], "content_truncated": bool(omitted),
                      "omitted_block_count": len(omitted), "omitted_block_details_truncated": len(omitted) > 50,
                      "descendant_clause_ids": evidence["descendant_clause_ids"],
                      **{name: evidence[name] for name in ("source_url", "filename", "sha256")}})
        references = resolver.references_for(address, clause)
        for index, reference in enumerate(references):
            if len(edges) >= max_edges:
                pending.append({"source": address, "reason": "edge_budget", "unprocessed_references": len(references)-index})
                truncated = True
                break
            resolution = resolver.resolve(reference, address)
            preview = {**reference, "raw": reference["raw"][:800], "raw_excerpt_truncated": len(reference["raw"]) > 800}
            edge = {"source": address, "reference": preview, **resolution}
            edges.append(edge)
            if resolution["status"] != "resolved":
                unresolved.append(edge)
                continue
            target = resolution["target"]
            if _key(target) in visited or _key(target) in scheduled:
                continue
            if depth >= max_depth:
                pending.append({"source": address, "target": target, "reason": "depth_budget"})
                truncated = True
            else:
                enqueue(target, depth+1, "reference")
    scopes = [corpus.status(name) for name in resolver.baselines]
    limitations = any(not item["ingestion_complete"] or item["has_parser_limitations"] for item in scopes)
    return {"baseline": baseline, "nodes": nodes, "edges": edges, "unresolved": unresolved,
            "pending": pending, "truncated": truncated,
            "has_gaps": bool(unresolved or pending or truncated or limitations),
            "coverage": "bounded_parsed_references_not_exhaustive_applicability",
            "parsed_reference_kinds": ["clause", "annex", "table", "specification", "bibliography", "typed_table_cells"],
            "budget": {"max_depth": max_depth, "max_nodes": max_nodes, "max_edges": max_edges,
                       "max_chars": max_chars, "per_node_chars": per_node_chars, "chars_used": chars_used}, "corpus_status": scopes}


def collect_evidence(corpus, query: str, baseline: str, *, subqueries: list[str] | None = None,
                     dependencies: list[str] | None = None, limit: int = 20, spec: str | None = None,
                     max_depth: int = 2, max_nodes: int = 60, max_chars: int = 200000,
                     neighbours: int = 1, search_fn=None) -> dict:
    _validate_budgets(max_depth, max_nodes, max_chars, neighbours=neighbours)
    search = multi_search(corpus, query, baseline, subqueries=subqueries, limit=limit, spec=spec, search_fn=search_fn)
    seeds = [{key: item[key] for key in ("baseline", "spec", "version", "clause")} for item in search["results"]]
    if seeds:
        graph = expand_references(corpus, baseline, seeds, dependencies=dependencies, max_depth=max_depth,
                                  max_nodes=max_nodes, max_chars=max_chars, include_context=True, neighbours=neighbours)
    else:
        # Still validate explicit scopes even when the search has no hits.
        resolver = ReferenceResolver(corpus, baseline, dependencies)
        graph = {"nodes": [], "edges": [], "unresolved": [], "pending": [], "truncated": False,
                 "has_gaps": True, "coverage": "no_evidence_found",
                 "corpus_status": [corpus.status(name) for name in resolver.baselines]}
    return {"query": query, "search": search, "evidence": graph,
            "has_gaps": graph["has_gaps"] or search["truncated"],
            "truncated": graph["truncated"] or search["truncated"]}
