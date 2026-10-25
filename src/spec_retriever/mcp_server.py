"""Optional read-only MCP tools. Imports never start a server or load a model."""

from pathlib import Path
import logging
from threading import Lock

from spec_retriever.spec_store.store import Corpus
from spec_retriever.retrieval.service import multi_search, expand_references as expand_graph, collect_evidence as collect


def create_server(database: str | Path, *, backend: str = "sqlite", model: str = "BAAI/bge-m3",
                  device: str = "cpu", prefix: str = "3gpp-specs", rerank: bool = False,
                  active_pointer: str | Path | None = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as error:
        raise ValueError("install the 'mcp' extra to serve MCP tools") from error
    server = FastMCP("3gpp-spec-retriever")
    if backend not in {"sqlite", "elastic"}:
        raise ValueError("MCP backend must be sqlite or elastic")
    if backend == "elastic":
        # On Windows, importing the NumPy/PyTorch DLL stack after stdio's I/O
        # threads start can stall inside native extension initialization.
        # Import dependencies before the transport; weights remain lazy.
        try:
            import FlagEmbedding  # noqa: F401
        except ImportError as error:
            raise ValueError("install the 'elastic' extra for hybrid MCP retrieval") from error
    state = {}
    model_lock, inference_lock = Lock(), Lock()

    def resolve_baseline(baseline: str) -> str:
        if baseline != "current":
            return baseline
        if active_pointer is None:
            raise ValueError("current requires a configured active corpus pointer")
        import json
        pointer = json.loads(Path(active_pointer).read_text(encoding="utf-8"))
        if not pointer.get("complete") or Path(pointer["database"]).resolve() != Path(database).resolve():
            raise ValueError("active corpus pointer is incomplete or belongs to a different database")
        return pointer["baseline"]

    def elastic_backend():
        from spec_retriever.retrieval.elastic import BGEEmbedder, ElasticBackend, create_client

        with model_lock:
            if "backend" not in state:
                logger = logging.getLogger(__name__)
                logger.info("Loading hybrid retrieval model")
                encoder = BGEEmbedder(model, device)
                logger.info("Hybrid retrieval model ready")
                state["backend"] = ElasticBackend(create_client(), encoder, prefix=prefix)
            return state["backend"]

    def search_function(corpus):
        if backend == "sqlite":
            return None
        def search(query, baseline, *, limit=20, spec=None):
            with inference_lock:
                return elastic_backend().search(corpus, query, baseline, limit=limit, spec=spec,
                                                rerank=rerank, rerank_candidates=max(40, limit))
        return search

    @server.tool()
    async def search_specs(query: str, baseline: str, limit: int = 20, spec: str | None = None,
                           subqueries: list[str] | None = None) -> dict:
        """Search indexed text; inspect corpus_status and truncated before drawing conclusions."""
        from anyio import to_thread
        baseline = resolve_baseline(baseline)

        def operation():
            with Corpus(database) as corpus:
                return multi_search(corpus, query, baseline, limit=limit, spec=spec,
                                    subqueries=subqueries, search_fn=search_function(corpus))
        return await to_thread.run_sync(operation)

    @server.tool()
    async def scan_corpus(baseline: str, any_terms: list[str] | None = None,
                         all_terms: list[str] | None = None, sections: list[dict] | None = None,
                         spec: str | None = None, offset: int = 0, limit: int = 100) -> dict:
        """Scan all supported blocks, then paginate. Follow next_offset; matches require relevance review."""
        from anyio import to_thread
        from spec_retriever.workflows.audit import scan_corpus as scan
        baseline = resolve_baseline(baseline)

        def operation():
            with Corpus(database) as corpus:
                return scan(corpus, baseline, any_terms=any_terms, all_terms=all_terms,
                            sections=sections, spec=spec, offset=offset, limit=limit)
        return await to_thread.run_sync(operation)

    @server.tool()
    def get_clause(baseline: str, spec: str, version: str, clause: str,
                   include_children: bool = False) -> dict:
        """Read an exact versioned clause, including ordered tables and source XML locations."""
        baseline = resolve_baseline(baseline)
        with Corpus(database) as corpus:
            return corpus.get_clause(baseline, spec, version, clause, include_children=include_children)

    @server.tool()
    def get_corpus_status(baseline: str) -> dict:
        """Report manifest import failures and unsupported content; coverage is not assessed."""
        requested = baseline
        baseline = resolve_baseline(baseline)
        with Corpus(database) as corpus:
            result = corpus.status(baseline)
        if requested == "current" and active_pointer is not None:
            import json
            pointer = json.loads(Path(active_pointer).read_text(encoding="utf-8"))
            result["build_scope"] = pointer.get("scope")
            result["selected_count"] = pointer.get("selected_count")
            result["indexed_count"] = pointer.get("indexed_count")
            result["build_complete"] = pointer.get("complete", False)
            pending = Path(active_pointer).with_name("pending.json")
            if pending.is_file():
                run = Path(json.loads(pending.read_text(encoding="utf-8"))["run"])
                report = run / "report.json"
                if report.is_file():
                    current_build = json.loads(report.read_text(encoding="utf-8"))
                    result["pending_build"] = {key: current_build[key] for key in
                        ("run", "selected_count", "downloaded_count", "indexed_count", "failures", "complete")}
        return result

    @server.tool()
    def list_clauses(baseline: str, spec: str, version: str, offset: int = 0, limit: int = 100) -> dict:
        """Navigate pinned document headings; next_offset permits explicit pagination."""
        baseline = resolve_baseline(baseline)
        with Corpus(database) as corpus:
            return corpus.list_clauses(baseline, spec, version, offset=offset, limit=limit)

    @server.tool()
    def expand_references(baseline: str, seeds: list[dict[str, str]], dependencies: list[str] | None = None,
                          max_depth: int = 2, max_nodes: int = 40, max_chars: int = 200000) -> dict:
        """Follow exact pinned references; inspect unresolved, pending and truncated."""
        baseline = resolve_baseline(baseline)
        with Corpus(database) as corpus:
            return expand_graph(corpus, baseline, seeds, dependencies=dependencies, max_depth=max_depth,
                                max_nodes=max_nodes, max_chars=max_chars)

    @server.tool()
    async def collect_evidence(query: str, baseline: str, subqueries: list[str] | None = None,
                         dependencies: list[str] | None = None, limit: int = 20, spec: str | None = None,
                         max_depth: int = 2, max_nodes: int = 60, max_chars: int = 200000) -> dict:
        """Collect search hits, full clauses, context and reference graph within explicit budgets."""
        from anyio import to_thread
        baseline = resolve_baseline(baseline)

        def operation():
            with Corpus(database) as corpus:
                return collect(corpus, query, baseline, subqueries=subqueries, dependencies=dependencies,
                    limit=limit, spec=spec, max_depth=max_depth, max_nodes=max_nodes, max_chars=max_chars,
                    search_fn=search_function(corpus))
        return await to_thread.run_sync(operation)

    return server
