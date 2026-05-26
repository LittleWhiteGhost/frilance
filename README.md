# FreelanceParser Bot

Telegram-бот для автоматического парсинга заказов с фриланс-бирж и отправки пользователям по подписке.

## Возможности

- Парсинг заказов с 5 площадок: **Kwork** (RSS), **FL.ru**, **Freelance.ru**, **Weblancer**, **YouDo**
- Пользователь сам выбирает интересующие **категории** и **площадки**
- Пробный период **3 дня** бесплатно (остаток триала переносится при оплате)
- **Тарифы**: Basic / Pro / Max — разные лимиты заказов в час и приоритет доставки
- Оплата подписки через **ЮKassa** (карты РФ) или **Telegram Stars** (XTR) — Stars работают везде, без РФ-карт
- Webhook YooKassa с **IP-allowlist** и автоматической верификацией платежа через re-fetch SDK
- **Реферальная программа**: рефереру +14 дней при первой оплате друга, другу +7 дней к триалу
- **Промокоды**: `bonus_days` (моментально +N дней) или `discount_pct` (скидка % на следующую оплату через ЮKassa)
- Автоматическая отправка новых заказов каждые 5 минут с учётом Telegram FloodWait
- Напоминания об истечении подписки (за 3 дня и 1 день), upsell-нудж Basic→Pro когда копится очередь
- Админ-панель: статистика, рассылка, список пользователей, создание промокодов

## Быстрый старт

### 1. Создай Telegram-бота

1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot` и следуй инструкциям
3. Сохрани полученный **токен** (пример: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

### 2. Зарегистрируйся в ЮKassa

1. Зайди на [yookassa.ru](https://yookassa.ru/) и создай аккаунт
2. Получи **Shop ID** и **Secret Key** в разделе настроек
3. Для тестирования используй тестовые данные из личного кабинета

### 3. Настрой переменные окружения

```bash
cp .env.example .env
```

Отредактируй `.env`:

```env
BOT_TOKEN=твой_токен_от_botfather
ADMIN_IDS=твой_telegram_id
YOOKASSA_SHOP_ID=твой_shop_id
YOOKASSA_SECRET_KEY=твой_secret_key
# Тарифы (руб.). По умолчанию: 299/599/1499 в месяц, 2499/4999/11999 в год
SUBSCRIPTION_PRICE_BASIC_MONTHLY=299
SUBSCRIPTION_PRICE_PRO_MONTHLY=599
SUBSCRIPTION_PRICE_MAX_MONTHLY=1499
TRIAL_DAYS=3
PARSE_INTERVAL=5
DATABASE_PATH=data/bot.db

# Webhook (production). Включи WEBHOOK_ENABLED=true и опубликуй порт 8080
# наружу так, чтобы YooKassa мог стучать на https://<твой_домен>/yookassa/webhook
WEBHOOK_ENABLED=true
# Defence-in-depth: разреши только YooKassa-подсети. `default` — встроенный
# список из bot/payments/yookassa_ips.py.
YOOKASSA_WEBHOOK_IP_ALLOWLIST=default
# Если за nginx/Cloudflare — true, чтобы брать клиентский IP из X-Forwarded-For
YOOKASSA_WEBHOOK_TRUST_PROXY=false
```

> Узнать свой Telegram ID: отправь любое сообщение боту [@userinfobot](https://t.me/userinfobot)

### 4. Запуск

#### Вариант A: Docker (рекомендуется)

```bash
docker-compose up -d --build
```

Просмотр логов:
```bash
docker-compose logs -f
```

Остановка:
```bash
docker-compose down
```

#### Вариант B: Без Docker

```bash
# Установи Python 3.11+
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Запуск
python -m bot.main
```

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Запуск бота и регистрация |
| `/menu` | Главное меню |
| `/categories` | Настроить категории |
| `/platforms` | Настроить площадки |
| `/subscription` | Информация о подписке |
| `/help` | Помощь |

### Админ-команды

| Команда | Описание |
|---------|----------|
| `/admin` | Статистика бота |
| `/broadcast <текст>` | Рассылка всем пользователям |
| `/users` | Список пользователей |

## Структура проекта

```
freelance-parser-bot/
├── bot/
│   ├── main.py              # Точка входа
│   ├── config.py             # Конфигурация
│   ├── database.py           # Работа с БД (SQLite)
│   ├── keyboards.py          # Клавиатуры
│   ├── scheduler.py          # Планировщик парсинга
│   ├── handlers/
│   │   ├── start.py          # /start, /menu, /help
│   │   ├── categories.py     # Выбор категорий и площадок
│   │   ├── subscription.py   # Информация о подписке
│   │   ├── orders.py         # Просмотр заказов
│   │   ├── payment.py        # Оплата через ЮKassa
│   │   └── admin.py          # Админ-команды
│   ├── parsers/
│   │   ├── base.py           # Базовый парсер
│   │   ├── kwork.py          # Парсер Kwork
│   │   ├── fl_ru.py          # Парсер FL.ru
│   │   ├── habr_freelance.py # Парсер Freelance.ru
│   │   ├── weblancer.py      # Парсер Weblancer
│   │   └── youdo.py          # Парсер YouDo
│   └── payments/
│       └── yookassa.py       # Интеграция с ЮKassa
├── .env.example              # Шаблон переменных окружения
├── requirements.txt          # Зависимости Python
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Настройка цен

Цены задаются per-tier в `.env` (есть дефолты в `bot/constants.py`):

```env
SUBSCRIPTION_PRICE_BASIC_MONTHLY=299
SUBSCRIPTION_PRICE_BASIC_YEARLY=2499
SUBSCRIPTION_PRICE_PRO_MONTHLY=599
SUBSCRIPTION_PRICE_PRO_YEARLY=4999
SUBSCRIPTION_PRICE_MAX_MONTHLY=1499
SUBSCRIPTION_PRICE_MAX_YEARLY=11999

# То же для Telegram Stars (целое число XTR):
STARS_PRICE_PRO_MONTHLY=500
# и т.д. для остальных тарифов
```

Названия тарифов/badge-метки/лимиты по заказам — в `bot/constants.py`.

## Важные замечания

- **Парсинг**: сайты фриланс-бирж могут менять структуру HTML. Если парсер перестал работать — обнови CSS-селекторы в соответствующем файле парсера.
- **Kwork**: использует RSS-ленту (`kwork.ru/rss`), так как сайт рендерится через JavaScript.
- **Freelance.ru**: заменил Habr Freelance, который был закрыт (HTTP 410).
- **YouDo**: использует anti-bot защиту (ServicePipe). Парсер может возвращать 0 результатов без прокси. Если YouDo не работает — отключи его или используй прокси.
- **ЮKassa**: для боевого режима нужен верифицированный аккаунт юрлица/ИП. Для тестов используй тестовый режим.
- **Rate Limiting**: интервал парсинга по умолчанию 5 минут. Не ставь слишком часто, чтобы избежать блокировки IP.

## Лицензия

MIT
