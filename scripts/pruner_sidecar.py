"""Source-tree entry point for the isolated resolver process."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pruner_studio.sidecar_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
