# Windows

> Windows-частина написана за документацією (ViGEm/vgamepad, hidapi, WinAPI SendInput, HidHide) і перевірена
> лише на Linux за допомогою імітацій у `tests/test_windows.py`. На справжній Windows її ще не запускали:
> очікуйте дрібних помилок і повідомляйте про них.

## Встановлення

1. Драйвер **ViGEmBus** (https://github.com/nefarius/ViGEmBus/releases), потім перезавантаження.
2. Python 3.11+ і `pip install ".[windows]"` (тягне `hidapi`, `vgamepad`, `aiohttp`).
3. `gamepad-remapd list` — має показати ваш геймпад/ресивер (бекенд `hidapi`). Є розкладка — добре; ні — `gamepad-remapd calibrate <шлях>`
   (шлях — з колонки `list`) навчить будь-який HID-геймпад.
4. `gamepad-remapd serve` — демон + Web UI на http://127.0.0.1:8765/.

## Що працює

* Вхід через hidapi (Report ID, handshake, вібрація — ті самі розкладки, що й на Linux; шина USB/Bluetooth
  береться з `bus_type` hidapi ≥ 0.13).
* Вихід — віртуальний **Xbox 360** (ViGEm). Вібрація гри передається на фізичний геймпад.
* **Клавіатура й миша** як цілі кнопок (`KEY_F`, `MOUSE_LEFT` …) — через SendInput, скан-кодами.
* Канал керування — TCP `127.0.0.1:8764` із токеном у `%APPDATA%\gamepad-remapd\control.token`.
* Потік-читач: `THREAD_PRIORITY_HIGHEST` і таймер 1 мс автоматично.

## Приховати оригінальний геймпад (HidHide)

Інакше гра бачить і фізичний, і віртуальний геймпад. Встановіть **HidHide**
(https://github.com/nefarius/HidHide), далі:

```
gamepad-remapd hidhide --vid-pid 054c:09cc          # лише показати команди
gamepad-remapd hidhide --vid-pid 054c:09cc --run    # виконати (термінал від адміністратора)
```

Команди: дозволити процес Python бачити приховане, сховати пристрій, увімкнути приховування.

## Обмеження

* XInput не має кнопок M1–M6/C/Z/ML/MR: такі цілі пропускаються (попередження в профілі). Відобразіть їх на
  існуючі кнопки (`"M1": "LB"`) або на клавіші (`"M1": "KEY_F"`).
* Режим DS4 на виході (`output_kind: ds4`) на Windows не підтримується — завжди Xbox 360.
* «Клавіатура як геймпад» (`keyboard` у `daemon.json`) — лише Linux.
* Автомапінг із HID-дескриптора на Windows не реалізований (потрібен HidP_*): для невідомих пристроїв —
  `calibrate`.
* `bench` на Windows міряє Pipeline без вартості системних викликів (сухий вихід).
* Автозапуск: Планувальник завдань → «При вході» → `pythonw -m gamepad_remapd serve`.

## Без компіляції hidapi

Якщо `pip install hidapi` не збирається (Python 3.14, немає Visual C++ Build Tools), нічого ставити не треба:
на Windows демон сам переходить на вбудований backend `winhid` (WinAPI через ctypes). Достатньо:

    pip install aiohttp vgamepad      # + драйвер ViGEmBus
    python -m gamepad_remapd list

`backend: "winhid"` у конфігу вмикає його примусово. Backend ще не перевірявся на реальній Windows — якщо `list`
нічого не показує або падає, надішліть вивід `python -m gamepad_remapd list -v`.
