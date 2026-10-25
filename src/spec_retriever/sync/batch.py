"""Resumable, all-series official Release corpus builds."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from uuid import uuid4

from spec_retriever.config import Config
from spec_retriever.spec_store.manifest import DocumentSpec, Manifest, load_manifest, sha256_file
from spec_retriever.spec_store.store import Corpus
from spec_retriever.sync.official import OfficialCatalog, download_archive, fetch_bytes
from spec_retriever.sync.service import convert_legacy_doc, extract_document


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _catalog_retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except (ValueError, OSError) as error:
            if attempt == 2 or "official fetch failed" not in str(error):
                raise
            time.sleep(2 ** attempt)


def discover(config: Config, *, catalog: OfficialCatalog | None = None,
             series: list[str] | None = None, workers: int = 6) -> dict:
    """Pin one newest archive per specification in the selected Release."""
    catalog = catalog or OfficialCatalog()
    numbers = sorted(set(series if series is not None else _catalog_retry(catalog.series)))
    errors: list[dict] = []

    def get_series(number: str):
        try:
            return number, _catalog_retry(lambda: catalog.series_specs(number, allow_empty=True)), None
        except (ValueError, OSError) as error:
            return number, [], str(error)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        series_rows = list(pool.map(get_series, numbers))
    specs = set()
    for number, found, error in series_rows:
        if error:
            errors.append({"series": number, "stage": "series_listing", "error": error})
        specs.update(found)

    def get_spec(spec: str):
        try:
            archive = _catalog_retry(lambda: catalog.latest(spec, config.release))
            return spec, archive, None
        except (ValueError, OSError) as error:
            return spec, None, str(error)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        spec_rows = []
        for count, row in enumerate(pool.map(get_spec, sorted(specs)), 1):
            spec_rows.append(row)
            if count % 100 == 0:
                print(f"Checked {count}/{len(specs)} official specification directories", file=sys.stderr)
    selected, excluded = [], []
    for spec, archive, error in spec_rows:
        if error:
            # A directory with no canonical versioned ZIP cannot establish
            # whether it has a Release 17 document. Never call that complete.
            errors.append({"spec": spec, "stage": "version_listing", "error": error})
        elif archive is None:
            excluded.append({"spec": spec, "reason": "no_version_in_release"})
        else:
            selected.append({"spec": spec, "version": archive.version, "source_url": archive.url})
    return {"release": config.release, "scope": "all" if series is None else "series_subset",
            "series": numbers, "checked_at": catalog.checked_at,
            "documents": selected, "excluded": excluded, "discovery_errors": errors,
            "selected_count": len(selected), "excluded_count": len(excluded),
            "discovered_spec_count": len(specs)}


def _resume_source(run: Path, item: dict) -> DocumentSpec | None:
    folder = run / "sources" / item["spec"]
    metadata = folder / "source.json"
    if not metadata.is_file():
        return None
    try:
        saved = json.loads(metadata.read_text(encoding="utf-8"))
        path = (run / saved["path"]).resolve()
        if (saved["spec"] != item["spec"] or saved["version"] != item["version"]
                or saved["source_url"] != item["source_url"]
                or path.parent != folder.resolve() or path.name != "source.docx" or not path.is_file()
                or sha256_file(path) != saved["sha256"]):
            return None
        return DocumentSpec(item["spec"], item["version"], saved["path"],
                            item["source_url"], saved["sha256"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _acquire(run: Path, item: dict, fetch) -> DocumentSpec:
    folder = run / "sources" / item["spec"]
    folder.mkdir(parents=True, exist_ok=True)
    archive_path = folder / "official.zip"
    if fetch is fetch_bytes:
        for attempt in range(3):
            archive_path.unlink(missing_ok=True)
            try:
                download_archive(item["source_url"], archive_path)
                break
            except ValueError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        archive = archive_path
        archive_hash = sha256_file(archive_path)
    else:
        archive = fetch(item["source_url"])
        archive_hash = hashlib.sha256(archive).hexdigest()
    # A failed attempt may leave a source file. Move it aside only after a new
    # official download succeeds; never trust an unverified partial source.
    for name in ("source.docx", "source.doc"):
        (folder / name).unlink(missing_ok=True)
    path, provenance = extract_document(archive, item["spec"], folder)
    archive_path.unlink(missing_ok=True)
    if path.suffix == ".doc":
        path = convert_legacy_doc(path)
        provenance["conversion"] = "legacy_doc_to_docx"
    source_hash = sha256_file(path)
    relative = path.relative_to(run).as_posix()
    _write_json(folder / "source.json", {**item, "path": relative, "sha256": source_hash,
                                        "archive_sha256": archive_hash, **provenance})
    return DocumentSpec(item["spec"], item["version"], relative,
                        item["source_url"], source_hash)


def build(config: Config, *, dry_run: bool = False, refresh: bool = False,
          catalog: OfficialCatalog | None = None, fetch=fetch_bytes,
          series: list[str] | None = None, workers: int = 6) -> dict:
    """Build a pinned corpus and publish it only after full selected coverage."""
    root = config.storage_root
    if dry_run:
        return discover(config, catalog=catalog, series=series, workers=workers)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".build.lock"
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError as error:
        raise ValueError(f"another build may be active; inspect {lock}") from error
    try:
        pending_path = root / "pending.json"
        if pending_path.is_file() and not refresh:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            run = Path(pending["run"]).resolve()
            if not run.is_relative_to(root.resolve()) or not run.is_dir():
                raise ValueError("pending build path is outside storage_root or missing")
            inventory = json.loads((run / "inventory.json").read_text(encoding="utf-8"))
            if inventory["release"] != config.release or (series is None) != (inventory["scope"] == "all"):
                raise ValueError("pending inventory has a different release or scope; use --refresh")
        else:
            inventory = discover(config, catalog=catalog, series=series, workers=workers)
            run = root / "runs" / (f"r{config.release}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                                   + "-" + uuid4().hex[:8])
            run.mkdir(parents=True)
            _write_json(run / "inventory.json", inventory)
            _write_json(pending_path, {"run": str(run), "release": config.release})

        documents: list[DocumentSpec] = []
        failures = list(inventory["discovery_errors"])
        for count, item in enumerate(inventory["documents"], 1):
            if shutil.disk_usage(root).free < 4 * 1024 ** 3:
                failures.append({"stage": "storage", "error": "less than 4 GiB free; build stopped"})
                break
            saved = _resume_source(run, item)
            if saved is not None:
                documents.append(saved)
            else:
                try:
                    documents.append(_acquire(run, item, fetch))
                except (ValueError, OSError, RuntimeError) as error:
                    failures.append({"spec": item["spec"], "stage": "download_or_extract", "error": str(error)})
            if count % 25 == 0:
                _write_json(run / "progress.json", {"selected_count": inventory["selected_count"],
                    "processed_count": count, "downloaded_count": len(documents), "failures": failures})
                print(f"Acquired {count}/{inventory['selected_count']} Release {config.release} documents", file=sys.stderr)

        baseline = None
        ingestion = None
        if documents:
            preliminary = Manifest("pending", config.release, tuple(documents), run)
            baseline = f"r{config.release}-all-{preliminary.digest[:16]}"
            manifest = Manifest(baseline, config.release, tuple(documents), run)
            lockfile = run / "baseline.lock.json"
            _write_json(lockfile, manifest.payload())
            with Corpus(config.database) as corpus:
                ingestion = corpus.ingest_resumable(load_manifest(lockfile))
            for row in ingestion["documents"]:
                if row["status"] != "indexed":
                    failures.append({"spec": row["spec"], "stage": "ingest", "error": row.get("error")})
        complete = (bool(documents) and not failures and len(documents) == inventory["selected_count"]
                    and ingestion is not None and ingestion["ingestion_complete"])
        report = {"release": config.release, "scope": inventory["scope"],
                  "inventory": str(run / "inventory.json"), "database": str(config.database),
                  "run": str(run), "baseline": baseline, "selected_count": inventory["selected_count"],
                  "downloaded_count": len(documents),
                  "indexed_count": sum(row["status"] == "indexed" for row in ingestion["documents"]) if ingestion else 0,
                  "failures": failures, "complete": complete, "published": False}
        _write_json(run / "report.json", report)
        if complete:
            if config.retrieval == "elastic":
                from spec_retriever.retrieval.elastic import BGEEmbedder, ElasticBackend, create_client
                client = create_client()
                try:
                    with Corpus(config.database) as corpus:
                        ElasticBackend(client, BGEEmbedder("BAAI/bge-m3", "cpu")).sync(corpus, baseline)
                finally:
                    client.close()
            pointer = {**report, "manifest": str(run / "baseline.lock.json"),
                       "manifest_sha256": manifest.digest, "active_pointer": str(config.active_pointer),
                       "published": True}
            _write_json(config.active_pointer, pointer)
            report["published"] = True
            _write_json(run / "report.json", report)
            pending_path.unlink()
        return report
    finally:
        lock.unlink()
