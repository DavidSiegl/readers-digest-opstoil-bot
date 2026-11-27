"""Teams chats, read through a Chromium profile of the tool's own; channels are not read. Needs the `teams` extra.

The profile lives next to the state db and is used for nothing but Teams:
the reader's own browser profile is never touched, and Playwright drives
this one over a pipe, with no debugging port. The reader logs in once,
headed, with whatever Microsoft asks for (`readers-digest teams-login`);
later runs reuse the profile's session headless.

Reading only: every write to the chat service is blocked (_read_only), so
nothing here posts, reacts or marks a message read. Only that service: the
client's other calls (presence, sign-in, telemetry) go through, since the
client cannot load without some of them.

A headless start may land on Microsoft's account picker, or on "Stay signed
in?". The picker is answered by clicking TeamsSettings.account, and the
question with its Yes. Anything more (a password, MFA, a federated sign-in)
stops the run with NotLoggedIn: this tool never types a password into a page.
"""

import base64
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from readers_digest import write_private
from readers_digest.config import TeamsSettings
from readers_digest.teams import chat
from readers_digest.teams.chat import Conversation, Message
from readers_digest.teams.shape import skeleton, url_pattern

__all__ = ["PlaywrightError", "NotLoggedIn", "TeamsError", "last_login", "login", "probe", "profile_dir", "read"]

TEAMS_URL = "https://teams.microsoft.com/v2/"
LOGIN_HOST = "login.microsoftonline.com"
# Teams' start can go through the account picker before the client loads.
OPEN_WAIT_S = 120
# A login page that is neither the picker nor "stay signed in" this long is
# asking for something only the reader can give.
LOGIN_PAGE_WAIT_S = 15
# The Yes button of Microsoft's "Stay signed in?" page, in any language.
STAY_SIGNED_IN_BUTTON = "#idSIButton9"
_STAY_SIGNED_IN = re.compile(r"stay signed in|angemeldet bleiben|rester connecté|mantener la sesión", re.IGNORECASE)
PAGE_TIMEOUT_MS = 90_000
# How long teams-login waits for the reader to log in, in the headed window.
LOGIN_WAIT_S = 600
# A conversation's messages come newest first, in pages. Beyond MAX_PAGES
# pages of unread messages, the oldest are not fetched.
PAGE_SIZE = 200
MAX_PAGES = 5
REQUEST_TIMEOUT_MS = 30_000
# The view the client asks for, which includes the messages' properties (mentions among them).
MESSAGES_VIEW = "msnp24Equivalent|supportsMessageProperties"
# The client's list of chats and teams with their channels.
ME_LIST = "/api/v3/teams/users/me"
# How long a probe lets the client load and talk to its services.
PROBE_WAIT_S = 60
# The client's own calls that carry chats: the chat service, and the
# service for teams and their channels, on the old host and the new.
API = re.compile(r"^https://teams\.(microsoft\.com|cloud\.microsoft)/api/(chatsvc|csa)/")
# Every request to the chat service, on either host: only reads get through.
CHATSVC = re.compile(r"^https://teams\.(microsoft\.com|cloud\.microsoft)/api/chatsvc/")
# Written into the profile by a successful teams-login.
LOGIN_MARK = "readers-digest-login"


class TeamsError(Exception):
    """Teams could not be read; the message says why."""


class NotLoggedIn(TeamsError):
    """Microsoft wants the reader: run readers-digest teams-login."""


def profile_dir(db: Path) -> Path:
    """The Chromium profile of the state db at `db`, which holds the Teams session."""
    return db.parent / "teams-profile"


def last_login(db: Path) -> datetime | None:
    """When teams-login last succeeded for the state db at `db`; None if it never has."""
    try:
        return datetime.fromtimestamp((profile_dir(db) / LOGIN_MARK).stat().st_mtime, UTC)
    except FileNotFoundError:
        return None


@contextmanager
def browser(db: Path, settings: TeamsSettings, headed: bool = False) -> Iterator[BrowserContext]:
    """The Teams profile of `db`, open in Chromium, closed when the block ends; writes to the chat service fail.

    The proxy is settings.proxy, else HTTPS_PROXY from the environment, else
    none. Raises PlaywrightError when Chromium cannot start (not installed:
    `playwright install chromium`).
    """
    proxy = settings.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    profile = profile_dir(db)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(profile, 0o700)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), headless=not headed, proxy={"server": proxy} if proxy else None,
            locale=settings.locale, timezone_id=settings.timezone,
        )
        ctx.route(CHATSVC, _read_only)
        try:
            yield ctx
        finally:
            ctx.close()


def login(db: Path, settings: TeamsSettings, wait_s: int = LOGIN_WAIT_S) -> None:
    """Open Teams headed for the reader to log in; return once the client talks to its chat service.

    Raises TeamsError if that does not happen within `wait_s` seconds.
    """
    with browser(db, settings, headed=True) as ctx:
        seen: list[str] = []
        ctx.on("response", lambda r: seen.append(r.url) if API.match(r.url) else None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        print("Log in to Teams in the window. It closes by itself once Teams has loaded your chats.")
        for _ in range(wait_s):
            if seen:
                # Let the client finish storing its tokens in the profile.
                page.wait_for_timeout(5000)
                (profile_dir(db) / LOGIN_MARK).write_text(datetime.now(UTC).isoformat() + "\n")
                return
            page.wait_for_timeout(1000)
        raise TeamsError(f"Teams did not load any chats within {wait_s}s; nothing is stored but the profile")


def _open(ctx: BrowserContext, settings: TeamsSettings, seen: Callable[[], bool], wait_s: int = OPEN_WAIT_S) -> Page:
    """Load Teams until `seen()` says its client has called its services, getting through the login on the way.

    Raises NotLoggedIn when Microsoft asks for more than the remembered
    session, and TeamsError when the client does not load within `wait_s`.
    """
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    picked = False
    on_login = 0
    for _ in range(wait_s):
        if seen():
            return page
        host = page.url.split("/")[2] if "://" in page.url else ""
        on_login = on_login + 1 if host == LOGIN_HOST else 0
        if host == LOGIN_HOST:
            account = (page.get_by_text(re.compile(f"^{re.escape(settings.account)}$", re.IGNORECASE))
                       if settings.account else None)
            if not picked and account is not None and account.count():
                account.first.click()
                picked = True
                on_login = 0
            elif _STAY_SIGNED_IN.search(_text(page)) and page.locator(STAY_SIGNED_IN_BUTTON).count():
                page.locator(STAY_SIGNED_IN_BUTTON).click()
                on_login = 0
            elif on_login > LOGIN_PAGE_WAIT_S:
                raise NotLoggedIn("Microsoft asks for more than the remembered session (an account, a password "
                                  "or MFA): run readers-digest teams-login")
        page.wait_for_timeout(1000)
    raise TeamsError(f"Teams did not load within {wait_s}s")


def read(db: Path, settings: TeamsSettings, known: dict[str, datetime] | None = None
         ) -> tuple[list[Conversation], list[Message], list[str]]:
    """Every chat the reader is in, the messages of the unread ones, and one line per chat that could not be read.

    Only conversations Teams marks unread are fetched, from the reader's
    read position on; without one, as far back as MAX_PAGES reach. `known`
    is the newest message stored per conversation: what is older is not
    fetched again, so a message edited after it was stored keeps its old
    text. The requests go out with the headers the client itself sent to the
    chat service (its access token among them), in the profile's own
    session. Raises TeamsError (NotLoggedIn among them), PlaywrightError, or
    OSError when the profile directory cannot be made.
    """
    cap: dict = {}

    def on_request(q) -> None:
        if "headers" not in cap and CHATSVC.match(q.url) and "/users/ME/" in q.url and "authorization" in q.headers:
            cap["headers"] = {k: v for k, v in q.headers.items()
                              if k not in ("cookie", "referer", "user-agent") and not k.startswith(":")}
            cap["base"] = q.url.split("/users/ME/")[0]

    def on_response(r) -> None:
        if "me" not in cap and API.match(r.url) and ME_LIST in r.url and r.status == 200:
            try:
                cap["me"] = r.json()
            except (PlaywrightError, ValueError):
                pass  # a later response carries it again

    with browser(db, settings) as ctx:
        ctx.on("request", on_request)
        ctx.on("response", on_response)
        _open(ctx, settings, lambda: "headers" in cap and "me" in cap)
        me = chat.my_mri(_object_id(cap["headers"]["authorization"]))
        try:
            convs = chat.conversations(cap["me"], me, datetime.now(UTC))
        except ValueError as e:
            raise TeamsError(str(e)) from e
        msgs: list[Message] = []
        skipped: list[str] = []
        for c, since in _plan(convs, known or {}):
            try:
                msgs += _messages(ctx, cap["base"], cap["headers"], c, since, me)
            except TeamsError as e:
                skipped.append(f"{c.kind} {c.title or c.id[:12]}: {e}")
    return convs, msgs, skipped


def _messages(ctx: BrowserContext, base: str, headers: dict, c: Conversation, since: datetime,
              me: str) -> list[Message]:
    url = (f"{base}/users/ME/conversations/{quote(c.id, safe='')}/messages?view={quote(MESSAGES_VIEW, safe='')}"
           f"&pageSize={PAGE_SIZE}&startTime={int(since.timestamp() * 1000)}")
    out: list[Message] = []
    for _ in range(MAX_PAGES):
        r = ctx.request.get(url, headers=headers, timeout=REQUEST_TIMEOUT_MS)
        if not r.ok:
            raise TeamsError(f"the chat service answered {r.status}")
        try:
            data = r.json()
        except (PlaywrightError, ValueError) as e:
            raise TeamsError(f"the chat service sent no JSON: {e}") from e
        try:
            out += chat.messages(c.id, data, me)
            oldest = chat.oldest_arrival(data)
        except ValueError as e:
            raise TeamsError(str(e)) from e
        older = chat.backward_link(data)
        # A full page whose oldest message is still new enough has more
        # behind it. Only links back to the same service are followed.
        if len(data.get("messages") or []) < PAGE_SIZE or oldest is None or oldest < since \
                or not older.startswith(base + "/"):
            break
        url = older
    return sorted((m for m in out if m.sent_at >= since), key=lambda m: (m.sent_at, m.id))


# What "from the start" means for a conversation with no read position.
_NEVER = datetime.fromtimestamp(0, UTC)


def _plan(convs: list[Conversation], known: dict[str, datetime]) -> list[tuple[Conversation, datetime]]:
    """The conversations to fetch, each from where: the unread ones, from the read position or the newest stored."""
    return [(c, max(c.read_at or _NEVER, known.get(c.id, _NEVER))) for c in convs if c.unread]


def _read_only(route) -> None:
    # The client could mark a chat read (a PUT of its consumption horizon)
    # when it opens one; that would take away the very marker this tool
    # reads, and the tool only reads.
    if route.request.method in ("GET", "HEAD", "OPTIONS"):
        route.continue_()
    else:
        route.abort()


def _object_id(authorization: str) -> str:
    """The reader's object id, from the access token's claims; read locally, the token is not checked here."""
    try:
        payload = authorization.split()[-1].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["oid"]
    except (IndexError, ValueError, KeyError, TypeError) as e:
        raise TeamsError(f"the client's token names no user ({type(e).__name__})") from e


def _text(page: Page) -> str:
    try:
        return page.evaluate("document.body ? document.body.innerText.slice(0, 2000) : ''")
    except PlaywrightError:
        return ""  # navigating; the next second looks again


def probe(db: Path, settings: TeamsSettings, out: Path, wait_s: int = PROBE_WAIT_S) -> int:
    """Record in `out` the shape of every chat and channel call the client makes, without its content.

    For fixing the reader when Microsoft changes what Teams sends: per call
    the method, the URL with ids replaced, the status, the request's header
    names (never their values) and the JSON's skeleton. Returns how many
    calls; raises as read() does.
    """
    calls: list[dict] = []

    def record(r) -> None:
        if not API.match(r.url):
            return
        try:
            body = skeleton(r.json())
        except (PlaywrightError, ValueError):
            body = None
        calls.append({"method": r.request.method, "url": url_pattern(r.url), "status": r.status,
                      "request_headers": sorted(r.request.headers), "body": body})

    with browser(db, settings) as ctx:
        ctx.on("response", record)
        page = _open(ctx, settings, lambda: bool(calls))
        page.wait_for_timeout(wait_s * 1000)
    write_private(out, json.dumps(calls, indent=1, ensure_ascii=False))
    return len(calls)
