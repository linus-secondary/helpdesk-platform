import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
os.environ.setdefault("INTERNAL_TOKEN", "test-internal-token")
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost:1/unused")
os.environ.setdefault("BEDROCK_ENABLED", "false")
