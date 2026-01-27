#!/usr/bin/env python3
"""관리자용: 월별 접속 코드 생성기 (GUI)"""

import hashlib
import hmac
import sys
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

APP_SECRET = "a4d6fef01e194c9b81a7c6151d447e0f"


def generate_code(year_month: str) -> str:
    """6자리 접속 코드 생성"""
    full_hash = hmac.new(
        APP_SECRET.encode("utf-8"),
        year_month.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return full_hash[:6]


class CodeGeneratorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("접속 코드 생성기 (관리자용)")
        self.setFixedSize(300, 150)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        self.setCentralWidget(central)

        form = QFormLayout()
        layout.addLayout(form)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("예: 202601")
        self.input_edit.setText(datetime.now().strftime("%Y%m"))
        self.input_edit.returnPressed.connect(self.generate)
        form.addRow("년월 (YYYYMM)", self.input_edit)

        self.generate_btn = QPushButton("코드 생성")
        self.generate_btn.clicked.connect(self.generate)
        layout.addWidget(self.generate_btn)

        self.result_label = QLabel("")
        self.result_label.setStyleSheet("font-size: 24px; font-weight: bold; color: #2563eb;")
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.result_label)

        self.generate()

    def generate(self) -> None:
        year_month = self.input_edit.text().strip()
        if len(year_month) != 6 or not year_month.isdigit():
            self.result_label.setText("YYYYMM 형식으로 입력")
            self.result_label.setStyleSheet("font-size: 14px; color: red;")
            return

        code = generate_code(year_month)
        self.result_label.setText(code)
        self.result_label.setStyleSheet("font-size: 24px; font-weight: bold; color: #2563eb;")


def main() -> int:
    app = QApplication(sys.argv)
    window = CodeGeneratorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
