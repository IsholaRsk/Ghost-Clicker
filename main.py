"""Moteur Playwright : répétitions configurables et un ou deux clics par cible."""

from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PROXY_GATEWAY_FILE = "/tmp/ghost-clicker-proxy-gateway.txt"
EXECUTION_MODES = frozenset({"fixed", "range", "continuous"})
MAX_CONFIGURED_REPETITIONS = 10_000
MAX_CONTINUOUS_RESULTS = 1_000
CONTINUOUS_WINDOW_SECONDS = 5 * 60


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
    second_button_text: str = ""


@dataclass
class VisitResult:
    started_at: str
    initial_url: str
    button_text: str
    second_button_text: str
    repetition: int = 0
    continuous_window: int = 0
    window_repetition_count: int = 0
    proxy_name: str = ""
    proxy_attempts: int = 0
    final_url: str = ""
    initial_status: int | None = None
    final_status: int | None = None
    success: bool = False
    error: str = ""
    duration_ms: int = 0


@dataclass
class RunReport:
    started_at: str
    execution_mode: str = "fixed"
    repetition_count: int | None = 1
    completed_repetitions: int = 0
    completed_visits: int = 0
    failed_visits: int = 0
    continuous_window: int = 0
    window_repetition_count: int = 0
    stopped: bool = False
    discarded_results: int = 0
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


def resolve_repetition_count(
    mode: str,
    fixed_count: int,
    minimum: int,
    maximum: int,
) -> int | None:
    """Valide le plan et renvoie le total, ou None pour le mode continu."""

    normalized_mode = mode.strip().lower()
    if normalized_mode not in EXECUTION_MODES:
        raise ConfigurationError(
            "Le mode d'exécution doit être fixed, range ou continuous."
        )
    values = {
        "nombre de répétitions": fixed_count,
        "minimum de répétitions": minimum,
        "maximum de répétitions": maximum,
    }
    for label, value in values.items():
        if value < 1 or value > MAX_CONFIGURED_REPETITIONS:
            raise ConfigurationError(
                f"Le {label} doit être compris entre 1 et "
                f"{MAX_CONFIGURED_REPETITIONS}."
            )
    if minimum > maximum:
        raise ConfigurationError(
            "Le minimum de répétitions ne peut pas dépasser le maximum."
        )
    if normalized_mode == "continuous":
        return None
    if normalized_mode == "range":
        return random.randint(minimum, maximum)
    return fixed_count


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


def validate_optional_button_text(value: str) -> str:
    text = value.strip()
    return validate_button_text(text) if text else ""


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


def page_url(page: Any) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def wait_for_navigation_outcome(
    page: Any,
    previous_url: str,
    opened_pages: Sequence[Any],
    timeout_ms: int,
) -> Any:
    """Attend une redirection de l'onglet courant ou un nouvel onglet."""

    deadline = time.monotonic() + (timeout_ms / 1_000)
    destination = None
    while destination is None:
        current_url = page_url(page)
        if current_url and current_url != previous_url:
            destination = page
            break
        for candidate in opened_pages:
            candidate_url = page_url(candidate)
            if candidate_url and candidate_url != "about:blank":
                destination = candidate
                break
        if destination is not None:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Le clic n'a déclenché ni redirection ni nouvel onglet."
            )
        try:
            page.wait_for_timeout(250)
        except Exception:
            time.sleep(0.25)

    remaining_ms = max(1_000, int((deadline - time.monotonic()) * 1_000))
    try:
        destination.wait_for_load_state("domcontentloaded", timeout=remaining_ms)
    except Exception:
        # La destination est déjà connue : une SPA peut ne pas émettre cet événement.
        pass
    return destination


def wait_for_redirect(page: Any, previous_url: str, timeout_ms: int) -> None:
    """Compatibilité : attend uniquement la redirection de la page courante."""

    wait_for_navigation_outcome(page, previous_url, [], timeout_ms)


def require_successful_target_response(response: Any) -> None:
    if response is None:
        raise RuntimeError("La page cible n'a renvoyé aucune réponse HTTP.")
    if not 200 <= response.status < 400:
        raise RuntimeError(
            f"Page cible inaccessible : statut HTTP {response.status}."
        )


def open_proxy_context(
    browser: Any,
    settings: BrowserSettings,
    result: VisitResult,
) -> tuple[Any, Any]:
    """Ouvre directement une page via la passerelle, sans requête de contrôle."""

    gateway = settings.gateway
    result.proxy_name = gateway.name
    result.proxy_attempts = 1
    context = browser.new_context(proxy=gateway.playwright_proxy())
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    return context, context.new_page()


def visit_target(
    playwright: Any,
    settings: BrowserSettings,
    target: Target,
    repetition: int,
) -> VisitResult:
    """Effectue une visite et ferme entièrement son navigateur."""

    started = time.monotonic()
    result = VisitResult(
        started_at=now_iso(),
        initial_url=target.url,
        button_text=target.button_text,
        second_button_text=target.second_button_text,
        repetition=repetition,
    )
    browser = None
    context = None
    page = None
    navigation_status: dict[str, int | None] = {"value": None}
    record_navigation_handler: Any | None = None
    new_page_handler: Any | None = None
    opened_pages: list[Any] = []

    try:
        browser = playwright.chromium.launch(headless=settings.headless)
        context, page = open_proxy_context(browser, settings, result)
        install_request_guard(page)

        def record_new_page(new_page: Any) -> None:
            if new_page is not page:
                opened_pages.append(new_page)

        new_page_handler = record_new_page

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
        first_click_target = page.get_by_text(target.button_text, exact=True).first
        first_click_target.wait_for(
            state="visible", timeout=settings.click_timeout_ms
        )
        box = first_click_target.bounding_box()
        if box is None:
            raise RuntimeError("Impossible de déterminer la position du bouton.")
        click_x = box["x"] + (box["width"] / 2)
        click_y = box["y"] + (box["height"] / 2)
        previous_url = page.url
        navigation_status["value"] = None
        context.on("page", new_page_handler)
        first_click_target.click(timeout=settings.click_timeout_ms)

        if target.second_button_text:
            page.wait_for_timeout(500)
            popup_already_open = any(
                page_url(candidate) not in {"", "about:blank"}
                for candidate in opened_pages
            )
            if page_url(page) == previous_url and not popup_already_open:
                page.mouse.click(click_x, click_y)

        destination_page = wait_for_navigation_outcome(
            page,
            previous_url,
            opened_pages,
            settings.redirect_timeout_ms,
        )

        if settings.wait_after_redirect_seconds:
            destination_page.wait_for_timeout(
                int(settings.wait_after_redirect_seconds * 1_000)
            )
        result.final_url = page_url(destination_page)
        result.final_status = (
            navigation_status["value"] if destination_page is page else None
        )
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
        if context is not None and new_page_handler is not None:
            try:
                context.remove_listener("page", new_page_handler)
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
    return result


def wait_with_cancellation(
    seconds: float,
    should_stop: Callable[[], bool] | None,
) -> bool:
    """Attend sans retarder une demande d'arrêt ; renvoie True si elle arrive."""

    deadline = time.monotonic() + seconds
    while True:
        if should_stop is not None and should_stop():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.25, remaining))


def run_targets(
    settings: BrowserSettings,
    targets: Sequence[Target],
    repetition_count: int | None = 1,
    execution_mode: str = "fixed",
    continuous_minimum: int = 1,
    continuous_maximum: int = 10,
    continuous_window_seconds: float = CONTINUOUS_WINDOW_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[RunReport], None] | None = None,
) -> RunReport:
    if not targets:
        raise ConfigurationError("Aucune URL n'est configurée.")
    if repetition_count is not None and not (
        1 <= repetition_count <= MAX_CONFIGURED_REPETITIONS
    ):
        raise ConfigurationError(
            "Le nombre de répétitions doit être compris entre 1 et "
            f"{MAX_CONFIGURED_REPETITIONS}."
        )
    if not (
        1 <= continuous_minimum <= continuous_maximum
        <= MAX_CONFIGURED_REPETITIONS
    ):
        raise ConfigurationError(
            "L'intervalle continu doit être compris entre 1 et "
            f"{MAX_CONFIGURED_REPETITIONS}, avec un minimum inférieur "
            "ou égal au maximum."
        )
    if continuous_window_seconds < 0:
        raise ConfigurationError("La durée d'une fenêtre continue est invalide.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright n'est pas installé. Exécutez pip install -r requirements.txt."
        ) from exc

    continuous = repetition_count is None
    report = RunReport(
        started_at=now_iso(),
        execution_mode="continuous" if continuous else execution_mode,
        repetition_count=repetition_count,
    )

    def stop_requested() -> bool:
        return bool(should_stop is not None and should_stop())

    def notify_progress() -> None:
        if on_progress is None:
            return
        try:
            on_progress(report)
        except Exception:
            # Le suivi d'interface ne doit jamais interrompre le navigateur.
            pass

    with sync_playwright() as playwright:
        try:
            repetition = 0
            window_repetitions_remaining = 0
            window_deadline = 0.0
            while continuous or repetition < (repetition_count or 0):
                if stop_requested():
                    report.stopped = True
                    break

                if continuous and window_repetitions_remaining == 0:
                    report.continuous_window += 1
                    report.window_repetition_count = random.randint(
                        continuous_minimum, continuous_maximum
                    )
                    window_repetitions_remaining = (
                        report.window_repetition_count
                    )
                    window_deadline = (
                        time.monotonic() + continuous_window_seconds
                    )
                    notify_progress()

                repetition += 1
                for target_index, target in enumerate(targets):
                    if stop_requested():
                        report.stopped = True
                        break

                    result = visit_target(
                        playwright, settings, target, repetition
                    )
                    if continuous:
                        result.continuous_window = report.continuous_window
                        result.window_repetition_count = (
                            report.window_repetition_count
                        )
                    report.results.append(result)
                    report.completed_visits += 1
                    if not result.success:
                        report.failed_visits += 1
                    if continuous and len(report.results) > MAX_CONTINUOUS_RESULTS:
                        del report.results[0]
                        report.discarded_results += 1
                    if target_index == len(targets) - 1:
                        report.completed_repetitions = repetition
                    notify_progress()

                    more_targets = target_index < len(targets) - 1
                    more_repetitions = continuous or repetition < (
                        repetition_count or 0
                    )
                    if (
                        settings.delay_between_targets_seconds
                        and (more_targets or more_repetitions)
                        and wait_with_cancellation(
                            settings.delay_between_targets_seconds,
                            should_stop,
                        )
                    ):
                        report.stopped = True
                        break

                if report.stopped:
                    break

                if continuous:
                    window_repetitions_remaining -= 1
                    if window_repetitions_remaining == 0:
                        remaining_window_seconds = max(
                            0.0, window_deadline - time.monotonic()
                        )
                        if remaining_window_seconds and wait_with_cancellation(
                            remaining_window_seconds,
                            should_stop,
                        ):
                            report.stopped = True
                            break

            completed_plan = continuous or (
                report.completed_repetitions == repetition_count
            )
            report.success = (
                report.completed_visits > 0
                and report.failed_visits == 0
                and completed_plan
            )
        except Exception as exc:
            report.error = redact_error(exc, settings)
            report.success = False
        finally:
            report.finished_at = now_iso()
            notify_progress()
    return report


def main() -> int:
    """Mode local facultatif avec une URL et deux libellés dans .env."""

    try:
        base_dir = Path(__file__).resolve().parent
        settings = load_browser_settings(base_dir)
        target = Target(
            url=validate_public_url(required_env("TARGET_URL")),
            button_text=validate_button_text(required_env("BUTTON_TEXT")),
            second_button_text=validate_optional_button_text(
                os.getenv("SECOND_BUTTON_TEXT", "")
            ),
        )
        execution_mode = os.getenv("EXECUTION_MODE", "fixed").strip().lower()
        fixed_count = parse_positive_int(
            os.getenv("REPETITION_COUNT"), 1, "REPETITION_COUNT"
        )
        repetition_minimum = parse_positive_int(
            os.getenv("REPETITION_MIN"), 1, "REPETITION_MIN"
        )
        repetition_maximum = parse_positive_int(
            os.getenv("REPETITION_MAX"), 10, "REPETITION_MAX"
        )
        repetition_count = resolve_repetition_count(
            execution_mode,
            fixed_count,
            repetition_minimum,
            repetition_maximum,
        )
        report = run_targets(
            settings,
            [target],
            repetition_count=repetition_count,
            execution_mode=execution_mode,
            continuous_minimum=repetition_minimum,
            continuous_maximum=repetition_maximum,
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.success else 1
    except (ConfigurationError, RuntimeError) as exc:
        print(f"Erreur : {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
