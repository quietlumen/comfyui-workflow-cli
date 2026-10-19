"""`python -m cwf` 入口。"""

from __future__ import annotations

import sys


def main() -> int:
    from .cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
