import os
import sys
import requests
import psycopg2
from datetime import datetime
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
            delay_minutes INTEGER,
            UNIQUE(date, train_type, train_number)
        )
    ''')

def fetch_and_save_data():
    if not DB_URL:
        raise ValueError("Kritická chyba: Chybí proměnná prostředí DATABASE_URL")

    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    init_db(cursor)

    # Správný pražský čas
    prague_tz = ZoneInfo("Europe/Prague")
    now = datetime.now(prague_tz)
    today_date = now.strftime('%Y-%m-%d')
    current_dow = now.weekday()

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
        
        try:
            t_delay = int(train.get('Delay', 0))
        except:
            t_delay = 0
            
        platform_raw = train.get('StandAndTrackBox', '')
        platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''

        # Trik: Uloží nový vlak, nebo updatuje existující. Pokud je nové nástupiště prázdné, nechá tam to staré!
        cursor.execute('''
            INSERT INTO train_history (date, day_of_week, train_type, train_number, planned_time, final_platform, initial_platform, delay_minutes)
            VALUES (%s, %s, %s, %s, %s, %s, '', %s)
            ON CONFLICT (date, train_type, train_number) 
            DO UPDATE SET 
                -- Logika pro initial_platform (Záznam první změny)
                initial_platform = CASE
                    -- 1. Pokud už initial_platform máme zapsané, nikdy ho nepřepisujeme (ignorujeme třetí a další změny)
                    WHEN train_history.initial_platform != '' THEN train_history.initial_platform
                    
                    -- 2. Pokud přišlo NOVÉ nástupiště, my už nějaké STARÉ máme, a LIŠÍ SE... 
                    -- tak to naše staré bezpečně uložíme do initial_platform.
                    WHEN EXCLUDED.final_platform != '' 
                         AND train_history.final_platform != '' 
                         AND EXCLUDED.final_platform != train_history.final_platform 
                    THEN train_history.final_platform
                    
                    -- 3. Jinak ho necháme prázdné
                    ELSE train_history.initial_platform
                END,
                
                -- Logika pro final_platform (Vždy drží to nejnovější)
                final_platform = CASE 
                    WHEN EXCLUDED.final_platform != '' THEN EXCLUDED.final_platform 
                    ELSE train_history.final_platform 
                END,
                
                -- Aktualizace zpoždění
                delay_minutes = EXCLUDED.delay_minutes
        ''', (today_date, current_dow, t_type, t_num, t_time, platform, t_delay))
        
        processed_count += 1

    conn.commit()
    conn.close()
    print(f"Hotovo. Zpracováno {processed_count} vlaků do jednotné historie.")

if __name__ == "__main__":
    try:
        fetch_and_save_data()
    except Exception as e:
        print(f"Kritická chyba: {e}")
        sys.exit(1)
