"""
Sumi Protocol Controller - MIP-002/MIP-003 compliant endpoints.

Provides discovery, availability, and job management for external systems.
"""

import asyncio
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from litestar import Controller, get, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException

from kodosumi import dtypes
from kodosumi.const import (DB_FILE, EVENT_META, SLEEP, STATUS_AWAITING,
                            STATUS_END, STATUS_ERROR, STATUS_RUNNING)
from kodosumi.helper import HTTPXClient, now
from kodosumi.service.expose import db
from kodosumi.service.expose.models import ExposeMeta
from kodosumi.service.proxy import LockNotFound, find_lock
from kodosumi.service.sumi.hash import create_input_hash
from kodosumi.service.sumi.models import (AgentPricing, AuthorInfo,
                                          AvailabilityResponse, CapabilityInfo,
                                          ErrorResponse, ExampleOutput,
                                          FixedPricing, InputSchemaResponse,
                                          JobStatusResponse, LegalInfo,
                                          LockSchemaResponse,
                                          ProvideInputRequest,
                                          ProvideInputResponse,
                                          StartJobRequest, StartJobResponse,
                                          SumiFlowItem, SumiFlowListResponse,
                                          SumiServiceDetail)
from kodosumi.service.sumi.schema import (convert_model_to_schema,
                                          create_empty_schema)

# User identifier for jobs started via Sumi protocol
SUMI_USER = "_sumi_"

# Pagination limits
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 10


def _parse_meta_data(data_yaml: Optional[str]) -> dict:
    """Parse the meta.data YAML field into a dict."""
    if not data_yaml:
        return {}
    try:
        return yaml.safe_load(data_yaml) or {}
    except yaml.YAMLError:
        return {}


# Regex pattern for valid path parameter names
_VALID_NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9\-_]*$')


def _validate_path_param(name: str, param_name: str = "name") -> str:
    """
    Validate a path parameter for URL safety.

    Rules:
    - Alphanumeric characters only (a-z, 0-9)
    - Hyphens (-) and underscores (_) allowed (not at start)
    - Must be lowercase
    - No whitespace or special characters

    Args:
        name: The path parameter value to validate
        param_name: Name of the parameter (for error messages)

    Returns:
        The validated name (lowercase)

    Raises:
        HTTPException: If validation fails (400 Bad Request)
    """
    if not name:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name}: cannot be empty"
        )

    # Convert to lowercase
    name_lower = name.lower()

    if not _VALID_NAME_PATTERN.match(name_lower):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name} '{name}': must contain only lowercase "
                   f"alphanumeric characters, hyphens, and underscores"
        )

    return name_lower


def _sanitize_name(name: str) -> str:
    """
    Sanitize name for use as URL slug identifier.

    Pruning rules:
    - Only alphanumeric characters, hyphens (-), and underscores (_) allowed
    - Whitespace replaced with hyphens
    - All other characters removed
    - Lowercase for consistency
    """
    # Replace whitespace with hyphens
    result = re.sub(r'\s+', '-', name)
    # Remove any character that is not alphanumeric, hyphen, or underscore
    result = re.sub(r'[^a-zA-Z0-9\-_]', '', result)
    # Convert to lowercase for URL consistency
    result = result.lower()
    # Remove consecutive hyphens
    result = re.sub(r'-+', '-', result)
    # Strip leading/trailing hyphens
    result = result.strip('-')
    return result or 'unnamed'


def _url_to_name(meta_url: str) -> str:
    """
    Generate default meta.name from meta.url (just the endpoint).

    The URL base_path has format: /{route_prefix}/{endpoint}
    Since route_prefix == expose.name by design, default name is just {endpoint}.

    Example: "/my-agent/process" -> "process"
    Example: "/single" -> "single"
    Example: "/deep/nested/path" -> "path"
    """
    parts = meta_url.strip("/").split("/")
    if not parts or (len(parts) == 1 and not parts[0]):
        return "root"
    # Use only the last element (endpoint), since route_prefix == expose.name
    endpoint = parts[-1]
    return _sanitize_name(endpoint)


def _build_sumi_url(app_server: str, parent: str, meta_name: str) -> str:
    """Build the Sumi protocol endpoint URL: /sumi/{parent}/{name}."""
    app_server = app_server.rstrip("/")
    return f"{app_server}/sumi/{parent}/{meta_name}"


def _build_base_url(app_server: str, meta_url: str) -> str:
    """Build full base URL (proxy endpoint) from server and meta endpoint path."""
    app_server = app_server.rstrip("/")
    if meta_url.startswith("/"):
        return f"{app_server}/-{meta_url}"
    return f"{app_server}/-/{meta_url}"


def _parse_agent_pricing(data: dict) -> List[AgentPricing]:
    """Parse agentPricing from meta data dict."""
    pricing_list = data.get("agentPricing", [])
    if not pricing_list:
        # Default pricing if not specified
        return [AgentPricing(
            pricingType="Fixed",
            fixedPricing=[FixedPricing(amount="0", unit="lovelace")]
        )]

    result = []
    for p in pricing_list:
        fixed_pricing = []
        for fp in p.get("fixedPricing", []):
            fixed_pricing.append(FixedPricing(
                amount=str(fp.get("amount", "0")),
                unit=fp.get("unit", "lovelace")
            ))
        result.append(AgentPricing(
            pricingType=p.get("pricingType", "Fixed"),
            fixedPricing=fixed_pricing
        ))
    return result


def _parse_author(data: dict) -> Optional[AuthorInfo]:
    """Parse author from meta data dict."""
    author_data = data.get("author")
    if not author_data:
        return None
    if isinstance(author_data, dict):
        return AuthorInfo(
            name=author_data.get("name"),
            contact_email=author_data.get("contact_email"),
            contact_other=author_data.get("contact_other"),
            organization=author_data.get("organization"),
        )
    return None


def _parse_capability(data: dict) -> Optional[CapabilityInfo]:
    """Parse capability from meta data dict."""
    cap_data = data.get("capability")
    if not cap_data or not isinstance(cap_data, dict):
        return None
    name = cap_data.get("name")
    version = cap_data.get("version")
    if name and version:
        return CapabilityInfo(name=name, version=version)
    return None


def _parse_legal(data: dict) -> Optional[LegalInfo]:
    """Parse legal from meta data dict."""
    legal_data = data.get("legal")
    if not legal_data or not isinstance(legal_data, dict):
        return None
    return LegalInfo(
        privacy_policy=legal_data.get("privacy_policy"),
        terms=legal_data.get("terms"),
        other=legal_data.get("other"),
    )


def _parse_example_output(data: dict) -> Optional[List[ExampleOutput]]:
    """Parse example_output from meta data dict."""
    examples = data.get("example_output", [])
    if not examples:
        return None
    result = []
    for ex in examples:
        if isinstance(ex, dict) and ex.get("name") and ex.get("mime_type") and ex.get("url"):
            result.append(ExampleOutput(
                name=ex["name"],
                mime_type=ex["mime_type"],
                url=ex["url"],
            ))
    return result if result else None


def _meta_to_flow_item(
    expose_name: str,
    expose_network: str,
    meta: ExposeMeta,
    app_server: str,
) -> SumiFlowItem:
    """Convert ExposeMeta to SumiFlowItem."""
    data = _parse_meta_data(meta.data)
    # Technical identifier derived from URL endpoint
    meta_name = _get_meta_name(meta)
    # id is {parent}/{name} - unique identifier for the flow
    flow_id = f"{expose_name}/{meta_name}"

    return SumiFlowItem(
        id=flow_id,
        parent=expose_name,
        name=meta_name,
        display=data.get("display") or meta_name,  # Display from data or endpoint name
        api_url=_build_sumi_url(app_server, expose_name, meta_name),
        base_url=_build_base_url(app_server, meta.url),
        tags=data.get("tags", ["untagged"]) or ["untagged"],
        agentPricing=_parse_agent_pricing(data),
        metadata_version=1,
        description=data.get("description"),
        image=data.get("image"),
        example_output=_parse_example_output(data),
        network=expose_network or "Preprod",
        state=meta.state or "dead",
        author=_parse_author(data),
        capability=_parse_capability(data),
        legal=_parse_legal(data),
    )


def _meta_to_service_detail(
    expose_name: str,
    expose_network: str,
    meta: ExposeMeta,
    app_server: str,
) -> SumiServiceDetail:
    """Convert ExposeMeta to full SumiServiceDetail."""
    data = _parse_meta_data(meta.data)
    # Technical identifier derived from URL endpoint
    meta_name = _get_meta_name(meta)
    # id is {parent}/{name} - unique identifier for the flow
    flow_id = f"{expose_name}/{meta_name}"

    return SumiServiceDetail(
        id=flow_id,
        parent=expose_name,
        name=meta_name,
        display=data.get("display") or meta_name,  # Display from data or endpoint name
        api_url=_build_sumi_url(app_server, expose_name, meta_name),
        base_url=_build_base_url(app_server, meta.url),
        tags=data.get("tags", ["untagged"]) or ["untagged"],
        agentPricing=_parse_agent_pricing(data),
        metadata_version=1,
        description=data.get("description"),
        image=data.get("image"),
        example_output=_parse_example_output(data),
        author=_parse_author(data),
        capability=_parse_capability(data),
        legal=_parse_legal(data),
        network=expose_network or "Preprod",
        state=meta.state or "dead",
        url=meta.url,
    )


async def _get_all_alive_flows(
    app_server: str,
    db_path: Optional[str] = None,
) -> List[tuple]:
    """
    Get all alive flows from all enabled, running exposes.

    Returns list of (expose_name, expose_network, ExposeMeta, app_server) tuples.
    """
    await db.init_database(db_path)
    rows = await db.get_all_exposes(db_path)

    result = []
    for row in rows:
        # Filter: enabled and RUNNING
        if not row.get("enabled"):
            continue
        if row.get("state") != "RUNNING":
            continue

        expose_name = row["name"]
        expose_network = row.get("network", "Preprod")

        # Parse meta entries
        meta_yaml = row.get("meta")
        if not meta_yaml:
            continue

        try:
            meta_list = yaml.safe_load(meta_yaml)
            if not meta_list:
                continue
            for m in meta_list:
                meta = ExposeMeta(**m)
                # Filter: only alive and enabled entries
                if meta.state == "alive" and meta.enabled:
                    result.append((expose_name, expose_network, meta, app_server))
        except (yaml.YAMLError, TypeError, ValueError):
            continue

    return result


async def _get_expose_alive_flows(
    expose_name: str,
    app_server: str,
    db_path: Optional[str] = None,
) -> List[tuple]:
    """
    Get alive flows from a specific expose.

    Returns list of (expose_name, expose_network, ExposeMeta, app_server) tuples.
    Raises NotFoundException if expose not found or not available.
    """
    await db.init_database(db_path)
    row = await db.get_expose(expose_name, db_path)

    if not row:
        raise NotFoundException(detail=f"Expose '{expose_name}' not found")

    if not row.get("enabled"):
        raise NotFoundException(detail=f"Expose '{expose_name}' is not enabled")

    if row.get("state") != "RUNNING":
        raise NotFoundException(detail=f"Expose '{expose_name}' is not running")

    expose_network = row.get("network", "Preprod")
    meta_yaml = row.get("meta")

    if not meta_yaml:
        return []

    result = []
    try:
        meta_list = yaml.safe_load(meta_yaml)
        if meta_list:
            for m in meta_list:
                meta = ExposeMeta(**m)
                # Filter: only alive and enabled entries
                if meta.state == "alive" and meta.enabled:
                    result.append((expose_name, expose_network, meta, app_server))
    except (yaml.YAMLError, TypeError, ValueError):
        pass

    return result


def _get_meta_name(meta: ExposeMeta) -> str:
    """
    Get the technical identifier for a meta entry.

    Always derived from the endpoint in meta.url.
    This is read-only - users can change display name but not the URL.
    """
    return _url_to_name(meta.url)


async def _get_meta_entry(
    expose_name: str,
    meta_name: str,
    db_path: Optional[str] = None,
) -> tuple:
    """
    Get a specific meta entry from an expose.

    Lookup by identifier derived from URL endpoint.

    Returns (row, ExposeMeta) tuple.
    Raises NotFoundException if not found.
    """
    await db.init_database(db_path)
    row = await db.get_expose(expose_name, db_path)

    if not row:
        raise NotFoundException(detail=f"Expose '{expose_name}' not found")

    if not row.get("enabled"):
        raise NotFoundException(detail=f"Expose '{expose_name}' is not enabled")

    if row.get("state") != "RUNNING":
        raise NotFoundException(detail=f"Expose '{expose_name}' is not running")

    meta_yaml = row.get("meta")
    if not meta_yaml:
        raise NotFoundException(
            detail=f"Service '{expose_name}/{meta_name}' not found"
        )

    try:
        meta_list = yaml.safe_load(meta_yaml)
        if meta_list:
            for m in meta_list:
                meta = ExposeMeta(**m)
                # Match by technical identifier (stored or derived)
                if _get_meta_name(meta) == meta_name:
                    return (row, meta)
    except (yaml.YAMLError, TypeError, ValueError):
        pass

    raise NotFoundException(
        detail=f"Service '{expose_name}/{meta_name}' not found"
    )


class SumiControl(Controller):
    """
    Sumi Protocol Controller.

    Provides MIP-002/MIP-003 compliant endpoints for service discovery
    and management.
    """

    path = "/sumi"
    tags = ["Sumi Protocol"]

    @get(
        "",
        summary="List all services",
        description="List all available meta flows across all enabled exposes. "
        "Returns MIP-002 compliant metadata. Paginated by offset.",
        operation_id="sumi_list_all",
    )
    async def list_all_flows(
        self,
        state: State,
        pp: int = DEFAULT_PAGE_SIZE,
        offset: Optional[str] = None,
    ) -> SumiFlowListResponse:
        """
        List all meta flows from all enabled, running exposes.

        Args:
            pp: Page size (items per page, max 100)
            offset: Last item ID from previous page (cursor-based pagination)
        """
        # Validate and cap page size
        pp = max(1, min(pp, MAX_PAGE_SIZE))
        app_server = state["settings"].APP_SERVER

        # Get all alive flows
        all_flows = await _get_all_alive_flows(app_server)

        # Convert to SumiFlowItem
        items = []
        for expose_name, expose_network, meta, srv in all_flows:
            item = _meta_to_flow_item(expose_name, expose_network, meta, srv)
            items.append(item)

        # Sort by ID for stable ordering
        items.sort(key=lambda x: x.id)

        # Apply offset-based pagination
        start_idx = 0
        if offset:
            for i, item in enumerate(items):
                if item.id == offset:
                    start_idx = i + 1
                    break

        end_idx = min(start_idx + pp, len(items))
        page_items = items[start_idx:end_idx]

        # Determine next offset
        next_offset = None
        if page_items and end_idx < len(items):
            next_offset = page_items[-1].id

        return SumiFlowListResponse(items=page_items, offset=next_offset)

    @get(
        "/{expose_name:str}",
        summary="List services for expose",
        description="List all available meta flows within a specific expose. "
        "Returns MIP-002 compliant metadata. Paginated by offset.",
        operation_id="sumi_list_expose",
    )
    async def list_expose_flows(
        self,
        state: State,
        expose_name: str,
        pp: int = DEFAULT_PAGE_SIZE,
        offset: Optional[str] = None,
    ) -> SumiFlowListResponse:
        """
        List meta flows for a specific expose.

        Args:
            expose_name: Name of the expose
            pp: Page size (max 100)
            offset: Cursor for pagination
        """
        # Validate and cap page size
        pp = max(1, min(pp, MAX_PAGE_SIZE))
        # Validate path parameter
        expose_name = _validate_path_param(expose_name, "expose_name")

        app_server = state["settings"].APP_SERVER

        # Get flows for this expose
        flows = await _get_expose_alive_flows(expose_name, app_server)

        # Convert to SumiFlowItem
        items = []
        for exp_name, exp_network, meta, srv in flows:
            item = _meta_to_flow_item(exp_name, exp_network, meta, srv)
            items.append(item)

        # Sort by id for stable ordering
        items.sort(key=lambda x: x.id)

        # Apply offset-based pagination
        start_idx = 0
        if offset:
            for i, item in enumerate(items):
                if item.id == offset:
                    start_idx = i + 1
                    break

        end_idx = min(start_idx + pp, len(items))
        page_items = items[start_idx:end_idx]

        # Determine next offset
        next_offset = None
        if page_items and end_idx < len(items):
            next_offset = page_items[-1].id

        return SumiFlowListResponse(items=page_items, offset=next_offset)

    @get(
        "/{expose_name:str}/{meta_name:str}",
        summary="Get service metadata",
        description="Get full MIP-002 compliant metadata for a specific service.",
        operation_id="sumi_get_service",
    )
    async def get_service_detail(
        self,
        state: State,
        expose_name: str,
        meta_name: str,
    ) -> SumiServiceDetail:
        """
        Get full MIP-002 metadata for a specific service.

        Args:
            expose_name: Name of the expose
            meta_name: Name (slug) of the meta entry
        """
        # Validate path parameters
        expose_name = _validate_path_param(expose_name, "expose_name")
        meta_name = _validate_path_param(meta_name, "meta_name")

        app_server = state["settings"].APP_SERVER

        row, meta = await _get_meta_entry(expose_name, meta_name)
        expose_network = row.get("network", "Preprod")

        return _meta_to_service_detail(
            expose_name, expose_network, meta, app_server
        )

    @get(
        "/{expose_name:str}/{meta_name:str}/availability",
        summary="Check service availability",
        description="MIP-003 compliant availability check for a specific service.",
        operation_id="sumi_availability",
    )
    async def check_availability(
        self,
        state: State,
        expose_name: str,
        meta_name: str,
    ) -> AvailabilityResponse:
        """
        Check if a service is available.

        Args:
            expose_name: Name of the expose
            meta_name: Name (slug) of the meta entry
        """
        # Validate path parameters
        expose_name = _validate_path_param(expose_name, "expose_name")
        meta_name = _validate_path_param(meta_name, "meta_name")

        try:
            row, meta = await _get_meta_entry(expose_name, meta_name)
        except NotFoundException:
            return AvailabilityResponse(
                status="unavailable",
                message=f"Service '{expose_name}/{meta_name}' not found or not available",
            )

        # Check if flow is disabled
        if not meta.enabled:
            return AvailabilityResponse(
                status="unavailable",
                message="Service is disabled",
            )

        # Check meta state
        if meta.state == "alive":
            data = _parse_meta_data(meta.data)
            display_name = data.get("display", meta_name)
            return AvailabilityResponse(
                status="available",
                message=f"{display_name} is ready to accept jobs",
            )
        else:
            return AvailabilityResponse(
                status="unavailable",
                message=f"Service endpoint is {meta.state or 'not responding'}",
            )

    @get(
        "/{expose_name:str}/{meta_name:str}/input_schema",
        summary="Get input schema",
        description="MIP-003 compliant input schema for job initiation.",
        operation_id="sumi_input_schema",
    )
    async def get_input_schema(
        self,
        state: State,
        expose_name: str,
        meta_name: str,
    ) -> InputSchemaResponse:
        """
        Get MIP-003 input schema for a service.

        This fetches the form schema from the actual endpoint and converts
        it to MIP-003 InputSchemaResponse format.

        Args:
            expose_name: Name of the expose
            meta_name: Name (slug) of the meta entry
        """
        # Validate path parameters
        expose_name = _validate_path_param(expose_name, "expose_name")
        meta_name = _validate_path_param(meta_name, "meta_name")

        # Get meta entry
        row, meta = await _get_meta_entry(expose_name, meta_name)

        # Build endpoint URL
        ray_serve_address = state["settings"].RAY_SERVE_ADDRESS

        # The meta.url is the endpoint path (e.g., "/my-expose/endpoint")
        endpoint_url = ray_serve_address.rstrip("/") + meta.url

        try:
            # Fetch schema from endpoint (GET returns form elements)
            async with HTTPXClient() as client:
                resp = await client.get(endpoint_url, timeout=10.0)

            if resp.status_code != 200:
                # Endpoint not responding properly
                return create_empty_schema()

            schema_data = resp.json()

            # The endpoint returns {"elements": [...], ...}
            elements = schema_data.get("elements", [])

            if not elements:
                return create_empty_schema()

            # Convert to MIP-003 format
            return convert_model_to_schema(elements)

        except Exception:
            # If we can't fetch the schema, return empty
            return create_empty_schema()

    @post(
        "/{expose_name:str}/{meta_name:str}/start_job",
        summary="Start a new job",
        description="MIP-003 compliant job initiation. Starts an execution "
        "and returns job ID and status URL.",
        operation_id="sumi_start_job",
    )
    async def start_job(
        self,
        state: State,
        expose_name: str,
        meta_name: str,
        data: StartJobRequest,
    ) -> StartJobResponse:
        """
        Start a new job execution.

        Args:
            expose_name: Name of the expose
            meta_name: Name (slug) of the meta entry
            data: StartJobRequest with identifier_from_purchaser and optional input_data
        """
        # Validate path parameters
        expose_name = _validate_path_param(expose_name, "expose_name")
        meta_name = _validate_path_param(meta_name, "meta_name")

        # Get meta entry
        row, meta = await _get_meta_entry(expose_name, meta_name)
        app_server = state["settings"].APP_SERVER

        # Calculate MIP-004 input hash
        input_hash = create_input_hash(
            data.input_data, data.identifier_from_purchaser
        )

        # Build endpoint URL
        ray_serve_address = state["settings"].RAY_SERVE_ADDRESS
        endpoint_url = ray_serve_address.rstrip("/") + meta.url

        # Build extra data for the job
        extra = {
            "identifier_from_purchaser": data.identifier_from_purchaser,
            "input_hash": input_hash,
            "sumi_endpoint": f"{expose_name}/{meta_name}",
        }

        try:
            # Submit job to endpoint
            # The endpoint expects form data but we send JSON with extra header
            async with HTTPXClient() as client:
                resp = await client.post(
                    endpoint_url,
                    json=data.input_data or {},
                    headers={
                        "X-Kodosumi-User": SUMI_USER,
                        "X-Kodosumi-Base": f"/-/{expose_name}",
                        "X-Kodosumi-Extra": json.dumps(extra),
                        "X-Kodosumi-Url": app_server,
                    },
                    timeout=30.0,
                )

            if resp.status_code != 200:
                return StartJobResponse(
                    job_id="",
                    status="error",
                    identifierFromPurchaser=data.identifier_from_purchaser,
                    input_hash=input_hash,
                    status_url="",
                )

            response_data = resp.json()
            job_id = response_data.get("result") or response_data.get("fid", "")

            if not job_id:
                return StartJobResponse(
                    job_id="",
                    status="error",
                    identifierFromPurchaser=data.identifier_from_purchaser,
                    input_hash=input_hash,
                    status_url="",
                )

            status_url = f"{app_server}/sumi/status/{job_id}"

            return StartJobResponse(
                job_id=job_id,
                status="success",
                identifierFromPurchaser=data.identifier_from_purchaser,
                input_hash=input_hash,
                status_url=status_url,
            )

        except Exception:
            return StartJobResponse(
                job_id="",
                status="error",
                identifierFromPurchaser=data.identifier_from_purchaser,
                input_hash=input_hash,
                status_url="",
            )

    @get(
        "/status/{job_id:str}",
        summary="Get job status",
        description="MIP-003 compliant job status retrieval. Returns current "
        "status and result if completed.",
        operation_id="sumi_job_status",
    )
    async def get_job_status(
        self,
        state: State,
        job_id: str,
    ) -> JobStatusResponse:
        """
        Get job status.

        Args:
            job_id: The job ID (fid) returned from start_job
        """
        exec_dir = Path(state["settings"].EXEC_DIR)

        # Search for the job across all user directories
        db_file = None
        for user_dir in exec_dir.iterdir():
            if not user_dir.is_dir():
                continue
            potential_db = user_dir / job_id / DB_FILE
            if potential_db.exists():
                db_file = potential_db
                break

        if not db_file:
            # Wait briefly in case job is still initializing
            await asyncio.sleep(SLEEP)
            for user_dir in exec_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                potential_db = user_dir / job_id / DB_FILE
                if potential_db.exists():
                    db_file = potential_db
                    break

        if not db_file:
            raise NotFoundException(detail=f"Job '{job_id}' not found")

        # Get status from database
        conn = sqlite3.connect(str(db_file), isolation_level=None)
        conn.execute('pragma journal_mode=wal;')
        conn.execute('pragma synchronous=normal;')
        conn.execute('pragma read_uncommitted=true;')

        try:
            status_data = await _get_job_status_from_db(conn, job_id)
            return status_data
        finally:
            conn.close()


async def _get_job_status_from_db(
    conn: sqlite3.Connection, job_id: str
) -> JobStatusResponse:
    """
    Query job status from the monitor database.

    Maps Kodosumi status to MIP-003 status.
    """
    cursor = conn.cursor()

    # Get timestamps
    cursor.execute("""
        SELECT MIN(timestamp), MAX(timestamp) FROM monitor
    """)
    first_ts, last_ts = cursor.fetchone()

    # Get current status
    cursor.execute("""
        SELECT message FROM monitor WHERE kind = 'status'
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
    """)
    row = cursor.fetchone()
    kodo_status = row[0] if row else None

    # Get final result
    cursor.execute("""
        SELECT message FROM monitor WHERE kind = 'final'
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
    """)
    row = cursor.fetchone()
    final_result = None
    if row:
        try:
            parsed = dtypes.DynamicModel.model_validate_json(row[0])
            final_result = parsed.model_dump()
        except Exception:
            final_result = {"raw": row[0]}

    # Get error if any
    cursor.execute("""
        SELECT message FROM monitor WHERE kind = 'error'
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
    """)
    row = cursor.fetchone()
    error_msg = row[0] if row else None

    # Get meta for identifier_from_purchaser
    cursor.execute("""
        SELECT message FROM monitor WHERE kind = 'meta'
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
    """)
    row = cursor.fetchone()
    identifier = None
    if row:
        try:
            meta_data = dtypes.DynamicModel.model_validate_json(row[0])
            meta_dict = meta_data.root.get("dict", {})
            extra = meta_dict.get("extra", {})
            if isinstance(extra, dict):
                identifier = extra.get("identifier_from_purchaser")
        except Exception:
            pass

    # Check for locks (awaiting_input)
    cursor.execute("""
        SELECT kind, message FROM monitor
        WHERE kind IN ('lock', 'lease')
        ORDER BY timestamp ASC
    """)
    locks = set()
    for kind, msg in cursor.fetchall():
        try:
            d = dtypes.DynamicModel.model_validate_json(msg)
            lid = d.root["dict"]["lid"]
            if kind == "lock":
                locks.add(lid)
            else:
                locks.discard(lid)
        except Exception:
            pass

    # Map Kodosumi status to MIP-003 status
    if kodo_status == STATUS_END or kodo_status == "finished":
        mip_status = "completed"
    elif kodo_status == STATUS_ERROR or kodo_status == "error":
        mip_status = "failed"
    elif locks:
        mip_status = "awaiting_input"
    elif kodo_status in (STATUS_RUNNING, "starting", "running"):
        mip_status = "running"
    else:
        mip_status = "running"  # Default to running if unknown

    # Calculate runtime
    runtime = None
    if first_ts and last_ts:
        runtime = last_ts - first_ts

    return JobStatusResponse(
        job_id=job_id,
        status=mip_status,
        result=final_result if mip_status == "completed" else None,
        error=error_msg if mip_status == "failed" else None,
        input_schema=None,  # Would need to fetch from lock if awaiting_input
        identifier_from_purchaser=identifier,
        started_at=first_ts,
        updated_at=last_ts,
        runtime=runtime,
    )


class SumiLockControl(Controller):
    """
    Sumi Protocol Lock Controller.

    Provides MIP-003 compliant lock/provide_input endpoints.
    """

    path = "/sumi/lock"
    tags = ["Sumi Protocol"]

    @get(
        "/{fid:str}/{lid:str}",
        summary="Get lock schema",
        description="Get MIP-003 compliant input schema for a pending lock.",
        operation_id="sumi_get_lock",
    )
    async def get_lock_schema(
        self,
        state: State,
        fid: str,
        lid: str,
    ) -> LockSchemaResponse:
        """
        Get input schema for a pending lock.

        Args:
            fid: Job ID (execution ID)
            lid: Lock ID
        """
        try:
            lock, actor = find_lock(fid, lid)
        except LockNotFound as e:
            raise NotFoundException(detail=e.message)

        # Check if lock is already released
        if lock.get("result") is not None:
            return LockSchemaResponse(
                job_id=fid,
                status_id=lid,
                status="released",
                input_schema=create_empty_schema(),
                expires_at=lock.get("expires"),
                prompt=None,
            )

        # Get schema from lock endpoint
        target = f"{lock['app_url']}/_lock_/{fid}/{lid}"

        try:
            async with HTTPXClient() as client:
                resp = await client.get(target, timeout=10.0)

            if resp.status_code != 200:
                return LockSchemaResponse(
                    job_id=fid,
                    status_id=lid,
                    status="pending",
                    input_schema=create_empty_schema(),
                    expires_at=lock.get("expires"),
                    prompt=None,
                )

            elements = resp.json()

            # Convert to MIP-003 schema
            input_schema = convert_model_to_schema(elements)

            return LockSchemaResponse(
                job_id=fid,
                status_id=lid,
                status="pending",
                input_schema=input_schema,
                expires_at=lock.get("expires"),
                prompt=lock.get("name"),  # Lock name can serve as prompt
            )

        except Exception:
            return LockSchemaResponse(
                job_id=fid,
                status_id=lid,
                status="pending",
                input_schema=create_empty_schema(),
                expires_at=lock.get("expires"),
                prompt=None,
            )

    @post(
        "/{fid:str}/{lid:str}",
        summary="Provide input to lock",
        description="MIP-003 compliant provide_input to release a lock.",
        operation_id="sumi_provide_input",
    )
    async def provide_input(
        self,
        state: State,
        fid: str,
        lid: str,
        data: ProvideInputRequest,
    ) -> ProvideInputResponse:
        """
        Provide input to release a pending lock.

        Args:
            fid: Job ID (execution ID)
            lid: Lock ID
            data: ProvideInputRequest with input data
        """
        try:
            lock, actor = find_lock(fid, lid)
        except LockNotFound as e:
            return ProvideInputResponse(
                status="error",
                input_hash=None,
            )

        # Check if lock is already released
        if lock.get("result") is not None:
            return ProvideInputResponse(
                status="error",
                input_hash=None,
            )

        # Post to lock endpoint
        target = f"{lock['app_url']}/_lock_/{fid}/{lid}"

        try:
            async with HTTPXClient() as client:
                resp = await client.post(
                    target,
                    json=data.input_data or {},
                    timeout=10.0,
                )

            if resp.status_code != 200:
                return ProvideInputResponse(
                    status="error",
                    input_hash=None,
                )

            response_data = resp.json()
            result = response_data.get("result")

            # Release the lock via the actor
            import ray
            ray.get(actor.lease.remote(lid, result))

            # Calculate input hash
            input_hash = create_input_hash(data.input_data, f"{fid}:{lid}")

            return ProvideInputResponse(
                status="success",
                input_hash=input_hash,
            )

        except Exception:
            return ProvideInputResponse(
                status="error",
                input_hash=None,
            )
