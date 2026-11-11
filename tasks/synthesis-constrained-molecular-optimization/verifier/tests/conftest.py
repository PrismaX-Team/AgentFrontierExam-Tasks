from __future__ import annotations

import sys
from pathlib import Path


VERIFIER_ROOT = Path(__file__).resolve().parents[1]
if str(VERIFIER_ROOT) not in sys.path:
    sys.path.insert(0, str(VERIFIER_ROOT))
