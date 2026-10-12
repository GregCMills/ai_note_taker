"""Accounts: each person signs in and gets their own private workspace under data/users/<name>.

There is no sign-up page; accounts are managed from the command line inside the app container:
    python -m server.accounts add <name>       asks for a password
    python -m server.accounts passwd <name>    also signs them out everywhere
    python -m server.accounts remove <name>    their notes stay on disk
    python -m server.accounts list
"""

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

from . import config

ACCOUNTS_FILE = config.DATA_DIR / "accounts.json"
SECRET_FILE = config.DATA_DIR / "session.key"
USERS_DIR = config.DATA_DIR / "users"
SESSION_COOKIE = "session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 10
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# What data/ held before accounts existed; the first account created takes it over.
PRE_ACCOUNT_DATA = ("notes", "transcripts", "memory", "agentlog", "settings.json")


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(_scrypt(password, salt)).decode()}"


def _check_password(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(_scrypt(password, base64.b64decode(salt)), base64.b64decode(digest))


# Checked against when the name doesn't exist, so a wrong name takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))

_cache: tuple[float, dict] | None = None


def load() -> dict[str, dict]:
    """name -> {"password": hash}. Re-read whenever the file changes, so CLI edits apply without a restart."""
    global _cache
    try:
        mtime = ACCOUNTS_FILE.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache is None or _cache[0] != mtime:
        _cache = (mtime, json.loads(ACCOUNTS_FILE.read_text()))
    return _cache[1]


def _save(users: dict[str, dict]) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ACCOUNTS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(users, indent=2))
    temporary.chmod(0o600)
    _give_to_app_user(temporary)
    os.replace(temporary, ACCOUNTS_FILE)


def _give_to_app_user(path: Path) -> None:
    """`docker compose exec` runs as root, but the server runs as APP_UID and must be able to read what we write."""
    if os.geteuid() == 0 and "APP_UID" in os.environ:
        os.chown(path, int(os.environ["APP_UID"]), int(os.environ.get("APP_GID", os.environ["APP_UID"])))


def verify(name: str, password: str) -> bool:
    """Deliberately slow (scrypt); call it off the event loop."""
    user = load().get(name)
    ok = _check_password(password, user["password"] if user else _DUMMY_HASH)
    return ok and user is not None


# ---- sessions: a signed cookie, so nothing needs storing per login ----------------------------------

def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_bytes()


def _signature(payload: str, password_hash: str) -> str:
    # Signing over the password hash means changing someone's password signs out all their sessions.
    return hmac.new(_secret(), f"{payload}|{password_hash}".encode(), hashlib.sha256).hexdigest()


def issue_token(name: str) -> str:
    payload = f"{name}:{int(time.time()) + SESSION_SECONDS}"
    return f"{payload}:{_signature(payload, load()[name]['password'])}"


def user_for_token(token: str | None) -> str | None:
    """The signed-in user, or None for a missing, forged, expired or revoked session."""
    if not token or token.count(":") != 2:
        return None
    name, expires, signature = token.split(":")
    user = load().get(name)
    if user is None or not expires.isdigit() or int(expires) < time.time():
        return None
    return name if hmac.compare_digest(signature, _signature(f"{name}:{expires}", user["password"])) else None


class LoginThrottle:
    """Refuses logins after too many recent failures from one address, or for one account."""

    WINDOW_S = 15 * 60
    LIMITS = {"ip": 5, "user": 10}

    def __init__(self):
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.WINDOW_S
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def allowed(self, ip: str, name: str) -> bool:
        return len(self._recent(f"ip:{ip}")) < self.LIMITS["ip"] and len(self._recent(f"user:{name}")) < self.LIMITS["user"]

    def failed(self, ip: str, name: str) -> None:
        for key in (f"ip:{ip}", f"user:{name}"):
            self._failures.setdefault(key, []).append(time.monotonic())

    def succeeded(self, ip: str, name: str) -> None:
        self._failures.pop(f"ip:{ip}", None)
        self._failures.pop(f"user:{name}", None)


# ---- command line ----------------------------------------------------------------------------------

def _ask_password() -> str:
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
        elif password != getpass.getpass("Again: "):
            print("Those didn't match.")
        else:
            return password


def _adopt_pre_account_data(name: str) -> None:
    """Move notes made before accounts existed into the first account, so nothing is lost."""
    found = [item for item in PRE_ACCOUNT_DATA if (config.DATA_DIR / item).exists()]
    if not found:
        return
    home = USERS_DIR / name
    home.mkdir(parents=True, exist_ok=True)
    for item in found:
        if not (home / item).exists():
            shutil.move(config.DATA_DIR / item, home / item)
    for path in (USERS_DIR, home):
        _give_to_app_user(path)
    print(f"Moved your existing {', '.join(found)} into {name}'s workspace.")


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] not in {"add", "passwd", "remove", "list"} or (argv[0] != "list" and len(argv) != 2):
        print(__doc__)
        return 2
    command, users = argv[0], dict(load())
    if command == "list":
        print("\n".join(sorted(users)) or "(no accounts yet)")
        return 0

    name = argv[1].strip().lower()
    if command == "add":
        if not NAME_PATTERN.fullmatch(name):
            print("Names are 1-32 characters: lowercase letters, digits, - and _, starting with a letter or digit.")
            return 1
        if name in users:
            print(f"{name} already exists; use passwd to change their password.")
            return 1
        first = not users
        users[name] = {"password": hash_password(_ask_password())}
        _save(users)
        if first:
            _adopt_pre_account_data(name)
        print(f"Added {name}.")
    elif name not in users:
        print(f"No account called {name}.")
        return 1
    elif command == "passwd":
        users[name]["password"] = hash_password(_ask_password())
        _save(users)
        print(f"Changed {name}'s password; they are signed out everywhere.")
    else:
        del users[name]
        _save(users)
        print(f"Removed {name}. Their notes are still in {USERS_DIR / name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
