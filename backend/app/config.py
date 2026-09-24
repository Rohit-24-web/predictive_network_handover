"""
config.py — Application configuration.

Centralised settings via environment variables with sensible defaults.
No pydantic-settings dependency — simple and explicit.
"""
from __future__ import annotations

import os

# Resolve artifact directory relative to this file's location
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_APP_DIR)

ARTIFACT_DIR: str = os.environ.get(
    "ARTIFACT_DIR",
    os.path.join(_BACKEND_DIR, "artifacts"),
)

# Server settings
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))

# Logging
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
