import subprocess
import time
import signal
import sys
import socket
from urllib.parse import urlencode
import yaml

COMPOSE_FILE = "compose.tsh.yml"

ADMINER_DRIVER_MAP = {
    "pgsql": "pgsql",
    "mysql": "server",
}

# Your centralized project configuration
DATABASES = [
    {
        "name": "kns_utils_staging",
        "cluster": "kns-utils-staging-huawei",
        "db_system": "pgsql",
        "db_user": "teleporteditor",
        "bridge_port": 5433,
        "adminer_port": 8081,
    },
    {
        "name": "db_staging_1",
        "cluster": "db-staging-1",
        "db_system": "mysql",
        "db_user": "teleporteditor",
        "bridge_port": 3307,
        "adminer_port": 8082,
    },
]


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
        print("❌ Error: The following ports are already in use:")
        for db_name, port_type, port in unavailable_ports:
            print(f"   • {db_name}: {port_type} ({port})")
        print("\nPlease free these ports or update your configuration.")
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
    print(f"✨ {COMPOSE_FILE} synchronized.")


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
        db["cluster"],
    ]

    socat_cmd = [
        "socat",
        f"TCP-LISTEN:{db['bridge_port']},fork,reuseaddr",
        f"TCP:127.0.0.1:{hidden_port}",
    ]

    tsh_p = subprocess.Popen(
        tsh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    socat_p = subprocess.Popen(socat_cmd)

    adminer_driver = ADMINER_DRIVER_MAP[db["db_system"]]
    query_params = urlencode(
        {
            adminer_driver: f"host.containers.internal:{db['bridge_port']}",
            "username": db["db_user"],
        }
    )
    adminer_url = f"http://localhost:{db['adminer_port']}/?{query_params}"

    print(f"  📦 {db['name']}")
    print(f"   ├─ Tunnel: {db['bridge_port']} → {hidden_port}")
    print(f"   ├─ Database: {db['db_system'].upper()} (user: {db['db_user']})")
    print(f"   └─ Adminer: {adminer_url}")

    return [tsh_p, socat_p]


def filter_databases(requested_names):
    """Filter DATABASES based on requested names and validate they exist."""
    if not requested_names:
        # No arguments provided, return all databases
        return DATABASES

    # Create a map of database names to configs
    db_map = {db["name"]: db for db in DATABASES}
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
        print("❌ Error: The following database(s) do not exist in configuration:")
        for name in invalid_names:
            print(f"   • {name}")
        print("\nAvailable databases:")
        for name in available_names:
            print(f"   • {name}")
        sys.exit(1)

    return filtered_dbs


def run_orchestrator(selected_databases):
    """Main execution loop."""
    print(
        f"📋 Selected databases: {', '.join([db['name'] for db in selected_databases])}\n"
    )

    # 1. Check if all ports are available
    print("🔍 Checking port availability...")
    check_all_ports(selected_databases)

    # 2. Sync the compose file
    generate_compose_file(selected_databases)

    # 3. Start Podman Compose (optional: you can run this manually too)
    print("\n🚀 Starting Adminer containers...")
    subprocess.run(["podman-compose", "-f", COMPOSE_FILE, "up", "-d"])

    # 4. Start Tunnels and Relays
    print("\n🚀 Establishing tunnels and port forwarding...")
    all_processes = []
    for db in selected_databases:
        all_processes.extend(start_project_tunnels(db))

    print("\n✅ Orchestrator active. Adminer instances are running.")

    def signal_handler(sig, frame):
        print("\nShutting down containers and tunnels...")
        subprocess.run(["podman-compose", "-f", COMPOSE_FILE, "down"])
        for p in all_processes:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    # Parse command-line arguments
    requested_db_names = []
    if len(sys.argv) > 1:
        # Support both space-separated and comma-separated arguments
        for arg in sys.argv[1:]:
            # Split by comma and strip whitespace
            names = [name.strip() for name in arg.split(",") if name.strip()]
            requested_db_names.extend(names)

    # Filter and validate databases
    selected_databases = filter_databases(requested_db_names)

    # Run orchestrator with selected databases
    run_orchestrator(selected_databases)
