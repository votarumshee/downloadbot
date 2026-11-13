# Развёртывание DownloadBot на отдельном VDS

Проверено на Ubuntu 26.04 LTS, Docker Engine 29.9.3 и Compose 5.7.1.
Начальная конфигурация сервера — 4 vCPU, 8 ГБ RAM, 75 ГБ диска.
Образ приложения содержит Node 24, FFmpeg и Pillow; на хосте они не требуются.
Порядок действий для агентов, параметры созданного VPS и правила проверки
релиза находятся в [AGENTS.md](../AGENTS.md).

## Структура

| Назначение | Путь |
|---|---|
| Исходники и Compose-файлы | `/srv/downloadbot` |
| Рабочие секреты, root-only, mode 0600 | `/etc/downloadbot/deployment.env` |
| Токен, allowlist и отдельный API-клиент Telegram, mode 0600 | `/etc/downloadbot/telegram.env` |
| Проверенный commit и manifest SHA-256 | `/etc/downloadbot/release.json`, `/srv/downloadbot/.deployment-manifest.json` |
| Ссылка для `env_file` базового Compose | `/srv/downloadbot/.env` → `/etc/downloadbot/deployment.env` |
| PostgreSQL и результаты | Docker volumes `downloadbot_postgres_data`, `downloadbot_downloads` |
| Локальные дампы PostgreSQL | `/var/backups/downloadbot` |
| Запуск приложения | `downloadbot.service` |
| Ежедневное резервирование | `downloadbot-backup.timer` |

Первоначальные секреты генерируются на сервере. База, API и мастер-ключ
шифрования должны иметь новые независимые значения; локальные dev-секреты
в production не переносятся. `POSTGRES_PASSWORD` должен быть URL-safe.
В исходниках `.env` и реальные ключи не хранятся.

Базовый `compose.yaml` применяется вместе с `deploy/compose.vds.yaml`.
Дополнение задаёт ограничения CPU/RAM, healthcheck API/воркера и ротацию
журналов до трёх файлов по 10 МБ для каждого контейнера.

## Установка и запуск

Docker устанавливается из [официального apt-репозитория](https://docs.docker.com/engine/install/ubuntu/).
Исходники передаются из проверенного checkout как архив без `.git`, `.env`,
`.venv`, `.runtime`, cookies и видео. На сервере файлы устанавливаются в
`/srv/downloadbot` от root. Создаётся файл окружения, а `.env` делается ссылкой
на него. Пример минимального состава окружения см. `.env.example`.

Выполнять на VDS с административными правами:

```sh
cd /srv/downloadbot
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml config --quiet
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml build
install -m 0644 deploy/downloadbot.service /etc/systemd/system/
install -m 0644 deploy/downloadbot-backup.service /etc/systemd/system/
install -m 0644 deploy/downloadbot-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now docker downloadbot.service
systemctl enable --now downloadbot-backup.timer
```

Статус и readiness:

```sh
systemctl status downloadbot.service
cd /srv/downloadbot
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml ps
curl --fail http://127.0.0.1:8000/readyz
```

API публикуется только на `127.0.0.1:8000`, PostgreSQL наружу не публикуется.
На новом VDS firewall разрешает входящие подключения по SSH/22. Вход по
SSH-ключу проверяется до отключения парольной SSH-аутентификации.

## Доступ с Windows через SSH-туннель

В PowerShell, заменив `VPS_IP` своим адресом:

```powershell
ssh -N -L 127.0.0.1:18000:127.0.0.1:8000 `
  -i "$env:USERPROFILE\.ssh\downloadbot-vds" `
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes `
  -o "UserKnownHostsFile=$env:USERPROFILE\.ssh\downloadbot_known_hosts" `
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 `
  ubuntu@VPS_IP
```

Пока туннель открыт, Swagger доступен по `http://127.0.0.1:18000/docs`.
В `Authorize` вводится клиентский API-ключ без дополнительного префикса.
Ключ клиента `owner` даёт доступ к собственным заданиям; административные
операции с аккаунтами требуют отдельного административного ключа.

Для настроенного рабочего места клиентский ключ сохранён в пользовательской
переменной Windows `VPS_DWNLBOT_API_KEY`. Скопировать без вывода в терминал:

```powershell
Set-Clipboard -Value ([Environment]::GetEnvironmentVariable('VPS_DWNLBOT_API_KEY', 'User'))
```

Первая SSH-проверка закрепила ключ нового сервера в отдельном
`~/.ssh/downloadbot_known_hosts`; дальнейшие подключения проверяют его строго.
Временный пароль OVHcloud при обязательном первом входе был заменён;
`VPS_DWNLBOT_PASS` обновлён в пользовательском окружении Windows. SSH использует
ключ. Recovery-копия секретов приложения сохранена отдельно от Git в
`~/.ssh/downloadbot-vds-recovery.env` на рабочем месте владельца.

Для публичного API следующим шагом подключается собственный домен и HTTPS.
Telegram long polling сможет работать с внутренним API без публичного домена.

## Резервирование

`downloadbot-backup.timer` запускается ежедневно в 03:30 UTC с разбросом до 15 минут.
Скрипт создаёт custom-format `pg_dump`, проверяет чтение через `pg_restore --list`,
а затем удаляет локальные дампы старше семи полных суток. Доступ к каталогу — root-only.

```sh
systemctl start downloadbot-backup.service
systemctl list-timers --all downloadbot-backup.timer
journalctl -u downloadbot-backup.service -n 20 --no-pager
```

На первом развёртывании дамп действительно восстановлен в отдельную временную БД;
число заданий совпало с исходной БД. После проверки временная БД удалена.
Мастер-ключ шифрования нужен для восстановления cookies и хранится отдельно
от дампов, в recovery-копии владельца. Автоматическая выгрузка дампов во внешнее
хранилище пока не подключена; локальные дампы не защищают от потери самого VDS.

## Обновление

Перед обновлением создать свежий дамп, передать проверенные исходники без секретов,
затем выполнить `docker compose ... up -d --build --wait` с обоими Compose-файлами
и тем же `--env-file`. Не менять `POSTGRES_PASSWORD` или мастер-ключ при обычном
обновлении. Результаты и очередь находятся в постоянных volumes.
Если Telegram подключён, к каждому Compose-вызову добавлять
`-f deploy/compose.telegram.yaml`; пересобирать и обновлять `api worker telegram`.
Один только рестарт не подхватывает изменённый Python-код из Docker image.

Фотоальбомы сохраняются одним ZIP-артефактом в том же volume, что и видео;
schema таблицы `jobs` не меняется. Для продолжения отправки большого альбома
бот при старте добавляет `bot_jobs.delivered_items` с default 0. Это совместимое
добавление: откат к предыдущему image не требует удаления столбца или восстановления БД.

## Подключение Telegram

Бот запускается отдельно через `deploy/compose.telegram.yaml` и
`downloadbot-telegram.service`. Его собственный root-only файл
`/etc/downloadbot/telegram.env` содержит:

```dotenv
TELEGRAM_BOT_TOKEN='<from-BotFather>'
TELEGRAM_ALLOWED_USER_IDS='<comma-separated-numeric-user-ids>'
DOWNLOADBOT_API_KEY='<separate-telegram-client-key>'
DOWNLOADBOT_DATABASE_URL='<PostgreSQL-DSN-with-the-existing-database-password>'
DOWNLOADBOT_API_URL='http://api:8000'
TELEGRAM_BOT_INSTANCE='downloadbot'
```

В `DOWNLOADBOT_CLIENTS_JSON` серверного окружения должен быть отдельный клиент
`telegram` с тем же клиентским ключом и пустым `allowed_account_ids`. После изменения
списка клиентов API должен перечитать конфигурацию при перезапуске. Боту не нужны
административный API-ключ и мастер-ключ шифрования.

Токен проверяется через `getMe` без вывода значения. Для первого владельца можно
использовать одноразовое сообщение `/start <случайный-код>` в личном чате: ID
извлекается только из сообщения с ожидаемым кодом, а затем записывается в allowlist.
Произвольный первый пользователь бота автоматически владельцем не становится.

```sh
cd /srv/downloadbot
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml -f deploy/compose.telegram.yaml config --quiet
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml -f deploy/compose.telegram.yaml build telegram
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml up -d --wait api worker
install -m 0644 deploy/downloadbot-telegram.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now downloadbot-telegram.service
```

Контейнер не получает volume с результатами или cookies; документы скачиваются
через авторизованный внутренний API. Временное хранилище ограничено 256 МБ для
четырёх параллельных отправок до 50 МБ. PostgreSQL сохраняет offset и принадлежность
заданий пользователям. Для закрытого контента требуется импорт сессии технического
аккаунта и явное разрешение клиенту на использование этого аккаунта; выбор такого
аккаунта в Telegram пока не реализован.
