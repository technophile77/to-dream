#!/usr/bin/env python3
"""Entry point for to-dream: puts ../lib on sys.path and calls to_dream.cli.main()."""
import os
import sys

if sys.version_info < (3, 10):
    sys.stderr.write("to-dream needs Python 3.10 or newer (found %s).\n" % sys.version.split()[0])
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

from to_dream.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
