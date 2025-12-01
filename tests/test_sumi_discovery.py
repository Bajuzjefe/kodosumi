"""
Tests for Sumi Protocol Discovery Endpoints (Phase 3).

Tests MIP-002 compliant discovery endpoints:
- GET /sumi - List all meta flows
- GET /sumi/{expose-name} - List flows for expose
- GET /sumi/{expose-name}/{meta-name} - Service detail
- GET /sumi/{expose-name}/{meta-name}/availability - Health check
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import yaml

from kodosumi.service.sumi.control import (
    _parse_meta_data,
    _validate_path_param,
    _sanitize_name,
    _url_to_name,
    _build_sumi_url,
    _build_base_url,
    _parse_agent_pricing,
    _parse_author,
    _meta_to_flow_item,
    _meta_to_service_detail,
    _get_all_alive_flows,
    _get_expose_alive_flows,
    _get_meta_entry,
)
from kodosumi.service.sumi.models import (
    SumiFlowItem,
    SumiFlowListResponse,
    SumiServiceDetail,
    AvailabilityResponse,
)
from kodosumi.service.expose.models import ExposeMeta
from kodosumi.service.expose import db
from litestar.exceptions import NotFoundException


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestParseMetaData:
    """Test _parse_meta_data helper."""

    def test_empty_data(self):
        result = _parse_meta_data(None)
        assert result == {}

        result = _parse_meta_data("")
        assert result == {}

    def test_valid_yaml(self):
        data = """
display: My Service
tags:
  - ai
  - test
"""
        result = _parse_meta_data(data)
        assert result["display"] == "My Service"
        assert result["tags"] == ["ai", "test"]

    def test_invalid_yaml(self):
        result = _parse_meta_data("{{invalid yaml")
        assert result == {}


class TestValidatePathParam:
    """Test _validate_path_param for URL path parameter validation."""

    def test_valid_simple_names(self):
        """Valid simple names pass through."""
        assert _validate_path_param("myflow") == "myflow"
        assert _validate_path_param("my-flow") == "my-flow"
        assert _validate_path_param("my_flow") == "my_flow"
        assert _validate_path_param("flow123") == "flow123"

    def test_converts_to_lowercase(self):
        """Uppercase is converted to lowercase."""
        assert _validate_path_param("MyFlow") == "myflow"
        assert _validate_path_param("MY-FLOW") == "my-flow"

    def test_rejects_empty(self):
        """Empty strings are rejected."""
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("")
        assert exc_info.value.status_code == 400
        assert "cannot be empty" in str(exc_info.value.detail)

    def test_rejects_whitespace(self):
        """Names with whitespace are rejected."""
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("my flow")
        assert exc_info.value.status_code == 400

    def test_rejects_special_chars(self):
        """Names with special characters are rejected."""
        from litestar.exceptions import HTTPException
        invalid_names = ["my@flow", "my.flow", "my/flow", "my!flow", "my#flow"]
        for name in invalid_names:
            with pytest.raises(HTTPException) as exc_info:
                _validate_path_param(name)
            assert exc_info.value.status_code == 400

    def test_rejects_leading_hyphen(self):
        """Names starting with hyphen are rejected."""
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("-myflow")
        assert exc_info.value.status_code == 400

    def test_rejects_leading_underscore(self):
        """Names starting with underscore are rejected."""
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("_myflow")
        assert exc_info.value.status_code == 400

    def test_custom_param_name_in_error(self):
        """Custom param name appears in error message."""
        from litestar.exceptions import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_path_param("bad@name", "expose_name")
        assert "expose_name" in str(exc_info.value.detail)


class TestSanitizeName:
    """Test _sanitize_name helper for URL slug generation."""

    def test_simple_name(self):
        """Simple alphanumeric names pass through."""
        assert _sanitize_name("my-flow") == "my-flow"
        assert _sanitize_name("flow_1") == "flow_1"
        assert _sanitize_name("TestFlow") == "testflow"

    def test_whitespace_to_hyphens(self):
        """Whitespace is converted to hyphens."""
        assert _sanitize_name("my flow") == "my-flow"
        assert _sanitize_name("my  flow") == "my-flow"
        assert _sanitize_name("my\tflow") == "my-flow"

    def test_special_chars_removed(self):
        """Special characters are removed."""
        assert _sanitize_name("my@flow!") == "myflow"
        assert _sanitize_name("flow#123") == "flow123"
        assert _sanitize_name("test.flow") == "testflow"
        assert _sanitize_name("my/flow") == "myflow"

    def test_preserves_hyphens_underscores(self):
        """Hyphens and underscores are preserved."""
        assert _sanitize_name("my-flow_v2") == "my-flow_v2"
        assert _sanitize_name("test_flow-1") == "test_flow-1"

    def test_consecutive_hyphens_collapsed(self):
        """Multiple consecutive hyphens are collapsed."""
        assert _sanitize_name("my--flow") == "my-flow"
        assert _sanitize_name("my - flow") == "my-flow"

    def test_leading_trailing_hyphens_stripped(self):
        """Leading/trailing hyphens are stripped."""
        assert _sanitize_name("-my-flow-") == "my-flow"
        assert _sanitize_name("  my flow  ") == "my-flow"

    def test_empty_becomes_unnamed(self):
        """Empty or all-special-chars becomes 'unnamed'."""
        assert _sanitize_name("") == "unnamed"
        assert _sanitize_name("@#$%") == "unnamed"
        assert _sanitize_name("   ") == "unnamed"

    def test_unicode_removed(self):
        """Unicode characters are removed."""
        assert _sanitize_name("my-flöw") == "my-flw"
        assert _sanitize_name("日本語flow") == "flow"


class TestUrlToName:
    """Test _url_to_name helper (generates default name from URL path)."""

    def test_single_element(self):
        """Single path element is returned."""
        assert _url_to_name("/endpoint") == "endpoint"
        assert _url_to_name("endpoint") == "endpoint"

    def test_multi_element_returns_last(self):
        """Multiple path elements: only the last (endpoint) is returned."""
        assert _url_to_name("/my-agent/process") == "process"
        assert _url_to_name("/deep/nested/path/endpoint") == "endpoint"

    def test_with_slashes(self):
        """Leading/trailing slashes are handled."""
        assert _url_to_name("/endpoint/") == "endpoint"
        assert _url_to_name("///endpoint///") == "endpoint"

    def test_sanitizes_result(self):
        """Result (endpoint) is sanitized (lowercase, special chars removed)."""
        assert _url_to_name("/My-Agent/END POINT") == "end-point"
        assert _url_to_name("/path/v2.0") == "v20"

    def test_empty_returns_root(self):
        """Empty path returns 'root'."""
        assert _url_to_name("") == "root"
        assert _url_to_name("/") == "root"
        assert _url_to_name("///") == "root"


class TestBuildSumiUrl:
    """Test _build_sumi_url helper."""

    def test_basic(self):
        url = _build_sumi_url("http://localhost:3370", "my-expose", "my-flow")
        assert url == "http://localhost:3370/sumi/my-expose/my-flow"

    def test_trailing_slash(self):
        url = _build_sumi_url("http://localhost:3370/", "my-expose", "my-flow")
        assert url == "http://localhost:3370/sumi/my-expose/my-flow"


class TestBuildBaseUrl:
    """Test _build_base_url helper."""

    def test_basic(self):
        url = _build_base_url("http://localhost:3370", "/my-expose/endpoint")
        assert url == "http://localhost:3370/-/my-expose/endpoint"

    def test_trailing_slash(self):
        url = _build_base_url("http://localhost:3370/", "/my-expose/endpoint")
        assert url == "http://localhost:3370/-/my-expose/endpoint"

    def test_no_leading_slash(self):
        url = _build_base_url("http://localhost:3370", "my-expose/endpoint")
        assert url == "http://localhost:3370/-/my-expose/endpoint"


class TestParseAgentPricing:
    """Test _parse_agent_pricing helper."""

    def test_empty(self):
        result = _parse_agent_pricing({})
        assert len(result) == 1
        assert result[0].pricingType == "Fixed"
        assert result[0].fixedPricing[0].amount == "0"

    def test_with_pricing(self):
        data = {
            "agentPricing": [
                {
                    "pricingType": "Fixed",
                    "fixedPricing": [
                        {"amount": "1000000", "unit": "lovelace"},
                        {"amount": "2000000", "unit": "lovelace"},
                    ],
                }
            ]
        }
        result = _parse_agent_pricing(data)
        assert len(result) == 1
        assert len(result[0].fixedPricing) == 2
        assert result[0].fixedPricing[0].amount == "1000000"


class TestParseAuthor:
    """Test _parse_author helper."""

    def test_empty(self):
        result = _parse_author({})
        assert result is None

    def test_with_author(self):
        data = {
            "author": {
                "name": "John Doe",
                "contact_email": "john@example.com",
                "organization": "Example Corp",
            }
        }
        result = _parse_author(data)
        assert result is not None
        assert result.name == "John Doe"
        assert result.contact_email == "john@example.com"
        assert result.organization == "Example Corp"


class TestMetaToFlowItem:
    """Test _meta_to_flow_item helper."""

    def test_basic(self):
        meta = ExposeMeta(
            url="/my-expose/process",
            name="ignored",  # stored name is ignored
            data="""
display: Test Flow
tags:
  - test
agentPricing:
  - pricingType: Fixed
    fixedPricing:
      - amount: "1000000"
        unit: lovelace
""",
            state="alive",
            heartbeat=1234567890.0,
        )

        result = _meta_to_flow_item(
            expose_name="my-expose",
            expose_network="Preprod",
            meta=meta,
            app_server="http://localhost:3370",
        )

        # name is derived from URL endpoint, stored meta.name is ignored
        assert result.id == "my-expose/process"
        assert result.parent == "my-expose"
        assert result.name == "process"
        assert result.display == "Test Flow"
        # api_url uses the endpoint-derived name
        assert result.api_url == "http://localhost:3370/sumi/my-expose/process"
        assert result.base_url == "http://localhost:3370/-/my-expose/process"
        assert result.tags == ["test"]
        assert result.network == "Preprod"
        assert result.state == "alive"

    def test_missing_tags_defaults(self):
        meta = ExposeMeta(
            url="/test/endpoint",
            name="test-flow",
            data="display: Test",
            state="alive",
        )

        result = _meta_to_flow_item(
            expose_name="expose",
            expose_network="Mainnet",
            meta=meta,
            app_server="http://localhost",
        )

        assert result.tags == ["untagged"]

    def test_name_derived_from_url_endpoint(self):
        """Test that name is always derived from URL endpoint (stored name ignored)."""
        meta = ExposeMeta(
            url="/my-agent/process",
            name="Ignored Name",  # This is ignored - name comes from endpoint
            data="",
            state="alive",
        )

        result = _meta_to_flow_item(
            expose_name="expose",
            expose_network="Preprod",
            meta=meta,
            app_server="http://localhost",
        )

        # name is derived from URL endpoint, not stored meta.name
        assert result.name == "process"
        assert result.id == "expose/process"
        assert result.api_url == "http://localhost/sumi/expose/process"
        assert result.base_url == "http://localhost/-/my-agent/process"
        # display falls back to endpoint name (meta.name is ignored)
        assert result.display == "process"

    def test_display_overrides_original_name(self):
        """Test that explicit display in data overrides meta.name."""
        meta = ExposeMeta(
            url="/test/endpoint",
            name="internal_flow_name",
            data="display: Pretty Display Name",
            state="alive",
        )

        result = _meta_to_flow_item(
            expose_name="expose",
            expose_network="Preprod",
            meta=meta,
            app_server="http://localhost",
        )

        assert result.display == "Pretty Display Name"


# =============================================================================
# Database Integration Tests
# =============================================================================


class TestGetAllAliveFlows:
    """Test _get_all_alive_flows database function."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "expose.db")
            yield db_path

    @pytest.mark.asyncio
    async def test_empty_database(self, temp_db):
        """Test with empty database."""
        await db.init_database(temp_db)
        result = await _get_all_alive_flows("http://localhost", temp_db)
        assert result == []

    @pytest.mark.asyncio
    async def test_with_running_expose(self, temp_db):
        """Test with running expose containing alive flows."""
        await db.init_database(temp_db)

        # Create a running expose with meta
        meta_yaml = yaml.dump([
            {
                "url": "/test/endpoint",
                "data": "display: Test Flow\ntags:\n  - test",
                "state": "alive",
                "heartbeat": 1234567890.0,
            }
        ])

        await db.upsert_expose(
            name="test-expose",
            display="Test Expose",
            network="Preprod",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="some bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        result = await _get_all_alive_flows("http://localhost", temp_db)
        assert len(result) == 1
        assert result[0][0] == "test-expose"  # expose_name
        assert result[0][2].url == "/test/endpoint"  # meta.url

    @pytest.mark.asyncio
    async def test_filters_disabled_exposes(self, temp_db):
        """Test that disabled exposes are filtered out."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([{"url": "/test", "state": "alive"}])

        await db.upsert_expose(
            name="disabled-expose",
            display="Disabled",
            network="Preprod",
            enabled=False,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        result = await _get_all_alive_flows("http://localhost", temp_db)
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_non_running_exposes(self, temp_db):
        """Test that non-RUNNING exposes are filtered out."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([{"url": "/test", "state": "alive"}])

        await db.upsert_expose(
            name="dead-expose",
            display="Dead",
            network="Preprod",
            enabled=True,
            state="DEAD",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        result = await _get_all_alive_flows("http://localhost", temp_db)
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_dead_meta_entries(self, temp_db):
        """Test that dead meta entries are filtered out."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([
            {"url": "/alive", "state": "alive"},
            {"url": "/dead", "state": "dead"},
        ])

        await db.upsert_expose(
            name="mixed-expose",
            display="Mixed",
            network="Preprod",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        result = await _get_all_alive_flows("http://localhost", temp_db)
        assert len(result) == 1
        assert result[0][2].url == "/alive"


class TestGetExposeAliveFlows:
    """Test _get_expose_alive_flows database function."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "expose.db")
            yield db_path

    @pytest.mark.asyncio
    async def test_not_found(self, temp_db):
        """Test with non-existent expose."""
        await db.init_database(temp_db)

        from litestar.exceptions import NotFoundException
        with pytest.raises(NotFoundException) as exc_info:
            await _get_expose_alive_flows("nonexistent", "http://localhost", temp_db)
        assert "not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_disabled(self, temp_db):
        """Test with disabled expose."""
        await db.init_database(temp_db)

        await db.upsert_expose(
            name="disabled",
            display="Disabled",
            network="Preprod",
            enabled=False,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=None,
            db_path=temp_db,
        )

        from litestar.exceptions import NotFoundException
        with pytest.raises(NotFoundException) as exc_info:
            await _get_expose_alive_flows("disabled", "http://localhost", temp_db)
        assert "not enabled" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_not_running(self, temp_db):
        """Test with non-RUNNING expose."""
        await db.init_database(temp_db)

        await db.upsert_expose(
            name="dead",
            display="Dead",
            network="Preprod",
            enabled=True,
            state="DEAD",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=None,
            db_path=temp_db,
        )

        from litestar.exceptions import NotFoundException
        with pytest.raises(NotFoundException) as exc_info:
            await _get_expose_alive_flows("dead", "http://localhost", temp_db)
        assert "not running" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_success(self, temp_db):
        """Test successful retrieval."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([
            {"url": "/test", "state": "alive"},
        ])

        await db.upsert_expose(
            name="good",
            display="Good",
            network="Mainnet",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        result = await _get_expose_alive_flows("good", "http://localhost", temp_db)
        assert len(result) == 1
        assert result[0][0] == "good"
        assert result[0][1] == "Mainnet"


class TestGetMetaEntry:
    """Test _get_meta_entry database function."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "expose.db")
            yield db_path

    @pytest.mark.asyncio
    async def test_not_found_expose(self, temp_db):
        """Test with non-existent expose."""
        await db.init_database(temp_db)

        from litestar.exceptions import NotFoundException
        with pytest.raises(NotFoundException):
            await _get_meta_entry("nonexistent", "flow", temp_db)

    @pytest.mark.asyncio
    async def test_not_found_meta(self, temp_db):
        """Test with non-existent meta entry (URL slug not found)."""
        await db.init_database(temp_db)

        # URL /test -> slug "test"
        meta_yaml = yaml.dump([{"url": "/test", "state": "alive"}])

        await db.upsert_expose(
            name="expose",
            display="Expose",
            network="Preprod",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        from litestar.exceptions import NotFoundException
        with pytest.raises(NotFoundException) as exc_info:
            # Look for non-existent slug
            await _get_meta_entry("expose", "nonexistent-slug", temp_db)
        assert "not found" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_success_by_endpoint(self, temp_db):
        """Test retrieval by endpoint derived from URL."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([
            {"url": "/my-agent/process", "state": "alive", "data": "display: My Flow"},
        ])

        await db.upsert_expose(
            name="expose",
            display="Expose",
            network="Preprod",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        # Look up by endpoint derived from URL (process)
        row, meta = await _get_meta_entry("expose", "process", temp_db)
        assert row["name"] == "expose"
        assert meta.url == "/my-agent/process"

    @pytest.mark.asyncio
    async def test_lookup_by_endpoint_only(self, temp_db):
        """Test that lookup is only by endpoint from URL."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([
            {"url": "/my-agent/analyze", "state": "alive", "data": "display: Analyzer"},
        ])

        await db.upsert_expose(
            name="expose",
            display="Expose",
            network="Preprod",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        # Lookup by non-existent slug fails
        with pytest.raises(NotFoundException):
            await _get_meta_entry("expose", "nonexistent", temp_db)

        # Lookup by endpoint from URL succeeds
        row, meta = await _get_meta_entry("expose", "analyze", temp_db)
        assert meta.url == "/my-agent/analyze"

    @pytest.mark.asyncio
    async def test_filters_disabled_meta(self, temp_db):
        """Test that disabled meta entries are not returned."""
        await db.init_database(temp_db)

        meta_yaml = yaml.dump([
            {"url": "/enabled-flow", "state": "alive", "enabled": True},
            {"url": "/disabled-flow", "state": "alive", "enabled": False},
        ])

        await db.upsert_expose(
            name="expose",
            display="Expose",
            network="Preprod",
            enabled=True,
            state="RUNNING",
            heartbeat=1234567890.0,
            bootstrap="bootstrap",
            meta=meta_yaml,
            db_path=temp_db,
        )

        # Enabled flow should be found
        row, meta = await _get_meta_entry("expose", "enabled-flow", temp_db)
        assert meta.url == "/enabled-flow"
        assert meta.enabled is True

        # Disabled flow should raise NotFoundException
        with pytest.raises(NotFoundException) as exc_info:
            await _get_meta_entry("expose", "disabled-flow", temp_db)
        assert "not found" in str(exc_info.value.detail)


# =============================================================================
# Model Conversion Tests
# =============================================================================


class TestSumiFlowItemModel:
    """Test SumiFlowItem model creation."""

    def test_full_item(self):
        item = SumiFlowItem(
            id="expose/endpoint",  # id is {parent}/{name}
            parent="expose",
            name="endpoint",
            display="Flow Name",
            api_url="http://localhost/sumi/expose/endpoint",
            base_url="http://localhost/-/expose/endpoint",
            tags=["ai", "test"],
            agentPricing=[],
            metadata_version=1,
            description="A description",
            image="http://example.com/image.png",
            network="Preprod",
            state="alive",
        )
        assert item.id == "expose/endpoint"
        assert item.name == "endpoint"
        assert item.display == "Flow Name"
        assert item.metadata_version == 1


class TestSumiServiceDetailModel:
    """Test SumiServiceDetail model creation."""

    def test_full_detail(self):
        detail = SumiServiceDetail(
            id="expose/endpoint",  # id is {parent}/{name}
            parent="expose",
            name="endpoint",
            display="Flow Name",
            api_url="http://localhost/sumi/expose/endpoint",
            base_url="http://localhost/-/expose/endpoint",
            tags=["ai"],
            agentPricing=[],
            metadata_version=1,
            network="Preprod",
            state="alive",
            url="/expose/endpoint",
        )
        assert detail.id == "expose/endpoint"
        assert detail.name == "endpoint"
        assert detail.url == "/expose/endpoint"


class TestAvailabilityResponseModel:
    """Test AvailabilityResponse model."""

    def test_available(self):
        resp = AvailabilityResponse(
            status="available",
            message="Ready",
        )
        assert resp.status == "available"
        assert resp.type == "masumi-agent"

    def test_unavailable(self):
        resp = AvailabilityResponse(
            status="unavailable",
            message="Not ready",
        )
        assert resp.status == "unavailable"


# =============================================================================
# Pagination Tests
# =============================================================================


class TestPagination:
    """Test pagination logic."""

    def test_offset_pagination(self):
        """Test offset-based pagination manually."""
        # id is {parent}/{name}
        items = [
            SumiFlowItem(
                id=f"expose/flow-{i}",
                parent="expose",
                name=f"flow-{i}",
                display=f"Flow {i}",
                api_url=f"http://localhost/sumi/expose/flow-{i}",
                base_url=f"http://localhost/-/expose/flow-{i}",
                tags=["test"],
                agentPricing=[],
                network="Preprod",
                state="alive",
            )
            for i in range(5)
        ]

        # First page (pp=2)
        pp = 2
        offset = None
        start_idx = 0
        end_idx = min(start_idx + pp, len(items))
        page_items = items[start_idx:end_idx]
        next_offset = page_items[-1].id if page_items and end_idx < len(items) else None

        assert len(page_items) == 2
        assert page_items[0].id == "expose/flow-0"
        assert next_offset == "expose/flow-1"

        # Second page
        offset = "expose/flow-1"
        start_idx = 0
        for i, item in enumerate(items):
            if item.id == offset:
                start_idx = i + 1
                break
        end_idx = min(start_idx + pp, len(items))
        page_items = items[start_idx:end_idx]
        next_offset = page_items[-1].id if page_items and end_idx < len(items) else None

        assert len(page_items) == 2
        assert page_items[0].id == "expose/flow-2"
        assert next_offset == "expose/flow-3"

        # Last page
        offset = "expose/flow-3"
        start_idx = 0
        for i, item in enumerate(items):
            if item.id == offset:
                start_idx = i + 1
                break
        end_idx = min(start_idx + pp, len(items))
        page_items = items[start_idx:end_idx]
        next_offset = page_items[-1].id if page_items and end_idx < len(items) else None

        assert len(page_items) == 1
        assert page_items[0].id == "expose/flow-4"
        assert next_offset is None  # No more pages
