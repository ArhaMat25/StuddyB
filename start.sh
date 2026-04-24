#!/bin/bash
set -e

# Запускаем Flask в фоне
python app.py &

# Запускаем бота
python bot.py
