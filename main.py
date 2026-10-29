"""Moteur Playwright : un proxy vérifié et un clic par URL publique."""

from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Configuration absente ou invalide."""


@dataclass(frozen=True)
class ProxyConfig:
    name: str
    server: str
    username: str = ""
    password: str = ""

    def playwright_proxy(self) -> dict[str, str]:
        proxy = {"server": self.server}
        if self.username and self.password:
            proxy["username"] = self.username
            proxy["password"] = self.password
        return proxy


@dataclass(frozen=True)
class BrowserSettings:
    proxies: tuple[ProxyConfig, ...]
    ip_check_url: str
    headless: bool
    navigation_timeout_ms: int
    click_timeout_ms: int
    redirect_timeout_ms: int
    wait_after_redirect_seconds: float
    delay_between_targets_seconds: float


@dataclass(frozen=True)
class Target:
    url: str
    button_text: str


@dataclass
class VisitResult:
    started_at: str
    initial_url: str
    button_text: str
    proxy_name: str = ""
    proxy_attempts: int = 0
    exit_ip: str = ""
    proxy_country: str = ""
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


def validate_public_url(value: str) -> str:
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


def parse_proxy_list_text(value: str) -> tuple[ProxyConfig, ...]:
    """Lit le format Rayobyte : username:password@host:port, une ligne par proxy."""

    proxies: list[ProxyConfig] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        try:
            authentication, address = line.rsplit("@", maxsplit=1)
            username, password = authentication.split(":", maxsplit=1)
            host, port = address.rsplit(":", maxsplit=1)
        except ValueError as exc:
            raise ConfigurationError(
                f"Proxy ligne {line_number} : format username:password@host:port attendu."
            ) from exc
        username = username.strip()
        password = password.strip()
        host = host.strip()
        port = port.strip()
        if not username or not password or not host or not port.isdigit():
            raise ConfigurationError(f"Proxy ligne {line_number} : valeur incomplète.")
        server = validate_proxy_server(f"http://{host}:{port}")
        proxies.append(
            ProxyConfig(f"proxy-{len(proxies) + 1:03d}", server, username, password)
        )
        seen.add(line)
        if len(proxies) > 100:
            raise ConfigurationError("Le fichier accepte au maximum 100 proxys uniques.")

    if not proxies:
        raise ConfigurationError("Le fichier de proxys ne contient aucune ligne valide.")
    return tuple(proxies)


def load_proxy_file(path: Path) -> tuple[ProxyConfig, ...]:
    if not path.exists():
        raise ConfigurationError(
            "Aucun fichier de proxys. Téléversez la liste Rayobyte dans l'interface."
        )
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("Impossible de lire le fichier de proxys.") from exc
    return parse_proxy_list_text(content)


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

    proxy_path = Path(os.getenv("PROXY_LIST_FILE", "proxies.txt"))
    if not proxy_path.is_absolute():
        proxy_path = base_dir / proxy_path
    return BrowserSettings(
        proxies=load_proxy_file(proxy_path),
        ip_check_url=validate_public_url(
            os.getenv("IP_CHECK_URL", "https://ipinfo.io/json")
        ),
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
    secrets = [
        secret
        for proxy in settings.proxies
        for secret in (proxy.username, proxy.password)
        if secret
    ]
    for secret in sorted(set(secrets), key=len, reverse=True):
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


def install_request_guard(page: Any) -> None:
    """Bloque uniquement les requêtes locales, privées ou réservées."""

    def handle(route: Any, request: Any) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme.lower() not in {"http", "https"}:
            route.continue_()
            return
        try:
            validate_public_url(request.url)
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


def choose_proxy_candidates(
    proxies: Sequence[ProxyConfig], rng: Any | None = None
) -> list[ProxyConfig]:
    """Choisit un proxy au hasard et un second distinct pour l'unique reprise."""

    if not proxies:
        raise ConfigurationError("La liste de proxys est vide.")
    random_source = rng or random.SystemRandom()
    return random_source.sample(list(proxies), min(2, len(proxies)))


def check_proxy(page: Any, settings: BrowserSettings) -> tuple[str, str]:
    response = page.goto(
        settings.ip_check_url,
        wait_until="domcontentloaded",
        timeout=settings.navigation_timeout_ms,
    )
    if response is None or not 200 <= response.status < 300:
        status = response.status if response else "aucun"
        raise RuntimeError(f"Contrôle proxy refusé : statut HTTP {status}.")
    try:
        payload = response.json()
    except Exception:
        payload = json.loads(response.text())
    exit_ip, country = extract_ip_information(payload)
    if not exit_ip:
        raise RuntimeError("Le contrôle proxy n'a renvoyé aucune adresse IP.")
    return exit_ip, country


def open_verified_proxy_context(
    browser: Any,
    settings: BrowserSettings,
    result: VisitResult,
    rng: Any | None = None,
) -> tuple[Any, Any]:
    """Vérifie le proxy aléatoire, puis essaie une seule fois un proxy distinct."""

    errors: list[str] = []
    for proxy in choose_proxy_candidates(settings.proxies, rng):
        context = None
        result.proxy_attempts += 1
        result.proxy_name = proxy.name
        try:
            context = browser.new_context(proxy=proxy.playwright_proxy())
            context.set_default_navigation_timeout(settings.navigation_timeout_ms)
            page = context.new_page()
            result.exit_ip, result.proxy_country = check_proxy(page, settings)
            return context, page
        except Exception as exc:
            errors.append(f"{proxy.name}: {redact_error(exc, settings)}")
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    raise RuntimeError("; ".join(errors) or "Aucun proxy disponible.")


def run_targets(
    settings: BrowserSettings,
    targets: Sequence[Target],
    rng: Any | None = None,
) -> RunReport:
    if not targets:
        raise ConfigurationError("Aucune URL n'est configurée.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright n'est pas installé. Exécutez pip install -r requirements.txt."
        ) from exc

    random_source = rng or random.SystemRandom()
    report = RunReport(started_at=now_iso())
    with sync_playwright() as playwright:
        try:
            for index, target in enumerate(targets):
                started = time.monotonic()
                result = VisitResult(
                    started_at=now_iso(),
                    initial_url=target.url,
                    button_text=target.button_text,
                )
                browser = None
                context = None
                page = None
                navigation_status: dict[str, int | None] = {"value": None}
                record_navigation_handler: Any | None = None

                try:
                    browser = playwright.chromium.launch(headless=settings.headless)
                    context, page = open_verified_proxy_context(
                        browser, settings, result, random_source
                    )
                    install_request_guard(page)

                    def record_navigation(
                        response: Any,
                        current_page: Any = page,
                        status_holder: dict[str, int | None] = navigation_status,
                    ) -> None:
                        try:
                            if (
                                response.request.is_navigation_request()
                                and response.frame == current_page.main_frame
                            ):
                                status_holder["value"] = response.status
                        except Exception:
                            return

                    record_navigation_handler = record_navigation
                    page.on("response", record_navigation_handler)
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
                    result.final_url = page.url if page is not None else ""
                    result.error = redact_error(exc, settings)
                finally:
                    if page is not None and record_navigation_handler is not None:
                        try:
                            page.remove_listener("response", record_navigation_handler)
                        except Exception:
                            pass
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
    return report


def main() -> int:
    """Mode local facultatif avec une URL et un texte de bouton dans .env."""

    try:
        base_dir = Path(__file__).resolve().parent
        settings = load_browser_settings(base_dir)
        target = Target(
            url=validate_public_url(required_env("TARGET_URL")),
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
