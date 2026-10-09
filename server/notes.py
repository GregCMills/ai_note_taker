"""Markdown notes stored in a git repository. Every change is a commit, which is what undo is built on."""

import subprocess
import threading
from pathlib import Path

FIELD = "\x1f"
RECORD = "\x1e"


class NoteError(Exception):
    pass


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

    def normalise(self, path: str) -> str:
        clean = path.strip().strip("/")
        if not clean.endswith(".md"):
            clean += ".md"
        full = (self.root / clean).resolve()
        if self.root not in full.parents or ".git" in full.relative_to(self.root).parts:
            raise NoteError(f"Invalid note path: {path}")
        return full.relative_to(self.root).as_posix()

    def _file(self, path: str) -> Path:
        return self.root / self.normalise(path)

    def paths(self) -> list[str]:
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*.md")
            if ".git" not in p.relative_to(self.root).parts
        )

    def exists(self, path: str) -> bool:
        return self._file(path).is_file()

    def read(self, path: str) -> str:
        file = self._file(path)
        if not file.is_file():
            raise NoteError(f"No such note: {path}")
        return file.read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        """Write to the working tree without committing."""
        file = self._file(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    def create(self, path: str) -> str:
        path = self.normalise(path)
        if self.exists(path):
            raise NoteError(f"Note already exists: {path}")
        title = Path(path).stem.replace("-", " ").replace("_", " ")
        self.write(path, f"# {title}\n")
        self.commit(path, f"Create {path}")
        return path

    def delete(self, path: str) -> None:
        path = self.normalise(path)
        with self._lock:
            self._git("rm", "-q", "-f", "--", path)
            self._git("commit", "-q", "-m", f"Delete {path}")

    def rename(self, old: str, new: str) -> str:
        old, new = self.normalise(old), self.normalise(new)
        if self.exists(new):
            raise NoteError(f"Note already exists: {new}")
        (self.root / new).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.commit(old, f"Save {old} before rename")
            self._git("mv", "--", old, new)
            self._git("commit", "-q", "-m", f"Rename {old} to {new}")
        return new

    def commit(self, path: str, message: str) -> str | None:
        """Commit the note if it changed. Returns the new commit sha, or None if nothing changed."""
        path = self.normalise(path)
        with self._lock:
            if not self._git("status", "--porcelain", "--", path).strip():
                return None
            self._git("add", "--", path)
            self._git("commit", "-q", "-m", message, "--", path)
            return self._git("rev-parse", "HEAD").strip()

    def log(self, path: str, limit: int = 20) -> list[dict]:
        path = self.normalise(path)
        out = self._git(
            "log", "--follow", f"-n{limit}", f"--format=%H{FIELD}%ct{FIELD}%s{FIELD}%b{RECORD}", "--", path
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

    def show(self, path: str, sha: str) -> str:
        path = self.normalise(path)
        return self._git("show", "--format=%s%n%n%b", self._verify(sha), "--", path)

    def revert(self, path: str, sha: str) -> None:
        """Undo the changes one commit made to this note, keeping everything after it."""
        path = self.normalise(path)
        sha = self._verify(sha)
        diff = self._git("diff", f"{sha}^", sha, "--", path)
        if not diff.strip():
            raise NoteError(f"Commit {sha[:7]} did not change {path}")
        try:
            self._git("apply", "-R", "--whitespace=nowarn", "-", stdin=diff)
        except NoteError as error:
            raise NoteError(
                f"Could not cleanly revert {sha[:7]}: later edits changed the same lines. "
                "Use git_restore to an earlier commit, or edit the note directly."
            ) from error
        if not self._file(path).exists():
            self.write(path, "")

    def restore(self, path: str, sha: str) -> None:
        """Set the note back to exactly how it was at a commit."""
        path = self.normalise(path)
        sha = self._verify(sha)
        try:
            content = self._git("show", f"{sha}:{path}")
        except NoteError as error:
            raise NoteError(f"{path} did not exist at commit {sha[:7]}") from error
        self.write(path, content)

    def _verify(self, sha: str) -> str:
        try:
            return self._git("rev-parse", "--verify", "-q", f"{sha.strip()}^{{commit}}").strip()
        except NoteError as error:
            raise NoteError(f"Unknown commit: {sha}") from error
