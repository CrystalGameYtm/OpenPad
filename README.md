# gamepad-remapd (OpenPad)

Асинхронний ремапер геймпадів на Python 3.11+: читає сирі HID-звіти (`/dev/hidraw*`, evdev, hidapi),
перетворює їх і віддає іграм як віртуальний геймпад (Linux — `uinput`, Windows — ViGEm).
Працює з геймпадами, які «з коробки» не мають драйвера: 2.4 ГГц-ресивери з вендорським handshake,
клони DualShock 4, пристрої з прихованими кнопками (M1–M6, C, Z, ML, MR).

* **Універсальність.** Розкладку можна взяти з готового JSON, зібрати з HID-дескриптора або **вивчити майстром
  калібрування** для будь-якого hidraw-геймпада.
* **Швидкість.** Обробка одного звіту — десятки мікросекунд (p99.9 у межах 1,5 мс; перевірка: `bench`).
* **Ремапінг.** Кнопки на кнопки, на кілька цілей, на **клавіші клавіатури й миші**; мертві зони, криві стіків,
  гіроскоп у стік, перемикання профілів без перезапуску.
* **Web UI** (`http://127.0.0.1:8765/`) — живий 2D-канвас кнопок і стіків, редагування профілю, hex-інспектор,
  опис протоколу пристрою (шина, формат звіту, handshake, вібрація, гіроскоп).
* **Керування з терміналу:** монітор, hex-sniffer, автопошук handshake, тест вібрації.

> Стан перевірки на реальному залізі — у [STATUS.md](STATUS.md). Windows-частина (ViGEm, SendInput, HidHide)
> написана за документацією й перевірена лише імітаціями; див. [packaging/windows/README.md](packaging/windows/README.md).

## Встановлення (Linux)

```bash
pip install -e ".[web]"            # ядро без залежностей; aiohttp потрібен лише для Web UI
sudo packaging/linux/install.sh    # udev-правила, uinput, група input (потім перелогінитись)
```

Перевірка:

```bash
gamepad-remapd list                # які пристрої видно і яка розкладка підібрана
gamepad-remapd serve               # демон + Web UI
```

З каталогу репозиторію без встановлення: `env PYTHONPATH=src python3 -m gamepad_remapd <команда>`.

Конфігурація — `~/.config/gamepad-remapd/` (приклад: [`config/daemon.example.json`](config/daemon.example.json)):
`daemon.json`, `profiles/`, `devices/` (власні та відкалібровані розкладки), `keymaps/`.

## Команди

| Команда | Призначення |
|---|---|
| `serve` / `daemon` / `web` | демон + Web UI / лише демон / лише UI до запущеного демона |
| `list` | HID-пристрої та підібрані розкладки |
| `monitor /dev/hidrawN` | живий монітор: яка кнопка/вісь розпізнана (`--raw` — сирі звіти) |
| `calibrate /dev/hidrawN` | майстер калібрування кнопок і осей (`--save`, `--broad`, `--paddles` — додати ML/MR) |
| `testpage` | веб-сторінка для перевірки кнопок і осей без демона (є й майстер калібрування) |
| `sniff` | hex-інспектор прихованих кнопок |
| `handshake-probe /dev/hidrawN` | автопошук handshake для «мовчазного» інтерфейсу ресивера |
| `autolayout` | розкладка з HID-дескриптора |
| `keyboards` | клавіатури, придатні як вхід (клавіатура → геймпад) |
| `rumble-test /dev/hidrawN` | вручну перевірити вібрацію (`--template`, `--sweep`, `--crc32-at`) |
| `sdl-rumble [--trace]` | вібрація через SDL3 як еталон; `--trace` показує, що SDL пише в hidraw |
| `check`, `bench`, `udev-rules` | перевірка профілю, затримка Pipeline, udev-правила доступу (`--hide VID:PID` — сховати оригінал) |
| `hidhide --vid-pid VID:PID` | Windows: команди HidHide, щоб сховати оригінал від ігор |

## Новий геймпад

1. `gamepad-remapd list` — є розкладка? Тоді все працює.
2. Ні — `gamepad-remapd calibrate /dev/hidrawN --save`: майстер сам вимірює шум пристрою, просить натиснути
   кожну кнопку й провести стіки, за потреби будить мовчазний пристрій handshake із бібліотеки SDL.
   Результат — `devices/calibrated_<vid>_<pid>.json` (пріоритет вищий за вбудовані; відсутнє — вібрація, гіроскоп,
   пропущені M-кнопки — підтягується з вбудованої розкладки тієї ж моделі).
3. Перевірте в `testpage` або `monitor`.

## Профілі та ремапінг

Профіль — JSON (`profiles/*.json`, редагується в Web UI без перезапуску):

```json
{
  "name": "shooter",
  "button_map": {
    "M1": "KEY_F",
    "M2": ["KEY_LEFTSHIFT", "B"],
    "TOUCHPAD": "MOUSE_LEFT",
    "SQUARE": "NONE"
  },
  "sticks": { "right": { "inner_deadzone": 0.08, "curve": { "type": "linear" } } }
}
```

Ключі `button_map` — імена вхідних кнопок розкладки (дивіться `list`/`monitor`). Ціль — кнопка віртуального
геймпада (`A B X Y LB RB LT RT BACK START GUIDE LS RS UP DOWN LEFT RIGHT M1..M6 C Z ML MR`), **клавіша**
`KEY_<ім'я>` (`KEY_F`, `KEY_SPACE`, `KEY_LEFTSHIFT`, `KEY_F5`, `KEY_VOLUMEUP` …), **кнопка миші**
`MOUSE_LEFT|RIGHT|MIDDLE|SIDE|EXTRA`, список цілей або `"NONE"`. Дві кнопки на одну ціль працюють як «АБО»
(відпускається, коли відпущено обидві).

Клавіші й миша віддаються окремим віртуальним пристроєм `Gamepad Remapper Virtual Keyboard`, який створюється
лише тоді, коли профіль містить такі цілі. Утримувані клавіші відпускаються при зміні профілю й відключенні
геймпада. Потрібен доступ до `/dev/uinput`. На Windows — через SendInput (див. packaging/windows/README.md).

Обернений напрямок — **клавіатура як геймпад** — описано в `keymaps/default.json` і секції `keyboard` у
`daemon.json` (`gamepad-remapd keyboards`).

## D-pad як кнопки

Деякі ігри на Linux не розуміють hat-D-pad віртуального геймпада (`ABS_HAT0X/Y`). У `daemon.json`:
`"dpad_mode": "buttons"` — D-pad віддається кнопками `BTN_DPAD_UP/DOWN/LEFT/RIGHT`. Увага: SDL/Steam за
замовчуванням очікують hat для Xbox 360, тож у деяких іграх «buttons» може, навпаки, зламати D-pad —
перевіряйте на конкретній грі.

## Приховати оригінальний геймпад від програм

Щоб гра бачила лише віртуальний геймпад, а не двічі (фізичний + віртуальний):

1. Демон сам забирає системні evdev-вузли фізичного пристрою (`grab_siblings`, увімкнено за замовчуванням).
2. Додатково, на рівні системи, — udev-правило, що прибирає з evdev-вузлів оригіналу ознаки геймпада
   (SDL, Steam, Chrome більше не знаходять його):
   ```bash
   gamepad-remapd udev-rules --hide 054c:09cc | sudo tee /etc/udev/rules.d/69-gamepad-remapd-hide.rules
   sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input   # і перепідключити геймпад
   ```
   Свій віртуальний пристрій правило не чіпає.
3. Програми на SDL з HIDAPI читають `/dev/hidrawN` напряму — udev-правило їх не зупинить. Для них:
   `SDL_GAMECONTROLLER_IGNORE_DEVICES=0x054c/0x09cc` (Steam: параметри запуску гри
   `SDL_GAMECONTROLLER_IGNORE_DEVICES=0x054c/0x09cc %command%`). Повністю закрити hidraw можна лише, запускаючи
   демон від окремого системного користувача/root, чого за замовчуванням проєкт не робить.

## Вібрація

Rumble гри передається на фізичний геймпад: через hidraw-шаблон розкладки (DS4 по USB і Bluetooth з CRC-32),
а для evdev-пристроїв — через ядровий FF. Деталі й обмеження — у [STATUS.md](STATUS.md).

## Тести

```bash
env PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -t .
```

## Структура

```
src/gamepad_remapd/
  input/     hidraw, evdev, hidapi, клавіатура — читання пристроїв
  logic/     розкладки, профілі, Pipeline, калібрування, автопошук handshake
  output/    uinput (геймпад + віртуальна клавіатура), ViGEm, null
  core/      менеджер пристроїв і сесії (читач у окремому потоці)
  tools/     monitor, calibrate, testpage, sniffer, rumble-test, sdl-rumble
  web/       Web UI (aiohttp + WebSocket, статика)
  layouts/   вбудовані розкладки (DS4 USB/BT, Flydigi V1 2.4G)
packaging/   udev, systemd, install.sh, нотатки для Windows
```

## Ліцензія

MIT — див. [LICENSE](LICENSE).
