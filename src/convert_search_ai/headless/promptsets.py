# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Extraction prompt-set definitions (EXTRACTION_PROMPT_SETS.md §4).

A prompt set is *what to extract from a kind of document*: typed fields,
instructions, a preferred model. This module is the definition itself, with no
storage: validation and normalisation (one canonical form, so two equal
definitions hash equal), compilation to the JSON Schema ``/v1/extract`` already
takes (so the extractor, its checks and its cache are reused unchanged), and the
shareable YAML form (§4.6), including the older ``type: extractor`` files.

The canonical body::

    {"name", "description", "slug", "instructions", "model",
     "applies_to": {"classifier_set", "labels"}, "markup": {"colours"},
     "fields": [{"name", "label", "type", "unit", "enum", "required",
                 "description", "minimum", "maximum"}]}
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import List, Optional, Tuple

FORMAT = "fileengine.prompt_set.v1"
TYPE_ONE = "extraction_prompt_set"
TYPE_MANY = "extraction_prompt_sets"

FIELD_TYPES = ("string", "text", "number", "integer", "boolean", "date", "enum", "list")
_NUMERIC = ("number", "integer")

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]{0,63}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")

MAX_NAME = 120
MAX_DESCRIPTION = 2000
MAX_LABEL = 120
MAX_UNIT = 32
MAX_ENUM = 100
MAX_FILE_BYTES = 256 * 1024
MAX_SETS_PER_FILE = 50


class DefinitionError(ValueError):
    """The definition is unusable. ``where`` names the set and field."""

    def __init__(self, message: str, where: str = ""):
        super().__init__(f"{where}: {message}" if where else message)
        self.where = where


# ------------------------------------------------------------------ normalise

def _text(v, limit: int, what: str, where: str) -> str:
    if v is None:
        return ""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise DefinitionError(f"{what} must be text", where)
    s = str(v).strip()
    if len(s) > limit:
        raise DefinitionError(f"{what} is longer than {limit} characters", where)
    return s


def _number(v, what: str, where: str):
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DefinitionError(f"{what} must be a number", where)
    return v


def normalise_field(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise DefinitionError("a field must be an object", where)
    name = raw.get("name")
    if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
        raise DefinitionError(f"field name {name!r} is not allowed (letters, digits, "
                              "_ . -; starting with a letter or _)", where)
    where = f"{where}, field {name}"
    ftype = raw.get("type") or "string"
    if ftype not in FIELD_TYPES:
        raise DefinitionError(f"type {ftype!r} is not one of {', '.join(FIELD_TYPES)}", where)
    out = {"name": name, "type": ftype,
           "label": _text(raw.get("label"), MAX_LABEL, "label", where),
           "description": _text(raw.get("description"), MAX_DESCRIPTION, "description", where),
           "required": bool(raw.get("required", False))}
    unit = _text(raw.get("unit"), MAX_UNIT, "unit", where)
    if unit:
        if ftype not in _NUMERIC:
            raise DefinitionError("a unit is only meaningful on a number or integer field", where)
        out["unit"] = unit
    if ftype == "enum":
        values = raw.get("enum")
        if not isinstance(values, list) or not values:
            raise DefinitionError("an enum field needs a list of values", where)
        if len(values) > MAX_ENUM:
            raise DefinitionError(f"more than {MAX_ENUM} enum values", where)
        seen, clean = set(), []
        for v in values:
            s = _text(v, MAX_LABEL, "an enum value", where)
            if not s:
                raise DefinitionError("an enum value is empty", where)
            if s not in seen:
                seen.add(s)
                clean.append(s)
        out["enum"] = clean
    elif raw.get("enum"):
        raise DefinitionError("enum values need type: enum", where)
    for bound in ("minimum", "maximum"):
        v = _number(raw.get(bound), bound, where)
        if v is not None:
            if ftype not in _NUMERIC:
                raise DefinitionError(f"{bound} is only meaningful on a number or integer field",
                                      where)
            out[bound] = v
    if "minimum" in out and "maximum" in out and out["minimum"] > out["maximum"]:
        raise DefinitionError("minimum is greater than maximum", where)
    return out


def normalise(raw, *, max_fields: int = 60, max_chars: int = 20000) -> dict:
    """The canonical body of a definition, or :class:`DefinitionError`."""
    if not isinstance(raw, dict):
        raise DefinitionError("a prompt set must be an object")
    name = _text(raw.get("name"), MAX_NAME, "name", "")
    if not name:
        raise DefinitionError("a prompt set needs a name")
    where = f"prompt set {name!r}"
    slug = raw.get("slug")
    if slug not in (None, ""):
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise DefinitionError(f"slug {slug!r} is not allowed (lower-case letters, digits, "
                                  ". _ -; 3 to 64 characters)", where)
    else:
        slug = ""
    model = raw.get("model")
    if model in (None, ""):
        model = ""
    elif not isinstance(model, str):
        raise DefinitionError("model must be a model id", where)
    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise DefinitionError("a prompt set needs at least one field", where)
    if len(fields_raw) > max_fields:
        raise DefinitionError(f"more than {max_fields} fields", where)
    fields, seen = [], set()
    for f in fields_raw:
        nf = normalise_field(f, where)
        if nf["name"] in seen:
            raise DefinitionError(f"field {nf['name']!r} appears twice", where)
        seen.add(nf["name"])
        fields.append(nf)
    applies = raw.get("applies_to") or {}
    if not isinstance(applies, dict):
        raise DefinitionError("applies_to must be an object", where)
    labels = applies.get("labels") or []
    if not isinstance(labels, list):
        raise DefinitionError("applies_to.labels must be a list", where)
    applies_to = {"classifier_set": _text(applies.get("classifier_set"), MAX_NAME,
                                          "applies_to.classifier_set", where),
                  "labels": sorted({_text(lb, MAX_NAME, "a label", where) for lb in labels} - {""})}
    markup = raw.get("markup") or {}
    colours = (markup.get("colours") if isinstance(markup, dict) else None) or {}
    if not isinstance(colours, dict):
        raise DefinitionError("markup.colours must map field names to #rrggbb", where)
    clean_colours = {}
    for k, v in colours.items():
        if k not in seen:
            raise DefinitionError(f"markup colour for unknown field {k!r}", where)
        if not isinstance(v, str) or not _COLOUR.fullmatch(v):
            raise DefinitionError(f"markup colour {v!r} for {k} is not #rrggbb", where)
        clean_colours[k] = v.lower()
    body = {"name": name,
            "description": _text(raw.get("description"), MAX_DESCRIPTION, "description", where),
            "slug": slug,
            "instructions": _text(raw.get("instructions"), max_chars, "instructions", where),
            "model": model,
            "applies_to": applies_to,
            "markup": {"colours": clean_colours},
            "fields": fields}
    if len(body["instructions"]) + len(json.dumps(compile_schema(body))) > max_chars:
        raise DefinitionError(f"fields and instructions exceed {max_chars} characters", where)
    return body


# ------------------------------------------------------------------ compile

def _field_schema(f: dict) -> dict:
    t = f["type"]
    if t in ("string", "text"):
        s = {"type": "string"}
    elif t in _NUMERIC:
        s = {"type": t}
        for bound in ("minimum", "maximum"):
            if bound in f:
                s[bound] = f[bound]
    elif t == "boolean":
        s = {"type": "boolean"}
    elif t == "date":
        s = {"type": "string", "format": "date"}
    elif t == "enum":
        s = {"type": "string", "enum": list(f["enum"])}
    else:  # list
        s = {"type": "array", "items": {"type": "string"}}
    if f.get("label"):
        s["title"] = f["label"]
    desc = f.get("description") or ""
    if t == "date":
        desc = (desc + " " if desc else "") + "(ISO 8601 date, YYYY-MM-DD)"
    if f.get("unit"):
        desc = (desc + " " if desc else "") + f"(unit: {f['unit']})"
        s["x-unit"] = f["unit"]
    if desc:
        s["description"] = desc
    return s


def compile_schema(body: dict) -> dict:
    """The JSON Schema ``/v1/extract`` takes. ``required`` is NOT sent: the
    model must still answer found:false for a field the document lacks; the
    rules (§6.2) are what treat a missing required field as a problem."""
    return {"type": "object",
            "properties": {f["name"]: _field_schema(f) for f in body["fields"]}}


def definition_hash(body: dict) -> str:
    """Equal definitions hash equal (an application's publish is a no-op when
    nothing changed, §4.5)."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:32]


def public_fields(body: dict) -> List[dict]:
    """The field definitions as a desktop application maps them."""
    return [dict(f) for f in body["fields"]]


# ------------------------------------------------------------------ YAML

def _yaml():
    import yaml
    return yaml


def to_export(body: dict, *, revision: Optional[int] = None, status: str = "published") -> dict:
    out = {"format": FORMAT, "type": TYPE_ONE, "name": body["name"]}
    if body.get("slug"):
        out["slug"] = body["slug"]
    if body.get("description"):
        out["description"] = body["description"]
    if body.get("instructions"):
        out["instructions"] = body["instructions"]
    if body.get("model"):
        out["model"] = body["model"]
    applies = body.get("applies_to") or {}
    if applies.get("classifier_set") or applies.get("labels"):
        out["applies_to"] = {k: v for k, v in applies.items() if v}
    colours = (body.get("markup") or {}).get("colours") or {}
    if colours:
        out["markup"] = {"colours": dict(colours)}
    fields = []
    for f in body["fields"]:
        g = {"name": f["name"]}
        for k in ("label", "type", "unit", "enum", "minimum", "maximum"):
            if f.get(k) not in (None, "", []):
                g[k] = f[k]
        if f.get("required"):
            g["required"] = True
        if f.get("description"):
            g["description"] = f["description"]
        fields.append(g)
    out["fields"] = fields
    if status != "published":
        out["status"] = status
    out["exported"] = {"from_revision": revision,
                       "at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return out


def dump_yaml(docs: List[dict]) -> str:
    yaml = _yaml()
    if len(docs) == 1:
        data = docs[0]
    else:
        data = {"format": FORMAT, "type": TYPE_MANY,
                "sets": [{k: v for k, v in d.items() if k not in ("format",)} for d in docs]}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def _legacy(raw: dict) -> Tuple[dict, List[str]]:
    """The SmolDocBot / classifier_and_extractor ``type: extractor`` form:
    ``prompt`` + ``fields: [{name, description}]``. Every field becomes a
    string; the importer is told the types need setting."""
    fields = []
    for f in raw.get("fields") or []:
        if isinstance(f, dict):
            fields.append({"name": f.get("name"), "type": "string",
                           "description": f.get("description") or ""})
    body = {"name": raw.get("name"), "instructions": raw.get("prompt") or "",
            "fields": fields}
    return body, ["imported from the older 'extractor' format: every field is text; "
                  "set the field types before publishing"]


def parse_import(text: str) -> List[Tuple[dict, List[str]]]:
    """``[(raw definition, warnings)]`` from a YAML (or JSON) file. Structure
    only; each definition is then normalised with the deployment's limits.
    Raises :class:`DefinitionError`; nothing is imported unless all parse."""
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise DefinitionError(f"the file is larger than {MAX_FILE_BYTES // 1024} KB")
    try:
        data = _yaml().safe_load(text)
    except Exception as e:
        raise DefinitionError(f"not valid YAML: {e}")
    if isinstance(data, list):
        data = {"type": TYPE_MANY, "sets": data}
    if not isinstance(data, dict):
        raise DefinitionError("the file does not hold a prompt set")
    kind = data.get("type")
    fmt = data.get("format")
    if fmt not in (None, FORMAT):
        raise DefinitionError(f"unknown format {fmt!r} (this server reads {FORMAT})")
    if kind == TYPE_MANY:
        items = data.get("sets")
        if not isinstance(items, list) or not items:
            raise DefinitionError("'sets' must be a non-empty list")
    elif kind in (TYPE_ONE, "extractor", None):
        items = [data]
    else:
        raise DefinitionError(f"type {kind!r} is not a prompt set")
    if len(items) > MAX_SETS_PER_FILE:
        raise DefinitionError(f"more than {MAX_SETS_PER_FILE} sets in one file")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise DefinitionError("each set must be an object")
        if item.get("type") == "extractor" or ("prompt" in item and "instructions" not in item):
            out.append(_legacy(item))
        else:
            raw = {k: v for k, v in item.items()
                   if k not in ("format", "type", "exported", "status")}
            out.append((raw, []))
    return out
