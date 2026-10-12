"""One user's private workspace: their projects, transcripts, agent memory and settings, and the agents working on them."""

import asyncio
import logging
from pathlib import Path

from . import config
from .agentlog import AgentLog
from .agents import STATUS, NoteAgents, UiHooks
from .hub import Channel
from .manager import MANAGER_SCOPE, ManagerHooks, ProjectManager
from .memory import MemoryStore
from .notes import NoteStore, clean_name
from .settings import Settings
from .transcripts import TranscriptStore

log = logging.getLogger("notetaker")


class Workspace:
    def __init__(self, user: str, root: Path, hub: Channel):
        root.mkdir(parents=True, exist_ok=True)
        self.user = user
        self.hub = hub
        self.notes = NoteStore(root / "notes")
        self.transcripts = TranscriptStore(root / "transcripts")
        self.settings = Settings(root)
        self.memories = MemoryStore(root / "memory")
        self.activity = AgentLog(root / "agentlog")
        self.agents = NoteAgents(self.notes, self.memories, self.activity)
        self.manager = ProjectManager(self.agents)
        self.viewing: dict[str, str] = {}  # project -> the note (tab) the user last looked at
        self.sessions: dict[str, ProjectSession] = {}
        self.manager_session = ManagerSession(self)
        if stuck := self.transcripts.fail_unfinished():
            log.warning("Marked %d segments from interrupted turns as failed for %s", stuck, user)

    def session(self, project: str) -> "ProjectSession":
        if project not in self.sessions:
            self.sessions[project] = ProjectSession(self, project)
        return self.sessions[project]

    def deliver(self, scope: str, segment: dict) -> None:
        """Hand a transcribed segment to the agent for that scope."""
        if scope == MANAGER_SCOPE:
            self.manager_session.add_speech(segment)
        else:
            self.session(scope).add(segment)

    def project_summaries(self) -> list[dict]:
        return [{"name": name, "notes": self.notes.notes(name)} for name in self.notes.projects()]

    # ---- pushing changes to the browser ----------------------------------------------------------

    async def push_projects(self) -> None:
        await self.hub.send_all({"type": "projects", "projects": self.project_summaries()})

    async def push_note(self, project: str, note: str) -> None:
        if self.notes.note_exists(project, note):
            await self.hub.send(project, {"type": "note", "note": note, "content": self.notes.read(project, note)})

    async def push_tabs(self, project: str, active: str) -> None:
        await self.hub.send(project, {"type": "tabs", "notes": self.notes.notes(project), "active": active})

    async def push_instructions(self, project: str) -> None:
        await self.hub.send(project, {"type": "instructions", "instructions": self.memories.load(project).instructions})

    async def push_history(self, project: str) -> None:
        await self.hub.send(project, {"type": "history", "commits": self.notes.log(project)})

    async def after_history_change(self, project: str) -> None:
        tabs = self.notes.notes(project)
        active = next(n for n in (self.viewing.get(project), *tabs) if n in tabs)
        await self.push_tabs(project, active)
        await self.push_projects()
        for note in tabs:
            await self.push_note(project, note)
        await self.push_history(project)

    def ui_hooks(self, project: str) -> UiHooks:
        async def notes_changed(changed: list[str], focus: str, section: int | None) -> None:
            for note in changed:
                await self.push_note(project, note)
            await self.hub.send(project, {"type": "focus", "note": focus, "section": section})

        async def tabs_changed(active: str) -> None:
            await self.push_tabs(project, active)
            await self.push_projects()
            await self.hub.send(project, {"type": "focus", "note": active, "section": None})

        return UiHooks(notes_changed, tabs_changed)

    def report_status_to(self, *scopes: str):
        """Live agent status (thinking / tool / context) for the AI button of everyone viewing these scopes."""

        async def send(step: str, detail: str) -> None:
            for scope in dict.fromkeys(scopes):
                await self.hub.send(scope, {"type": "agent_step", "step": step, "detail": detail})

        return send

    # ---- project operations, shared by the REST endpoints and the project manager agent ------------

    async def create_project_op(self, name: str) -> str:
        created = self.notes.create_project(name)
        await self.push_projects()
        return created

    async def delete_projects_op(self, names: list[str]) -> None:
        names = [clean_name(n, "project") for n in names]
        for name in names:
            await self.session(name).lock.acquire()
        try:
            self.notes.delete_projects(names)
            for name in names:
                self.transcripts.delete(name)
                self.memories.delete(name)
                self.activity.delete(name)
                self.viewing.pop(name, None)
        finally:
            for name in names:
                self.session(name).lock.release()
                self.sessions.pop(name, None)
        for name in names:
            await self.hub.send(name, {"type": "project_closed"})
        await self.push_projects()

    async def rename_project_op(self, old: str, new: str) -> str:
        old = clean_name(old, "project")
        async with self.session(old).lock:
            new = self.notes.rename_project(old, new)
            self.transcripts.rename(old, new)
            self.memories.rename(old, new)
            self.activity.rename(old, new)
        self.sessions.pop(old, None)
        self.viewing.pop(old, None)
        await self.hub.send(old, {"type": "open_project", "name": new})
        await self.push_projects()
        return new

    async def undelete_project_op(self, name: str) -> str:
        restored = self.notes.undelete_project(name)
        await self.push_projects()
        return restored

    async def restore_version_op(self, project: str, sha: str) -> None:
        project = clean_name(project, "project")
        async with self.session(project).lock:
            self.notes.restore(project, sha)
            self.notes.commit(project, f"Restore to {sha[:7]}")
        await self.after_history_change(project)

    def manager_hooks(self, origin: str) -> ManagerHooks:
        """`origin` is where the request came from (a project, or the manager screen): "open" switches that view."""

        async def open_project(name: str) -> None:
            await self.hub.send(origin, {"type": "open_project", "name": name})

        return ManagerHooks(
            self.create_project_op,
            self.rename_project_op,
            self.delete_projects_op,
            self.undelete_project_op,
            self.restore_version_op,
            open_project,
        )


class ManagerSession:
    """Runs the project manager over speech from the manager screen and requests passed on by scribes."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.lock = asyncio.Lock()
        self._pending: list[dict] = []
        self._task: asyncio.Task | None = None

    def add_speech(self, segment: dict) -> None:
        self._add({"segments": [segment], "origin": None})

    def add_request(self, request: str, origin: str) -> None:
        self._add({"request": request, "origin": origin})

    def _add(self, item: dict) -> None:
        self._pending.append(item)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._pending:
            item = self._pending.pop(0)
            # Consecutive speech is handled together, like a scribe batch.
            while "segments" in item and self._pending and "segments" in self._pending[0]:
                item["segments"] += self._pending.pop(0)["segments"]
            async with self.lock:
                await self._process(item)

    async def _process(self, item: dict) -> None:
        ws, hub = self.ws, self.ws.hub
        origin = item["origin"]
        scopes = [MANAGER_SCOPE] + ([origin] if origin else [])
        segments = item.get("segments", [])
        ids = [s["id"] for s in segments]
        if segments:
            text = "New speech from the user:\n" + "\n".join(f"S{s['id']}: {s['text']}" for s in segments)
        else:
            text = f"Request from the note-taking assistant in \"{origin}\", on the user's behalf:\n{item['request']}"
        for scope in scopes:
            await hub.send(scope, {"type": "agent", "state": "working", "segments": ids})
        STATUS.set(ws.report_status_to(*scopes))
        try:
            reply, _ = await ws.manager.handle(text, origin, ws.manager_hooks(origin or MANAGER_SCOPE))
        except Exception as error:
            log.exception("Project manager failed")
            for scope in scopes:
                await hub.send(scope, {"type": "agent", "state": "error", "message": f"Project manager error: {error}"})
            if ids:
                await hub.send(MANAGER_SCOPE, {"type": "segments", "segments": ws.transcripts.update(MANAGER_SCOPE, ids, status="error")})
            return
        if ids:
            await hub.send(MANAGER_SCOPE, {"type": "segments", "segments": ws.transcripts.update(MANAGER_SCOPE, ids, status="done")})
        for scope in scopes:
            if scope == MANAGER_SCOPE or ws.notes.project_exists(scope):
                await hub.send(scope, {"type": "agent", "state": "idle", "summary": reply})
                await hub.send(scope, {"type": "activity"})


class ProjectSession:
    """Queues speech for one project and runs the agents over it one batch at a time."""

    def __init__(self, ws: Workspace, project: str):
        self.ws = ws
        self.project = project
        self.lock = asyncio.Lock()
        self._pending: list[dict] = []
        self._task: asyncio.Task | None = None

    def add(self, segment: dict) -> None:
        self._pending.append(segment)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        ws = self.ws
        while self._pending:
            batch, self._pending = self._pending, []
            async with self.lock:
                await self._process(batch)
                if not self._pending and ws.notes.project_exists(self.project):
                    try:
                        await ws.agents.tidy(self.project)
                        if not self._pending and config.LLM_WARMUP:
                            await ws.agents.warm(self.project, ws.settings.system_prompt, ws.viewing.get(self.project))
                    except Exception:
                        log.exception("Tidying %s failed", self.project)

    async def _process(self, batch: list[dict]) -> None:
        ws, hub = self.ws, self.ws.hub
        project, ids = self.project, [s["id"] for s in batch]
        if not ws.notes.project_exists(project):
            # e.g. still recording into a project that was just deleted.
            await hub.send(project, {"type": "agent", "state": "error", "message": f'"{project}" no longer exists, so that speech was not added'})
            return
        await hub.send(project, {"type": "agent", "state": "working", "segments": ids})
        STATUS.set(ws.report_status_to(project))
        failure: Exception | None = None
        for attempt in range(3):
            try:
                sha, summary, manager_requests = await ws.agents.process(
                    project, batch, ws.settings.system_prompt, ws.ui_hooks(project), ws.viewing.get(project)
                )
                failure = None
                break
            except Exception as error:
                log.exception("Agent failed on %s", project)
                failure = error
                if attempt == 2:
                    break
                await hub.send(project, {"type": "agent", "state": "error", "message": f"LLM error, retrying: {error}"})
                await asyncio.sleep(3 * (attempt + 1))
                await hub.send(project, {"type": "agent", "state": "working", "segments": ids})
        if failure is not None:
            changed = ws.transcripts.update(project, ids, status="error")
            await hub.send(project, {"type": "segments", "segments": changed})
            await hub.send(project, {"type": "agent", "state": "error", "message": f"LLM error, gave up: {failure}"})
            return

        changed = ws.transcripts.update(project, ids, status="done", commit=sha)
        await hub.send(project, {"type": "segments", "segments": changed})
        await ws.push_history(project)
        await ws.push_instructions(project)
        await hub.send(project, {"type": "agent", "state": "idle", "summary": summary})
        await hub.send(project, {"type": "activity"})
        # Runs once this project's lock is released, so the manager can safely rename or delete it.
        for request in manager_requests:
            ws.manager_session.add_request(request, project)
