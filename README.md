# Cloack-Scanner — Массовая проверка клоакинговых ссылок

Инструмент для массовой проверки клоакинговых ссылок через мобильные профили Octo Browser с поддержкой мобильных прокси.

## Особенности

- ✅ **Мобильные профили** (Android/iOS) по умолчанию
- ✅ **Мобильные прокси** с ротацией
- ✅ **Сохранение профилей** для повторного использования (опционально)
- ✅ **Гибкая настройка** UA, GEO, прокси через CLI или файлы
- ✅ **Устойчивость к ошибкам** — один плохой URL не останавливает весь процесс

## Установка

```powershell
cd D:\BS\AGNumbers\OctoScanner
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Быстрый старт

### Базовый запуск (Android профили, без прокси, Octo генерит UA/GEO)

```powershell
python scanner.py --targets targets.txt
```

### С мобильными прокси (ротация)

```powershell
python scanner.py --targets targets.txt --proxy-file proxies.txt --proxy-rotate
```

### С удалением профилей (по умолчанию профили сохраняются)

```powershell
python scanner.py --targets targets.txt --delete-profile
```

### С кастомными UA и GEO

```powershell
python scanner.py --targets targets.txt --ua random --geo inline --geo-lat 55.7558 --geo-lon 37.6173 --timezone Europe/Moscow --locale ru-RU
```

## Параметры запуска

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

### GEO настройки

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

### Таймауты

- `--api-timeout` — таймаут API запросов (сек, по умолчанию: 30)
- `--page-timeout` — таймаут загрузки страницы (сек, по умолчанию: 45)
- `--wait-timeout` — таймаут явных ожиданий (сек, по умолчанию: 30)
- `--connect-timeout` — таймаут подключения Selenium (сек, по умолчанию: 30)

## Примеры файлов

### targets.txt

```
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

Результаты сохраняются в `evidence/{timestamp}_{domain}/`:
- `screenshot.png` — скриншот страницы
- `page.html` — исходный код страницы
- `metadata.json` — метаданные (final_url, title, timestamp)

Логи сохраняются в `logs/octoscanner_{timestamp}.log`

## Важные замечания

1. **Octo Browser должен быть запущен** и Local API доступен на `http://127.0.0.1:58888`
2. **По умолчанию профили сохраняются** для повторного использования — используйте `--delete-profile` для удаления после завершения
3. **Мобильные прокси** рекомендуется использовать с `--os android` или `--os ios`
4. **Ротация прокси** (`--proxy-rotate`) переключает прокси для каждого URL из файла
5. **Массовая проверка клоакинга** — профили создаются для каждого URL, что позволяет эффективно проверять множество ссылок
