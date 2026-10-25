#!/usr/bin/env python3
"""Print every PromQL query in a Grafana dashboard as a Prometheus rule file.

`promtool check rules` then parses each one, which is the cheapest way to have
the real PromQL parser reject a typo before Grafana shows an empty panel.
"""

from __future__ import annotations

import json
import sys

# Grafana template variables, replaced with something the parser accepts.
VARIABLES = {
    "${device}": "example",
    "$device": "example",
    "$__rate_interval": "1m",
    "$__interval": "5m",
    "$__range": "1d",
}


def constants(dashboard: dict) -> dict[str, str]:
    """Default values of the constant and textbox variables."""
    return {
        v["name"]: v["query"]
        for v in dashboard.get("templating", {}).get("list", [])
        if v.get("type") in ("constant", "textbox")
    }


def _panels(panels: list[dict]):
    for panel in panels:
        yield panel
        # A collapsed row keeps its panels inside it.
        yield from _panels(panel.get("panels", []))


def expressions(dashboard: dict) -> list[tuple[str, str]]:
    variables = dict(VARIABLES)
    for name, value in constants(dashboard).items():
        variables[f"${{{name}}}"] = value
        variables[f"${name}"] = value
    found = []
    for panel in _panels(dashboard.get("panels", [])):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                for name, value in variables.items():
                    expr = expr.replace(name, value)
                found.append((f"{panel['title']} / {target['refId']}", expr))
    return found


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        dashboard = json.load(f)
    rules = [
        {"record": f"dashboard:query_{i}", "expr": expr, "labels": {"panel": where}}
        for i, (where, expr) in enumerate(expressions(dashboard))
    ]
    # JSON is valid YAML, and promtool reads either.
    json.dump({"groups": [{"name": "dashboard", "rules": rules}]}, sys.stdout, indent=2)


if __name__ == "__main__":
    main(sys.argv[1])
