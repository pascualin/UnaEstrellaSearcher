from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts import config_ui


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.terminate_called = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True

    def communicate(self):
        return self.stdout, self.stderr


class RunControlTests(unittest.TestCase):
    def setUp(self) -> None:
        with config_ui._active_run_lock:
            config_ui._active_run.update(
                {"process": None, "mode": "", "cancel_requested": False}
            )

    def tearDown(self) -> None:
        with config_ui._active_run_lock:
            process = config_ui._active_run.get("process")
            if process is not None and process.poll() is None:
                process.terminate()
            config_ui._active_run.update(
                {"process": None, "mode": "", "cancel_requested": False}
            )

    def test_stop_marks_the_active_run_and_terminates_its_process(self) -> None:
        process = FakeProcess()
        with config_ui._active_run_lock:
            config_ui._active_run.update(
                {"process": process, "mode": "episode", "cancel_requested": False}
            )

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "progress.log"
            stopped = config_ui._stop_active_run(log_path)
            event = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertTrue(stopped)
        self.assertTrue(process.terminate_called)
        self.assertEqual(event, {"event": "run_cancel_requested", "mode": "episode"})
        self.assertEqual(
            config_ui._active_run_status(),
            {"active": True, "mode": "episode", "stopping": True},
        )

    def test_monitor_reports_cancellation_without_a_failure(self) -> None:
        process = FakeProcess(returncode=-15, stderr="terminated")
        with config_ui._active_run_lock:
            config_ui._active_run.update(
                {"process": process, "mode": "weekly", "cancel_requested": True}
            )

        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "progress.log"
            config_ui._monitor_run_process(process, "weekly", log_path)
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(events[-1], {"event": "run_cancelled", "mode": "weekly"})
        self.assertFalse(any(event["event"] == "run_failed" for event in events))
        self.assertEqual(
            config_ui._active_run_status(),
            {"active": False, "mode": "", "stopping": False},
        )

    def test_start_is_rejected_while_another_run_is_active(self) -> None:
        process = FakeProcess()
        with config_ui._active_run_lock:
            config_ui._active_run.update(
                {"process": process, "mode": "episode", "cancel_requested": False}
            )

        with tempfile.TemporaryDirectory() as directory:
            started = config_ui._start_run_process(
                ["python3", "-m", "humor_reviews.run", "weekly"],
                "weekly",
                Path(directory) / "progress.log",
            )

        self.assertFalse(started)

    def test_real_background_process_can_be_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "progress.log"
            started = config_ui._start_run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                "episode",
                log_path,
            )
            self.assertTrue(started)
            self.assertTrue(config_ui._active_run_status()["active"])

            self.assertTrue(config_ui._stop_active_run(log_path))
            deadline = time.monotonic() + 3
            events = []
            while time.monotonic() < deadline:
                events = [
                    json.loads(line)
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]
                if any(event["event"] == "run_cancelled" for event in events):
                    break
                time.sleep(0.02)

        self.assertFalse(config_ui._active_run_status()["active"])
        self.assertEqual(events[-1], {"event": "run_cancelled", "mode": "episode"})
        self.assertFalse(any(event["event"] == "run_failed" for event in events))


if __name__ == "__main__":
    unittest.main()
