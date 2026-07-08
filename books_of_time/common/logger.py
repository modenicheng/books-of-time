"""
Logger module — Rich-powered logging for books-of-time.

Usage:
    from books_of_time.common.logger import get_logger

    logger = get_logger(__name__)
    logger.info("Hello, rich world!")
    logger.warning("Something suspicious")
    logger.error("Something failed", exc_info=True)

Entry-point (optional, for customisation):
    from books_of_time.common.logger import setup_logging

    setup_logging(level="DEBUG", tracebacks_show_locals=True)
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

# ---------------------------------------------------------------------------
# Single shared console instance (can be replaced via setup_logging)
# ---------------------------------------------------------------------------
_MODULE_CONSOLE: Console = Console()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def setup_logging(
    level: int | str = logging.INFO,
    *,
    show_time: bool = True,
    show_level: bool = True,
    show_path: bool = True,
    rich_tracebacks: bool = True,
    tracebacks_show_locals: bool = False,
    tracebacks_extra_lines: int = 3,
    tracebacks_width: int | None = None,
    log_format: str = "%(message)s",
    date_format: str = "[%Y-%m-%d %X]",
    console: Console | None = None,
    keywords: list[str] | None = None,
) -> None:
    """Configure the root logger with a single RichHandler.

    Call this **once** from your application entry-point
    (e.g. ``main.py``) to override defaults.  If you never call it,
    :func:`get_logger` will auto-configure with the defaults shown above.

    Parameters
    ----------
    level:
        Logging threshold (e.g. ``logging.DEBUG``, ``"INFO"``).
    show_time, show_level, show_path:
        Toggle the respective columns in the RichHandler output.
    rich_tracebacks:
        If ``True``, exceptions are rendered with Rich's
        syntax-highlighted traceback.
    tracebacks_show_locals:
        Include local variables in traceback output.
    tracebacks_extra_lines:
        Lines of context around each frame in tracebacks.
    tracebacks_width:
        Character width of tracebacks (``None`` → console width).
    log_format:
        ``logging.Formatter`` style string.  The default ``%(message)s``
        works well because RichHandler formats the prefix columns itself.
    date_format:
        ``logging.Formatter`` date format string.
    console:
        A custom :class:`rich.console.Console` instance.
    keywords:
        Optional list of keywords that will trigger a log level
        (e.g. ``["SPAM", "HAM"]``).
    """
    # Prevent duplicate handlers when setup_logging is called more than once
    root_logger = logging.getLogger()
    for h in root_logger.handlers[:]:
        if isinstance(h, RichHandler):
            root_logger.removeHandler(h)

    effective_console = console or _MODULE_CONSOLE

    handler = RichHandler(
        level=level,
        console=effective_console,
        show_time=show_time,
        show_level=show_level,
        show_path=show_path,
        rich_tracebacks=rich_tracebacks,
        tracebacks_show_locals=tracebacks_show_locals,
        tracebacks_extra_lines=tracebacks_extra_lines,
        tracebacks_width=tracebacks_width,
        omit_repeated_times=False,
        keywords=keywords,
    )
    handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    root_logger.setLevel(level)
    root_logger.addHandler(handler)


def get_logger(name: str = "books-of-time") -> logging.Logger:
    """Return a logger instance.

    If the root logger has **no** RichHandler configured yet,
    :func:`setup_logging` is called automatically with default arguments
    so that you can just do::

        logger = get_logger(__name__)
        logger.info("works out of the box")

    Parameters
    ----------
    name:
        Logger name — pass ``__name__`` from your module.
    """
    logger = logging.getLogger(name)

    # Auto-setup on first call (lazy initialisation)
    root_logger = logging.getLogger()
    if not any(isinstance(h, RichHandler) for h in root_logger.handlers):
        setup_logging()

    return logger


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------
__all__ = [
    "get_logger",
    "setup_logging",
]
