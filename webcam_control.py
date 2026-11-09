#!/usr/bin/env python3
"""Logitech BRIO controls: resolution (4K / 1080p), autofocus, and two preset focus levels."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import cairo
import math
import re
import subprocess

POLL_MS = 1500
TICK_MS = 16
EASE = 0.22
ROW_H = 64
ROW_TOP = 40
MARGIN_X = 36
PILL_H = 34
PILL_W = 150
PILL_GAP = 10
PILL_FONT_SIZE = 12.5
CHECK_SIZE = 26
RES_NOTE_H = 22  # space under the resolution row for the 4K availability note

RESOLUTIONS = [("4K", 3840, 2160), ("1080p", 1920, 1080)]
# focus_absolute: 0 = infinity, higher = closer. BRIO range is 0-255 in steps of 5.
FOCUS_LEVELS = [("Far", 0), ("Near", 50)]

ACCENT = (0.35, 0.55, 0.95)
OK_COLOR = (0.40, 0.80, 0.55)
WARN_COLOR = (0.95, 0.45, 0.35)


def v4l2(*args):
    try:
        out = subprocess.run(["v4l2-ctl", *args], capture_output=True, text=True, timeout=2)
        return out.returncode, out.stdout, out.stderr
    except Exception as e:
        return 1, "", str(e)


def find_brio():
    """First BRIO node that supports video capture."""
    rc, out, _ = v4l2("--list-devices")
    if rc != 0:
        return None
    in_brio = False
    for line in out.splitlines():
        if not line.startswith("\t"):
            in_brio = "BRIO" in line
            continue
        node = line.strip()
        if in_brio and node.startswith("/dev/video"):
            rc, info, _ = v4l2("-d", node, "--info")
            m = re.search(r"Device Caps\s*:\s*0x([0-9a-f]+)", info)
            if m and int(m.group(1), 16) & 0x1:
                return node
    return None


def read_controls(dev):
    rc, out, _ = v4l2("-d", dev, "--get-ctrl", "focus_automatic_continuous,focus_absolute")
    ctrls = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if v.strip().lstrip("-").isdigit():
            ctrls[k.strip()] = int(v)
    return ctrls


def read_format(dev):
    rc, out, _ = v4l2("-d", dev, "--get-fmt-video")
    m = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def read_sizes(dev):
    rc, out, _ = v4l2("-d", dev, "--list-formats-ext")
    return {(int(w), int(h)) for w, h in re.findall(r"Size: Discrete (\d+)x(\d+)", out)}


class Toggle:
    """A clickable rect with an eased highlight amount (0..1)."""

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.x = self.y = 0.0
        self.w = PILL_W
        self.h = PILL_H
        self.on = False
        self.enabled = True
        self.glow = 0.0

    def contains(self, x, y):
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


class WebcamView(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.dev = None
        self.sizes = set()
        self.status = ""
        self.status_color = OK_COLOR
        self.res_note = ""
        self.res_pills = [Toggle(("res", w, h), label) for label, w, h in RESOLUTIONS]
        self.af_check = Toggle(("af",), "Autofocus")
        self.af_check.w = self.af_check.h = CHECK_SIZE
        self.focus_pills = [Toggle(("focus", v), f"{label}  ({v})") for label, v in FOCUS_LEVELS]
        self.set_draw_func(self.on_draw)

        click = Gtk.GestureClick.new()
        click.connect("released", self.on_click)
        self.add_controller(click)

        GLib.timeout_add(POLL_MS, self.poll)
        GLib.timeout_add(TICK_MS, self.tick)
        self.poll()

    def all_toggles(self):
        return self.res_pills + [self.af_check] + self.focus_pills

    # ---- data ----
    def poll(self):
        # Re-read sizes every poll so the 4K note clears once the camera is replugged into USB 3.
        self.sizes = read_sizes(self.dev) if self.dev else set()
        if not self.sizes:
            self.dev = find_brio()
            self.sizes = read_sizes(self.dev) if self.dev else set()
            if self.dev and self.status == "BRIO not found":
                self.set_status("")
        if self.dev is None:
            self.res_note = ""
            self.set_status("BRIO not found", WARN_COLOR)
            for t in self.all_toggles():
                t.enabled = False
            self.queue_draw()
            return True

        fmt = read_format(self.dev)
        for p in self.res_pills:
            _, w, h = p.key
            p.on = fmt == (w, h)
            p.enabled = (w, h) in self.sizes

        ctrls = read_controls(self.dev)
        af = bool(ctrls.get("focus_automatic_continuous", 0))
        focus = ctrls.get("focus_absolute")
        self.af_check.on = af
        self.af_check.enabled = "focus_automatic_continuous" in ctrls
        for p in self.focus_pills:
            p.on = not af and focus == p.key[1]
            p.enabled = not af

        self.res_note = "" if self.res_pills[0].enabled else \
            "4K unavailable: camera is on a USB 2.0 link — use a USB 3 port"
        self.layout()
        self.queue_draw()
        return True

    @staticmethod
    def row_cy(row):
        return ROW_TOP + ROW_H * row + ROW_H / 2 + (RES_NOTE_H if row > 0 else 0)

    def set_status(self, text, color=OK_COLOR):
        self.status = text
        self.status_color = color

    def layout(self):
        label_w = 130
        x0 = MARGIN_X + label_w
        for row, items in enumerate([self.res_pills, [self.af_check], self.focus_pills]):
            cy = self.row_cy(row)
            x = x0
            for t in items:
                t.x = x
                t.y = cy - t.h / 2
                x += t.w + PILL_GAP

    def tick(self):
        moving = False
        for t in self.all_toggles():
            target = 1.0 if t.on else 0.0
            d = target - t.glow
            if abs(d) > 0.01:
                t.glow += d * EASE
                moving = True
            else:
                t.glow = target
        if moving:
            self.queue_draw()
        return True

    # ---- actions ----
    def on_click(self, gesture, n_press, x, y):
        if self.dev is None:
            return
        hit = next((t for t in self.all_toggles() if t.enabled and t.contains(x, y)), None)
        if hit is None:
            return
        kind = hit.key[0]
        if kind == "res":
            _, w, h = hit.key
            rc, _, err = v4l2("-d", self.dev, f"--set-fmt-video=width={w},height={h}")
            if rc != 0 or read_format(self.dev) != (w, h):
                busy = "busy" in err.lower()
                self.set_status(
                    "Camera is in use — close apps using it to change resolution" if busy
                    else f"Failed to set {hit.label}: {err.strip() or 'rejected by driver'}",
                    WARN_COLOR,
                )
            else:
                self.set_status(f"Resolution set to {w}x{h}")
        elif kind == "af":
            v4l2("-d", self.dev, f"--set-ctrl=focus_automatic_continuous={0 if hit.on else 1}")
            self.set_status("Autofocus off" if hit.on else "Autofocus on")
        elif kind == "focus":
            v4l2("-d", self.dev, f"--set-ctrl=focus_absolute={hit.key[1]}")
            self.set_status(f"Focus set to {hit.label}")
        self.poll()

    # ---- drawing ----
    def on_draw(self, area, cr, w, h):
        cr.set_source_rgb(0.11, 0.11, 0.13)
        cr.paint()

        labels = ["Resolution", "Autofocus", "Focus"]
        for row, text in enumerate(labels):
            cy = self.row_cy(row)
            cr.set_source_rgb(0.92, 0.92, 0.95)
            cr.select_font_face("sans-serif", 0, 0)
            cr.set_font_size(13)
            ext = cr.text_extents(text)
            cr.move_to(MARGIN_X, cy - ext.height / 2 - ext.y_bearing)
            cr.show_text(text)

        for p in self.res_pills + self.focus_pills:
            self.draw_pill(cr, p)
        self.draw_check(cr, self.af_check)

        if self.res_note:
            p = self.res_pills[0]
            cr.set_source_rgb(*WARN_COLOR)
            cr.select_font_face("sans-serif", 0, 0)
            cr.set_font_size(11)
            cr.move_to(p.x, p.y + p.h + 18)
            cr.show_text(self.truncate(cr, self.res_note, w - p.x - MARGIN_X))

        if self.dev:
            cr.set_source_rgba(0.6, 0.6, 0.65, 1)
            cr.select_font_face("sans-serif", 0, 0)
            cr.set_font_size(10.5)
            cr.move_to(w - MARGIN_X - cr.text_extents(self.dev).width, 24)
            cr.show_text(self.dev)

        if self.status:
            cr.set_source_rgb(*self.status_color)
            cr.select_font_face("sans-serif", 0, 0)
            cr.set_font_size(11.5)
            cr.move_to(MARGIN_X, ROW_TOP + ROW_H * 3 + RES_NOTE_H + 20)
            cr.show_text(self.truncate(cr, self.status, w - 2 * MARGIN_X))

    def draw_pill(self, cr, t):
        cr.save()
        r, g, bl = ACCENT
        alpha = 1.0 if t.enabled else 0.35
        self.rounded_rect(cr, t.x, t.y, t.w, t.h, t.h / 2)
        cr.set_source_rgba(r, g, bl, (0.18 + 0.72 * t.glow) * alpha)
        cr.fill_preserve()
        cr.set_source_rgba(r, g, bl, 0.9 * alpha)
        cr.set_line_width(1.6)
        cr.stroke()

        shade = 0.92 + 0.07 * t.glow
        cr.set_source_rgba(shade, shade, shade + 0.02, alpha)
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(PILL_FONT_SIZE)
        ext = cr.text_extents(t.label)
        cx, cy = t.x + t.w / 2, t.y + t.h / 2
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
        cr.show_text(t.label)
        cr.restore()

    def draw_check(self, cr, t):
        cr.save()
        r, g, bl = ACCENT
        alpha = 1.0 if t.enabled else 0.35
        self.rounded_rect(cr, t.x, t.y, t.w, t.h, 6)
        cr.set_source_rgba(r, g, bl, (0.18 + 0.72 * t.glow) * alpha)
        cr.fill_preserve()
        cr.set_source_rgba(r, g, bl, 0.9 * alpha)
        cr.set_line_width(1.6)
        cr.stroke()
        if t.glow > 0.05:
            cr.set_source_rgba(1, 1, 1, t.glow * alpha)
            cr.set_line_width(3)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(t.x + t.w * 0.25, t.y + t.h * 0.52)
            cr.line_to(t.x + t.w * 0.43, t.y + t.h * 0.70)
            cr.line_to(t.x + t.w * 0.76, t.y + t.h * 0.32)
            cr.stroke()
        cr.restore()

    @staticmethod
    def rounded_rect(cr, x, y, w, h, radius):
        cr.new_path()
        cr.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
        cr.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
        cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()

    @staticmethod
    def truncate(cr, text, max_w):
        if cr.text_extents(text).width <= max_w:
            return text
        while text and cr.text_extents(text + "…").width > max_w:
            text = text[:-1]
        return text + "…"


class WebcamWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Webcam Control")
        self.set_default_size(560, 310)
        self.set_child(WebcamView())


class WebcamApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.wade.webcamcontrol")

    def do_activate(self):
        win = self.props.active_window or WebcamWindow(self)
        win.present()


if __name__ == "__main__":
    WebcamApp().run()
