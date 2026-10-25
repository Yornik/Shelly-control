#!/usr/bin/env python3
"""Fail if the shipped package could do more to the device than Switch.Set.

The relay switches a 1.1 kW boiler, so what the package can send is checked
here, not promised. Over every module in shelly_control/ this asserts:

1. The Shelly allowlist (shelly.ALLOWED_METHODS, method -> parameters) is
   exactly the copy pinned below, and Switch.Set is its only write method.
   Adding a method means changing both files, which a reviewer sees.
2. Every string that looks like an RPC method (Component.Method) is on that
   list, compared case-insensitively as the device does. "Switch.Set" itself
   appears only in shelly.py.
3. Every string that looks like a URL path is one the package needs: the
   prefixes the two clients build on, the web routes (web.py only), and the
   default state directory (config.py only).
4. No string or name mentions another write method (Toggle, Reboot,
   SetConfig, ...), a Gen1 control path, firmware, script, webhook or KVS
   calls, a read that exposes private data, or the Tibber API itself: this
   service reads prices from the tibber-exporter and holds no Tibber token.
5. Only two HTTP call sites: one `self.session.get(..., allow_redirects=False)`
   in ShellyClient._call and one in PriceClient._get. `.session` appears
   nowhere else but in the two classes' __init__. Only shelly.py and
   prices.py import requests, and only its auth, adapters and exceptions
   modules. No other HTTP, WebSocket or MQTT client, no subprocess.
6. Who can switch: `set_output` is called only in Controller._apply, and only
   controller.py and __main__.py import from shelly.py.
7. No dynamic access that would hide any of the above: getattr, setattr,
   eval, exec, compile, __import__, importlib.
8. Templates escape everything and load nothing from outside: no |safe,
   Markup or autoescape switch, no inline script, style or event handler, no
   external URL, and every form posts to a known route. The page's script
   makes no requests of its own.

Only syntax is inspected (strings, names, attributes, imports), never
comments or docstrings, so the text explaining what is forbidden does not trip
the check. Code cannot read a docstring back: __doc__ is forbidden.
The runtime checks in shelly_control/shelly.py and prices.py (the allowlist in
the call, and an adapter that checks every request as it is sent) back this up.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "shelly_control"

# The pinned copy. Keep it in step with ALLOWED_METHODS in shelly_control/shelly.py.
EXPECTED_METHODS = {
    "Shelly.GetDeviceInfo": frozenset(),
    "Switch.GetStatus": frozenset({"id"}),
    "Switch.Set": frozenset({"id", "on"}),
}
EXPECTED_WRITE_METHODS = frozenset({"Switch.Set"})
SWITCH_SET = "Switch.Set"

# Path-like strings each module may contain. "*" applies to every module.
CLIENT_PATHS = frozenset({"/", "/rpc/", "/homes/", "/prices"})
ROUTES = frozenset(
    {
        "/",
        "/settings",
        "/override",
        "/override/clear",
        "/api/v1/status",
        "/api/v1/plan",
        "/api/v1/settings",
        "/api/v1/override",
        "/api/v1/decisions",
        "/metrics",
        "/healthz",
        "/readyz",
        "/api/",
        "/static/",
        "/?notice=",
        "/?notice=override#override",
        "/?notice=cleared#override",
        "/?notice=no_override#override",
    }
)
ALLOWED_PATHS = {
    "*": CLIENT_PATHS,
    "web.py": ROUTES,
    "config.py": frozenset({"/data"}),
    "prices.py": frozenset({"/homes/[0-9a-fA-F-]{36}/prices"}),
}

# Write side and privacy-sensitive reads of the Gen2 API, Gen1 control paths,
# and the Tibber API. Matched case-insensitively in strings and names.
FORBIDDEN_FRAGMENTS = (
    # RPC methods that change something; ".set" is allowed only as Switch.Set.
    ".set",
    "toggle",
    "reboot",
    "factoryreset",
    "resetcounters",
    "resetwificonfig",
    ".update",
    "checkforupdate",
    "detectlocation",
    ".create",
    ".delete",
    ".put",
    ".trigger",
    ".eval",
    ".start",
    ".stop",
    ".scan",
    ".calibrate",
    ".open",
    ".close",
    "gotoposition",
    "backup",
    "defaultconfig",
    "script.",
    "schedule.",
    "webhook.",
    "kvs.",
    "virtual.",
    "http.get",
    "http.post",
    "http.request",
    "sensoraddon.",
    "matter.",
    "shelly.getconfig",
    "shelly.getstatus",
    "sys.getconfig",
    "wifi.getconfig",
    "mqtt.getconfig",
    "cloud.getconfig",
    "getcode",
    "getsetupcode",
    "ident=",
    "/relay",
    "/roller",
    "/light",
    "/color",
    "/white",
    "/cct",
    "/rgb",
    "/ota",
    "/script",
    "/debug",
    "/settings",
    "turn=",
    "timer=",
    "toggle_after",
    "ws://",
    "wss://",
    "mqtt://",
    # The Tibber API: prices come from the exporter only.
    "api.tibber.com",
    "tibber.com",
    "graphql",
    "gql",
    "bearer",
    "authorization",
    "tibber_token",
    "period=now",
)
# Exact strings that may contain a forbidden fragment, and where.
EXEMPT_STRINGS = {
    SWITCH_SET: frozenset({"shelly.py"}),
    "/settings": frozenset({"web.py"}),
    "/api/v1/settings": frozenset({"web.py"}),
}

# Strings that are only suspicious on their own: HTTP verbs other than GET.
FORBIDDEN_STRINGS = frozenset({"ident", "post", "put", "patch", "delete", "turn", "toggle"})
VERB_MODULES = frozenset({"web.py"})

FORBIDDEN_IMPORTS = (
    "aiohttp",
    "aioshelly",
    "http.client",
    "httpx",
    "paho",
    "socket",
    "urllib.request",
    "urllib3",
    "websocket",
    "websockets",
    "subprocess",
    "pty",
    "ctypes",
)
REQUESTS_MODULES = frozenset({"shelly.py", "prices.py"})
ALLOWED_REQUESTS_MODULES = frozenset({"requests.adapters", "requests.auth", "requests.exceptions"})
SHELLY_IMPORTERS = frozenset({"controller.py", "__main__.py"})

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "request", "head", "options"})
WRITE_VERBS = HTTP_METHODS - {"get"}

# (module, class) -> (set-up function, call function): where the session may be touched.
CLIENTS = {
    ("shelly.py", "ShellyClient"): ("__init__", "_call"),
    ("prices.py", "PriceClient"): ("__init__", "_get"),
}
EXPECTED_HTTP_CALLS = {("shelly.py", "ShellyClient"): 1, ("prices.py", "PriceClient"): 1}

SWITCH_CALL = "set_output"
SWITCH_CALLER = ("controller.py", "Controller", "_apply")

SETUP_ONLY_ATTRIBUTES = frozenset({"Session", "mount"})
NEVER_ATTRIBUTES = frozenset(
    {"prepare_request", "adapters", "get_adapter", "api", "sessions", "urlopen", "Request"}
)
DYNAMIC_NAMES = frozenset(
    {"getattr", "setattr", "delattr", "eval", "exec", "compile", "__import__", "importlib"}
)
DYNAMIC_ATTRIBUTES = frozenset({"system", "popen", "__setattr__", "__getattribute__", "__doc__"})

FILE_SUFFIXES = frozenset({"json", "css", "js", "svg", "html", "woff2", "txt", "tmp", "png"})
_RPC_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9]*\.([A-Za-z][A-Za-z0-9]*)")
_ALLOWED_METHODS = {method.casefold() for method in EXPECTED_METHODS}

# Templates and static files.
TEMPLATE_FORBIDDEN = (
    re.compile(r"\|\s*safe\b"),
    re.compile(r"Markup\s*\("),
    re.compile(r"autoescape", re.IGNORECASE),
    re.compile(r"\sstyle\s*=", re.IGNORECASE),
    re.compile(r"<style", re.IGNORECASE),
    re.compile(r"\son[a-z]+\s*=", re.IGNORECASE),
    re.compile(r"<script(?![^>]*\ssrc=)", re.IGNORECASE),
    re.compile(r"(?<!xmlns=\")https?://", re.IGNORECASE),
    re.compile(r"\bjavascript:", re.IGNORECASE),
)
_FORM_ACTION = re.compile(r"""<form\b[^>]*\saction="([^"]*)\"""")
SCRIPT_FORBIDDEN = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "Function(",
    "fetch(",
    "XMLHttpRequest",
    "sendBeacon",
    "WebSocket",
    "http:",
    "https:",
)
STYLE_FORBIDDEN = ("http:", "https:", "@import", "expression(")


def _no_redirects(node: ast.Call) -> bool:
    return any(
        kw.arg == "allow_redirects"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is False
        for kw in node.keywords
    )


def _scopes(tree: ast.AST) -> dict[ast.AST, tuple[str | None, str | None]]:
    """Each node -> (enclosing class name, enclosing function name)."""
    scopes: dict[ast.AST, tuple[str | None, str | None]] = {}

    def visit(node: ast.AST, cls: str | None, func: str | None) -> None:
        scopes[node] = (cls, func)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, child.name, None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, cls, child.name)
            else:
                visit(child, cls, func)

    visit(tree, None, None)
    return scopes


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _session_get_call(node: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> ast.Call | None:
    """For `.session` in `self.session.get(...)`, the call; else None."""
    get = parents.get(node)
    call = parents.get(get) if get is not None else None
    if (
        isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(get, ast.Attribute)
        and get.attr == "get"
        and get.value is node
        and isinstance(call, ast.Call)
        and call.func is get
    ):
        return call
    return None


def _is_super_send(node: ast.Attribute, func: str | None) -> bool:
    value = node.value
    return (
        func == "send"
        and isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "super"
    )


def _is_route_decorator(node: ast.Attribute, module: str) -> bool:
    """`app.post`, `app.put`, `app.delete` in web.py: routes for requests coming in."""
    return module in VERB_MODULES and isinstance(node.value, ast.Name) and node.value.id == "app"


def _check_transport(node, scope, parents, module: str, where: str, calls: dict) -> list[str]:
    problems: list[str] = []
    cls, func = scope
    client = CLIENTS.get((module, cls))
    if isinstance(node, ast.Attribute):
        attr = node.attr
        if attr == "session":
            call = _session_get_call(node, parents)
            if client and func == client[0]:
                pass
            elif client and func == client[1] and call is not None:
                calls[(module, cls)] = calls.get((module, cls), 0) + 1
                if not _no_redirects(call):
                    problems.append(f"{where}: the session call must not follow redirects")
            else:
                problems.append(
                    f"{where}: the session is used outside the clients' __init__ and their "
                    "single .get() call"
                )
        elif attr in WRITE_VERBS and not _is_route_decorator(node, module):
            problems.append(f"{where}: .{attr}() is not a GET; only GET leaves the process")
        elif attr == "get" and isinstance(node.value, ast.Name) and node.value.id == "requests":
            problems.append(f"{where}: requests.get() bypasses the clients")
        elif attr == "send" and not _is_super_send(node, func):
            problems.append(f"{where}: .send() bypasses the clients")
        elif attr in SETUP_ONLY_ATTRIBUTES and not (client and func == client[0]):
            problems.append(f"{where}: .{attr} is only allowed in a client's __init__")
        elif attr in NEVER_ATTRIBUTES:
            problems.append(f"{where}: .{attr} reaches the network around the clients")
        elif attr in DYNAMIC_ATTRIBUTES:
            problems.append(f"{where}: .{attr} is not allowed")
        elif attr == SWITCH_CALL and (module, cls, func) != SWITCH_CALLER:
            problems.append(
                f"{where}: {SWITCH_CALL} may only be called in "
                f"{SWITCH_CALLER[1]}.{SWITCH_CALLER[2]}"
            )
    elif isinstance(node, ast.Name):
        if node.id in DYNAMIC_NAMES:
            problems.append(f"{where}: {node.id}() hides what is called from this check")
        elif node.id == "__doc__":
            problems.append(f"{where}: __doc__ is not allowed")
        elif node.id == SWITCH_CALL:
            problems.append(f"{where}: {SWITCH_CALL} may only be called as a method")
    elif isinstance(node, ast.Import):
        for alias in node.names:
            name = alias.name
            if name == "importlib" or name.startswith("importlib."):
                problems.append(f"{where}: forbidden import {name}")
            elif name == "requests" and module not in REQUESTS_MODULES:
                problems.append(f"{where}: only shelly.py and prices.py may import requests")
            elif name.startswith("requests.") and (
                name not in ALLOWED_REQUESTS_MODULES or module not in REQUESTS_MODULES
            ):
                problems.append(f"{where}: forbidden import {name}")
    elif isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod == "importlib" or mod.startswith("importlib."):
            problems.append(f"{where}: forbidden import {mod}")
        elif mod == "requests" or mod.startswith("requests."):
            if module not in REQUESTS_MODULES:
                problems.append(f"{where}: only shelly.py and prices.py may import requests")
            elif mod == "requests":
                for alias in node.names:
                    if f"requests.{alias.name}" not in ALLOWED_REQUESTS_MODULES:
                        problems.append(f"{where}: forbidden import from requests: {alias.name}")
            elif mod not in ALLOWED_REQUESTS_MODULES:
                problems.append(f"{where}: forbidden import {mod}")
        if node.level and mod == "shelly" and module not in SHELLY_IMPORTERS:
            problems.append(f"{where}: only controller.py and __main__.py may import from shelly")
    return problems


def _check_string(value: str, module: str, where: str) -> list[str]:
    problems = []
    match = _RPC_METHOD.fullmatch(value)
    if match and match[1].lower() not in FILE_SUFFIXES and value.casefold() not in _ALLOWED_METHODS:
        problems.append(f"{where}: RPC method not allowlisted: {value[:60]}")
    if match and value.casefold() == SWITCH_SET.casefold() and module != "shelly.py":
        problems.append(f"{where}: {SWITCH_SET} may only appear in shelly.py")
    if value.startswith("/"):
        allowed = ALLOWED_PATHS["*"] | ALLOWED_PATHS.get(module, frozenset())
        if value not in allowed:
            problems.append(f"{where}: path not allowed: {value[:60]}")
    if value.casefold() in FORBIDDEN_STRINGS and module not in VERB_MODULES:
        problems.append(f"{where}: forbidden string {value!r}")
    return problems


def _fragments(text: str, module: str, where: str) -> list[str]:
    if module in EXEMPT_STRINGS.get(text, frozenset()):
        return []
    lowered = text.lower()
    return [
        f"{where}: write-side, private or Tibber API reference {fragment!r} in {text[:60]!r}"
        for fragment in FORBIDDEN_FRAGMENTS
        if fragment in lowered
    ]


def _docstrings(tree: ast.AST) -> set[int]:
    """ids of the docstring nodes: documentation, read like comments."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def check_source(source: str, module: str, calls: dict | None = None) -> list[str]:
    """Problems found in one module's source. `module` is its file name."""
    calls = {} if calls is None else calls
    problems: list[str] = []
    tree = ast.parse(source, module)
    scopes = _scopes(tree)
    parents = _parents(tree)
    docstrings = _docstrings(tree)
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        line = getattr(node, "lineno", "?")  # noqa: B009 - not every node has a line
        where = f"{module}:{line}"
        problems += _check_transport(node, scopes[node], parents, module, where, calls)
        texts: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
            problems += _check_string(node.value, module, where)
        elif isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(node.arg)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{n}" for n in names]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
                    problems.append(f"{where}: forbidden import {name}")
        for text in texts:
            problems += _fragments(text, module, where)
    return problems


def check_templates(package: Path = PACKAGE) -> list[str]:
    problems = []
    for path in sorted((package / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern in TEMPLATE_FORBIDDEN:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"templates/{path.name}:{line}: not allowed: {match[0]!r}")
        for match in _FORM_ACTION.finditer(text):
            # A fragment only says where the answer scrolls to; the route is what counts.
            route, _, fragment = match[1].partition("#")
            if route not in ROUTES or not re.fullmatch(r"[a-z-]*", fragment):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"templates/{path.name}:{line}: form posts to {match[1]!r}")
    for path in sorted((package / "static").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        problems += [
            f"static/{path.name}: not allowed: {word!r}"
            for word in SCRIPT_FORBIDDEN
            if word in text
        ]
    for path in sorted((package / "static").glob("*.css")):
        text = path.read_text(encoding="utf-8")
        problems += [
            f"static/{path.name}: not allowed: {word!r}" for word in STYLE_FORBIDDEN if word in text
        ]
    return problems


def check_package(package: Path = PACKAGE) -> list[str]:
    problems: list[str] = []
    calls: dict = {}
    for path in sorted(package.rglob("*.py")):
        problems += check_source(path.read_text(encoding="utf-8"), path.name, calls)
    if calls != EXPECTED_HTTP_CALLS:
        problems.append(
            f"expected exactly one HTTP call site in ShellyClient._call and one in "
            f"PriceClient._get, found {calls or 'none'}"
        )
    if (package / "templates").is_dir():
        problems += check_templates(package)

    sys.path.insert(0, str(package.parent))
    from shelly_control.shelly import ALLOWED_METHODS, WRITE_METHODS

    if dict(ALLOWED_METHODS) != EXPECTED_METHODS:
        added = sorted(set(ALLOWED_METHODS) - set(EXPECTED_METHODS))
        removed = sorted(set(EXPECTED_METHODS) - set(ALLOWED_METHODS))
        changed = sorted(
            m
            for m in set(ALLOWED_METHODS) & set(EXPECTED_METHODS)
            if ALLOWED_METHODS[m] != EXPECTED_METHODS[m]
        )
        problems.append(
            f"ALLOWED_METHODS changed: added {added}, removed {removed}, "
            f"parameters changed {changed}"
        )
    if WRITE_METHODS != EXPECTED_WRITE_METHODS:
        problems.append(f"WRITE_METHODS changed: {sorted(WRITE_METHODS)}")
    return problems


def main() -> int:
    problems = check_package()
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(f"allowlist guard: {len(problems)} problem(s)")
        return 1
    print(
        f"allowlist guard: OK ({len(EXPECTED_METHODS)} allowlisted methods, "
        f"write: {', '.join(sorted(EXPECTED_WRITE_METHODS))})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
