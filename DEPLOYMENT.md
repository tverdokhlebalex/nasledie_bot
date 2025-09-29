# Инструкция по развертыванию бота "Наследие"

## Подготовка к развертыванию

### 1. Загрузка файлов на сервер

Скопируйте следующие файлы на сервер:

```bash
# Основные файлы проекта
scp -r . user@server:/path/to/nasledie_bot/

# Или загрузите архив wheelhouse отдельно
scp wheelhouse.tar.gz user@server:/path/to/nasledie_bot/
```

### 2. Распаковка wheelhouse (если загружали архивом)

```bash
cd /path/to/nasledie_bot/
tar -xzf wheelhouse.tar.gz
```

### 3. Настройка переменных окружения

Создайте файл `.env` на сервере:

```bash
# Основные настройки
BOT_TOKEN=your_bot_token_here
ADMIN_CHAT_ID=your_admin_chat_id
APP_SECRET=your_secret_key_here

# API настройки
API_URL=http://localhost:8000
DATABASE_URL=postgresql://user:password@db:5432/nasledie_bot

# Настройки очков
ARTICLE_POINTS=10
PHOTO_POINTS=5

# Настройки для совместимости (можно оставить пустыми)
TEAM_SIZE=
ROUTE_COUNT=
COORDINATOR_CONTACT=
PUBLIC_WEBAPP_URL=
WEBAPP_URL=
```

### 4. Настройка базы данных

```bash
# Создайте базу данных PostgreSQL
sudo -u postgres createdb nasledie_bot
sudo -u postgres createuser nasledie_user
sudo -u postgres psql -c "ALTER USER nasledie_user PASSWORD 'your_password';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE nasledie_bot TO nasledie_user;"
```

### 5. Запуск приложения

```bash
# Соберите и запустите контейнеры
docker-compose up -d --build

# Проверьте статус
docker-compose ps

# Посмотрите логи
docker-compose logs -f
```

## Проверка работоспособности

### 1. Проверка API

```bash
curl http://localhost:8000/health
```

### 2. Проверка бота

Отправьте команду `/start` боту в Telegram.

### 3. Проверка админ-функций

В админ-чате должны работать команды:
- `/leaderboard` - просмотр лидерборда
- Кнопки модерации для отправленных ссылок и фото

## Загрузка участников

### 1. Подготовка CSV файла

Создайте файл `data/participants.csv`:

```csv
phone,first_name,last_name,team_number
+79890876902,Александр,Иванов,1
+79853823050,Лада,Петрова,1
+79123456789,Мария,Сидорова,2
```

### 2. Загрузка через API

```bash
curl -X POST http://localhost:8000/api/admin/participants/upload \
  -H "x-app-secret: your_secret_key_here" \
  -F "file=@data/participants.csv"
```

## Мониторинг

### Логи приложения

```bash
# Логи всех сервисов
docker-compose logs -f

# Логи конкретного сервиса
docker-compose logs -f app
docker-compose logs -f bot
```

### Проверка состояния

```bash
# Статус контейнеров
docker-compose ps

# Использование ресурсов
docker stats
```

## Обновление

### 1. Остановка сервисов

```bash
docker-compose down
```

### 2. Обновление кода

```bash
git pull origin main
```

### 3. Обновление wheelhouse (если нужно)

```bash
# Распакуйте новый wheelhouse
tar -xzf wheelhouse.tar.gz
```

### 4. Перезапуск

```bash
docker-compose up -d --build
```

## Резервное копирование

### База данных

```bash
# Создание бэкапа
docker-compose exec db pg_dump -U nasledie_user nasledie_bot > backup_$(date +%Y%m%d_%H%M%S).sql

# Восстановление
docker-compose exec -T db psql -U nasledie_user nasledie_bot < backup_file.sql
```

## Устранение неполадок

### Проблемы с билдом

```bash
# Очистка кеша Docker
docker system prune -f

# Пересборка без кеша
docker-compose build --no-cache
```

### Проблемы с базой данных

```bash
# Проверка подключения
docker-compose exec db psql -U nasledie_user -d nasledie_bot -c "SELECT 1;"

# Пересоздание базы
docker-compose down
docker volume rm nasledie_bot_db_data
docker-compose up -d
```

### Проблемы с ботом

```bash
# Проверка токена
curl -X GET "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getMe"

# Логи бота
docker-compose logs bot
```
