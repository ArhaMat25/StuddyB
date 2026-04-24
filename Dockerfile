FROM python:3.11-slim

WORKDIR /app

# Обновляем список пакетов и устанавливаем зависимости
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Для работы OpenCV/PIL не нужен libgl1-mesa-glx, 
# он требуется только для GUI приложений

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
