"""CLI for Web Push (VAPID) — ``hermeswire push ...`` (#483).

Thin top-level wrapper around ``hermeswire.channels.push.cmd_push``; the handler
lives in the channels package, this just owns the argparse subtree. Pure
relocation from ``__main__`` (#495).
"""

from __future__ import annotations


def register_push_parser(subparsers) -> None:
    from hermeswire.channels.push import cmd_push

    push_parser = subparsers.add_parser(
        "push", help="Web Push (VAPID) for the PWA — generate keys / check status"
    )
    push_sub = push_parser.add_subparsers(dest="push_cmd")
    push_sub.add_parser("keygen", help="Generate a VAPID keypair for ~/.hermeswire/.env")
    push_sub.add_parser("status", help="Show push readiness + subscription count")
    push_parser.set_defaults(func=cmd_push)
