"""The project manager: who you talk to with no project open, and who the scribe asks to manage projects.

It can create, rename, delete and open projects, bring back deleted ones, and restore a project to an earlier
version. The actual changes go through `ManagerHooks`, which the server implements, so locking, cleanup of a
project's memory and transcripts, and updating open browsers all happen in one place.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.exceptions import IncompleteToolCall, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart

from . import config
from .agentlog import slim_messages
from .agents import (
    CONTROL_TOKEN,
    MAX_TOOL_FAILURES,
    SUBRUNS,
    NoteAgents,
    StopTurn,
    compact_turn,
    describe_commit,
    describe_turn,
    report,
    stream_status,
    turn_limits,
)
from .notes import NoteError
from .sections import estimate_tokens

log = logging.getLogger(__name__)

# The manager's own memory, transcript and activity log are stored under this name. Project names cannot start
# with a dot, so it never collides with a real project.
MANAGER_SCOPE = ".manager"

MANAGER_RULES = """You are the project manager for a voice note-taking app. The user's notes are organised into projects, and each project holds one or more notes (tabs). The user talks to you when no project is open, and the note-taking assistant inside a project passes you requests about whole projects.

Each message lists every project (with its notes and how it starts), the deleted projects that can be brought back, which project the user has open, and the new request or speech.

- Use tools to act: create_project, rename_project, delete_projects, restore_deleted_project, project_history, restore_project_version and open_project.
- Speech is transcribed automatically and may mishear names: match project names loosely against the list.
- To take the user into a project, call open_project. When the user creates a project to start working in, open it too.
- Only delete projects the user clearly named. Deleted projects can be brought back with restore_deleted_project.
- To undo changes inside a project, use project_history to find the right version, then restore_project_version.
- You manage projects, not their contents. If the user starts dictating notes, suggest opening or creating a project for them.
- If the speech is filler or noise, do nothing and reply "no change".
- Reply with one or two short sentences: what you did, or the answer to their question about projects."""


@dataclass
class ManagerHooks:
    """Server-side actions the manager's tools call. Each one does its own locking and tells open browsers."""

    create: Callable[[str], Awaitable[str]]
    rename: Callable[[str, str], Awaitable[str]]
    delete: Callable[[list[str]], Awaitable[None]]
    undelete: Callable[[str], Awaitable[str]]
    restore_version: Callable[[str, str], Awaitable[None]]
    open: Callable[[str], Awaitable[None]]


@dataclass
class ManagerDeps:
    hooks: ManagerHooks
    origin: str | None  # the project the user is in, if any
    actions: list[str] = field(default_factory=list)
    failure_messages: set[str] = field(default_factory=set)
    calls: set[tuple] = field(default_factory=set)

    def fail(self, message: str) -> str:
        # Count distinct problems: several calls sent at once that hit the same error are one mistake, not many.
        self.failure_messages.add(message)
        if len(self.failure_messages) >= MAX_TOOL_FAILURES:
            raise StopTurn(message)
        return f"Error: {message}"

    def repeated(self, *call) -> str | None:
        if call in self.calls:
            return self.fail("that exact change was already made this turn; it is done, so stop and reply")
        self.calls.add(call)
        return None


def _when(timestamp: float) -> str:
    return time.strftime("%d %b %H:%M", time.localtime(timestamp))


class ProjectManager:
    def __init__(self, agents: NoteAgents):
        self.agents = agents
        self.notes = agents.notes
        self.agent = self._build()

    def match(self, name: str) -> str | None:
        """The existing project called `name`, ignoring case and spacing."""
        wanted = " ".join(name.lower().split())
        return next((p for p in self.notes.projects() if p.lower() == wanted), None)

    def overview(self, origin: str | None) -> str:
        lines = []
        for project in self.notes.projects():
            tabs = self.notes.notes(project)
            first = next((self.notes.read(project, n) for n in tabs if self.notes.read(project, n).strip()), "")
            opening = " ".join(line.strip("#- ").strip() for line in first.splitlines() if line.strip() and not line.startswith("<!--"))
            commits = self.notes.log(project, 1)
            changed = _when(commits[0]["time"]) if commits else "never"
            lines.append(
                f'- "{project}": {len(tabs)} note(s) ({", ".join(tabs)}), last changed {changed}. '
                f'Starts: "{opening[:100] or "(empty)"}"'
            )
        deleted = [f'"{d["name"]}" (deleted {_when(d["time"])})' for d in self.notes.deleted_projects()[:15]]
        where = f'The user is in project "{origin}".' if origin else "The user has no project open."
        return (
            f"Projects, most recently changed first:\n{chr(10).join(lines) or '(none yet)'}\n\n"
            f"Deleted projects that can be brought back: {', '.join(deleted) or '(none)'}\n\n{where}"
        )

    async def handle(self, request: str, origin: str | None, hooks: ManagerHooks) -> tuple[str, list[str]]:
        """Run one request (speech, or a request passed on by the scribe). Returns (reply, actions taken)."""
        started = time.monotonic()
        subruns: list[dict] = []
        SUBRUNS.set(subruns)
        memories, activity = self.agents.memories, self.agents.activity
        memory = memories.load(MANAGER_SCOPE)
        memory.turn += 1
        await self.agents._compact_history(memory)

        deps = ManagerDeps(hooks, origin)
        prompt = f"{self.overview(origin)}\n\n{request}"
        history = memory.history()
        history_tokens = sum(estimate_tokens(describe_turn(t)) for t in memory.turns)
        instructions = MANAGER_RULES + (f"\n\n## Earlier in this session\n{memory.history_summary}" if memory.history_summary else "")
        await report("thinking")
        with capture_run_messages() as messages:
            try:
                result = await self.agent.run(
                    prompt,
                    deps=deps,
                    message_history=history,
                    instructions=instructions,
                    usage_limits=turn_limits(),
                    event_stream_handler=stream_status,
                )
                reply = next(iter(CONTROL_TOKEN.sub("", result.output).strip().splitlines()), "")
                turn = result.new_messages()
            except (UsageLimitExceeded, StopTurn, IncompleteToolCall) as stopped:
                log.warning("Project manager stopped early: %s", stopped)
                reply = "; ".join(deps.actions) or "Sorry, I couldn't do that."
                turn = [*messages[len(history):], ModelResponse(parts=[TextPart(reply)])]
        memory.turns.append(compact_turn(turn, request))
        memories.save(MANAGER_SCOPE, memory)

        activity.append(MANAGER_SCOPE, {
            "time": time.time(),
            "seconds": round(time.monotonic() - started, 2),
            "segments": [],
            "speech": request,
            "summary": reply,
            "actions": deps.actions,
            "commit": None,
            "estimate": {
                "instructions": estimate_tokens(instructions),
                "history": history_tokens,
                "notes": 0,
                "prompt": estimate_tokens(prompt),
            },
            "history_turns": len(memory.turns) - 1,
            "collapsed_sections": 0,
            "instructions": instructions,
            "prompt": prompt,
            "messages": slim_messages(turn),
            "subruns": subruns,
        })
        return reply or "; ".join(deps.actions) or "no change", deps.actions

    def _build(self) -> Agent[ManagerDeps, str]:
        manager = Agent(self.agents.model, deps_type=ManagerDeps, model_settings=self.agents.settings)

        async def act(deps: ManagerDeps, call: tuple, action: Callable[[], Awaitable[str]]) -> str:
            if refused := deps.repeated(*call):
                return refused
            try:
                done = await action()
            except NoteError as error:
                return deps.fail(str(error))
            deps.actions.append(done)
            return f"OK, {done}. Projects now: {', '.join(self.notes.projects()) or '(none)'}"

        @manager.tool
        async def create_project(ctx: RunContext[ManagerDeps], name: str) -> str:
            """Create a new, empty project.

            Args:
                name: The new project's name.
            """

            async def go() -> str:
                return f'created "{await ctx.deps.hooks.create(name)}"'

            return await act(ctx.deps, ("create", name), go)

        @manager.tool
        async def rename_project(ctx: RunContext[ManagerDeps], name: str, new_name: str) -> str:
            """Rename a project.

            Args:
                name: The project to rename.
                new_name: Its new name.
            """
            project = self.match(name)
            if project is None:
                return ctx.deps.fail(f'no project "{name}"')

            async def go() -> str:
                return f'renamed "{project}" to "{await ctx.deps.hooks.rename(project, new_name)}"'

            return await act(ctx.deps, ("rename", project, new_name), go)

        @manager.tool
        async def delete_projects(ctx: RunContext[ManagerDeps], names: list[str]) -> str:
            """Delete one or more projects. They can be brought back later with restore_deleted_project.

            Args:
                names: The projects to delete.
            """
            projects = [self.match(n) for n in names]
            if missing := [n for n, p in zip(names, projects) if p is None]:
                return ctx.deps.fail(f"no project called {', '.join(missing)}")

            async def go() -> str:
                await ctx.deps.hooks.delete(projects)
                return f"deleted {', '.join(projects)}"

            return await act(ctx.deps, ("delete", *sorted(projects)), go)

        @manager.tool
        async def restore_deleted_project(ctx: RunContext[ManagerDeps], name: str) -> str:
            """Bring back a deleted project exactly as it was before it was deleted.

            Args:
                name: The deleted project's name.
            """

            async def go() -> str:
                return f'brought back "{await ctx.deps.hooks.undelete(name)}"'

            return await act(ctx.deps, ("undelete", name), go)

        @manager.tool
        def project_history(ctx: RunContext[ManagerDeps], name: str, limit: int = 15) -> str:
            """List a project's recent changes, newest first, with the speech that caused each.

            Args:
                name: The project.
                limit: How many changes to list.
            """
            project = self.match(name)
            if project is None:
                return ctx.deps.fail(f'no project "{name}"')
            commits = self.notes.log(project, limit)
            return "\n".join(f"{_when(c['time'])} {describe_commit(c)}" for c in commits) or "(no changes)"

        @manager.tool
        async def restore_project_version(ctx: RunContext[ManagerDeps], name: str, commit: str) -> str:
            """Set a whole project back to how it was at an earlier change. History is kept, so this can be undone.

            Args:
                name: The project.
                commit: The commit id to go back to, from project_history.
            """
            project = self.match(name)
            if project is None:
                return ctx.deps.fail(f'no project "{name}"')

            async def go() -> str:
                await ctx.deps.hooks.restore_version(project, commit)
                return f'restored "{project}" to {commit[:7]}'

            return await act(ctx.deps, ("restore", project, commit), go)

        @manager.tool
        async def open_project(ctx: RunContext[ManagerDeps], name: str) -> str:
            """Take the user into a project, so they can start taking notes in it.

            Args:
                name: The project to open.
            """
            project = self.match(name)
            if project is None:
                return ctx.deps.fail(f'no project "{name}"')

            async def go() -> str:
                await ctx.deps.hooks.open(project)
                return f'opened "{project}"'

            return await act(ctx.deps, ("open", project), go)

        return manager
