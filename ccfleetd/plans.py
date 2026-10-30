"""What a Claude plan is called where people read it.

Claude Code can still report the legacy Max tier identifiers. Display their
current names: Max 5x is Max, and Max 20x is Ultra (verified October 30, 2026
at https://claude.com/pricing). A name does not establish account verification
or usage limits; those remain separate provider-reported facts.
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: Preserve unknown legacy tier names without inventing a current entitlement.
MAX_TIER_RE = re.compile(r"(?:^|_)max_(\d{1,3})x$")
LEGACY_MAX_NAMES = {"5": "Max", "20": "Ultra"}
#: Subscription types as Anthropic names the plans.
NAMED = {"pro": "Pro", "max": "Max", "ultra": "Ultra", "team": "Team", "enterprise": "Enterprise",
         "free": "Free"}


def label(subscription: Any, tier: Any = None) -> Optional[str]:
    """The current plan name; None when nothing says which it is.

    The tier is read only for Max, which is the one plan it tells apart: a
    Pro sign-in beside a Max tier says Pro, as the sign-in is the account's
    own word for its plan. A type this does not know is said as sent, which
    is how the pages said every plan before.
    """
    sub = subscription.strip() if isinstance(subscription, str) else ""
    found = MAX_TIER_RE.search(tier) if isinstance(tier, str) else None
    if found and sub.lower() in ("", "max"):
        multiple = found.group(1)
        return LEGACY_MAX_NAMES.get(multiple, f"Max {multiple}x")
    return NAMED.get(sub.lower(), sub) or None
