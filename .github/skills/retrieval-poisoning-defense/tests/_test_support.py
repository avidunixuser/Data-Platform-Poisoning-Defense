"""Make the portable skill scripts importable during unittest discovery."""

from pathlib import Path
import sys


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
