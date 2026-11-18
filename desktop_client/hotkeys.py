"""Windows system integration: global hotkeys and a tray menu.

Both exist because the overlay uses ``WindowDoesNotAcceptFocus``: a Qt
``QShortcut`` only fires while the window is active, which it never is during a
call. Global hotkeys are registered with ``RegisterHotKey`` so F8/F9 work while
another application has focus. The tray menu is the escape hatch when the
overlay is in click-through mode and cannot be clicked at all.

Everything degrades to a no-op off Windows or when registration fails.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
VK_F8 = 0x77
VK_F9 = 0x78


class _WindowsHotkeyFilter(QObject):
    """Processes WM_HOTKEY messages and dispatches them to callbacks."""

    def __init__(self, callbacks: Dict[int, Callable[[], None]]):
        super().__init__()
        self._callbacks = callbacks
        self._filter = None

    def install(self) -> None:
        from PySide6.QtCore import QAbstractNativeEventFilter

        outer = self

        class _Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):  # noqa: N802
                try:
                    import ctypes
                    from ctypes import wintypes

                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == WM_HOTKEY:
                        callback = outer._callbacks.get(int(msg.wParam))
                        if callback is not None:
                            callback()
                            return True, 0
                except (Exception, KeyboardInterrupt):  # pragma: no cover - never crash the event loop
                    logger.debug("Hotkey filter error", exc_info=True)
                return False, 0

        self._filter = _Filter()
        QApplication.instance().installNativeEventFilter(self._filter)

    def uninstall(self) -> None:
        if self._filter is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None


class GlobalHotkeys:
    """Registers process-wide hotkeys (Windows only)."""

    def __init__(self, widget: QWidget, bindings: Dict[int, Callable[[], None]]):
        self.widget = widget
        self.bindings = bindings
        self._registered: list = []
        self._filter: Optional[_WindowsHotkeyFilter] = None
        self._next_id = 0xB000

    def register(self) -> bool:
        import sys

        if sys.platform != "win32":
            logger.info("Global hotkeys are Windows-only; shortcuts remain local")
            return False
        try:
            import ctypes

            hwnd = int(self.widget.winId())
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            id_callbacks: Dict[int, Callable[[], None]] = {}
            self._filter = _WindowsHotkeyFilter(id_callbacks)
            self._filter.install()
            for vk, callback in self.bindings.items():
                hotkey_id = self._next_id
                self._next_id += 1
                if user32.RegisterHotKey(hwnd, hotkey_id, MOD_NOREPEAT, vk):
                    self._registered.append((hwnd, hotkey_id))
                    id_callbacks[hotkey_id] = callback
                else:
                    logger.warning("RegisterHotKey failed for VK 0x%02X", vk)
            return bool(self._registered)
        except Exception as exc:
            logger.warning("Global hotkey registration failed: %s", exc)
            return False

    def unregister(self) -> None:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            for hwnd, hotkey_id in self._registered:
                user32.UnregisterHotKey(hwnd, hotkey_id)
        except Exception:  # pragma: no cover - shutdown best effort
            pass
        self._registered.clear()
        if self._filter is not None:
            self._filter.uninstall()
            self._filter = None


def _tray_icon() -> QIcon:
    """Draw a small dot icon so the build needs no asset files."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(17, 24, 39, 235))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(1, 1, 30, 30)
    painter.setBrush(QColor(239, 68, 68))
    painter.drawEllipse(11, 11, 10, 10)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QObject):
    """Tray entry point: show/hide, toggles, and quit."""

    def __init__(self, overlay, on_quit: Callable[[], None], extra_actions=None):
        super().__init__(overlay)
        self.overlay = overlay
        self._on_quit = on_quit

        self.tray = QSystemTrayIcon(_tray_icon(), self)
        self.tray.setToolTip("Overlay")

        menu = QMenu()

        self.show_action = QAction("Hide overlay", menu)
        self.show_action.triggered.connect(self._toggle_visibility)

        self.click_through_action = QAction("Click-through", menu)
        self.click_through_action.triggered.connect(
            self.overlay._on_toggle_click_through_btn
        )

        self.stealth_action = QAction("Stealth exclusion", menu)
        self.stealth_action.triggered.connect(self.overlay.toggle_stealth)

        self.stop_action = QAction("Stop capture", menu)
        self.stop_action.triggered.connect(self.overlay.on_stop_clicked)

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._on_quit)

        menu.addAction(self.show_action)
        menu.addSeparator()
        menu.addAction(self.click_through_action)
        menu.addAction(self.stealth_action)
        menu.addSeparator()
        for label, callback in extra_actions or []:
            action = QAction(label, menu)
            action.triggered.connect(callback)
            menu.addAction(action)
        if extra_actions:
            menu.addSeparator()
        menu.addAction(self.stop_action)
        menu.addAction(quit_action)

        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)

    def show(self) -> bool:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.info("No system tray available")
            return False
        self.tray.show()
        return True

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visibility()

    def _toggle_visibility(self) -> None:
        if self.overlay.isVisible():
            self.overlay.hide()
            self.show_action.setText("Show overlay")
        else:
            self.overlay.show()
            self.show_action.setText("Hide overlay")

    def close(self) -> None:
        self.tray.hide()
