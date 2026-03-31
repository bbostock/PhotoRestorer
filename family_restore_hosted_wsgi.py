#!/usr/bin/env python3
"""Hosted upload-only WSGI app for cPanel/Passenger deployments."""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote

from family_restore_server import (
    JOB_LOCK,
    PREVIEW_DIR,
    ROOT,
    AutoProcessState,
    RefusalError,
    build_restore_request,
    coerce_int,
    compare_cache_path,
    default_config,
    ensure_runtime_dirs,
    ensure_session_dirs,
    process_restore_job,
    rotate_image_file,
    save_uploaded_image,
    scan_source_images,
    session_paths,
)


SESSION_AUTO_STATES: dict[str, AutoProcessState] = {}
SESSION_AUTO_LOCK = threading.Lock()


def app_path(script_name: str, suffix: str) -> str:
    prefix = script_name.rstrip("/")
    return f"{prefix}{suffix}" if prefix else suffix


def build_file_url(script_name: str, path: Path) -> str:
    return f"{app_path(script_name, '/api/file')}?path={quote(str(path))}"


def route_path(path: str) -> str:
    if not path or path == "/":
        return "/"
    if path.endswith("/family_restore_gui.html"):
        return "/family_restore_gui.html"
    api_index = path.find("/api/")
    if api_index >= 0:
        return path[api_index:]
    return path


def get_or_create_session(environ: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    script_name = environ.get("SCRIPT_NAME", "")
    cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
    existing = cookie.get("photo_restorer_session")
    headers: list[tuple[str, str]] = []
    if existing and existing.value:
        session_id = existing.value
    else:
        session_id = secrets.token_urlsafe(18)
        cookie_path = script_name.rstrip("/") or "/"
        headers.append(("Set-Cookie", f"photo_restorer_session={session_id}; Path={cookie_path}; SameSite=Lax"))
    ensure_session_dirs(session_id)
    return session_id, headers


def json_response(start_response: Any, payload: dict[str, Any], status: str = "200 OK", extra_headers: list[tuple[str, str]] | None = None) -> list[bytes]:
    body = json.dumps(payload).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def bytes_response(start_response: Any, body: bytes, content_type: str, status: str = "200 OK", extra_headers: list[tuple[str, str]] | None = None) -> list[bytes]:
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def read_json_body(environ: dict[str, Any]) -> dict[str, Any]:
    length = int(environ.get("CONTENT_LENGTH") or "0")
    raw = environ["wsgi.input"].read(length) if length > 0 else b"{}"
    return json.loads(raw.decode("utf-8"))


def hosted_references(session_id: str) -> list[Path]:
    ref_dir = ensure_session_dirs(session_id)["references"]
    return sorted([path for path in ref_dir.iterdir() if path.is_file()], key=lambda path: path.name.lower())


def hosted_config(session_id: str, script_name: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    config = default_config()
    raw = raw or {}
    config.update(
        {
            "prompt_text": str(raw.get("prompt_text", config["prompt_text"])).strip() or config["prompt_text"],
            "extra_note": str(raw.get("extra_note", "")).strip(),
            "colorize": bool(raw.get("colorize", config["colorize"])),
            "overwrite_existing": bool(raw.get("overwrite_existing", False)),
            "auto_pause_seconds": coerce_int(raw.get("auto_pause_seconds"), config["auto_pause_seconds"], minimum=0, maximum=600),
            "auto_include_restored": bool(raw.get("auto_include_restored", False)),
            "selected_folder": "",
            "app_mode": "hosted",
        }
    )
    refs = hosted_references(session_id)
    config["reference_image"] = str(refs[0]) if len(refs) > 0 else ""
    config["reference_image_2"] = str(refs[1]) if len(refs) > 1 else ""
    config["reference_preview_url"] = build_file_url(script_name, refs[0]) if len(refs) > 0 else None
    config["reference_preview_url_2"] = build_file_url(script_name, refs[1]) if len(refs) > 1 else None
    return config


def rewrite_image_row(script_name: str, row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    source_path = Path(payload["source_path"])
    payload["source_url"] = build_file_url(script_name, source_path)
    latest_restore_path = payload.get("latest_restore_path")
    if latest_restore_path:
        restored_path = Path(latest_restore_path)
        payload["latest_restore_url"] = build_file_url(script_name, restored_path)
        compare_path = compare_cache_path(source_path, restored_path)
        payload["compare_url"] = build_file_url(script_name, compare_path) if compare_path.exists() else None
    else:
        payload["latest_restore_url"] = None
        payload["compare_url"] = None
    return payload


def hosted_images(session_id: str, script_name: str) -> list[dict[str, Any]]:
    folder = ensure_session_dirs(session_id)["targets"]
    return [rewrite_image_row(script_name, row) for row in scan_source_images(str(folder))]


def session_file_allowed(session_id: str, path: Path, editable: bool = False) -> bool:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        return False
    session_base = session_paths(session_id)["base"].resolve()
    if editable:
        return (session_base == resolved.parent or session_base in resolved.parents) and resolved.exists() and resolved.is_file()
    preview_root = PREVIEW_DIR.resolve()
    return (
        session_base == resolved
        or session_base in resolved.parents
        or preview_root == resolved
        or preview_root in resolved.parents
    )


def get_session_auto_state(session_id: str) -> AutoProcessState:
    with SESSION_AUTO_LOCK:
        state = SESSION_AUTO_STATES.get(session_id)
        if state is None:
            state = AutoProcessState()
            SESSION_AUTO_STATES[session_id] = state
        return state


def rewrite_restore_payload(script_name: str, request: Any, payload: dict[str, Any]) -> dict[str, Any]:
    source_path = Path(request.selected_folder) / request.filename
    restored_path = Path(payload["latest_restore_path"])
    compare_path = compare_cache_path(source_path, restored_path)
    updated = dict(payload)
    updated["source_url"] = build_file_url(script_name, source_path)
    updated["restored_color_url"] = build_file_url(script_name, restored_path)
    updated["compare_url"] = build_file_url(script_name, compare_path) if compare_path.exists() else None
    return updated


def hosted_auto_worker(session_id: str, config: dict[str, Any], filenames: list[str], script_name: str) -> None:
    state = get_session_auto_state(session_id)
    request_template = {
        "selected_folder": config["selected_folder"],
        "prompt_text": config["prompt_text"],
        "reference_image": config["reference_image"],
        "reference_image_2": config["reference_image_2"],
        "extra_note": config["extra_note"],
        "colorize": config["colorize"],
        "overwrite_existing": config["overwrite_existing"],
        "api_key": str(config.get("api_key", "")).strip(),
    }
    try:
        for filename in filenames:
            if state.should_stop():
                break
            state.update_current(filename)
            request = build_restore_request({**request_template, "filename": filename})
            with JOB_LOCK:
                try:
                    payload = process_restore_job(request)
                    payload = rewrite_restore_payload(script_name, request, payload)
                except Exception as exc:
                    state.mark_error(str(exc))
                    break
            state.mark_success(payload)
            pause_seconds = config["auto_pause_seconds"]
            if pause_seconds <= 0:
                continue
            for _ in range(pause_seconds):
                if state.should_stop():
                    break
                time.sleep(1)
            if state.should_stop():
                break
    finally:
        state.finish()


def start_hosted_auto_process(session_id: str, script_name: str, body: dict[str, Any]) -> dict[str, Any]:
    state = get_session_auto_state(session_id)
    if state.snapshot()["running"]:
        raise RuntimeError("Automatic processing is already running for this session")
    config = hosted_config(session_id, script_name, body)
    config.update(
        {
            "selected_folder": str(ensure_session_dirs(session_id)["targets"]),
            "reference_image": str(body.get("reference_image", config["reference_image"])).strip(),
            "reference_image_2": str(body.get("reference_image_2", config["reference_image_2"])).strip(),
            "api_key": str(body.get("api_key", "")).strip(),
        }
    )
    if config["auto_pause_seconds"] <= 0:
        raise ValueError("Automatic processing requires a pause greater than 0 seconds")
    images = scan_source_images(config["selected_folder"])
    if not config["auto_include_restored"]:
        images = [image for image in images if not image["restored"]]
    if not images:
        raise ValueError("No uploaded target photos are available for automatic processing")
    filenames = [image["filename"] for image in images]
    state.start(total=len(filenames), pause_seconds=config["auto_pause_seconds"], include_restored=config["auto_include_restored"])
    thread = threading.Thread(target=hosted_auto_worker, args=(session_id, config, filenames, script_name), daemon=True)
    thread.start()
    return state.snapshot()


def application(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    ensure_runtime_dirs()
    script_name = environ.get("SCRIPT_NAME", "")
    path_info = environ.get("PATH_INFO", "") or "/"
    routed_path = route_path(path_info)
    session_id, session_headers = get_or_create_session(environ)
    query = parse_qs(environ.get("QUERY_STRING", ""))

    try:
        if environ["REQUEST_METHOD"] == "GET":
            if routed_path in {"", "/", "/family_restore_gui.html"}:
                return bytes_response(
                    start_response,
                    (ROOT / "family_restore_gui.html").read_bytes(),
                    "text/html; charset=utf-8",
                    extra_headers=session_headers,
                )
            if routed_path == "/api/app-info":
                return json_response(start_response, {"ok": True, "app_mode": "hosted", "session_id": session_id}, extra_headers=session_headers)
            if routed_path == "/api/config":
                return json_response(start_response, {"ok": True, **hosted_config(session_id, script_name)}, extra_headers=session_headers)
            if routed_path == "/api/images":
                return json_response(start_response, {"ok": True, "images": hosted_images(session_id, script_name)}, extra_headers=session_headers)
            if routed_path == "/api/process-status":
                return json_response(start_response, {"ok": True, **get_session_auto_state(session_id).snapshot()}, extra_headers=session_headers)
            if routed_path == "/api/file":
                path_text = query.get("path", [""])[0]
                if not path_text:
                    return json_response(start_response, {"ok": False, "error": "Missing file path"}, "400 Bad Request", session_headers)
                path = Path(unquote(path_text)).expanduser()
                if not session_file_allowed(session_id, path, editable=False):
                    return json_response(start_response, {"ok": False, "error": "File access is not allowed"}, "403 Forbidden", session_headers)
                if not path.exists() or not path.is_file():
                    return json_response(start_response, {"ok": False, "error": "File not found"}, "404 Not Found", session_headers)
                mime_type, _ = mimetypes.guess_type(path.name)
                return bytes_response(
                    start_response,
                    path.read_bytes(),
                    mime_type or "application/octet-stream",
                    extra_headers=[("Cache-Control", "no-store"), *session_headers],
                )
            return json_response(start_response, {"ok": False, "error": "Unknown endpoint"}, "404 Not Found", session_headers)

        if environ["REQUEST_METHOD"] == "POST":
            body = read_json_body(environ)
            if routed_path == "/api/config":
                return json_response(start_response, {"ok": True, **hosted_config(session_id, script_name, body)}, extra_headers=session_headers)
            if routed_path == "/api/reference-upload":
                ref_dir = ensure_session_dirs(session_id)["references"]
                payload = save_uploaded_image(str(body.get("filename", "")).strip(), str(body.get("data_url", "")).strip(), ref_dir)
                payload["url"] = build_file_url(script_name, Path(payload["path"]))
                return json_response(start_response, {"ok": True, **payload}, extra_headers=session_headers)
            if routed_path == "/api/target-upload":
                target_dir = ensure_session_dirs(session_id)["targets"]
                payload = save_uploaded_image(str(body.get("filename", "")).strip(), str(body.get("data_url", "")).strip(), target_dir)
                payload["url"] = build_file_url(script_name, Path(payload["path"]))
                return json_response(start_response, {"ok": True, **payload}, extra_headers=session_headers)
            if routed_path == "/api/restore":
                if get_session_auto_state(session_id).snapshot()["running"]:
                    return json_response(start_response, {"ok": False, "error": "Automatic processing is already running for this session"}, "409 Conflict", session_headers)
                body["selected_folder"] = str(ensure_session_dirs(session_id)["targets"])
                request = build_restore_request(body)
                if not JOB_LOCK.acquire(blocking=False):
                    return json_response(start_response, {"ok": False, "error": "Another restore job is already running"}, "409 Conflict", session_headers)
                try:
                    payload = process_restore_job(request)
                    payload = rewrite_restore_payload(script_name, request, payload)
                finally:
                    JOB_LOCK.release()
                return json_response(start_response, {"ok": True, **payload}, extra_headers=session_headers)
            if routed_path == "/api/process-folder":
                payload = start_hosted_auto_process(session_id, script_name, body)
                return json_response(start_response, {"ok": True, **payload}, extra_headers=session_headers)
            if routed_path == "/api/process-stop":
                state = get_session_auto_state(session_id)
                state.request_stop()
                return json_response(start_response, {"ok": True, **state.snapshot()}, extra_headers=session_headers)
            if routed_path == "/api/rotate-save":
                path_text = str(body.get("path", "")).strip()
                clockwise_degrees = int(body.get("clockwise_degrees", 0))
                if not path_text:
                    raise ValueError("Missing image path")
                path = Path(path_text).expanduser()
                if not session_file_allowed(session_id, path, editable=True):
                    raise ValueError("That file cannot be modified")
                compare_path = rotate_image_file(path, clockwise_degrees)
                return json_response(
                    start_response,
                    {
                        "ok": True,
                        "path": str(path.resolve()),
                        "file_url": build_file_url(script_name, path.resolve()),
                        "compare_url": build_file_url(script_name, compare_path) if compare_path else None,
                    },
                    extra_headers=session_headers,
                )
            return json_response(start_response, {"ok": False, "error": "Unknown endpoint"}, "404 Not Found", session_headers)
    except json.JSONDecodeError:
        return json_response(start_response, {"ok": False, "error": "Invalid JSON"}, "400 Bad Request", session_headers)
    except RefusalError as exc:
        return json_response(start_response, {"ok": False, "error": str(exc), "refusal": True}, "422 Unprocessable Entity", session_headers)
    except FileNotFoundError as exc:
        return json_response(start_response, {"ok": False, "error": str(exc)}, "404 Not Found", session_headers)
    except ValueError as exc:
        return json_response(start_response, {"ok": False, "error": str(exc)}, "400 Bad Request", session_headers)
    except RuntimeError as exc:
        return json_response(start_response, {"ok": False, "error": str(exc)}, "409 Conflict", session_headers)
    except Exception as exc:
        return json_response(start_response, {"ok": False, "error": str(exc)}, "500 Internal Server Error", session_headers)

    return json_response(start_response, {"ok": False, "error": "Method not allowed"}, "405 Method Not Allowed", session_headers)
