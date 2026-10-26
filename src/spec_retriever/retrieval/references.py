"""Conservative reference extraction. Uncertain targets remain explicit gaps."""

from __future__ import annotations

import re
import json

from spec_retriever.spec_store.docx_parser import CLAUSE_NUMBER

SPEC = re.compile(r"\b(?:3GPP\s+)?(?:TS|TR)\s+(\d{2,3}\.\d{3})\b", re.I)
VERSION = re.compile(r"\b(?:V|version\s+)(\d+\.\d+\.\d+)\b", re.I)
RELEASE = re.compile(r"\b(?:Release|Rel[.-]?)\s*(\d+)\b", re.I)
LABEL = re.compile(r"\[(\d+[A-Za-z]?)\]")
ENTRY = re.compile(r"^\s*\[(\d+[A-Za-z]?)\]\s*(.*)$", re.S)
CLAUSE = re.compile(r"\b(?:subclauses?|clauses?|sections?)\s+(" + CLAUSE_NUMBER + r")\b(?![\w]|\.[A-Za-z0-9_])", re.I)
ANNEX_REF = re.compile(r"\bAnnex\s+([A-Z])\b", re.I)
TABLE_REF = re.compile(r"\bTable\s+((?:\d+[A-Z]?|[A-Z])(?:\.\d+[A-Z]?)*-\d+[A-Z]?)\b", re.I)


def _specification(text: str) -> dict:
    matches = list(SPEC.finditer(text))
    version = VERSION.search(text)
    release = RELEASE.search(text)
    if len({match.group(1) for match in matches}) != 1:
        return {"reason": "external_standard" if not matches else "ambiguous_specification", "raw": text}
    return {"spec": matches[0].group(1), "version": version.group(1) if version else None,
            "release": int(release.group(1)) if release else None, "raw": text}


def bibliography(clauses: list[dict]) -> dict[str, dict]:
    entries = {}
    current = None
    for clause in clauses:
        if not re.fullmatch(r"(?:normative\s+)?references", clause["title"].strip(), re.I):
            continue
        for block in clause["blocks"]:
            if block["kind"] == "heading":
                continue
            for line in block["text"].splitlines():
                match = ENTRY.match(line)
                if match:
                    current = match.group(1)
                    entries[current] = entries.get(current, "") + " " + match.group(2)
                elif current and line.strip():
                    entries[current] += " " + line.strip()
    return {key: _specification(value.strip()) for key, value in entries.items()}


def extract_references(clause: dict, *, source_spec: str, source_version: str,
                       references: dict[str, dict]) -> list[dict]:
    """Resolve paragraph scope only; multiple named specs are intentionally ambiguous."""
    found = []
    is_bibliography = bool(re.fullmatch(r"(?:normative\s+)?references", clause["title"].strip(), re.I))
    blocks = []
    for block in clause["blocks"]:
        if block["kind"] == "table":
            for row_index, row in enumerate(block.get("rows", [])):
                for column_index, cell in enumerate(row):
                    blocks.append({"kind": "cell", "text": cell["text"], "location": {
                        **block["location"], "row_index": row_index, "column_index": column_index}})
        else:
            blocks.append(block)
    for block in blocks:
        if block["kind"] == "heading":
            continue
        # Normative code blocks can contain [1] etc.; only known labels are
        # interpreted, with unknown citation-like labels separately reported.
        for text in re.split(r"\n+|(?<=[.;])\s+(?=[A-Z])", block["text"]):
            if not text.strip():
                continue
            if re.search(r"\b(?:subclauses?|clauses?|sections?)\s+\d+(?:\.[0-9A-Za-z]+)*\.x\b", text, re.I):
                found.append({"kind": "clause", "reason": "unsupported_reference_placeholder",
                              "raw": text, "location": block["location"]})
            anchors = []
            for match in SPEC.finditer(text):
                fragment = text[match.start():]
                next_spec = SPEC.search(fragment, match.end()-match.start())
                if next_spec:
                    fragment = fragment[:next_spec.start()]
                anchors.append(_specification(fragment))
            for match in LABEL.finditer(text):
                label = match.group(1)
                target = references.get(label)
                if target:
                    anchors.append({**target, "label": label})
                elif re.search(r"(?:see|refer|defined|specified|TS|TR)\b", text[:match.start()], re.I):
                    found.append({"kind": "citation", "reason": "unknown_reference_label", "label": label,
                                  "raw": text, "location": block["location"]})
            unique = {}
            for anchor in anchors:
                key = (anchor.get("spec"), anchor.get("version"), anchor.get("release"), anchor.get("reason"))
                unique[key] = anchor
            anchors = list(unique.values())
            # The inline name and its bibliography label name the same source.
            # Preserve a dated bibliography entry rather than treating an
            # unversioned inline spelling as a second conflicting target.
            dated = {anchor.get("spec") for anchor in anchors if anchor.get("version")}
            anchors = [anchor for anchor in anchors if anchor.get("spec") not in dated
                       or anchor.get("version") or anchor.get("reason") or anchor.get("release")]
            matches = [(match, "clause") for match in CLAUSE.finditer(text)]
            matches += [(match, "annex") for match in ANNEX_REF.finditer(text)]
            matches += [(match, "table") for match in TABLE_REF.finditer(text)
                        if not (match.start() == 0 and text[match.end():].lstrip().startswith(":"))]
            used_specs = set()
            for match, kind in matches:
                raw_id = match.group(1)
                targets = [raw_id]
                tail = text[match.end():]
                # A bounded list such as "clauses 5.1, 5.2 and 5.3".
                if kind == "clause":
                    while extra := re.match(r"^\s*(?:,\s*(?:and\s+)?|and\s+|or\s+)(" + CLAUSE_NUMBER + r")\b", tail, re.I):
                        targets.append(extra.group(1))
                        tail = tail[extra.end():]
                if re.match(r"^\s*(?:to\s+|through\s+|[–−-]\s*)(" + CLAUSE_NUMBER + r")\b", tail, re.I):
                    found.append({"kind": kind, "reason": "unsupported_clause_range", "raw": text,
                                  "location": block["location"]})
                    continue
                local = bool(re.search(r"^\s+(?:of|in)\s+(?:this|the present)\s+(?:specification|document)", tail, re.I))
                governed = re.match(r"^\s+(?:of|in)\s+(?:3GPP\s+)?(?:TS|TR)\s+(\d{2,3}\.\d{3})\b", tail, re.I)
                if governed and re.match(r"^\s*(?:\[\d+[A-Za-z]?\])?\s*(?:and|or|,)\s+(?:3GPP\s+)?(?:TS|TR)\b", tail[governed.end():], re.I):
                    governed = None  # One clause governed by a list of specs remains ambiguous.
                preceding = re.search(r"(?:TS|TR)\s+(\d{2,3}\.\d{3})(?:\s*\[\d+[A-Za-z]?\])?\s*[,;:]\s*$", text[:match.start()], re.I)
                explicit_spec = (governed or preceding).group(1) if governed or preceding else None
                explicit_targets = [anchor for anchor in anchors if anchor.get("spec") == explicit_spec] if explicit_spec else []
                external_governor = re.match(r"^\s+(?:of|in)\s+(?:IETF\s+)?(?:RFC\b|OpenAPI\b|ETSI\b)", tail, re.I)
                if external_governor:
                    target = {"reason": "external_standard"}
                elif explicit_spec and len(explicit_targets) == 1:
                    target = dict(explicit_targets[0])
                elif local or not anchors or (not explicit_spec and all(anchor.get("reason") == "external_standard" for anchor in anchors)):
                    target = {"spec": source_spec, "version": source_version}
                elif len(anchors) == 1:
                    target = dict(anchors[0])
                else:
                    found.append({"kind": kind, "reason": "ambiguous_target", "raw": text,
                                  "location": block["location"], "candidates": anchors})
                    continue
                for clause_id in targets:
                    used_specs.add(target.get("spec"))
                    found.append({**target, "kind": kind, "clause": clause_id.upper() if kind == "annex" else clause_id, "raw": text,
                                  "location": block["location"]})
            # Naming a document is useful evidence even when no clause was given.
            for anchor in anchors:
                if is_bibliography or anchor.get("spec") not in used_specs:
                    found.append({**anchor, "kind": "document", "clause": None, "raw": text,
                                  "location": block["location"]})
    return found


class ReferenceResolver:
    def __init__(self, corpus, baseline: str, dependencies: list[str] | None = None):
        if dependencies is not None and (not isinstance(dependencies, list)
                or not all(isinstance(name, str) and name.strip() for name in dependencies)):
            raise ValueError("dependencies must be a list of explicit baseline names")
        self.corpus, self.baseline = corpus, baseline
        self.baselines = list(dict.fromkeys([baseline, *(dependencies or [])]))
        if len(self.baselines) > 20:
            raise ValueError("at most 20 explicit baseline scopes are supported")
        for name in self.baselines:
            corpus.status(name)
        self._documents = {}
        self._symbols = None
        self._bibliographies = {}
        self._table_owners = {}
        self._clause_ids = {}
        self._candidates = {}

    def _records(self, baseline: str, spec: str, version: str) -> list[dict]:
        key = (baseline, spec, version)
        if key not in self._documents:
            rows = self.corpus.connection.execute("""SELECT c.payload FROM clauses c
                JOIN documents d ON d.id=c.document_id
                WHERE d.baseline=? AND d.spec=? AND d.version=? ORDER BY c.ordinal""", key)
            self._documents[key] = [json.loads(row["payload"]) for row in rows]
        return self._documents[key]

    def references_for(self, address: dict, clause: dict, *, include_types: bool = True) -> list[dict]:
        records = self._records(address["baseline"], address["spec"], address["version"])
        key = (address["baseline"], address["spec"], address["version"])
        if key not in self._bibliographies:
            self._bibliographies[key] = bibliography(records)
        found = extract_references(clause, source_spec=address["spec"], source_version=address["version"],
                                   references=self._bibliographies[key])
        if not include_types:
            return found
        if self._symbols is None:
            self._symbols = {}
            for baseline in self.baselines:
                for document in self.corpus.status(baseline)["documents"]:
                    if document["status"] != "indexed":
                        continue
                    for record in self._records(baseline, document["spec"], document["version"]):
                        match = re.match(r"^(?:Type|Enumeration)(?::\s*|\s+)([A-Za-z][A-Za-z0-9_]*)\b", record["title"])
                        if match:
                            target = {"baseline": baseline, "spec": document["spec"],
                                      "version": document["version"], "clause": record["clause_id"]}
                            self._symbols.setdefault(match.group(1), []).append(target)
                        if re.search(r"\bsimple\s+data\s+types?\b", record["title"], re.I):
                            for block in record["blocks"]:
                                rows = block.get("rows", [])
                                if not rows or not rows[0] or re.sub(r"[^a-z]", "", rows[0][0]["text"].lower()) not in {"datatype", "typename"}:
                                    continue
                                for row in rows[1:]:
                                    name = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)", row[0]["text"].strip()) if row and row[0].get("column_span", 1) == 1 else None
                                    if name:
                                        self._symbols.setdefault(name.group(1), []).append({"baseline": baseline,
                                            "spec": document["spec"], "version": document["version"], "clause": record["clause_id"]})
        # Datatype cells often name another definition without saying 'clause'.
        # Only typed columns and exact, case-sensitive definition names qualify.
        for block in clause["blocks"]:
            rows = block.get("rows", [])
            if block["kind"] != "table" or not rows:
                continue
            columns = [index for index, cell in enumerate(rows[0])
                       if re.sub(r"[^a-z]", "", cell["text"].lower()) in {"type", "datatype"}]
            for row_index, row in enumerate(rows[1:], 1):
                for column in columns:
                    if column >= len(row):
                        continue
                    text = row[column]["text"]
                    if row[column].get("column_span", 1) > 1 or re.fullmatch(r"\s*(?:n/?a|not applicable|-)\s*", text, re.I):
                        continue
                    for symbol in dict.fromkeys(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", text)):
                        if symbol.casefold() in {"string", "integer", "number", "boolean", "object", "array", "map",
                                "any", "null", "float", "double", "binary", "byte", "int32", "int64", "uint32", "uint64",
                                "oneof", "anyof", "allof", "enum", "enumeration"}:
                            continue
                        candidates = self._symbols.get(symbol, [])
                        local = [item for item in candidates if all(item[key] == address[key]
                                 for key in ("baseline", "spec", "version"))]
                        candidates = local or candidates
                        candidates = [dict(items) for items in dict.fromkeys(tuple(sorted(item.items())) for item in candidates)]
                        if not candidates and (re.match(r"^[A-Z]", symbol) or re.search(r"[a-z][A-Z]", symbol)):
                            found.append({"kind": "type", "reason": "type_definition_not_indexed", "symbol": symbol,
                                "raw": text, "location": {**block["location"], "row_index": row_index, "column_index": column}})
                        candidates = [item for item in candidates if item != address]
                        if not candidates:
                            continue
                        reference = {"kind": "type", "symbol": symbol, "raw": text,
                            "location": {**block["location"], "row_index": row_index, "column_index": column}}
                        if len(candidates) == 1:
                            reference.update(candidates[0])
                        else:
                            reference.update(reason="ambiguous_type_reference", candidates=candidates)
                        found.append(reference)
        return found

    def resolve(self, reference: dict, source: dict) -> dict:
        if reference.get("reason"):
            return {"status": "unresolved", "reason": reference["reason"]}
        spec = reference["spec"]
        if spec not in self._candidates:
            self._candidates[spec] = self.corpus.document_candidates(self.baselines, spec)
        entries = list(self._candidates[spec])
        if reference.get("baseline"):
            entries = [item for item in entries if item["baseline"] == reference["baseline"]]
        version = reference.get("version")
        release = reference.get("release")
        if version:
            entries = [item for item in entries if item["version"] == version]
        if release:
            entries = [item for item in entries if int(item["version"].split(".")[0]) == release]
        if not entries:
            return {"status": "unresolved", "reason": "version_not_pinned" if version or release else "document_not_pinned"}
        # An undated reference uses the source document's own pinned baseline
        # when present. Explicit dependency scopes supply missing specifications.
        preferred = [item for item in entries if item["baseline"] == source["baseline"]]
        if preferred:
            entries = preferred
        identities = {(item["baseline"], item["version"]) for item in entries}
        if len(identities) != 1:
            return {"status": "unresolved", "reason": "ambiguous_pinned_version",
                    "candidates": [{key: item[key] for key in ("baseline", "spec", "version")} for item in entries]}
        entry = entries[0]
        if entry["status"] != "indexed":
            return {"status": "unresolved", "reason": "document_not_indexed", "document_status": entry["status"]}
        target = {key: entry[key] for key in ("baseline", "spec", "version")}
        clause_id = reference.get("clause")
        if clause_id is None:
            return {"status": "document_only", "target": target,
                    "reason": "document_reference_requires_clause_selection"}
        if reference["kind"] == "table":
            key = (target["baseline"], spec, target["version"])
            if key not in self._table_owners:
                self._table_owners[key] = {}
                for record in self._records(**target):
                    for block in record["blocks"]:
                        match = re.match(r"^Table\s+([A-Za-z0-9.]+-\d+[A-Za-z]?)\s*:", block["text"], re.I)
                        if match:
                            self._table_owners[key].setdefault(match.group(1).casefold(), set()).add(record["clause_id"])
            owners = sorted(self._table_owners[key].get(clause_id.casefold(), set()))
            if len(owners) != 1:
                return {"status": "unresolved", "reason": "table_owner_not_unique", "candidates": owners}
            clause_id = owners[0]
        target["clause"] = clause_id
        document_id = self.corpus.connection.execute(
            "SELECT id FROM documents WHERE baseline=? AND spec=? AND version=?",
            (target["baseline"], spec, target["version"])).fetchone()
        clause_rows = (self.corpus.connection.execute(
            "SELECT clause_id FROM clauses WHERE document_id=? AND "
            "(clause_id=? OR substr(clause_id,1,length(?)+1)=? || '~')",
            (document_id["id"], clause_id, clause_id, clause_id)).fetchall() if document_id else [])
        if not clause_rows:
            return {"status": "unresolved", "reason": "clause_not_found", "target": target}
        if len(clause_rows) > 1:
            return {"status": "unresolved", "reason": "ambiguous_printed_clause_id", "target": target,
                    "candidates": [row["clause_id"] for row in clause_rows]}
        return {"status": "resolved", "target": target, "version_policy": "exact" if version else "pinned_baseline"}
