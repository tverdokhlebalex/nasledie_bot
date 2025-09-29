#!/bin/bash

# Быстрый тест основных функций бота
# Запускает только критически важные тесты

set -e

# Используем переменные окружения по умолчанию
APP_SECRET=${APP_SECRET:-"NasledieCode"}

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${BLUE}[$(date +'%H:%M:%S')]${NC} $1"
}

success() {
    echo -e "${GREEN}✅ $1${NC}"
}

error() {
    echo -e "${RED}❌ $1${NC}"
}

# Проверяем, что контейнеры запущены
check_containers() {
    log "Проверяем контейнеры..."
    
    if ! docker-compose ps | grep -q "Up"; then
        error "Контейнеры не запущены. Запустите: docker-compose up -d"
        exit 1
    fi
    
    success "Контейнеры запущены"
}

# Проверяем API
check_api() {
    log "Проверяем API..."
    
    response=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/health" || echo "000")
    
    if [ "$response" = "200" ]; then
        success "API доступен"
    else
        error "API недоступен (код: $response)"
        exit 1
    fi
}

# Проверяем базу данных
check_database() {
    log "Проверяем базу данных..."
    
    # Проверяем подключение через API
    response=$(curl -s -H "x-app-secret: $APP_SECRET" "http://localhost:8000/api/users/all" || echo "error")
    
    if echo "$response" | grep -q "tg_id" || echo "$response" | grep -q "\[\]"; then
        success "База данных доступна"
    else
        error "Проблемы с базой данных"
        exit 1
    fi
}

# Проверяем логи на ошибки
check_logs() {
    log "Проверяем логи на ошибки..."
    
    # Проверяем логи бота
    bot_errors=$(docker-compose logs bot --tail=50 | grep -i "error\|exception\|traceback" | wc -l)
    if [ "$bot_errors" -gt 0 ]; then
        error "Найдены ошибки в логах бота: $bot_errors"
        docker-compose logs bot --tail=10 | grep -i "error\|exception\|traceback"
    else
        success "Ошибок в логах бота не найдено"
    fi
    
    # Проверяем логи API
    api_errors=$(docker-compose logs app --tail=50 | grep -i "error\|exception\|traceback" | wc -l)
    if [ "$api_errors" -gt 0 ]; then
        error "Найдены ошибки в логах API: $api_errors"
        docker-compose logs app --tail=10 | grep -i "error\|exception\|traceback"
    else
        success "Ошибок в логах API не найдено"
    fi
}

# Проверяем основные API endpoints
check_endpoints() {
    log "Проверяем основные API endpoints..."
    
    # Health
    if curl -s "http://localhost:8000/health" | grep -q "ok"; then
        success "Health endpoint работает"
    else
        error "Health endpoint не работает"
    fi
    
    # Leaderboard (требует секретный ключ)
    if [ -n "$APP_SECRET" ]; then
        response=$(curl -s -H "x-app-secret: $APP_SECRET" "http://localhost:8000/api/leaderboard")
        if echo "$response" | grep -q "team_id\|\[\]"; then
            success "Leaderboard API работает"
        else
            error "Leaderboard API не работает"
        fi
    else
        warning "APP_SECRET не установлен, пропускаем проверку leaderboard"
    fi
}

# Основная функция
main() {
    log "🚀 Быстрая проверка бота 'Наследие'"
    
    check_containers
    check_api
    check_database
    check_logs
    check_endpoints
    
    log "🎉 Быстрая проверка завершена!"
    echo ""
    echo "Для полного тестирования запустите:"
    echo "  source .env.test && ./scripts/e2e.sh"
}

# Обработка аргументов
case "${1:-}" in
    "help"|"-h"|"--help")
        echo "Быстрая проверка основных функций бота"
        echo ""
        echo "Использование: $0 [help]"
        echo ""
        echo "Что проверяется:"
        echo "  - Статус контейнеров"
        echo "  - Доступность API"
        echo "  - Подключение к БД"
        echo "  - Ошибки в логах"
        echo "  - Основные API endpoints"
        ;;
    *)
        main
        ;;
esac
