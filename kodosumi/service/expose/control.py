"""
Controller for expose API endpoints.

All endpoints require operator role authentication.
"""

import asyncio
import time
from pathlib import Path
from typing import List, Optional

import yaml
import litestar
from litestar import Request, delete, get, post
from litestar.datastructures import State
from litestar.exceptions import NotFoundException, ValidationException
from litestar.response import Redirect, Stream, Template

from kodosumi.helper import HTTPXClient
from kodosumi.service.expose.boot import (
    BootMessage,
    BootStep,
    boot_lock,
    run_boot_process,
    run_shutdown,
    start_boot_background,
)
from kodosumi.service.jwt import operator_guard
from kodosumi.service.expose import db
from kodosumi.service.expose.models import (
    ExposeCreate,
    ExposeMeta,
    ExposeResponse,
    meta_to_yaml,
)

# Default serve config
RAY_SERVE_CONFIG = "./data/serve_config.yaml"

DEFAULT_SERVE_CONFIG = """# Kodosumi Ray Serve Configuration
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


async def get_ray_serve_status(ray_dashboard: str) -> dict:
    """
    Query Ray Serve API for application statuses.
    Returns dict mapping route_prefix to status.
    """
    url = f"{ray_dashboard}/api/serve/applications/"
    try:
        async with HTTPXClient() as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                js = resp.json()
                apps = js.get("applications", {})
                # Map route_prefix to status
                result = {}
                for app_name, app_info in apps.items():
                    route_prefix = app_info.get("route_prefix", f"/{app_name}")
                    result[route_prefix] = app_info.get("status", "UNKNOWN")
                return result
    except Exception:
        pass
    return {}


def ensure_serve_config():
    """Ensure serve_config.yaml exists with defaults."""
    config_path = Path(RAY_SERVE_CONFIG)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(DEFAULT_SERVE_CONFIG)


class ExposeControl(litestar.Controller):
    """Controller for expose management endpoints."""

    path = "/expose"
    tags = ["Expose"]
    guards = [operator_guard]

    @get(
        "",
        summary="List all expose items",
        description="Retrieve all expose items from the database.",
        operation_id="expose_list",
    )
    async def list_exposes(self, state: State) -> List[ExposeResponse]:
        """Get all expose items."""
        await db.init_database()
        rows = await db.get_all_exposes()
        return [ExposeResponse.from_db_row(row) for row in rows]

    @get(
        "/{name:str}",
        summary="Get expose item",
        description="Retrieve a single expose item by name.",
        operation_id="expose_get",
    )
    async def get_expose(self, name: str, state: State) -> ExposeResponse:
        """Get a single expose item by name."""
        await db.init_database()
        row = await db.get_expose(name)
        if not row:
            raise NotFoundException(detail=f"Expose '{name}' not found")
        return ExposeResponse.from_db_row(row)

    @post(
        "",
        summary="Create or update expose item",
        description="Create a new expose item or update an existing one.",
        operation_id="expose_upsert",
    )
    async def upsert_expose(
        self, data: ExposeCreate, state: State
    ) -> ExposeResponse:
        """Create or update an expose item."""
        await db.init_database()
        now = time.time()

        # Determine state
        if not data.bootstrap or not data.bootstrap.strip():
            # No bootstrap config = DRAFT
            state_value = "DRAFT"
        elif not data.enabled:
            # Disabled = DEAD
            state_value = "DEAD"
        else:
            # Query Ray Serve API for status
            ray_dashboard = state["settings"].RAY_DASHBOARD
            statuses = await get_ray_serve_status(ray_dashboard)
            route_prefix = f"/{data.name}"
            if route_prefix in statuses:
                state_value = statuses[route_prefix]
            else:
                state_value = "DEAD"

        # Convert meta to YAML for storage
        meta_yaml = meta_to_yaml(data.meta)

        # Upsert
        row = await db.upsert_expose(
            name=data.name,
            display=data.display,
            network=data.network,
            enabled=data.enabled,
            state=state_value,
            heartbeat=now,
            bootstrap=data.bootstrap,
            meta=meta_yaml,
        )

        return ExposeResponse.from_db_row(row)

    @delete(
        "/{name:str}",
        summary="Delete expose item",
        description="Permanently delete an expose item. This action cannot be undone.",
        operation_id="expose_delete",
    )
    async def delete_expose(self, name: str, state: State) -> None:
        """Delete an expose item."""
        await db.init_database()
        deleted = await db.delete_expose(name)
        if not deleted:
            raise NotFoundException(detail=f"Expose '{name}' not found")


class ExposeUIControl(litestar.Controller):
    """Controller for expose UI pages."""

    path = "/admin/expose"
    tags = ["Expose UI"]
    guards = [operator_guard]

    @get(
        "/",
        summary="Expose main page",
        description="Display the main expose management page.",
        operation_id="expose_main_page",
    )
    async def main_page(self, state: State) -> Template | Redirect:
        """Render the main expose page with card listing."""
        # If boot is in progress, redirect operator to boot screen
        if boot_lock.is_locked:
            return Redirect(path="/admin/expose/boot")

        await db.init_database()
        rows = await db.get_all_exposes()
        items = [ExposeResponse.from_db_row(row) for row in rows]

        # Calculate active/total flows for each item
        for item in items:
            if item.meta:
                total = len(item.meta)
                alive = sum(1 for m in item.meta if m.state == "alive")
                item._flow_stats = f"{alive}/{total}"
                # Check for stale indicators
                if item.enabled:
                    item._stale = any(m.state != "alive" for m in item.meta)
                else:
                    item._stale = any(m.state == "alive" for m in item.meta)
            else:
                item._flow_stats = "0/0"
                item._stale = False

        return Template("expose/main.html", context={"items": items})

    @get(
        "/new",
        summary="Create expose page",
        description="Display form for creating a new expose item.",
        operation_id="expose_new_page",
    )
    async def new_page(self, state: State) -> Template:
        """Render the create expose form."""
        return Template("expose/edit.html", context={
            "item": None,
            "is_new": True
        })

    @get(
        "/edit/{name:str}",
        summary="Edit expose page",
        description="Display form for editing an expose item.",
        operation_id="expose_edit_page",
    )
    async def edit_page(self, name: str, state: State) -> Template:
        """Render the edit expose form."""
        await db.init_database()
        row = await db.get_expose(name)
        if not row:
            raise NotFoundException(detail=f"Expose '{name}' not found")

        item = ExposeResponse.from_db_row(row)
        return Template("expose/edit.html", context={
            "item": item,
            "is_new": False
        })

    @get(
        "/globals",
        summary="Global config page",
        description="Display the global serve configuration editor.",
        operation_id="expose_globals_page",
    )
    async def globals_page(self, state: State) -> Template:
        """Render the global config editor."""
        ensure_serve_config()
        config_path = Path(RAY_SERVE_CONFIG)
        config_content = config_path.read_text() if config_path.exists() else ""
        return Template("expose/globals.html", context={
            "config": config_content,
            "config_path": RAY_SERVE_CONFIG
        })

    @post(
        "/globals",
        summary="Save global config",
        description="Save the global serve configuration.",
        operation_id="expose_globals_save",
    )
    async def save_globals(self, request: Request, state: State) -> Template | Redirect:
        """Save global config and redirect."""
        form_data = await request.form()
        config_content = form_data.get("config", "")

        # Validate YAML
        try:
            yaml.safe_load(config_content)
        except yaml.YAMLError as e:
            return Template("expose/globals.html", context={
                "config": config_content,
                "config_path": RAY_SERVE_CONFIG,
                "error": f"Invalid YAML: {e}"
            })

        # Save
        config_path = Path(RAY_SERVE_CONFIG)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_content)

        return Redirect(path="/admin/expose")


class BootControl(litestar.Controller):
    """Controller for boot/shutdown endpoints."""

    path = "/boot"
    tags = ["Boot"]
    guards = [operator_guard]

    @post(
        "",
        summary="Boot all enabled exposures",
        description="Start Ray Serve deployment for all enabled exposures. Returns streaming text output.",
        operation_id="boot_start",
    )
    async def boot(self, request: Request, state: State, force: bool = False) -> Stream:
        """
        Execute boot process with streaming output.

        The boot runs as a background task so it continues even if
        the client disconnects. The initiator subscribes to the
        message stream just like late joiners.

        Args:
            force: Override existing boot lock if True
        """
        # Get settings
        ray_dashboard = state["settings"].RAY_DASHBOARD
        ray_serve_address = state["settings"].RAY_SERVER
        app_server = state["settings"].APP_SERVER

        # Get auth cookies from request
        auth_cookies = dict(request.cookies)

        # Start boot as background task
        started = await start_boot_background(
            ray_dashboard=ray_dashboard,
            ray_serve_address=ray_serve_address,
            app_server=app_server,
            auth_cookies=auth_cookies,
            force=force,
            owner=request.user or "operator"
        )

        if not started and not force:
            # Boot already in progress, return error
            async def already_running():
                yield "[ERROR] Boot already in progress. Use force=true to override.\n"
            return Stream(already_running(), media_type="text/plain")

        # Subscribe to message stream (same as late joiner)
        queue = boot_lock.subscribe()

        async def generate():
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                        yield f"{msg}\n"
                        if msg.step in (BootStep.COMPLETE, BootStep.ERROR):
                            break
                    except asyncio.TimeoutError:
                        if not boot_lock.is_locked and queue.empty():
                            break
                        continue
            finally:
                boot_lock.unsubscribe(queue)

        return Stream(generate(), media_type="text/plain")

    @get(
        "",
        summary="Get boot status",
        description="Get current boot status and messages if boot is in progress.",
        operation_id="boot_status",
    )
    async def boot_status(self, state: State) -> dict:
        """Get current boot lock status."""
        return {
            "locked": boot_lock.is_locked,
            "lock_time": boot_lock.lock_time,
            "messages": [str(m) for m in boot_lock.messages]
        }

    @get(
        "/stream",
        summary="Stream boot messages",
        description="Subscribe to boot message stream (for operators joining an in-progress boot).",
        operation_id="boot_stream",
    )
    async def boot_stream(self, state: State) -> Stream:
        """Stream boot messages to client."""
        if not boot_lock.is_locked:
            async def no_boot():
                yield "No boot in progress\n"
            return Stream(no_boot(), media_type="text/plain")

        queue = boot_lock.subscribe()

        async def generate():
            try:
                while True:
                    try:
                        # Short timeout to check for new messages
                        msg = await asyncio.wait_for(queue.get(), timeout=0.5)
                        yield f"{msg}\n"
                        if msg.step in (BootStep.COMPLETE, BootStep.ERROR):
                            break
                    except asyncio.TimeoutError:
                        # If lock released and queue empty, we're done
                        if not boot_lock.is_locked and queue.empty():
                            break
                        continue
            finally:
                boot_lock.unsubscribe(queue)

        return Stream(generate(), media_type="text/plain")

    @delete(
        "",
        summary="Shutdown Ray Serve",
        description="Execute serve shutdown command.",
        operation_id="boot_shutdown",
        status_code=200,
    )
    async def shutdown(self, state: State) -> Stream:
        """Execute shutdown with streaming output."""
        async def generate():
            async for msg in run_shutdown():
                yield f"{msg}\n"

        return Stream(generate(), media_type="text/plain")


class BootUIControl(litestar.Controller):
    """Controller for boot UI pages."""

    path = "/admin/expose/boot"
    tags = ["Boot UI"]
    guards = [operator_guard]

    @get(
        "",
        summary="Boot screen",
        description="Display the boot console screen.",
        operation_id="boot_page",
    )
    async def boot_page(self, state: State) -> Template:
        """Render the boot screen."""
        return Template("expose/boot.html", context={
            "is_locked": boot_lock.is_locked,
            "messages": [str(m) for m in boot_lock.messages]
        })

    @get(
        "/shutdown",
        summary="Shutdown confirmation screen",
        description="Display shutdown confirmation dialog.",
        operation_id="shutdown_page",
    )
    async def shutdown_page(self, state: State) -> Template:
        """Render the shutdown confirmation screen."""
        return Template("expose/shutdown.html", context={})


class MaintenanceControl(litestar.Controller):
    """
    Controller for maintenance page.

    This is shown to regular users when the system is undergoing
    boot/deployment. No authentication required.
    """

    path = "/maintenance"
    tags = ["Maintenance"]
    # No guards - accessible to everyone

    @get(
        "",
        summary="Maintenance page",
        description="Display maintenance page during system boot.",
        operation_id="maintenance_page",
    )
    async def maintenance_page(self, state: State) -> Template | Redirect:
        """
        Render the maintenance page.

        If not in maintenance mode (boot not in progress), redirect to home.
        """
        if not boot_lock.is_locked:
            # Not in maintenance, redirect to home
            return Redirect(path="/")

        return Template("expose/maintenance.html", context={
            "is_booting": True
        })
