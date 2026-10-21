import shutil

import pytest

from covenant_engine import config


@pytest.fixture
def abs_dir(tmp_path):
    """A private copy of the stored abstracts, so tests that write (sign-off) never touch data/abstract/v1."""
    d = tmp_path / "abstracts"
    shutil.copytree(config.ROOT / config.ABSTRACT_DIR, d)
    return str(d)
