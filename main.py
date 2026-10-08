"""Vercel FastAPI entrypoint; package media tools with the function."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
binary_dirs = [
    ROOT / "vendor" / "bin",
]
current_path = os.environ.get("PATH", "")
os.environ["PATH"] = os.pathsep.join([str(path) for path in binary_dirs if path.exists()] + [current_path])

from server.vercel_app import create_vercel_app

app = create_vercel_app()
