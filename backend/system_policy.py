"""Opt-in BlueZ repair policy. Run mutations explicitly through pkexec; never restart BlueZ."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

NOTE = "Restart Bluetooth explicitly to apply policy changes; restarting disconnects all devices."


class PolicyError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_policy(data):
    """Return the configured value and a byte-preserving confirm version."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError("Configuration is not UTF-8") from exc
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text) or "\r" in text.replace("\r\n", ""):
        raise PolicyError("Malformed configuration")
    lines = text.splitlines(keepends=True)
    sections, keys = set(), set()
    section = None
    general_end = None
    match_line = None
    value = None
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if not body.strip() or body.lstrip().startswith(("#", ";")):
            continue
        header = re.fullmatch(r"\s*\[([A-Za-z0-9_.-]+)\]\s*(?:[#;].*)?", body)
        if header:
            if section == "General":
                general_end = index
            section = header[1]
            if section.lower() in sections:
                raise PolicyError("Duplicate section")
            sections.add(section.lower())
            if section.lower() == "general" and section != "General":
                raise PolicyError("Ambiguous General section spelling")
            continue
        setting = re.fullmatch(r"(\s*([A-Za-z0-9_.-]+)\s*=\s*)(.*?)(\s*(?:[#;].*)?)", body)
        if not setting or section is None:
            raise PolicyError("Malformed INI setting")
        key = (section.lower(), setting[2].lower())
        if key in keys:
            raise PolicyError("Duplicate setting")
        keys.add(key)
        if section == "General" and setting[2].lower() == "justworksrepairing":
            if setting[2] != "JustWorksRepairing":
                raise PolicyError("Ambiguous policy spelling")
            value = setting[3].strip()
            if value not in ("always", "never", "confirm"):
                raise PolicyError("Unknown JustWorksRepairing value")
            ending = line[len(body):]
            match_line = (index, setting[1] + "confirm" + setting[4] + ending)
    newline = "\r\n" if "\r\n" in text else "\n"
    if match_line:
        lines[match_line[0]] = match_line[1]
    else:
        if section == "General":
            general_end = len(lines)
        if "general" not in sections:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += newline
            lines.extend(["[General]" + newline, "JustWorksRepairing=confirm" + newline])
        else:
            if general_end and not lines[general_end - 1].endswith("\n"):
                lines[general_end - 1] += newline
            lines.insert(general_end, "JustWorksRepairing=confirm" + newline)
    return value, "".join(lines).encode("utf-8")


class SystemPolicy:
    # Injection is for tests/embedding only. CLI deliberately exposes no paths.
    def __init__(self, config=Path("/etc/bluetooth/main.conf"),
                 state_dir=Path("/var/lib/omarchy-airpods")):
        self.config = Path(config)
        self.directory = Path(state_dir)
        self.state = self.directory / "state.json"
        self.backup = self.directory / "backup.bin"
        self.lock = self.directory / "policy.lock"

    @staticmethod
    def _check(path):
        for part in (path, *path.parents):
            if part.is_symlink():
                raise PolicyError("Symlink refused: " + str(part))

    @classmethod
    def _read(cls, path):
        cls._check(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PolicyError("Expected a regular file with one link: " + str(path))
            return stream.read(), stat.S_IMODE(info.st_mode)

    @staticmethod
    def _sync(directory):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, path, data, mode):
        self._check(path)
        fd, name = tempfile.mkstemp(prefix=".airpods-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            self._sync(path.parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _save(self, state):
        self._write(self.state, json.dumps(state, sort_keys=True).encode(), 0o600)

    def _load(self):
        for path in (self.config, self.directory, self.state, self.backup, self.lock):
            self._check(path)
        if not self.state.exists():
            return None
        raw, mode = self._read(self.state)
        try:
            state = json.loads(raw)
            valid = (mode == 0o600 and state["version"] == 1
                     and state["phase"] in ("prepared", "enabled", "restoring")
                     and type(state["mode"]) is int and 0 <= state["mode"] <= 0o7777
                     and all(re.fullmatch(r"[0-9a-f]{64}", state[k])
                             for k in ("before", "after")))
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise PolicyError("Invalid transaction state")
        # During restore cleanup the backup can already be gone.
        if state["phase"] == "enabled" or self.backup.exists():
            prior, mode = self._read(self.backup)
            if mode != 0o600 or digest(prior) != state["before"]:
                raise PolicyError("Backup integrity failure")
        return state

    @contextlib.contextmanager
    def _locked(self):
        self._check(self.directory)
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        # Persist the journal directory entry before any transaction writes.
        # Also sync on retry: a prior mkdir may have survived a failed fsync.
        self._sync(self.directory.parent)
        info = self.directory.stat()
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise PolicyError("State directory must be owned by the caller with mode 0700")
        self._check(self.lock)
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise PolicyError("Invalid lock file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _current(self):
        try:
            return self._read(self.config)
        except FileNotFoundError as exc:
            raise PolicyError("Missing Bluetooth configuration; refusing to create it") from exc

    @staticmethod
    def _matches(current, state, key):
        return digest(current[0]) == state[key] and current[1] == state["mode"]

    def _cleanup(self):
        # Keep restoring metadata until backup deletion is durable.
        self.backup.unlink(missing_ok=True)
        self._sync(self.directory)
        self.state.unlink(missing_ok=True)
        self._sync(self.directory)

    def _recover(self, state):
        if not state:
            # Never discard an unexplained backup from an older or damaged transaction.
            if self.backup.exists():
                raise PolicyError("Orphan backup; retain it for manual recovery")
            return None
        current = self._current()
        if state["phase"] == "restoring":
            if self._matches(current, state, "before"):
                self._cleanup()
                return None
            if not self._matches(current, state, "after"):
                raise PolicyError("Configuration changed; refusing recovery")
            prior, _ = self._read(self.backup)
            self._write(self.config, prior, state["mode"])
            self._cleanup()
            return None
        if state["phase"] == "prepared":
            if not self.backup.exists():
                if not self._matches(current, state, "before"):
                    raise PolicyError("Missing backup; refusing recovery")
                self._write(self.backup, current[0], 0o600)
            if self._matches(current, state, "before"):
                _, changed = parse_policy(current[0])
                if digest(changed) != state["after"]:
                    raise PolicyError("Transaction content mismatch")
                self._write(self.config, changed, state["mode"])
            elif not self._matches(current, state, "after"):
                raise PolicyError("Configuration changed; refusing recovery")
            state["phase"] = "enabled"
            self._save(state)
        return state

    def status(self):
        current = self._current()
        value, _ = parse_policy(current[0])
        readable = True
        try:
            state = self._load()
        except PermissionError:
            state = None
            readable = False
        return {"policy": value, "configured": value is not None,
                "state_readable": readable,
                "managed": (state is not None) if readable else None,
                "phase": state["phase"] if state else None,
                "conflict": bool(state and not self._matches(current, state, "after")) if readable else None,
                "restart_required": None, "note": NOTE}

    def enable(self):
        with self._locked():
            state = self._recover(self._load())
            current = self._current()
            if state:
                if not self._matches(current, state, "after"):
                    raise PolicyError("Configuration changed since enable; refusing reapply")
            else:
                _, changed = parse_policy(current[0])
                state = {"version": 1, "phase": "prepared", "mode": current[1],
                         "before": digest(current[0]), "after": digest(changed)}
                self._save(state)
                self._recover(state)
            return {**self.status(), "restart_required": True}

    def restore(self):
        with self._locked():
            state = self._recover(self._load())
            if state:
                if not self._matches(self._current(), state, "after"):
                    raise PolicyError("Configuration changed since enable; refusing restore")
                state["phase"] = "restoring"
                self._save(state)
                self._recover(state)
            return {**self.status(), "restart_required": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "restore", "status"))
    args = parser.parse_args(argv)
    try:
        if args.action != "status" and os.geteuid() != 0:
            raise PolicyError("Explicit root invocation required: pkexec /usr/bin/python3 -I backend/system_policy.py " + args.action)
        result = getattr(SystemPolicy(), args.action)()
    except (PolicyError, OSError) as exc:
        print(json.dumps({"error": str(exc), "note": NOTE}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
