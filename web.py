"""Application web : cibles, passerelles, exécutions et résultats persistants."""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, redirect, request, url_for

from database import StorageError, SupabaseStorage
from main import (
    DEFAULT_PROXY_GATEWAY_FILE,
    EXECUTION_MODES,
    MAX_CONFIGURED_REPETITIONS,
    BrowserSettings,
    ConfigurationError,
    RunReport,
    Target,
    load_browser_settings,
    parse_proxy_gateway_text,
    resolve_repetition_count,
    run_targets,
    validate_button_text,
    validate_optional_button_text,
    validate_public_url,
)

BASE_DIR = Path(__file__).resolve().parent
APP = Flask(__name__)
app = APP
CSRF_TOKEN = secrets.token_urlsafe(32)
STATE_LOCK = threading.Lock()
REPORTS_LOCK = threading.Lock()
DATABASE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
DATABASE_STATE: dict[str, Any] = {
    "configured": False,
    "connected": False,
    "last_error": "",
}
RUN_STATE: dict[str, Any] = {
    "running": False,
    "stop_requested": False,
    "source": "",
    "started_at": "",
    "finished_at": "",
    "last_error": "",
    "next_run_at": "",
    "execution_mode": "fixed",
    "planned_repetitions": 1,
    "completed_repetitions": 0,
    "completed_visits": 0,
    "continuous_window": 0,
    "window_repetition_count": 0,
}

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "targets": [],
    "gateways": [],
    "schedule_enabled": False,
    "interval_minutes": 60,
    "execution_mode": "fixed",
    "repetition_count": 1,
    "repetition_min": 1,
    "repetition_max": 10,
}
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
MAX_TARGET_ALIAS_LENGTH = 100
MAX_GATEWAYS = 20


def set_database_state(*, configured: bool, connected: bool, error: str = "") -> None:
    with DATABASE_LOCK:
        DATABASE_STATE.update(
            configured=configured,
            connected=connected,
            last_error=error[:300],
        )


def supabase_storage() -> SupabaseStorage | None:
    """Construit le client sans jamais conserver ni journaliser sa clé."""

    configured = bool(
        os.getenv("SUPABASE_URL", "").strip()
        or os.getenv("SUPABASE_SECRET_KEY", "").strip()
    )
    try:
        storage = SupabaseStorage.from_env()
    except StorageError as exc:
        set_database_state(
            configured=configured,
            connected=False,
            error=str(exc),
        )
        return None
    if storage is None:
        set_database_state(configured=False, connected=False)
    return storage


def database_success() -> None:
    set_database_state(configured=True, connected=True)


def database_failure(exc: StorageError) -> None:
    set_database_state(configured=True, connected=False, error=str(exc))


def record_database_event(event_type: str, payload: dict[str, Any]) -> None:
    """Journal d'audit non sensible ; une panne ne bloque jamais le bot."""

    storage = supabase_storage()
    if storage is None:
        return
    try:
        storage.record_event(event_type, payload)
        database_success()
    except StorageError as exc:
        database_failure(exc)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def storage_path(variable: str, default: str) -> Path:
    path = Path(os.getenv(variable, default))
    return path if path.is_absolute() else BASE_DIR / path


def settings_path() -> Path:
    return storage_path("SETTINGS_FILE", "/tmp/ghost-clicker-settings.json")


def reports_path() -> Path:
    return storage_path("REPORTS_FILE", "/tmp/ghost-clicker-reports.json")


def proxy_gateway_file_path() -> Path:
    return storage_path("PROXY_GATEWAY_FILE", DEFAULT_PROXY_GATEWAY_FILE)


def proxy_gateways_file_path() -> Path:
    return storage_path(
        "PROXY_GATEWAYS_FILE", "/tmp/ghost-clicker-proxy-gateways.json"
    )


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def atomic_write_private_text(path: Path, value: str) -> None:
    """Écrit le fichier secret avec le mode 0600 avant de le rendre visible."""

    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(value)
        temporary_path.replace(path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ConfigurationError(
            "Impossible d'enregistrer la passerelle proxy."
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def read_gateway_secrets() -> dict[str, str]:
    path = proxy_gateways_file_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(secret)
        for key, secret in value.items()
        if SAFE_IDENTIFIER_PATTERN.fullmatch(str(key))
        and isinstance(secret, str)
    }


def write_gateway_secrets(value: dict[str, str]) -> None:
    atomic_write_private_text(
        proxy_gateways_file_path(),
        json.dumps(value, ensure_ascii=False, indent=2),
    )


def bounded_integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def validate_target_alias(value: str) -> str:
    alias = " ".join(value.strip().split())
    if not alias:
        raise ConfigurationError("Le pseudo de la cible est obligatoire.")
    if len(alias) > MAX_TARGET_ALIAS_LENGTH:
        raise ConfigurationError(
            f"Le pseudo est limité à {MAX_TARGET_ALIAS_LENGTH} caractères."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in alias):
        raise ConfigurationError("Le pseudo contient un caractère interdit.")
    return alias


def stable_target_id(item: dict[str, Any], position: int) -> str:
    current = str(item.get("id", "")).strip()
    if SAFE_IDENTIFIER_PATTERN.fullmatch(current):
        return current
    source = "\x1f".join(
        (
            str(position),
            str(item.get("url", "")),
            str(item.get("button_text", "")),
            str(item.get("second_button_text", "")),
        )
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
    return f"target_{digest}"


def normalize_target_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for position, raw_item in enumerate(value, start=1):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        url = str(item.get("url", "")).strip()
        raw_alias = " ".join(str(item.get("alias", "")).strip().split())
        if not raw_alias:
            try:
                raw_alias = urlsplit(url).hostname or f"Cible {position}"
            except ValueError:
                raw_alias = f"Cible {position}"
        normalized.append(
            {
                "id": stable_target_id(item, position),
                "alias": raw_alias[:MAX_TARGET_ALIAS_LENGTH],
                "url": url,
                "button_text": str(item.get("button_text", "")).strip(),
                "second_button_text": str(
                    item.get("second_button_text", "")
                ).strip(),
                "selected": bool(item.get("selected", True)),
            }
        )
    return normalized


def normalize_gateway_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    active_seen = False
    for position, raw_item in enumerate(value[:MAX_GATEWAYS], start=1):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        identifier = str(item.get("id", "")).strip()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(identifier):
            source = f"{position}\x1f{item.get('alias', '')}"
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]
            identifier = f"gateway_{digest}"
        alias = " ".join(str(item.get("alias", "")).strip().split())
        if not alias:
            alias = f"Passerelle {position}"
        active = bool(item.get("active", False)) and not active_seen
        active_seen = active_seen or active
        normalized.append(
            {
                "id": identifier,
                "alias": alias[:MAX_TARGET_ALIAS_LENGTH],
                "active": active,
            }
        )
    if normalized and not active_seen:
        normalized[0]["active"] = True
    return normalized


def validate_gateway_alias(value: str) -> str:
    alias = " ".join(value.strip().split())
    if not alias:
        raise ConfigurationError("Le pseudo de la passerelle est obligatoire.")
    if len(alias) > MAX_TARGET_ALIAS_LENGTH:
        raise ConfigurationError(
            f"Le pseudo est limité à {MAX_TARGET_ALIAS_LENGTH} caractères."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in alias):
        raise ConfigurationError("Le pseudo contient un caractère interdit.")
    return alias


def gateways_for_api(settings: dict[str, Any]) -> list[dict[str, Any]]:
    secrets_by_id = read_gateway_secrets()
    return [
        {**gateway, "configured": gateway["id"] in secrets_by_id}
        for gateway in settings.get("gateways", [])
        if isinstance(gateway, dict)
    ]


def migrate_legacy_gateway(settings: dict[str, Any]) -> None:
    """Adopte une ancienne passerelle unique sans exposer ses identifiants."""

    gateways = settings.get("gateways", [])
    path = proxy_gateway_file_path()
    if gateways or not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigurationError(
            "Impossible de lire l'ancienne passerelle éphémère."
        ) from exc
    parsed = parse_proxy_gateway_text(content)
    identifier = "gateway_legacy"
    settings["gateways"] = [
        {"id": identifier, "alias": parsed.name, "active": True}
    ]
    secrets_by_id = read_gateway_secrets()
    secrets_by_id[identifier] = content.strip()
    write_gateway_secrets(secrets_by_id)
    save_app_settings(settings)


def sync_active_gateway(settings: dict[str, Any]) -> bool:
    migrate_legacy_gateway(settings)
    gateways = settings.get("gateways", [])
    if not isinstance(gateways, list) or not gateways:
        return False
    active = next(
        (
            item
            for item in gateways
            if isinstance(item, dict) and item.get("active")
        ),
        None,
    )
    secrets_by_id = read_gateway_secrets()
    secret = secrets_by_id.get(str(active.get("id", ""))) if active else None
    path = proxy_gateway_file_path()
    if active is None or not secret:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                "Impossible de désactiver la passerelle éphémère."
            ) from exc
        return False
    alias = validate_gateway_alias(str(active.get("alias", "")))
    atomic_write_private_text(path, f"# alias: {alias}\n{secret.strip()}\n")
    return True


def validated_target_record(
    value: dict[str, Any], *, target_id: str | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError("Les données de la cible sont invalides.")
    identifier = target_id or f"target_{secrets.token_urlsafe(12)}"
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ConfigurationError("Identifiant de cible invalide.")
    selected = value.get("selected", True)
    if not isinstance(selected, bool):
        raise ConfigurationError("L'état de sélection de la cible est invalide.")
    return {
        "id": identifier,
        "alias": validate_target_alias(str(value.get("alias", ""))),
        "url": validate_public_url(str(value.get("url", ""))),
        "button_text": validate_button_text(str(value.get("button_text", ""))),
        "second_button_text": validate_optional_button_text(
            str(value.get("second_button_text", ""))
        ),
        "selected": selected,
    }


def normalize_app_settings(value: dict[str, Any]) -> dict[str, Any]:
    mode = str(value.get("execution_mode", "fixed")).strip().lower()
    if mode not in EXECUTION_MODES:
        mode = "fixed"
    minimum = bounded_integer(
        value.get("repetition_min"), 1, 1, MAX_CONFIGURED_REPETITIONS
    )
    maximum = bounded_integer(
        value.get("repetition_max"), 10, 1, MAX_CONFIGURED_REPETITIONS
    )
    maximum = max(maximum, minimum)
    return {
        "targets": normalize_target_records(value.get("targets", [])),
        "gateways": normalize_gateway_records(value.get("gateways", [])),
        "schedule_enabled": bool(value.get("schedule_enabled", False)),
        "interval_minutes": bounded_integer(
            value.get("interval_minutes"), 60, 60, 10_080
        ),
        "execution_mode": mode,
        "repetition_count": bounded_integer(
            value.get("repetition_count"),
            1,
            1,
            MAX_CONFIGURED_REPETITIONS,
        ),
        "repetition_min": minimum,
        "repetition_max": maximum,
    }


def read_local_app_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return dict(DEFAULT_APP_SETTINGS)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_APP_SETTINGS)
    if not isinstance(value, dict):
        return dict(DEFAULT_APP_SETTINGS)
    return normalize_app_settings(value)


def load_app_settings() -> dict[str, Any]:
    local_value = read_local_app_settings()
    storage = supabase_storage()
    if storage is None:
        return local_value
    try:
        stored_value = storage.load_settings()
        if stored_value is None:
            # Première connexion : le cache local ou les valeurs par défaut
            # initialisent la ligne persistante.
            storage.save_settings(local_value)
            stored_value = local_value
        database_success()
        normalized = normalize_app_settings(stored_value)
    except StorageError as exc:
        database_failure(exc)
        return local_value
    try:
        atomic_write_json(settings_path(), normalized)
    except OSError:
        # Supabase reste la source de vérité si le cache /tmp est indisponible.
        pass
    return normalized


def save_app_settings(value: dict[str, Any]) -> None:
    normalized = normalize_app_settings(value)
    storage = supabase_storage()
    database_requested = bool(
        os.getenv("SUPABASE_URL", "").strip()
        or os.getenv("SUPABASE_SECRET_KEY", "").strip()
    )
    if storage is None and database_requested:
        with DATABASE_LOCK:
            error = str(DATABASE_STATE["last_error"])
        raise ConfigurationError(error or "Supabase n'est pas configuré.")
    if storage is not None:
        try:
            storage.save_settings(normalized)
            database_success()
        except StorageError as exc:
            database_failure(exc)
            raise ConfigurationError(
                "Impossible d'enregistrer les paramètres dans Supabase."
            ) from exc
    atomic_write_json(settings_path(), normalized)


def read_reports_unlocked() -> list[dict[str, Any]]:
    path = reports_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def load_reports(*, all_data: bool = False) -> list[dict[str, Any]]:
    storage = supabase_storage()
    if storage is not None:
        try:
            reports = storage.load_reports(all_data=all_data)
            database_success()
            return reports
        except StorageError as exc:
            database_failure(exc)
    with REPORTS_LOCK:
        return read_reports_unlocked()


def append_report(value: dict[str, Any]) -> None:
    report = dict(value)
    report.setdefault("run_id", secrets.token_urlsafe(12))
    with REPORTS_LOCK:
        reports = read_reports_unlocked()
        reports.append(report)
        atomic_write_json(reports_path(), reports[-200:])
    storage = supabase_storage()
    if storage is not None:
        try:
            storage.append_report(report)
            database_success()
        except StorageError as exc:
            database_failure(exc)


def upsert_report(value: dict[str, Any]) -> None:
    """Crée ou remplace le rapport et sa dernière visite terminée."""

    run_id = str(value.get("run_id", "")).strip()
    if not run_id:
        raise ValueError("Le rapport progressif doit contenir un run_id.")
    with REPORTS_LOCK:
        reports = read_reports_unlocked()
        for index, report in enumerate(reports):
            if report.get("run_id") == run_id:
                reports[index] = value
                break
        else:
            reports.append(value)
        atomic_write_json(reports_path(), reports[-200:])
    storage = supabase_storage()
    if storage is not None:
        try:
            storage.upsert_report(value)
            database_success()
        except StorageError as exc:
            # Le cache local préserve la progression et le navigateur continue.
            database_failure(exc)


def supabase_deletion_error(action: str) -> ConfigurationError:
    with DATABASE_LOCK:
        detail = str(DATABASE_STATE.get("last_error", "")).strip()
    if not detail:
        detail = (
            "vérifiez SUPABASE_SECRET_KEY et la connexion du projet dans Render"
        )
    return ConfigurationError(
        f"Impossible de {action} dans Supabase : {detail.rstrip('.')}."
    )


def clear_stored_reports() -> None:
    storage = supabase_storage()
    database_requested = bool(
        os.getenv("SUPABASE_URL", "").strip()
        or os.getenv("SUPABASE_SECRET_KEY", "").strip()
    )
    if storage is None and database_requested:
        raise supabase_deletion_error("effacer les résultats")
    if storage is not None:
        try:
            storage.clear_reports()
            database_success()
        except StorageError as exc:
            database_failure(exc)
            raise supabase_deletion_error("effacer les résultats") from exc
    path = reports_path()
    with REPORTS_LOCK:
        if path.exists():
            path.unlink()


def delete_stored_results(
    *,
    visits: list[dict[str, Any]] | None = None,
    run_ids: list[str] | None = None,
) -> dict[str, int]:
    selected_visits = visits or []
    selected_runs = run_ids or []
    if not selected_visits and not selected_runs:
        raise ConfigurationError("Sélectionnez au moins un résultat à supprimer.")
    if len(selected_visits) > 500 or len(selected_runs) > 200:
        raise ConfigurationError("La sélection de suppression est trop grande.")

    normalized_visits: list[dict[str, Any]] = []
    for item in selected_visits:
        if not isinstance(item, dict):
            raise ConfigurationError("Sélection de visite invalide.")
        run_id = str(item.get("run_id", "")).strip()
        try:
            visit_number = int(item.get("visit_number", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("Numéro de visite invalide.") from exc
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(run_id) or visit_number < 1:
            raise ConfigurationError("Sélection de visite invalide.")
        normalized_visits.append(
            {"run_id": run_id, "visit_number": visit_number}
        )

    normalized_runs = []
    for value in selected_runs:
        run_id = str(value).strip()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(run_id):
            raise ConfigurationError("Identifiant d'exécution invalide.")
        normalized_runs.append(run_id)

    storage = supabase_storage()
    database_requested = bool(
        os.getenv("SUPABASE_URL", "").strip()
        or os.getenv("SUPABASE_SECRET_KEY", "").strip()
    )
    if storage is None and database_requested:
        raise supabase_deletion_error("supprimer les résultats")
    summary = {"visits": 0, "runs": 0}
    if storage is not None:
        try:
            summary = storage.delete_results(
                visits=normalized_visits,
                run_ids=normalized_runs,
            )
            database_success()
        except StorageError as exc:
            database_failure(exc)
            raise supabase_deletion_error("supprimer les résultats") from exc

    visit_keys = {
        (item["run_id"], item["visit_number"]) for item in normalized_visits
    }
    run_id_set = set(normalized_runs)
    with REPORTS_LOCK:
        reports = read_reports_unlocked()
        retained_reports: list[dict[str, Any]] = []
        local_deleted_visits = 0
        local_deleted_runs = 0
        for report in reports:
            run_id = str(report.get("run_id", ""))
            if run_id in run_id_set:
                local_deleted_runs += 1
                continue
            results = report.get("results", [])
            if isinstance(results, list):
                try:
                    completed = int(report.get("completed_visits", len(results)))
                except (TypeError, ValueError):
                    completed = len(results)
                first_number = max(1, completed - len(results) + 1)
                retained_results = []
                for offset, result in enumerate(results):
                    if not isinstance(result, dict):
                        retained_results.append(result)
                        continue
                    try:
                        visit_number = int(
                            result.get("visit_number", first_number + offset)
                        )
                    except (TypeError, ValueError):
                        visit_number = first_number + offset
                    if (run_id, visit_number) in visit_keys:
                        local_deleted_visits += 1
                    else:
                        retained_result = dict(result)
                        retained_result["visit_number"] = visit_number
                        retained_results.append(retained_result)
                report = dict(report)
                report["results"] = retained_results
            retained_reports.append(report)
        atomic_write_json(reports_path(), retained_reports[-200:])

    if storage is None:
        summary = {"visits": local_deleted_visits, "runs": local_deleted_runs}
    return summary


def targets_from_settings(value: dict[str, Any]) -> list[Target]:
    targets: list[Target] = []
    raw_targets = value.get("targets", [])
    if not isinstance(raw_targets, list):
        raise ConfigurationError("La liste enregistrée est invalide.")
    if len(raw_targets) > 50:
        raise ConfigurationError("Maximum : 50 URL.")
    for item in raw_targets:
        if not isinstance(item, dict):
            raise ConfigurationError("Une entrée enregistrée est invalide.")
        if not bool(item.get("selected", True)):
            continue
        targets.append(
            Target(
                url=validate_public_url(str(item.get("url", ""))),
                button_text=validate_button_text(str(item.get("button_text", ""))),
                second_button_text=validate_optional_button_text(
                    str(item.get("second_button_text", ""))
                ),
                target_id=str(item.get("id", "")),
                alias=validate_target_alias(str(item.get("alias", ""))),
            )
        )
    return targets


def parse_targets_text(value: str) -> list[Target]:
    targets: list[Target] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", maxsplit=2)
        if len(parts) not in {2, 3}:
            raise ConfigurationError(
                f"Ligne {line_number} : utilisez le format "
                "URL | premier libellé [| second libellé]."
            )
        url, button_text = parts[:2]
        second_button_text = parts[2] if len(parts) == 3 else ""
        targets.append(
            Target(
                url=validate_public_url(url),
                button_text=validate_button_text(button_text),
                second_button_text=validate_optional_button_text(
                    second_button_text
                ),
            )
        )
    if len(targets) > 50:
        raise ConfigurationError("Maximum : 50 URL.")
    return targets


def targets_to_text(value: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in value.get("targets", []):
        if isinstance(item, dict):
            line = f"{item.get('url', '')} | {item.get('button_text', '')}"
            second_button_text = str(item.get("second_button_text", "")).strip()
            if second_button_text:
                line += f" | {second_button_text}"
            lines.append(line)
    return "\n".join(lines)


def _authorized() -> bool:
    expected = os.getenv("ADMIN_TOKEN", "")
    authorization = request.authorization
    if not expected or authorization is None:
        return False
    return authorization.username == "admin" and hmac.compare_digest(
        authorization.password or "", expected
    )


@APP.before_request
def require_authentication() -> Response | None:
    if request.endpoint == "health":
        return None
    if not os.getenv("ADMIN_TOKEN", ""):
        return Response(
            "ADMIN_TOKEN n'est pas configuré.",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    if _authorized():
        return None
    return Response(
        "Authentification requise.",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Ghost-Clicker"'},
        content_type="text/plain; charset=utf-8",
    )


@APP.after_request
def add_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def require_csrf() -> None:
    supplied = request.headers.get("X-CSRF-Token", "") or request.form.get(
        "csrf_token", ""
    )
    if not hmac.compare_digest(supplied, CSRF_TOKEN):
        raise ConfigurationError("Jeton de sécurité invalide. Rechargez la page.")


def read_browser_configuration(
    app_settings: dict[str, Any] | None = None,
) -> tuple[BrowserSettings | None, str]:
    try:
        sync_active_gateway(app_settings or load_app_settings())
        return load_browser_settings(BASE_DIR), ""
    except (ConfigurationError, OSError) as exc:
        return None, str(exc)


def execute_job(source: str) -> None:
    run_id = ""
    try:
        app_settings = load_app_settings()
        sync_active_gateway(app_settings)
        browser_settings = load_browser_settings(BASE_DIR)
        targets = targets_from_settings(app_settings)
        if not targets:
            raise ConfigurationError("Aucune URL n'est sélectionnée.")

        execution_mode = str(app_settings["execution_mode"])
        repetition_count = resolve_repetition_count(
            execution_mode,
            int(app_settings["repetition_count"]),
            int(app_settings["repetition_min"]),
            int(app_settings["repetition_max"]),
        )
        run_id = secrets.token_urlsafe(12)
        with STATE_LOCK:
            RUN_STATE.update(
                execution_mode=execution_mode,
                planned_repetitions=repetition_count,
            )
        record_database_event(
            "run_started",
            {
                "run_id": run_id,
                "source": source,
                "execution_mode": execution_mode,
                "planned_repetitions": repetition_count,
                "target_count": len(targets),
                "started_at": now_iso(),
            },
        )

        def report_value(
            report: RunReport, *, in_progress: bool
        ) -> dict[str, Any]:
            value = report.to_dict()
            value.update(
                run_id=run_id,
                source=source,
                in_progress=in_progress,
                updated_at=now_iso(),
            )
            return value

        def update_progress(report: RunReport) -> None:
            with STATE_LOCK:
                RUN_STATE.update(
                    completed_repetitions=report.completed_repetitions,
                    completed_visits=report.completed_visits,
                    continuous_window=report.continuous_window,
                    window_repetition_count=report.window_repetition_count,
                )
            # Le CSV et le tableau deviennent disponibles après chaque visite.
            upsert_report(report_value(report, in_progress=True))

        report = run_targets(
            browser_settings,
            targets,
            repetition_count=repetition_count,
            execution_mode=execution_mode,
            continuous_minimum=int(app_settings["repetition_min"]),
            continuous_maximum=int(app_settings["repetition_max"]),
            should_stop=STOP_EVENT.is_set,
            on_progress=update_progress,
        )
        upsert_report(report_value(report, in_progress=False))
        record_database_event(
            "run_finished",
            {
                "run_id": run_id,
                "source": source,
                "success": report.success,
                "stopped": report.stopped,
                "completed_repetitions": report.completed_repetitions,
                "completed_visits": report.completed_visits,
                "failed_visits": report.failed_visits,
                "finished_at": report.finished_at,
            },
        )
        with STATE_LOCK:
            RUN_STATE["last_error"] = report.error
    except Exception as exc:
        record_database_event(
            "run_failed",
            {
                "run_id": run_id,
                "source": source,
                "error_type": type(exc).__name__,
                "failed_at": now_iso(),
            },
        )
        with STATE_LOCK:
            RUN_STATE["last_error"] = str(exc)[:500]
    finally:
        with STATE_LOCK:
            RUN_STATE["running"] = False
            RUN_STATE["stop_requested"] = False
            RUN_STATE["finished_at"] = now_iso()
        STOP_EVENT.clear()


def start_job(source: str) -> bool:
    with STATE_LOCK:
        if RUN_STATE["running"]:
            return False
        STOP_EVENT.clear()
        RUN_STATE.update(
            running=True,
            stop_requested=False,
            source=source,
            started_at=now_iso(),
            finished_at="",
            last_error="",
            execution_mode="fixed",
            planned_repetitions=1,
            completed_repetitions=0,
            completed_visits=0,
            continuous_window=0,
            window_repetition_count=0,
        )
    thread = threading.Thread(target=execute_job, args=(source,), daemon=True)
    thread.start()
    return True


def request_job_stop() -> bool:
    with STATE_LOCK:
        if not RUN_STATE["running"]:
            return False
        RUN_STATE["stop_requested"] = True
        STOP_EVENT.set()
        return True


def scheduler_loop() -> None:
    next_run_timestamp: float | None = None
    while True:
        app_settings = load_app_settings()
        enabled = bool(app_settings.get("schedule_enabled", False))
        interval = max(60, int(app_settings.get("interval_minutes", 60)))
        now = time.time()

        if not enabled:
            next_run_timestamp = None
            with STATE_LOCK:
                RUN_STATE["next_run_at"] = ""
        else:
            if next_run_timestamp is None:
                next_run_timestamp = now + interval * 60
            if now >= next_run_timestamp:
                start_job("planifié")
                next_run_timestamp = now + interval * 60
            with STATE_LOCK:
                RUN_STATE["next_run_at"] = datetime.fromtimestamp(
                    next_run_timestamp
                ).astimezone().isoformat(timespec="seconds")
        time.sleep(15)


def require_idle_configuration() -> None:
    with STATE_LOCK:
        if RUN_STATE["running"]:
            raise ConfigurationError(
                "Attendez l'arrêt de l'exécution avant cette modification."
            )


def request_json_object() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ConfigurationError("Le corps JSON est invalide.")
    return value


def reports_for_api(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_report in reports:
        report = dict(raw_report)
        raw_results = report.get("results", [])
        results = raw_results if isinstance(raw_results, list) else []
        try:
            completed = int(report.get("completed_visits", len(results)))
        except (TypeError, ValueError):
            completed = len(results)
        first_number = max(1, completed - len(results) + 1)
        report["results"] = []
        for offset, raw_result in enumerate(results):
            if not isinstance(raw_result, dict):
                continue
            result = dict(raw_result)
            result.setdefault("visit_number", first_number + offset)
            report["results"].append(result)
        normalized.append(report)
    return normalized


def api_snapshot(*, include_targets: bool = True) -> dict[str, Any]:
    reports = reports_for_api(load_reports())
    settings = load_app_settings() if include_targets else None
    gateway_ready = sync_active_gateway(settings) if settings is not None else False
    with STATE_LOCK:
        state = dict(RUN_STATE)
    with DATABASE_LOCK:
        database_state = dict(DATABASE_STATE)
    value: dict[str, Any] = {
        "ok": True,
        "run": state,
        "database": database_state,
        "gateway_ready": gateway_ready,
        "reports": reports,
    }
    if settings is not None:
        value["targets"] = settings.get("targets", [])
        value["gateways"] = gateways_for_api(settings)
        value["selected_target_count"] = sum(
            1 for item in value["targets"] if item.get("selected", True)
        )
    return value


def api_error(exc: ConfigurationError, status: int = 400) -> tuple[Response, int]:
    return jsonify({"ok": False, "error": str(exc)[:500]}), status


@APP.get("/api/state")
def api_state() -> Response:
    return jsonify(api_snapshot())


@APP.get("/api/status")
def api_status() -> Response:
    with STATE_LOCK:
        state = dict(RUN_STATE)
    return jsonify({"ok": True, "run": state})


@APP.get("/api/gateways")
def api_list_gateways() -> Response:
    settings = load_app_settings()
    sync_active_gateway(settings)
    return jsonify({"ok": True, "gateways": gateways_for_api(settings)})


@APP.post("/api/gateways")
def api_add_gateway() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        payload = request_json_object()
        alias = validate_gateway_alias(str(payload.get("alias", "")))
        content = str(payload.get("gateway", "")).strip()
        if not content:
            raise ConfigurationError("Saisissez les identifiants de la passerelle.")
        if len(content) > 4_096:
            raise ConfigurationError("La passerelle proxy est trop longue.")
        parse_proxy_gateway_text(content)
        requested_active = payload.get("active", False)
        if not isinstance(requested_active, bool):
            raise ConfigurationError("L'état actif de la passerelle est invalide.")

        settings = load_app_settings()
        gateways = list(settings.get("gateways", []))
        if len(gateways) >= MAX_GATEWAYS:
            raise ConfigurationError(f"Maximum : {MAX_GATEWAYS} passerelles.")
        identifier = f"gateway_{secrets.token_urlsafe(12)}"
        make_active = requested_active or not gateways
        if make_active:
            gateways = [{**item, "active": False} for item in gateways]
        gateway = {"id": identifier, "alias": alias, "active": make_active}
        gateways.append(gateway)
        settings["gateways"] = gateways
        save_app_settings(settings)

        secrets_by_id = read_gateway_secrets()
        secrets_by_id[identifier] = content
        write_gateway_secrets(secrets_by_id)
        sync_active_gateway(settings)
        record_database_event(
            "gateway_created",
            {
                "gateway_id": identifier,
                "alias": alias,
                "active": make_active,
                "updated_at": now_iso(),
            },
        )
        return jsonify(
            {
                "ok": True,
                "message": "Passerelle ajoutée.",
                "gateways": gateways_for_api(settings),
            }
        )
    except ConfigurationError as exc:
        return api_error(exc)


@APP.patch("/api/gateways/<gateway_id>")
def api_update_gateway(gateway_id: str) -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(gateway_id):
            raise ConfigurationError("Identifiant de passerelle invalide.")
        payload = request_json_object()
        settings = load_app_settings()
        gateways = list(settings.get("gateways", []))
        for index, gateway in enumerate(gateways):
            if gateway.get("id") == gateway_id:
                updated = dict(gateway)
                if "alias" in payload:
                    updated["alias"] = validate_gateway_alias(
                        str(payload.get("alias", ""))
                    )
                gateways[index] = updated
                break
        else:
            raise ConfigurationError("Passerelle introuvable.")

        content = str(payload.get("gateway", "")).strip()
        if content:
            if len(content) > 4_096:
                raise ConfigurationError("La passerelle proxy est trop longue.")
            parse_proxy_gateway_text(content)
        settings["gateways"] = gateways
        save_app_settings(settings)
        if content:
            secrets_by_id = read_gateway_secrets()
            secrets_by_id[gateway_id] = content
            write_gateway_secrets(secrets_by_id)
        sync_active_gateway(settings)
        record_database_event(
            "gateway_updated",
            {"gateway_id": gateway_id, "updated_at": now_iso()},
        )
        return jsonify(
            {
                "ok": True,
                "message": "Passerelle modifiée.",
                "gateways": gateways_for_api(settings),
            }
        )
    except ConfigurationError as exc:
        return api_error(
            exc, 404 if str(exc) == "Passerelle introuvable." else 400
        )


@APP.post("/api/gateways/<gateway_id>/select")
def api_select_gateway(gateway_id: str) -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(gateway_id):
            raise ConfigurationError("Identifiant de passerelle invalide.")
        secrets_by_id = read_gateway_secrets()
        if gateway_id not in secrets_by_id:
            raise ConfigurationError(
                "Ressaisissez les identifiants avant d'activer cette passerelle."
            )
        settings = load_app_settings()
        gateways = list(settings.get("gateways", []))
        if not any(item.get("id") == gateway_id for item in gateways):
            raise ConfigurationError("Passerelle introuvable.")
        gateways = [
            {**item, "active": item.get("id") == gateway_id}
            for item in gateways
        ]
        settings["gateways"] = gateways
        save_app_settings(settings)
        sync_active_gateway(settings)
        record_database_event(
            "gateway_selected",
            {"gateway_id": gateway_id, "updated_at": now_iso()},
        )
        return jsonify(
            {
                "ok": True,
                "message": "Passerelle active mise à jour.",
                "gateways": gateways_for_api(settings),
            }
        )
    except ConfigurationError as exc:
        return api_error(
            exc, 404 if str(exc) == "Passerelle introuvable." else 400
        )


@APP.delete("/api/gateways/<gateway_id>")
def api_delete_gateway(gateway_id: str) -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(gateway_id):
            raise ConfigurationError("Identifiant de passerelle invalide.")
        settings = load_app_settings()
        gateways = list(settings.get("gateways", []))
        retained = [item for item in gateways if item.get("id") != gateway_id]
        if len(retained) == len(gateways):
            raise ConfigurationError("Passerelle introuvable.")
        secrets_by_id = read_gateway_secrets()
        secrets_by_id.pop(gateway_id, None)
        if retained and not any(item.get("active") for item in retained):
            configured_id = next(
                (
                    str(item.get("id"))
                    for item in retained
                    if str(item.get("id")) in secrets_by_id
                ),
                str(retained[0].get("id")),
            )
            retained = [
                {**item, "active": str(item.get("id")) == configured_id}
                for item in retained
            ]
        settings["gateways"] = retained
        if not retained:
            try:
                proxy_gateway_file_path().unlink(missing_ok=True)
            except OSError as exc:
                raise ConfigurationError(
                    "Impossible de supprimer la passerelle active."
                ) from exc
        save_app_settings(settings)
        write_gateway_secrets(secrets_by_id)
        sync_active_gateway(settings)
        record_database_event(
            "gateway_deleted",
            {"gateway_id": gateway_id, "updated_at": now_iso()},
        )
        return jsonify(
            {
                "ok": True,
                "message": "Passerelle supprimée.",
                "gateways": gateways_for_api(settings),
            }
        )
    except ConfigurationError as exc:
        return api_error(
            exc, 404 if str(exc) == "Passerelle introuvable." else 400
        )


@APP.post("/api/gateway")
def api_save_gateway() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        content = str(request_json_object().get("gateway", "")).strip()
        if not content:
            raise ConfigurationError("Saisissez la passerelle proxy.")
        if len(content) > 4_096:
            raise ConfigurationError("La passerelle proxy est trop longue.")
        parse_proxy_gateway_text(content)
        settings = load_app_settings()
        gateways = list(settings.get("gateways", []))
        active = next((item for item in gateways if item.get("active")), None)
        if active is None:
            atomic_write_private_text(proxy_gateway_file_path(), f"{content}\n")
            sync_active_gateway(settings)
        else:
            secrets_by_id = read_gateway_secrets()
            secrets_by_id[str(active["id"])] = content
            write_gateway_secrets(secrets_by_id)
            sync_active_gateway(settings)
        record_database_event(
            "gateway_updated", {"configured": True, "updated_at": now_iso()}
        )
        return jsonify(
            {
                "ok": True,
                "message": "Passerelle enregistrée.",
                "gateways": gateways_for_api(settings),
            }
        )
    except ConfigurationError as exc:
        return api_error(exc)


@APP.get("/api/targets")
def api_list_targets() -> Response:
    targets = load_app_settings().get("targets", [])
    return jsonify({"ok": True, "targets": targets})


@APP.post("/api/targets")
def api_add_target() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        current = load_app_settings()
        targets = list(current.get("targets", []))
        if len(targets) >= 50:
            raise ConfigurationError("Maximum : 50 URL.")
        target = validated_target_record(request_json_object())
        targets.append(target)
        current["targets"] = targets
        save_app_settings(current)
        record_database_event(
            "target_created",
            {
                "target_id": target["id"],
                "alias": target["alias"],
                "selected": target["selected"],
                "updated_at": now_iso(),
            },
        )
        return jsonify(
            {
                "ok": True,
                "message": "Cible ajoutée.",
                "targets": targets,
            }
        )
    except ConfigurationError as exc:
        return api_error(exc)


@APP.patch("/api/targets/<target_id>")
def api_update_target(target_id: str) -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(target_id):
            raise ConfigurationError("Identifiant de cible invalide.")
        payload = request_json_object()
        current = load_app_settings()
        targets = list(current.get("targets", []))
        for index, target in enumerate(targets):
            if target.get("id") == target_id:
                merged = dict(target)
                merged.update(payload)
                targets[index] = validated_target_record(
                    merged, target_id=target_id
                )
                break
        else:
            raise ConfigurationError("Cible introuvable.")
        current["targets"] = targets
        save_app_settings(current)
        record_database_event(
            "target_updated",
            {"target_id": target_id, "updated_at": now_iso()},
        )
        return jsonify(
            {
                "ok": True,
                "message": "Cible modifiée.",
                "targets": targets,
            }
        )
    except ConfigurationError as exc:
        return api_error(exc, 404 if str(exc) == "Cible introuvable." else 400)


@APP.delete("/api/targets/<target_id>")
def api_delete_target(target_id: str) -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        if not SAFE_IDENTIFIER_PATTERN.fullmatch(target_id):
            raise ConfigurationError("Identifiant de cible invalide.")
        current = load_app_settings()
        targets = list(current.get("targets", []))
        retained = [item for item in targets if item.get("id") != target_id]
        if len(retained) == len(targets):
            raise ConfigurationError("Cible introuvable.")
        current["targets"] = retained
        save_app_settings(current)
        record_database_event(
            "target_deleted",
            {"target_id": target_id, "updated_at": now_iso()},
        )
        return jsonify(
            {
                "ok": True,
                "message": "Cible supprimée.",
                "targets": retained,
            }
        )
    except ConfigurationError as exc:
        return api_error(exc, 404 if str(exc) == "Cible introuvable." else 400)


@APP.post("/api/targets/selection")
def api_select_targets() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        payload = request_json_object()
        mode = str(payload.get("mode", "")).strip().lower()
        current = load_app_settings()
        targets = list(current.get("targets", []))
        if mode in {"all", "none"}:
            selected_ids = {
                str(item.get("id", "")) for item in targets
            } if mode == "all" else set()
        else:
            raw_ids = payload.get("selected_ids", [])
            if not isinstance(raw_ids, list):
                raise ConfigurationError("La sélection des cibles est invalide.")
            selected_ids = {str(value) for value in raw_ids}
            known_ids = {str(item.get("id", "")) for item in targets}
            if not selected_ids.issubset(known_ids):
                raise ConfigurationError("La sélection contient une cible inconnue.")
        updated = []
        for item in targets:
            target = dict(item)
            target["selected"] = target.get("id") in selected_ids
            updated.append(target)
        current["targets"] = updated
        save_app_settings(current)
        record_database_event(
            "target_selection_updated",
            {
                "selected_count": len(selected_ids),
                "updated_at": now_iso(),
            },
        )
        return jsonify(
            {
                "ok": True,
                "message": f"{len(selected_ids)} cible(s) sélectionnée(s).",
                "targets": updated,
            }
        )
    except ConfigurationError as exc:
        return api_error(exc)


@APP.post("/api/settings")
def api_update_settings() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        require_idle_configuration()
        payload = request_json_object()
        current = load_app_settings()
        interval = int(payload.get("interval_minutes", 60))
        if not 60 <= interval <= 10_080:
            raise ConfigurationError(
                "L'intervalle doit être compris entre 60 et 10080 minutes."
            )
        execution_mode = str(payload.get("execution_mode", "fixed")).strip()
        repetition_count = int(payload.get("repetition_count", 1))
        repetition_min = int(payload.get("repetition_min", 1))
        repetition_max = int(payload.get("repetition_max", 10))
        resolve_repetition_count(
            execution_mode,
            repetition_count,
            repetition_min,
            repetition_max,
        )
        current.update(
            schedule_enabled=bool(payload.get("schedule_enabled", False)),
            interval_minutes=interval,
            execution_mode=execution_mode,
            repetition_count=repetition_count,
            repetition_min=repetition_min,
            repetition_max=repetition_max,
        )
        save_app_settings(current)
        record_database_event(
            "settings_updated",
            {
                "schedule_enabled": current["schedule_enabled"],
                "interval_minutes": interval,
                "execution_mode": execution_mode,
                "repetition_count": repetition_count,
                "repetition_min": repetition_min,
                "repetition_max": repetition_max,
                "updated_at": now_iso(),
            },
        )
        return jsonify({"ok": True, "message": "Paramètres enregistrés."})
    except (ConfigurationError, TypeError, ValueError) as exc:
        if isinstance(exc, ConfigurationError):
            return api_error(exc)
        return api_error(ConfigurationError("Les valeurs numériques sont invalides."))


@APP.post("/api/run")
def api_run_now() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        app_settings = load_app_settings()
        sync_active_gateway(app_settings)
        load_browser_settings(BASE_DIR)
        targets = targets_from_settings(app_settings)
        if not targets:
            raise ConfigurationError("Sélectionnez au moins une URL.")
        if not start_job("manuel"):
            raise ConfigurationError("Une exécution est déjà en cours.")
        return jsonify({"ok": True, "message": "Exécution démarrée."})
    except ConfigurationError as exc:
        return api_error(exc)


@APP.post("/api/stop")
def api_stop_now() -> tuple[Response, int] | Response:
    try:
        require_csrf()
        if not request_job_stop():
            raise ConfigurationError("Aucune exécution n'est en cours.")
        record_database_event(
            "stop_requested", {"requested_at": now_iso()}
        )
        return jsonify(
            {
                "ok": True,
                "message": "Arrêt demandé. La visite en cours va se terminer.",
            }
        )
    except ConfigurationError as exc:
        return api_error(exc)


@APP.delete("/api/results")
@APP.post("/api/results/delete")
def api_delete_results() -> tuple[Response, int] | Response:
    """Supprime l'historique même pendant une exécution en cours."""

    try:
        require_csrf()
        payload = request_json_object()
        action = str(payload.get("action", "selected")).strip().lower()
        if action == "all":
            clear_stored_reports()
            summary = {"visits": 0, "runs": 0}
            event_type = "reports_cleared"
            message = "Historique Supabase effacé."
        elif action == "selected":
            raw_visits = payload.get("visits", [])
            raw_run_ids = payload.get("run_ids", [])
            if not isinstance(raw_visits, list) or not isinstance(raw_run_ids, list):
                raise ConfigurationError("La sélection des résultats est invalide.")
            summary = delete_stored_results(
                visits=raw_visits,
                run_ids=[str(value) for value in raw_run_ids],
            )
            event_type = "results_deleted"
            message = (
                "Suppression Supabase terminée : "
                f"{summary['visits']} visite(s) et {summary['runs']} exécution(s)."
            )
        else:
            raise ConfigurationError("Action de suppression inconnue.")

        with STATE_LOCK:
            running = bool(RUN_STATE["running"])
        if running:
            message += (
                " L'exécution en cours peut encore enregistrer de nouvelles visites."
            )
        record_database_event(
            event_type,
            {
                "deleted_visits": summary["visits"],
                "deleted_runs": summary["runs"],
                "updated_at": now_iso(),
            },
        )
        reports = reports_for_api(load_reports())
        with DATABASE_LOCK:
            database_state = dict(DATABASE_STATE)
        return jsonify(
            {
                "ok": True,
                "message": message,
                "summary": summary,
                "reports": reports,
                "database": database_state,
            }
        )
    except ConfigurationError as exc:
        return api_error(exc)


@APP.get("/health")
def health() -> Response:
    _settings, error = read_browser_configuration()
    with DATABASE_LOCK:
        database_degraded = bool(
            DATABASE_STATE["configured"]
            and not DATABASE_STATE["connected"]
            and DATABASE_STATE["last_error"]
        )
    status = "degraded" if error or database_degraded else "ok"
    return Response(f"{status}\n", content_type="text/plain; charset=utf-8")


def read_uploaded_text(field_name: str) -> str:
    uploaded = request.files.get(field_name)
    if uploaded is None or not uploaded.filename:
        raise ConfigurationError("Sélectionnez un fichier texte.")
    content = uploaded.stream.read(512_001)
    if len(content) > 512_000:
        raise ConfigurationError("Le fichier dépasse 500 Ko.")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("Le fichier doit être encodé en UTF-8.") from exc


@APP.post("/gateway")
def save_gateway() -> Response:
    try:
        require_csrf()
        require_idle_configuration()
        content = request.form.get("gateway", "").strip()
        if not content:
            raise ConfigurationError("Saisissez la passerelle proxy.")
        if len(content) > 4_096:
            raise ConfigurationError("La passerelle proxy est trop longue.")
        parse_proxy_gateway_text(content)
        settings = load_app_settings()
        gateways = list(settings.get("gateways", []))
        active = next((item for item in gateways if item.get("active")), None)
        if active is None:
            atomic_write_private_text(proxy_gateway_file_path(), f"{content}\n")
            sync_active_gateway(settings)
        else:
            secrets_by_id = read_gateway_secrets()
            secrets_by_id[str(active["id"])] = content
            write_gateway_secrets(secrets_by_id)
            sync_active_gateway(settings)
        record_database_event(
            "gateway_updated",
            {"configured": True, "updated_at": now_iso()},
        )
        return redirect(url_for("home", gateway="1"), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/upload-targets")
def upload_targets() -> Response:
    try:
        require_csrf()
        content = read_uploaded_text("target_file")
        targets = parse_targets_text(content)
        current = load_app_settings()
        current["targets"] = [
            {
                "url": target.url,
                "button_text": target.button_text,
                "second_button_text": target.second_button_text,
            }
            for target in targets
        ]
        save_app_settings(current)
        record_database_event(
            "targets_imported",
            {"target_count": len(targets), "updated_at": now_iso()},
        )
        return redirect(url_for("home", targets=str(len(targets))), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/settings")
def update_settings() -> Response:
    try:
        require_csrf()
        current = load_app_settings()
        target_records = list(current.get("targets", []))
        if "targets" in request.form:
            imported_targets = parse_targets_text(request.form.get("targets", ""))
            target_records = [
                {
                    "url": target.url,
                    "button_text": target.button_text,
                    "second_button_text": target.second_button_text,
                    "selected": True,
                }
                for target in imported_targets
            ]
        raw_interval = request.form.get("interval_minutes", "60").strip()
        try:
            interval = int(raw_interval)
        except ValueError as exc:
            raise ConfigurationError("L'intervalle doit être un entier.") from exc
        if not 60 <= interval <= 10_080:
            raise ConfigurationError(
                "L'intervalle doit être compris entre 60 et 10080 minutes."
            )

        execution_mode = request.form.get("execution_mode", "fixed").strip()
        try:
            repetition_count = int(
                request.form.get("repetition_count", "1").strip()
            )
            repetition_min = int(
                request.form.get("repetition_min", "1").strip()
            )
            repetition_max = int(
                request.form.get("repetition_max", "10").strip()
            )
        except ValueError as exc:
            raise ConfigurationError(
                "Les répétitions doivent être des nombres entiers."
            ) from exc
        resolve_repetition_count(
            execution_mode,
            repetition_count,
            repetition_min,
            repetition_max,
        )

        schedule_enabled = request.form.get("schedule_enabled") == "on"
        save_app_settings(
            {
                "targets": target_records,
                "gateways": current.get("gateways", []),
                "schedule_enabled": schedule_enabled,
                "interval_minutes": interval,
                "execution_mode": execution_mode,
                "repetition_count": repetition_count,
                "repetition_min": repetition_min,
                "repetition_max": repetition_max,
            }
        )
        record_database_event(
            "settings_updated",
            {
                "target_count": len(target_records),
                "schedule_enabled": schedule_enabled,
                "interval_minutes": interval,
                "execution_mode": execution_mode,
                "repetition_count": repetition_count,
                "repetition_min": repetition_min,
                "repetition_max": repetition_max,
                "updated_at": now_iso(),
            },
        )
        return redirect(url_for("home", saved="1"), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/run")
def run_now() -> Response:
    try:
        require_csrf()
        app_settings = load_app_settings()
        sync_active_gateway(app_settings)
        load_browser_settings(BASE_DIR)
        targets = targets_from_settings(app_settings)
        if not targets:
            raise ConfigurationError("Sélectionnez au moins une URL.")
        started = start_job("manuel")
        return redirect(
            url_for("home", started="1" if started else "0"), code=303
        )
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/stop")
def stop_now() -> Response:
    try:
        require_csrf()
        requested = request_job_stop()
        if requested:
            record_database_event(
                "stop_requested",
                {"requested_at": now_iso()},
            )
        return redirect(
            url_for("home", stopped="1" if requested else "0"), code=303
        )
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/clear-results")
def clear_results() -> Response:
    try:
        require_csrf()
        clear_stored_reports()
        record_database_event(
            "reports_cleared",
            {"cleared_at": now_iso()},
        )
        return redirect(url_for("home", cleared="1"), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.get("/results.csv")
def download_results() -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "execution_id",
            "numero_visite",
            "source",
            "date",
            "cible_id",
            "pseudo_cible",
            "mode_execution",
            "repetition",
            "repetitions_planifiees",
            "cycle_continu",
            "repetitions_du_cycle",
            "proxy",
            "connexions_proxy",
            "url_initiale",
            "premier_libelle",
            "second_libelle",
            "url_finale",
            "statut_initial",
            "statut_final",
            "succes",
            "erreur",
            "duree_ms",
        ]
    )
    # L'interface limite l'affichage récent ; l'export récupère tout
    # l'historique persistant disponible.
    for report in load_reports(all_data=True):
        for result in report.get("results", []):
            writer.writerow(
                [
                    report.get("run_id", ""),
                    result.get("visit_number", ""),
                    report.get("source", ""),
                    result.get("started_at", ""),
                    result.get("target_id", ""),
                    result.get("target_alias", ""),
                    report.get("execution_mode", "fixed"),
                    result.get("repetition", ""),
                    report.get("repetition_count", ""),
                    result.get("continuous_window", ""),
                    result.get("window_repetition_count", ""),
                    result.get("proxy_name", ""),
                    result.get("proxy_attempts", ""),
                    result.get("initial_url", ""),
                    result.get("button_text", ""),
                    result.get("second_button_text", ""),
                    result.get("final_url", ""),
                    result.get("initial_status", ""),
                    result.get("final_status", ""),
                    result.get("success", False),
                    result.get("error", ""),
                    result.get("duration_ms", ""),
                ]
            )
    return Response(
        output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ghost-clicker-results.csv"},
    )


@APP.get("/")
def home() -> str:
    app_settings = load_app_settings()
    _browser_settings, configuration_error = read_browser_configuration(app_settings)
    reports = reports_for_api(load_reports())
    gateways = gateways_for_api(app_settings)
    with STATE_LOCK:
        state = dict(RUN_STATE)
    with DATABASE_LOCK:
        database_state = dict(DATABASE_STATE)

    messages: list[str] = []
    if request.args.get("error"):
        messages.append(request.args["error"][:500])
    if configuration_error:
        messages.append(f"Configuration serveur : {configuration_error}")
    if database_state["configured"] and database_state["last_error"]:
        messages.append(f"Base de données : {database_state['last_error']}")
    if state["last_error"]:
        messages.append(f"Dernière erreur : {state['last_error']}")

    execution_mode = str(app_settings.get("execution_mode", "fixed"))
    targets = app_settings.get("targets", [])
    selected_count = sum(1 for item in targets if item.get("selected", True))
    if database_state["connected"]:
        database_status = "Supabase connectée"
        database_class = "ready"
    elif database_state["configured"]:
        database_status = "Connexion requise"
        database_class = "warning"
    else:
        database_status = "Cache local"
        database_class = "warning"

    def script_json(value: Any) -> str:
        return (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )

    page = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d0f12">
<title>Ghost Clicker</title>
<style>
:root{color-scheme:dark;--canvas:#0d0f12;--surface:#15181c;--soft:#1a1e23;--ink:#f2f4f7;--muted:#9ba3ad;--line:#292e35;--strong:#3c434d;--success:#58c68a;--success-bg:#14271d;--warning:#f0b84b;--danger:#ff8b81;--danger-bg:#2b1818;--focus:#7e9cc8;--nav-h:72px;--top-h:58px}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:var(--canvas)}body{color:var(--ink);font:14px/1.5 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}button,input,select{font:inherit}a{color:inherit}
.topbar{height:var(--top-h);border-bottom:1px solid var(--line);background:rgba(13,15,18,.94);backdrop-filter:blur(12px);display:flex;align-items:center;position:relative;z-index:20}.topbar-inner{width:min(1100px,calc(100% - 28px));margin:auto;display:flex;align-items:center;justify-content:space-between}.brand{display:flex;align-items:center;gap:10px;font-weight:750}.brand-mark{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;background:var(--ink);color:var(--canvas);font-size:12px}.page-title{color:var(--muted);font-size:12px;font-weight:700}
.notices{position:fixed;z-index:50;top:66px;left:50%;transform:translateX(-50%);width:min(620px,calc(100% - 24px));display:grid;gap:7px}.notice{padding:11px 13px;border:1px solid #2b3b4d;border-left:3px solid #7893b3;border-radius:9px;background:#151d27;color:#c6d2df;box-shadow:0 10px 30px rgba(0,0,0,.25)}.notice.error{border-color:#5c2c2c;border-left-color:var(--danger);background:var(--danger-bg);color:#ffd0cc}
.app-viewport{height:calc(100dvh - var(--top-h) - var(--nav-h));overflow:hidden}.app-track{height:100%;display:flex;align-items:stretch;transition:transform .32s cubic-bezier(.22,.61,.36,1);will-change:transform}.app-page{flex:0 0 100%;height:100%;overflow-y:auto;overscroll-behavior:contain;padding:18px max(14px,calc((100% - 1100px)/2)) 28px;scrollbar-gutter:stable}.page-head{margin-bottom:16px}.eyebrow{display:block;margin-bottom:6px;color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.13em;text-transform:uppercase}h1,h2,p{margin-top:0}h1{margin-bottom:5px;font-size:26px;line-height:1.15;letter-spacing:-.035em}h2{margin:0;font-size:17px;letter-spacing:-.02em}.page-head p,.panel-head p,.hint{color:var(--muted)}.page-head p{margin:0}
.status-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.stat{min-width:0;padding:14px 15px;border:1px solid var(--line);border-radius:12px;background:var(--surface)}.stat-label{display:block;margin-bottom:6px;color:var(--muted);font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.stat-value{display:flex;align-items:center;gap:8px;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px;font-weight:700}.status-dot{width:8px;height:8px;flex:none;border-radius:50%}.status-dot.idle,.status-dot.ready{background:var(--success)}.status-dot.running{background:#7da6e3;animation:pulse 1.7s infinite}.status-dot.warning{background:var(--warning)}@keyframes pulse{50%{opacity:.35}}
.panel{margin-bottom:14px;border:1px solid var(--line);border-radius:13px;background:var(--surface);overflow:hidden}.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:17px 18px 14px;border-bottom:1px solid var(--line)}.panel-head p{margin:4px 0 0;font-size:12px}.panel-body{padding:18px}.count{padding:4px 8px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font-size:10px;white-space:nowrap}.two-col{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}.wide{grid-column:1/-1}
label{display:block;margin-bottom:5px;color:#c9ced5;font-size:11px;font-weight:700}input,select{width:100%;height:40px;border:1px solid var(--strong);border-radius:9px;background:#101317;color:var(--ink);padding:0 11px;outline:none}input:focus,select:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(126,156,200,.13)}input[type=checkbox],input[type=radio]{width:15px;height:15px;padding:0;accent-color:#dce3ec}.check-row{display:flex;align-items:center;gap:8px;margin-top:10px}.check-row label{margin:0}
button,.button{min-height:38px;border:1px solid var(--ink);border-radius:9px;background:var(--ink);color:#11151a;padding:0 13px;font-size:12px;font-weight:800;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}button:hover,.button:hover{opacity:.86}button:disabled{cursor:not-allowed;opacity:.4}.secondary{border-color:var(--strong);background:transparent;color:var(--ink)}.danger{border-color:#713b3b;background:var(--danger-bg);color:var(--danger)}.small{min-height:30px;padding:0 9px;font-size:10px}.actions,.form-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.form-actions{margin-top:14px}.badge{display:inline-block;padding:3px 7px;border-radius:999px;font-size:9px;font-weight:800}.success{background:var(--success-bg);color:var(--success)}.failure{background:var(--danger-bg);color:var(--danger)}.active-badge{background:#172337;color:#9bbce8}.missing-badge{background:#2a2212;color:var(--warning)}
.mode-options{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}.mode-option{display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid var(--line);border-radius:9px;background:#121519}.mode-option span{font-size:11px}.mode-option small{display:block;color:var(--muted);font-weight:400}.repeat-fields{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.run-specs{display:grid;margin:0 0 16px}.run-specs div{display:flex;justify-content:space-between;gap:16px;padding:9px 0;border-bottom:1px solid var(--line)}.run-specs dt{color:var(--muted);font-size:11px}.run-specs dd{margin:0;text-align:right;font-size:11px;font-weight:700}
.table-wrap{overflow:auto}table{width:100%;min-width:880px;border-collapse:collapse}th,td{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{position:sticky;top:0;z-index:2;background:var(--soft);color:var(--muted);font-size:9px;font-weight:800;letter-spacing:.07em;text-transform:uppercase}td{font-size:11px}tbody tr:hover{background:#191d22}.url-cell{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row-actions{display:flex;gap:5px}.target-selected,.gateway-active{background:#171d1a}.empty{padding:30px;text-align:center;color:var(--muted)}
.bottom-nav{height:calc(var(--nav-h) + env(safe-area-inset-bottom));position:fixed;z-index:40;left:0;right:0;bottom:0;padding:6px max(10px,calc((100% - 680px)/2)) env(safe-area-inset-bottom);border-top:1px solid var(--line);background:rgba(17,20,24,.96);backdrop-filter:blur(16px);display:grid;grid-template-columns:repeat(5,1fr);gap:3px}.nav-item{min-width:0;border:0;background:transparent;color:var(--muted);border-radius:10px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;padding:5px 2px;font-size:9px;font-weight:700}.nav-item svg{width:21px;height:21px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.nav-item.active{color:var(--ink);background:#22272e}.nav-item:hover{opacity:1;background:#1c2026}.footer-note{color:var(--muted);font-size:11px;text-align:center;padding:8px 0}
@media(max-width:800px){.status-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two-col,.form-grid{grid-template-columns:1fr}.wide{grid-column:auto}.mode-options,.repeat-fields{grid-template-columns:1fr}.app-page{padding-left:12px;padding-right:12px}}
@media(max-width:440px){:root{--nav-h:68px}.page-head{margin-bottom:12px}h1{font-size:23px}.panel-head,.panel-body{padding:14px}.status-grid{gap:7px}.stat{padding:12px}.nav-item span{font-size:8px}}
@media(prefers-reduced-motion:reduce){.app-track{transition:none}*{animation:none!important}}
</style>
</head>
<body>
<header class="topbar"><div class="topbar-inner"><div class="brand"><span class="brand-mark">G</span><span>Ghost Clicker</span></div><span id="page-title" class="page-title">Accueil</span></div></header>
<div id="notices" class="notices" aria-live="polite">__MESSAGES__</div>
<main class="app-viewport"><div id="app-track" class="app-track">
<section class="app-page" data-page="home" aria-label="Accueil">
<div class="page-head"><span class="eyebrow">Tableau de bord</span><h1>Vue d’ensemble</h1><p>Lancez le bot et suivez son état en temps réel.</p></div>
<div class="status-grid"><article class="stat"><span class="stat-label">Exécution</span><p id="run-status" class="stat-value"><span id="run-dot" class="status-dot idle"></span>En attente</p></article><article class="stat"><span class="stat-label">Base de données</span><p class="stat-value"><span id="database-dot" class="status-dot __DATABASE_CLASS__"></span><span id="database-status">__DATABASE_STATUS__</span></p></article><article class="stat"><span class="stat-label">Passerelle active</span><p class="stat-value"><span id="gateway-dot" class="status-dot warning"></span><span id="gateway-status">Absente</span></p></article><article class="stat"><span class="stat-label">Cibles actives</span><p id="selected-count-card" class="stat-value">__SELECTED_COUNT__ / __TARGET_COUNT__</p></article></div>
<div class="two-col"><section class="panel"><header class="panel-head"><div><span class="eyebrow">Exécution</span><h2>Lancer le scénario</h2><p>Seules les URL cochées utilisent la passerelle active.</p></div></header><div class="panel-body"><dl class="run-specs"><div><dt>Cibles sélectionnées</dt><dd id="run-selected-count">__SELECTED_COUNT__</dd></div><div><dt>Passerelle</dt><dd id="run-gateway-name">Aucune</dd></div><div><dt>Navigateur</dt><dd>Chromium neuf par visite</dd></div></dl><div class="actions"><button id="run-button" type="button">Lancer maintenant</button><button id="stop-button" class="danger" type="button" disabled>Arrêter</button></div></div></section><section class="panel"><header class="panel-head"><div><span class="eyebrow">Raccourcis</span><h2>Actions</h2><p>Accédez directement aux données utiles.</p></div></header><div class="panel-body"><div class="actions"><button class="secondary" data-go="targets" type="button">Gérer les URL</button><button class="secondary" data-go="gateways" type="button">Gérer les passerelles</button><button class="secondary" data-go="results" type="button">Voir les résultats</button><a class="button secondary" href="/results.csv">Exporter le CSV</a></div></div></section></div><p class="footer-note">Statut léger actualisé automatiquement, sans rechargement complet.</p>
</section>

<section class="app-page" data-page="targets" aria-label="Cibles">
<div class="page-head"><span class="eyebrow">Cibles</span><h1>Bibliothèque d’URL</h1><p>Ajoutez, modifiez et choisissez les URL utilisées lors de chaque lancement.</p></div>
<section class="panel"><header class="panel-head"><div><h2 id="target-form-title">Nouvelle cible</h2><p>Un pseudo permet de reconnaître rapidement chaque URL.</p></div><span id="target-count" class="count">__TARGET_COUNT__ cible(s)</span></header><div class="panel-body"><form id="target-form"><input id="target-id" type="hidden"><div class="form-grid"><div><label for="target-alias">Pseudo</label><input id="target-alias" maxlength="100" placeholder="Page d’accueil" required></div><div><label for="target-url">URL</label><input id="target-url" type="url" placeholder="https://site.example/page" required></div><div><label for="target-button">Premier libellé</label><input id="target-button" placeholder="Continuer" required></div><div><label for="target-second-button">Second libellé facultatif</label><input id="target-second-button" placeholder="Confirmer"></div></div><div class="check-row"><input id="target-selected" type="checkbox" checked><label for="target-selected">Utiliser lors des prochains lancements</label></div><div class="form-actions"><button data-config id="target-submit" type="submit">Ajouter</button><button data-config id="target-cancel" class="secondary" type="button" hidden>Annuler</button></div></form></div></section>
<section class="panel"><header class="panel-head"><div class="actions"><button data-config id="targets-all" class="secondary small" type="button">Tout sélectionner</button><button data-config id="targets-none" class="secondary small" type="button">Tout désélectionner</button></div><span id="selected-target-count" class="count">__SELECTED_COUNT__ sélectionnée(s)</span></header><div class="table-wrap"><table><thead><tr><th>Utiliser</th><th>Pseudo</th><th>URL</th><th>Premier libellé</th><th>Second libellé</th><th>Actions</th></tr></thead><tbody id="target-rows"></tbody></table></div></section>
</section>

<section class="app-page" data-page="gateways" aria-label="Passerelles">
<div class="page-head"><span class="eyebrow">Réseau</span><h1>Passerelles Rayobyte</h1><p>Les pseudos persistent dans Supabase. Les identifiants restent protégés dans un fichier éphémère privé et doivent être ressaisis après un redéploiement.</p></div>
<section class="panel"><header class="panel-head"><div><h2 id="gateway-form-title">Nouvelle passerelle</h2><p>Une seule passerelle peut être active à la fois.</p></div><span id="gateway-count" class="count">0 passerelle</span></header><div class="panel-body"><form id="gateway-form"><input id="gateway-id" type="hidden"><div class="form-grid"><div><label for="gateway-alias">Pseudo</label><input id="gateway-alias" maxlength="100" placeholder="Rayobyte principale" required></div><div><label for="gateway-secret">Identifiants</label><input id="gateway-secret" type="password" autocomplete="off" placeholder="username:password@host:port"></div></div><div class="check-row"><input id="gateway-active" type="checkbox"><label for="gateway-active">Définir comme passerelle active</label></div><p class="hint">En modification, laissez les identifiants vides pour conserver la valeur éphémère actuelle.</p><div class="form-actions"><button data-config id="gateway-submit" type="submit">Ajouter</button><button data-config id="gateway-cancel" class="secondary" type="button" hidden>Annuler</button></div></form></div></section>
<section class="panel"><header class="panel-head"><div><h2>Passerelles enregistrées</h2><p>Réactivez les entrées marquées « identifiants manquants » après un redéploiement.</p></div></header><div class="table-wrap"><table><thead><tr><th>Active</th><th>Pseudo</th><th>Identifiants</th><th>Actions</th></tr></thead><tbody id="gateway-rows"></tbody></table></div></section>
</section>

<section class="app-page" data-page="results" aria-label="Résultats">
<div class="page-head"><span class="eyebrow">Historique</span><h1>Résultats Supabase</h1><p>Consultez et supprimez les visites directement depuis l’application.</p></div>
<section id="results-panel" class="panel" aria-busy="false"><header class="panel-head"><div><div class="actions"><button id="delete-selected-results" class="danger small" type="button" disabled>Supprimer la sélection</button><button id="clear-results" class="danger small" type="button">Tout effacer</button><a class="button secondary small" href="/results.csv">Télécharger le CSV</a></div><p>La suppression reste disponible pendant une exécution. Les nouvelles visites continueront à apparaître.</p></div><span id="result-count" class="count">0 visite</span></header><div class="table-wrap"><table><thead><tr><th><input id="select-all-results" type="checkbox" aria-label="Tout sélectionner"></th><th>Date</th><th>Pseudo</th><th>Source</th><th>Répétition</th><th>URL initiale</th><th>URL finale</th><th>Résultat</th><th>Erreur</th><th>Actions</th></tr></thead><tbody id="result-rows"></tbody></table></div></section>
</section>

<section class="app-page" data-page="settings" aria-label="Réglages">
<div class="page-head"><span class="eyebrow">Réglages</span><h1>Exécution et planification</h1><p>Configurez les répétitions et les lancements automatiques.</p></div>
<section class="panel"><header class="panel-head"><div><h2>Mode d’exécution</h2><p>La sélection des URL et de la passerelle est gérée dans leurs onglets.</p></div></header><div class="panel-body"><form id="settings-form"><label>Mode de répétition</label><div class="mode-options"><label class="mode-option"><input type="radio" name="execution_mode" value="fixed" __FIXED_CHECKED__><span>Nombre fixe<small>Valeur exacte.</small></span></label><label class="mode-option"><input type="radio" name="execution_mode" value="range" __RANGE_CHECKED__><span>Intervalle<small>Total tiré min/max.</small></span></label><label class="mode-option"><input type="radio" name="execution_mode" value="continuous" __CONTINUOUS_CHECKED__><span>Continu<small>Cycles de cinq minutes.</small></span></label></div><div class="repeat-fields"><div><label for="repetition-count">Nombre fixe</label><input id="repetition-count" type="number" min="1" max="10000" value="__REPETITION_COUNT__"></div><div><label for="repetition-min">Minimum</label><input id="repetition-min" type="number" min="1" max="10000" value="__REPETITION_MIN__"></div><div><label for="repetition-max">Maximum</label><input id="repetition-max" type="number" min="1" max="10000" value="__REPETITION_MAX__"></div></div><div style="margin-top:12px"><label for="interval-minutes">Intervalle planifié en minutes</label><input id="interval-minutes" type="number" min="60" max="10080" value="__INTERVAL__"></div><div class="check-row"><input id="schedule-enabled" type="checkbox" __SCHEDULE_CHECKED__><label for="schedule-enabled">Activer la planification</label></div><div class="form-actions"><button data-config type="submit">Enregistrer les réglages</button></div></form></div></section>
</section>
</div></main>

<nav class="bottom-nav" aria-label="Navigation principale">
<button class="nav-item active" data-nav="home" type="button" aria-label="Accueil"><svg viewBox="0 0 24 24"><path d="m3 11 9-8 9 8"></path><path d="M5 10v10h14V10"></path><path d="M9 20v-6h6v6"></path></svg><span>Accueil</span></button>
<button class="nav-item" data-nav="targets" type="button" aria-label="URL"><svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"></path><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"></path></svg><span>URL</span></button>
<button class="nav-item" data-nav="gateways" type="button" aria-label="Passerelles"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="2"></rect><rect x="3" y="14" width="18" height="6" rx="2"></rect><path d="M7 7h.01M7 17h.01M17 7h2M17 17h2"></path></svg><span>Passerelles</span></button>
<button class="nav-item" data-nav="results" type="button" aria-label="Résultats"><svg viewBox="0 0 24 24"><path d="M4 19V9M10 19V5M16 19v-7M22 19H2"></path></svg><span>Résultats</span></button>
<button class="nav-item" data-nav="settings" type="button" aria-label="Réglages"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"></path></svg><span>Réglages</span></button>
</nav>
<script>
const csrfToken=__CSRF_JSON__;
let targets=__TARGETS_JSON__;
let gateways=__GATEWAYS_JSON__;
let reports=__REPORTS_JSON__;
let runState=__RUN_STATE_JSON__;
let databaseState=__DATABASE_STATE_JSON__;
let resultMutationPending=false;
const notices=document.getElementById('notices');
const pages=['home','targets','gateways','results','settings'];
const pageLabels={home:'Accueil',targets:'URL',gateways:'Passerelles',results:'Résultats',settings:'Réglages'};
let activePage='home';
function showNotice(message,isError=false){const node=document.createElement('div');node.className='notice'+(isError?' error':'');node.textContent=message;notices.replaceChildren(node);window.setTimeout(()=>{if(node.isConnected)node.remove()},6500)}
async function api(url,{method='GET',body=null}={}){const options={method,headers:{Accept:'application/json'}};if(method!=='GET')options.headers['X-CSRF-Token']=csrfToken;if(body!==null){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body)}const response=await fetch(url,options);let value;try{value=await response.json()}catch{throw new Error('Réponse serveur invalide.')}if(!response.ok||!value.ok)throw new Error(value.error||'Opération impossible.');return value}
function goToPage(name,updateHash=true){const index=pages.indexOf(name);if(index<0)return;activePage=name;document.getElementById('app-track').style.transform=`translateX(-${index*100}%)`;document.querySelectorAll('.nav-item').forEach(node=>node.classList.toggle('active',node.dataset.nav===name));document.querySelectorAll('.app-page').forEach(node=>{const hidden=node.dataset.page!==name;node.setAttribute('aria-hidden',String(hidden));node.inert=hidden});document.getElementById('page-title').textContent=pageLabels[name];if(updateHash)history.replaceState(null,'',`#${name}`)}
document.querySelectorAll('[data-nav]').forEach(node=>node.addEventListener('click',()=>goToPage(node.dataset.nav)));document.querySelectorAll('[data-go]').forEach(node=>node.addEventListener('click',()=>goToPage(node.dataset.go)));window.addEventListener('hashchange',()=>goToPage(location.hash.slice(1)||'home',false));
let touchStartX=0;document.getElementById('app-track').addEventListener('touchstart',event=>{touchStartX=event.changedTouches[0].screenX},{passive:true});document.getElementById('app-track').addEventListener('touchend',event=>{const delta=event.changedTouches[0].screenX-touchStartX;if(Math.abs(delta)<60)return;const index=pages.indexOf(activePage)+(delta<0?1:-1);if(index>=0&&index<pages.length)goToPage(pages[index])},{passive:true});
function cell(text,className=''){const node=document.createElement('td');node.textContent=text??'';if(className)node.className=className;return node}function actionButton(label,className,handler){const node=document.createElement('button');node.type='button';node.className=(className||'secondary')+' small';node.textContent=label;node.addEventListener('click',handler);return node}
function resetTargetForm(){document.getElementById('target-form').reset();document.getElementById('target-id').value='';document.getElementById('target-selected').checked=true;document.getElementById('target-form-title').textContent='Nouvelle cible';document.getElementById('target-submit').textContent='Ajouter';document.getElementById('target-cancel').hidden=true}
function editTarget(target){document.getElementById('target-id').value=target.id;document.getElementById('target-alias').value=target.alias;document.getElementById('target-url').value=target.url;document.getElementById('target-button').value=target.button_text;document.getElementById('target-second-button').value=target.second_button_text||'';document.getElementById('target-selected').checked=target.selected;document.getElementById('target-form-title').textContent='Modifier la cible';document.getElementById('target-submit').textContent='Enregistrer';document.getElementById('target-cancel').hidden=false;goToPage('targets');document.getElementById('target-alias').focus()}
function renderTargets(){const body=document.getElementById('target-rows');body.replaceChildren();const selected=targets.filter(item=>item.selected).length;document.getElementById('target-count').textContent=`${targets.length} cible(s)`;document.getElementById('selected-target-count').textContent=`${selected} sélectionnée(s)`;document.getElementById('selected-count-card').textContent=`${selected} / ${targets.length}`;document.getElementById('run-selected-count').textContent=String(selected);if(!targets.length){const row=document.createElement('tr');const empty=cell('Aucune cible.','empty');empty.colSpan=6;row.append(empty);body.append(row);renderRun();return}for(const target of targets){const row=document.createElement('tr');if(target.selected)row.className='target-selected';const selectCell=document.createElement('td');const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=Boolean(target.selected);checkbox.disabled=Boolean(runState.running);checkbox.addEventListener('change',async()=>{try{const value=await api(`/api/targets/${encodeURIComponent(target.id)}`,{method:'PATCH',body:{selected:checkbox.checked}});targets=value.targets;renderTargets();showNotice(value.message)}catch(error){checkbox.checked=!checkbox.checked;showNotice(error.message,true)}});selectCell.append(checkbox);row.append(selectCell,cell(target.alias),cell(target.url,'url-cell'),cell(target.button_text),cell(target.second_button_text||'—'));const actions=document.createElement('td');actions.className='row-actions';actions.append(actionButton('Modifier','secondary',()=>editTarget(target)),actionButton('Supprimer','danger',async()=>{if(!confirm(`Supprimer la cible « ${target.alias} » ?`))return;try{const value=await api(`/api/targets/${encodeURIComponent(target.id)}`,{method:'DELETE'});targets=value.targets;resetTargetForm();renderTargets();showNotice(value.message)}catch(error){showNotice(error.message,true)}}));row.append(actions);body.append(row)}renderRun()}
function resetGatewayForm(){document.getElementById('gateway-form').reset();document.getElementById('gateway-id').value='';document.getElementById('gateway-secret').required=true;document.getElementById('gateway-active').disabled=false;document.getElementById('gateway-form-title').textContent='Nouvelle passerelle';document.getElementById('gateway-submit').textContent='Ajouter';document.getElementById('gateway-cancel').hidden=true}
function editGateway(gateway){document.getElementById('gateway-id').value=gateway.id;document.getElementById('gateway-alias').value=gateway.alias;document.getElementById('gateway-secret').value='';document.getElementById('gateway-secret').required=false;document.getElementById('gateway-active').checked=gateway.active;document.getElementById('gateway-active').disabled=gateway.active;document.getElementById('gateway-form-title').textContent='Modifier la passerelle';document.getElementById('gateway-submit').textContent='Enregistrer';document.getElementById('gateway-cancel').hidden=false;goToPage('gateways');document.getElementById('gateway-alias').focus()}
function renderGateways(){const body=document.getElementById('gateway-rows');body.replaceChildren();document.getElementById('gateway-count').textContent=`${gateways.length} passerelle(s)`;if(!gateways.length){const row=document.createElement('tr');const empty=cell('Aucune passerelle.','empty');empty.colSpan=4;row.append(empty);body.append(row);renderConnection();return}for(const gateway of gateways){const row=document.createElement('tr');if(gateway.active)row.className='gateway-active';const activeCell=document.createElement('td');const active=document.createElement('span');active.className='badge '+(gateway.active?'active-badge':'');active.textContent=gateway.active?'ACTIVE':'—';activeCell.append(active);const statusCell=document.createElement('td');const status=document.createElement('span');status.className='badge '+(gateway.configured?'success':'missing-badge');status.textContent=gateway.configured?'DISPONIBLES':'MANQUANTS';statusCell.append(status);const actions=document.createElement('td');actions.className='row-actions';if(!gateway.active)actions.append(actionButton('Activer','secondary',async()=>{try{const value=await api(`/api/gateways/${encodeURIComponent(gateway.id)}/select`,{method:'POST',body:{}});gateways=value.gateways;renderGateways();showNotice(value.message)}catch(error){showNotice(error.message,true)}}));actions.append(actionButton('Modifier','secondary',()=>editGateway(gateway)),actionButton('Supprimer','danger',async()=>{if(!confirm(`Supprimer la passerelle « ${gateway.alias} » ?`))return;try{const value=await api(`/api/gateways/${encodeURIComponent(gateway.id)}`,{method:'DELETE'});gateways=value.gateways;resetGatewayForm();renderGateways();showNotice(value.message)}catch(error){showNotice(error.message,true)}}));row.append(activeCell,cell(gateway.alias),statusCell,actions);body.append(row)}renderConnection();renderRun()}
function renderConnection(){const active=gateways.find(item=>item.active);const ready=Boolean(active&&active.configured);document.getElementById('gateway-status').textContent=active?(ready?active.alias:`${active.alias} · identifiants manquants`):'Absente';document.getElementById('gateway-dot').className='status-dot '+(ready?'ready':'warning');document.getElementById('run-gateway-name').textContent=active?active.alias:'Aucune';let label='Cache local',css='warning';if(databaseState.connected){label='Supabase connectée';css='ready'}else if(databaseState.configured){label='Connexion requise'}document.getElementById('database-status').textContent=label;document.getElementById('database-dot').className='status-dot '+css}
function renderRun(){const running=Boolean(runState.running);const stopping=Boolean(runState.stop_requested);let text='En attente';if(running){const planned=runState.planned_repetitions===null?'continu':`${runState.planned_repetitions} répétition(s)`;text=`${stopping?'Arrêt demandé':'Exécution en cours'} · ${runState.completed_visits||0} visite(s) · ${planned}`}document.getElementById('run-status').lastChild.textContent=text;document.getElementById('run-dot').className='status-dot '+(running?'running':'idle');const selected=targets.filter(item=>item.selected).length;const active=gateways.find(item=>item.active&&item.configured);document.getElementById('run-button').disabled=running||selected===0||!active;document.getElementById('stop-button').disabled=!running||stopping;document.querySelectorAll('[data-config]').forEach(node=>{node.disabled=running});document.querySelectorAll('#target-rows input,#target-rows button,#gateway-rows button').forEach(node=>{node.disabled=running});updateResultSelection()}
function resultLabel(report,result){if(report.execution_mode==='continuous')return `cycle ${result.continuous_window||''} · ${result.window_repetition_count||''}`;return `${result.repetition||''} / ${report.repetition_count??''}`}
function resultSelector(runId,visitNumber=null){const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.className='result-selector';checkbox.dataset.runId=runId;checkbox.dataset.kind=visitNumber===null?'run':'visit';if(visitNumber!==null)checkbox.dataset.visitNumber=String(visitNumber);checkbox.addEventListener('change',updateResultSelection);return checkbox}
function renderReports(){const body=document.getElementById('result-rows');body.replaceChildren();let visitCount=0;let emptyRunCount=0;for(const report of [...reports].reverse()){const runId=String(report.run_id||'');const results=Array.isArray(report.results)?report.results:[];if(!results.length){emptyRunCount++;const row=document.createElement('tr');const selectCell=document.createElement('td');selectCell.append(resultSelector(runId));const outcome=document.createElement('td');const badge=document.createElement('span');badge.className='badge '+(report.in_progress?'active-badge':report.success?'success':report.error?'failure':'missing-badge');badge.textContent=report.in_progress?'EN COURS':report.success?'TERMINÉE':report.error?'ÉCHEC':'SANS VISITE';outcome.append(badge);const actions=document.createElement('td');actions.className='row-actions';actions.append(actionButton('Exécution','danger',()=>deleteRuns([runId])));row.append(selectCell,cell(report.started_at||''),cell('—'),cell(String(report.source||'')+(report.in_progress?' · EN COURS':'')),cell(report.execution_mode||'—'),cell('—'),cell('—'),outcome,cell(report.error||'','url-cell'),actions);body.append(row);continue}for(const result of [...results].reverse()){visitCount++;const row=document.createElement('tr');const selectCell=document.createElement('td');selectCell.append(resultSelector(runId,Number(result.visit_number)));const source=String(report.source||'')+(report.in_progress?' · EN COURS':'');const outcome=document.createElement('td');const badge=document.createElement('span');badge.className='badge '+(result.success?'success':'failure');badge.textContent=result.success?'SUCCÈS':'ÉCHEC';outcome.append(badge);const actions=document.createElement('td');actions.className='row-actions';actions.append(actionButton('Visite','danger',()=>deleteVisits([{run_id:runId,visit_number:Number(result.visit_number)}])),actionButton('Exécution','danger',()=>deleteRuns([runId])));row.append(selectCell,cell(result.started_at||''),cell(result.target_alias||result.target_id||'—'),cell(source),cell(resultLabel(report,result)),cell(result.initial_url||'','url-cell'),cell(result.final_url||'','url-cell'),outcome,cell(result.error||'','url-cell'),actions);body.append(row)}}if(!visitCount&&!emptyRunCount){const row=document.createElement('tr');const empty=cell('Aucun résultat.','empty');empty.colSpan=10;row.append(empty);body.append(row)}const counts=[`${visitCount} visite(s)`];if(emptyRunCount)counts.push(`${emptyRunCount} exécution(s) sans visite`);document.getElementById('result-count').textContent=counts.join(' · ');document.getElementById('select-all-results').checked=false;updateResultSelection()}
function updateResultSelection(){const selectors=[...document.querySelectorAll('.result-selector')];const selected=selectors.filter(node=>node.checked).length;document.querySelectorAll('#result-rows input,#result-rows button').forEach(node=>{node.disabled=resultMutationPending});document.getElementById('select-all-results').disabled=resultMutationPending||selectors.length===0;document.getElementById('delete-selected-results').disabled=resultMutationPending||selected===0;document.getElementById('clear-results').disabled=resultMutationPending}
function setResultMutationPending(pending){resultMutationPending=pending;document.getElementById('results-panel').setAttribute('aria-busy',String(pending));updateResultSelection()}
async function deleteResultData(body,confirmation){if(!confirm(confirmation))return;setResultMutationPending(true);try{const value=await api('/api/results/delete',{method:'POST',body});reports=value.reports;databaseState=value.database||databaseState;renderReports();renderConnection();showNotice(value.message)}catch(error){showNotice(error.message,true)}finally{setResultMutationPending(false)}}
async function deleteVisits(visits){if(!visits.length)return;await deleteResultData({action:'selected',visits,run_ids:[]},`Supprimer ${visits.length} visite(s) de Supabase ?`)}
async function deleteRuns(runIds){if(!runIds.length)return;await deleteResultData({action:'selected',visits:[],run_ids:runIds},'Supprimer toute cette exécution et ses visites de Supabase ?')}
async function refreshState(){try{const value=await api('/api/state');runState=value.run;databaseState=value.database;reports=value.reports;gateways=value.gateways;if(!document.getElementById('target-id').value)targets=value.targets;renderTargets();renderGateways();renderReports();renderConnection();renderRun()}catch(error){showNotice(error.message,true)}}
document.getElementById('target-form').addEventListener('submit',async event=>{event.preventDefault();const id=document.getElementById('target-id').value;const payload={alias:document.getElementById('target-alias').value,url:document.getElementById('target-url').value,button_text:document.getElementById('target-button').value,second_button_text:document.getElementById('target-second-button').value,selected:document.getElementById('target-selected').checked};try{const value=await api(id?`/api/targets/${encodeURIComponent(id)}`:'/api/targets',{method:id?'PATCH':'POST',body:payload});targets=value.targets;resetTargetForm();renderTargets();showNotice(value.message)}catch(error){showNotice(error.message,true)}});document.getElementById('target-cancel').addEventListener('click',resetTargetForm);for(const [id,mode] of [['targets-all','all'],['targets-none','none']])document.getElementById(id).addEventListener('click',async()=>{try{const value=await api('/api/targets/selection',{method:'POST',body:{mode}});targets=value.targets;renderTargets();showNotice(value.message)}catch(error){showNotice(error.message,true)}});
document.getElementById('gateway-form').addEventListener('submit',async event=>{event.preventDefault();const id=document.getElementById('gateway-id').value;const wantsActive=document.getElementById('gateway-active').checked;const payload={alias:document.getElementById('gateway-alias').value,gateway:document.getElementById('gateway-secret').value,active:wantsActive};try{let value=await api(id?`/api/gateways/${encodeURIComponent(id)}`:'/api/gateways',{method:id?'PATCH':'POST',body:payload});gateways=value.gateways;if(id&&wantsActive&&!gateways.find(item=>item.id===id)?.active){value=await api(`/api/gateways/${encodeURIComponent(id)}/select`,{method:'POST',body:{}});gateways=value.gateways}resetGatewayForm();renderGateways();showNotice(value.message)}catch(error){showNotice(error.message,true)}});document.getElementById('gateway-cancel').addEventListener('click',resetGatewayForm);
document.getElementById('settings-form').addEventListener('submit',async event=>{event.preventDefault();const payload={execution_mode:document.querySelector('input[name="execution_mode"]:checked').value,repetition_count:Number(document.getElementById('repetition-count').value),repetition_min:Number(document.getElementById('repetition-min').value),repetition_max:Number(document.getElementById('repetition-max').value),interval_minutes:Number(document.getElementById('interval-minutes').value),schedule_enabled:document.getElementById('schedule-enabled').checked};try{const value=await api('/api/settings',{method:'POST',body:payload});showNotice(value.message)}catch(error){showNotice(error.message,true)}});
document.getElementById('run-button').addEventListener('click',async()=>{try{const value=await api('/api/run',{method:'POST',body:{}});showNotice(value.message);await refreshState()}catch(error){showNotice(error.message,true)}});document.getElementById('stop-button').addEventListener('click',async()=>{try{const value=await api('/api/stop',{method:'POST',body:{}});showNotice(value.message);await refreshState()}catch(error){showNotice(error.message,true)}});
document.getElementById('select-all-results').addEventListener('change',event=>{document.querySelectorAll('.result-selector').forEach(node=>{node.checked=event.target.checked});updateResultSelection()});document.getElementById('delete-selected-results').addEventListener('click',()=>{const selected=[...document.querySelectorAll('.result-selector:checked')];const visits=selected.filter(node=>node.dataset.kind==='visit').map(node=>({run_id:node.dataset.runId,visit_number:Number(node.dataset.visitNumber)}));const runIds=selected.filter(node=>node.dataset.kind==='run').map(node=>node.dataset.runId);deleteResultData({action:'selected',visits,run_ids:runIds},`Supprimer ${selected.length} élément(s) de Supabase ?`)});document.getElementById('clear-results').addEventListener('click',()=>deleteResultData({action:'all'},'Effacer définitivement tout l’historique Supabase ?'));
let pollInProgress=false;async function pollStatus(){if(pollInProgress||resultMutationPending)return;pollInProgress=true;try{const value=await api('/api/status');const wasRunning=Boolean(runState.running);runState=value.run;renderRun();if(runState.running||wasRunning)await refreshState()}catch(error){showNotice(error.message,true)}finally{pollInProgress=false}}
renderTargets();renderGateways();renderReports();renderConnection();renderRun();resetGatewayForm();goToPage(pages.includes(location.hash.slice(1))?location.hash.slice(1):'home',false);window.setInterval(pollStatus,3000);
</script>
</body>
</html>"""
    replacements = {
        "__MESSAGES__": "".join(
            f'<div class="notice error">{html.escape(message)}</div>'
            for message in messages
        ),
        "__DATABASE_CLASS__": database_class,
        "__DATABASE_STATUS__": html.escape(database_status),
        "__SELECTED_COUNT__": str(selected_count),
        "__TARGET_COUNT__": str(len(targets)),
        "__FIXED_CHECKED__": "checked" if execution_mode == "fixed" else "",
        "__RANGE_CHECKED__": "checked" if execution_mode == "range" else "",
        "__CONTINUOUS_CHECKED__": (
            "checked" if execution_mode == "continuous" else ""
        ),
        "__REPETITION_COUNT__": str(app_settings.get("repetition_count", 1)),
        "__REPETITION_MIN__": str(app_settings.get("repetition_min", 1)),
        "__REPETITION_MAX__": str(app_settings.get("repetition_max", 10)),
        "__INTERVAL__": str(app_settings.get("interval_minutes", 60)),
        "__SCHEDULE_CHECKED__": (
            "checked" if app_settings.get("schedule_enabled") else ""
        ),
        "__CSRF_JSON__": script_json(CSRF_TOKEN),
        "__TARGETS_JSON__": script_json(targets),
        "__GATEWAYS_JSON__": script_json(gateways),
        "__REPORTS_JSON__": script_json(reports),
        "__RUN_STATE_JSON__": script_json(state),
        "__DATABASE_STATE_JSON__": script_json(database_state),
    }
    marker_pattern = re.compile(
        "|".join(re.escape(marker) for marker in replacements)
    )
    return marker_pattern.sub(lambda match: replacements[match.group(0)], page)


scheduler = threading.Thread(target=scheduler_loop, daemon=True)
scheduler.start()


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
