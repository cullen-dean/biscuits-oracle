"""Step 3 exploration: save raw API responses to data/samples/ so we can study them.

Usage:
  python scripts/explore.py tools                      -> saves the list of tools and their inputs
  python scripts/explore.py call <tool> '<json args>'  -> saves one tool call's result
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import MCPClient  # noqa: E402

OUT = Path(__file__).parent.parent / "data" / "samples"


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {path}")


def main():
    client = MCPClient()
    if sys.argv[1] == "tools":
        save("tools", client.list_tools())
    elif sys.argv[1] == "call":
        tool = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        label = tool + ("_" + "_".join(str(v) for v in args.values()) if args else "")
        save(label[:120], client.call(tool, **args))


if __name__ == "__main__":
    main()
