from __future__ import annotations
from rich.console import Console
from rich.theme import Theme

_theme = Theme({
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "muted": "grey50",
    "title": "bold white",
    "accent": "cyan",
})

console = Console(theme=_theme)
print = console.print
