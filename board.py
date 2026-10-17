#!/usr/bin/env python3
"""FossilBoard — точка входа. Вся логика в пакете fossilboard/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fossilboard.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())