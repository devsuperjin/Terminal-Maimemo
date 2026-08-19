# terminal_maimemo

A terminal client for the MaiMemo (墨墨背单词) **web-study** app, built with
[textual](https://github.com/Textualize/textual). Recite vocabulary, review,
and get pronunciation audio right in your terminal.

Managed with [uv](https://docs.astral.sh/uv/).

---

## Quick start

```bash
uv sync                       # install deps (textual / httpx / websockets / miniaudio)
uv run terminal_maimemo       # launch
```

Sign-in options (on the login screen):

1. **Password sign-in** — phone/email + password (recommended).
2. **SMS code sign-in** — enter your phone number → "Send code" → enter the code.
3. **Paste login credential** — the credential is the **`sid` cookie** the
   server issues after sign-in (`tc-apis.maimemo.com`, `Path=/study`, valid
   7 days). After signing in to the web app in your browser, press F12 →
   Console and run `document.cookie` (or find `sid` under Application →
   Cookies), then paste `sid=...` or the whole cookie string into the app
   (or `uv run terminal_maimemo --sid <sid>`).

Credentials are stored in `~/.terminal_maimemo/config.json` (override the
directory with the `MAIMEMO_CONFIG_DIR` env var). `uv run terminal_maimemo --reset`
clears them.

## Keybindings (study screen)

| Key | Action |
| --- | --- |
| `Space` | Reveal the answer |
| `1` / `2` / `3` / `4` | Familiar / Vague / Forget / Mastered |
| `Backspace` | Previous word |
| `r` | More review (count from `review_more_count`) |
| `p` | Play the current word's pronunciation (real MP3, no TTS) |
| `c` | Reconnect (restore the session after a drop) |
| `l` | Log out |
| `Ctrl+Q` | Quit |

Failures (connect, submit, disconnect, audio…) show as toasts in the top-right corner.

## Commands

```bash
uv run terminal_maimemo --sid <SID>       # save the sid cookie and open the study screen
uv run terminal_maimemo --reset           # clear saved credentials
uv run terminal_maimemo --audio-diagnose  # print audio environment (default device/players/ALSA devices)
uv run terminal_maimemo --audio-test      # play a test tone and report each playback method
uv run python scripts/live_test.py        # read-only connectivity test (never submits an answer)
uv run python tests/test_proto.py         # protobuf codec self-tests
uv run python tests/test_client.py        # frame-protocol self-tests
```

---

## Reverse-engineered API

Everything comes from analyzing the web app's front-end JS bundles at
<https://tc-apis.maimemo.com/webstudy/app> (`app.7a092439.js` /
`common.e6c8186d.js` / `vendors.1c3c998e.js`, uniapp/Taro + ts-proto output);
no private documentation was used.

### 1. Sign-in (OIDC)

The web app signs in through the MaiMemo account centre (OpenID Connect
authorization-code flow):

```
GET https://accounts.maimemo.com/oidc/auth
    ?client_id=66ea70d4223bbbf3ce328daa
    &scope=openid profile memo.app memo.content offline_access
    &response_type=code
    &redirect_uri=https://tc-apis.maimemo.com/study/api/v1/users/auth/callback
    &state=https://tc-apis.maimemo.com/webstudy/app
    &resource=https://accounts.maimemo.com/res/62cfcd9199a82b6f574875f7
    &prompt=consent
```

- The first request lands on the interaction page `/interaction/<uid>` (with a `csrf`).
- Send SMS code: `POST /interaction/<uid>/verifycode`, JSON `{"identity": <phone>}`, header `x-csrf-token`.
- Sign in: `POST /interaction/<uid>/login`, form `csrf + identity + code` (or
  `identity + password`; SMS and password share this endpoint).
- Then the consent page ("授权确认"): `POST /interaction/<uid>/confirm`.
- Finally the callback `GET /study/api/v1/users/auth/callback?code=...` issues the
  real credential: **`Set-Cookie: sid=...; Path=/study; HttpOnly; Secure`**
  (7 days), and 302s back to `https://tc-apis.maimemo.com/webstudy/app`.

### 2. REST

| Method | Endpoint | Notes |
| --- | --- | --- |
| `POST` | `https://tc-apis.maimemo.com/study/api/v1/webstudy/precheck` | Validate sign-in, header `Cookie: sid=<sid>`, returns `{"data":{"is_privileged":bool}}`; 401 = not signed in |

> Only the web app's own entry points are used (`tc-apis.maimemo.com` and the
> `accounts.maimemo.com` sign-in page); the legacy `api.maimemo.com` API is not used.

### 3. WebSocket study session (core)

```
wss://tc-apis.maimemo.com/study/ws/webstudy?token=
```

The handshake carries `Cookie: sid=<sid>` (same as the web app: empty `token`
param, cookie auth).

Frames are protobuf `WebsocketProtocolMessage` (fields: `id`=1 string,
`reply_id`=2 string, `event`=3 enum, `data`=4 bytes, `success`=5 bool,
`errors`=6 repeated `WebsocketProtocolError`); `event` is a numeric enum and
the request/response types are listed in `client.py`'s `EVENT_TYPES`.

Connection flow: the server first sends `SYSTEM_READY` → the client sends
`WEBSTUDY_INIT_STUDY` (empty request) → the server returns `settings`
(`{code, value}`, value is `google.protobuf.Value`), `user`, `privileges`,
`dictionary_settings`. Heartbeat: reply to `SYSTEM_PING` with a `response`
frame (echoing `reply_id`).

Study loop:

| Event (number) | Request | Response highlights |
| --- | --- | --- |
| `WEBSTUDY_GET_WORD` (1002) | `{back: bool}` | `word`, `interpretations`, `phrases`, `notes`, `progress{finished,total}`, `response_predicts`, `memory_history`, `is_first_show`, `has_prev_word` |

**Pronunciation (sound endpoint)**: `word.pronunciations[].url`, e.g.
`https://cdn-by.maimemo.com/speeches/<uuid>.mp3` (real recordings, not TTS),
chosen by `accent` (`US`/`UK`, from the `app.speech.setting.voc.accent`
setting). The site's phrase sound is browser `speechSynthesis` (TTS, no server
endpoint); this app never uses TTS — only the real pronunciation files
(`p` to replay; auto-plays on a new word when
`app.speech.setting.voc.auto_play.before_answer.enabled` is on).

Playback goes through the **system audio framework** (Ubuntu 22.04+ uses
PipeWire; WirePlumber manages the default device; pipewire-pulse provides the
PulseAudio compatibility layer) and prefers the system default device (the one
you picked, e.g. a Jabra headset): `paplay` (PulseAudio client) → `pw-play`
(PipeWire native) → miniaudio in-process → `aplay -D default` → every
`plughw:X,Y` from `aplay -l` → `ffplay`/`mpv`.

| Event (number) | Request | Response highlights |
| --- | --- | --- |
| `WEBSTUDY_SUBMIT_RESPONSE` (1003) | `{voc_id, response, study_method, recall_duration, study_duration}` | `next` (full `WsWebStudyGetWordResponse` for the next word; GET_WORD again if no `word`) |
| `WEBSTUDY_REVIEW_MORE` (1007) | `{count}` | adds review words |
| `WEBSTUDY_GET_STUDY_STATES` (1006) | bools selecting which fields | `study_book`, `study_progress`, `day_limit`, `learned_count`, `blocked_count`, … |
| `WEBSTUDY_ADVANCE_REVIEW` (1010) | `{voc_ids, count, behind_voc_id}` | advance review |
| `WEBSTUDY_ADD_WORDS` (1011) | `{words:[{voc_id}]}` / `{by_book}` | add words |
| `WEBSTUDY_DELETE_WORDS` (1005) | `{voc_ids}` | delete words |
| `WEBSTUDY_SET_STUDY_SETTINGS` (1012) | `{study_method, study_order, day_limit, ...}` | study settings |
| `WEBSTUDY_PRECHECK_SIGN`/`WEBSTUDY_SIGN` (1008/1009) | — / `{verify_data}` | daily check-in |
| `WEBSTUDY_QUERY_WORD` (1100) | `{spelling, phrase, interpretation}` | word lookup |
| `WEBSTUDY_SEARCH_BOOKS` (1101) / `GET_RECOMMEND_BOOKS` (1103) / `SET_STUDY_BOOK` (1102) | ... | word books |

`study_method`: `STUDY_EN_CN`=0 (EN→CN), `STUDY_CN_EN`=1 (CN→EN); `StudyResponse`:
`FAMILIAR`=1, `VAGUE`=2, `FORGET`=3, `WELL_FAMILIAR`=4.

### 4. Protobuf schema

`src/terminal_maimemo/schema.json` was auto-extracted from the ts-proto
`encode(...)` functions in the front-end bundle (126 messages, 15 enums, with
field numbers and types). `proto.py` is a pure-Python codec with no protoc
dependency, cross-validated byte-for-byte against official `google.protobuf`
(see `tests/`).

---

## Project layout

```
src/terminal_maimemo/
  schema.json     # extracted protobuf schema (126 messages / 15 enums)
  proto.py        # pure-Python protobuf codec
  client.py       # REST + WebSocket client (event mapping, frame codec)
  auth.py         # OIDC sign-in (SMS / password), sid cookie capture
  audio.py        # pronunciation playback (real MP3s, system audio framework)
  config.py       # ~/.terminal_maimemo config & credential storage
  screens.py      # LoginScreen / StudyScreen
  app.py          # MaimemoApp + CLI entry point
  app.tcss        # theme styles
tests/            # codec & frame-protocol self-tests
scripts/live_test.py  # read-only connectivity test
```

## Disclaimer

For personal learning use; not affiliated with MaiMemo. The protocol and
endpoints may change at any time. Please follow MaiMemo's terms of service and
use only your own account.
