"""
Tests for Sumi Protocol Input Schema Endpoint (Phase 4).

Tests MIP-003 input schema conversion and endpoint:
- Kodosumi form element to MIP-003 InputField conversion
- GET /sumi/{expose-name}/{meta-name}/input_schema endpoint
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from kodosumi.service.sumi.schema import (
    convert_element_to_input_field,
    convert_model_to_schema,
    create_empty_schema,
    _convert_validations,
    _convert_data,
    TYPE_MAP,
)
from kodosumi.service.sumi.models import InputField, InputSchemaResponse


# =============================================================================
# Type Mapping Tests
# =============================================================================


class TestTypeMapping:
    """Test Kodosumi to MIP-003 type mapping."""

    def test_text_to_string(self):
        assert TYPE_MAP["text"] == "string"

    def test_password_to_string(self):
        assert TYPE_MAP["password"] == "string"

    def test_textarea_to_string(self):
        assert TYPE_MAP["textarea"] == "string"

    def test_number_to_number(self):
        assert TYPE_MAP["number"] == "number"

    def test_boolean_to_boolean(self):
        assert TYPE_MAP["boolean"] == "boolean"

    def test_select_to_option(self):
        assert TYPE_MAP["select"] == "option"

    def test_file_to_string(self):
        assert TYPE_MAP["file"] == "string"


# =============================================================================
# Validation Conversion Tests
# =============================================================================


class TestConvertValidations:
    """Test _convert_validations helper."""

    def test_empty_element(self):
        result = _convert_validations({})
        assert result is None

    def test_required(self):
        result = _convert_validations({"required": True})
        assert result["required"] is True

    def test_pattern(self):
        result = _convert_validations({"pattern": r"^\d+$"})
        assert result["pattern"] == r"^\d+$"

    def test_min_max_length(self):
        result = _convert_validations({"min_length": 5, "max_length": 100})
        assert result["min_length"] == 5
        assert result["max_length"] == 100

    def test_min_max_value(self):
        result = _convert_validations({"min_value": 0, "max_value": 1000})
        assert result["min"] == 0
        assert result["max"] == 1000

    def test_step(self):
        result = _convert_validations({"step": 0.5})
        assert result["step"] == 0.5

    def test_multiple_validations(self):
        result = _convert_validations({
            "required": True,
            "min_length": 1,
            "max_length": 200,
            "pattern": r".*",
        })
        assert result["required"] is True
        assert result["min_length"] == 1
        assert result["max_length"] == 200
        assert result["pattern"] == r".*"


# =============================================================================
# Data Conversion Tests
# =============================================================================


class TestConvertData:
    """Test _convert_data helper."""

    def test_empty_element(self):
        result = _convert_data({})
        assert result is None

    def test_placeholder(self):
        result = _convert_data({"placeholder": "Enter value..."})
        assert result["placeholder"] == "Enter value..."

    def test_default_value(self):
        result = _convert_data({"value": "default"})
        assert result["default"] == "default"

    def test_select_options(self):
        element = {
            "option": [
                {"name": "opt1", "label": "Option 1", "value": True},
                {"name": "opt2", "label": "Option 2", "value": False},
            ]
        }
        result = _convert_data(element)
        assert "options" in result
        assert len(result["options"]) == 2
        assert result["options"][0]["value"] == "opt1"
        assert result["options"][0]["label"] == "Option 1"
        assert result["options"][0]["selected"] is True

    def test_file_input_type(self):
        result = _convert_data({"type": "file"})
        assert result["input_type"] == "file"

    def test_textarea_input_type(self):
        result = _convert_data({"type": "textarea"})
        assert result["input_type"] == "textarea"

    def test_date_input_type(self):
        result = _convert_data({"type": "date"})
        assert result["input_type"] == "date"


# =============================================================================
# Element Conversion Tests
# =============================================================================


class TestConvertElementToInputField:
    """Test convert_element_to_input_field function."""

    def test_text_input(self):
        element = {
            "type": "text",
            "name": "query",
            "label": "Search Query",
            "required": True,
            "placeholder": "Enter search term...",
        }
        result = convert_element_to_input_field(element)

        assert result is not None
        assert result.id == "query"
        assert result.type == "string"
        assert result.name == "Search Query"
        assert result.validations["required"] is True
        assert result.data["placeholder"] == "Enter search term..."

    def test_number_input(self):
        element = {
            "type": "number",
            "name": "count",
            "label": "Count",
            "min_value": 1,
            "max_value": 100,
            "step": 1,
        }
        result = convert_element_to_input_field(element)

        assert result is not None
        assert result.id == "count"
        assert result.type == "number"
        assert result.validations["min"] == 1
        assert result.validations["max"] == 100

    def test_checkbox(self):
        element = {
            "type": "boolean",
            "name": "agree",
            "label": "I agree to terms",
            "option": "Yes",
        }
        result = convert_element_to_input_field(element)

        assert result is not None
        assert result.id == "agree"
        assert result.type == "boolean"
        assert result.data["option_text"] == "Yes"

    def test_select(self):
        element = {
            "type": "select",
            "name": "language",
            "label": "Language",
            "option": [
                {"name": "en", "label": "English", "value": True},
                {"name": "de", "label": "German", "value": False},
            ],
        }
        result = convert_element_to_input_field(element)

        assert result is not None
        assert result.id == "language"
        assert result.type == "option"
        assert len(result.data["options"]) == 2

    def test_textarea(self):
        element = {
            "type": "textarea",
            "name": "description",
            "label": "Description",
            "rows": 5,
            "cols": 50,
            "max_length": 1000,
        }
        result = convert_element_to_input_field(element)

        assert result is not None
        assert result.id == "description"
        assert result.type == "string"
        assert result.data["input_type"] == "textarea"
        assert result.validations["rows"] == 5
        assert result.validations["max_length"] == 1000

    def test_file_input(self):
        element = {
            "type": "file",
            "name": "documents",
            "label": "Upload Documents",
            "multiple": True,
        }
        result = convert_element_to_input_field(element)

        assert result is not None
        assert result.id == "documents"
        assert result.type == "string"
        assert result.data["input_type"] == "file"
        assert result.validations["multiple"] is True

    def test_skip_non_input_elements(self):
        assert convert_element_to_input_field({"type": "html"}) is None
        assert convert_element_to_input_field({"type": "markdown"}) is None
        assert convert_element_to_input_field({"type": "submit"}) is None
        assert convert_element_to_input_field({"type": "cancel"}) is None
        assert convert_element_to_input_field({"type": "action"}) is None
        assert convert_element_to_input_field({"type": "errors"}) is None

    def test_skip_elements_without_name(self):
        result = convert_element_to_input_field({
            "type": "text",
            "label": "No name field",
        })
        assert result is None


# =============================================================================
# Model Conversion Tests
# =============================================================================


class TestConvertModelToSchema:
    """Test convert_model_to_schema function."""

    def test_empty_elements(self):
        result = convert_model_to_schema([])
        assert result.input_data is None
        assert result.input_groups is None

    def test_single_field(self):
        elements = [
            {"type": "text", "name": "query", "label": "Query"},
        ]
        result = convert_model_to_schema(elements)

        assert result.input_data is not None
        assert len(result.input_data) == 1
        assert result.input_data[0].id == "query"

    def test_multiple_fields(self):
        elements = [
            {"type": "text", "name": "name", "label": "Name"},
            {"type": "number", "name": "age", "label": "Age"},
            {"type": "boolean", "name": "subscribe", "label": "Subscribe"},
        ]
        result = convert_model_to_schema(elements)

        assert result.input_data is not None
        assert len(result.input_data) == 3
        assert result.input_data[0].id == "name"
        assert result.input_data[1].id == "age"
        assert result.input_data[2].id == "subscribe"

    def test_filters_non_input_elements(self):
        elements = [
            {"type": "html", "text": "<h1>Form</h1>"},
            {"type": "text", "name": "field1", "label": "Field 1"},
            {"type": "markdown", "text": "Some text"},
            {"type": "number", "name": "field2", "label": "Field 2"},
            {"type": "submit", "text": "Submit"},
        ]
        result = convert_model_to_schema(elements)

        assert result.input_data is not None
        assert len(result.input_data) == 2
        assert result.input_data[0].id == "field1"
        assert result.input_data[1].id == "field2"


class TestCreateEmptySchema:
    """Test create_empty_schema function."""

    def test_creates_empty_schema(self):
        result = create_empty_schema()
        assert isinstance(result, InputSchemaResponse)
        assert result.input_data is None
        assert result.input_groups is None


# =============================================================================
# InputField Model Tests
# =============================================================================


class TestInputFieldModel:
    """Test InputField model creation."""

    def test_minimal(self):
        field = InputField(id="test", type="string")
        assert field.id == "test"
        assert field.type == "string"
        assert field.name is None
        assert field.data is None
        assert field.validations is None

    def test_full(self):
        field = InputField(
            id="query",
            type="string",
            name="Search Query",
            data={"placeholder": "Enter term", "description": "Search term"},
            validations={"required": True, "min_length": 1},
        )
        assert field.id == "query"
        assert field.type == "string"
        assert field.name == "Search Query"
        assert field.data["placeholder"] == "Enter term"
        assert field.validations["required"] is True


# =============================================================================
# InputSchemaResponse Model Tests
# =============================================================================


class TestInputSchemaResponseModel:
    """Test InputSchemaResponse model."""

    def test_empty(self):
        schema = InputSchemaResponse()
        assert schema.input_data is None
        assert schema.input_groups is None

    def test_with_input_data(self):
        fields = [
            InputField(id="f1", type="string"),
            InputField(id="f2", type="number"),
        ]
        schema = InputSchemaResponse(input_data=fields)
        assert schema.input_data is not None
        assert len(schema.input_data) == 2

    def test_serialization(self):
        field = InputField(
            id="query",
            type="string",
            name="Query",
            data={"placeholder": "Enter..."},
            validations={"required": True},
        )
        schema = InputSchemaResponse(input_data=[field])
        data = schema.model_dump()

        assert data["input_data"] is not None
        assert len(data["input_data"]) == 1
        assert data["input_data"][0]["id"] == "query"
