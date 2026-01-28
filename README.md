# Cloack-Scanner — Массовая проверка клоакинговых ссылок

Инструмент создаёт (или использует существующие) профили в Octo Browser, подключается к ним через **Playwright / CDP**, открывает каждый URL, сохраняет артефакты (скриншот, HTML, метаданные) и формирует CSV‑отчёт для последующего анализа клоакинга.

---

## Особенности

- ✅ **Мобильные профили** (Android/iOS) по умолчанию
- ✅ **Мобильные прокси** с ротацией
- ✅ **Сохранение профилей** для повторного использования (опционально)
- ✅ **Гибкая настройка** UA, GEO, прокси через CLI или файлы
- ✅ **Устойчивость к ошибкам** — один плохой URL не останавливает весь процесс
- ✅ **CSV‑отчёт** с результатами проверки (`evidence/report.csv`)
- ✅ **Улучшенное логирование** с прогрессом и статистикой (`logs/`)
- ✅ **Объектно‑ориентированный API** (класс `OctoCloakChecker`)
- ✅ **Автоматическое обнаружение редиректов** (признак клоакинга)

---

## Требования

- **Python** 3.11+ (рекомендуется 3.11/3.12)
- Установленный и запущенный **Octo Browser** с включённым Local API
  - Local API: `http://127.0.0.1:58888` (порт можно изменить в настройках Octo)
  - Включён API‑токен (Cloud API)
- Доступ в интернет для Cloud API Octo (создание/удаление профилей, прокси)

Где взять **API‑ключ Octo**:

- В Octo Browser: `Settings → Additional → API Token`
- Скопируйте токен и используйте его в параметре `--api-key` или через переменную окружения/настройку в ваших скриптах.

**Важно:** не коммитьте реальный API‑ключ в GitHub‑репозиторий. Используйте переменные окружения или файлы `.env`, которые добавлены в `.gitignore`.

---

## Установка (локальная среда)

Пример для Windows / PowerShell:

```powershell
cd path\to\OctoScanner
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Сканер использует **Playwright** (CDP) для подключения к профилям Octo, не Selenium.

---

## Быстрый старт: сканирование списка ссылок

1. **Подготовьте Octo Browser**
   - Запустите Octo Browser.
   - Включите Local API и убедитесь, что порт совпадает с `--api-base` (по умолчанию `http://127.0.0.1:58888`).
   - Включите и скопируйте API‑токен (Cloud API).

2. **Создайте файл целей `targets.txt`** (по одному URL в строке, см. пример ниже).

3. **(Опционально) Настройте прокси и GEO** через файлы `proxies.txt`, `proxies.json`, `profile_overrides.example.json`.

4. **Проверьте соединение с Octo** (рекомендуется при первом запуске):

```powershell
python diagnose.py --api-key YOUR_OCTO_API_KEY
```

5. **Запустите основной сканер**.

### Базовый запуск (Android‑профили, без прокси, Octo генерирует UA/GEO)

```powershell
python scanner.py --targets targets.txt --api-key YOUR_OCTO_API_KEY
```

### С мобильными прокси (ротация из файла)

```powershell
python scanner.py --targets targets.txt --api-key YOUR_OCTO_API_KEY `
  --proxy-file proxies.txt --proxy-rotate
```

### С удалением профилей после проверки

По умолчанию профили **сохраняются** для повторного использования. Чтобы очищать их после проверки:

```powershell
python scanner.py --targets targets.txt --api-key YOUR_OCTO_API_KEY --delete-profile
```

### С кастомными UA и GEO

```powershell
python scanner.py --targets targets.txt --api-key YOUR_OCTO_API_KEY `
  --ua random `
  --geo inline --geo-lat 55.7558 --geo-lon 37.6173 --timezone Europe/Moscow --locale ru-RU
```

---

## Параметры запуска `scanner.py`

### OS профиля

- `--os {android|ios|win|mac}` — выбор OS (по умолчанию: `android`)
- `--os-version` — версия OS (по умолчанию: Android 13, iOS 17, Windows 11, macOS 14)

### User-Agent

- `--ua {random|octo|file|custom}` — режим UA (по умолчанию: `octo` — Octo генерит)
  - `random` — случайный из встроенного списка мобильных UA
  - `octo` — Octo генерирует UA автоматически
  - `file` — случайный из файла (`--ua-file`)
  - `custom` — явный UA (`--ua-value`)
- `--ua-file` — путь к файлу с UA (1 на строку)
- `--ua-value` — явный User-Agent строка

### Прокси

- `--proxy-uuid` — UUID существующего прокси в Octo
- `--proxy-file` — файл с прокси (JSON массив или текстовый: `host:port:user:pass`)
- `--proxy-rotate` — ротировать прокси из файла для каждого URL

### GEO‑настройки

- `--geo {octo|inline|file}` — режим GEO (по умолчанию: `octo`)
  - `octo` — Octo генерирует GEO автоматически
  - `inline` — задаёте через CLI параметры
  - `file` — JSON файл с настройками (`--geo-file`)
- `--geo-lat`, `--geo-lon` — координаты (для `--geo inline`)
- `--geo-accuracy` — точность в метрах (по умолчанию: 50)
- `--timezone` — таймзона, напр. `Europe/Moscow`
- `--locale` — локаль, напр. `ru-RU`

### Профили

- `--delete-profile` — удалять профили после завершения (по умолчанию профили сохраняются для повторного использования)
- `--profile-overrides` — путь к JSON с произвольными настройками профиля
- `--allow-port-scan` — если API не возвращает debug_port, сканировать порты 52xxx и 92xx для поиска CDP
- `--debug-port PORT` — использовать этот CDP‑порт (из Octo UI), если API/скан не дали порт

### Таймауты

- `--api-timeout` — таймаут API запросов (сек, по умолчанию: 30)
- `--page-timeout` — таймаут загрузки страницы (сек, по умолчанию: 45)
- `--wait-timeout` — таймаут явных ожиданий (сек, по умолчанию: 30)
- `--connect-timeout` — таймаут подключения CDP/Playwright (сек, по умолчанию: 30)

---

## Структура проекта

- `scanner.py` — основной CLI‑скрипт для массовой проверки URL; внутри использует класс `OctoCloakChecker`.
- `octo_client.py` — обёртка над Octo Local/Cloud API и Playwright (классы `OctoClient`, `OctoAutomator`).
- `diagnose.py` — диагностический скрипт: проверяет доступность Local API, Cloud API и CDP‑портов (рекомендуется запускать при проблемах).
- `example_navigate.py` — минимальный пример: создать профиль → стартовать → открыть страницу → вывести заголовок → остановить.
- `stop_all_profiles.py` — утилита для остановки всех запущенных профилей Octo Browser (полезно при отладке «залипших» профилей).
- `targets.txt` — список URL для проверки (1 URL на строку).
- `proxies.txt` / `proxies.json` — настройки прокси (текстовый или JSON‑формат).
- `profile_overrides.example.json` — пример JSON c переопределением fingerprint/GEO/таймзоны для профиля.
- `ua_custom.example.txt` — пример файла с кастомными User‑Agent строками.
- `evidence/` — папка для артефактов сканирования (создаётся автоматически, в Git обычно игнорируется).
- `logs/` — логи работы сканера (также целесообразно игнорировать в Git).

---

## Устранение неполадок: «профиль стартует, CDP нет»

**Почему так:** Локальный HTTP‑сервер **58888** — это **Local API** (старт/стоп). Playwright подключается по **CDP** — отдельный порт у каждого браузера. Если API не отдаёт `debug_port` / `ws_endpoint`, сканер не подключается и не открывает URL.

**Как сейчас работает старт:**

- Используется **только** `POST http://localhost:58888/api/profiles/start` (официальный Local API, как в [блоге Octo](https://blog.octobrowser.net/automating-octo-browser-using-api) и octo-mcp).
- Тело: `uuid`, `headless`, `debug_port`, `timeout`, `only_local`, `flags`. Стартовые URL **не** передаются в start — страница открывается через `goto` после подключения по CDP.

**Если всё ещё не работает:**

1. **Явный порт** — закройте все профили, затем:
   ```powershell
   python scanner.py --targets targets.txt --api-key YOUR_OCTO_API_KEY --debug-port 9222
   ```
2. **Лог** — в `logs/octoscanner_*.log` найдите строку `API вернул success, data=None. Ответ: {...}` и проверьте её содержимое.
3. **Octo** — `Settings → Additional → API / Automation`; обновите Octo Browser до актуальной версии.
4. С `--debug-port` скан портов **не выполняется**.

---

## Примеры файлов конфигурации

### targets.txt

Создайте файл `targets.txt` на основе примера `targets.example.txt`:

```text
https://example.com/page1
https://example.com/page2
example.com/page3
```

### proxies.txt (текстовый формат)

```
proxy1.example.com:8080:user1:pass1
proxy2.example.com:8080:user2:pass2
192.168.1.100:3128
```

### proxies.json (JSON формат)

```json
[
  {
    "host": "proxy1.example.com",
    "port": 8080,
    "username": "user1",
    "password": "pass1",
    "type": "http"
  }
]
```

## Результаты

### Структура файлов

Результаты сохраняются в `evidence/{timestamp}_{domain}/`:
- `screenshot.png` — скриншот страницы (full-page или viewport)
- `page.html` — исходный код страницы (рендеренный DOM)
- `metadata.json` — метаданные (final_url, title, timestamp)

### CSV отчет

После завершения проверки создается файл `evidence/report.csv` со следующими колонками:
- **Original_URL** — исходный URL для проверки
- **Final_URL** — финальный URL после всех редиректов
- **Page_Title** — заголовок страницы
- **Screenshot_Path** — путь к скриншоту (относительно корня проекта)
- **Status** — статус проверки (`success`, `error`, `timeout`)
- **Error** — описание ошибки (если есть)

**Пример использования CSV отчета:**
```python
import pandas as pd

# Читаем результаты
df = pd.read_csv('evidence/report.csv')

# Фильтруем URL с редиректами (возможный клоакинг)
cloaked = df[df['Original_URL'] != df['Final_URL']]
print(f"Найдено {len(cloaked)} URL с редиректами (возможен клоакинг)")
```

### Логи

Логи сохраняются в `logs/octoscanner_{timestamp}.log` с подробной информацией о:
- Прогрессе проверки (URL 1/10, 2/10, ...)
- Обнаруженных редиректах
- Ошибках и таймаутах
- Итоговой статистике

### Итоговая статистика

После завершения проверки выводится статистика:
- Всего URL проверено
- Успешно проверено
- Таймауты
- Ошибки
- Обнаружено редиректов (возможен клоакинг)

## Использование как библиотека

Вы можете использовать `OctoCloakChecker` как библиотеку в своем коде:

```python
from scanner import OctoCloakChecker
from octo_client import OctoClient
from pathlib import Path

# Создаем клиент Octo Browser
octo = OctoClient(
    base_url="http://127.0.0.1:58888",
    api_key="your-api-key"
)

# Создаем checker
checker = OctoCloakChecker(
    octo_client=octo,
    evidence_root=Path("evidence"),
    os_name="android",
    os_version="13",
)

# Проверяем один URL
result = checker.check_url("https://example.com")
print(f"Status: {result['status']}")
print(f"Final URL: {result['final_url']}")

# Проверяем список URL
urls = ["https://example.com", "https://test.com"]
results = checker.check_urls(
    urls=urls,
    proxy_rotate=True,
    csv_report_path=Path("my_report.csv")
)
```

## Важные замечания

1. **Octo Browser должен быть запущен** и Local API доступен на `http://127.0.0.1:58888`
2. **По умолчанию профили сохраняются** для повторного использования — используйте `--delete-profile` для удаления после завершения
3. **Мобильные прокси** рекомендуется использовать с `--os android` или `--os ios`
4. **Ротация прокси** (`--proxy-rotate`) переключает прокси для каждого URL из файла
5. **Массовая проверка клоакинга** — профили создаются для каждого URL, что позволяет эффективно проверять множество ссылок
6. **CSV отчет** автоматически создается в `evidence/report.csv` после завершения проверки
7. **Обнаружение клоакинга** — если `Original_URL != Final_URL`, это может указывать на клоакинг (показ разного контента для мобильных и десктопных устройств)
