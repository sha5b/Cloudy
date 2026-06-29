# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Standalone image viewer — a non-modal, draggable, minimizable toplevel window
(the same convention as the compose/event editors).

Opens inline chat images (and mail attachments) at full size in their own window
the user can park, resize or minimize. Supports fit-to-window (the default),
scroll-wheel zoom, drag-to-pan when zoomed in, and a Download button.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .attachments import save_bytes_dialog
from .imaging import thumbnail_texture

_MAX_EDGE = 2200  # cap the decoded texture so huge images stay light to render
_ZOOM_STEP = 1.25
_ZOOM_MAX = 8.0


class ImageWindow(Adw.Window):
    """View ``data`` (image bytes) with zoom/pan; the header carries Download."""

    def __init__(self, parent, data: bytes, name: str = "image"):
        # NOT transient_for / not modal: GNOME treats transient windows as
        # dialogs and hides minimize/maximize. As an independent toplevel it can
        # be parked, resized or minimized while the main window stays usable.
        super().__init__(modal=False)
        self._parent = parent
        self._data = data
        self._name = name or "image"
        # ``None`` scale = fit-to-window; a float = that multiple of native px.
        self._scale: float | None = None
        self._nat_w = self._nat_h = 1
        self.set_title(self._name)
        self.set_default_size(900, 700)

        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text=_("Zoom out"))
        zoom_out.connect("clicked", lambda *_a: self._zoom(out=True))
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text=_("Zoom in"))
        zoom_in.connect("clicked", lambda *_a: self._zoom(out=False))
        fit = Gtk.Button(icon_name="zoom-fit-best-symbolic",
                         tooltip_text=_("Fit to window"))
        fit.connect("clicked", lambda *_a: self._set_scale(None))
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Download"))
        save.connect("clicked", lambda *_a: self._save())

        header = Adw.HeaderBar()
        header.set_decoration_layout(":minimize,maximize,close")
        zoom_box = Gtk.Box(spacing=0)
        zoom_box.add_css_class("linked")
        zoom_box.append(zoom_out)
        zoom_box.append(fit)
        zoom_box.append(zoom_in)
        header.pack_start(zoom_box)
        header.pack_end(save)

        self._pic = Gtk.Picture(hexpand=True, vexpand=True)
        self._pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        try:
            tex = thumbnail_texture(data, _MAX_EDGE)
            self._nat_w, self._nat_h = tex.get_width(), tex.get_height()
            self._pic.set_paintable(tex)
        except Exception:  # noqa: BLE001 - undecodable payload → empty viewer
            pass

        self._scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self._scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_child(self._pic)

        # Scroll wheel zooms (and is consumed so the view doesn't scroll instead).
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self._scroller.add_controller(scroll)
        # Drag pans the viewport when the image is larger than the window.
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self._scroller.add_controller(drag)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._scroller)
        self.set_content(toolbar)

    # -- zoom / pan -------------------------------------------------------
    def _fit_scale(self) -> float:
        """The scale at which the image fills the viewport (the fit baseline)."""
        vw, vh = self._scroller.get_width(), self._scroller.get_height()
        if vw <= 1 or vh <= 1:
            return 1.0
        return min(vw / self._nat_w, vh / self._nat_h)

    def _set_scale(self, scale: float | None) -> None:
        self._scale = scale
        if scale is None:
            self._pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            self._pic.set_halign(Gtk.Align.FILL)
            self._pic.set_valign(Gtk.Align.FILL)
            self._pic.set_size_request(-1, -1)
            self.set_cursor(None)
        else:
            self._pic.set_content_fit(Gtk.ContentFit.FILL)
            self._pic.set_halign(Gtk.Align.CENTER)
            self._pic.set_valign(Gtk.Align.CENTER)
            self._pic.set_size_request(round(self._nat_w * scale),
                                       round(self._nat_h * scale))
            self.set_cursor(Gdk.Cursor.new_from_name("grab", None))

    def _zoom(self, *, out: bool) -> None:
        base = self._scale if self._scale is not None else self._fit_scale()
        new = base / _ZOOM_STEP if out else base * _ZOOM_STEP
        fit = self._fit_scale()
        # Snap back to fit-to-window once we shrink to (or below) the fit size.
        if new <= fit:
            self._set_scale(None)
        else:
            self._set_scale(min(new, _ZOOM_MAX))

    def _on_scroll(self, _ctrl, _dx, dy) -> bool:
        if dy != 0:
            self._zoom(out=dy > 0)
        return True  # consume — never let the wheel scroll instead of zoom

    def _on_drag_begin(self, _g, _x, _y) -> None:
        self._h0 = self._scroller.get_hadjustment().get_value()
        self._v0 = self._scroller.get_vadjustment().get_value()

    def _on_drag_update(self, _g, offx, offy) -> None:
        self._scroller.get_hadjustment().set_value(self._h0 - offx)
        self._scroller.get_vadjustment().set_value(self._v0 - offy)

    # -- download ---------------------------------------------------------
    def _save(self) -> None:
        def on_done(error):
            if error:
                self._toast(error)

        save_bytes_dialog(self, self._data, self._name, on_done)

    def _toast(self, message: str) -> None:
        """Best-effort toast on the parent window or as a fallback print."""
        app = self._parent.get_application() if self._parent else None
        win = app.props.active_window if app else None
        if win is not None and hasattr(win, "add_toast"):
            win.add_toast(message)
        else:
            print(f"[media-window] {message}")
