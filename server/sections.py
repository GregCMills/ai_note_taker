"""A note as a list of sections, each starting at a heading and tagged with a hidden, stable id.

On disk every section is preceded by `<!-- §12 -->`, invisible when the markdown is rendered.
Text without a marker (new content, or a manual edit) is given fresh ids when the note is parsed.
"""

import hashlib
import re
from dataclasses import dataclass

MARKER = re.compile(r"^<!-- §(\d+) -->[ \t]*\n?", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}\s", re.MULTILINE)
LINE_NUMBER = re.compile(r"^\[\d+\] ?", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    return len(text) * 2 // 7


# Every line with text in a note has a number, counted from 1 per note; blank lines and section markers don't count.
# The app shows the same numbers beside the note (web/src/highlight.ts), so the user can say "delete line 23".


def count_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


def number_lines(text: str, first: int) -> str:
    """Prefix each line with text with its number, e.g. "[23] It was raining."."""
    numbered, line = [], first
    for row in text.split("\n"):
        if row.strip():
            row, line = f"[{line}] {row}", line + 1
        numbered.append(row)
    return "\n".join(numbered)


def strip_line_numbers(text: str) -> str:
    """Remove line numbers the scribe copied from its view of the note; they are not part of the text."""
    return LINE_NUMBER.sub("", text)


@dataclass
class Collapse:
    """How a section is shown to the scribe in a long project: a summary of all of it (keep_from None), or a
    summary of its earlier part followed by the text from `keep_from` onwards, word for word."""

    summary: str
    keep_from: int | None = None


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

    def body_start(self) -> int:
        """Where the text after the heading line starts (0 if the section has no heading)."""
        if not self.text.lstrip().startswith("#"):
            return 0
        newline = self.text.find("\n")
        return len(self.text) if newline == -1 else newline + 1

    def tail_start(self, keep_tokens: int) -> int | None:
        """The earliest break after which at most `keep_tokens` of text remain, so the end of the section can be
        kept word for word while the part before it is summarised. Breaks are line or paragraph ends, or sentence
        ends inside a long paragraph. None if there is none."""
        start = self.body_start()
        for match in re.finditer(r"\n\s*\n|\n|(?<=[.!?][\"'”’)])\s+|(?<=[.!?])\s+", self.text[start:]):
            position = start + match.end()
            if estimate_tokens(self.text[position:]) <= keep_tokens and estimate_tokens(self.text[start:position]) > 0:
                return position
        return None


def split_at_headings(text: str) -> list[str]:
    starts = [m.start() for m in HEADING.finditer(text)]
    bounds = [0, *[s for s in starts if s > 0], len(text)]
    pieces = [text[a:b].strip("\n") for a, b in zip(bounds, bounds[1:])]
    return [p for p in pieces if p.strip()]


class Document:
    def __init__(self, sections: list[Section]):
        self.sections = sections

    @classmethod
    def parse(cls, raw: str, next_id_floor: int = 1, assign: bool = True) -> "Document":
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

        if not assign:
            return cls(sections)
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

    def first_line(self, section: Section) -> int:
        """The line number of the section's first line of text."""
        return 1 + sum(count_lines(s.text) for s in self.sections[: self.sections.index(section)])

    def locate_line(self, number: int) -> tuple[Section, int] | None:
        """The section holding line `number`, and that line's row in `section.text.split("\\n")`."""
        line = 1
        for section in self.sections:
            for row, text in enumerate(section.text.split("\n")):
                if text.strip():
                    if line == number:
                        return section, row
                    line += 1
        return None

    def line_count(self) -> int:
        return sum(count_lines(s.text) for s in self.sections)

    def delete_lines(self, numbers: set[int]) -> list[tuple[int, Section, str]]:
        """Remove the numbered lines, dropping sections left empty. Returns (number, section, text) per line removed."""
        removed, line = [], 1
        for section in self.sections:
            kept = []
            for row in section.text.split("\n"):
                if row.strip():
                    if line in numbers:
                        removed.append((line, section, row.strip()))
                        row = None
                    line += 1
                if row is not None:
                    kept.append(row)
            section.text = re.sub(r"\n\s*\n(\s*\n)+", "\n\n", "\n".join(kept)).strip("\n")
        self.sections = [s for s in self.sections if s.text.strip()]
        return removed

    def render(self, collapsed: dict[int, Collapse]) -> str:
        """The scribe's view, with line numbers: full sections, or summaries (of all or the top part of a section) for
        ids in `collapsed`."""
        if not self.sections:
            return "(empty note)"
        blocks, line = [], 1
        for s in self.sections:
            fold = collapsed.get(s.id)
            first, line = line, line + count_lines(s.text)
            if fold is not None and fold.keep_from is None:
                blocks.append(
                    f'<section id="{s.id}" collapsed>\n{s.title}\nSummary: {fold.summary}\n'
                    f"(call open_section({s.id}) for the full text)\n</section>"
                )
            elif fold is not None:
                heading = number_lines(s.text[: s.body_start()].strip(), first)
                before = s.text[: fold.keep_from]
                # The kept text can start partway through a line (at a sentence end), which keeps that line's number.
                mid_line = before.split("\n")[-1].strip() != ""
                tail = number_lines(s.text[fold.keep_from:].strip(), first + count_lines(before) - mid_line)
                blocks.append(
                    f'<section id="{s.id}" partly-collapsed>\n{heading}\n'
                    f"[Earlier part of this section, summarised: {fold.summary} (call open_section({s.id}) to read it in full)]\n"
                    f"{tail}\n</section>"
                )
            else:
                blocks.append(f'<section id="{s.id}">\n{number_lines(s.text.strip(), first)}\n</section>')
        return "\n".join(blocks)


def strip_markers(raw: str) -> str:
    return MARKER.sub("", raw)


def number_sections(docs: list[Document], next_id_floor: int) -> int:
    """Give every new or duplicated section an id unique across a project's notes. Returns the next free id."""
    seen: set[int] = set()
    for doc in docs:
        for section in doc.sections:
            if section.id is None or section.id in seen:
                section.id = None
            else:
                seen.add(section.id)
    next_id = max(max(seen, default=0) + 1, next_id_floor)
    for doc in docs:
        for section in doc.sections:
            if section.id is None:
                section.id = next_id
                next_id += 1
    return next_id
