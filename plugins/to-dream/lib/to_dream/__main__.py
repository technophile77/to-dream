"""Allow ``python -m to_dream <command>`` (with ``plugins/to-dream/lib`` on the module path)."""

import sys

from to_dream.cli import main

sys.exit(main())
