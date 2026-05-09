"""
Captcha Everything — exteraGram plugin.

Requires a captcha (pick-the-correct-emoji) before every outgoing message.
Intended as a self-restraint / parental-control style plugin.

Note on scope:
    The public plugin SDK does not expose a per-keystroke hook on the
    message input field, so "every action" here means every outgoing
    message / edit / media caption that goes through
    on_send_message_hook. Trying to hook TextWatcher on the internal
    ChatActivity is private-API and breaks across updates, so we stay
    in the supported surface.
"""

from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Optional, Tuple

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
__version__ = "1.0.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=12.5.1"
__sdk_version__ = ">=1.4.3.6"


# ---------------------------------------------------------------------------
# Captcha content
# ---------------------------------------------------------------------------

# (name, emoji). The name is what we ask the user to pick; the emoji is the
# button label. Keep the set diverse so buttons look clearly different.
_CAPTCHA_POOL: List[Tuple[str, str]] = [
    ("cat",       "🐱"),
    ("dog",       "🐶"),
    ("fox",       "🦊"),
    ("bear",      "🐻"),
    ("panda",     "🐼"),
    ("lion",      "🦁"),
    ("tiger",     "🐯"),
    ("frog",      "🐸"),
    ("monkey",    "🐵"),
    ("pig",       "🐷"),
    ("owl",       "🦉"),
    ("unicorn",   "🦄"),
    ("apple",     "🍎"),
    ("banana",    "🍌"),
    ("pizza",     "🍕"),
    ("rocket",    "🚀"),
    ("car",       "🚗"),
    ("house",     "🏠"),
    ("heart",     "❤️"),
    ("star",      "⭐"),
]


# Settings keys
_SK_ENABLED       = "enabled"
_SK_OPTION_COUNT  = "option_count"
_SK_FAIL_MESSAGE  = "fail_message"

_DEFAULTS: Dict[str, Any] = {
    _SK_ENABLED: True,
    _SK_OPTION_COUNT: 3,       # 3 / 4 / 5
    _SK_FAIL_MESSAGE: True,    # show toast on wrong answer
}


class CaptchaEverythingPlugin(BasePlugin):
    """Blocks every outgoing message behind a pick-the-emoji captcha."""

    # ---- lifecycle --------------------------------------------------------

    def on_plugin_load(self) -> None:
        # Register the outgoing-message hook.
        self.add_on_send_message_hook()
        # In-memory set of message ids we've already verified, so that after
        # a successful captcha we re-enter on_send_message_hook and let the
        # message pass without asking again.
        self._verified_once: set = set()
        self._verified_lock = threading.Lock()
        self.log("Captcha Everything loaded")

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
                key=_SK_FAIL_MESSAGE,
                text="Show toast on wrong answer",
                default=_DEFAULTS[_SK_FAIL_MESSAGE],
            ),
            Header(text="About"),
            Text(
                text=(
                    "Blocks every outgoing message until you tap the picture "
                    "whose name is shown in the dialog title.\n\n"
                    "Per-keystroke capture is not supported by the plugin SDK, "
                    "so the captcha runs once per send, not per letter."
                )
            ),
        ]

    # ---- helpers ----------------------------------------------------------

    def _get_bool(self, key: str) -> bool:
        value = self.get_setting(key, _DEFAULTS[key])
        # Settings may come back as strings depending on SDK version.
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

    def _message_key(self, params: Any) -> str:
        """A best-effort identifier for a message attempt, so that after a
        successful captcha the re-sent message is not re-intercepted."""
        try:
            msg = getattr(params, "message", None)
            peer = getattr(params, "peer", None) or getattr(params, "dialog_id", None)
            ts = getattr(params, "timestamp", None)
            return f"{peer}|{msg}|{ts}"
        except Exception:
            return f"rand|{random.random()}"

    def _pick_challenge(self, n: int) -> Tuple[Tuple[str, str], List[Tuple[str, str]]]:
        options = random.sample(_CAPTCHA_POOL, n)
        answer = random.choice(options)
        return answer, options

    # ---- hook -------------------------------------------------------------

    def on_send_message_hook(self, account: int, params: Any) -> HookResult:
        # Respect the enable toggle.
        if not self._get_bool(_SK_ENABLED):
            return HookResult(strategy=HookStrategy.DEFAULT)

        key = self._message_key(params)

        # Re-entry after a successful captcha: let the message through once.
        with self._verified_lock:
            if key in self._verified_once:
                self._verified_once.discard(key)
                return HookResult(strategy=HookStrategy.DEFAULT)

        # Otherwise, cancel the send and show the captcha on the UI thread.
        option_count = self._get_option_count()
        answer, options = self._pick_challenge(option_count)

        def _show_dialog() -> None:
            self._show_captcha_dialog(account, params, key, answer, options)

        client_utils.run_on_ui_thread(_show_dialog)

        return HookResult(strategy=HookStrategy.CANCEL)

    # ---- dialog -----------------------------------------------------------

    def _show_captcha_dialog(
        self,
        account: int,
        params: Any,
        key: str,
        answer: Tuple[str, str],
        options: List[Tuple[str, str]],
    ) -> None:
        answer_name, answer_emoji = answer

        builder = AlertDialogBuilder(client_utils.get_last_fragment().getParentActivity())
        builder.set_title(f"Pick: {answer_name}")
        builder.set_message(
            "Tap the correct picture to send your message.\n"
            "Wrong answer cancels the send."
        )
        builder.set_cancelable(True)

        # AlertDialog only gives us 3 slots (positive / negative / neutral),
        # so for up to 3 options we use native buttons, and for 4-6 options
        # we render them as a single-choice list.
        if len(options) <= 3:
            slots = ["positive", "negative", "neutral"]
            for slot, (name, emoji) in zip(slots, options):
                setter = {
                    "positive": builder.set_positive_button,
                    "negative": builder.set_negative_button,
                    "neutral":  builder.set_neutral_button,
                }[slot]
                is_correct = (name == answer_name)

                def _on_click(_dialog=None, _which=None, correct=is_correct):
                    self._handle_answer(correct, account, params, key, answer, options)

                setter(emoji, _on_click)
        else:
            labels = [emoji for _, emoji in options]

            def _on_select(_dialog=None, which: int = -1):
                if 0 <= which < len(options):
                    name, _ = options[which]
                    correct = (name == answer_name)
                else:
                    correct = False
                self._handle_answer(correct, account, params, key, answer, options)

            builder.set_items(labels, _on_select)
            builder.set_negative_button("Cancel", lambda *_: self._toast("Cancelled."))

        builder.show()

    def _handle_answer(
        self,
        correct: bool,
        account: int,
        params: Any,
        key: str,
        answer: Tuple[str, str],
        options: List[Tuple[str, str]],
    ) -> None:
        if correct:
            # Mark this message as pre-approved and re-send via client_utils,
            # which re-enters on_send_message_hook; the key check lets it pass.
            with self._verified_lock:
                self._verified_once.add(key)
            try:
                self._resend(account, params)
            except Exception as e:
                self.log(f"Captcha: resend failed: {e!r}")
                self._toast("Could not resend message.")
                with self._verified_lock:
                    self._verified_once.discard(key)
            return

        # Wrong answer: do nothing further. Optionally toast.
        if self._get_bool(_SK_FAIL_MESSAGE):
            self._toast(f"Wrong. Answer was {answer[1]} ({answer[0]}).")

    # ---- resend -----------------------------------------------------------

    def _resend(self, account: int, params: Any) -> None:
        """Try a few known shapes of client_utils.send_message across SDK versions."""
        text = getattr(params, "message", None)
        peer = getattr(params, "peer", None) or getattr(params, "dialog_id", None)

        # Newer SDKs: re-emit params directly.
        send_params = getattr(client_utils, "send_message", None)
        if callable(send_params):
            try:
                send_params(params)  # type: ignore[arg-type]
                return
            except TypeError:
                pass
            try:
                send_params(account, params)  # type: ignore[arg-type]
                return
            except TypeError:
                pass
            if text is not None and peer is not None:
                send_params(peer, text)  # type: ignore[arg-type]
                return

        raise RuntimeError("client_utils.send_message not available")

    # ---- toast ------------------------------------------------------------

    def _toast(self, text: str) -> None:
        try:
            client_utils.show_toast(text)
        except Exception:
            # Fallback: nothing more we can do without UI.
            self.log(f"toast: {text}")
