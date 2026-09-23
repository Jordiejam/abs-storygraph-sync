"""Test bootstrap shared by the route tests.

Two things have to happen before ``app`` is imported: DATA_DIR must point at a
throwaway directory (the module reads it at import time and the tests write real
state files), and authlib must be importable. authlib is a real dependency —
see requirements.txt — so it is only stubbed when running outside the container,
never masked when it is actually installed.
"""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="abs-sg-tests-"))

try:
    import authlib.integrations.flask_client  # noqa: F401
except ModuleNotFoundError:
    class _OAuth:
        def __init__(self, *args, **kwargs):
            pass

        def register(self, *args, **kwargs):
            return None

    for name in ("authlib", "authlib.integrations"):
        sys.modules.setdefault(name, types.ModuleType(name))
    stub = types.ModuleType("authlib.integrations.flask_client")
    stub.OAuth = _OAuth
    sys.modules["authlib.integrations.flask_client"] = stub
