"""Completion detection for scheduled tasks.

Handles:
- Task context files (coordinate between dispatch and any completion hook)
- System summary prompt (ask agent to write summary)
- Summary file parsing (extract status from YAML front matter)
- Completion signals (headless process exit and/or summary file)
"""

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple


class CompletionError(Exception):
    """Raised when completion detection fails."""

    pass


class CompletionTimeout(CompletionError):  # noqa: N818  # public API name, renaming breaks callers
    """Raised when waiting for completion times out."""

    pass


# Directory for task coordination files
TASKS_DIR = Path.home() / ".agentwire" / "tasks"

# Shells that indicate agent died and fell back to a bare shell. A Hermes
# agent is a python process (running `hermes chat`), NOT one of these — see
# `_session_has_agent` for why python is handled separately.
_BARE_SHELLS = {"zsh", "bash", "sh", "fish", "tcsh", "csh"}

# python interpreters whose cmdline must be inspected to tell an agent-python
# (running `hermes`) from a daemon-python (running `agentwire`).
_PYTHON_INTERPRETERS = {"python", "python3"}


def _pid_is_hermes_agent(pid: str) -> bool:
    """Is the process *pid* a Hermes agent (not the agentwire daemon)?

    A Hermes agent runs `hermes chat ...` under a python interpreter; the
    scheduler/portal daemon runs `agentwire ...`. Inspecting the cmdline is
    what keeps a daemon-python pane from reading as "agent alive" and hanging
    the wait forever.
    """
    if not pid:
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "args="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    cmdline = result.stdout.strip()
    if not cmdline:
        return False
    return "hermes" in cmdline and "agentwire" not in cmdline


def _command_is_agent(command: str, pid: str | None = None) -> bool:
    """Is this pane's process an agent (not a bare shell, not a daemon)?

    Non-shell commands read as "agent alive" (existing behavior); a python
    interpreter additionally requires the cmdline to reference `hermes`, so a
    daemon-python or a stray python process does not masquerade as the agent.
    """
    cmd = (command or "").strip().lower()
    if cmd in _BARE_SHELLS:
        return False
    if cmd in _PYTHON_INTERPRETERS or cmd.startswith("python3"):
        return _pid_is_hermes_agent(pid or "")
    return True


def _session_has_agent(session: str) -> bool:
    """Check if session exists and has an agent running in any pane.

    A Hermes agent runs as a python process; the pane command alone can't tell
    it from a daemon-python, so python panes are resolved via the process
    cmdline (``_pid_is_hermes_agent``).
    """
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"={session}",
         "-F", "#{pane_current_command}\t#{pane_pid}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False  # Session doesn't exist

    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        command = parts[0]
        pid = parts[1] if len(parts) > 1 else ""
        if _command_is_agent(command, pid):
            return True

    return False


class SummaryResult(NamedTuple):
    """Parsed result from a task summary file."""

    status: str  # complete, incomplete, failed
    summary: str  # One-line summary
    files_modified: list[str]  # List of modified files
    blockers: list[str]  # List of blockers (if any)
    raw_content: str  # Full file content


# System prompt sent after task completion to get structured summary
SYSTEM_SUMMARY_PROMPT = """Write a task summary to {summary_file} in YAML front matter format:

```markdown
---
status: complete | incomplete | failed
summary: one line describing what you accomplished
files_modified:
  - path/to/file1
  - path/to/file2
blockers:
  - any issues preventing completion
---

Additional notes about what was done, challenges encountered, etc.
```

Status meanings:
- complete: Task finished successfully
- incomplete: Task partially done, more work needed (not a failure)
- failed: Task could not be completed due to errors

Write the file now."""


def generate_summary_filename(session: str, task_name: str) -> str:
    """Generate a session-scoped timestamped summary filename.

    Includes session name so multiple sessions sharing a project directory
    don't collide on summary files or trigger false TASK-ORPHAN detection.

    Args:
        session: Session name
        task_name: Task name (for context)

    Returns:
        Relative path like .agentwire/task-summary-mysession-2024-01-15T07-00-00.md
    """
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f".agentwire/task-summary-{session}-{task_name}-{timestamp}.md"


# =============================================================================
# Task Context (coordinate between dispatch and any completion hook)
# =============================================================================


def write_task_context(
    session: str,
    task_name: str,
    summary_file: str,
    attempt: int = 1,
    exit_on_complete: bool = True,
    mode: str = "standard",
    max_iterations: int = 3,
    iteration: int = 1,
    loop_review: bool = True,
    loop_delay: int = 0,
    original_prompt: str = "",
) -> Path:
    """Write task context file for completion coordination.

    The context records:
    - A scheduled task is running
    - What summary file to request
    - Whether to exit the session after completion
    - Loop mode configuration (mode, iteration count, review flag, delay)

    Args:
        session: Session name
        task_name: Task being executed
        summary_file: Relative path for summary file
        attempt: Current attempt number
        exit_on_complete: Whether to exit session after task completion
        mode: Task mode ("standard" or "loop")
        max_iterations: Maximum loop iterations (loop mode only)
        iteration: Current iteration number (loop mode only)
        loop_review: Whether to write review files between iterations
        loop_delay: Seconds to wait between loop iterations (loop mode only)
        original_prompt: Fully expanded task prompt (for re-sending in loop mode)

    Returns:
        Path to the context file
    """
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    context = {
        "task": task_name,
        "summary_file": summary_file,
        "started_at": datetime.now().isoformat(),
        "attempt": attempt,
        "idle_count": 0,  # Hook increments this
        "exit_on_complete": exit_on_complete,
        "mode": mode,
        "max_iterations": max_iterations,
        "iteration": iteration,
        "loop_review": loop_review,
        "loop_delay": loop_delay,
        "original_prompt": original_prompt,
    }

    context_file = TASKS_DIR / f"{session}.json"
    # Worktree session names contain a slash (e.g. "proj/branch"), which
    # nests the context file one directory down — create it.
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_text(json.dumps(context, indent=2))
    return context_file


def clear_task_context(session: str) -> None:
    """Remove task context and completion signal files.

    Args:
        session: Session name
    """
    context_file = TASKS_DIR / f"{session}.json"
    try:
        context_file.unlink(missing_ok=True)
    except OSError:
        pass


# =============================================================================
# Summary-file discovery and parsing helpers
# =============================================================================


def _find_summary(
    summary_path: Path | None,
    summary_glob: str | None,
    context_file: Path,
) -> Path | None:
    """Locate the written summary file, exact path first, then glob.

    Agents sometimes invent their own timestamp instead of using the provided
    filename, so a fuzzy glob catches nearby matches. A glob match is only
    accepted if written after the context file (i.e. by this run).
    """
    if summary_path and summary_path.exists() and summary_path.stat().st_size > 0:
        return summary_path
    if summary_path and summary_glob:
        parent = summary_path.parent
        candidates = sorted(
            parent.glob(summary_glob),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            newest = candidates[0]
            if context_file.exists():
                ctx_mtime = context_file.stat().st_mtime
                if newest.stat().st_mtime > ctx_mtime and newest.stat().st_size > 0:
                    return newest
    return None


def _finalize_summary(found_summary: Path) -> dict | None:
    """Parse a found summary file into the wait's result, clearing any outage.

    Returns None when the file is only partially written (caller should
    retry), else the completion dict. A written summary is proof a turn
    actually ran, which is proof the credentials work — so any recorded
    outage is cleared here, the hook behind "reopens on the first successful
    turn".
    """
    time.sleep(0.5)
    try:
        result = parse_summary_file(found_summary)
    except CompletionError:
        return None  # File may be partially written, retry
    from .auth_expired import clear_state

    clear_state()
    return {
        "status": result.status,
        "summary": result.summary,
        "summary_file": str(found_summary),
    }


def _read_process_stderr(process) -> str:
    """Best-effort read of a finished subprocess's stderr (or a passed string)."""
    stderr = getattr(process, "stderr", None)
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", "replace")
    if isinstance(stderr, str):
        return stderr
    try:
        return stderr.read() or ""
    except (OSError, ValueError):
        return ""


def wait_for_completion_signal(
    session: str,
    poll_interval: float = 10.0,
    summary_path: Path | None = None,
    max_duration: int = 0,
    transcript_since: float | None = None,
    process=None,
    provider: str | None = None,
    stderr: str | None = None,
) -> dict:
    """Wait for task completion.

    Two dispatch models are supported:

    * **Headless** (``process`` given) — completion is the ``hermes -z``/``-q``
      process exit (0/1). The agent writes the summary file as its final step
      (instructed via the summary prompt), so the wait returns the parsed
      summary once the process has exited. No idle polling, no ``/exit``.
    * **REPL** (``process`` is None) — the summary file (plus context-file
      cleanup) is the signal, with the usage-limit, auth-failure and
      agent-death guards below.

    Exits:
    1. Summary file appears (task completed normally)
    2. Headless process exits (0/1) and the summary is read; a hard auth
       failure in its stderr is reported as ``status=auth_expired``
    3. Session dies (agent crashed, tmux killed)
    4. ``max_duration`` elapses, when the task sets one
    5. The session is parked on a usage limit (``status=usage_limit``)
    6. Hermes refuses the turn for an expired provider auth
       (``status=auth_expired``) — terminal until a human re-auths

    ``max_duration`` is a per-attempt wall clock, not an idle timer.

    Args:
        session: Session name
        poll_interval: Seconds between checks
        summary_path: Path to the summary .md file the agent will write
        max_duration: Seconds before giving up (0 = unbounded)
        transcript_since: Epoch floor for "was this turn produced by the
            current attempt?" — the attempt's start. In headless mode it is
            not needed (the process exit is the anchor); kept for the REPL
            message-scan floor and callers that still pass it.
        process: A ``subprocess.Popen`` handle for headless ``hermes -z``/``-q``
            dispatch. When given, the wait blocks on this process rather than
            polling a tmux pane.
        provider: The effective provider for the headless run, enabling the
            ``hermes auth status`` pre-flight on failure.
        stderr: Captured stderr of a finished headless run (used when
            ``process`` is not a live handle but its output is already known).

    Returns:
        Dict with 'status', 'summary', 'summary_file' keys

    Raises:
        CompletionTimeout: If the session dies, the headless run fails without
            a summary, or max_duration elapses, before the task completed.
    """
    # Build glob pattern for fuzzy summary detection (agents sometimes
    # invent their own timestamp instead of using the provided filename).
    summary_glob = None
    if summary_path:
        stem = summary_path.stem  # without .md
        prefix = stem[:-19] if len(stem) > 19 else stem
        summary_glob = f"{prefix}*.md"

    started = time.time()

    while True:
        context_file = TASKS_DIR / f"{session}.json"
        found_summary = _find_summary(summary_path, summary_glob, context_file)

        if process is not None:
            # ---- headless: completion = process exit ----
            if process.poll() is None:
                # Still running; accept a summary+cleanup if a hook already ran.
                if found_summary is not None and not context_file.exists():
                    result = _finalize_summary(found_summary)
                    if result is not None:
                        return result
            else:
                rc = process.returncode
                err = stderr if stderr is not None else _read_process_stderr(process)
                detail = _check_auth(session, summary_path, started, transcript_since,
                                     stderr=err, provider=provider)
                if detail is not None:
                    return {"status": "auth_expired",
                            "summary": _auth_summary(detail)}
                # The summary file is the status; the agent writes it as its
                # final step. Read it now that the process has exited.
                if found_summary is not None:
                    result = _finalize_summary(found_summary)
                    if result is not None:
                        return result
                if rc in (0, 1):
                    raise CompletionTimeout(
                        f"hermes run for '{session}' exited {rc} but wrote no "
                        f"summary file before completing"
                    )
                raise CompletionTimeout(
                    f"hermes run for '{session}' failed (exit {rc}) before "
                    f"writing a summary"
                )
        else:
            # ---- REPL: summary file + context cleanup ----
            if found_summary is not None and not context_file.exists():
                result = _finalize_summary(found_summary)
                if result is not None:
                    return result

            # Usage-limit park (zero-LLM, deterministic).
            from .usage_limit import check_and_park

            if check_and_park(session, source="ensure"):
                return {
                    "status": "usage_limit",
                    "summary": "Session parked on usage limit; auto-resumes after reset",
                }

            # Expired provider auth: the turn was REFUSED, so no completion
            # signal can ever arrive. Checked BEFORE `_session_has_agent` so
            # the run reports the cause rather than the eventual symptom.
            detail = _check_auth(session, summary_path, started, transcript_since,
                                 stderr=stderr, provider=provider)
            if detail is not None:
                return {"status": "auth_expired", "summary": _auth_summary(detail)}

            # Session gone or agent crashed (fell back to bare shell).
            if not _session_has_agent(session):
                raise CompletionTimeout(
                    f"Session '{session}' died or agent exited before task completed"
                )

        if max_duration > 0:
            elapsed = time.time() - started
            if elapsed >= max_duration:
                raise CompletionTimeout(
                    f"Task exceeded max_duration ({max_duration}s) after "
                    f"{int(elapsed)}s — the agent never signalled completion"
                )

        time.sleep(poll_interval)


def _check_auth(session, summary_path, started, transcript_since, *, stderr, provider):
    """Run the auth-failure probe and return the detail dict (or None)."""
    from .auth_expired import check_and_flag

    return check_and_flag(
        session,
        project_path=summary_path.parent if summary_path else None,
        since=started if transcript_since is None else transcript_since,
        source="ensure",
        stderr=stderr,
        provider=provider,
    )


def _auth_summary(detail: dict) -> str:
    from .auth_expired import summary_line

    return summary_line(detail)


def get_summary_prompt(summary_file: str) -> str:
    """Get the system summary prompt with the filename filled in.

    Args:
        summary_file: Path to the summary file to create

    Returns:
        Complete prompt string
    """
    return SYSTEM_SUMMARY_PROMPT.format(summary_file=summary_file)


def parse_summary_file(path: Path) -> SummaryResult:
    """Parse a task summary file.

    Supports two formats:

    1. YAML front matter (from Python SYSTEM_SUMMARY_PROMPT):
        ---
        status: complete
        summary: Did the thing
        files_modified:
          - path/to/file
        ---

    2. Markdown headings (from hook summary prompt):
        # Task Summary
        ## Status
        complete
        ## What Was Done
        Description here
        ## Notes
        Extra context

    Args:
        path: Path to the summary file

    Returns:
        SummaryResult with parsed fields

    Raises:
        CompletionError: If file cannot be parsed
    """
    try:
        content = path.read_text()
    except OSError as e:
        raise CompletionError(f"Cannot read summary file: {e}")

    # Default values
    status = "incomplete"
    summary = ""
    files_modified: list[str] = []
    blockers: list[str] = []

    if content.startswith("---"):
        # Parse YAML front matter format
        end_match = re.search(r"\n---\s*\n", content[3:])
        if end_match:
            yaml_content = content[3:3 + end_match.start()]

            # Track which list we're currently parsing
            current_list: str | None = None

            for line in yaml_content.split("\n"):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                if stripped.startswith("status:"):
                    status = stripped.split(":", 1)[1].strip()
                    current_list = None
                elif stripped.startswith("summary:"):
                    summary = stripped.split(":", 1)[1].strip()
                    current_list = None
                elif stripped.startswith("files_modified:"):
                    current_list = "files"
                elif stripped.startswith("blockers:"):
                    current_list = "blockers"
                elif stripped.startswith("- "):
                    item = stripped[2:].strip()
                    if current_list == "files":
                        files_modified.append(item)
                    elif current_list == "blockers":
                        blockers.append(item)
    else:
        # Parse markdown heading format (## Status, ## What Was Done, etc.)
        sections: dict[str, list[str]] = {}
        current_section: str | None = None

        for line in content.split("\n"):
            stripped = line.strip()
            heading = re.match(r"^#{1,3}\s+(.+)", stripped)
            if heading:
                current_section = heading.group(1).lower()
                continue
            if current_section and stripped:
                sections.setdefault(current_section, []).append(stripped)

        if "status" in sections:
            status = sections["status"][0].strip().lower()
        if "what was done" in sections:
            summary = " ".join(sections["what was done"])
        elif "summary" in sections:
            summary = " ".join(sections["summary"])

    # Validate status — also accept "error" as "failed"
    if status == "error":
        status = "failed"
    if status not in ("complete", "incomplete", "failed"):
        status = "incomplete"

    return SummaryResult(
        status=status,
        summary=summary,
        files_modified=files_modified,
        blockers=blockers,
        raw_content=content,
    )


def status_to_exit_code(status: str) -> int:
    """Convert status string to exit code.

    Args:
        status: Task status (complete, incomplete, failed, usage_limit,
            auth_expired)

    Returns:
        Exit code (0=complete, 1=failed, 2=incomplete, 7=usage_limit,
        8=auth_expired)
    """
    if status == "complete":
        return 0
    elif status == "failed":
        return 1
    elif status == "usage_limit":
        return 7
    elif status == "auth_expired":
        # Distinct from incomplete so the scheduler can gate the rest of the
        # fleet instead of letting each task discover the outage itself.
        return 8
    else:
        return 2
