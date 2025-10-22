#!/usr/bin/env python3
"""Qt GUI front-end for goyang_client.py."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QDateTime, QProcess, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStatusBar,
    QTextEdit,
    QWidget,
)


COURT_INFO: Dict[str, Dict[str, object]] = {
    "성사야외": {
        "cvalue": "5",
        "codes": {idx: 16 + idx for idx in range(1, 9)},  # 1 -> 17 ... 8 -> 24
    },
    "충장": {
        "cvalue": "7",
        "codes": {1: 28, 2: 29, 3: 30, 4: 31},
    },
}

PAYMENT_RATES = {
    "weekday": {"day": 8000, "night": 10000},
    "weekend": {"day": 10000, "night": 13000},
}

SLOT_OPTIONS = [
    ("06:00 ~ 08:00", 6),
    ("08:00 ~ 10:00", 8),
    ("10:00 ~ 12:00", 10),
    ("12:00 ~ 14:00", 12),
    ("14:00 ~ 16:00", 14),
    ("16:00 ~ 18:00", 16),
    ("18:00 ~ 20:00", 18),
    ("20:00 ~ 22:00", 20),
]

SLOT_MAP = dict(SLOT_OPTIONS)


class ReservationWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("고양시 테니스 코트 자동 예약")
        self.resize(640, 520)

        self._scheduled_timer: Optional[QTimer] = None
        self._process: Optional[QProcess] = None

        central = QWidget(self)
        central_layout = QGridLayout(central)
        central_layout.setColumnStretch(0, 1)
        central_layout.setRowStretch(1, 1)
        self.setCentralWidget(central)

        form_group = QGroupBox("예약 정보 입력", central)
        form_layout = QFormLayout(form_group)
        form_group.setLayout(form_layout)

        self.user_id_edit = QLineEdit()
        form_layout.addRow("아이디", self.user_id_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        form_layout.addRow("비밀번호", self.password_edit)

        self.reservation_date_edit = QDateEdit()
        self.reservation_date_edit.setCalendarPopup(True)
        self.reservation_date_edit.setDate(QDate.currentDate())
        form_layout.addRow("예약 날짜", self.reservation_date_edit)

        self.court_combo = QComboBox()
        self.court_combo.addItems(COURT_INFO.keys())
        self.court_combo.currentTextChanged.connect(self.update_court_number_range)
        form_layout.addRow("코트", self.court_combo)

        self.court_number_spin = QSpinBox()
        self.court_number_spin.setMinimum(1)
        self.court_number_spin.setMaximum(8)
        form_layout.addRow("코트 번호", self.court_number_spin)

        self.timeslot_combo = QComboBox()
        for label, _ in SLOT_OPTIONS:
            self.timeslot_combo.addItem(label)
        form_layout.addRow("예약 시간 (2시간)", self.timeslot_combo)

        citizen_box = QHBoxLayout()
        self.citizen_check = QCheckBox("고양시민")
        self.citizen_check.setChecked(True)
        self.senior_check = QCheckBox("고령자")
        citizen_box.addWidget(self.citizen_check)
        citizen_box.addWidget(self.senior_check)
        form_layout.addRow("할인 대상", citizen_box)

        self.schedule_datetime_edit = QDateTimeEdit()
        self.schedule_datetime_edit.setCalendarPopup(True)
        self.schedule_datetime_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        now = QDateTime.currentDateTime()
        self.schedule_datetime_edit.setDateTime(now)
        form_layout.addRow("실행 시각 (KST)", self.schedule_datetime_edit)

        central_layout.addWidget(form_group, 0, 0)

        button_layout = QHBoxLayout()
        self.schedule_button = QPushButton("예약 스케줄")
        self.schedule_button.clicked.connect(self.schedule_reservation)
        button_layout.addWidget(self.schedule_button)

        self.cancel_button = QPushButton("예약 취소")
        self.cancel_button.clicked.connect(self.cancel_schedule)
        self.cancel_button.setEnabled(False)
        button_layout.addWidget(self.cancel_button)

        central_layout.addLayout(button_layout, 2, 0)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        central_layout.addWidget(self.log_view, 1, 0)

        self.statusbar = QStatusBar(self)
        self.setStatusBar(self.statusbar)

        menu = self.menuBar().addMenu("도움말")
        about_action = QAction("정보", self)
        about_action.triggered.connect(self.show_about)
        menu.addAction(about_action)

        self.update_court_number_range(self.court_combo.currentText())

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "정보",
            "고양시 테니스 코트 자동 예약 GUI\nPySide6 기반\n\n"
            "예약 시각이 되면 내부적으로 goyang_client.py를 실행합니다.",
        )

    def schedule_reservation(self) -> None:
        if self._scheduled_timer:
            QMessageBox.warning(self, "경고", "이미 예약 작업이 대기 중입니다.")
            return

        user_id = self.user_id_edit.text().strip()
        password = self.password_edit.text()
        if not user_id or not password:
            QMessageBox.warning(self, "입력 오류", "아이디와 비밀번호를 모두 입력하세요.")
            return

        court_name = self.court_combo.currentText()
        court_info = COURT_INFO.get(court_name, {})
        cvalue = court_info.get("cvalue")
        court_codes: Dict[int, int] = court_info.get("codes", {})

        court_number = self.court_number_spin.value()
        if court_number not in court_codes:
            QMessageBox.warning(self, "입력 오류", f"{court_name}에는 {court_number}번 코트가 존재하지 않습니다.")
            return
        court_code = court_codes[court_number]

        cdate_q = self.reservation_date_edit.date()
        cdate = cdate_q.toString("yyyy-MM-dd")
        slot_label = self.timeslot_combo.currentText()
        slot_number = SLOT_MAP[slot_label]
        is_night = slot_number >= 18

        schedule_dt = self.schedule_datetime_edit.dateTime().toPython().replace(tzinfo=ZoneInfo("Asia/Seoul"))
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        delay_seconds = max(0, (schedule_dt - now).total_seconds())

        payment_amount = self.calculate_payment_amount(cdate_q, is_night)
        if self.citizen_check.isChecked() or self.senior_check.isChecked():
            payment_amount //= 2

        good_name = f"{court_name} {court_number}번 예약"
        reserve_slot_parts = [
            cdate,
            cvalue,
            str(court_code),
            str(slot_number),
            str(payment_amount),
        ]

        client_args = [
            "--login-userid",
            user_id,
            "--login-password",
            password,
            "--reserve-cvalue",
            cvalue,
            "--reserve-date",
            cdate,
            "--reserve-slot",
            *reserve_slot_parts,
            "--payment-good-name",
            good_name,
            "--payment-buyer-name",
            user_id,
            "--payment-amount",
            str(payment_amount),
        ]

        if getattr(sys, "frozen", False):
            program = sys.executable
            process_args = ["--client", *client_args]
            command_preview = " ".join([program, *process_args])
        else:
            program = sys.executable
            script_path = str(Path(__file__).resolve())
            process_args = [script_path, "--client", *client_args]
            command_preview = " ".join([program, *process_args])

        self.log(f"예약 스케줄 설정: {schedule_dt.astimezone(ZoneInfo('Asia/Seoul'))}")
        self.log(f"명령어: {command_preview}")

        self._scheduled_timer = QTimer(self)
        self._scheduled_timer.setSingleShot(True)
        self._scheduled_timer.timeout.connect(lambda: self.run_process(program, process_args))
        self._scheduled_timer.start(int(delay_seconds * 1000))

        self.schedule_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.statusbar.showMessage("예약 실행 대기 중...", 5000)

    def cancel_schedule(self) -> None:
        if self._scheduled_timer:
            self._scheduled_timer.stop()
            self._scheduled_timer = None
            self.log("예약 실행이 취소되었습니다.")
        self.schedule_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.statusbar.clearMessage()

    def run_process(self, program: str, arguments: list[str]) -> None:
        self._scheduled_timer = None
        self.schedule_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

        self._process = QProcess(self)
        self._process.setProgram(program)
        self._process.setArguments(arguments)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read_process_output)
        self._process.finished.connect(self._process_finished)
        self._process.start()

        if not self._process.waitForStarted(5000):
            QMessageBox.critical(self, "실행 실패", "goyang_client.py 실행을 시작하지 못했습니다.")
            self._process = None
            return

        self.log("goyang_client.py 실행을 시작했습니다.")

    def _read_process_output(self) -> None:
        if not self._process:
            return
        data = self._process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self.log(data.rstrip())

    def _process_finished(self) -> None:
        if not self._process:
            return
        exit_code = self._process.exitCode()
        self.log(f"goyang_client.py 종료 (exit code: {exit_code})")
        self._process = None

    def log(self, message: str) -> None:
        timestamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
        self.log_view.append(f"[{timestamp}] {message}")

    @staticmethod
    def calculate_payment_amount(reservation_date: QDate, is_night: bool) -> int:
        target_date = date(reservation_date.year(), reservation_date.month(), reservation_date.day())
        weekday_index = target_date.weekday()  # Monday=0
        period_key = "weekend" if weekday_index >= 5 else "weekday"
        time_key = "night" if is_night else "day"
        return PAYMENT_RATES[period_key][time_key]

    def update_court_number_range(self, court_name: str) -> None:
        court_info = COURT_INFO.get(court_name, {})
        codes: Dict[int, int] = court_info.get("codes", {})
        if codes:
            numbers = sorted(codes.keys())
            self.court_number_spin.setMinimum(numbers[0])
            self.court_number_spin.setMaximum(numbers[-1])
            if self.court_number_spin.value() not in numbers:
                self.court_number_spin.setValue(numbers[0])


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--client", action="store_true")
    args, rest = parser.parse_known_args(argv)

    if args.client:
        from goyang_client import main as client_main

        return client_main(rest)

    app = QApplication(sys.argv)
    window = ReservationWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
