import os
import re
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Sequence, Union

import fastjsonschema as FJS

from .. import api, dist_name, types

if sys.version_info[:2] >= (3, 8):
    # TODO: Import directly (no need for conditional) when `python_requires = >= 3.8`
    from importlib import metadata as _M  # pragma: no cover
else:
    import importlib_metadata as _M  # pragma: no cover


TEXT_REPLACEMENTS = MappingProxyType(
    {
        "from fastjsonschema import": "from .fastjsonschema_exceptions import",
    }
)


def vendorify(
    output_dir: Union[str, os.PathLike] = ".",
    main_file: str = "__init__.py",
    original_cmd: str = "",
    plugins: Union[api.AllPlugins, Sequence[types.Plugin]] = api.ALL_PLUGINS,
    text_replacements: Mapping[str, str] = TEXT_REPLACEMENTS,
) -> Path:
    """Populate the given ``output_dir`` with all files necessary to perform
    the validation.
    The validation can be performed by calling the ``validate`` function inside the
    the file named with the ``main_file`` value.
    ``text_replacements`` can be used to
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    replacements = {**TEXT_REPLACEMENTS, **text_replacements}

    validator = api.Validator(plugins)
    code = FJS.compile_to_code(validator.schema, validator.handlers, validator.formats)
    code = replace_text(_fix_generated_code(code), replacements)
    (out / "fastjsonschema_validations.py").write_text(code, "UTF-8")

    copy_fastjsonschema_exceptions(out, replacements)
    copy_module("extra_validations", out, replacements)
    copy_module("format", out, replacements)
    write_main(out / main_file, validator.schema, replacements)
    write_notice(out, main_file, original_cmd, replacements)
    (out / "__init__.py").touch()

    return out


def replace_text(text: str, replacements: Dict[str, str]) -> str:
    for orig, subst in replacements.items():
        if subst != orig:
            text = text.replace(orig, subst)
    return text


def copy_fastjsonschema_exceptions(
    output_dir: Path, replacements: Dict[str, str]
) -> Path:
    file = output_dir / "fastjsonschema_exceptions.py"
    code = replace_text(api.read_text("fastjsonschema", "exceptions.py"), replacements)
    file.write_text(code, "UTF-8")
    return file


def copy_module(name: str, output_dir: Path, replacements: Dict[str, str]) -> Path:
    file = output_dir / f"{name}.py"
    code = api.read_text(api.__package__, f"{name}.py")
    code = replace_text(code, replacements)
    file.write_text(code, "UTF-8")
    return file


def write_main(
    file_path: Path, schema: types.Schema, replacements: Dict[str, str]
) -> Path:
    file_path.touch()
    # TODO write from template
    return file_path


def write_notice(
    out: Path, main_file: str, cmd: str, replacements: Dict[str, str]
) -> Path:
    if cmd:
        opening = api.read_text(__name__, "cli-notice.template")
        opening = opening.format(command=cmd)
    else:
        opening = api.read_text(__name__, "api-notice.template")
    notice = api.read_text(__name__, "NOTICE.template")
    notice = notice.format(notice=opening, main_file=main_file, **load_licenses())
    notice = replace_text(notice, replacements)

    file = out / "NOTICE"
    file.write_text(notice, "UTF-8")
    return file


def load_licenses() -> Dict[str, str]:
    return {
        "fastjsonschema_license": _find_and_load_licence(_M.files("fastjsonschema")),
        "validate_pyproject_license": _find_and_load_licence(_M.files(dist_name)),
    }


NOCHECK_HEADERS = (
    "# noqa",
    "# type: ignore",
    "# flake8: noqa",
    "# pylint: skip-file",
    "# mypy: ignore-errors",
    "# yapf: disable",
    "# pylama:skip=1",
    "\n\n# *** PLEASE DO NOT MODIFY DIRECTLY: Automatically generated code *** \n\n\n",
)
PICKLED_PATTERNS = r"^REGEX_PATTERNS = pickle.loads\((.*)\)$"
PICKLED_PATTERNS_REGEX = re.compile(PICKLED_PATTERNS, re.M)
VALIDATION_FN_DEF_PATTERN = r"^([\t ])*def\s*validate(_[\w_]+)?\(data\):"
VALIDATION_FN_DEF = re.compile(VALIDATION_FN_DEF_PATTERN, re.M | re.I)


def _fix_generated_code(code: str) -> str:
    code = VALIDATION_FN_DEF.sub(r"\1def validate\2(data, custom_formats):", code)

    # Replace the pickled regexes with calls to `re.compile`
    match = PICKLED_PATTERNS_REGEX.search(code)
    if match:
        import ast
        import pickle

        pickled_regexes = ast.literal_eval(match.group(1))
        regexes = pickle.loads(pickled_regexes).items()
        regexes_ = (f"{k!r}: {_repr_regex(v)}" for k, v in regexes)
        repr_ = "{\n    " + ",\n    ".join(regexes_) + "\n}"
        subst = f"REGEX_PATTERNS = {repr_}"
        code = code.replace(match.group(0), subst)
        code = code.replace("import re, pickle", "import re")

    return "\n".join(NOCHECK_HEADERS) + code


def _find_and_load_licence(files: Optional[Sequence[_M.PackagePath]]) -> str:
    if files is None:
        raise ImportError("Could not find LICENSE for package")
    return next(f for f in files if f.stem.upper() == "LICENSE").read_text("UTF-8")


def _repr_regex(regex: re.Pattern) -> str:
    # Unfortunately using `pprint.pformat` is causing errors
    all_flags = ("A", "I", "DEBUG", "L", "M", "S", "X")
    flags = " | ".join(f"re.{f}" for f in all_flags if regex.flags & getattr(re, f))
    flags = ", " + flags if flags else ""
    return f"re.compile({regex.pattern!r}{flags})"
