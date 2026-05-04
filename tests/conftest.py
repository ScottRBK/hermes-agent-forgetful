"""Pytest configuration — make this dir an isolated rootdir.

The plugin's parent directory contains an ``__init__.py`` (because
hermes-agent's loader needs it), which makes pytest treat the whole
plugin as a Package and try to import that ``__init__.py`` at
collection time. ``__init__.py`` does package-relative imports
(``from .client import ...``) which fail when imported as a top-level
module from pytest.

Putting ``conftest.py`` here, combined with ``--import-mode=importlib``
in ``pytest.ini`` (sibling), keeps pytest from walking up past
``tests/``.
"""

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))
