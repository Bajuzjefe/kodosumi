"""
Boot process implementation for Kodosumi.

This module handles the step-by-step boot process:
- (A) Start Ray Deployment
- (B) Ray Serve Health-Check
- (C) Register Flows
- (D) Retrieve Flow Details
- (E) Iterate Expose Items and update meta
"""

import asyncio
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import yaml

from kodosumi.helper import HTTPXClient
from kodosumi.service.expose import db
from kodosumi.const import KODOSUMI_API, KODOSUMI_AUTHOR, KODOSUMI_ORGANIZATION

# Constants (also defined in control.py - kept in sync)
RAY_SERVE_CONFIG = "./data/serve_config.yaml"


def ensure_serve_config():
    """Ensure serve_config.yaml exists with defaults."""
    config_path = Path(RAY_SERVE_CONFIG)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        default_config = """# Kodosumi Ray Serve Configuration
proxy_location: EveryNode

http_options:
  host: 0.0.0.0
  port: 8005

grpc_options:
  port: 9000
  grpc_servicer_functions: []

logging_config:
  encoding: TEXT
  log_level: WARNING
  logs_dir: null
  enable_access_log: true
"""
        config_path.write_text(default_config)

# Configuration constants
BOOT_HEALTH_TIMEOUT = 300  # 5 minutes - apps install their own virtualenvs!
BOOT_POLL_INTERVAL = 2     # seconds between status polls

# Total number of main steps for progress tracking
BOOT_TOTAL_STEPS = 5  # A, B, C, D, E

# Ray Serve Application Status (from ray.serve.schema.ApplicationStatus)
FINAL_STATES = {"NOT_STARTED", "RUNNING", "UNHEALTHY", "DEPLOY_FAILED"}
TRANSITIONAL_STATES = {"DEPLOYING", "DELETING"}

# Default serve config template
DEFAULT_SERVE_CONFIG = {
    "proxy_location": "EveryNode",
    "http_options": {
        "host": "0.0.0.0",
        "port": 8005
    },
    "grpc_options": {
        "port": 9000,
        "grpc_servicer_functions": []
    },
    "logging_config": {
        "encoding": "TEXT",
        "log_level": "WARNING",
        "logs_dir": None,
        "enable_access_log": True
    }
}


def get_ray_serve_address_from_config(config_path: str = RAY_SERVE_CONFIG, fallback: str = "http://localhost:8005") -> str:
    """
    Extract Ray Serve HTTP address from serve_config.yaml.

    Reads http_options.host and http_options.port from the config.
    Falls back to the provided fallback address if config is missing or invalid.

    Args:
        config_path: Path to serve_config.yaml
        fallback: Default address if config is unavailable

    Returns:
        Ray Serve HTTP address (e.g., "http://localhost:8005")
    """
    path = Path(config_path)
    if not path.exists():
        return fallback

    try:
        with open(path, "r") as f:
            config = yaml.safe_load(f) or {}

        http_options = config.get("http_options", {})
        host = http_options.get("host", "localhost")
        port = http_options.get("port", 8005)

        # Convert 0.0.0.0 to localhost for client connections
        if host == "0.0.0.0":
            host = "localhost"

        return f"http://{host}:{port}"
    except Exception:
        return fallback


class BootStep(str, Enum):
    """Boot process main steps (phases)."""
    DEPLOY = "deploy"           # (A) Start Ray Deployment
    HEALTH = "health"           # (B) Ray Serve Health-Check
    REGISTER = "register"       # (C) Register Flows
    RETRIEVE = "retrieve"       # (D) Retrieve Flow Details
    UPDATE = "update"           # (E) Update Meta
    COMPLETE = "complete"
    ERROR = "error"


class MessageType(str, Enum):
    """Type of boot message."""
    STEP_START = "step_start"     # Starting a main step
    STEP_END = "step_end"         # Completed a main step
    ACTIVITY = "activity"         # Activity within a step
    RESULT = "result"             # Result/response from an activity
    INFO = "info"                 # General info
    WARNING = "warning"           # Warning message
    ERROR = "error"               # Error message
    PROGRESS = "progress"         # Progress update


@dataclass
class BootProgress:
    """Tracks overall boot progress."""
    current_step: int = 0
    total_steps: int = BOOT_TOTAL_STEPS
    step_name: str = ""
    activities_done: int = 0
    activities_total: int = 0

    @property
    def percent(self) -> int:
        """Calculate percentage complete."""
        if self.total_steps == 0:
            return 0
        base = (self.current_step / self.total_steps) * 100
        if self.activities_total > 0:
            step_progress = (self.activities_done / self.activities_total) * (100 / self.total_steps)
            return min(100, int(base + step_progress))
        return min(100, int(base))

    def to_dict(self) -> dict:
        return {
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "step_name": self.step_name,
            "activities_done": self.activities_done,
            "activities_total": self.activities_total,
            "percent": self.percent
        }


@dataclass
class BootMessage:
    """A message yielded during boot process."""
    step: BootStep
    msg_type: MessageType
    message: str
    timestamp: float = field(default_factory=time.time)
    target: Optional[str] = None      # e.g., expose name, URL
    result: Optional[str] = None      # e.g., status code, state
    progress: Optional[BootProgress] = None
    data: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        """Format message for console output."""
        parts = []

        # Step indicator
        if self.msg_type == MessageType.STEP_START:
            parts.append(f"=== [{self.step.value.upper()}] {self.message} ===")
        elif self.msg_type == MessageType.STEP_END:
            parts.append(f"--- [{self.step.value}] {self.message} ---")
        elif self.msg_type == MessageType.ERROR:
            parts.append(f"[ERROR] {self.message}")
        elif self.msg_type == MessageType.WARNING:
            parts.append(f"[WARN] {self.message}")
        else:
            # Activity/Result format
            if self.target:
                parts.append(f"  [{self.target}]")
            parts.append(f" {self.message}")
            if self.result:
                parts.append(f" -> {self.result}")

        return "".join(parts).strip()

    def to_json(self) -> dict:
        """Convert to JSON-serializable dict for streaming."""
        return {
            "step": self.step.value,
            "type": self.msg_type.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "target": self.target,
            "result": self.result,
            "progress": self.progress.to_dict() if self.progress else None,
            "text": str(self)
        }


class BootLock:
    """
    Manages boot process lock to prevent concurrent boots.

    Uses a simple in-memory singleton pattern with optional
    file-based persistence for crash recovery.
    """

    _instance: Optional["BootLock"] = None
    _lock_file = Path("./data/boot.lock")

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._locked = False
            cls._instance._lock_time: Optional[float] = None
            cls._instance._owner: Optional[str] = None
            cls._instance._messages: List[BootMessage] = []
            cls._instance._subscribers: List[asyncio.Queue] = []
        return cls._instance

    @property
    def is_locked(self) -> bool:
        return self._locked

    @property
    def lock_time(self) -> Optional[float]:
        return self._lock_time

    @property
    def messages(self) -> List[BootMessage]:
        return self._messages.copy()

    def acquire(self, owner: str = "system", force: bool = False) -> bool:
        """
        Acquire the boot lock.

        Args:
            owner: Identifier for lock owner
            force: If True, forcibly acquire even if locked

        Returns:
            True if lock acquired, False if already locked
        """
        if self._locked and not force:
            return False

        self._locked = True
        self._lock_time = time.time()
        self._owner = owner
        self._messages = []
        self._subscribers = []

        # Create lock file
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file.write_text(f"{owner}:{self._lock_time}")

        return True

    def release(self) -> None:
        """Release the boot lock."""
        self._locked = False
        self._lock_time = None
        self._owner = None

        # Remove lock file
        if self._lock_file.exists():
            self._lock_file.unlink()

    async def add_message(self, msg: BootMessage) -> None:
        """Add a message and notify all subscribers."""
        self._messages.append(msg)
        # Notify all subscribers
        for queue in self._subscribers:
            try:
                await queue.put(msg)
            except:
                pass

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to boot messages. Returns a queue that receives messages."""
        queue: asyncio.Queue = asyncio.Queue()
        # Send existing messages
        for msg in self._messages:
            queue.put_nowait(msg)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from boot messages."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)


# Global lock instance
boot_lock = BootLock()

# Background task reference (to prevent garbage collection)
_boot_task: Optional[asyncio.Task] = None


# =============================================================================
# Step A: Deploy Functions
# =============================================================================

def load_serve_config(config_path: str = RAY_SERVE_CONFIG) -> dict:
    """
    Load and parse serve_config.yaml, create default if missing.

    Args:
        config_path: Path to the serve config YAML file

    Returns:
        Parsed config dictionary with 'applications' key initialized to empty list
    """
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        # Create default config
        config = DEFAULT_SERVE_CONFIG.copy()
        config["applications"] = []
        with open(path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
        return config

    # Load existing config
    with open(path, "r") as f:
        config = yaml.safe_load(f) or {}

    # Ensure applications list exists
    if "applications" not in config:
        config["applications"] = []

    return config


def parse_bootstrap(bootstrap_yaml: str, expose_name: str) -> dict:
    """
    Parse expose bootstrap YAML into Ray Serve application config.

    The bootstrap field contains application-specific config like:
        import_path: mymodule:app
        runtime_env:
          pip: [openai, pydantic]

    This function adds the required name and route_prefix fields.

    Args:
        bootstrap_yaml: YAML string from expose.bootstrap field
        expose_name: Name of the expose (used for app name and route)

    Returns:
        Complete application config dict ready for Ray Serve

    Raises:
        ValueError: If bootstrap is empty or invalid YAML
    """
    if not bootstrap_yaml or not bootstrap_yaml.strip():
        raise ValueError(f"Empty bootstrap for expose '{expose_name}'")

    try:
        app_config = yaml.safe_load(bootstrap_yaml)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in bootstrap for '{expose_name}': {e}")

    if not isinstance(app_config, dict):
        raise ValueError(f"Bootstrap must be a YAML dict for '{expose_name}'")

    # Validate required field
    if "import_path" not in app_config:
        raise ValueError(f"Bootstrap missing 'import_path' for '{expose_name}'")

    # Add/override name and route_prefix
    app_config["name"] = expose_name
    app_config["route_prefix"] = f"/{expose_name}"

    return app_config


async def run_serve_deploy(config_path: str) -> tuple[int, str, str]:
    """
    Run 'serve deploy' command via asyncio subprocess.

    Args:
        config_path: Path to the merged serve config YAML file

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    process = await asyncio.create_subprocess_exec(
        "serve", "deploy", config_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout_bytes, stderr_bytes = await process.communicate()

    stdout = stdout_bytes.decode("utf-8") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8") if stderr_bytes else ""

    return (process.returncode or 0, stdout, stderr)


async def _step_deploy(
    progress: BootProgress
) -> AsyncGenerator[BootMessage, None]:
    """
    Step A: Deploy Ray Serve applications.

    1. Load global config from serve_config.yaml
    2. Get enabled exposes with bootstrap from database
    3. Parse each bootstrap and add to applications list
    4. Write merged config to temp file
    5. Run 'serve deploy' command

    Yields BootMessage objects for progress tracking.
    """
    # Initialize database
    await db.init_database()

    # Get all exposes
    exposes = await db.get_all_exposes()

    # Filter to enabled exposes with valid bootstrap
    enabled_exposes = [
        e for e in exposes
        if e.get("enabled") and e.get("bootstrap") and e.get("bootstrap").strip()
    ]

    # Setup progress tracking
    progress.current_step = 0
    progress.step_name = "Deploy"
    progress.activities_total = len(enabled_exposes) + 2  # +2 for load config and deploy command
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.STEP_START,
        message="Starting Ray Serve deployment",
        progress=progress
    )

    # Check if there are any exposes to deploy
    if not enabled_exposes:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.WARNING,
            message="No enabled exposes with bootstrap configuration found",
            progress=progress
        )
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.STEP_END,
            message="Deployment skipped (no applications)",
            progress=progress
        )
        return

    # Load global serve config
    try:
        config = load_serve_config()
        progress.activities_done += 1
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ACTIVITY,
            message="Loading global configuration",
            target="serve_config.yaml",
            result="OK",
            progress=progress
        )
    except Exception as e:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ERROR,
            message=f"Failed to load serve config: {e}",
            progress=progress
        )
        return

    # Parse each expose's bootstrap and build applications list
    applications = []
    deployed_names = []

    for expose in enabled_exposes:
        name = expose["name"]
        bootstrap = expose.get("bootstrap", "")

        try:
            app_config = parse_bootstrap(bootstrap, name)
            applications.append(app_config)
            deployed_names.append(name)

            progress.activities_done += 1
            yield BootMessage(
                step=BootStep.DEPLOY,
                msg_type=MessageType.ACTIVITY,
                message="Prepared deployment config",
                target=name,
                result=f"route=/{name}",
                progress=progress
            )
        except ValueError as e:
            yield BootMessage(
                step=BootStep.DEPLOY,
                msg_type=MessageType.WARNING,
                message=str(e),
                target=name,
                progress=progress
            )

    if not applications:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.WARNING,
            message="No valid applications to deploy",
            progress=progress
        )
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.STEP_END,
            message="Deployment skipped (no valid configs)",
            progress=progress
        )
        return

    # Merge applications into config
    config["applications"] = applications

    # Write merged config to temp file
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            delete=False,
            prefix="serve_deploy_"
        ) as f:
            yaml.dump(config, f, default_flow_style=False)
            temp_config_path = f.name

        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ACTIVITY,
            message="Created merged deployment config",
            target=temp_config_path,
            progress=progress
        )
    except Exception as e:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ERROR,
            message=f"Failed to write temp config: {e}",
            progress=progress
        )
        return

    # Run serve deploy
    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.ACTIVITY,
        message=f"Running serve deploy ({len(applications)} applications)",
        target="serve",
        progress=progress
    )

    try:
        returncode, stdout, stderr = await run_serve_deploy(temp_config_path)

        # Clean up temp file
        try:
            Path(temp_config_path).unlink()
        except:
            pass

        if returncode != 0:
            error_msg = stderr.strip() if stderr else f"Exit code {returncode}"
            yield BootMessage(
                step=BootStep.DEPLOY,
                msg_type=MessageType.ERROR,
                message=f"serve deploy failed: {error_msg}",
                progress=progress
            )
            return

        progress.activities_done += 1
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.RESULT,
            message="serve deploy command",
            result="success",
            progress=progress
        )

    except FileNotFoundError:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ERROR,
            message="'serve' command not found. Is Ray Serve installed?",
            progress=progress
        )
        return
    except Exception as e:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ERROR,
            message=f"serve deploy failed: {e}",
            progress=progress
        )
        return

    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.STEP_END,
        message=f"Deployment initiated ({len(applications)} applications)",
        progress=progress,
        data={"deployed_names": deployed_names}
    )


# =============================================================================
# Step B: Health Check Functions
# =============================================================================

def is_final_state(status: str) -> bool:
    """Check if status is a final (non-transitional) state."""
    return status in FINAL_STATES


async def get_current_status(ray_dashboard: str, app_names: Optional[List[str]] = None) -> Dict[str, dict]:
    """
    Get current status of Ray Serve applications (non-blocking).

    Unlike poll_until_final_state(), this returns immediately with current status.
    Use this for status pages or manual checks.

    Args:
        ray_dashboard: Ray dashboard URL
        app_names: Optional list of app names to filter. If None, returns all apps.

    Returns:
        Dict mapping app name to status info:
        {
            "app-name": {
                "name": "app-name",
                "status": "RUNNING",  # or DEPLOYING, UNHEALTHY, etc.
                "message": "",
                "is_final": True
            }
        }
    """
    try:
        all_status = await query_ray_serve_status(ray_dashboard)
    except Exception as e:
        # Return error status for all requested apps
        if app_names:
            return {
                name: {"name": name, "status": "ERROR", "message": str(e), "is_final": True}
                for name in app_names
            }
        return {}

    # Add is_final flag to each status
    for name, info in all_status.items():
        info["is_final"] = is_final_state(info.get("status", ""))

    # Filter if app_names provided
    if app_names:
        return {name: all_status.get(name, {"name": name, "status": "NOT_FOUND", "message": "", "is_final": True})
                for name in app_names}

    return all_status


async def query_ray_serve_status(ray_dashboard: str) -> Dict[str, dict]:
    """
    Query Ray Serve API for all application statuses.

    Args:
        ray_dashboard: Ray dashboard URL (e.g., http://localhost:8265)

    Returns:
        Dict mapping app name to status info:
        {
            "app-name": {
                "name": "app-name",
                "status": "RUNNING",
                "message": ""
            }
        }

    Raises:
        Exception on connection error
    """
    import httpx

    url = f"{ray_dashboard.rstrip('/')}/api/serve/applications/"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

    # Extract applications dict
    applications = data.get("applications", {})

    # Normalize to {name: {status, message, ...}}
    result = {}
    for name, info in applications.items():
        result[name] = {
            "name": name,
            "status": info.get("status", "UNKNOWN"),
            "message": info.get("message", ""),
        }

    return result


async def poll_until_final_state(
    ray_dashboard: str,
    app_names: List[str],
    timeout: int = BOOT_HEALTH_TIMEOUT,
    interval: int = BOOT_POLL_INTERVAL
) -> Dict[str, dict]:
    """
    Poll Ray Serve API until ALL apps reach a final state.

    Args:
        ray_dashboard: Ray dashboard URL
        app_names: List of application names to monitor
        timeout: Maximum seconds to wait
        interval: Seconds between polls

    Returns:
        Dict mapping app name to final status info

    Raises:
        TimeoutError if any app still transitioning after timeout
    """
    start_time = time.time()
    final_statuses: Dict[str, dict] = {}

    while True:
        elapsed = time.time() - start_time
        if elapsed >= timeout:
            # Mark remaining apps as timed out
            for name in app_names:
                if name not in final_statuses:
                    final_statuses[name] = {
                        "name": name,
                        "status": "TIMEOUT",
                        "message": f"Timed out after {timeout}s"
                    }
            raise TimeoutError(f"Health check timed out after {timeout}s")

        try:
            current_status = await query_ray_serve_status(ray_dashboard)
        except Exception as e:
            # Connection error - wait and retry
            await asyncio.sleep(interval)
            continue

        # Check each app
        all_final = True
        for name in app_names:
            if name in final_statuses:
                # Already reached final state
                continue

            if name in current_status:
                status = current_status[name]["status"]
                if is_final_state(status):
                    final_statuses[name] = current_status[name]
                else:
                    all_final = False
            else:
                # App not yet visible in Ray Serve
                all_final = False

        if all_final and len(final_statuses) == len(app_names):
            return final_statuses

        await asyncio.sleep(interval)


async def _step_health_check(
    ray_dashboard: str,
    deployed_names: List[str],
    progress: BootProgress
) -> AsyncGenerator[BootMessage, None]:
    """
    Step B: Wait for all deployed applications to reach a final state.

    Polls Ray Serve API until all apps are RUNNING, UNHEALTHY, DEPLOY_FAILED, etc.
    Each app installs its own virtualenv, so they complete at different times.

    Yields BootMessage objects for progress tracking.
    """
    progress.current_step = 1
    progress.step_name = "Health Check"
    progress.activities_total = len(deployed_names)
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.STEP_START,
        message=f"Waiting for deployments to complete (timeout: {BOOT_HEALTH_TIMEOUT}s)",
        progress=progress
    )

    if not deployed_names:
        yield BootMessage(
            step=BootStep.HEALTH,
            msg_type=MessageType.WARNING,
            message="No applications to check",
            progress=progress
        )
        yield BootMessage(
            step=BootStep.HEALTH,
            msg_type=MessageType.STEP_END,
            message="Health check skipped (no applications)",
            progress=progress
        )
        return

    # Track which apps have reached final state
    final_statuses: Dict[str, dict] = {}
    start_time = time.time()
    last_status: Dict[str, str] = {}

    while True:
        elapsed = time.time() - start_time

        # Check timeout
        if elapsed >= BOOT_HEALTH_TIMEOUT:
            # Mark remaining apps as timed out
            for name in deployed_names:
                if name not in final_statuses:
                    final_statuses[name] = {
                        "name": name,
                        "status": "TIMEOUT",
                        "message": f"Timed out after {BOOT_HEALTH_TIMEOUT}s"
                    }
                    yield BootMessage(
                        step=BootStep.HEALTH,
                        msg_type=MessageType.WARNING,
                        message=f"Health check timeout",
                        target=name,
                        result="TIMEOUT",
                        progress=progress
                    )
                    # Update database
                    await db.update_expose_state(name, "UNHEALTHY", time.time())
            break

        # Query Ray Serve API
        try:
            current_status = await query_ray_serve_status(ray_dashboard)
        except Exception as e:
            yield BootMessage(
                step=BootStep.HEALTH,
                msg_type=MessageType.WARNING,
                message=f"Failed to query Ray Serve API: {e}",
                progress=progress
            )
            await asyncio.sleep(BOOT_POLL_INTERVAL)
            continue

        # Check each app
        all_final = True
        for name in deployed_names:
            if name in final_statuses:
                continue

            if name in current_status:
                status = current_status[name]["status"]
                message = current_status[name].get("message", "")

                # Only emit message if status changed
                if name not in last_status or last_status[name] != status:
                    last_status[name] = status

                    if is_final_state(status):
                        final_statuses[name] = current_status[name]
                        progress.activities_done += 1

                        yield BootMessage(
                            step=BootStep.HEALTH,
                            msg_type=MessageType.RESULT,
                            message="Reached final state",
                            target=name,
                            result=status,
                            progress=progress
                        )

                        # Update database
                        db_state = "RUNNING" if status == "RUNNING" else "UNHEALTHY"
                        await db.update_expose_state(name, db_state, time.time())

                        if status in ("DEPLOY_FAILED", "UNHEALTHY"):
                            yield BootMessage(
                                step=BootStep.HEALTH,
                                msg_type=MessageType.WARNING,
                                message=message or f"Deployment failed",
                                target=name,
                                progress=progress
                            )
                    else:
                        # Still transitioning
                        all_final = False
                        yield BootMessage(
                            step=BootStep.HEALTH,
                            msg_type=MessageType.ACTIVITY,
                            message=f"Status: {status}",
                            target=name,
                            result=message if message else None,
                            progress=progress
                        )
            else:
                # App not yet visible
                all_final = False

        # Check if all done
        if all_final and len(final_statuses) == len(deployed_names):
            break

        await asyncio.sleep(BOOT_POLL_INTERVAL)

    # Summary
    running = sum(1 for s in final_statuses.values() if s["status"] == "RUNNING")
    failed = len(final_statuses) - running

    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.STEP_END,
        message=f"All deployments complete ({running} running, {failed} failed)",
        progress=progress,
        data={"final_statuses": final_statuses}
    )


# =============================================================================
# Step C: Register Flows Functions
# =============================================================================

@dataclass
class DiscoveredFlow:
    """A flow endpoint discovered from OpenAPI spec."""
    app_name: str
    path: str
    method: str
    summary: str
    description: str
    tags: List[str]
    author: Optional[str] = None
    organization: Optional[str] = None


async def fetch_openapi_spec(ray_serve_address: str, app_name: str) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """
    Fetch and parse OpenAPI JSON from a deployed app.

    Args:
        ray_serve_address: Ray Serve address (e.g., http://localhost:8005)
        app_name: Name of the application

    Returns:
        Tuple of (spec_dict, url, error_message):
        - (dict, url, None) on success
        - (None, url, error_string) on failure
    """
    import httpx

    url = f"{ray_serve_address.rstrip('/')}/{app_name}/openapi.json"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            if response.status_code == 404:
                return (None, url, f"404 Not Found")
            response.raise_for_status()
            return (response.json(), url, None)
    except httpx.TimeoutException:
        return (None, url, f"Timeout")
    except httpx.ConnectError as e:
        return (None, url, f"Connection error: {e}")
    except Exception as e:
        return (None, url, f"Error: {e}")


def extract_kodosumi_endpoints(openapi_spec: dict, app_name: str) -> List[DiscoveredFlow]:
    """
    Extract endpoints marked with x-kodosumi from OpenAPI spec.

    Looks for endpoints with openapi_extra containing:
    - x-kodosumi: true (the actual marker used by ServeAPI)
    - Also checks legacy variants: x-kodosumi-api, KODOSUMI_API

    Args:
        openapi_spec: Parsed OpenAPI JSON
        app_name: Name of the application

    Returns:
        List of DiscoveredFlow objects
    """
    flows = []
    paths = openapi_spec.get("paths", {})

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue

            # Check for Kodosumi marker in various locations
            is_kodosumi = False

            # Primary marker: x-kodosumi (used by ServeAPI)
            if operation.get(KODOSUMI_API) is True:
                is_kodosumi = True
            # Legacy variants for compatibility
            elif operation.get("x-kodosumi-api") is True:
                is_kodosumi = True
            elif operation.get("KODOSUMI_API") is True:
                is_kodosumi = True

            # Also check in custom extensions
            extensions = operation.get("x-openapi-extra", {})
            if extensions.get("KODOSUMI_API") is True:
                is_kodosumi = True

            if not is_kodosumi:
                continue

            # Build full path including app route prefix
            # OpenAPI paths are relative to the app's mount point
            full_path = f"/{app_name}{path}" if not path.startswith(f"/{app_name}") else path

            # Extract metadata using actual constants from const.py
            flow = DiscoveredFlow(
                app_name=app_name,
                path=full_path,
                method=method.upper(),
                summary=operation.get("summary", ""),
                description=operation.get("description", ""),
                tags=operation.get("tags", []),
                author=operation.get(KODOSUMI_AUTHOR),
                organization=operation.get(KODOSUMI_ORGANIZATION),
            )
            flows.append(flow)

    return flows


async def _step_register_flows(
    ray_serve_address: str,
    app_server: str,
    running_apps: List[str],
    auth_cookies: Optional[Dict[str, str]],
    progress: BootProgress
) -> AsyncGenerator[BootMessage, None]:
    """
    Step C: Register flow endpoints with Kodosumi.

    For each running app:
    1. Check if OpenAPI spec is available
    2. Call POST /flow/register with the OpenAPI URL to register flows

    Yields BootMessage objects for progress tracking.
    """
    import httpx

    progress.current_step = 2
    progress.step_name = "Register Flows"
    progress.activities_total = len(running_apps) * 2  # check + register per app
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.STEP_START,
        message="Registering flow endpoints",
        progress=progress
    )

    if not running_apps:
        yield BootMessage(
            step=BootStep.REGISTER,
            msg_type=MessageType.WARNING,
            message="No running applications to register",
            progress=progress
        )
        yield BootMessage(
            step=BootStep.REGISTER,
            msg_type=MessageType.STEP_END,
            message="Flow registration skipped (no applications)",
            progress=progress
        )
        return

    all_flows: List[DiscoveredFlow] = []
    registered_urls: List[str] = []

    for app_name in running_apps:
        # First check if OpenAPI spec is available
        spec, openapi_url, error = await fetch_openapi_spec(ray_serve_address, app_name)
        progress.activities_done += 1

        yield BootMessage(
            step=BootStep.REGISTER,
            msg_type=MessageType.ACTIVITY,
            message=f"GET {openapi_url}",
            target=app_name,
            result="200 OK" if spec else error,
            progress=progress
        )

        if spec is None:
            yield BootMessage(
                step=BootStep.REGISTER,
                msg_type=MessageType.WARNING,
                message=f"OpenAPI spec not available, skipping registration",
                target=app_name,
                progress=progress
            )
            progress.activities_done += 1  # skip register step
            continue

        # Show what paths were found
        paths = list(spec.get("paths", {}).keys())
        kodosumi_count = len(extract_kodosumi_endpoints(spec, app_name))

        yield BootMessage(
            step=BootStep.REGISTER,
            msg_type=MessageType.ACTIVITY,
            message=f"Found {len(paths)} path(s), {kodosumi_count} with x-kodosumi marker",
            target=app_name,
            result=", ".join(paths[:5]) + ("..." if len(paths) > 5 else ""),
            progress=progress
        )

        if kodosumi_count == 0:
            yield BootMessage(
                step=BootStep.REGISTER,
                msg_type=MessageType.INFO,
                message=f"No endpoints with '{KODOSUMI_API}' marker found",
                target=app_name,
                result="Use @app.enter() or add openapi_extra={'x-kodosumi': True}",
                progress=progress
            )
            progress.activities_done += 1  # skip register step
            continue

        # Call POST /flow/register to register the flows
        register_url = f"{app_server.rstrip('/')}/flow/register"

        yield BootMessage(
            step=BootStep.REGISTER,
            msg_type=MessageType.ACTIVITY,
            message=f"POST {register_url}",
            target=app_name,
            progress=progress
        )

        try:
            async with httpx.AsyncClient(timeout=30.0, cookies=auth_cookies) as client:
                response = await client.post(
                    register_url,
                    json={"url": openapi_url}
                )

                progress.activities_done += 1

                if response.status_code in (200, 201):
                    registered = response.json()
                    num_registered = len(registered) if isinstance(registered, list) else 0
                    registered_urls.append(openapi_url)

                    # Track discovered flows for step D
                    flows = extract_kodosumi_endpoints(spec, app_name)
                    all_flows.extend(flows)

                    yield BootMessage(
                        step=BootStep.REGISTER,
                        msg_type=MessageType.RESULT,
                        message=f"Registered {num_registered} flow(s)",
                        target=app_name,
                        result=f"from {openapi_url}",
                        progress=progress
                    )
                else:
                    yield BootMessage(
                        step=BootStep.REGISTER,
                        msg_type=MessageType.WARNING,
                        message=f"Registration failed: {response.status_code}",
                        target=app_name,
                        result=response.text[:100] if response.text else "No details",
                        progress=progress
                    )

        except httpx.TimeoutException:
            progress.activities_done += 1
            yield BootMessage(
                step=BootStep.REGISTER,
                msg_type=MessageType.WARNING,
                message="Registration request timed out",
                target=app_name,
                progress=progress
            )
        except Exception as e:
            progress.activities_done += 1
            yield BootMessage(
                step=BootStep.REGISTER,
                msg_type=MessageType.WARNING,
                message=f"Registration failed: {e}",
                target=app_name,
                progress=progress
            )

    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.STEP_END,
        message=f"Flow registration complete ({len(all_flows)} flows from {len(registered_urls)} apps)",
        progress=progress,
        data={"discovered_flows": all_flows, "registered_urls": registered_urls}
    )


# =============================================================================
# Step D: Retrieve Flows Functions
# =============================================================================

@dataclass
class FlowStatus:
    """Status of a discovered flow endpoint."""
    flow: DiscoveredFlow
    state: str  # alive, dead, not-found, timeout
    response_code: Optional[int]
    checked_at: float


async def check_flow_health(ray_serve_address: str, path: str, timeout: float = 10.0) -> tuple[str, Optional[int]]:
    """
    Send HEAD request to flow endpoint to check health.

    Args:
        ray_serve_address: Ray Serve address (e.g., http://localhost:8005)
        path: Full path to the endpoint (e.g., /my-agent/run)
        timeout: Request timeout in seconds

    Returns:
        Tuple of (state, response_code):
        - ("alive", 200) - Endpoint responding normally
        - ("alive", 405) - Method not allowed but endpoint exists
        - ("not-found", 404) - Endpoint not found
        - ("dead", 5xx) - Server error
        - ("timeout", None) - Request timed out
    """
    import httpx

    url = f"{ray_serve_address.rstrip('/')}{path}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.head(url)

            code = response.status_code

            if code in (200, 204, 405):
                # 405 means endpoint exists but HEAD not allowed - still alive
                return ("alive", code)
            elif code == 404:
                return ("not-found", code)
            elif 500 <= code < 600:
                return ("dead", code)
            else:
                # Other codes (3xx, 4xx) - treat as alive but note the code
                return ("alive", code)

    except httpx.TimeoutException:
        return ("timeout", None)
    except Exception:
        return ("dead", None)


async def check_all_flows(
    ray_serve_address: str,
    flows: List[DiscoveredFlow]
) -> Dict[str, List[FlowStatus]]:
    """
    Check health of all flows in parallel, grouped by app_name.

    Args:
        ray_serve_address: Ray Serve address
        flows: List of discovered flows to check

    Returns:
        Dict mapping app_name to list of FlowStatus objects
    """
    check_time = time.time()
    results: Dict[str, List[FlowStatus]] = {}

    # Create tasks for all flows
    async def check_one(flow: DiscoveredFlow) -> FlowStatus:
        state, code = await check_flow_health(ray_serve_address, flow.path)
        return FlowStatus(
            flow=flow,
            state=state,
            response_code=code,
            checked_at=check_time
        )

    # Run all checks in parallel
    flow_statuses = await asyncio.gather(*[check_one(f) for f in flows])

    # Group by app_name
    for status in flow_statuses:
        app_name = status.flow.app_name
        if app_name not in results:
            results[app_name] = []
        results[app_name].append(status)

    return results


async def _step_retrieve_flows(
    ray_serve_address: str,
    discovered_flows: List[DiscoveredFlow],
    progress: BootProgress
) -> AsyncGenerator[BootMessage, None]:
    """
    Step D: Test each discovered flow endpoint with HEAD requests.

    Verifies that each flow endpoint is responding.

    Yields BootMessage objects for progress tracking.
    """
    progress.current_step = 3
    progress.step_name = "Retrieve Flows"
    progress.activities_total = len(discovered_flows)
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.STEP_START,
        message="Testing flow endpoints",
        progress=progress
    )

    if not discovered_flows:
        yield BootMessage(
            step=BootStep.RETRIEVE,
            msg_type=MessageType.WARNING,
            message="No flows to test",
            progress=progress
        )
        yield BootMessage(
            step=BootStep.RETRIEVE,
            msg_type=MessageType.STEP_END,
            message="Flow retrieval skipped (no flows)",
            progress=progress
        )
        return

    # Group flows by app for reporting
    flows_by_app: Dict[str, List[DiscoveredFlow]] = {}
    for flow in discovered_flows:
        if flow.app_name not in flows_by_app:
            flows_by_app[flow.app_name] = []
        flows_by_app[flow.app_name].append(flow)

    # Report what we're checking
    for app_name, app_flows in flows_by_app.items():
        yield BootMessage(
            step=BootStep.RETRIEVE,
            msg_type=MessageType.ACTIVITY,
            message=f"Checking {len(app_flows)} endpoint(s)",
            target=app_name,
            progress=progress
        )

    # Check all flows
    flow_statuses = await check_all_flows(ray_serve_address, discovered_flows)

    # Report results
    total_alive = 0
    total_dead = 0

    for app_name, statuses in flow_statuses.items():
        for status in statuses:
            progress.activities_done += 1

            state_icon = {
                "alive": "200 OK",
                "not-found": "404",
                "dead": str(status.response_code or "error"),
                "timeout": "timeout"
            }.get(status.state, status.state)

            yield BootMessage(
                step=BootStep.RETRIEVE,
                msg_type=MessageType.RESULT,
                message=f"HEAD {status.flow.path}",
                target=app_name,
                result=f"{state_icon} ({status.state})",
                progress=progress
            )

            if status.state == "alive":
                total_alive += 1
            else:
                total_dead += 1

    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.STEP_END,
        message=f"Retrieved {len(discovered_flows)} flow endpoints ({total_alive} alive, {total_dead} dead)",
        progress=progress,
        data={"flow_statuses": flow_statuses}
    )


# =============================================================================
# Step E: Update Meta Functions
# =============================================================================

@dataclass
class ExposeMeta:
    """Metadata for a flow endpoint stored in expose.meta."""
    url: str         # Full path (e.g., "/app-name/run")
    name: str        # Endpoint name (e.g., "run")
    data: str        # YAML metadata (summary, description, tags)
    state: str       # "alive" | "dead" | "not-found" | "timeout"
    heartbeat: float # Timestamp when checked


def flow_status_to_meta(status: FlowStatus) -> ExposeMeta:
    """
    Convert FlowStatus to ExposeMeta for database storage.

    Args:
        status: FlowStatus from Step D

    Returns:
        ExposeMeta ready for serialization
    """
    flow = status.flow

    # Extract endpoint name from path (last segment)
    path_parts = flow.path.strip("/").split("/")
    name = path_parts[-1] if path_parts else flow.path

    # Build data YAML with metadata
    data_dict = {
        "summary": flow.summary or "",
        "description": flow.description or "",
        "method": flow.method,
        "tags": flow.tags or [],
    }
    if flow.author:
        data_dict["author"] = flow.author
    if flow.organization:
        data_dict["organization"] = flow.organization

    data_yaml = yaml.dump(data_dict, default_flow_style=False).strip()

    return ExposeMeta(
        url=flow.path,
        name=name,
        data=data_yaml,
        state=status.state,
        heartbeat=status.checked_at,
    )


def serialize_meta_list(metas: List[ExposeMeta]) -> str:
    """
    Serialize ExposeMeta list to YAML string.

    Args:
        metas: List of ExposeMeta objects

    Returns:
        YAML string for storage in expose.meta column
    """
    meta_dicts = []
    for meta in metas:
        meta_dicts.append({
            "url": meta.url,
            "name": meta.name,
            "data": meta.data,
            "state": meta.state,
            "heartbeat": meta.heartbeat,
        })

    return yaml.dump(meta_dicts, default_flow_style=False)


async def save_expose_meta(app_name: str, metas: List[ExposeMeta]) -> None:
    """
    Update expose.meta in database.

    Args:
        app_name: Name of the expose
        metas: List of ExposeMeta for this expose
    """
    meta_yaml = serialize_meta_list(metas)
    await db.update_expose_meta(app_name, meta_yaml)


async def _step_update_meta(
    flow_statuses: Dict[str, List[FlowStatus]],
    progress: BootProgress
) -> AsyncGenerator[BootMessage, None]:
    """
    Step E: Update expose metadata in database.

    Converts FlowStatus to ExposeMeta and saves to database.

    Yields BootMessage objects for progress tracking.
    """
    progress.current_step = 4
    progress.step_name = "Update Meta"
    progress.activities_total = len(flow_statuses)
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.STEP_START,
        message="Updating expose metadata",
        progress=progress
    )

    if not flow_statuses:
        yield BootMessage(
            step=BootStep.UPDATE,
            msg_type=MessageType.WARNING,
            message="No flow statuses to save",
            progress=progress
        )
        yield BootMessage(
            step=BootStep.UPDATE,
            msg_type=MessageType.STEP_END,
            message="Meta update skipped (no flows)",
            progress=progress
        )
        return

    total_saved = 0
    total_alive = 0

    for app_name, statuses in flow_statuses.items():
        progress.activities_done += 1

        yield BootMessage(
            step=BootStep.UPDATE,
            msg_type=MessageType.ACTIVITY,
            message=f"Saving {len(statuses)} flow entries",
            target=app_name,
            progress=progress
        )

        try:
            # Convert to ExposeMeta
            metas = [flow_status_to_meta(s) for s in statuses]

            # Save to database
            await save_expose_meta(app_name, metas)

            # Count results
            alive_count = sum(1 for m in metas if m.state == "alive")
            total_saved += len(metas)
            total_alive += alive_count

            yield BootMessage(
                step=BootStep.UPDATE,
                msg_type=MessageType.RESULT,
                message=f"Updated meta",
                target=app_name,
                result=f"{len(metas)} flows ({alive_count} alive)",
                progress=progress
            )

        except Exception as e:
            yield BootMessage(
                step=BootStep.UPDATE,
                msg_type=MessageType.WARNING,
                message=f"Failed to save meta: {e}",
                target=app_name,
                progress=progress
            )

    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.STEP_END,
        message=f"Metadata update complete ({total_saved} flows saved, {total_alive} alive)",
        progress=progress
    )


async def start_boot_background(
    ray_dashboard: str,
    ray_serve_address: str,
    app_server: str,
    auth_cookies: Optional[Dict[str, str]] = None,
    force: bool = False,
    owner: str = "system",
    mock: bool = True
) -> bool:
    """
    Start the boot process as a background task.

    Returns True if boot was started, False if already in progress.
    """
    global _boot_task

    # Check if already running (and not forcing)
    if boot_lock.is_locked and not force:
        return False

    async def run_task():
        try:
            async for msg in run_boot_process(
                ray_dashboard=ray_dashboard,
                ray_serve_address=ray_serve_address,
                app_server=app_server,
                auth_cookies=auth_cookies,
                force=force,
                owner=owner,
                mock=mock
            ):
                # Messages are already added to boot_lock in run_boot_process
                # Just consume the generator
                pass
        except asyncio.CancelledError:
            # Task was cancelled - add error message
            msg = BootMessage(
                step=BootStep.ERROR,
                msg_type=MessageType.ERROR,
                message="Boot process was cancelled"
            )
            await boot_lock.add_message(msg)
            boot_lock.release()
        except Exception as e:
            # Unexpected error
            msg = BootMessage(
                step=BootStep.ERROR,
                msg_type=MessageType.ERROR,
                message=f"Boot failed: {str(e)}"
            )
            await boot_lock.add_message(msg)
            boot_lock.release()

    # Start background task
    _boot_task = asyncio.create_task(run_task())

    # Give it a moment to acquire the lock
    await asyncio.sleep(0.1)

    return boot_lock.is_locked


async def run_boot_process(
    ray_dashboard: str,
    ray_serve_address: str,
    app_server: str,
    auth_cookies: Optional[Dict[str, str]] = None,
    force: bool = False,
    owner: str = "system",
    mock: bool = True  # TODO: Set to False when implementing real processes
) -> AsyncGenerator[BootMessage, None]:
    """
    Execute the boot process step by step.

    Yields BootMessage objects for each step and activity.

    Args:
        ray_dashboard: Ray dashboard URL (e.g., http://localhost:8265)
        ray_serve_address: Ray Serve address for deployment
        app_server: Kodosumi app server URL
        auth_cookies: Authentication cookies for internal API calls
        force: Force boot even if lock is held
        owner: Lock owner identifier
        mock: If True, use mocked processes for testing
    """

    # Acquire lock
    if not boot_lock.acquire(owner=owner, force=force):
        msg = BootMessage(
            step=BootStep.ERROR,
            msg_type=MessageType.ERROR,
            message="Boot already in progress. Use force=true to override."
        )
        await boot_lock.add_message(msg)
        yield msg
        return

    progress = BootProgress()
    start_time = time.time()

    # Format start time for display
    start_dt = datetime.fromtimestamp(start_time)
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Emit start time message
    msg = BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.INFO,
        message=f"Boot started at {start_str}"
    )
    await boot_lock.add_message(msg)
    yield msg

    try:
        if mock:
            # Use mocked processes
            async for msg in _mock_boot_process(progress):
                await boot_lock.add_message(msg)
                yield msg
                if msg.msg_type == MessageType.ERROR:
                    return
        else:
            # Real implementation (TODO)
            async for msg in _real_boot_process(
                ray_dashboard, ray_serve_address, app_server, auth_cookies, progress
            ):
                await boot_lock.add_message(msg)
                yield msg
                if msg.msg_type == MessageType.ERROR:
                    return

        # Calculate runtime
        end_time = time.time()
        end_dt = datetime.fromtimestamp(end_time)
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        runtime_secs = end_time - start_time
        runtime_str = f"{runtime_secs:.1f}s"

        # Complete
        progress.current_step = progress.total_steps
        msg = BootMessage(
            step=BootStep.COMPLETE,
            msg_type=MessageType.STEP_END,
            message=f"Boot completed at {end_str} (runtime: {runtime_str})",
            progress=progress
        )
        await boot_lock.add_message(msg)
        yield msg

    except Exception as e:
        # Calculate runtime even on failure
        end_time = time.time()
        end_dt = datetime.fromtimestamp(end_time)
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        runtime_secs = end_time - start_time
        runtime_str = f"{runtime_secs:.1f}s"

        msg = BootMessage(
            step=BootStep.ERROR,
            msg_type=MessageType.ERROR,
            message=f"Boot failed at {end_str} (runtime: {runtime_str}): {str(e)}"
        )
        await boot_lock.add_message(msg)
        yield msg
    finally:
        boot_lock.release()


async def _mock_boot_process(progress: BootProgress) -> AsyncGenerator[BootMessage, None]:
    """
    Mocked boot process for testing UI and streaming.
    Simulates all steps with realistic delays (~15 seconds total).
    """
    await db.init_database()
    exposes = await db.get_all_exposes()

    # Filter to enabled exposes with bootstrap
    enabled_exposes = [
        e for e in exposes
        if e.get("enabled") and e.get("bootstrap") and e.get("bootstrap").strip()
    ]

    # If no real exposes, create mock ones for demo
    if not enabled_exposes:
        mock_exposes = [
            {"name": "demo-agent-1", "enabled": True, "bootstrap": "demo"},
            {"name": "demo-agent-2", "enabled": True, "bootstrap": "demo"},
            {"name": "demo-agent-3", "enabled": True, "bootstrap": "demo"},
        ]
        enabled_exposes = mock_exposes
        exposes = mock_exposes

    # =========================================================================
    # Step A: Deploy (~3 seconds)
    # =========================================================================
    progress.current_step = 0
    progress.step_name = "Deploy"
    progress.activities_total = len(enabled_exposes) + 2
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.STEP_START,
        message="Starting Ray Serve deployment",
        progress=progress
    )

    # Activity: Load global config
    await asyncio.sleep(0.8)
    progress.activities_done += 1
    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.ACTIVITY,
        message="Loading global configuration",
        target="serve_config.yaml",
        result="OK",
        progress=progress
    )

    # Activity: Prepare each expose
    for expose in exposes:
        await asyncio.sleep(0.4)
        name = expose["name"]

        if not expose.get("enabled"):
            yield BootMessage(
                step=BootStep.DEPLOY,
                msg_type=MessageType.INFO,
                message="Skipping (disabled)",
                target=name,
                progress=progress
            )
            continue

        bootstrap = expose.get("bootstrap")
        if not bootstrap or not bootstrap.strip():
            yield BootMessage(
                step=BootStep.DEPLOY,
                msg_type=MessageType.INFO,
                message="Skipping (no bootstrap)",
                target=name,
                progress=progress
            )
            continue

        progress.activities_done += 1
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ACTIVITY,
            message="Prepared deployment config",
            target=name,
            result=f"route=/{name}",
            progress=progress
        )

    # Activity: Run serve deploy
    await asyncio.sleep(1.5)
    progress.activities_done += 1
    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.RESULT,
        message="serve deploy command",
        result="success (mocked)",
        progress=progress
    )

    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.STEP_END,
        message="Deployment initiated",
        progress=progress
    )

    # =========================================================================
    # Step B: Health Check (~3 seconds)
    # =========================================================================
    progress.current_step = 1
    progress.step_name = "Health Check"
    progress.activities_total = len(enabled_exposes)
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.STEP_START,
        message=f"Checking application health (timeout: {BOOT_HEALTH_TIMEOUT}s)",
        progress=progress
    )

    for expose in enabled_exposes:
        await asyncio.sleep(1.0)  # Simulate polling delay
        name = expose["name"]
        progress.activities_done += 1

        # Mock: always RUNNING for demo
        status = "RUNNING"
        # Only update real exposes in DB
        if "demo-agent" not in name:
            await db.update_expose_state(name, status, time.time())

        yield BootMessage(
            step=BootStep.HEALTH,
            msg_type=MessageType.RESULT,
            message="Application status",
            target=name,
            result=status,
            progress=progress
        )

    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.STEP_END,
        message=f"Health check complete ({len(enabled_exposes)} applications)",
        progress=progress
    )

    # =========================================================================
    # Step C: Register Flows (~2 seconds)
    # =========================================================================
    progress.current_step = 2
    progress.step_name = "Register Flows"
    progress.activities_total = 1
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.STEP_START,
        message="Registering flows via REST API",
        progress=progress
    )

    await asyncio.sleep(2.0)
    progress.activities_done = 1
    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.ACTIVITY,
        message="POST /flow/register",
        target="/-/routes",
        result="201 Created (mocked)",
        progress=progress
    )

    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.STEP_END,
        message="Flow registration complete",
        progress=progress
    )

    # =========================================================================
    # Step D: Retrieve Flows (~4 seconds)
    # =========================================================================
    progress.current_step = 3
    progress.step_name = "Retrieve Flows"
    # Mock some flows per expose
    mock_flows_per_expose = 2
    progress.activities_total = len(enabled_exposes) * mock_flows_per_expose
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.STEP_START,
        message="Retrieving flow details",
        progress=progress
    )

    await asyncio.sleep(0.5)
    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.ACTIVITY,
        message="GET /flow",
        result=f"{len(enabled_exposes) * mock_flows_per_expose} flows (mocked)",
        progress=progress
    )

    for expose in enabled_exposes:
        name = expose["name"]
        for i in range(mock_flows_per_expose):
            await asyncio.sleep(0.5)
            progress.activities_done += 1

            yield BootMessage(
                step=BootStep.RETRIEVE,
                msg_type=MessageType.RESULT,
                message="HEAD request",
                target=f"{name}/flow{i+1}",
                result="200 OK (alive)",
                progress=progress
            )

    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.STEP_END,
        message=f"Retrieved {progress.activities_done} flow endpoints",
        progress=progress
    )

    # =========================================================================
    # Step E: Update Meta (~3 seconds)
    # =========================================================================
    progress.current_step = 4
    progress.step_name = "Update Meta"
    progress.activities_total = len(enabled_exposes)
    progress.activities_done = 0

    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.STEP_START,
        message="Updating expose metadata",
        progress=progress
    )

    for expose in enabled_exposes:
        await asyncio.sleep(1.0)
        name = expose["name"]
        progress.activities_done += 1

        yield BootMessage(
            step=BootStep.UPDATE,
            msg_type=MessageType.ACTIVITY,
            message="Updated meta entries",
            target=name,
            result=f"{mock_flows_per_expose} flows",
            progress=progress
        )

    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.STEP_END,
        message="Metadata update complete",
        progress=progress
    )


async def _real_boot_process(
    ray_dashboard: str,
    ray_serve_address: str,
    app_server: str,
    auth_cookies: Optional[Dict[str, str]],
    progress: BootProgress
) -> AsyncGenerator[BootMessage, None]:
    """
    Real boot process implementation.

    Executes steps A through E:
    - A: Deploy (serve deploy)
    - B: Health Check (poll Ray API) - TODO
    - C: Register Flows (OpenAPI discovery) - TODO
    - D: Retrieve Flows (HEAD requests) - TODO
    - E: Update Meta (database update) - TODO
    """
    deployed_names: List[str] = []

    # =========================================================================
    # Step A: Deploy
    # =========================================================================
    async for msg in _step_deploy(progress):
        yield msg
        if msg.msg_type == MessageType.ERROR:
            return
        # Capture deployed names for subsequent steps
        if msg.data and "deployed_names" in msg.data:
            deployed_names = msg.data["deployed_names"]

    # Check if we have any deployed apps to continue with
    if not deployed_names:
        yield BootMessage(
            step=BootStep.COMPLETE,
            msg_type=MessageType.INFO,
            message="No applications deployed, skipping remaining steps",
            progress=progress
        )
        return

    # =========================================================================
    # Step B: Health Check
    # =========================================================================
    final_statuses: Dict[str, dict] = {}
    async for msg in _step_health_check(ray_dashboard, deployed_names, progress):
        yield msg
        if msg.msg_type == MessageType.ERROR:
            return
        # Capture final statuses for subsequent steps
        if msg.data and "final_statuses" in msg.data:
            final_statuses = msg.data["final_statuses"]

    # Filter to only running apps for subsequent steps
    running_apps = [name for name, info in final_statuses.items() if info.get("status") == "RUNNING"]
    if not running_apps:
        yield BootMessage(
            step=BootStep.HEALTH,
            msg_type=MessageType.WARNING,
            message="No applications running, skipping remaining steps",
            progress=progress
        )
        return

    # =========================================================================
    # Step C: Register Flows
    # =========================================================================
    discovered_flows: List[DiscoveredFlow] = []
    async for msg in _step_register_flows(
        ray_serve_address, app_server, running_apps, auth_cookies, progress
    ):
        yield msg
        if msg.msg_type == MessageType.ERROR:
            return
        # Capture discovered flows for subsequent steps
        if msg.data and "discovered_flows" in msg.data:
            discovered_flows = msg.data["discovered_flows"]

    # =========================================================================
    # Step D: Retrieve Flows
    # =========================================================================
    flow_statuses: Dict[str, List[FlowStatus]] = {}
    async for msg in _step_retrieve_flows(ray_serve_address, discovered_flows, progress):
        yield msg
        if msg.msg_type == MessageType.ERROR:
            return
        # Capture flow statuses for Step E
        if msg.data and "flow_statuses" in msg.data:
            flow_statuses = msg.data["flow_statuses"]

    # =========================================================================
    # Step E: Update Meta
    # =========================================================================
    async for msg in _step_update_meta(flow_statuses, progress):
        yield msg
        if msg.msg_type == MessageType.ERROR:
            return


async def run_shutdown() -> AsyncGenerator[BootMessage, None]:
    """
    Execute serve shutdown with streaming output.
    """
    yield BootMessage(
        step=BootStep.DEPLOY,
        msg_type=MessageType.STEP_START,
        message="Shutting down Ray Serve"
    )

    try:
        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.ACTIVITY,
            message="Running serve shutdown -y",
            target="serve"
        )

        process = await asyncio.create_subprocess_exec(
            "serve", "shutdown", "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            yield BootMessage(
                step=BootStep.ERROR,
                msg_type=MessageType.ERROR,
                message=f"Shutdown failed: {error_msg}"
            )
            return

        yield BootMessage(
            step=BootStep.DEPLOY,
            msg_type=MessageType.RESULT,
            message="serve shutdown",
            result="success"
        )

        # Update all exposes to DEAD
        yield BootMessage(
            step=BootStep.UPDATE,
            msg_type=MessageType.STEP_START,
            message="Updating expose states"
        )

        await db.init_database()
        exposes = await db.get_all_exposes()
        for expose in exposes:
            name = expose["name"]
            await db.update_expose_state(name, "DEAD", time.time())
            yield BootMessage(
                step=BootStep.UPDATE,
                msg_type=MessageType.ACTIVITY,
                message="Set state to DEAD",
                target=name
            )

        yield BootMessage(
            step=BootStep.COMPLETE,
            msg_type=MessageType.STEP_END,
            message="Shutdown complete"
        )

    except Exception as e:
        yield BootMessage(
            step=BootStep.ERROR,
            msg_type=MessageType.ERROR,
            message=f"Shutdown failed: {e}"
        )
