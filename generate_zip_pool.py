"""Génère un pool Rayobyte US ciblé par ZIP avec des sessions uniques.

Le script lit le serveur, l'utilisateur et le mot de passe de base depuis .env,
choisit des ZIP dans zip_codes_us.txt, ajoute une session alphanumérique conforme
à la documentation Rayobyte, puis met à jour .env et proxies.txt.

Aucun secret n'est affiché et aucune requête réseau n'est envoyée.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
from collections import Counter
from collections.abc import MutableMapping
from pathlib import Path

POOL_START = "# --- GHOST_CLICKER_ZIP_POOL_START ---"
POOL_END = "# --- GHOST_CLICKER_ZIP_POOL_END ---"
SESSION_ALPHABET = string.ascii_lowercase + string.digits
ZIP_RE = re.compile(r"^\d{5}$")


class PoolConfigurationError(ValueError):
    """Configuration locale manquante ou dangereuse."""


def load_dotenv_values(path: Path) -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise PoolConfigurationError(
            "python-dotenv n'est pas installé. Exécutez : "
            "python -m pip install -r requirements.txt"
        ) from exc
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if key and value is not None
    }


def load_zip_codes(path: Path) -> list[str]:
    if not path.exists():
        raise PoolConfigurationError(f"Fichier ZIP introuvable : {path}")

    codes: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        if not ZIP_RE.fullmatch(value):
            raise PoolConfigurationError(
                f"Code ZIP invalide à la ligne {line_number} : cinq chiffres attendus."
            )
        if value not in codes:
            codes.append(value)

    if not codes:
        raise PoolConfigurationError("La liste de codes ZIP est vide.")
    return codes


def country_password_prefix(password: str) -> str:
    """Conserve le secret et le ciblage US, retire d'anciens sélecteurs après US."""

    marker = "-country-US"
    position = password.rfind(marker)
    if position < 1:
        raise PoolConfigurationError(
            "RAYOBYTE_US_PASSWORD doit contenir le ciblage exact -country-US."
        )
    return password[: position + len(marker)]


def make_session(existing: set[str], rng: random.SystemRandom) -> str:
    while True:
        session = "".join(rng.choice(SESSION_ALPHABET) for _ in range(8))
        if session not in existing:
            existing.add(session)
            return session


def choose_zip_sequence(
    zip_codes: list[str], size: int, rng: random.SystemRandom
) -> list[str]:
    """Mélange des cycles complets pour répartir les ZIP sans biais majeur."""

    selected: list[str] = []
    while len(selected) < size:
        cycle = list(zip_codes)
        rng.shuffle(cycle)
        selected.extend(cycle)
    return selected[:size]


def build_profiles(zip_codes: list[str], size: int) -> list[tuple[str, str]]:
    if not 1 <= size <= 500:
        raise PoolConfigurationError("La taille du pool doit être comprise entre 1 et 500.")
    rng = random.SystemRandom()
    selected_zips = choose_zip_sequence(zip_codes, size, rng)
    sessions: set[str] = set()
    return [(zip_code, make_session(sessions, rng)) for zip_code in selected_zips]


def require_base_values(values: MutableMapping[str, str]) -> str:
    for variable in (
        "RAYOBYTE_US_PROXY_SERVER",
        "RAYOBYTE_US_USERNAME",
        "RAYOBYTE_US_PASSWORD",
    ):
        if not values.get(variable, "").strip():
            raise PoolConfigurationError(f"Variable absente ou vide : {variable}.")
    return country_password_prefix(values["RAYOBYTE_US_PASSWORD"].strip())


def populate_runtime_pool(
    environment: MutableMapping[str, str],
    zip_path: Path,
    proxy_path: Path,
    size: int,
) -> Counter[str]:
    """Construit le pool dans l'environnement du processus, sans fichier .env."""

    zip_codes = load_zip_codes(zip_path)
    prefix = require_base_values(environment)
    profiles = build_profiles(zip_codes, size)

    for key in list(environment):
        if key.startswith("RAYOBYTE_US_POOL_PASSWORD_"):
            del environment[key]
    for index, (zip_code, session) in enumerate(profiles, start=1):
        environment[f"RAYOBYTE_US_POOL_PASSWORD_{index:03d}"] = (
            f"{prefix}-zip-{zip_code}-session-{session}"
        )

    write_proxy_list(proxy_path, profiles)
    return Counter(zip_code for zip_code, _session in profiles)


def remove_generated_block(content: str) -> str:
    if POOL_START not in content and POOL_END not in content:
        return content.rstrip()
    if content.count(POOL_START) != 1 or content.count(POOL_END) != 1:
        raise PoolConfigurationError(
            "Bloc de pool .env incomplet ou dupliqué ; restaurez .env avant de relancer."
        )
    before, remainder = content.split(POOL_START, maxsplit=1)
    _, after = remainder.split(POOL_END, maxsplit=1)
    return f"{before.rstrip()}\n{after.strip()}".rstrip()


def write_env_pool(
    env_path: Path,
    base_content: str,
    password_prefix: str,
    profiles: list[tuple[str, str]],
) -> None:
    lines = [POOL_START, "# Généré automatiquement : ne pas modifier ce bloc à la main."]
    for index, (zip_code, session) in enumerate(profiles, start=1):
        password = f"{password_prefix}-zip-{zip_code}-session-{session}"
        lines.append(
            f"RAYOBYTE_US_POOL_PASSWORD_{index:03d}="
            f"{json.dumps(password, ensure_ascii=False)}"
        )
    lines.append(POOL_END)
    content = f"{remove_generated_block(base_content)}\n\n" + "\n".join(lines) + "\n"

    temporary = env_path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(env_path)
    os.chmod(env_path, 0o600)


def write_proxy_list(path: Path, profiles: list[tuple[str, str]]) -> None:
    lines = ["name,country,server_env,username_env,password_env"]
    for index, (zip_code, _session) in enumerate(profiles, start=1):
        lines.append(
            f"rayobyte-us-{index:03d}-zip-{zip_code},US,"
            f"RAYOBYTE_US_PROXY_SERVER,RAYOBYTE_US_USERNAME,"
            f"RAYOBYTE_US_POOL_PASSWORD_{index:03d}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_pool(
    env_path: Path, zip_path: Path, proxy_path: Path, size: int
) -> Counter[str]:
    if not env_path.exists():
        raise PoolConfigurationError(
            ".env est introuvable. Copiez d'abord .env.example vers .env."
        )

    values = load_dotenv_values(env_path)
    prefix = require_base_values(values)
    profiles = build_profiles(load_zip_codes(zip_path), size)

    original_env = env_path.read_text(encoding="utf-8")
    write_env_pool(env_path, original_env, prefix, profiles)
    write_proxy_list(proxy_path, profiles)
    return Counter(zip_code for zip_code, _session in profiles)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Génère un pool Rayobyte US avec ZIP aléatoires et sessions uniques."
    )
    parser.add_argument("--size", type=int, default=100, help="Nombre de profils (défaut : 100).")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Chemin de .env.")
    parser.add_argument(
        "--zip-file",
        type=Path,
        default=Path("zip_codes_us.txt"),
        help="Liste des ZIP US, un par ligne.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("proxies.txt"),
        help="Fichier proxies.txt à générer.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        distribution = generate_pool(args.env, args.zip_file, args.output, args.size)
    except PoolConfigurationError as exc:
        print(f"Erreur : {exc}")
        return 2

    print(f"Pool généré : {args.size} profils US")
    print(f"ZIP distincts utilisés : {len(distribution)}")
    print("ASN utilisés : aucun")
    print("Sessions uniques : oui")
    print(f"Secrets écrits uniquement dans : {args.env}")
    print(f"Références non secrètes écrites dans : {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
