"""
Octo Browser Local API wrapper for OctoScanner.

This module intentionally keeps all HTTP / API concerns in one place:
- Consistent timeouts
- Centralized error handling and response validation
- Minimal, typed return values for the scanner orchestration code

API base (assumed): http://127.0.0.1:58888

Reference (simulation):
- POST   /api/profiles                -> create profile, returns {"uuid": "..."} (or {"data": {"uuid": "..."}})
- POST   /api/profiles/start          -> start profile, returns {"selenium_port": 12345, "ws_endpoint": "..."}
- POST   /api/profiles/stop           -> stop profile
- DELETE /api/profiles/delete         -> delete profiles, payload {"uuid": ["..."]}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import logging
import time
import requests


log = logging.getLogger(__name__)


class OctoAPIError(RuntimeError):
    """Raised for network issues, non-2xx responses, or malformed API responses."""


@dataclass(frozen=True)
class StartedProfile:
    uuid: str
    selenium_port: int
    ws_endpoint: Optional[str] = None


class OctoClient:
    """
    Thin client for Octo Browser Local API.

    Notes on "anti-detect" profile creation:
    - The simulation API only documents {"title": "...", "os": "win"}.
    - Real Octo deployments often support richer fingerprint settings (UA, timezone, WebRTC, etc).
      This client accepts a few optional fields but will still work if Octo ignores them.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:58888",
        timeout_s: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._session = requests.Session()

    def _request(self, method: str, path: str, json_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    json=json_payload,
                    timeout=self.timeout_s,
                )
            except requests.RequestException as e:
                last_exc = e
                # Транзиентные сетевые ошибки: попробуем повторить.
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise OctoAPIError(
                    f"Octo API request failed: {method} {url} ({e.__class__.__name__}: {e})"
                ) from e

            # 429: у Octo есть лимиты даже на Local API для некоторых запросов (Start Profile, One-time profile).
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait_s = float(retry_after) if retry_after is not None else 1.0
                except ValueError:
                    wait_s = 1.0
                if attempt < self.max_retries:
                    time.sleep(max(wait_s, 0.5))
                    continue
                body_preview = resp.text[:2000] if resp.text else ""
                raise OctoAPIError(f"Octo API rate limited: {method} {url} -> HTTP 429: {body_preview}")

            # Транзиентные 5xx — обычно имеет смысл повторить.
            if resp.status_code in (502, 503, 504):
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue

            if not (200 <= resp.status_code < 300):
                body_preview = resp.text[:2000] if resp.text else ""
                raise OctoAPIError(f"Octo API error: {method} {url} -> HTTP {resp.status_code}: {body_preview}")

        try:
            data = resp.json() if resp.content else {}
        except ValueError as e:
            raise OctoAPIError(f"Octo API returned non-JSON response: {method} {url}") from e

        if not isinstance(data, dict):
            raise OctoAPIError(f"Octo API returned unexpected JSON type: {type(data).__name__}")

        return data

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        Глубокое объединение словарей:
        - dict + dict -> рекурсивно мерджим
        - иначе override заменяет base
        Возвращает НОВЫЙ dict (не мутирует входные).
        """
        out: Dict[str, Any] = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = OctoClient._deep_merge(out[k], v)  # type: ignore[arg-type]
            else:
                out[k] = v
        return out

    @staticmethod
    def _extract_uuid(resp_json: Dict[str, Any]) -> str:
        # Support a couple of common shapes: {"uuid": "..."} or {"data": {"uuid": "..."}}
        if isinstance(resp_json.get("uuid"), str) and resp_json["uuid"]:
            return resp_json["uuid"]
        data = resp_json.get("data")
        if isinstance(data, dict) and isinstance(data.get("uuid"), str) and data["uuid"]:
            return data["uuid"]
        raise OctoAPIError(f"Create profile response missing uuid: {resp_json!r}")

    def create_profile(
        self,
        title: str,
        os_name: str = "android",
        os_version: Optional[str] = "13",
        user_agent: Optional[str] = None,
        tags: Optional[List[str]] = None,
        payload_overrides: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a new Octo profile.

        - **os_name**: Use "android" (default), "ios", "win", or "mac".
        - **os_version**: Optional OS version (for Android: 12/13/14, for iOS: 16/17, etc).
        - **user_agent**: Optional; if omitted Octo may generate a default UA.
        - **tags**: Optional; stored by Octo if supported.
        - **payload_overrides**: Optional dict to merge into payload (for proxy, geo, etc).
        """

        # В Postman/Docs для создания профиля часто используется структура:
        # { "title": "...", "fingerprint": { "os": "android", "os_version": "13" } }
        # Но в локальном "упрощённом" API может быть достаточно { "title": "...", "os": "android" }.
        # Поэтому отправляем оба варианта — Octo обычно игнорирует неизвестные/лишние поля.
        fingerprint: Dict[str, Any] = {"os": os_name}
        if os_version:
            fingerprint["os_version"] = str(os_version)

        payload: Dict[str, Any] = {"title": title, "os": os_name, "fingerprint": fingerprint}

        # Optional fields: safe to include; Octo may ignore unknown keys.
        if user_agent:
            payload["userAgent"] = user_agent
        if tags:
            payload["tags"] = tags

        # Позволяет "точечно" задавать любые поля Octo (UA/GEO/таймзона/WebRTC/Proxy и т.д.)
        # в том формате, который требует ваша версия клиента.
        if payload_overrides:
            payload = self._deep_merge(payload, payload_overrides)

        resp = self._request("POST", "/api/profiles", json_payload=payload)
        uuid = self._extract_uuid(resp)
        log.debug("Created Octo profile uuid=%s (os=%s, os_version=%s)", uuid, os_name, os_version)
        return uuid

    def start_profile(
        self,
        uuid: str,
        headless: bool = False,
        flags: Optional[List[str]] = None,
    ) -> StartedProfile:
        # В документации Octo встречаются параметры старта:
        # - headless: bool
        # - flags: ["--start-maximized", ...]
        payload: Dict[str, Any] = {"uuid": uuid, "headless": headless}
        if flags:
            payload["flags"] = flags
        resp = self._request("POST", "/api/profiles/start", json_payload=payload)

        # Поддержим несколько возможных схем ответа:
        # {"selenium_port": 12345, "ws_endpoint": "..."}
        # {"data": {"selenium_port": 12345, "ws_endpoint": "..."}}
        data: Dict[str, Any] = resp.get("data") if isinstance(resp.get("data"), dict) else resp

        selenium_port = data.get("selenium_port")
        ws_endpoint = data.get("ws_endpoint")

        if not isinstance(selenium_port, int):
            raise OctoAPIError(f"Start profile response missing selenium_port: {resp!r}")
        if ws_endpoint is not None and not isinstance(ws_endpoint, str):
            ws_endpoint = None

        log.debug("Started Octo profile uuid=%s selenium_port=%s", uuid, selenium_port)
        return StartedProfile(uuid=uuid, selenium_port=selenium_port, ws_endpoint=ws_endpoint)

    def stop_profile(self, uuid: str) -> None:
        payload = {"uuid": uuid}
        self._request("POST", "/api/profiles/stop", json_payload=payload)
        log.debug("Stopped Octo profile uuid=%s", uuid)

    def delete_profiles(self, uuids: List[str]) -> None:
        # Simulation: DELETE /api/profiles/delete with JSON {"uuid": ["..."]}
        payload = {"uuid": uuids}
        self._request("DELETE", "/api/profiles/delete", json_payload=payload)
        log.debug("Deleted Octo profiles uuids=%s", uuids)
