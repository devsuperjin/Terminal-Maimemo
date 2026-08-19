"""Maimemo terminal app entry point (textual)."""

from __future__ import annotations

import logging

from textual.app import App
from textual.binding import Binding

from . import config as config_mod
from .client import MaimemoClient
from .screens import LoginScreen, StudyScreen

log = logging.getLogger(__name__)


class MaimemoApp(App):
    """MaiMemo web-study terminal client."""

    TITLE = "MaiMemo · Terminal Study"
    SUB_TITLE = "maimemo web-study TUI"
    CSS_PATH = "app.tcss"

    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.client: MaimemoClient | None = None

    def on_mount(self) -> None:
        self._bootstrap()

    def _bootstrap(self) -> None:
        if config_mod.load_sid():
            self.switch_to_study()
        else:
            self.push_screen(LoginScreen())

    def switch_to_study(self) -> None:
        sid = config_mod.load_sid()
        if not sid:
            self.push_screen(LoginScreen())
            return
        self.client = MaimemoClient(sid)
        self.push_screen(StudyScreen(self.client))

    def switch_to_login(self) -> None:
        self.push_screen(LoginScreen())

    def action_quit(self) -> None:
        self.exit()


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(prog="terminal_maimemo", description="MaiMemo vocabulary terminal client")
    parser.add_argument(
        "--sid",
        "--token",
        dest="sid",
        metavar="SID",
        help="Save the login credential (sid cookie value, or a cookie/token string) and launch",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear saved credentials and exit",
    )
    parser.add_argument(
        "--audio-diagnose",
        action="store_true",
        help="Print audio environment diagnostics (players, default device, ALSA devices)",
    )
    parser.add_argument(
        "--audio-test",
        action="store_true",
        help="Play a test tone and report each playback method's result",
    )
    args = parser.parse_args()

    if args.audio_diagnose:
        from . import audio as audio_mod

        print(audio_mod.diagnose())
        return
    if args.audio_test:
        from . import audio as audio_mod

        print(audio_mod.play_tone_diagnose())
        return

    if args.reset:
        config_mod.clear_sid()
        print("Login info cleared")
        return
    if args.sid:
        sid = config_mod.extract_credential(args.sid)
        if not sid:
            print("Unrecognized credential — paste a sid or cookie string")
            return
        config_mod.save_sid(sid)
        print("Credential saved")

    MaimemoApp().run()


if __name__ == "__main__":
    main()
