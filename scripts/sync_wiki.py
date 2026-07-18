from __future__ import annotations

import sys
from pathlib import Path

from kr_stock_wiki.sync import sync_wiki_tree


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: sync_wiki.py SOURCE TARGET", file=sys.stderr)
        return 2
    changes = sync_wiki_tree(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"copied={len(changes.copied)} removed={len(changes.removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
