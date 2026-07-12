# SPDX-License-Identifier: GPL-2.0-or-later
"""Compatibility wrapper: the proven engine now ships with Impasto."""

import sys
from pathlib import Path

_ADDONS = str(Path(__file__).resolve().parents[2] / "addons")
if _ADDONS not in sys.path:
    sys.path.insert(0, _ADDONS)

from impasto.gpu_engine import *  # noqa: F401,F403
