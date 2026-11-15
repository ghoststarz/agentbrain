"""支持 `python -m agentbrain ...` 入口。"""

from __future__ import annotations

import sys

from agentbrain.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
