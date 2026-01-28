"""
OctoScanner — массовая проверка клоакинговых ссылок через мобильные профили Octo Browser.

Идея:
Для КАЖДОГО URL создаём мобильный профиль Octo Browser -> запускаем ->
подключаемся через Playwright (CDP, OctoAutomator) -> переходим на URL -> собираем артефакты ->
отключаемся, останавливаем профиль (удаление опционально).

Заметки:
- Ошибки на одном URL не останавливают прогон; в `finally` всегда disconnect + stop_profile.
- Таймауты: API / страница / ожидания. По умолчанию мобильные профили (Android).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from octo_client import OctoAPIError, OctoAutomationError, OctoAutomator, OctoClient, StartedProfile


# Список мобильных UA для Android / Chrome Mobile.
# В проде лучше регулярно обновлять и/или отдавать выбор Octo (user_agent=None).
MOBILE_ANDROID_UAS: List[str] = [
    "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
]


def utc_timestamp_compact() -> str:
    # Пример: 20260126_135501Z
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = utc_timestamp_compact()
    log_path = log_dir / f"octoscanner_{ts}.log"

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

    handlers: List[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]

    logging.basicConfig(level=numeric_level, format=fmt, handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # шумные соединения requests


def normalize_url(raw: str) -> Optional[str]:
    """
    Нормализуем вход:
    - пустые строки/комментарии игнорируем
    - если нет схемы — добавляем https:// (для подозрительных URL это обычно ожидаемо)
    """
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        s = "https://" + s
    return s


def normalize_url_for_compare(raw: Optional[str]) -> Optional[str]:
    """
    Нормализует URL для сравнения редиректов:
    - нижний регистр для scheme/host
    - удаляет fragment
    - убирает trailing slash, если путь не корень
    """
    if not raw:
        return None
    try:
        parsed = urlparse(raw.strip())
    except Exception:
        return raw.strip()
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    # Убираем стандартные порты
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or ""
    # Считаем пустой путь и "/" одинаковыми.
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path[:-1]
    return f"{scheme}://{netloc}{path}{'?' + parsed.query if parsed.query else ''}"


def iter_targets(targets_path: Path) -> Iterable[str]:
    for line in targets_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        url = normalize_url(line)
        if url:
            yield url


def read_ua_file(path: Path) -> List[str]:
    """
    Читает UA из файла (по 1 на строку). Пустые и начинающиеся с # игнорируются.
    """
    out: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = (line or "").strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def read_json_file(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_proxy_file(path: Path) -> List[Dict[str, Any]]:
    """
    Читает прокси из файла. Поддерживает форматы:
    - JSON массив: [{"host": "...", "port": 8080, "username": "...", "password": "..."}, ...]
    - Текстовый формат (1 прокси на строку): host:port:username:password или host:port
    """
    content = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not content:
        return []

    # Пробуем JSON
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except (json.JSONDecodeError, ValueError):
        pass

    # Текстовый формат
    proxies: List[Dict[str, Any]] = []
    log = logging.getLogger(__name__)
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(":")
        if len(parts) >= 2:
            proxy: Dict[str, Any] = {"host": parts[0], "port": int(parts[1])}
            if len(parts) >= 4:
                proxy["username"] = parts[2]
                proxy["password"] = parts[3]
                log.debug("Read proxy from file: host=%s, port=%s, username=%s, password=%s",
                         proxy["host"], proxy["port"], proxy["username"], "***")
            else:
                log.debug("Read proxy from file (no auth): host=%s, port=%s", proxy["host"], proxy["port"])
            proxies.append(proxy)

    return proxies

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Глубокий merge словарей (для аккуратного объединения fingerprint/geo/etc).
    override "побеждает" base при конфликте.
    """
    out: Dict[str, Any] = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def safe_domain_for_folder(url: str) -> str:
    """
    Делаем безопасное имя папки из домена.
    Если URL кривой — используем 'unknown-host'.
    """
    try:
        host = urlparse(url).netloc or "unknown-host"
    except Exception:
        host = "unknown-host"
    host = host.strip().lower()
    host = re.sub(r"[^a-z0-9._-]+", "_", host)
    return host[:120] if host else "unknown-host"


class OctoCloakChecker:
    """
    Класс для проверки клоакинговых ссылок через Octo Browser.
    
    Инкапсулирует логику создания профилей, запуска браузера, навигации
    и сбора доказательств клоакинга.
    """
    
    def __init__(
        self,
        octo_client: OctoClient,
        evidence_root: Path,
        ua_mode: str = "octo",
        ua_custom: Optional[List[str]] = None,
        profile_overrides: Optional[Dict[str, Any]] = None,
        geo_mode: str = "octo",
        geo_file: Optional[Path] = None,
        geo_lat: Optional[float] = None,
        geo_lon: Optional[float] = None,
        geo_accuracy: Optional[float] = None,
        timezone_name: Optional[str] = None,
        locale: Optional[str] = None,
        os_name: str = "android",
        os_version: Optional[str] = None,
        page_timeout_s: float = 45.0,
        wait_timeout_s: float = 30.0,
        connect_timeout_s: float = 30.0,
        allow_port_scan: bool = False,
    ):
        self.octo = octo_client
        self.allow_port_scan = allow_port_scan
        self.evidence_root = evidence_root
        self.ua_mode = ua_mode
        self.ua_custom = ua_custom
        self.profile_overrides = profile_overrides
        self.geo_mode = geo_mode
        self.geo_file = geo_file
        self.geo_lat = geo_lat
        self.geo_lon = geo_lon
        self.geo_accuracy = geo_accuracy
        self.timezone_name = timezone_name
        self.locale = locale
        self.os_name = os_name
        self.os_version = os_version
        self.page_timeout_s = page_timeout_s
        self.wait_timeout_s = wait_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.log = logging.getLogger(self.__class__.__name__)
    
    def check_url(
        self,
        url: str,
        proxy: Optional[Dict[str, Any]] = None,
        proxy_uuid: Optional[str] = None,
        proxy_use_api: bool = False,
        keep_profile: bool = True,
    ) -> Dict[str, Any]:
        """
        Проверяет один URL на клоакинг.
        
        Args:
            url: URL для проверки
            proxy: Настройки прокси (словарь с host, port, username, password, type)
            proxy_uuid: UUID существующего прокси в Octo
            keep_profile: Сохранять ли профиль после проверки
        
        Returns:
            Словарь с результатами проверки:
            {
                "original_url": str,
                "final_url": Optional[str],
                "page_title": Optional[str],
                "screenshot_path": Optional[Path],
                "status": str,  # "success", "error", "timeout"
                "error": Optional[str],
            }
        """
        return investigate_one(
            url=url,
            octo=self.octo,
            evidence_root=self.evidence_root,
            ua_mode=self.ua_mode,
            ua_custom=self.ua_custom,
            profile_overrides=self.profile_overrides,
            geo_mode=self.geo_mode,
            geo_file=self.geo_file,
            geo_lat=self.geo_lat,
            geo_lon=self.geo_lon,
            geo_accuracy=self.geo_accuracy,
            timezone_name=self.timezone_name,
            locale=self.locale,
            proxy=proxy,
            proxy_uuid=proxy_uuid,
            proxy_use_api=proxy_use_api,
            os_name=self.os_name,
            os_version=self.os_version,
            keep_profile=keep_profile,
            page_timeout_s=self.page_timeout_s,
            wait_timeout_s=self.wait_timeout_s,
            connect_timeout_s=self.connect_timeout_s,
            allow_port_scan=self.allow_port_scan,
        )

    def check_urls(
        self,
        urls: List[str],
        proxy_list: Optional[List[Dict[str, Any]]] = None,
        proxy_uuid: Optional[str] = None,
        proxy_rotate: bool = False,
        keep_profile: bool = True,
        csv_report_path: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """
        Проверяет список URL на клоакинг.
        
        Args:
            urls: Список URL для проверки
            proxy_list: Список прокси для ротации
            proxy_uuid: UUID существующего прокси в Octo
            proxy_rotate: Ротировать ли прокси для каждого URL
            keep_profile: Сохранять ли профили после проверки
            csv_report_path: Путь для сохранения CSV отчета (по умолчанию: evidence_root/report.csv)
        
        Returns:
            Список результатов проверки для каждого URL
        """
        results: List[Dict[str, Any]] = []
        proxy_idx = 0
        
        if csv_report_path is None:
            csv_report_path = self.evidence_root / "report.csv"
        
        for idx, url in enumerate(urls, start=1):
            self.log.info("=" * 80)
            self.log.info("🔍 Проверка URL [%d/%d]: %s", idx, len(urls), url)
            self.log.info("=" * 80)
            
            # Выбираем прокси для текущего URL
            current_proxy: Optional[Dict[str, Any]] = None
            current_proxy_uuid: Optional[str] = proxy_uuid
            
            if proxy_list:
                if proxy_rotate:
                    current_proxy = proxy_list[proxy_idx % len(proxy_list)]
                    proxy_idx += 1
                    self.log.debug("Используется прокси [%d]: %s:%s", 
                                 proxy_idx - 1, current_proxy.get("host"), current_proxy.get("port"))
                else:
                    # Используем первый прокси для всех URL
                    current_proxy = proxy_list[0]
            
            result = self.check_url(
                url=url,
                proxy=current_proxy,
                proxy_uuid=current_proxy_uuid,
                proxy_use_api=args.proxy_use_api,
                keep_profile=keep_profile,
            )
            
            results.append(result)
            
            # Логируем результат проверки
            if result["status"] == "success":
                if normalize_url_for_compare(result["final_url"]) != normalize_url_for_compare(url):
                    self.log.info("✅ Успешно. Обнаружен редирект (возможен клоакинг): %s -> %s", 
                                url, result["final_url"])
                else:
                    self.log.info("✅ Успешно. URL не изменился (редиректа нет)")
            elif result["status"] == "timeout":
                self.log.warning("⏱️  Таймаут при проверке URL: %s", url)
            else:
                self.log.error("❌ Ошибка при проверке URL: %s - %s", url, result.get("error", "Unknown error"))
        
        # Сохраняем результаты в CSV
        save_results_to_csv(results, csv_report_path)
        
        # Статистика
        success_count = sum(1 for r in results if r["status"] == "success")
        error_count = sum(1 for r in results if r["status"] == "error")
        timeout_count = sum(1 for r in results if r["status"] == "timeout")
        redirect_count = sum(
            1
            for r in results
            if r["status"] == "success"
            and normalize_url_for_compare(r.get("final_url")) != normalize_url_for_compare(r.get("original_url"))
        )
        
        self.log.info("=" * 80)
        self.log.info("📊 ИТОГОВАЯ СТАТИСТИКА:")
        self.log.info("   Всего URL: %d", len(results))
        self.log.info("   ✅ Успешно: %d", success_count)
        self.log.info("   ⏱️  Таймаут: %d", timeout_count)
        self.log.info("   ❌ Ошибки: %d", error_count)
        self.log.info("   🔄 Редиректы (возможен клоакинг): %d", redirect_count)
        self.log.info("   📄 CSV отчет: %s", csv_report_path)
        self.log.info("=" * 80)
        
        return results


def save_results_to_csv(results: List[Dict[str, Any]], csv_path: Path) -> None:
    """
    Сохраняет результаты проверки в CSV файл.
    
    CSV содержит колонки:
    - Original_URL: исходный URL
    - Final_URL: финальный URL после редиректов
    - Page_Title: заголовок страницы
    - Screenshot_Path: путь к скриншоту (относительно корня проекта)
    - Status: статус проверки (success/error/timeout)
    - Error: описание ошибки (если есть)
    """
    log = logging.getLogger(__name__)
    
    if not results:
        log.warning("Нет результатов для сохранения в CSV")
        return
    
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    
    fieldnames = ["Original_URL", "Final_URL", "Page_Title", "Screenshot_Path", "Status", "Error"]
    
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for result in results:
            # Преобразуем Path в строку (относительный путь)
            screenshot_path_str = None
            if result.get("screenshot_path"):
                screenshot_path = result["screenshot_path"]
                if isinstance(screenshot_path, Path):
                    # Делаем путь относительным к корню проекта
                    try:
                        screenshot_path_str = str(screenshot_path.relative_to(Path.cwd()))
                    except ValueError:
                        # Если не получается сделать относительным, используем абсолютный
                        screenshot_path_str = str(screenshot_path)
                else:
                    screenshot_path_str = str(screenshot_path)
            
            writer.writerow({
                "Original_URL": result.get("original_url", ""),
                "Final_URL": result.get("final_url", ""),
                "Page_Title": result.get("page_title", ""),
                "Screenshot_Path": screenshot_path_str or "",
                "Status": result.get("status", "unknown"),
                "Error": result.get("error", ""),
            })
    
    log.info("Результаты сохранены в CSV: %s (%d записей)", csv_path, len(results))


def ensure_evidence_dir(root: Path, url: str) -> Path:
    ts = utc_timestamp_compact()
    domain = safe_domain_for_folder(url)
    out_dir = root / f"{ts}_{domain}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def collect_evidence_playwright(auto: OctoAutomator, out_dir: Path) -> Tuple[str, str, Path]:
    """
    Собираем улики через Playwright (OctoAutomator):
    screenshot.png (full-page), page.html, metadata.json.
    Возвращаем (final_url, title, screenshot_path).
    """
    final_url = auto.get_url()
    title = auto.get_title() or ""

    screenshot_path = out_dir / "screenshot.png"
    auto.screenshot(str(screenshot_path), full_page=True)

    html_path = out_dir / "page.html"
    html_path.write_text(auto.get_html() or "", encoding="utf-8", errors="ignore")

    meta_path = out_dir / "metadata.json"
    meta = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "final_url": final_url,
        "title": title,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return final_url, title, screenshot_path


def choose_user_agent(mode: str) -> Optional[str]:
    """
    Выбор UA:
    - 'random'  -> случайный из списка мобильных UA
    - 'octo'    -> None (пусть Octo поставит дефолт/свой генератор)
    """
    mode = (mode or "octo").lower()
    if mode == "random":
        return random.choice(MOBILE_ANDROID_UAS)
    return None


def choose_user_agent_from(mode: str, custom_uas: Optional[List[str]]) -> Optional[str]:
    mode = (mode or "octo").lower()
    if mode == "custom":
        # В custom_uas передадим список из 1 элемента (ua-value).
        if custom_uas:
            return custom_uas[0]
        return None
    if mode == "file":
        if custom_uas:
            return random.choice(custom_uas)
        return None
    return choose_user_agent(mode)


def build_geo_overrides(
    mode: str,
    *,
    geo_lat: Optional[float],
    geo_lon: Optional[float],
    geo_accuracy: Optional[float],
    timezone_name: Optional[str],
    locale: Optional[str],
    geo_file: Optional[Path],
) -> Dict[str, Any]:
    """
    Возвращает payload_overrides для Octo create_profile().

    Важно: точные имена полей зависят от версии Octo.
    Поэтому мы:
    - даём "best-effort" популярные ключи,
    - и позволяем указать `--geo file` с полным JSON, который просто мержится в payload.
    """
    mode = (mode or "octo").lower()
    if mode == "file":
        if geo_file and geo_file.exists():
            obj = read_json_file(geo_file)
            return obj if isinstance(obj, dict) else {}
        return {}
    if mode != "inline":
        return {}

    overrides: Dict[str, Any] = {}

    # Best-effort варианты, которые часто встречаются в антидетект-профилях:
    # - timezone / locale / languages
    # - geolocation: manual coords
    geo_obj: Dict[str, Any] = {"mode": "manual"}
    if geo_lat is not None:
        geo_obj["latitude"] = float(geo_lat)
    if geo_lon is not None:
        geo_obj["longitude"] = float(geo_lon)
    if geo_accuracy is not None:
        geo_obj["accuracy"] = float(geo_accuracy)

    fp: Dict[str, Any] = {}
    if timezone_name:
        fp["timezone"] = timezone_name
    if locale:
        fp["locale"] = locale
        # часто формат: ["ru-RU","ru"]
        fp["languages"] = [locale, locale.split("-")[0]] if "-" in locale else [locale]

    # Кладём GEO сразу в несколько мест (Octo может ожидать одну из схем).
    fp["geolocation"] = geo_obj
    overrides["fingerprint"] = fp
    overrides["geolocation"] = geo_obj
    if timezone_name:
        overrides["timezone"] = timezone_name
    if locale:
        overrides["locale"] = locale

    return overrides


def build_mobile_overrides(os_name: str) -> Dict[str, Any]:
    """
    Минимальные overrides для принудительного "mobile" профиля.
    Octo может игнорировать неизвестные поля, но device_type обычно поддерживается.
    """
    if (os_name or "").lower() in ("android", "ios"):
        return {"fingerprint": {"device_type": "phone"}}
    return {}


def build_proxy_payload(
    proxy: Optional[Dict[str, Any]], proxy_uuid: Optional[str], use_object_format: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Формирует payload для прокси в формате Octo Browser Cloud API.
    
    Поддерживает два формата:
    1. Строковый формат (по умолчанию): "host:port:username:password"
    2. Объектный формат (use_object_format=True): {"host": "...", "port": ..., "username": "...", "password": "..."}
    
    Поддерживает:
    - proxy_uuid: UUID существующего прокси в Octo (возвращает объект {"uuid": "..."})
    - proxy: словарь с настройками прокси (host, port, username, password, type)
    - use_object_format: если True, возвращает объектный формат вместо строкового
    """
    log = logging.getLogger(__name__)
    
    if proxy_uuid:
        return {"proxy": {"uuid": proxy_uuid}}
    if proxy:
        host = proxy.get("host", "")
        port = proxy.get("port", 8080)
        username = proxy.get("username", "")
        password = proxy.get("password", "")
        proxy_type = proxy.get("type", "http").lower()  # http, socks4, socks5, https
        
        if use_object_format:
            # Объектный формат для Cloud API (ожидает login/password)
            proxy_payload: Dict[str, Any] = {
                "host": host,
                "port": int(port),
                "type": proxy_type,
            }
            if username:
                proxy_payload["login"] = username
            if password:
                proxy_payload["password"] = password
            log.debug("Built proxy payload (object format): host=%s, port=%s, login=%s, password=%s, type=%s",
                     host, port, username if username else "(empty)", "***" if password else "(empty)", proxy_type)
            return {"proxy": proxy_payload}
        else:
            # Строковый формат (по умолчанию): "host:port:username:password"
            if username and password:
                proxy_string = f"{host}:{port}:{username}:{password}"
            else:
                proxy_string = f"{host}:{port}"
            
            # Если указан протокол (не http), добавляем префикс
            if proxy_type and proxy_type != "http":
                proxy_string = f"{proxy_type}://{proxy_string}"
            
            log.debug("Built proxy payload (string format): %s (type=%s, username=%s, password=%s)",
                     proxy_string.replace(f":{password}", ":***") if password else proxy_string,
                     proxy_type, username if username else "(empty)", "***" if password else "(empty)")
            return {"proxy": proxy_string}
    return None


def investigate_one(
    url: str,
    octo: OctoClient,
    evidence_root: Path,
    ua_mode: str,
    ua_custom: Optional[List[str]],
    profile_overrides: Optional[Dict[str, Any]],
    geo_mode: str,
    geo_file: Optional[Path],
    geo_lat: Optional[float],
    geo_lon: Optional[float],
    geo_accuracy: Optional[float],
    timezone_name: Optional[str],
    locale: Optional[str],
    proxy: Optional[Dict[str, Any]],
    proxy_uuid: Optional[str],
    proxy_use_api: bool,
    os_name: str,
    os_version: Optional[str],
    keep_profile: bool,
    page_timeout_s: float,
    wait_timeout_s: float,
    connect_timeout_s: float,
    allow_port_scan: bool = False,
    debug_port_override: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Полный цикл на один URL:
    create profile -> start -> Playwright (CDP) -> navigate -> wait -> collect -> cleanup -> stop profile
    
    Возвращает словарь с результатами:
    {
        "original_url": str,
        "final_url": Optional[str],
        "page_title": Optional[str],
        "screenshot_path": Optional[Path],
        "status": str,  # "success", "error", "timeout"
        "error": Optional[str],
    }
    """
    log = logging.getLogger(__name__)
    out_dir = ensure_evidence_dir(evidence_root, url)

    uuid: Optional[str] = None
    started: Optional[StartedProfile] = None
    auto: Optional[OctoAutomator] = None
    
    # Результат по умолчанию
    result = {
        "original_url": url,
        "final_url": None,
        "page_title": None,
        "screenshot_path": None,
        "status": "error",
        "error": None,
    }

    # Мобильный профиль (Android по умолчанию):
    # - os="android" на стороне Octo
    # - UA либо random, либо отдаём на генерацию Octo
    user_agent = choose_user_agent_from(ua_mode, ua_custom)

    # GEO/таймзона/локаль: либо Octo генерит (пусто), либо inline/file.
    geo_overrides = build_geo_overrides(
        geo_mode,
        geo_lat=geo_lat,
        geo_lon=geo_lon,
        geo_accuracy=geo_accuracy,
        timezone_name=timezone_name,
        locale=locale,
        geo_file=geo_file,
    )

    # Прокси настройки
    # Cloud API требует либо UUID существующего прокси, либо объектный формат с полями proxy
    # Сначала пробуем создать прокси через API и использовать UUID
    current_proxy_uuid = proxy_uuid
    proxy_overrides: Optional[Dict[str, Any]] = None
    if proxy:
        if current_proxy_uuid:
            proxy_overrides = build_proxy_payload(None, current_proxy_uuid, use_object_format=False)
        elif proxy_use_api:
            try:
                current_proxy_uuid = octo.create_proxy(
                    host=proxy.get("host", ""),
                    port=proxy.get("port", 8080),
                    username=proxy.get("username"),
                    password=proxy.get("password"),
                    proxy_type=proxy.get("type", "http"),
                )
                log.debug("Created proxy via API, uuid=%s", current_proxy_uuid)
                proxy_overrides = build_proxy_payload(None, current_proxy_uuid, use_object_format=False)
            except Exception as e:
                log.warning("Failed to create proxy via API: %s, using object format directly", e)
                proxy_overrides = build_proxy_payload(proxy, None, use_object_format=True)
        else:
            # Прямое указание прокси без создания через API (избегаем rate limit).
            proxy_overrides = build_proxy_payload(proxy, None, use_object_format=True)
    
    # Логируем proxy_overrides для отладки
    if proxy_overrides:
        log.debug("Proxy overrides before merge: %s", json.dumps(proxy_overrides, indent=2, ensure_ascii=False).replace('"password": "', '"password": "***'))

    merged_overrides: Optional[Dict[str, Any]] = None
    if profile_overrides or geo_overrides or proxy_overrides:
        merged_overrides = {}
        if profile_overrides:
            merged_overrides = deep_merge(merged_overrides, profile_overrides)
        if geo_overrides:
            merged_overrides = deep_merge(merged_overrides, geo_overrides)
        if proxy_overrides:
            merged_overrides = deep_merge(merged_overrides, proxy_overrides)
            # Логируем после merge для проверки
            log.debug("Merged overrides after proxy merge: %s", json.dumps(merged_overrides, indent=2, ensure_ascii=False).replace('"password": "', '"password": "***'))

    try:
        # Создаем профиль через Cloud API
        uuid = octo.create_profile(
            title=f"Scanner_Mobile_{utc_timestamp_compact()}",
            os_name=os_name,
            os_version=os_version,
            user_agent=user_agent,
            tags=["OctoScanner", "Mobile"],
            payload_overrides=merged_overrides,
        )
        
        # Запускаем профиль через Local API
        # Local API видит профили, созданные через Cloud API после синхронизации
        # Пробуем с повторными попытками и увеличиваем задержку
        import time
        log.info("⏳ Ожидание синхронизации профиля между Cloud и Local API (3 секунды)...")
        time.sleep(3)
        
        def _do_start():
            return octo.start_profile(
                uuid,
                headless=False,
                flags=["--disable-backgrounding-occluded-windows"],
                start_pages=[url],
                allow_port_scan=allow_port_scan,
                debug_port_override=debug_port_override,
            )

        log.info("🚀 Запуск профиля через Local API с URL: %s", url)
        started = None
        start_error = None
        
        # Retry-логика с обработкой "Zombie profile" ситуации
        max_start_retries = 3
        zombie_wait_s = 12.0  # Ожидание при "zombie profile" (запущен, но без debug_port)
        
        for start_attempt in range(max_start_retries):
            try:
                started = _do_start()
                log.info("✅ Профиль запущен, debug_port=%s (Playwright CDP)", started.debug_port)
                start_error = None
                break  # Успешно запущен, выходим из цикла
            except OctoAPIError as e:
                start_error = e
                err_s = str(e).lower()
                
                # Проверяем, является ли это "Zombie profile" ситуацией
                is_zombie = (
                    "already running" in err_s and 
                    ("debug_port" in err_s or "ws_endpoint" in err_s or "not in get" in err_s)
                )
                
                if "already_started" in err_s or "already started" in err_s or "already running" in err_s:
                    if is_zombie and start_attempt < max_start_retries - 1:
                        # "Zombie profile": запущен, но без debug_port
                        log.warning(
                            "⚠️  Zombie profile обнаружен (запущен без debug_port) → "
                            "force_stop, ожидание %d с, повторная попытка %d/%d...",
                            zombie_wait_s, start_attempt + 2, max_start_retries
                        )
                        
                        # Пробуем force_stop (теперь возвращает bool)
                        force_stop_success = octo.force_stop_profile(uuid, max_retries=3, initial_wait_s=3.0)
                        if not force_stop_success:
                            log.warning("force_stop не удался, но продолжаем попытку...")
                        
                        # Даем Octo время на "отлипание" - увеличиваем ожидание при каждой попытке
                        wait_time = zombie_wait_s + (start_attempt * 3.0)  # 12s, 15s, 18s
                        log.info("⏳ Ожидание %d секунд для синхронизации состояния Octo...", wait_time)
                        time.sleep(wait_time)
                        
                        # Продолжаем цикл для повторной попытки
                        continue
                    elif start_attempt < max_start_retries - 1:
                        # Обычная ситуация "already running" (не zombie)
                        log.warning(
                            "Профиль уже запущен / already running → force_stop, пауза 5 с, "
                            "повторная попытка %d/%d...",
                            start_attempt + 2, max_start_retries
                        )
                        force_stop_success = octo.force_stop_profile(uuid, max_retries=2, initial_wait_s=2.0)
                        if not force_stop_success:
                            log.warning("force_stop не удался, но продолжаем попытку...")
                        time.sleep(5)
                        # Продолжаем цикл для повторной попытки
                        continue
                    else:
                        # Последняя попытка не удалась
                        log.error("Все попытки запуска профиля исчерпаны (%d/%d)", max_start_retries, max_start_retries)
                        break
                else:
                    # Другая ошибка (не "already running")
                    if start_attempt < max_start_retries - 1:
                        wait_time = 2.0 * (start_attempt + 1)  # 2s, 4s
                        log.warning("Ошибка запуска профиля: %s. Повтор через %d с (попытка %d/%d)...", 
                                   e, wait_time, start_attempt + 2, max_start_retries)
                        time.sleep(wait_time)
                        continue
                    else:
                        # Последняя попытка
                        break
            if started is None:
                log.warning("Failed to start profile via API: %s. Checking if profile is already running...", start_error)
                profile_status = octo.get_profile_status(uuid)
                log.debug("Profile status response: %s", profile_status)
            else:
                profile_status = None
            if started is None and profile_status:
                status_data = profile_status.get("data") if isinstance(profile_status.get("data"), dict) else profile_status
                log.debug("Profile status data: %s", status_data)
                
                # Проверяем статус профиля - если status=1, профиль запущен
                profile_status_value = status_data.get("status")
                if profile_status_value == 1 or profile_status_value == "running" or status_data.get("is_running"):
                    log.info("Profile is running (status=%s), but selenium_port not in status. Trying to get from running profiles list...", profile_status_value)
                    
                    # Пробуем получить список запущенных профилей через Local API
                    # Local API может иметь информацию о selenium_port для запущенных профилей
                    try:
                        running_resp = octo._request("GET", "/api/v2/automation/profiles", use_cloud_api=False)
                        log.debug("Running profiles response: %s", running_resp)
                        
                        # Ищем наш профиль в списке запущенных
                        if isinstance(running_resp, dict):
                            running_profiles = running_resp.get("data", []) or running_resp.get("profiles", []) or running_resp.get("list", [])
                            if isinstance(running_profiles, list):
                                for running_profile in running_profiles:
                                    if isinstance(running_profile, dict) and running_profile.get("uuid") == uuid:
                                        # Пробуем получить selenium_port из запущенного профиля
                                        running_data = running_profile.get("data") if isinstance(running_profile.get("data"), dict) else running_profile
                                        selenium_port = (
                                            running_data.get("selenium_port") or 
                                            running_data.get("port") or 
                                            running_data.get("debug_port") or
                                            running_data.get("ws", {}).get("selenium") if isinstance(running_data.get("ws"), dict) else None
                                        )
                                        
                                        if isinstance(selenium_port, str) and ":" in selenium_port:
                                            try:
                                                selenium_port = int(selenium_port.split(":")[-1])
                                            except (ValueError, IndexError):
                                                selenium_port = None
                                        
                                        if isinstance(selenium_port, int):
                                            log.info("Found selenium_port=%s from running profiles list", selenium_port)
                                            ws_endpoint = running_data.get("ws_endpoint") or running_data.get("webdriver")
                                            started = StartedProfile(uuid=uuid, debug_port=selenium_port, ws_endpoint=ws_endpoint)
                                            break
                    except Exception as list_error:
                        log.debug("Failed to get running profiles list: %s", list_error)
                
                # Если selenium_port все еще не найден, но профиль запущен (status=1)
                # Пробуем получить информацию о запущенных профилях через другой эндпоинт
                if started is None:
                    log.warning("Profile is running (status=1) but selenium_port not found in status or running profiles list.")
                    log.warning("Trying to get selenium port from active/running profiles endpoint...")
                    
                    # Пробуем получить список активных профилей через Local API
                    try:
                        active_resp = octo._request("GET", "/api/v2/automation/profiles/active", use_cloud_api=False)
                        log.debug("Active profiles response: %s", active_resp)
                        
                        if isinstance(active_resp, dict):
                            active_profiles = active_resp.get("data", []) or active_resp.get("profiles", []) or active_resp.get("list", [])
                            if isinstance(active_profiles, list):
                                for active_profile in active_profiles:
                                    if isinstance(active_profile, dict) and active_profile.get("uuid") == uuid:
                                        active_data = active_profile.get("data") if isinstance(active_profile.get("data"), dict) else active_profile
                                        selenium_port = (
                                            active_data.get("selenium_port") or 
                                            active_data.get("port") or 
                                            active_data.get("debug_port") or
                                            active_data.get("ws", {}).get("selenium") if isinstance(active_data.get("ws"), dict) else None
                                        )
                                        
                                        if isinstance(selenium_port, str) and ":" in selenium_port:
                                            try:
                                                selenium_port = int(selenium_port.split(":")[-1])
                                            except (ValueError, IndexError):
                                                selenium_port = None
                                        
                                        if isinstance(selenium_port, int):
                                            log.info("Found selenium_port=%s from active profiles", selenium_port)
                                            ws_endpoint = active_data.get("ws_endpoint") or active_data.get("webdriver")
                                            started = StartedProfile(uuid=uuid, debug_port=selenium_port, ws_endpoint=ws_endpoint)
                                            break
                    except Exception as active_error:
                        log.debug("Failed to get active profiles: %s", active_error)
                    
                    # Если все еще не найден selenium_port
                    if started is None:
                        log.warning("⚠️  Профиль запущен, но selenium_port не найден через API методы.")
                        log.warning("   Это может означать, что:")
                        log.warning("   1. Профиль запускается, но Selenium еще не инициализирован")
                        log.warning("   2. Профиль был запущен вручную в Octo Browser")
                        log.warning("   3. Требуется больше времени для синхронизации")
                        # Не выбрасываем исключение здесь - попробуем еще раз через get_profile_status
                        started = None
            elif started is None:
                log.error("Could not get profile status. Profile may not be running or API endpoint not available.")
                raise start_error
        
        if started is None:
            log.error("Failed to start profile or get debug_port/ws_endpoint. Cannot proceed with Playwright (CDP) connection.")
            if 'start_error' not in locals():
                start_error = OctoAPIError("Failed to start profile or get debug_port/ws_endpoint")
            raise start_error

        log.info("🔌 Подключение к профилю через Playwright (CDP)...")
        try:
            auto = OctoAutomator(started)
            auto.connect()
            log.info("✅ Подключено через Playwright (CDP)")
        except Exception as conn_error:
            log.error("❌ Не удалось подключиться по CDP: %s", conn_error)
            raise

        page_timeout_ms = int(page_timeout_s * 1000)
        wait_timeout_ms = int(wait_timeout_s * 1000)

        log.info("Открываю URL: %s", url)
        nav_ok = False
        nav_error: Optional[Exception] = None
        # Делаем 2 попытки с разными wait_until, чтобы не падать на тяжёлых страницах.
        for attempt, wait_until in enumerate(("domcontentloaded", "commit"), start=1):
            try:
                auto.goto(url, wait_until=wait_until, timeout_ms=page_timeout_ms)
                nav_ok = True
                break
            except Exception as e:
                nav_error = e
                log.warning(
                    "Попытка %d/%d открыть URL не удалась (wait_until=%s): %s",
                    attempt,
                    2,
                    wait_until,
                    e,
                )
                # Если это вторая попытка — просто продолжаем, соберём то что есть.
                continue

        try:
            auto.wait_for("body", state="visible", timeout_ms=wait_timeout_ms)
        except Exception as wait_err:
            log.warning("Таймаут/ошибка ожидания body (собираю то, что есть): %s", wait_err)

        final_url, title, screenshot_path = collect_evidence_playwright(auto, out_dir)
        log.info("Собрано: final_url=%s | title=%s | dir=%s", final_url, title, out_dir)
        
        # Обновляем результат
        result.update({
            "final_url": final_url,
            "page_title": title,
            "screenshot_path": screenshot_path,
            "status": "success" if nav_ok else "timeout",
            "error": None if nav_ok else (str(nav_error) if nav_error else "Navigation timeout"),
        })
        
        # Проверяем, был ли редирект (признак возможного клоакинга)
        if normalize_url_for_compare(final_url) != normalize_url_for_compare(url):
            log.info("⚠️  Обнаружен редирект: %s -> %s (возможен клоакинг)", url, final_url)
        else:
            log.info("✓ URL не изменился (редиректа нет)")

    except OctoAutomationError as e:
        err_msg = str(e)
        if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
            log.warning("Таймаут при обработке URL %s: %s", url, e)
            result.update({"status": "timeout", "error": err_msg})
        else:
            log.exception("Ошибка автоматизации (Playwright) при URL %s: %s", url, e)
            result.update({"status": "error", "error": err_msg})
    except (OctoAPIError, Exception) as e:
        log.exception("Ошибка при обработке URL %s: %s", url, e)
        result.update({"status": "error", "error": str(e)})

    finally:
        if auto is not None:
            try:
                auto.disconnect()
            except Exception:
                log.debug("auto.disconnect() завершился с ошибкой", exc_info=True)
        if uuid and uuid != "one-time":
            try:
                octo.stop_profile(uuid)
            except Exception:
                log.debug("stop_profile(%s) завершился с ошибкой", uuid, exc_info=True)
            if not keep_profile:
                try:
                    octo.delete_profiles([uuid])
                    log.info("Профиль удалён (UUID=%s)", uuid)
                except Exception:
                    log.debug("delete_profiles(%s) завершился с ошибкой", uuid, exc_info=True)
    
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OctoScanner — URL investigation loop via Octo Browser Local API")
    p.add_argument("--targets", default="targets.txt", help="Путь к targets.txt (по умолчанию: targets.txt)")
    p.add_argument("--evidence-dir", default="evidence", help="Корневая папка для улик (по умолчанию: evidence/)")
    p.add_argument("--log-dir", default="logs", help="Папка логов (по умолчанию: logs/)")
    p.add_argument("--api-base", default="http://127.0.0.1:58888", help="Octo Local API base URL")
    p.add_argument("--api-key", default="", help="Octo API ключ (X-Octo-Api-Token)")

    p.add_argument("--os", choices=["android", "ios", "win", "mac"], default="android", help="OS профиля: android / ios / win / mac")
    p.add_argument("--os-version", default="", help="Версия OS (для Android: 12/13/14, для iOS: 16/17, для win: 10/11)")

    p.add_argument(
        "--ua",
        choices=["random", "octo", "file", "custom"],
        default="octo",
        help="User-Agent: random / octo / file / custom",
    )
    p.add_argument("--ua-file", default="", help="Путь к файлу с UA (1 на строку). Используется при --ua file.")
    p.add_argument("--ua-value", default="", help="Явный User-Agent. Используется при --ua custom.")

    p.add_argument(
        "--profile-overrides",
        default="",
        help="Путь к JSON с произвольными полями профиля Octo (мерджится в payload create_profile).",
    )

    p.add_argument("--geo", choices=["octo", "inline", "file"], default="octo", help="GEO: octo / inline / file")
    p.add_argument("--geo-file", default="", help="JSON с GEO-настройками (мерджится в payload). Используется при --geo file.")
    p.add_argument("--geo-lat", type=float, default=None, help="Широта (для --geo inline)")
    p.add_argument("--geo-lon", type=float, default=None, help="Долгота (для --geo inline)")
    p.add_argument("--geo-accuracy", type=float, default=50.0, help="Точность в метрах (для --geo inline)")
    p.add_argument("--timezone", default="", help="Timezone, напр. Europe/Moscow (для --geo inline)")
    p.add_argument("--locale", default="", help="Locale, напр. ru-RU (для --geo inline)")

    p.add_argument("--proxy-uuid", default="", help="UUID существующего прокси в Octo")
    p.add_argument("--proxy-file", default="", help="Файл с прокси (JSON массив или текстовый формат: host:port:user:pass)")
    p.add_argument("--proxy-rotate", action="store_true", help="Ротировать прокси из --proxy-file для каждого URL")
    p.add_argument("--proxy-use-api", action="store_true", help="Создавать прокси через Cloud API (иначе используется прямое указание)")

    p.add_argument("--delete-profile", action="store_true", help="Удалять профили после завершения (по умолчанию удаляем)")
    p.add_argument("--keep-profile", action="store_true", help="НЕ удалять профили после завершения")
    p.add_argument("--force-mobile", action="store_true", help="Принудительно использовать мобильный профиль (UA + device_type)")
    p.add_argument("--workers", type=int, default=1, help="Количество параллельных профилей (по умолчанию: 1)")
    p.add_argument("--max-active-profiles", type=int, default=0, help="Максимум одновременно активных профилей (0 = равно --workers)")
    p.add_argument("--allow-port-scan", action="store_true", help="Если API не возвращает debug_port, сканировать порты 52xxx и 92xx для поиска CDP")
    p.add_argument("--debug-port", type=int, default=0, metavar="PORT", help="Использовать этот CDP-порт (из Octo UI), если API/скан не дали порт")

    p.add_argument("--api-timeout", type=float, default=30.0, help="Таймаут API (сек)")
    p.add_argument("--page-timeout", type=float, default=45.0, help="Таймаут загрузки страницы (сек)")
    p.add_argument("--wait-timeout", type=float, default=30.0, help="Таймаут явных ожиданий (сек)")
    p.add_argument("--connect-timeout", type=float, default=30.0, help="Таймаут подключения CDP/Playwright (сек)")

    p.add_argument("--log-level", default="INFO", help="Уровень логирования: DEBUG/INFO/WARNING/ERROR")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(Path(args.log_dir), level=args.log_level)

    log = logging.getLogger(__name__)
    targets_path = Path(args.targets)
    if not targets_path.exists():
        log.error("Файл целей не найден: %s", targets_path.resolve())
        return 2

    evidence_root = Path(args.evidence_dir)
    evidence_root.mkdir(parents=True, exist_ok=True)

    api_key = args.api_key.strip() or None
    octo = OctoClient(base_url=args.api_base, timeout_s=args.api_timeout, api_key=api_key)

    ua_custom: Optional[List[str]] = None
    if args.ua == "file" and args.ua_file:
        ua_path = Path(args.ua_file)
        if ua_path.exists():
            ua_custom = read_ua_file(ua_path)
    elif args.ua == "custom":
        ua_val = (args.ua_value or "").strip()
        if ua_val:
            ua_custom = [ua_val]

    profile_overrides: Optional[Dict[str, Any]] = None
    if args.profile_overrides:
        po_path = Path(args.profile_overrides)
        if po_path.exists():
            try:
                obj = read_json_file(po_path)
                if isinstance(obj, dict):
                    profile_overrides = obj
                else:
                    log.warning("--profile-overrides не dict JSON, игнорирую: %s", po_path)
            except Exception:
                log.exception("Не удалось прочитать --profile-overrides JSON, игнорирую: %s", po_path)

    if args.force_mobile:
        # Принудительно мобильный профиль через device_type, UA оставляем на Octo.
        mobile_overrides = build_mobile_overrides(args.os)
        if mobile_overrides:
            profile_overrides = deep_merge(profile_overrides or {}, mobile_overrides)

    geo_file: Optional[Path] = Path(args.geo_file) if args.geo_file else None
    timezone_name = args.timezone.strip() or None
    locale = args.locale.strip() or None

    # OS настройки
    os_name = args.os
    os_version: Optional[str] = args.os_version.strip() or None
    if not os_version:
        # Дефолтные версии по OS
        if os_name == "android":
            os_version = "13"
        elif os_name == "ios":
            os_version = "17"
        elif os_name == "win":
            os_version = "11"
        elif os_name == "mac":
            os_version = "14"

    # Прокси настройки
    proxy_uuid: Optional[str] = args.proxy_uuid.strip() or None
    proxy_list: List[Dict[str, Any]] = []
    if args.proxy_file:
        proxy_path = Path(args.proxy_file)
        if proxy_path.exists():
            try:
                proxy_list = read_proxy_file(proxy_path)
                log.info("Загружено прокси из файла: %d", len(proxy_list))
            except Exception:
                log.exception("Не удалось прочитать --proxy-file, игнорирую: %s", proxy_path)
        else:
            log.warning("Файл прокси не найден: %s", proxy_path)

    urls = list(iter_targets(targets_path))
    if not urls:
        log.warning("В %s нет URL для обработки.", targets_path.resolve())
        return 0

    delete_profiles = True if not args.keep_profile else args.delete_profile
    log.info("Старт. URL в очереди: %d | OS: %s %s | Delete profiles: %s", len(urls), os_name, os_version, delete_profiles)

    # Список результатов для CSV отчета
    results: List[Dict[str, Any]] = [None] * len(urls)  # type: ignore[list-item]

    def _resolve_proxy(idx: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        current_proxy: Optional[Dict[str, Any]] = None
        current_proxy_uuid: Optional[str] = proxy_uuid
        if proxy_list:
            if args.proxy_rotate:
                current_proxy = proxy_list[idx % len(proxy_list)]
            else:
                current_proxy = proxy_list[0]
        return current_proxy, current_proxy_uuid

    max_active = args.max_active_profiles if args.max_active_profiles and args.max_active_profiles > 0 else args.workers
    semaphore = threading.Semaphore(max_active)

    def _run_one(idx: int, url: str) -> Tuple[int, Dict[str, Any]]:
        log.info("=" * 80)
        log.info("🔍 Проверка URL [%d/%d]: %s", idx + 1, len(urls), url)
        log.info("=" * 80)

        current_proxy, current_proxy_uuid = _resolve_proxy(idx)
        # Создаём отдельный клиент на поток, чтобы избежать гонок в Session.
        octo_local = OctoClient(base_url=args.api_base, timeout_s=args.api_timeout, api_key=api_key)

        semaphore.acquire()
        try:
            result = investigate_one(
                url=url,
                octo=octo_local,
                evidence_root=evidence_root,
                ua_mode=args.ua,
                ua_custom=ua_custom,
                profile_overrides=profile_overrides,
                geo_mode=args.geo,
                geo_file=geo_file,
                geo_lat=args.geo_lat,
                geo_lon=args.geo_lon,
                geo_accuracy=args.geo_accuracy,
                timezone_name=timezone_name,
                locale=locale,
                proxy=current_proxy,
                proxy_uuid=current_proxy_uuid,
                proxy_use_api=args.proxy_use_api,
                os_name=os_name,
                os_version=os_version,
                keep_profile=not delete_profiles,
                page_timeout_s=args.page_timeout,
                wait_timeout_s=args.wait_timeout,
                connect_timeout_s=args.connect_timeout,
                allow_port_scan=args.allow_port_scan,
                debug_port_override=args.debug_port or None,
            )
        finally:
            semaphore.release()
        return idx, result

    if args.workers <= 1:
        for idx, url in enumerate(urls):
            _, result = _run_one(idx, url)
            results[idx] = result
            if result["status"] == "success":
                if normalize_url_for_compare(result["final_url"]) != normalize_url_for_compare(url):
                    log.info("✅ Успешно. Обнаружен редирект (возможен клоакинг): %s -> %s", url, result["final_url"])
                else:
                    log.info("✅ Успешно. URL не изменился (редиректа нет)")
            elif result["status"] == "timeout":
                log.warning("⏱️  Таймаут при проверке URL: %s", url)
            else:
                log.error("❌ Ошибка при проверке URL: %s - %s", url, result.get("error", "Unknown error"))
    else:
        log.info("Запуск в %d поток(а/ов)", args.workers)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(_run_one, idx, url) for idx, url in enumerate(urls)]
            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
                url = urls[idx]
                if result["status"] == "success":
                    if normalize_url_for_compare(result["final_url"]) != normalize_url_for_compare(url):
                        log.info("✅ Успешно. Обнаружен редирект (возможен клоакинг): %s -> %s", url, result["final_url"])
                    else:
                        log.info("✅ Успешно. URL не изменился (редиректа нет)")
                elif result["status"] == "timeout":
                    log.warning("⏱️  Таймаут при проверке URL: %s", url)
                else:
                    log.error("❌ Ошибка при проверке URL: %s - %s", url, result.get("error", "Unknown error"))

    # Сохраняем результаты в CSV
    csv_path = evidence_root / "report.csv"
    save_results_to_csv(results, csv_path)
    
    # Статистика
    success_count = sum(1 for r in results if r["status"] == "success")
    error_count = sum(1 for r in results if r["status"] == "error")
    timeout_count = sum(1 for r in results if r["status"] == "timeout")
    redirect_count = sum(
        1
        for r in results
        if r["status"] == "success"
        and normalize_url_for_compare(r.get("final_url")) != normalize_url_for_compare(r.get("original_url"))
    )
    
    log.info("=" * 80)
    log.info("📊 ИТОГОВАЯ СТАТИСТИКА:")
    log.info("   Всего URL: %d", len(results))
    log.info("   ✅ Успешно: %d", success_count)
    log.info("   ⏱️  Таймаут: %d", timeout_count)
    log.info("   ❌ Ошибки: %d", error_count)
    log.info("   🔄 Редиректы (возможен клоакинг): %d", redirect_count)
    log.info("   📄 CSV отчет: %s", csv_path)
    log.info("=" * 80)
    log.info("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
