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
    # Step B: Health Check - TODO
    # =========================================================================
    progress.current_step = 1
    progress.step_name = "Health Check"
    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.STEP_START,
        message=f"Waiting for deployments to complete (timeout: {BOOT_HEALTH_TIMEOUT}s)",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.WARNING,
        message="Health check not yet implemented - skipping",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.HEALTH,
        msg_type=MessageType.STEP_END,
        message="Health check skipped (not implemented)",
        progress=progress
    )

    # =========================================================================
    # Step C: Register Flows - TODO
    # =========================================================================
    progress.current_step = 2
    progress.step_name = "Register Flows"
    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.STEP_START,
        message="Discovering flow endpoints",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.WARNING,
        message="Flow registration not yet implemented - skipping",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.REGISTER,
        msg_type=MessageType.STEP_END,
        message="Flow registration skipped (not implemented)",
        progress=progress
    )

    # =========================================================================
    # Step D: Retrieve Flows - TODO
    # =========================================================================
    progress.current_step = 3
    progress.step_name = "Retrieve Flows"
    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.STEP_START,
        message="Testing flow endpoints",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.WARNING,
        message="Flow retrieval not yet implemented - skipping",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.RETRIEVE,
        msg_type=MessageType.STEP_END,
        message="Flow retrieval skipped (not implemented)",
        progress=progress
    )

    # =========================================================================
    # Step E: Update Meta - TODO
    # =========================================================================
    progress.current_step = 4
    progress.step_name = "Update Meta"
    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.STEP_START,
        message="Updating expose metadata",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.WARNING,
        message="Meta update not yet implemented - skipping",
        progress=progress
    )
    yield BootMessage(
        step=BootStep.UPDATE,
        msg_type=MessageType.STEP_END,
        message="Meta update skipped (not implemented)",
        progress=progress
    )


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
