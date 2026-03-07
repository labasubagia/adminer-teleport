"""Main orchestration logic for managing database tunnels and Adminer instances."""

import os
import shutil
import signal
import asyncio
from typing import List, Optional

from .models import Database, ProcessInfo
from .config import OUTPUT_DIR, COMPOSE_PATH, filter_databases, load_settings
from .utils import run_preflight_checks, check_all_ports
from .compose import generate_compose_file
from .exceptions import ProcessStartupError, OrchestratorError


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
                    COMPOSE_PATH,
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


async def run_orchestrator(requested_databases: List[str]) -> None:
    """Main execution loop."""

    shutdown_event = asyncio.Event()
    process_list: List[ProcessInfo] = []
    compose_cmd: Optional[List[str]] = None

    def signal_handler_sync():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler_sync)
    loop.add_signal_handler(signal.SIGTERM, signal_handler_sync)

    try:
        # Run pre-flight checks
        compose_cmd = await run_preflight_checks()

        # Load and filter settings
        settings = load_settings()
        selected_databases = filter_databases(requested_databases, settings)

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
                COMPOSE_PATH,
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
