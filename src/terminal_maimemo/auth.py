"""Authentication for the maimemo web-study service.

The web-study app (https://tc-apis.maimemo.com/webstudy/app) obtains its bearer
token through its own login entry:

    https://tc-apis.maimemo.com/study/api/v1/users/auth/login?return_url=...

That link redirects into maimemo's account centre where the user signs in with
a password or an SMS code; the final authorization code is exchanged at

    https://tc-apis.maimemo.com/study/api/v1/users/auth/callback

which issues the token used by the study API / websocket.

This module implements that flow with an ``httpx.AsyncClient`` keeping the
session cookies and following the same redirect chain a browser would.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# The web-study app's own login entry (built by its index page JS).
WEBSTUDY_LOGIN_URL = (
    "https://tc-apis.maimemo.com/study/api/v1/users/auth/login"
    "?return_url=https%3A%2F%2Ftc-apis.maimemo.com%2Fwebstudy%2Fapp"
)

ACCOUNTS_BASE = "https://accounts.maimemo.com"

MAX_HOPS = 12
_REDIRECTS = (301, 302, 303, 307, 308)


class AuthError(Exception):
    """Raised for any login failure with a user-facing message."""


def _extract_token(url: str, body: str = "") -> str | None:
    """Look for a `token=...` in a URL query string or in an HTML body."""
    m = re.search(r"[?&]token=([^&\s\"']+)", url)
    if m:
        return m.group(1)
    m = re.search(r'token["\']?\s*[:=]\s*["\']([^"\']+)["\']', body)
    if m:
        return m.group(1)
    m = re.search(r"[?&]token=([^&\s\"']+)", body)
    if m:
        return m.group(1)
    return None


class OidcClient:
    """Stateful login session (cookies + interaction state)."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        self.uid: str | None = None
        self.csrf: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def begin(self) -> None:
        """Follow the source login link to the sign-in page and parse it."""
        url = WEBSTUDY_LOGIN_URL
        resp = None
        for _ in range(MAX_HOPS):
            resp = await self.client.get(url)
            if resp.status_code in _REDIRECTS:
                loc = resp.headers.get("location")
                if not loc:
                    raise AuthError("Sign-in redirect is missing a target; try again")
                url = loc if loc.startswith("http") else ACCOUNTS_BASE + loc
                continue
            break
        if resp is None or resp.status_code >= 400:
            raise AuthError(f"Could not open the sign-in page (HTTP {resp.status_code})")
        html = resp.text
        uid = re.search(r"var uid = '([^']+)'", html)
        if not uid:
            uid = re.search(r"/interaction/([^/]+)/login", html)
        if not uid:
            raise AuthError("Could not parse the sign-in page; try again, or use Paste credential")
        self.uid = uid.group(1)
        csrf = re.search(r'name="csrf" value="([^"]*)"', html)
        self.csrf = csrf.group(1) if csrf else ""
        if not self.csrf:
            raise AuthError("Sign-in page is missing the CSRF token; try again")
        log.debug("login interaction uid=%s", self.uid)

    async def send_sms_code(self, phone: str) -> dict[str, Any]:
        """Request an SMS verification code. Returns the JSON body."""
        if not self.uid:
            await self.begin()
        resp = await self.client.post(
            f"{ACCOUNTS_BASE}/interaction/{self.uid}/verifycode",
            json={"identity": phone},
            headers={
                "Content-Type": "application/json",
                "x-csrf-token": self.csrf or "",
                "Referer": f"{ACCOUNTS_BASE}/interaction/{self.uid}",
            },
        )
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code >= 400 or not body.get("success", True):
            errs = body.get("errors", [])
            msg = errs[0].get("message", "Failed to send the code") if errs else f"Failed to send the code (HTTP {resp.status_code})"
            raise AuthError(msg)
        return body

    async def _complete(self, form: dict[str, str]) -> str:
        """POST the login form, follow redirects, return the session id (sid)."""
        if not self.uid:
            raise AuthError("Sign-in flow has not been started")
        login_url = f"{ACCOUNTS_BASE}/interaction/{self.uid}/login"
        resp = await self.client.post(
            login_url,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{ACCOUNTS_BASE}/interaction/{self.uid}",
            },
        )
        if resp.status_code not in (301, 302, 303, 307, 308):
            err = re.search(r'id="login-error"[^>]*>([^<]*)', resp.text)
            raise AuthError(err.group(1).strip() if err else f"Sign-in failed (HTTP {resp.status_code})")

        url = resp.headers.get("location", "")
        for _ in range(MAX_HOPS):
            if not url:
                break
            if url.startswith("/"):
                url = ACCOUNTS_BASE + url
            # the callback sets the real credential as the `sid` cookie
            sid = self.client.cookies.get("sid")
            if sid:
                await self.client.aclose()
                return sid
            token = _extract_token(url)
            if token:
                await self.client.aclose()
                return token
            resp = await self.client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                url = resp.headers.get("location", "")
                continue
            sid = self.client.cookies.get("sid")
            if sid:
                await self.client.aclose()
                return sid
            token = _extract_token(str(resp.url), resp.text)
            if token:
                await self.client.aclose()
                return token
            # consent / confirmation page ("授权确认"): POST its confirm form
            action = self._consent_action(resp.text)
            if action:
                csrf2 = re.search(r'name="csrf" value="([^"]*)"', resp.text)
                href = action
                data = {"csrf": csrf2.group(1)} if csrf2 else {}
                resp = await self.client.post(
                    href if href.startswith("http") else ACCOUNTS_BASE + href,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Referer": str(resp.url)},
                )
                url = resp.headers.get("location", "") if resp.status_code in (301, 302, 303, 307, 308) else ""
                continue
            url = ""
        await self.client.aclose()
        raise AuthError("Signed in but no credential was returned (unexpected redirect chain); try again")

    @staticmethod
    def _consent_action(html: str) -> str | None:
        """Find the 'confirm/同意授权' form action on the consent page, if any."""
        title = re.search(r"<title>([^<]*)</title>", html)
        if title and any(k in title.group(1) for k in ("授权", "同意", "确认")):
            m = re.search(r'<form[^>]*action="([^"]*confirm[^"]*)"', html)
            if m:
                return m.group(1)
            m = re.search(r'<form[^>]*action="([^"]*consent[^"]*)"', html)
            if m:
                return m.group(1)
        # fallback: a submit button labelled 授权/同意/允许
        for m in re.finditer(r'<button[^>]*type="submit"[^>]*>\s*([^<]{1,20})', html):
            if any(k in m.group(1) for k in ("授权", "同意", "允许")):
                fm = re.search(r'<form[^>]*action="([^"]*)"', html)
                if fm:
                    return fm.group(1)
        return None

    async def login_with_code(self, phone: str, code: str) -> str:
        if not self.uid:
            await self.begin()
        return await self._complete(
            {"csrf": self.csrf or "", "identity": phone, "code": code}
        )

    async def login_with_password(self, identity: str, password: str) -> str:
        if not self.uid:
            await self.begin()
        return await self._complete(
            {"csrf": self.csrf or "", "identity": identity, "password": password, "code": ""}
        )


async def login_with_sms(phone: str, code: str) -> str:
    """One-shot SMS login; returns the bearer token."""
    oidc = OidcClient()
    try:
        await oidc.begin()
        return await oidc.login_with_code(phone, code)
    finally:
        await oidc.close()


async def login_with_password(identity: str, password: str) -> str:
    """One-shot password login; returns the bearer token."""
    oidc = OidcClient()
    try:
        await oidc.begin()
        return await oidc.login_with_password(identity, password)
    finally:
        await oidc.close()
