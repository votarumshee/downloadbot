# DownloadBot

Асинхронный API загрузки видео с YouTube, X, Instagram и Vimeo, с PostgreSQL,
отдельными воркерами, зашифрованными сессиями технических аккаунтов и Telegram-ботом.

[Техническое задание](docs/TASK.md) · [Проверка на реальных сервисах](docs/LIVE_TESTS.md)

## Быстрый запуск

Требуются Python 3.11+, PostgreSQL 16+, Node.js 22+ или Deno для YouTube,
FFmpeg для объединения отдельных дорожек. Без FFmpeg загрузчик выбирает готовый
формат с видео и аудио; доступные качество и форматы могут быть ограничены.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -c constraints.txt -e '.[dev]'
# Либо используйте существующую PostgreSQL, либо локальный Windows helper:
powershell -ExecutionPolicy Bypass -File scripts/dev_postgres.ps1 start
```

Helper загружает официальные portable binaries PostgreSQL в игнорируемую `.runtime`,
создаёт отдельные базы приложения и тестов, слушает только localhost. Реквизиты
сохраняются в `.runtime/db.env`; не отправляйте этот файл в Git.

```powershell
# Перенос DSN в окружение без вывода пароля:
Get-Content .runtime/db.env | ForEach-Object {
    if ($_ -match '^(DOWNLOADBOT_DATABASE_URL)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
python -m downloadbot init
python -m downloadbot api
# Во втором терминале из того же каталога:
python -m downloadbot worker
```

`init` создаёт `.env` с новыми API/шифровальными ключами и не выводит их в терминал.
Если файл существует, команда не перезаписывает его. Приложение читает `.env`
из текущего каталога. OpenAPI: `http://127.0.0.1:8000/docs`.
Для Windows локальный запуск в фоне: `python scripts/start_local.py`;
PID и логи сохраняются в `.runtime`. Не запускайте второй комплект на том же порту.

## Docker Compose

Скопируйте `.env.example` в `.env`, задайте случайный URL-safe `POSTGRES_PASSWORD`
(hex или base64url, без символов `@:/?#%`),
`DOWNLOADBOT_API_KEY` (24+ символов), Fernet `DOWNLOADBOT_ENCRYPTION_KEY`.
Ключ Fernet можно получить `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

```sh
docker compose up --build -d
docker compose logs -f api worker
```

Контейнер включает FFmpeg и Node 24. API доступен только на localhost:8000;
для внешнего доступа поставьте TLS reverse proxy с rate limit. PostgreSQL
не публикует порт наружу. Для параллельных загрузок: `docker compose up -d --scale worker=3`.
Все воркеры и API должны иметь общий volume результатов и одинаковый ключ шифрования.

## HTTP API

```python
import os, time, httpx
from dotenv import load_dotenv
load_dotenv()
client = httpx.Client(base_url="http://127.0.0.1:8000",
    headers={"Authorization": "Bearer " + os.environ["DOWNLOADBOT_API_KEY"]})
r = client.post("/v1/jobs", json={
    "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "max_height": 720
}, headers={"Idempotency-Key": "example-001"})
r.raise_for_status()
job = r.json()
while job["status"] in {"queued", "running"}:
    time.sleep(2)
    job = client.get(f"/v1/jobs/{job['id']}").json()
if job["status"] == "succeeded":
    with client.stream("GET", job["file_url"]) as response:
        response.raise_for_status()
        with open("video." + job["ext"], "wb") as output:
            for chunk in response.iter_bytes():
                output.write(chunk)
else:
    print(job["status"], job["error"])
```

Публичные видео не требуют `account_id`. Секретный API-ключ передавайте только
заголовком и по HTTPS при удалённом доступе. `/healthz` проверяет API,
`/readyz` — доступность PostgreSQL и heartbeat хотя бы одного воркера.

## Технические аккаунты

1. Оператор вручную входит в отдельный технический аккаунт и убеждается, что
   нужное видео ему доступно. Экспортирует Netscape `cookies.txt` только для
   доменов нужного сервиса. Для YouTube допустимы `youtube.com` и `google.com`.
2. С административным ключом вызывает `POST /v1/accounts`:

```python
from pathlib import Path
r = client.post("/v1/accounts", json={"id": "youtube-team", "platform": "youtube",
    "cookies": Path("secrets/youtube.cookies.txt").read_text(encoding="utf-8")})
r.raise_for_status()
# При создании задания явно указывается "account_id": "youtube-team".
```

3. Для обычных клиентов задаёт в `.env` отдельные ключи и права:

```dotenv
DOWNLOADBOT_CLIENTS_JSON='[{"id":"team","api_key":"replace-with-a-random-key-of-at-least-24-characters","allowed_account_ids":["youtube-team"]},{"id":"telegram","api_key":"replace-with-another-random-key-of-at-least-24-characters","allowed_account_ids":[]}]'
```

4. При `needs_refresh` повторно экспортирует cookies и отправляет
   `PUT /v1/accounts/youtube-team/cookies` с `{ "cookies": "..." }`.
   `DELETE /v1/accounts/youtube-team` отключает аккаунт, стирает ciphertext
   и запрашивает отмену его текущей загрузки.

Пароли/2FA в сервис не передаются. Закрытое видео доступно только если аккаунт
имеет соответствующее разрешение/подписку. Необходимо бережно использовать аккаунты:
платформа может ограничить автоматические запросы или отозвать сессию. Особенности
экспорта YouTube cookies описаны в [документации yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).
TTL результата по умолчанию — 24 часа. Файлы cookies в `data/private` доступны
только сервисному пользователю: в Linux установите права `0700`; в Windows
ограничьте ACL каталога этому пользователю. Администратор ОС и оператор ключа
шифрования входят в доверенную границу сервиса.

## Telegram

В отдельном процессе, с отдельным непривилегированным API-ключом клиента:

```dotenv
TELEGRAM_BOT_TOKEN=token-from-BotFather
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
DOWNLOADBOT_API_URL=http://127.0.0.1:8000
TELEGRAM_BOT_INSTANCE=default
```

Перед запуском задайте `DOWNLOADBOT_API_KEY` в окружении процесса бота равным
ключу клиента `telegram`; он перекроет административный ключ из `.env`.
`python -m downloadbot.telegram_bot` запускает long polling без публичного webhook.
Бот принимает сообщения только в личных чатах от пользователей из allowlist:
ссылку или `/download URL`, `/status ID`, `/cancel ID`. Готовые файлы до 50 MB
передаются документом. Более крупные доступны через авторизованный API.
Выбор технических аккаунтов через Telegram в этой версии не предоставлен.

Offset, связь заданий с пользователем и состояние доставки хранятся в PostgreSQL.
После аварии между отправкой и фиксацией доставки возможна повторная отправка.
Один `TELEGRAM_BOT_INSTANCE` допускает один активный polling-процесс.

## Настройки и проверки

Все backend-настройки имеют префикс `DOWNLOADBOT_`; см. `.env.example` и `config.py`.
По умолчанию: 100 MB, 30 минут видео, 300 секунд на попытку, 2 попытки,
100 активных заданий всего, 10 на клиента, 10 секунд между заданиями аккаунта.
Сохранение метаданных бессрочное; очистка старых метаданных планируется отдельной
операционной задачей, чтобы не нарушать гарантии `Idempotency-Key`.

```powershell
python -m pytest
python -m ruff check src tests scripts
# Только при явном намерении обращаться к реальным сервисам:
python scripts/live_smoke.py --output .runtime/live-smoke.json
```

Тесты БД используют только `DOWNLOADBOT_TEST_DATABASE_URL` с именем базы,
заканчивающимся на `_test`, либо этот ключ из `.runtime/db.env`. Рабочая БД
не очищается. Без тестовой PostgreSQL интеграционные тесты пропускаются явно.
Проверка закрытого видео: `python scripts/live_smoke.py --url URL --account-id ID`.

## Эксплуатация и развитие

PostgreSQL хранит шифротекст cookies; ключ шифрования резервируется отдельно.
Не меняйте ключ без повторного импорта аккаунтов: автоматическая ротация мастер-ключа
в v0.1 не реализована. Схема v1 создаётся идемпотентно при запуске; следующие
версии требуют явных миграций. Для масштабирования на разные машины следующим
шагом будут S3-адаптер, управляемые права клиентов, метрики и миграции Alembic.

URL-проверки, ограничения загрузчика и проверка адресов уменьшают риск SSRF;
для публичного production-развёртывания дополнительно используйте egress firewall
и изолированного сервисного пользователя. Доступность форматов зависит от IP,
региона, состояния сессии и изменений сервиса. yt-dlp закреплён в `constraints.txt`;
обновления проверяйте тестами и реальными загрузками. [Официальная документация](https://github.com/yt-dlp/yt-dlp).
