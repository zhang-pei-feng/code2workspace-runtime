#!/usr/bin/env python3
import os
import sys
from pathlib import Path


def main() -> None:
    target = Path(__file__).resolve().with_name("search_tools.py")
    os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])


if __name__ == "__main__":
    main()
