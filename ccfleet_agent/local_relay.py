"""Retired local-agent relay: fail closed with an upgrade message.

This module reads no Claude credential, opens no network connection, and accepts
no inference requests. Use the slot-native project connector instead.
"""

import sys


def main() -> int:
    print("The local-agent relay is retired. Update CC Fleet and use the "
          "slot-native project connector.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
