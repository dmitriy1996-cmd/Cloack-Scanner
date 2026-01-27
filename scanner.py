"""
OctoScanner — массовая проверка клоакинговых ссылок через мобильные профили Octo Browser.

Идея:
Для КАЖДОГО URL создаём мобильный профиль Octo Browser -> запускаем с мобильным прокси ->
подключаемся Selenium Remote WebDriver -> переходим на URL -> собираем артефакты ->
останавливаем профиль (удаление опционально, по умолчанию профили сохраняются для повторного использования).

Заметки по безопасности и надёжности:
- Скрипт не должен "падать" на одном плохом URL: ошибки логируем и продолжаем.
- Очистка в `finally`: даже если навигация/selenium упали — профиль всё равно пытаемся остановить.
- По умолчанию включены таймауты (API/страница/ожидания).
- По умолчанию используются мобильные профили (Android) с поддержкой мобильных прокси.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from octo_client import OctoClient, OctoAPIError, StartedProfile


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


def ensure_evidence_dir(root: Path, url: str) -> Path:
    ts = utc_timestamp_compact()
    domain = safe_domain_for_folder(url)
    out_dir = root / f"{ts}_{domain}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def build_chrome_options(user_agent: Optional[str]) -> ChromeOptions:
    """
    ChromeOptions для Remote WebDriver.
    Важно:
    - С Octo профиль уже должен иметь "анти-детект" настройки.
    - Эти аргументы — best-effort и могут игнорироваться удалённой стороной.
    """
    options = ChromeOptions()

    # Минимизируем типичные "automation" сигналы (не панацея).
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")

    # UA можно задать здесь, но предпочтительнее в профиле Octo.
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    return options


def connect_remote_driver(selenium_port: int, user_agent: Optional[str], connect_timeout_s: float) -> WebDriver:
    """
    Подключаемся к Selenium, который поднял Octo.

    Некоторые Selenium-обвязки слушают на /wd/hub, некоторые — на корне.
    Пробуем оба варианта.
    """
    endpoints = [
        f"http://127.0.0.1:{selenium_port}/wd/hub",
        f"http://127.0.0.1:{selenium_port}",
    ]

    last_err: Optional[BaseException] = None
    options = build_chrome_options(user_agent)

    for endpoint in endpoints:
        try:
            driver = webdriver.Remote(
                command_executor=endpoint,
                options=options,
            )
            # На уровне драйвера тоже задаём лимиты, чтобы не зависать бесконечно.
            driver.set_page_load_timeout(connect_timeout_s)
            driver.set_script_timeout(connect_timeout_s)
            logging.getLogger(__name__).debug("Remote WebDriver connected via %s", endpoint)
            return driver
        except Exception as e:
            last_err = e

    raise WebDriverException(f"Не удалось подключиться к Selenium на порту {selenium_port}: {last_err}")


def wait_for_page_ready(driver: WebDriver, timeout_s: float) -> None:
    """
    Явные ожидания:
    - DOM готов (document.readyState == 'complete')
    - есть <body>
    """
    wait = WebDriverWait(driver, timeout_s)

    # 1) Ждём "complete" (часть сайтов может держать pending из-за long-polling,
    # поэтому в проде иногда используют 'interactive' + отдельные условия).
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

    # 2) Минимально убедимся, что тело страницы присутствует.
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def try_fullpage_screenshot(driver: WebDriver, out_path: Path) -> bool:
    """
    Пытаемся сделать full-page скриншот через CDP (Chrome DevTools Protocol).
    Если недоступно (часто бывает на Remote) — вернём False, чтобы сделать обычный скриншот.
    """
    try:
        # Selenium 4: execute_cdp_cmd доступен для chromium драйверов.
        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        width = int(metrics["contentSize"]["width"])
        height = int(metrics["contentSize"]["height"])
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "mobile": False,
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
        })
        screenshot = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        import base64  # локально, чтобы не грузить namespace

        out_path.write_bytes(base64.b64decode(screenshot["data"]))
        return True
    except Exception:
        return False


def collect_evidence(driver: WebDriver, out_dir: Path) -> Tuple[str, str]:
    """
    Собираем "улики":
    - screenshot.png (full-page best-effort)
    - page.html (рендеренный DOM)
    - metadata.json (final_url, title, timestamps)
    Возвращаем (final_url, title).
    """
    final_url = driver.current_url
    title = driver.title or ""

    screenshot_path = out_dir / "screenshot.png"
    if not try_fullpage_screenshot(driver, screenshot_path):
        driver.save_screenshot(str(screenshot_path))

    html_path = out_dir / "page.html"
    html_path.write_text(driver.page_source or "", encoding="utf-8", errors="ignore")

    meta_path = out_dir / "metadata.json"
    meta = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "final_url": final_url,
        "title": title,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return final_url, title


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


def build_proxy_payload(
    proxy: Optional[Dict[str, Any]], proxy_uuid: Optional[str]
) -> Optional[Dict[str, Any]]:
    """
    Формирует payload для прокси в формате Octo.
    Поддерживает:
    - proxy_uuid: UUID существующего прокси в Octo
    - proxy: словарь с настройками прокси (host, port, username, password, type)
    """
    if proxy_uuid:
        return {"proxy": {"uuid": proxy_uuid}}
    if proxy:
        # Octo обычно ожидает структуру:
        # {"proxy": {"host": "...", "port": 8080, "username": "...", "password": "...", "type": "http"}}
        proxy_payload: Dict[str, Any] = {
            "host": proxy.get("host", ""),
            "port": proxy.get("port", 8080),
        }
        if proxy.get("username"):
            proxy_payload["username"] = proxy["username"]
        if proxy.get("password"):
            proxy_payload["password"] = proxy["password"]
        proxy_payload["type"] = proxy.get("type", "http")  # http, socks4, socks5
        return {"proxy": proxy_payload}
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
    os_name: str,
    os_version: Optional[str],
    keep_profile: bool,
    page_timeout_s: float,
    wait_timeout_s: float,
    connect_timeout_s: float,
) -> None:
    """
    Полный цикл на один URL:
    create profile -> start -> selenium -> navigate -> wait -> collect -> cleanup -> (delete profile если не keep_profile)
    """
    log = logging.getLogger(__name__)
    out_dir = ensure_evidence_dir(evidence_root, url)

    uuid: Optional[str] = None
    started: Optional[StartedProfile] = None
    driver: Optional[WebDriver] = None

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
    proxy_overrides = build_proxy_payload(proxy, proxy_uuid)

    merged_overrides: Optional[Dict[str, Any]] = None
    if profile_overrides or geo_overrides or proxy_overrides:
        merged_overrides = {}
        if profile_overrides:
            merged_overrides = deep_merge(merged_overrides, profile_overrides)
        if geo_overrides:
            merged_overrides = deep_merge(merged_overrides, geo_overrides)
        if proxy_overrides:
            merged_overrides = deep_merge(merged_overrides, proxy_overrides)

    try:
        uuid = octo.create_profile(
            title=f"Scanner_Mobile_{utc_timestamp_compact()}",
            os_name=os_name,
            os_version=os_version,
            user_agent=user_agent,
            tags=["OctoScanner", "Mobile"],
            payload_overrides=merged_overrides,
        )
        started = octo.start_profile(
            uuid,
            headless=False,
            flags=[
                "--disable-backgrounding-occluded-windows",
            ],
        )

        # Подключаемся к Selenium.
        driver = connect_remote_driver(started.selenium_port, user_agent=user_agent, connect_timeout_s=connect_timeout_s)

        # Настроим ограничения ожидания загрузки страницы.
        driver.set_page_load_timeout(page_timeout_s)
        driver.set_script_timeout(page_timeout_s)

        log.info("Открываю URL: %s", url)
        driver.get(url)

        try:
            wait_for_page_ready(driver, timeout_s=wait_timeout_s)
        except TimeoutException:
            # На "тяжёлых" страницах readyState может не стать complete.
            # Это не критично: всё равно попробуем собрать артефакты из того, что успело загрузиться.
            log.warning("Таймаут ожидания готовности страницы (буду собирать то, что есть): %s", url)

        final_url, title = collect_evidence(driver, out_dir)
        log.info("Собрано: final_url=%s | title=%s | dir=%s", final_url, title, out_dir)

    except (OctoAPIError, WebDriverException, Exception) as e:
        # Любая ошибка на текущем URL не должна останавливать весь прогон.
        log.exception("Ошибка при обработке URL %s: %s", url, e)

    finally:
        # 1) Закрываем Selenium.
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                log.debug("driver.quit() завершился с ошибкой", exc_info=True)

        # 2) Останавливаем профиль (если был создан/запущен).
        if uuid is not None:
            try:
                octo.stop_profile(uuid)
            except Exception:
                log.debug("stop_profile(%s) завершился с ошибкой", uuid, exc_info=True)

            # 3) Удаляем профиль только если keep_profile=False (по умолчанию сохраняем для повторного использования).
            if keep_profile:
                log.info("Профиль сохранён для повторного использования: uuid=%s", uuid)
            else:
                try:
                    octo.delete_profiles([uuid])
                    log.debug("Удалён профиль uuid=%s", uuid)
                except Exception:
                    log.debug("delete_profiles([%s]) завершился с ошибкой", uuid, exc_info=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OctoScanner — URL investigation loop via Octo Browser Local API")
    p.add_argument("--targets", default="targets.txt", help="Путь к targets.txt (по умолчанию: targets.txt)")
    p.add_argument("--evidence-dir", default="evidence", help="Корневая папка для улик (по умолчанию: evidence/)")
    p.add_argument("--log-dir", default="logs", help="Папка логов (по умолчанию: logs/)")
    p.add_argument("--api-base", default="http://127.0.0.1:58888", help="Octo Local API base URL")

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

    p.add_argument("--delete-profile", action="store_true", help="Удалять профили после завершения (по умолчанию профили сохраняются)")

    p.add_argument("--api-timeout", type=float, default=30.0, help="Таймаут API (сек)")
    p.add_argument("--page-timeout", type=float, default=45.0, help="Таймаут загрузки страницы (сек)")
    p.add_argument("--wait-timeout", type=float, default=30.0, help="Таймаут явных ожиданий (сек)")
    p.add_argument("--connect-timeout", type=float, default=30.0, help="Таймаут подключения Selenium/скриптов (сек)")

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

    octo = OctoClient(base_url=args.api_base, timeout_s=args.api_timeout)

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

    log.info("Старт. URL в очереди: %d | OS: %s %s | Delete profiles: %s", len(urls), os_name, os_version, args.delete_profile)

    # Индекс для ротации прокси
    proxy_idx = 0

    for idx, url in enumerate(urls, start=1):
        log.info("---- [%d/%d] %s ----", idx, len(urls), url)

        # Выбираем прокси для текущего URL
        current_proxy: Optional[Dict[str, Any]] = None
        current_proxy_uuid: Optional[str] = proxy_uuid

        if proxy_list:
            if args.proxy_rotate:
                current_proxy = proxy_list[proxy_idx % len(proxy_list)]
                proxy_idx += 1
                log.debug("Используется прокси [%d]: %s:%s", proxy_idx - 1, current_proxy.get("host"), current_proxy.get("port"))
            else:
                # Используем первый прокси для всех URL
                current_proxy = proxy_list[0]

        investigate_one(
            url=url,
            octo=octo,
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
            os_name=os_name,
            os_version=os_version,
            keep_profile=not args.delete_profile,  # По умолчанию сохраняем (keep_profile=True)
            page_timeout_s=args.page_timeout,
            wait_timeout_s=args.wait_timeout,
            connect_timeout_s=args.connect_timeout,
        )

    log.info("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
