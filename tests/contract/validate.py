"""A minimal JSON Schema (2020-12) validator for exactly the vocabulary
`contract/v1/schema/*.json` uses: `type`, `enum`, `const`, `pattern`, `minimum`,
`maximum`, `maxLength`, `minItems`, `maxItems`, `required`, `properties`,
`additionalProperties`, `items`, `$ref`/`$defs`, `allOf`, `if`/`then`/`else`, and
the boolean schemas `true`/`false`.

Not a general-purpose validator - CONTRACT.md documents that the contract itself
never uses `$data`, custom keywords, or `format` (build note for implementers,
ajv standalone), so this only needs to cover what is actually there. It exists so
the client's own tests can prove its projection validates without adding a
runtime dependency for a test-only need.
"""

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SchemaError:
    pointer: str
    keyword: str
    message: str


Schema = dict[str, Any] | bool


def _check_type(expected: str, instance: object, pointer: str) -> list[SchemaError]:
    checks = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, int | float) and not isinstance(instance, bool),
        "boolean": isinstance(instance, bool),
    }
    if checks.get(expected, True):
        return []
    return [SchemaError(pointer, "type", f"expected {expected}, got {type(instance).__name__}")]


def validate(
    schema: Schema, instance: object, defs: dict[str, Any], pointer: str = ""
) -> list[SchemaError]:
    """Validate `instance` against `schema`, returning every violation found.

    Each keyword is applied independently and gated on the instance's own runtime
    type, matching real JSON Schema semantics: `required` on a non-object instance
    is vacuously satisfied, not an error, which is what makes the `if` conditionals
    in the utterance schema work as pure predicates.
    """
    if schema is True:
        return []
    if schema is False:
        return [SchemaError(pointer, "false", "no instance is valid against `false`")]

    errors: list[SchemaError] = []

    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return validate(defs[name], instance, defs, pointer)

    if "type" in schema:
        errors += _check_type(schema["type"], instance, pointer)

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(SchemaError(pointer, "enum", f"value must be one of {schema['enum']}"))

    if "const" in schema and instance != schema["const"]:
        errors.append(SchemaError(pointer, "const", f"value must equal {schema['const']!r}"))

    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(SchemaError(pointer, "pattern", "does not match the required pattern"))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(SchemaError(pointer, "maxLength", "string is too long"))

    if isinstance(instance, int | float) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(SchemaError(pointer, "minimum", f"must be >= {schema['minimum']}"))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(SchemaError(pointer, "maximum", f"must be <= {schema['maximum']}"))

    if isinstance(instance, dict):
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(
                    SchemaError(f"{pointer}/{required}", "required", f"{required} is missing")
                )
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors += validate(properties[key], value, defs, f"{pointer}/{key}")
            elif additional is False:
                errors.append(
                    SchemaError(f"{pointer}/{key}", "additionalProperties", f"{key} not allowed")
                )
            elif isinstance(additional, dict):
                errors += validate(additional, value, defs, f"{pointer}/{key}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(SchemaError(pointer, "minItems", "too few items"))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(SchemaError(pointer, "maxItems", "too many items"))
        if "items" in schema:
            for index, item in enumerate(instance):
                errors += validate(schema["items"], item, defs, f"{pointer}/{index}")

    for sub in schema.get("allOf", []):
        errors += validate(sub, instance, defs, pointer)

    if "if" in schema:
        if not validate(schema["if"], instance, defs, pointer):
            if "then" in schema:
                errors += validate(schema["then"], instance, defs, pointer)
        elif "else" in schema:
            errors += validate(schema["else"], instance, defs, pointer)

    return errors


def validate_document(schema: dict[str, Any], instance: object) -> list[SchemaError]:
    """Validate a full schema document (with its own `$defs`) against one instance."""
    return validate(schema, instance, schema.get("$defs", {}))
