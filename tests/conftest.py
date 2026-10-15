"""Use the real AstrBot API with an isolated runtime data directory."""

import os
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN.parent))
sys.path.insert(0, str(PLUGIN.parent / "AstrBot"))
RUNTIME = tempfile.TemporaryDirectory(prefix="astrbot-rclone-tests-")
os.environ["ASTRBOT_ROOT"] = RUNTIME.name
