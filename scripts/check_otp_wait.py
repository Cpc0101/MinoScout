#!/usr/bin/env python3
"""Disposable OTP wait smoke; also runs in the existing worker-check/CI path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mino_scout.runtime.otp_probe import run

if __name__ == '__main__':
    raise SystemExit(run())
