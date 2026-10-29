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
    BrowserSettings,
    ConfigurationError,
    Target,
    load_browser_settings,
    parse_proxy_gateway_text,
    run_targets,
    validate_button_text,
    validate_public_url,
)

BASE_DIR = Path(__file__).resolve().parent
APP = Flask(__name__)
app = APP
CSRF_TOKEN = secrets.token_urlsafe(32)
STATE_LOCK = threading.Lock()
RUN_STATE: dict[str, Any] = {
    "running": False,
    "source": "",
    "started_at": "",
    "finished_at": "",
    "last_error": "",
    "next_run_at": "",
}

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "targets": [],
    "schedule_enabled": False,
    "interval_minutes": 60,
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
    return storage_path(
        "PROXY_GATEWAY_FILE", "/tmp/ghost-clicker-proxy-gateway.txt"
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
    try:
        interval = int(value.get("interval_minutes", 60))
    except (TypeError, ValueError):
        interval = 60
    return {
        "targets": value.get("targets", []),
        "schedule_enabled": bool(value.get("schedule_enabled", False)),
        "interval_minutes": max(60, min(interval, 10_080)),
    }


def save_app_settings(value: dict[str, Any]) -> None:
    atomic_write_json(settings_path(), value)


def load_reports() -> list[dict[str, Any]]:
    path = reports_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def append_report(value: dict[str, Any]) -> None:
    reports = load_reports()
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
                second_button_text=validate_button_text(
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
        if len(parts) != 3:
            raise ConfigurationError(
                f"Ligne {line_number} : utilisez le format "
                "URL | premier libellé | second libellé."
            )
        url, button_text, second_button_text = parts
        targets.append(
            Target(
                url=validate_public_url(url),
                button_text=validate_button_text(button_text),
                second_button_text=validate_button_text(second_button_text),
            )
        )
    if len(targets) > 50:
        raise ConfigurationError("Maximum : 50 URL.")
    return targets


def targets_to_text(value: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in value.get("targets", []):
        if isinstance(item, dict):
            lines.append(
                f"{item.get('url', '')} | {item.get('button_text', '')} | "
                f"{item.get('second_button_text', '')}"
            )
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
        report = run_targets(browser_settings, targets)
        value = report.to_dict()
        value["source"] = source
        append_report(value)
        with STATE_LOCK:
            RUN_STATE["last_error"] = report.error
    except Exception as exc:
        with STATE_LOCK:
            RUN_STATE["last_error"] = str(exc)[:500]
    finally:
        with STATE_LOCK:
            RUN_STATE["running"] = False
            RUN_STATE["finished_at"] = now_iso()


def start_job(source: str) -> bool:
    with STATE_LOCK:
        if RUN_STATE["running"]:
            return False
        RUN_STATE.update(
            running=True,
            source=source,
            started_at=now_iso(),
            finished_at="",
            last_error="",
        )
    thread = threading.Thread(target=execute_job, args=(source,), daemon=True)
    thread.start()
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


@APP.post("/clear-results")
def clear_results() -> Response:
    try:
        require_csrf()
        path = reports_path()
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
            "proxy",
            "tentatives_proxy",
            "ip_sortie",
            "pays",
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
                    result.get("proxy_name", ""),
                    result.get("proxy_attempts", ""),
                    result.get("exit_ip", ""),
                    result.get("proxy_country", ""),
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
        for result in reversed(report.get("results", [])):
            success = bool(result.get("success"))
            css_class = "success" if success else "failure"
            label = "SUCCÈS" if success else "ÉCHEC"
            rows.append(
                f"""
                <tr>
                  <td>{html.escape(str(result.get('started_at', '')))}</td>
                  <td>{html.escape(str(report.get('source', '')))}</td>
                  <td>{html.escape(str(result.get('proxy_name', '')))}</td>
                  <td class="url">{html.escape(str(result.get('initial_url', '')))}</td>
                  <td>{html.escape(str(result.get('button_text', '')))}</td>
                  <td>{html.escape(str(result.get('second_button_text', '')))}</td>
                  <td class="url">{html.escape(str(result.get('final_url', '')))}</td>
                  <td><span class="badge {css_class}">{label}</span></td>
                  <td class="error">{html.escape(str(result.get('error', '')))}</td>
                </tr>"""
            )
    return "".join(rows) or '<tr><td colspan="9" class="empty">Aucun résultat.</td></tr>'


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
    if request.args.get("cleared") == "1":
        messages.append("Résultats effacés.")
    if request.args.get("error"):
        messages.append(request.args["error"][:500])
    if configuration_error:
        messages.append(f"Configuration serveur : {configuration_error}")
    if state["last_error"]:
        messages.append(f"Dernière erreur : {state['last_error']}")

    status = "Exécution en cours" if state["running"] else "En attente"
    refresh = '<meta http-equiv="refresh" content="5">' if state["running"] else ""
    run_disabled = " disabled" if state["running"] or configuration_error else ""
    edit_disabled = " disabled" if state["running"] else ""
    message_html = "".join(
        f'<div class="notice">{html.escape(message)}</div>' for message in messages
    )
    checked = " checked" if app_settings.get("schedule_enabled") else ""
    targets_text = html.escape(targets_to_text(app_settings))
    interval = int(app_settings.get("interval_minutes", 60))
    gateway_status = (
        "configurée" if proxy_gateway_file_path().exists() else "absente"
    )

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>Ghost-Clicker</title><style>
:root{{--bg:#07111f;--panel:#10243a;--line:#29445f;--text:#eef5fb;--muted:#93a9c0;--accent:#55dfc1;--blue:#75aaff;--red:#ff7583}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 8% 0,#173d5e,var(--bg) 38%);color:var(--text);font:14px/1.55 Segoe UI,Arial,sans-serif;min-height:100vh}}
main{{max-width:1200px;margin:auto;padding:30px 20px}}h1{{font-size:32px;margin:0}}h1 span{{color:var(--accent)}}p{{color:var(--muted)}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.card{{background:linear-gradient(145deg,#132c47,var(--panel));border:1px solid var(--line);border-radius:16px;padding:20px;margin:16px 0}}
label{{display:block;font-weight:700;margin:11px 0 5px}}textarea,input[type=number],input[type=file],input[type=password]{{width:100%;background:#081827;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:11px;font:inherit}}textarea{{min-height:190px;resize:vertical}}
button,a.button{{display:inline-block;border:0;border-radius:9px;padding:11px 15px;margin:8px 8px 0 0;font-weight:750;text-decoration:none;cursor:pointer}}button{{background:var(--accent);color:#06141d}}button.secondary,a.button{{background:var(--blue);color:#07111f}}button.danger{{background:var(--red)}}button:disabled{{opacity:.4;cursor:not-allowed}}
.notice{{padding:11px 14px;margin:10px 0;border-left:3px solid var(--blue);background:#10243a;color:#dce8f4}}code{{color:#b6f7e8}}.meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.meta div{{background:#0b1b2b;padding:12px;border-radius:10px}}.meta small{{display:block;color:var(--muted)}}
.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:1000px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:11px;text-transform:uppercase}}td.url,td.error{{max-width:270px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.badge{{padding:4px 7px;border-radius:20px;font-size:10px;font-weight:800}}.success{{color:var(--accent);background:#55dfc119}}.failure{{color:var(--red);background:#ff758319}}.empty{{text-align:center;color:var(--muted)}}
@media(max-width:800px){{.grid,.meta{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>Ghost<span>Clicker</span></h1><p>Test fonctionnel autorisé : une nouvelle connexion à la passerelle proxy rotative pour chaque URL, deux clics explicites sur le bouton aux libellés configurés, puis attente de redirection.</p>
{message_html}
<section class="card"><div class="meta"><div><small>État</small><strong>{status}</strong></div><div><small>Passerelle rotative</small><strong>{gateway_status}</strong></div><div><small>Prochaine exécution</small><strong>{html.escape(str(state['next_run_at'] or 'désactivée'))}</strong></div></div></section>
<section class="card"><h2>Configurer la passerelle et les cibles</h2><div class="grid">
<form method="post" action="{url_for('save_gateway')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><label>Passerelle Rayobyte — username:password@host:port</label><input type="password" name="gateway" autocomplete="off" required><button type="submit"{edit_disabled}>Enregistrer la passerelle</button></form>
<form method="post" action="{url_for('upload_targets')}" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><label>Fichier cibles — URL | premier libellé | second libellé</label><input type="file" name="target_file" accept=".txt,text/plain" required><button type="submit"{edit_disabled}>Importer les URL</button></form>
</div></section>
<div class="grid"><section class="card"><h2>Paramètres en ligne</h2>
<form method="post" action="{url_for('update_settings')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}">
<label for="targets">Une ligne par visite : URL | premier libellé exact | second libellé exact</label>
<textarea id="targets" name="targets" placeholder="https://site-autorise.example/page | Continuer | Confirmer">{targets_text}</textarea>
<label for="interval">Intervalle planifié en minutes (minimum 60)</label><input id="interval" type="number" name="interval_minutes" min="60" max="10080" value="{interval}">
<label><input type="checkbox" name="schedule_enabled"{checked}> Activer la planification</label>
<button type="submit"{edit_disabled}>Enregistrer</button></form></section>
<section class="card"><h2>Exécution</h2><p>Chromium fonctionne en mode headless dans Docker. Pour chaque URL, une nouvelle connexion à la passerelle rotative est contrôlée. En cas d’échec, une seconde connexion neuve à la même passerelle est essayée une seule fois. Le premier libellé est cliqué, le bot attend le second libellé, clique une seconde fois, puis attend la redirection.</p>
<form method="post" action="{url_for('run_now')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><button type="submit"{run_disabled}>Lancer maintenant</button></form>
<a class="button" href="{url_for('download_results')}">Télécharger le CSV</a>
<form method="post" action="{url_for('clear_results')}"><input type="hidden" name="csrf_token" value="{CSRF_TOKEN}"><button class="danger" type="submit">Effacer les résultats</button></form>
<p>Les listes, paramètres et résultats utilisent un stockage temporaire et disparaissent lors d’un redéploiement.</p></section></div>
<section class="card"><h2>Résultats</h2><div class="table-wrap"><table><thead><tr><th>Date</th><th>Source</th><th>Proxy</th><th>URL initiale</th><th>Premier libellé</th><th>Second libellé</th><th>URL finale</th><th>Résultat</th><th>Erreur</th></tr></thead><tbody>{render_result_rows(reports)}</tbody></table></div></section>
</main></body></html>"""


scheduler = threading.Thread(target=scheduler_loop, daemon=True)
scheduler.start()


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
