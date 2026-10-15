# Windows

1. Встановіть драйвер **ViGEmBus** (https://github.com/nefarius/ViGEmBus/releases) і перезавантажтесь.
2. `pip install "gamepad-remapd[windows]"` (тягне `hidapi`, `vgamepad`, `aiohttp`).
3. `gamepad-remapd list` — має показати ваш ресивер. Якщо розкладки немає — див. «Додавання нового пристрою».
4. `gamepad-remapd serve` — демон + Web UI на http://127.0.0.1:8765/.

Особливості:
* Канал керування — TCP `127.0.0.1:8764` із токеном у `%APPDATA%\gamepad-remapd\control.token`.
* XInput не має кнопок M1–M6/C/Z: такі цілі пропускаються (попередження в профілі). Рішення —
  відображати їх на існуючі кнопки (напр. `"M1": "LB"`) або на `LT`/`RT` у профілі.
* Для ексклюзивності додайте фізичний пристрій у HidHide (https://github.com/nefarius/HidHide),
  інакше гра бачитиме і фізичний, і віртуальний геймпад.
* Потік-читач отримує `THREAD_PRIORITY_HIGHEST` та таймер 1 мс автоматично.
* Автозапуск: Планувальник завдань → «При вході» → `pythonw -m gamepad_remapd serve`.
* Модулі `input/hidapi_backend.py` та `output/vigem_windows.py` написані за документацією, але в цьому
  Linux-середовищі не запускались — перевірте їх на своєму ПК.
