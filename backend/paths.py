"""Filesystem locations shared by backend modules."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "jobs.db"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
ICON_PATH = PROJECT_ROOT / "assets" / "public" / "odradek-icon.png"
