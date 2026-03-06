# Adminer with Teleport Proxy

Orchestrator for running Adminer instances with Teleport database proxy tunnels. This tool automatically sets up secure database connections through Teleport and provides web-based database management via Adminer.

## Features

- 🔒 Secure database access through Teleport proxy
- 🐳 Containerized Adminer instances (Podman/Docker)
- � Automatic container runtime detection (supports Docker Compose v2, Podman Compose, Docker Compose v1)
- �🔌 Automatic port forwarding and tunnel management
- ✅ Port availability checking before startup
- 🎯 Selective database launching
- 🧹 Clean shutdown handling (Ctrl+C)

## Prerequisites

- Python 3.6+
- [Teleport](https://goteleport.com/) (`tsh` CLI tool)
- Container runtime with compose: **one of the following:**
  - [Docker](https://www.docker.com/) with `docker compose` (v2 plugin)
  - [Podman](https://podman.io/) with `podman-compose`
  - `docker-compose` (v1 standalone)
- `socat` for port forwarding
- PyYAML package: `pip install pyyaml`

## Configuration

Database configurations are stored in `settings.json` (git-ignored). Create this file from the provided template:

```bash
cp settings.example.json settings.json
```

Then edit `settings.json` to configure your databases:

```json
{
  "databases": [
    {
      "name": "kns_utils_staging",           // Unique identifier
      "cluster": "kns-utils-staging-huawei", // Teleport database cluster name
      "db_system": "pgsql",                  // Database type: "pgsql" or "mysql"
      "db_user": "teleporteditor",           // Database user
      "bridge_port": 5433,                   // Local port for database connection
      "adminer_port": 8081                   // Web interface port
    }
  ]
}
```

### Configuration Validation

The script validates all database configurations on startup:
- **Required fields**: `name`, `cluster`, `db_system`, `db_user`, `bridge_port`, `adminer_port`
- **db_system**: Must be `pgsql` or `mysql`
- **Port numbers**: Must be integers between 1 and 65535
- **Unique names**: Each database must have a unique name

### Port Architecture

Each database uses three ports:
- **adminer_port**: Web UI access (e.g., 8081)
- **bridge_port**: socat relay for container access (e.g., 5433)
- **hidden_port**: Teleport tunnel endpoint (auto: bridge_port + 1000)

## Usage

### Run All Databases

```bash
python main.py
```

### Run Specific Databases

Space-separated:
```bash
python main.py db_staging_1 kns_utils_staging
```

Comma-separated:
```bash
python main.py db_staging_1,kns_utils_staging
```

Single database:
```bash
python main.py kns_utils_staging
```

### Access Adminer

Once running, access the web interface at the URLs displayed in the output:
```
📦 kns_utils_staging
 ├─ Tunnel: 5433 → 6433
 ├─ Database: PGSQL (user: teleporteditor)
 └─ Adminer: http://localhost:8081/?pgsql=host.containers.internal:5433&username=teleporteditor
```

Click the Adminer URL or navigate to it in your browser.

## How It Works

1. **Pre-flight Checks**: Verifies container runtime, `tsh`, `socat` are installed, and Teleport is logged in
2. **Port Check**: Validates all required ports are available
3. **Compose Generation**: Creates `compose.tsh.yml` with Adminer container configs
4. **Container Startup**: Launches Adminer containers via detected compose tool
5. **Tunnel Creation**: For each database:
   - Starts `tsh proxy db --tunnel` on hidden_port
   - Starts `socat` relay forwarding bridge_port → hidden_port
6. **Ready**: Adminer instances are accessible via web browser

### Architecture Diagram

```
Browser → Adminer Container → host.containers.internal:bridge_port
                                        ↓ (socat)
                              localhost:bridge_port → localhost:hidden_port
                                                            ↓ (tsh tunnel)
                                                    Teleport → Database
```

## Stopping

Press `Ctrl+C` to gracefully shutdown:
- Stops all containers
- Terminates all tunnels and relays
- Cleans up resources

## Troubleshooting

### Pre-flight Check Failures

If you see errors during pre-flight checks:

**tsh not installed:**
```bash
# Install Teleport
# See: https://goteleport.com/docs/installation/
```

**socat not installed:**
```bash
# Ubuntu/Debian
sudo apt install socat

# macOS
brew install socat
```

**Not logged in to Teleport:**
```bash
tsh login --proxy=your-proxy.teleport.sh
```

**No container compose tool found:**
```bash
# Install Docker (with compose v2)
# See: https://docs.docker.com/get-docker/

# OR install Podman with podman-compose
sudo apt install podman podman-compose  # Ubuntu/Debian
brew install podman podman-compose      # macOS

# OR install docker-compose v1
pip install docker-compose
```

### Port Already in Use

If you see port availability errors:
```bash
❌ Error: The following ports are already in use:
   • kns_utils_staging: bridge_port (5433)
```

**Solutions:**
1. Find and stop the process using the port: `lsof -ti:5433 | xargs kill`
2. Change the port in your configuration
3. Run only databases whose ports are available

### Database Not Found

```bash
❌ Error: The following database(s) do not exist in configuration:
   • my_database
```

Check your `settings.json` file for available database names.

### Settings File Errors

**File not found:**
```bash
cp settings.example.json settings.json
# Then edit settings.json with your configurations
```

**Invalid JSON:**
Ensure your `settings.json` has valid JSON syntax (use a JSON validator).

**Missing required fields:**
Each database must have: `name`, `cluster`, `db_system`, `db_user`, `bridge_port`, `adminer_port`

**Invalid db_system:**
Only `pgsql` and `mysql` are supported.

### Container Issues

Check container status (replace with your compose command):
```bash
# Docker Compose v2
docker compose -f compose.tsh.yml ps

# Podman Compose
podman-compose -f compose.tsh.yml ps

# Docker Compose v1
docker-compose -f compose.tsh.yml ps
```

View logs:
```bash
# Docker Compose v2
docker compose -f compose.tsh.yml logs

# Podman Compose
podman-compose -f compose.tsh.yml logs

# Docker Compose v1
docker-compose -f compose.tsh.yml logs
```

## Customization

### Adminer Theme

Change the theme in `generate_compose_file()`:
```python
"ADMINER_DESIGN": "hever",  # Options: hever, pepa-linha, etc.
```

### Plugins

Add Adminer plugins to `plugins-enabled/` directory. They're automatically mounted into containers.

## File Structure

```
.
├── main.py                      # Main orchestrator script
├── settings.json                # Database configurations (git-ignored)
├── settings.example.json        # Configuration template
├── compose.tsh.yml              # Auto-generated compose file (git-ignored)
├── plugins-enabled/             # Adminer plugins
│   └── login-password-less.php  # Passwordless login plugin
└── README.md                    # This file
```

## License

This is an internal infrastructure tool. Use according to your organization's policies.
