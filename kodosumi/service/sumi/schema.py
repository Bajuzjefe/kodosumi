"""
MIP-003 Input Schema Conversion.

Converts Kodosumi form elements to MIP-003 InputField format.
"""

from typing import Any, Dict, List, Optional

from kodosumi.service.sumi.models import InputField, InputGroup, InputSchemaResponse


# Kodosumi type to MIP-003 type mapping
TYPE_MAP = {
    "text": "string",
    "password": "string",
    "textarea": "string",
    "number": "number",
    "date": "string",
    "time": "string",
    "datetime-local": "string",
    "boolean": "boolean",
    "option": "option",
    "select": "option",
    "file": "string",
}


def _convert_validations(element: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert Kodosumi element validations to MIP-003 format."""
    validations = {}

    if element.get("required"):
        validations["required"] = True

    if element.get("pattern"):
        validations["pattern"] = element["pattern"]

    if element.get("size"):
        validations["max_length"] = element["size"]

    if element.get("min_length"):
        validations["min_length"] = element["min_length"]

    if element.get("max_length"):
        validations["max_length"] = element["max_length"]

    if element.get("min_value") is not None:
        validations["min"] = element["min_value"]

    if element.get("max_value") is not None:
        validations["max"] = element["max_value"]

    if element.get("min_date"):
        validations["min_date"] = element["min_date"]

    if element.get("max_date"):
        validations["max_date"] = element["max_date"]

    if element.get("min_time"):
        validations["min_time"] = element["min_time"]

    if element.get("max_time"):
        validations["max_time"] = element["max_time"]

    if element.get("min_datetime"):
        validations["min_datetime"] = element["min_datetime"]

    if element.get("max_datetime"):
        validations["max_datetime"] = element["max_datetime"]

    if element.get("step"):
        validations["step"] = element["step"]

    if element.get("rows"):
        validations["rows"] = element["rows"]

    if element.get("cols"):
        validations["cols"] = element["cols"]

    if element.get("multiple"):
        validations["multiple"] = element["multiple"]

    if element.get("directory"):
        validations["directory"] = element["directory"]

    return validations if validations else None


def _convert_data(element: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert Kodosumi element data to MIP-003 format."""
    data = {}

    if element.get("placeholder"):
        data["placeholder"] = element["placeholder"]

    if element.get("value"):
        data["default"] = element["value"]

    # For checkbox, include the option text (option is a string)
    if element.get("type") == "boolean" and element.get("option"):
        data["option_text"] = element["option"]
    # For select type, include options (option is a list of dicts)
    elif element.get("option") and isinstance(element.get("option"), list):
        options = []
        for opt in element["option"]:
            if isinstance(opt, dict):
                options.append({
                    "value": opt.get("name"),
                    "label": opt.get("label", opt.get("name")),
                    "selected": opt.get("value", False),
                })
        if options:
            data["options"] = options

    # Mark file inputs
    if element.get("type") == "file":
        data["input_type"] = "file"

    # Mark textarea
    if element.get("type") == "textarea":
        data["input_type"] = "textarea"

    # Mark date/time types
    if element.get("type") in ("date", "time", "datetime-local"):
        data["input_type"] = element["type"]

    return data if data else None


def convert_element_to_input_field(element: Dict[str, Any]) -> Optional[InputField]:
    """
    Convert a Kodosumi form element to MIP-003 InputField.

    Args:
        element: Kodosumi form element dict (from to_dict())

    Returns:
        InputField or None if not a form input element
    """
    elem_type = element.get("type", "")

    # Skip non-input elements
    if elem_type in ("html", "markdown", "submit", "cancel", "action", "errors", "break", "hr"):
        return None

    # Must have a name to be an input
    name = element.get("name")
    if not name:
        return None

    # Map to MIP-003 type
    mip_type = TYPE_MAP.get(elem_type, "string")

    return InputField(
        id=name,
        type=mip_type,
        name=element.get("label"),
        data=_convert_data(element),
        validations=_convert_validations(element),
    )


def convert_model_to_schema(elements: List[Dict[str, Any]]) -> InputSchemaResponse:
    """
    Convert Kodosumi Model elements to MIP-003 InputSchemaResponse.

    Args:
        elements: List of form element dicts (from Model.get_model())

    Returns:
        MIP-003 InputSchemaResponse
    """
    input_fields = []

    for elem in elements:
        field = convert_element_to_input_field(elem)
        if field:
            input_fields.append(field)

    return InputSchemaResponse(
        input_data=input_fields if input_fields else None,
        input_groups=None,
    )


def create_empty_schema() -> InputSchemaResponse:
    """Create an empty schema response (no inputs required)."""
    return InputSchemaResponse(
        input_data=None,
        input_groups=None,
    )
