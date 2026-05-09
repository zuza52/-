"""
Captcha Everything — exteraGram plugin.

Requires a captcha (pick-the-correct-emoji) before every outgoing message.
Intended as a self-restraint / anti-impulse send filter.

How it works now (v1.1.0):
    On the first attempt to send a message the plugin shows a picture-pick
    captcha and CANCELS the send. The user does NOT lose the typed text -
    it stays in the input field. On a correct answer the plugin arms a
    one-shot "pass" flag; the user simply taps send again and the message
    flies through untouched. On a wrong answer nothing is armed and the
    next send will show a fresh captcha.

    This avoids any fragile re-send through internal APIs, which is what
    broke v1.0.0 ("messages stopped sending after picking").

Note on scope:
    The public plugin SDK does not expose a per-keystroke hook on the
    message input field, so "every action" here means every outgoing
    message / edit / media caption that goes through
    on_send_message_hook.
"""

from __future__ import annotations

import random
import threading
import time
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
__version__ = "1.1.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=12.5.1"
__sdk_version__ = ">=1.4.3.6"


# ---------------------------------------------------------------------------
# Captcha content
# ---------------------------------------------------------------------------

# (name, emoji). The name is what we ask the user to pick; the emoji is the
# button label. Keep the set diverse so buttons look clearly different.
_CAPTCHA_POOL: List[Tuple[str, str]] = [
    ("cat",     "🐱"),
    ("dog",     "🐶"),
    ("fox",     "🦊"),
    ("bear",    "🐻"),
    ("panda",   "🐼"),
    ("lion",    "🦁"),
    ("tiger",   "🐯"),
    ("frog",    "🐸"),
    ("monkey",  "🐵"),
    ("pig",     "🐷"),
    ("owl",     "🦉"),
    ("unicorn", "🦄"),
    ("apple",   "🍎"),
    ("banana",  "🍌"),
    ("pizza",   "🍕"),
    ("rocket",  "🚀"),
    ("car",     "🚗"),
    ("house",   "🏠"),
    ("heart",   "❤️"),
    ("star",    "⭐"),
]

# How long (seconds) the one-shot "pass" remains armed after a correct answer.
# If the user doesn't hit send within this window, the pass is discarded and
# the next attempt will show a captcha again.
_PASS_TTL_SECONDS = 30.0


# Settings keys
_SK_ENABLED      = "enabled"
_SK_OPTION_COUNT = "option_count"
_SK_FAIL_TOAST   = "fail_toast"

_DEFAULTS: Dict[str, Any] = {
    _SK_ENABLED: True,
    _SK_OPTION_COUNT: 3,     # 3 / 4 / 5
    _SK_FAIL_TOAST: True,    # show toast on wrong answer
}


class CaptchaEverythingPlugin(BasePlugin):
    """Blocks every outgoing message behind a pick-the-emoji captcha."""

    # ---- lifecycle --------------------------------------------------------

    def on_plugin_load(self) -> None:
        self.add_on_send_message_hook()

        # One-shot pass: after a correct captcha, the *next* send (within
        # _PASS_TTL_SECONDS) is allowed to go through. Then the pass is
        # consumed and the next send requires a new captcha.
        self._pass_until: float = 0.0
        self._lock = threading.Lock()

        # Anti-spam: if the user is already answering a captcha, don't pop
        # a second dialog on top when something else fires the hook.
        self._dialog_open: bool = False

        self.log("Captcha Everything v1.1.0 loaded")

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
                    "2. A dialog appears asking you to pick a picture.\n"
                    "3. If you pick correctly, your typed text stays in the input - "
                    "just tap Send once more and the message flies through.\n"
                    "4. If you pick wrong, nothing is sent; try again.\n\n"
                    "The one-shot pass expires in 30 seconds after a correct "
                    "answer, so leaving the chat cancels the bypass."
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

    def _pass_active(self) -> bool:
        return time.monotonic() < self._pass_until

    def _consume_pass(self) -> None:
        self._pass_until = 0.0

    def _arm_pass(self) -> None:
        self._pass_until = time.monotonic() + _PASS_TTL_SECONDS

    # ---- hook -------------------------------------------------------------

    def on_send_message_hook(self, account: int, params: Any) -> HookResult:
        # Plugin disabled -> do nothing.
        if not self._get_bool(_SK_ENABLED):
            return HookResult(strategy=HookStrategy.DEFAULT)

        with self._lock:
            # One-shot pass armed by a previous successful captcha:
            # let this send through and disarm.
            if self._pass_active():
                self._consume_pass()
                return HookResult(strategy=HookStrategy.DEFAULT)

            # Already showing a dialog: drop silently to avoid stacking.
            if self._dialog_open:
                return HookResult(strategy=HookStrategy.CANCEL)

            self._dialog_open = True

        # Show captcha on the UI thread and cancel this send.
        option_count = self._get_option_count()
        answer, options = self._pick_challenge(option_count)

        def _show():
            try:
                self._show_captcha_dialog(answer, options)
            except Exception as e:
                self.log(f"Captcha: dialog failed: {e!r}")
                with self._lock:
                    self._dialog_open = False

        try:
            client_utils.run_on_ui_thread(_show)
        except Exception as e:
            # If we can't even post to UI, don't lock the user out - just
            # disarm and let sends pass to avoid bricking the chat.
            self.log(f"Captcha: run_on_ui_thread failed: {e!r}")
            with self._lock:
                self._dialog_open = False
            return HookResult(strategy=HookStrategy.DEFAULT)

        return HookResult(strategy=HookStrategy.CANCEL)

    # ---- dialog -----------------------------------------------------------

    def _show_captcha_dialog(
        self,
        answer: Tuple[str, str],
        options: List[Tuple[str, str]],
    ) -> None:
        answer_name, _answer_emoji = answer

        activity = client_utils.get_last_fragment().getParentActivity()
        builder = AlertDialogBuilder(activity)
        builder.set_title(f"Pick: {answer_name}")
        builder.set_message(
            "Tap the correct picture. If correct, your message stays in "
            "the input - just tap Send again to deliver it."
        )
        builder.set_cancelable(True)

        # Called when the dialog is dismissed any way (button / back / tap-out)
        # to make sure we never leave _dialog_open stuck True.
        def _on_dismiss(*_args, **_kwargs):
            with self._lock:
                self._dialog_open = False

        # AlertDialog has only 3 button slots. For <=3 options use native
        # buttons; for 4-6 render a single-choice list.
        if len(options) <= 3:
            slots = ["positive", "negative", "neutral"]
            for slot, (name, emoji) in zip(slots, options):
                setter = {
                    "positive": builder.set_positive_button,
                    "negative": builder.set_negative_button,
                    "neutral":  builder.set_neutral_button,
                }[slot]
                is_correct = (name == answer_name)

                def _on_click(_dialog=None, _which=None, correct=is_correct, picked_name=name):
                    self._handle_answer(correct, answer_name, picked_name)
                    _on_dismiss()

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
                self._handle_answer(correct, answer_name, picked_name)
                _on_dismiss()

            builder.set_items(labels, _on_select)
            builder.set_negative_button(
                "Cancel",
                lambda *_: (self._toast("Cancelled."), _on_dismiss()),
            )

        # Some SDKs expose set_on_dismiss_listener; if so, wire it too.
        try:
            builder.set_on_dismiss_listener(_on_dismiss)
        except Exception:
            pass

        builder.show()

    # ---- answer handling --------------------------------------------------

    def _handle_answer(self, correct: bool, answer_name: str, picked_name: str) -> None:
        if correct:
            with self._lock:
                self._arm_pass()
            self._toast("Correct - tap Send again to deliver.")
            return

        if self._get_bool(_SK_FAIL_TOAST):
            self._toast(f"Wrong. Answer was '{answer_name}'.")
        # Make sure no stale pass is left.
        with self._lock:
            self._consume_pass()

    # ---- toast ------------------------------------------------------------

    def _toast(self, text: str) -> None:
        try:
            client_utils.show_toast(text)
        except Exception:
            try:
                self.log(f"toast: {text}")
            except Exception:
                pass
