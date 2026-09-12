"""Integration fixtures — must run INSIDE the released AnotherMe image.

The CI workflow (or local docker run) mounts this repo at /repo and its
plugin/ dir at /data/plugins/<PLUGIN_NAME>. These fixtures mirror the host's
make_config/make_client but pin plugins_dir to an empty dir so only the
mounted community plugins load, regardless of what the image bundles.
"""
import os
from pathlib import Path

import pytest

PLUGIN_NAME = "chat"
_COMMUNITY = Path("/data/plugins")


@pytest.fixture(autouse=True)
def _restore_env():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def make_config(tmp_path):
    from kernel.config import Config

    def factory(**overrides):
        bundled = tmp_path / "empty-bundled"
        bundled.mkdir(exist_ok=True)
        defaults = {
            "data_dir": tmp_path,
            "vaults_dir": tmp_path / "vaults",
            "db_path": tmp_path / "test.db",
            "main_vault": "test-main",
            "auth_password": "",
            "plugins_dir": bundled,
            "community_dir": _COMMUNITY,
        }
        defaults.update(overrides)
        return Config(**defaults)

    return factory


@pytest.fixture
def make_client(make_config):
    from fastapi.testclient import TestClient
    from kernel.app import create_app

    def factory(**config_overrides):
        return TestClient(create_app(make_config(**config_overrides)))

    return factory
