#!/bin/bash
# HermesWire session-end observer — Hermes ``on_session_end`` lifecycle hook.
#
# Replaces the Claude ``Notification``/idle hook. Hermes has no idle event:
# headless ``hermes -z`` signals completion by EXITING the process, and the
# interactive path fires this observer on the real ``on_session_end`` lifecycle
# transition (turn produced a final response). There is no idle polling, no
# ``/exit``, no two-pass summary injection, and no loop iteration here — those
# are the scheduler's/``ensure``'s job now.
#
# Responsibilities on session end:
#   1. If a scheduled task context exists (``~/.hermeswire/tasks/<session>.json``),
#      read the summary the agent already wrote (instructed via the launch
#      prompt's ``## When done`` section), queue it to the parent through the
#      queue-processor protocol, and remove the context file — the completion
#      signal ``ensure``'s wait blocks on.
#   2. Otherwise, notify the parent (``notify-parent --queued``) that this
#      session finished, riding the polite msg inbox instead of a direct paste.
#
# The usage-limit park / prompt-routing / cohort fan-out guards that the old
# idle hook carried now live in the scheduler (``ensure``/``watchdog``), which
# already owns those markers — see ``usage_limit.is_parked`` and
# ``cohort.blocking``.

DEBUG_LOG="/tmp/hermeswire-session-end-debug.log"
log() { echo "[$(date -Iseconds)] $*" >> "$DEBUG_LOG"; }

# Find hermeswire binary (env var > which > default)
HERMESWIRE="${HERMESWIRE_BIN:-$(which hermeswire 2>/dev/null || echo "$HOME/.local/bin/hermeswire")}"

input=$(cat)
log "Hook fired: on_session_end TMUX_PANE=$TMUX_PANE TMUX=$TMUX"

session_id=$(echo "$input" | jq -r '.session_id // ""' 2>/dev/null)
completed=$(echo "$input" | jq -r '.completed // true' 2>/dev/null)
cwd=$(echo "$input" | jq -r '.cwd // ""' 2>/dev/null)

# Resolve the tmux session name (the real runtime context).
tmux_session=""
if [[ -n "$TMUX" ]]; then
    tmux_session=$(tmux display-message -p '#S' 2>/dev/null || echo "")
fi
[[ -z "$tmux_session" && -n "$session_id" ]] && tmux_session="$session_id"

# A headless `hermes -z` run has no pane to route to; there is nothing to do
# unless we can name the session.
if [[ -z "$tmux_session" ]]; then
    log "No session name resolvable — nothing to do"
    exit 0
fi

task_context_file="$HOME/.hermeswire/tasks/${tmux_session}.json"

if [[ -f "$task_context_file" ]]; then
    log "Scheduled task context found for $tmux_session"

    summary_file=$(jq -r '.summary_file // ""' "$task_context_file" 2>/dev/null)
    exit_on_complete=$(jq -r 'if .exit_on_complete == null then true else .exit_on_complete end' "$task_context_file" 2>/dev/null)

    # Locate the summary the agent wrote as its final action.
    summary_path=""
    if [[ -n "$summary_file" && -n "$cwd" ]]; then
        summary_path="${cwd}/${summary_file}"
    elif [[ -n "$summary_file" ]]; then
        # Fall back to the projects dir convention when the payload lacks cwd.
        summary_path="$HOME/projects/${tmux_session}/${summary_file}"
    fi

    if [[ -n "$summary_path" && -f "$summary_path" ]]; then
        summary_content=$(cat "$summary_path" 2>/dev/null || echo "")
    else
        summary_content=""
    fi

    # Clean up the context file so `ensure`'s completion poll unblocks. This is
    # the completion signal — it must happen on BOTH exit_on_complete branches.
    rm -f "$task_context_file" 2>/dev/null
    log "Cleaned up task context for $tmux_session (exit_on_complete=$exit_on_complete)"

    if [[ -n "$summary_content" ]]; then
        message="[WORKER SUMMARY ${tmux_session}]

${summary_content}"

        # Queue the notification using the same mkdir lock protocol as the
        # queue processor — an append racing the processor's head-trim would
        # lose a message.
        queue_dir="$HOME/.hermeswire/queues"
        queue_file="${queue_dir}/${tmux_session}.jsonl"
        lock_dir="${queue_dir}/${tmux_session}.lock"
        mkdir -p "$queue_dir"

        escaped_message=$(printf '%s' "$message" | jq -Rs .)
        timestamp=$(date +%s)000
        lock_tries=0
        while ! mkdir "$lock_dir" 2>/dev/null; do
            lock_tries=$((lock_tries + 1))
            [[ $lock_tries -ge 50 ]] && break
            sleep 0.1
        done
        echo "{\"timestamp\":${timestamp},\"message\":${escaped_message}}" >> "$queue_file"
        rmdir "$lock_dir" 2>/dev/null
        log "Queued summary notification for $tmux_session"

        # Start the queue processor if not already running.
        pid_file="${queue_dir}/${tmux_session}.pid"
        if [[ ! -f "$pid_file" ]] || ! kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null; then
            nohup "$HOME/.hermeswire/queue-processor.sh" "$tmux_session" >/dev/null 2>&1 &
            log "Started queue processor for $tmux_session"
        fi
    else
        log "No summary file found for $tmux_session — nothing to queue"
    fi
else
    # No task context — a plain (non-scheduled) session ended. Tell its parent
    # via the polite msg inbox (kind=done). Resolution lives inside
    # notify-parent (prompt_router.resolve_parent).
    log "No task context — notifying parent of session end"
    (
        out=$($HERMESWIRE notify-parent --queued -q "session finished" 2>&1) \
            || log "notify-parent --queued failed: $out"
    ) &
fi

exit 0
