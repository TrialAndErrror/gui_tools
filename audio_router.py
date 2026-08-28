#!/usr/bin/env python3
"""Drag-and-drop audio router: drag ball (app stream) onto a circle (output device)."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import cairo
import json
import math
import subprocess

POLL_MS = 1500
TICK_MS = 16
EASE = 0.22
CIRCLE_R_MIN = 55
CIRCLE_R_MAX = 90
PILL_H = 30
PILL_PAD_X = 16
PILL_GAP = 10
PILL_FONT_SIZE = 12.5

PALETTE = [
    (0.35, 0.55, 0.95), (0.95, 0.45, 0.35), (0.40, 0.80, 0.55),
    (0.90, 0.70, 0.25), (0.75, 0.45, 0.90), (0.30, 0.80, 0.85),
    (0.90, 0.45, 0.70), (0.60, 0.60, 0.60),
]


def pactl_json(*args):
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=2
        )
        if out.returncode != 0:
            return []
        return json.loads(out.stdout or "[]")
    except Exception:
        return []


def move_sink_input(input_index, sink_index):
    subprocess.run(
        ["pactl", "move-sink-input", str(input_index), str(sink_index)],
        capture_output=True, timeout=2,
    )


def stream_label(props, fallback):
    name = props.get("application.name") or props.get("media.name") or fallback
    return name


class Circle:
    def __init__(self, index, name):
        self.index = index
        self.name = name
        self.x = 0.0
        self.y = 0.0
        self.r = CIRCLE_R_MIN
        self.color = PALETTE[index % len(PALETTE)]
        self.is_default = False


class Ball:
    def __init__(self, index, label, sink_index):
        self.index = index
        self.label = label
        self.sink_index = sink_index
        self.x = None
        self.y = None
        self.w = 80.0
        self.h = PILL_H
        self.target_x = 0.0
        self.target_y = 0.0
        self.dragging = False
        self.drag_dx = 0.0
        self.drag_dy = 0.0
        self.corked = False
        self.muted = False
        self.seen = True


class AudioRouterView(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.circles = {}   # sink_index -> Circle
        self.balls = {}     # input_index -> Ball
        self.default_sink_name = None
        self.dragged_ball = None
        self._measure_cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
        self.set_draw_func(self.on_draw)

        drag = Gtk.GestureDrag.new()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.add_controller(drag)

        GLib.timeout_add(POLL_MS, self.poll)
        GLib.timeout_add(TICK_MS, self.tick)
        self.poll()

    # ---- data ----
    def poll(self):
        sinks = pactl_json("list", "sinks")
        inputs = pactl_json("list", "sink-inputs")
        info = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2)
        default_name = None
        for line in info.stdout.splitlines():
            if line.startswith("Default Sink:"):
                default_name = line.split(":", 1)[1].strip()

        seen_sinks = set()
        for s in sinks:
            idx = s["index"]
            seen_sinks.add(idx)
            c = self.circles.get(idx)
            if c is None:
                c = Circle(idx, s.get("description") or s.get("name") or str(idx))
                self.circles[idx] = c
            else:
                c.name = s.get("description") or s.get("name") or str(idx)
            c.is_default = (s.get("name") == default_name)
        for idx in list(self.circles):
            if idx not in seen_sinks:
                del self.circles[idx]

        raw = []
        for i in inputs:
            props = i.get("properties", {})
            label = stream_label(props, f"stream {i['index']}")
            raw.append((i["index"], label, i.get("sink"), bool(i.get("corked")), bool(i.get("mute"))))

        counts = {}
        for _, label, _, _, _ in raw:
            counts[label] = counts.get(label, 0) + 1

        seen_balls = set()
        ordinal = {}
        for idx, label, sink, corked, muted in sorted(raw, key=lambda t: t[0]):
            seen_balls.add(idx)
            if counts[label] > 1:
                ordinal[label] = ordinal.get(label, 0) + 1
                display = f"{label} #{ordinal[label]}"
            else:
                display = label
            b = self.balls.get(idx)
            if b is None:
                b = Ball(idx, display, sink)
                self.balls[idx] = b
            else:
                b.label = display
                if not b.dragging:
                    b.sink_index = sink
            b.corked = corked
            b.muted = muted
        for idx in list(self.balls):
            b = self.balls[idx]
            if idx not in seen_balls and not b.dragging:
                del self.balls[idx]

        self.layout()
        return True

    def layout(self):
        w = self.get_width() or 900
        h = self.get_height() or 600
        n = max(1, len(self.circles))
        margin = 90
        usable_w = max(200, w - 2 * margin)
        spacing = usable_w / n
        r = max(CIRCLE_R_MIN, min(CIRCLE_R_MAX, spacing * 0.38))
        cy = 150
        for i, c in enumerate(sorted(self.circles.values(), key=lambda c: c.index)):
            c.x = margin + spacing * (i + 0.5)
            c.y = cy
            c.r = r

        mcr = self._measure_cr
        mcr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        mcr.set_font_size(PILL_FONT_SIZE)
        for b in self.balls.values():
            b.w = mcr.text_extents(b.label).width + 2 * PILL_PAD_X
            b.h = PILL_H

        by_sink = {}
        for b in self.balls.values():
            by_sink.setdefault(b.sink_index, []).append(b)

        for sink_idx, blist in by_sink.items():
            c = self.circles.get(sink_idx)
            blist.sort(key=lambda b: b.index)
            n = len(blist)
            if c is None:
                for k, b in enumerate(blist):
                    if b.dragging:
                        continue
                    tx = w / 2 + (k - (n - 1) / 2) * (b.w + PILL_GAP)
                    ty = h - 90
                    b.target_x, b.target_y = tx, ty
                    if b.x is None:
                        b.x, b.y = tx, ty
                continue

            step = 2 * math.pi / n
            for k, b in enumerate(blist):
                if b.dragging:
                    continue
                angle = math.pi / 2 + k * step
                tx = c.x + c.r * math.cos(angle)
                ty = c.y + c.r * math.sin(angle)
                b.target_x, b.target_y = tx, ty
                if b.x is None:
                    b.x, b.y = tx, ty

    def tick(self):
        moving = False
        for b in self.balls.values():
            if b.dragging or b.x is None:
                continue
            dx = b.target_x - b.x
            dy = b.target_y - b.y
            if abs(dx) > 0.3 or abs(dy) > 0.3:
                b.x += dx * EASE
                b.y += dy * EASE
                moving = True
        if moving or self.dragged_ball:
            self.queue_draw()
        return True

    # ---- drag handling ----
    def ball_at(self, x, y):
        for b in self.balls.values():
            if b.x is None:
                continue
            if abs(x - b.x) <= b.w / 2 + 3 and abs(y - b.y) <= b.h / 2 + 3:
                return b
        return None

    def circle_at(self, x, y):
        for c in self.circles.values():
            if math.hypot(c.x - x, c.y - y) <= c.r:
                return c
        return None

    def on_drag_begin(self, gesture, start_x, start_y):
        b = self.ball_at(start_x, start_y)
        if b is None:
            return
        b.dragging = True
        b.drag_dx = start_x - b.x
        b.drag_dy = start_y - b.y
        self.dragged_ball = b
        self.queue_draw()

    def on_drag_update(self, gesture, off_x, off_y):
        b = self.dragged_ball
        if b is None:
            return
        ok, start_x, start_y = gesture.get_start_point()
        b.x = start_x + off_x - b.drag_dx
        b.y = start_y + off_y - b.drag_dy
        self.queue_draw()

    def on_drag_end(self, gesture, off_x, off_y):
        b = self.dragged_ball
        if b is None:
            return
        self.dragged_ball = None
        b.dragging = False
        target = self.circle_at(b.x, b.y)
        if target is not None and target.index != b.sink_index:
            move_sink_input(b.index, target.index)
            b.sink_index = target.index
        self.layout()
        self.queue_draw()

    # ---- drawing ----
    def on_draw(self, area, cr, w, h):
        cr.set_source_rgb(0.11, 0.11, 0.13)
        cr.paint()

        for c in self.circles.values():
            r, g, bl = c.color
            cr.new_path()
            cr.arc(c.x, c.y, c.r, 0, 2 * math.pi)
            cr.set_source_rgba(r, g, bl, 0.18)
            cr.fill_preserve()
            cr.set_source_rgba(r, g, bl, 0.9)
            cr.set_line_width(3 if c.is_default else 1.6)
            cr.stroke()

            cr.set_source_rgb(0.92, 0.92, 0.95)
            self.draw_centered_text(cr, c.name, c.x, c.y + c.r + PILL_H + 20, 13, w_limit=c.r * 2 + 40)
            if c.is_default:
                self.draw_centered_text(cr, "default", c.x, c.y - c.r - PILL_H - 12, 10.5, w_limit=100, color=(r, g, bl))

        dragged = self.dragged_ball
        for b in self.balls.values():
            if b is dragged or b.x is None:
                continue
            self.draw_ball(cr, b)
        if dragged is not None:
            self.draw_ball(cr, dragged, highlight=True)

    def draw_ball(self, cr, b, highlight=False):
        cr.save()
        left = b.x - b.w / 2
        top = b.y - b.h / 2

        if highlight:
            self.rounded_rect(cr, left - 3, top - 3, b.w + 6, b.h + 6, (b.h + 6) / 2)
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.fill()

        idle = b.corked or b.muted
        pill_alpha = 1.0 if highlight else (0.55 if idle else 0.92)
        self.rounded_rect(cr, left, top, b.w, b.h, b.h / 2)
        cr.set_source_rgba(0.97, 0.97, 0.99, pill_alpha)
        cr.fill_preserve()
        cr.set_source_rgba(0.05, 0.05, 0.08, 0.85 if not idle else 0.4)
        cr.set_line_width(1.4)
        cr.stroke()

        cr.set_source_rgba(0.05, 0.05, 0.07, 1.0 if not idle else 0.6)
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(PILL_FONT_SIZE)
        ext = cr.text_extents(b.label)
        cr.move_to(b.x - ext.width / 2 - ext.x_bearing, b.y - ext.height / 2 - ext.y_bearing)
        cr.show_text(b.label)
        cr.restore()

    @staticmethod
    def rounded_rect(cr, x, y, w, h, radius):
        cr.new_path()
        cr.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
        cr.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
        cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def draw_centered_text(self, cr, text, x, y, size, w_limit=140, color=None):
        if color:
            cr.set_source_rgb(*color)
        cr.select_font_face("sans-serif", 0, 0)
        cr.set_font_size(size)
        text = self.truncate(cr, text, w_limit)
        ext = cr.text_extents(text)
        cr.move_to(x - ext.width / 2 - ext.x_bearing, y)
        cr.show_text(text)

    @staticmethod
    def truncate(cr, text, max_w):
        if cr.text_extents(text).width <= max_w:
            return text
        while text and cr.text_extents(text + "…").width > max_w:
            text = text[:-1]
        return text + "…"


class AudioRouterWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Audio Router")
        self.set_default_size(940, 340)
        view = AudioRouterView()
        self.set_child(view)


class AudioRouterApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.wade.audiorouter")

    def do_activate(self):
        win = self.props.active_window or AudioRouterWindow(self)
        win.present()


if __name__ == "__main__":
    AudioRouterApp().run()
