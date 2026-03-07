"""Data models for the Adminer orchestrator."""

import os
import re
import asyncio
from typing import List, Dict, Any, Optional, TextIO
from urllib.parse import urlencode
from dataclasses import dataclass

from .exceptions import ConfigurationError


@dataclass
class Database:
    """Database configuration."""

    name: str
    cluster: str
    db_system: str
    db_user: str
    bridge_port: int
    adminer_port: int
    db_name: Optional[str] = None

    HIDDEN_PORT_OFFSET = 1000

    ADMINER_DRIVER_MAP = {
        "pgsql": "pgsql",
        "mysql": "server",
    }

    REQUIRED_FIELDS = [
        "name",
        "cluster",
        "db_system",
        "db_user",
        "bridge_port",
        "adminer_port",
    ]

    def __post_init__(self):
        """Validate database configuration after initialization."""
        if self.db_system not in self.ADMINER_DRIVER_MAP:
            raise ConfigurationError(
                f"Invalid db_system '{self.db_system}' for database '{self.name}'. "
                f"Supported systems: {', '.join(self.ADMINER_DRIVER_MAP.keys())}"
            )

        for port_field, port_value in [
            ("bridge_port", self.bridge_port),
            ("adminer_port", self.adminer_port),
        ]:
            if not isinstance(port_value, int) or not (1 <= port_value <= 65535):
                raise ConfigurationError(
                    f"Invalid {port_field} '{port_value}' for database '{self.name}'. "
                    f"Port must be an integer between 1 and 65535"
                )

        if self.hidden_port > 65535:
            raise ConfigurationError(
                f"Invalid bridge_port '{self.bridge_port}' for database '{self.name}'. "
                f"Hidden port ({self.hidden_port}) would exceed 65535. Use bridge_port <= {65535 - self.HIDDEN_PORT_OFFSET}"
            )

    @property
    def hidden_port(self) -> int:
        """Calculate the hidden port for tsh tunnel."""
        return self.bridge_port + self.HIDDEN_PORT_OFFSET

    @property
    def service_name(self) -> str:
        """Get sanitized service name for Docker Compose.

        Only alphanumeric characters, hyphens, and underscores are allowed.
        Invalid characters are replaced with underscores.
        """
        return re.sub(r"[^a-zA-Z0-9_-]", "_", self.name)

    @property
    def adminer_url(self) -> str:
        """Build the Adminer URL with query parameters for the database."""
        adminer_driver = self.ADMINER_DRIVER_MAP[self.db_system]
        query_map = {
            adminer_driver: f"host.containers.internal:{self.bridge_port}",
            "username": self.db_user,
        }
        if self.db_name:
            query_map["db"] = self.db_name

        query_params = urlencode(query_map)
        return f"http://localhost:{self.adminer_port}/?{query_params}"

    def check_ports_available(self) -> List[tuple[str, int]]:
        """Check if all ports for this database are available.

        Returns:
            List of (port_type, port) tuples for ports that are unavailable.
        """
        from .utils import is_port_available

        unavailable = []
        for port_type, port in [
            ("bridge_port", self.bridge_port),
            ("hidden_port", self.hidden_port),
            ("adminer_port", self.adminer_port),
        ]:
            if not is_port_available(port):
                unavailable.append((port_type, port))
        return unavailable

    def print_info(self) -> None:
        """Print connection information for this database."""
        print(f"🔗 [{self.name}]")
        print(f"   - Tunnel: {self.bridge_port} → {self.hidden_port}")
        print(f"   - Database: {self.db_system.upper()} (user: {self.db_user})")
        print(f"   - Adminer: {self.adminer_url}")

    def build_tsh_command(self) -> List[str]:
        """Build the tsh tunnel command for this database."""
        cmd = [
            "tsh",
            "proxy",
            "db",
            "--tunnel",
            f"--port={self.hidden_port}",
            f"--db-user={self.db_user}",
        ]
        if self.db_name:
            cmd.append(f"--db-name={self.db_name}")
        cmd.append(self.cluster)
        return cmd

    def build_socat_command(self) -> List[str]:
        """Build the socat relay command for this database."""
        return [
            "socat",
            f"TCP-LISTEN:{self.bridge_port},fork,reuseaddr",
            f"TCP:127.0.0.1:{self.hidden_port}",
        ]

    def to_compose_service(self) -> Dict[str, Any]:
        """Generate the Docker Compose service definition for this database."""
        return {
            "image": "adminer",
            "restart": "unless-stopped",
            "ports": [f"{self.adminer_port}:8080"],
            "environment": {
                "ADMINER_DESIGN": "hever",
                "ADMINER_DEFAULT_SERVER": f"host.containers.internal:{self.bridge_port}",
            },
            "volumes": ["./plugins-enabled:/var/www/html/plugins-enabled:ro"],
            "extra_hosts": ["host.containers.internal:host-gateway"],
        }

    @classmethod
    def from_dict(cls, db_dict: Dict[str, Any], idx: int) -> "Database":
        """Create a Database instance from a dictionary with validation."""
        missing_fields = [
            field_name
            for field_name in cls.REQUIRED_FIELDS
            if field_name not in db_dict
        ]
        if missing_fields:
            raise ConfigurationError(
                f"Database at index {idx} is missing required fields: {', '.join(missing_fields)}"
            )

        return cls(
            name=db_dict["name"],
            cluster=db_dict["cluster"],
            db_system=db_dict["db_system"],
            db_user=db_dict["db_user"],
            bridge_port=db_dict["bridge_port"],
            adminer_port=db_dict["adminer_port"],
            db_name=db_dict.get("db_name"),
        )


@dataclass
class ProcessInfo:
    """Information about a running process."""

    process: asyncio.subprocess.Process
    db_name: str
    type: str
    log_file: TextIO

    @classmethod
    def compute_log_path(cls, db_name: str, process_type: str) -> str:
        """Compute log file path for a given database and process type.

        Args:
            db_name: Name of the database
            process_type: Type of process ('tsh' or 'socat')

        Returns:
            Full path to the log file
        """
        from .config import OUTPUT_DIR

        return os.path.join(OUTPUT_DIR, f"{db_name}_{process_type}.log")

    async def force_kill(self) -> None:
        """Force kill this process if it didn't terminate gracefully."""
        if self.process.returncode is None:
            print(f"⚠️  Force killing {self.type} for {self.db_name}")
            self.process.kill()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
