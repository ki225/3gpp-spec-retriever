"""JSON command line interface shared by humans, scripts and agents."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from spec_retriever.spec_store.manifest import load_manifest, lock_manifest
from spec_retriever.spec_store.store import Corpus
from spec_retriever.retrieval.service import multi_search, expand_references, collect_evidence


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Version-pinned 3GPP clause retrieval")
    commands = root.add_subparsers(dest="command", required=True)
    lock = commands.add_parser("lock", help="pin source DOCX checksums")
    lock.add_argument("manifest")
    lock.add_argument("--output", required=True)
    ingest = commands.add_parser("ingest", help="index a locked corpus baseline")
    ingest.add_argument("manifest")
    ingest.add_argument("--db", required=True)
    search = commands.add_parser("search", help="search with provenance and import limitations")
    search.add_argument("query")
    search.add_argument("--db", required=True)
    search.add_argument("--baseline", required=True)
    search.add_argument("--spec")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--backend", choices=["sqlite", "elastic"], default="sqlite")
    search.add_argument("--subquery", action="append", default=[])
    clause = commands.add_parser("clause", help="read an exact clause without semantic substitution")
    clause.add_argument("--db", required=True)
    clause.add_argument("--baseline", required=True)
    clause.add_argument("--spec", required=True)
    clause.add_argument("--version", required=True)
    clause.add_argument("--clause", required=True)
    clause.add_argument("--include-children", action="store_true")
    status = commands.add_parser("status", help="report missing or partially parsed sources")
    status.add_argument("--db", required=True)
    status.add_argument("--baseline", required=True)
    serve = commands.add_parser("serve", help="serve read-only MCP tools over stdio")
    serve.add_argument("--db", required=True)
    serve.add_argument("--backend", choices=["sqlite", "elastic"], default="sqlite")
    sync = commands.add_parser("sync-elastic", help="publish an Elasticsearch/BGE-M3 snapshot")
    sync.add_argument("--db", required=True)
    sync.add_argument("--baseline", required=True)
    listing = commands.add_parser("list-clauses", help="navigate the exact document's clause addresses")
    for flag in ("db", "baseline", "spec", "version"):
        listing.add_argument("--" + flag, required=True)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    expansion = commands.add_parser("expand", help="follow pinned references with explicit budgets")
    expansion.add_argument("--db", required=True)
    expansion.add_argument("--baseline", required=True)
    expansion.add_argument("--seed", action="append", required=True, help="SPEC@VERSION:CLAUSE")
    collection = commands.add_parser("collect", help="search, read context and expand references")
    collection.add_argument("query")
    collection.add_argument("--db", required=True)
    collection.add_argument("--baseline", required=True)
    collection.add_argument("--subquery", action="append", default=[])
    collection.add_argument("--spec")
    collection.add_argument("--limit", type=int, default=20)
    collection.add_argument("--backend", choices=["sqlite", "elastic"], default="sqlite")
    for command in (expansion, collection):
        command.add_argument("--dependency-baseline", action="append", default=[])
        command.add_argument("--max-depth", type=int, default=2)
        command.add_argument("--max-nodes", type=int, default=60)
        command.add_argument("--max-chars", type=int, default=200000)
    evaluation = commands.add_parser("evaluate", help="measure recall against a provenance-labelled dataset")
    evaluation.add_argument("dataset")
    evaluation.add_argument("--db", required=True)
    evaluation.add_argument("--mode", choices=["single", "multi", "collect"], default="collect")
    evaluation.add_argument("--limit", type=int, default=20)
    evaluation.add_argument("--backend", choices=["sqlite", "elastic"], default="sqlite")
    scan = commands.add_parser("scan", help="scan all source blocks, without a ranking cutoff")
    scan.add_argument("--db", required=True)
    scan.add_argument("--baseline", required=True)
    scan.add_argument("--term", action="append", default=[])
    scan.add_argument("--all-term", action="append", default=[])
    scan.add_argument("--spec")
    scan.add_argument("--offset", type=int, default=0)
    scan.add_argument("--limit", type=int, default=100)
    audit = commands.add_parser("audit", help="export all evidence for an explicit pinned feature case")
    audit.add_argument("case")
    audit.add_argument("--db", required=True)
    audit.add_argument("--output", required=True, help="new JSON evidence file; existing files are never overwritten")
    review = commands.add_parser("review", help="check clause-by-clause analysis coverage and evidence provenance")
    review.add_argument("bundle")
    review.add_argument("review")
    heal = commands.add_parser("heal-import", help="repair a recorded DOCX parse failure and open a reviewed PR")
    heal.add_argument("manifest", help="locked manifest for the failed baseline")
    heal.add_argument("--db", required=True)
    heal.add_argument("--repo", default=".")
    heal.add_argument("--directory", default="evaluation/self-heal")
    heal.add_argument("--base", default="main")
    heal.add_argument("--remote", default="origin")
    heal.add_argument("--model", help="Codex model for repair; omit to use local default")
    heal.add_argument("--review-model", help="independent review model; omit to use repair model")
    heal.add_argument("--max-attempts", type=int, default=2, choices=(1, 2))
    heal.add_argument("--timeout", type=int, default=900, help="seconds allowed per agent/validation step")
    heal.add_argument("--no-pr", action="store_true", help="validate locally without pushing or opening a PR")
    for name in ("check-documents", "sync-documents"):
        command = commands.add_parser(name, help="check official Release versions and local document inventory" if name == "check-documents" else "download, ingest, embed and publish an updated document generation")
        command.add_argument("manifest", help="locked manifest; or @PATH to a managed current.json pointer")
        command.add_argument("--db", required=True)
        command.add_argument("--required-spec", action="append", default=[])
        command.add_argument("--dependency-baseline", action="append", default=[])
        command.add_argument("--include-references", action="store_true", help="also select undated/same-Release references for update")
        command.add_argument("--series", action="append", default=[], help="compare an entire official series, e.g. 29; may be expensive")
        command.add_argument("--output", help="new JSON report; never overwrite an existing report")
        if name == "sync-documents":
            document_sync = command
            command.add_argument("--directory", required=True, help="managed generation directory, with atomic current.json")
            command.add_argument("--lexical-only", action="store_true", help="explicitly skip embeddings; result reports not_requested")
            command.add_argument("--force", action="store_true", help="rebuild an otherwise current managed generation")
    for command in (search, sync, collection, evaluation, serve, document_sync):
        command.add_argument("--model", default="BAAI/bge-m3")
        command.add_argument("--device", default="cpu")
        command.add_argument("--index-prefix", default="3gpp-specs")
        command.add_argument("--rerank", action="store_true", help="rerank hybrid candidates with BGE-M3 ColBERT")
    return root


def _seed(text: str) -> dict:
    try:
        spec, rest = text.split("@", 1)
        version, clause = rest.split(":", 1)
    except ValueError as error:
        raise ValueError("seed must use SPEC@VERSION:CLAUSE") from error
    if not all((spec.strip(), version.strip(), clause.strip())):
        raise ValueError("seed must use SPEC@VERSION:CLAUSE")
    return {"spec": spec, "version": version, "clause": clause}


def _run_query(corpus, args, search_fn=None):
    if args.command == "search":
        return multi_search(corpus, args.query, args.baseline, subqueries=args.subquery,
                            limit=args.limit, spec=args.spec, search_fn=search_fn)
    if args.command == "collect":
        return collect_evidence(corpus, args.query, args.baseline, subqueries=args.subquery,
            dependencies=args.dependency_baseline, limit=args.limit, spec=args.spec, max_depth=args.max_depth,
            max_nodes=args.max_nodes, max_chars=args.max_chars, search_fn=search_fn)
    from spec_retriever.workflows.evaluation import evaluate

    return evaluate(corpus, args.dataset, mode=args.mode, limit=args.limit, search_fn=search_fn)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        from pathlib import Path

        # Consumers can follow the atomically published active generation.
        for field, pointer_key in (("baseline", "baseline"), ("manifest", "manifest")):
            value = getattr(args, field, None)
            if isinstance(value, str) and value.startswith("@"):
                pointer = json.loads(Path(value[1:]).read_text(encoding="utf-8"))
                if pointer.get("database") and hasattr(args, "db") and Path(pointer["database"]).resolve() != Path(args.db).resolve():
                    raise ValueError("active pointer belongs to a different database")
                setattr(args, field, pointer[pointer_key])
        if getattr(args, "output", None) and args.command in {"check-documents", "sync-documents"} and Path(args.output).exists():
            raise ValueError("report output already exists; choose a new filename")
        if args.command == "lock":
            manifest = lock_manifest(args.manifest, args.output)
            result = {"baseline": manifest.baseline, "manifest_sha256": manifest.digest,
                      "documents": len(manifest.documents), "output": args.output}
        elif args.command == "review":
            from spec_retriever.workflows.review import review_bundle

            result = review_bundle(args.bundle, args.review)
        elif args.command == "heal-import":
            from spec_retriever.workflows.self_heal import heal_import

            result = heal_import(args.manifest, args.db, repo=args.repo, directory=args.directory,
                                 base=args.base, remote=args.remote, model=args.model,
                                 review_model=args.review_model, max_attempts=args.max_attempts,
                                 publish_pr=not args.no_pr, timeout=args.timeout)
        elif args.command == "serve":
            from .mcp_server import create_server

            create_server(args.db, backend=args.backend, model=args.model, device=args.device,
                          prefix=args.index_prefix, rerank=args.rerank).run(transport="stdio")
            return 0
        else:
            with Corpus(args.db) as corpus:
                if args.command in {"check-documents", "sync-documents"}:
                    from spec_retriever.sync.service import check_documents, sync_documents

                    manifest = load_manifest(args.manifest)
                    options = dict(required_specs=args.required_spec, dependencies=args.dependency_baseline,
                                   include_references=args.include_references, series=args.series)
                    if args.command == "check-documents":
                        result = check_documents(corpus, manifest, **options)
                    elif args.lexical_only:
                        result = sync_documents(corpus, manifest, args.directory, lexical_only=True, force=args.force, **options)
                    else:
                        from spec_retriever.retrieval.elastic import BGEEmbedder, ElasticBackend, create_client, _fingerprint

                        client = create_client()
                        try:
                            # Load model lazily, after the source check/download stage.
                            def embed(store, baseline):
                                backend = ElasticBackend(client, BGEEmbedder(args.model, args.device), prefix=args.index_prefix)
                                return backend.sync(store, baseline)
                            def embedding_current(store, baseline):
                                import hashlib

                                alias = args.index_prefix + "-" + hashlib.sha256(baseline.encode()).hexdigest()[:16]
                                if not client.indices.exists_alias(name=alias):
                                    return False
                                mappings = client.indices.get_mapping(index=alias)
                                expected = _fingerprint(store.status(baseline), args.model)
                                return bool(mappings) and all(item["mappings"].get("_meta", {}).get("fingerprint") == expected
                                                              for item in mappings.values())
                            result = sync_documents(corpus, manifest, args.directory, embed=embed, force=args.force,
                                                    embedding_profile=args.model + ':' + args.index_prefix,
                                                    embedding_current=embedding_current, **options)
                        finally:
                            client.close()
                    if args.output:
                        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                        with Path(args.output).open("x", encoding="utf-8") as stream:
                            json.dump(result, stream, ensure_ascii=False, indent=2)
                elif args.command == "ingest":
                    result = corpus.ingest(load_manifest(args.manifest))
                elif args.command == "status":
                    result = corpus.status(args.baseline)
                elif args.command == "scan":
                    from spec_retriever.workflows.audit import scan_corpus

                    result = scan_corpus(corpus, args.baseline, any_terms=args.term, all_terms=args.all_term,
                                         spec=args.spec, offset=args.offset, limit=args.limit)
                elif args.command == "audit":
                    from pathlib import Path
                    from spec_retriever.workflows.audit import audit_case

                    output = Path(args.output)
                    if output.exists():
                        raise ValueError("audit output already exists; choose a new filename")
                    bundle = audit_case(corpus, args.case)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("x", encoding="utf-8") as stream:
                        json.dump(bundle, stream, ensure_ascii=False, indent=2)
                    result = {key: bundle[key] for key in ("case_id", "baseline", "case_sha256",
                        "manifest_sha256", "scan_complete", "scanned_block_count", "total_matches",
                        "feature_counts", "required_documents_ready", "required_document_gaps",
                        "section_selector_gaps", "scope_ready", "review_status")}
                    result["output"] = str(output)
                elif args.command == "clause":
                    result = corpus.get_clause(args.baseline, args.spec, args.version, args.clause,
                                               include_children=args.include_children)
                elif args.command == "list-clauses":
                    result = corpus.list_clauses(args.baseline, args.spec, args.version, offset=args.offset, limit=args.limit)
                elif args.command == "expand":
                    result = expand_references(corpus, args.baseline, [_seed(seed) for seed in args.seed],
                        dependencies=args.dependency_baseline, max_depth=args.max_depth,
                        max_nodes=args.max_nodes, max_chars=args.max_chars)
                elif args.command in {"search", "collect", "evaluate"} and args.backend == "sqlite":
                    result = _run_query(corpus, args)
                else:
                    from spec_retriever.retrieval.elastic import BGEEmbedder, ElasticBackend, create_client

                    client = create_client()
                    try:
                        backend = ElasticBackend(client, BGEEmbedder(args.model, args.device), prefix=args.index_prefix)
                        if args.command == "sync-elastic":
                            result = backend.sync(corpus, args.baseline)
                        else:
                            def search_fn(query, baseline, *, limit=20, spec=None):
                                return backend.search(corpus, query, baseline, limit=limit, spec=spec,
                                                      rerank=args.rerank, rerank_candidates=max(40, limit))
                            result = _run_query(corpus, args, search_fn)
                    finally:
                        client.close()
        displayed = result
        if args.command in {"check-documents", "sync-documents"} and args.output:
            displayed = {key: value for key, value in result.items() if key not in {"reference_inventory", "missing_references"}}
            displayed.update(output=args.output, missing_reference_count=len(result.get("missing_references", [])),
                             missing_reference_specs=sorted({row["spec"] for row in result.get("missing_references", [])}))
        print(json.dumps(displayed, ensure_ascii=False, indent=2))
        return 2 if ((args.command == "ingest" and not result["ingestion_complete"])
                     or (args.command == "heal-import" and result["status"] not in
                         {"pr_created", "validated", "no_parse_failure"})
                     or (args.command == "check-documents" and (not result["all_selected_current"] or result["missing_references"]))
                     or (args.command == "audit" and not result["scope_ready"])
                     or (args.command == "review" and (not result["review_coverage_complete"] or not result["scope_ready"]))) else 0
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired,
            json.JSONDecodeError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
