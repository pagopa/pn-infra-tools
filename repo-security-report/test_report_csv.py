import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from report_csv import ISSUE_COLUMNS, load_csv_report, write_metadata


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reporter = load_script("reporter", "dependabot-report.py")
html = load_script("html_report", "generate-html-report.py")


class CsvReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "2026-08-25-issues.csv"
        self.rows = [
            reporter.RepoReport(
                "example/service", reporter.SeverityCounts(high=1),
                reporter.SeverityCounts(unknown=1), 1, 1,
            ),
            reporter.RepoReport(
                "example/clean", reporter.SeverityCounts(),
                reporter.SeverityCounts(), 0, 0,
            ),
        ]
        self.issues = [
            reporter.SecurityIssue(
                source, "example/service", str(index), severity, "open",
                'Title with comma, quote " and\nnewline', "", "", "", "", "",
            )
            for index, (source, severity) in enumerate([
                ("dependabot", "HIGH"), ("code_scanning", "UNKNOWN"),
                ("secret_scanning", ""), ("renovate", ""),
            ])
        ]
        self.snapshot = reporter.build_report(
            [r.to_dict() for r in self.rows], "2026-08-25T09:30:00Z",
            "dismissed", "custom-renovate",
        )
        self.snapshot.update(report_date="2026-08-25", csv_is_detailed=True)

    def write_csv(self, detailed=True, metadata=True):
        with self.path.open("w", encoding="utf-8", newline="") as out:
            if detailed:
                reporter.render_issues_csv(self.issues, out)
            else:
                reporter.render_csv(self.rows, out)
        if metadata:
            write_metadata(str(self.path), self.snapshot, "2026-08-25")

    def test_both_formats_round_trip(self):
        for detailed in (True, False):
            with self.subTest(detailed=detailed):
                self.write_csv(detailed)
                result = load_csv_report(self.path)
                for key in ("repositories", "totals", "generated_at", "alert_state", "renovate_user", "report_date"):
                    self.assertEqual(result[key], self.snapshot[key])
                self.assertEqual(result["csv_is_detailed"], detailed)

    def test_legacy_formats_warn_and_skip_total(self):
        for detailed in (True, False):
            self.write_csv(detailed, metadata=False)
            with self.assertWarns(UserWarning):
                result = load_csv_report(self.path)
            self.assertEqual(result["totals"]["total"], 4)
            self.assertEqual(len(result["repositories"]), 1 if detailed else 2)
            self.assertEqual(result["report_date"], "2026-08-25")
            self.assertEqual(result["alert_state"], "unknown")

    def test_empty_detailed_preserves_clean_repository(self):
        self.rows = [self.rows[1]]
        self.issues = []
        self.snapshot["repositories"] = [self.rows[0].to_dict()]
        self.write_csv()
        result = load_csv_report(self.path)
        self.assertEqual(result["repositories"], self.snapshot["repositories"])
        self.assertEqual(result["totals"]["total"], 0)

    def test_changed_csv_rejects_stale_sidecar(self):
        self.write_csv()
        with self.path.open("a") as out:
            out.write("\n")
        with self.assertRaisesRegex(ValueError, "does not match metadata"):
            load_csv_report(self.path)

    def test_invalid_csv_is_rejected(self):
        for text in (
            "wrong,header\n",
            ",".join(ISSUE_COLUMNS) + "\ndependabot,example/service\n",
        ):
            self.path.write_text(text)
            with self.assertWarns(UserWarning), self.assertRaises(ValueError):
                load_csv_report(self.path)

    def test_summary_invalid_counts_and_totals(self):
        self.write_csv(False, metadata=False)
        original = self.path.read_text()
        for text in (
            original.replace("example/service,0,1", "example/service,0,-1"),
            original.replace("TOTAL,0,1", "TOTAL,0,2"),
            original.replace("example/clean,0,0", "example/service,0,0"),
        ):
            self.path.write_text(text)
            with self.assertWarns(UserWarning), self.assertRaises(ValueError):
                load_csv_report(self.path)

    def test_invalid_source_and_severity(self):
        for source, severity in (("other", "HIGH"), ("dependabot", "invalid")):
            self.issues[0].source = source
            self.issues[0].severity = severity
            self.write_csv(metadata=False)
            with self.assertWarns(UserWarning), self.assertRaises(ValueError):
                load_csv_report(self.path)

    def test_generate_csv_then_publish_then_html_without_github(self):
        for detailed in (True, False):
            with self.subTest(detailed=detailed):
                args = reporter.parse_args([
                    "--issues-csv", str(self.path),
                ] if detailed else [
                    "-o", "csv", "--out-file", str(self.path),
                ])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(reporter.write_outputs(args, self.rows, self.issues, self.snapshot), 0)
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(reporter, "load_env_file", return_value=None),
                    patch.object(reporter, "_github_get_json", side_effect=AssertionError("GitHub queried")),
                    patch.object(reporter, "publish_report", return_value="https://example/page") as publish,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(reporter.main([
                        "--input-csv", str(self.path), "--confluence",
                        "--confluence-token", "test-token",
                    ]), 0)
                kwargs = publish.call_args.kwargs
                self.assertEqual(kwargs["csv_path"], str(self.path))
                self.assertEqual(kwargs["title"], "2026-08-25 Report di sicurezza Github")
                self.assertIn("example/clean", kwargs["storage_html"])
                self.assertIn("2026-08-25T09:30:00Z", kwargs["storage_html"])
                self.assertIn("ignorati", kwargs["storage_html"])
                if not detailed:
                    self.assertIn("non le singole segnalazioni", kwargs["storage_html"])
                output = self.path.with_suffix(".html")
                with patch.object(sys, "argv", ["generate-html-report.py", "-i", str(self.path), "-o", str(output)]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(html.main(), 0)
                rendered = output.read_text()
                self.assertIn('"repository": "example/clean"', rendered)
                self.assertIn('"total": 4', rendered)
                self.assertNotIn("__REPORT_JSON__", rendered)
                self.assertIn("function renderSourceTabs()", rendered)

    def test_json_html_remains_supported(self):
        source = self.path.with_suffix(".json")
        source.write_text(json.dumps(self.snapshot))
        output = self.path.with_suffix(".html")
        with patch.object(sys, "argv", ["generate-html-report.py", "-i", str(source), "-o", str(output)]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(html.main(), 0)
        self.assertIn('"alert_state": "dismissed"', output.read_text())

    def test_html_includes_github_repository_section_links(self):
        source = self.path.with_suffix(".json")
        source.write_text(json.dumps(self.snapshot))
        output = self.path.with_suffix(".html")
        with patch.object(sys, "argv", ["generate-html-report.py", "-i", str(source), "-o", str(output)]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(html.main(), 0)

        rendered = output.read_text()
        for section in (
            "security/dependabot",
            "security/code-scanning",
            "security/secret-scanning",
            "pulls",
        ):
            self.assertIn(section, rendered)
        self.assertIn("target=\"_blank\" rel=\"noopener noreferrer\"", rendered)
        self.assertIn("encodeURIComponent", rendered)

    def test_offline_json_preserves_snapshot(self):
        self.write_csv()
        output = io.StringIO()
        with patch.object(reporter, "load_env_file", return_value=None), contextlib.redirect_stdout(output):
            self.assertEqual(reporter.main(["--input-csv", str(self.path), "-o", "json"]), 0)
        payload = json.loads(output.getvalue())
        for key in payload:
            self.assertEqual(payload[key], self.snapshot[key])

    def test_offline_table_preserves_snapshot_time(self):
        self.write_csv()
        output = io.StringIO()
        with patch.object(reporter, "load_env_file", return_value=None), contextlib.redirect_stdout(output):
            self.assertEqual(reporter.main(["--input-csv", str(self.path)]), 0)
        self.assertIn("Generated   : 2026-08-25 09:30 UTC", output.getvalue())

    def test_malformed_quoting_returns_error(self):
        self.path.write_text(",".join(ISSUE_COLUMNS) + '\ndependabot,"unterminated\n')
        with (
            patch.object(reporter, "load_env_file", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertWarns(UserWarning),
        ):
            self.assertEqual(reporter.main(["--input-csv", str(self.path)]), 1)

    def test_live_generation_writes_both_reusable_formats(self):
        summary_path = self.path.with_name("summary.csv")
        with (
            patch.dict(os.environ, {"GH_TOKEN": "test-token"}, clear=True),
            patch.object(reporter, "load_env_file", return_value=None),
            patch.object(reporter, "iter_dependabot_alerts", return_value=[
                {"number": 1, "state": "open", "security_advisory": {"severity": "high"}},
            ]) as fetch_dep,
            patch.object(reporter, "iter_code_scanning_alerts", return_value=[]),
            patch.object(reporter, "iter_secret_scanning_alerts", return_value=[]),
            patch.object(reporter, "iter_renovate_prs", return_value=[]),
        ):
            self.assertEqual(reporter.main([
                "example/service", "-o", "csv", "--out-file", str(summary_path),
                "--issues-csv", str(self.path),
            ]), 0)
        fetch_dep.assert_called_once_with("example/service", "open", "test-token")
        summary = load_csv_report(summary_path)
        detailed = load_csv_report(self.path)
        self.assertEqual(summary["repositories"], detailed["repositories"])
        self.assertEqual(summary["totals"]["total"], 1)
        self.assertEqual(summary["generated_at"], detailed["generated_at"])
        self.assertEqual(summary["alert_state"], "open")

    def test_offline_conflicting_arguments_and_overwrite_are_rejected(self):
        self.write_csv()
        original = self.path.read_bytes()
        for flags in (["example/repo"], ["--issues-csv", "other.csv"], ["--out-file", str(self.path)]):
            with patch.object(reporter, "load_env_file", return_value=None), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(reporter.main(["--input-csv", str(self.path), *flags]), 1)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
