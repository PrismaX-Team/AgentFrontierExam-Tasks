from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import entry, validation_main

if __name__ == "__main__":
    raise SystemExit(entry(validation_main))
