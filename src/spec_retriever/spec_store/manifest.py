"""Explicit corpus baselines. A lockfile pins bytes, not just Release names."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class DocumentSpec:
    spec: str
    version: str
    path: str
    source_url: str
    sha256: str | None = None


@dataclass(frozen=True)
class Manifest:
    baseline: str
    release: int
    documents: tuple[DocumentSpec, ...]
    directory: Path

    def document_path(self, document: DocumentSpec) -> Path:
        return (self.directory / document.path).resolve()

    def payload(self) -> dict:
        return {
            "schema_version": 1,
            "baseline": self.baseline,
            "release": self.release,
            "documents": [vars(item) for item in self.documents],
        }

    @property
    def digest(self) -> str:
        # Entry order does not change the identity of the pinned corpus.
        payload = self.payload()
        payload["documents"] = sorted(payload["documents"], key=lambda item: item["spec"])
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def load_manifest(path: str | Path, *, require_hash: bool = True) -> Manifest:
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("manifest schema_version must be 1")
    baseline = raw.get("baseline")
    if not isinstance(baseline, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", baseline):
        raise ValueError("baseline must be 1-100 letters, digits, dots, underscores or hyphens")
    release = raw.get("release")
    if type(release) is not int or release < 3:
        raise ValueError("release must be an integer >= 3")
    entries = raw.get("documents")
    if not isinstance(entries, list) or not entries:
        raise ValueError("documents must be a nonempty list")
    documents = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each document must be an object")
        for field in ("spec", "version", "path", "source_url"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"document {field} must be a nonempty string")
        if not re.fullmatch(r"\d{2,3}\.\d{3}(?:-\d+)?", item["spec"]):
            raise ValueError(f"invalid specification number: {item['spec']}")
        if not re.fullmatch(r"\d+\.\d+\.\d+", item["version"]):
            raise ValueError(f"invalid version: {item['version']}")
        if int(item["version"].split(".")[0]) != release:
            raise ValueError(f"version {item['version']} does not belong to Release {release}")
        if item["spec"] in seen:
            raise ValueError(f"baseline has multiple entries for specification {item['spec']}")
        seen.add(item["spec"])
        if Path(item["path"]).suffix.lower() != ".docx":
            raise ValueError("only DOCX input is supported; convert legacy DOC/PDF explicitly")
        url = urlparse(item["source_url"])
        if url.scheme not in {"https", "http"} or not url.netloc or url.username or url.password:
            raise ValueError("source_url must be an HTTP(S) URL without credentials")
        checksum = item.get("sha256")
        if checksum is not None and (not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("sha256 must contain 64 lowercase hexadecimal characters")
        if require_hash and checksum is None:
            raise ValueError("unlocked manifest: run 'spec-retriever lock' first")
        documents.append(DocumentSpec(**{key: item.get(key) for key in DocumentSpec.__dataclass_fields__}))
    return Manifest(baseline, release, tuple(documents), path.parent)


def lock_manifest(source: str | Path, output: str | Path) -> Manifest:
    manifest = load_manifest(source, require_hash=False)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("lock output already exists; choose a new filename")
    import os

    payload = manifest.payload()
    for document, item in zip(manifest.documents, payload["documents"]):
        document_path = manifest.document_path(document)
        checksum = sha256_file(document_path)
        if document.sha256 is not None and checksum != document.sha256:
            raise ValueError(f"checksum mismatch for {document.spec}")
        item["sha256"] = checksum
        item["path"] = os.path.relpath(document_path, output.parent).replace("\\", "/")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return load_manifest(output)
