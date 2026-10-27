"""Moteur Playwright : un proxy, une liste autorisée, un clic par URL."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Configuration absente ou invalide."""


@dataclass(frozen=True)
class BrowserSettings:
    proxy_server: str
    proxy_username: str
    proxy_password: str
    ip_check_url: str
    allowed_domains: tuple[str, ...]
    headless: bool
    navigation_timeout_ms: int
    click_timeout_ms: int
    redirect_timeout_ms: int
    wait_after_redirect_seconds: float
    delay_between_targets_seconds: float

    def playwright_proxy(self) -> dict[str, str]:
        proxy = {"server": self.proxy_server}
        if self.proxy_username and self.proxy_password:
            proxy["username"] = self.proxy_username
            proxy["password"] = self.proxy_password
        return proxy


@dataclass(frozen=True)
class Target:
    url: str
    button_text: str


@dataclass
class VisitResult:
    started_at: str
    initial_url: str
    button_text: str
    final_url: str = ""
    initial_status: int | None = None
    final_status: int | None = None
    success: bool = False
    error: str = ""
    duration_ms: int = 0


@dataclass
class RunReport:
    started_at: str
    finished_at: str = ""
    exit_ip: str = ""
    proxy_country: str = ""
    success: bool = False
    error: str = ""
    results: list[VisitResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["results"] = [asdict(result) for result in self.results]
        return value


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"La variable {name} est absente ou vide.")
    return value


def parse_bool(value: str | None, default: bool, name: str) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "oui", "on"}:
        return True
    if normalized in {"0", "false", "no", "non", "off"}:
        return False
    raise ConfigurationError(f"{name} doit valoir true ou false.")


def parse_positive_int(
    value: str | None, default: int, name: str, minimum: int = 1
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} doit être un entier.") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} doit être supérieur ou égal à {minimum}.")
    return parsed


def parse_nonnegative_float(value: str | None, default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} doit être un nombre.") from exc
    if parsed < 0:
        raise ConfigurationError(f"{name} ne peut pas être négatif.")
    return parsed


def parse_allowed_domains(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        raise ConfigurationError(
            "ALLOWED_DOMAINS est obligatoire pour limiter l'automatisation aux sites autorisés."
        )
    domains: list[str] = []
    for raw in value.split(","):
        domain = raw.strip().lower().rstrip(".")
        if not domain:
            continue
        if "://" in domain or "/" in domain or ":" in domain:
            raise ConfigurationError(
                "ALLOWED_DOMAINS attend des domaines sans protocole, chemin ou port."
            )
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigurationError(f"Domaine invalide : {domain!r}.") from exc
        if domain not in domains:
            domains.append(domain)
    if not domains:
        raise ConfigurationError("ALLOWED_DOMAINS ne contient aucun domaine.")
    return tuple(domains)


def validate_public_url(value: str, allowed_domains: Sequence[str] = ()) -> str:
    url = value.strip()
    if not url:
        raise ConfigurationError("URL vide.")
    if len(url) > 2_048:
        raise ConfigurationError("URL trop longue.")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ConfigurationError("URL mal formée.") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ConfigurationError("Seules les URL http et https sont autorisées.")
    if not parsed.hostname:
        raise ConfigurationError("Nom d'hôte absent dans l'URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("Les identifiants intégrés à une URL sont interdits.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Port invalide dans l'URL.") from exc

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ConfigurationError("Les adresses locales sont interdites.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ConfigurationError("Les adresses privées ou réservées sont interdites.")

    if allowed_domains and not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in allowed_domains
    ):
        raise ConfigurationError(
            f"Le domaine {hostname} n'est pas présent dans ALLOWED_DOMAINS."
        )
    return url


def validate_proxy_server(value: str) -> str:
    server = value.strip().rstrip("/")
    try:
        parsed = urlsplit(server)
    except ValueError as exc:
        raise ConfigurationError("PROXY_SERVER est mal formé.") from exc
    if parsed.scheme.lower() not in {"http", "https", "socks5"}:
        raise ConfigurationError(
            "PROXY_SERVER doit commencer par http://, https:// ou socks5://."
        )
    if not parsed.hostname:
        raise ConfigurationError("Hôte absent dans PROXY_SERVER.")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(
            "Placez l'utilisateur et le mot de passe dans les variables séparées."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Port de proxy invalide.") from exc
    if port is None:
        raise ConfigurationError("Port absent dans PROXY_SERVER.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigurationError("PROXY_SERVER ne doit contenir ni chemin ni requête.")
    return server


def validate_button_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise ConfigurationError("Le texte du bouton est vide.")
    if len(text) > 200:
        raise ConfigurationError("Le texte du bouton dépasse 200 caractères.")
    return text


def load_browser_settings(base_dir: Path) -> BrowserSettings:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ConfigurationError(
            "python-dotenv n'est pas installé. Exécutez pip install -r requirements.txt."
        ) from exc
    load_dotenv(base_dir / ".env", override=False)

    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()
    if bool(username) != bool(password):
        raise ConfigurationError(
            "PROXY_USERNAME et PROXY_PASSWORD doivent être tous les deux remplis."
        )

    domains = parse_allowed_domains(os.getenv("ALLOWED_DOMAINS"))
    return BrowserSettings(
        proxy_server=validate_proxy_server(required_env("PROXY_SERVER")),
        proxy_username=username,
        proxy_password=password,
        ip_check_url=validate_public_url(
            os.getenv("IP_CHECK_URL", "https://ipinfo.io/json")
        ),
        allowed_domains=domains,
        headless=parse_bool(os.getenv("HEADLESS"), True, "HEADLESS"),
        navigation_timeout_ms=parse_positive_int(
            os.getenv("NAVIGATION_TIMEOUT_MS"), 30_000, "NAVIGATION_TIMEOUT_MS", 1_000
        ),
        click_timeout_ms=parse_positive_int(
            os.getenv("CLICK_TIMEOUT_MS"), 15_000, "CLICK_TIMEOUT_MS", 1_000
        ),
        redirect_timeout_ms=parse_positive_int(
            os.getenv("REDIRECT_TIMEOUT_MS"), 30_000, "REDIRECT_TIMEOUT_MS", 1_000
        ),
        wait_after_redirect_seconds=parse_nonnegative_float(
            os.getenv("WAIT_AFTER_REDIRECT_SECONDS"),
            2.0,
            "WAIT_AFTER_REDIRECT_SECONDS",
        ),
        delay_between_targets_seconds=parse_nonnegative_float(
            os.getenv("DELAY_BETWEEN_TARGETS_SECONDS"),
            3.0,
            "DELAY_BETWEEN_TARGETS_SECONDS",
        ),
    )


def redact_error(error: BaseException | str, settings: BrowserSettings) -> str:
    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    for secret in (settings.proxy_username, settings.proxy_password):
        if secret:
            message = message.replace(secret, "***")
    message = re.sub(
        r"(?i)(https?|socks5)://[^/@\s]+@",
        lambda match: f"{match.group(1)}://***@",
        message,
    )
    return message[:1_000]


def extract_ip_information(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    ip = str(payload.get("ip") or payload.get("query") or "").strip()
    country = str(
        payload.get("country")
        or payload.get("country_code")
        or payload.get("countryCode")
        or ""
    ).strip()
    return ip, country


def install_request_guard(page: Any, allowed_domains: Sequence[str]) -> None:
    """Bloque les navigations privées et les redirections hors liste blanche."""

    def handle(route: Any, request: Any) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme.lower() not in {"http", "https"}:
            route.continue_()
            return
        try:
            is_main_document = (
                request.is_navigation_request() and request.frame == page.main_frame
            )
        except Exception:
            is_main_document = False
        try:
            validate_public_url(
                request.url, allowed_domains if is_main_document else ()
            )
        except ConfigurationError:
            route.abort("blockedbyclient")
            return
        route.continue_()

    page.route("**/*", handle)


def wait_for_redirect(page: Any, previous_url: str, timeout_ms: int) -> None:
    deadline = time.monotonic() + (timeout_ms / 1_000)
    while page.url == previous_url:
        if time.monotonic() >= deadline:
            raise TimeoutError("Le clic n'a déclenché aucune redirection.")
        page.wait_for_timeout(250)
    remaining_ms = max(1_000, int((deadline - time.monotonic()) * 1_000))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=remaining_ms)
    except Exception:
        # L'URL a déjà changé : un site SPA peut ne pas produire de nouvel événement DOM.
        pass


def run_targets(settings: BrowserSettings, targets: Sequence[Target]) -> RunReport:
    if not targets:
        raise ConfigurationError("Aucune URL n'est configurée.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright n'est pas installé. Exécutez pip install -r requirements.txt."
        ) from exc

    report = RunReport(started_at=now_iso())
    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(headless=settings.headless)
            context = browser.new_context(proxy=settings.playwright_proxy())
            context.set_default_navigation_timeout(settings.navigation_timeout_ms)
            page = context.new_page()

            check_response = page.goto(
                settings.ip_check_url,
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )
            if check_response is None or not 200 <= check_response.status < 300:
                status = check_response.status if check_response else "aucun"
                raise RuntimeError(f"Contrôle proxy refusé : statut HTTP {status}.")
            try:
                payload = check_response.json()
            except Exception:
                payload = json.loads(check_response.text())
            report.exit_ip, report.proxy_country = extract_ip_information(payload)
            if not report.exit_ip:
                raise RuntimeError("Le contrôle proxy n'a renvoyé aucune adresse IP.")

            install_request_guard(page, settings.allowed_domains)

            for index, target in enumerate(targets):
                started = time.monotonic()
                result = VisitResult(
                    started_at=now_iso(),
                    initial_url=target.url,
                    button_text=target.button_text,
                )
                navigation_status: dict[str, int | None] = {"value": None}

                def record_navigation(response: Any) -> None:
                    try:
                        if (
                            response.request.is_navigation_request()
                            and response.frame == page.main_frame
                        ):
                            navigation_status["value"] = response.status
                    except Exception:
                        return

                page.on("response", record_navigation)
                try:
                    response = page.goto(
                        target.url,
                        wait_until="domcontentloaded",
                        timeout=settings.navigation_timeout_ms,
                    )
                    result.initial_status = response.status if response else None
                    previous_url = page.url
                    navigation_status["value"] = None

                    button = page.get_by_role(
                        "button", name=target.button_text, exact=True
                    ).first
                    button.wait_for(state="visible", timeout=settings.click_timeout_ms)
                    button.click(timeout=settings.click_timeout_ms)
                    wait_for_redirect(
                        page, previous_url, settings.redirect_timeout_ms
                    )

                    if settings.wait_after_redirect_seconds:
                        page.wait_for_timeout(
                            int(settings.wait_after_redirect_seconds * 1_000)
                        )
                    result.final_url = page.url
                    result.final_status = navigation_status["value"]
                    result.success = True
                except Exception as exc:
                    result.final_url = page.url
                    result.error = redact_error(exc, settings)
                finally:
                    page.remove_listener("response", record_navigation)
                    result.duration_ms = round((time.monotonic() - started) * 1_000)
                    report.results.append(result)

                if (
                    settings.delay_between_targets_seconds
                    and index < len(targets) - 1
                ):
                    time.sleep(settings.delay_between_targets_seconds)

            report.success = bool(report.results) and all(
                result.success for result in report.results
            )
        except Exception as exc:
            report.error = redact_error(exc, settings)
            report.success = False
        finally:
            report.finished_at = now_iso()
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    return report


def main() -> int:
    """Mode local facultatif avec une URL et un texte de bouton dans .env."""

    try:
        base_dir = Path(__file__).resolve().parent
        settings = load_browser_settings(base_dir)
        target = Target(
            url=validate_public_url(
                required_env("TARGET_URL"), settings.allowed_domains
            ),
            button_text=validate_button_text(required_env("BUTTON_TEXT")),
        )
        report = run_targets(settings, [target])
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.success else 1
    except (ConfigurationError, RuntimeError) as exc:
        print(f"Erreur : {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
