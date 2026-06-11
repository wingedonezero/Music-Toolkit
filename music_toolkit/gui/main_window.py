# music_toolkit/gui/main_window.py

from PySide6 import QtWidgets, QtGui
from music_toolkit.core.managers import AppManager
from music_toolkit.tools.flac_verifier.flac_verifier_gui import FlacVerifierWidget
from music_toolkit.tools.dr_meter.dr_meter_gui import DrMeterWidget
from music_toolkit.tools.authenticity_checker.authenticity_checker_gui import AuthenticityCheckerWidget

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music-Toolkit")
        self.resize(1400, 900)
        self.app_manager = AppManager()
        self.open_tools = {}
        self.tab_widget = QtWidgets.QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tab_widget)
        self._create_actions()
        self._create_menus()

    def _create_actions(self):
        self.open_flac_verifier_action = QtGui.QAction("FLAC Verifier", self)
        self.open_flac_verifier_action.triggered.connect(self.open_flac_verifier)

        self.open_dr_meter_action = QtGui.QAction("DR Meter", self)
        self.open_dr_meter_action.triggered.connect(self.open_dr_meter)

        self.open_authenticity_checker_action = QtGui.QAction("Authenticity Checker", self)
        self.open_authenticity_checker_action.triggered.connect(self.open_authenticity_checker)

    def _create_menus(self):
        menu_bar = self.menuBar()
        tools_menu = menu_bar.addMenu("&Tools")
        tools_menu.addAction(self.open_flac_verifier_action)
        tools_menu.addAction(self.open_dr_meter_action)
        tools_menu.addAction(self.open_authenticity_checker_action)

    def open_flac_verifier(self): self._open_tool("FlacVerifier", "FLAC Verifier", FlacVerifierWidget)
    def open_dr_meter(self): self._open_tool("DrMeter", "DR Meter", DrMeterWidget)
    def open_authenticity_checker(self): self._open_tool(
        "AuthenticityChecker", "Authenticity Checker", AuthenticityCheckerWidget
    )

    def _open_tool(self, tool_name, tab_title, widget_class):
        if tool_name in self.open_tools:
            self.tab_widget.setCurrentWidget(self.open_tools[tool_name])
            return

        tool_widget = widget_class(app_manager=self.app_manager)
        index = self.tab_widget.addTab(tool_widget, tab_title)
        self.tab_widget.setCurrentIndex(index)
        self.open_tools[tool_name] = tool_widget

    def _close_tab(self, index: int):
        widget_to_close = self.tab_widget.widget(index)
        if not widget_to_close: return

        tool_name_to_remove = next((name for name, widget in self.open_tools.items() if widget == widget_to_close), None)

        try:
            if hasattr(widget_to_close, 'save_settings'):
                print(f"Saving settings for {tool_name_to_remove}...")
                widget_to_close.save_settings()
        except Exception as e:
            print(f"Error saving settings for {tool_name_to_remove}: {e}")

        try:
            if hasattr(widget_to_close, 'shutdown'):
                print(f"Closing tab for {tool_name_to_remove}. Shutting down worker...")
                widget_to_close.shutdown()
        except Exception as e:
            print(f"Error shutting down {tool_name_to_remove}: {e}")

        self.tab_widget.removeTab(index)
        if tool_name_to_remove:
            del self.open_tools[tool_name_to_remove]
        widget_to_close.deleteLater()

    def closeEvent(self, event: QtGui.QCloseEvent):
        print("Main window is closing. Saving all settings and shutting down threads...")
        # Close all tabs properly - iterate in reverse to avoid index shifting
        while self.tab_widget.count() > 0:
            widget = self.tab_widget.widget(0)
            tool_name = next((name for name, w in self.open_tools.items() if w == widget), None)
            try:
                if hasattr(widget, 'save_settings'):
                    widget.save_settings()
            except Exception as e:
                print(f"Error saving settings for {tool_name}: {e}")
            try:
                if hasattr(widget, 'shutdown'):
                    widget.shutdown()
            except Exception as e:
                print(f"Error shutting down {tool_name}: {e}")
            self.tab_widget.removeTab(0)
            if tool_name:
                self.open_tools.pop(tool_name, None)
            widget.deleteLater()
        self.open_tools.clear()
        event.accept()
