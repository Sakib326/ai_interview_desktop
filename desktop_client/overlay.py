"""Always-on-top translucent desktop overlay window with capture exclusion support.

Implements PySide6 QWidget overlay with:
- Window flags: FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus
- Translucent rounded rect paintEvent with dynamic alpha for black-box fallback
- Pulsing red recording dot, status row, model badge, and stealth badge
- Read-only QTextEdit for final transcript turns and separate QLabel for partials
- Window-level click-through (WindowTransparentForInput)
- Windows display affinity re-application rules across HWND re-creations
"""

import math
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop_client import stealth
from desktop_client.stealth import StealthState


class PulsingDot(QWidget):
    """Small animated pulsing red circle indicating live microphone capture."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)  # ~25 FPS pulse animation

    def _tick(self):
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        # Oscillate alpha between 90 and 255
        alpha = int(172 + 82 * math.sin(self._phase))
        painter.setBrush(QColor(239, 68, 68, alpha))
        painter.drawEllipse(2, 2, 10, 10)


class OverlayWindow(QWidget):
    """Translucent always-on-top overlay for live transcript display."""

    closed = Signal()
    stop_requested = Signal()
    retry_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # §10.4: Neutral window title to prevent title-matching detection heuristics
        self.setWindowTitle("Overlay")

        # Window flags per §4
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )

        # Widget attributes per §4
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # Internal state
        self._click_through: bool = False
        self._stealth_enabled: bool = True
        self._stealth_state: StealthState = StealthState.UNSUPPORTED
        self._drag_pos: Optional[QPoint] = None

        self._init_ui()
        self._setup_shortcuts()

        self.resize(640, 460)
        self.setMinimumSize(460, 280)

    def _init_ui(self):
        """Construct the layout: status row, transcript area, and controls row."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 14, 18, 14)
        main_layout.setSpacing(10)

        # ---------------- 1. Status Row ----------------
        status_row = QHBoxLayout()
        status_row.setSpacing(8)

        # Red pulsing dot
        self.pulsing_dot = PulsingDot(self)
        status_row.addWidget(self.pulsing_dot)

        # Status text ("listening")
        self.status_label = QLabel("listening", self)
        status_font = QFont()
        status_font.setPointSize(10)
        status_font.setBold(True)
        self.status_label.setFont(status_font)
        self.status_label.setStyleSheet("color: #e5e7eb; background: transparent;")
        status_row.addWidget(self.status_label)

        # Stealth badge (§10.5)
        self.stealth_badge = QLabel(self)
        self.stealth_badge.setStyleSheet(
            "padding: 2px 8px; border-radius: 9px; font-size: 11px; font-weight: bold; background: #374151; color: #9ca3af;"
        )
        status_row.addWidget(self.stealth_badge)

        status_row.addStretch()

        # Model name (right-aligned)
        self.model_label = QLabel("gemini-2.5-flash", self)
        self.model_label.setStyleSheet("color: #9ca3af; font-size: 11px; background: transparent;")
        status_row.addWidget(self.model_label)

        main_layout.addLayout(status_row)

        # ---------------- 2. Transcript Area ----------------
        # Read-only QTextEdit for final turns
        self.transcript_edit = QTextEdit(self)
        self.transcript_edit.setReadOnly(True)
        self.transcript_edit.setFrameShape(QFrame.Shape.NoFrame)
        self.transcript_edit.setStyleSheet(
            """
            QTextEdit {
                background: transparent;
                color: #f3f4f6;
                font-size: 13px;
                line-height: 1.4;
                border: none;
            }
            QScrollBar:vertical {
                background: rgba(255, 255, 255, 10);
                width: 6px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 60);
                border-radius: 3px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )
        main_layout.addWidget(self.transcript_edit, stretch=1)

        # ---------------- 2b. Live answer area ----------------
        self.answer_header = QLabel("answer", self)
        self.answer_header.setStyleSheet(
            "color: #6ee7b7; font-size: 11px; font-weight: bold; "
            "letter-spacing: 1px; background: transparent;"
        )
        self.answer_header.hide()
        main_layout.addWidget(self.answer_header)

        self.answer_edit = QTextEdit(self)
        self.answer_edit.setReadOnly(True)
        self.answer_edit.setFrameShape(QFrame.Shape.NoFrame)
        self.answer_edit.setStyleSheet(
            """
            QTextEdit {
                background: rgba(16, 185, 129, 0.08);
                color: #ecfdf5;
                font-size: 13px;
                border: none;
                border-radius: 8px;
                padding: 6px;
            }
            QScrollBar:vertical {
                background: rgba(255, 255, 255, 10);
                width: 6px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 60);
                border-radius: 3px;
            }
            """
        )
        self.answer_edit.hide()
        main_layout.addWidget(self.answer_edit, stretch=1)

        # Separate QLabel for mutable partials to prevent full-text reflow on every delta
        self.partial_label = QLabel(self)
        self.partial_label.setWordWrap(True)
        self.partial_label.setStyleSheet("color: #9ca3af; font-size: 12px; font-style: italic; background: transparent;")
        main_layout.addWidget(self.partial_label)

        # ---------------- 3. Controls Row ----------------
        self.controls_layout = QHBoxLayout()
        self.controls_layout.setSpacing(8)

        self.click_through_btn = QPushButton("Click-through (F8)", self)
        self.click_through_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.12); color: #d1d5db; border-radius: 6px; padding: 4px 10px; font-size: 11px; border: none; }"
            "QPushButton:hover { background: rgba(255,255,255,0.22); color: #ffffff; }"
        )
        self.click_through_btn.clicked.connect(self._on_toggle_click_through_btn)
        self.controls_layout.addWidget(self.click_through_btn)

        self.stealth_btn = QPushButton("Stealth: ON (F9)", self)
        self.stealth_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.12); color: #d1d5db; border-radius: 6px; padding: 4px 10px; font-size: 11px; border: none; }"
            "QPushButton:hover { background: rgba(255,255,255,0.22); color: #ffffff; }"
        )
        self.stealth_btn.clicked.connect(self.toggle_stealth)
        self.controls_layout.addWidget(self.stealth_btn)

        self.controls_layout.addStretch()

        self.retry_btn = QPushButton("Retry", self)
        self.retry_btn.setStyleSheet(
            "QPushButton { background: rgba(251,191,36,0.25); color: #fcd34d; border-radius: 6px; padding: 4px 12px; font-size: 11px; border: none; }"
            "QPushButton:hover { background: rgba(251,191,36,0.45); color: #ffffff; }"
        )
        self.retry_btn.clicked.connect(self.retry_requested.emit)
        self.retry_btn.hide()
        self.controls_layout.addWidget(self.retry_btn)

        self.stop_btn = QPushButton("Stop", self)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: rgba(239,68,68,0.25); color: #fca5a5; border-radius: 6px; padding: 4px 12px; font-size: 11px; border: none; }"
            "QPushButton:hover { background: rgba(239,68,68,0.45); color: #ffffff; }"
        )
        self.stop_btn.clicked.connect(self.on_stop_clicked)
        self.controls_layout.addWidget(self.stop_btn)

        self.close_btn = QPushButton("Close", self)
        self.close_btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.12); color: #d1d5db; border-radius: 6px; padding: 4px 12px; font-size: 11px; border: none; }"
            "QPushButton:hover { background: rgba(255,255,255,0.22); color: #ffffff; }"
        )
        self.close_btn.clicked.connect(self.close)
        self.controls_layout.addWidget(self.close_btn)

        main_layout.addLayout(self.controls_layout)

        # Badge starts as capture-visible; showEvent() applies the real
        # affinity and updates the badge from the API result.
        self.set_stealth_state(StealthState.UNSUPPORTED)

    def _setup_shortcuts(self):
        """Hardcoded keyboard shortcuts for skeleton iteration.

        Note per §5: WindowDoesNotAcceptFocus may prevent keyboard delivery
        when overlay is unfocused; OS-level global hotkeys or tray are planned
        for production.
        """
        self.shortcut_click_through = QShortcut(QKeySequence("F8"), self)
        self.shortcut_click_through.activated.connect(self._on_toggle_click_through_btn)

        self.shortcut_stealth = QShortcut(QKeySequence("F9"), self)
        self.shortcut_stealth.activated.connect(self.toggle_stealth)

    # ---------------- Paint Event ----------------
    def paintEvent(self, event):
        """Draw antialiased rounded background.

        §10.3 Rule 6: If state == BLACK_BOX, raise paint alpha toward 255
        so capture-side black box looks intentional instead of broken.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        if self._stealth_state == StealthState.BLACK_BOX:
            bg_color = QColor(0, 0, 0, 250)
        else:
            bg_color = QColor(0, 0, 0, 200)

        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 14, 14)
        painter.fillPath(path, bg_color)

    # ---------------- Stealth Re-application Rules (§10.3) ----------------
    def showEvent(self, event):
        """Rule 1: Re-apply stealth in showEvent."""
        super().showEvent(event)
        QTimer.singleShot(0, self._reapply_stealth)

    def _reapply_stealth(self):
        """Rule 2 & 4: Re-apply stealth using current state field on GUI thread."""
        if self._stealth_enabled:
            state = stealth.apply(self)
            self.set_stealth_state(state)
        else:
            stealth.clear(self)
            # Manually disabled means capture-visible, not an API failure.
            self.set_stealth_state(StealthState.UNSUPPORTED)

    def toggle_stealth(self):
        """Toggle stealth exclusion on/off."""
        self._stealth_enabled = not self._stealth_enabled
        self.stealth_btn.setText(f"Stealth: {'ON' if self._stealth_enabled else 'OFF'} (F9)")
        self._reapply_stealth()

    # ---------------- Click-Through Mode (§5) ----------------
    def set_click_through(self, enabled: bool):
        """Toggle click-through transparency using WindowTransparentForInput.

        Never sets WA_TransparentForMouseEvents on the widget while expecting
        child buttons to work, as Qt disables mouse delivery to children too.
        """
        if self._click_through == enabled:
            return
        self._click_through = enabled

        # WindowTransparentForInput passes all clicks to underlying applications
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)

        # Qt destroys and recreates the native HWND on setWindowFlag!
        self.show()

        # Re-apply stealth via singleShot(0) as HWND does not exist synchronously
        QTimer.singleShot(0, self._reapply_stealth)

        # Update button text
        self.click_through_btn.setText(f"Click-through: {'ON' if enabled else 'OFF'} (F8)")
        self.update()

    def _on_toggle_click_through_btn(self):
        self.set_click_through(not self._click_through)

    # ---------------- Dragging (Interactive Mode Only) ----------------
    def mousePressEvent(self, event: QMouseEvent):
        if not self._click_through and event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle and hasattr(handle, "startSystemMove") and handle.startSystemMove():
                event.accept()
                return
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._click_through and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # ---------------- Public API ----------------
    def set_transcript(self, text: str):
        """Set the finalized transcript text."""
        self.transcript_edit.setPlainText(text)
        # Scroll to bottom
        scrollbar = self.transcript_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def append_transcript(self, text: str):
        """Append finalized turn text to transcript."""
        current = self.transcript_edit.toPlainText()
        if current:
            self.transcript_edit.setPlainText(current + "\n" + text)
        else:
            self.transcript_edit.setPlainText(text)
        scrollbar = self.transcript_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_partial(self, text: str):
        """Update mutable partial transcript line."""
        self.partial_label.setText(text)

    def set_status(self, text: str):
        """Set the status row text."""
        self.status_label.setText(text)

    def set_model(self, text: str):
        """Set the model name badge."""
        self.model_label.setText(text)

    # ---------------- Transcript / answer rendering ----------------
    def set_turns(self, turns):
        """Render [(label, text), ...] as the finalized transcript."""
        lines = [f"{label}: {text}" for label, text in turns]
        self.transcript_edit.setPlainText("\n".join(lines))
        scrollbar = self.transcript_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _ensure_answer_visible(self):
        if not self.answer_header.isVisible():
            self.answer_header.show()
            self.answer_edit.show()

    def set_answer_loading(self):
        """Show the answer pane with a pending placeholder."""
        self._ensure_answer_visible()
        self.answer_header.setText("answer")
        self.answer_edit.setPlainText("thinking\u2026")
        self._scroll_answer()

    def append_answer_token(self, text: str):
        """Append one streamed answer token."""
        self._ensure_answer_visible()
        self.answer_header.setText("answer")
        if self.answer_edit.toPlainText() == "thinking\u2026":
            self.answer_edit.setPlainText("")
        cursor = self.answer_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.answer_edit.setTextCursor(cursor)
        self._scroll_answer()

    def set_answer(self, text: str, title: str = "answer"):
        """Replace the answer pane with the validated final answer."""
        self._ensure_answer_visible()
        self.answer_header.setText(title)
        self.answer_edit.setPlainText(text)
        self._scroll_answer()

    def set_answer_error(self, message: str, retryable: bool = False):
        self._ensure_answer_visible()
        self.answer_header.setText("answer unavailable")
        self.answer_edit.setPlainText(message)
        self.retry_btn.setVisible(retryable)
        self._scroll_answer()

    def clear_answer(self):
        self.answer_edit.clear()
        self.answer_edit.hide()
        self.answer_header.hide()
        self.retry_btn.hide()

    def _scroll_answer(self):
        scrollbar = self.answer_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_capture_state(self, state: str):
        """Reflect the backend capture_status snapshot for the mic source."""
        if state == "active":
            self.set_status("listening")
        elif state == "paused":
            self.set_status("paused")
        elif state == "inactive":
            self.set_status("stopped")

    def set_stealth_state(self, state: StealthState):
        """Update stealth state field and UI badge (§10.5)."""
        self._stealth_state = state

        badge_configs = {
            StealthState.EXCLUDED: ("hidden from share", "#14532d", "#4ade80"),
            StealthState.BLACK_BOX: ("hidden (opaque)", "#78350f", "#fbbf24"),
            StealthState.FAILED: ("capture exclusion failed", "#7f1d1d", "#f87171"),
            StealthState.UNSUPPORTED: ("visible to share", "#374151", "#9ca3af"),
        }

        text, bg_color, text_color = badge_configs.get(
            state, ("visible to share", "#374151", "#9ca3af")
        )

        self.stealth_badge.setText(text)
        self.stealth_badge.setStyleSheet(
            f"padding: 2px 8px; border-radius: 9px; font-size: 11px; font-weight: bold; background: {bg_color}; color: {text_color};"
        )
        self.update()

    def closeEvent(self, event):
        """Notify the owner before the overlay hides."""
        self.closed.emit()
        super().closeEvent(event)

    def on_stop_clicked(self):
        """Request a graceful audio stop; the window stays open."""
        self.set_status("stopping")
        self.set_partial("Capture stopping...")
        self.stop_requested.emit()
