"""
automation_lane.py -- Automation Envelope ("Cello Curve") System for Crystal DAW.
==================================================================================
Provides per-track parameter automation with a visual node-based curve editor
that sits below each track row in the arrangement timeline.

Classes:
    AutomationNode     -- Single breakpoint: (beat_position, normalised_value 0-1).
    AutomationEnvelope -- Ordered list of nodes; linear interpolation + I/O helpers.
    AutomationLane     -- QWidget curve editor bound to one AudioFxChain.
    AutomationPanel    -- Scrollable vertical stack of visible AutomationLane widgets.

Real-time execution:
    AudioFxChain.apply_automation(beat_pos) evaluates every envelope stored in
    chain.envelopes and writes the results directly to chain.volume / chain.pan /
    plugin params.  The caller (MainWindow._on_refresh_tick) drives this at 20 Hz
    from the GUI thread -- no allocations happen on the C++ side because each
    affected setter (e.g. Flanger.set_rate) only writes to a float member.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QComboBox,
    QLabel, QSizePolicy, QScrollArea,
)
from PySide6.QtCore import Qt, Signal, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QPainterPath, QBrush, QFont,
)

if TYPE_CHECKING:
    from .audio_fx_chain import AudioFxChain


# ---------------------------------------------------------------------------
# Data models -- pure Python, no Qt, safe to access from any thread.
# ---------------------------------------------------------------------------

@dataclass
class AutomationNode:
    """A single breakpoint on an automation curve."""
    beat_pos: float   # X-axis: timeline position in beats
    value:    float   # Y-axis: normalised 0.0 (min) … 1.0 (max)


@dataclass
class AutomationEnvelope:
    """
    One automation curve for a single parameter.

    Nodes are kept sorted by beat_pos.  evaluate() performs piecewise-linear
    interpolation; values outside the node range are clamped to the nearest
    endpoint (flat extension to the left and right of the node span).

    The object lives inside AudioFxChain.envelopes[key] so both the QWidget
    editor and AudioFxChain.apply_automation() share the same instance.
    """

    target_key: str   = ""    # e.g. "volume", "pan", "Flanger.rate"
    min_val:    float = 0.0   # Actual parameter minimum
    max_val:    float = 1.0   # Actual parameter maximum
    nodes: List[AutomationNode] = field(default_factory=list)

    # ── Value evaluation ──────────────────────────────────────────────────────

    def evaluate(self, beat_pos: float) -> float:
        """Return the actual parameter value at beat_pos via linear interpolation."""
        if not self.nodes:
            # No nodes: return the midpoint so existing processing is unchanged
            return (self.min_val + self.max_val) * 0.5

        nodes = sorted(self.nodes, key=lambda n: n.beat_pos)

        # Flat extension before first node
        if beat_pos <= nodes[0].beat_pos:
            return self._denorm(nodes[0].value)
        # Flat extension after last node
        if beat_pos >= nodes[-1].beat_pos:
            return self._denorm(nodes[-1].value)

        # Piecewise linear interpolation
        for i in range(len(nodes) - 1):
            a, b = nodes[i], nodes[i + 1]
            if a.beat_pos <= beat_pos <= b.beat_pos:
                span = b.beat_pos - a.beat_pos
                if span < 1e-9:
                    return self._denorm(a.value)
                t = (beat_pos - a.beat_pos) / span
                return self._denorm(a.value + t * (b.value - a.value))

        return self._denorm(nodes[-1].value)

    def _denorm(self, norm: float) -> float:
        """Normalised 0–1 → actual parameter value, clamped to [min, max]."""
        return self.min_val + max(0.0, min(1.0, norm)) * (self.max_val - self.min_val)

    def norm(self, actual: float) -> float:
        """Actual parameter value → normalised 0–1."""
        rng = self.max_val - self.min_val
        if rng < 1e-9:
            return 0.5
        return max(0.0, min(1.0, (actual - self.min_val) / rng))

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "target_key": self.target_key,
            "min_val":    self.min_val,
            "max_val":    self.max_val,
            "nodes":      [{"beat": n.beat_pos, "value": n.value}
                           for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutomationEnvelope":
        env = cls(
            target_key=d.get("target_key", ""),
            min_val=d.get("min_val", 0.0),
            max_val=d.get("max_val", 1.0),
        )
        env.nodes = [
            AutomationNode(beat_pos=n["beat"], value=n["value"])
            for n in d.get("nodes", [])
        ]
        return env


# ---------------------------------------------------------------------------
# Parameter range heuristics
# ---------------------------------------------------------------------------

def _infer_range(key: str, current: float) -> tuple:
    """Guess a sensible (min, max) range from a parameter name convention."""
    k = key.lower()
    if k in ("volume", "output"):          return (0.0,  2.0)
    if "pan" in k:                         return (-1.0, 1.0)
    if "attack" in k and "ms" in k:        return (0.1,  200.0)
    if "release" in k and "ms" in k:       return (10.0, 2000.0)
    if "decay" in k and "ms" in k:         return (1.0,  1000.0)
    if "_ms" in k or k.endswith("ms"):     return (0.0,  2000.0)
    if "crossover" in k and "hz" in k:     return (200.0, 10000.0)
    if "hz" in k or "freq" in k:           return (20.0, 20000.0)
    if "rate" in k:                        return (0.0,  10.0)
    if "depth" in k or "amount" in k:      return (0.0,  1.0)
    if "drive" in k or "pregain" in k:     return (0.0,  1.0)
    if "sustain" in k or "mix" in k:       return (0.0,  1.0)
    if "wet" in k:                         return (0.0,  1.0)
    if "feedback" in k:                    return (0.0,  0.99)
    if "threshold" in k:                   return (-60.0, 0.0)
    if "db" in k:                          return (-60.0, 6.0)
    if "ratio" in k:                       return (1.0,  20.0)
    if "semitone" in k:                    return (-12.0, 12.0)
    if "width" in k:                       return (0.0,  2.0)
    # Fallback: scale around current value
    mag = abs(current) * 4.0 if current != 0.0 else 1.0
    return (0.0, max(1.0, mag))


def build_target_list(chain: "AudioFxChain") -> List[tuple]:
    """
    Enumerate automatable parameters exposed by a chain.
    Returns list of (display_name, key, min_val, max_val) tuples.
    Volume and pan are always first; plugin float params follow.
    """
    targets = [
        ("Volume",  "volume",  0.0,  2.0),
        ("Panning", "pan",    -1.0,  1.0),
    ]
    for plugin in chain.plugins:
        if plugin is None or not hasattr(plugin, "get_params"):
            continue
        pname = getattr(plugin, "DISPLAY_NAME", "Plugin")
        try:
            params = plugin.get_params()
        except Exception:
            continue
        for pkey, pval in params.items():
            if not isinstance(pval, float):
                continue
            lo, hi = _infer_range(pkey, pval)
            targets.append((
                f"{pname}: {pkey}",
                f"{pname}.{pkey}",
                lo, hi,
            ))
    return targets


# ---------------------------------------------------------------------------
# AutomationLane -- QWidget curve editor for one track
# ---------------------------------------------------------------------------

class AutomationLane(QWidget):
    """
    Draws and edits automation envelopes for a single track.

    The lane writes directly into chain.envelopes[key] so AudioFxChain.apply_automation()
    picks up every node edit on its next call with no copying.

    Interactions:
        Ctrl + Left-click on empty area → add node at cursor
        Left-click + drag on existing node → move node
        Right-click on existing node → delete node

    The left-side header (HEADER_W = 168 px) contains a target-parameter
    dropdown and a track label; the remaining width is the curve canvas.
    Horizontal scroll and zoom level are kept in sync with TrackArrangeView
    via set_view_x() / set_beat_width().
    """

    LANE_HEIGHT: int = 72    # Fixed pixel height of one lane
    NODE_RADIUS: int = 6     # Node handle radius (pixels)
    HEADER_W:    int = 168   # Must match TrackArrangeView.HEADER_WIDTH

    # Emitted after any node change so the project can be flagged as dirty.
    envelope_changed = Signal()

    def __init__(self,
                 chain: "AudioFxChain",
                 track_id: int,
                 track_name: str,
                 track_color: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._chain       = chain
        self._track_id    = track_id
        self._track_name  = track_name
        self._track_color = track_color

        # Timeline view state -- synchronised with TrackArrangeView
        self._view_x:     float = 0.0  # Leftmost visible beat
        self._beat_width: int   = 18   # Pixels per beat

        # Active target key shown in the dropdown
        self._active_key: str = "volume"
        self._ensure_envelope("volume", 0.0, 2.0)

        # Mouse interaction state
        self._dragged_node: Optional[AutomationNode] = None
        self._hover_node:   Optional[AutomationNode] = None

        self.setFixedHeight(self.LANE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self._build_header_ui()
        self._refresh_dropdown()

    # ── Header controls ───────────────────────────────────────────────────────

    def _build_header_ui(self) -> None:
        """Create the target-parameter dropdown and track name label overlaid on
        the left header region of the lane widget."""
        self._dropdown = QComboBox(self)
        self._dropdown.setGeometry(4, 4, self.HEADER_W - 8, 22)
        self._dropdown.setStyleSheet("""
            QComboBox {
                background: #0A0E22;
                color: #00E5FF;
                border: 1px solid rgba(0,229,255,0.4);
                border-radius: 3px;
                font-size: 10px;
                padding: 0 4px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background: #0A0E22;
                color: #C8E6FF;
                selection-background-color: #00E5FF;
                selection-color: #030308;
                border: 1px solid #00E5FF;
            }
        """)
        self._dropdown.currentIndexChanged.connect(self._on_target_changed)

        # Track name hint under the dropdown
        self._track_lbl = QLabel(f"AUTO  ·  {self._track_name}", self)
        self._track_lbl.setGeometry(4, 28, self.HEADER_W - 8, 14)
        self._track_lbl.setStyleSheet(
            "color:#3D5A80; font-size:9px; background:transparent;")

    def _ensure_envelope(self, key: str,
                         lo: float, hi: float) -> AutomationEnvelope:
        """Return the chain's envelope for key, creating a fresh one if absent."""
        if key not in self._chain.envelopes:
            self._chain.envelopes[key] = AutomationEnvelope(
                target_key=key, min_val=lo, max_val=hi)
        return self._chain.envelopes[key]

    def _refresh_dropdown(self) -> None:
        """Repopulate the dropdown by querying the chain for automatable params."""
        self._dropdown.blockSignals(True)
        self._dropdown.clear()
        for display, key, lo, hi in build_target_list(self._chain):
            self._dropdown.addItem(display, userData=(key, lo, hi))
        # Restore selection to the previously active key if still present
        for i in range(self._dropdown.count()):
            data = self._dropdown.itemData(i)
            if data and data[0] == self._active_key:
                self._dropdown.setCurrentIndex(i)
                break
        self._dropdown.blockSignals(False)

    # ── Target dropdown handler ───────────────────────────────────────────────

    def _on_target_changed(self, idx: int) -> None:
        """User picked a new target parameter -- switch the active envelope."""
        data = self._dropdown.itemData(idx)
        if data:
            key, lo, hi = data
            self._active_key = key
            self._ensure_envelope(key, lo, hi)
            self.update()

    @property
    def _active_env(self) -> AutomationEnvelope:
        """The envelope currently shown and edited in this lane."""
        return self._chain.envelopes.get(
            self._active_key,
            AutomationEnvelope(target_key=self._active_key))

    # ── View sync (called by AutomationPanel on scroll / zoom) ───────────────

    def set_view_x(self, beat: float) -> None:
        """Align horizontal scroll with the main timeline (beat = leftmost beat)."""
        self._view_x = max(0.0, beat)
        self.update()

    def set_beat_width(self, bw: int) -> None:
        """Match zoom level of the main timeline (pixels per beat)."""
        self._beat_width = max(4, bw)
        self.update()

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _beat_to_x(self, beat: float) -> float:
        return self.HEADER_W + (beat - self._view_x) * self._beat_width

    def _x_to_beat(self, x: float) -> float:
        if self._beat_width <= 0:
            return 0.0
        return max(0.0, (x - self.HEADER_W) / self._beat_width + self._view_x)

    def _value_to_y(self, norm: float) -> float:
        """Normalised 0–1 → pixel Y; 1.0 maps to the top, 0.0 to the bottom."""
        pad = 8
        return pad + (1.0 - max(0.0, min(1.0, norm))) * (self.height() - pad * 2)

    def _y_to_value(self, y: float) -> float:
        """Pixel Y → normalised 0–1."""
        pad = 8
        h = self.height() - pad * 2
        return max(0.0, min(1.0, 1.0 - (y - pad) / max(1, h)))

    def _node_at(self, x: float, y: float) -> Optional[AutomationNode]:
        """Return the node nearest to (x, y) within NODE_RADIUS, or None."""
        best, best_d2 = None, (self.NODE_RADIUS * 1.6) ** 2
        for node in self._active_env.nodes:
            nx = self._beat_to_x(node.beat_pos)
            ny = self._value_to_y(node.value)
            d2 = (x - nx) ** 2 + (y - ny) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = node
        return best

    # ── Mouse events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, ev) -> None:
        x, y = ev.position().x(), ev.position().y()
        # Clicks in the header region go to the dropdown / label -- ignore here.
        if x < self.HEADER_W:
            return

        if ev.button() == Qt.RightButton:
            # Right-click: delete the nearest node
            node = self._node_at(x, y)
            if node and node in self._active_env.nodes:
                self._active_env.nodes.remove(node)
                self._hover_node = None
                self.envelope_changed.emit()
                self.update()
            return

        if ev.button() == Qt.LeftButton:
            node = self._node_at(x, y)
            if node:
                # Begin dragging an existing node
                self._dragged_node = node
            elif ev.modifiers() & Qt.ControlModifier:
                # Ctrl + click: add a new node at cursor position
                new_node = AutomationNode(
                    beat_pos=self._x_to_beat(x),
                    value=self._y_to_value(y),
                )
                self._active_env.nodes.append(new_node)
                self._active_env.nodes.sort(key=lambda n: n.beat_pos)
                self._dragged_node = new_node
                self.envelope_changed.emit()
                self.update()

    def mouseMoveEvent(self, ev) -> None:
        x, y = ev.position().x(), ev.position().y()
        if self._dragged_node:
            # Move the grabbed node, keeping x >= 0 and y within canvas
            self._dragged_node.beat_pos = max(0.0, self._x_to_beat(x))
            self._dragged_node.value    = self._y_to_value(y)
            # Re-sort so the line always draws correctly
            self._active_env.nodes.sort(key=lambda n: n.beat_pos)
            self.envelope_changed.emit()
            self.update()
        else:
            # Update hover highlight for cursor feedback
            node = self._node_at(x, y)
            if node != self._hover_node:
                self._hover_node = node
                self.setCursor(Qt.SizeAllCursor if node else
                               (Qt.CrossCursor if x >= self.HEADER_W
                                else Qt.ArrowCursor))
                self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if self._dragged_node:
            self._dragged_node = None
            self.envelope_changed.emit()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        # Canvas background
        p.fillRect(0, 0, W, H, QColor("#07091A"))

        # Header background + right border
        p.fillRect(0, 0, self.HEADER_W, H, QColor("#0A0E22"))
        p.setPen(QPen(QColor(0, 229, 255, 40), 1))
        p.drawLine(self.HEADER_W, 0, self.HEADER_W, H)

        # Thin track-color bar on the far left edge
        p.fillRect(0, 0, 3, H, QColor(self._track_color))

        # Dashed centre-line (default / midpoint value reference)
        env = self._active_env
        mid_norm = env.norm((env.min_val + env.max_val) * 0.5)
        mid_y = int(self._value_to_y(mid_norm))
        p.setPen(QPen(QColor(0, 229, 255, 18), 1, Qt.DashLine))
        p.drawLine(self.HEADER_W, mid_y, W, mid_y)

        # Draw curve or placeholder hint
        nodes = sorted(env.nodes, key=lambda n: n.beat_pos)
        if nodes:
            self._draw_curve(p, nodes, W, H)
        else:
            p.setPen(QColor(61, 90, 128, 160))
            p.setFont(QFont("Arial", 8))
            p.drawText(self.HEADER_W + 10, H // 2 + 4,
                       "Ctrl + Click to add automation nodes")

        # Bottom separator
        p.setPen(QPen(QColor(0, 229, 255, 22), 1))
        p.drawLine(0, H - 1, W, H - 1)
        p.end()

    def _draw_curve(self, p: QPainter,
                    nodes: List[AutomationNode], W: int, H: int) -> None:
        """Draw the envelope: filled area + line + node handles."""
        env = self._active_env

        # Build the polyline: flat extension left → nodes → flat extension right
        first_y = self._value_to_y(nodes[0].value)
        last_y  = self._value_to_y(nodes[-1].value)

        path = QPainterPath()
        path.moveTo(self.HEADER_W, first_y)
        for node in nodes:
            nx = self._beat_to_x(node.beat_pos)
            ny = self._value_to_y(node.value)
            path.lineTo(nx, ny)
        path.lineTo(W, last_y)

        # Translucent fill below the curve
        fill = QPainterPath(path)
        fill.lineTo(W, H)
        fill.lineTo(self.HEADER_W, H)
        fill.closeSubpath()
        fc = QColor("#00E5FF")
        fc.setAlpha(18)
        p.fillPath(fill, QBrush(fc))

        # The envelope line
        p.setPen(QPen(QColor("#00E5FF"), 1.8,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)

        # Node handles -- gold when hovered/dragged, cyan otherwise
        for node in nodes:
            nx = self._beat_to_x(node.beat_pos)
            ny = self._value_to_y(node.value)
            active = (node is self._hover_node or node is self._dragged_node)
            p.setPen(QPen(QColor("#030308"), 1.5))
            p.setBrush(QBrush(QColor("#FFD700" if active else "#00E5FF")))
            p.drawEllipse(QPointF(nx, ny), self.NODE_RADIUS, self.NODE_RADIUS)

    # ── Serialisation passthrough ─────────────────────────────────────────────

    def get_active_key(self) -> str:
        return self._active_key

    def set_active_key(self, key: str) -> None:
        """Restore the active dropdown selection after a project load."""
        self._active_key = key
        self._refresh_dropdown()
        self.update()


# ---------------------------------------------------------------------------
# AutomationPanel -- vertical stack of lanes shown below the timeline
# ---------------------------------------------------------------------------

class AutomationPanel(QWidget):
    """
    Scrollable container widget shown in a QSplitter below TrackArrangeView.
    Stacks AutomationLane widgets vertically.
    Horizontal scroll and zoom are kept in sync with the main timeline.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Maps track_id → AutomationLane so lanes can be added/removed by id.
        self._lanes: Dict[int, AutomationLane] = {}

        # Inner content widget that holds the lane stack
        self._content = QWidget()
        self._content.setStyleSheet("background:#060A18;")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)
        self._layout.addStretch()   # Pushes lanes to the top

        # Wrap in a scroll area so many lanes don't overflow the panel
        self._scroll = QScrollArea(self)
        self._scroll.setWidget(self._content)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet("background:#060A18; border:none;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._scroll)

        self.setStyleSheet("background:#060A18;")
        self.setMinimumHeight(0)
        # Panel starts hidden; shown automatically when the first lane is added.
        self.hide()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_lane(self, track_id: int, chain: "AudioFxChain",
                 name: str, color: str) -> AutomationLane:
        """
        Show an automation lane for track_id.
        If a lane for this track already exists it is returned unchanged.
        """
        if track_id in self._lanes:
            return self._lanes[track_id]
        lane = AutomationLane(chain, track_id, name, color)
        self._lanes[track_id] = lane
        # Insert before the stretch item so the stretch stays at the bottom
        self._layout.insertWidget(self._layout.count() - 1, lane)
        self.show()
        self._update_min_height()
        return lane

    def remove_lane(self, track_id: int) -> None:
        """Hide and destroy the lane for track_id."""
        lane = self._lanes.pop(track_id, None)
        if lane:
            self._layout.removeWidget(lane)
            lane.deleteLater()
        if not self._lanes:
            self.hide()
        else:
            self._update_min_height()

    def has_lane(self, track_id: int) -> bool:
        return track_id in self._lanes

    def get_lane(self, track_id: int) -> Optional[AutomationLane]:
        return self._lanes.get(track_id)

    def set_view_x(self, beat: float) -> None:
        """Synchronise horizontal scroll offset with the arrangement view."""
        for lane in self._lanes.values():
            lane.set_view_x(beat)

    def set_beat_width(self, bw: int) -> None:
        """Synchronise zoom level with the arrangement view."""
        for lane in self._lanes.values():
            lane.set_beat_width(bw)

    def all_lanes(self) -> Dict[int, AutomationLane]:
        return dict(self._lanes)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _update_min_height(self) -> None:
        """Keep the panel a sensible size so lanes are immediately visible."""
        n = len(self._lanes)
        # Show up to 3 lanes worth at once; the scroll area handles the rest.
        self.setMinimumHeight(
            min(n * AutomationLane.LANE_HEIGHT + 4,
                3 * AutomationLane.LANE_HEIGHT + 4))
