"""Shared helpers for the hooks: find the Unity project and the ujob.py path."""
import os
import re

UJOB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "ujob.py").replace("\\", "/")


def _native(p):
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p or "")
    return f"{m.group(1).upper()}:{m.group(2) or '/'}" if os.name == "nt" and m else p


def find_unity_project(*starts):
    for start in starts:
        if not start:
            continue
        d = os.path.abspath(_native(start))
        while True:
            if os.path.isfile(os.path.join(d, "ProjectSettings", "ProjectVersion.txt")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None
