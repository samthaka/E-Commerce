import os

import pytest
from dotenv import load_dotenv


def pytest_configure(config):
    load_dotenv()
    env_config = os.getenv("ENV_CONFIG", "dev").strip().lower()
    if env_config != "test":
        raise pytest.UsageError("Tests can only run when ENV_CONFIG=test.")
