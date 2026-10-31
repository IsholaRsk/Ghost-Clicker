"""Client minimal Supabase REST pour la persistance de Ghost Clicker."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class StorageError(RuntimeError):
    """Supabase est mal configuré ou temporairement indisponible."""


@dataclass(frozen=True)
class SupabaseStorage:
    """Stocke la configuration et l'historique via PostgREST."""

    url: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        normalized_url = self.url.strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.lower().endswith(".supabase.co")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise StorageError("SUPABASE_URL doit être une URL HTTPS valide.")
        key = self.api_key.strip()
        if (
            len(key) < 20
            or any(character.isspace() for character in key)
            or not key.startswith("sb_secret_")
        ):
            raise StorageError(
                "SUPABASE_SECRET_KEY doit être une clé serveur sb_secret_."
            )
        if self.timeout_seconds <= 0:
            raise StorageError("SUPABASE_TIMEOUT_SECONDS doit être positif.")
        object.__setattr__(self, "url", normalized_url)
        object.__setattr__(self, "api_key", key)

    @classmethod
    def from_env(cls) -> SupabaseStorage | None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
        if not url and not key:
            return None
        if not url or not key:
            raise StorageError(
                "SUPABASE_URL et SUPABASE_SECRET_KEY doivent être configurées ensemble."
            )
        raw_timeout = os.getenv("SUPABASE_TIMEOUT_SECONDS", "10").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise StorageError(
                "SUPABASE_TIMEOUT_SECONDS doit être un nombre."
            ) from exc
        return cls(url=url, api_key=key, timeout_seconds=timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        body: Any = None,
        prefer: str = "",
    ) -> Any:
        endpoint = f"{self.url}/rest/v1/{path.lstrip('/')}"
        if query:
            endpoint = f"{endpoint}?{urlencode(query)}"
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "apikey": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        # Une clé sb_secret_* s'envoie dans `apikey` : ce n'est pas un JWT et
        # elle ne doit donc pas être copiée dans un en-tête Bearer.
        if prefer:
            headers["Prefer"] = prefer
        request = Request(endpoint, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                content = response.read()
        except HTTPError as exc:
            # Le corps PostgREST peut contenir des données utilisateur : ne pas le
            # recopier dans les logs ou dans l'interface.
            raise StorageError(
                f"Supabase a refusé la requête (HTTP {exc.code})."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise StorageError("Supabase est temporairement inaccessible.") from exc

        if not content:
            return None
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError("Supabase a renvoyé une réponse invalide.") from exc

    def check(self) -> None:
        self._request(
            "GET",
            "ghost_clicker_settings",
            query={"select": "id", "limit": 1},
        )

    def load_settings(self) -> dict[str, Any] | None:
        rows = self._request(
            "GET",
            "ghost_clicker_settings",
            query={"select": "payload", "id": "eq.default", "limit": 1},
        )
        if not isinstance(rows, list) or not rows:
            return None
        payload = rows[0].get("payload") if isinstance(rows[0], dict) else None
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise StorageError("La configuration Supabase est invalide.")
        return payload

    def save_settings(self, value: dict[str, Any]) -> None:
        self._request(
            "POST",
            "ghost_clicker_settings",
            query={"on_conflict": "id"},
            body={"id": "default", "payload": value},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    @staticmethod
    def _report_parts(
        value: dict[str, Any],
    ) -> tuple[str, dict[str, Any], int | None, dict[str, Any] | None]:
        run_id = str(value.get("run_id", "")).strip()
        if not run_id:
            run_id = uuid.uuid4().hex
        summary = dict(value)
        summary["run_id"] = run_id
        raw_results = summary.pop("results", [])
        results = raw_results if isinstance(raw_results, list) else []
        try:
            completed_visits = int(summary.get("completed_visits", 0))
        except (TypeError, ValueError):
            completed_visits = 0
        if completed_visits <= 0 and results:
            completed_visits = len(results)
            summary["completed_visits"] = completed_visits
        latest = results[-1] if results and isinstance(results[-1], dict) else None
        visit_number = completed_visits if latest is not None else None
        return run_id, summary, visit_number, latest

    def upsert_report(self, value: dict[str, Any]) -> str:
        run_id, summary, visit_number, latest = self._report_parts(value)
        self._request(
            "POST",
            "rpc/ghost_clicker_upsert_report",
            body={
                "p_run_id": run_id,
                "p_report": summary,
                "p_visit_number": visit_number,
                "p_visit": latest,
            },
        )
        return run_id

    def append_report(self, value: dict[str, Any]) -> str:
        run_id = str(value.get("run_id", "")).strip() or uuid.uuid4().hex
        results = value.get("results", [])
        valid_results = results if isinstance(results, list) else []
        summary = dict(value)
        summary["run_id"] = run_id
        summary["completed_visits"] = max(
            int(summary.get("completed_visits", 0) or 0), len(valid_results)
        )
        if not valid_results:
            self.upsert_report(summary)
            return run_id
        for visit_number, result in enumerate(valid_results, start=1):
            progressive = dict(summary)
            progressive["completed_visits"] = visit_number
            progressive["results"] = [result]
            self.upsert_report(progressive)
        self.upsert_report(summary)
        return run_id

    def _load_report_rows(self, *, all_reports: bool) -> list[dict[str, Any]]:
        page_size = 1_000
        report_limit = None if all_reports else 200
        rows: list[dict[str, Any]] = []
        offset = 0
        while report_limit is None or len(rows) < report_limit:
            limit = page_size
            if report_limit is not None:
                limit = min(limit, report_limit - len(rows))
            page = self._request(
                "GET",
                "ghost_clicker_reports",
                query={
                    "select": "run_id,payload,updated_at",
                    "order": "updated_at.desc",
                    "limit": limit,
                    "offset": offset,
                },
            )
            if not isinstance(page, list):
                raise StorageError("La liste des rapports Supabase est invalide.")
            rows.extend(item for item in page if isinstance(item, dict))
            if len(page) < limit:
                break
            offset += len(page)
        return rows

    @staticmethod
    def _in_filter(values: list[str]) -> str:
        # Les run_id générés par l'application sont alphanumériques, avec « - »
        # et « _ ». Refuser toute autre valeur évite une expression PostgREST
        # ambiguë si une ligne a été ajoutée manuellement dans la base.
        safe = []
        for value in values:
            if value and all(character.isalnum() or character in "-_" for character in value):
                safe.append(value)
        return f"in.({','.join(safe)})" if safe else "eq.__aucun_run__"

    def _load_visits(
        self,
        run_ids: list[str],
        *,
        all_visits: bool,
    ) -> list[dict[str, Any]]:
        page_size = 1_000
        maximum = None if all_visits else 5_000
        visits: list[dict[str, Any]] = []
        for chunk_start in range(0, len(run_ids), 50):
            chunk = run_ids[chunk_start : chunk_start + 50]
            offset = 0
            while maximum is None or len(visits) < maximum:
                limit = page_size
                if maximum is not None:
                    limit = min(limit, maximum - len(visits))
                page = self._request(
                    "GET",
                    "ghost_clicker_visits",
                    query={
                        "select": "run_id,visit_number,payload",
                        "run_id": self._in_filter(chunk),
                        "order": "created_at.desc",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                if not isinstance(page, list):
                    raise StorageError(
                        "La liste des visites Supabase est invalide."
                    )
                visits.extend(item for item in page if isinstance(item, dict))
                if len(page) < limit:
                    break
                offset += len(page)
            if maximum is not None and len(visits) >= maximum:
                break
        return visits

    def load_reports(self, *, all_data: bool = False) -> list[dict[str, Any]]:
        rows = self._load_report_rows(all_reports=all_data)
        run_ids = [str(row.get("run_id", "")) for row in rows]
        visits = self._load_visits(run_ids, all_visits=all_data)
        visits_by_run: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for row in visits:
            run_id = str(row.get("run_id", ""))
            payload = row.get("payload")
            if not run_id or not isinstance(payload, dict):
                continue
            try:
                visit_number = int(row.get("visit_number", 0))
            except (TypeError, ValueError):
                continue
            visits_by_run.setdefault(run_id, []).append((visit_number, payload))

        reports: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            report = dict(payload)
            run_id = str(row.get("run_id", ""))
            report["run_id"] = run_id
            report["results"] = [
                result
                for _number, result in sorted(
                    visits_by_run.get(run_id, []), key=lambda item: item[0]
                )
            ]
            reports.append(report)
        return reports

    def clear_reports(self) -> None:
        self._request("POST", "rpc/ghost_clicker_clear_reports", body={})

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        normalized_type = event_type.strip()
        if not normalized_type or len(normalized_type) > 100:
            raise StorageError("Le type d'événement Supabase est invalide.")
        self._request(
            "POST",
            "ghost_clicker_events",
            body={"event_type": normalized_type, "payload": payload},
            prefer="return=minimal",
        )
