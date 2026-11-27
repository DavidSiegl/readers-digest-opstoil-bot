"""The desktop window: the digest as pages of rows, with Sync and Review buttons, and a tray icon.

Started by `readers-digest gui` (cli.gui). Sync and review run as
readers-digest subprocesses, one at a time, so the window never blocks on
mail, Teams or the model, and a crash in either does not take it down.

Reads the state db as `show` does and opens no port: nothing but this
process can press its buttons. Its tray icon (tray.py) is on the session
bus, which only this user's processes can reach.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from readers_digest import EXIT_FAILED, __version__, failure_line  # noqa: E402
from readers_digest import config as config_  # noqa: E402
from readers_digest.config import Config, ConfigError  # noqa: E402
from readers_digest.digest import collect, previous_working_day  # noqa: E402
from readers_digest.gui import model  # noqa: E402
from readers_digest.gui.tray import Tray  # noqa: E402
from readers_digest.store import Store, default_path  # noqa: E402

# Reverse-DNS id of the application; whoever publishes the tool should put their own namespace here.
APP_ID = "io.github.readers_digest_bot.ReadersDigest"
ICON_NAME = "readers-digest"
APP_NAME = "Reader's Digest Bot"
ICON_DIR = Path(__file__).parent / "icons"
# The timer may sync in the background; the db is cheap to read again.
RELOAD_S = 60
# A toast shows one line; a longer error is cut to this many characters.
TOAST_WIDTH = 200

CSS = b"""
.badge { padding: 1px 8px; border-radius: 999px; background: alpha(currentColor, 0.1); font-size: smaller; }
.badge.warn { background: alpha(@warning_bg_color, 0.35); }
.detail { font-size: smaller; }
"""


@dataclass(frozen=True)
class Options:
    """What the window was started with."""

    # The config file, read again at every reload: the store refolds every
    # ticket whenever it is opened with other rules than the last opener's,
    # so a window holding the rules it started with would undo each rule
    # change the timer's runs pick up.
    config: Path
    db: Path
    # The command line that runs readers-digest, with the global options: [python, -m, readers_digest, --config, …].
    cli: list[str]


@dataclass(frozen=True)
class _Command:
    """A readers-digest run waiting its turn (Window.run_cli)."""

    # What toasts and the spinner call it; two commands with the same name do the same thing.
    what: str
    args: list[str]
    # The output's last line says what happened, and the success toast shows it.
    say_last: bool


class Window(Adw.ApplicationWindow):
    def __init__(self, app: "App", opts: Options):
        super().__init__(application=app, title=APP_NAME, default_width=960, default_height=760)
        self.opts = opts
        # The config as last read; a reload that cannot read it keeps this one and shows nothing new.
        self.cfg: Config = config_.load(opts.config)
        # The readers-digest command running now, by its name (_Command.what); None when none is.
        self.running: str | None = None
        # Commands asked for while another ran, started in turn as each ends.
        # Never non-empty while running is None. In memory only: a quit drops them.
        self.queued: list[_Command] = []
        # What needs the reader (View.actions), and what of it they have seen:
        # all of it, whenever the window had the focus. The rest lights the
        # tray icon's dot. In memory only, so after a restart everything
        # open counts as new once.
        self.actions: frozenset[str] = frozenset()
        self.seen: frozenset[str] = frozenset()
        self.summary = ""

        self.stack = Adw.ViewStack()
        self.toasts = Adw.ToastOverlay()
        self.banner = Adw.Banner(use_markup=False)
        self.footer = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], wrap=True,
                                margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.review_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                        wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12,
                                        left_margin=12, right_margin=12)

        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        self.spinner = Gtk.Spinner()
        self.sync_button = Gtk.Button(label="Sync", tooltip_text="Read new mail and chats (readers-digest sync)")
        self.sync_button.connect("clicked", lambda _b: self.run_cli("sync", ["sync"], say_last=True))
        self.review_button = Gtk.Button(label="Review",
                                        tooltip_text="Tag new mail and chats (readers-digest review): this sends "
                                                     "their text to the configured review command")
        self.review_button.set_sensitive(self.cfg.review.command is not None)
        self.review_button.connect("clicked", lambda _b: self.run_cli("review", ["review", "--quiet"]))
        reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Read the db again (Ctrl+R)")
        reload.connect("clicked", lambda _b: self.reload())
        logo = Gtk.Button(child=Gtk.Image(icon_name=ICON_NAME, pixel_size=24), css_classes=["flat"],
                          tooltip_text=f"About {APP_NAME}")
        logo.connect("clicked", lambda _b: self.about())
        header.pack_start(logo)
        header.pack_start(self.sync_button)
        header.pack_start(self.review_button)
        header.pack_start(self.spinner)
        header.pack_end(reload)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.banner)
        body.append(self.stack)
        self.stack.set_vexpand(True)
        body.append(Gtk.Separator())
        body.append(self.footer)
        self.toasts.set_child(body)

        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)

        self.connect("close-request", self._close)
        self.connect("notify::is-active", lambda *_: self._focused())

        action = Gio.SimpleAction(name="reload")
        action.connect("activate", lambda *_: self.reload())
        self.add_action(action)

        # The digest's pages, added by the first reload that reads the db;
        # _show_page keeps the Review page after them.
        self.pages: dict[str, Gtk.ScrolledWindow] = {}
        self.review_page = Gtk.ScrolledWindow(child=self.review_view, vexpand=True)
        self.stack.add_titled_with_icon(self.review_page, "review", "Review", "document-edit-symbolic")
        # What each page last showed, and the rows and folded groups the
        # reader has open (_keep_open): a reload must not close them.
        self.shown: dict[str, model.Page] = {}
        self.expanded: set[str] = set()
        self.reload()
        GLib.timeout_add_seconds(RELOAD_S, self._tick)

    def _close(self, _win: Gtk.Window) -> bool:
        # Into the tray while a bar shows it; quit otherwise, or the window
        # would be gone with nothing left to bring it back.
        tray = self.get_application().tray
        if tray is None or not tray.registered:
            return False
        self.set_visible(False)
        return True

    def _tick(self) -> bool:
        # Not in the middle of a run: its reload follows when it ends.
        if self.running is None:
            self.reload()
        return GLib.SOURCE_CONTINUE

    def reload(self) -> None:
        """Read the state db and review.md again and show what changed."""
        now = datetime.now().astimezone()
        try:
            self.cfg = config_.load(self.opts.config)
        except ConfigError as e:
            self.toast(f"cannot read the config: {failure_line(e)}")
            return
        self._busy(self.running)  # [review] command may have come or gone
        try:
            with Store(self.opts.db, self.cfg.rules) as s:
                d = collect(s, self.cfg, previous_working_day(now), now)
        except Exception as e:  # noqa: BLE001 - shown, and the window stays usable
            self.toast(f"cannot read {self.opts.db}: {failure_line(e)}")
            return
        v = model.build(d)
        self.set_title(f"{APP_NAME} · {v.title}")
        self.banner.set_title(" — ".join(v.banners))
        self.banner.set_revealed(bool(v.banners))
        self.footer.set_label(v.footer)
        for page in v.pages:
            self._show_page(page)
        review = self._show_review()
        # A new review is news. hash() differs between runs, but `seen` does
        # not outlive the process either.
        self.actions = v.actions | ({f"review {hash(review)}"} if review is not None else set())
        self.summary = model.summary(v)
        self._mark()

    def _focused(self) -> None:
        # The timer may have synced since the last reload: read the db again
        # when the window gets the focus, so what is looked at is current.
        if self.is_active() and self.running is None:
            self.reload()
        else:
            self._mark()

    def _mark(self) -> None:
        if self.is_active():
            self.seen = self.actions
        tray = self.get_application().tray
        if tray is None:
            return
        new = bool(self.actions - self.seen)
        tray.set_attention(new)
        tray.set_tooltip(self.summary + ("\nNew since you last looked" if new else ""))

    def _show_page(self, page: model.Page) -> None:
        # Built anew only if it changed: new widgets would drop a text
        # selection the reader is making.
        if self.shown.get(page.name) == page:
            return
        self.shown[page.name] = page
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=24,
                      margin_start=12, margin_end=12)
        for g in page.groups:
            box.append(_group(g, self, f"{page.name}\0{g.title}"))
        clamp = Adw.Clamp(child=box, maximum_size=1100)
        scroll = self.pages.get(page.name)
        if scroll is None:
            scroll = self.pages[page.name] = Gtk.ScrolledWindow(child=clamp, vexpand=True)
            # A stack lists its pages in the order they were added.
            visible = self.stack.get_visible_child_name()
            self.stack.remove(self.review_page)
            self.stack.add_titled_with_icon(scroll, page.name, page.title, page.icon)
            self.stack.add_titled_with_icon(self.review_page, "review", "Review", "document-edit-symbolic")
            # Stay where the reader was, unless this is the first digest page:
            # then the digest is shown rather than the Review page.
            if visible is not None and len(self.pages) > 1:
                self.stack.set_visible_child_name(visible)
        else:
            # Keep the reader's place across a reload.
            adj = scroll.get_vadjustment()
            pos = adj.get_value()
            scroll.set_child(clamp)
            GLib.idle_add(_restore, adj, pos)
        sp = self.stack.get_page(scroll)
        sp.set_badge_number(page.count)
        sp.set_needs_attention(page.name in ("mail", "teams") and page.count > 0)

    def _show_review(self) -> str | None:
        """Show review.md; returns its text, or None when there is none to read."""
        p = self.opts.db.parent / "review.md"
        review = None
        try:
            text = review = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ("No review yet. Press Review to have the model tag the new mail and chats."
                    if self.cfg.review.command else "Review is off: set [review] command in the config.")
        except OSError as e:
            text = f"cannot read {p}: {e}"
        buf = self.review_view.get_buffer()
        if buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False) != text:
            buf.set_text(text)
        return review

    def run_cli(self, what: str, args: list[str], say_last: bool = False) -> None:
        """Run readers-digest `args` in the background once the commands before it have ended; a toast says how it went.

        One command runs at a time; one asked for while another runs waits
        its turn, and runs whether those before it succeed or fail. One named
        `what` that is running or queued already is not queued again.
        say_last: on success, the toast shows the output's last line.
        """
        if what == self.running or any(c.what == what for c in self.queued):
            self.toast(f"{what} is {'running' if what == self.running else 'queued'} already")
            return
        self.queued.append(_Command(what, args, say_last))
        if self.running:
            self.toast(f"{what} queued; it starts when {self.running} ends")
            self._busy(self.running)
            return
        self._next()

    def _next(self) -> None:
        # Start the first queued command that can start; one that cannot is
        # dropped with its toast, and the next one tried.
        while self.queued and self.running is None:
            c = self.queued.pop(0)
            try:
                proc = Gio.Subprocess.new([*self.opts.cli, *c.args],
                                          Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
            except GLib.Error as e:
                self.toast(f"cannot start {c.what}: {e.message}")
                continue
            self._busy(c.what)
            proc.communicate_utf8_async(None, None, self._done, c)

    def _done(self, proc: Gio.Subprocess, result: Gio.AsyncResult, c: _Command) -> None:
        try:
            _ok, out, _err = proc.communicate_utf8_finish(result)
        except GLib.Error as e:
            out = e.message
        self._busy(None)
        lines = [line for line in (out or "").splitlines() if line.strip()]
        if proc.get_successful():
            self.toast(f"{c.what} done" + (f": {lines[-1]}" if c.say_last and lines else ""))
        else:
            # The tool's own error lines start with its name.
            errors = [line for line in lines if line.startswith("readers-digest: ")]
            last = (errors or lines)[-1] if lines else f"exit {proc.get_exit_status()}"
            self.toast(f"{c.what} failed: {last[:TOAST_WIDTH]}", timeout=0)
        self.reload()
        self._next()

    def _busy(self, what: str | None) -> None:
        self.running = what
        self.sync_button.set_sensitive(what is None)
        self.review_button.set_sensitive(what is None and self.cfg.review.command is not None)
        self.spinner.set_spinning(what is not None)
        then = f"; then {', '.join(c.what for c in self.queued)}" if self.queued else ""
        self.spinner.set_tooltip_text(f"{what} running{then}" if what else None)

    def toast(self, text: str, timeout: int = 4) -> None:
        """Show `text` as a toast for `timeout` seconds, 0 until closed."""
        # Markup off before the text is set: it may be a command's output (_plain says why).
        t = Adw.Toast(timeout=timeout)
        t.set_use_markup(False)
        t.set_title(text)
        self.toasts.add_toast(t)

    def copy(self, text: str, what: str) -> None:
        """Put `text` on the clipboard and toast "copied <what>"."""
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast(f"copied {what}")

    def about(self) -> None:
        Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=ICON_NAME,
            version=__version__,
            license_type=Gtk.License.MIT_X11,
            comments="Your open tickets, new mail and unread Teams chats, read without changing a thing. "
                     "Review sends new mail and chats to the command the config names.",
        ).present(self)

    def open_link(self, uri: str) -> None:
        Gtk.UriLauncher(uri=uri).launch(self, None, None, None)


def _restore(adj: Gtk.Adjustment, pos: float) -> bool:
    adj.set_value(pos)
    return GLib.SOURCE_REMOVE


def _keep_open(widget: Gtk.Expander | Adw.ExpanderRow, key: str, win: Window) -> None:
    """Open `widget` if the reader had it open before the last rebuild, and note when they open or close it.

    `key` names it across rebuilds: the page, the group's title, the row's key.
    """
    widget.set_expanded(key in win.expanded)

    def changed(w, _param) -> None:
        if w.get_expanded():
            win.expanded.add(key)
        else:
            win.expanded.discard(key)

    widget.connect("notify::expanded", changed)


def _group(g: model.Group, win: Window, key: str) -> Gtk.Widget:
    """A heading and a boxed list. Titles are plain labels: they may hold mail text."""
    rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    if not g.rows:
        rows.append(_plain(Adw.ActionRow(css_classes=["dim-label"]), "None"))
    for r in g.rows:
        rows.append(_row(r, win, f"{key}\0{r.key or r.title}"))
    heading = Gtk.Label(label=f"{g.title} ({len(g.rows)})", xalign=0, css_classes=["heading"],
                        margin_top=12, margin_bottom=6, use_markup=False)
    if not g.folded:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(heading)
        box.append(rows)
        return box
    folded = Gtk.Expander(label_widget=heading, child=rows)
    _keep_open(folded, key, win)
    return folded


def _plain(row: Adw.PreferencesRow, title: str, subtitle: str = "") -> Adw.PreferencesRow:
    """Set the row's text as plain text.

    Markup has to be off before the text is set: a row built with title=…,
    use_markup=False parses the title as markup first, and mail text with an
    `&` or `<` in it is then dropped or misread.
    """
    row.set_use_markup(False)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def _row(r: model.Row, win: Window, key: str) -> Gtk.Widget:
    if r.details:
        row = _plain(Adw.ExpanderRow(), r.title, r.subtitle)
        _keep_open(row, key, win)
        for d in r.details:
            row.add_row(_plain(Adw.ActionRow(title_lines=0, title_selectable=True, css_classes=["detail"]), d))
    else:
        row = _plain(Adw.ActionRow(subtitle_lines=2), r.title, r.subtitle)
    add = row.add_suffix
    if r.warn:
        row.add_css_class("warning")
    for b in r.badges:
        add(Gtk.Label(label=b.text, use_markup=False, valign=Gtk.Align.CENTER,
                      css_classes=["badge", "warn"] if b.warn else ["badge"]))
    if r.draft:
        btn = Gtk.Button(icon_name="edit-paste-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                         tooltip_text="Copy the model's reply draft, to paste where you answer")
        btn.connect("clicked", lambda _b, text=r.draft: win.copy(text, "the draft"))
        add(btn)
    if r.link:
        btn = Gtk.Button(icon_name="adw-external-link-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open",
                         css_classes=["flat"])
        btn.connect("clicked", lambda _b, uri=r.link: win.open_link(uri))
        add(btn)
    return row


class App(Adw.Application):
    def __init__(self, opts: Options, hidden: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.opts = opts
        # Only the first activation can start hidden; a later one raises the window.
        self.hidden = hidden
        # The tray icon; None without a session bus.
        self.tray: Tray | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(ICON_DIR))
        Gtk.Window.set_default_icon_name(ICON_NAME)
        css = Gtk.CssProvider()
        css.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        quit_ = Gio.SimpleAction(name="quit")
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.reload", ["<Control>r", "F5"])
        conn = self.get_dbus_connection()
        if conn is not None:
            self.tray = Tray(conn, APP_ID, APP_NAME, ICON_NAME, ICON_DIR, activate=self.activate, menu=[
                ("Open", self.activate),
                ("Sync", lambda: self._window().run_cli("sync", ["sync"], say_last=True)),
                (None, None),
                ("Quit", self.quit),
            ])

    def do_shutdown(self) -> None:
        if self.tray is not None:
            self.tray.close()
        Adw.Application.do_shutdown(self)

    def do_activate(self) -> None:
        # A second `readers-digest gui`, and a click on the tray icon, raise
        # the window. Started --hidden (at login), the window is built, since
        # it keeps the tray icon's dot and tooltip current, but not shown.
        win = self._window()
        if self.hidden:
            self.hidden = False
            return
        win.present()

    def _window(self) -> Window:
        # Hidden in the tray, the window still exists: closing only hides it.
        return self.get_active_window() or Window(self, self.opts)


def main(argv: list[str] | None = None) -> int:
    """Run the window until it quits, with the options in `argv` (default: sys.argv[1:]); returns an exit status.

    A config that cannot be loaded is reported on stderr and returns EXIT_FAILED.
    """
    p = argparse.ArgumentParser(prog="readers-digest gui")
    p.add_argument("--cli-python", required=True, help="the Python that runs readers-digest for sync and review")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--hidden", action="store_true", help="start in the tray, without opening the window")
    args = p.parse_args(argv)
    path = args.config or config_.default_path()
    try:
        config_.load(path)  # to fail here, on stderr, rather than in the window
    except ConfigError as e:
        print(f"readers-digest: {e}", file=sys.stderr)
        return EXIT_FAILED
    cli = [args.cli_python, "-m", "readers_digest"]
    cli += ["--config", str(args.config)] if args.config else []
    cli += ["--db", str(args.db)] if args.db else []
    opts = Options(path, args.db or default_path(), cli)
    # GApplication must not see our own options.
    return App(opts, args.hidden).run([sys.argv[0]])
