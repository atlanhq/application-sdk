"""Turn on SSH signing for every later commit in a job, as the CI machine user.

Writes the signing key to the runner's temp dir, checks it is a usable unencrypted
OpenSSH private key, sets git's global signing config, and exports the committer
identity through GITHUB_ENV. GitHub verifies a commit signature against the
committer, so only the committer is set: each bot keeps its own author, and gates
that recognise a bot's commits by author keep working.

With no key it warns and changes nothing, so a repo the secret is not shared with
behaves exactly as an unsigned job did.

Stdlib only: the action also runs on ubuntu-slim, whose system Python has nothing
else installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess]


class SigningError(Exception):
    """The job cannot sign; the message is safe to print (it never holds the key)."""


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def checked_identity(name: str, email: str) -> Identity:
    """The committer, refused if it could break out of a GITHUB_ENV line.

    GITHUB_ENV is line-oriented: a newline in a value starts a new assignment that
    every later step inherits (e.g. PATH=...). Any control character is refused.
    """
    for label, value in (("committer-name", name), ("committer-email", email)):
        if not value.strip():
            raise SigningError(f"{label} is empty.")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise SigningError(f"{label} contains a control character.")
    if "@" not in email:
        raise SigningError("committer-email is not an email address.")
    return Identity(name=name, email=email)


def write_key(key: str, temp_dir: Path) -> Path:
    """The key as a 0600 file under the runner's temp dir, which is wiped at job end."""
    path = temp_dir / "bot-signing-key"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key.strip() + "\n")
    return path


def fingerprint(key_file: Path, run: Runner) -> str:
    """The key's public fingerprint; raises when it is not a usable private key."""
    if shutil.which("ssh-keygen") is None:
        raise SigningError(
            "ssh-keygen is not on this runner; SSH commit signing needs openssh-client."
        )
    derived = run(
        ["ssh-keygen", "-y", "-f", str(key_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    if derived.returncode != 0:
        raise SigningError(
            "The signing key is not a valid unencrypted OpenSSH private key."
        )
    listed = run(
        ["ssh-keygen", "-l", "-f", str(key_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = listed.stdout.split()
    return parts[1] if listed.returncode == 0 and len(parts) > 1 else "unknown"


def configure(
    key: str,
    identity: Identity,
    *,
    temp_dir: Path,
    github_env: Path,
    run: Runner = subprocess.run,
) -> str | None:
    """Turn signing on and return the key's fingerprint, or None when there is no key."""
    if not key.strip():
        return None
    key_file = write_key(key, temp_dir)
    try:
        fp = fingerprint(key_file, run)
    except SigningError:
        key_file.unlink(missing_ok=True)
        raise
    for setting, value in (
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key_file)),
        ("commit.gpgsign", "true"),
    ):
        run(["git", "config", "--global", setting, value], check=True)
    with github_env.open("a", encoding="utf-8") as fh:
        fh.write(f"GIT_COMMITTER_NAME={identity.name}\n")
        fh.write(f"GIT_COMMITTER_EMAIL={identity.email}\n")
    return fp


def main(env: dict[str, str] | None = None, run: Runner = subprocess.run) -> int:
    env = dict(os.environ) if env is None else env
    output = Path(env["GITHUB_OUTPUT"])
    try:
        identity = checked_identity(
            env.get("COMMITTER_NAME", ""), env.get("COMMITTER_EMAIL", "")
        )
        signed_with = configure(
            env.get("SIGNING_KEY", ""),
            identity,
            temp_dir=Path(env["RUNNER_TEMP"]),
            github_env=Path(env["GITHUB_ENV"]),
            run=run,
        )
    except SigningError as exc:
        print(f"::error::{exc}")
        return 1
    with output.open("a", encoding="utf-8") as fh:
        fh.write(f"signing={'true' if signed_with else 'false'}\n")
    if signed_with is None:
        print(
            "::warning::No signing key provided; commits from this job will be unsigned "
            "and PRs carrying them stay blocked by the signed-commits ruleset."
        )
    else:
        print(
            f"Signing commits, committed as {identity.name} <{identity.email}>, with key {signed_with}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
