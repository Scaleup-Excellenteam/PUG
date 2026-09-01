"""Local standard-library web interface for the autocomplete system."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from autocomplete_system.analytics import AdminService, AnalyticsStore
from autocomplete_system.constants import DEFAULT_DATA_DIRECTORY
from autocomplete_system.engine import AutocompleteSystem
from autocomplete_system.logging_config import configure_system_logging, log_event
from autocomplete_system.models import AutoCompleteData, RankingMode
from autocomplete_system.normalization import normalize_text
from autocomplete_system.storage import load_ranking_mode_setting


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_REQUEST_BODY_BYTES = 16 * 1024
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
    "/admin": "admin.html",
    "/admin/": "admin.html",
    "/admin.html": "admin.html",
    "/admin.css": "admin.css",
    "/admin.js": "admin.js",
}
LOGGER = logging.getLogger("autocomplete.web")


def _completion_payload(
    sentence_id: int, completion: AutoCompleteData
) -> dict[str, str | int]:
    return {
        "sentence_id": sentence_id,
        "completed_sentence": completion.completed_sentence,
        "source_text": completion.source_text,
        "offset": completion.offset,
        "score": completion.score,
    }


def create_request_handler(
    system: AutocompleteSystem,
    analytics: AnalyticsStore | None = None,
    admin_service: AdminService | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler bound to one loaded autocomplete system."""

    class AutocompleteRequestHandler(BaseHTTPRequestHandler):
        server_version = "AutocompleteWeb/1.0"

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            request = urlsplit(self.path)
            if request.path == "/api/suggestions":
                parameters = parse_qs(request.query)
                self._serve_suggestions(
                    parameters.get("query", [""])[0],
                    parameters.get("input_method", ["typed"])[0],
                )
                return
            if request.path == "/api/admin/dashboard":
                self._serve_admin_dashboard()
                return
            if request.path == "/api/admin/sentences":
                self._serve_admin_sentences(parse_qs(request.query))
                return
            if request.path == "/api/admin/log-files":
                self._serve_admin_log_files()
                return
            if request.path == "/api/admin/logs":
                self._serve_admin_logs(parse_qs(request.query))
                return
            if request.path == "/api/admin/logs/download":
                self._serve_admin_log_download(parse_qs(request.query))
                return
            if request.path == "/api/admin/export":
                self._serve_admin_export(parse_qs(request.query).get("format", ["json"])[0])
                return

            static_name = STATIC_FILES.get(request.path)
            if static_name is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "The requested resource was not found."},
                )
                return
            self._serve_static_file(static_name)

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            request_path = urlsplit(self.path).path
            if request_path == "/api/select":
                self._record_selection()
                return
            if request_path == "/api/events":
                self._record_client_event()
                return
            if request_path == "/api/admin/settings/popularity":
                self._set_popularity_enabled()
                return
            admin_actions = {
                "/api/admin/actions/reset-analytics": self._reset_analytics,
                "/api/admin/actions/reset-popularity": self._reset_popularity,
                "/api/admin/actions/rebuild-index": self._start_rebuild,
            }
            action = admin_actions.get(request_path)
            if action is not None:
                action()
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "The requested resource was not found."},
            )

        def _serve_suggestions(self, query: str, input_method: str) -> None:
            if input_method not in {"typed", "voice"}:
                input_method = "typed"
            started = time.perf_counter()
            try:
                ranked = system.get_ranked_completions(query)
                payload = [
                    _completion_payload(sentence_id, completion)
                    for sentence_id, completion in ranked
                ]
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                if analytics is not None:
                    analytics.record(
                        "search",
                        query=query,
                        normalized_query=normalize_text(query),
                        input_method=input_method,
                        duration_ms=duration_ms,
                        result_count=len(payload),
                        results=payload,
                        result_sentence_ids=[item["sentence_id"] for item in payload],
                        result_scores=[item["score"] for item in payload],
                        **self._request_metadata(),
                    )
                self._send_json(
                    HTTPStatus.OK,
                    {"suggestions": payload, "duration_ms": duration_ms},
                )
            except Exception as error:
                log_event(
                    LOGGER,
                    "web_search_failed",
                    level=logging.ERROR,
                    query=query,
                    input_method=input_method,
                    error_type=type(error).__name__,
                    error_message=str(error),
                    exc_info=True,
                    **self._request_metadata(),
                )
                if analytics is not None:
                    analytics.record(
                        "error",
                        operation="search",
                        query=query,
                        error_type=type(error).__name__,
                        error_message=str(error),
                        **self._request_metadata(),
                    )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "The search could not be completed."},
                )

        def _record_selection(self) -> None:
            payload = self._read_json_body()
            if payload is None:
                return

            sentence_id = payload.get("sentence_id") if isinstance(payload, dict) else None
            if not isinstance(sentence_id, int) or isinstance(sentence_id, bool):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "sentence_id must be an integer."},
                )
                return

            try:
                system.record_selection(sentence_id)
            except IndexError:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "The selected sentence does not exist."},
                )
                return

            if system.data_directory is not None:
                system.save_usage_stats()

            record = system.master_array[sentence_id]
            if admin_service is not None:
                admin_service.note_selection(sentence_id)
            if analytics is not None:
                analytics.record(
                    "selection",
                    sentence_id=sentence_id,
                    completed_sentence=record.original_text,
                    source_text=record.source_path,
                    offset=record.line_number,
                    usage_count=record.usage_count,
                    query=str(payload.get("query", "")),
                    input_method=(
                        payload.get("input_method")
                        if payload.get("input_method") in {"typed", "voice"}
                        else "typed"
                    ),
                    **self._request_metadata(),
                )
            self._send_json(
                HTTPStatus.OK,
                {
                    "selected": {
                        "sentence_id": sentence_id,
                        "completed_sentence": record.original_text,
                        "source_text": record.source_path,
                        "offset": record.line_number,
                        "usage_count": record.usage_count,
                    }
                },
            )

        def _record_client_event(self) -> None:
            payload = self._read_json_body()
            if payload is None:
                return
            allowed_event_types = {
                "page_view",
                "voice_start",
                "voice_result",
                "voice_error",
                "voice_end",
            }
            event_type = payload.get("event_type")
            details = payload.get("details", {})
            if event_type not in allowed_event_types or not isinstance(details, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Unsupported client event."},
                )
                return
            if analytics is not None:
                analytics.record(
                    "client_event",
                    client_event_type=event_type,
                    details=details,
                    **self._request_metadata(),
                )
            log_event(
                LOGGER,
                "client_event_received",
                client_event_type=event_type,
                client_details=details,
                **self._request_metadata(),
            )
            self._send_json(HTTPStatus.ACCEPTED, {"recorded": True})

        def _serve_admin_dashboard(self) -> None:
            if not self._require_admin():
                return
            assert admin_service is not None
            self._send_json(HTTPStatus.OK, admin_service.dashboard())

        def _serve_admin_sentences(self, parameters: dict[str, list[str]]) -> None:
            if not self._require_admin():
                return
            try:
                offset = int(parameters.get("offset", ["0"])[0])
                limit = int(parameters.get("limit", ["25"])[0])
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "offset and limit must be integers."},
                )
                return
            if offset < 0 or limit < 1 or limit > 100:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "offset must be non-negative and limit must be 1-100."},
                )
                return
            assert admin_service is not None
            self._send_json(
                HTTPStatus.OK,
                admin_service.sentences_page(offset, limit),
            )

        def _serve_admin_export(self, export_format: str) -> None:
            if not self._require_admin():
                return
            assert analytics is not None
            if export_format == "json":
                self._send_download(
                    analytics.export_json(),
                    "application/json; charset=utf-8",
                    "autocomplete-analytics.json",
                )
            elif export_format == "csv":
                self._send_download(
                    analytics.export_csv(),
                    "text/csv; charset=utf-8",
                    "autocomplete-analytics.csv",
                )
            else:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Export format must be json or csv."},
                )

        def _serve_admin_log_files(self) -> None:
            if not self._require_admin():
                return
            assert admin_service is not None
            self._send_json(HTTPStatus.OK, {"files": admin_service.log_files()})

        def _serve_admin_logs(self, parameters: dict[str, list[str]]) -> None:
            if not self._require_admin():
                return
            try:
                limit = int(parameters.get("limit", ["200"])[0])
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "limit must be an integer."},
                )
                return
            if limit < 1 or limit > 500:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "limit must be between 1 and 500."},
                )
                return
            assert admin_service is not None
            try:
                payload = admin_service.read_system_logs(
                    parameters.get("file", ["system.jsonl"])[0],
                    limit,
                    parameters.get("level", [""])[0],
                    parameters.get("component", [""])[0],
                    parameters.get("search", [""])[0],
                )
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, payload)

        def _serve_admin_log_download(
            self,
            parameters: dict[str, list[str]],
        ) -> None:
            if not self._require_admin():
                return
            assert admin_service is not None
            filename = parameters.get("file", ["system.jsonl"])[0]
            try:
                path = admin_service.resolve_log_file(filename)
                body = path.read_bytes()
            except (ValueError, FileNotFoundError) as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            self._send_download(
                body,
                "application/x-ndjson; charset=utf-8",
                filename,
            )

        def _reset_analytics(self) -> None:
            payload = self._confirmed_admin_payload("RESET ANALYTICS")
            if payload is None:
                return
            assert admin_service is not None and analytics is not None
            admin_service.reset_analytics()
            analytics.record(
                "admin_action",
                action="reset_analytics",
                **self._request_metadata(),
            )
            log_event(
                LOGGER,
                "admin_action_completed",
                action="reset_analytics",
                **self._request_metadata(),
            )
            self._send_json(HTTPStatus.OK, {"message": "Analytics history was reset."})

        def _reset_popularity(self) -> None:
            payload = self._confirmed_admin_payload("RESET POPULARITY")
            if payload is None:
                return
            assert admin_service is not None and analytics is not None
            admin_service.reset_popularity()
            analytics.record(
                "admin_action",
                action="reset_popularity",
                **self._request_metadata(),
            )
            log_event(
                LOGGER,
                "admin_action_completed",
                action="reset_popularity",
                **self._request_metadata(),
            )
            self._send_json(HTTPStatus.OK, {"message": "Popularity data was reset."})

        def _set_popularity_enabled(self) -> None:
            if not self._require_admin():
                return
            payload = self._read_json_body()
            if payload is None:
                return
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "enabled must be a boolean."},
                )
                return

            assert admin_service is not None and analytics is not None
            ranking_mode = admin_service.set_popularity_enabled(enabled)
            analytics.record(
                "admin_action",
                action="set_popularity_enabled",
                enabled=enabled,
                ranking_mode=ranking_mode.value,
                **self._request_metadata(),
            )
            log_event(
                LOGGER,
                "admin_action_completed",
                action="set_popularity_enabled",
                enabled=enabled,
                ranking_mode=ranking_mode.value,
                **self._request_metadata(),
            )
            state = "enabled" if enabled else "disabled"
            self._send_json(
                HTTPStatus.OK,
                {
                    "message": f"Popularity weighting is now {state}.",
                    "popularity_enabled": enabled,
                    "ranking_mode": ranking_mode.value,
                },
            )

        def _start_rebuild(self) -> None:
            payload = self._confirmed_admin_payload("REBUILD INDEX")
            if payload is None:
                return
            assert admin_service is not None and analytics is not None
            try:
                rebuild = admin_service.start_rebuild()
            except (FileNotFoundError, RuntimeError) as error:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            analytics.record(
                "admin_action",
                action="start_replacement_index_build",
                target_directory=rebuild.get("target_directory"),
                **self._request_metadata(),
            )
            log_event(
                LOGGER,
                "admin_action_started",
                action="start_replacement_index_build",
                rebuild=rebuild,
                **self._request_metadata(),
            )
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "message": "A non-destructive replacement-index build was started.",
                    "rebuild": rebuild,
                },
            )

        def _confirmed_admin_payload(self, confirmation: str) -> dict[str, Any] | None:
            if not self._require_admin():
                return None
            payload = self._read_json_body()
            if payload is None:
                return None
            if payload.get("confirmation") != confirmation:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f'Type "{confirmation}" to confirm this action.'},
                )
                return None
            return payload

        def _require_admin(self) -> bool:
            try:
                is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                is_loopback = False
            if not is_loopback:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "Administration is available only from this computer."},
                )
                return False
            if admin_service is not None and analytics is not None:
                return True
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "Administrative analytics are not configured."},
            )
            return False

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Invalid Content-Length header."},
                )
                return None
            if content_length <= 0 or content_length > MAX_REQUEST_BODY_BYTES:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "The request body is missing or too large."},
                )
                return None
            try:
                payload: Any = json.loads(
                    self.rfile.read(content_length).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "The request body must be valid UTF-8 JSON."},
                )
                return None
            if not isinstance(payload, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "The JSON request body must be an object."},
                )
                return None
            return payload

        def _request_metadata(self) -> dict[str, object]:
            return {
                "client_address": self.client_address[0],
                "user_agent": self.headers.get("User-Agent", ""),
            }

        def _serve_static_file(self, filename: str) -> None:
            path = WEB_DIRECTORY / filename
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "A required web asset is missing."},
                )
                return

            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self._send_security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: HTTPStatus, payload: object) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self._send_security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_download(
            self,
            body: bytes,
            content_type: str,
            filename: str,
        ) -> None:
            self.send_response(HTTPStatus.OK)
            self._send_security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "microphone=(self)")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )

        def log_message(self, format: str, *args: object) -> None:
            """Keep the local terminal focused on lifecycle messages."""

        def log_request(
            self,
            code: int | str = "-",
            size: int | str = "-",
        ) -> None:
            log_event(
                LOGGER,
                "http_request_completed",
                method=self.command,
                path=self.path,
                status_code=code,
                response_size=size,
                **self._request_metadata(),
            )

    return AutocompleteRequestHandler


def create_server(
    system: AutocompleteSystem,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    analytics: AnalyticsStore | None = None,
    admin_service: AdminService | None = None,
) -> HTTPServer:
    """Create the single-user local HTTP server without starting it."""

    return HTTPServer(
        (host, port),
        create_request_handler(system, analytics, admin_service),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local autocomplete website.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIRECTORY,
        help="Directory containing the serialized index (default: data).",
    )
    parser.add_argument(
        "--mode",
        type=RankingMode,
        choices=list(RankingMode),
        default=None,
        help=(
            "Override the saved web ranking mode for this run. Without this option, "
            "the saved Admin preference is used, defaulting to popularity."
        ),
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def run_server(
    system: AutocompleteSystem,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    announce: Callable[[str], None] = print,
) -> None:
    """Serve until Ctrl+C, then save mutable usage data and close cleanly."""

    project_directory = Path(__file__).resolve().parent
    analytics = AnalyticsStore(system.data_directory or DEFAULT_DATA_DIRECTORY)
    admin_service = AdminService(system, analytics, project_directory)
    analytics.record(
        "server_start",
        host=host,
        port=port,
        ranking_mode=system.ranking_mode.value,
        index_backend=type(system.index).__name__,
    )
    log_event(
        LOGGER,
        "web_server_started",
        host=host,
        port=port,
        ranking_mode=system.ranking_mode.value,
        index_backend=type(system.index).__name__,
    )
    server = create_server(system, host, port, analytics, admin_service)
    display_host = "localhost" if host in {"127.0.0.1", "localhost"} else host
    announce(f"Autocomplete website is ready at http://{display_host}:{server.server_port}")
    announce("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        analytics.record("server_stop")
        log_event(LOGGER, "web_server_stopped", host=host, port=port)
        if system.data_directory is not None:
            system.save_usage_stats()
        system.close()


def main() -> None:
    configure_system_logging()
    args = parse_args()
    ranking_mode = args.mode or load_ranking_mode_setting(
        args.data_dir,
        RankingMode.POPULARITY,
    )
    system = AutocompleteSystem.load(args.data_dir, ranking_mode)
    run_server(system, args.host, args.port)


if __name__ == "__main__":
    main()
