"""
Captcha Everything - exteraGram plugin.

Requires a captcha (pick-the-correct-emoji) before every outgoing message.
The captcha is blocking: on a correct answer the message is sent
immediately, on a wrong answer it is cancelled. No second tap needed.
"""

from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Tuple

from base_plugin import BasePlugin, HookResult, HookStrategy
from ui.alert import AlertDialogBuilder
from ui.settings import Header, Switch, Selector, Text
import client_utils

# ---------------------------------------------------------------------------
# Plugin metadata (must be plain top-level constants)
# ---------------------------------------------------------------------------

__id__ = "captcha_everything"
__name__ = "Captcha Everything"
__description__ = (
    "Ask a pick-the-picture captcha before every outgoing message. "
    "Useful as a self-discipline / anti-impulse send filter."
)
__author__ = "@you"
__version__ = "1.2.0"
__icon__ = "exteraPlugins/1"


# ---------------------------------------------------------------------------
# Captcha content
# ---------------------------------------------------------------------------

# (name, emoji). The name is what we ask the user to pick; the emoji is the
# button label. Keep the set diverse so buttons look clearly different.
_CAPTCHA_POOL: List[Tuple[str, str]] = [
    ("cat",     "\U0001F431"),
    ("dog",     "\U0001F436"),
    ("fox",     "\U0001F98A"),
    ("bear",    "\U0001F43B"),
    ("panda",   "\U0001F43C"),
    ("lion",    "\U0001F981"),
    ("tiger",   "\U0001F42F"),
    ("frog",    "\U0001F438"),
    ("monkey",  "\U0001F435"),
    ("pig",     "\U0001F437"),
    ("owl",     "\U0001F989"),
    ("unicorn", "\U0001F984"),
    ("apple",   "\U0001F34E"),
    ("banana",  "\U0001F34C"),
    ("pizza",   "\U0001F355"),
    ("rocket",  "\U0001F680"),
    ("car",     "\U0001F697"),
    ("house",   "\U0001F3E0"),
    ("heart",   "\u2764\ufe0f"),
    ("star",    "\u2B50"),
]

# Max time we are willing to block the hook waiting for the user's answer.
# If the user ignores the dialog for that long, we cancel the send.
_HOOK_WAIT_SECONDS = 60.0

# Settings keys
_SK_ENABLED      = "enabled"
_SK_OPTION_COUNT = "option_count"
_SK_FAIL_TOAST   = "fail_toast"

_DEFAULTS: Dict[str, Any] = {
    _SK_ENABLED: True,
    _SK_OPTION_COUNT: 3,     # 3 / 4 / 5
    _SK_FAIL_TOAST: True,
}


class CaptchaEverythingPlugin(BasePlugin):
    """Blocks every outgoing message behind a pick-the-emoji captcha."""

    # ---- lifecycle --------------------------------------------------------

    def on_plugin_load(self) -> None:
        self.add_on_send_message_hook()
        # Serialize dialogs: one captcha at a time across all chats.
        self._busy = threading.Lock()
        self.log("Captcha Everything v1.2.0 loaded")

    def on_plugin_unload(self) -> None:
        self.log("Captcha Everything unloaded")

    # ---- settings UI ------------------------------------------------------

    def create_settings(self) -> List[Any]:
        return [
            Header(text="Captcha"),
            Switch(
                key=_SK_ENABLED,
                text="Enable captcha on send",
                subtext="Require picking the correct picture before each outgoing message.",
                default=_DEFAULTS[_SK_ENABLED],
            ),
            Selector(
                key=_SK_OPTION_COUNT,
                text="Number of options",
                items=["3", "4", "5"],
                default=str(_DEFAULTS[_SK_OPTION_COUNT]),
            ),
            Switch(
                key=_SK_FAIL_TOAST,
                text="Show toast on wrong answer",
                default=_DEFAULTS[_SK_FAIL_TOAST],
            ),
            Header(text="How to use"),
            Text(
                text=(
                    "1. Type a message and tap Send.\n"
                    "2. A dialog appears with several pictures.\n"
                    "3. Tap the one that matches the title - the message is sent immediately.\n"
                    "4. Tap the wrong one (or dismiss the dialog) and nothing is sent."
                )
            ),
        ]

    # ---- helpers ----------------------------------------------------------

    def _get_bool(self, key: str) -> bool:
        value = self.get_setting(key, _DEFAULTS[key])
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _get_option_count(self) -> int:
        raw = self.get_setting(_SK_OPTION_COUNT, _DEFAULTS[_SK_OPTION_COUNT])
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = _DEFAULTS[_SK_OPTION_COUNT]
        return max(2, min(6, n))

    def _pick_challenge(self, n: int) -> Tuple[Tuple[str, str], List[Tuple[str, str]]]:
        options = random.sample(_CAPTCHA_POOL, n)
        answer = random.choice(options)
        return answer, options

    # ---- hook -------------------------------------------------------------

    def on_send_message_hook(self, account: int, params: Any) -> HookResult:
        """Block the sending thread until the user answers the captcha."""
        if not self._get_bool(_SK_ENABLED):
            return HookResult(strategy=HookStrategy.DEFAULT)

        # Only one captcha at a time. If another send fires while a dialog
        # is already open, cancel this one silently to avoid dialog stacking.
        if not self._busy.acquire(blocking=False):
            return HookResult(strategy=HookStrategy.CANCEL)

        try:
            option_count = self._get_option_count()
            answer, options = self._pick_challenge(option_count)

            done = threading.Event()
            # result is a one-element list so inner closures can mutate it
            # without needing `nonlocal`.
            result: List[bool] = [False]

            def _show() -> None:
                try:
                    self._show_captcha_dialog(answer, options, result, done)
                except Exception as e:
                    self.log(f"Captcha: dialog failed: {e!r}")
                    done.set()

            try:
                client_utils.run_on_ui_thread(_show)
            except Exception as e:
                # Can't show UI at all: don't lock the user out of the chat.
                self.log(f"Captcha: run_on_ui_thread failed: {e!r}")
                return HookResult(strategy=HookStrategy.DEFAULT)

            # Block this (non-UI) thread until the user picks something
            # or the timeout hits. The app's sending pipeline waits for us
            # to return, so the outcome is applied atomically to THIS send.
            finished = done.wait(_HOOK_WAIT_SECONDS)
            if not finished:
                self.log("Captcha: timed out waiting for user answer")
                return HookResult(strategy=HookStrategy.CANCEL)

            if result[0]:
                return HookResult(strategy=HookStrategy.DEFAULT)
            return HookResult(strategy=HookStrategy.CANCEL)
        finally:
            self._busy.release()

    # ---- dialog -----------------------------------------------------------

    def _show_captcha_dialog(
        self,
        answer: Tuple[str, str],
        options: List[Tuple[str, str]],
        result: List[bool],
        done: threading.Event,
    ) -> None:
        answer_name, _answer_emoji = answer

        activity = client_utils.get_last_fragment().getParentActivity()
        builder = AlertDialogBuilder(activity)
        builder.set_title(f"Pick: {answer_name}")
        builder.set_message("Tap the correct picture to send your message.")
        # Do not let the user dismiss with back-press or tap-outside,
        # otherwise the sending thread would be stuck waiting.
        builder.set_cancelable(False)

        def _finish(correct: bool, picked_name: str = "") -> None:
            result[0] = correct
            if not correct and self._get_bool(_SK_FAIL_TOAST):
                self._toast(f"Wrong. Answer was '{answer_name}'.")
            done.set()

        # AlertDialog has only 3 button slots. For <=3 options use native
        # buttons; for 4-6 render a single-choice list and keep a Cancel
        # button that counts as a wrong answer.
        if len(options) <= 3:
            slots = ["positive", "negative", "neutral"]
            for slot, (name, emoji) in zip(slots, options):
                setter = {
                    "positive": builder.set_positive_button,
                    "negative": builder.set_negative_button,
                    "neutral":  builder.set_neutral_button,
                }[slot]
                is_correct = (name == answer_name)

                def _on_click(_dialog=None, _which=None,
                              correct=is_correct, picked_name=name):
                    _finish(correct, picked_name)

                setter(emoji, _on_click)
        else:
            labels = [emoji for _, emoji in options]

            def _on_select(_dialog=None, which: int = -1):
                if 0 <= which < len(options):
                    picked_name = options[which][0]
                    correct = (picked_name == answer_name)
                else:
                    picked_name = ""
                    correct = False
                _finish(correct, picked_name)

            builder.set_items(labels, _on_select)
            builder.set_negative_button("Cancel", lambda *_: _finish(False))

        # Safety net: if the dialog gets dismissed some other way, treat it
        # as a wrong answer so the sending thread is never stuck.
        try:
            builder.set_on_dismiss_listener(
                lambda *_: (None if done.is_set() else _finish(False))
            )
        except Exception:
            pass

        builder.show()

    # ---- toast ------------------------------------------------------------

    def _toast(self, text: str) -> None:
        try:
            client_utils.show_toast(text)
        except Exception:
            try:
                self.log(f"toast: {text}")
            except Exception:
                pass
