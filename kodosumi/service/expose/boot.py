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
BOOT_HEALTH_TIMEOUT = 60  # seconds to wait for health checks
BOOT_POLL_INTERVAL = 2    # seconds between health check polls

# Total number of main steps for progress tracking
BOOT_TOTAL_STEPS = 5  # A, B, C, D, E


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

        # Complete
        progress.current_step = progress.total_steps
        msg = BootMessage(
            step=BootStep.COMPLETE,
            msg_type=MessageType.STEP_END,
            message="Boot process completed successfully",
            progress=progress
        )
        await boot_lock.add_message(msg)
        yield msg

    except Exception as e:
        msg = BootMessage(
            step=BootStep.ERROR,
            msg_type=MessageType.ERROR,
            message=f"Boot failed: {str(e)}"
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
    TODO: Implement when ready to connect to actual Ray Serve.
    """
    yield BootMessage(
        step=BootStep.ERROR,
        msg_type=MessageType.ERROR,
        message="Real boot process not yet implemented. Use mock=True."
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
