"""Interface web Docker : paramètres éphémères, exécution manuelle et planifiée."""

from __future__ import annotations

import csv
import hmac
import html
import io
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, redirect, request, url_for

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
STOP_EVENT = threading.Event()
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
}

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "targets": [],
    "schedule_enabled": False,
    "interval_minutes": 60,
    "execution_mode": "fixed",
    "repetition_count": 1,
    "repetition_min": 1,
    "repetition_max": 10,
}


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


def bounded_integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def load_app_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return dict(DEFAULT_APP_SETTINGS)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_APP_SETTINGS)
    if not isinstance(value, dict):
        return dict(DEFAULT_APP_SETTINGS)

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
        "targets": value.get("targets", []),
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


def save_app_settings(value: dict[str, Any]) -> None:
    atomic_write_json(settings_path(), value)


def read_reports_unlocked() -> list[dict[str, Any]]:
    path = reports_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def load_reports() -> list[dict[str, Any]]:
    with REPORTS_LOCK:
        return read_reports_unlocked()


def append_report(value: dict[str, Any]) -> None:
    with REPORTS_LOCK:
        reports = read_reports_unlocked()
        reports.append(value)
        atomic_write_json(reports_path(), reports[-200:])


def upsert_report(value: dict[str, Any]) -> None:
    """Crée ou remplace le rapport d'une exécution en cours."""

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
        targets.append(
            Target(
                url=validate_public_url(str(item.get("url", ""))),
                button_text=validate_button_text(str(item.get("button_text", ""))),
                second_button_text=validate_optional_button_text(
                    str(item.get("second_button_text", ""))
                ),
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


def require_csrf() -> None:
    supplied = request.form.get("csrf_token", "")
    if not hmac.compare_digest(supplied, CSRF_TOKEN):
        raise ConfigurationError("Jeton de formulaire invalide. Rechargez la page.")


def read_browser_configuration() -> tuple[BrowserSettings | None, str]:
    try:
        return load_browser_settings(BASE_DIR), ""
    except ConfigurationError as exc:
        return None, str(exc)


def execute_job(source: str) -> None:
    try:
        browser_settings = load_browser_settings(BASE_DIR)
        app_settings = load_app_settings()
        targets = targets_from_settings(app_settings)
        if not targets:
            raise ConfigurationError("Aucune URL n'est enregistrée.")

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
                )
            # Le CSV et le tableau deviennent disponibles après chaque visite.
            upsert_report(report_value(report, in_progress=True))

        report = run_targets(
            browser_settings,
            targets,
            repetition_count=repetition_count,
            execution_mode=execution_mode,
            should_stop=STOP_EVENT.is_set,
            on_progress=update_progress,
        )
        upsert_report(report_value(report, in_progress=False))
        with STATE_LOCK:
            RUN_STATE["last_error"] = report.error
    except Exception as exc:
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


@APP.get("/health")
def health() -> Response:
    _settings, error = read_browser_configuration()
    status = "degraded" if error else "ok"
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
        content = request.form.get("gateway", "").strip()
        if not content:
            raise ConfigurationError("Saisissez la passerelle proxy.")
        if len(content) > 4_096:
            raise ConfigurationError("La passerelle proxy est trop longue.")
        parse_proxy_gateway_text(content)
        atomic_write_private_text(proxy_gateway_file_path(), f"{content}\n")
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
        return redirect(url_for("home", targets=str(len(targets))), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/settings")
def update_settings() -> Response:
    try:
        require_csrf()
        targets = parse_targets_text(request.form.get("targets", ""))
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

        save_app_settings(
            {
                "targets": [
                    {
                        "url": target.url,
                        "button_text": target.button_text,
                        "second_button_text": target.second_button_text,
                    }
                    for target in targets
                ],
                "schedule_enabled": request.form.get("schedule_enabled") == "on",
                "interval_minutes": interval,
                "execution_mode": execution_mode,
                "repetition_count": repetition_count,
                "repetition_min": repetition_min,
                "repetition_max": repetition_max,
            }
        )
        return redirect(url_for("home", saved="1"), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/run")
def run_now() -> Response:
    try:
        require_csrf()
        load_browser_settings(BASE_DIR)
        targets = targets_from_settings(load_app_settings())
        if not targets:
            raise ConfigurationError("Enregistrez au moins une URL.")
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
        return redirect(
            url_for("home", stopped="1" if requested else "0"), code=303
        )
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.post("/clear-results")
def clear_results() -> Response:
    try:
        require_csrf()
        path = reports_path()
        with REPORTS_LOCK:
            if path.exists():
                path.unlink()
        return redirect(url_for("home", cleared="1"), code=303)
    except ConfigurationError as exc:
        return redirect(url_for("home", error=str(exc)), code=303)


@APP.get("/results.csv")
def download_results() -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "source",
            "date",
            "mode_execution",
            "repetition",
            "repetitions_planifiees",
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
    for report in load_reports():
        for result in report.get("results", []):
            writer.writerow(
                [
                    report.get("source", ""),
                    result.get("started_at", ""),
                    report.get("execution_mode", "fixed"),
                    result.get("repetition", ""),
                    report.get("repetition_count", ""),
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


def render_result_rows(reports: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for report in reversed(reports):
        total = report.get("repetition_count")
        total_label = (
            "continu"
            if report.get("execution_mode") == "continuous" or total is None
            else str(total)
        )
        source_label = str(report.get("source", ""))
        if report.get("in_progress"):
            source_label = f"{source_label} · EN COURS"
        for result in reversed(report.get("results", [])):
            success = bool(result.get("success"))
            css_class = "success" if success else "failure"
            label = "SUCCÈS" if success else "ÉCHEC"
            rows.append(
                f"""
                <tr>
                  <td>{html.escape(str(result.get('started_at', '')))}</td>
                  <td>{html.escape(source_label)}</td>
                  <td>{html.escape(str(result.get('repetition', '')))} / {html.escape(total_label)}</td>
                  <td>{html.escape(str(result.get('proxy_name', '')))}</td>
                  <td class="url">{html.escape(str(result.get('initial_url', '')))}</td>
                  <td>{html.escape(str(result.get('button_text', '')))}</td>
                  <td>{html.escape(str(result.get('second_button_text', '')))}</td>
                  <td class="url">{html.escape(str(result.get('final_url', '')))}</td>
                  <td><span class="badge {css_class}">{label}</span></td>
                  <td class="error">{html.escape(str(result.get('error', '')))}</td>
                </tr>"""
            )
    return "".join(rows) or '<tr><td colspan="10" class="empty">Aucun résultat.</td></tr>'


@APP.get("/")
def home() -> str:
    app_settings = load_app_settings()
    _browser_settings, configuration_error = read_browser_configuration()
    reports = load_reports()
    with STATE_LOCK:
        state = dict(RUN_STATE)

    messages: list[str] = []
    if request.args.get("saved") == "1":
        messages.append("Paramètres enregistrés.")
    if request.args.get("gateway") == "1":
        messages.append("Passerelle proxy rotative enregistrée.")
    if request.args.get("targets"):
        messages.append(f"{request.args['targets']} cible(s) importée(s).")
    if request.args.get("started") == "1":
        messages.append("Exécution démarrée.")
    if request.args.get("started") == "0":
        messages.append("Une exécution est déjà en cours.")
    if request.args.get("stopped") == "1":
        messages.append("Arrêt demandé. La visite en cours va se terminer proprement.")
    if request.args.get("stopped") == "0":
        messages.append("Aucune exécution n'est en cours.")
    if request.args.get("cleared") == "1":
        messages.append("Résultats effacés.")
    if request.args.get("error"):
        messages.append(request.args["error"][:500])
    if configuration_error:
        messages.append(f"Configuration serveur : {configuration_error}")
    if state["last_error"]:
        messages.append(f"Dernière erreur : {state['last_error']}")

    if state["running"]:
        status_prefix = (
            "Arrêt demandé" if state["stop_requested"] else "Exécution en cours"
        )
        planned = state.get("planned_repetitions")
        plan_label = "continu" if planned is None else f"{planned} répétition(s)"
        status = (
            f"{status_prefix} · {state['completed_visits']} visite(s) · "
            f"{plan_label}"
        )
    else:
        status = "En attente"
    status_class = "running" if state["running"] else "idle"
    refresh = '<meta http-equiv="refresh" content="5">' if state["running"] else ""
    run_disabled = " disabled" if state["running"] or configuration_error else ""
    stop_disabled = (
        " disabled"
        if not state["running"] or state["stop_requested"]
        else ""
    )
    edit_disabled = " disabled" if state["running"] else ""
    message_html = "".join(
        f'<div class="notice">{html.escape(message)}</div>' for message in messages
    )
    checked = " checked" if app_settings.get("schedule_enabled") else ""
    targets_text = html.escape(targets_to_text(app_settings))
    interval = int(app_settings.get("interval_minutes", 60))
    execution_mode = str(app_settings.get("execution_mode", "fixed"))
    repetition_count = int(app_settings.get("repetition_count", 1))
    repetition_min = int(app_settings.get("repetition_min", 1))
    repetition_max = int(app_settings.get("repetition_max", 10))
    fixed_checked = " checked" if execution_mode == "fixed" else ""
    range_checked = " checked" if execution_mode == "range" else ""
    continuous_checked = " checked" if execution_mode == "continuous" else ""
    if execution_mode == "continuous":
        execution_summary = "Mode continu, jusqu'à l'arrêt manuel"
    elif execution_mode == "range":
        execution_summary = (
            f"Intervalle de {repetition_min} à {repetition_max} répétitions"
        )
    else:
        execution_summary = f"{repetition_count} répétition(s)"
    gateway_ready = proxy_gateway_file_path().exists()
    gateway_status = "Configurée" if gateway_ready else "Absente"
    gateway_class = "ready" if gateway_ready else "warning"
    target_count = len(app_settings.get("targets", []))

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>Ghost Clicker — Console</title>
<style>
:root{{color-scheme:dark;--canvas:#0d0f12;--surface:#15181c;--surface-soft:#1a1e23;--ink:#f2f4f7;--muted:#9ba3ad;--line:#292e35;--line-strong:#3c434d;--accent:#f2f4f7;--success:#58c68a;--success-soft:#14271d;--warning:#f0b84b;--warning-soft:#2a2212;--danger:#ff8b81;--danger-soft:#2b1818;--focus:#7e9cc8}}
*{{box-sizing:border-box}}
html{{background:var(--canvas)}}
body{{margin:0;min-height:100vh;background:var(--canvas);color:var(--ink);font:14px/1.55 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
a{{color:inherit}}
.shell{{width:min(1160px,calc(100% - 40px));margin:0 auto}}
.topbar{{background:rgba(13,15,18,.92);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20;backdrop-filter:blur(10px)}}
.topbar-inner{{height:64px;display:flex;align-items:center;justify-content:space-between;gap:24px}}
.brand{{display:inline-flex;align-items:center;gap:11px;text-decoration:none;font-size:15px;font-weight:680;letter-spacing:-.01em}}
.brand-mark{{width:28px;height:28px;display:grid;place-items:center;background:var(--ink);color:var(--canvas);border-radius:7px;font-size:12px;font-weight:800}}
.topbar-note{{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}}
.service-dot{{width:7px;height:7px;border-radius:50%;background:var(--success);box-shadow:0 0 0 3px var(--success-soft)}}
main{{padding:52px 0 64px}}
.hero{{display:flex;align-items:flex-end;justify-content:space-between;gap:40px;margin-bottom:30px}}
.eyebrow{{display:block;margin-bottom:9px;color:var(--muted);font-size:11px;font-weight:750;letter-spacing:.13em;text-transform:uppercase}}
h1,h2,p{{margin-top:0}}
h1{{margin-bottom:12px;font-size:clamp(30px,5vw,46px);line-height:1.08;letter-spacing:-.045em;font-weight:720}}
h2{{margin-bottom:0;font-size:18px;line-height:1.3;letter-spacing:-.02em;font-weight:680}}
.hero-copy{{max-width:680px;margin-bottom:0;color:var(--muted);font-size:15px}}
.hero-facts{{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;max-width:330px}}
.fact{{border:1px solid var(--line);background:var(--surface);padding:7px 10px;border-radius:999px;color:var(--muted);font-size:11px;white-space:nowrap}}
.notices{{display:grid;gap:8px;margin-bottom:18px}}
.notice{{padding:11px 13px;border:1px solid #2b3b4d;border-left:3px solid #7893b3;border-radius:8px;background:#151d27;color:#c6d2df}}
.status-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:20px}}
.stat{{min-width:0;padding:16px 18px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}}
.stat-label{{display:block;margin-bottom:8px;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}}
.stat-value{{display:flex;align-items:center;gap:9px;margin:0;font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.stat-value time{{overflow:hidden;text-overflow:ellipsis}}
.status-dot{{width:8px;height:8px;flex:0 0 auto;border-radius:50%}}
.status-dot.idle,.status-dot.ready{{background:var(--success)}}
.status-dot.running{{background:#7da6e3;animation:pulse 1.7s ease-in-out infinite}}
.status-dot.warning{{background:var(--warning)}}
@keyframes pulse{{50%{{opacity:.38}}}}
.panel{{margin-bottom:16px;border:1px solid var(--line);border-radius:12px;background:var(--surface);overflow:hidden}}
.panel-header{{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;padding:20px 22px 17px;border-bottom:1px solid var(--line)}}
.panel-heading p{{margin:5px 0 0;color:var(--muted);font-size:13px}}
.count{{flex:0 0 auto;padding:5px 9px;border-radius:999px;background:var(--surface-soft);border:1px solid var(--line);color:var(--muted);font-size:11px}}
.panel-body{{padding:22px}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}
.content-grid{{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(340px,.92fr);gap:16px;align-items:start}}
.content-grid .panel{{height:100%}}
.form-block{{padding:16px;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft)}}
.form-title{{margin:0 0 3px;font-size:13px;font-weight:680}}
.form-hint{{margin:0 0 13px;color:var(--muted);font-size:12px}}
.mode-options{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:14px 0}}
.mode-option{{display:flex;align-items:flex-start;gap:8px;margin:0;padding:11px;border:1px solid var(--line);border-radius:7px;background:var(--surface-soft);cursor:pointer}}
.mode-option:has(input:checked){{border-color:var(--focus);background:#1a2330}}
.mode-option input{{width:15px;height:15px;margin:2px 0 0;accent-color:var(--ink)}}
.mode-option span{{display:block;font-size:12px;font-weight:680}}
.mode-option small{{display:block;margin-top:2px;color:var(--muted);font-size:10px;font-weight:450}}
.repeat-fields{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}
label{{display:block;margin:14px 0 6px;color:#c9ced6;font-size:12px;font-weight:650}}
.form-block label:first-of-type{{margin-top:0}}
textarea,input[type=number],input[type=file],input[type=password]{{width:100%;border:1px solid var(--line-strong);border-radius:7px;background:var(--surface);color:var(--ink);padding:10px 11px;font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}}
textarea{{min-height:190px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.6}}
input:focus,textarea:focus{{border-color:var(--focus);box-shadow:0 0 0 3px rgba(126,156,200,.18)}}
input[type=file]{{padding:6px}}
input[type=file]::file-selector-button{{margin-right:10px;border:0;border-radius:5px;background:#252a31;color:var(--ink);padding:7px 9px;font:inherit;font-size:12px;cursor:pointer}}
.toggle-row{{display:flex;align-items:center;gap:9px;margin-top:15px;font-size:13px;font-weight:550}}
.toggle-row input{{width:16px;height:16px;margin:0;accent-color:var(--ink)}}
button,a.button{{display:inline-flex;align-items:center;justify-content:center;min-height:38px;margin:14px 7px 0 0;border:1px solid var(--ink);border-radius:7px;padding:8px 13px;background:var(--ink);color:var(--canvas);font:inherit;font-size:12px;font-weight:680;text-decoration:none;cursor:pointer;transition:background .15s,border-color .15s,transform .15s}}
button:hover,a.button:hover{{background:#d9dde2;border-color:#d9dde2}}
button:active,a.button:active{{transform:translateY(1px)}}
button:focus-visible,a.button:focus-visible{{outline:3px solid rgba(126,156,200,.32);outline-offset:2px}}
button:disabled{{opacity:.42;cursor:not-allowed;transform:none}}
a.button.secondary,button.secondary{{border-color:var(--line-strong);background:var(--surface);color:var(--ink)}}
a.button.secondary:hover,button.secondary:hover{{border-color:#626b77;background:var(--surface-soft)}}
button.stop{{border-color:#765b2a;background:#221c12;color:#f0c56a}}
button.stop:hover{{border-color:#aa8140;background:#2c2315}}
button.danger{{border-color:#693b3a;background:var(--surface);color:var(--danger)}}
button.danger:hover{{border-color:#9f504b;background:var(--danger-soft)}}
.inline-form{{display:inline}}
.run-copy{{margin:0 0 18px;color:var(--muted)}}
.run-specs{{margin:0;border-top:1px solid var(--line)}}
.run-specs div{{display:flex;justify-content:space-between;gap:18px;padding:11px 0;border-bottom:1px solid var(--line)}}
.run-specs dt{{color:var(--muted)}}
.run-specs dd{{margin:0;text-align:right;font-weight:620}}
.actions{{display:flex;align-items:center;flex-wrap:wrap;margin-top:4px}}
.storage-note{{margin:17px 0 0;color:var(--muted);font-size:11px}}
.table-wrap{{overflow:auto}}
table{{width:100%;min-width:1120px;border-collapse:collapse}}
th,td{{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:var(--surface-soft);color:var(--muted);font-size:10px;font-weight:750;letter-spacing:.07em;text-transform:uppercase}}
tbody tr:last-child td{{border-bottom:0}}
tbody tr:hover{{background:#191d22}}
td{{font-size:12px}}
td.url,td.error{{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.badge{{display:inline-block;padding:3px 7px;border-radius:999px;font-size:9px;font-weight:800;letter-spacing:.04em}}
.success{{background:var(--success-soft);color:var(--success)}}
.failure{{background:var(--danger-soft);color:var(--danger)}}
.empty{{padding:34px;text-align:center;color:var(--muted)}}
.footer{{padding-top:8px;color:var(--muted);font-size:11px;text-align:center}}
@media(max-width:860px){{.hero{{display:block}}.hero-facts{{justify-content:flex-start;max-width:none;margin-top:18px}}.status-grid,.grid,.content-grid{{grid-template-columns:1fr}}.content-grid .panel{{height:auto}}}}
@media(max-width:560px){{.shell{{width:min(100% - 24px,1160px)}}.topbar-note{{display:none}}main{{padding-top:34px}}.hero{{margin-bottom:22px}}.status-grid{{gap:8px}}.stat{{padding:14px}}.panel-header,.panel-body{{padding:17px}}.mode-options,.repeat-fields{{grid-template-columns:1fr}}.actions{{align-items:stretch}}.actions button,.actions a.button,.actions form{{width:100%;margin-right:0}}.actions form{{display:block}}.actions form button{{width:100%}}}}
@media(prefers-reduced-motion:reduce){{*{{scroll-behavior:auto!important;animation:none!important;transition:none!important}}}}
</style>
</head>
<body>
<header class="topbar">
  <div class="shell topbar-inner">
    <a class="brand" href="{url_for('home')}" aria-label="Accueil Ghost Clicker"><span class="brand-mark">G</span><span>Ghost Clicker</span></a>
    <div class="topbar-note"><span class="service-dot"></span>Console d’exécution sécurisée</div>
  </div>
</header>
<main class="shell">
  <section class="hero">
    <div>
      <span class="eyebrow">Automatisation</span>
      <h1>Console d’exécution</h1>
      <p class="hero-copy">Configurez les cibles, contrôlez la planification et consultez les résultats depuis un espace unique.</p>
    </div>
    <div class="hero-facts" aria-label="Règles d’exécution">
      <span class="fact">Nombre fixe ou intervalle</span>
      <span class="fact">Mode continu avec arrêt</span>
      <span class="fact">1 ou 2 clics / visite</span>
    </div>
  </section>

  <div class="notices" aria-live="polite">{message_html}</div>

  <section class="status-grid" aria-label="État du service">
    <article class="stat"><span class="stat-label">État</span><p class="stat-value"><span class="status-dot {status_class}"></span>{status}</p></article>
    <article class="stat"><span class="stat-label">Passerelle rotative</span><p class="stat-value"><span class="status-dot {gateway_class}"></span>{gateway_status}</p></article>
    <article class="stat"><span class="stat-label">Prochaine exécution</span><p class="stat-value"><time>{html.escape(str(state['next_run_at'] or 'Désactivée'))}</time></p></article>
  </section>

  <section class="panel">
    <header class="panel-header">
      <div class="panel-heading"><span class="eyebrow">Configuration</span><h2>Passerelle et cibles</h2><p>Les données sensibles restent stockées temporairement sur le service.</p></div>
      <span class="count">{target_count} cible(s)</span>
    </header>
    <div class="panel-body grid">
      <div class="form-block">
        <p class="form-title">Passerelle Rayobyte</p>
        <p class="form-hint">Une seule passerelle rotative, conservée de façon éphémère.</p>
        <form method="post" action="{url_for('save_gateway')}">
          <input type="hidden" name="csrf_token" value="{CSRF_TOKEN}">
          <label for="gateway">Identifiants de connexion</label>
          <input id="gateway" type="password" name="gateway" autocomplete="off" placeholder="username:password@host:port" required>
          <button type="submit"{edit_disabled}>Enregistrer</button>
        </form>
      </div>
      <div class="form-block">
        <p class="form-title">Importer les cibles</p>
        <p class="form-hint">Format : URL | premier libellé [| second libellé].</p>
        <form method="post" action="{url_for('upload_targets')}" enctype="multipart/form-data">
          <input type="hidden" name="csrf_token" value="{CSRF_TOKEN}">
          <label for="target_file">Fichier texte UTF-8</label>
          <input id="target_file" type="file" name="target_file" accept=".txt,text/plain" required>
          <button type="submit"{edit_disabled}>Importer</button>
        </form>
      </div>
    </div>
  </section>

  <div class="content-grid">
    <section class="panel">
      <header class="panel-header"><div class="panel-heading"><span class="eyebrow">Paramètres</span><h2>Cibles et planification</h2><p>Modifiez la liste sans redéployer le service.</p></div></header>
      <div class="panel-body">
        <form method="post" action="{url_for('update_settings')}">
          <input type="hidden" name="csrf_token" value="{CSRF_TOKEN}">
          <label for="targets">Une ligne par cible</label>
          <textarea id="targets" name="targets" placeholder="Un clic : https://site-autorise.example/page | Continuer&#10;Deux clics : https://site-autorise.example/page | Continuer | Confirmer">{targets_text}</textarea>
          <label>Mode de répétition</label>
          <div class="mode-options">
            <label class="mode-option"><input type="radio" name="execution_mode" value="fixed"{fixed_checked}><span>Nombre fixe<small>Utilise exactement la valeur choisie.</small></span></label>
            <label class="mode-option"><input type="radio" name="execution_mode" value="range"{range_checked}><span>Intervalle<small>Tire un total entre le minimum et le maximum.</small></span></label>
            <label class="mode-option"><input type="radio" name="execution_mode" value="continuous"{continuous_checked}><span>Continu<small>Fonctionne jusqu'au clic sur Arrêter.</small></span></label>
          </div>
          <div class="repeat-fields">
            <div><label for="repetition_count">Nombre fixe</label><input id="repetition_count" type="number" name="repetition_count" min="1" max="{MAX_CONFIGURED_REPETITIONS}" value="{repetition_count}"></div>
            <div><label for="repetition_min">Minimum</label><input id="repetition_min" type="number" name="repetition_min" min="1" max="{MAX_CONFIGURED_REPETITIONS}" value="{repetition_min}"></div>
            <div><label for="repetition_max">Maximum</label><input id="repetition_max" type="number" name="repetition_max" min="1" max="{MAX_CONFIGURED_REPETITIONS}" value="{repetition_max}"></div>
          </div>
          <label for="interval">Intervalle planifié en minutes</label>
          <input id="interval" type="number" name="interval_minutes" min="60" max="10080" value="{interval}">
          <label class="toggle-row"><input type="checkbox" name="schedule_enabled"{checked}>Activer la planification</label>
          <button type="submit"{edit_disabled}>Enregistrer les paramètres</button>
        </form>
      </div>
    </section>

    <section class="panel">
      <header class="panel-header"><div class="panel-heading"><span class="eyebrow">Exécution</span><h2>Lancer le scénario</h2><p>Une seule exécution peut être active à la fois.</p></div></header>
      <div class="panel-body">
        <p class="run-copy">Chromium ouvre directement chaque cible via une nouvelle connexion. Un seul libellé déclenche un clic ; un second libellé déclenche le deuxième clic après 500 ms.</p>
        <dl class="run-specs">
          <div><dt>Répétitions</dt><dd>{html.escape(execution_summary)}</dd></div>
          <div><dt>Contrôle IP préalable</dt><dd>Aucun</dd></div>
          <div><dt>Fermeture navigateur</dt><dd>Après chaque visite</dd></div>
        </dl>
        <div class="actions">
          <form class="inline-form" method="post" action="{url_for('run_now')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><button type="submit"{run_disabled}>Lancer maintenant</button></form>
          <form class="inline-form" method="post" action="{url_for('stop_now')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><button class="stop" type="submit"{stop_disabled}>Arrêter</button></form>
          <a class="button secondary" href="{url_for('download_results')}">Télécharger le CSV</a>
          <form class="inline-form" method="post" action="{url_for('clear_results')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><button class="danger" type="submit">Effacer les résultats</button></form>
        </div>
        <p class="storage-note">Le tableau et le CSV sont mis à jour après chaque visite. Configuration et résultats restent temporaires et disparaissent lors d’un redéploiement. En mode continu, les 1 000 résultats les plus récents sont conservés.</p>
      </div>
    </section>
  </div>

  <section class="panel">
    <header class="panel-header"><div class="panel-heading"><span class="eyebrow">Historique</span><h2>Résultats</h2><p>Dernières visites enregistrées par le service.</p></div></header>
    <div class="table-wrap"><table><thead><tr><th>Date</th><th>Source</th><th>Répétition</th><th>Proxy</th><th>URL initiale</th><th>Premier libellé</th><th>Second libellé</th><th>URL finale</th><th>Résultat</th><th>Erreur</th></tr></thead><tbody>{render_result_rows(reports)}</tbody></table></div>
  </section>

  <footer class="footer">Ghost Clicker · Interface d’administration</footer>
</main>
</body>
</html>"""


scheduler = threading.Thread(target=scheduler_loop, daemon=True)
scheduler.start()


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
