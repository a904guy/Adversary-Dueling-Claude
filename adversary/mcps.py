"""MCP servers the supervisor is given by name with --adversary-mcp, and how they're allowed.

The supervisor runs with `--setting-sources project`, so the user's allow rules can't widen its
read-only toolset; a side effect is that Claude Code doesn't load the user's MCP servers for it
either, so by default it has none but the bridge's own. Each server named with --adversary-mcp is
looked up where Claude Code itself would find it and handed over with --mcp-config:

- user scope:    ~/.claude.json  "mcpServers"
- project scope: <repo>/.mcp.json "mcpServers", only the ones approved for this folder
- local scope:   ~/.claude.json  "projects"[<repo>]["mcpServers"]

(a more specific scope wins a name clash, and servers switched off for the folder are left out).
Claude in Chrome isn't a configured server but a built-in integration; it's passed on with
--chrome when it is on by default (`claudeInChromeDefaultEnabled`).

claude.ai connectors and plugin-provided servers can't be copied (they come from the account
and from installed plugins, not a definition), so they can't be named.

One spec per server: `NAME` (all its tools), `NAME:readonly` (a preset that inspects without
acting, for browser servers), or `NAME:tool1,tool2`; `none` clears the list (on resume).
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CHROME = "claude-in-chrome"   # the built-in Claude in Chrome integration, enabled with --chrome
OWN = "adversary"             # the bridge's own server; a user server of that name is skipped

# Tools that look without acting: navigate, read, screenshot, console/network, audits. Left out:
# clicking, typing, filling forms, uploads, dialogs and running script in the page.
_DEVTOOLS_READONLY = [
    "list_pages", "select_page", "new_page", "close_page", "navigate_page", "wait_for", "resize_page",
    "emulate", "take_snapshot", "take_screenshot", "take_heapsnapshot", "get_css_styles",
    "list_console_messages", "get_console_message", "list_network_requests", "get_network_request",
    "performance_start_trace", "performance_stop_trace", "performance_analyze_insight", "lighthouse_audit",
]
_CHROME_READONLY = [
    "tabs_context_mcp", "tabs_create_mcp", "navigate", "read_page", "get_page_text", "find",
    "read_console_messages", "read_network_requests", "resize_window",
]


def tool_prefix(name: str) -> str:
    """The `mcp__<server>` prefix Claude Code gives a server's tools (it replaces characters
    outside [A-Za-z0-9_-], e.g. "claude.ai Gmail" -> "claude_ai_Gmail")."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings(repo: Path, home: Path) -> dict:
    """The MCP-approval keys from the settings files, most specific last (so it wins)."""
    merged: dict = {}
    for p in (home / ".claude" / "settings.json", repo / ".claude" / "settings.json",
              repo / ".claude" / "settings.local.json"):
        s = _load(p)
        for key in ("enableAllProjectMcpServers", "enabledMcpjsonServers", "disabledMcpjsonServers"):
            if key in s:
                merged[key] = s[key]
    return merged


def approvals(repo, home: Path | None = None) -> dict:
    """The answers already given to Claude Code's "New MCP servers found" dialog for `repo`'s
    .mcp.json. They're saved in local/user settings, which the supervisor's launch doesn't read,
    so it would ask again every launch; its own --settings carries them over instead."""
    repo, home = Path(repo), (home or Path.home())
    proj = (_load(home / ".claude.json").get("projects") or {}).get(str(repo)) or {}
    out = _settings(repo, home)
    for key in ("enabledMcpjsonServers", "disabledMcpjsonServers"):
        names = list(out.get(key) or []) + [n for n in proj.get(key) or [] if n not in (out.get(key) or [])]
        if names:
            out[key] = names
    if proj.get("enableAllProjectMcpServers"):
        out["enableAllProjectMcpServers"] = True
    return out


def enabled_servers(repo, home: Path | None = None) -> dict[str, dict]:
    """The MCP servers Claude Code would load in `repo`, name -> definition."""
    repo, home = Path(repo), (home or Path.home())
    cfg = _load(home / ".claude.json")
    proj = (cfg.get("projects") or {}).get(str(repo)) or {}
    settings = _settings(repo, home)
    servers: dict[str, dict] = {}
    servers.update(cfg.get("mcpServers") or {})                                    # user
    approved = set(proj.get("enabledMcpjsonServers") or []) | set(settings.get("enabledMcpjsonServers") or [])
    rejected = set(proj.get("disabledMcpjsonServers") or []) | set(settings.get("disabledMcpjsonServers") or [])
    allow_all = bool(proj.get("enableAllProjectMcpServers") or settings.get("enableAllProjectMcpServers"))
    for name, d in (_load(repo / ".mcp.json").get("mcpServers") or {}).items():    # project, if approved
        if name not in rejected and (allow_all or name in approved):
            servers[name] = d
    servers.update(proj.get("mcpServers") or {})                                   # local
    for name in proj.get("disabledMcpServers") or []:                              # switched off here
        servers.pop(name, None)
    return {n: d for n, d in servers.items() if isinstance(d, dict) and n != OWN}


def chrome_enabled(home: Path | None = None) -> bool:
    return bool(_load((home or Path.home()) / ".claude.json").get("claudeInChromeDefaultEnabled"))


def _readonly_tools(name: str, definition: dict | None) -> list[str] | None:
    if name == CHROME:
        return _CHROME_READONLY
    blob = json.dumps(definition or {})
    if "chrome-devtools-mcp" in blob:
        return _DEVTOOLS_READONLY
    return None


@dataclass
class McpPlan:
    servers: dict[str, dict] = field(default_factory=dict)   # definitions for --mcp-config
    allow: list[str] = field(default_factory=list)            # --allowedTools entries
    chrome: bool = False                                      # pass --chrome
    labels: list[str] = field(default_factory=list)           # "chrome-devtools (read-only)", for logs/prompt


def plan(specs: list[str] | None, repo, home: Path | None = None, strict: bool = False) -> McpPlan:
    """What the supervisor gets for `specs` (None/[]/["none"] = nothing). Unknown names raise
    ValueError when `strict` (at the command line), and are skipped otherwise (a server removed
    since the run began)."""
    specs = [s.strip() for s in (specs or []) if s and s.strip()]
    out = McpPlan()
    if not specs or any(s.lower() == "none" for s in specs):
        if strict and len(specs) > 1:
            raise ValueError("--adversary-mcp none can't be combined with server names")
        return out
    available = enabled_servers(repo, home)
    chrome_on = chrome_enabled(home)
    for spec in specs:
        name, _, tools = spec.partition(":")
        name, tools = name.strip(), tools.strip()
        if name == CHROME:
            definition = None
        elif name in available:
            definition = available[name]
        else:
            if strict:
                have = ", ".join(sorted(available) + ([CHROME] if chrome_on else [])) or "none"
                raise ValueError(f"no enabled MCP server named '{name}' (available: {have})")
            continue
        prefix = tool_prefix(name)
        if not tools or tools.lower() == "all":
            allow, label = [prefix], name
        elif tools.lower() == "readonly":
            preset = _readonly_tools(name, definition)
            if preset is None:
                if strict:
                    raise ValueError(f"there's no read-only preset for '{name}'; name its tools instead "
                                     f"(--adversary-mcp {name}:tool1,tool2)")
                continue
            allow, label = [f"{prefix}__{t}" for t in preset], f"{name} (read-only)"
        else:
            picked = [t.strip() for t in tools.split(",") if t.strip()]
            allow, label = [f"{prefix}__{t}" for t in picked], f"{name} ({', '.join(picked)})"
        if name == CHROME:
            out.chrome = True
        else:
            out.servers[name] = definition
        out.allow += allow
        out.labels.append(label)
    return out
