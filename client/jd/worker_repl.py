"""Interactive REPL for jd_worker_cli (mysql-style shell)."""

from __future__ import annotations

import getpass
import os
import shlex
import sys
from typing import Dict, List, Optional

from jd import __version__
from jd.auth import HUB_API_KEYS_URL, resolve_hub_url, validate_hub_api_key


_REPL_HELP = """\
Interactive commands use the same syntax as the CLI (key=value tokens).

Session
  use <expId>              Set default experiment for this session
  use                      Show current experiment
  expId=<id> <command>     One-off override (also updates session)

Management (expId required unless set via use)
  exp-list                 Experiments with worker counts
  worker-list              List workers
  worker-status <id>       Worker details
  worker-logs <id> [lines=N] [follow=true]
  exp-status               Experiment summary
  server-info              Job counts from server
  where                    Paths (registry, jd_data, logs)
  show-config <id>         Stored launch config
  stop all|<id>|job=<id>   Stop workers
  confirm-stop             Stop all (type name to confirm)
  stop all-experiments     Machine-wide shutdown
  restart all|<id>         Restart workers
  scale num_workers=<N>    Scale worker count
  drain                    Finish current jobs, no new work
  prune                    Remove stale registry rows
  clear_all                Wipe ALL local experiment cache (type clear_all to confirm)

Start workers
  entry_script=<path> [num_workers=N] [machine_type=…] …

Global
  health [expId=<id>]      Hub + server check
  version                  Package info
  help, \\h                This help
  exit, quit, \\q          Leave interactive mode

Trailing semicolons are optional (mysql-style).
"""


def _seed_kv(seed_argv: List[str]) -> Dict[str, str]:
    kv: Dict[str, str] = {}
    for arg in seed_argv:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def _resolve_api_key(kv: Dict[str, str]) -> str:
    return (
        kv.get("api_key")
        or os.environ.get("JD_API_KEY")
        or ""
    ).strip()


def ensure_interactive_api_key(seed_argv: Optional[List[str]] = None) -> None:
    """Require a Hub-valid API key before interactive mode.

    Uses ``JD_API_KEY`` / ``api_key=`` when set; otherwise prompts. Always
    verifies the key with the Hub. On success sets ``JD_API_KEY`` (and
    ``JD_HUB_URL`` when provided) for the rest of the session.
    """
    kv = _seed_kv(seed_argv or [])
    hub_url = resolve_hub_url(hub=kv.get("hub"), hub_url=kv.get("hub_url"))
    api_key = _resolve_api_key(kv)

    if not api_key:
        print(
            "Hub API key required for interactive mode.\n"
            f"Set export JD_API_KEY=jd_… or create a key at {HUB_API_KEYS_URL}"
        )
        try:
            api_key = getpass.getpass("API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

    while True:
        if not api_key:
            try:
                api_key = getpass.getpass("API key: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(1)
            if not api_key:
                continue

        print(f"Checking API key with Hub ({hub_url})…")
        valid, err = validate_hub_api_key(hub_url, api_key)
        if valid:
            os.environ["JD_API_KEY"] = api_key
            os.environ["JD_HUB_URL"] = hub_url
            return

        print(err)
        print(f"Manage keys at {HUB_API_KEYS_URL}")
        try:
            api_key = getpass.getpass("Enter API key again (Ctrl+C to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)


def _history_path() -> str:
    from jd.worker_registry import resolve_cache_parent

    root = resolve_cache_parent()
    hist_dir = os.path.join(root, ".cache")
    os.makedirs(hist_dir, exist_ok=True)
    return os.path.join(hist_dir, "jd_worker_history")


def _load_readline() -> None:
    try:
        import readline
    except ImportError:
        return
    histfile = _history_path()
    try:
        readline.read_history_file(histfile)
    except OSError:
        pass
    readline.set_history_length(2000)


def _save_readline() -> None:
    try:
        import readline
    except ImportError:
        return
    try:
        readline.write_history_file(_history_path())
    except OSError:
        pass


def _parse_tokens(line: str) -> List[str]:
    line = line.strip().rstrip(";").strip()
    if not line:
        return []
    try:
        return shlex.split(line, posix=not sys.platform.startswith("win"))
    except ValueError as exc:
        print(f"Parse error: {exc}")
        return []


def _merge_session_argv(session: Dict[str, str], tokens: List[str]) -> List[str]:
    """Inject session expId when the line omits it."""
    has_exp = any(t.startswith("expId=") for t in tokens)
    argv: List[str] = []
    if session.get("expId") and not has_exp:
        argv.append(f"expId={session['expId']}")
    argv.extend(tokens)
    return argv


def _update_session_from_tokens(session: Dict[str, str], tokens: List[str]) -> None:
    for token in tokens:
        if token.startswith("expId="):
            session["expId"] = token.split("=", 1)[1].strip().lower()


def _prompt(session: Dict[str, str]) -> str:
    exp = session.get("expId", "")
    return f"jd[{exp}]> " if exp else "jd> "


def _print_banner(session: Dict[str, str]) -> None:
    print(f"jd_worker_cli {__version__} — interactive mode")
    if session.get("expId"):
        print(f"Experiment: {session['expId']}  (use <name> to switch)")
    else:
        print("No experiment selected — use my_exp  or  expId=my_exp worker-list")
    print("Type help for commands, exit or Ctrl-D to quit.")


def _handle_builtin(line: str, session: Dict[str, str]) -> bool:
    """Return True if the line was handled (do not dispatch)."""
    lower = line.lower()
    if lower in ("exit", "quit", "\\q"):
        return True
    if lower in ("help", "\\h", "?"):
        print(_REPL_HELP)
        return True
    if lower == "use":
        exp = session.get("expId")
        print(f"Current experiment: {exp or '(none)'}")
        return True
    if lower.startswith("use "):
        name = line[4:].strip()
        if name.startswith("expId="):
            name = name.split("=", 1)[1].strip()
        if not name:
            print("Usage: use <expId>")
        else:
            session["expId"] = name.lower()
            print(f"Using experiment '{session['expId']}'.")
        return True
    return False


def _seed_session(session: Dict[str, str], seed_argv: List[str]) -> None:
    for arg in seed_argv:
        if arg.startswith("expId="):
            session["expId"] = arg.split("=", 1)[1].strip().lower()


def run_repl(seed_argv: Optional[List[str]] = None) -> None:
    """Run the interactive shell until the user exits."""
    from jd.worker_commands import dispatch

    ensure_interactive_api_key(seed_argv)

    session: Dict[str, str] = {}
    env_exp = (os.environ.get("JD_EXP_ID") or "").strip().lower()
    if env_exp:
        session["expId"] = env_exp
    if seed_argv:
        _seed_session(session, seed_argv)

    _load_readline()
    _print_banner(session)

    while True:
        try:
            line = input(_prompt(session))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        line = line.strip()
        if not line:
            continue

        if _handle_builtin(line, session):
            if line.lower() in ("exit", "quit", "\\q"):
                break
            continue

        tokens = _parse_tokens(line)
        if not tokens:
            continue

        _update_session_from_tokens(session, tokens)
        argv = _merge_session_argv(session, tokens)

        try:
            dispatch(argv)
        except SystemExit:
            pass

    _save_readline()
