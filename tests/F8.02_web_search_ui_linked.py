"""FB6: real release UI from the linked package origin: English and French, provider-dependent fields,
responsive geometry, drafts and persistence. The managed origin runs in F8.01_web_search_ui.py; the case is in web_search_ui_case.py."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from web_search_ui_case import run


def main():
    shots = run("linked")
    assert len(shots) == 8 and all(path.is_file() and path.stat().st_size > 0 for path in shots), shots


if __name__ == "__main__":
    main()
