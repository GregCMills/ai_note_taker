"""The three agents behind a note.

- Scribe: runs on every batch of speech and edits the note section by section.
- Historian: handles undo / redo / "go back to..." through git. The scribe calls it with ask_history,
  so the git tools never clutter the scribe's context.
- Librarian: the context manager. Writes the section summaries shown in place of collapsed sections,
  and the running summary that replaces old conversation turns.
"""

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from . import config
from .memory import MemoryStore, NoteMemory, SectionInfo
from .notes import NoteError, NoteStore
from .sections import Document, Section, estimate_tokens, split_at_headings

log = logging.getLogger(__name__)

SCRIBE_RULES = """
## How this works
Each message gives you the current note, its recent changes, new speech from the user (transcribed automatically, numbered S1, S2, ...), and the user's saved instructions for this note. Update the note to reflect the new speech, then reply with one short line saying what you changed.

The note is split into sections with ids. In a long note some sections are collapsed to a summary: call open_section before editing one of those.

- Change the note only with tools: edit_section for small changes, write_section to add, rewrite or delete a whole section.
- A section is one heading plus its content. When adding a section, write only that section: never repeat the note's title or other sections.
- Transcription can mishear words. Fix them from context.
- Commands about the notes are not content. Never write them into the note.
  - Undo, redo, "scrap that", "go back to before X", "put it back": call ask_history with just that request, then make any other changes yourself.
  - Corrections ("no, I meant Tuesday") are ordinary edits.
  - Guidance on how to take notes ("put action items first", "keep it brief"): save it with set_instructions, merged with the existing instructions.
- If the new speech adds nothing (filler, false starts, noise), change nothing and reply "no change".
- Never invent content the user did not say.
"""

HISTORIAN_INSTRUCTIONS = """You manage the edit history of one markdown note, kept in git. Every change is a commit whose subject lists the speech segments (S1, S2, ...) that caused it and what was done; the body holds the speech itself.

You receive a request from the note-taking assistant, such as "undo that" or "go back to before I talked about food". Find the right commit from the list given (use git_log or git_show if you need more), then call restore yourself. Do not describe what you would do: do it.

- restore sets the note back to exactly how it was at a commit. It adds a new commit and never deletes history, so redo is just restoring to the commit from before the undo.
- "Undo that" usually means restoring to the commit before the most recent change. Skip commits whose subject says "no change".
- If the user wants only part of an earlier change removed while keeping what came after, do not restore. Reply saying what should be removed so the assistant can edit it.
- Reply with one short line: what you restored to, or why you did not."""

LIBRARIAN_INSTRUCTIONS = """You compress context for a note-taking assistant. Be faithful and specific: keep names, numbers, dates and decisions. Never add anything. No preamble."""


def describe_commit(commit: dict) -> str:
    speech = " / ".join(line for line in commit["body"].splitlines() if line.startswith("S"))
    line = f"{commit['short']} {commit['subject']}"
    return f"{line}\n    speech: {speech[:300]}" if speech else line


def describe_turn(turn: list[ModelMessage]) -> str:
    lines = []
    for message in turn:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                lines.append(f"User: {part.content}")
            elif isinstance(part, ToolCallPart):
                lines.append(f"Assistant called {part.tool_name}({json.dumps(part.args_as_dict())[:200]})")
            elif isinstance(part, ToolReturnPart):
                lines.append(f"  -> {str(part.content)[:200]}")
            elif isinstance(part, TextPart) and part.content.strip():
                lines.append(f"Assistant: {part.content.strip()}")
    return "\n".join(lines)


def _shorten(value, limit: int):
    if isinstance(value, str) and len(value) > limit:
        return f"{value[: limit // 2]}… ({len(value)} chars)"
    return value


def compact_turn(messages: list[ModelMessage], speech: str) -> list[ModelMessage]:
    """What is kept of a turn for later context: the speech instead of the full prompt, long arguments trimmed."""
    compacted: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            parts = []
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    part = replace(part, content=f"New speech:\n{speech}")
                elif isinstance(part, ToolReturnPart):
                    part = replace(part, content=_shorten(str(part.content), 400))
                parts.append(part)
            compacted.append(replace(message, parts=parts, instructions=None))
        elif isinstance(message, ModelResponse):
            parts = []
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    part = replace(part, args={k: _shorten(v, 240) for k, v in part.args_as_dict().items()})
                    parts.append(part)
                elif isinstance(part, TextPart):
                    parts.append(part)
            compacted.append(replace(message, parts=parts))
    return compacted


@dataclass
class ScribeDeps:
    path: str
    doc: Document
    memory: NoteMemory
    collapsed: dict[int, str]
    system_prompt: str
    on_change: Callable[[], Awaitable[None]]
    actions: list[str] = field(default_factory=list)


@dataclass
class HistorianDeps:
    path: str
    restored: str | None = None


class NoteAgents:
    def __init__(self, notes: NoteStore, memories: MemoryStore):
        self.notes = notes
        self.memories = memories
        self.model = OpenAIChatModel(
            config.LLM_MODEL,
            provider=OpenAIProvider(base_url=config.LLM_URL, api_key=config.LLM_API_KEY or "none"),
        )
        self.settings = ModelSettings(temperature=config.LLM_TEMPERATURE)
        self.scribe = self._build_scribe()
        self.historian = self._build_historian()
        self.librarian = Agent(self.model, instructions=LIBRARIAN_INSTRUCTIONS, model_settings=self.settings)

    async def process(
        self, path: str, segments: list[dict], system_prompt: str, on_change: Callable[[], Awaitable[None]]
    ) -> tuple[str | None, str]:
        """Fold new speech into the note. Returns (commit sha or None, one-line summary)."""
        memory = self.memories.load(path)
        memory.turn += 1
        doc = self._load_doc(path, memory)
        self.notes.write(path, doc.serialize())

        await self._compact_history(memory)
        collapsed = await self._collapse(doc, memory)
        deps = ScribeDeps(path, doc, memory, collapsed, system_prompt, on_change)
        speech = "\n".join(f"S{s['id']}: {s['text']}" for s in segments)
        commits = "\n".join(describe_commit(c) for c in self.notes.log(path, config.RECENT_COMMITS_IN_CONTEXT))
        prompt = (
            f'The note "{path}" currently reads:\n<note>\n{doc.render(collapsed)}\n</note>\n\n'
            f"Recent changes (newest first; ask_history can undo or restore any of these):\n{commits}\n\n"
            f"Update the document as per this transcription:\n{speech}\n\n"
            f"Remember this, the user's instructions for this note:\n{memory.instructions or '(none yet)'}"
        )

        history = memory.history()
        with capture_run_messages() as messages:
            try:
                result = await self.scribe.run(
                    prompt,
                    deps=deps,
                    message_history=history,
                    usage_limits=UsageLimits(request_limit=config.LLM_MAX_STEPS),
                )
                summary = result.output.strip()
                turn = result.new_messages()
            except UsageLimitExceeded:
                log.warning("Scribe hit the step limit on %s", path)
                summary = "stopped after too many steps"
                turn = [*messages[len(history):], ModelResponse(parts=[TextPart(summary)])]
        memory.turns.append(compact_turn(turn, speech))

        self.memories.save(path, memory)
        ids = " ".join(f"S{s['id']}" for s in segments)
        subject = f"[{ids}] {'; '.join(deps.actions) or 'no change'}"
        sha = self.notes.commit(path, f"{subject}\n\n{summary}\n\n{speech}")
        return sha, summary or "; ".join(deps.actions) or "no change"

    # ---- context management -------------------------------------------------------------------

    def _load_doc(self, path: str, memory: NoteMemory) -> Document:
        doc = Document.parse(self.notes.read(path), memory.next_section_id)
        memory.next_section_id = max(memory.next_section_id, max(doc.ids(), default=0) + 1)
        return doc

    async def _collapse(self, doc: Document, memory: NoteMemory) -> dict[int, str]:
        """Collapse the least recently touched sections to summaries until the note fits its budget."""
        total = doc.tokens()
        if total <= config.NOTE_TOKEN_BUDGET:
            return {}
        target = int(config.NOTE_TOKEN_BUDGET * 0.75)
        collapsed: dict[int, str] = {}
        oldest_first = sorted(doc.sections, key=lambda s: memory.sections.get(s.id, SectionInfo()).touched)
        for section in oldest_first:
            if total <= target:
                break
            if memory.sections.get(section.id, SectionInfo()).touched >= memory.turn - 1:
                continue
            summary = await self._section_summary(section, memory)
            saved = estimate_tokens(section.text) - estimate_tokens(summary) - 25
            if saved > 0:
                collapsed[section.id] = summary
                total -= saved
        return collapsed

    async def _section_summary(self, section: Section, memory: NoteMemory) -> str:
        info = memory.sections.setdefault(section.id, SectionInfo())
        if info.digest != section.digest or not info.summary:
            result = await self.librarian.run(
                "Summarise this note section in at most two sentences, so an assistant knows what it "
                f"contains and when it would need to open it:\n\n{section.text}"
            )
            info.summary, info.digest = result.output.strip(), section.digest
        return info.summary

    async def _compact_history(self, memory: NoteMemory) -> None:
        """Fold older turns into the running summary once the history passes its token budget."""
        sizes = [estimate_tokens(describe_turn(turn)) for turn in memory.turns]
        if sum(sizes) <= config.HISTORY_TOKEN_BUDGET:
            return
        keep = min(config.KEEP_RECENT_TURNS, len(memory.turns))
        while keep > 2 and sum(sizes[-keep:]) > config.HISTORY_TOKEN_BUDGET * 0.6:
            keep -= 1
        old, recent = memory.turns[:-keep], memory.turns[-keep:]
        if not old:
            return
        transcript = "\n\n".join(describe_turn(turn) for turn in old)
        result = await self.librarian.run(
            "Below is the running summary of a note-taking session, then older turns that are leaving the "
            "assistant's context. Write an updated summary of the conversation, under 200 words. The assistant "
            "always sees the full note separately, so do not restate the note's contents. Cover: the order "
            "topics came up in, corrections and changes of mind, undo/redo requests and what they affected "
            "(by section id), and anything the user said they would come back to.\n\n"
            f"Current summary:\n{memory.history_summary or '(none)'}\n\nOlder turns:\n{transcript}"
        )
        memory.history_summary = result.output.strip()
        memory.turns = recent
        log.info("Summarised %d old turns", len(old))

    # ---- scribe -------------------------------------------------------------------------------

    def _build_scribe(self) -> Agent[ScribeDeps, str]:
        scribe = Agent(self.model, deps_type=ScribeDeps, model_settings=self.settings)

        @scribe.instructions
        def instructions(ctx: RunContext[ScribeDeps]) -> str:
            text = ctx.deps.system_prompt + "\n" + SCRIBE_RULES
            if ctx.deps.memory.history_summary:
                text += f"\n## Earlier in this session\n{ctx.deps.memory.history_summary}\n"
            return text

        @scribe.tool
        async def edit_section(ctx: RunContext[ScribeDeps], section_id: int, old_text: str, new_text: str) -> str:
            """Replace exact text inside one section. old_text must appear exactly once in it; use an empty new_text to delete.

            Args:
                section_id: The section to edit.
                old_text: Exact text currently in the section.
                new_text: Replacement text.
            """
            deps = ctx.deps
            section = deps.doc.get(section_id)
            if section is None:
                return f"Error: no section {section_id}. Sections: {sorted(deps.doc.ids())}"
            if section_id in deps.collapsed:
                return f"Error: section {section_id} is collapsed. Call open_section({section_id}) first."
            count = section.text.count(old_text) if old_text else 0
            if count != 1:
                return f"Error: old_text appears {count} times in section {section_id}; it must match exactly once."
            section.text = section.text.replace(old_text, new_text, 1)
            added = await self._save(deps, f"edit §{section_id}", [section_id])
            return "OK" + (f". New sections created from added headings: {added}" if added else "")

        @scribe.tool
        async def write_section(
            ctx: RunContext[ScribeDeps], content: str, section_id: int | None = None, after_section_id: int | None = None
        ) -> str:
            """Add, rewrite or delete a whole section (heading included).

            Args:
                content: Markdown for this one section, starting with its own heading (e.g. "## Food"). Empty deletes the section.
                section_id: The section to rewrite or delete. Omit to add a new section.
                after_section_id: Where to add a new section. Omit to add at the end.
            """
            deps = ctx.deps
            others = {s.text.strip() for s in deps.doc.sections if s.id != section_id}
            content = "\n\n".join(p for p in split_at_headings(content) if p.strip() not in others)
            if section_id is not None:
                section = deps.doc.get(section_id)
                if section is None:
                    return f"Error: no section {section_id}. Sections: {sorted(deps.doc.ids())}"
                if not content.strip():
                    deps.doc.sections.remove(section)
                    deps.collapsed.pop(section_id, None)
                    await self._save(deps, f"delete §{section_id}", [])
                    return "OK, deleted"
                section.text = content
                deps.collapsed.pop(section_id, None)
                added = await self._save(deps, f"rewrite §{section_id}", [section_id])
                return "OK" + (f". New sections created from added headings: {added}" if added else "")

            if not content.strip():
                return "Error: content is empty, or only repeats existing sections"
            position = len(deps.doc.sections)
            if after_section_id is not None:
                anchor = deps.doc.get(after_section_id)
                if anchor is None:
                    return f"Error: no section {after_section_id}. Sections: {sorted(deps.doc.ids())}"
                position = deps.doc.sections.index(anchor) + 1
            deps.doc.sections.insert(position, Section(None, content))
            added = await self._save(deps, "add", [])
            return f"OK, added as section {', '.join(map(str, added))}"

        @scribe.tool
        def open_section(ctx: RunContext[ScribeDeps], section_id: int) -> str:
            """Show the full text of a collapsed section so it can be edited.

            Args:
                section_id: The section to open.
            """
            section = ctx.deps.doc.get(section_id)
            if section is None:
                return f"Error: no section {section_id}. Sections: {sorted(ctx.deps.doc.ids())}"
            ctx.deps.collapsed.pop(section_id, None)
            ctx.deps.memory.touch(section_id)
            return section.text

        @scribe.tool
        async def set_instructions(ctx: RunContext[ScribeDeps], instructions: str) -> str:
            """Save the user's guidance for how to take notes in this note. Replaces the saved instructions, so include anything still relevant from before.

            Args:
                instructions: The complete instructions to remember.
            """
            ctx.deps.memory.instructions = instructions.strip()
            ctx.deps.actions.append("set instructions")
            await ctx.deps.on_change()
            return "OK, saved"

        @scribe.tool
        async def ask_history(ctx: RunContext[ScribeDeps], request: str) -> str:
            """Ask the history keeper to undo, redo, or go back to an earlier version of the note.

            Args:
                request: What the user wants, in their words plus any detail you know.
            """
            deps = ctx.deps
            commits = "\n".join(describe_commit(c) for c in self.notes.log(deps.path, 10))
            historian = HistorianDeps(deps.path)
            result = await self.historian.run(
                f"Request: {request}\n\nRecent commits (newest first):\n{commits}",
                deps=historian,
                usage_limits=UsageLimits(request_limit=6),
            )
            if not historian.restored:
                return result.output
            deps.doc = self._load_doc(deps.path, deps.memory)
            deps.collapsed = {k: v for k, v in deps.collapsed.items() if deps.doc.get(k)}
            deps.actions.append(f"restore {historian.restored[:7]}")
            await deps.on_change()
            return f"{result.output}\n\nThe note now reads:\n{deps.doc.render(deps.collapsed)}"

        return scribe

    async def _save(self, deps: ScribeDeps, action: str, touched: list[int]) -> list[int]:
        """Write the scribe's working document to disk. Returns ids of sections that are new."""
        before = deps.doc.ids()
        deps.doc = deps.doc.normalised(deps.memory.next_section_id)
        added = sorted(deps.doc.ids() - before)
        deps.memory.next_section_id = max(deps.memory.next_section_id, max(deps.doc.ids(), default=0) + 1)
        for section_id in [*touched, *added]:
            deps.memory.touch(section_id)
        if added and action == "add":
            action = "add " + " ".join(f"§{i}" for i in added)
        deps.actions.append(action)
        self.notes.write(deps.path, deps.doc.serialize())
        await deps.on_change()
        return added

    # ---- historian ----------------------------------------------------------------------------

    def _build_historian(self) -> Agent[HistorianDeps, str]:
        historian = Agent(
            self.model, deps_type=HistorianDeps, instructions=HISTORIAN_INSTRUCTIONS, model_settings=self.settings
        )

        @historian.tool
        def git_log(ctx: RunContext[HistorianDeps], limit: int = 30) -> str:
            """List commits for this note, newest first, with the speech that caused each.

            Args:
                limit: How many commits to list.
            """
            return "\n".join(describe_commit(c) for c in self.notes.log(ctx.deps.path, limit)) or "(no commits)"

        @historian.tool
        def git_show(ctx: RunContext[HistorianDeps], commit: str) -> str:
            """Show the diff a commit made to this note.

            Args:
                commit: Commit id.
            """
            try:
                return self.notes.show(ctx.deps.path, commit)[:4000]
            except NoteError as error:
                return f"Error: {error}"

        @historian.tool
        def restore(ctx: RunContext[HistorianDeps], commit: str) -> str:
            """Set the note back to exactly how it was at a commit. History is kept, so this can itself be undone.

            Args:
                commit: Commit id to restore to.
            """
            try:
                self.notes.restore(ctx.deps.path, commit)
            except NoteError as error:
                return f"Error: {error}"
            ctx.deps.restored = commit
            return "OK, restored"

        return historian
