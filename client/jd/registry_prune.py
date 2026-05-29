"""Startup registry cleanup — dead workers, orphaned instances, empty experiments."""

from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from jd.worker_registry import (
    exp_cache_dir,
    iter_experiment_registries,
)

# Show a terminal progress bar when scanning at least this many worker rows.
_PROGRESS_MIN_WORKERS = 50
_REGISTRY_FRESH_ENV = "JD_REGISTRY_FRESH"


@dataclass
class PruneSummary:
    workers_removed: int = 0
    instances_released: int = 0
    experiments_removed: int = 0
    token_dirs_removed: int = 0
    workers_scanned: int = 0
    elapsed_sec: float = 0.0


class _TerminalProgress:
    """Single-line stderr progress bar with ETA (only rendered when enabled)."""

    __slots__ = ("_total", "_label", "_done", "_start", "_enabled", "_active")

    def __init__(self, total: int, label: str, *, enabled: bool) -> None:
        self._total = max(int(total), 1)
        self._label = label
        self._done = 0
        self._start = time.monotonic()
        self._enabled = enabled
        self._active = False

    def advance(self, n: int = 1) -> None:
        self._done += n
        if not self._enabled:
            return
        self._active = True
        elapsed = time.monotonic() - self._start
        rate = self._done / elapsed if elapsed > 0 else 0.0
        remaining = max(0, self._total - self._done)
        eta = remaining / rate if rate > 0 else 0.0
        pct = min(100, int(100 * self._done / self._total))
        width = 28
        filled = int(width * self._done / self._total)
        bar = ("=" * filled).ljust(width, " ")
        sys.stderr.write(
            f"\r{self._label} [{bar}] {pct:3d}% "
            f"({self._done}/{self._total}) ETA {eta:5.1f}s"
        )
        sys.stderr.flush()

    def close(self) -> None:
        if self._active:
            sys.stderr.write("\n")
            sys.stderr.flush()


def _cleanup_legacy_token_dirs(exp_path: str) -> int:
    """Remove legacy per-worker ``.token`` directories under an experiment cache."""
    removed = 0
    if not os.path.isdir(exp_path):
        return 0
    for entry in os.listdir(exp_path):
        if entry == "workers.db":
            continue
        token_dir = os.path.join(exp_path, entry)
        if not os.path.isdir(token_dir):
            continue
        token_file = os.path.join(token_dir, ".token")
        if os.path.isfile(token_file):
            try:
                os.remove(token_file)
                os.rmdir(token_dir)
                removed += 1
            except OSError:
                pass
    return removed


def prune_all_registries(
    parent: Optional[str] = None,
    *,
    show_progress: bool = True,
) -> PruneSummary:
    """Deep-clean all local experiment registries on this machine."""
    summary = PruneSummary()
    t0 = time.monotonic()

    registries = iter_experiment_registries(parent)
    if not registries:
        summary.elapsed_sec = time.monotonic() - t0
        return summary

    # Count rows up front so the progress bar total is stable.
    worker_counts: List[int] = []
    for reg in registries:
        worker_counts.append(reg.count_workers())
    total_workers = sum(worker_counts)
    summary.workers_scanned = total_workers

    use_bar = show_progress and total_workers >= _PROGRESS_MIN_WORKERS
    progress = _TerminalProgress(
        total_workers,
        "Cleaning local worker registry",
        enabled=use_bar,
    )
    tick: Callable[[int], None] = progress.advance

    try:
        idx = 0
        for reg, n_workers in zip(registries, worker_counts):
            stats = reg.deep_prune(progress_tick=tick if n_workers else None)
            summary.workers_removed += stats.get("workers_removed", 0)
            summary.instances_released += stats.get("instances_released", 0)
            if stats.get("experiment_removed"):
                summary.experiments_removed += 1
            else:
                summary.token_dirs_removed += _cleanup_legacy_token_dirs(
                    exp_cache_dir(reg.exp_id, parent),
                )
            idx += 1
    finally:
        progress.close()

    summary.elapsed_sec = time.monotonic() - t0
    return summary


def ensure_registry_pruned(parent: Optional[str] = None) -> PruneSummary:
    """Run startup prune once per CLI process; mark registry fresh for hot paths."""
    os.environ.pop(_REGISTRY_FRESH_ENV, None)
    summary = prune_all_registries(parent, show_progress=True)
    os.environ[_REGISTRY_FRESH_ENV] = "1"
    return summary


def registry_recently_pruned() -> bool:
    """True when this process already ran ``ensure_registry_pruned``."""
    return os.environ.get(_REGISTRY_FRESH_ENV) == "1"
