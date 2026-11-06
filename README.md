# gamepad-remapd (OpenPad)

Асинхронний ремапер геймпадів на Python 3.11+: читає сирі HID-звіти (`/dev/hidraw*`, evdev, hidapi),
перетворює їх і віддає іграм як віртуальний геймпад (Linux — `uinput`, Windows — ViGEm).
Працює з геймпадами, які «з коробки» не мають драйвера: 2.4 ГГц-ресивери з вендорським handshake,
клони DualShock 4, пристрої з прихованими кнопками (M1–M6, C, Z, ML, MR).

* **Універсальність.** Розкладку можна взяти з готового JSON, зібрати з HID-дескриптора або **вивчити майстром
  калібрування** для будь-якого hidraw-геймпада.
* **Швидкість.** Обробка одного звіту — десятки мікросекунд (p99.9 у межах 1,5 мс; перевірка: `bench`).
* **Ремапінг.** Кнопки на кнопки, на кілька цілей, на **клавіші клавіатури й миші**; мертві зони, криві стіків,
  гіроскоп у стік або мишу (живі °/с в UI, калібрування нуля одним кліком), перемикання профілів без перезапуску.
* **Вкладка «Протокол» у Web UI:** опис протоколу, вибір режиму роботи геймпада (де пристрій це дозволяє, напр. Switch Pro: повний 0x30 з IMU / простий 0x3F),
  вибір вихідного протоколу (емуляція XInput — рекомендовано; штатні DS4/DInput — експериментально) і перемикач «Ховати оригінал».
* **Web UI** (`http://127.0.0.1:8765/`) — живий 2D-канвас кнопок і стіків, редагування профілю, hex-інспектор,
  опис протоколу пристрою (шина, формат звіту, handshake, вібрація, гіроскоп).
* **Керування з терміналу:** монітор, hex-sniffer, автопошук handshake, тест вібрації.

> Стан перевірки на реальному залізі — у [STATUS.md](STATUS.md). Windows-частина (ViGEm, SendInput, HidHide)
> написана за документацією й перевірена лише імітаціями; див. [packaging/windows/README.md](packaging/windows/README.md).

## Встановлення (Linux)

```bash
pip install -e ".[web]"            # ядро без залежностей; aiohttp потрібен лише для Web UI
sudo packaging/linux/install.sh    # udev-правила, uinput, група input (потім перелогінитись)
# (`setup-hide` перегенерує 70-gamepad-remapd.rules під ваші пристрої — див. розділ про приховування)
```

Перевірка:

```bash
gamepad-remapd list                # які пристрої видно і яка розкладка підібрана
gamepad-remapd serve               # демон + Web UI
```

Windows: `pip install aiohttp vgamepad` + драйвер ViGEmBus; `hidapi` не обов'язковий — є вбудований WinAPI-backend
(див. [packaging/windows/README.md](packaging/windows/README.md)).

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
| `check`, `bench`, `udev-rules` | перевірка профілю, затримка Pipeline, udev-правила доступу (`--hide VID:PID` — лише evdev-ознаки) |
| `hidhide --vid-pid VID:PID` | Windows: команди HidHide, щоб сховати оригінал від ігор |
| `setup-hide` | Linux: автоналаштування приховування оригіналу (udev + systemd-сервіс із `CAP_FOWNER`), `--uninstall` |
| `unhide` | Linux: повернути права на вузли оригіналу після аварії демона |
| `sdl-list` | що реально бачить SDL3 (перевірка приховування) |
| `serve --privileged` | Linux: запуск із `CAP_FOWNER` (sudo + setpriv), щоб демон міг сховати оригінал |

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

## Сховати оригінал від інших програм (SDL, емулятори, ігри)

SDL знаходить геймпади, **відкриваючи** вузли `/dev/input/event*`, `/dev/input/js*` і `/dev/hidraw*`; вузол, який відкрити
не вдалось, він пропускає. Тому демон після відкриття пристрою закриває (chmod 0600) **усі вузли цього фізичного пристрою**
(hidraw, event, js; клавіатури й миші композитних ресиверів не чіпає). Його власні дескриптори лишаються робочими, SDL бачить
лише віртуальний геймпад. Раніше evdev ховався тільки захопленням (EVIOCGRAB) — SDL усе одно відкривав «мертвий» дублікат.

Без права закривати вузли (потрібен `CAP_FOWNER`) приховування **не застосовується зовсім** (жодних напівзаходів) —
статус у вкладці «Протокол» і журнал це скажуть. Щоб воно працювало:

    gamepad-remapd setup-hide --auto --service        # udev-правила + systemd-сервіс із CAP_FOWNER (сам просить sudo)
    gamepad-remapd setup-hide --hide 04b4:2412 --service   # або конкретний VID:PID
    gamepad-remapd setup-hide --auto --dry-run        # лише показати, що буде зроблено
    gamepad-remapd setup-hide --uninstall             # прибрати правила й сервіс
    python3 -m gamepad_remapd serve --privileged      # разовий ручний запуск із CAP_FOWNER (sudo + setpriv)

Перевірка й відновлення:

    gamepad-remapd sdl-list     # що саме бачить SDL3 (шлях, VID:PID); оригіналу в списку бути не має
    gamepad-remapd unhide       # повернути права вузлам після аварії демона (демон робить це й сам при старті)

Права повертаються при закритті пристрою/зупинці демона; облік — по вузлах (кілька інтерфейсів одного пристрою). Bluetooth:
чіпаємо лише вузли самого HID-пристрою, не BT-адаптера. УВАГА: SDL з HIDAPI не показує evdev-пристрої з VID:PID DualShock —
для віртуального виходу в емуляторах обирайте XInput.

## Вихідний протокол

Вкладка «Протокол» → «Вихідний протокол» (для кожної моделі окремо; на Windows — лише Xbox 360 через ViGEm):

* **Емуляція XInput (Xbox 360)** — рекомендовано; працює в SDL-іграх і емуляторах (перевірено: ScarletNX, CTR Turbocharged).
* **Штатний: DualShock 4 / DInput (загальний геймпад)** — експериментально. Для віртуального пристрою SDL не має мапінгу
  (а DS4 з VID:PID Sony SDL віддає власному HIDAPI-драйверу), тож більшість ігор такий вихід не розпізнає.
  UI і журнал про це попереджають.

## Гіроскоп

У профілі секція `gyro` (у стік або мишу, криві, інверсії, активація кнопкою). Вкладка «Профіль» показує живі °/с
і має кнопку «Калібрувати нуль» (геймпад нерухомо ~1,5 с; bias зберігається в профілі).

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
  input/     hidraw, evdev, hidapi/winhid, клавіатура — читання пристроїв; hide_linux — приховування оригіналу
  logic/     розкладки, профілі, Pipeline, калібрування, автопошук handshake
  output/    uinput (геймпад + віртуальна клавіатура), ViGEm, null
  core/      менеджер пристроїв і сесії (читач у окремому потоці)
  tools/     monitor, calibrate, testpage, sniffer, rumble-test, sdl-rumble, sdl-list, setup-hide
  web/       Web UI (aiohttp + WebSocket, статика)
  layouts/   вбудовані розкладки (DS4 USB/BT, Flydigi V1 2.4G, Switch Pro)
packaging/   udev, systemd, install.sh, нотатки для Windows
```

## Ліцензія

MIT — див. [LICENSE](LICENSE).
