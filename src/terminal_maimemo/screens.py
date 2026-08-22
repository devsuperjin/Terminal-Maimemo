"""Terminal UI for the maimemo web-study app (textual)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ItemGrid, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Static

from . import audio as audio_mod
from . import auth as auth_mod
from . import config as config_mod
from . import proto
from .client import MaimemoClient, MaimemoError

# StudyResponse enum values
RESP_FAMILIAR = 1
RESP_VAGUE = 2
RESP_FORGET = 3
RESP_WELL_FAMILIAR = 4

GRADE_LABELS = {
    RESP_FAMILIAR: ("Familiar", "1"),
    RESP_VAGUE: ("Vague", "2"),
    RESP_FORGET: ("Forget", "3"),
    RESP_WELL_FAMILIAR: ("Mastered", "4"),
}


def _toast(screen: Screen, message: str, title: str, severity: str = "error") -> None:
    """Show a toast notification (error/warning/information)."""
    screen.notify(message, title=title, severity=severity, timeout=8)


class LoginScreen(Screen):
    """Login: paste token, SMS code login, or password login."""

    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(
            Vertical(
                Static("[b]MaiMemo — Terminal Study[/]", classes="title"),
                Static("Sign in to start studying", classes="subtitle"),
                Static("1. Paste login credential", classes="section"),
                Input(placeholder="sid cookie / token value (copy from browser)", id="token-input"),
                Button("Save credential", id="token-save", variant="primary"),
                Static("2. SMS code sign-in", classes="section"),
                Horizontal(
                    Input(placeholder="Phone number", id="phone-input"),
                    Button("Send code", id="send-code"),
                ),
                Input(placeholder="Verification code", id="code-input"),
                Button("Sign in", id="sms-login", variant="primary"),
                Static("3. Password sign-in", classes="section"),
                Input(placeholder="Phone / email", id="pwd-identity"),
                Input(placeholder="Password", password=True, id="pwd-password"),
                Button("Password sign-in", id="pwd-login", variant="primary"),
                Static("", id="login-status", classes="status"),
                id="login-box",
            ),
            id="login-scroll",
        )
        yield Footer()

    def on_mount(self) -> None:
        cfg = config_mod.load_config()
        if cfg.get("phone"):
            self.query_one("#phone-input", Input).value = cfg["phone"]

    @on(Button.Pressed, "#token-save")
    def _save_token(self) -> None:
        sid = config_mod.extract_credential(self.query_one("#token-input", Input).value)
        if not sid:
            _toast(self, "Enter a sid / token (or a cookie string)", "Cannot sign in", "error")
            self._status("Enter a credential", error=True)
            return
        config_mod.save_sid(sid)
        _toast(self, "Credential saved", "Success", "information")
        self.app.switch_to_study()  # type: ignore[attr-defined]

    @on(Button.Pressed, "#send-code")
    @work(thread=False, exclusive=True)
    async def _send_code(self) -> None:
        phone = self.query_one("#phone-input", Input).value.strip()
        if not phone:
            _toast(self, "Enter your phone number", "Cannot send", "error")
            return
        self._status("Sending code…")
        try:
            oidc = auth_mod.OidcClient()
            self.app._oidc = oidc  # keep session for the login step  # type: ignore[attr-defined]
            await oidc.begin()
            await oidc.send_sms_code(phone)
            cfg = config_mod.load_config()
            cfg["phone"] = phone
            config_mod.save_config(cfg)
            self._status("Code sent to " + phone)
            _toast(self, f"Code sent to {phone} (resend in 60s)", "Code sent", "information")
        except Exception as exc:
            self._status(f"Send failed: {exc}", error=True)
            _toast(self, str(exc), "Failed to send code")

    @on(Button.Pressed, "#sms-login")
    @work(thread=False, exclusive=True)
    async def _sms_login(self) -> None:
        phone = self.query_one("#phone-input", Input).value.strip()
        code = self.query_one("#code-input", Input).value.strip()
        if not phone or not code:
            _toast(self, "Enter your phone number and code", "Cannot sign in", "error")
            return
        self._status("Signing in…")
        try:
            oidc: auth_mod.OidcClient | None = getattr(self.app, "_oidc", None)
            if oidc is not None and oidc.uid:
                token = await oidc.login_with_code(phone, code)
            else:
                token = await auth_mod.login_with_sms(phone, code)
            config_mod.save_sid(token)
            cfg = config_mod.load_config()
            cfg["phone"] = phone
            config_mod.save_config(cfg)
            self._status("Signed in")
            _toast(self, "Signed in — opening study screen…", "Welcome", "information")
            self.app.switch_to_study()  # type: ignore[attr-defined]
        except Exception as exc:
            self._status(f"Sign-in failed: {exc}", error=True)
            _toast(self, str(exc), "Sign-in failed")

    @on(Button.Pressed, "#pwd-login")
    @work(thread=False, exclusive=True)
    async def _pwd_login(self) -> None:
        identity = self.query_one("#pwd-identity", Input).value.strip()
        password = self.query_one("#pwd-password", Input).value
        if not identity or not password:
            _toast(self, "Enter your account and password", "Cannot sign in", "error")
            return
        self._status("Signing in…")
        try:
            token = await auth_mod.login_with_password(identity, password)
            config_mod.save_sid(token)
            self._status("Signed in")
            _toast(self, "Signed in — opening study screen…", "Welcome", "information")
            self.app.switch_to_study()  # type: ignore[attr-defined]
        except Exception as exc:
            self._status(f"Sign-in failed: {exc}", error=True)
            _toast(self, str(exc), "Sign-in failed")

    def _status(self, text: str, error: bool = False) -> None:
        w = self.query_one("#login-status", Static)
        w.update(f"[{'red' if error else 'green'}]{text}[/]")

    def action_quit(self) -> None:
        self.app.exit()  # type: ignore[attr-defined]


class StudyScreen(Screen):
    """The recitation screen: shows a word, lets you grade it."""

    BINDINGS = [
        Binding("space", "reveal", "Answer"),
        # Keep the footer compact; 2–4 remain active but are covered by the
        # single "Rate 1-4" entry shown for the 1 key.
        Binding("1", "grade_familiar", "Rate 1-4"),
        Binding("2", "grade_vague", "Vague", show=False),
        Binding("3", "grade_forget", "Forget", show=False),
        Binding("4", "grade_well", "Mastered", show=False),
        Binding("backspace", "prev_word", "Prev"),
        Binding("r", "review_more", "More", show=False),
        Binding("p", "play_audio", "Sound", show=False),
        Binding("c", "reconnect", "Reconnect", show=False),
        Binding("l", "logout", "Logout", show=False),
        Binding("b", "toggle_grade_buttons", "Buttons", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
    ]

    MEMORY_HISTORY_COLORS = {
        1: "#82d9be",
        2: "#f4c96b",
        3: "#f2a38f",
        4: "#82d9be",
        5: "#aeb6c1",
        6: "#f2a38f",
        10: "#8db8f2",
        11: "#aeb6c1",
    }

    def __init__(self, client: MaimemoClient) -> None:
        super().__init__()
        self.client = client
        self.current: dict[str, Any] = {}
        self.revealed = False
        self._shown_at = 0.0
        self._revealed_at = 0.0
        self._busy = False
        self._show_grade_buttons = bool(config_mod.load_config().get("show_grade_buttons", False))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(
            Static("", id="progress", classes="progress"),
            Static("", id="word", classes="word"),
            Static("", id="phonetics", classes="phonetics"),
            Static("", id="answer", classes="answer"),
            Horizontal(
                Button("SPACE · Reveal", id="reveal-button", variant="primary"),
                id="reveal-controls",
            ),
            ItemGrid(
                Button("Familiar", id="grade-familiar-button", variant="success"),
                Button("Vague", id="grade-vague-button", variant="warning"),
                Button("Forget", id="grade-forget-button", variant="error"),
                Button("Mastered", id="grade-mastered-button", variant="primary"),
                min_column_width=16,
                id="grade-buttons",
            ),
            Static("", id="extra", classes="extra"),
            Static("", id="history", classes="history"),
            Static("", id="status", classes="status"),
            id="study-box",
        )
        yield Footer()

    # ------------------------------------------------------------- lifecycle
    async def on_mount(self) -> None:
        self._update_grade_buttons()
        self.run_worker(self._watch_connection(), exclusive=True, group="conn")
        self.run_worker(self._setup(), exclusive=True)

    async def _setup(self) -> None:
        try:
            await self.client.connect()
            user = self.client.user
            name = user.get("name") or f"ID {user.get('id')}"
            self._status(f"Connected · user {name}")
            states = await self.client.get_study_states()
            self._render_states(states)
            await self._load_word()
        except MaimemoError as exc:
            self._status(f"Connection failed: {exc}", error=True)
            _toast(self, str(exc), "Connection failed")
        except Exception as exc:  # pragma: no cover
            self._status(f"Connection failed: {exc}", error=True)
            _toast(self, str(exc), "Connection failed")

    async def _watch_connection(self) -> None:
        """Notify when the websocket drops unexpectedly; survive reconnects."""
        while self.is_active:
            closed = self.client._closed
            await closed.wait()
            if self.client.unexpected_close:
                self._status("Connection lost", error=True)
                _toast(self, "Connection lost (network or idle timeout) — press c to reconnect", "Disconnected")
            # wait until a reconnect replaces the closed event (or screen closes)
            while self.is_active and closed is self.client._closed:
                await asyncio.sleep(0.5)

    # ------------------------------------------------------------- helpers
    def _status(self, text: str, error: bool = False) -> None:
        color = "red" if error else "green"
        self.query_one("#status", Static).update(f"[{color}]{text}[/]")

    def _render_states(self, states: dict[str, Any]) -> None:
        book = states.get("study_book") or {}
        prog = states.get("study_progress") or {}
        parts = []
        if book:
            parts.append(f"Book: {book.get('name') or book.get('catalog_name') or '?'}")
        if prog:
            parts.append(f"Progress: {prog.get('finished', 0)}/{prog.get('total', 0)}")
        if states.get("learned_count") is not None:
            parts.append(f"Learned {states.get('learned_count')}")
        if parts:
            self.query_one("#progress", Static).update("  ".join(parts))

    def _setting(self, key: str, default: Any = None) -> Any:
        """Read a setting, unwrapping double-wrapped values (like the web app)."""
        value = self.client.settings.get(key, default)
        if isinstance(value, dict):
            for k in ("string_value", "bool_value", "number_value"):
                if k in value:
                    return value[k]
        return value

    def _method_value(self) -> int:
        method = self._setting("study.algorithm.study_method", "EC")
        return 1 if method == "CE" else 0

    def _update_grade_buttons(self) -> None:
        self.query_one("#reveal-controls", Horizontal).display = self._show_grade_buttons
        self.query_one("#grade-buttons", ItemGrid).display = self._show_grade_buttons
        has_word = bool(self.current.get("word"))
        self.query_one("#reveal-button", Button).disabled = (
            self._busy or self.revealed or not has_word
        )
        for button_id in (
            "#grade-familiar-button",
            "#grade-vague-button",
            "#grade-forget-button",
            "#grade-mastered-button",
        ):
            self.query_one(button_id, Button).disabled = (
                self._busy or not self.revealed or not has_word
            )

    def _render_word(self) -> None:
        word = self.current.get("word") or {}
        if not word:
            self.query_one("#word", Static).update("[yellow](no word data)[/]")
            return
        spelling = word.get("spelling", "")
        self.query_one("#word", Static).update(f"[bold #7ae0ff]{spelling}[/]")
        phon = " ".join(
            f"[dim]{p}[/]" for p in (word.get("phonetic_us"), word.get("phonetic_uk")) if p
        )
        self.query_one("#phonetics", Static).update(phon)
        self._render_answer(show=self.revealed)
        self._update_grade_buttons()

    def _render_answer(self, show: bool) -> None:
        if not show:
            self.query_one("#answer", Static).update("[dim]Press [b]SPACE[/] to reveal the answer[/]")
            return
        lines: list[str] = []
        for it in self.current.get("interpretations", []):
            tags = "".join(f"[{t}]" for t in it.get("tags", []))
            lines.append(f"{tags} {it.get('interpretation', '')}")
        for ph in self.current.get("phrases", []):
            lines.append(f"[cyan]{ph.get('phrase', '')}[/] — {ph.get('interpretation', '')}")
        for n in self.current.get("notes", []):
            lines.append(f"[magenta]{n.get('type', '')}:[/] {n.get('note', '')}")
        self.query_one("#answer", Static).update("\n".join(lines) or "[dim](no meaning)[/]")

    def _render_extra(self) -> None:
        prog = self.current.get("progress") or {}
        parts = []
        if prog:
            parts.append(f"Group progress {prog.get('finished', 0)}/{prog.get('total', 0)}")
        predicts = self.current.get("response_predicts") or []
        if predicts:
            days = "/".join(str(p.get("days")) for p in predicts if p.get("days"))
            parts.append(f"Next review: {days} day(s)")
        self.query_one("#extra", Static).update("  ".join(parts))
        self._render_history()

    @staticmethod
    def _format_history_date(value: Any) -> str:
        if not isinstance(value, (int, float)) or value <= 0:
            return ""
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")

    @staticmethod
    def _format_history_day(day: Any) -> str:
        # The API uses -1 as a calendar marker for activity recorded today.
        if day == -1:
            return "Today"
        return f"D{day}" if day is not None else "D?"

    def _render_history(self) -> None:
        """Render memory history as compact colored markers."""
        history = self.current.get("memory_history") or {}
        lines: list[str] = []
        first = self._format_history_date(history.get("first_study_date"))
        last = self._format_history_date(history.get("last_study_date"))
        times = history.get("study_times")
        if first or last or times:
            date_range = " → ".join(part for part in (first, last) if part)
            summary = date_range or "Memory history"
            if times:
                summary += f"  · {times} review(s)"
            lines.append(f"[bold #d5d9e2]Memory history[/]  {summary}")
        if history.get("is_error"):
            lines.append("[red]Memory history unavailable[/]")
        markers: list[str] = []
        for item in history.get("items") or []:
            kind = item.get("type", 0)
            if isinstance(kind, str):
                kind = proto.enum_value("WebStudyMemoryHistoryItemType", kind)
            color = self.MEMORY_HISTORY_COLORS.get(kind, "#b8c0cc")
            day_text = self._format_history_day(item.get("day"))
            markers.append(f"[black on {color}] {day_text} [/]")
        if markers:
            lines.append(" ".join(markers))
        self.query_one("#history", Static).update("  ".join(lines))

    # ------------------------------------------------------------- actions
    async def _load_word(self, back: bool = False) -> None:
        self._busy = True
        self._update_grade_buttons()
        try:
            resp = await self.client.get_word(back=back)
            self.current = resp
            self.revealed = False
            self._shown_at = time.monotonic()
            self._render_word()
            self._render_extra()
            self._status("")
            self.run_worker(self._autoplay(), exclusive=False, group="audio")
        except MaimemoError as exc:
            self._status(f"Failed to load word: {exc}", error=True)
            _toast(self, str(exc), "Load failed")
        finally:
            self._busy = False
            self._update_grade_buttons()

    def action_reveal(self) -> None:
        if self.revealed or self._busy:
            return
        if not self.current:
            return
        self.revealed = True
        self._revealed_at = time.monotonic()
        self._render_answer(show=True)
        self._render_extra()
        self._update_grade_buttons()

    async def _grade(self, response: int) -> None:
        # A grade is only valid after the user explicitly reveals the answer.
        # Do not implicitly reveal and submit when a number key is pressed.
        if self._busy or not self.current or not self.revealed:
            return
        word = self.current.get("word") or {}
        if not word:
            return
        recall_ms = int((self._revealed_at - self._shown_at) * 1000)
        study_ms = int((time.monotonic() - self._revealed_at) * 1000)
        self._busy = True
        try:
            label = GRADE_LABELS.get(response, ("?", ""))[0]
            self._status(f"Answered: {label}")
            resp = await self.client.submit_response(
                word.get("id", ""),
                response,
                study_method=self._method_value(),
                recall_duration=max(recall_ms, 0),
                study_duration=max(study_ms, 0),
            )
            nxt = (resp or {}).get("next") or {}
            if nxt.get("word"):
                self.current = nxt
                self.revealed = False
                self._shown_at = time.monotonic()
                self._render_word()
                self._render_extra()
                self._status("")
                self.run_worker(self._autoplay(), exclusive=False, group="audio")
            else:
                await self._load_word()
        except MaimemoError as exc:
            self._status(f"Submit failed: {exc}", error=True)
            _toast(self, f"Submit failed: {exc} (press c to reconnect and retry)", "Submit failed")
        finally:
            self._busy = False

    def action_grade_familiar(self) -> None:
        self.run_worker(self._grade(RESP_FAMILIAR), exclusive=True, group="grade")

    def action_grade_vague(self) -> None:
        self.run_worker(self._grade(RESP_VAGUE), exclusive=True, group="grade")

    def action_grade_forget(self) -> None:
        self.run_worker(self._grade(RESP_FORGET), exclusive=True, group="grade")

    def action_grade_well(self) -> None:
        self.run_worker(self._grade(RESP_WELL_FAMILIAR), exclusive=True, group="grade")

    @on(Button.Pressed, "#grade-familiar-button")
    def _press_grade_familiar(self) -> None:
        self.action_grade_familiar()

    @on(Button.Pressed, "#grade-vague-button")
    def _press_grade_vague(self) -> None:
        self.action_grade_vague()

    @on(Button.Pressed, "#grade-forget-button")
    def _press_grade_forget(self) -> None:
        self.action_grade_forget()

    @on(Button.Pressed, "#grade-mastered-button")
    def _press_grade_mastered(self) -> None:
        self.action_grade_well()

    @on(Button.Pressed, "#reveal-button")
    def _press_reveal(self) -> None:
        self.action_reveal()

    def action_toggle_grade_buttons(self) -> None:
        cfg = config_mod.load_config()
        self._show_grade_buttons = not bool(cfg.get("show_grade_buttons", False))
        cfg["show_grade_buttons"] = self._show_grade_buttons
        config_mod.save_config(cfg)
        self._update_grade_buttons()
        state = "enabled" if self._show_grade_buttons else "disabled"
        _toast(self, f"Grading buttons {state}", "Study controls", "information")

    def action_prev_word(self) -> None:
        self.run_worker(self._load_word(back=True), exclusive=True, group="grade")

    def action_review_more(self) -> None:
        self.run_worker(self._review_more(), exclusive=True, group="misc")

    async def _review_more(self) -> None:
        cfg = config_mod.load_config()
        count = int(cfg.get("review_more_count", 10))
        self._status(f"Requesting {count} more…")
        try:
            await self.client.review_more(count)
            self._status(f"Added {count} review words")
            _toast(self, f"Added {count} review words", "More review", "information")
        except MaimemoError as exc:
            self._status(f"Failed: {exc}", error=True)
            _toast(self, str(exc), "More review failed")

    def action_play_audio(self) -> None:
        self.run_worker(self._play_audio(silent=False), exclusive=True, group="audio")

    async def _play_audio(self, silent: bool) -> None:
        word = self.current.get("word") or {}
        accent = self._setting("app.speech.setting.voc.accent", "")
        url = audio_mod.choose_pronunciation(word, accent)
        if not url:
            if not silent:
                _toast(self, "No pronunciation file for this word", "Pronunciation", "warning")
            return
        ok = await audio_mod.play_mp3(url)
        if not ok and not silent:
            _toast(
                self,
                "Cannot play audio. Run `terminal_maimemo --audio-diagnose` to inspect the audio environment",
                "Pronunciation",
                "warning",
            )

    async def _autoplay(self) -> None:
        enabled = self._setting("app.speech.setting.voc.auto_play.before_answer.enabled", False)
        if enabled:
            await self._play_audio(silent=True)

    def action_reconnect(self) -> None:
        self.run_worker(self._reconnect(), exclusive=True, group="misc")

    async def _reconnect(self) -> None:
        self._status("Reconnecting…")
        try:
            await self.client.reconnect()
            user = self.client.user
            name = user.get("name") or f"ID {user.get('id')}"
            self._status(f"Reconnected · user {name}")
            states = await self.client.get_study_states()
            self._render_states(states)
            await self._load_word()
            _toast(self, "Reconnected", "Reconnected", "information")
        except MaimemoError as exc:
            self._status(f"Reconnect failed: {exc}", error=True)
            _toast(self, str(exc), "Reconnect failed")

    def action_logout(self) -> None:
        self.run_worker(self._logout(), exclusive=True, group="misc")

    async def _logout(self) -> None:
        config_mod.clear_sid()
        await self.client.close()
        _toast(self, "Signed out", "Bye", "information")
        self.app.switch_to_login()  # type: ignore[attr-defined]

    def action_quit(self) -> None:
        self.app.exit()  # type: ignore[attr-defined]
