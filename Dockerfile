FROM python:3.12-slim

WORKDIR /app

# Зависимости отдельным слоем для кэширования сборки
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Кэш-снапшот живёт здесь; в compose монтируется как volume
RUN mkdir -p data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
