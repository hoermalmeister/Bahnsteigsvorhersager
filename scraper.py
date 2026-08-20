import os
import sys
import re
import requests
import psycopg2
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_URL = os.environ.get('DATABASE_URL')

def init_db(cursor):
    """Založí jednotnou tabulku pro historii (pokud neexistuje)"""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS train_history (
            id SERIAL PRIMARY KEY,
            date VARCHAR(20),
            day_of_week INTEGER,
            train_type VARCHAR(50),
            train_number VARCHAR(50),
            planned_time VARCHAR(20),
            final_platform VARCHAR(50),
            initial_platform VARCHAR(50) DEFAULT '',
            delay_minutes INTEGER,
            UNIQUE(date, train_type, train_number)
        )
    ''')

def get_train_date_and_dow(train, prague_now):
    """Vytáhne přesné datum z URL vlaku, nebo chytře odhadne přesah přes půlnoc"""
    url = train.get('URL', '')
    
    # 1. Pokus o vytažení data přímo z URL (nejpřesnější)
    match = re.search(r'/(\d{1,2}\.\d{1,2}\.\d{4})/', url)
    if match:
        date_str = match.group(1)
        try:
            # Převedeme např. "18.8.2026" na standardní "2026-08-18" a zjistíme den v týdnu
            train_dt = datetime.strptime(date_str, "%d.%m.%Y").date()
            return train_dt.strftime('%Y-%m-%d'), train_dt.weekday()
        except ValueError:
            pass
            
    # 2. Fallback: Pokud URL selže, použijeme matematiku
    time_str = train.get('DT', '00:00')
    try:
        h, m = map(int, time_str.split(':'))
        # Pokud je večer (po 20:00) a vlak jede v noci (0-4 h), je to zítra
        if prague_now.hour > 20 and h < 4:
            train_dt = prague_now.date() + timedelta(days=1)
        else:
            train_dt = prague_now.date()
        return train_dt.strftime('%Y-%m-%d'), train_dt.weekday()
    except:
        return prague_now.strftime('%Y-%m-%d'), prague_now.weekday()


def fetch_and_save_data():
    if not DB_URL:
        raise ValueError("Kritická chyba: Chybí proměnná prostředí DATABASE_URL")

    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    init_db(cursor)

    prague_tz = ZoneInfo("Europe/Prague")
    now = datetime.now(prague_tz)

    url = "https://www.cd.cz/stanice/5433295/getopt"
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cd.cz/stanice/brno-hl-n-/5433295"
    }

    print(f"[{now.strftime('%H:%M:%S')}] Spouštím API Českých drah...")

    try:
        resp_dep = requests.post(url, headers=headers, data="language=cs&isDeep=true&toHistory=false")
        deps = resp_dep.json().get('Trains', [])
        
        resp_arr = requests.post(url, headers=headers, data="language=cs&isDeep=false&toHistory=false")
        arrs = resp_arr.json().get('Trains', [])
    except Exception as e:
        print(f"Chyba při stahování dat z API: {e}")
        sys.exit(1)

    # Filtrace končících vlaků
    dep_numbers = {str(t.get('TrainNumber', '')) for t in deps}
    live_data = deps.copy()
    
    for t in arrs:
        if str(t.get('TrainNumber', '')) not in dep_numbers:
            live_data.append(t)

    processed_count = 0

    for train in live_data:
        t_type = train.get('Type', '')
        t_num = str(train.get('TrainNumber', ''))
        t_time = train.get('DT', '')
        
        # Ošetření přesného data a dne v týdnu pro tento konkrétní vlak
        t_date, t_dow = get_train_date_and_dow(train, now)
        
        try:
            t_delay = int(train.get('Delay', 0))
        except:
            t_delay = 0
            
        platform_raw = train.get('StandAndTrackBox', '')
        platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''

        cursor.execute('''
            INSERT INTO train_history (date, day_of_week, train_type, train_number, planned_time, final_platform, initial_platform, delay_minutes)
            VALUES (%s, %s, %s, %s, %s, %s, '', %s)
            ON CONFLICT (date, train_type, train_number) 
            DO UPDATE SET 
                initial_platform = CASE
                    WHEN train_history.initial_platform != '' THEN train_history.initial_platform
                    WHEN EXCLUDED.final_platform != '' 
                         AND train_history.final_platform != '' 
                         AND EXCLUDED.final_platform != train_history.final_platform 
                    THEN train_history.final_platform
                    ELSE train_history.initial_platform
                END,
                final_platform = CASE 
                    WHEN EXCLUDED.final_platform != '' THEN EXCLUDED.final_platform 
                    ELSE train_history.final_platform 
                END,
                delay_minutes = EXCLUDED.delay_minutes
        ''', (t_date, t_dow, t_type, t_num, t_time, platform, t_delay))
        
        processed_count += 1

    conn.commit()
    conn.close()
    print(f"Hotovo. Zpracováno {processed_count} vlaků. Půlnoční spoje byly ošetřeny.")

if __name__ == "__main__":
    try:
        fetch_and_save_data()
    except Exception as e:
        print(f"Kritická chyba: {e}")
        sys.exit(1)
