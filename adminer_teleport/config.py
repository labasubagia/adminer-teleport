"""Configuration loading and constants."""

import os
import json
from typing import List

from .models import Database
from .exceptions import ConfigurationError


# Constants
COMPOSE_PATH = os.path.realpath(
    os.getenv(
        "ADMINER_TELEPORT_COMPOSE_PATH",
        os.path.join(os.path.dirname(__file__), "..", "compose.yml"),
    )
)
SETTINGS_PATH = os.path.realpath(
    os.getenv(
        "ADMINER_TELEPORT_SETTING_PATH",
        os.path.join(os.path.dirname(__file__), "..", "settings.json"),
    )
)
OUTPUT_DIR = os.path.realpath(
    os.getenv(
        "ADMINER_TELEPORT_OUTPUT_DIR",
        os.path.join(os.path.dirname(__file__), "..", "output"),
    )
)


def load_settings() -> List[Database]:
    """Load and validate database settings from settings.json."""
    try:
        with open(SETTINGS_PATH, "r") as f:
            settings = json.load(f)
    except FileNotFoundError:
        raise ConfigurationError(
            f"{SETTINGS_PATH} not found. Create {SETTINGS_PATH} with your database configurations."
        )
    except json.JSONDecodeError as e:
        raise ConfigurationError(f"Invalid JSON in {SETTINGS_PATH}: {e}")

    if "databases" not in settings:
        raise ConfigurationError(f"'databases' key not found in {SETTINGS_PATH}")

    if not isinstance(settings["databases"], list):
        raise ConfigurationError(f"'databases' must be a list in {SETTINGS_PATH}")

    if not settings["databases"]:
        raise ConfigurationError(f"No databases configured in {SETTINGS_PATH}")

    databases = []
    for idx, db_dict in enumerate(settings["databases"]):
        databases.append(Database.from_dict(db_dict, idx))

    # Check for duplicate database names
    names = [db.name for db in databases]
    if len(names) != len(set(names)):
        duplicates = [name for name in names if names.count(name) > 1]
        raise ConfigurationError(
            f"Duplicate database names found: {', '.join(set(duplicates))}"
        )

    return databases


def filter_databases(
    requested_names: List[str], databases: List[Database]
) -> List[Database]:
    """Filter databases based on requested names and validate they exist."""
    if not requested_names:
        return databases

    db_map = {db.name: db for db in databases}

    filtered_dbs = [db_map[name] for name in requested_names if name in db_map]
    invalid_names = [name for name in requested_names if name not in db_map]

    if invalid_names:
        invalid_list = "\n".join([f"   - {name}" for name in invalid_names])
        available_list = "\n".join([f"   - {name}" for name in db_map])
        raise ConfigurationError(
            f"The following database(s) do not exist in configuration:\n{invalid_list}\n"
            f"Available databases:\n{available_list}"
        )

    return filtered_dbs
