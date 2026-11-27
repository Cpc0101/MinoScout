#!/usr/bin/env python3
"""Check debug lifecycle contracts in disposable workers, with no target actions."""
import multiprocessing
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mino_scout.runtime.debug_probe import run

if __name__ == '__main__':
    multiprocessing.freeze_support()
    run()
