"""Popular short object names for worker instance labels (max 6 letters)."""

from __future__ import annotations

import re
from pathlib import Path

# Lowercase a-z only, length 1–6; no underscores (worker_id uses "_" as separator).
INSTANCE_NAME_RE = re.compile(r"^[a-z]{1,6}$")

_WORDLIST_PATH = Path(__file__).with_name("instance_names_wordlist.txt")


def _load_default_instance_names() -> tuple[str, ...]:
    if not _WORDLIST_PATH.is_file():
        raise FileNotFoundError(
            f"missing instance name wordlist: {_WORDLIST_PATH}"
        )
    names: list[str] = []
    for line in _WORDLIST_PATH.read_text(encoding="utf-8").splitlines():
        word = line.strip().lower()
        if word and INSTANCE_NAME_RE.fullmatch(word):
            names.append(word)
    if len(names) < 500:
        raise ValueError(
            f"instance name wordlist must have at least 500 entries, got {len(names)}"
        )
    return tuple(names)


DEFAULT_INSTANCE_NAMES: tuple[str, ...] = _load_default_instance_names()


def normalize_instance_name(name: str) -> str:
    return (name or "").strip().lower()


def is_valid_instance_name(name: str) -> bool:
    return bool(INSTANCE_NAME_RE.fullmatch(normalize_instance_name(name)))
