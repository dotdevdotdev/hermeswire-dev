#!/bin/bash
# AgentWire permission gate — Hermes ``pre_tool_call`` approve-escalating hook.
#
# Replaces the Claude ``PermissionRequest`` hook. Hermes has no Claude-style
# ``permissionDecision`` and no per-call ``permission_mode``: ask escalation is
# expressed via the ``{"action": "approve"}`` directive, which routes the tool
# call through Hermes's NATIVE human-approval gate. That gate fires the
# ``pre_approval_request`` / ``post_approval_response`` observers — the
# observer-only notify/route surface (portal routing via
# ``prompt_router.notify_permission_request``) lives there, not here.
#
# The rule-based safety gate is the damage-control hooks (#11), which already
# fail closed on unattended dispatches and emit their own ``{"action": ...}``
# directives. This hook is the coarse permission *posture* gate:
#
# - Unattended scheduler dispatch (``AGENTWIRE_UNATTENDED=1``): allow (exit 0).
#   There is no human to confirm, and damage-control already blocks anything
#   the task did not pre-grant — escalating here would only wedge a headless
#   ``hermes -z`` run.
# - A recorded bypass/auto posture (``--yolo``): allow (exit 0), parity with
#   the old ``bypassPermissions``/``auto`` short-circuit.
# - Otherwise: escalate to the native human-approval gate via
#   ``{"action": "approve"}``, so confirmation flows through Hermes's gateway
#   approval callback instead of the old portal POST-and-wait.
set -u

input=$(cat)

# Unattended scheduler dispatch — allow; damage-control is the gate.
if [[ "${AGENTWIRE_UNATTENDED:-0}" == "1" ]]; then
    exit 0
fi

# A recorded bypass/auto posture short-circuits friction (parity with the old
# bypassPermissions/auto allow). Best-effort — a missing field is not bypass.
# Python is used for JSON parsing (no jq dependency), matching the stack the
# damage-control hooks run on.
posture=$(printf '%s' "$input" | python3 -c "import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get('permission_mode') or d.get('posture') or '')
except Exception:
    pass" 2>/dev/null || true)
case "$posture" in
    bypassPermissions|auto|bypass|yolo)
        exit 0
        ;;
esac

# Escalate to Hermes's native human-approval gate.
echo '{"action": "approve", "message": "permission gate: escalate to approval"}'
exit 0
