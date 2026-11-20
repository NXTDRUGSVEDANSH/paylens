"""Restore the seed-only knowledge base so a demo run is reproducible."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core import cache          # noqa: E402
from backend.kb import seed, store      # noqa: E402
from semantic.loader import get_model   # noqa: E402

if __name__ == "__main__":
    keep = "--all" not in sys.argv
    model = get_model()
    if not store.available():
        print("knowledge base unavailable"); raise SystemExit(1)
    n = store.reset(keep_seeds=keep)
    cache.invalidate()
    print(f"deleted {n} record(s) (seeds {'kept' if keep else 'dropped'})")
    if not keep:
        print(seed.seed(model, force=True))
