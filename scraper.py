import os
import requests
import psycopg2
import json
from datetime import datetime

# Bezpečné načtení databázové URL z prostředí
DB_URL = os.environ.get('DATABASE_URL')

def setup_database():
    if not DB_URL:
        raise ValueError("Chybí proměnná prostředí DATABASE_URL")
        
    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    
    # SERIAL je PostgreSQL ekvivalent pro AUTOINCREMENT
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS train_schedules (
            id SERIAL PRIMARY KEY,
            date TEXT,
            train_type TEXT,
            train_number TEXT,
            planned_time TEXT,
            UNIQUE(date, train_type, train_number, planned_time)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS platform_logs (
            id SERIAL PRIMARY KEY,
            schedule_id INTEGER REFERENCES train_schedules(id),
            timestamp TEXT,
            platform_track TEXT,
            delay_minutes INTEGER
        )
    ''')
    conn.commit()
    return conn

def fetch_cd_api():
    url = "https://www.cd.cz/stanice/5433295/getopt"
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cd.cz/stanice/brno-hl-n-/5433295"
    }
    payload = "language=cs&isDeep=true&toHistory=false"
    
    response = requests.post(url, headers=headers, data=payload)
    response.raise_for_status()
    return response.json()

def process_and_save(conn, api_data):
    cursor = conn.cursor()
    today_date = datetime.now().strftime('%Y-%m-%d')
    current_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    zmeny_pocet = 0

    trains = api_data.get('Trains', [])
    
    for train in trains:
        train_number = str(train.get('TrainNumber', ''))
        train_type = train.get('Type', '')
        planned_time = train.get('DT', '')
        delay = train.get('Delay', 0)
        
        platform_raw = train.get('StandAndTrackBox', '')
        platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''
        
        # PostgreSQL syntaxe pro "Vlož, nebo přeskoč" a parametry jako %s
        cursor.execute('''
            INSERT INTO train_schedules (date, train_type, train_number, planned_time)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (date, train_type, train_number, planned_time) DO NOTHING
        ''', (today_date, train_type, train_number, planned_time))
        
        cursor.execute('''
            SELECT id FROM train_schedules 
            WHERE date=%s AND train_type=%s AND train_number=%s AND planned_time=%s
        ''', (today_date, train_type, train_number, planned_time))
        schedule_result = cursor.fetchone()
        
        if not schedule_result:
            continue
            
        schedule_id = schedule_result[0]

        cursor.execute('''
            SELECT platform_track, delay_minutes FROM platform_logs
            WHERE schedule_id = %s
            ORDER BY timestamp DESC LIMIT 1
        ''', (schedule_id,))
        last_log = cursor.fetchone()

        if not last_log or last_log[0] != platform or last_log[1] != delay:
            cursor.execute('''
                INSERT INTO platform_logs (schedule_id, timestamp, platform_track, delay_minutes)
                VALUES (%s, %s, %s, %s)
            ''', (schedule_id, current_timestamp, platform, delay))
            
            zmeny_pocet += 1
            print(f"[{planned_time}] Zapsáno: {train_type} {train_number} | Nástupiště: '{platform}' | Zpoždění: {delay} min")

    conn.commit()
    print(f"Celkem zapsáno {zmeny_pocet} nových záznamů (změn).")

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Spouštím API Českých drah...")
    db_connection = None
    try:
        db_connection = setup_database()
        data = fetch_cd_api()
        process_and_save(db_connection, data)
    except requests.exceptions.RequestException as e:
        print(f"Chyba sítě: {e}")
    except json.JSONDecodeError:
        print("Chyba: Nevalidní JSON.")
    except Exception as e:
        print(f"Kritická chyba: {e}")
    finally:
        if db_connection:
            db_connection.close()
