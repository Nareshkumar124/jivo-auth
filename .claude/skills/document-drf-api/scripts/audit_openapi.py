#!/usr/bin/env python
"""
Audit a drf-spectacular OpenAPI schema for documentation problems.

Generate the schema first, then run this with the project's Python (it needs
`jsonschema`, which drf-spectacular already depends on):

    python manage.py spectacular --format openapi-json --file /tmp/openapi.json
    python audit_openapi.py /tmp/openapi.json
    python audit_openapi.py /tmp/openapi.json \\
        --public "POST /api/v1/auth/login/" "GET /api/v1/health/" \\
        --scheme appKey "GET /api/v1/apps/users/"

It prints one line per operation (tag, security, summary, request and response
components) followed by the problems it found. Exit status is 1 when there
are errors, so it can be used in CI.

--public lists the operations that must have no security requirement.
--scheme NAME OPS... (repeatable) lists operations that must require exactly
that scheme. With --public, every other operation must require some scheme.

Checks:
  errors    global `security` block, security requirements that reference an
            undefined scheme, optional auth (`{}` alternative), missing
            tags/summary/description, protected operation without a 401
            response, examples that don't match their schema, --public and
            --scheme mismatches
  warnings  several alternative schemes on one operation, operations without
            any examples, 2xx responses without a body schema (other than
            204), a 2xx response documented with the request's component
"""

import argparse
import json
import sys

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def load(path):
    with open(path) as handle:
        return json.load(handle)


def to_json_schema(node, components, seen=()):
    """OpenAPI 3.0 schema -> JSON Schema: inline $refs, map `nullable`."""

    if isinstance(node, list):
        return [to_json_schema(item, components, seen) for item in node]

    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        name = node["$ref"].split("/")[-1]

        if name in seen:  # recursive component; don't expand forever
            return {}

        return to_json_schema(components.get(name, {}), components, seen + (name,))

    schema = {
        key: to_json_schema(value, components, seen)
        for key, value in node.items()
        if key not in ("nullable", "readOnly", "writeOnly", "example", "discriminator")
    }

    if node.get("nullable"):
        schema = {"anyOf": [schema, {"type": "null"}]}

    return schema


def ref_name(schema):
    if not isinstance(schema, dict):
        return None

    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]

    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        inner = ref_name(schema["items"])
        return f"[{inner}]" if inner else "[inline]"

    return "inline" if schema else None


def json_content(body):
    content = (body or {}).get("content", {})

    for media_type, value in content.items():
        if "json" in media_type:
            return value

    return next(iter(content.values()), None)


def security_label(operation):
    if "security" not in operation:
        return "PUBLIC"

    parts = []

    for requirement in operation["security"]:
        parts.append("+".join(requirement) if requirement else "{}")

    return " | ".join(parts) or "PUBLIC"


def audit(spec, public=None, schemes_required=None):
    errors, warnings, rows = [], [], []
    schemes_required = schemes_required or {}

    components = spec.get("components", {}).get("schemas", {})
    schemes = spec.get("components", {}).get("securitySchemes", {})

    if spec.get("security"):
        errors.append(
            "global `security` is set; it applies to every operation (public "
            "ones included). Remove SPECTACULAR_SETTINGS['SECURITY'] and let "
            "operations get security from their authentication classes."
        )

    required_by_op = {
        op: name
        for name, ops in schemes_required.items()
        for op in ops
    }

    seen_ops = set()

    for path, item in spec.get("paths", {}).items():
        for method, operation in item.items():
            if method not in METHODS:
                continue

            op = f"{method.upper()} {path}"
            seen_ops.add(op)

            request = json_content(operation.get("requestBody"))
            request_ref = ref_name(request.get("schema")) if request else None
            responses = operation.get("responses", {})

            rows.append(
                (
                    op,
                    ",".join(operation.get("tags", [])) or "-",
                    security_label(operation),
                    operation.get("summary") or "-",
                    request_ref or "-",
                    " ".join(
                        f"{code}:{ref_name((json_content(resp) or {}).get('schema')) or '-'}"
                        for code, resp in responses.items()
                    ),
                )
            )

            for field in ("tags", "summary", "description"):
                if not operation.get(field):
                    errors.append(f"{op}: missing {field}")

            security = operation.get("security")

            if security is not None:
                names = [name for requirement in security for name in requirement]

                for name in names:
                    if name not in schemes:
                        errors.append(
                            f"{op}: security references undefined scheme `{name}`"
                        )

                if {} in security and names:
                    errors.append(
                        f"{op}: auth is shown as optional ({{}} alternative). "
                        "Public endpoints should use extend_schema(auth=[])."
                    )

                if len(set(names)) > 1:
                    warnings.append(
                        f"{op}: several alternative security schemes {sorted(set(names))}; "
                        "fine only if the endpoint really accepts each one"
                    )

                if names and {} not in security and "401" not in responses:
                    errors.append(f"{op}: protected but no 401 response documented")

            if op in required_by_op:
                expected = [{required_by_op[op]: []}]

                if security != expected:
                    errors.append(
                        f"{op}: should require only `{required_by_op[op]}`, "
                        f"documents {security_label(operation)}"
                    )

            elif public is not None:
                if op in public and security:
                    errors.append(f"{op}: listed as public but requires {security_label(operation)}")

                if op not in public and not security:
                    errors.append(f"{op}: not listed as public but has no security requirement")

            example_count = len((request or {}).get("examples", {}))

            for code, response in responses.items():
                content = json_content(response)

                if code.startswith("2") and code != "204" and not (content or {}).get("schema"):
                    warnings.append(f"{op}: {code} response has no body schema")

                if not content:
                    continue

                example_count += len(content.get("examples", {}))

                if (
                    code.startswith("2")
                    and request_ref
                    and request_ref not in ("inline", "[inline]")
                    and ref_name(content.get("schema")) == request_ref
                ):
                    warnings.append(
                        f"{op}: {code} response uses the request component "
                        f"`{request_ref}`; check the response really has that "
                        "shape (SimpleJWT token views often don't)"
                    )

            if not example_count:
                warnings.append(f"{op}: no examples")

            if jsonschema is None:
                continue

            bodies = [("request", request)] if request else []
            bodies += [(code, json_content(resp)) for code, resp in responses.items()]

            for where, content in bodies:
                if not content or "schema" not in content:
                    continue

                schema = to_json_schema(content["schema"], components)

                for name, example in content.get("examples", {}).items():
                    if "value" not in example:
                        continue

                    try:
                        jsonschema.validate(example["value"], schema)
                    except jsonschema.ValidationError as exc:
                        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
                        errors.append(
                            f"{op}: {where} example `{name}` doesn't match its "
                            f"schema at {location}: {exc.message}"
                        )

    listed = set(public or ()) | set(required_by_op)

    for op in sorted(listed - seen_ops):
        errors.append(f"{op}: listed on the command line but not in the schema")

    if jsonschema is None:
        warnings.append("jsonschema not installed; examples were not checked")

    return rows, errors, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("schema", help="OpenAPI JSON from `manage.py spectacular --format openapi-json`")
    parser.add_argument(
        "--public",
        nargs="*",
        metavar="'METHOD /path/'",
        help="operations that must be public; every other one must require auth",
    )
    parser.add_argument(
        "--scheme",
        nargs="+",
        action="append",
        default=[],
        metavar="NAME 'METHOD /path/'",
        help="a security scheme name followed by the operations that must require only it (repeatable)",
    )
    parser.add_argument("--quiet", action="store_true", help="only print problems")
    args = parser.parse_args()

    spec = load(args.schema)
    public = {op.strip() for op in args.public} if args.public is not None else None
    schemes_required = {name: {op.strip() for op in ops} for name, *ops in args.scheme}
    rows, errors, warnings = audit(spec, public, schemes_required)

    if not args.quiet:
        width = max((len(row[0]) for row in rows), default=0)

        for op, tags, security, summary, request, responses in rows:
            print(f"{op:<{width}}  [{tags}] {security}  \"{summary}\"")
            print(f"{'':<{width}}  req={request}  {responses}")

        defined = ", ".join(spec.get("components", {}).get("securitySchemes", {})) or "none"
        print(f"\n{len(rows)} operations; security schemes: {defined}")

    for message in errors:
        print(f"ERROR   {message}")

    for message in warnings:
        print(f"WARNING {message}")

    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
