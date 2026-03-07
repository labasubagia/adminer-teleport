"""Utility functions for system checks and port management."""

import socket
import subprocess
import shutil
import asyncio
from typing import List, Optional

from .models import Database
from .exceptions import PortAvailabilityError, PreflightCheckError


def is_port_available(port, host="127.0.0.1"):
    """Check if a port is available for binding."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.bind((host, port))
        return True
    except (socket.error, OSError):
        return False


def check_all_ports(databases: List[Database]) -> None:
    """Check if all required ports are available before starting."""
    unavailable_ports = []

    for db in databases:
        db_unavailable = db.check_ports_available()
        for port_type, port in db_unavailable:
            unavailable_ports.append((db.name, port_type, port))

    if unavailable_ports:
        port_list = "\n".join(
            f"   - {db_name}: {port_type} ({port})"
            for db_name, port_type, port in unavailable_ports
        )
        raise PortAvailabilityError(
            f"The following ports are already in use:\n{port_list}\nPlease free these ports or update your configuration."
        )

    print("✅ All required ports are available.")


def check_command_exists(command: str) -> bool:
    """Check if a command is available in the system."""
    return shutil.which(command) is not None


def check_command_available(command: str, not_found_message: str) -> bool:
    """Check if a command is available and print status."""
    if check_command_exists(command):
        print(f"✅ {command} is installed")
        return True
    else:
        print(f"❌ {command} is not installed")
        print(f"   {not_found_message}")
        return False


async def detect_compose_command() -> Optional[List[str]]:
    """Detect which container compose command is available."""
    # Check for docker compose (v2 plugin-style)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode == 0:
            return ["docker", "compose"]
    except (asyncio.TimeoutError, FileNotFoundError):
        pass

    # Check for podman-compose
    if check_command_exists("podman-compose"):
        return ["podman-compose"]

    # Check for docker-compose (v1 standalone)
    if check_command_exists("docker-compose"):
        return ["docker-compose"]

    return None


def check_tsh_logged_in():
    """Check if user is logged in to Teleport."""
    try:
        result = subprocess.run(
            ["tsh", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


async def run_preflight_checks() -> Optional[List[str]]:
    """Verify all prerequisites before starting the orchestrator.

    Returns:
        The detected compose command as a list of strings.
    """
    print("🔍 Running pre-flight checks...")

    checks_passed = True

    compose_cmd = await detect_compose_command()
    if compose_cmd:
        compose_name = " ".join(compose_cmd)
        print(f"✅ Container runtime found: {compose_name}")
    else:
        print("❌ No container compose tool found")
        print("   Install one of: docker compose, podman-compose, or docker-compose")
        checks_passed = False

    if check_command_available(
        "tsh", "Install Teleport: https://goteleport.com/docs/installation/"
    ):
        if check_tsh_logged_in():
            print("✅ Logged in to Teleport")
        else:
            print("❌ Not logged in to Teleport")
            print("   Log in with: tsh login --proxy=your-proxy.teleport.sh")
            checks_passed = False
    else:
        checks_passed = False

    if not check_command_available(
        "socat", "Install with: sudo apt install socat  # or brew install socat"
    ):
        checks_passed = False

    if not checks_passed:
        raise PreflightCheckError(
            "Pre-flight checks failed. Please resolve the issues above."
        )

    return compose_cmd
