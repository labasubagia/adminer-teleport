"""Entry point for the Adminer Teleport orchestrator."""

import sys
import asyncio

from adminer_teleport.exceptions import OrchestratorError
from adminer_teleport.orchestrator import run_orchestrator


if __name__ == "__main__":
    try:
        # Parse command-line arguments (space-separated or comma-separated)
        requested_db_names = [
            name.strip()
            for arg in sys.argv[1:]
            for name in arg.split(",")
            if name.strip()
        ]

        # Run orchestrator with selected databases
        asyncio.run(run_orchestrator(requested_db_names))
        sys.exit(0)
    except OrchestratorError as e:
        print("❌ Orchestrator error occurred. Please check the logs for details.")
        print(f"Error details: {e}")
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
