import subprocess
import signal
import sys
import socket
import json
import os
import threading
import time
from urllib.parse import urlencode
import yaml

COMPOSE_FILE = "compose.tsh.yml"
SETTINGS_FILE = "settings.json"
COMPOSE_CMD = None  # Will be set during pre-flight checks
OPEN_FILE_HANDLES = []  # Track file handles for cleanup

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


def load_settings():
    """Load and validate database settings from settings.json."""
    try:
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    except FileNotFoundError:
        print(f"❌ {SETTINGS_FILE} not found")
        print(f"Create {SETTINGS_FILE} with your database configurations.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in {SETTINGS_FILE}: {e}")
        sys.exit(1)

    if "databases" not in settings:
        print(f"❌ 'databases' key not found in {SETTINGS_FILE}")
        sys.exit(1)

    if not isinstance(settings["databases"], list):
        print(f"❌ 'databases' must be a list in {SETTINGS_FILE}")
        sys.exit(1)

    if not settings["databases"]:
        print(f"❌ No databases configured in {SETTINGS_FILE}")
        sys.exit(1)

    # Validate each database configuration
    for idx, db in enumerate(settings["databases"]):
        # Check required fields
        missing_fields = [field for field in REQUIRED_DB_FIELDS if field not in db]
        if missing_fields:
            print(
                f"❌ Database at index {idx} is missing required fields: {', '.join(missing_fields)}"
            )
            sys.exit(1)

        # Validate db_system
        if db["db_system"] not in ADMINER_DRIVER_MAP:
            print(
                f"❌ Invalid db_system '{db['db_system']}' for database '{db['name']}'. "
                f"Supported systems: {', '.join(ADMINER_DRIVER_MAP.keys())}"
            )
            sys.exit(1)

        # Validate port numbers
        for port_field in ["bridge_port", "adminer_port"]:
            if not isinstance(db[port_field], int) or not (
                1 <= db[port_field] <= 65535
            ):
                print(
                    f"❌ Invalid {port_field} '{db[port_field]}' for database '{db['name']}'. "
                    f"Port must be an integer between 1 and 65535"
                )
                sys.exit(1)

    return settings["databases"]


def get_hidden_port(bridge_port: int) -> int:
    return bridge_port + 1000


def is_port_available(port, host="127.0.0.1"):
    """Check if a port is available for binding."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.bind((host, port))
        sock.close()
        return True
    except (socket.error, OSError):
        return False


def check_all_ports(databases):
    """Check if all required ports are available before starting."""
    unavailable_ports = []

    for db in databases:
        bridge_port = db["bridge_port"]
        hidden_port = get_hidden_port(bridge_port)
        adminer_port = db["adminer_port"]

        if not is_port_available(bridge_port):
            unavailable_ports.append((db["name"], "bridge_port", bridge_port))
        if not is_port_available(hidden_port):
            unavailable_ports.append((db["name"], "hidden_port", hidden_port))
        if not is_port_available(adminer_port):
            unavailable_ports.append((db["name"], "adminer_port", adminer_port))

    if unavailable_ports:
        print("❌ The following ports are already in use:")
        for db_name, port_type, port in unavailable_ports:
            print(f"   - {db_name}: {port_type} ({port})")
        print("Please free these ports or update your configuration.")
        sys.exit(1)

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


def start_project_tunnels(db):
    """Starts the tsh tunnel and the socat relay."""
    hidden_port = get_hidden_port(db["bridge_port"])

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

    socat_cmd = [
        "socat",
        f"TCP-LISTEN:{db['bridge_port']},fork,reuseaddr",
        f"TCP:127.0.0.1:{hidden_port}",
    ]

    # Create output directory and open log files
    os.makedirs("output", exist_ok=True)
    tsh_log = open(f"output/{db['name']}_tsh.log", "w")
    socat_log = open(f"output/{db['name']}_socat.log", "w")
    OPEN_FILE_HANDLES.extend([tsh_log, socat_log])

    try:
        tsh_p = subprocess.Popen(tsh_cmd, stdout=tsh_log, stderr=tsh_log)
    except Exception as e:
        print(f"❌ Failed to start tsh for {db['name']}: {e}")
        tsh_log.close()
        socat_log.close()
        raise

    try:
        socat_p = subprocess.Popen(socat_cmd, stdout=socat_log, stderr=socat_log)
    except Exception as e:
        print(f"❌ Failed to start socat for {db['name']}: {e}")
        tsh_p.terminate()
        tsh_log.close()
        socat_log.close()
        raise

    adminer_driver = ADMINER_DRIVER_MAP[db["db_system"]]
    query_map = {
        adminer_driver: f"host.containers.internal:{db['bridge_port']}",
        "username": db["db_user"],
    }
    if "db_name" in db and db["db_name"]:
        query_map["db"] = db["db_name"]

    query_params = urlencode(query_map)
    adminer_url = f"http://localhost:{db['adminer_port']}/?{query_params}"

    print(f"🔗 [{db['name']}]")
    print(f"   - Tunnel: {db['bridge_port']} → {hidden_port}")
    print(f"   - Database: {db['db_system'].upper()} (user: {db['db_user']})")
    print(f"   - Adminer: {adminer_url}")

    return [
        {"process": tsh_p, "db_name": db["name"], "type": "tsh"},
        {"process": socat_p, "db_name": db["name"], "type": "socat"},
    ]


def check_command_exists(command):
    """Check if a command is available in the system."""
    try:
        subprocess.run(
            ["which", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def detect_compose_command():
    """Detect which container compose command is available."""
    # Check for docker compose (v2 plugin-style)
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
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
        # tsh status returns 0 if logged in, non-zero otherwise
        return result.returncode == 0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def run_preflight_checks():
    """Verify all prerequisites before starting the orchestrator."""
    global COMPOSE_CMD

    print("🔍 Running pre-flight checks...")

    checks_passed = True

    # Check container compose availability
    COMPOSE_CMD = detect_compose_command()
    if COMPOSE_CMD:
        compose_name = " ".join(COMPOSE_CMD)
        print(f"✅ Container runtime found: {compose_name}")
    else:
        print("❌ No container compose tool found")
        print("   Install one of: docker compose, podman-compose, or docker-compose")
        checks_passed = False
    if check_command_exists("tsh"):
        print("✅ tsh is installed")
    else:
        print("❌ tsh is not installed")
        print("   Install Teleport: https://goteleport.com/docs/installation/")
        checks_passed = False

    # Check socat installation
    if check_command_exists("socat"):
        print("✅ socat is installed")
    else:
        print("❌ socat is not installed")
        print("   Install with: sudo apt install socat  # or brew install socat")
        checks_passed = False

    # Check tsh login status
    if check_command_exists("tsh"):
        if check_tsh_logged_in():
            print("✅ Logged in to Teleport")
        else:
            print("❌ Not logged in to Teleport")
            print("   Log in with: tsh login --proxy=your-proxy.teleport.sh")
            checks_passed = False

    if not checks_passed:
        print("❌ Pre-flight checks failed. Please resolve the issues above.")
        sys.exit(1)


def filter_databases(requested_names, databases):
    """Filter databases based on requested names and validate they exist."""
    if not requested_names:
        # No arguments provided, return all databases
        return databases

    # Create a map of database names to configs
    db_map = {db["name"]: db for db in databases}
    available_names = list(db_map.keys())

    # Validate and filter
    filtered_dbs = []
    invalid_names = []

    for name in requested_names:
        if name in db_map:
            filtered_dbs.append(db_map[name])
        else:
            invalid_names.append(name)

    if invalid_names:
        print("❌ The following database(s) do not exist in configuration:")
        for name in invalid_names:
            print(f"   - {name}")
        print("Available databases:")
        for name in available_names:
            print(f"   - {name}")
        sys.exit(1)

    return filtered_dbs


def monitor_processes(process_list, shutdown_event):
    """Monitor all processes and terminate everything if any process fails."""
    while not shutdown_event.is_set():
        for proc_info in process_list:
            process = proc_info["process"]
            poll_result = process.poll()
            if poll_result is not None:  # Process has terminated
                log_file = f"output/{proc_info['db_name']}_{proc_info['type']}.log"
                print(
                    f"❌ {proc_info['type']} process for '{proc_info['db_name']}' has failed (exit code: {poll_result})"
                )
                print(f"   Check log file: {log_file}")
                print("🛑 Terminating all processes and shutting down...")

                # Signal shutdown
                shutdown_event.set()

                # Terminate all remaining processes
                for p_info in process_list:
                    p = p_info["process"]
                    if p.poll() is None:  # Only terminate if still running
                        p.terminate()

                # Shut down containers
                compose_log = open("output/compose.log", "a")
                subprocess.run(
                    [*COMPOSE_CMD, "-f", COMPOSE_FILE, "down"],
                    stdout=compose_log,
                    stderr=compose_log,
                )
                compose_log.close()

                # Close file handles
                for fh in OPEN_FILE_HANDLES:
                    try:
                        fh.close()
                    except Exception:
                        pass

                sys.exit(1)

        # Check every 2 seconds
        shutdown_event.wait(2)


def run_orchestrator(selected_databases):
    """Main execution loop."""
    # 0. Run pre-flight checks
    run_preflight_checks()

    # Create output directory for all logs
    os.makedirs("output", exist_ok=True)

    print(
        f"📦 Selected databases: {', '.join([db['name'] for db in selected_databases])}"
    )

    # 1. Check if all ports are available
    print("🔍 Checking port availability...")
    check_all_ports(selected_databases)

    # 2. Sync the compose file
    generate_compose_file(selected_databases)

    # 3. Start Container Compose
    print("🚀 Starting Adminer containers...")
    compose_log = open("output/compose.log", "a")
    subprocess.run(
        [*COMPOSE_CMD, "-f", COMPOSE_FILE, "up", "-d"],
        stdout=compose_log,
        stderr=compose_log,
    )
    compose_log.close()

    # 4. Start Tunnels and Relays
    print("🔗 Establishing tunnels and port forwarding...")
    process_list = []
    for db in selected_databases:
        try:
            process_list.extend(start_project_tunnels(db))
        except Exception as e:
            print(f"❌ Failed to start tunnels for {db['name']}: {e}")
            # Clean up any processes that were started
            for proc_info in process_list:
                proc_info["process"].terminate()
            compose_log = open("output/compose.log", "a")
            subprocess.run(
                [*COMPOSE_CMD, "-f", COMPOSE_FILE, "down"],
                stdout=compose_log,
                stderr=compose_log,
            )
            compose_log.close()
            sys.exit(1)

    # 5. Validate all processes started successfully
    print("⏳ Validating process startup...")
    time.sleep(2)  # Give processes time to fail if there's an issue
    failed_processes = []
    for proc_info in process_list:
        if proc_info["process"].poll() is not None:
            failed_processes.append(proc_info)

    if failed_processes:
        print("❌ The following processes failed to start:")
        for proc_info in failed_processes:
            log_file = f"output/{proc_info['db_name']}_{proc_info['type']}.log"
            print(
                f"   • {proc_info['type']} for {proc_info['db_name']} - check {log_file}"
            )
        # Clean up
        for proc_info in process_list:
            if proc_info["process"].poll() is None:
                proc_info["process"].terminate()
        compose_log = open("output/compose.log", "a")
        subprocess.run(
            [*COMPOSE_CMD, "-f", COMPOSE_FILE, "down"],
            stdout=compose_log,
            stderr=compose_log,
        )
        compose_log.close()
        for fh in OPEN_FILE_HANDLES:
            try:
                fh.close()
            except Exception:
                pass
        sys.exit(1)

    print("✅ Orchestrator active. Adminer instances are running.")
    print("👀 Monitoring processes for failures...")

    # Event for coordinating shutdown
    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        print("\n🛑 Shutting down containers and tunnels...")
        shutdown_event.set()
        # Redirect compose down output to log file
        compose_log = open("output/compose.log", "a")
        subprocess.run(
            [*COMPOSE_CMD, "-f", COMPOSE_FILE, "down"],
            stdout=compose_log,
            stderr=compose_log,
        )
        compose_log.close()
        for proc_info in process_list:
            p = proc_info["process"]
            if p.poll() is None:
                p.terminate()
        # Wait for processes to terminate gracefully
        time.sleep(1)
        for proc_info in process_list:
            p = proc_info["process"]
            if p.poll() is None:
                print(
                    f"⚠️  Force killing {proc_info['type']} for {proc_info['db_name']}"
                )
                p.kill()
        # Close file handles
        for fh in OPEN_FILE_HANDLES:
            try:
                fh.close()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Start monitoring thread
    monitor_thread = threading.Thread(
        target=monitor_processes,
        args=(process_list, shutdown_event),
        daemon=True,
    )
    monitor_thread.start()

    # Wait for shutdown signal
    shutdown_event.wait()


if __name__ == "__main__":
    # Load database settings
    databases = load_settings()

    # Parse command-line arguments
    requested_db_names = []
    if len(sys.argv) > 1:
        # Support both space-separated and comma-separated arguments
        for arg in sys.argv[1:]:
            # Split by comma and strip whitespace
            names = [name.strip() for name in arg.split(",") if name.strip()]
            requested_db_names.extend(names)

    # Filter and validate databases
    selected_databases = filter_databases(requested_db_names, databases)

    # Run orchestrator with selected databases
    run_orchestrator(selected_databases)
