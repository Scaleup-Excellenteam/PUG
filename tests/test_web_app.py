from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from autocomplete_system.analytics import AdminService, AnalyticsStore, RebuildManager
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.indexer import build_index
from autocomplete_system.models import RankingMode
from web_app import create_server


class WebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
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
        self.admin_service = AdminService(self.system, self.analytics, Path.cwd())
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

    def test_admin_page_and_exports_are_available(self) -> None:
        self.read_json("/api/suggestions?query=demo")
        with urlopen(self.base_url + "/admin", timeout=2) as response:
            html = response.read().decode("utf-8")
        self.assertIn("Autocomplete overview", html)
        self.assertIn("Administrative actions", html)

        with urlopen(self.base_url + "/api/admin/export?format=json", timeout=2) as response:
            exported = json.loads(response.read().decode("utf-8"))
            self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(exported["events"][0]["event_type"], "search")

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
            manager = RebuildManager(Path.cwd(), source, root / "rebuilds")

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
