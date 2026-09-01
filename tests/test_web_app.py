from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from autocomplete_system.analytics import AdminService, AnalyticsStore, RebuildManager
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.models import RankingMode
from autocomplete_system.storage import load_ranking_mode_setting, save_index
from web_app import create_request_handler, create_server


class WebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.root = root
        corpus = root / "Archive"
        corpus.mkdir()
        (corpus / "example.txt").write_text(
            "Hello, world!\nThis is a demo.\n",
            encoding="utf-8",
        )
        index, master = build_index(corpus)
        self.system = AutocompleteSystem(
            index,
            master,
            ranking_mode=RankingMode.POPULARITY,
        )
        self.analytics = AnalyticsStore(root / "web-data")
        logs = root / "logs"
        logs.mkdir()
        (logs / "system.jsonl").write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "level": "INFO",
                        "logger": "autocomplete.engine",
                        "event": "search_completed",
                        "message": "search completed",
                        "details": {"query": "demo"},
                    },
                    {
                        "timestamp": "2026-01-01T00:00:01+00:00",
                        "level": "ERROR",
                        "logger": "autocomplete.web",
                        "event": "web_search_failed",
                        "message": "search failed",
                        "details": {"query": "broken"},
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.admin_service = AdminService(self.system, self.analytics, root)
        self.server = create_server(
            self.system,
            port=0,
            analytics=self.analytics,
            admin_service=self.admin_service,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.system.close()
        self.temporary_directory.cleanup()

    def read_json(self, path: str) -> dict[str, object]:
        with urlopen(self.base_url + path, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def assert_raw_post_error(
        self,
        path: str,
        body: bytes,
        expected_status: HTTPStatus,
    ) -> None:
        request = Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, expected_status)

    def test_home_page_and_static_assets_are_served(self) -> None:
        with urlopen(self.base_url + "/", timeout=2) as response:
            html = response.read().decode("utf-8")
        self.assertIn("PUG Search", html)
        self.assertIn("Start typing to search", html)
        self.assertIn("Full sentence selected", html)
        self.assertIn('id="voice-button"', html)

        with urlopen(self.base_url + "/styles.css", timeout=2) as response:
            css = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("text/css", response.headers["Content-Type"])
            self.assertIn("overflow-wrap: anywhere", css)

        with urlopen(self.base_url + "/app.js", timeout=2) as response:
            javascript = response.read().decode("utf-8")
            self.assertIn("window.webkitSpeechRecognition", javascript)
            self.assertEqual(response.headers["Permissions-Policy"], "microphone=(self)")

    def test_missing_static_asset_is_reported_as_server_error(self) -> None:
        with patch("web_app.WEB_DIRECTORY", self.root / "missing-web-directory"):
            with self.assertRaises(HTTPError) as context:
                urlopen(self.base_url + "/styles.css", timeout=2)
        self.assertEqual(context.exception.code, HTTPStatus.INTERNAL_SERVER_ERROR)

    def test_suggestions_endpoint_returns_ids_and_required_fields(self) -> None:
        payload = self.read_json("/api/suggestions?query=hello")
        suggestion = payload["suggestions"][0]
        self.assertEqual(suggestion["sentence_id"], 0)
        self.assertEqual(suggestion["completed_sentence"], "Hello, world!")
        self.assertEqual(suggestion["source_text"], "example.txt")
        self.assertEqual(suggestion["offset"], 1)
        self.assertEqual(suggestion["score"], 10)

    def test_click_equivalent_records_exact_selected_sentence(self) -> None:
        request = Request(
            self.base_url + "/api/select",
            data=json.dumps({"sentence_id": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload["selected"]["completed_sentence"], "This is a demo.")
        self.assertEqual(payload["selected"]["usage_count"], 1)
        self.assertEqual(self.system.master_array[1].usage_count, 1)

    def test_selection_endpoint_persists_usage_and_voice_metadata(self) -> None:
        data_directory = self.root / "selection-data"
        save_index(data_directory, self.system.index, self.system.master_array)
        self.system.data_directory = data_directory

        selected = self.post_json(
            "/api/select",
            {"sentence_id": 1, "query": "demo", "input_method": "voice"},
        )
        self.assertEqual(selected["selected"]["usage_count"], 1)
        reloaded = AutocompleteSystem.load(data_directory, RankingMode.POPULARITY)
        try:
            self.assertEqual(reloaded.master_array[1].usage_count, 1)
        finally:
            reloaded.close()
        events, _ = self.analytics.read_events()
        selection = next(event for event in events if event["event_type"] == "selection")
        self.assertEqual(selection["input_method"], "voice")
        self.assertEqual(selection["query"], "demo")

    def test_invalid_selection_is_rejected(self) -> None:
        request = Request(
            self.base_url + "/api/select",
            data=json.dumps({"sentence_id": "not-an-integer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 400)

    def test_unknown_selection_and_unknown_routes_return_not_found(self) -> None:
        with self.assertRaises(HTTPError) as selection_error:
            self.post_json("/api/select", {"sentence_id": 99})
        self.assertEqual(selection_error.exception.code, HTTPStatus.NOT_FOUND)

        with self.assertRaises(HTTPError) as get_error:
            urlopen(self.base_url + "/does-not-exist", timeout=2)
        self.assertEqual(get_error.exception.code, HTTPStatus.NOT_FOUND)

        with self.assertRaises(HTTPError) as post_error:
            self.post_json("/api/does-not-exist", {})
        self.assertEqual(post_error.exception.code, HTTPStatus.NOT_FOUND)

    def test_invalid_json_bodies_and_client_events_are_rejected(self) -> None:
        invalid_json = Request(
            self.base_url + "/api/events",
            data=b"{",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as json_error:
            urlopen(invalid_json, timeout=2)
        self.assertEqual(json_error.exception.code, HTTPStatus.BAD_REQUEST)

        json_array = Request(
            self.base_url + "/api/events",
            data=b"[]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as type_error:
            urlopen(json_array, timeout=2)
        self.assertEqual(type_error.exception.code, HTTPStatus.BAD_REQUEST)

        with self.assertRaises(HTTPError) as event_error:
            self.post_json(
                "/api/events",
                {"event_type": "unsupported", "details": {}},
            )
        self.assertEqual(event_error.exception.code, HTTPStatus.BAD_REQUEST)

        empty_body = Request(
            self.base_url + "/api/events",
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as empty_error:
            urlopen(empty_body, timeout=2)
        self.assertEqual(empty_error.exception.code, HTTPStatus.BAD_REQUEST)

    def test_every_json_admin_action_rejects_malformed_bodies(self) -> None:
        for path in (
            "/api/select",
            "/api/admin/settings/popularity",
            "/api/admin/actions/reset-analytics",
            "/api/admin/actions/reset-popularity",
            "/api/admin/actions/rebuild-index",
        ):
            with self.subTest(path=path):
                self.assert_raw_post_error(path, b"{", HTTPStatus.BAD_REQUEST)

    def test_invalid_content_length_is_rejected(self) -> None:
        host, port = self.server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=2)
        try:
            connection.putrequest("POST", "/api/events")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "not-a-number")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, HTTPStatus.BAD_REQUEST)
            self.assertIn("Invalid Content-Length", response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_admin_dashboard_records_complete_search_analytics(self) -> None:
        self.read_json("/api/suggestions?query=hello&input_method=voice")
        dashboard = self.read_json("/api/admin/dashboard")
        self.assertEqual(dashboard["corpus"]["total_sentences"], 2)
        self.assertEqual(dashboard["analytics"]["searches"]["total"], 1)
        self.assertEqual(dashboard["analytics"]["searches"]["voice"], 1)
        self.assertEqual(
            dashboard["analytics"]["top_queries"][0]["normalized_query"],
            "hello",
        )

        page = self.read_json("/api/admin/sentences?offset=0&limit=1")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["records"][0]["original_text"], "Hello, world!")

    def test_invalid_input_method_falls_back_to_typed(self) -> None:
        self.read_json("/api/suggestions?query=hello&input_method=unknown")
        dashboard = self.read_json("/api/admin/dashboard")
        self.assertEqual(dashboard["analytics"]["searches"]["typed"], 1)
        self.assertEqual(dashboard["analytics"]["searches"]["voice"], 0)

    def test_search_failures_return_500_and_are_recorded(self) -> None:
        with patch.object(
            AutocompleteSystem,
            "get_ranked_completions",
            side_effect=RuntimeError("simulated search failure"),
        ):
            with self.assertRaises(HTTPError) as context:
                urlopen(self.base_url + "/api/suggestions?query=demo", timeout=2)
        self.assertEqual(context.exception.code, HTTPStatus.INTERNAL_SERVER_ERROR)
        events, _ = self.analytics.read_events()
        error = next(event for event in events if event["event_type"] == "error")
        self.assertEqual(error["operation"], "search")
        self.assertEqual(error["error_type"], "RuntimeError")

    def test_admin_popularity_uses_persisted_usage_after_restart(self) -> None:
        data_directory = self.root / "persistent-data"
        save_index(data_directory, self.system.index, self.system.master_array)

        first_process = AutocompleteSystem.load(
            data_directory,
            ranking_mode=RankingMode.POPULARITY,
        )
        first_process.record_selection(1)
        first_process.save_usage_stats()
        first_process.close()

        restarted_system = AutocompleteSystem.load(
            data_directory,
            ranking_mode=RankingMode.POPULARITY,
        )
        try:
            empty_analytics = AnalyticsStore(self.root / "fresh-analytics")
            dashboard = AdminService(
                restarted_system,
                empty_analytics,
                self.root,
            ).dashboard()

            self.assertEqual(dashboard["analytics"]["selections"], 0)
            popularity = dashboard["corpus"]["popularity"]
            self.assertEqual(popularity["total_usage"], 1)
            self.assertEqual(popularity["sentences_with_usage"], 1)
            self.assertEqual(popularity["top_sentences"][0]["sentence_id"], 1)
            self.assertEqual(popularity["top_sentences"][0]["usage_count"], 1)
        finally:
            restarted_system.close()

    def test_admin_can_disable_and_enable_popularity_weighting(self) -> None:
        self.post_json("/api/select", {"sentence_id": 1, "query": "demo"})
        weighted = self.read_json("/api/suggestions?query=demo")
        self.assertEqual(weighted["suggestions"][0]["score"], 13)

        disabled = self.post_json(
            "/api/admin/settings/popularity",
            {"enabled": False},
        )
        self.assertFalse(disabled["popularity_enabled"])
        unweighted = self.read_json("/api/suggestions?query=demo")
        self.assertEqual(unweighted["suggestions"][0]["score"], 8)
        dashboard = self.read_json("/api/admin/dashboard")
        self.assertFalse(dashboard["configuration"]["popularity_enabled"])
        self.assertEqual(dashboard["corpus"]["popularity"]["total_usage"], 1)

        enabled = self.post_json(
            "/api/admin/settings/popularity",
            {"enabled": True},
        )
        self.assertTrue(enabled["popularity_enabled"])
        weighted_again = self.read_json("/api/suggestions?query=demo")
        self.assertEqual(weighted_again["suggestions"][0]["score"], 13)

    def test_admin_toggle_does_not_change_candidates_without_usage_bonus(self) -> None:
        enabled = self.read_json("/api/suggestions?query=demo")
        enabled_ids = [item["sentence_id"] for item in enabled["suggestions"]]

        self.post_json("/api/admin/settings/popularity", {"enabled": False})
        disabled = self.read_json("/api/suggestions?query=demo")
        disabled_ids = [item["sentence_id"] for item in disabled["suggestions"]]

        self.assertEqual(disabled_ids, enabled_ids)

    def test_admin_popularity_setting_is_persisted(self) -> None:
        data_directory = self.root / "ranking-data"
        save_index(data_directory, self.system.index, self.system.master_array)
        persisted_system = AutocompleteSystem.load(
            data_directory,
            ranking_mode=RankingMode.POPULARITY,
        )
        try:
            service = AdminService(
                persisted_system,
                AnalyticsStore(self.root / "ranking-analytics"),
                self.root,
            )
            service.set_popularity_enabled(False)
        finally:
            persisted_system.close()

        self.assertEqual(
            load_ranking_mode_setting(data_directory, RankingMode.POPULARITY),
            RankingMode.ASSIGNMENT,
        )

    def test_admin_popularity_setting_requires_boolean(self) -> None:
        request = Request(
            self.base_url + "/api/admin/settings/popularity",
            data=json.dumps({"enabled": "false"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 400)

    def test_admin_page_and_exports_are_available(self) -> None:
        self.read_json("/api/suggestions?query=demo")
        with urlopen(self.base_url + "/admin", timeout=2) as response:
            html = response.read().decode("utf-8")
        self.assertIn("Autocomplete overview", html)
        self.assertIn("Administrative actions", html)
        self.assertIn("Live system logs", html)
        self.assertIn('id="toggle-popularity"', html)
        self.assertIn("Popularity ranking", html)

        with urlopen(self.base_url + "/api/admin/export?format=json", timeout=2) as response:
            exported = json.loads(response.read().decode("utf-8"))
            self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(exported["events"][0]["event_type"], "search")

        with urlopen(self.base_url + "/api/admin/export?format=csv", timeout=2) as response:
            csv_export = response.read().decode("utf-8-sig")
            self.assertIn("text/csv", response.headers["Content-Type"])
            self.assertIn("event_type", csv_export)

        with self.assertRaises(HTTPError) as invalid_export:
            urlopen(self.base_url + "/api/admin/export?format=xml", timeout=2)
        self.assertEqual(invalid_export.exception.code, HTTPStatus.BAD_REQUEST)

    def test_admin_pagination_and_log_query_validation(self) -> None:
        for path in (
            "/api/admin/sentences?offset=bad&limit=10",
            "/api/admin/sentences?offset=-1&limit=10",
            "/api/admin/sentences?offset=0&limit=101",
            "/api/admin/logs?limit=bad",
            "/api/admin/logs?limit=0",
            "/api/admin/logs?limit=501",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as context:
                urlopen(self.base_url + path, timeout=2)
            self.assertEqual(context.exception.code, HTTPStatus.BAD_REQUEST)

    def test_admin_is_unavailable_when_services_are_not_configured(self) -> None:
        server = create_server(self.system, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            for path in (
                "/api/admin/dashboard",
                "/api/admin/sentences",
                "/api/admin/export?format=json",
                "/api/admin/log-files",
                "/api/admin/logs",
                "/api/admin/logs/download",
            ):
                with self.subTest(path=path), self.assertRaises(HTTPError) as context:
                    urlopen(base_url + path, timeout=2)
                self.assertEqual(context.exception.code, HTTPStatus.SERVICE_UNAVAILABLE)

            for path, payload in (
                ("/api/admin/settings/popularity", {"enabled": False}),
                (
                    "/api/admin/actions/reset-analytics",
                    {"confirmation": "RESET ANALYTICS"},
                ),
                (
                    "/api/admin/actions/reset-popularity",
                    {"confirmation": "RESET POPULARITY"},
                ),
                (
                    "/api/admin/actions/rebuild-index",
                    {"confirmation": "REBUILD INDEX"},
                ),
            ):
                request = Request(
                    base_url + path,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.subTest(path=path), self.assertRaises(HTTPError) as context:
                    urlopen(request, timeout=2)
                self.assertEqual(context.exception.code, HTTPStatus.SERVICE_UNAVAILABLE)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_admin_log_viewer_filters_downloads_and_blocks_unknown_paths(self) -> None:
        files = self.read_json("/api/admin/log-files")
        self.assertEqual(files["files"][0]["filename"], "system.jsonl")

        logs = self.read_json("/api/admin/logs?file=system.jsonl&level=ERROR&limit=50")
        self.assertEqual(logs["record_count"], 1)
        self.assertEqual(logs["records"][0]["event"], "web_search_failed")
        self.assertEqual(logs["records"][0]["details"]["query"], "broken")

        with urlopen(
            self.base_url + "/api/admin/logs/download?file=system.jsonl",
            timeout=2,
        ) as response:
            body = response.read().decode("utf-8")
            self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("search_completed", body)

        with self.assertRaises(HTTPError) as context:
            urlopen(
                self.base_url + "/api/admin/logs?file=../README.md&limit=50",
                timeout=2,
            )
        self.assertEqual(context.exception.code, 404)

        with self.assertRaises(HTTPError) as missing_download:
            urlopen(
                self.base_url + "/api/admin/logs/download?file=system.jsonl.5",
                timeout=2,
            )
        self.assertEqual(missing_download.exception.code, HTTPStatus.NOT_FOUND)

    def test_admin_rejects_remote_and_invalid_client_addresses(self) -> None:
        handler_type = create_request_handler(
            self.system,
            self.analytics,
            self.admin_service,
        )
        for address in ("203.0.113.10", "not-an-ip"):
            with self.subTest(address=address):
                handler = object.__new__(handler_type)
                handler.client_address = (address, 12345)
                handler._send_json = Mock()

                self.assertFalse(handler._require_admin())
                handler._send_json.assert_called_once_with(
                    HTTPStatus.FORBIDDEN,
                    {"error": "Administration is available only from this computer."},
                )

    def test_admin_resets_require_confirmation_and_reset_popularity(self) -> None:
        self.post_json("/api/select", {"sentence_id": 0, "query": "hello"})
        request = Request(
            self.base_url + "/api/admin/actions/reset-popularity",
            data=json.dumps({"confirmation": "wrong"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 400)
        self.assertEqual(self.system.master_array[0].usage_count, 1)

        result = self.post_json(
            "/api/admin/actions/reset-popularity",
            {"confirmation": "RESET POPULARITY"},
        )
        self.assertIn("reset", result["message"].lower())
        self.assertEqual(self.system.master_array[0].usage_count, 0)

    def test_admin_reset_analytics_keeps_only_the_audit_event(self) -> None:
        self.read_json("/api/suggestions?query=hello")
        result = self.post_json(
            "/api/admin/actions/reset-analytics",
            {"confirmation": "RESET ANALYTICS"},
        )
        self.assertIn("reset", result["message"].lower())
        events, malformed = self.analytics.read_events()
        self.assertEqual(malformed, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "admin_action")
        self.assertEqual(events[0]["action"], "reset_analytics")

    def test_admin_rebuild_reports_missing_configured_archive(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self.post_json(
                "/api/admin/actions/rebuild-index",
                {"confirmation": "REBUILD INDEX"},
            )
        self.assertEqual(context.exception.code, HTTPStatus.CONFLICT)

    def test_admin_rebuild_success_is_audited(self) -> None:
        rebuild = {"state": "running", "pid": 123, "target_directory": "replacement"}
        with patch.object(self.admin_service, "start_rebuild", return_value=rebuild):
            result = self.post_json(
                "/api/admin/actions/rebuild-index",
                {"confirmation": "REBUILD INDEX"},
            )
        self.assertEqual(result["rebuild"], rebuild)
        events, _ = self.analytics.read_events()
        action = next(
            event
            for event in events
            if event.get("action") == "start_replacement_index_build"
        )
        self.assertEqual(action["target_directory"], "replacement")

    def test_client_voice_events_are_persisted(self) -> None:
        self.post_json(
            "/api/events",
            {"event_type": "voice_error", "details": {"error": "no-speech"}},
        )
        events, malformed = self.analytics.read_events()
        self.assertEqual(malformed, 0)
        self.assertEqual(events[0]["event_type"], "client_event")
        self.assertEqual(events[0]["client_event_type"], "voice_error")


class RebuildManagerTests(unittest.TestCase):
    def test_replacement_build_is_non_destructive_and_reports_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            (source / "tiny.txt").write_text("A tiny sentence.\n", encoding="utf-8")
            manager = RebuildManager(Path(__file__).resolve().parent.parent, source, root / "rebuilds")

            started = manager.start()
            self.assertEqual(started["state"], "running")
            deadline = __import__("time").monotonic() + 10
            status = manager.status()
            while status["state"] == "running" and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.05)
                status = manager.status()

            self.assertEqual(status["state"], "completed", status["log_tail"])
            target = Path(status["target_directory"])
            self.assertTrue((target / "index.pkl").is_file())
            self.assertTrue((target / "sentences.pkl").is_file())


if __name__ == "__main__":
    unittest.main()
