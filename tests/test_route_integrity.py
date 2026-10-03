"""Structural regression guards for defects that break whole features silently.

Two real defects motivated this file:

1. A decorator was separated from its handler by an inserted helper::

       @land_router.get("/{land_id}")

       def _investigation_pipeline_status(land, documents, ...):

   Python applied the decorator to the helper, so ``GET /api/land-records/{id}``
   was served by a function whose five arguments FastAPI read as *required
   query parameters*. Every parcel-detail request answered 422 and roughly 25
   tests failed, while ``compileall`` stayed clean because the file is valid
   Python.

2. ``mapping._ensure_tables`` migrated every document location column except
   ``location_verified_by`` / ``location_verified_at``, so the audited
   exact-pin UPDATE raised ``sqlite3.OperationalError: no such column``.

Both are invisible to a syntax check, so they are pinned here instead.
"""
import inspect
import re

import pytest
from fastapi.routing import APIRoute


def _walk(routes, out=None):
    """Flatten app routes across FastAPI's nested router wrappers."""
    if out is None:
        out = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append(route)
        # FastAPI >= 0.1xx wraps include_router targets instead of flattening.
        included = getattr(getattr(route, "include_context", None), "included_router", None)
        if included is not None:
            _walk(included.routes, out)
        elif hasattr(route, "routes") and not isinstance(route, APIRoute):
            _walk(route.routes, out)
    return out


@pytest.fixture(scope="module")
def api_routes():
    from main import app

    routes = _walk(app.routes)
    assert routes, "the app must expose API routes"
    return routes


def test_every_route_handler_is_a_real_endpoint_not_a_private_helper(api_routes):
    """A route bound to a ``_``-prefixed helper is a misplaced decorator."""
    offenders = [
        f"{sorted(r.methods)} {r.path} -> {r.endpoint.__name__}"
        for r in api_routes
        if r.endpoint.__name__.startswith("_")
    ]
    assert offenders == [], (
        "These routes are bound to private helper functions, which means a "
        "decorator was separated from its intended handler:\n  "
        + "\n  ".join(offenders)
    )


def test_every_path_parameter_exists_in_its_handler_signature(api_routes):
    """Path params absent from the signature degrade into required query params."""
    offenders = []
    for route in api_routes:
        declared = {p.name for p in (route.dependant.path_params or [])}
        signature = set(inspect.signature(route.endpoint).parameters)
        missing = declared - signature
        if missing:
            offenders.append(f"{route.path} -> {route.endpoint.__name__} missing {sorted(missing)}")
    assert offenders == [], (
        "Path parameters are not accepted by their handler, so FastAPI treats "
        "them as required query parameters and the route 422s:\n  "
        + "\n  ".join(offenders)
    )


def test_no_handler_demands_query_params_that_look_like_path_params(api_routes):
    """Guard the exact 422 shape the misplaced land-record decorator produced.

    Works from the endpoint signature rather than FastAPI internals so it does
    not depend on the ModelField API of a particular version.
    """
    for route in api_routes:
        path_params = {p.name for p in (route.dependant.path_params or [])}
        signature = inspect.signature(route.endpoint)
        required_query = [
            name for name, param in signature.parameters.items()
            if name not in path_params
            and param.default is inspect.Parameter.empty
            and param.annotation is not inspect.Parameter.empty
            and not _is_injected_dependency(param)
        ]
        if not required_query:
            continue
        # A GET detail route that requires several unrelated arguments is the
        # signature of a helper having swallowed the decorator.
        assert not (
            "GET" in route.methods
            and len(required_query) >= 3
            and "{" in route.path
        ), (
            f"{route.path} -> {route.endpoint.__name__} requires query params "
            f"{sorted(required_query)}; a helper is likely bound to this route."
        )


def _is_injected_dependency(param) -> bool:
    """True for Depends()/Security() arguments, which are never query params."""
    import typing

    from fastapi.params import Depends

    if isinstance(param.default, Depends):
        return True
    # Annotated[str, Depends(fn)] form: the marker lives in the annotation.
    annotation = getattr(param, "annotation", None)
    if getattr(annotation, "__metadata__", None):
        return any(isinstance(m, Depends) for m in annotation.__metadata__)
    return False


def test_parcel_detail_route_is_bound_to_the_detail_handler(api_routes):
    """Pin the specific route that regressed."""
    matches = [
        r for r in api_routes
        if r.path == "/api/land-records/{land_id}" and "GET" in r.methods
    ]
    assert len(matches) == 1, f"expected exactly one parcel-detail route, got {len(matches)}"
    assert matches[0].endpoint.__name__ == "land_record_detail"


def test_document_location_columns_written_by_code_are_all_migrated():
    """Every documents.location_* column written in SQL must be migrated.

    Reads the migration list from mapping.py and cross-checks it against the
    column names used in ``UPDATE documents SET ...`` statements.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "mapping.py"
    text = source.read_text(encoding="utf-8")

    migrated = set(re.findall(r'_ensure_document_column\(\s*db,\s*"([a-z0-9_]+)"', text))
    assert migrated, "no document column migrations found; the regex drifted"

    written = set()
    for statement in re.findall(r"UPDATE\s+documents\s+SET\s+(.*?)\s+WHERE", text, re.IGNORECASE | re.DOTALL):
        for assignment in statement.split(","):
            name = assignment.split("=")[0].strip().strip('"')
            if re.fullmatch(r"[a-z0-9_]+", name):
                written.add(name)

    # Core columns created by server.init_db rather than by the mapping layer.
    core = {"status", "fields", "validation", "verdict", "updated_at", "reviewer_comments",
            "lat", "lon", "original_fields", "ocr_text", "cleaned_ocr_text"}
    missing = {c for c in written if c not in migrated and c not in core}
    assert not missing, (
        f"documents columns written by mapping.py are never migrated: {sorted(missing)}. "
        "Add them to _ensure_tables via _ensure_document_column."
    )


def test_live_database_has_the_document_location_columns():
    """The migration must actually produce the columns on a real database."""
    import sqlite3

    import mapping
    import server

    mapping._ensure_tables()
    with server.get_db() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)").fetchall()}

    required = {
        "lat", "lon", "map_geometry", "map_geometry_source", "map_geometry_status",
        "map_geometry_updated_at", "location_accuracy_m", "location_source_detail",
        "location_reason", "location_verified_by", "location_verified_at",
    }
    missing = required - columns
    assert not missing, f"documents table is missing columns: {sorted(missing)}"
