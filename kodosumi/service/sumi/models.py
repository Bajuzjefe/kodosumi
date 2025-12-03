"""
Pydantic models for Sumi Protocol (MIP-002/MIP-003 compliant).
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# =============================================================================
# MIP-002: On-Chain Metadata Models
# =============================================================================


class AuthorInfo(BaseModel):
    """MIP-002 author metadata."""

    name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_other: Optional[str] = None
    organization: Optional[str] = None


class CapabilityInfo(BaseModel):
    """MIP-002 capability descriptor."""

    name: str
    version: str


class LegalInfo(BaseModel):
    """MIP-002 legal compliance."""

    privacy_policy: Optional[str] = None
    terms: Optional[str] = None
    other: Optional[str] = None


class FixedPricing(BaseModel):
    """MIP-002 fixed pricing entry."""

    amount: str  # String to handle large numbers
    unit: str  # e.g., "lovelace" (1,000,000 lovelace = 1 ADA)


class AgentPricing(BaseModel):
    """MIP-002 pricing configuration."""

    pricingType: Literal["Fixed"] = "Fixed"  # Only Fixed supported currently
    fixedPricing: List[FixedPricing]


class ExampleOutput(BaseModel):
    """MIP-002 example output reference.

    Note: This is for OUTPUT examples - samples of what the service
    PRODUCES, not what it accepts as input.
    """

    name: str
    mime_type: str  # e.g., "application/json", "image/png", "application/pdf"
    url: str  # URL to example output file


# =============================================================================
# Discovery Models
# =============================================================================


class SumiFlowItem(BaseModel):
    """Flow item with MIP-002 core fields for list responses."""

    # Identifiers
    id: str = Field(description="Unique identifier: {parent} or {parent}/{name}")
    parent: str = Field(description="Expose name (expose.name)")
    name: str = Field(description="Endpoint name (empty for root endpoint)")

    # MIP-002 required fields
    display: str = Field(description="Human-readable name (meta.data.display)")
    api_url: str = Field(description="Sumi protocol endpoint: /sumi/{parent} or /sumi/{parent}/{name}")
    base_url: str = Field(description="Full Ray Serve URL: http://host:port/{route_prefix}/{endpoint}")
    tags: List[str] = Field(description="Tags (min 1 required)")
    agentPricing: List[AgentPricing] = Field(description="Pricing info")
    metadata_version: int = Field(default=1, description="MIP-002 version")

    # MIP-002 recommended fields
    description: Optional[str] = None
    image: Optional[str] = None
    example_output: Optional[List[ExampleOutput]] = Field(
        default=None, description="Samples of service OUTPUT"
    )

    # Kodosumi-specific
    network: str = Field(description='"Preprod" | "Mainnet"')
    state: str = Field(description='"alive" | "dead"')

    # MIP-002 optional (always included per protocol)
    author: Optional[AuthorInfo] = None
    capability: Optional[CapabilityInfo] = None
    legal: Optional[LegalInfo] = None


class SumiFlowListResponse(BaseModel):
    """Paginated list of flows."""

    items: List[SumiFlowItem]
    offset: Optional[str] = Field(
        default=None, description="ID of last item, None if no more pages"
    )


class SumiServiceDetail(BaseModel):
    """Full MIP-002 compliant service metadata."""

    # Identifiers
    id: str = Field(description="Unique identifier: {parent} or {parent}/{name}")
    parent: str = Field(description="Expose name (expose.name)")
    name: str = Field(description="Endpoint name (empty for root endpoint)")

    # MIP-002 REQUIRED fields
    display: str = Field(description="Human-readable name (meta.data.display)")
    api_url: str = Field(description="Sumi protocol endpoint: /sumi/{parent} or /sumi/{parent}/{name}")
    base_url: str = Field(description="Full Ray Serve URL: http://host:port/{route_prefix}/{endpoint}")
    tags: List[str] = Field(description="Tags (min 1 required)")
    agentPricing: List[AgentPricing]
    metadata_version: int = Field(default=1)

    # MIP-002 RECOMMENDED fields
    description: Optional[str] = None
    image: Optional[str] = None
    example_output: Optional[List[ExampleOutput]] = Field(
        default=None, description="Samples of service OUTPUT"
    )

    # MIP-002 OPTIONAL fields (always included per protocol)
    author: Optional[AuthorInfo] = None
    capability: Optional[CapabilityInfo] = None
    legal: Optional[LegalInfo] = None

    # Kodosumi-specific fields
    network: str = Field(description="Cardano network: Preprod | Mainnet")
    state: str = Field(description="Service state: alive | dead")
    url: str = Field(description="Ray endpoint path (meta.url): /{route_prefix}/{endpoint}")


# =============================================================================
# MIP-003: Availability
# =============================================================================


class AvailabilityResponse(BaseModel):
    """MIP-003 availability response."""

    status: Literal["available", "unavailable"]
    type: str = Field(default="masumi-agent", description="MIP-003 required identifier")
    message: Optional[str] = None


# =============================================================================
# MIP-003: Input Schema
# =============================================================================


class InputField(BaseModel):
    """MIP-003 input field definition."""

    id: str = Field(description="Field identifier")
    type: Literal[
        # Text & Content
        "text", "textarea", "search", "password", "hidden", "none",
        # Numeric
        "number", "range",
        # Selection
        "option", "radio", "checkbox", "boolean",
        # Date/Time
        "date", "datetime-local", "time", "month", "week",
        # Web-based
        "email", "url", "tel",
        # Media
        "color", "file",
    ]
    name: Optional[str] = Field(default=None, description="Display label")
    data: Optional[dict] = Field(
        default=None, description="Config: description, options list, etc."
    )
    validations: Optional[dict] = Field(
        default=None, description="Constraints: format, min, max, etc."
    )


class InputGroup(BaseModel):
    """MIP-003 grouped input fields."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    inputs: List[InputField]


class InputSchemaResponse(BaseModel):
    """MIP-003 input schema response."""

    input_data: Optional[List[InputField]] = Field(
        default=None, description="Flat input fields"
    )
    input_groups: Optional[List[InputGroup]] = Field(
        default=None, description="Grouped input fields"
    )


# =============================================================================
# MIP-003: Start Job
# =============================================================================


class StartJobRequest(BaseModel):
    """MIP-003 start job request."""

    identifier_from_purchaser: str = Field(
        description="Customer-defined job identifier"
    )
    input_data: Optional[dict] = Field(
        default=None, description="Job inputs conforming to schema"
    )


class StartJobResponse(BaseModel):
    """MIP-003 start job response."""

    job_id: str = Field(description="Kodosumi execution ID (fid)")
    status: Literal["success", "error"]
    identifierFromPurchaser: str = Field(description="Echoed identifier")
    input_hash: Optional[str] = Field(
        default=None, description="MIP-004 hash of input_data"
    )

    # Kodosumi extensions (not in MIP-003 but useful)
    status_url: str = Field(description="URL to poll for status")


# =============================================================================
# MIP-003: Job Status
# =============================================================================


class JobStatusResponse(BaseModel):
    """MIP-003 status response."""

    job_id: str
    status: Literal[
        "awaiting_payment",  # Not used in Kodosumi (no payment)
        "awaiting_input",  # Maps to Kodosumi "awaiting" (lock pending)
        "running",  # Job in progress
        "completed",  # Maps to Kodosumi "finished"
        "failed",  # Maps to Kodosumi "error"
    ]

    # Conditional fields
    input_schema: Optional[InputSchemaResponse] = Field(
        default=None, description='When status="awaiting_input"'
    )
    result: Optional[dict] = Field(default=None, description='When status="completed"')
    error: Optional[str] = Field(default=None, description='When status="failed"')

    # Kodosumi extensions
    identifier_from_purchaser: Optional[str] = None
    started_at: Optional[float] = None
    updated_at: Optional[float] = None
    runtime: Optional[float] = None


# =============================================================================
# MIP-003: Lock (provide_input)
# =============================================================================


class LockSchemaResponse(BaseModel):
    """Lock input schema for awaiting_input status."""

    job_id: str = Field(description="fid")
    status_id: str = Field(description="lid (lock ID)")
    status: Literal["pending", "released", "expired"]
    input_schema: InputSchemaResponse = Field(description="MIP-003 format")
    expires_at: Optional[float] = None
    prompt: Optional[str] = Field(
        default=None, description="Human-readable question"
    )


class ProvideInputRequest(BaseModel):
    """MIP-003 provide_input request (adapted for lock)."""

    input_data: Optional[dict] = Field(
        default=None, description="Simple input values"
    )
    input_groups: Optional[List[dict]] = Field(
        default=None, description="Grouped input values"
    )


class ProvideInputResponse(BaseModel):
    """MIP-003 provide_input response."""

    status: Literal["success", "error"]
    input_hash: Optional[str] = Field(
        default=None, description="Hash of submitted data"
    )


# =============================================================================
# Error Response
# =============================================================================


class ErrorResponse(BaseModel):
    """Standard error response."""

    status: Literal["error"] = "error"
    message: str
    code: Optional[str] = Field(
        default=None, description="Machine-readable error code"
    )
