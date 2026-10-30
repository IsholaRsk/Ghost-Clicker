"""Moteur Playwright : un proxy vérifié et deux clics par URL publique."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PROXY_GATEWAY_FILE = "/tmp/ghost-clicker-proxy-gateway.txt"


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
    gateway: ProxyConfig
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
    second_button_text: str


@dataclass
class VisitResult:
    started_at: str
    initial_url: str
    button_text: str
    second_button_text: str
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


def clean_url_boundaries(value: str) -> str:
    """Retire les espaces et caractères Unicode invisibles copiés aux extrémités."""

    def is_boundary_noise(character: str) -> bool:
        return character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}

    start = 0
    end = len(value)
    while start < end and is_boundary_noise(value[start]):
        start += 1
    while end > start and is_boundary_noise(value[end - 1]):
        end -= 1
    return value[start:end]


def validate_public_url(value: str) -> str:
    url = clean_url_boundaries(value)
    if not url:
        raise ConfigurationError("URL vide.")
    if any(
        character.isspace()
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in url
    ):
        raise ConfigurationError(
            "L'URL contient un espace ou un caractère invisible non autorisé."
        )
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


def parse_proxy_gateway_text(value: str) -> ProxyConfig:
    """Lit une passerelle rotative : username:password@host:port."""

    lines = [
        (line_number, raw_line.strip())
        for line_number, raw_line in enumerate(value.splitlines(), start=1)
        if raw_line.strip() and not raw_line.strip().startswith("#")
    ]
    if not lines:
        raise ConfigurationError("Le fichier ne contient aucune passerelle proxy.")
    if len(lines) != 1:
        raise ConfigurationError(
            "Le fichier doit contenir une seule passerelle proxy rotative."
        )

    line_number, line = lines[0]
    try:
        authentication, address = line.rsplit("@", maxsplit=1)
        username, password = authentication.split(":", maxsplit=1)
        host, port = address.rsplit(":", maxsplit=1)
    except ValueError as exc:
        raise ConfigurationError(
            f"Passerelle ligne {line_number} : format "
            "username:password@host:port attendu."
        ) from exc
    username = username.strip()
    password = password.strip()
    host = host.strip()
    port = port.strip()
    if not username or not password or not host or not port.isdigit():
        raise ConfigurationError(
            f"Passerelle ligne {line_number} : valeur incomplète."
        )
    server = validate_proxy_server(f"http://{host}:{port}")
    return ProxyConfig("passerelle-rotative", server, username, password)


def load_proxy_gateway(path: Path) -> ProxyConfig:
    if not path.exists():
        raise ConfigurationError(
            "Aucune passerelle proxy. Enregistrez-la dans l'interface."
        )
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(
            "Impossible de lire le fichier de passerelle proxy."
        ) from exc
    return parse_proxy_gateway_text(content)


def validate_button_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise ConfigurationError("Le texte du bouton est vide.")
    if len(text) > 200:
        raise ConfigurationError("Le texte du bouton dépasse 200 caractères.")
    return text


def proxy_gateway_path(base_dir: Path) -> Path:
    path = Path(os.getenv("PROXY_GATEWAY_FILE", DEFAULT_PROXY_GATEWAY_FILE))
    return path if path.is_absolute() else base_dir / path


def load_browser_settings(base_dir: Path) -> BrowserSettings:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ConfigurationError(
            "python-dotenv n'est pas installé. Exécutez pip install -r requirements.txt."
        ) from exc
    load_dotenv(base_dir / ".env", override=False)

    return BrowserSettings(
        gateway=load_proxy_gateway(proxy_gateway_path(base_dir)),
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
        for secret in (settings.gateway.username, settings.gateway.password)
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


def normalize_visible_text(value: str) -> str:
    return " ".join(value.split())


def visible_text_at_point(page: Any, x: float, y: float) -> str:
    value = page.evaluate(
        """({x, y}) => {
            const element = document.elementFromPoint(x, y);
            if (!element) return '';
            const clickable = element.closest(
                'button, a, [role="button"], [role="link"]'
            );
            const source = clickable || element;
            return source.innerText || source.textContent || '';
        }""",
        {"x": x, "y": y},
    )
    return normalize_visible_text(str(value or ""))


def wait_for_text_change_at_point(
    page: Any, x: float, y: float, previous_text: str, timeout_ms: int
) -> str:
    """Attend que le contenu visible au même emplacement change."""

    previous = normalize_visible_text(previous_text)
    deadline = time.monotonic() + (timeout_ms / 1_000)
    while True:
        current = visible_text_at_point(page, x, y)
        if current and current != previous:
            return current
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Le contenu du bouton n'a pas changé après le premier clic."
            )
        page.wait_for_timeout(250)


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


def require_successful_target_response(response: Any) -> None:
    if response is None:
        raise RuntimeError("La page cible n'a renvoyé aucune réponse HTTP.")
    if not 200 <= response.status < 400:
        raise RuntimeError(
            f"Page cible inaccessible : statut HTTP {response.status}."
        )


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
) -> tuple[Any, Any]:
    """Vérifie la passerelle et recrée une connexion une seule fois en cas d'échec."""

    errors: list[str] = []
    gateway = settings.gateway
    result.proxy_name = gateway.name
    for attempt in range(1, 3):
        context = None
        result.proxy_attempts += 1
        try:
            context = browser.new_context(proxy=gateway.playwright_proxy())
            context.set_default_navigation_timeout(settings.navigation_timeout_ms)
            page = context.new_page()
            result.exit_ip, result.proxy_country = check_proxy(page, settings)
            return context, page
        except Exception as exc:
            errors.append(
                f"tentative {attempt}: {redact_error(exc, settings)}"
            )
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    raise RuntimeError("; ".join(errors))


def run_targets(
    settings: BrowserSettings,
    targets: Sequence[Target],
) -> RunReport:
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
        try:
            for index, target in enumerate(targets):
                started = time.monotonic()
                result = VisitResult(
                    started_at=now_iso(),
                    initial_url=target.url,
                    button_text=target.button_text,
                    second_button_text=target.second_button_text,
                )
                browser = None
                context = None
                page = None
                navigation_status: dict[str, int | None] = {"value": None}
                record_navigation_handler: Any | None = None

                try:
                    browser = playwright.chromium.launch(headless=settings.headless)
                    context, page = open_verified_proxy_context(
                        browser, settings, result
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
                    require_successful_target_response(response)
                    first_click_target = page.get_by_text(
                        target.button_text, exact=True
                    ).first
                    first_click_target.wait_for(
                        state="visible", timeout=settings.click_timeout_ms
                    )
                    box = first_click_target.bounding_box()
                    if box is None:
                        raise RuntimeError(
                            "Impossible de déterminer la position du bouton."
                        )
                    click_x = box["x"] + (box["width"] / 2)
                    click_y = box["y"] + (box["height"] / 2)
                    initial_text = (
                        visible_text_at_point(page, click_x, click_y)
                        or target.button_text
                    )
                    first_click_target.click(timeout=settings.click_timeout_ms)

                    wait_for_text_change_at_point(
                        page,
                        click_x,
                        click_y,
                        initial_text,
                        settings.click_timeout_ms,
                    )
                    previous_url = page.url
                    navigation_status["value"] = None
                    page.mouse.click(click_x, click_y)
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
    """Mode local facultatif avec une URL et deux libellés dans .env."""

    try:
        base_dir = Path(__file__).resolve().parent
        settings = load_browser_settings(base_dir)
        target = Target(
            url=validate_public_url(required_env("TARGET_URL")),
            button_text=validate_button_text(required_env("BUTTON_TEXT")),
            second_button_text=validate_button_text(
                required_env("SECOND_BUTTON_TEXT")
            ),
        )
        report = run_targets(settings, [target])
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.success else 1
    except (ConfigurationError, RuntimeError) as exc:
        print(f"Erreur : {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
