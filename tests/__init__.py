"""Test bootstrap for the repository's uninstalled bridge and pinned dependency."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))

# The repository pins cryptopos-core in its local virtual environment, while
# the required verification command deliberately uses the system `python3`.
# The package is pure Python, so make that pinned installation importable
# without activating the environment or installing anything globally.
site_packages = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
if not site_packages:
    raise RuntimeError("the repository virtual environment has no site-packages directory")
sys.path.insert(0, str(site_packages[0]))
