"""What a Claude plan is called where people read it.

Claude Code says a plan in two fields, and neither is the name people know.
`subscriptionType` from the sign-in ("pro", "max") does not tell Max 5x from
Max 20x; the account's rate-limit tier (`organizationRateLimitTier`, e.g.
"default_claude_max_20x") does. Erik, 2026-09-24: the holder and the operator
both see each account's level, so it is said the way Anthropic sells it.
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: The multiple in a Max tier: "default_claude_max_20x" is Max 20x.
MAX_TIER_RE = re.compile(r"max_(\d{1,3})x")
#: Subscription types as Anthropic names the plans.
NAMED = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise",
         "free": "Free"}


def label(subscription: Any, tier: Any = None) -> Optional[str]:
    """The plan's name, "Max 20x"; None when nothing says which it is.

    The tier is read only for Max, which is the one plan it tells apart: a
    Pro sign-in beside a Max tier says Pro, as the sign-in is the account's
    own word for its plan. A type this does not know is said as sent, which
    is how the pages said every plan before.
    """
    sub = subscription.strip() if isinstance(subscription, str) else ""
    found = MAX_TIER_RE.search(tier) if isinstance(tier, str) else None
    if found and sub.lower() in ("", "max"):
        return f"Max {found.group(1)}x"
    return NAMED.get(sub.lower(), sub) or None
