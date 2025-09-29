#!/bin/bash

# Скрипт для сборки wheel'ов для Linux архитектуры
# Использует Docker для сборки wheel'ов в Linux окружении

echo "Создаем wheelhouse для Linux архитектуры..."

# Создаем временный Dockerfile для сборки wheel'ов
cat > Dockerfile.wheels << 'EOF'
FROM python:3.11-slim

WORKDIR /wheels

# Устанавливаем pip и wheel
RUN python -m pip install --upgrade pip wheel

# Копируем requirements.txt
COPY requirements.txt .

# Собираем wheel'ы
RUN python -m pip wheel --no-deps -r requirements.txt

# Копируем wheel'ы в выходную директорию
CMD ["sh", "-c", "cp *.whl /output/"]
EOF

# Создаем директорию для wheel'ов
mkdir -p wheelhouse_linux

# Собираем wheel'ы в Docker контейнере
echo "Собираем wheel'ы в Docker контейнере..."
docker build -f Dockerfile.wheels -t wheel-builder .
docker run --rm -v "$(pwd)/wheelhouse_linux:/output" wheel-builder

# Очищаем временный файл
rm Dockerfile.wheels

echo "Wheel'ы собраны в директории wheelhouse_linux/"
echo "Теперь можно скопировать их в wheelhouse/ и загрузить на сервер"
