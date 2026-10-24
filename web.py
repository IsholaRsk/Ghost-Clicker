"""Interface Render minimale pour lancer Ghost-Clicker et consulter les résultats."""

from __future__ import annotations

import hmac
import html
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, redirect, request, send_file, url_for

from generate_zip_pool import PoolConfigurationError, populate_runtime_pool
from main import (
    ConfigurationError,
    ResultStore,
    load_links_from_environment,
    main as run_ghost_clicker,
)

BASE_DIR = Path(__file__).resolve().parent
APP = Flask(__name__)
app = APP  # Nom conventionnel utilisé par Gunicorn.
STATE_LOCK = threading.Lock()
RUN_STATE: dict[str, Any] = {
    "running": False,
    "last_started": "",
    "last_finished": "",
    "last_exit_code": None,
    "last_error": "",
}
RUNTIME_ERROR = ""


def _path_from_env(variable: str, default: str) -> Path:
    value = os.getenv(variable, default)
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def _positive_int(variable: str, default: int, maximum: int) -> int:
    raw = os.getenv(variable, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise PoolConfigurationError(f"{variable} doit être un entier.") from exc
    if not 1 <= value <= maximum:
        raise PoolConfigurationError(
            f"{variable} doit être compris entre 1 et {maximum}."
        )
    return value


def prepare_runtime_pool() -> None:
    """Génère les mots de passe ciblés en mémoire à partir des secrets Render."""

    global RUNTIME_ERROR
    try:
        pool_size = _positive_int("PROXY_POOL_SIZE", 100, 500)
        distribution = populate_runtime_pool(
            os.environ,
            _path_from_env("ZIP_CODES_FILE", "zip_codes_us.txt"),
            _path_from_env("PROXIES_FILE", "proxies.txt"),
            pool_size,
        )
        RUNTIME_ERROR = ""
        APP.logger.info(
            "Pool Render prêt : %d profils, %d ZIP distincts, aucun ASN.",
            pool_size,
            len(distribution),
        )
    except PoolConfigurationError as exc:
        # Le service reste accessible afin d'afficher le diagnostic de configuration.
        RUNTIME_ERROR = str(exc)
        APP.logger.warning("Pool Render non initialisé : %s", RUNTIME_ERROR)


def _results_dir() -> Path:
    return _path_from_env("RESULTS_DIR", "results")


def ensure_dashboard() -> Path:
    store = ResultStore(_results_dir())
    if not store.dashboard_path.exists():
        store.render_dashboard()
    return store.dashboard_path


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
            "ADMIN_TOKEN n'est pas configuré dans Render.",
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


@APP.get("/health")
def health() -> Response:
    status = "degraded" if RUNTIME_ERROR else "ok"
    return Response(f"{status}\n", content_type="text/plain; charset=utf-8")


def _run_in_background() -> None:
    with STATE_LOCK:
        RUN_STATE.update(
            running=True,
            last_started=datetime.now().astimezone().isoformat(timespec="seconds"),
            last_finished="",
            last_exit_code=None,
            last_error="",
        )
    try:
        exit_code = run_ghost_clicker()
        with STATE_LOCK:
            RUN_STATE["last_exit_code"] = exit_code
    except Exception as exc:  # Dernier rempart autour du thread web.
        with STATE_LOCK:
            RUN_STATE["last_error"] = type(exc).__name__
        APP.logger.exception("Exécution web interrompue.")
    finally:
        with STATE_LOCK:
            RUN_STATE["running"] = False
            RUN_STATE["last_finished"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )


@APP.post("/run")
def start_run() -> Response:
    if RUNTIME_ERROR:
        return Response(
            f"Configuration incomplète : {RUNTIME_ERROR}",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
    if not os.getenv("TARGET_URLS", "").strip():
        return Response(
            "TARGET_URLS est vide dans Render.",
            status=400,
            content_type="text/plain; charset=utf-8",
        )

    with STATE_LOCK:
        if RUN_STATE["running"]:
            return redirect(url_for("home"), code=303)
        RUN_STATE["running"] = True

    thread = threading.Thread(target=_run_in_background, daemon=True)
    thread.start()
    return redirect(url_for("home"), code=303)


@APP.get("/dashboard")
def dashboard() -> Response:
    response = send_file(ensure_dashboard(), mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@APP.get("/results.csv")
def results_csv() -> Response:
    path = _results_dir() / "results.csv"
    if not path.exists():
        return Response(
            "Aucun CSV disponible. Lancez d'abord une exécution.",
            status=404,
            content_type="text/plain; charset=utf-8",
        )
    return send_file(path, mimetype="text/csv", as_attachment=True)


@APP.get("/")
def home() -> str:
    with STATE_LOCK:
        state = dict(RUN_STATE)

    running = bool(state["running"])
    if running:
        status_label = "Exécution en cours"
        status_class = "running"
    elif state["last_exit_code"] == 0:
        status_label = "Dernière exécution réussie"
        status_class = "success"
    elif state["last_exit_code"] is not None or state["last_error"]:
        status_label = "Dernière exécution avec erreur"
        status_class = "failure"
    else:
        status_label = "En attente"
        status_class = "idle"

    target_value = os.getenv("TARGET_URLS", "").strip()
    try:
        target_count = len(load_links_from_environment(target_value)[0])
    except ConfigurationError:
        target_count = 0
    runtime_message = html.escape(RUNTIME_ERROR) if RUNTIME_ERROR else "Pool prêt"
    disabled = " disabled" if running or RUNTIME_ERROR or not target_value else ""
    refresh = '<meta http-equiv="refresh" content="4">' if running else ""

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  {refresh}<title>Ghost-Clicker — Render</title>
  <style>
    :root{{--bg:#07111f;--panel:#102238;--text:#ecf4fb;--muted:#94a9bf;--accent:#56e0c3;--red:#ff7180;--blue:#72aaff}}
    *{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 10% 0,#163957,var(--bg) 38%);color:var(--text);font:15px/1.55 Segoe UI,Arial,sans-serif;min-height:100vh}}
    main{{max-width:900px;margin:auto;padding:40px 22px}} h1{{font-size:34px;margin:0}} h1 span{{color:var(--accent)}} p{{color:var(--muted)}}
    .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:28px 0}} .card{{padding:20px;background:linear-gradient(145deg,#132b45,var(--panel));border:1px solid #29425d;border-radius:16px}}
    .card small{{color:var(--muted);display:block;text-transform:uppercase;letter-spacing:.7px}} .card strong{{font-size:22px;display:block;margin-top:8px;word-break:break-word}}
    .status{{display:inline-block;padding:6px 10px;border-radius:99px;font-weight:700}} .running{{color:#ffd374;background:#ffd37418}} .success{{color:var(--accent);background:#56e0c318}} .failure{{color:var(--red);background:#ff718018}} .idle{{color:#aac2dc;background:#aac2dc18}}
    .actions{{display:flex;gap:12px;flex-wrap:wrap}} button,a.button{{border:0;border-radius:10px;padding:12px 17px;font-weight:750;text-decoration:none;cursor:pointer}} button{{background:var(--accent);color:#06141e}} button:disabled{{opacity:.4;cursor:not-allowed}} a.button{{background:var(--blue);color:#07111f}} a.secondary{{background:#1b334c;color:var(--text)}}
    .notice{{margin-top:22px;padding:15px;border-left:3px solid var(--blue);background:#102238;color:var(--muted)}} code{{color:#baf7eb}} @media(max-width:650px){{.grid{{grid-template-columns:1fr}}}}
  </style>
</head>
<body><main>
  <h1>Ghost<span>Clicker</span></h1>
  <p>Contrôle manuel des proxys et des URL configurées dans Render.</p>
  <div class="grid">
    <section class="card"><small>État</small><strong><span class="status {status_class}">{status_label}</span></strong></section>
    <section class="card"><small>URL configurées</small><strong>{target_count}</strong></section>
    <section class="card"><small>Pool</small><strong>{runtime_message}</strong></section>
  </div>
  <div class="actions">
    <form method="post" action="{url_for('start_run')}"><button type="submit"{disabled}>Lancer le contrôle</button></form>
    <a class="button" href="{url_for('dashboard')}">Voir le dashboard</a>
    <a class="button secondary" href="{url_for('results_csv')}">Télécharger le CSV</a>
  </div>
  <div class="notice">
    Modifiez les URL avec la variable Render <code>TARGET_URLS</code>, puis redéployez le service.
    <code>ALLOWED_DOMAINS</code> est déduit automatiquement. Stockage temporaire : les résultats
    disparaissent lors d'un redéploiement.
  </div>
  <p>Début : {html.escape(str(state['last_started'] or '—'))}<br>
     Fin : {html.escape(str(state['last_finished'] or '—'))}<br>
     Code de sortie : {html.escape(str(state['last_exit_code']) if state['last_exit_code'] is not None else '—')}</p>
</main></body></html>"""


prepare_runtime_pool()
