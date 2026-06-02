#!/usr/bin/env python3
"""
OMNIBRAIN // COGNITION TERMINAL  —  launcher.

    python python.py

Opens a LAN-syncable chat terminal that talks to any OpenAI-compatible
endpoint. All settings and conversations are shared live across every screen
connected to this server.

Environment overrides:
    OMNIBRAIN_PORT   (default 5005)
"""

from app import run

if __name__ == "__main__":
    run()
