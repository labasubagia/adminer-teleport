import subprocess
import signal
import sys
import socket
import json
import os
import shutil
import asyncio
from typing import List, Dict, Any, Optional
from urllib.parse import urlencode
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


COMPOSE_FILE = "compose.tsh.yml"
SETTINGS_FILE = "settings.json"
COMPOSE_CMD = None  # Will be set during pre-flight checks
HIDDEN_PORT_OFFSET = 1000

ADMINER_DRIVER_MAP = {
    "pgsql": "pgsql",
    "mysql": "server",
}

REQUIRED_DB_FIELDS = [
    "name",
    "cluster",
    "db_system",
    "db_user",
    "bridge_port",
    "adminer_port",
]


def validate_database_config(db: Dict[str, Any], idx: int) -> None:
    """Validate a single database configuration."""
    missing_fields = [field for field in REQUIRED_DB_FIELDS if field not in db]
    if missing_fields:
        raise ConfigurationError(
            f"Database at index {idx} is missing required fields: {', '.join(missing_fields)}"
        )

    if db["db_system"] not in ADMINER_DRIVER_MAP:
        raise ConfigurationError(
            f"Invalid db_system '{db['db_system']}' for database '{db['name']}'. "
            f"Supported systems: {', '.join(ADMINER_DRIVER_MAP.keys())}"
        )

    for port_field in ["bridge_port", "adminer_port"]:
        if not isinstance(db[port_field], int) or not (1 <= db[port_field] <= 65535):
            raise ConfigurationError(
                f"Invalid {port_field} '{db[port_field]}' for database '{db['name']}'. "
                f"Port must be an integer between 1 and 65535"
            )

    hidden_port = get_hidden_port(db["bridge_port"])
    if hidden_port > 65535:
        raise ConfigurationError(
            f"Invalid bridge_port '{db['bridge_port']}' for database '{db['name']}'. "
            f"Hidden port ({hidden_port}) would exceed 65535. Use bridge_port <= {65535 - HIDDEN_PORT_OFFSET}"
        )


def load_settings():
    """Load and validate database settings from settings.json."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    except FileNotFoundError:
        raise ConfigurationError(
            f"{SETTINGS_FILE} not found. Create {SETTINGS_FILE} with your database configurations."
        )
    except json.JSONDecodeError as e:
        raise ConfigurationError(f"Invalid JSON in {SETTINGS_FILE}: {e}")

    if "databases" not in settings:
        raise ConfigurationError(f"'databases' key not found in {SETTINGS_FILE}")

    if not isinstance(settings["databases"], list):
        raise ConfigurationError(f"'databases' must be a list in {SETTINGS_FILE}")

    if not settings["databases"]:
        raise ConfigurationError(f"No databases configured in {SETTINGS_FILE}")

    for idx, db in enumerate(settings["databases"]):
        validate_database_config(db, idx)

    return settings["databases"]


def get_hidden_port(bridge_port: int) -> int:
    return bridge_port + HIDDEN_PORT_OFFSET


def build_adminer_url(db: Dict[str, Any]) -> str:
    """Build the Adminer URL with query parameters for the database."""
    adminer_driver = ADMINER_DRIVER_MAP[db["db_system"]]
    query_map = {
        adminer_driver: f"host.containers.internal:{db['bridge_port']}",
        "username": db["db_user"],
    }
    if "db_name" in db and db["db_name"]:
        query_map["db"] = db["db_name"]

    query_params = urlencode(query_map)
    return f"http://localhost:{db['adminer_port']}/?{query_params}"


def print_database_info(db: Dict[str, Any], hidden_port: int, adminer_url: str) -> None:
    """Print connection information for a database."""
    print(f"🔗 [{db['name']}]")
    print(f"   - Tunnel: {db['bridge_port']} → {hidden_port}")
    print(f"   - Database: {db['db_system'].upper()} (user: {db['db_user']})")
    print(f"   - Adminer: {adminer_url}")


def is_port_available(port, host="127.0.0.1"):
    """Check if a port is available for binding."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.bind((host, port))
        return True
    except (socket.error, OSError):
        return False


def check_all_ports(databases):
    """Check if all required ports are available before starting."""
    unavailable_ports = []

    for db in databases:
        ports_to_check = [
            ("bridge_port", db["bridge_port"]),
            ("hidden_port", get_hidden_port(db["bridge_port"])),
            ("adminer_port", db["adminer_port"]),
        ]
        for port_type, port in ports_to_check:
            if not is_port_available(port):
                unavailable_ports.append((db["name"], port_type, port))

    if unavailable_ports:
        port_list = "\n".join(
            f"   - {db_name}: {port_type} ({port})"
            for db_name, port_type, port in unavailable_ports
        )
        raise PortAvailabilityError(
            f"The following ports are already in use:\n{port_list}\nPlease free these ports or update your configuration."
        )

    print("✅ All required ports are available.")


def generate_compose_file(databases):
    """Generates a compose file based on the DATABASES list."""
    compose_dict = {"services": {}, "networks": {"adminer_net": {"driver": "bridge"}}}

    for db in databases:
        service_name = db["name"].replace("-", "_")
        compose_dict["services"][service_name] = {
            "image": "adminer",
            "restart": "unless-stopped",
            "ports": [f"{db['adminer_port']}:8080"],
            "environment": {
                "ADMINER_DESIGN": "hever",
                "ADMINER_DEFAULT_SERVER": f"host.containers.internal:{db['bridge_port']}",
            },
            "volumes": ["./plugins-enabled:/var/www/html/plugins-enabled:ro"],
            "extra_hosts": ["host.containers.internal:host-gateway"],
            "networks": ["adminer_net"],
        }

    with open(COMPOSE_FILE, "w") as f:
        yaml.dump(compose_dict, f, default_flow_style=False)
    print(f"✅ {COMPOSE_FILE} synchronized.")


async def start_project_tunnels(db: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Starts the tsh tunnel and the socat relay."""
    hidden_port = get_hidden_port(db["bridge_port"])

    # Build tsh command
    tsh_cmd = [
        "tsh",
        "proxy",
        "db",
        "--tunnel",
        f"--port={hidden_port}",
        f"--db-user={db['db_user']}",
    ]
    if "db_name" in db and db["db_name"]:
        tsh_cmd.append(f"--db-name={db['db_name']}")
    tsh_cmd.append(db["cluster"])

    # Build socat command
    socat_cmd = [
        "socat",
        f"TCP-LISTEN:{db['bridge_port']},fork,reuseaddr",
        f"TCP:127.0.0.1:{hidden_port}",
    ]

    # Create output directory and open log files
    # Note: Files remain open and are closed later in cleanup()
    os.makedirs("output", exist_ok=True)

    tsh_log_path = f"output/{db['name']}_tsh.log"
    socat_log_path = f"output/{db['name']}_socat.log"

    tsh_log = open(tsh_log_path, "w")
    socat_log = open(socat_log_path, "w")
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
        tsh_log.close()
        socat_log.close()
        raise

    adminer_url = build_adminer_url(db)
    print_database_info(db, hidden_port, adminer_url)

    return [
        {
            "process": tsh_p,
            "db_name": db["name"],
            "type": "tsh",
            "log_path": tsh_log_path,
            "log_file": tsh_log,
        },
        {
            "process": socat_p,
            "db_name": db["name"],
            "type": "socat",
            "log_path": socat_log_path,
            "log_file": socat_log,
        },
    ]


def check_command_exists(command):
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


async def run_preflight_checks():
    """Verify all prerequisites before starting the orchestrator."""
    global COMPOSE_CMD

    print("🔍 Running pre-flight checks...")

    checks_passed = True

    COMPOSE_CMD = await detect_compose_command()
    if COMPOSE_CMD:
        compose_name = " ".join(COMPOSE_CMD)
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


def filter_databases(requested_names, databases):
    """Filter databases based on requested names and validate they exist."""
    if not requested_names:
        return databases

    db_map = {db["name"]: db for db in databases}

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


async def force_kill_process(proc_info: Dict[str, Any]) -> None:
    """Force kill a process that didn't terminate gracefully."""
    p = proc_info["process"]
    if p.returncode is None:
        print(f"⚠️  Force killing {proc_info['type']} for {proc_info['db_name']}")
        p.kill()
        try:
            await asyncio.wait_for(p.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def cleanup(process_list: List[Dict[str, Any]]) -> None:
    """Centralized cleanup function."""
    print("🛑 Shutting down containers and tunnels...")

    if not process_list:
        print("⚠️  No processes to clean up")
        return

    # Terminate all processes
    terminate_tasks = []
    for proc_info in process_list:
        p = proc_info["process"]
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
                *[force_kill_process(proc_info) for proc_info in process_list],
                return_exceptions=True,
            )

    # Close log file handles
    for proc_info in process_list:
        if "log_file" in proc_info:
            try:
                proc_info["log_file"].close()
            except Exception:
                pass

    # Shut down containers
    if COMPOSE_CMD:
        with open("output/compose.log", "a") as compose_log:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *COMPOSE_CMD,
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


async def validate_processes_started(process_list: List[Dict[str, Any]]) -> None:
    """Validate all processes started successfully after a brief delay."""
    print("⏳ Validating process startup...")
    await asyncio.sleep(2)

    failed_processes = [
        proc_info
        for proc_info in process_list
        if proc_info["process"].returncode is not None
    ]

    if failed_processes:
        failed_list = "\n".join(
            f"   • {proc_info['type']} for {proc_info['db_name']} - check {proc_info['log_path']}"
            for proc_info in failed_processes
        )
        raise ProcessStartupError(
            f"The following processes failed to start:\n{failed_list}"
        )


async def run_orchestrator(selected_databases: List[Dict[str, Any]]) -> None:
    """Main execution loop."""

    is_shutdown = False
    process_list = []

    def signal_handler_sync():
        nonlocal is_shutdown
        is_shutdown = True

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler_sync)
    loop.add_signal_handler(signal.SIGTERM, signal_handler_sync)

    try:
        # Run pre-flight checks
        await run_preflight_checks()

        # Remove and recreate output directory for clean logs
        if os.path.exists("output"):
            shutil.rmtree("output")
        os.makedirs("output")

        print(
            f"📦 Selected databases: {', '.join([db['name'] for db in selected_databases])}"
        )

        # Check if all ports are available
        print("🔍 Checking port availability...")
        check_all_ports(selected_databases)

        # Sync the compose file
        generate_compose_file(selected_databases)

        # Start Container Compose
        print("🚀 Starting Adminer containers...")
        with open("output/compose.log", "a") as compose_log:
            proc = await asyncio.create_subprocess_exec(
                *COMPOSE_CMD,
                "-f",
                COMPOSE_FILE,
                "up",
                "-d",
                stdout=compose_log,
                stderr=compose_log,
            )
            await proc.communicate()

        # Start Tunnels and Relays
        print("🔗 Establishing tunnels and port forwarding...")

        for db in selected_databases:
            try:
                process_list.extend(await start_project_tunnels(db))
            except Exception as e:
                raise ProcessStartupError(
                    f"Failed to start tunnels for {db['name']}: {e}"
                )

        # Validate all processes started successfully
        await validate_processes_started(process_list)

        print("✅ Orchestrator active. Adminer instances are running.")
        print("👀 Monitoring processes for failures...")

        # Monitor processes - wait for any process to exit
        wait_tasks = {}
        for proc_info in process_list:
            task = asyncio.create_task(proc_info["process"].wait())
            wait_tasks[task] = proc_info

        # Wait for any process to complete
        done, pending = await asyncio.wait(
            wait_tasks.keys(), return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel remaining wait tasks
        for task in pending:
            task.cancel()

        # Check if it was triggered by shutdown signal
        if is_shutdown:
            return

        # A process failed - find which one and handle it
        for completed_task in done:
            proc_info = wait_tasks[completed_task]
            exit_code = await completed_task
            raise OrchestratorError(
                f"{proc_info['type']} process for '{proc_info['db_name']}' failed with exit code {exit_code}. "
                f"Check log file: {proc_info['log_path']}"
            )
    except Exception as e:
        print(f"❌ {e}")
        raise
    finally:
        await cleanup(process_list)


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
