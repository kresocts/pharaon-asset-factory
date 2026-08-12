from __future__ import annotations

import unittest
from unittest.mock import patch

from validation import run_ci


class RunCiTests(unittest.TestCase):
    def test_default_runs_all_stages_in_order(self) -> None:
        with patch.object(run_ci, "run_stage", return_value=0) as run_stage:
            result = run_ci.main([])

        self.assertEqual(result, 0)
        self.assertEqual([call.args[0] for call in run_stage.call_args_list], ["tests", "metadata"])

    def test_stage_option_runs_only_selected_stage(self) -> None:
        with patch.object(run_ci, "run_stage", return_value=0) as run_stage:
            result = run_ci.main(["--stage", "metadata"])

        self.assertEqual(result, 0)
        run_stage.assert_called_once_with("metadata")

    def test_failure_stops_later_stages_and_propagates_exit_code(self) -> None:
        with patch.object(run_ci, "run_stage", return_value=7) as run_stage:
            result = run_ci.main([])

        self.assertEqual(result, 7)
        run_stage.assert_called_once_with("tests")

    def test_stage_commands_use_current_python(self) -> None:
        tests_command = run_ci.STAGES["tests"][1]
        metadata_command = run_ci.STAGES["metadata"][1]

        self.assertEqual(tests_command[0], run_ci.sys.executable)
        self.assertEqual(metadata_command[0], run_ci.sys.executable)


if __name__ == "__main__":
    unittest.main()
