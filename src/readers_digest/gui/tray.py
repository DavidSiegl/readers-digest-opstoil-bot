"""The window's icon in the system tray (Waybar's `tray` module and the like).

GTK 4 has no tray API, so this speaks the protocol itself over D-Bus: a
StatusNotifierItem, registered with the StatusNotifierWatcher the bar runs,
and a small com.canonical.dbusmenu for its right-click menu. Both are
exported on the application's own session-bus connection, under its unique
name, so nothing else has to be owned.

Left click opens the window, the menu offers Open, Sync and Quit, and the
tooltip shows model.summary(). While something new waits for the user
the icon is the one with a dot, and the item says NeedsAttention: bars
differ in what they do with the status, but all of them redraw the icon.
The bar may render the tooltip as markup, so its text is escaped: it can
hold a sync error, which can hold mail text.
"""

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402 - after require_version, as PyGObject needs

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"
# Sizes of the icon sent as pixels, for a bar that cannot find it by name.
PIXMAP_SIZES = (22, 32, 64)

ITEM_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="u" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewAttentionIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
    <arg type="u" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
    <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ia{sv})"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>
"""


class Tray:
    """The icon, for as long as a watcher is on the bus.

    `registered` says whether a bar shows it right now; the window hides
    to the tray on close only while it does, so it can always be got back.
    """

    def __init__(self, conn: Gio.DBusConnection, app_id: str, title: str, icon_name: str, icon_dir: Path,
                 menu: list[tuple[str | None, Callable[[], None] | None]], activate: Callable[[], None]):
        self.conn = conn
        self.app_id = app_id
        self.title = title
        self.icon_name = icon_name
        self.icon_dir = icon_dir
        self.activate = activate
        # dbusmenu ids: 0 is the root, then one per entry; a None label is a separator.
        self.menu = menu
        self.tooltip = ""
        self.attention = False
        self.registered = False
        self.pixmaps = _pixmaps(icon_dir / f"{icon_name}.svg")
        self.attention_name = f"{icon_name}-attention"
        self.attention_pixmaps = _pixmaps(icon_dir / f"{self.attention_name}.svg")
        self._ids = [
            conn.register_object(ITEM_PATH, _iface(ITEM_XML), self._item_call, self._item_get, None),
            conn.register_object(MENU_PATH, _iface(MENU_XML), self._menu_call, self._menu_get, None),
        ]
        self._watch = Gio.bus_watch_name_on_connection(conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                       self._appeared, self._vanished)

    def close(self) -> None:
        """Take the icon out of the tray and stop answering for it on the bus."""
        Gio.bus_unwatch_name(self._watch)
        for i in self._ids:
            self.conn.unregister_object(i)
        self.registered = False

    def set_tooltip(self, text: str) -> None:
        """Show `text`, plain text, as the icon's tooltip."""
        if text != self.tooltip:
            self.tooltip = text
            self._emit("NewToolTip")

    def set_attention(self, on: bool) -> None:
        """Show the icon with its dot (`on`) or without it."""
        if on != self.attention:
            self.attention = on
            self._emit("NewIcon")
            self._emit("NewStatus", GLib.Variant("(s)", (self._status(),)))

    def _status(self) -> str:
        return "NeedsAttention" if self.attention else "Active"

    def _emit(self, signal: str, args: GLib.Variant | None = None) -> None:
        try:
            self.conn.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", signal, args)
        except GLib.Error as e:
            # The bus is going away; the next bar to appear reads the properties anew.
            print(f"readers-digest: tray: cannot send {signal}: {e.message}", flush=True)

    # A watcher (re)appears whenever the bar starts, so register every time.
    def _appeared(self, conn: Gio.DBusConnection, _name: str, _owner: str) -> None:
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (ITEM_PATH,)), None, Gio.DBusCallFlags.NONE, -1, None, self._registered)

    def _registered(self, conn: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            conn.call_finish(result)
        except GLib.Error as e:
            print(f"readers-digest: tray: cannot register with {WATCHER}: {e.message}", flush=True)
            self.registered = False
            return
        self.registered = True

    def _vanished(self, _conn: Gio.DBusConnection, _name: str) -> None:
        self.registered = False

    def _item_get(self, _c, _sender, _path, _iface, prop: str) -> GLib.Variant | None:
        values = {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", self.app_id),
            "Title": ("s", self.title),
            "Status": ("s", self._status()),
            "WindowId": ("u", 0),
            "IconName": ("s", self.attention_name if self.attention else self.icon_name),
            "IconThemePath": ("s", str(self.icon_dir)),
            "IconPixmap": ("a(iiay)", self.attention_pixmaps if self.attention else self.pixmaps),
            "AttentionIconName": ("s", self.attention_name),
            "AttentionIconPixmap": ("a(iiay)", self.attention_pixmaps),
            "ToolTip": ("(sa(iiay)ss)", ("", [], self.title, GLib.markup_escape_text(self.tooltip))),
            "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU_PATH),
        }
        if prop not in values:
            return None
        return GLib.Variant(*values[prop])

    def _item_call(self, _c, _sender, _path, _iface, method: str, _params, inv: Gio.DBusMethodInvocation) -> None:
        # ContextMenu is only called by a bar that cannot show Menu itself;
        # the window is the next best thing.
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            self.activate()
        inv.return_value(None)

    def _menu_get(self, _c, _sender, _path, _iface, prop: str) -> GLib.Variant | None:
        values = {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
                  "IconThemePath": ("as", [])}
        if prop not in values:
            return None
        return GLib.Variant(*values[prop])

    def _menu_call(self, _c, _sender, _path, _iface, method: str, params: GLib.Variant,
                   inv: Gio.DBusMethodInvocation) -> None:
        if method == "GetLayout":
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (1, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            inv.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._props(i)) for i in ids if i in self._items()],)))
        elif method == "GetProperty":
            i, name = params.unpack()
            inv.return_value(GLib.Variant("(v)", (self._props(i).get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, event, _data, _time = params.unpack()
            if event == "clicked":
                self._click(i)
            inv.return_value(None)
        elif method == "EventGroup":
            for i, event, _data, _time in params.unpack()[0]:
                if event == "clicked":
                    self._click(i)
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _items(self) -> dict[int, tuple[str | None, Callable[[], None] | None]]:
        return {i: entry for i, entry in enumerate(self.menu, start=1)}

    def _props(self, i: int) -> dict[str, GLib.Variant]:
        if i == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        label, _ = self._items()[i]
        if label is None:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", label)}

    def _layout(self) -> tuple:
        children = [GLib.Variant("(ia{sv}av)", (i, self._props(i), [])) for i in self._items()]
        return (0, self._props(0), children)

    def _click(self, i: int) -> None:
        _, action = self._items().get(i, (None, None))
        if action is not None:
            # Not from inside the D-Bus call: the action may open a window.
            GLib.idle_add(_once, action)


def _once(f: Callable[[], None]) -> bool:
    f()
    return GLib.SOURCE_REMOVE


def _iface(xml: str) -> Gio.DBusInterfaceInfo:
    return Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]


def _pixmaps(svg: Path) -> list[tuple[int, int, bytes]]:
    """The icon as the spec's ARGB32 pixels, in network byte order."""
    out = []
    for n in PIXMAP_SIZES:
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), n, n)
        except GLib.Error as e:
            print(f"readers-digest: tray: cannot load {svg}: {e.message}", flush=True)
            return []
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
        w, h, stride, px = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_pixels()
        argb = bytearray(w * h * 4)
        for y in range(h):
            row = px[y * stride:y * stride + w * 4]
            line = argb[y * w * 4:(y + 1) * w * 4]
            line[0::4], line[1::4], line[2::4], line[3::4] = row[3::4], row[0::4], row[1::4], row[2::4]
            argb[y * w * 4:(y + 1) * w * 4] = line
        out.append((w, h, bytes(argb)))
    return out
