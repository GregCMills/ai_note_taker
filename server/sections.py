"""A note as a list of sections, each starting at a heading and tagged with a hidden, stable id.

On disk every section is preceded by `<!-- §12 -->`, invisible when the markdown is rendered.
Text without a marker (new content, or a manual edit) is given fresh ids when the note is parsed.
"""

import hashlib
import re
from dataclasses import dataclass

MARKER = re.compile(r"^<!-- §(\d+) -->[ \t]*\n?", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    return len(text) * 2 // 7


@dataclass
class Section:
    id: int | None
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.text.encode()).hexdigest()[:12]

    @property
    def title(self) -> str:
        first = self.text.strip().splitlines()[0] if self.text.strip() else ""
        return first.lstrip("#").strip()[:80]


def split_at_headings(text: str) -> list[str]:
    starts = [m.start() for m in HEADING.finditer(text)]
    bounds = [0, *[s for s in starts if s > 0], len(text)]
    pieces = [text[a:b].strip("\n") for a, b in zip(bounds, bounds[1:])]
    return [p for p in pieces if p.strip()]


class Document:
    def __init__(self, sections: list[Section]):
        self.sections = sections

    @classmethod
    def parse(cls, raw: str, next_id_floor: int = 1) -> "Document":
        chunks: list[tuple[int | None, str]] = []
        matches = list(MARKER.finditer(raw))
        if not matches or matches[0].start() > 0:
            chunks.append((None, raw[: matches[0].start() if matches else len(raw)]))
        for match, following in zip(matches, [*matches[1:], None]):
            chunks.append((int(match.group(1)), raw[match.end(): following.start() if following else len(raw)]))

        sections: list[Section] = []
        seen: set[int] = set()
        for section_id, text in chunks:
            for index, piece in enumerate(split_at_headings(text)):
                keep = section_id if index == 0 and section_id not in seen else None
                if keep is not None:
                    seen.add(keep)
                sections.append(Section(keep, piece))

        next_id = max(max(seen, default=0) + 1, next_id_floor)
        for section in sections:
            if section.id is None:
                section.id = next_id
                next_id += 1
        return cls(sections)

    def serialize(self) -> str:
        return "\n".join(
            (f"<!-- §{s.id} -->\n" if s.id is not None else "") + s.text.strip("\n") + "\n" for s in self.sections
        )

    def normalised(self, next_id_floor: int = 1) -> "Document":
        """Re-parse so new or edited text is split at headings and given ids."""
        return Document.parse(self.serialize(), next_id_floor)

    def get(self, section_id: int) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def ids(self) -> set[int]:
        return {s.id for s in self.sections if s.id is not None}

    def tokens(self) -> int:
        return estimate_tokens(self.serialize())

    def render(self, collapsed: dict[int, str]) -> str:
        """The scribe's view: full sections, or a summary for ids in `collapsed`."""
        if not self.sections:
            return "(empty note)"
        blocks = []
        for s in self.sections:
            if s.id in collapsed:
                blocks.append(
                    f'<section id="{s.id}" collapsed>\n{s.title}\nSummary: {collapsed[s.id]}\n'
                    f"(call open_section({s.id}) for the full text)\n</section>"
                )
            else:
                blocks.append(f'<section id="{s.id}">\n{s.text.strip()}\n</section>')
        return "\n".join(blocks)


def strip_markers(raw: str) -> str:
    return MARKER.sub("", raw)
