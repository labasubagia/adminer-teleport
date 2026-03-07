"""Docker Compose file generation."""

import yaml
from typing import List

from .models import Database
from .config import COMPOSE_PATH


def generate_compose_file(databases: List[Database]) -> None:
    """Generates a compose file based on the DATABASES list."""
    compose_dict = {"services": {}}

    for db in databases:
        compose_dict["services"][db.service_name] = db.to_compose_service()

    with open(COMPOSE_PATH, "w") as f:
        yaml.dump(compose_dict, f, default_flow_style=False)
    print(f"✅ {COMPOSE_PATH} synchronized.")
