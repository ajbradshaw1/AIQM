"""
Application entry point — QApplication setup and main().
"""

import sys
from PyQt6.QtWidgets import QApplication
from gui.app_icon import install_application_icon
from gui.heater_control.main_window import MainWindow


def main():
    resource = None
    if len(sys.argv) > 1:
        port = sys.argv[1]
        resource = f"ASRL{port}::INSTR"
        print(f"Using specified port: {port}")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    install_application_icon(app)

    window = MainWindow(resource)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
