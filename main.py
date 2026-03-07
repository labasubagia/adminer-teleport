import subprocess
import signal
import sys
import socket
import json
import os
import shutil
import asyncio
import re
from typing import List, Dict, Any, Optional, TextIO
from urllib.parse import urlencode
from dataclasses import dataclass
import yaml


# Custom Exceptions
class OrchestratorError(Exception):
    """Base exception for orchestrator errors."""


class ConfigurationError(OrchestratorError):
    """Raised when configuration is invalid."""


class PortAvailabilityError(OrchestratorError):
    """Raised when required ports are unavailable."""


class ProcessStartupError(OrchestratorError):
    """Raised when processes fail to start."""


class PreflightCheckError(OrchestratorError):
    """Raised when preflight checks fail."""


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


COMPOSE_FILE = "compose.yml"
SETTINGS_PATH = os.getenv(
    "ADMINER_TELEPORT_SETTING_PATH", os.path.join(os.getcwd(), "settings.json")
)
OUTPUT_DIR = os.getenv(
    "ADMINER_TELEPORT_OUTPUT_DIR", os.path.join(os.getcwd(), "output")
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


def generate_compose_file(databases: List[Database]) -> None:
    """Generates a compose file based on the DATABASES list."""
    compose_dict = {"services": {}}

    for db in databases:
        compose_dict["services"][db.service_name] = db.to_compose_service()

    with open(COMPOSE_FILE, "w") as f:
        yaml.dump(compose_dict, f, default_flow_style=False)
    print(f"✅ {COMPOSE_FILE} synchronized.")


async def start_db_tunnel(db: Database) -> List[ProcessInfo]:
    """Starts the tsh tunnel and the socat relay."""
    # Build commands using Database methods
    tsh_cmd = db.build_tsh_command()
    socat_cmd = db.build_socat_command()

    # Create output directory and open log files
    # Note: Files remain open and are closed later in cleanup()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    tsh_log = open(ProcessInfo.compute_log_path(db.name, "tsh"), "w")
    socat_log = open(ProcessInfo.compute_log_path(db.name, "socat"), "w")
    tsh_p = None

    try:
        tsh_p = await asyncio.create_subprocess_exec(
            *tsh_cmd, stdout=tsh_log, stderr=tsh_log
        )
        socat_p = await asyncio.create_subprocess_exec(
            *socat_cmd, stdout=socat_log, stderr=socat_log
        )
    except Exception:
        if tsh_p is not None:
            tsh_p.terminate()
            await tsh_p.wait()
        tsh_log.close()
        socat_log.close()
        raise

    db.print_info()

    return [
        ProcessInfo(
            process=tsh_p,
            db_name=db.name,
            type="tsh",
            log_file=tsh_log,
        ),
        ProcessInfo(
            process=socat_p,
            db_name=db.name,
            type="socat",
            log_file=socat_log,
        ),
    ]


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


async def cleanup(
    process_list: List[ProcessInfo], compose_cmd: Optional[List[str]] = None
) -> None:
    """Centralized cleanup function.

    Args:
        process_list: List of processes to terminate.
        compose_cmd: The compose command to use for shutting down containers.
    """
    print("🛑 Shutting down containers and tunnels...")

    if not process_list:
        print("⚠️  No processes to clean up")
        return

    # Terminate all processes
    terminate_tasks = []
    for proc_info in process_list:
        p = proc_info.process
        if p.returncode is None:
            p.terminate()
            terminate_tasks.append(p.wait())

    # Wait for processes to terminate gracefully
    if terminate_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*terminate_tasks, return_exceptions=True),
                timeout=5,
            )
        except asyncio.TimeoutError:
            print("⚠️  Some processes did not terminate gracefully, force killing...")
            await asyncio.gather(
                *[proc_info.force_kill() for proc_info in process_list],
                return_exceptions=True,
            )

    # Close log file handles
    for proc_info in process_list:
        try:
            proc_info.log_file.close()
        except Exception:
            pass

    # Shut down containers
    if compose_cmd:
        compose_log_path = os.path.join(OUTPUT_DIR, "compose.log")
        with open(compose_log_path, "a") as compose_log:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *compose_cmd,
                    "-f",
                    COMPOSE_FILE,
                    "down",
                    stdout=compose_log,
                    stderr=compose_log,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                print("⚠️  Compose down timed out")
                proc.kill()
            except Exception as e:
                print(f"⚠️  Error during compose down: {e}")


async def validate_processes_started(process_list: List[ProcessInfo]) -> None:
    """Validate all processes started successfully after a brief delay."""
    print("⏳ Validating process startup...")
    await asyncio.sleep(2)

    failed_processes = [
        proc_info
        for proc_info in process_list
        if proc_info.process.returncode is not None
    ]

    if failed_processes:
        failed_list = "\n".join(
            f"   • {proc_info.type} for {proc_info.db_name} - check {proc_info.log_file.name}"
            for proc_info in failed_processes
        )
        raise ProcessStartupError(
            f"The following processes failed to start:\n{failed_list}"
        )


async def run_orchestrator(selected_databases: List[Database]) -> None:
    """Main execution loop."""

    shutdown_event = asyncio.Event()
    process_list: List[ProcessInfo] = []

    def signal_handler_sync():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler_sync)
    loop.add_signal_handler(signal.SIGTERM, signal_handler_sync)

    try:
        # Run pre-flight checks
        compose_cmd = await run_preflight_checks()

        # Remove and recreate output directory for clean logs
        if os.path.exists(OUTPUT_DIR):
            shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR)

        print(
            f"📦 Selected databases: {', '.join([db.name for db in selected_databases])}"
        )

        # Check if all ports are available
        print("🔍 Checking port availability...")
        check_all_ports(selected_databases)

        # Sync the compose file
        generate_compose_file(selected_databases)

        # Start Container Compose
        print("🚀 Starting Adminer containers...")
        compose_log_path = os.path.join(OUTPUT_DIR, "compose.log")
        with open(compose_log_path, "w") as compose_log:
            proc = await asyncio.create_subprocess_exec(
                *compose_cmd,
                "-f",
                COMPOSE_FILE,
                "up",
                "-d",
                stdout=compose_log,
                stderr=compose_log,
            )
            await proc.communicate()
            if proc.returncode != 0:
                raise ProcessStartupError(
                    f"Container compose failed to start (exit code {proc.returncode}). Check {compose_log_path}"
                )

        # Start Tunnels and Relays
        print("🔗 Establishing tunnels and port forwarding...")

        for db in selected_databases:
            try:
                process_list.extend(await start_db_tunnel(db))
            except Exception as e:
                raise ProcessStartupError(f"Failed to start tunnels for {db.name}: {e}")

        # Validate all processes started successfully
        await validate_processes_started(process_list)

        print("✅ Orchestrator active. Adminer instances are running.")
        print("👀 Monitoring processes for failures...")

        # Monitor processes - wait for any process to exit
        wait_tasks = {}
        for proc_info in process_list:
            task = asyncio.create_task(proc_info.process.wait())
            wait_tasks[task] = proc_info

        # Wait for any process to complete
        done, pending = await asyncio.wait(
            wait_tasks.keys(), return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel remaining wait tasks
        for task in pending:
            task.cancel()

        # Check if it was triggered by shutdown signal
        if shutdown_event.is_set():
            return

        # A process failed - find which one and handle it
        for completed_task in done:
            proc_info = wait_tasks[completed_task]
            exit_code = await completed_task
            raise OrchestratorError(
                f"{proc_info.type} process for '{proc_info.db_name}' failed with exit code {exit_code}. "
                f"Check log file: {proc_info.log_file.name}"
            )
    except Exception as e:
        print(f"❌ {e}")
        raise
    finally:
        await cleanup(process_list, compose_cmd)


if __name__ == "__main__":
    try:
        # Load database settings
        databases = load_settings()

        # Parse command-line arguments (space-separated or comma-separated)
        requested_db_names = [
            name.strip()
            for arg in sys.argv[1:]
            for name in arg.split(",")
            if name.strip()
        ]

        # Filter and validate databases
        selected_databases = filter_databases(requested_db_names, databases)

        # Run orchestrator with selected databases
        asyncio.run(run_orchestrator(selected_databases))
        sys.exit(0)
    except OrchestratorError:
        sys.exit(1)
    except KeyboardInterrupt:
        # User interrupted - already handled by signal handler
        sys.exit(0)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        print("👋 Finished!")
