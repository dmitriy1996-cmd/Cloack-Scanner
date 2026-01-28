"""
Octo Browser Local API wrapper for OctoScanner.

- OctoClient: HTTP API (create/start/stop profiles). Start returns StartedProfile (debug_port, ws_endpoint).
- OctoAutomator: CDP automation via Playwright (connect_over_cdp to ws_endpoint). goto, click, type,
  wait_for, scroll, get_html, screenshot. Requires: pip install playwright && python -m playwright install chromium.

API base (assumed): http://127.0.0.1:58888 (Local API)

Reference (Octo Browser API v2):
- Cloud API (https://app.octobrowser.net):
  - POST   /api/v2/automation/profiles                -> create profile, returns {"uuid": "..."}
  - DELETE /api/v2/automation/profiles                -> delete profiles, payload {"uuid": ["..."]}
- Local API (http://127.0.0.1:58888):
  - POST   /api/v2/automation/profiles/{uuid}/start   -> start profile, returns debug_port, ws_endpoint
  - POST   /api/v2/automation/profiles/{uuid}/stop    -> stop profile
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import json
import logging
import time

import requests

# Optional Playwright for OctoAutomator (CDP automation)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore[misc, assignment]

log = logging.getLogger(__name__)


class OctoAPIError(RuntimeError):
    """Raised for network issues, non-2xx responses, or malformed API responses."""


class OctoAutomationError(RuntimeError):
    """Raised for CDP/Playwright automation failures (connect, goto, click, etc.)."""


@dataclass(frozen=True)
class StartedProfile:
    """
    Represents a started Octo profile with CDP/debug connection info.

    - debug_port: Chrome DevTools Protocol port (int). Use for CDP/WebDriver.
    - ws_endpoint: WebSocket URL for CDP, e.g. ws://127.0.0.1:53215/devtools/browser/...
    - selenium_port: Alias for debug_port (backward compatibility). Prefer debug_port.
    """

    uuid: str
    debug_port: int
    ws_endpoint: Optional[str] = None

    @property
    def selenium_port(self) -> int:
        """Backward-compat alias for debug_port. Prefer debug_port."""
        return self.debug_port


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
        api_key: Optional[str] = None,
        cloud_api_url: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Cloud API используется для создания/управления профилями
        # Local API используется для запуска/остановки уже созданных профилей
        self.cloud_api_url = (cloud_api_url or "https://app.octobrowser.net").rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.api_key = api_key
        self._session = requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        json_payload: Optional[Dict[str, Any]] = None,
        use_cloud_api: bool = False,
        *,
        allow_list: bool = False,
    ) -> Any:
        """
        Выполняет HTTP запрос к Octo Browser API.
        
        use_cloud_api=True:  Cloud API (https://app.octobrowser.net) - для создания/удаления профилей
        use_cloud_api=False: Local API (http://127.0.0.1:58888) - для запуска/остановки профилей
        """
        base = self.cloud_api_url if use_cloud_api else self.base_url
        url = f"{base}{path}"
        last_exc: Optional[BaseException] = None
        
        # Подготовка заголовков
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-Octo-Api-Token"] = self.api_key
        
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method=method,
                    url=url,
                    json=json_payload,
                    headers=headers,
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
            if allow_list and isinstance(data, list):
                return data
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

    @staticmethod
    def _parse_debug_port(val: Any) -> Optional[int]:
        """Parse debug_port from API response (may be str or int). Returns None if invalid."""
        if val is None:
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            s = val.strip()
            if ":" in s:
                try:
                    return int(s.split(":")[-1])
                except (ValueError, IndexError):
                    return None
            try:
                return int(s)
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _fetch_ws_endpoint_from_port(port: int, timeout_s: float = 5.0) -> Optional[str]:
        """
        Fetch CDP WebSocket URL from http://127.0.0.1:{port}/json/version.
        Use when API returns debug_port but not ws_endpoint.
        """
        url = f"http://127.0.0.1:{port}/json/version"
        try:
            r = requests.get(url, timeout=timeout_s)
            if r.status_code != 200:
                return None
            data = r.json()
            ws = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
            return str(ws) if isinstance(ws, str) and ws else None
        except Exception as e:
            log.debug("Could not fetch ws_endpoint from %s: %s", url, e)
            return None

    @staticmethod
    def _port_from_ws_url(ws: str) -> Optional[int]:
        """Extract port from ws://127.0.0.1:53215/... or wss://... . Returns None if invalid."""
        if not isinstance(ws, str) or not ws.strip():
            return None
        try:
            from urllib.parse import urlparse
            p = urlparse(ws.strip())
            if p.port is not None:
                return int(p.port)
            if p.hostname and ":" in (p.netloc or ""):
                return int((p.netloc or "").split(":")[-1])
        except Exception:
            pass
        return None

    def create_proxy(
        self,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        proxy_type: str = "http",
    ) -> str:
        """
        Create a proxy in Octo Browser Cloud API and return its UUID.
        
        According to Octo Browser API v2 documentation, format is:
        {"title": "...", "host": "...", "port": ..., "type": "...", "login": "...", "password": "..."}
        Note: field is "login", not "username"
        """
        payload: Dict[str, Any] = {
            "title": f"Proxy_{host}_{port}",
            "host": host,
            "port": int(port),
            "type": proxy_type.lower(),
        }
        if username:
            payload["login"] = username  # API использует "login", не "username"
        if password:
            payload["password"] = password
        
        log.debug("Creating proxy: title=%s, host=%s, port=%s, login=%s, type=%s", 
                 payload["title"], host, port, username or "(empty)", proxy_type)
        
        resp = self._request("POST", "/api/v2/automation/proxies", json_payload=payload, use_cloud_api=True)
        uuid = self._extract_uuid(resp)
        log.debug("Created proxy uuid=%s", uuid)
        return uuid

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

        # Согласно документации Octo Browser API, правильная структура:
        # { "title": "...", "fingerprint": { "os": "android", "os_version": "13" } }
        fingerprint: Dict[str, Any] = {"os": os_name}
        if os_version:
            fingerprint["os_version"] = str(os_version)

        payload: Dict[str, Any] = {"title": title, "fingerprint": fingerprint}

        # Optional fields: safe to include; Octo may ignore unknown keys.
        if user_agent:
            payload["userAgent"] = user_agent
        if tags:
            payload["tags"] = tags

        # Позволяет "точечно" задавать любые поля Octo (UA/GEO/таймзона/WebRTC/Proxy и т.д.)
        # в том формате, который требует ваша версия клиента.
        if payload_overrides:
            payload = self._deep_merge(payload, payload_overrides)
        
        # Логируем финальный payload для отладки (скрываем пароли)
        debug_payload = json.dumps(payload, indent=2, ensure_ascii=False)
        if "password" in debug_payload.lower():
            import re
            debug_payload = re.sub(r'"password"\s*:\s*"[^"]*"', '"password": "***"', debug_payload, flags=re.IGNORECASE)
        log.debug("Creating profile with payload: %s", debug_payload)
        
        # Детальное логирование прокси для отладки
        if "proxy" in payload:
            proxy_value = payload.get("proxy")
            if isinstance(proxy_value, str):
                # Прокси в строковом формате: "host:port:username:password" или "protocol://host:port:username:password"
                # Скрываем пароль для безопасности
                if ":" in proxy_value:
                    parts = proxy_value.split(":")
                    if len(parts) >= 4:
                        # Формат: host:port:username:password
                        log.debug("Proxy in payload (string format): %s:%s:%s:***", 
                                 parts[0], parts[1], parts[2])
                    elif len(parts) >= 2:
                        # Формат: host:port (без авторизации)
                        log.debug("Proxy in payload (string format, no auth): %s:%s", 
                                 parts[0], parts[1])
                    else:
                        log.debug("Proxy in payload (string format): %s", proxy_value.replace(":", ":***") if ":" in proxy_value else proxy_value)
                else:
                    log.debug("Proxy in payload (string format): %s", proxy_value)
            elif isinstance(proxy_value, dict):
                # Прокси в объектном формате (для UUID или других случаев)
                log.debug("Proxy in payload (object format): %s", proxy_value)

        # Пробуем создать профиль через Local API сначала (может работать лучше для запуска)
        # Если не работает, используем Cloud API
        try:
            resp = self._request("POST", "/api/v2/automation/profiles", json_payload=payload, use_cloud_api=False)
            log.debug("Created profile via Local API")
        except OctoAPIError as local_e:
            log.debug("Local API create failed: %s, using Cloud API...", local_e)
            # Создание профиля через Cloud API
            def _cloud_create(payload_obj: Dict[str, Any]) -> Dict[str, Any]:
                return self._request("POST", "/api/v2/automation/profiles", json_payload=payload_obj, use_cloud_api=True)

            # Cloud API может не принимать userAgent и/или временно лимитировать профили.
            retry_waits = [3.0, 6.0, 10.0]
            last_cloud_error: Optional[OctoAPIError] = None
            for attempt, wait_s in enumerate([0.0] + retry_waits, start=1):
                if wait_s:
                    time.sleep(wait_s)
                try:
                    resp = _cloud_create(payload)
                    log.debug("Created profile via Cloud API")
                    last_cloud_error = None
                    break
                except OctoAPIError as cloud_e:
                    last_cloud_error = cloud_e
                    msg = str(cloud_e)
                    if "extra_forbidden" in msg and "userAgent" in msg:
                        log.warning("Cloud API не принимает userAgent — повторяю create_profile без userAgent")
                        payload = dict(payload)
                        payload.pop("userAgent", None)
                        continue
                    if "limit_reached" in msg or "Maximum profiles" in msg:
                        log.warning("Достигнут лимит профилей, попытка %d/%d через %.0fс", attempt, len(retry_waits) + 1, wait_s or retry_waits[0])
                        continue
                    if "rate_limited" in msg or "429" in msg:
                        log.warning("Rate limit при создании профиля, попытка %d/%d через %.0fс", attempt, len(retry_waits) + 1, wait_s or retry_waits[0])
                        continue
                    raise
            if last_cloud_error is not None:
                raise last_cloud_error
        uuid = self._extract_uuid(resp)
        log.debug("Created Octo profile uuid=%s (os=%s, os_version=%s)", uuid, os_name, os_version)
        return uuid

    def create_one_time_profile(
        self,
        title: str,
        os_name: str = "android",
        os_version: Optional[str] = "13",
        user_agent: Optional[str] = None,
        tags: Optional[List[str]] = None,
        payload_overrides: Optional[Dict[str, Any]] = None,
        headless: bool = False,
        flags: Optional[List[str]] = None,
    ) -> StartedProfile:
        """
        Create and start a one-time profile in a single request.
        One-time profiles are automatically deleted when closed.
        
        Returns StartedProfile with selenium_port for automation.
        """
        # Согласно документации Octo Browser API, правильная структура:
        # { "title": "...", "fingerprint": { "os": "android", "os_version": "13" } }
        fingerprint: Dict[str, Any] = {"os": os_name}
        if os_version:
            fingerprint["os_version"] = str(os_version)

        payload: Dict[str, Any] = {"title": title, "fingerprint": fingerprint}

        # Optional fields: safe to include; Octo may ignore unknown keys.
        if user_agent:
            payload["userAgent"] = user_agent
        if tags:
            payload["tags"] = tags
        
        # Параметры запуска НЕ добавляем в payload создания - они будут переданы при запуске
        # headless и flags недопустимы при создании профиля через /api/v2/automation/profiles
        
        # Позволяет "точечно" задавать любые поля Octo (UA/GEO/таймзона/WebRTC/Proxy и т.д.)
        if payload_overrides:
            payload = self._deep_merge(payload, payload_overrides)
        
        # Логируем финальный payload для отладки (скрываем пароли)
        debug_payload = json.dumps(payload, indent=2, ensure_ascii=False)
        if "password" in debug_payload.lower():
            import re
            debug_payload = re.sub(r'"password"\s*:\s*"[^"]*"', '"password": "***"', debug_payload, flags=re.IGNORECASE)
        log.debug("Creating one-time profile with payload: %s", debug_payload)
        
        # Детальное логирование прокси для отладки
        if "proxy" in payload:
            proxy_value = payload.get("proxy")
            if isinstance(proxy_value, str):
                log.debug("Proxy in one-time profile payload (string format): %s", 
                         proxy_value.replace(":", ":***") if ":" in proxy_value else proxy_value)
            elif isinstance(proxy_value, dict):
                log.debug("Proxy in one-time profile payload (object format): host=%s, port=%s, username=%s, password=%s, type=%s",
                         proxy_value.get("host"), proxy_value.get("port"),
                         proxy_value.get("username", "(empty)"),
                         "***" if proxy_value.get("password") else "(empty)",
                         proxy_value.get("type"))

        # One-time profile: пробуем специальные эндпоинты, если не работают - используем обычное создание + запуск
        # Сначала пробуем специальные эндпоинты для one-time (если они существуют)
        one_time_endpoints = [
            ("/api/v2/automation/profiles/one-time", "POST"),
            ("/api/v2/automation/one-time-profile", "POST"),
            ("/api/v2/profiles/one-time", "POST"),
        ]
        
        # Для one-time эндпоинтов добавляем параметры запуска в payload
        one_time_payload = dict(payload)
        one_time_payload["headless"] = headless
        if flags:
            one_time_payload["flags"] = flags
        
        last_error = None
        resp = None
        
        # Пробуем специальные one-time эндпоинты
        for endpoint, method in one_time_endpoints:
            try:
                resp = self._request(method, endpoint, json_payload=one_time_payload, use_cloud_api=True)
                log.debug("Successfully created one-time profile via endpoint: %s", endpoint)
                break
            except OctoAPIError as e:
                last_error = e
                if "404" not in str(e) and "Not Found" not in str(e) and "405" not in str(e) and "Method Not Allowed" not in str(e):
                    # Если это не 404/405, значит эндпоинт существует, но есть другая ошибка
                    raise
                log.debug("One-time endpoint not found: %s %s", method, endpoint)
                continue
        
        # Если специальные эндпоинты не работают, используем обычное создание + запуск
        if resp is None:
            log.debug("One-time endpoints not available, using create + start workflow")
            
            # Пробуем создать профиль через Local API (может работать лучше для запуска)
            uuid = None
            try:
                resp = self._request("POST", "/api/v2/automation/profiles", json_payload=payload, use_cloud_api=False)
                uuid = self._extract_uuid(resp)
                log.debug("Created profile via Local API, uuid=%s", uuid)
            except OctoAPIError as local_create_error:
                log.debug("Local API create failed: %s, trying Cloud API...", local_create_error)
                # Если Local API не поддерживает создание, используем Cloud API
                resp = self._request("POST", "/api/v2/automation/profiles", json_payload=payload, use_cloud_api=True)
                uuid = self._extract_uuid(resp)
                log.debug("Created profile via Cloud API, uuid=%s", uuid)
            
            # Профили, созданные через Cloud API, должны запускаться через Local API
            # Но Local API может не видеть их сразу - нужна синхронизация
            start_payload: Dict[str, Any] = {"headless": headless}
            if flags:
                start_payload["flags"] = flags
            
            log.debug("Starting profile %s via Local API (waiting for sync from Cloud API)...", uuid)
            start_resp = None
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        time.sleep(2)  # Задержка для синхронизации между Cloud и Local API
                    start_resp = self._request("POST", f"/api/v2/automation/profiles/{uuid}/start", 
                                              json_payload=start_payload, 
                                              use_cloud_api=False)
                    log.debug("Successfully started profile via Local API on attempt %d", attempt + 1)
                    break
                except OctoAPIError as e:
                    if "404" in str(e) and attempt < max_retries - 1:
                        log.debug("Profile not yet visible in Local API, retrying... (attempt %d/%d)", attempt + 1, max_retries)
                        continue
                    raise
            
            if start_resp is None:
                raise OctoAPIError(f"Could not start profile {uuid} via Local API after {max_retries} attempts")
            
            data = start_resp.get("data") if isinstance(start_resp.get("data"), dict) else start_resp
            raw = data.get("debug_port") or data.get("selenium_port") or data.get("port")
            debug_port = self._parse_debug_port(raw)
            if not isinstance(debug_port, int):
                raise OctoAPIError(f"Start profile response missing debug_port/selenium_port: {start_resp!r}")
            ws_endpoint = data.get("ws_endpoint") or data.get("webdriver")
            if not (isinstance(ws_endpoint, str) and ws_endpoint.strip()):
                ws_endpoint = self._fetch_ws_endpoint_from_port(debug_port) if debug_port else None
            else:
                ws_endpoint = ws_endpoint.strip()
            log.debug("Created and started profile uuid=%s debug_port=%s", uuid, debug_port)
            return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)

        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        uuid = data.get("uuid") or resp.get("uuid") or "one-time"
        raw = data.get("debug_port") or data.get("selenium_port") or resp.get("selenium_port") or data.get("port") or resp.get("port")
        debug_port = self._parse_debug_port(raw)
        if not isinstance(debug_port, int):
            raise OctoAPIError(f"One-time profile response missing debug_port/selenium_port: {resp!r}")
        ws_endpoint = data.get("ws_endpoint") or resp.get("ws_endpoint") or data.get("webdriver") or resp.get("webdriver")
        if not (isinstance(ws_endpoint, str) and ws_endpoint.strip()):
            ws_endpoint = self._fetch_ws_endpoint_from_port(debug_port) if debug_port else None
        else:
            ws_endpoint = ws_endpoint.strip()
        log.debug("Created and started one-time profile uuid=%s debug_port=%s", uuid, debug_port)
        return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)

    def get_profile_status(self, uuid: str) -> Optional[Dict[str, Any]]:
        """
        Get profile status and information via Local API.
        Returns profile info including selenium_port if profile is running.
        """
        # Пробуем разные эндпоинты для получения статуса профиля
        endpoints_to_try = [
            (f"/api/v2/automation/profiles/{uuid}", False),  # Local API
            (f"/api/v2/automation/profiles/{uuid}", True),   # Cloud API
            (f"/api/v2/profiles/{uuid}", False),             # Local API альтернатива
            (f"/api/v2/profiles/{uuid}", True),              # Cloud API альтернатива
        ]
        
        for endpoint, use_cloud in endpoints_to_try:
            try:
                resp = self._request("GET", endpoint, use_cloud_api=use_cloud)
                log.debug("Got profile status from %s: %s", endpoint, resp)
                return resp
            except OctoAPIError as e:
                log.debug("Failed to get profile status from %s: %s", endpoint, e)
                continue
        
        # Если все эндпоинты не работают, пробуем получить список запущенных профилей
        try:
            log.debug("Trying to get list of running profiles from Local API...")
            # Пробуем получить список запущенных/активных профилей
            running_profiles_endpoints = [
                ("/api/v2/automation/profiles/active", False),
                ("/api/v2/automation/profiles?status=running", False),
                ("/api/v2/automation/profiles?status=1", False),
                ("/api/v2/automation/profiles", False),
            ]
            
            for endpoint, use_cloud in running_profiles_endpoints:
                try:
                    profiles_resp = self._request("GET", endpoint, use_cloud_api=use_cloud)
                    log.debug("Got profiles list from %s (first 500 chars): %s", endpoint, str(profiles_resp)[:500])
                    # Ищем профиль в списке
                    if isinstance(profiles_resp, dict):
                        profiles = profiles_resp.get("data", []) or profiles_resp.get("profiles", []) or profiles_resp.get("list", [])
                        if isinstance(profiles, list):
                            for profile in profiles:
                                if isinstance(profile, dict):
                                    profile_uuid = profile.get("uuid") or (profile.get("data", {}) if isinstance(profile.get("data"), dict) else {}).get("uuid")
                                    if profile_uuid == uuid:
                                        log.debug("Found profile in list: %s", profile)
                                        return profile
                except OctoAPIError as list_e:
                    log.debug("Failed to get profiles list from %s: %s", endpoint, list_e)
                    continue
        except Exception as e:
            log.debug("Error getting profiles list: %s", e)
        
        log.debug("Could not get profile status via any method")
        return None

    def start_profile(
        self,
        uuid: str,
        headless: bool = False,
        flags: Optional[List[str]] = None,
        start_pages: Optional[List[str]] = None,
        allow_port_scan: bool = False,
        debug_port_override: Optional[int] = None,
    ) -> StartedProfile:
        """
        Start an existing profile via Local API.

        Returns StartedProfile with debug_port (int) and ws_endpoint (str).

        Args:
            uuid: Profile UUID to start.
            headless: Run without GUI.
            flags: Optional Chrome flags.
            start_pages: List of URLs to open on start.
            allow_port_scan: If True, when API omits debug_port, scan 52xxx+92xx.
            debug_port_override: If set, use this port when API/scan fail (e.g. from Octo UI).
        """
        # Официальный Local API (блог Octo, octo-mcp): POST /api/profiles/start
        # Ответ — «сырой» объект: {uuid, state, ws_endpoint, debug_port, ...} без обёртки {success, data}.
        # v2/automation часто возвращает {success, data: None} — CDP не получаем.
        payload: Dict[str, Any] = {
            "uuid": uuid,
            "headless": bool(headless),
            "debug_port": int(debug_port_override) if debug_port_override else True,
            "timeout": 120,
            "only_local": True,
            "flags": list(flags) if flags else [],
        }
        # start_pages не передаём в start — URL открываем через goto после CDP.
        if start_pages:
            log.info("Start pages не передаём в API (открываем через goto): %s", start_pages)
        
        endpoint = "/api/profiles/start"
        resp = None
        last_error = None
        already_started_no_port = False
        
        try:
            resp = self._request("POST", endpoint, json_payload=payload, use_cloud_api=False)
            log.info("POST %s → ответ: keys=%s", endpoint, list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
        except OctoAPIError as e:
            last_error = e
            # Если профиль уже запущен — пробуем взять данные из active
            if "already_started" in str(e) or "already started" in str(e).lower():
                    log.info("ℹ️  Профиль уже запущен, получаю информацию о запущенном профиле...")
                    try:
                        running_profiles_resp = {}
                        for active_path in ("/api/profiles/active", "/api/profiles"):
                            try:
                                running_profiles_resp = self._request(
                                    "GET",
                                    active_path,
                                    use_cloud_api=False,
                                    allow_list=True,
                                )
                                log.debug("Running profiles from %s: %s", active_path, running_profiles_resp)
                                break
                            except Exception:
                                continue
                        
                        # Ищем наш профиль в списке запущенных
                        if isinstance(running_profiles_resp, (dict, list)):
                            profiles_list = []
                            if isinstance(running_profiles_resp, dict):
                                profiles_list = running_profiles_resp.get("data", []) or running_profiles_resp.get("profiles", []) or running_profiles_resp.get("list", [])
                            elif isinstance(running_profiles_resp, list):
                                profiles_list = running_profiles_resp
                            
                            if isinstance(profiles_list, list):
                                for profile in profiles_list:
                                    if isinstance(profile, dict):
                                        profile_uuid = profile.get("uuid") or (profile.get("data", {}) if isinstance(profile.get("data"), dict) else {}).get("uuid")
                                        if profile_uuid == uuid:
                                            # Нашли наш профиль, извлекаем debug_port
                                            profile_data = profile.get("data") if isinstance(profile.get("data"), dict) else profile
                                            debug_port = (
                                                profile_data.get("debug_port") or
                                                profile_data.get("selenium_port") or
                                                profile_data.get("port") or
                                                profile.get("debug_port") or
                                                profile.get("selenium_port") or
                                                profile.get("port")
                                            )
                                            
                                            # Обрабатываем строковый формат
                                            if isinstance(debug_port, str):
                                                if ":" in debug_port:
                                                    try:
                                                        debug_port = int(debug_port.split(":")[-1])
                                                    except (ValueError, IndexError):
                                                        debug_port = None
                                                else:
                                                    try:
                                                        debug_port = int(debug_port)
                                                    except (ValueError, TypeError):
                                                        debug_port = None
                                            
                                            ws_endpoint = profile_data.get("ws_endpoint") or profile.get("ws_endpoint")
                                            if isinstance(ws_endpoint, str):
                                                ws_endpoint = ws_endpoint.strip() or None
                                            if isinstance(debug_port, int):
                                                log.info("✅ Found debug_port=%s from already running profile", debug_port)
                                                return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)
                                            if ws_endpoint:
                                                port = OctoClient._port_from_ws_url(ws_endpoint)
                                                if isinstance(port, int):
                                                    log.info("✅ Found ws_endpoint, port=%s from already running profile", port)
                                                    return StartedProfile(uuid=uuid, debug_port=port, ws_endpoint=ws_endpoint)
                    except Exception as running_info_error:
                        log.debug("Failed to get running profile info: %s", running_info_error)
                    
                    # Если не нашли через список, пробуем получить информацию напрямую о профиле
                    # Пробуем разные эндпоинты для получения информации о запущенном профиле
                    profile_info_endpoints = [
                        f"/api/profiles/{uuid}",
                        f"/api/v2/profiles/{uuid}",
                        f"/api/v2/automation/profiles/{uuid}",
                    ]
                    
                    for profile_info_endpoint in profile_info_endpoints:
                        try:
                            profile_info_resp = self._request("GET", profile_info_endpoint, use_cloud_api=False)
                            log.debug("Profile info response from %s: %s", profile_info_endpoint, profile_info_resp)
                            
                            profile_info_data = profile_info_resp.get("data") if isinstance(profile_info_resp.get("data"), dict) else profile_info_resp
                            debug_port = (
                                profile_info_data.get("debug_port") or
                                profile_info_data.get("selenium_port") or
                                profile_info_data.get("port") or
                                profile_info_resp.get("debug_port") or
                                profile_info_resp.get("selenium_port") or
                                profile_info_resp.get("port")
                            )
                            
                            if isinstance(debug_port, str):
                                if ":" in debug_port:
                                    try:
                                        debug_port = int(debug_port.split(":")[-1])
                                    except (ValueError, IndexError):
                                        debug_port = None
                                else:
                                    try:
                                        debug_port = int(debug_port)
                                    except (ValueError, TypeError):
                                        debug_port = None
                            
                            ws_endpoint = profile_info_data.get("ws_endpoint") or profile_info_resp.get("ws_endpoint")
                            if isinstance(ws_endpoint, str):
                                ws_endpoint = ws_endpoint.strip() or None
                            if isinstance(debug_port, int):
                                log.info("✅ Found debug_port=%s from profile info (%s)", debug_port, profile_info_endpoint)
                                return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)
                            if ws_endpoint:
                                port = OctoClient._port_from_ws_url(ws_endpoint)
                                if isinstance(port, int):
                                    log.info("✅ Found ws_endpoint, port=%s from profile info (%s)", port, profile_info_endpoint)
                                    return StartedProfile(uuid=uuid, debug_port=port, ws_endpoint=ws_endpoint)
                        except Exception as profile_info_error:
                            log.debug("Failed to get profile info from %s: %s", profile_info_endpoint, profile_info_error)
                            continue
                    
                    # Если не нашли через прямые эндпоинты, пробуем список запущенных профилей через другие эндпоинты
                    running_list_endpoints = [
                        "/api/profiles",
                        "/api/v2/profiles",
                        "/api/v2/automation/profiles",
                    ]
                    
                    for list_endpoint in running_list_endpoints:
                        try:
                            running_list_resp = self._request("GET", list_endpoint, use_cloud_api=False)
                            log.debug("Running profiles list from %s: %s", list_endpoint, running_list_resp)
                            
                            if isinstance(running_list_resp, (dict, list)):
                                profiles_list = []
                                if isinstance(running_list_resp, dict):
                                    profiles_list = running_list_resp.get("data", []) or running_list_resp.get("profiles", []) or running_list_resp.get("list", [])
                                elif isinstance(running_list_resp, list):
                                    profiles_list = running_list_resp
                                
                                if isinstance(profiles_list, list):
                                    for profile in profiles_list:
                                        if isinstance(profile, dict):
                                            profile_uuid = profile.get("uuid") or (profile.get("data", {}) if isinstance(profile.get("data"), dict) else {}).get("uuid")
                                            if profile_uuid == uuid:
                                                profile_data = profile.get("data") if isinstance(profile.get("data"), dict) else profile
                                                debug_port = (
                                                    profile_data.get("debug_port") or
                                                    profile_data.get("selenium_port") or
                                                    profile_data.get("port") or
                                                    profile.get("debug_port") or
                                                    profile.get("selenium_port") or
                                                    profile.get("port")
                                                )
                                                
                                                if isinstance(debug_port, str):
                                                    if ":" in debug_port:
                                                        try:
                                                            debug_port = int(debug_port.split(":")[-1])
                                                        except (ValueError, IndexError):
                                                            debug_port = None
                                                    else:
                                                        try:
                                                            debug_port = int(debug_port)
                                                        except (ValueError, TypeError):
                                                            debug_port = None
                                                
                                                ws_endpoint = profile_data.get("ws_endpoint") or profile.get("ws_endpoint")
                                                if isinstance(ws_endpoint, str):
                                                    ws_endpoint = ws_endpoint.strip() or None
                                                if isinstance(debug_port, int):
                                                    log.info("✅ Found debug_port=%s from running profiles list (%s)", debug_port, list_endpoint)
                                                    return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)
                                                if ws_endpoint:
                                                    port = OctoClient._port_from_ws_url(ws_endpoint)
                                                    if isinstance(port, int):
                                                        log.info("✅ Found ws_endpoint, port=%s from running list (%s)", port, list_endpoint)
                                                        return StartedProfile(uuid=uuid, debug_port=port, ws_endpoint=ws_endpoint)
                        except Exception as list_error:
                            log.debug("Failed to get running profiles from %s: %s", list_endpoint, list_error)
                            continue
                    
                    already_started_no_port = True
                    log.info("GET /profiles/active или /profiles — ответ (нет debug_port/ws): %s", running_profiles_resp)
                    log.warning("⚠️  Профиль уже запущен, но debug_port/ws_endpoint не найден в active. Пробуем Hard Reset...")
                    
                    # Hard Reset: агрессивная попытка остановить профиль
                    log.info("🔄 Hard Reset: попытка принудительной остановки профиля...")
                    force_stop_success = self.force_stop_profile(uuid, max_retries=3, initial_wait_s=3.0)
                    
                    if force_stop_success:
                        # Даем время Octo на "отлипание" после force_stop
                        wait_after_stop = 15.0
                        log.info("⏳ Ожидание %d секунд после force_stop для синхронизации состояния...", wait_after_stop)
                        time.sleep(wait_after_stop)
                        
                        # Пробуем снова запустить профиль
                        log.info("🔄 Повторная попытка запуска профиля после Hard Reset...")
                        try:
                            retry_resp = self._request("POST", endpoint, json_payload=payload, use_cloud_api=False)
                            log.debug("Hard Reset retry response: %s", retry_resp)
                            
                            # Парсим ответ от повторного запуска
                            def _extract_retry(resp: dict) -> tuple:
                                out_port, out_ws = None, None
                                for src in (resp, resp.get("data") or {}):
                                    if not isinstance(src, dict):
                                        continue
                                    raw = src.get("debug_port") or src.get("selenium_port") or src.get("port")
                                    p = OctoClient._parse_debug_port(raw)
                                    w = src.get("ws_endpoint") or src.get("webdriver")
                                    if isinstance(w, str) and w.strip():
                                        w = w.strip()
                                    else:
                                        w = None
                                    if isinstance(p, int):
                                        out_port, out_ws = p, w
                                        break
                                return (out_port, out_ws)
                            
                            retry_port, retry_ws = _extract_retry(retry_resp)
                            if isinstance(retry_port, int):
                                log.info("✅ Hard Reset успешен: профиль запущен после force_stop, debug_port=%s", retry_port)
                                if not retry_ws and retry_port:
                                    retry_ws = OctoClient._fetch_ws_endpoint_from_port(retry_port)
                                return StartedProfile(uuid=uuid, debug_port=retry_port, ws_endpoint=retry_ws)
                            else:
                                log.warning("Hard Reset: профиль запущен, но debug_port все еще недоступен")
                        except OctoAPIError as retry_e:
                            log.warning("Hard Reset: повторный запуск не удался: %s", retry_e)
                    else:
                        log.warning("Hard Reset: force_stop не удался, профиль может быть в нестабильном состоянии")
                    
                    log.warning("⚠️  Hard Reset не помог. Профиль все еще в состоянии 'zombie' (запущен без debug_port)")
            else:
                raise
        except Exception as e:
            wrap = e if isinstance(e, OctoAPIError) else OctoAPIError(str(e))
            raise OctoAPIError(f"Could not start profile {uuid} via Local API: {wrap}") from e
        
        if already_started_no_port:
            # Если разрешен порт-скан или задан debug_port_override, попробуем продолжить
            if allow_port_scan or debug_port_override:
                log.warning(
                    "Profile %s already running but debug_port/ws_endpoint missing. "
                    "Continuing with port scan/debug_port override fallback.",
                    uuid,
                )
                # Дадим пройти в ветку с retry/port-scan логикой ниже
                resp = {"success": True, "data": None}
            else:
                raise OctoAPIError(
                    f"Profile {uuid} already running but debug_port/ws_endpoint not in GET /profiles/active or /profiles. "
                    f"Hard Reset attempted but failed. Use allow_port_scan=True or --debug-port PORT."
                )
        if resp is None:
            try:
                resp = self._request("POST", f"/api/v2/automation/profiles/{uuid}/start", json_payload=payload, use_cloud_api=False)
                log.info("Fallback: POST /api/v2/automation/.../start → keys=%s", list(resp.keys()) if isinstance(resp, dict) else "?")
            except OctoAPIError as final_e:
                raise OctoAPIError(f"Could not start profile {uuid} via Local API. Last error: {last_error or final_e}")
        
        # Парсим ответ: сырой {uuid, ws_endpoint, debug_port, ...} или обёртка {success, data: {...}}
        def _extract(resp: dict) -> tuple:
            out_port, out_ws = None, None
            for src in (resp, resp.get("data") or {}):
                if not isinstance(src, dict):
                    continue
                raw = src.get("debug_port") or src.get("selenium_port") or src.get("port")
                p = OctoClient._parse_debug_port(raw)
                w = src.get("ws_endpoint") or src.get("webdriver")
                if isinstance(w, str) and w.strip():
                    w = w.strip()
                else:
                    w = None
                if isinstance(p, int):
                    out_port, out_ws = p, w
                    break
            return (out_port, out_ws)

        log.debug("Start profile response: %s", resp)
        debug_port, ws_endpoint = _extract(resp)
        if isinstance(debug_port, int):
            log.info("✅ debug_port=%s from start response", debug_port)
            if not ws_endpoint and debug_port:
                ws_endpoint = OctoClient._fetch_ws_endpoint_from_port(debug_port)
            return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)
        
        if resp.get("data") is None and resp.get("success") is True:
            log.info("API вернул success, data=None (нет debug_port). Ответ: %s", resp)

            # Пробуем с повторными попытками и увеличивающимися задержками
            log.debug("Local API returned success but data is None, profile may not be synced yet. Retrying with delays...")
            max_retries = 5
            for retry in range(max_retries):
                wait_time = 2 * (retry + 1)  # Увеличиваем задержку: 2, 4, 6, 8, 10 секунд
                log.debug("Waiting %d seconds for profile sync (retry %d/%d)...", wait_time, retry + 1, max_retries)
                time.sleep(wait_time)
                
                try:
                    retry_resp = self._request("POST", "/api/profiles/start", json_payload=payload, use_cloud_api=False)
                    log.debug("Retry %d response: %s", retry + 1, retry_resp)
                    rp, rw = _extract(retry_resp)
                    if isinstance(rp, int):
                        resp = retry_resp
                        log.info("Profile started on retry %d, debug_port=%s", retry + 1, rp)
                        break
                except OctoAPIError as retry_e:
                    log.debug("Retry %d failed: %s", retry + 1, retry_e)
                    if retry == max_retries - 1:
                        # Последняя попытка - пробуем Cloud API для запуска
                        log.debug("All Local API retries failed, trying Cloud API for start as last resort...")
                        try:
                            cloud_endpoint = f"/api/v2/automation/profiles/{uuid}/start"
                            cloud_resp = self._request("POST", cloud_endpoint, json_payload=payload, use_cloud_api=True)
                            log.debug("Cloud API start response: %s", cloud_resp)
                            # Проверяем наличие debug_port или selenium_port в ответе
                            if cloud_resp.get("data") is not None or cloud_resp.get("debug_port") or cloud_resp.get("selenium_port"):
                                resp = cloud_resp
                                log.info("✅ Profile started via Cloud API")
                                break
                        except OctoAPIError as cloud_e:
                            log.debug("Cloud API start also failed: %s", cloud_e)
                            pass
                        # Не выбрасываем исключение здесь - продолжим проверку эндпоинтов ниже
                    continue
            
            debug_port, ws_endpoint = _extract(resp)
            if isinstance(debug_port, int):
                log.info("✅ debug_port=%s from retry/cloud", debug_port)
                if not ws_endpoint and debug_port:
                    ws_endpoint = OctoClient._fetch_ws_endpoint_from_port(debug_port)
                return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)
            
            # Если после всех попыток data все еще None, но success=True, профиль может быть запущен
            if resp.get("data") is None and resp.get("success") is True:
                log.debug("Profile start returned data=None but success=True. Waiting for profile to fully start...")
                # Даем больше времени профилю полностью запуститься (Selenium может запускаться дольше)
                log.info("⏳ Ожидание инициализации CDP-порта (до ~30 сек)...")
                time.sleep(5)  # Увеличиваем начальное ожидание
                
                # Пробуем получить информацию о запущенном профиле через разные эндпоинты
                # Local API может иметь специальный эндпоинт для получения информации о запущенных профилях
                endpoints_to_check = [
                    f"/api/v2/automation/profiles/{uuid}",
                    f"/api/v2/profiles/{uuid}",
                    f"/api/v2/automation/profiles/{uuid}/status",
                    f"/api/v2/profiles/{uuid}/status",
                ]
                
                # Также пробуем получить список всех запущенных профилей
                list_endpoints = [
                    "/api/v2/automation/profiles",
                    "/api/v2/profiles",
                    "/api/v2/automation/profiles/active",
                    "/api/v2/profiles/active",
                ]
                
                # Пробуем несколько раз с задержками, так как профиль может запускаться постепенно
                # Также пробуем Cloud API для получения информации о профиле
                max_info_retries = 6
                for info_retry in range(max_info_retries):
                    if info_retry > 0:
                        wait_seconds = 3 + info_retry  # 4, 5, 6, 7, 8 секунд
                        log.debug("Retry %d/%d: waiting %d seconds before checking profile info...", 
                                info_retry + 1, max_info_retries, wait_seconds)
                        time.sleep(wait_seconds)
                    
                    # Пробуем также Cloud API для получения информации о профиле
                    if info_retry > 0:  # Пробуем Cloud API начиная со второй попытки
                        try:
                            cloud_info_resp = self._request("GET", f"/api/v2/automation/profiles/{uuid}", use_cloud_api=True)
                            log.debug("Got profile info from Cloud API (retry %d): %s", info_retry + 1, cloud_info_resp)
                            
                            cloud_data = cloud_info_resp.get("data") if isinstance(cloud_info_resp.get("data"), dict) else cloud_info_resp
                            selenium_port = (
                                cloud_data.get("selenium_port") or 
                                cloud_data.get("port") or
                                cloud_data.get("debug_port") or
                                cloud_data.get("webdriver_port") or
                                cloud_info_resp.get("selenium_port") or
                                cloud_info_resp.get("port") or
                                (cloud_data.get("ws", {}) if isinstance(cloud_data.get("ws"), dict) else {}).get("selenium")
                            )
                            
                            if isinstance(selenium_port, str):
                                if ":" in selenium_port:
                                    try:
                                        selenium_port = int(selenium_port.split(":")[-1])
                                    except (ValueError, IndexError):
                                        selenium_port = None
                                else:
                                    try:
                                        selenium_port = int(selenium_port)
                                    except (ValueError, TypeError):
                                        selenium_port = None
                            
                            if isinstance(selenium_port, int):
                                log.info("✅ Found selenium_port=%s from Cloud API (retry %d)", selenium_port, info_retry + 1)
                                ws_endpoint = cloud_data.get("ws_endpoint") or cloud_info_resp.get("ws_endpoint")
                                return StartedProfile(uuid=uuid, debug_port=selenium_port, ws_endpoint=ws_endpoint)
                        except OctoAPIError as cloud_info_e:
                            log.debug("Failed to get profile info from Cloud API (retry %d): %s", info_retry + 1, cloud_info_e)
                    
                    for check_endpoint in endpoints_to_check:
                        try:
                            check_resp = self._request("GET", check_endpoint, use_cloud_api=False)
                            log.debug("Got response from %s (retry %d): %s", check_endpoint, info_retry + 1, check_resp)
                            
                            # Ищем selenium_port в ответе
                            check_data = check_resp.get("data") if isinstance(check_resp.get("data"), dict) else check_resp
                            
                            # Проверяем все возможные места, где может быть порт
                            selenium_port = (
                                check_data.get("selenium_port") or 
                                check_data.get("port") or
                                check_data.get("debug_port") or
                                check_data.get("webdriver_port") or
                                check_resp.get("selenium_port") or
                                check_resp.get("port") or
                                check_resp.get("debug_port") or
                                check_resp.get("webdriver_port") or
                                (check_data.get("ws", {}) if isinstance(check_data.get("ws"), dict) else {}).get("selenium") or
                                (check_resp.get("ws", {}) if isinstance(check_resp.get("ws"), dict) else {}).get("selenium")
                            )
                            
                            # Если порт в формате строки "127.0.0.1:xxxx" или "xxxx"
                            if isinstance(selenium_port, str):
                                if ":" in selenium_port:
                                    try:
                                        selenium_port = int(selenium_port.split(":")[-1])
                                    except (ValueError, IndexError):
                                        selenium_port = None
                                else:
                                    try:
                                        selenium_port = int(selenium_port)
                                    except (ValueError, TypeError):
                                        selenium_port = None
                            
                            if isinstance(selenium_port, int):
                                log.info("✅ Found selenium_port=%s from endpoint %s (retry %d)", 
                                       selenium_port, check_endpoint, info_retry + 1)
                                ws_endpoint = check_data.get("ws_endpoint") or check_resp.get("ws_endpoint")
                                return StartedProfile(uuid=uuid, debug_port=selenium_port, ws_endpoint=ws_endpoint)
                        except OctoAPIError as check_e:
                            log.debug("Failed to get info from %s (retry %d): %s", check_endpoint, info_retry + 1, check_e)
                            continue
                    
                    # Если не нашли в endpoints_to_check, пробуем список профилей
                    if info_retry < max_info_retries - 1:
                        continue  # Продолжаем попытки
                
                # Пробуем получить список запущенных профилей (тоже с повторными попытками)
                log.debug("Trying to get selenium_port from running profiles list...")
                for info_retry in range(max_info_retries):
                    if info_retry > 0:
                        wait_seconds = 3 + info_retry
                        log.debug("List retry %d/%d: waiting %d seconds...", info_retry + 1, max_info_retries, wait_seconds)
                        time.sleep(wait_seconds)
                    
                    for list_endpoint in list_endpoints:
                        try:
                            list_resp = self._request("GET", list_endpoint, use_cloud_api=False)
                            log.debug("Got response from list endpoint %s (retry %d, first 500 chars): %s", 
                                    list_endpoint, info_retry + 1, str(list_resp)[:500])
                            
                            # Ищем наш профиль в списке
                            if isinstance(list_resp, dict):
                                profiles = list_resp.get("data", []) or list_resp.get("profiles", []) or list_resp.get("list", [])
                                if isinstance(profiles, list):
                                    for profile in profiles:
                                        if isinstance(profile, dict):
                                            profile_uuid = profile.get("uuid") or (profile.get("data", {}) if isinstance(profile.get("data"), dict) else {}).get("uuid")
                                            if profile_uuid == uuid:
                                                # Пробуем извлечь selenium_port из профиля в списке
                                                profile_data = profile.get("data") if isinstance(profile.get("data"), dict) else profile
                                                selenium_port = (
                                                    profile_data.get("selenium_port") or 
                                                    profile_data.get("port") or
                                                    profile_data.get("debug_port") or
                                                    profile_data.get("webdriver_port") or
                                                    profile.get("selenium_port") or
                                                    profile.get("port") or
                                                    profile.get("debug_port") or
                                                    profile.get("webdriver_port") or
                                                    (profile_data.get("ws", {}) if isinstance(profile_data.get("ws"), dict) else {}).get("selenium") or
                                                    (profile.get("ws", {}) if isinstance(profile.get("ws"), dict) else {}).get("selenium")
                                                )
                                                
                                                # Обрабатываем строковый формат порта
                                                if isinstance(selenium_port, str):
                                                    if ":" in selenium_port:
                                                        try:
                                                            selenium_port = int(selenium_port.split(":")[-1])
                                                        except (ValueError, IndexError):
                                                            selenium_port = None
                                                    else:
                                                        try:
                                                            selenium_port = int(selenium_port)
                                                        except (ValueError, TypeError):
                                                            selenium_port = None
                                                
                                                if isinstance(selenium_port, int):
                                                    log.info("✅ Found selenium_port=%s from running profiles list (%s, retry %d)", 
                                                           selenium_port, list_endpoint, info_retry + 1)
                                                    ws_endpoint = profile_data.get("ws_endpoint") or profile.get("ws_endpoint")
                                                    return StartedProfile(uuid=uuid, debug_port=selenium_port, ws_endpoint=ws_endpoint)
                        except OctoAPIError as list_e:
                            log.debug("Failed to get list from %s (retry %d): %s", list_endpoint, info_retry + 1, list_e)
                            continue
                    
                    # Если нашли порт, выходим из цикла
                    # (проверка будет в следующем блоке кода)
                
                # Если не нашли через эндпоинты, пробуем get_profile_status (тоже с повторными попытками)
                log.debug("Trying get_profile_status as fallback...")
                for status_retry in range(max_info_retries):
                    if status_retry > 0:
                        wait_seconds = 3 + status_retry
                        log.debug("Status retry %d/%d: waiting %d seconds...", status_retry + 1, max_info_retries, wait_seconds)
                        time.sleep(wait_seconds)
                    
                    profile_status = self.get_profile_status(uuid)
                    if profile_status:
                        status_data = profile_status.get("data") if isinstance(profile_status.get("data"), dict) else profile_status
                        selenium_port = (
                            status_data.get("selenium_port") or 
                            status_data.get("port") or
                            status_data.get("debug_port") or
                            status_data.get("webdriver_port") or
                            profile_status.get("selenium_port") or
                            profile_status.get("port") or
                            (status_data.get("ws", {}) if isinstance(status_data.get("ws"), dict) else {}).get("selenium")
                        )
                        
                        # Обрабатываем строковый формат
                        if isinstance(selenium_port, str):
                            if ":" in selenium_port:
                                try:
                                    selenium_port = int(selenium_port.split(":")[-1])
                                except (ValueError, IndexError):
                                    selenium_port = None
                            else:
                                try:
                                    selenium_port = int(selenium_port)
                                except (ValueError, TypeError):
                                    selenium_port = None
                        
                        if isinstance(selenium_port, int):
                            log.info("✅ Found selenium_port=%s from get_profile_status (retry %d)", selenium_port, status_retry + 1)
                            ws_endpoint = status_data.get("ws_endpoint") or profile_status.get("ws_endpoint")
                            return StartedProfile(uuid=uuid, debug_port=selenium_port, ws_endpoint=ws_endpoint)
                
                if not allow_port_scan and not debug_port_override:
                    log.error(
                        "Profile started but debug_port not available via API. "
                        "Use allow_port_scan=True or --debug-port PORT."
                    )
                    raise OctoAPIError(
                        f"Profile {uuid} started (success=True) but debug_port not available. "
                        f"Use allow_port_scan=True or --debug-port PORT."
                    )

                if debug_port_override:
                    ws = OctoClient._fetch_ws_endpoint_from_port(debug_port_override)
                    if ws:
                        log.info("Используем --debug-port %s, подключаемся по CDP", debug_port_override)
                        return StartedProfile(uuid=uuid, debug_port=debug_port_override, ws_endpoint=ws)
                    log.error("Порт %s не отдаёт CDP (/json/version). Закрой другие Chrome/профили или укажи другой порт.", debug_port_override)
                    raise OctoAPIError(
                        f"Profile {uuid} started but --debug-port {debug_port_override} has no CDP. "
                        f"Try another port or allow_port_scan without --debug-port."
                    )

                log.warning("Profile started but debug_port not available via API. Port scan enabled...")
                # Octo часто использует 52xxx (пример: 52341); также 92xx. Сначала 52xxx, потом 92xx.
                ports_52k = list(range(52000, 53201))
                ports_92 = list(range(9222, 9351))
                ports_to_try = ports_52k + ports_92
                log.info("Сканирование CDP: порты %d–%d и %d–%d...", ports_52k[0], ports_52k[-1], ports_92[0], ports_92[-1])

                import socket
                for port in ports_to_try:
                    try:
                        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        sock.settimeout(0.5)
                        result = sock.connect_ex(('127.0.0.1', port))
                        sock.close()
                        if result != 0:
                            continue
                        # Сначала CDP (Octo использует его), затем Selenium
                        ws = None
                        for ep in (f"http://127.0.0.1:{port}/json/version", f"http://127.0.0.1:{port}/wd/hub/status"):
                            try:
                                r = requests.get(ep, timeout=2)
                                if r.status_code == 200 or (ep.endswith("/wd/hub/status") and r.status_code in (404, 405)):
                                    ws = OctoClient._fetch_ws_endpoint_from_port(port)
                                    if ws:
                                        log.info("✅ Найден CDP-порт %s", port)
                                        return StartedProfile(uuid=uuid, debug_port=port, ws_endpoint=ws)
                                    break
                            except requests.RequestException:
                                continue
                    except Exception as e:
                        log.debug("Port %s check failed: %s", port, e)
                
                if debug_port_override:
                    ws = OctoClient._fetch_ws_endpoint_from_port(debug_port_override)
                    if ws:
                        log.info("Используем --debug-port %s (скан не нашёл CDP)", debug_port_override)
                        return StartedProfile(uuid=uuid, debug_port=debug_port_override, ws_endpoint=ws)
                log.error("❌ Не удалось найти debug_port/CDP через API или сканирование портов")
                log.error("   Профиль UUID: %s", uuid)
                log.error("   Варианты: 1) --debug-port PORT (порт из Octo UI)  2) перезапуск Octo  3) проверить настройки автоматизации")
                raise OctoAPIError(
                    f"Profile {uuid} started but debug_port not available. "
                    f"Port scan 52xxx+92xx: no CDP. Use --debug-port PORT or restart Octo."
                )
        
        data: Dict[str, Any] = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        raw = (
            data.get("debug_port") or resp.get("debug_port") or
            data.get("selenium_port") or resp.get("selenium_port") or
            data.get("port") or resp.get("port") or
            data.get("webdriver_port") or resp.get("webdriver_port") or
            (data.get("ws", {}) if isinstance(data.get("ws"), dict) else {}).get("selenium") or
            (resp.get("ws", {}) if isinstance(resp.get("ws"), dict) else {}).get("selenium")
        )
        debug_port = OctoClient._parse_debug_port(raw)
        if not isinstance(debug_port, int):
            raise OctoAPIError(f"Start profile response missing debug_port/selenium_port: {resp!r}")

        ws_endpoint = data.get("ws_endpoint") or resp.get("ws_endpoint") or data.get("webdriver") or resp.get("webdriver")
        if isinstance(ws_endpoint, str) and ws_endpoint.strip():
            ws_endpoint = ws_endpoint.strip()
        else:
            ws_endpoint = None
        if not ws_endpoint and debug_port:
            ws_endpoint = OctoClient._fetch_ws_endpoint_from_port(debug_port)

        log.info("✅ Started Octo profile uuid=%s debug_port=%s", uuid, debug_port)
        return StartedProfile(uuid=uuid, debug_port=debug_port, ws_endpoint=ws_endpoint)

    def stop_profile(self, uuid: str) -> None:
        """Stop a running profile via Local API. Tries /api/profiles/stop, force_stop, then v2."""
        body = {"uuid": uuid}
        for endpoint, use_body in (
            ("/api/profiles/stop", True),
            ("/api/profiles/force_stop", True),
            (f"/api/v2/automation/profiles/{uuid}/stop", True),
            (f"/api/profiles/{uuid}/stop", True),
        ):
            try:
                self._request("POST", endpoint, json_payload=body if use_body else None, use_cloud_api=False)
                log.info("Профиль остановлен: %s via %s", uuid, endpoint)
                return
            except OctoAPIError as e:
                log.debug("Stop %s failed: %s", endpoint, e)
                continue
        log.warning("Не удалось остановить профиль %s через Local API", uuid)

    def force_stop_profile(self, uuid: str, max_retries: int = 3, initial_wait_s: float = 2.0) -> bool:
        """
        Force-stop profile. Use when normal stop leaves profile running.
        
        Args:
            uuid: Profile UUID to stop
            max_retries: Maximum number of retry attempts
            initial_wait_s: Initial wait time between retries (exponentially increases)
        
        Returns:
            True if stop succeeded, False otherwise
        """
        body = {"uuid": uuid}
        endpoints = ("/api/profiles/force_stop", "/api/profiles/stop", f"/api/v2/automation/profiles/{uuid}/stop")
        
        for attempt in range(max_retries):
            wait_time = initial_wait_s * (2 ** attempt)  # Exponential backoff: 2s, 4s, 8s
            if attempt > 0:
                log.debug("Force-stop retry %d/%d, waiting %d seconds...", attempt, max_retries, wait_time)
                time.sleep(wait_time)
            
            for endpoint in endpoints:
                try:
                    self._request("POST", endpoint, json_payload=body, use_cloud_api=False)
                    log.info("Force-stop профиля %s via %s (attempt %d)", uuid, endpoint, attempt + 1)
                    return True
                except OctoAPIError as e:
                    log.debug("Force-stop %s failed (attempt %d): %s", endpoint, attempt + 1, e)
                    continue
            
            # Если все эндпоинты не сработали, пробуем Cloud API на последней попытке
            if attempt == max_retries - 1:
                try:
                    cloud_endpoint = f"/api/v2/automation/profiles/{uuid}/stop"
                    self._request("POST", cloud_endpoint, json_payload=body, use_cloud_api=True)
                    log.info("Force-stop профиля %s via Cloud API %s (last attempt)", uuid, cloud_endpoint)
                    return True
                except OctoAPIError as cloud_e:
                    log.debug("Force-stop via Cloud API also failed: %s", cloud_e)
        
        log.warning("Force-stop профиля %s не удался после %d попыток", uuid, max_retries)
        return False

    def delete_profiles(self, uuids: List[str]) -> None:
        """
        Delete profiles via Cloud API.
        
        According to Octo Browser API v2 documentation, deleting profiles
        should be done via Cloud API (https://app.octobrowser.net).
        """
        payload = {"uuid": uuids}
        self._request("DELETE", "/api/v2/automation/profiles", json_payload=payload, use_cloud_api=True)
        log.debug("Deleted Octo profiles uuids=%s", uuids)


class OctoAutomator:
    """
    CDP-based automation for an already started Octo profile.
    Uses Playwright over CDP (ws_endpoint). Reuses the first page if present.
    """

    def __init__(self, started: StartedProfile) -> None:
        self._started = started
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    def _resolve_ws_endpoint(self) -> str:
        ws = self._started.ws_endpoint
        if isinstance(ws, str) and ws.strip():
            return ws.strip()
        if isinstance(self._started.debug_port, int):
            fetched = OctoClient._fetch_ws_endpoint_from_port(self._started.debug_port)
            if fetched:
                return fetched
        raise OctoAutomationError(
            "No ws_endpoint and could not derive from debug_port. "
            "Ensure the profile was started with debug_port=True and API returns ws_endpoint or debug_port."
        )

    def connect(self) -> OctoAutomator:
        """Connect to the running Octo profile via CDP. Reuses first page if present."""
        if sync_playwright is None:
            raise OctoAutomationError("Playwright not installed. pip install playwright && python -m playwright install chromium")
        ws = self._resolve_ws_endpoint()
        log.info("Connecting to Octo profile via CDP: %s", ws[:80] + "..." if len(ws) > 80 else ws)
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.connect_over_cdp(ws)
        except Exception as e:
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            raise OctoAutomationError(f"Failed to connect over CDP: {e}") from e

        contexts = self._browser.contexts
        if not contexts:
            self.disconnect()
            raise OctoAutomationError("No browser contexts in connected profile")
        ctx = contexts[0]
        if ctx.pages:
            self._page = ctx.pages[0]
            log.debug("Reusing existing page (first tab)")
        else:
            self._page = ctx.new_page()
            log.debug("Opened new page")
        log.info("OctoAutomator connected")
        return self

    def disconnect(self) -> None:
        """Disconnect from the browser. Does NOT stop the Octo profile."""
        errs: List[str] = []
        if self._browser:
            try:
                self._browser.close()
            except Exception as e:
                errs.append(f"browser.close: {e}")
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception as e:
                errs.append(f"playwright.stop: {e}")
            self._playwright = None
        self._page = None
        if errs:
            log.debug("Disconnect warnings: %s", errs)
        log.info("OctoAutomator disconnected")

    def close(self) -> None:
        """Alias for disconnect(). Does NOT stop the Octo profile."""
        self.disconnect()

    def _page_or_raise(self) -> Any:
        if self._page is None:
            raise OctoAutomationError("Not connected. Call connect() first.")
        return self._page

    def goto(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 60000,
    ) -> None:
        """Navigate to url. wait_until: load | domcontentloaded | commit."""
        page = self._page_or_raise()
        log.info("goto %s (wait_until=%s)", url, wait_until)
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as e:
            raise OctoAutomationError(f"goto {url!r} failed: {e}") from e

    def click(self, selector: str, timeout_ms: int = 30000) -> None:
        """Click the first element matching selector."""
        page = self._page_or_raise()
        log.info("click %s", selector)
        try:
            page.click(selector, timeout=timeout_ms)
        except Exception as e:
            raise OctoAutomationError(f"click {selector!r} failed: {e}") from e

    def type(
        self,
        selector: str,
        text: str,
        clear: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        """Type text into the first element matching selector. If clear=True, clear first; else append."""
        page = self._page_or_raise()
        log.info("type %s (clear=%s) len=%d", selector, clear, len(text))
        try:
            el = page.locator(selector).first
            el.wait_for(state="visible", timeout=timeout_ms)
            if clear:
                el.fill(text)
            else:
                el.focus()
                el.press_sequentially(text)
        except Exception as e:
            raise OctoAutomationError(f"type {selector!r} failed: {e}") from e

    def wait_for(
        self,
        selector: str,
        state: str = "visible",
        timeout_ms: int = 30000,
    ) -> None:
        """Wait for selector to reach state (visible | attached | hidden)."""
        page = self._page_or_raise()
        log.debug("wait_for %s state=%s", selector, state)
        try:
            page.locator(selector).first.wait_for(state=state, timeout=timeout_ms)
        except Exception as e:
            raise OctoAutomationError(f"wait_for {selector!r} state={state!r} failed: {e}") from e

    def scroll(
        self,
        pixels: int = 1200,
        steps: int = 3,
        delay_ms: int = 200,
    ) -> None:
        """Scroll down by pixels over steps, with delay_ms between steps."""
        page = self._page_or_raise()
        log.debug("scroll pixels=%s steps=%s delay_ms=%s", pixels, steps, delay_ms)
        if steps <= 0:
            steps = 1
        chunk = pixels // steps
        try:
            for _ in range(steps):
                page.mouse.wheel(0, chunk)
                time.sleep(delay_ms / 1000.0)
        except Exception as e:
            raise OctoAutomationError(f"scroll failed: {e}") from e

    def get_url(self) -> str:
        """Return current page URL."""
        page = self._page_or_raise()
        try:
            return page.url
        except Exception as e:
            raise OctoAutomationError(f"get_url failed: {e}") from e

    def get_title(self) -> str:
        """Return page title."""
        page = self._page_or_raise()
        try:
            return page.title()
        except Exception as e:
            raise OctoAutomationError(f"get_title failed: {e}") from e

    def get_html(self) -> str:
        """Return page HTML (document.outerHTML)."""
        page = self._page_or_raise()
        log.debug("get_html")
        try:
            return page.content()
        except Exception as e:
            raise OctoAutomationError(f"get_html failed: {e}") from e

    def screenshot(self, path: str, full_page: bool = False) -> None:
        """Save a screenshot to path. If full_page=True, capture full scrollable page."""
        page = self._page_or_raise()
        log.info("screenshot %s (full_page=%s)", path, full_page)
        try:
            page.screenshot(path=path, full_page=full_page)
        except Exception as e:
            raise OctoAutomationError(f"screenshot {path!r} failed: {e}") from e
