# Adminer with Teleport Proxy

Orchestrator for running Adminer instances with Teleport database proxy tunnels. This tool automatically sets up secure database connections through Teleport and provides web-based database management via Adminer.

## Features

- 🔒 Secure database access through Teleport proxy
- 🐳 Containerized Adminer instances (Podman/Docker)
- 🔍 Automatic container runtime detection (supports Docker Compose v2, Podman Compose, Docker Compose v1)
- 🔌 Automatic port forwarding and tunnel management
- ✅ Port availability checking before startup
- 🎯 Selective database launching
- 🧹 Clean shutdown handling (Ctrl+C)

## Prerequisites

- Python 3.13+ (managed via [uv](https://docs.astral.sh/uv/))
- [uv](https://docs.astral.sh/uv/) for Python environment and dependency management
- [Teleport](https://goteleport.com/) (`tsh` CLI tool)
- Container runtime with compose: **one of the following:**
  - [Docker](https://www.docker.com/) with `docker compose` (v2 plugin)
  - [Podman](https://podman.io/) with `podman-compose`
  - `docker-compose` (v1 standalone)
- `socat` for port forwarding

## Installation

Sync dependencies using uv:

```bash
uv sync
```

This will:
- Create a virtual environment in `.venv/`
- Install Python 3.13 (if not already available)
- Install all dependencies from `pyproject.toml`

## Configuration

Database configurations are stored in `settings.json` (git-ignored) by default. Create this file from the provided template:

```bash
cp settings.example.json settings.json
```

Then edit `settings.json` to configure your databases:

```json
{
  "databases": [
    {
      "name": "example_database",      // Unique identifier
      "cluster": "your-cluster-name",  // Teleport database cluster name
      "db_system": "pgsql",            // Database type: "pgsql" or "mysql"
      "db_user": "your-username",      // Database user
      "db_name": "my_database",        // Database name (optional, but required by some newer Teleport versions, especially for PostgreSQL)
      "bridge_port": 5433,             // Local port for database connection
      "adminer_port": 8081             // Web interface port
    },
    {
      "name": "another_database",
      "cluster": "another-cluster-name",
      "db_system": "mysql",
      "db_user": "your-username",
      "bridge_port": 3307,
      "adminer_port": 8082
    }
  ]
}
```

**Note**: The `db_name` field is optional but **strongly recommended**. Some newer versions of Teleport require it (especially when connecting to PostgreSQL databases). If omitted, the `--db-name` argument won't be passed to the `tsh proxy db` command.

### Environment Variables

You can customize file paths using environment variables:

- **ADMINER_TELEPORT_SETTING_PATH**: Path to settings file (default: `./settings.json`)
- **ADMINER_TELEPORT_OUTPUT_DIR**: Directory for log files (default: `./output`)

Example:
```bash
export ADMINER_TELEPORT_SETTING_PATH=/path/to/custom/settings.json
export ADMINER_TELEPORT_OUTPUT_DIR=/tmp/adminer-logs
uv run main.py
```

### Configuration Validation

The script validates all database configurations on startup:
- **Required fields**: `name`, `cluster`, `db_system`, `db_user`, `bridge_port`, `adminer_port`
- **Optional fields**: `db_name` (recommended, required by newer Teleport versions for PostgreSQL)
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
uv run main.py
```

### Run Specific Databases

Space-separated:
```bash
uv run main.py example_database another_database
```

Comma-separated:
```bash
uv run main.py example_database,another_database
```

Single database:
```bash
uv run main.py example_database
```

### Access Adminer

Once running, access the web interface at the URLs displayed in the output:
```
📦 example_database
 ├─ Tunnel: 5433 → 6433
 ├─ Database: PGSQL (user: your-username)
 └─ Adminer: http://localhost:8081/?pgsql=host.containers.internal:5433&username=your-username
```

Click the Adminer URL or navigate to it in your browser. 

**Login**: The passwordless plugin is enabled by default. Use password `a` to login (can be changed in `plugins-enabled/login-password-less.php`).

## How It Works

1. **Pre-flight Checks**: Verifies container runtime, `tsh`, `socat` are installed, and Teleport is logged in
2. **Port Check**: Validates all required ports are available
3. **Compose Generation**: Creates `compose.yml` with Adminer container configs
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
   • example_database: bridge_port (5433)
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
Optional but recommended: `db_name` (required by newer Teleport versions, especially for PostgreSQL)

**Invalid db_system:**
Only `pgsql` and `mysql` are supported.

### Container Issues

Check container status (replace with your compose command):
```bash
# Docker Compose v2
docker compose -f compose.yml ps

# Podman Compose
podman-compose -f compose.yml ps

# Docker Compose v1
docker-compose -f compose.yml ps
```

View logs:
```bash
# Docker Compose v2
docker compose -f compose.yml logs

# Podman Compose
podman-compose -f compose.yml logs

# Docker Compose v1
docker-compose -f compose.yml logs
```

## Customization

### Adminer Theme

Change the theme in `generate_compose_file()`:
```python
"ADMINER_DESIGN": "hever",  # Options: hever, pepa-linha, etc.
```

### Plugins

Add Adminer plugins to `plugins-enabled/` directory. They're automatically mounted into containers.

#### Passwordless Login

The included `login-password-less.php` plugin enables login using a fixed password instead of the actual database password:
- **Default password**: `a`
- To change: Edit `plugins-enabled/login-password-less.php` and update the password in `password_hash("a", ...)` to your preferred value
- This is useful for quick access through Teleport proxy where authentication is already handled

## Logs

Process logs are automatically captured in the `output/` directory (configurable via `ADMINER_TELEPORT_OUTPUT_DIR`):
- `output/{database_name}_tsh.log` - Teleport tunnel logs
- `output/{database_name}_socat.log` - socat relay logs
- `output/compose.log` - Container compose logs

These logs are useful for debugging connection issues or monitoring tunnel activity.

## File Structure

```
.
├── main.py                      # Main orchestrator script
├── settings.json                # Database configurations (git-ignored)
├── settings.example.json        # Configuration template
├── compose.yml                  # Auto-generated compose file (git-ignored)
├── output/                      # Process logs directory (git-ignored)
│   ├── {db_name}_tsh.log       # Teleport tunnel stdout/stderr
│   ├── {db_name}_socat.log     # socat relay stdout/stderr
│   └── compose.log              # Container compose logs
├── plugins-enabled/             # Adminer plugins
│   └── login-password-less.php  # Passwordless login plugin
└── README.md                    # This file
```

## License

This is an internal infrastructure tool. Use according to your organization's policies.
