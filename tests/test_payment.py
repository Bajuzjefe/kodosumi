"""
Tests for the Masumi payment integration module.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from kodosumi.runner.payment import (
    MasumiClient,
    PaymentError,
    PaymentTimeoutError,
    PaymentInitError,
    PaymentSubmitError,
    create_result_hash,
)
from kodosumi.config import Settings


class TestCreateResultHash:
    """Tests for create_result_hash function."""

    def test_none_result(self):
        """Hash of None should be consistent."""
        result = create_result_hash(None)
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex

    def test_string_result(self):
        """Hash of string should be consistent."""
        result1 = create_result_hash("test")
        result2 = create_result_hash("test")
        assert result1 == result2
        assert len(result1) == 64

    def test_dict_result(self):
        """Hash of dict should be deterministic (sorted keys)."""
        result1 = create_result_hash({"b": 2, "a": 1})
        result2 = create_result_hash({"a": 1, "b": 2})
        assert result1 == result2

    def test_different_results_different_hashes(self):
        """Different results should produce different hashes."""
        hash1 = create_result_hash("result1")
        hash2 = create_result_hash("result2")
        assert hash1 != hash2


class TestMasumiClientConfig:
    """Tests for MasumiClient configuration."""

    def test_client_uses_settings(self):
        """Client should use values from Settings."""
        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"
        settings.MASUMI_PAY_BY = 45
        settings.MASUMI_SUBMIT_RESULT = 44
        settings.MASUMI_POLL_INTERVAL = 3.0

        client = MasumiClient(settings)

        assert client.base_url == "https://test.masumi.network/api/v1"
        assert client.token == "test-token"
        assert client.pay_by_minutes == 45
        assert client.submit_result_minutes == 44
        assert client.poll_interval == 3.0

    def test_base_url_trailing_slash_stripped(self):
        """Base URL trailing slash should be stripped."""
        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1/"

        client = MasumiClient(settings)

        assert client.base_url == "https://test.masumi.network/api/v1"


class TestMasumiClientHeaders:
    """Tests for MasumiClient header generation."""

    def test_headers_with_token(self):
        """Headers should include token when configured."""
        settings = Settings()
        settings.MASUMI_TOKEN = "my-secret-token"

        client = MasumiClient(settings)
        headers = client._get_headers()

        assert headers["token"] == "my-secret-token"
        assert headers["accept"] == "application/json"
        assert headers["Content-Type"] == "application/json"

    def test_headers_without_token(self):
        """Headers should have empty token when not configured."""
        settings = Settings()
        settings.MASUMI_TOKEN = None

        client = MasumiClient(settings)
        headers = client._get_headers()

        assert headers["token"] == ""


class TestMasumiClientDeadlines:
    """Tests for deadline calculation."""

    def test_deadlines_are_iso_format(self):
        """Deadlines should be in ISO format."""
        settings = Settings()
        settings.MASUMI_PAY_BY = 30
        settings.MASUMI_SUBMIT_RESULT = 29

        client = MasumiClient(settings)
        pay_by, submit_by = client._calculate_deadlines()

        # Should be parseable as ISO format
        datetime.fromisoformat(pay_by.replace("Z", "+00:00"))
        datetime.fromisoformat(submit_by.replace("Z", "+00:00"))

    def test_submit_deadline_before_pay_deadline(self):
        """Submit deadline should be before pay deadline."""
        settings = Settings()
        settings.MASUMI_PAY_BY = 30
        settings.MASUMI_SUBMIT_RESULT = 29

        client = MasumiClient(settings)
        pay_by, submit_by = client._calculate_deadlines()

        pay_dt = datetime.fromisoformat(pay_by.replace("Z", "+00:00"))
        submit_dt = datetime.fromisoformat(submit_by.replace("Z", "+00:00"))

        assert submit_dt < pay_dt


class TestMasumiClientInitPayment:
    """Tests for init_payment method."""

    @pytest.mark.asyncio
    async def test_init_payment_success(self):
        """Successful payment init should return response."""
        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"

        client = MasumiClient(settings)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "blockchainIdentifier": "abc123",
                "payByTime": "2025-01-01T00:00:00.000Z"
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = await client.init_payment(
                agent_identifier="agent123",
                network="Preprod",
                input_hash="hash123",
                identifier_from_purchaser="purchaser123",
            )

            assert result["data"]["blockchainIdentifier"] == "abc123"

    @pytest.mark.asyncio
    async def test_init_payment_http_error(self):
        """HTTP error should raise PaymentInitError."""
        import httpx

        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"

        client = MasumiClient(settings)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad request"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client.post.return_value.raise_for_status.side_effect = (
                httpx.HTTPStatusError(
                    "Error", request=MagicMock(), response=mock_response
                )
            )
            mock_client_class.return_value = mock_client

            with pytest.raises(PaymentInitError) as exc_info:
                await client.init_payment(
                    agent_identifier="agent123",
                    network="Preprod",
                    input_hash="hash123",
                    identifier_from_purchaser="purchaser123",
                )

            assert "400" in str(exc_info.value)


class TestMasumiClientGetPaymentStatus:
    """Tests for get_payment_status method."""

    @pytest.mark.asyncio
    async def test_payment_found(self):
        """Should return payment when found."""
        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"

        client = MasumiClient(settings)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "Payments": [
                    {"blockchainIdentifier": "abc123", "onChainState": "FundsLocked"},
                    {"blockchainIdentifier": "other", "onChainState": "Pending"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = await client.get_payment_status("abc123", "Preprod")

            assert result is not None
            assert result["onChainState"] == "FundsLocked"

    @pytest.mark.asyncio
    async def test_payment_not_found(self):
        """Should return None when payment not found."""
        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"

        client = MasumiClient(settings)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "Payments": [
                    {"blockchainIdentifier": "other", "onChainState": "Pending"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.get.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = await client.get_payment_status("abc123", "Preprod")

            assert result is None


class TestMasumiClientSubmitResult:
    """Tests for submit_result method."""

    @pytest.mark.asyncio
    async def test_submit_success(self):
        """Successful submit should return response."""
        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"

        client = MasumiClient(settings)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = await client.submit_result(
                blockchain_identifier="abc123",
                network="Preprod",
                result_hash="hash123",
            )

            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_submit_http_error(self):
        """HTTP error should raise PaymentSubmitError."""
        import httpx

        settings = Settings()
        settings.MASUMI_BASE_URL = "https://test.masumi.network/api/v1"
        settings.MASUMI_TOKEN = "test-token"

        client = MasumiClient(settings)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server error"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client.post.return_value.raise_for_status.side_effect = (
                httpx.HTTPStatusError(
                    "Error", request=MagicMock(), response=mock_response
                )
            )
            mock_client_class.return_value = mock_client

            with pytest.raises(PaymentSubmitError) as exc_info:
                await client.submit_result(
                    blockchain_identifier="abc123",
                    network="Preprod",
                    result_hash="hash123",
                )

            assert "500" in str(exc_info.value)
