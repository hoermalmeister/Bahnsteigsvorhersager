# 1. Použijeme stabilní a odlehčený Python 3.11
FROM python:3.11-slim

# 2. Nastavíme pracovní složku
WORKDIR /app

# 3. Zkopírujeme nákupní seznam a nainstalujeme ho
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Zkopírujeme zbytek tvého kódu (app.py, templates atd.)
COPY . .

# 5. PŘÍKAZ KE SPUŠTĚNÍ - Gunicorn nabindovaný na port, který nám Railway přidělí
CMD gunicorn app:app -b 0.0.0.0:$PORT
