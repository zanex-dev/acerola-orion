# -*- coding: utf-8 -*-
"""
Lightweight Antigravity `Stop` hook entrypoint (`python -m acerola_orion.vault_hook`).
Checks `vault_auto_backup` switch in `~/.gemini/acerola_orion/config.json` BEFORE importing heavy modules,
so turning the switch OFF skips disk scanning and backup module imports.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def _quick_switch_enabled() -> bool:
    home = os.environ.get("ACEROLA_ORION_HOME")
    cfg_path = (Path(home) if home else (Path.home() / ".gemini" / "acerola_orion")) / "config.json"
    if not cfg_path.is_file():
        return True
    try:
        obj = json.loads(cfg_path.read_text(encoding="utf-8-sig", errors="replace"))
        if isinstance(obj, dict) and "vault_auto_backup" in obj:
            return bool(obj["vault_auto_backup"])
    except Exception:
        pass
    return True


def main() -> int:
    try:
        if _quick_switch_enabled():
            from .vault import run_stop_hook_from_stdin

            out = run_stop_hook_from_stdin()
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
            return 0
    except Exception:
        pass
    sys.stdout.write('{"decision": "stop"}\n')
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
