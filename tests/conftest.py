"""Test bootstrap. Before ``app`` is imported, DATA_DIR must point at a
throwaway directory, and authlib (a real dependency) is stubbed only if it
isn't installed."""

import os
import shutil
import sys
import tempfile
import types
from unittest import mock

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


# Each per-user store, the directory constant it's built from, and its folder.
_STORES = (
    ("_config_store", "CONFIG_DIR", "config"),
    ("_sync_store", "SYNC_STATE_DIR", "sync_state"),
    ("_import_store", "IMPORT_STATE_DIR", "import_state"),
    ("_scheduler_store", "SCHEDULER_STATE_DIR", "scheduler_state"),
    ("_edition_store", "EDITIONS_DIR", "editions"),
)


def isolate_state(case) -> str:
    """Point every store and the users file at a fresh directory for this
    test, with an empty status cache, all undone afterwards. Returns the
    directory."""
    import app as A

    data_dir = tempfile.mkdtemp(prefix="abs-sg-test-")
    case.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
    patches = {"USERS_FILE": f"{data_dir}/users.json"}
    for store, constant, folder in _STORES:
        patches[constant] = f"{data_dir}/{folder}"
        patches[store] = A._UserJsonStore(patches[constant])
    for name, value in patches.items():
        patcher = mock.patch.object(A, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)
    A._status_cache.clear()
    return data_dir
