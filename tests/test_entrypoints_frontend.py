from __future__ import annotations

import argparse
import io
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

import benchmark
import build_index as build_index_cli
import main as cli
import web_app
from autocomplete_system.build_metrics import read_build_metrics
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.models import RankingMode
from autocomplete_system.storage import load_index, save_index


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.languages: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
        if tag == "html" and attributes.get("lang"):
            self.languages.append(str(attributes["lang"]))


class EntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.corpus = self.root / "Archive"
        self.corpus.mkdir()
        (self.corpus / "sample.txt").write_text(
            "Hello, world!\nThis is a demo.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_all_argument_parsers_accept_documented_options(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["main.py", "--data-dir", "custom", "--mode", "popularity"],
        ):
            cli_args = cli.parse_args()
        self.assertEqual(cli_args.data_dir, Path("custom"))
        self.assertEqual(cli_args.mode, RankingMode.POPULARITY)

        with patch.object(
            sys,
            "argv",
            [
                "build_index.py",
                "--source",
                "one.txt",
                "--source",
                "two.zip",
                "--data-dir",
                "output",
                "--backend",
                "trie",
            ],
        ):
            build_args = build_index_cli.parse_args()
        self.assertEqual(build_args.sources, [Path("one.txt"), Path("two.zip")])
        self.assertEqual(build_args.backend, "trie")

        with patch.object(
            sys,
            "argv",
            ["benchmark.py", "demo", "--repeat", "2", "--mode", "assignment"],
        ):
            benchmark_args = benchmark.parse_args()
        self.assertEqual(benchmark_args.queries, ["demo"])
        self.assertEqual(benchmark_args.repeat, 2)

        with patch.object(
            sys,
            "argv",
            ["web_app.py", "--port", "9000", "--mode", "assignment"],
        ):
            web_args = web_app.parse_args()
        self.assertEqual(web_args.port, 9000)
        self.assertEqual(web_args.mode, RankingMode.ASSIGNMENT)

    def test_offline_cli_builds_loadable_trie_and_sqlite_indexes(self) -> None:
        for backend in ("trie", "sqlite"):
            with self.subTest(backend=backend):
                data_directory = self.root / f"data-{backend}"
                arguments = argparse.Namespace(
                    sources=[self.corpus],
                    data_dir=data_directory,
                    backend=backend,
                )
                output = io.StringIO()
                with (
                    patch.object(build_index_cli, "parse_args", return_value=arguments),
                    patch.object(build_index_cli, "configure_system_logging"),
                    patch.object(build_index_cli, "log_event"),
                    redirect_stdout(output),
                ):
                    build_index_cli.main()

                index, master = load_index(data_directory)
                metrics = read_build_metrics(data_directory)
                self.assertIsNotNone(metrics)
                assert metrics is not None
                self.assertEqual(metrics["sentence_count"], len(master))
                self.assertGreater(metrics["output_bytes"], 0)
                loaded = AutocompleteSystem(index, master, data_directory)
                try:
                    self.assertEqual(
                        loaded.get_best_k_completions("demo")[0].completed_sentence,
                        "This is a demo.",
                    )
                    self.assertIn(f"using {backend}", output.getvalue())
                finally:
                    loaded.close()

    def test_sqlite_offline_cli_prints_the_progress_callback(self) -> None:
        arguments = argparse.Namespace(
            sources=[self.corpus],
            data_dir=self.root / "progress-data",
            backend="sqlite",
        )
        fake_index = Mock()

        def build_with_progress(
            _sources: object,
            _data_directory: Path,
            progress_callback: object,
        ) -> tuple[Mock, list[object]]:
            assert callable(progress_callback)
            progress_callback(100_000)
            return fake_index, []

        output = io.StringIO()
        with (
            patch.object(build_index_cli, "parse_args", return_value=arguments),
            patch.object(build_index_cli, "configure_system_logging"),
            patch.object(
                build_index_cli,
                "build_sqlite_index",
                side_effect=build_with_progress,
            ),
            patch.object(build_index_cli, "save_index") as save,
            patch.object(build_index_cli, "log_event"),
            redirect_stdout(output),
        ):
            build_index_cli.main()

        self.assertIn("Read 100,000 sentences", output.getvalue())
        save.assert_called_once_with(arguments.data_dir, fake_index, [])

    def test_benchmark_main_reports_load_and_query_metrics(self) -> None:
        index, master = build_index(self.corpus)
        data_directory = self.root / "benchmark-data"
        save_index(data_directory, index, master)
        arguments = argparse.Namespace(
            queries=["demo", "not-found"],
            data_dir=data_directory,
            repeat=2,
            mode=RankingMode.ASSIGNMENT,
        )
        output = io.StringIO()
        with (
            patch.object(benchmark, "parse_args", return_value=arguments),
            patch.object(benchmark, "configure_system_logging"),
            redirect_stdout(output),
        ):
            benchmark.main()

        rendered = output.getvalue()
        self.assertIn("load_seconds=", rendered)
        self.assertIn("query='demo'", rendered)
        self.assertIn("results=1", rendered)
        self.assertIn("top='<none>'", rendered)

    def test_benchmark_rejects_nonpositive_repeat(self) -> None:
        arguments = argparse.Namespace(
            queries=["demo"],
            data_dir=self.root / "unused",
            repeat=0,
            mode=RankingMode.ASSIGNMENT,
        )
        with (
            patch.object(benchmark, "parse_args", return_value=arguments),
            patch.object(benchmark, "configure_system_logging"),
            self.assertRaisesRegex(ValueError, "at least 1"),
        ):
            benchmark.main()

    def test_cli_main_loads_configured_system_and_hands_it_to_loop(self) -> None:
        arguments = argparse.Namespace(
            data_dir=self.root / "data",
            mode=RankingMode.POPULARITY,
        )
        fake_system = Mock()
        with (
            patch.object(cli, "parse_args", return_value=arguments),
            patch.object(cli, "configure_system_logging"),
            patch.object(AutocompleteSystem, "load", return_value=fake_system) as load,
            patch.object(cli, "run_cli") as run,
        ):
            cli.main()
        load.assert_called_once_with(arguments.data_dir, RankingMode.POPULARITY)
        run.assert_called_once_with(fake_system)

    def test_cli_handles_keyboard_interrupt_and_hash_without_a_result(self) -> None:
        index, master = build_index(self.corpus)
        system = AutocompleteSystem(index, master)
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["xyz", "#", KeyboardInterrupt]),
            redirect_stdout(output),
        ):
            cli.run_cli(system)
        self.assertEqual([record.usage_count for record in master], [0, 0])
        self.assertGreaterEqual(output.getvalue().count(cli.READY_PROMPT), 2)

    def test_web_main_uses_saved_mode_unless_cli_overrides_it(self) -> None:
        arguments = argparse.Namespace(
            data_dir=self.root / "data",
            mode=None,
            host="127.0.0.1",
            port=0,
        )
        fake_system = Mock()
        with (
            patch.object(web_app, "parse_args", return_value=arguments),
            patch.object(web_app, "configure_system_logging"),
            patch.object(
                web_app,
                "load_ranking_mode_setting",
                return_value=RankingMode.ASSIGNMENT,
            ) as load_setting,
            patch.object(AutocompleteSystem, "load", return_value=fake_system) as load,
            patch.object(web_app, "run_server") as run,
        ):
            web_app.main()
        load_setting.assert_called_once_with(arguments.data_dir, RankingMode.POPULARITY)
        load.assert_called_once_with(arguments.data_dir, RankingMode.ASSIGNMENT)
        run.assert_called_once_with(fake_system, arguments.host, arguments.port)

    def test_web_server_lifecycle_saves_and_closes_cleanly(self) -> None:
        index, master = build_index(self.corpus)
        data_directory = self.root / "web-data"
        save_index(data_directory, index, master)
        system = AutocompleteSystem.load(data_directory, RankingMode.POPULARITY)

        class InterruptingServer:
            server_port = 43210

            def __init__(self) -> None:
                self.closed = False

            @staticmethod
            def serve_forever() -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        fake_server = InterruptingServer()
        announcements: list[str] = []
        with patch.object(web_app, "create_server", return_value=fake_server):
            web_app.run_server(system, port=0, announce=announcements.append)

        self.assertTrue(fake_server.closed)
        self.assertIn("http://localhost:43210", announcements[0])
        self.assertTrue((data_directory / "usage_stats.json").is_file())

    def test_python_entrypoint_guards_execute_their_real_main_functions(self) -> None:
        benchmark_system = Mock()
        benchmark_system.get_best_k_completions.return_value = []
        with (
            patch.object(
                sys,
                "argv",
                ["benchmark.py", "demo", "--repeat", "1"],
            ),
            patch(
                "autocomplete_system.logging_config.configure_system_logging"
            ),
            patch.object(
                AutocompleteSystem,
                "load",
                return_value=benchmark_system,
            ),
            redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(PROJECT_ROOT / "benchmark.py"), run_name="__main__")
        benchmark_system.close.assert_called_once_with()

        fake_index = Mock()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "build_index.py",
                    "--backend",
                    "trie",
                    "--source",
                    str(self.corpus),
                    "--data-dir",
                    str(self.root / "guard-build-data"),
                ],
            ),
            patch(
                "autocomplete_system.logging_config.configure_system_logging"
            ),
            patch("autocomplete_system.logging_config.log_event"),
            patch(
                "autocomplete_system.indexer.build_index",
                return_value=(fake_index, []),
            ),
            patch("autocomplete_system.storage.save_index") as save,
            redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(PROJECT_ROOT / "build_index.py"), run_name="__main__")
        save.assert_called_once()

        cli_system = Mock()
        cli_system.ranking_mode = RankingMode.ASSIGNMENT
        cli_system.index = fake_index
        cli_system.data_directory = None
        with (
            patch.object(
                sys,
                "argv",
                ["main.py", "--data-dir", str(self.root), "--mode", "assignment"],
            ),
            patch("builtins.input", side_effect=EOFError),
            patch(
                "autocomplete_system.logging_config.configure_system_logging"
            ),
            patch.object(AutocompleteSystem, "load", return_value=cli_system),
            redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(PROJECT_ROOT / "main.py"), run_name="__main__")
        cli_system.close.assert_called_once_with()

        class InterruptingHTTPServer:
            server_port = 8765

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            @staticmethod
            def serve_forever() -> None:
                raise KeyboardInterrupt

            @staticmethod
            def server_close() -> None:
                pass

        web_system = Mock()
        web_system.ranking_mode = RankingMode.ASSIGNMENT
        web_system.index = fake_index
        web_system.master_array = []
        web_system.data_directory = self.root / "guard-web-data"
        with (
            patch.object(
                sys,
                "argv",
                ["web_app.py", "--data-dir", str(web_system.data_directory)],
            ),
            patch(
                "autocomplete_system.logging_config.configure_system_logging"
            ),
            patch(
                "autocomplete_system.storage.load_ranking_mode_setting",
                return_value=RankingMode.ASSIGNMENT,
            ),
            patch.object(AutocompleteSystem, "load", return_value=web_system),
            patch("http.server.HTTPServer", InterruptingHTTPServer),
            redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(PROJECT_ROOT / "web_app.py"), run_name="__main__")
        web_system.save_usage_stats.assert_called_once_with()
        web_system.close.assert_called_once_with()


class FrontendContractTests(unittest.TestCase):
    def test_every_literal_javascript_dom_id_exists_in_its_html(self) -> None:
        for html_name, javascript_name in (
            ("index.html", "app.js"),
            ("admin.html", "admin.js"),
        ):
            with self.subTest(javascript=javascript_name):
                html = (PROJECT_ROOT / "web" / html_name).read_text(encoding="utf-8")
                javascript = (PROJECT_ROOT / "web" / javascript_name).read_text(
                    encoding="utf-8"
                )
                parser = _IdCollector()
                parser.feed(html)
                referenced_ids = set(
                    re.findall(r'byId\(["\']([^"\']+)["\']\)', javascript)
                )
                self.assertEqual(referenced_ids - parser.ids, set())
                self.assertEqual(parser.languages, ["en"])

    def test_frontend_api_contracts_are_exposed_by_the_server(self) -> None:
        app_javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        admin_javascript = (PROJECT_ROOT / "web" / "admin.js").read_text(
            encoding="utf-8"
        )
        frontend_markup = "".join(
            (PROJECT_ROOT / "web" / filename).read_text(encoding="utf-8")
            for filename in ("index.html", "admin.html")
        )
        server_source = (PROJECT_ROOT / "web_app.py").read_text(encoding="utf-8")
        required_routes = {
            "/api/suggestions",
            "/api/next_word",
            "/api/select",
            "/api/events",
            "/api/admin/dashboard",
            "/api/admin/sentences",
            "/api/admin/export",
            "/api/admin/log-files",
            "/api/admin/logs",
            "/api/admin/logs/download",
            "/api/admin/settings/popularity",
        }
        combined_frontend = app_javascript + admin_javascript + frontend_markup
        for route in required_routes:
            with self.subTest(route=route):
                self.assertIn(route, combined_frontend)
                self.assertIn(route, server_source)

    def test_ghost_text_is_layered_and_tab_acceptance_is_separate_from_dropdown(self) -> None:
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        css = (PROJECT_ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")

        ghost_position = html.index('id="search-ghost"')
        input_position = html.index('id="search-input"')
        self.assertLess(ghost_position, input_position)
        self.assertIn('disabled tabindex="-1" aria-hidden="true"', html)
        self.assertIn("#search-ghost", css)
        self.assertIn("position: absolute;", css)
        self.assertIn("background: transparent;", css)
        self.assertIn('event.key === "Tab"', javascript)
        self.assertIn('new Event("input", { bubbles: true })', javascript)
        self.assertIn("ghostInput.value.startsWith(input.value)", javascript)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable for JS syntax checks")
    def test_javascript_files_pass_the_runtime_syntax_checker(self) -> None:
        for filename in ("app.js", "admin.js"):
            with self.subTest(filename=filename):
                completed = subprocess.run(
                    [shutil.which("node") or "node", "--check", str(PROJECT_ROOT / "web" / filename)],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
