---
name: hermeswire-cli
description: Full `hermeswire` CLI command reference — session/pane management, portal, TTS/STT, voice, channels (email + quo, outbound-only), machine/tunnel/lock management, projects/history/roles, scheduler, web helper (fetch), safety/diagnostics. Use when running or composing `hermeswire ...` shell commands, building automation scripts, or answering "how do I X from the CLI".
---

# HermesWire CLI Reference

```bash
# Session management
hermeswire new -s name           # not: tmux new-session
hermeswire new -s name --no-soul # skip the always-injected soul personality role
hermeswire new -s name --first-message "idea"  # deliver first prompt once agent boots
                                #   (verified paste, local only; failure ≠ command failure)
hermeswire send -s name "prompt" # not: tmux send-keys
hermeswire send -s name --wait-ready --timeout 60 -- "prompt"
                                # wait for agent boot (banner + screen-stable +
                                #   trust-prompt auto-accept), verified delivery,
                                #   exit 1 if unverified; local only
hermeswire send-keys -s name key1 key2  # raw keys with pauses
hermeswire send-keys -s name --pane 2 key  # target a specific pane
hermeswire new -s name --created-by orch  # FORCE this creator (prompt-routing parent)
                                #   regardless of project; '' forces standalone.
                                #   Default (no flag): inherit the calling tmux session
                                #   ONLY if -p/-s targets the caller's own project — a
                                #   different project defaults to a standalone root (#715)
hermeswire output -s name        # not: tmux capture-pane
hermeswire info -s name          # session metadata (cwd, panes) as JSON
hermeswire kill -s name          # not: tmux kill-session
hermeswire list                  # not: tmux list-sessions
hermeswire recreate -s name      # destroy and recreate with fresh worktree
                                #   DESTRUCTIVE: rm -rf's the worktree dir + new branch
hermeswire restart -s name       # relaunch IN PLACE, same conversation (#871) — /exit,
                                #   regenerate flags from the recorded roles/posture/model,
                                #   relaunch at the same cwd with --resume. Nothing on disk
                                #   is touched (unlike recreate) and no new tmux session is
                                #   made (unlike `history resume`). Works on a session that
                                #   ISN'T running — that's the post-reboot/post-rebuild case.
                                #   If the conversation's history is orphaned or gone it
                                #   starts FRESH with the role intact and says so.
                                #   Waits for the agent to come back (exit 1 if it doesn't);
                                #   --no-wait skips. Local only; can't restart itself.
hermeswire wait --children       # BLOCK on the child sessions you spawned (#852) — collects
                                #   each report, tears the child down, names the ones that
                                #   never reported. Waiting idly instead gets YOU reaped
                                #   mid-fan-out (idle != done). --timeout bounds this call;
                                #   exit 1 = still pending, just call it again.
                                #   Enrollment is automatic on `new`/`worktree` (opt out with
                                #   --no-cohort; --kind orchestrator never enrolls).
hermeswire worktree name         # new branch + worktree + STANDALONE session
                                #   "worktree session" ALWAYS means this command — never
                                #   `spawn --branch` (that makes a pane). Defaults to the
                                #   bypass posture (autonomous, topology-driven — same for
                                #   every role on this verb); override with --posture.
                                #   ROLE defaults to "worker" (zero behavior change) — the
                                #   worker-worktree etiquette role is intrinsic to that
                                #   default (isolation, no rebuild/restart, verify
                                #   in-worktree, draft PR + notify-back) — first prompts
                                #   only need the task itself
                                #   Base branch (default mode): --base wins, else config
                                #   worktree.default_base, else the repo's actual default
                                #   branch (origin/HEAD, fallback current) — no hardcoded 'main'.
                                #   --project defaults to the git root of cwd (monorepo-safe:
                                #   many sessions can target one repo from different branches).
                                #   config worktree.naming can template the NEW branch name.
hermeswire worktree name -b develop  # from specific base branch
hermeswire worktree name -c      # from repo's current branch
hermeswire worktree name -e      # checkout existing branch (no new branch)
hermeswire worktree name --ref v2.0  # detached at tag/commit
hermeswire worktree name --prompt "task"  # spawn AND seed the first message in one call (verified delivery)
hermeswire worktree name --kind orchestrator  # ROLE override (#716): a durable, replaceable-
                                #   persona project window instead of a safety-railed
                                #   subordinate. Roots by default (created_by='') unless
                                #   --created-by says otherwise.
hermeswire orchestrator [name] -p <project>  # sugar for `worktree --kind orchestrator`
                                #   (name defaults to "orchestrator") — the durable-window
                                #   one-liner for a monorepo/large-repo project.
hermeswire worktree name --kind reviewer  # ROLE override (#827): a PR-review station — safety-
                                #   railed the other way (never opens/merges its own PR,
                                #   iterates via msg_send, reports a verdict via notify_parent).
                                #   Stays parented like worker (not rooted). Pane/main topology
                                #   is the typical default (`hermeswire new --kind reviewer`);
                                #   use `worktree --kind reviewer` for a local checkout to e2e
                                #   a sibling's branch.
hermeswire worktree --list       # list this repo's worktree sessions + read-only git status; --all = every repo
hermeswire worktree --status name  # read-only git status (dirty/ahead/behind/pushed) for one worktree
hermeswire worktree --dangling    # LIVE worker sessions with an OPEN PR and no live recorded
                                #   parent (#716) — a PR nobody is positioned to review/merge.
                                #   Distinct from --list's "orphan" (dead session, disk remnant).
hermeswire worktree --remove name  # ATOMIC teardown: kill session + force-remove worktree + delete
                                #   merged branch (local+remote) + unregister — fails loudly (non-zero,
                                #   registry entry kept) if the dir can't actually be cleared (#717).
                                #   --keep-branch skips branch cleanup; --force-delete-branch deletes
                                #   even if not confirmed merged (but refuses an OPEN PR — #756 — unless
                                #   --close-pr-branch is also given, since that would silently close it).
hermeswire worktree --prune      # drop registry entries whose worktree is gone + git worktree prune
                                #   --gc-merged: also tear down (session+worktree+branch) any
                                #   still-present entry whose branch is confirmed merged
hermeswire tabs track --session name --tab-id <id> [--url <url>]  # bookkeeping for a
                                #   claude-in-chrome tab the session opened, so worktree
                                #   teardown can report it if the session never closes it
hermeswire tabs untrack --session name --tab-id <id>  # drop tracking after closing it yourself
hermeswire tabs list [--session name]  # list tracked tabs (debug leaked verification tabs)
hermeswire fork -s name          # fork session into new worktree
hermeswire fork -s name -t project/branch --commit abc123  # fork from specific commit

# Helper session — a worker session with NO isolation (#838): shares the
# caller's checkout, so zero git work at creation (no worktree, no branch,
# nothing in the worktree registry). Reproduces a worker pane's one real
# advantage while keeping msg inbox / voice / prompt routing / portal
# visibility. `wait --children` collects its report then reaps it (topology
# "main"), unlike a worktree child which is left alive.
hermeswire helper name           # worker session sharing this checkout
hermeswire helper name -p ~/projects/repo --prompt "run the suite, report failures"
hermeswire helper name --roles wiki  # extra roles STACK on worker + shared-checkout
                                #   The `shared-checkout` role is auto-injected:
                                #   read/edit freely, NEVER commit/branch/checkout/
                                #   stash/reset/pull — the checkout's owner commits.
                                #   Needs a commit or a PR? Use `worktree` instead.
                                #   Teardown is just `hermeswire kill -s name`.

# Pane commands (worker PANES inside the current session — NOT worktree
# sessions; for parallel autonomous work use `hermeswire worktree` above)
hermeswire spawn --roles worker  # spawn worker pane
hermeswire spawn --branch name   # worker pane on an isolated worktree (still a pane)
hermeswire send --pane 1 "task"  # send to pane
hermeswire output --pane 1       # read pane output
hermeswire kill --pane 1         # kill pane
hermeswire jump --pane 1         # focus pane
hermeswire split -s name         # add terminal pane(s)
hermeswire detach -s name        # move pane to its own session
hermeswire resize -s name        # resize window to fit largest client

# Boot everything
hermeswire up                    # boot all services (portal, TTS, STT, scheduler,
                                #   custom) then start/attach the dev session
hermeswire up --no-tts --no-stt  # skip optional voice services
hermeswire up --dev              # run portal from source (uv run)

# Portal management
hermeswire portal start          # start in tmux
hermeswire portal stop           # stop portal
hermeswire portal restart        # stop + start
hermeswire portal status         # check health
hermeswire portal token          # print the bootstrap auth token (host/CLI/MCP credential)
hermeswire portal token --rotate # generate a new bootstrap token (re-enter on devices)

# Per-device credentials (#423) — pair a device for its OWN revocable token
hermeswire portal pair [--name phone]   # print a short-lived pairing code + QR → /pair?code=
hermeswire portal devices [--json]      # list paired devices (id, name, last-seen, status)
hermeswire portal revoke <id>           # revoke ONE device (others keep working)

# Scratch pad (shared notes — portal drawer Alt+N; file: ~/.hermeswire/scratchpad.json)
hermeswire scratchpad list       # list notes (newest first)
hermeswire scratchpad add "text" --source mysession  # add a note (drawer refreshes live)
hermeswire scratchpad remove <id> # delete a note
hermeswire scratchpad clear      # delete all notes

# Custom services (registered long-running sessions — services.custom in config;
# autostart on portal launch, health-checked + restarted by the portal watchdog)
hermeswire services list         # registry: autostart/restart/healthcheck per service
hermeswire services status       # run healthchecks now (exit 1 if something's down)
hermeswire services status name  # one service
hermeswire services up <name>    # start (also clears 'down' state)
hermeswire services up --all     # start all autostart services (skips downed)
hermeswire services down <name>  # stop AND keep stopped (watchdog won't respawn)

# TTS/STT servers
hermeswire tts start|stop|status # TTS server management
hermeswire stt start|stop|status # STT server management (host shim on :8101)
hermeswire stt start             # engine from stt.engine (auto|moonshine|whisper); moonshine = fast CPU
hermeswire stt start --backend moonshine --model moonshine/base --port 8101  # ad-hoc overrides

# Voice
hermeswire say "text"            # speak (auto-routes to browser or local)
hermeswire say -s name "text"    # speak to specific session
hermeswire say "spoken" --display "richer card"  # speak AND show a desktop toast with different text (one call)
hermeswire notify-parent "text"   # notify parent session (worker→orchestrator)
hermeswire notify-parent --to name "text" # notify specific session
hermeswire notify-parent --raw --to name "text"  # verbatim, no [NOTIFY ...] prefix
                                # (delivery is safety-gated: refuses targets showing a
                                #  live dialog / bare shells / parked sessions, verified paste)

# Prompt routing (interactive prompts → parent session; see wiki sessions/prompt-routing.md)
hermeswire prompts status        # pending prompt markers
hermeswire prompts tick          # run one sweep now (watchdog does this every 60s)
hermeswire prompts answer -s name --pane 0 --expect <hash> 2  # guarded answer:
                                #   re-detects + hash-compares before sending keys —
                                #   NEVER answer dialogs with raw send-keys
hermeswire prompts clear -s name --pane 1  # drop a marker

# Polite messaging (non-interrupting agent-to-agent inbox; see wiki sessions/messaging.md)
hermeswire msg send --to name "text"          # queue a message (delivers when their box is clear)
hermeswire msg send --to name --kind done "PR #312 drafted"  # kinds: note|done|request|escalation|ingest|voice|idle (idle = the idle hook's synthetic placeholder, #952)
hermeswire msg send --to name --kind ingest --ref "/path/report.md" "topic"  # PASSIVE: never auto-delivered, pull-only
hermeswire msg send --to @all "team update"   # broadcast to live agent sessions except sender
hermeswire msg send --to name --body-file /tmp/body.md  # code-bearing body, no shell escaping ('-' = stdin)
hermeswire msg inbox -s name                  # peek pending + passive (does not drain/consume)
hermeswire msg pull -s name                   # read + REMOVE passive (ingest) messages — the voluntary pull
hermeswire msg dead -s name                   # list dropped (dead-lettered) msgs + reason/timestamp
hermeswire msg flush -s name                  # attempt a drain now (still gated on empty box + safe target)

# Research dropbox (Briefing Mode)
hermeswire research dir -s name               # print the dropbox path (~/.hermeswire/research/<session>/)
hermeswire research ensure -s name            # create + print the dropbox path
                                # `msg` NEVER clobbers a human's draft — unlike `send`, which
                                # pastes + Enter immediately. Use `send` only to forcibly drive a session now.

hermeswire listen start|stop|cancel  # host voice recording (needs stt.backend: custom + the :8101 shim)
hermeswire listen stop -s name   # transcribe + send to a tmux session (default)
hermeswire listen stop --type    # transcribe + type at cursor (Hammerspoon paste)
hermeswire listen stop --stdout  # transcribe + print raw transcript to stdout, no paste/send
                                #   (scripting hook — Hammerspoon etc. capture $())

# Voices (custom/cloned voices come from a TTS shim — see docs/wiki/voice/shim-contract.md)
hermeswire tts voices            # list available voices (custom-shim voices or Kokoro presets)

# Artifact windows (agent visual canvas)
hermeswire open <url> --title "T"  # announce URL/local file as a click-to-open artifact notification (#817)
hermeswire open dashboard.html     # announce from ~/.hermeswire/artifacts/ — human clicks to open

# Channels (outbound notification integrations — email + quo)
hermeswire channels list         # list all registered channels
hermeswire channels list --json  # JSON output

# Email (send-only channel)
hermeswire email --to addr --subject "Subject" --body "Body"
hermeswire email --body "msg" # uses default_to from config
hermeswire email --attach file.pdf --body "See attached"

# Quo SMS (send-only channel, no deps)
hermeswire quo --body "msg" --to "+1234567890"

# Machine management
hermeswire machine list
hermeswire machine add <id> --host <host> --user <user>
hermeswire machine remove <id>

# SSH tunnels (for remote services)
hermeswire tunnels up            # create all required tunnels
hermeswire tunnels down          # tear down all tunnels
hermeswire tunnels status        # show tunnel health
hermeswire tunnels check         # verify tunnels are working

# Lock management (for scheduled tasks)
hermeswire lock list             # list all locks
hermeswire lock clean            # remove stale locks
hermeswire lock remove <session> # force-remove a specific lock

# Project discovery
hermeswire projects list         # discover projects from projects_dir
hermeswire projects list --json  # JSON output for scripting
hermeswire projects create name              # mkdir + minimal .hermeswire.yml (bypass)
                                            # (in git repos, .hermeswire.yml is auto-added to
                                            #  .gitignore — personal config, keep it untracked)
hermeswire projects create name --git-init   # also run `git init`
hermeswire projects create name --from URL   # git clone URL instead of mkdir

# Session history
hermeswire history list          # list conversation history
hermeswire history show <id>     # show session details
hermeswire history resume <id>   # resume session (always forks)

# Shareable conversation handoffs (issue #157)
hermeswire handoff init [--title hint]      # create bundle dir + pre-filled ai-handoff.md template
hermeswire handoff render <bundle-dir>      # render show-the-story.html from ai-handoff.md
hermeswire handoff list                     # list past bundles
# Inside a Hermes Agent session, prefer the /handoff-bundle skill — it walks the
# agent through filling the template using full conversation context (free, no
# fresh LLM call). Outputs land in ~/.hermeswire/artifacts/handoff-<slug>/.

# Roles management
hermeswire roles list            # list available roles
hermeswire roles show <name>     # show role details

# Scheduled workloads
hermeswire ensure -s name --task task  # run named task reliably
hermeswire task list [session]         # list tasks for session/project
hermeswire task show session/task      # show task definition
hermeswire task validate session/task  # validate task syntax

# URL fetch (helper usable from any session, including pi)
hermeswire fetch <url>                 # fetch a page via Jina Reader (markdown, JS-rendered)
hermeswire fetch <url> --limit 4000    # cap chars (default 8000, 0 = no limit)

# Safety & diagnostics
hermeswire safety check "cmd"    # test if command would be blocked
hermeswire safety status         # show pattern counts and recent blocks
hermeswire safety logs           # query audit logs
hermeswire safety install        # install damage control hooks
hermeswire hooks install         # install hermeswire-owned hooks + global skills (Hermes Agent)
hermeswire hooks uninstall       # remove hermeswire-owned hooks (Hermes Agent)
hermeswire hooks status          # check hook installation status
hermeswire network status        # complete network health check
hermeswire doctor                # auto-diagnose and fix issues
hermeswire doctor --voice        # only the push-to-talk path: mic, STT shim, portal/tunnel, tmux+PTT (pass/fail + fix)

# Notifications (the notify-* family, by target)
hermeswire notify-event EVENT    # broadcast a portal lifecycle event (session/pane); usually called by tmux hooks
hermeswire notify-parent "text"  # text up to your parent/orchestrator session
hermeswire notify-user "text"    # desktop toast for the human (safe markdown: bold, [links](url), line breaks)

# MCP Server
hermeswire mcp                   # expose hermeswire as MCP server

# Scheduler
hermeswire scheduler start|serve|stop|status # manage scheduler daemon
hermeswire scheduler board                   # show task board with overdue scores
hermeswire scheduler live                    # show live scheduler state
hermeswire scheduler events                  # show recent scheduler events
hermeswire scheduler history                 # show recent run history
hermeswire scheduler run task                # force-run a task now
hermeswire scheduler enable|disable task     # enable/disable a task
hermeswire scheduler report [--since 8h] [--artifact]  # generate morning report HTML
hermeswire scheduler dashboard               # open scheduler dashboard

# Usage-limit recovery (deterministic watchdog, see docs/wiki/usage-limit-recovery.md)
hermeswire limits tick           # one watchdog pass: sweep panes, resume what's due
hermeswire limits status         # show sessions parked on usage limits
hermeswire limits resume -s name [--force]  # manually resume a parked session now
hermeswire limits install        # install + load the launchd watchdog (60s tick)
hermeswire limits uninstall      # unload + remove the watchdog

# Setup & Development
hermeswire init                  # interactive setup wizard (ends on the portal URL)
hermeswire init --assisted       # ...and spawn the Claude TTS/STT setup session at the end
hermeswire generate-certs        # generate SSL certificates
hermeswire up                    # boot all services + dev session (see "Boot everything")
hermeswire dev                   # start/attach to dev session ONLY (no services)
hermeswire rebuild               # clear uv cache and reinstall
hermeswire uninstall             # uninstall the tool
```

`hermeswire dev` only spawns the `hermeswire` agent session — it does NOT start
the portal or any service. Use `hermeswire up` after a reboot to bring up the
full stack. `up` brings up portal → TTS → STT → autostart custom services, then
runs `dev`; the scheduler rides along via the portal's `scheduler.autostart`.
TTS only starts a local service for the `custom` tier (the `default` tier uses browser/OS voice — no service); STT is skipped without `stt.url`.

Session formats: `name`, `project/branch` (worktree), `name@machine` (remote)
Pane targeting: `--pane N` auto-detects session from `$TMUX_PANE`

For CLI details: `hermeswire --help` or `hermeswire <cmd> --help`
