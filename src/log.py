"""Shared rich-based pretty logging for the pipeline."""
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

# The log lines use → ✓ ✗ ·. A Windows console in a legacy code page (cp1252)
# raises UnicodeEncodeError on them from inside rich; switching stdout to UTF-8
# (replacing anything still unencodable) keeps the same output everywhere else.
for _stream in (sys.stdout, sys.stderr):
    try:
        if (_stream.encoding or "").lower().replace("-", "") != "utf8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()
_file_console: Console | None = None


def init_log_file(path) -> None:
    """Mirror all subsequent log output to a plain-text file at `path`."""
    global _file_console
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "w", encoding="utf-8", buffering=1)
    _file_console = Console(
        file=f, force_terminal=False, no_color=True, width=120, highlight=False
    )


def _print(*args, **kwargs) -> None:
    console.print(*args, **kwargs)
    if _file_console is not None:
        _file_console.print(*args, **kwargs)


def rule(label: str, **kwargs) -> None:
    console.rule(label, **kwargs)
    if _file_console is not None:
        _file_console.rule(label, **kwargs)


def section(label: str) -> None:
    """Top-level section header — used by run_all between major stages."""
    _print()
    _print(Panel.fit(f"[bold white]{label}[/]", border_style="bold cyan"))


def subsection(label: str) -> None:
    """Sub-stage header inside a module's main()."""
    _print()
    _print(Rule(f"[bold magenta]{label}[/]", style="magenta", align="left"))


def step(label: str) -> None:
    """Discrete step within a subsection (e.g. 'Generating script 2/5')."""
    _print(f"[bold blue]▸[/] {label}")


def info(msg: str) -> None:
    _print(f"  [dim]· {msg}[/]")


def success(msg: str) -> None:
    _print(f"  [bold green]✓[/] {msg}")


def warn(msg: str) -> None:
    _print(f"  [bold yellow]![/] {msg}")


def error(msg: str) -> None:
    _print(f"  [bold red]✗[/] {msg}")


def status(label: str):
    """Context manager: animated spinner with label. Use during long ops."""
    return console.status(f"[cyan]{label}[/]", spinner="dots")
