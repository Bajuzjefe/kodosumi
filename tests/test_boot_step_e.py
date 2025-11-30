"""
Tests for Boot Process Step E: Update Meta (Database Update)

Tests the following functions:
- ExposeMeta dataclass
- flow_status_to_meta() - Convert FlowStatus to ExposeMeta
- serialize_meta_list() - Serialize metas to YAML
- save_expose_meta() - Save to database
- _step_update_meta() - Full update meta step generator
"""
import pytest
from unittest.mock import AsyncMock, patch
import yaml
import time

from kodosumi.service.expose.boot import (
    ExposeMeta,
    FlowStatus,
    DiscoveredFlow,
    flow_status_to_meta,
    serialize_meta_list,
    save_expose_meta,
    _step_update_meta,
    BootProgress,
    BootStep,
    MessageType,
)


# =============================================================================
# ExposeMeta Tests
# =============================================================================

class TestExposeMeta:
    """Tests for ExposeMeta dataclass."""

    def test_create_expose_meta(self):
        """Test creating ExposeMeta."""
        meta = ExposeMeta(
            url="/app/run",
            name="run",
            data="summary: Run Agent",
            state="alive",
            heartbeat=1700000000.0,
        )

        assert meta.url == "/app/run"
        assert meta.name == "run"
        assert meta.data == "summary: Run Agent"
        assert meta.state == "alive"
        assert meta.heartbeat == 1700000000.0


# =============================================================================
# flow_status_to_meta Tests
# =============================================================================

class TestFlowStatusToMeta:
    """Tests for flow_status_to_meta function."""

    def test_converts_basic_flow(self):
        """Test converting a basic FlowStatus."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/run",
            method="POST",
            summary="Run Agent",
            description="Execute the agent workflow",
            tags=["agent", "workflow"],
        )

        status = FlowStatus(
            flow=flow,
            state="alive",
            response_code=200,
            checked_at=1700000000.0,
        )

        meta = flow_status_to_meta(status)

        assert meta.url == "/test-app/run"
        assert meta.name == "run"
        assert meta.state == "alive"
        assert meta.heartbeat == 1700000000.0
        assert "summary: Run Agent" in meta.data
        assert "description: Execute the agent workflow" in meta.data
        assert "method: POST" in meta.data

    def test_extracts_name_from_path(self):
        """Test endpoint name is extracted from path."""
        flow = DiscoveredFlow(
            app_name="my-agent",
            path="/my-agent/v1/execute",
            method="POST",
            summary="Execute",
            description="",
            tags=[],
        )

        status = FlowStatus(
            flow=flow,
            state="alive",
            response_code=200,
            checked_at=1700000000.0,
        )

        meta = flow_status_to_meta(status)

        assert meta.name == "execute"

    def test_includes_author_metadata(self):
        """Test author and organization are included."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/run",
            method="POST",
            summary="Run",
            description="",
            tags=[],
            author="dev@example.com",
            organization="MyOrg",
        )

        status = FlowStatus(
            flow=flow,
            state="alive",
            response_code=200,
            checked_at=1700000000.0,
        )

        meta = flow_status_to_meta(status)

        assert "author: dev@example.com" in meta.data
        assert "organization: MyOrg" in meta.data

    def test_preserves_dead_state(self):
        """Test dead state is preserved."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/broken",
            method="POST",
            summary="Broken",
            description="",
            tags=[],
        )

        status = FlowStatus(
            flow=flow,
            state="dead",
            response_code=500,
            checked_at=1700000000.0,
        )

        meta = flow_status_to_meta(status)

        assert meta.state == "dead"

    def test_handles_empty_tags(self):
        """Test empty tags list is handled."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/run",
            method="POST",
            summary="Run",
            description="",
            tags=[],
        )

        status = FlowStatus(
            flow=flow,
            state="alive",
            response_code=200,
            checked_at=1700000000.0,
        )

        meta = flow_status_to_meta(status)

        # Should have tags: [] in YAML
        assert "tags:" in meta.data


# =============================================================================
# serialize_meta_list Tests
# =============================================================================

class TestSerializeMetaList:
    """Tests for serialize_meta_list function."""

    def test_serializes_empty_list(self):
        """Test serializing empty list."""
        result = serialize_meta_list([])
        assert result == "[]\n"

    def test_serializes_single_meta(self):
        """Test serializing single meta."""
        meta = ExposeMeta(
            url="/app/run",
            name="run",
            data="summary: Run",
            state="alive",
            heartbeat=1700000000.0,
        )

        result = serialize_meta_list([meta])

        # Parse back to verify structure
        parsed = yaml.safe_load(result)
        assert len(parsed) == 1
        assert parsed[0]["url"] == "/app/run"
        assert parsed[0]["name"] == "run"
        assert parsed[0]["state"] == "alive"

    def test_serializes_multiple_metas(self):
        """Test serializing multiple metas."""
        metas = [
            ExposeMeta(
                url="/app/run",
                name="run",
                data="summary: Run",
                state="alive",
                heartbeat=1700000000.0,
            ),
            ExposeMeta(
                url="/app/query",
                name="query",
                data="summary: Query",
                state="dead",
                heartbeat=1700000000.0,
            ),
        ]

        result = serialize_meta_list(metas)

        parsed = yaml.safe_load(result)
        assert len(parsed) == 2
        assert parsed[0]["name"] == "run"
        assert parsed[1]["name"] == "query"

    def test_preserves_data_field(self):
        """Test data field is preserved as string."""
        meta = ExposeMeta(
            url="/app/run",
            name="run",
            data="summary: Run Agent\ndescription: Execute workflow\nmethod: POST",
            state="alive",
            heartbeat=1700000000.0,
        )

        result = serialize_meta_list([meta])

        parsed = yaml.safe_load(result)
        assert "summary: Run Agent" in parsed[0]["data"]


# =============================================================================
# save_expose_meta Tests
# =============================================================================

class TestSaveExposeMeta:
    """Tests for save_expose_meta function."""

    @pytest.mark.asyncio
    async def test_calls_db_update(self):
        """Test database update is called."""
        metas = [
            ExposeMeta(
                url="/app/run",
                name="run",
                data="summary: Run",
                state="alive",
                heartbeat=1700000000.0,
            ),
        ]

        with patch("kodosumi.service.expose.boot.db.update_expose_meta") as mock_update:
            mock_update.return_value = None

            await save_expose_meta("test-app", metas)

            mock_update.assert_called_once()
            args = mock_update.call_args[0]
            assert args[0] == "test-app"
            # Second arg is YAML string
            assert "url:" in args[1]


# =============================================================================
# _step_update_meta Tests
# =============================================================================

class TestStepUpdateMeta:
    """Tests for _step_update_meta generator function."""

    @pytest.mark.asyncio
    async def test_step_with_no_statuses(self):
        """Test step with empty flow statuses."""
        progress = BootProgress()
        messages = []

        async for msg in _step_update_meta({}, progress):
            messages.append(msg)

        assert len(messages) >= 2
        assert messages[0].msg_type == MessageType.STEP_START
        assert any(m.msg_type == MessageType.WARNING for m in messages)
        assert any(m.msg_type == MessageType.STEP_END for m in messages)

    @pytest.mark.asyncio
    async def test_step_saves_metas(self):
        """Test step saves metas to database."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/run",
            method="POST",
            summary="Run",
            description="",
            tags=[],
        )

        flow_statuses = {
            "test-app": [
                FlowStatus(
                    flow=flow,
                    state="alive",
                    response_code=200,
                    checked_at=time.time(),
                )
            ]
        }

        progress = BootProgress()
        messages = []

        with patch("kodosumi.service.expose.boot.db.update_expose_meta") as mock_update:
            mock_update.return_value = None

            async for msg in _step_update_meta(flow_statuses, progress):
                messages.append(msg)

        # Check message types
        types = [m.msg_type for m in messages]
        assert MessageType.STEP_START in types
        assert MessageType.ACTIVITY in types
        assert MessageType.RESULT in types
        assert MessageType.STEP_END in types

        # Check db was called
        mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_step_counts_flows(self):
        """Test step counts saved and alive flows."""
        flows = [
            DiscoveredFlow(
                app_name="app",
                path="/app/good",
                method="POST",
                summary="Good",
                description="",
                tags=[],
            ),
            DiscoveredFlow(
                app_name="app",
                path="/app/bad",
                method="POST",
                summary="Bad",
                description="",
                tags=[],
            ),
        ]

        flow_statuses = {
            "app": [
                FlowStatus(flow=flows[0], state="alive", response_code=200, checked_at=time.time()),
                FlowStatus(flow=flows[1], state="dead", response_code=500, checked_at=time.time()),
            ]
        }

        progress = BootProgress()
        messages = []

        with patch("kodosumi.service.expose.boot.db.update_expose_meta") as mock_update:
            mock_update.return_value = None

            async for msg in _step_update_meta(flow_statuses, progress):
                messages.append(msg)

        end_msg = next(m for m in messages if m.msg_type == MessageType.STEP_END)
        assert "2 flows saved" in end_msg.message
        assert "1 alive" in end_msg.message

    @pytest.mark.asyncio
    async def test_step_handles_multiple_apps(self):
        """Test step handles multiple apps."""
        flow1 = DiscoveredFlow(
            app_name="app1",
            path="/app1/run",
            method="POST",
            summary="Run",
            description="",
            tags=[],
        )

        flow2 = DiscoveredFlow(
            app_name="app2",
            path="/app2/execute",
            method="POST",
            summary="Execute",
            description="",
            tags=[],
        )

        flow_statuses = {
            "app1": [FlowStatus(flow=flow1, state="alive", response_code=200, checked_at=time.time())],
            "app2": [FlowStatus(flow=flow2, state="alive", response_code=200, checked_at=time.time())],
        }

        progress = BootProgress()
        messages = []

        with patch("kodosumi.service.expose.boot.db.update_expose_meta") as mock_update:
            mock_update.return_value = None

            async for msg in _step_update_meta(flow_statuses, progress):
                messages.append(msg)

        # Should have called db for each app
        assert mock_update.call_count == 2

    @pytest.mark.asyncio
    async def test_step_handles_db_error(self):
        """Test step handles database errors gracefully."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/run",
            method="POST",
            summary="Run",
            description="",
            tags=[],
        )

        flow_statuses = {
            "test-app": [
                FlowStatus(flow=flow, state="alive", response_code=200, checked_at=time.time())
            ]
        }

        progress = BootProgress()
        messages = []

        with patch("kodosumi.service.expose.boot.db.update_expose_meta") as mock_update:
            mock_update.side_effect = Exception("Database error")

            async for msg in _step_update_meta(flow_statuses, progress):
                messages.append(msg)

        # Should have warning about failure
        warnings = [m for m in messages if m.msg_type == MessageType.WARNING]
        assert len(warnings) >= 1
        assert any("Failed to save meta" in m.message for m in warnings)

    @pytest.mark.asyncio
    async def test_step_progress_tracking(self):
        """Test progress is tracked correctly."""
        flow = DiscoveredFlow(
            app_name="test-app",
            path="/test-app/run",
            method="POST",
            summary="Run",
            description="",
            tags=[],
        )

        flow_statuses = {
            "test-app": [
                FlowStatus(flow=flow, state="alive", response_code=200, checked_at=time.time())
            ]
        }

        progress = BootProgress()

        with patch("kodosumi.service.expose.boot.db.update_expose_meta") as mock_update:
            mock_update.return_value = None

            async for msg in _step_update_meta(flow_statuses, progress):
                pass

        assert progress.current_step == 4
        assert progress.step_name == "Update Meta"
        assert progress.activities_done == 1


# =============================================================================
# Integration Tests
# =============================================================================

class TestUpdateMetaIntegration:
    """Integration tests for metadata update."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        """Test full conversion from FlowStatus to database."""
        # Create realistic flow statuses
        flows = [
            DiscoveredFlow(
                app_name="agent-service",
                path="/agent-service/run",
                method="POST",
                summary="Run Agent",
                description="Execute the agent with inputs",
                tags=["agent", "execute"],
                author="team@company.com",
            ),
            DiscoveredFlow(
                app_name="agent-service",
                path="/agent-service/status",
                method="GET",
                summary="Get Status",
                description="Check agent status",
                tags=["status"],
            ),
        ]

        flow_statuses = {
            "agent-service": [
                FlowStatus(flow=flows[0], state="alive", response_code=200, checked_at=time.time()),
                FlowStatus(flow=flows[1], state="alive", response_code=200, checked_at=time.time()),
            ]
        }

        saved_yaml = None

        async def capture_save(name, yaml_str):
            nonlocal saved_yaml
            saved_yaml = yaml_str

        with patch("kodosumi.service.expose.boot.db.update_expose_meta", side_effect=capture_save):
            async for _ in _step_update_meta(flow_statuses, BootProgress()):
                pass

        # Parse saved YAML
        parsed = yaml.safe_load(saved_yaml)
        assert len(parsed) == 2

        # Check first entry
        run_entry = next(e for e in parsed if e["name"] == "run")
        assert run_entry["url"] == "/agent-service/run"
        assert run_entry["state"] == "alive"
        assert "summary: Run Agent" in run_entry["data"]
        assert "author: team@company.com" in run_entry["data"]
