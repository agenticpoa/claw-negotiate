#!/usr/bin/env python3
"""Print the APOA SAFE negotiation demo checklist."""
from __future__ import annotations

import argparse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKLIST = REPO_ROOT / "docs" / "demo_checklist.md"


QUICK = """# APOA SAFE Demo Quick Script

Founder bot DM:

```text
My profile
```

If needed:

```text
Update my profile - I'm Juan Figuera, CEO of Avocado
```

Start:

```text
Live negotiation with Nora Vassileva at SD Fund. Cap $30M to $40M, 10% discount, pro-rata required.
```

Reply after confirm:

```text
GO
```

Investor bot DM:

```text
My profile
```

If needed:

```text
Update my profile - I'm Nora Vassileva, partner at SD Fund
```

Join:

```text
Joining INV-XXXXX via @AgenticPOA_bot, I am Nora Vassileva at SD Fund, cap up to $35M, 10% discount, pro-rata required
```

Reply after confirm:

```text
GO
```

Group chat:

```text
/bind INV-XXXXX
```

Watch for:

- Round 0 in group
- alternating founder/investor cards, no duplicates
- private signing links in DMs only
- executed PDF in group after both signatures
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Print only the pasteable live-demo script.",
    )
    args = parser.parse_args()
    if args.quick:
        print(QUICK, end="" if QUICK.endswith("\n") else "\n")
    else:
        print(CHECKLIST.read_text(encoding="utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
