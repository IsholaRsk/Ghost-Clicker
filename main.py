"""Ghost-Clicker : vérification de proxys et visites contrôlées avec Playwright.

Le programme ne clique sur aucun élément. Il valide les entrées, teste chaque
proxy dans un contexte Chromium isolé, répartit les URL entre les proxys valides
et génère un CSV ainsi qu'un tableau de bord HTML local.
"""

from __future__ import annotations

import csv
import html
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

APP_NAME = "Ghost-Clicker"
LOGGER = logging.getLogger(APP_NAME)
SUPPORTED_COUNTRIES = {"US", "CA"}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

RESULT_FIELDS = [
    "run_id",
    "type_enregistrement",
    "date_heure",
    "proxy",
    "url_initiale",
    "url_finale",
    "pays_configure",
    "pays_detecte",
    "adresse_ip_sortie",
    "statut_http",
    "resultat_test",
    "duree_ms",
    "message_erreur",
]


class ConfigurationError(ValueError):
    """Erreur de configuration destinée à être affichée sans secret."""


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    links_file: Path
    proxies_file: Path
    results_dir: Path
    ip_check_url: str
    headless: bool
    navigation_timeout_ms: int
    proxy_check_timeout_ms: int
    delay_between_urls_seconds: float
    allowed_domains: tuple[str, ...]
    ignore_https_errors: bool
    max_dashboard_rows: int


@dataclass(frozen=True)
class ProxyConfig:
    name: str
    country: str
    server: str
    username: str | None = None
    password: str | None = None

    def playwright_options(self) -> dict[str, str]:
        options = {"server": self.server}
        if self.username is not None and self.password is not None:
            options["username"] = self.username
            options["password"] = self.password
        return options


@dataclass(frozen=True)
class ProxyConfigIssue:
    name: str
    country: str
    message: str


@dataclass(frozen=True)
class InvalidLink:
    line_number: int
    value: str
    message: str


@dataclass
class ProxyCheck:
    success: bool
    result: str
    final_url: str = ""
    exit_ip: str = ""
    detected_country: str = ""
    http_status: int | None = None
    duration_ms: int = 0
    error: str = ""


@dataclass
class ResultRecord:
    run_id: str
    type_enregistrement: str
    date_heure: str
    proxy: str = ""
    url_initiale: str = ""
    url_finale: str = ""
    pays_configure: str = ""
    pays_detecte: str = ""
    adresse_ip_sortie: str = ""
    statut_http: int | str = ""
    resultat_test: str = ""
    duree_ms: int | str = ""
    message_erreur: str = ""

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        return {field: row.get(field, "") for field in RESULT_FIELDS}


@dataclass
class ActiveProxySession:
    proxy: ProxyConfig
    context: Any
    page: Any
    check: ProxyCheck


def now_iso() -> str:
    """Date locale avec fuseau, à la seconde près."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def parse_bool(value: str | None, default: bool, variable: str) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "oui", "on"}:
        return True
    if normalized in {"0", "false", "no", "non", "off"}:
        return False
    raise ConfigurationError(
        f"{variable} doit valoir true/false (valeur reçue non reconnue)."
    )


def parse_positive_int(
    value: str | None, default: int, variable: str, minimum: int = 1
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{variable} doit être un entier.") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{variable} doit être supérieur ou égal à {minimum}.")
    return parsed


def parse_nonnegative_float(
    value: str | None, default: float, variable: str
) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{variable} doit être un nombre.") from exc
    if parsed < 0:
        raise ConfigurationError(f"{variable} ne peut pas être négatif.")
    return parsed


def parse_allowed_domains(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()

    domains: list[str] = []
    for raw_domain in value.split(","):
        domain = raw_domain.strip().lower().rstrip(".")
        if not domain:
            continue
        if "://" in domain or "/" in domain or ":" in domain:
            raise ConfigurationError(
                "ALLOWED_DOMAINS attend des noms de domaine séparés par des virgules, "
                "sans protocole, chemin ou port."
            )
        try:
            ascii_domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigurationError(
                f"Nom de domaine invalide dans ALLOWED_DOMAINS : {domain!r}."
            ) from exc
        if ascii_domain not in domains:
            domains.append(ascii_domain)
    return tuple(domains)


def load_settings(base_dir: Path) -> Settings:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ConfigurationError(
            "python-dotenv n'est pas installé. Exécutez : pip install -r requirements.txt"
        ) from exc

    load_dotenv(dotenv_path=base_dir / ".env", override=False)

    ip_check_url = os.getenv("IP_CHECK_URL", "https://ipinfo.io/json").strip()
    if not ip_check_url:
        raise ConfigurationError("IP_CHECK_URL ne peut pas être vide.")
    # Ce service est lui aussi une entrée réseau : on lui applique la même validation.
    ip_check_url = validate_url(ip_check_url)

    return Settings(
        base_dir=base_dir,
        links_file=base_dir / os.getenv("LINKS_FILE", "links.txt"),
        proxies_file=base_dir / os.getenv("PROXIES_FILE", "proxies.txt"),
        results_dir=base_dir / os.getenv("RESULTS_DIR", "results"),
        ip_check_url=ip_check_url,
        headless=parse_bool(os.getenv("HEADLESS"), False, "HEADLESS"),
        navigation_timeout_ms=parse_positive_int(
            os.getenv("NAVIGATION_TIMEOUT_MS"), 30_000, "NAVIGATION_TIMEOUT_MS", 1_000
        ),
        proxy_check_timeout_ms=parse_positive_int(
            os.getenv("PROXY_CHECK_TIMEOUT_MS"),
            20_000,
            "PROXY_CHECK_TIMEOUT_MS",
            1_000,
        ),
        delay_between_urls_seconds=parse_nonnegative_float(
            os.getenv("DELAY_BETWEEN_URLS_SECONDS"),
            3.0,
            "DELAY_BETWEEN_URLS_SECONDS",
        ),
        allowed_domains=parse_allowed_domains(os.getenv("ALLOWED_DOMAINS")),
        ignore_https_errors=parse_bool(
            os.getenv("IGNORE_HTTPS_ERRORS"), False, "IGNORE_HTTPS_ERRORS"
        ),
        max_dashboard_rows=parse_positive_int(
            os.getenv("MAX_DASHBOARD_ROWS"), 2_000, "MAX_DASHBOARD_ROWS", 100
        ),
    )


def _hostname_is_forbidden(hostname: str) -> bool:
    normalized = hostname.lower().rstrip(".")
    if normalized == "localhost" or normalized.endswith((".localhost", ".local")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


def validate_url(raw_url: str, allowed_domains: Sequence[str] = ()) -> str:
    """Valide une URL HTTP(S) publique et éventuellement sa liste d'autorisation."""

    url = raw_url.strip()
    if not url:
        raise ValueError("URL vide.")
    if len(url) > 2_048:
        raise ValueError("URL trop longue (maximum : 2048 caractères).")

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("URL mal formée.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Seuls les protocoles http et https sont autorisés.")
    if not parsed.hostname:
        raise ValueError("Nom d'hôte absent.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Les identifiants intégrés à une URL ne sont pas autorisés.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Port invalide.") from exc

    hostname = parsed.hostname.lower().rstrip(".")
    if _hostname_is_forbidden(hostname):
        raise ValueError("Les hôtes locaux, privés ou réservés ne sont pas autorisés.")

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Nom d'hôte international invalide.") from exc

    if allowed_domains and not any(
        ascii_hostname == domain or ascii_hostname.endswith(f".{domain}")
        for domain in allowed_domains
    ):
        raise ValueError("Domaine absent de ALLOWED_DOMAINS.")

    return url


def install_navigation_guard(page: Any, allowed_domains: Sequence[str] = ()) -> None:
    """Bloque une redirection du document principal vers une URL non autorisée."""

    def handle_route(route: Any, request: Any) -> None:
        try:
            is_main_document = (
                request.is_navigation_request() and request.frame == page.main_frame
            )
        except Exception:
            is_main_document = False

        if is_main_document:
            try:
                validate_url(request.url, allowed_domains)
            except ValueError:
                LOGGER.warning(
                    "Navigation principale bloquée : redirection vers une URL non autorisée."
                )
                route.abort("blockedbyclient")
                return
        route.continue_()

    page.route("**/*", handle_route)


def load_links(
    path: Path, allowed_domains: Sequence[str] = ()
) -> tuple[list[str], list[InvalidLink]]:
    if not path.exists():
        raise ConfigurationError(f"Fichier d'URL introuvable : {path.name}")

    valid: list[str] = []
    invalid: list[InvalidLink] = []
    seen: set[str] = set()

    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            value = raw_line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                url = validate_url(value, allowed_domains)
            except ValueError as exc:
                invalid.append(InvalidLink(line_number, value, str(exc)))
                continue
            if url in seen:
                continue
            seen.add(url)
            valid.append(url)

    return valid, invalid


def validate_proxy_server(server: str) -> str:
    value = server.strip()
    if not value:
        raise ValueError("endpoint vide")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("endpoint mal formé") from exc

    if parsed.scheme.lower() not in {"http", "https", "socks5"}:
        raise ValueError("le protocole doit être http, https ou socks5")
    if not parsed.hostname:
        raise ValueError("nom d'hôte absent")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "ne placez pas les identifiants dans l'endpoint ; utilisez les variables dédiées"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("port invalide") from exc
    if port is None:
        raise ValueError("port absent")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("l'endpoint ne doit contenir ni chemin, ni requête, ni fragment")
    return value.rstrip("/")


def _env_value(
    environment: Mapping[str, str], variable_name: str, field_label: str
) -> str:
    if not ENV_NAME_RE.fullmatch(variable_name):
        raise ValueError(f"nom de variable invalide pour {field_label}")
    value = environment.get(variable_name, "").strip()
    if not value:
        raise ValueError(f"variable {variable_name} absente ou vide")
    return value


def load_proxy_configs(
    path: Path, environment: Mapping[str, str] | None = None
) -> tuple[list[ProxyConfig], list[ProxyConfigIssue]]:
    """Charge les références de variables depuis le CSV, jamais les secrets du CSV."""

    if not path.exists():
        raise ConfigurationError(f"Fichier de proxys introuvable : {path.name}")
    environment = os.environ if environment is None else environment
    proxies: list[ProxyConfig] = []
    issues: list[ProxyConfigIssue] = []

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required_headers = {
            "name",
            "country",
            "server_env",
            "username_env",
            "password_env",
        }
        if reader.fieldnames is None or not required_headers.issubset(reader.fieldnames):
            raise ConfigurationError(
                "proxies.txt doit être un CSV avec les colonnes : "
                "name,country,server_env,username_env,password_env"
            )

        used_names: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            if not name or name.startswith("#"):
                continue
            country = (row.get("country") or "").strip().upper()
            try:
                if name in used_names:
                    raise ValueError("nom de proxy dupliqué")
                if country not in SUPPORTED_COUNTRIES:
                    raise ValueError("le pays doit être US ou CA")

                server_env = (row.get("server_env") or "").strip()
                username_env = (row.get("username_env") or "").strip()
                password_env = (row.get("password_env") or "").strip()
                server = validate_proxy_server(
                    _env_value(environment, server_env, "server_env")
                )

                if bool(username_env) != bool(password_env):
                    raise ValueError(
                        "username_env et password_env doivent être tous les deux renseignés "
                        "ou tous les deux vides"
                    )

                username: str | None = None
                password: str | None = None
                if username_env and password_env:
                    username = _env_value(environment, username_env, "username_env")
                    password = _env_value(environment, password_env, "password_env")

                proxies.append(ProxyConfig(name, country, server, username, password))
                used_names.add(name)
            except ValueError as exc:
                issues.append(
                    ProxyConfigIssue(
                        name=name or f"ligne-{line_number}",
                        country=country if country in SUPPORTED_COUNTRIES else "",
                        message=f"Ligne {line_number} : {exc}.",
                    )
                )

    return proxies, issues


def redact_error(error: BaseException | str, secrets: Iterable[str] = ()) -> str:
    """Retire les secrets connus et les authentifications incluses dans des URL."""

    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        message = message.replace(secret, "***")
    message = re.sub(
        r"(?i)(https?|socks5)://[^/@\s]+@", lambda match: f"{match.group(1)}://***@", message
    )
    message = re.sub(
        r"(?i)(password|passwd|pwd)(\s*[=:]\s*)[^\s,;]+", r"\1\2***", message
    )
    return message[:1_000]


def _extract_ip(payload: Mapping[str, Any]) -> str:
    for key in ("ip", "query", "origin"):
        raw_value = payload.get(key)
        if raw_value:
            candidate = str(raw_value).split(",", maxsplit=1)[0].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return ""


def normalize_country(value: Any) -> str:
    if value is None:
        return ""
    normalized = str(value).strip().upper()
    aliases = {
        "UNITED STATES": "US",
        "UNITED STATES OF AMERICA": "US",
        "USA": "US",
        "CANADA": "CA",
    }
    return aliases.get(normalized, normalized)


def _extract_country(payload: Mapping[str, Any]) -> str:
    for key in ("country", "country_code", "countryCode"):
        if payload.get(key):
            return normalize_country(payload[key])
    return ""


def check_proxy(
    page: Any,
    proxy: ProxyConfig,
    check_url: str,
    timeout_ms: int,
    secrets: Iterable[str],
) -> ProxyCheck:
    started = time.perf_counter()
    final_url = ""
    try:
        page.bring_to_front()
        response = page.goto(check_url, wait_until="domcontentloaded", timeout=timeout_ms)
        final_url = page.url
        if response is None:
            raise RuntimeError("le service de vérification n'a renvoyé aucune réponse HTTP")

        status = response.status
        if not 200 <= status < 300:
            return ProxyCheck(
                success=False,
                result="ECHEC_PROXY",
                final_url=final_url,
                http_status=status,
                duration_ms=round((time.perf_counter() - started) * 1_000),
                error=f"Service de vérification : statut HTTP {status}.",
            )

        try:
            payload = response.json()
        except Exception:
            payload = json.loads(response.text())
        if not isinstance(payload, dict):
            raise RuntimeError("réponse JSON inattendue du service de vérification")

        exit_ip = _extract_ip(payload)
        if not exit_ip:
            raise RuntimeError("adresse IP absente de la réponse de vérification")
        detected_country = _extract_country(payload)
        duration_ms = round((time.perf_counter() - started) * 1_000)

        if detected_country and detected_country != proxy.country:
            return ProxyCheck(
                success=False,
                result="ECHEC_PAYS",
                final_url=final_url,
                exit_ip=exit_ip,
                detected_country=detected_country,
                http_status=status,
                duration_ms=duration_ms,
                error=(
                    f"Pays de sortie détecté : {detected_country}; "
                    f"pays configuré : {proxy.country}."
                ),
            )

        return ProxyCheck(
            success=True,
            result=("SUCCES" if detected_country else "SUCCES_SANS_GEOLOCALISATION"),
            final_url=final_url,
            exit_ip=exit_ip,
            detected_country=detected_country,
            http_status=status,
            duration_ms=duration_ms,
            error=(
                "Le service n'a pas fourni de pays; seule la connectivité et l'IP "
                "ont été vérifiées."
                if not detected_country
                else ""
            ),
        )
    except Exception as exc:
        result = "DELAI_DEPASSE" if "Timeout" in type(exc).__name__ else "ECHEC_PROXY"
        return ProxyCheck(
            success=False,
            result=result,
            final_url=final_url or getattr(page, "url", ""),
            duration_ms=round((time.perf_counter() - started) * 1_000),
            error=redact_error(exc, secrets),
        )


def visit_url(
    page: Any,
    url: str,
    proxy: ProxyConfig,
    check: ProxyCheck,
    timeout_ms: int,
    run_id: str,
    secrets: Iterable[str],
) -> ResultRecord:
    started = time.perf_counter()
    final_url = ""
    try:
        page.bring_to_front()
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        final_url = page.url
        duration_ms = round((time.perf_counter() - started) * 1_000)
        if response is None:
            return ResultRecord(
                run_id=run_id,
                type_enregistrement="navigation",
                date_heure=now_iso(),
                proxy=proxy.name,
                url_initiale=url,
                url_finale=final_url,
                pays_configure=proxy.country,
                pays_detecte=check.detected_country,
                adresse_ip_sortie=check.exit_ip,
                resultat_test="ECHEC_RESEAU",
                duree_ms=duration_ms,
                message_erreur="La navigation n'a renvoyé aucune réponse HTTP.",
            )

        status = response.status
        success = 200 <= status < 400
        return ResultRecord(
            run_id=run_id,
            type_enregistrement="navigation",
            date_heure=now_iso(),
            proxy=proxy.name,
            url_initiale=url,
            url_finale=final_url,
            pays_configure=proxy.country,
            pays_detecte=check.detected_country,
            adresse_ip_sortie=check.exit_ip,
            statut_http=status,
            resultat_test="SUCCES" if success else "ECHEC_HTTP",
            duree_ms=duration_ms,
            message_erreur="" if success else f"Statut HTTP final {status}.",
        )
    except Exception as exc:
        result = "DELAI_DEPASSE" if "Timeout" in type(exc).__name__ else "ECHEC_RESEAU"
        return ResultRecord(
            run_id=run_id,
            type_enregistrement="navigation",
            date_heure=now_iso(),
            proxy=proxy.name,
            url_initiale=url,
            url_finale=final_url or getattr(page, "url", ""),
            pays_configure=proxy.country,
            pays_detecte=check.detected_country,
            adresse_ip_sortie=check.exit_ip,
            resultat_test=result,
            duree_ms=round((time.perf_counter() - started) * 1_000),
            message_erreur=redact_error(exc, secrets),
        )


class ResultStore:
    def __init__(self, results_dir: Path, max_dashboard_rows: int = 2_000) -> None:
        self.results_dir = results_dir
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.results_dir / "results.csv"
        self.dashboard_path = self.results_dir / "dashboard.html"
        self.max_dashboard_rows = max_dashboard_rows

    def add(self, record: ResultRecord) -> None:
        needs_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        with self.csv_path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(record.to_row())

    def read_all(self) -> list[dict[str, str]]:
        if not self.csv_path.exists():
            return []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return [dict(row) for row in reader]

    def render_dashboard(self) -> None:
        rows = self.read_all()
        html_content = build_dashboard(rows, self.max_dashboard_rows)
        temporary_path = self.dashboard_path.with_suffix(".html.tmp")
        temporary_path.write_text(html_content, encoding="utf-8")
        temporary_path.replace(self.dashboard_path)


def _is_success(row: Mapping[str, str]) -> bool:
    return (row.get("resultat_test") or "").startswith("SUCCES")


def _percent(successes: int, total: int) -> float:
    return round((successes / total) * 100, 1) if total else 0.0


def _safe_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _link_or_text(value: str) -> str:
    escaped = html.escape(value)
    try:
        safe = validate_url(value)
    except ValueError:
        return escaped or "—"
    return (
        f'<a href="{html.escape(safe, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">{escaped}</a>'
    )


def _status_class(result: str) -> str:
    if result.startswith("SUCCES"):
        return "success"
    if result in {"URL_INVALIDE", "CONFIGURATION_INVALIDE"}:
        return "warning"
    return "failure"


def build_dashboard(rows: Sequence[Mapping[str, str]], max_rows: int = 2_000) -> str:
    navigation_rows = [row for row in rows if row.get("type_enregistrement") == "navigation"]
    proxy_rows = [row for row in rows if row.get("type_enregistrement") == "proxy_test"]
    successful_visits = sum(_is_success(row) for row in navigation_rows)
    successful_proxies = sum(_is_success(row) for row in proxy_rows)
    visit_rate = _percent(successful_visits, len(navigation_rows))
    proxy_rate = _percent(successful_proxies, len(proxy_rows))
    durations = [
        _safe_int(row.get("duree_ms"))
        for row in navigation_rows
        if _safe_int(row.get("duree_ms")) > 0
    ]
    average_duration = round(sum(durations) / len(durations)) if durations else 0
    run_ids = {row.get("run_id", "") for row in rows if row.get("run_id")}

    country_cards: list[str] = []
    for country in sorted(SUPPORTED_COUNTRIES):
        country_rows = [row for row in navigation_rows if row.get("pays_configure") == country]
        country_success = sum(_is_success(row) for row in country_rows)
        rate = _percent(country_success, len(country_rows))
        country_cards.append(
            f"""
            <div class="country-row">
              <div><strong>{country}</strong><span>{country_success}/{len(country_rows)} réussies</span></div>
              <div class="bar"><i style="width:{rate}%"></i></div>
              <b>{rate:.1f}%</b>
            </div>"""
        )

    table_rows: list[str] = []
    displayed_rows = list(rows)[-max_rows:]
    displayed_rows.reverse()
    for row in displayed_rows:
        result = row.get("resultat_test", "")
        css_class = _status_class(result)
        searchable = " ".join(str(value) for value in row.values()).lower()
        record_type = row.get("type_enregistrement", "")
        country = row.get("pays_configure", "")
        table_rows.append(
            f"""
            <tr data-result="{html.escape(css_class, quote=True)}"
                data-country="{html.escape(country, quote=True)}"
                data-type="{html.escape(record_type, quote=True)}"
                data-search="{html.escape(searchable, quote=True)}">
              <td>{html.escape(row.get('date_heure', '')) or '—'}</td>
              <td><span class="type-tag">{html.escape(record_type) or '—'}</span></td>
              <td>{html.escape(row.get('proxy', '')) or '—'}</td>
              <td class="url-cell">{_link_or_text(row.get('url_initiale', ''))}</td>
              <td class="url-cell">{_link_or_text(row.get('url_finale', ''))}</td>
              <td>{html.escape(country) or '—'}</td>
              <td>{html.escape(row.get('pays_detecte', '')) or '—'}</td>
              <td><code>{html.escape(row.get('adresse_ip_sortie', '')) or '—'}</code></td>
              <td>{html.escape(str(row.get('statut_http', ''))) or '—'}</td>
              <td><span class="status {css_class}">{html.escape(result) or '—'}</span></td>
              <td>{html.escape(str(row.get('duree_ms', ''))) or '—'}</td>
              <td class="error-cell" title="{html.escape(row.get('message_erreur', ''), quote=True)}">
                {html.escape(row.get('message_erreur', '')) or '—'}
              </td>
            </tr>"""
        )

    generated_at = now_iso()
    latest_run = rows[-1].get("run_id", "—") if rows else "—"
    no_data = (
        '<tr><td colspan="12" class="empty">Aucun résultat pour le moment.</td></tr>'
        if not table_rows
        else ""
    )
    truncation_note = (
        f"Affichage limité aux {max_rows} lignes les plus récentes sur {len(rows)}. "
        if len(rows) > max_rows
        else ""
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
  <title>Ghost-Clicker — Dashboard</title>
  <style>
    :root {{ --bg:#07111f; --panel:#0d1b2d; --panel2:#12243a; --text:#eaf2fb;
      --muted:#8ca3bd; --accent:#56e0c3; --blue:#66a6ff; --red:#ff6f7d; --amber:#ffcb66;
      --border:rgba(151,180,211,.16); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--text); background:radial-gradient(circle at 8% 5%, #123253 0, var(--bg) 34%);
      font:14px/1.5 Inter,Segoe UI,Arial,sans-serif; min-height:100vh; }}
    .shell {{ max-width:1500px; margin:auto; padding:28px; }}
    header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:24px; }}
    h1 {{ margin:0; font-size:30px; letter-spacing:-.7px; }}
    h1 span {{ color:var(--accent); }}
    header p, .meta {{ color:var(--muted); margin:5px 0 0; }}
    .download {{ color:#06131f; background:var(--accent); text-decoration:none; font-weight:750; padding:10px 15px; border-radius:10px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; }}
    .card, .panel {{ background:linear-gradient(145deg,rgba(18,36,58,.96),rgba(11,25,42,.96));
      border:1px solid var(--border); border-radius:16px; box-shadow:0 18px 50px rgba(0,0,0,.17); }}
    .card {{ padding:19px; }} .card label {{ display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.8px; }}
    .card strong {{ display:block; font-size:27px; margin-top:7px; }} .card small {{ color:var(--muted); }}
    .split {{ display:grid; grid-template-columns:minmax(250px,.8fr) minmax(400px,1.2fr); gap:14px; margin:14px 0; }}
    .panel {{ padding:20px; }} .panel h2 {{ margin:0 0 16px; font-size:16px; }}
    .gauge-wrap {{ display:flex; gap:22px; align-items:center; }}
    .gauge {{ width:122px; aspect-ratio:1; border-radius:50%; display:grid; place-items:center;
      background:conic-gradient(var(--accent) {visit_rate}%, rgba(255,255,255,.08) 0); position:relative; }}
    .gauge:after {{ content:""; width:88px; aspect-ratio:1; border-radius:50%; background:var(--panel); position:absolute; }}
    .gauge b {{ z-index:1; font-size:23px; }} .gauge-copy p {{ color:var(--muted); margin:5px 0; }}
    .country-row {{ display:grid; grid-template-columns:150px 1fr 58px; align-items:center; gap:14px; margin:17px 0; }}
    .country-row span {{ display:block; color:var(--muted); font-size:12px; }} .country-row b {{ text-align:right; }}
    .bar {{ height:8px; border-radius:9px; background:rgba(255,255,255,.08); overflow:hidden; }}
    .bar i {{ display:block; height:100%; border-radius:9px; background:linear-gradient(90deg,var(--blue),var(--accent)); }}
    .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }}
    input, select {{ color:var(--text); background:#091727; border:1px solid var(--border); border-radius:9px; padding:10px 12px; outline:none; }}
    input {{ flex:1; min-width:240px; }} input:focus,select:focus {{ border-color:var(--blue); }}
    .table-wrap {{ overflow:auto; border:1px solid var(--border); border-radius:12px; }}
    table {{ width:100%; border-collapse:collapse; min-width:1350px; }} th,td {{ text-align:left; padding:11px 12px; border-bottom:1px solid var(--border); vertical-align:top; }}
    th {{ position:sticky; top:0; background:#11243a; color:#acc0d5; text-transform:uppercase; letter-spacing:.55px; font-size:10px; z-index:2; }}
    tbody tr:hover {{ background:rgba(102,166,255,.055); }} td {{ max-width:240px; }}
    a {{ color:#8fc2ff; }} code {{ color:#b5f7e9; }} .url-cell,.error-cell {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    .status {{ display:inline-block; border-radius:20px; padding:4px 8px; font-size:10px; font-weight:800; letter-spacing:.3px; white-space:nowrap; }}
    .success {{ color:var(--accent); background:rgba(86,224,195,.12); }} .failure {{ color:var(--red); background:rgba(255,111,125,.12); }}
    .warning {{ color:var(--amber); background:rgba(255,203,102,.12); }} .type-tag {{ color:#bcd0e4; font-size:11px; }}
    .empty {{ text-align:center; color:var(--muted); padding:35px; }} footer {{ color:var(--muted); margin-top:14px; font-size:12px; }}
    @media (max-width:900px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} .split {{ grid-template-columns:1fr; }} header {{ align-items:flex-start; }} }}
    @media (max-width:560px) {{ .shell {{ padding:17px; }} .grid {{ grid-template-columns:1fr; }} header {{ flex-direction:column; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div><h1>Ghost<span>Clicker</span></h1><p>Contrôle de connectivité et performance des navigations</p></div>
      <a class="download" href="results.csv" download>Télécharger le CSV</a>
    </header>

    <section class="grid">
      <article class="card"><label>Navigations</label><strong>{len(navigation_rows)}</strong><small>{successful_visits} réussies</small></article>
      <article class="card"><label>Taux de réussite</label><strong>{visit_rate:.1f}%</strong><small>Statuts HTTP 200–399</small></article>
      <article class="card"><label>Proxys vérifiés</label><strong>{successful_proxies}/{len(proxy_rows)}</strong><small>{proxy_rate:.1f}% valides</small></article>
      <article class="card"><label>Durée moyenne</label><strong>{average_duration} ms</strong><small>Navigation jusqu'au DOM</small></article>
    </section>

    <section class="split">
      <article class="panel"><h2>Réussite globale</h2><div class="gauge-wrap">
        <div class="gauge"><b>{visit_rate:.1f}%</b></div>
        <div class="gauge-copy"><strong>{successful_visits} succès</strong><p>{len(navigation_rows)-successful_visits} échecs</p><p>{len(run_ids)} exécution(s) enregistrée(s)</p></div>
      </div></article>
      <article class="panel"><h2>Performance par pays configuré</h2>{''.join(country_cards)}</article>
    </section>

    <section class="panel">
      <h2>Journal des résultats</h2>
      <div class="toolbar">
        <input id="search" type="search" placeholder="Rechercher une URL, une IP, un proxy…" aria-label="Rechercher">
        <select id="resultFilter" aria-label="Filtrer par résultat"><option value="">Tous les résultats</option><option value="success">Succès</option><option value="failure">Échecs</option><option value="warning">Alertes</option></select>
        <select id="countryFilter" aria-label="Filtrer par pays"><option value="">Tous les pays</option><option value="US">US</option><option value="CA">CA</option></select>
        <select id="typeFilter" aria-label="Filtrer par type"><option value="">Tous les types</option><option value="navigation">Navigation</option><option value="proxy_test">Test proxy</option><option value="validation">Validation</option><option value="system">Système</option></select>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Date et heure</th><th>Type</th><th>Proxy</th><th>URL initiale</th><th>URL finale</th><th>Pays configuré</th><th>Pays détecté</th><th>IP de sortie</th><th>HTTP</th><th>Résultat</th><th>Durée (ms)</th><th>Erreur</th></tr></thead>
        <tbody id="resultsBody">{''.join(table_rows)}{no_data}</tbody>
      </table></div>
      <footer>{html.escape(truncation_note)}Dernière exécution : {html.escape(latest_run)} · Dashboard généré le {html.escape(generated_at)}.</footer>
    </section>
  </main>
  <script>
    (() => {{
      const search = document.getElementById('search');
      const result = document.getElementById('resultFilter');
      const country = document.getElementById('countryFilter');
      const type = document.getElementById('typeFilter');
      const rows = [...document.querySelectorAll('#resultsBody tr[data-result]')];
      function filterRows() {{
        const query = search.value.trim().toLowerCase();
        rows.forEach(row => {{
          const visible = (!query || row.dataset.search.includes(query)) &&
            (!result.value || row.dataset.result === result.value) &&
            (!country.value || row.dataset.country === country.value) &&
            (!type.value || row.dataset.type === type.value);
          row.hidden = !visible;
        }});
      }}
      [search, result, country, type].forEach(control => control.addEventListener('input', filterRows));
    }})();
  </script>
</body>
</html>
"""


def proxy_check_record(
    run_id: str, proxy: ProxyConfig, check_url: str, check: ProxyCheck
) -> ResultRecord:
    return ResultRecord(
        run_id=run_id,
        type_enregistrement="proxy_test",
        date_heure=now_iso(),
        proxy=proxy.name,
        url_initiale=check_url,
        url_finale=check.final_url,
        pays_configure=proxy.country,
        pays_detecte=check.detected_country,
        adresse_ip_sortie=check.exit_ip,
        statut_http=check.http_status or "",
        resultat_test=check.result,
        duree_ms=check.duration_ms,
        message_erreur=check.error,
    )


def run_browser_workflow(
    settings: Settings,
    proxies: Sequence[ProxyConfig],
    urls: Sequence[str],
    store: ResultStore,
    run_id: str,
) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        store.add(
            ResultRecord(
                run_id=run_id,
                type_enregistrement="system",
                date_heure=now_iso(),
                resultat_test="ECHEC_DEPENDANCE",
                message_erreur=(
                    "Playwright n'est pas installé. Exécutez pip install -r requirements.txt "
                    "puis python -m playwright install chromium."
                ),
            )
        )
        LOGGER.error("Playwright n'est pas installé : %s", exc)
        return False

    all_secrets = [
        secret
        for proxy in proxies
        for secret in (proxy.username, proxy.password)
        if secret
    ]
    active_sessions: list[ActiveProxySession] = []
    browser = None
    had_failure = False

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=settings.headless)
            except Exception as exc:
                message = redact_error(exc, all_secrets)
                store.add(
                    ResultRecord(
                        run_id=run_id,
                        type_enregistrement="system",
                        date_heure=now_iso(),
                        resultat_test="ECHEC_NAVIGATEUR",
                        message_erreur=message,
                    )
                )
                LOGGER.error("Impossible de lancer Chromium : %s", message)
                return False

            for proxy in proxies:
                context = None
                try:
                    LOGGER.info("Vérification du proxy %s (%s)…", proxy.name, proxy.country)
                    context = browser.new_context(
                        proxy=proxy.playwright_options(),
                        ignore_https_errors=settings.ignore_https_errors,
                    )
                    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
                    page = context.new_page()
                    check = check_proxy(
                        page,
                        proxy,
                        settings.ip_check_url,
                        settings.proxy_check_timeout_ms,
                        all_secrets,
                    )
                    store.add(proxy_check_record(run_id, proxy, settings.ip_check_url, check))
                    if check.success:
                        install_navigation_guard(page, settings.allowed_domains)
                        active_sessions.append(ActiveProxySession(proxy, context, page, check))
                        LOGGER.info(
                            "Proxy %s valide : pays=%s, IP=%s.",
                            proxy.name,
                            check.detected_country or "non fourni",
                            check.exit_ip,
                        )
                    else:
                        had_failure = True
                        LOGGER.warning(
                            "Proxy %s refusé : %s — %s",
                            proxy.name,
                            check.result,
                            check.error,
                        )
                        context.close()
                except Exception as exc:
                    had_failure = True
                    message = redact_error(exc, all_secrets)
                    check = ProxyCheck(False, "ECHEC_PROXY", error=message)
                    store.add(proxy_check_record(run_id, proxy, settings.ip_check_url, check))
                    LOGGER.warning("Échec du proxy %s : %s", proxy.name, message)
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass

            if not active_sessions:
                LOGGER.error("Aucun proxy n'a passé le contrôle; aucune URL ne sera visitée.")
                return False

            LOGGER.info(
                "%d URL(s) seront réparties entre %d proxy(s) valide(s).",
                len(urls),
                len(active_sessions),
            )
            for index, url in enumerate(urls):
                session = active_sessions[index % len(active_sessions)]
                LOGGER.info(
                    "Navigation %d/%d via %s : %s",
                    index + 1,
                    len(urls),
                    session.proxy.name,
                    url,
                )
                record = visit_url(
                    session.page,
                    url,
                    session.proxy,
                    session.check,
                    settings.navigation_timeout_ms,
                    run_id,
                    all_secrets,
                )
                store.add(record)
                if not record.resultat_test.startswith("SUCCES"):
                    had_failure = True
                    LOGGER.warning(
                        "Navigation en échec : %s — %s",
                        record.resultat_test,
                        record.message_erreur,
                    )
                if (
                    settings.delay_between_urls_seconds > 0
                    and index < len(urls) - 1
                ):
                    time.sleep(settings.delay_between_urls_seconds)
    finally:
        for session in active_sessions:
            try:
                session.context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    return not had_failure


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    base_dir = Path(__file__).resolve().parent
    run_id = new_run_id()

    try:
        settings = load_settings(base_dir)
    except ConfigurationError as exc:
        LOGGER.error("Configuration invalide : %s", exc)
        return 2

    store = ResultStore(settings.results_dir, settings.max_dashboard_rows)
    workflow_successful = False

    try:
        try:
            urls, invalid_links = load_links(
                settings.links_file, settings.allowed_domains
            )
            proxies, proxy_issues = load_proxy_configs(settings.proxies_file)
        except ConfigurationError as exc:
            store.add(
                ResultRecord(
                    run_id=run_id,
                    type_enregistrement="system",
                    date_heure=now_iso(),
                    resultat_test="CONFIGURATION_INVALIDE",
                    message_erreur=str(exc),
                )
            )
            LOGGER.error("Configuration invalide : %s", exc)
            return 2

        for invalid in invalid_links:
            store.add(
                ResultRecord(
                    run_id=run_id,
                    type_enregistrement="validation",
                    date_heure=now_iso(),
                    url_initiale=invalid.value,
                    resultat_test="URL_INVALIDE",
                    message_erreur=f"Ligne {invalid.line_number} : {invalid.message}",
                )
            )
            LOGGER.warning(
                "URL ignorée à la ligne %d : %s", invalid.line_number, invalid.message
            )

        for issue in proxy_issues:
            store.add(
                ResultRecord(
                    run_id=run_id,
                    type_enregistrement="validation",
                    date_heure=now_iso(),
                    proxy=issue.name,
                    pays_configure=issue.country,
                    resultat_test="CONFIGURATION_INVALIDE",
                    message_erreur=issue.message,
                )
            )
            LOGGER.warning("Proxy %s ignoré : %s", issue.name, issue.message)

        if not urls:
            message = "Aucune URL valide n'a été trouvée dans links.txt."
            store.add(
                ResultRecord(
                    run_id=run_id,
                    type_enregistrement="system",
                    date_heure=now_iso(),
                    resultat_test="CONFIGURATION_INVALIDE",
                    message_erreur=message,
                )
            )
            LOGGER.error(message)
            return 2
        if not proxies:
            message = (
                "Aucun proxy utilisable. Renseignez .env avec les données exactes "
                "du fournisseur et vérifiez proxies.txt."
            )
            store.add(
                ResultRecord(
                    run_id=run_id,
                    type_enregistrement="system",
                    date_heure=now_iso(),
                    resultat_test="CONFIGURATION_INVALIDE",
                    message_erreur=message,
                )
            )
            LOGGER.error(message)
            return 2

        workflow_successful = run_browser_workflow(
            settings, proxies, urls, store, run_id
        )
        if invalid_links or proxy_issues:
            workflow_successful = False
        return 0 if workflow_successful else 1
    except KeyboardInterrupt:
        store.add(
            ResultRecord(
                run_id=run_id,
                type_enregistrement="system",
                date_heure=now_iso(),
                resultat_test="INTERROMPU",
                message_erreur="Exécution interrompue par l'utilisateur.",
            )
        )
        LOGGER.warning("Exécution interrompue; fermeture du navigateur en cours.")
        return 130
    except Exception as exc:
        loaded_proxies = locals().get("proxies", ())
        known_secrets = [
            secret
            for proxy in loaded_proxies
            for secret in (proxy.username, proxy.password)
            if secret
        ]
        message = redact_error(exc, known_secrets)
        store.add(
            ResultRecord(
                run_id=run_id,
                type_enregistrement="system",
                date_heure=now_iso(),
                resultat_test="ECHEC_INTERNE",
                message_erreur=message,
            )
        )
        # Pas de traceback ici : l'exception d'une bibliothèque pourrait contenir
        # une configuration d'authentification non filtrée.
        LOGGER.error("Erreur interne : %s", message)
        return 1
    finally:
        try:
            store.render_dashboard()
            LOGGER.info("Dashboard : %s", store.dashboard_path)
            LOGGER.info("CSV : %s", store.csv_path)
        except Exception as exc:
            LOGGER.error("Impossible de générer le dashboard : %s", redact_error(exc))


if __name__ == "__main__":
    raise SystemExit(main())
