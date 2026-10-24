"""Serializable source evidence shared by both retrieval backends."""

from dataclasses import dataclass, field


@dataclass
class Clause:
    clause_id: str
    title: str
    title_path: list[str]
    parent: str | None
    ordinal: int
    annex_type: str | None = None
    blocks: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(block["text"] for block in self.blocks if block["text"])


@dataclass
class ParsedDocument:
    clauses: list[Clause]
    warnings: list[dict]


def make_chunks(clause: Clause, *, max_chars: int = 1250, overlap: int = 150) -> list[dict]:
    """Keep structured tables atomic; window long text with explicit offsets."""
    if max_chars < 100 or not 0 <= overlap < max_chars:
        raise ValueError("max_chars must be >= 100 and 0 <= overlap < max_chars")
    chunks = []
    for block in clause.blocks:
        text = block["text"]
        if not text:
            continue
        # Full table rows/cells remain available even when the table exceeds the
        # embedding window. Retrieval backends must report model truncation.
        step = max_chars - overlap
        starts = [0] if block["kind"] == "table" else range(0, len(text), step)
        for start in starts:
            end = len(text) if block["kind"] == "table" else min(start + max_chars, len(text))
            chunks.append({
                "text": text[start:end],
                "search_text": "\n".join([*clause.title_path, text[start:end]]),
                "location": block["location"],
                "char_start": start,
                "char_end": end,
                "kind": block["kind"],
            })
            if end == len(text):
                break
    if not chunks:
        # Empty parent clauses still carry a useful exact address and title.
        chunks.append({"text": clause.title, "search_text": "\n".join(clause.title_path),
                       "location": None, "char_start": 0, "char_end": len(clause.title), "kind": "heading"})
    return chunks
