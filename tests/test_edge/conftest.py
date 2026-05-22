"""Skip tests/test_edge/ when the private edge submodule is not present.

CI checks out without submodules; running these tests would fail at import.
Local dev with the submodule sees them run normally.
"""
import importlib.util
from pathlib import Path

_EDGE_INIT = Path(__file__).parent.parent.parent / "edge" / "pool_indexer" / "__init__.py"

if not _EDGE_INIT.exists() or importlib.util.find_spec("edge.pool_indexer") is None:
    collect_ignore_glob = ["*.py"]
