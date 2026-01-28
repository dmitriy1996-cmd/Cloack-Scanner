#!/usr/bin/env python3
"""
Скрипт для остановки всех запущенных профилей Octo Browser.
Использует улучшенную логику force_stop из обновленного octo_client.py.

API-ключ НЕ должен быть захардкожен в репозитории.
Используйте переменную окружения OCTO_API_KEY или передавайте ключ через аргументы командной строки.
"""

import os
import sys
from octo_client import OctoClient

API_KEY = os.getenv("OCTO_API_KEY", "YOUR_OCTO_API_KEY_HERE")
LOCAL_API_URL = os.getenv("OCTO_LOCAL_API_URL", "http://127.0.0.1:58888")

def main():
    print("=" * 80)
    print("🛑 ОСТАНОВКА ВСЕХ ПРОФИЛЕЙ OCTO BROWSER")
    print("=" * 80)
    
    client = OctoClient(
        base_url=LOCAL_API_URL,
        api_key=API_KEY,
        timeout_s=30.0
    )
    
    # Получаем список профилей
    try:
        # Пробуем разные эндпоинты
        profiles_data = None
        for endpoint in ["/api/profiles/active", "/api/profiles"]:
            try:
                # Используем requests напрямую, т.к. _request не поддерживает массивы
                import requests
                headers = {"X-Octo-Api-Token": API_KEY, "Content-Type": "application/json"}
                resp = requests.get(f"{LOCAL_API_URL}{endpoint}", headers=headers, timeout=10)
                
                if resp.status_code == 200:
                    data = resp.json()
                    profiles_data = data
                    print(f"✅ Получен список профилей через {endpoint}")
                    print(f"   Тип ответа: {type(data).__name__}")
                    break
            except Exception as e:
                print(f"⚠️  {endpoint} не сработал: {e}")
                continue
        
        if not profiles_data:
            print("❌ Не удалось получить список профилей")
            print("   Возможно, профилей нет или они уже остановлены")
            return 0
        
        # Извлекаем UUID профилей
        profiles = []
        if isinstance(profiles_data, dict):
            profiles = profiles_data.get("data", []) or profiles_data.get("profiles", []) or []
        elif isinstance(profiles_data, list):
            profiles = profiles_data
        
        if not profiles:
            print("✅ Нет запущенных профилей")
            return 0
        
        print(f"\n📋 Найдено профилей для остановки: {len(profiles)}")
        
        # Останавливаем каждый профиль
        stopped = 0
        failed = 0
        
        for idx, profile in enumerate(profiles, 1):
            uuid = profile.get("uuid") if isinstance(profile, dict) else profile
            if not uuid:
                continue
            
            print(f"\n[{idx}/{len(profiles)}] Остановка профиля {uuid}...")
            
            try:
                # Используем улучшенный force_stop с повторными попытками
                success = client.force_stop_profile(uuid, max_retries=3, initial_wait_s=2.0)
                if success:
                    print(f"   ✅ Профиль {uuid} остановлен")
                    stopped += 1
                else:
                    print(f"   ⚠️  Профиль {uuid} - не удалось остановить")
                    failed += 1
            except Exception as e:
                print(f"   ❌ Ошибка при остановке {uuid}: {e}")
                failed += 1
        
        # Итоги
        print("\n" + "=" * 80)
        print("📊 ИТОГИ:")
        print(f"   ✅ Остановлено: {stopped}")
        print(f"   ⚠️  Не удалось: {failed}")
        print("=" * 80)
        
        if stopped > 0:
            print("\n✅ Все профили остановлены! Теперь можно запускать scanner.py")
            return 0
        else:
            print("\n⚠️  Некоторые профили не удалось остановить")
            print("   Попробуйте остановить их вручную в Octo Browser")
            return 1
            
    except Exception as e:
        print(f"\n❌ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return 2

if __name__ == "__main__":
    sys.exit(main())
