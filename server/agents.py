"""The three agents behind a project of notes.

- Scribe: runs on every batch of speech and edits the notes section by section, creating notes (tabs) as needed.
- Historian: handles undo / redo / "go back to..." through git. The scribe calls it with ask_history,
  so the git tools never clutter the scribe's context.
- Librarian: the context manager. Writes the section summaries shown in place of collapsed sections,
  and the running summary that replaces old conversation turns.
"""

import os

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import hashlib
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Literal

from pydantic_ai import Agent, RunContext, capture_run_messages
# UnexpectedModelBehavior covers the model running into its token cap (with or without partial output).
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartStartEvent,
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
from .agentlog import AgentLog, slim_messages
from .memory import MemoryStore, NoteMemory, SectionInfo
from .notes import NoteError, NoteStore
from .sections import (
    Collapse,
    Document,
    Section,
    estimate_tokens,
    number_lines,
    number_sections,
    split_at_headings,
    strip_line_numbers,
)

log = logging.getLogger(__name__)

# Distinct mistakes allowed per turn before it is ended. The model often recovers after two.
MAX_TOOL_FAILURES = 4
# Rewriting a section at least this long down to under half its size is refused: it is almost always the model
# sending only the changed sentence, which would silently delete the rest.
SHRINK_GUARD_WORDS = 80
EDIT_SHRINK_GUARD_WORDS = 40


class StopTurn(Exception):
    """Ends a scribe turn that keeps failing; edits made so far are kept."""

# Live status for the UI (the AI button): "thinking", "tool" or "context", reported while agents work.
STATUS: ContextVar[Callable[[str, str], Awaitable[None]] | None] = ContextVar("status", default=None)
# Tools that read the note rather than change it are shown as the context agent at work.
CONTEXT_TOOLS = {"open_section"}


async def report(step: str, detail: str = "") -> None:
    if (callback := STATUS.get()) is not None:
        await callback(step, detail)


async def stream_status(_ctx, events) -> None:
    """Turns an agent run's event stream into live status: thinking, using a tool, or reading context."""
    async for event in events:
        if isinstance(event, FunctionToolCallEvent):
            name = event.part.tool_name
            await report("context" if name in CONTEXT_TOOLS else "tool", name)
        elif isinstance(event, FunctionToolResultEvent):
            await report("thinking")
        elif isinstance(event, PartStartEvent) and event.part.part_kind == "thinking":
            await report("thinking", "reasoning")


# Sub-agent runs (historian, librarian) made during the current scribe turn, for the activity log.
SUBRUNS: ContextVar[list[dict] | None] = ContextVar("subruns", default=None)


def record_subrun(agent: str, purpose: str, result, started: float) -> None:
    runs = SUBRUNS.get()
    if runs is not None:
        runs.append(
            {"agent": agent, "purpose": purpose, "seconds": round(time.monotonic() - started, 2),
             "messages": slim_messages(result.all_messages())}
        )


def since_prompt(messages: list[ModelMessage], prompt: str, history_length: int) -> list[ModelMessage]:
    """The messages of the current turn, from a capture that may also hold (possibly trimmed) history: everything
    from the request carrying this turn's prompt. Falls back to slicing off the history."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) and part.content == prompt for part in message.parts
        ):
            return messages[index:]
    return messages[history_length:]


def turn_limits(requests: int = config.LLM_MAX_STEPS) -> UsageLimits:
    """Per-turn limits: how many model calls, and how many tokens they may generate in total."""
    return UsageLimits(request_limit=requests, output_tokens_limit=config.LLM_TURN_OUTPUT_TOKENS)


def scribe_instructions(system_prompt: str, memory: NoteMemory) -> str:
    text = system_prompt + "\n" + SCRIBE_RULES
    if memory.history_summary:
        text += f"\n## Earlier in this session\n{memory.history_summary}\n"
    return text


# Gemma occasionally leaks chat-template tokens such as "<|channel>" into its text.
CONTROL_TOKEN = re.compile(r"<\|[a-z_]+\|?>")

SCRIBE_RULES = """
## How this works
You keep the notes for one project. A project has one or more notes, which the user sees as tabs. Each message gives you every note (the active one is the tab the user is looking at), the project's recent changes, new speech from the user (transcribed automatically, numbered S1, S2, ...), and the user's saved instructions for this project. Update the notes to reflect the new speech, then reply with one short line saying what you changed. Never repeat the notes in your reply.

Notes are split into sections whose ids are unique across the whole project. In a long project, sections near the top of a note may be shown as a summary, either whole ("collapsed") or just their earlier part ("partly-collapsed", with the end still shown word for word). Call open_section to read the full text before changing summarised text.

- Change notes only with tools: insert_text to add text inside a section without removing anything, edit_section to change or remove text, write_section to add, rewrite or delete a whole section, move_section to reorder or move a section into another note, manage_note to create, rename, delete or switch to (open) a note.
- New sections go in the active note unless the user names another note or the content plainly belongs in an existing one.
- Create a new note when the user asks for one (a new note, page or tab), or when their instructions say how to split things into notes.
- A section is one heading plus its content. When adding a section, write only that section: never repeat a note's title or other sections.
- The speech comes from automatic speech-to-text, so expect the usual transcription errors: words replaced by similar-sounding ones ("Owen fixed" for "oh and fix", "crudies" for "cronies"), names misspelled or broken up, stray or random words picked up from noise, and missing or wrong punctuation. Read for what the user most likely meant, using the notes and recent speech as context, and write that. When a word makes no sense, prefer the sound-alike that fits; leave out stray words that don't belong. Don't add a garbled word as new content, and don't invent detail to make sense of it.
- Commands about the notes are not content. Carry them out literally and never write them into a note, even in another style or form.
  - "Clear the note", "start over": delete every section of that note with write_section(section_id=..., content="").
  - "Delete this note/tab": manage_note(action="delete"). "Go to the X note": manage_note(action="open").
  - Anything about whole projects (new project, rename/delete/restore a project, switch to another project): ask_project_manager.
  - Undo, redo, "scrap that", "go back to before X", "put it back": call ask_history with just that request, then make any other changes yourself.
  - Corrections ("no, I meant Tuesday") are ordinary edits.
  - Guidance on how to take notes ("put action items first", "keep it brief"): save it with set_instructions, merged with the existing instructions.
- The user's saved instructions shape how content is written. They never apply to commands.
- Speech can also be a request to work on what is already in the notes: "update the characters", "reformat the timeline", "tidy chapter one", "fill in the locations from the draft". Do it from the notes' existing content. That is a real request, not "no change".
- If the speech is only filler, false starts or noise, change nothing and reply "no change".
- To move text, add it in its new place and remove it from the old one in the same turn; to move a whole section, use move_section. Never delete and recreate a note to change something inside it.
- Leave no empty or duplicate headings behind.
- In stories, record what the story says. A word spoken in dialogue is not a character's name unless the story uses it as one.
- Every line of text is shown with its line number, like "[23] It was raining.". The user sees the same numbers beside the note, counted per note, so "line 23", "text 23" or "number 23" means that line of the note they are looking at (the active one) unless they name another. To delete numbered lines, call delete_lines once with all of them. The numbers are not part of the text: never write them in old_text, new_text or anything else.
- Keep old_text short: just the words being changed, enough to be unique, never the whole section. To add to the end of a section, use its last sentence or line as old_text and repeat it in new_text followed by the addition.
- Make one edit per section that needs changing, including sections the user's instructions ask you to keep up to date. When a tool replies OK that change is done: go on to the next change or reply, and never send the same call twice. Each reply lists the notes and sections as they are now.
- Never invent content the user did not say.
"""

HISTORIAN_INSTRUCTIONS = """You manage the edit history of one project of markdown notes, kept in git. Every change is a commit whose subject lists the speech segments (S1, S2, ...) that caused it and what was done; the body holds the speech itself.

You receive a request from the note-taking assistant, such as "undo that" or "go back to before I talked about food". Find the right commit from the list given (use git_log or git_show if you need more), then call restore yourself. Do not describe what you would do: do it.

- restore sets the whole project (every note, and which notes exist) back to exactly how it was at a commit. It adds a new commit and never deletes history, so redo is just restoring to the commit from before the undo.
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


# Tool calls whose arguments stay whole in the saved history: the model re-uses them (e.g. merging instructions)
# and once copied a shortened version, marker and all, into the user's instructions.
KEEP_ARGS_WHOLE = {"set_instructions"}
# The marker `_shorten` leaves in history. Text containing it was copied from history, not written.
SHORTENED = re.compile(r"… \(\d+ chars\)")


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
                if isinstance(part, ToolCallPart) and part.tool_name not in KEEP_ARGS_WHOLE:
                    part = replace(part, args={k: _shorten(v, 240) for k, v in part.args_as_dict().items()})
                    parts.append(part)
                elif isinstance(part, TextPart):
                    parts.append(part)
            compacted.append(replace(message, parts=parts))
    return compacted


@dataclass
class UiHooks:
    """How the scribe tells open browsers what it is doing, so the view can follow along."""

    notes_changed: Callable[[list[str], str, int | None], Awaitable[None]]  # notes to refresh, focus note, section
    tabs_changed: Callable[[str], Awaitable[None]]  # active note


@dataclass
class ScribeDeps:
    project: str
    docs: dict[str, Document]  # in tab order
    active: str
    memory: NoteMemory
    collapsed: dict[int, Collapse]
    system_prompt: str
    hooks: UiHooks
    actions: list[str] = field(default_factory=list)
    failure_messages: set[str] = field(default_factory=set)
    calls: dict[tuple, str | None] = field(default_factory=dict)  # call -> None if it worked, else its error
    last_call: tuple | None = None
    manager_requests: list[str] = field(default_factory=list)

    def find(self, section_id: int) -> tuple[str, Section] | None:
        for note, doc in self.docs.items():
            if (section := doc.get(section_id)) is not None:
                return note, section
        return None

    def match_note(self, name: str | None) -> str | None:
        """The note called `name`, ignoring case and spacing."""
        wanted = " ".join((name or "").lower().removesuffix(".md").split())
        return next((note for note in self.docs if note.lower() == wanted), None)

    def outline(self) -> str:
        notes = "; ".join(
            f'"{note}"{" (active)" if note == self.active else ""}: '
            + (", ".join(f"§{s.id} {s.title or '(untitled)'}" for s in doc.sections) or "(empty)")
            for note, doc in self.docs.items()
        )
        return f"Notes now: {notes}"

    def fail(self, message: str) -> str:
        if self.last_call is not None:
            self.calls[self.last_call] = message
        # Count distinct problems: several calls sent at once that hit the same error are one mistake, not many.
        self.failure_messages.add(message)
        if len(self.failure_messages) >= MAX_TOOL_FAILURES:
            raise StopTurn(message)
        return f"Error: {message}. {self.outline()}"

    def copied_shortened(self, *texts: str | None) -> str | None:
        """Refuse text copied from a shortened history entry ("… (402 chars)"), which would save it truncated."""
        if any(text and SHORTENED.search(text) for text in texts):
            return self.fail(
                'that text contains "… (N chars)", which marks a shortened copy from your history. Write out the '
                "full text instead (the current notes and instructions are shown in full in the latest message)"
            )
        return None

    def repeated(self, *call) -> str | None:
        """Refuse a call identical to one already made this turn: the model sometimes loops on success, and
        sometimes retries a failed call unchanged. The reply says which, so a failure is never reported as done."""
        if call in self.calls:
            earlier = self.calls[call]
            self.last_call = None
            if earlier is None:
                return self.fail("that exact change was already made this turn; it is done, so stop and reply")
            return self.fail(
                f"you already tried exactly this and it failed ({earlier}). Nothing was changed. Fix the call: "
                "the notes in the latest message are the current text, so copy old_text from there"
            )
        self.calls[call] = None
        self.last_call = call
        return None


SENTENCE = re.compile(r"[^.!?\n]+[.!?]+[\"'”’)]*|[^\n]+$", re.MULTILINE)


def dropped_warning(old_text: str, new_text: str) -> str:
    """Name the sentences an edit removed when it also kept others, which usually means a forgotten anchor.

    Not an error: replacing or deleting text on purpose is normal. It just lets the scribe notice and fix a slip.
    """
    sentences = [m.group(0).strip() for m in SENTENCE.finditer(old_text) if len(m.group(0).split()) >= 4]
    new_words = set(re.findall(r"\w+", new_text.lower()))

    def survives(sentence: str) -> bool:
        # Reworded counts as kept: only flag a sentence whose words have mostly disappeared.
        words = set(re.findall(r"\w+", sentence.lower()))
        return sentence in new_text or len(words & new_words) >= 0.6 * len(words)

    kept = [s for s in sentences if survives(s)]
    dropped = [s for s in sentences if not survives(s)]
    if not kept or not dropped:
        return ""
    quoted = "; ".join(f'"{s[:80]}"' for s in dropped[:3])
    return (
        f" Note: these sentences from old_text are no longer in the section: {quoted}. If that was not intended, "
        "put them back (and use insert_text to add text without removing any)."
    )


def drop_empty_twins(doc: Document) -> None:
    """Remove heading-only sections that repeat another section's heading in the same note (leftovers from
    edits). A lone empty heading is kept: it is usually a placeholder the user wants."""
    titles = [s.title.lower() for s in doc.sections]
    doc.sections = [
        s for s in doc.sections
        if s.text[s.body_start():].strip() or titles.count(s.title.lower()) == 1
    ]


def render_project(docs: dict[str, Document], active: str, collapsed: dict[int, Collapse]) -> str:
    return "\n".join(
        f'<note name="{note}"{" active" if note == active else ""}>\n{doc.render(collapsed)}\n</note>'
        for note, doc in docs.items()
    )


@dataclass
class HistorianDeps:
    project: str
    restored: str | None = None


class NoteAgents:
    def __init__(self, notes: NoteStore, memories: MemoryStore, activity: AgentLog):
        self.notes = notes
        self.memories = memories
        self.activity = activity
        self.model = OpenAIChatModel(
            config.LLM_MODEL,
            provider=OpenAIProvider(base_url=config.LLM_URL, api_key=config.LLM_API_KEY or "none"),
        )
        self.settings = ModelSettings(temperature=config.LLM_TEMPERATURE, max_tokens=config.LLM_MAX_TOKENS)
        self.scribe = self._build_scribe()
        self.historian = self._build_historian()
        self.librarian = Agent(
            self.model,
            instructions=LIBRARIAN_INSTRUCTIONS,
            model_settings=ModelSettings(temperature=config.LLM_TEMPERATURE, max_tokens=config.LIBRARIAN_MAX_TOKENS),
        )

    async def process(
        self, project: str, segments: list[dict], system_prompt: str, hooks: UiHooks, viewing: str | None = None
    ) -> tuple[str | None, str, list[str]]:
        """Fold new speech into the project's notes.

        Returns (commit sha or None, one-line summary, requests for the project manager to run after this turn).
        """
        started = time.monotonic()
        subruns: list[dict] = []
        SUBRUNS.set(subruns)
        memory = self.memories.load(project)
        memory.turn += 1
        docs = self._load_project(project, memory)
        if not docs:
            raise NoteError(f'Project "{project}" has no notes (was it deleted?)')
        active = next(n for n in (viewing, memory.active_note, *docs) if n in docs)

        await self._compact_history(memory)
        collapsed = await self._collapse(docs, memory)
        deps = ScribeDeps(project, docs, active, memory, collapsed, system_prompt, hooks)
        speech = "\n".join(f"S{s['id']}: {s['text']}" for s in segments)
        recent = self.notes.log(project, config.RECENT_COMMITS_IN_CONTEXT)
        commits = "\n".join(describe_commit(c) for c in recent)
        # Commits newer than the scribe's own last one were made by the user (hand edits, reverts, restores).
        by_hand = []
        for commit in recent:
            if commit["subject"].startswith("[S"):
                break
            by_hand.append(commit["subject"])
        hand_note = (
            f"Since your last turn the user changed the notes by hand ({'; '.join(by_hand)}). The notes below are "
            "the current text: trust them over anything you remember writing.\n\n"
            if by_hand
            else ""
        )
        tabs = ", ".join(f'"{n}"' + (" (active: the user is looking at this one)" if n == active else "") for n in docs)
        prompt = (
            f'Project "{project}" has these notes, shown as tabs: {tabs}\n\n'
            f"{render_project(docs, active, collapsed)}\n\n"
            f"Recent changes (newest first; ask_history can undo or restore any of these):\n{commits}\n\n"
            f"{hand_note}"
            f"Update the notes as per this transcription:\n{speech}\n\n"
            f"Remember this, the user's instructions for this project:\n{memory.instructions or '(none yet)'}"
        )

        history = memory.history()
        instructions = scribe_instructions(system_prompt, memory)
        history_tokens = sum(estimate_tokens(describe_turn(t)) for t in memory.turns)
        await report("thinking")
        with capture_run_messages() as messages:
            try:
                result = await self.scribe.run(
                    prompt,
                    deps=deps,
                    message_history=history,
                    usage_limits=turn_limits(),
                    event_stream_handler=stream_status,
                )
                # Only the first line: the model sometimes echoes the whole note after its summary.
                summary = next(iter(CONTROL_TOKEN.sub("", result.output).strip().splitlines()), "")
                if not deps.actions and deps.failure_messages:
                    # The model sometimes claims success after failed edits; report what actually happened.
                    summary = f"couldn't make that change ({sorted(deps.failure_messages)[0][:120]})"
                elif deps.actions and summary.lower().strip(" .") in ("", "no change"):
                    # ...and sometimes says "no change" after changes it did make.
                    summary = "; ".join(deps.actions)
                turn = result.new_messages()
            except (UsageLimitExceeded, StopTurn, UnexpectedModelBehavior) as stopped:
                log.warning("Scribe stopped early on %s: %s", project, stopped)
                summary = "; ".join(deps.actions) or "stopped: could not make that change"
                turn = [*since_prompt(messages, prompt, len(history)), ModelResponse(parts=[TextPart(summary)])]
        memory.turns.append(compact_turn(turn, speech))
        memory.active_note = deps.active

        self.memories.save(project, memory)
        ids = " ".join(f"S{s['id']}" for s in segments)
        subject = f"[{ids}] {'; '.join(deps.actions) or 'no change'}"
        sha = self.notes.commit(project, f"{subject}\n\n{summary}\n\n{speech}")
        self.activity.append(project, {
            "time": time.time(),
            "seconds": round(time.monotonic() - started, 2),
            "segments": [s["id"] for s in segments],
            "speech": speech,
            "summary": summary,
            "actions": deps.actions,
            "commit": sha,
            "estimate": {
                "instructions": estimate_tokens(instructions),
                "history": history_tokens,
                "notes": estimate_tokens(render_project(docs, active, collapsed)),
                "prompt": estimate_tokens(prompt),
            },
            "history_turns": len(memory.turns) - 1,
            "collapsed_sections": len(collapsed),
            "instructions": instructions,
            "prompt": prompt,
            "messages": slim_messages(turn),
            "subruns": subruns,
        })
        return sha, summary or "; ".join(deps.actions) or "no change", deps.manager_requests

    # ---- context management -------------------------------------------------------------------

    def _load_project(self, project: str, memory: NoteMemory) -> dict[str, Document]:
        """Every note in tab order, with section ids made unique across the project (and written back)."""
        docs = {note: Document.parse(self.notes.read(project, note), assign=False) for note in self.notes.notes(project)}
        for doc in docs.values():
            drop_empty_twins(doc)
        memory.next_section_id = number_sections(list(docs.values()), memory.next_section_id)
        for note, doc in docs.items():
            self.notes.write(project, note, doc.serialize())
        return docs

    async def _collapse(self, docs: dict[str, Document], memory: NoteMemory) -> dict[int, Collapse]:
        """Fit the project into the note budget by summarising, starting from the top of each note.

        Everything is shown in full until the budget is passed. Then whole sections are summarised from the top
        of each note down (never a note's last section, nor anything edited in the last couple of turns), and if
        that is not enough, the earlier part of long sections is summarised while their end, where the user is
        most likely still working, stays word for word.
        """
        total = sum(doc.tokens() for doc in docs.values())
        if total <= config.NOTE_TOKEN_BUDGET:
            return {}
        target = int(config.NOTE_TOKEN_BUDGET * 0.75)
        collapsed: dict[int, Collapse] = {}

        def recent(section: Section) -> bool:
            return memory.sections.get(section.id, SectionInfo()).touched >= memory.turn - 1

        # Whole sections, topmost first across all notes.
        tops = sorted(
            ((index, section) for doc in docs.values() for index, section in enumerate(doc.sections[:-1])),
            key=lambda pair: pair[0],
        )
        for _, section in tops:
            if total <= target:
                return collapsed
            if recent(section):
                continue
            summary = await self._section_summary(section, section.text, memory)
            saved = estimate_tokens(section.text) - estimate_tokens(summary) - 25
            if saved > 0:
                collapsed[section.id] = Collapse(summary)
                total -= saved

        # Still too big: summarise the earlier part of the longest remaining sections, keeping their end.
        remaining = sorted(
            (s for doc in docs.values() for s in doc.sections if s.id not in collapsed),
            key=lambda s: estimate_tokens(s.text),
            reverse=True,
        )
        for section in remaining:
            if total <= target:
                break
            keep_from = section.tail_start(config.KEEP_TAIL_TOKENS)
            if keep_from is None:
                continue
            head = section.text[section.body_start(): keep_from]
            summary = await self._section_summary(section, head, memory)
            saved = estimate_tokens(head) - estimate_tokens(summary) - 30
            if saved > 0:
                collapsed[section.id] = Collapse(summary, keep_from)
                total -= saved
        return collapsed

    async def _section_summary(self, section: Section, text: str, memory: NoteMemory) -> str:
        """A short summary of `text` (all or the earlier part of `section`), cached until that text changes."""
        info = memory.sections.setdefault(section.id, SectionInfo())
        digest = hashlib.sha1(text.encode()).hexdigest()[:12]
        if info.digest != digest or not info.summary:
            started = time.monotonic()
            await report("context", f"summarising §{section.id}")
            result = await self.librarian.run(
                "Summarise this note text in at most three sentences, so an assistant knows what it contains "
                "(names, events, decisions) and when it would need to read it in full:\n\n"
                f"{text}"
            )
            record_subrun("librarian", f"summarise §{section.id}", result, started)
            info.summary, info.digest = result.output.strip(), digest
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
        started = time.monotonic()
        await report("context", "summarising older conversation")
        result = await self.librarian.run(
            "Below is the running summary of a note-taking session, then older turns that are leaving the "
            "assistant's context. Write an updated summary of the conversation, under 200 words. The assistant "
            "always sees the full note separately, so do not restate the note's contents. Cover: the order "
            "topics came up in, corrections and changes of mind, undo/redo requests and what they affected "
            "(by section id), and anything the user said they would come back to.\n\n"
            f"Current summary:\n{memory.history_summary or '(none)'}\n\nOlder turns:\n{transcript}"
        )
        record_subrun("librarian", f"summarise {len(old)} old turns", result, started)
        memory.history_summary = result.output.strip()
        memory.turns = recent
        log.info("Summarised %d old turns", len(old))

    # ---- scribe -------------------------------------------------------------------------------

    def _build_scribe(self) -> Agent[ScribeDeps, str]:
        scribe = Agent(self.model, deps_type=ScribeDeps, model_settings=self.settings)

        @scribe.instructions
        def instructions(ctx: RunContext[ScribeDeps]) -> str:
            return scribe_instructions(ctx.deps.system_prompt, ctx.deps.memory)

        @scribe.tool
        async def edit_section(ctx: RunContext[ScribeDeps], section_id: int, old_text: str, new_text: str) -> str:
            """Replace exact text inside one section. old_text must appear exactly once in it; use an empty new_text to delete.

            Keep old_text short (a few words to a sentence), never the whole section.

            Args:
                section_id: The section to edit (in any note).
                old_text: The exact words being changed, just enough to be unique. To append, the section's last sentence.
                new_text: Replacement text.
            """
            deps = ctx.deps
            old_text, new_text = strip_line_numbers(old_text), strip_line_numbers(new_text)
            if refused := deps.copied_shortened(new_text) or deps.repeated("edit", section_id, old_text, new_text):
                return refused
            if old_text == new_text:
                return deps.fail("new_text is the same as old_text, so that edit changes nothing")
            # Replacing a long passage with a short one is almost always the model sending only the new sentence,
            # which would silently delete the rest (it once wiped a whole story this way). Deleting on purpose,
            # with an empty new_text, is still allowed.
            old_words, new_words = len(old_text.split()), len(new_text.split())
            if old_words >= EDIT_SHRINK_GUARD_WORDS and 0 < new_words < old_words / 2:
                return deps.fail(
                    f"that would replace {old_words} words with {new_words}, deleting most of them. To add text, "
                    "use a short old_text (e.g. the last sentence) and put it back in new_text followed by the "
                    "addition. To delete text on purpose, use an empty new_text"
                )
            found = deps.find(section_id)
            if found is None:
                return deps.fail(f"no section {section_id}")
            note, section = found
            fold = deps.collapsed.get(section_id)
            if fold is not None and fold.keep_from is None:
                return deps.fail(f"section {section_id} is collapsed; call open_section({section_id}) first")
            count = section.text.count(old_text) if old_text else 0
            if count > 1 and old_text.strip() and section.text.rstrip().endswith(old_text.strip()):
                # Quoting the section's closing words means "add to the end": use the final occurrence.
                cut = section.text.rstrip().rfind(old_text.strip())
                section.text = section.text[:cut] + new_text.strip() + "\n"
            elif count > 1:
                return deps.fail(
                    f"old_text appears {count} times in section {section_id}; include a few more words around it so it is unique"
                )
            elif count == 0:
                if new_text.strip() and new_text.strip() in section.text:
                    return deps.fail(
                        f"old_text is not in section {section_id}, but new_text already is: the change may already be "
                        "made, or old_text and new_text are swapped. old_text must be the text as it is now"
                    )
                return deps.fail(f"old_text is not in section {section_id}; copy it exactly from the section")
            else:
                section.text = section.text.replace(old_text, new_text, 1)
            await self._save(deps, [note], note, f"edit §{section_id}", [section_id])
            return f"OK.{dropped_warning(old_text, new_text)} {deps.outline()}"

        @scribe.tool
        async def delete_lines(ctx: RunContext[ScribeDeps], lines: list[int], note: str | None = None) -> str:
            """Delete whole lines by their line numbers. Pass every line to delete in one call.

            Args:
                lines: The line numbers to delete, as shown in the latest notes (e.g. [38, 39, 40]).
                note: The note they are in. Omit for the active note.
            """
            deps = ctx.deps
            target = deps.match_note(note) if note else deps.active
            if target is None:
                return deps.fail(f'no note "{note}"')
            if refused := deps.repeated("delete_lines", target, tuple(sorted(lines))):
                return refused
            doc = deps.docs[target]
            removed = doc.delete_lines(set(lines))
            if not removed:
                return deps.fail(f"note \"{target}\" has no line {', '.join(map(str, lines))}")
            touched = sorted({s.id for _, s, _ in removed if s.id is not None})
            for section_id in touched:
                deps.collapsed.pop(section_id, None)
            listed = "; ".join(f'{n}: "{text[:60]}"' for n, _, text in removed)
            missing = sorted(set(lines) - {n for n, _, _ in removed})
            await self._save(deps, [target], target, f"deleted line {', '.join(str(n) for n, _, _ in removed)}", [
                s.id for s in doc.sections if s.id in touched
            ])
            note_missing = f" There was no line {', '.join(map(str, missing))}." if missing else ""
            return f"OK, deleted {listed}.{note_missing} Lines after these are now numbered lower. {deps.outline()}"

        @scribe.tool
        async def insert_text(ctx: RunContext[ScribeDeps], section_id: int, after_text: str, text: str) -> str:
            """Insert new text into a section right after some existing text, without removing anything.
            Use this to add a sentence or paragraph in the middle or at the end of a section.

            Args:
                section_id: The section to add to.
                after_text: Exact existing text (a few words to a sentence) that the new text should follow.
                text: The text to insert. It goes on a new line (a new paragraph) if after_text ends a line.
            """
            deps = ctx.deps
            after_text, text = strip_line_numbers(after_text), strip_line_numbers(text)
            if refused := deps.copied_shortened(text) or deps.repeated("insert", section_id, after_text, text):
                return refused
            found = deps.find(section_id)
            if found is None:
                return deps.fail(f"no section {section_id}")
            note, section = found
            fold = deps.collapsed.get(section_id)
            if fold is not None and fold.keep_from is None:
                return deps.fail(f"section {section_id} is collapsed; call open_section({section_id}) first")
            anchor = after_text.strip()
            count = section.text.count(anchor) if anchor else 0
            if count == 0:
                return deps.fail(f"after_text is not in section {section_id}; copy a few words exactly from the section")
            if count > 1 and not section.text.rstrip().endswith(anchor):
                return deps.fail(f"after_text appears {count} times in section {section_id}; include a few more words so it is unique")
            # With repeats, quoting the section's closing words means "at the end".
            cut = (section.text.rfind(anchor) if count > 1 else section.text.find(anchor)) + len(anchor)
            rest = section.text[cut:]
            if not rest.strip() or rest.lstrip(" \t").startswith("\n"):
                # The anchor ends a line: the new text becomes its own paragraph.
                section.text = section.text[:cut] + "\n\n" + text.strip() + rest
            else:
                section.text = section.text[:cut] + " " + text.strip() + rest
            await self._save(deps, [note], note, f"insert into §{section_id}", [section_id])
            return f"OK, inserted. {deps.outline()}"

        @scribe.tool
        async def write_section(
            ctx: RunContext[ScribeDeps],
            content: str,
            section_id: int | None = None,
            after_section_id: int | None = None,
            note: str | None = None,
        ) -> str:
            """Add, rewrite or delete a whole section (heading included).

            Args:
                content: Markdown for this one section, starting with its own heading (e.g. "## Food"). Empty deletes the section.
                section_id: The section to rewrite or delete. Omit to add a new section.
                after_section_id: Where to add a new section. Omit to add at the end of the note; 0 for its top.
                note: Which note to add a new section to. Omit for the active note.
            """
            deps = ctx.deps
            content = strip_line_numbers(content)
            if refused := deps.copied_shortened(content) or deps.repeated("write", content, section_id, after_section_id, note):
                return refused
            if section_id is not None:
                found = deps.find(section_id)
                if found is None:
                    return deps.fail(f"no section {section_id}")
                target, section = found
                if not content.strip():
                    deps.docs[target].sections.remove(section)
                    deps.collapsed.pop(section_id, None)
                    await self._save(deps, [target], target, f"delete §{section_id}", [])
                    return f"OK, deleted. {deps.outline()}"
                if section_id in deps.collapsed:
                    return deps.fail(
                        f"section {section_id} is shown summarised, so rewriting it whole would lose text you have "
                        f"not seen. Use edit_section, or call open_section({section_id}) first"
                    )
                old_words, new_words = len(section.text.split()), len(content.split())
                if old_words >= SHRINK_GUARD_WORDS and new_words < old_words / 2:
                    return deps.fail(
                        f"that would cut section {section_id} from {old_words} words to {new_words}. To change part of "
                        "a section use edit_section; to delete it, write empty content"
                    )
                if content.strip() == section.text.strip():
                    return deps.fail(f"section {section_id} already has exactly that text, so the rewrite changes nothing")
                section.text = content
                deps.collapsed.pop(section_id, None)
                await self._save(deps, [target], target, f"rewrite §{section_id}", [section_id])
                return f"OK. {deps.outline()}"

            anchor = deps.find(after_section_id) if after_section_id else None
            # A wrong anchor is harmless when the note is named (e.g. "after §1" in an empty note): add it there.
            misplaced = bool(after_section_id and anchor is None and note and deps.match_note(note))
            if after_section_id and anchor is None and not misplaced:
                return deps.fail(f"no section {after_section_id}")
            target = anchor[0] if anchor else (deps.match_note(note) if note else deps.active)
            if target is None:
                return deps.fail(f'no note "{note}"; create it first with manage_note(action="create")')
            doc = deps.docs[target]
            # The model sometimes repeats the note's title or other sections when adding one.
            existing = {s.text.strip() for s in doc.sections}
            content = "\n\n".join(p for p in split_at_headings(content) if p.strip() not in existing)
            if not content.strip():
                return deps.fail("that section already exists word for word, so nothing was added (to reorder, use move_section)")
            for piece in split_at_headings(content):
                twin = next((s for s in doc.sections if s.title and s.title.lower() == Section(None, piece).title.lower()), None)
                if twin is not None:
                    return deps.fail(
                        f'§{twin.id} is already "{twin.title}"; add to it with edit_section(section_id={twin.id}) instead of a second section'
                    )
            position = doc.sections.index(anchor[1]) + 1 if anchor else (0 if after_section_id == 0 else len(doc.sections))
            doc.sections.insert(position, Section(None, content))
            await self._save(deps, [target], target, "add", [])
            where = f' (there is no section {after_section_id}, so it went at the end of "{target}")' if misplaced else ""
            return f"OK, added{where}. {deps.outline()}"

        @scribe.tool
        async def move_section(
            ctx: RunContext[ScribeDeps], section_id: int, after_section_id: int, note: str | None = None
        ) -> str:
            """Move a section below another section, which may be in a different note. Use 0 to move it to the top of a note.

            Args:
                section_id: The section to move.
                after_section_id: The section it should come after, or 0 for the top of a note.
                note: With after_section_id 0, which note's top to move it to. Omit for its current note.
            """
            deps = ctx.deps
            if refused := deps.repeated("move", section_id, after_section_id, note):
                return refused
            found = deps.find(section_id)
            if found is None:
                return deps.fail(f"no section {section_id}")
            source, section = found
            if after_section_id == section_id:
                return deps.fail("a section cannot be moved below itself")
            anchor = deps.find(after_section_id) if after_section_id else None
            if after_section_id and anchor is None:
                return deps.fail(f"no section {after_section_id}")
            target = anchor[0] if anchor else (deps.match_note(note) if note else source)
            if target is None:
                return deps.fail(f'no note "{note}"')
            deps.docs[source].sections.remove(section)
            sections = deps.docs[target].sections
            sections.insert(sections.index(anchor[1]) + 1 if anchor else 0, section)
            where = f" to {target}" if target != source else ""
            await self._save(deps, list({source, target}), target, f"move §{section_id}{where}", [section_id])
            return f"OK, moved. {deps.outline()}"

        @scribe.tool
        async def manage_note(
            ctx: RunContext[ScribeDeps],
            action: Literal["create", "rename", "delete", "open"],
            name: str,
            new_name: str | None = None,
        ) -> str:
            """Create, rename, delete, or open (switch the user's view to) a note, i.e. a tab in this project.

            Args:
                action: What to do.
                name: The note to act on, or the new note's name when creating.
                new_name: The new name, when renaming.
            """
            deps = ctx.deps
            if refused := deps.repeated("note", action, name, new_name):
                return refused
            existing = deps.match_note(name)
            try:
                if action == "create":
                    if existing:
                        return deps.fail(f'note "{existing}" already exists; use manage_note(action="open") to switch to it')
                    created = self.notes.create_note(deps.project, name, after=deps.active)
                    deps.docs = {n: deps.docs.get(n) or Document([]) for n in self.notes.notes(deps.project)}
                    deps.active = created
                    deps.actions.append(f'create note "{created}"')
                elif existing is None:
                    return deps.fail(f'no note "{name}"')
                elif action == "rename":
                    if not new_name:
                        return deps.fail("new_name is needed to rename a note")
                    renamed = self.notes.rename_note(deps.project, existing, new_name)
                    deps.docs = {renamed if n == existing else n: d for n, d in deps.docs.items()}
                    deps.active = renamed if deps.active == existing else deps.active
                    deps.actions.append(f'rename note "{existing}" to "{renamed}"')
                elif action == "delete":
                    self.notes.delete_note(deps.project, existing)
                    for section in deps.docs.pop(existing).sections:
                        deps.collapsed.pop(section.id, None)
                    deps.active = deps.active if deps.active in deps.docs else next(iter(deps.docs))
                    deps.actions.append(f'delete note "{existing}"')
                else:
                    deps.active = existing
            except NoteError as error:
                return deps.fail(str(error))
            await deps.hooks.tabs_changed(deps.active)
            return f"OK. {deps.outline()}"

        @scribe.tool
        def open_section(ctx: RunContext[ScribeDeps], section_id: int) -> str:
            """Show the full text of a collapsed section so it can be edited.

            Args:
                section_id: The section to open.
            """
            found = ctx.deps.find(section_id)
            if found is None:
                return ctx.deps.fail(f"no section {section_id}")
            ctx.deps.collapsed.pop(section_id, None)
            ctx.deps.memory.touch(section_id)
            note, section = found
            return number_lines(section.text.strip(), ctx.deps.docs[note].first_line(section))

        @scribe.tool
        async def set_instructions(ctx: RunContext[ScribeDeps], instructions: str) -> str:
            """Save the user's guidance for how to take notes in this project. Replaces the saved instructions, so include anything still relevant from before.

            Args:
                instructions: The complete instructions to remember.
            """
            if refused := ctx.deps.copied_shortened(instructions):
                return refused
            ctx.deps.memory.instructions = instructions.strip()
            ctx.deps.actions.append("set instructions")
            return "OK, saved"

        @scribe.tool
        async def ask_history(ctx: RunContext[ScribeDeps], request: str) -> str:
            """Ask the history keeper to undo, redo, or go back to an earlier version of the project.

            Args:
                request: What the user wants, in their words plus any detail you know.
            """
            deps = ctx.deps
            commits = "\n".join(describe_commit(c) for c in self.notes.log(deps.project, 10))
            historian = HistorianDeps(deps.project)
            started = time.monotonic()
            result = await self.historian.run(
                f"Request: {request}\n\nRecent commits (newest first):\n{commits}",
                deps=historian,
                usage_limits=turn_limits(requests=6),
                event_stream_handler=stream_status,
            )
            record_subrun("historian", request, result, started)
            if not historian.restored:
                return result.output
            deps.docs = self._load_project(deps.project, deps.memory)
            deps.collapsed = {k: v for k, v in deps.collapsed.items() if deps.find(k)}
            deps.active = deps.active if deps.active in deps.docs else next(iter(deps.docs))
            deps.actions.append(f"restore {historian.restored[:7]}")
            await deps.hooks.tabs_changed(deps.active)
            await deps.hooks.notes_changed(list(deps.docs), deps.active, None)
            return f"{result.output}\n\nThe notes now read:\n{render_project(deps.docs, deps.active, deps.collapsed)}"

        @scribe.tool
        async def ask_project_manager(ctx: RunContext[ScribeDeps], request: str) -> str:
            """Ask the project manager to create, rename, delete, restore or switch to a project (not a note in
            this project: use manage_note for those). It acts as soon as this turn is finished.

            Args:
                request: What the user wants, in their words plus any detail you know.
            """
            ctx.deps.manager_requests.append(request)
            ctx.deps.actions.append("asked project manager")
            return "OK, passed to the project manager. It will act as soon as you finish this turn."

        return scribe

    async def _save(self, deps: ScribeDeps, notes: list[str], focus: str, action: str, touched: list[int]) -> None:
        """Write changed notes to disk, renumber new sections, and point the user's view at the change."""
        before = {s.id for doc in deps.docs.values() for s in doc.sections}
        for note in notes:
            deps.docs[note] = Document.parse(deps.docs[note].serialize(), assign=False)
        deps.memory.next_section_id = number_sections(list(deps.docs.values()), deps.memory.next_section_id)
        added = sorted({s.id for doc in deps.docs.values() for s in doc.sections} - before)
        for section_id in [*touched, *added]:
            deps.memory.touch(section_id)
        if added and action == "add":
            action = "add " + " ".join(f"§{i}" for i in added)
        deps.actions.append(action)
        for note in notes:
            self.notes.write(deps.project, note, deps.docs[note].serialize())
        tab_changed = deps.active != focus
        deps.active = focus
        if tab_changed:
            await deps.hooks.tabs_changed(focus)
        await deps.hooks.notes_changed(notes, focus, (touched or added or [None])[0])

    # ---- historian ----------------------------------------------------------------------------

    def _build_historian(self) -> Agent[HistorianDeps, str]:
        historian = Agent(
            self.model, deps_type=HistorianDeps, instructions=HISTORIAN_INSTRUCTIONS, model_settings=self.settings
        )

        @historian.tool
        def git_log(ctx: RunContext[HistorianDeps], limit: int = 30) -> str:
            """List commits for this project, newest first, with the speech that caused each.

            Args:
                limit: How many commits to list.
            """
            return "\n".join(describe_commit(c) for c in self.notes.log(ctx.deps.project, limit)) or "(no commits)"

        @historian.tool
        def git_show(ctx: RunContext[HistorianDeps], commit: str) -> str:
            """Show the diff a commit made to this project.

            Args:
                commit: Commit id.
            """
            try:
                return self.notes.show(ctx.deps.project, commit)[:4000]
            except NoteError as error:
                return f"Error: {error}"

        @historian.tool
        def restore(ctx: RunContext[HistorianDeps], commit: str) -> str:
            """Set the whole project back to exactly how it was at a commit. History is kept, so this can itself be undone.

            Args:
                commit: Commit id to restore to.
            """
            try:
                self.notes.restore(ctx.deps.project, commit)
            except NoteError as error:
                return f"Error: {error}"
            ctx.deps.restored = commit
            return "OK, restored"

        return historian
