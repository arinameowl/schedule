# The Teachers: расписание преподавателей

Telegram-бот + Mini App. Преподаватели отмечают окна, менеджеры видят свободные часы и записывают учеников, преподаватели получают уведомления о записях, переносах и отменах. Менеджеры отмечают, что урок внесён в langlisto (бот напомнит через 30 минут, если забыли).

## Запуск
1. В @BotFather создайте бота и получите токен. Узнайте свой Telegram ID (бот выдаст по команде /id).
2. Нужен домен с HTTPS (Mini App не открывается без него). Самый простой вариант на VPS: Caddy.
   ```
   schedule.example.com { reverse_proxy localhost:8000 }
   ```
3. Установка и старт:
   ```
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # заполните значения
   set -a; . ./.env; set +a
   uvicorn main:app --host 127.0.0.1 --port 8000
   ```
   Для постоянной работы оберните в systemd-сервис.
4. В @BotFather: /setmenubutton, укажите BASE_URL, чтобы кнопка приложения была в чате с ботом.
5. Добавьте людей: напишите боту от своего аккаунта (ADMIN_IDS) команду
   `/add 123456789 teacher Анна Иванова` или `/add 987654321 manager Мария`.

## Как это устроено
- `main.py`: API, бот, база SQLite (`data.db`), напоминания. Свободные слоты считаются как «шаблон окон минус выходные дни минус уроки».
- `static/index.html`: Mini App. Преподаватель видит «Окна» и «Уроки», менеджер «Свободно» и «Записи».
- Все часы считаются в поясе `BUSINESS_TZ`. Урок занимает 1 час, окна по часам с 8 до 22 (меняется в `HOURS` в index.html).

## Что дальше
Если у langlisto появится API, в `create_booking`, `move_booking` и `cancel_booking` добавляется вызов платформы, и ручная отметка «внесено» не понадобится.

## Домен, HTTPS и автозапуск
- Домен: купите у регистратора (reg.ru, Timeweb, Beget и т.п.) и создайте A-запись `schedule` на IP сервера. Подождите, пока DNS обновится (от нескольких минут до пары часов).
- Сервер: VPS с Ubuntu (1 ядро и 1 ГБ памяти хватит), порты 22, 80 и 443 открыты.
- HTTPS: `sudo apt install caddy`, запишите в `/etc/caddy/Caddyfile` блок из шага 2, затем `sudo systemctl reload caddy`. Сертификат Let's Encrypt Caddy выпустит и будет продлевать сам.
- Автозапуск: `/etc/systemd/system/schedule.service`
  ```
  [Unit]
  Description=Teachers schedule
  After=network.target
  [Service]
  WorkingDirectory=/opt/teachers-scheduler
  EnvironmentFile=/opt/teachers-scheduler/.env
  ExecStart=/opt/teachers-scheduler/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
  Restart=always
  [Install]
  WantedBy=multi-user.target
  ```
  Затем `sudo systemctl enable --now schedule`.
- Проверка до покупки домена: `cloudflared tunnel --url http://localhost:8000` выдаёт временный https-адрес, его можно подставить в BASE_URL.
