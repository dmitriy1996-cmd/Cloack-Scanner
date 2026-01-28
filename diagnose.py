#!/usr/bin/env python3
"""
Диагностический скрипт для проверки Octo Browser API.
Проверяет доступность Local API, Cloud API, и наличие запущенных профилей.
"""

import os
import requests
import sys
from typing import Dict, Any

# API-ключ НЕ должен быть захардкожен в репозитории.
# Используйте переменную окружения OCTO_API_KEY или передавайте ключ через аргументы командной строки.
API_KEY = os.getenv("OCTO_API_KEY", "YOUR_OCTO_API_KEY_HERE")
LOCAL_API_URL = os.getenv("OCTO_LOCAL_API_URL", "http://127.0.0.1:58888")
CLOUD_API_URL = os.getenv("OCTO_CLOUD_API_URL", "https://app.octobrowser.net")

def test_endpoint(url: str, headers: Dict[str, str] = None, name: str = "Endpoint") -> bool:
    """Тестирует доступность эндпоинта."""
    try:
        print(f"\n🔍 Проверка {name}: {url}")
        response = requests.get(url, headers=headers or {}, timeout=10)
        print(f"   ✅ Статус: {response.status_code}")
        
        try:
            data = response.json()
            print(f"   📄 Ответ (первые 200 символов): {str(data)[:200]}")
            return True
        except ValueError:
            print(f"   ⚠️  Не JSON ответ: {response.text[:200]}")
            return response.status_code < 400
    except requests.exceptions.ConnectionError as e:
        print(f"   ❌ Ошибка подключения: {e}")
        return False
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        return False

def main():
    print("=" * 80)
    print("🔧 ДИАГНОСТИКА OCTO BROWSER API")
    print("=" * 80)
    
    headers = {
        "X-Octo-Api-Token": API_KEY,
        "Content-Type": "application/json"
    }
    
    # 1. Проверка Local API
    print("\n📍 1. LOCAL API (должен быть доступен, когда Octo Browser запущен)")
    local_working = False
    
    # Пробуем разные эндпоинты Local API
    endpoints_to_try = [
        ("/api/profiles", "Список профилей (старый API)"),
        ("/api/v2/automation/profiles", "Список профилей (API v2)"),
        ("/api/profiles/active", "Активные профили"),
        ("/", "Корневой эндпоинт"),
    ]
    
    for endpoint, description in endpoints_to_try:
        if test_endpoint(f"{LOCAL_API_URL}{endpoint}", headers, f"Local API: {description}"):
            local_working = True
            break
    
    if not local_working:
        print("\n❌ LOCAL API НЕ ДОСТУПЕН!")
        print("   Возможные причины:")
        print("   1. Octo Browser не запущен")
        print("   2. Local API выключен в настройках Octo Browser")
        print("   3. Используется другой порт (не 58888)")
        print("\n   Решение:")
        print("   - Запустите Octo Browser")
        print("   - Откройте Settings → Additional → API")
        print("   - Убедитесь, что Local API включен и порт = 58888")
    
    # 2. Проверка Cloud API
    print("\n📍 2. CLOUD API (для создания/удаления профилей)")
    cloud_working = test_endpoint(
        f"{CLOUD_API_URL}/api/v2/automation/profiles",
        headers,
        "Cloud API: Список профилей"
    )
    
    if not cloud_working:
        print("\n⚠️  CLOUD API НЕ ДОСТУПЕН!")
        print("   Возможные причины:")
        print("   1. Неверный API ключ")
        print("   2. Нет интернет-соединения")
        print("   3. API ключ не активирован в аккаунте")
        print("\n   Решение:")
        print("   - Проверьте API ключ в Octo Browser:")
        print("     Settings → Additional → API Token")
        print(f"   - Ваш текущий ключ: {API_KEY[:20]}...")
    
    # 3. Проверка портов CDP
    print("\n📍 3. CDP ПОРТЫ (для Playwright/Selenium подключения)")
    print("   Сканирование портов 52000-52100 и 9222-9232...")
    
    found_ports = []
    for port in list(range(52000, 52101)) + list(range(9222, 9233)):
        try:
            response = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5)
            if response.status_code == 200:
                found_ports.append(port)
                print(f"   ✅ Найден CDP порт: {port}")
        except:
            pass
    
    if not found_ports:
        print("   ⚠️  Не найдено активных CDP портов")
        print("   Это нормально, если профили не запущены")
    
    # Итоговый отчет
    print("\n" + "=" * 80)
    print("📊 ИТОГОВЫЙ ОТЧЕТ")
    print("=" * 80)
    
    if local_working and cloud_working:
        print("✅ Все API доступны! Можно запускать scanner.py")
        print("\nРекомендуемая команда:")
        print(f'python scanner.py --targets targets.txt --api-key {API_KEY} --allow-port-scan')
        return 0
    elif local_working:
        print("⚠️  Local API работает, но Cloud API недоступен")
        print("   Вы сможете запускать профили, но не создавать новые")
        return 1
    else:
        print("❌ Local API недоступен - scanner.py не будет работать")
        print("   Сначала исправьте проблемы с Local API (см. выше)")
        return 2

if __name__ == "__main__":
    sys.exit(main())
