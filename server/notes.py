"""Projects of markdown notes, stored in a git repository. Every change is a commit, which is what undo is built on.

A project is a folder; each note in it is a `.md` file shown as a tab. The tab order lives in the project's
`.order` file, so restoring an old commit brings back the tabs exactly as they were.
"""

import json
import subprocess
import threading
from pathlib import Path

FIELD = "\x1f"
RECORD = "\x1e"
ORDER_FILE = ".order"
DEFAULT_NOTE = "Notes"
FORBIDDEN = set('/\\:*?"<>|')


class NoteError(Exception):
    pass


def clean_name(name: str, kind: str) -> str:
    name = " ".join(name.strip().removesuffix(".md").split())
    if not name or name.startswith(".") or FORBIDDEN & set(name) or len(name) > 80:
        raise NoteError(f"Invalid {kind} name: {name!r}")
    return name


class NoteStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init", "-q", "-b", "main")
            self._git("commit", "-q", "--allow-empty", "-m", "Initialise notes")

    def _git(self, *args: str, stdin: str | None = None) -> str:
        with self._lock:
            result = subprocess.run(
                ["git", "-c", "user.name=notetaker", "-c", "user.email=notetaker@localhost", *args],
                cwd=self.root,
                input=stdin,
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            raise NoteError((result.stderr or result.stdout).strip())
        return result.stdout

    # ---- projects -----------------------------------------------------------------------------

    def _dir(self, project: str) -> Path:
        return self.root / clean_name(project, "project")

    def projects(self) -> list[str]:
        """Project names, most recently changed first."""
        dirs = [p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        changed = {p: max((f.stat().st_mtime for f in p.iterdir()), default=p.stat().st_mtime) for p in dirs}
        return [p.name for p in sorted(dirs, key=lambda p: changed[p], reverse=True)]

    def project_exists(self, project: str) -> bool:
        return self._dir(project).is_dir()

    def create_project(self, project: str) -> str:
        project = clean_name(project, "project")
        if self.project_exists(project):
            raise NoteError(f"Project already exists: {project}")
        self.create_note(project, DEFAULT_NOTE)
        self.commit(project, f"Create project {project}")
        return project

    def delete_projects(self, projects: list[str]) -> None:
        names = [clean_name(p, "project") for p in projects]
        with self._lock:
            for name in names:
                if self.project_exists(name):
                    self._git("rm", "-r", "-q", "-f", "--", name)
            self._git("commit", "-q", "--allow-empty", "-m", f"Delete {', '.join(names)}")

    def rename_project(self, old: str, new: str) -> str:
        old, new = clean_name(old, "project"), clean_name(new, "project")
        if self.project_exists(new):
            raise NoteError(f"Project already exists: {new}")
        with self._lock:
            self.commit(old, f"Save {old} before rename")
            self._git("mv", "--", old, new)
            self._git("commit", "-q", "-m", f"Rename project {old} to {new}")
        return new

    def deleted_projects(self) -> list[dict]:
        """Projects that were deleted and can be brought back, most recently deleted first."""
        out = self._git("log", "--diff-filter=D", f"--format={RECORD}%H{FIELD}%ct", "--name-only", "--", f"*/{ORDER_FILE}")
        found: dict[str, dict] = {}
        for record in out.split(RECORD):
            lines = [line for line in record.strip().splitlines() if line.strip()]
            if not lines:
                continue
            sha, time = lines[0].split(FIELD)
            for path in lines[1:]:
                name = path.split("/")[0]
                if name not in found and not self.project_exists(name):
                    found[name] = {"name": name, "sha": sha, "time": int(time)}
        return list(found.values())

    def undelete_project(self, project: str) -> str:
        """Bring back a deleted project exactly as it was just before it was deleted."""
        wanted = clean_name(project, "project")
        match = next((d for d in self.deleted_projects() if d["name"].lower() == wanted.lower()), None)
        if match is None:
            raise NoteError(f"No deleted project called {wanted}")
        name = match["name"]
        with self._lock:
            self._git("checkout", f"{match['sha']}^", "--", name)
            self._git("commit", "-q", "-m", f"Restore deleted project {name}", "--", name)
        return name

    # ---- notes (tabs) -------------------------------------------------------------------------

    def _file(self, project: str, note: str) -> Path:
        return self._dir(project) / f"{clean_name(note, 'note')}.md"

    def notes(self, project: str) -> list[str]:
        """The project's notes in tab order."""
        folder = self._dir(project)
        on_disk = sorted(p.stem for p in folder.glob("*.md")) if folder.is_dir() else []
        order_file = folder / ORDER_FILE
        saved = json.loads(order_file.read_text()) if order_file.exists() else []
        return [n for n in saved if n in on_disk] + [n for n in on_disk if n not in saved]

    def _save_order(self, project: str, order: list[str]) -> None:
        (self._dir(project) / ORDER_FILE).write_text(json.dumps(order))

    def note_exists(self, project: str, note: str) -> bool:
        return self._file(project, note).is_file()

    def read(self, project: str, note: str) -> str:
        file = self._file(project, note)
        if not file.is_file():
            raise NoteError(f'No note "{note}" in {project}')
        return file.read_text(encoding="utf-8")

    def write(self, project: str, note: str, content: str) -> None:
        """Write to the working tree; changes are committed together at the end of a turn."""
        file = self._file(project, note)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    def create_note(self, project: str, note: str, after: str | None = None) -> str:
        note = clean_name(note, "note")
        if self.note_exists(project, note):
            raise NoteError(f'Note "{note}" already exists')
        order = self.notes(project)
        self.write(project, note, "")
        position = order.index(after) + 1 if after in order else len(order)
        self._save_order(project, [*order[:position], note, *order[position:]])
        return note

    def delete_note(self, project: str, note: str) -> None:
        order = self.notes(project)
        if note not in order:
            raise NoteError(f'No note "{note}"')
        if len(order) == 1:
            raise NoteError("A project needs at least one note")
        self._file(project, note).unlink()
        self._save_order(project, [n for n in order if n != note])

    def rename_note(self, project: str, old: str, new: str) -> str:
        new = clean_name(new, "note")
        order = self.notes(project)
        if old not in order:
            raise NoteError(f'No note "{old}"')
        if new in order:
            raise NoteError(f'Note "{new}" already exists')
        self._file(project, old).rename(self._file(project, new))
        self._save_order(project, [new if n == old else n for n in order])
        return new

    # ---- history ------------------------------------------------------------------------------

    def commit(self, project: str, message: str) -> str | None:
        """Commit everything that changed in the project. Returns the commit sha, or None if nothing changed."""
        folder = clean_name(project, "project")
        with self._lock:
            self._git("add", "-A", "--", folder)
            if not self._git("diff", "--cached", "--name-only", "--", folder).strip():
                return None
            self._git("commit", "-q", "-m", message, "--", folder)
            return self._git("rev-parse", "HEAD").strip()

    def _history_range(self, folder: str) -> list[str]:
        """Commits since this project was created (or renamed to this name).

        A deleted project's commits stay in git, so a new project reusing the name must not see them.
        """
        start = self._git("log", "-1", "--diff-filter=AR", "--format=%H", "--", f"{folder}/{ORDER_FILE}").strip()
        return [f"{start}^..HEAD"] if start else []

    def log(self, project: str, limit: int = 20) -> list[dict]:
        folder = clean_name(project, "project")
        out = self._git(
            "log", f"-n{limit}", f"--format=%H{FIELD}%ct{FIELD}%s{FIELD}%b{RECORD}", *self._history_range(folder), "--", folder
        )
        commits = []
        for record in out.split(RECORD):
            record = record.strip("\n")
            if not record:
                continue
            sha, time, subject, body = record.split(FIELD, 3)
            commits.append(
                {"sha": sha, "short": sha[:7], "time": int(time), "subject": subject, "body": body.strip()}
            )
        return commits

    def _in_history(self, folder: str, sha: str) -> str:
        """Refuse commits from before this project existed (e.g. a deleted project with the same name)."""
        sha = self._verify(sha)
        listed = self._git("rev-list", *(self._history_range(folder) or ["HEAD"]), "--", folder).split()
        if sha not in listed:
            raise NoteError(f"Commit {sha[:7]} is not part of this project's history")
        return sha

    def show(self, project: str, sha: str) -> str:
        folder = clean_name(project, "project")
        return self._git("show", "--format=%s%n%n%b", self._in_history(folder, sha), "--", folder)

    def revert(self, project: str, sha: str) -> None:
        """Undo the changes one commit made to the project, keeping everything after it."""
        folder = clean_name(project, "project")
        sha = self._in_history(folder, sha)
        diff = self._git("diff", f"{sha}^", sha, "--", folder)
        if not diff.strip():
            raise NoteError(f"Commit {sha[:7]} did not change {project}")
        try:
            self._git("apply", "-R", "--whitespace=nowarn", "-", stdin=diff)
        except NoteError as error:
            raise NoteError(
                f"Could not cleanly revert {sha[:7]}: later edits changed the same lines. Restore an earlier version instead."
            ) from error

    def restore(self, project: str, sha: str) -> None:
        """Set the whole project (every note, and the tabs) back to exactly how it was at a commit."""
        folder = clean_name(project, "project")
        sha = self._in_history(folder, sha)
        with self._lock:
            then = set(self._git("ls-tree", "-r", "--name-only", sha, "--", folder).split())
            if not any(name.endswith(".md") for name in then):
                raise NoteError(f"{project} had no notes at commit {sha[:7]}")
            for file in self._dir(project).iterdir():
                if file.is_file() and f"{folder}/{file.name}" not in then:
                    file.unlink()
            self._git("checkout", sha, "--", folder)

    def _verify(self, sha: str) -> str:
        try:
            return self._git("rev-parse", "--verify", "-q", f"{sha.strip()}^{{commit}}").strip()
        except NoteError as error:
            raise NoteError(f"Unknown commit: {sha}") from error
