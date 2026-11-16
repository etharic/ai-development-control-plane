"""Read-only classifier for `gh api` (W-0013).

`gh api` is allowed by the permission rules so routine inspection (PR reviews, comments, compare,
branches, rulesets, settings reads) needs no owner prompt. guard_bash.py calls `classify` and turns
anything not provably read-only into an owner prompt ("ask"). It fails closed: unknown flags,
unparseable input, other hosts, method overrides and GraphQL mutations all ask.

gh semantics this accounts for:
- the default method is GET, but any -f/-F/--raw-field/--field or --input switches it to POST,
  unless -X/--method GET is given explicitly (fields then become query parameters);
- --method/-X may be spelled `-X GET`, `-XGET`, `--method GET`, `--method=GET`;
- headers can carry X-HTTP-Method-Override;
- `graphql` is always POST; it is read-only only for a single `query` document with no mutation
  or subscription (fields given as -f query=..., never --input or -F file references);
- `--hostname` other than github.com targets another server.

Like every local hook, this is not an authorization boundary against code run by an allowed
interpreter. It removes prompt friction for direct reads; GitHub-side protections are the boundary.
"""
from __future__ import annotations

import re

VALUE_FLAGS = {"-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header",
               "--input", "-q", "--jq", "-t", "--template", "--hostname", "-p", "--preview", "--cache"}
BOOL_FLAGS = {"--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"}
READ_METHODS = {"GET", "HEAD"}


def _split(args: list[str]) -> tuple[list[tuple[str, str | None]], list[str]] | None:
    """Return ([(flag, value)], positionals) or None if an unknown flag appears."""
    flags, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            name, val = a.split("=", 1)
            if name not in VALUE_FLAGS:
                return None
            flags.append((name, val))
        elif a.startswith("-X") and len(a) > 2 and not a.startswith("--"):
            flags.append(("-X", a[2:]))
        elif a in VALUE_FLAGS:
            if i + 1 >= len(args):
                return None
            flags.append((a, args[i + 1]))
            i += 1
        elif a in BOOL_FLAGS:
            flags.append((a, None))
        elif a.startswith("-") and a != "-":
            return None
        else:
            pos.append(a)
        i += 1
    return flags, pos


def _graphql_read_only(fields: list[tuple[str, str]]) -> bool:
    docs = [v.split("=", 1)[1] for f, v in fields if v and v.startswith("query=")]
    others = [v for f, v in fields if not (v and v.startswith("query="))]
    if len(docs) != 1 or any(f in ("-F", "--field") for f, v in fields if v and v.startswith("query=")):
        return False  # -F query=@file reads a file we cannot see
    # remaining fields are variables; -F values starting with @ read files: still harmless for reads
    del others
    doc = re.sub(r"#[^\n]*", "", docs[0]).strip()
    if re.search(r"\b(mutation|subscription)\b", doc, re.I):
        return False
    return doc.startswith("{") or re.match(r"query\b", doc, re.I) is not None


def classify(args: list[str]) -> str | None:
    """None if `gh api <args>` is provably read-only, else a reason string (caller asks the owner)."""
    split = _split(args)
    if split is None:
        return "unrecognised gh api option"
    flags, pos = split
    if len(pos) != 1:
        return "expected exactly one endpoint"
    endpoint = pos[0].lstrip("/")
    names = [f for f, _ in flags]
    methods = [v.upper() for f, v in flags if f in ("-X", "--method")]
    fields = [(f, v) for f, v in flags if f in ("-f", "--raw-field", "-F", "--field")]
    for f, v in flags:
        if f in ("-H", "--header") and re.search(r"method-override", v or "", re.I):
            return "method-override header"
        if f == "--hostname" and (v or "").lower() not in ("github.com", "api.github.com"):
            return "non-github.com host"
    if "--input" in names:
        return "--input sends a request body"
    if endpoint == "graphql":
        if methods:
            return "graphql with an explicit method"
        return None if _graphql_read_only(fields) else "graphql request is not a single read-only query"
    if methods:
        if len(set(methods)) != 1 or methods[0] not in READ_METHODS:
            return f"method {methods[-1]}"
        return None
    if fields:
        return "fields without an explicit -X GET make gh send a POST"
    return None
