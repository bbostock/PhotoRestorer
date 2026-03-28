#!/usr/bin/env python3
"""Local historical family photo restoration web app."""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from PIL import Image, ImageOps
except ModuleNotFoundError:  # pragma: no cover - depends on local env
    Image = None
    ImageOps = None

try:
    from google import genai
    from google.genai import types
except ModuleNotFoundError:  # pragma: no cover - depends on local env
    genai = None
    types = None


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "family_restore_prompt_config.json"
LOG_DIR = ROOT / "logs"
TOKEN_LOG_PATH = LOG_DIR / "token_usage.csv"

CACHE_ROOT = Path(tempfile.gettempdir()) / "photo_restorer"
REFERENCE_UPLOAD_DIR = CACHE_ROOT / "reference_uploads"
PREVIEW_DIR = CACHE_ROOT / "compare_previews"

HOST = "0.0.0.0"
PORT = 8765
DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
VALID_SOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
VALID_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
RESTORED_OUTPUT_RE = re.compile(r"^(?P<stem>.+)_r(?P<index>\d{2})\.png$", re.IGNORECASE)
EXTENSION_PRIORITY = {".png": 0, ".jpg": 1, ".jpeg": 1}
JOB_LOCK = threading.Lock()

FORCED_PROMPT_PREFIX = """Task: Historical family photo restoration.

This is a non-sexual, fully clothed historical family photograph. Innocent family photos of children are valid restoration input when treated as ordinary family portraits.

Hard requirements:
- The target photo is the only authority for scene layout. Keep the exact camera angle, crop, room or landscape layout, furniture, walls, windows, horizon, spacing between people, body placement, and overall composition from the target photo.
- Preserve the exact facial identity, age cues, expression, and number of people from the target photo.
- Preserve the exact pose, head direction, body position, hand placement, silhouette, framing, and camera viewpoint from the target photo.
- Repair fading, scratches, tears, stains, discoloration, and age damage carefully while keeping the image natural and believable.
- If the target photo is blurry or facial detail is weak, and a reference photo of the same people is provided, use that reference only to recover facial identity and realistic face detail for the people already present in the target photo, while still matching the target photo's pose, head direction, age, expression, and exact positions in frame.
- If the target photo is too ambiguous to recover a detail confidently, keep that detail restrained or slightly soft rather than changing the composition or inventing a new arrangement.
- Treat this as restoration, not reimagining. Do not invent extra people, limbs, props, scenery, jewelry, or modern details.
- If reference images are provided, use them only for identity reinforcement, hair, clothing, and color guidance. Never copy pose, framing, camera distance, room layout, background, or extra bodies from references.
"""

DEFAULT_USER_PROMPT = """Restore the target photograph carefully while preserving its original composition, facial identity, pose, and historical character.

Repair fading, scratches, stains, tears, discoloration, blur, and age damage where possible. If a clearer reference photo of the same people is provided, use it to recover facial identity and face detail only for the people already visible in the target photo. Do not change the target photo's layout, spacing, furniture, room, or camera framing. Keep the result restrained, natural, and period-appropriate.
"""


class RefusalError(RuntimeError):
    """Raised when Gemini refuses to return an image."""


@dataclass
class RestoreRequest:
    filename: str
    selected_folder: str
    prompt_text: str
    reference_image: str
    reference_image_2: str
    extra_note: str
    colorize: bool
    overwrite_existing: bool


class AutoProcessState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.current_filename: str | None = None
        self.completed = 0
        self.total = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.pause_seconds = 0
        self.include_restored = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "stop_requested": self.stop_requested,
                "current_filename": self.current_filename,
                "completed": self.completed,
                "total": self.total,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "last_result": self.last_result,
                "last_error": self.last_error,
                "pause_seconds": self.pause_seconds,
                "include_restored": self.include_restored,
            }

    def start(self, total: int, pause_seconds: int, include_restored: bool) -> None:
        with self.lock:
            self.running = True
            self.stop_requested = False
            self.current_filename = None
            self.completed = 0
            self.total = total
            self.started_at = time.time()
            self.finished_at = None
            self.last_result = None
            self.last_error = None
            self.pause_seconds = pause_seconds
            self.include_restored = include_restored

    def update_current(self, filename: str | None) -> None:
        with self.lock:
            self.current_filename = filename

    def mark_success(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.completed += 1
            self.last_result = payload
            self.last_error = None

    def mark_error(self, message: str) -> None:
        with self.lock:
            self.last_error = message

    def request_stop(self) -> None:
        with self.lock:
            self.stop_requested = True

    def should_stop(self) -> bool:
        with self.lock:
            return self.stop_requested

    def finish(self) -> None:
        with self.lock:
            self.running = False
            self.current_filename = None
            self.finished_at = time.time()


AUTO_STATE = AutoProcessState()


def ensure_runtime_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REFERENCE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def require_pillow() -> None:
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow is not installed. Run `pip install -r requirements.txt`.")


def require_genai() -> None:
    if genai is None or types is None:
        raise RuntimeError("google-genai is not installed. Run `pip install -r requirements.txt`.")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return default.copy()


def default_config() -> dict[str, Any]:
    return {
        "selected_folder": "",
        "prompt_text": DEFAULT_USER_PROMPT,
        "reference_image": "",
        "reference_image_2": "",
        "extra_note": "",
        "colorize": False,
        "overwrite_existing": False,
        "auto_pause_seconds": 5,
        "auto_include_restored": False,
    }


def build_file_url(path: Path) -> str:
    return f"/api/file?path={quote(str(path))}"


def load_prompt_config() -> dict[str, Any]:
    payload = load_json(CONFIG_PATH, default_config())
    selected_folder = str(payload.get("selected_folder", "")).strip()
    payload["selected_folder"] = selected_folder
    payload["prompt_text"] = str(payload.get("prompt_text", DEFAULT_USER_PROMPT)).strip() or DEFAULT_USER_PROMPT
    payload["reference_image"] = str(payload.get("reference_image", "")).strip()
    payload["reference_image_2"] = str(payload.get("reference_image_2", "")).strip()
    payload["extra_note"] = str(payload.get("extra_note", "")).strip()
    payload["colorize"] = bool(payload.get("colorize", False))
    payload["overwrite_existing"] = bool(payload.get("overwrite_existing", False))
    payload["auto_pause_seconds"] = coerce_int(payload.get("auto_pause_seconds"), 5, minimum=0, maximum=600)
    payload["auto_include_restored"] = bool(payload.get("auto_include_restored", False))
    payload["reference_preview_url"] = preview_url_for_file(payload["reference_image"])
    payload["reference_preview_url_2"] = preview_url_for_file(payload["reference_image_2"])
    return payload


def save_prompt_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    config = load_prompt_config()
    selected_folder = str(raw_config.get("selected_folder", config["selected_folder"])).strip()
    if selected_folder:
        folder_path = Path(selected_folder).expanduser()
        if not folder_path.exists() or not folder_path.is_dir():
            raise FileNotFoundError(f"Selected folder not found: {selected_folder}")
        selected_folder = str(folder_path.resolve())
    payload = {
        "selected_folder": selected_folder,
        "prompt_text": str(raw_config.get("prompt_text", config["prompt_text"])).strip() or DEFAULT_USER_PROMPT,
        "reference_image": str(raw_config.get("reference_image", config["reference_image"])).strip(),
        "reference_image_2": str(raw_config.get("reference_image_2", config["reference_image_2"])).strip(),
        "extra_note": str(raw_config.get("extra_note", config["extra_note"])).strip(),
        "colorize": bool(raw_config.get("colorize", config["colorize"])),
        "overwrite_existing": bool(raw_config.get("overwrite_existing", config["overwrite_existing"])),
        "auto_pause_seconds": coerce_int(raw_config.get("auto_pause_seconds"), config["auto_pause_seconds"], minimum=0, maximum=600),
        "auto_include_restored": bool(raw_config.get("auto_include_restored", config["auto_include_restored"])),
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return load_prompt_config()


def preview_url_for_file(file_path: str) -> str | None:
    if not file_path:
        return None
    path = Path(file_path).expanduser()
    if path.exists() and path.is_file():
        return build_file_url(path)
    return None


def coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def parse_data_url(data_url: str) -> tuple[str, bytes]:
    if not data_url.startswith("data:") or ";base64," not in data_url:
        raise ValueError("Invalid uploaded image data")
    header, encoded = data_url.split(";base64,", 1)
    return header[5:], base64.b64decode(encoded)


def validate_reference_image(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Reference image not found: {path_text}")
    return str(path.resolve())


def save_uploaded_reference(filename: str, data_url: str) -> dict[str, Any]:
    ext = Path(filename).suffix.lower()
    if ext not in VALID_UPLOAD_EXTENSIONS:
        raise ValueError("Unsupported reference file type")
    _, raw = parse_data_url(data_url)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).stem).strip("._") or "reference"
    out_path = REFERENCE_UPLOAD_DIR / f"{int(time.time() * 1000)}_{safe_stem}{ext}"
    out_path.write_bytes(raw)
    return {
        "filename": out_path.name,
        "path": str(out_path),
        "url": build_file_url(out_path),
    }


def list_directories(path_text: str | None) -> dict[str, Any]:
    start_path = Path(path_text).expanduser() if path_text else Path.home()
    if not start_path.exists():
        start_path = start_path.parent if start_path.parent.exists() else Path.home()
    if not start_path.is_dir():
        start_path = start_path.parent
    start_path = start_path.resolve()
    directories = []
    try:
        children = sorted(start_path.iterdir(), key=lambda item: item.name.lower())
    except PermissionError:
        children = []
    for child in children:
        if child.is_dir():
            directories.append({"name": child.name, "path": str(child.resolve())})
    parent = start_path.parent if start_path.parent != start_path else None
    return {
        "path": str(start_path),
        "parent": str(parent) if parent else None,
        "directories": directories,
    }


def is_restored_output(path: Path) -> bool:
    return bool(RESTORED_OUTPUT_RE.match(path.name))


def base_source_stem(path: Path) -> str:
    return path.stem


def choose_best_sources(folder: Path) -> list[Path]:
    selected: dict[str, Path] = {}
    for child in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_file():
            continue
        if child.suffix.lower() not in VALID_SOURCE_EXTENSIONS:
            continue
        if is_restored_output(child):
            continue
        stem = base_source_stem(child)
        current = selected.get(stem)
        if current is None or EXTENSION_PRIORITY[child.suffix.lower()] < EXTENSION_PRIORITY[current.suffix.lower()]:
            selected[stem] = child
    return sorted(selected.values(), key=lambda item: item.name.lower())


def list_restore_outputs(folder: Path, source_stem: str) -> list[tuple[int, Path]]:
    outputs: list[tuple[int, Path]] = []
    for child in folder.iterdir():
        if not child.is_file():
            continue
        match = RESTORED_OUTPUT_RE.match(child.name)
        if match and match.group("stem") == source_stem:
            outputs.append((int(match.group("index")), child))
    return sorted(outputs, key=lambda item: item[0])


def latest_restore_output(folder: Path, source_stem: str) -> tuple[int, Path] | None:
    outputs = list_restore_outputs(folder, source_stem)
    return outputs[-1] if outputs else None


def next_restore_output(folder: Path, source_stem: str) -> tuple[int, Path]:
    latest = latest_restore_output(folder, source_stem)
    next_index = 1 if latest is None else latest[0] + 1
    return next_index, folder / f"{source_stem}_r{next_index:02d}.png"


def compare_cache_path(source_path: Path, restored_path: Path) -> Path:
    source_stamp = int(source_path.stat().st_mtime)
    restored_stamp = int(restored_path.stat().st_mtime)
    key = f"{source_path.resolve()}::{source_stamp}::{restored_path.resolve()}::{restored_stamp}"
    digest = re.sub(r"[^A-Za-z0-9]+", "_", key)[-140:]
    return PREVIEW_DIR / f"{digest}.jpg"


def scan_source_images(folder_text: str) -> list[dict[str, Any]]:
    if not folder_text:
        return []
    folder = Path(folder_text).expanduser()
    if not folder.exists() or not folder.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for source_path in choose_best_sources(folder):
        latest = latest_restore_output(folder, source_path.stem)
        latest_path = latest[1] if latest else None
        compare_path = None
        if latest_path:
            compare_path = compare_cache_path(source_path, latest_path)
        next_index, next_path = next_restore_output(folder, source_path.stem)
        rows.append(
            {
                "filename": source_path.name,
                "stem": source_path.stem,
                "source_path": str(source_path),
                "source_url": build_file_url(source_path),
                "restored": latest_path is not None,
                "restored_count": len(list_restore_outputs(folder, source_path.stem)),
                "latest_restore_name": latest_path.name if latest_path else None,
                "latest_restore_path": str(latest_path) if latest_path else None,
                "latest_restore_url": build_file_url(latest_path) if latest_path else None,
                "compare_url": build_file_url(compare_path) if compare_path and compare_path.exists() else None,
                "next_output_name": next_path.name,
                "next_output_path": str(next_path),
            }
        )
    return rows


def flatten_for_preview(image: Image.Image, background: tuple[int, int, int] = (235, 230, 223)) -> Image.Image:
    require_pillow()
    rgba = image.convert("RGBA")
    base = Image.new("RGBA", rgba.size, background + (255,))
    return Image.alpha_composite(base, rgba).convert("RGB")


def load_oriented_image(path: Path) -> Image.Image:
    require_pillow()
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.copy()


def normalize_to_source_frame(source_path: Path, restored_path: Path) -> None:
    require_pillow()
    source_img = load_oriented_image(source_path)
    restored_img = load_oriented_image(restored_path)
    src_w, src_h = source_img.size
    if src_w <= 0 or src_h <= 0:
        return
    target_size = (src_w, src_h)
    fitted = ImageOps.fit(restored_img.convert("RGBA"), target_size, method=Image.Resampling.LANCZOS)
    fitted.save(restored_path)


def create_compare_image(source_path: Path, restored_path: Path) -> Path:
    require_pillow()
    out_path = compare_cache_path(source_path, restored_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    source_img = load_oriented_image(source_path)
    restored_img = load_oriented_image(restored_path)
    left = flatten_for_preview(source_img)
    right = flatten_for_preview(restored_img)
    target_height = max(left.height, right.height)
    if left.height != target_height:
        left = left.resize((int(round(left.width * target_height / left.height)), target_height), Image.Resampling.LANCZOS)
    if right.height != target_height:
        right = right.resize((int(round(right.width * target_height / right.height)), target_height), Image.Resampling.LANCZOS)
    compare = Image.new("RGB", (left.width + right.width, target_height), (235, 230, 223))
    compare.paste(left, (0, 0))
    compare.paste(right, (left.width, 0))
    compare.save(out_path, format="JPEG", quality=92)
    return out_path


def find_source_path_by_stem(folder: Path, stem: str) -> Path | None:
    for source_path in choose_best_sources(folder):
        if source_path.stem == stem:
            return source_path
    return None


def related_compare_pair(path: Path) -> tuple[Path, Path] | None:
    folder = path.parent
    match = RESTORED_OUTPUT_RE.match(path.name)
    if match:
        source_path = find_source_path_by_stem(folder, match.group("stem"))
        return (source_path, path) if source_path else None
    if path.suffix.lower() in VALID_SOURCE_EXTENSIONS and not is_restored_output(path):
        latest = latest_restore_output(folder, path.stem)
        if latest:
            return path, latest[1]
    return None


def rotate_image_file(path: Path, clockwise_degrees: int) -> Path | None:
    require_pillow()
    normalized = clockwise_degrees % 360
    if normalized == 0:
        return None
    image = load_oriented_image(path)
    rotated = image.rotate(-normalized, expand=True)
    rotated.save(path)
    pair = related_compare_pair(path)
    if pair:
        return create_compare_image(pair[0], pair[1])
    return None


def image_to_png_bytes(path: Path) -> bytes:
    require_pillow()
    image = load_oriented_image(path)
    converted = image.convert("RGB")
    buf = io.BytesIO()
    converted.save(buf, format="PNG")
    return buf.getvalue()


def get_client() -> genai.Client:
    require_genai()
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")
    return genai.Client(api_key=api_key)


def build_effective_prompt(request: RestoreRequest) -> str:
    color_instruction = (
        "Colorization mode: apply realistic, restrained, period-appropriate color while preserving the original photograph's historical character."
        if request.colorize
        else "Cleanup-only mode: restore the image without adding speculative color."
    )
    parts = [
        FORCED_PROMPT_PREFIX.strip(),
        color_instruction,
        "User guidance:",
        request.prompt_text.strip() or DEFAULT_USER_PROMPT,
    ]
    if request.extra_note:
        parts.extend(["Image-specific note:", request.extra_note.strip()])
    return "\n\n".join(parts).strip()


def usage_string(usage_metadata: Any) -> str:
    if not usage_metadata:
        return ""
    prompt_tokens = getattr(usage_metadata, "prompt_token_count", 0)
    candidate_tokens = getattr(usage_metadata, "candidates_token_count", 0)
    total_tokens = getattr(usage_metadata, "total_token_count", 0)
    return f"Total Tokens: {total_tokens} (Prompt: {prompt_tokens}, Candidate: {candidate_tokens})"


def log_token_usage(image_name: str, usage_metadata: Any) -> str:
    usage = usage_string(usage_metadata)
    if not usage_metadata:
        return usage
    write_header = not TOKEN_LOG_PATH.exists()
    with TOKEN_LOG_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(["timestamp", "image_name", "model", "prompt_tokens", "candidate_tokens", "total_tokens"])
        writer.writerow(
            [
                int(time.time()),
                image_name,
                DEFAULT_MODEL,
                getattr(usage_metadata, "prompt_token_count", 0),
                getattr(usage_metadata, "candidates_token_count", 0),
                getattr(usage_metadata, "total_token_count", 0),
            ]
        )
    return usage


def extract_image_from_response(response: Any, output_path: Path) -> str:
    if getattr(response, "candidates", None):
        for candidate in response.candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None)
            if not parts:
                continue
            for part in parts:
                inline_data = getattr(part, "inline_data", None)
                if inline_data and getattr(inline_data, "data", None):
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(inline_data.data)
                    return "saved"
    block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    finish_reason = None
    if getattr(response, "candidates", None):
        finish_reason = getattr(response.candidates[0], "finish_reason", None)
    reason = block_reason or finish_reason or "model refusal"
    raise RefusalError(f"Gemini refused the historical family photo request: {reason}")


def run_gemini_restore(request: RestoreRequest, source_path: Path, output_path: Path) -> dict[str, Any]:
    client = get_client()
    prompt = build_effective_prompt(request)
    contents: list[Any] = [
        prompt,
        "TARGET SOURCE: This image defines the entire composition. Preserve the exact room or outdoor layout, furniture or building positions, number of people, person-to-person spacing, body placement, head direction, hand placement, framing, crop, and camera viewpoint from this target photo.",
        types.Part.from_bytes(data=image_to_png_bytes(source_path), mime_type="image/png"),
    ]
    reference_path = validate_reference_image(request.reference_image)
    if reference_path:
        contents.append(
            "REFERENCE SOURCE 1: Use this only as a same-person identity reference to recover face detail, hair, clothing, and color for the people already present in the target photo. Do not copy the scene, pose, framing, camera distance, background, or arrangement from this image."
        )
        contents.append(types.Part.from_bytes(data=image_to_png_bytes(Path(reference_path)), mime_type="image/png"))
    reference_path_2 = validate_reference_image(request.reference_image_2)
    if reference_path_2:
        contents.append(
            "REFERENCE SOURCE 2: Secondary same-person identity reference for face detail, hair, clothing, and color only. Never copy the scene, pose, framing, camera distance, or arrangement from this image."
        )
        contents.append(types.Part.from_bytes(data=image_to_png_bytes(Path(reference_path_2)), mime_type="image/png"))

    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.1,
            top_p=0.9,
            top_k=10,
            max_output_tokens=2048,
            safety_settings=[
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
            ],
        ),
    )
    usage = log_token_usage(source_path.name, getattr(response, "usage_metadata", None))
    extract_image_from_response(response, output_path)
    normalize_to_source_frame(source_path, output_path)
    compare_path = create_compare_image(source_path, output_path)
    return {
        "filename": request.filename,
        "source_url": build_file_url(source_path),
        "restored_color_url": build_file_url(output_path),
        "compare_url": build_file_url(compare_path),
        "output_name": output_path.name,
        "message": f"Restore complete. {usage}".strip(),
        "backend": "gemini",
        "refusal": False,
    }


def build_restore_request(data: dict[str, Any]) -> RestoreRequest:
    filename = str(data.get("filename", "")).strip()
    if not filename:
        raise ValueError("No source filename provided")
    selected_folder = str(data.get("selected_folder", "")).strip()
    if not selected_folder:
        raise ValueError("No source folder selected")
    folder = Path(selected_folder).expanduser()
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"Selected folder not found: {selected_folder}")
    source_path = folder / filename
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")
    return RestoreRequest(
        filename=filename,
        selected_folder=str(folder.resolve()),
        prompt_text=str(data.get("prompt_text", DEFAULT_USER_PROMPT)).strip() or DEFAULT_USER_PROMPT,
        reference_image=str(data.get("reference_image", "")).strip(),
        reference_image_2=str(data.get("reference_image_2", "")).strip(),
        extra_note=str(data.get("extra_note", "")).strip(),
        colorize=bool(data.get("colorize", False)),
        overwrite_existing=bool(data.get("overwrite_existing", False)),
    )


def output_path_for_request(request: RestoreRequest) -> Path:
    folder = Path(request.selected_folder)
    source_stem = Path(request.filename).stem
    latest = latest_restore_output(folder, source_stem)
    if latest and request.overwrite_existing:
        return latest[1]
    return next_restore_output(folder, source_stem)[1]


def process_restore_job(request: RestoreRequest) -> dict[str, Any]:
    source_path = Path(request.selected_folder) / request.filename
    output_path = output_path_for_request(request)
    payload = run_gemini_restore(request, source_path, output_path)
    payload["latest_restore_path"] = str(output_path)
    return payload


def auto_process_worker(config: dict[str, Any], filenames: list[str]) -> None:
    request_template = {
        "selected_folder": config["selected_folder"],
        "prompt_text": config["prompt_text"],
        "reference_image": config["reference_image"],
        "reference_image_2": config["reference_image_2"],
        "extra_note": config["extra_note"],
        "colorize": config["colorize"],
        "overwrite_existing": config["overwrite_existing"],
    }
    try:
        for filename in filenames:
            if AUTO_STATE.should_stop():
                break
            AUTO_STATE.update_current(filename)
            request = build_restore_request({**request_template, "filename": filename})
            with JOB_LOCK:
                try:
                    payload = process_restore_job(request)
                except Exception as exc:
                    AUTO_STATE.mark_error(str(exc))
                    break
            AUTO_STATE.mark_success(payload)
            pause_seconds = config["auto_pause_seconds"]
            if pause_seconds <= 0:
                continue
            for _ in range(pause_seconds):
                if AUTO_STATE.should_stop():
                    break
                time.sleep(1)
            if AUTO_STATE.should_stop():
                break
    finally:
        AUTO_STATE.finish()


def start_auto_process(config: dict[str, Any]) -> dict[str, Any]:
    if AUTO_STATE.snapshot()["running"]:
        raise RuntimeError("Automatic processing is already running")
    if not config["selected_folder"]:
        raise ValueError("Select a source folder before starting automatic processing")
    if config["auto_pause_seconds"] <= 0:
        raise ValueError("Automatic processing requires a pause greater than 0 seconds")
    images = scan_source_images(config["selected_folder"])
    if not config["auto_include_restored"]:
        images = [image for image in images if not image["restored"]]
    if not images:
        raise ValueError("No images available for automatic processing")
    filenames = [image["filename"] for image in images]
    AUTO_STATE.start(total=len(filenames), pause_seconds=config["auto_pause_seconds"], include_restored=config["auto_include_restored"])
    thread = threading.Thread(target=auto_process_worker, args=(config, filenames), daemon=True)
    thread.start()
    return AUTO_STATE.snapshot()


def stop_auto_process() -> dict[str, Any]:
    AUTO_STATE.request_stop()
    return AUTO_STATE.snapshot()


def is_allowed_file_access(path: Path) -> bool:
    try:
        path = path.resolve()
    except FileNotFoundError:
        return False
    config = load_prompt_config()
    selected_folder = config.get("selected_folder", "")
    allowed_roots = [REFERENCE_UPLOAD_DIR.resolve(), PREVIEW_DIR.resolve()]
    if selected_folder:
        allowed_roots.append(Path(selected_folder).expanduser().resolve())
    for ref_key in ("reference_image", "reference_image_2"):
        ref_value = str(config.get(ref_key, "")).strip()
        if ref_value:
            try:
                if path == Path(ref_value).expanduser().resolve():
                    return True
            except FileNotFoundError:
                continue
    return any(root == path or root in path.parents for root in allowed_roots)


def is_allowed_edit_path(path: Path) -> bool:
    try:
        path = path.resolve()
    except FileNotFoundError:
        return False
    config = load_prompt_config()
    selected_folder = str(config.get("selected_folder", "")).strip()
    if not selected_folder:
        return False
    root = Path(selected_folder).expanduser().resolve()
    if not (root == path.parent or root in path.parents):
        return False
    if not path.exists() or not path.is_file():
        return False
    suffix = path.suffix.lower()
    return suffix in VALID_SOURCE_EXTENSIONS or suffix == ".png"


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoRestorer/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"ok": False, "error": "File not found"}, code=404)
            return
        mime_type, _ = mimetypes.guess_type(path.name)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path in ("/", "/family_restore_gui.html"):
            self._send_text_file(ROOT / "family_restore_gui.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/config":
            self._send_json({"ok": True, **load_prompt_config()})
            return
        if parsed.path == "/api/folders":
            path_text = query.get("path", [""])[0]
            self._send_json({"ok": True, **list_directories(path_text)})
            return
        if parsed.path == "/api/images":
            config = load_prompt_config()
            self._send_json({"ok": True, "images": scan_source_images(config["selected_folder"])})
            return
        if parsed.path == "/api/process-status":
            self._send_json({"ok": True, **AUTO_STATE.snapshot()})
            return
        if parsed.path == "/api/file":
            path_text = query.get("path", [""])[0]
            if not path_text:
                self._send_json({"ok": False, "error": "Missing file path"}, code=400)
                return
            path = Path(unquote(path_text)).expanduser()
            if not is_allowed_file_access(path):
                self._send_json({"ok": False, "error": "File access is not allowed"}, code=403)
                return
            self._send_file(path)
            return
        self._send_json({"ok": False, "error": "Unknown endpoint"}, code=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/config":
                payload = save_prompt_config(self._read_json_body())
                self._send_json({"ok": True, **payload})
                return
            if parsed.path == "/api/reference-upload":
                data = self._read_json_body()
                payload = save_uploaded_reference(str(data.get("filename", "")).strip(), str(data.get("data_url", "")).strip())
                self._send_json({"ok": True, **payload})
                return
            if parsed.path == "/api/restore":
                if AUTO_STATE.snapshot()["running"]:
                    self._send_json({"ok": False, "error": "Automatic processing is already running"}, code=409)
                    return
                request = build_restore_request(self._read_json_body())
                if not JOB_LOCK.acquire(blocking=False):
                    self._send_json({"ok": False, "error": "Another restore job is already running"}, code=409)
                    return
                try:
                    payload = process_restore_job(request)
                finally:
                    JOB_LOCK.release()
                self._send_json({"ok": True, **payload})
                return
            if parsed.path == "/api/process-folder":
                config = save_prompt_config(self._read_json_body())
                payload = start_auto_process(config)
                self._send_json({"ok": True, **payload})
                return
            if parsed.path == "/api/process-stop":
                payload = stop_auto_process()
                self._send_json({"ok": True, **payload})
                return
            if parsed.path == "/api/rotate-save":
                data = self._read_json_body()
                path_text = str(data.get("path", "")).strip()
                clockwise_degrees = int(data.get("clockwise_degrees", 0))
                if not path_text:
                    raise ValueError("Missing image path")
                path = Path(path_text).expanduser()
                if not is_allowed_edit_path(path):
                    raise ValueError("That file cannot be modified")
                compare_path = rotate_image_file(path, clockwise_degrees)
                self._send_json(
                    {
                        "ok": True,
                        "path": str(path.resolve()),
                        "file_url": build_file_url(path.resolve()),
                        "compare_url": build_file_url(compare_path) if compare_path else None,
                    }
                )
                return
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON"}, code=400)
            return
        except RefusalError as exc:
            self._send_json({"ok": False, "error": str(exc), "refusal": True}, code=422)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, code=404)
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, code=400)
            return
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": str(exc)}, code=409)
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, code=500)
            return
        self._send_json({"ok": False, "error": "Unknown endpoint"}, code=404)


def main() -> int:
    ensure_runtime_dirs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PhotoRestorer UI: http://127.0.0.1:{PORT}/family_restore_gui.html")
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
        if lan_ip and not lan_ip.startswith("127."):
            print(f"PhotoRestorer UI (LAN): http://{lan_ip}:{PORT}/family_restore_gui.html")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
