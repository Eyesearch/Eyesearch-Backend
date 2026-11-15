"""The key script runs, which is not the same as its functions being tested.

`python scripts/api_key.py` imported `build_client` from the wrong module and
died with ImportError inside the container — after the deploy, in the operator's
hands, where no test had been. This one imports it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "api_key.py"


def test_the_script_imports():
    spec = importlib.util.spec_from_file_location("api_key_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)


def test_the_script_names_a_client_that_exists():
    """The factory it imports has to be the one the app actually uses."""
    import app.libsql_http as libsql_http

    assert hasattr(libsql_http, "build_client")


def test_the_government_seed_script_imports():
    """The seed is run by hand on the box, where an ImportError costs a deploy."""
    spec = importlib.util.spec_from_file_location(
        "seed_government", SCRIPT.parent / "seed_government.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.main)
    assert len(module.GOVERNMENT_SITES) > 20
    assert "https://pmkisan.gov.in/" in module.GOVERNMENT_SITES


def test_the_district_seed_uses_the_registry_names():
    """`mysuru.nic.in` does not exist; `mysore.nic.in` does.

    A state portal that answers 403 is not reachable, but its districts are on
    the same `.nic.in` estate that already crawls for other states.
    """
    spec = importlib.util.spec_from_file_location(
        "seed_government_districts", SCRIPT.parent / "seed_government.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert len(module.DISTRICT_SITES) > 20
    assert "https://mysore.nic.in/" in module.DISTRICT_SITES
    assert "https://bengaluruurban.nic.in/" in module.DISTRICT_SITES
    assert not any("mysuru" in url for url in module.DISTRICT_SITES)
