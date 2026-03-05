import subprocess
import time
import signal
import sys
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
        "internal_port": 6433,
        "adminer_port": 8081,
    },
    {
        "name": "db_staging_1",
        "cluster": "db-staging-1",
        "db_system": "mysql",
        "db_user": "teleporteditor",
        "bridge_port": 3307,
        "internal_port": 4307,
        "adminer_port": 8082,
    },
]


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
    tsh_cmd = [
        "tsh",
        "proxy",
        "db",
        "--tunnel",
        f"--port={db['internal_port']}",
        f"--db-user={db['db_user']}",
        db["cluster"],
    ]

    socat_cmd = [
        "socat",
        f"TCP-LISTEN:{db['bridge_port']},fork,reuseaddr",
        f"TCP:127.0.0.1:{db['internal_port']}",
    ]

    tsh_p = subprocess.Popen(
        tsh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    socat_p = subprocess.Popen(socat_cmd)

    adminer_url = f"http://localhost:{db['adminer_port']}/?{ADMINER_DRIVER_MAP[db['db_system']]}=host.containers.internal%3A{db['bridge_port']}&username={db['db_user']}"
    print(f"  📦 {db['name']}")
    print(f"   ├─ Tunnel: {db['bridge_port']} → {db['internal_port']}")
    print(f"   ├─ Database: {db['db_system'].upper()} (user: {db['db_user']})")
    print(f"   └─ Adminer: {adminer_url}")

    return [tsh_p, socat_p]


def run_orchestrator():
    """Main execution loop."""
    # 1. Sync the compose file first
    generate_compose_file(DATABASES)

    # 2. Start Podman Compose (optional: you can run this manually too)
    print("\n🚀 Starting Adminer containers...")
    subprocess.run(["podman-compose", "-f", COMPOSE_FILE, "up", "-d"])

    # 3. Start Tunnels and Relays
    print("\n🚀 Establishing tunnels and port forwarding...")
    all_processes = []
    for db in DATABASES:
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
    run_orchestrator()
