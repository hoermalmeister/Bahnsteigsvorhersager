import os
import sys
import re
import requests
import psycopg2
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time

DB_URL = os.environ.get('DATABASE_URL')

STATIONS = {
    "praha": {"id": "5457076", "slug": "praha-hln"},
    "brno": {"id": "5433295", "slug": "brno-hln"},
    "olomouc": {"id": "5434362", "slug": "olomouc-hln"}
}

def init_db(cursor):
    for key in STATIONS.keys():
        table_name = f"history_{key}"
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {table_name} (
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
    url = train.get('URL', '')
    match = re.search(r'/(\d{1,2}\.\d{1,2}\.\d{4})/', url)
    if match:
        date_str = match.group(1)
        try:
            train_dt = datetime.strptime(date_str, "%d.%m.%Y").date()
            return train_dt.strftime('%Y-%m-%d'), train_dt.weekday()
        except ValueError:
            pass
            
    time_str = train.get('DT', '00:00')
    try:
        h, m = map(int, time_str.split(':'))
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
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    })
    
    try:
        session.get("https://www.cd.cz/", timeout=10)
    except:
        pass

    print(f"[{now.strftime('%H:%M:%S')}] Spouštím hromadný sběr pro {len(STATIONS)} stanic...")
    total_processed = 0

    for station_key, st in STATIONS.items():
        station_id = st["id"]
        slug = st["slug"]
        url = f"https://www.cd.cz/stanice/{slug}/{station_id}/getopt"
        session.headers.update({"Referer": f"https://www.cd.cz/stanice/{slug}/{station_id}"})
        table_name = f"history_{station_key}"

        try:
            resp_dep = session.post(url, data="language=cs&isDeep=true&toHistory=false", timeout=10)
            deps = resp_dep.json().get('Trains', [])
            
            resp_arr = session.post(url, data="language=cs&isDeep=false&toHistory=false", timeout=10)
            arrs = resp_arr.json().get('Trains', [])
        except Exception as e:
            print(f"Chyba při stahování dat pro {station_key}: {e}")
            continue

        dep_numbers = {str(t.get('TrainNumber', '')) for t in deps}
        live_data = deps.copy()
        for t in arrs:
            if str(t.get('TrainNumber', '')) not in dep_numbers:
                live_data.append(t)

        station_processed = 0
        for train in live_data:
            t_type = train.get('Type', '')
            t_num = str(train.get('TrainNumber', ''))
            t_time = train.get('DT', '')
            t_date, t_dow = get_train_date_and_dow(train, now)
            
            try:
                t_delay = int(train.get('Delay', 0))
            except:
                t_delay = 0
                
            platform_raw = train.get('StandAndTrackBox', '')
            platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''

            cursor.execute(f'''
                INSERT INTO {table_name} (date, day_of_week, train_type, train_number, planned_time, final_platform, initial_platform, delay_minutes)
                VALUES (%s, %s, %s, %s, %s, %s, '', %s)
                ON CONFLICT (date, train_type, train_number) 
                DO UPDATE SET 
                    initial_platform = CASE
                        WHEN {table_name}.initial_platform != '' THEN {table_name}.initial_platform
                        WHEN EXCLUDED.final_platform != '' 
                             AND {table_name}.final_platform != '' 
                             AND EXCLUDED.final_platform != {table_name}.final_platform 
                        THEN {table_name}.final_platform
                        ELSE {table_name}.initial_platform
                    END,
                    final_platform = CASE 
                        WHEN EXCLUDED.final_platform != '' THEN EXCLUDED.final_platform 
                        ELSE {table_name}.final_platform 
                    END,
                    delay_minutes = EXCLUDED.delay_minutes
            ''', (t_date, t_dow, t_type, t_num, t_time, platform, t_delay))
            
            station_processed += 1
            total_processed += 1

        print(f" - {station_key.upper()} zpracováno: {station_processed} spojů.")
        time.sleep(2)

    conn.commit()
    conn.close()
    print(f"Hotovo. Zpracováno celkem {total_processed} záznamů v síti.")

if __name__ == "__main__":
    try:
        fetch_and_save_data()
    except Exception as e:
        print(f"Kritická chyba: {e}")
        sys.exit(1)
