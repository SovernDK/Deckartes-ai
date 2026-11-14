"""Builds the definition the model sees for a tool, from its name, type hints and docstring.

A parameter is required unless its type allows None, even when it has a default. A tool's
`confirm` parameter is left out: tools.run_tool passes it, never the model.
"""

import inspect
import types
import typing
from collections.abc import Callable
from functools import cache

from docstring_parser import DocstringStyle, parse

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}

def _json_types(annotation) -> set[str]:
    """JSON type names of an annotation; "null" among them means it allows None."""
    if annotation is inspect.Parameter.empty:
        return {"string"}
    if annotation is None or annotation is type(None):
        return {"null"}
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return set().union(*(_json_types(option) for option in typing.get_args(annotation)))
    return {_JSON_TYPES.get(origin or annotation, "string")}

def _parse_docstring(docstring: str) -> tuple[str, dict[str, str]]:
    """The description before Args:, and each argument's description on one line."""
    doc = parse(docstring, style=DocstringStyle.GOOGLE)
    return doc.description or "", {param.arg_name: " ".join((param.description or "").split())
                                    for param in doc.params}

# The parameter through which tools.run_tool hands a tool the turn's way of asking the user
CONFIRM = "confirm"

def model_signature(function: Callable) -> inspect.Signature:
    signature = inspect.signature(function)
    return signature.replace(parameters=[parameter for parameter in signature.parameters.values() if parameter.name != CONFIRM])

@cache
def tool_definition(function: Callable) -> dict:
    """The OpenAI-format definition of a tool function."""
    description, arguments = _parse_docstring(inspect.getdoc(function) or "")
    hints = typing.get_type_hints(function)
    properties, required = {}, []
    for name in model_signature(function).parameters:
        json_types = _json_types(hints.get(name, inspect.Parameter.empty))
        if "null" not in json_types:
            required.append(name)
        json_types.discard("null")
        properties[name] = {"description": arguments.get(name, ""), "type": ", ".join(sorted(json_types))}
    return {"type": "function", "function": {
        "name": function.__name__,
        "description": description,
        "parameters": {"type": "object", "required": required, "properties": properties},
    }}
