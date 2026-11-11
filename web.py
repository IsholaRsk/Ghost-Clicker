"""Application web : cibles, passerelles, exécutions et résultats persistants."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import random
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
    validate_button_configuration,
    validate_public_url,
)

BASE_DIR = Path(__file__).resolve().parent
APP = Flask(__name__)
app = APP
CSRF_TOKEN = secrets.token_urlsafe(32)
STATE_LOCK = threading.Lock()
REPORTS_LOCK = threading.Lock()
DATABASE_LOCK = threading.Lock()
GATEWAY_SECRETS_LOCK = threading.RLock()
CAMPAIGN_LOCK = threading.RLock()
METRICS_LOCK = threading.RLock()
GATEWAY_SECRETS_CACHE: dict[str, str] | None = None
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
    "interval_campaign_active": False,
    "interval_campaign_total": 0,
    "interval_campaign_launched": 0,
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
    "interval_execution_count": 20,
    "interval_duration_days": 1,
    "interval_repetition_count": 1,
    "interval_campaign": {},
    "lifetime_successful_visits": 0,
    "lifetime_metrics_initialized": False,
    "redirect_success_migrated": False,
}
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
MAX_TARGET_ALIAS_LENGTH = 100
MAX_GATEWAYS = 20
MAX_INTERVAL_EXECUTIONS = 10_000
MAX_INTERVAL_DURATION_DAYS = 365
MAX_STORED_VISITS = 500


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


def normalized_gateway_secrets(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): secret.strip()
        for key, secret in value.items()
        if SAFE_IDENTIFIER_PATTERN.fullmatch(str(key))
        and isinstance(secret, str)
        and 0 < len(secret.strip()) <= 4_096
    }


def read_local_gateway_secrets() -> dict[str, str]:
    path = proxy_gateways_file_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return normalized_gateway_secrets(value)


def write_local_gateway_secrets(value: dict[str, str]) -> None:
    atomic_write_private_text(
        proxy_gateways_file_path(),
        json.dumps(value, ensure_ascii=False, indent=2),
    )


def read_gateway_secrets() -> dict[str, str]:
    """Charge Vault et conserve un cache privé pour l'exécution du navigateur."""

    global GATEWAY_SECRETS_CACHE
    with GATEWAY_SECRETS_LOCK:
        if GATEWAY_SECRETS_CACHE is not None:
            return dict(GATEWAY_SECRETS_CACHE)
        local_secrets = read_local_gateway_secrets()
        storage = supabase_storage()
        if storage is not None:
            try:
                persistent_secrets = normalized_gateway_secrets(
                    storage.load_gateway_secrets()
                )
                merged_secrets = {**persistent_secrets, **local_secrets}
                if merged_secrets != persistent_secrets:
                    storage.save_gateway_secrets(merged_secrets)
                database_success()
                GATEWAY_SECRETS_CACHE = merged_secrets
                try:
                    write_local_gateway_secrets(merged_secrets)
                except ConfigurationError:
                    # Vault reste la source durable ; ce cache est facultatif.
                    pass
                return dict(merged_secrets)
            except StorageError as exc:
                database_failure(exc)
        if not (
            os.getenv("SUPABASE_URL", "").strip()
            or os.getenv("SUPABASE_SECRET_KEY", "").strip()
        ):
            GATEWAY_SECRETS_CACHE = local_secrets
        return dict(local_secrets)


def write_gateway_secrets(value: dict[str, str]) -> None:
    """Enregistre dans Vault avant de mettre à jour le cache privé local."""

    global GATEWAY_SECRETS_CACHE
    normalized = normalized_gateway_secrets(value)
    if len(normalized) != len(value):
        raise ConfigurationError("Le registre de passerelles est invalide.")
    with GATEWAY_SECRETS_LOCK:
        storage = supabase_storage()
        database_requested = bool(
            os.getenv("SUPABASE_URL", "").strip()
            or os.getenv("SUPABASE_SECRET_KEY", "").strip()
        )
        if storage is None and database_requested:
            raise ConfigurationError(
                "Impossible de conserver les passerelles dans Supabase."
            )
        if storage is not None:
            try:
                storage.save_gateway_secrets(normalized)
                database_success()
            except StorageError as exc:
                database_failure(exc)
                raise ConfigurationError(
                    "Impossible de conserver les passerelles dans Supabase."
                ) from exc
            GATEWAY_SECRETS_CACHE = dict(normalized)
            try:
                write_local_gateway_secrets(normalized)
            except ConfigurationError:
                # L'absence du cache local ne remet pas en cause Vault.
                pass
            return
        write_local_gateway_secrets(normalized)
        GATEWAY_SECRETS_CACHE = dict(normalized)


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
    button_text, second_button_text = validate_button_configuration(
        str(value.get("button_text", "")),
        str(value.get("second_button_text", "")),
    )
    return {
        "id": identifier,
        "alias": validate_target_alias(str(value.get("alias", ""))),
        "url": validate_public_url(str(value.get("url", ""))),
        "button_text": button_text,
        "second_button_text": second_button_text,
        "selected": selected,
    }


def normalize_interval_campaign(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    campaign_id = str(value.get("id", "")).strip()
    if not SAFE_IDENTIFIER_PATTERN.fullmatch(campaign_id):
        return {}
    raw_schedule = value.get("scheduled_timestamps", [])
    if not isinstance(raw_schedule, list):
        return {}
    scheduled_timestamps: list[float] = []
    for raw_timestamp in raw_schedule[:MAX_INTERVAL_EXECUTIONS + 10]:
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            continue
        if timestamp > 0:
            scheduled_timestamps.append(timestamp)
    scheduled_timestamps.sort()
    total = bounded_integer(
        value.get("total_executions"),
        len(scheduled_timestamps),
        1,
        MAX_INTERVAL_EXECUTIONS,
    )
    launched = bounded_integer(
        value.get("launched_executions"),
        max(0, total - len(scheduled_timestamps)),
        0,
        total,
    )
    active = bool(value.get("active", False)) and bool(scheduled_timestamps)
    return {
        "id": campaign_id,
        "active": active,
        "started_at": str(value.get("started_at", ""))[:64],
        "ends_at": str(value.get("ends_at", ""))[:64],
        "total_executions": total,
        "launched_executions": launched,
        "scheduled_timestamps": scheduled_timestamps,
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
        "interval_execution_count": bounded_integer(
            value.get("interval_execution_count"),
            20,
            1,
            MAX_INTERVAL_EXECUTIONS,
        ),
        "interval_duration_days": bounded_integer(
            value.get("interval_duration_days"),
            1,
            1,
            MAX_INTERVAL_DURATION_DAYS,
        ),
        "interval_repetition_count": bounded_integer(
            value.get("interval_repetition_count"),
            1,
            1,
            MAX_CONFIGURED_REPETITIONS,
        ),
        "interval_campaign": normalize_interval_campaign(
            value.get("interval_campaign", {})
        ),
        "lifetime_successful_visits": bounded_integer(
            value.get("lifetime_successful_visits"),
            0,
            0,
            9_223_372_036_854_775_807,
        ),
        "lifetime_metrics_initialized": bool(
            value.get("lifetime_metrics_initialized", False)
        ),
        "redirect_success_migrated": bool(
            value.get("redirect_success_migrated", False)
        ),
    }


def calculate_earnings(successful_visits: int) -> int:
    """Calcule les gains en dollars à partir des blocs complets de 250 clics."""

    completed_blocks = max(0, int(successful_visits)) // 250
    return min(completed_blocks, 2) * 12 + max(completed_blocks - 2, 0) * 8


def settings_for_api(value: dict[str, Any]) -> dict[str, Any]:
    """Expose uniquement les réglages non sensibles nécessaires à React."""

    keys = (
        "schedule_enabled",
        "interval_minutes",
        "execution_mode",
        "repetition_count",
        "repetition_min",
        "repetition_max",
        "interval_execution_count",
        "interval_duration_days",
        "interval_repetition_count",
        "lifetime_successful_visits",
    )
    public_settings = {
        key: value.get(key, DEFAULT_APP_SETTINGS[key]) for key in keys
    }
    public_settings["earnings_balance"] = calculate_earnings(
        int(public_settings["lifetime_successful_visits"])
    )
    return public_settings


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


def is_successful_visit_result(result: dict[str, Any]) -> bool:
    """Écarte les anciens faux positifs sans redirection automatique."""

    if not bool(result.get("success")):
        return False
    initial_url = str(result.get("initial_url", "")).strip()
    final_url = str(result.get("final_url", "")).strip()
    button_text = str(result.get("button_text", "")).strip()
    if button_text or not initial_url:
        return True
    return bool(final_url and final_url != initial_url)


def count_successful_visits(reports: list[dict[str, Any]]) -> int:
    return sum(
        1
        for report in reports
        for result in report.get("results", [])
        if isinstance(result, dict) and is_successful_visit_result(result)
    )


def count_legacy_redirect_false_positives(
    reports: list[dict[str, Any]],
) -> int:
    return sum(
        1
        for report in reports
        for result in report.get("results", [])
        if isinstance(result, dict)
        and bool(result.get("success"))
        and not is_successful_visit_result(result)
    )


def initialize_lifetime_metrics(
    settings: dict[str, Any], reports: list[dict[str, Any]]
) -> dict[str, Any]:
    """Reprend l'historique et corrige les succès sans redirection une fois."""

    if settings.get("lifetime_metrics_initialized", False) and settings.get(
        "redirect_success_migrated", False
    ):
        return settings
    with METRICS_LOCK:
        current = load_app_settings()
        initialized = bool(current.get("lifetime_metrics_initialized", False))
        migrated = bool(current.get("redirect_success_migrated", False))
        if initialized and migrated:
            return current
        if not initialized:
            current["lifetime_successful_visits"] = count_successful_visits(
                reports
            )
            current["lifetime_metrics_initialized"] = True
        elif not migrated:
            false_positives = count_legacy_redirect_false_positives(reports)
            current["lifetime_successful_visits"] = max(
                0,
                int(current.get("lifetime_successful_visits", 0))
                - false_positives,
            )
        current["redirect_success_migrated"] = True
        try:
            save_app_settings(current)
        except (ConfigurationError, OSError):
            # Le tableau reste consultable pendant une indisponibilité du stockage.
            pass
        return current


def increment_lifetime_successful_visits(delta: int) -> int:
    """Ajoute atomiquement les visites réussies au compteur de rémunération."""

    if delta < 1:
        return 0
    with METRICS_LOCK:
        settings = load_app_settings()
        reports = load_reports(all_data=True)
        initialized = bool(settings.get("lifetime_metrics_initialized", False))
        if not initialized:
            # Le rapport de la visite courante a déjà été persisté : le
            # comptage initial la contient et il ne faut pas ajouter delta.
            settings["lifetime_successful_visits"] = count_successful_visits(
                reports
            )
            settings["lifetime_metrics_initialized"] = True
        elif not settings.get("redirect_success_migrated", False):
            false_positives = count_legacy_redirect_false_positives(reports)
            settings["lifetime_successful_visits"] = max(
                0,
                int(settings.get("lifetime_successful_visits", 0))
                - false_positives,
            )
        settings["redirect_success_migrated"] = True
        current = int(settings.get("lifetime_successful_visits", 0))
        increment = delta if initialized else 0
        updated = min(current + increment, 9_223_372_036_854_775_807)
        settings["lifetime_successful_visits"] = updated
        save_app_settings(settings)
        return updated


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


def retain_latest_visits(
    reports: list[dict[str, Any]], maximum: int = MAX_STORED_VISITS
) -> list[dict[str, Any]]:
    """Conserve les visites les plus récentes sans créer de faux rapports vides."""

    remaining = maximum
    retained_reversed: list[dict[str, Any]] = []
    for raw_report in reversed(reports):
        report = dict(raw_report)
        raw_results = report.get("results", [])
        results = raw_results if isinstance(raw_results, list) else []
        if results:
            if remaining <= 0:
                continue
            retained_results = results[-remaining:]
            report["results"] = retained_results
            remaining -= len(retained_results)
            retained_reversed.append(report)
            continue
        try:
            completed_visits = int(report.get("completed_visits", 0))
        except (TypeError, ValueError):
            completed_visits = 0
        if completed_visits <= 0:
            retained_reversed.append(report)
    retained_reversed.reverse()
    return retained_reversed[-200:]


def append_report(value: dict[str, Any]) -> None:
    report = dict(value)
    report.setdefault("run_id", secrets.token_urlsafe(12))
    with REPORTS_LOCK:
        reports = read_reports_unlocked()
        reports.append(report)
        atomic_write_json(reports_path(), retain_latest_visits(reports))
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
        atomic_write_json(reports_path(), retain_latest_visits(reports))
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
        button_text, second_button_text = validate_button_configuration(
            str(item.get("button_text", "")),
            str(item.get("second_button_text", "")),
        )
        targets.append(
            Target(
                url=validate_public_url(str(item.get("url", ""))),
                button_text=button_text,
                second_button_text=second_button_text,
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
        if len(parts) not in {1, 2, 3}:
            raise ConfigurationError(
                f"Ligne {line_number} : utilisez le format "
                "URL [| premier libellé [| second libellé]]."
            )
        url = parts[0]
        raw_button_text = parts[1] if len(parts) >= 2 else ""
        raw_second_button_text = parts[2] if len(parts) == 3 else ""
        button_text, second_button_text = validate_button_configuration(
            raw_button_text, raw_second_button_text
        )
        targets.append(
            Target(
                url=validate_public_url(url),
                button_text=button_text,
                second_button_text=second_button_text,
            )
        )
    if len(targets) > 50:
        raise ConfigurationError("Maximum : 50 URL.")
    return targets


def targets_to_text(value: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in value.get("targets", []):
        if isinstance(item, dict):
            line = str(item.get("url", ""))
            button_text = str(item.get("button_text", "")).strip()
            second_button_text = str(item.get("second_button_text", "")).strip()
            if button_text:
                line += f" | {button_text}"
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


def execute_job(
    source: str,
    execution_mode_override: str | None = None,
    repetition_count_override: int | None = None,
) -> None:
    run_id = ""
    try:
        app_settings = load_app_settings()
        sync_active_gateway(app_settings)
        browser_settings = load_browser_settings(BASE_DIR)
        targets = targets_from_settings(app_settings)
        if not targets:
            raise ConfigurationError("Aucune URL n'est sélectionnée.")

        execution_mode = execution_mode_override or str(
            app_settings["execution_mode"]
        )
        repetition_count = repetition_count_override
        if repetition_count is None:
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

        metrics_processed_visits = 0

        def update_progress(report: RunReport) -> None:
            nonlocal metrics_processed_visits
            with STATE_LOCK:
                RUN_STATE.update(
                    completed_repetitions=report.completed_repetitions,
                    completed_visits=report.completed_visits,
                    continuous_window=report.continuous_window,
                    window_repetition_count=report.window_repetition_count,
                )
            # Le rapport persistant reste la source de vérité et devient
            # disponible avant la mise à jour du solde.
            upsert_report(report_value(report, in_progress=True))
            if report.completed_visits > metrics_processed_visits:
                first_available_visit = report.discarded_results + 1
                successful_delta = sum(
                    1
                    for visit_number, result in enumerate(
                        report.results, start=first_available_visit
                    )
                    if visit_number > metrics_processed_visits and result.success
                )
                try:
                    if successful_delta:
                        increment_lifetime_successful_visits(successful_delta)
                except (ConfigurationError, OSError):
                    # Le rapport reste prioritaire ; le compteur sera retenté
                    # au prochain progrès tant que les résultats sont disponibles.
                    pass
                else:
                    metrics_processed_visits = report.completed_visits

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


def start_job(
    source: str,
    *,
    execution_mode_override: str | None = None,
    repetition_count_override: int | None = None,
) -> bool:
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
            execution_mode=execution_mode_override or "fixed",
            planned_repetitions=(
                repetition_count_override
                if repetition_count_override is not None
                else 1
            ),
            completed_repetitions=0,
            completed_visits=0,
            continuous_window=0,
            window_repetition_count=0,
        )
    thread = threading.Thread(
        target=execute_job,
        args=(source, execution_mode_override, repetition_count_override),
        daemon=True,
    )
    thread.start()
    return True


def build_interval_campaign(
    requested_executions: int,
    duration_days: int,
    *,
    now_timestamp: float | None = None,
) -> dict[str, Any]:
    if not 1 <= requested_executions <= MAX_INTERVAL_EXECUTIONS:
        raise ConfigurationError(
            f"Le nombre d'exécutions doit être compris entre 1 et "
            f"{MAX_INTERVAL_EXECUTIONS}."
        )
    if not 1 <= duration_days <= MAX_INTERVAL_DURATION_DAYS:
        raise ConfigurationError(
            f"La durée doit être comprise entre 1 et "
            f"{MAX_INTERVAL_DURATION_DAYS} jours."
        )
    started_timestamp = time.time() if now_timestamp is None else now_timestamp
    total_executions = random.randint(
        max(1, requested_executions - 10),
        min(MAX_INTERVAL_EXECUTIONS, requested_executions + 10),
    )
    duration_seconds = duration_days * 24 * 60 * 60
    scheduled_timestamps = sorted(
        started_timestamp + random.uniform(0, duration_seconds)
        for _index in range(total_executions)
    )
    ends_timestamp = started_timestamp + duration_seconds
    return {
        "id": f"campaign_{secrets.token_urlsafe(12)}",
        "active": True,
        "started_at": datetime.fromtimestamp(started_timestamp)
        .astimezone()
        .isoformat(timespec="seconds"),
        "ends_at": datetime.fromtimestamp(ends_timestamp)
        .astimezone()
        .isoformat(timespec="seconds"),
        "total_executions": total_executions,
        "launched_executions": 0,
        "scheduled_timestamps": scheduled_timestamps,
    }


def update_interval_campaign_state(campaign: dict[str, Any]) -> None:
    schedule = campaign.get("scheduled_timestamps", [])
    next_timestamp = schedule[0] if campaign.get("active") and schedule else None
    with STATE_LOCK:
        RUN_STATE.update(
            interval_campaign_active=bool(campaign.get("active", False)),
            interval_campaign_total=int(campaign.get("total_executions", 0)),
            interval_campaign_launched=int(
                campaign.get("launched_executions", 0)
            ),
            next_run_at=(
                datetime.fromtimestamp(float(next_timestamp))
                .astimezone()
                .isoformat(timespec="seconds")
                if next_timestamp is not None
                else ""
            ),
        )


def start_interval_campaign() -> dict[str, Any]:
    with CAMPAIGN_LOCK:
        with STATE_LOCK:
            if RUN_STATE["running"]:
                raise ConfigurationError("Une exécution est déjà en cours.")
        settings = load_app_settings()
        active_campaign = settings.get("interval_campaign", {})
        if isinstance(active_campaign, dict) and active_campaign.get("active"):
            raise ConfigurationError("Une campagne Intervalle est déjà active.")
        campaign = build_interval_campaign(
            int(settings["interval_execution_count"]),
            int(settings["interval_duration_days"]),
        )
        settings["interval_campaign"] = campaign
        save_app_settings(settings)
        update_interval_campaign_state(campaign)
    record_database_event(
        "interval_campaign_started",
        {
            "campaign_id": campaign["id"],
            "requested_executions": settings["interval_execution_count"],
            "total_executions": campaign["total_executions"],
            "duration_days": settings["interval_duration_days"],
            "repetitions_per_execution": settings[
                "interval_repetition_count"
            ],
            "started_at": campaign["started_at"],
            "ends_at": campaign["ends_at"],
        },
    )
    return campaign


def process_interval_campaign(now_timestamp: float | None = None) -> bool:
    """Lance au plus une exécution arrivée à échéance."""

    current_timestamp = time.time() if now_timestamp is None else now_timestamp
    with CAMPAIGN_LOCK:
        settings = load_app_settings()
        campaign = settings.get("interval_campaign", {})
        if not isinstance(campaign, dict) or not campaign.get("active"):
            update_interval_campaign_state({})
            return False
        schedule = list(campaign.get("scheduled_timestamps", []))
        if not schedule:
            campaign["active"] = False
            settings["interval_campaign"] = campaign
            save_app_settings(settings)
            update_interval_campaign_state(campaign)
            return False
        update_interval_campaign_state(campaign)
        if float(schedule[0]) > current_timestamp:
            return False
        with STATE_LOCK:
            if RUN_STATE["running"]:
                return False
        execution_number = int(campaign.get("launched_executions", 0)) + 1
        total_executions = int(campaign.get("total_executions", len(schedule)))
        due_timestamp = schedule.pop(0)
        campaign["scheduled_timestamps"] = schedule
        campaign["launched_executions"] = execution_number
        campaign["active"] = bool(schedule)
        settings["interval_campaign"] = campaign
        save_app_settings(settings)
        started = start_job(
            f"intervalle {execution_number}/{total_executions}",
            execution_mode_override="range",
            repetition_count_override=int(settings["interval_repetition_count"]),
        )
        if not started:
            schedule.insert(0, due_timestamp)
            campaign["scheduled_timestamps"] = schedule
            campaign["launched_executions"] = execution_number - 1
            campaign["active"] = True
            settings["interval_campaign"] = campaign
            save_app_settings(settings)
            update_interval_campaign_state(campaign)
            return False
        update_interval_campaign_state(campaign)
    record_database_event(
        "interval_execution_started",
        {
            "campaign_id": campaign.get("id", ""),
            "execution_number": execution_number,
            "total_executions": total_executions,
            "scheduled_at": datetime.fromtimestamp(float(due_timestamp))
            .astimezone()
            .isoformat(timespec="seconds"),
            "started_at": now_iso(),
        },
    )
    return True


def request_job_stop() -> bool:
    campaign_cancelled = False
    with CAMPAIGN_LOCK:
        settings = load_app_settings()
        campaign = settings.get("interval_campaign", {})
        if isinstance(campaign, dict) and campaign.get("active"):
            campaign["active"] = False
            campaign["scheduled_timestamps"] = []
            settings["interval_campaign"] = campaign
            save_app_settings(settings)
            update_interval_campaign_state(campaign)
            campaign_cancelled = True
    with STATE_LOCK:
        running = bool(RUN_STATE["running"])
        if running:
            RUN_STATE["stop_requested"] = True
            STOP_EVENT.set()
    return running or campaign_cancelled


def scheduler_loop() -> None:
    next_run_timestamp: float | None = None
    while True:
        app_settings = load_app_settings()
        campaign = app_settings.get("interval_campaign", {})
        if isinstance(campaign, dict) and campaign.get("active"):
            next_run_timestamp = None
            process_interval_campaign()
            time.sleep(15)
            continue

        enabled = bool(app_settings.get("schedule_enabled", False)) and str(
            app_settings.get("execution_mode", "fixed")
        ) != "range"
        interval = max(60, int(app_settings.get("interval_minutes", 60)))
        now = time.time()

        if not enabled:
            next_run_timestamp = None
            with STATE_LOCK:
                if not RUN_STATE.get("interval_campaign_active"):
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
    settings = load_app_settings()
    campaign = settings.get("interval_campaign", {})
    if isinstance(campaign, dict) and campaign.get("active"):
        raise ConfigurationError(
            "Arrêtez la campagne Intervalle avant cette modification."
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
            if result.get("success") and not is_successful_visit_result(result):
                result["success"] = False
                if not result.get("error"):
                    result["error"] = (
                        "Aucune redirection ni nouvel onglet n'a été détecté."
                    )
            report["results"].append(result)
        normalized.append(report)
    return normalized


def api_snapshot(*, include_targets: bool = True) -> dict[str, Any]:
    stored_reports = load_reports()
    settings = load_app_settings() if include_targets else None
    if settings is not None:
        settings = initialize_lifetime_metrics(settings, stored_reports)
    reports = reports_for_api(stored_reports)
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
        value["settings"] = settings_for_api(settings)
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
        interval_execution_count = int(
            payload.get("interval_execution_count", 20)
        )
        interval_duration_days = int(payload.get("interval_duration_days", 1))
        interval_repetition_count = int(
            payload.get("interval_repetition_count", 1)
        )
        if execution_mode not in EXECUTION_MODES:
            raise ConfigurationError("Le mode d'exécution est invalide.")
        if execution_mode != "range":
            resolve_repetition_count(
                execution_mode,
                repetition_count,
                repetition_min,
                repetition_max,
            )
        if not 1 <= interval_execution_count <= MAX_INTERVAL_EXECUTIONS:
            raise ConfigurationError(
                f"Le nombre d'exécutions doit être compris entre 1 et "
                f"{MAX_INTERVAL_EXECUTIONS}."
            )
        if not 1 <= interval_duration_days <= MAX_INTERVAL_DURATION_DAYS:
            raise ConfigurationError(
                f"La durée doit être comprise entre 1 et "
                f"{MAX_INTERVAL_DURATION_DAYS} jours."
            )
        if not 1 <= interval_repetition_count <= MAX_CONFIGURED_REPETITIONS:
            raise ConfigurationError(
                "Le nombre de répétitions par exécution est invalide."
            )
        current.update(
            schedule_enabled=(
                bool(payload.get("schedule_enabled", False))
                and execution_mode != "range"
            ),
            interval_minutes=interval,
            execution_mode=execution_mode,
            repetition_count=repetition_count,
            repetition_min=repetition_min,
            repetition_max=repetition_max,
            interval_execution_count=interval_execution_count,
            interval_duration_days=interval_duration_days,
            interval_repetition_count=interval_repetition_count,
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
                "interval_execution_count": interval_execution_count,
                "interval_duration_days": interval_duration_days,
                "interval_repetition_count": interval_repetition_count,
                "updated_at": now_iso(),
            },
        )
        return jsonify(
            {
                "ok": True,
                "message": "Paramètres enregistrés.",
                "settings": settings_for_api(current),
            }
        )
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
        if app_settings.get("execution_mode") == "range":
            campaign = start_interval_campaign()
            return jsonify(
                {
                    "ok": True,
                    "message": (
                        f"Campagne démarrée : {campaign['total_executions']} "
                        "exécution(s) répartie(s) aléatoirement."
                    ),
                }
            )
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
                "message": "Arrêt pris en compte. La visite en cours se terminera proprement et toute campagne active est annulée.",
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
        require_idle_configuration()
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
            interval_execution_count = int(
                request.form.get("interval_execution_count", "20").strip()
            )
            interval_duration_days = int(
                request.form.get("interval_duration_days", "1").strip()
            )
            interval_repetition_count = int(
                request.form.get("interval_repetition_count", "1").strip()
            )
        except ValueError as exc:
            raise ConfigurationError(
                "Les paramètres d'exécution doivent être des nombres entiers."
            ) from exc
        if execution_mode not in EXECUTION_MODES:
            raise ConfigurationError("Le mode d'exécution est invalide.")
        if execution_mode != "range":
            resolve_repetition_count(
                execution_mode,
                repetition_count,
                repetition_min,
                repetition_max,
            )
        if not 1 <= interval_execution_count <= MAX_INTERVAL_EXECUTIONS:
            raise ConfigurationError("Le nombre d'exécutions est invalide.")
        if not 1 <= interval_duration_days <= MAX_INTERVAL_DURATION_DAYS:
            raise ConfigurationError("La durée en jours est invalide.")
        if not 1 <= interval_repetition_count <= MAX_CONFIGURED_REPETITIONS:
            raise ConfigurationError(
                "Le nombre de répétitions par exécution est invalide."
            )

        schedule_enabled = (
            request.form.get("schedule_enabled") == "on"
            and execution_mode != "range"
        )
        current.update(
            targets=target_records,
            schedule_enabled=schedule_enabled,
            interval_minutes=interval,
            execution_mode=execution_mode,
            repetition_count=repetition_count,
            repetition_min=repetition_min,
            repetition_max=repetition_max,
            interval_execution_count=interval_execution_count,
            interval_duration_days=interval_duration_days,
            interval_repetition_count=interval_repetition_count,
        )
        save_app_settings(current)
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
                "interval_execution_count": interval_execution_count,
                "interval_duration_days": interval_duration_days,
                "interval_repetition_count": interval_repetition_count,
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
        if app_settings.get("execution_mode") == "range":
            start_interval_campaign()
            started = True
        else:
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
    for report in reports_for_api(load_reports(all_data=True)):
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
    stored_reports = load_reports()
    app_settings = initialize_lifetime_metrics(app_settings, stored_reports)
    reports = reports_for_api(stored_reports)
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

    def script_json(value: Any) -> str:
        return (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )

    bootstrap = {
        "csrfToken": CSRF_TOKEN,
        "messages": messages,
        "targets": app_settings.get("targets", []),
        "gateways": gateways,
        "reports": reports,
        "run": state,
        "database": database_state,
        "settings": settings_for_api(app_settings),
    }
    bootstrap_json = script_json(bootstrap)
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#09090b">
<title>Ghost Clicker</title>
<link rel="stylesheet" href="/static/app.css?v=1">
</head>
<body>
<div id="root"></div>
<script id="ghost-clicker-bootstrap" type="application/json">{bootstrap_json}</script>
<script src="/static/app.js?v=1" defer></script>
</body>
</html>"""


scheduler = threading.Thread(target=scheduler_loop, daemon=True)
scheduler.start()


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
