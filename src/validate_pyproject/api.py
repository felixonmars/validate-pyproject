"""
Retrieve JSON schemas for validating dicts representing a ``pyproject.toml`` file.
"""
import json
import logging
import sys
from enum import Enum
from functools import reduce
from itertools import chain
from types import MappingProxyType
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
)

import fastjsonschema as FJS

from . import errors, format
from .extra_validations import EXTRA_VALIDATIONS
from .types import FormatValidationFn, Plugin, Schema, ValidationFn

if sys.version_info[:2] >= (3, 7):  # pragma: no cover
    # TODO: Import directly (no need for conditional) when `python_requires = >= 3.7`
    from importlib.resources import read_text
else:  # pragma: no cover
    try:
        from pkgutil import get_data  # pragma: no cover
    except ImportError as ex:
        msg = "Please install `setuptools` or `importlib_metadata`"
        raise ImportError(msg) from ex

    # The following "polyfill" is taken from PyScaffold (licensed under MIT)
    # https://github.com/pyscaffold/pyscaffold/blob/master/LICENSE.txt
    # https://github.com/pyscaffold/pyscaffold/blob/609f548574618834e6056997aff411b43a24e3fb/src/pyscaffold/templates/__init__.py#L27

    def read_text(package, resource, encoding="utf-8") -> str:  # pragma: no cover
        data = get_data(package, resource)
        if data is None:
            raise FileNotFoundError(f"{resource!r} resource not found in {package!r}")
        return data.decode(encoding)


T = TypeVar("T", bound=Mapping)
AllPlugins = Enum("AllPlugins", "ALL_PLUGINS")
ALL_PLUGINS = AllPlugins.ALL_PLUGINS

TOP_LEVEL_SCHEMA = "pyproject_toml"
PROJECT_TABLE_SCHEMA = "pep621_project"

FORMAT_FUNCTIONS: Mapping[str, FormatValidationFn] = MappingProxyType(
    {
        fn.__name__.replace("_", "-"): fn
        for fn in format.__dict__.values()
        if callable(fn) and not fn.__name__.startswith("_")
    }
)

_logger = logging.getLogger(__name__)
_chain_iter = chain.from_iterable


def load(name: str, package: str = __package__, ext: str = ".schema.json") -> Schema:
    """Load the schema from a JSON Schema file.
    The returned dict-like object is immutable.
    """
    return Schema(MappingProxyType(json.loads(read_text(package, f"{name}{ext}"))))


def plugin_id(plugin: Plugin):
    return f"{plugin.__module__}.{plugin.__class__.__qualname__}"


class SchemaRegistry(Mapping[str, Schema]):
    """Repository of parsed JSON Schemas used for validating a ``pyproject.toml``.

    During instantiation the schemas equivalent to PEP 517, PEP 518 and PEP 621
    will be combined with the schemas for the ``tool`` subtables provided by the
    plugins.

    Since this object work as a mapping between each schema ``$id`` and the schema
    itself, all schemas provided by plugins **MUST** have a top level ``$id``.
    """

    def __init__(self, plugins: Sequence[Plugin] = ()):
        self._schemas: Dict[str, Tuple[str, str, Schema]] = {}
        # (which part of the TOML, who defines, schema)

        top_level = dict(load(TOP_LEVEL_SCHEMA))  # Make it mutable
        self._spec_version = top_level["$schema"]
        top_properties = top_level["properties"]
        tool_properties = top_properties["tool"].setdefault("properties", {})

        # Add PEP 621
        project_table_schema = load(PROJECT_TABLE_SCHEMA)
        self._ensure_compatibility(PROJECT_TABLE_SCHEMA, project_table_schema)
        sid = project_table_schema["$id"]
        top_level["project"] = {"$ref": sid}
        self._schemas = {sid: ("project", f"{__name__} - PEP621", project_table_schema)}

        # Add tools using Plugins

        for plugin in plugins:
            pid, tool, schema = plugin_id(plugin), plugin.tool_name, plugin.tool_schema
            if tool in tool_properties:
                _logger.warning(f"{pid} overwrites `tool.{tool}` schema")
            else:
                _logger.info(f"{pid} defines `tool.{tool}` schema")
            sid = self._ensure_compatibility(tool, schema)["$id"]
            tool_properties[tool] = {"$ref": sid}
            self._schemas[sid] = (f"tool.{tool}", pid, schema)

        self._main_id = sid = top_level["$id"]
        main_schema = Schema(MappingProxyType(top_level))  # make it immutable
        self._schemas[sid] = ("<$ROOT>", f"{__name__} - PEP517/518", main_schema)

    @property
    def spec_version(self) -> str:
        """Version of the JSON Schema spec in use"""
        return self._spec_version

    @property
    def main(self) -> Schema:
        """Top level schema for validating a ``pyproject.toml`` file"""
        return self._schemas[self._main_id][-1]

    def _ensure_compatibility(self, reference: str, schema: Schema) -> Schema:
        if "$id" not in schema:
            raise errors.SchemaMissingId(reference)
        version = schema.get("$schema")
        if version and version != self.spec_version:
            raise errors.InvalidSchemaVersion(reference, version, self.spec_version)
        sid = schema["$id"]
        if sid in self._schemas:
            raise errors.SchemaWithDuplicatedId(sid)
        return schema

    def __getitem__(self, key: str) -> Schema:
        return self._schemas[key][-1]

    def __iter__(self) -> Iterator[str]:
        return iter(self._schemas)

    def __len__(self) -> int:
        return len(self._schemas)


class RefHandler(Mapping[str, Callable[[str], Schema]]):
    """:mod:`fastjsonschema` allows passing a dict-like object to load external schema
    ``$ref``s. Such objects map the URI schema (e.g. ``http``, ``https``, ``ftp``)
    into a function that receives the schema URI and returns the schema (as parsed JSON)
    (otherwise :mod:`urllib` is used and the URI is assumed to be a valid URL).
    This class will ensure all the URIs are loaded from the local registry.
    """

    def __init__(self, registry: Mapping[str, Schema]):
        self._uri_schemas = ["http", "https"]
        self._registry = registry

    def __contains__(self, key) -> bool:
        if not isinstance(key, str):
            return False
        if key not in self._uri_schemas:
            self._uri_schemas.append(key)
        return True

    def __iter__(self) -> Iterator[str]:
        return iter(self._uri_schemas)

    def __len__(self):
        return len(self._uri_schemas)

    def __getitem__(self, key: str) -> Callable[[str], Schema]:
        """All the references should be retrieved from the registry"""
        return self._registry.__getitem__


class Validator:
    def __init__(
        self,
        plugins: Union[Sequence[Plugin], AllPlugins] = ALL_PLUGINS,
        format_validators: Mapping[str, FormatValidationFn] = FORMAT_FUNCTIONS,
        extra_validations: Sequence[ValidationFn] = EXTRA_VALIDATIONS,
    ):
        self._cache: Optional[ValidationFn] = None
        self._schema: Optional[Schema] = None
        self._format_validators: Optional[Dict[str, FormatValidationFn]] = None
        self._in_format_validators = dict(format_validators)
        # REMOVED: Plugins can no longer specify extra validations
        # >>> self._extra_validations: Optional[List[ValidationFn]] = None
        self._in_extra_validations = list(extra_validations)

        if plugins is ALL_PLUGINS:
            from .plugins import list_from_entry_points

            self.plugins = tuple(list_from_entry_points())
        else:
            self.plugins = tuple(plugins)  # force immutability / read only

        self._schema_registry = SchemaRegistry(self.plugins)
        self.handlers = RefHandler(self._schema_registry)

    @property
    def schema(self) -> Schema:
        """Top level ``pyproject.toml`` JSON Schema"""
        return self._schema_registry.main

    @property
    def extra_validations(self) -> List[ValidationFn]:
        # REMOVED: Plugins can no longer specify extra validations
        # (it is too complicated to embed them)
        # >>> if self._extra_validations is None:
        # >>>     from_plugins = _chain_iter(p.extra_validations for p in self.plugins)
        # >>>     self._extra_validations = [*self._in_extra_validations, *from_plugins]
        return self._in_extra_validations

    @property
    def formats(self) -> Dict[str, FormatValidationFn]:
        """Mapping between JSON Schema formats and functions that validates them"""
        if self._format_validators is None:
            formats = _chain_iter(
                p.format_validators.items()
                for p in self.plugins
                if hasattr(p, "format_validators")
            )
            formats = chain(self._in_format_validators.items(), formats)
            self._format_validators = dict(formats)
        return self._format_validators

    def __getitem__(self, schema_id: str) -> Schema:
        """Retrieve a schema from registry"""
        return self._schema_registry[schema_id]

    def __call__(self, pyproject: T) -> T:
        if self._cache is None:
            compiled = FJS.compile(self.schema, self.handlers, self.formats)
            self._cache = cast(ValidationFn, compiled)

        self._cache(pyproject)
        return reduce(lambda acc, fn: fn(acc), self.extra_validations, pyproject)
