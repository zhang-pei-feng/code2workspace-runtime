"""Clipboard utilities for code2workspace-cli."""

from __future__ import annotations

import base64
import logging
import os
import pathlib
from typing import TYPE_CHECKING

from code2workspace_cli.config import get_glyphs

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from textual.app import App
    from textual.widget import Widget

_PREVIEW_MAX_LENGTH = 40


def _copy_osc52(text: str) -> None:
    """Copy text using OSC 52 escape sequence (works over SSH/tmux)."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    osc52_seq = f"\033]52;c;{encoded}\a"
    if os.environ.get("TMUX"):
        osc52_seq = f"\033Ptmux;\033{osc52_seq}\033\\"

    with pathlib.Path("/dev/tty").open("w", encoding="utf-8") as tty:
        tty.write(osc52_seq)
        tty.flush()


def _shorten_preview(texts: list[str]) -> str:
    """Shorten text for notification preview.

    Returns:
        Shortened preview text suitable for notification display.
    """
    glyphs = get_glyphs()
    dense_text = glyphs.newline.join(texts).replace("\n", glyphs.newline)
    if len(dense_text) > _PREVIEW_MAX_LENGTH:
        return f"{dense_text[: _PREVIEW_MAX_LENGTH - 1]}{glyphs.ellipsis}"
    return dense_text


def _is_remote_terminal_session() -> bool:
    """Return whether the current TTY is likely remote-controlled via SSH."""
    return any(
        os.environ.get(name)
        for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    )


def _get_copy_methods(app: App) -> list[tuple[str, object]]:
    """Return clipboard copy methods in preferred order.

    Prefer explicit system/terminal clipboard integrations over Textual's
    generic `copy_to_clipboard()` path, which can appear to succeed in some
    terminal setups without actually updating the user's clipboard.
    """
    copy_methods: list[tuple[str, object]] = []

    try:
        import pyperclip

        copy_methods.append(("host", pyperclip.copy))
    except ImportError:
        pass

    if _is_remote_terminal_session() or os.environ.get("TMUX"):
        copy_methods.append(("local", _copy_osc52))

    copy_methods.append(("fallback", app.copy_to_clipboard))
    return copy_methods


def copy_selection_to_clipboard(app: App, source_widget: Widget | None = None) -> None:
    """Copy selected text from app widgets to clipboard.

    This queries all widgets for their text_selection and copies
    any selected text to the system clipboard.
    """
    selected_texts = []

    widgets = [source_widget] if source_widget is not None else app.query("*")
    for widget in widgets:
        if widget is None:
            continue
        if not hasattr(widget, "text_selection") or not widget.text_selection:
            continue

        selection = widget.text_selection

        if selection.end is None:
            continue

        try:
            result = widget.get_selection(selection)
        except (AttributeError, TypeError, ValueError, IndexError) as e:
            logger.debug(
                "Failed to get selection from widget %s: %s",
                type(widget).__name__,
                e,
                exc_info=True,
            )
            continue

        if not result:
            continue

        try:
            selected_text, _ = result
        except (TypeError, ValueError) as e:
            logger.debug(
                "Malformed selection result from widget %s: %s",
                type(widget).__name__,
                e,
                exc_info=True,
            )
            continue
        if selected_text.strip():
            selected_texts.append(selected_text)

    if not selected_texts:
        return

    combined_text = "\n".join(selected_texts)

    copied_targets: list[str] = []
    fallback_error = False
    for target, copy_fn in _get_copy_methods(app):
        if target == "fallback" and copied_targets:
            continue
        try:
            copy_fn(combined_text)
        except (OSError, RuntimeError, TypeError) as e:
            logger.debug(
                "Clipboard copy method %s failed: %s",
                getattr(copy_fn, "__name__", repr(copy_fn)),
                e,
                exc_info=True,
            )
            if target == "fallback":
                fallback_error = True
            continue

        if target != "fallback":
            copied_targets.append(target)
            continue

        if not copied_targets:
            copied_targets.append("fallback")

    if copied_targets:
        destination = ""
        if copied_targets == ["host"]:
            destination = " to host clipboard"
        elif copied_targets == ["local"]:
            destination = " to local clipboard"
        elif "host" in copied_targets and "local" in copied_targets:
            destination = " to host and local clipboards"
        app.notify(
            f'"{_shorten_preview(selected_texts)}" copied{destination}',
            severity="information",
            timeout=2,
            markup=False,
        )
        return

    # If all methods fail, still notify but warn
    app.notify(
        "Failed to copy - no clipboard method available"
        if not fallback_error
        else "Failed to copy - clipboard backends rejected the selection",
        severity="warning",
        timeout=3,
    )
