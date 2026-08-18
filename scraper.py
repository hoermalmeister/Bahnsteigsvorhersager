import os
import requests
import psycopg2
import sys
from datetime import datetime

# Načtení hesla do databáze z GitHub Secrets
DB_URL = os.environ.get('DATABASE_URL')

def init_db(cursor):
    """Založí tabulky, pokud ještě neexistují"""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS train_schedules (
            id SERIAL PRIMARY KEY,
            date VARCHAR(20),
            train_type VARCHAR(50),
            train_number VARCHAR(50),
            planned_time VARCHAR(20),
            UNIQUE(date, train_type, train_number)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS platform_logs (
            id SERIAL PRIMARY KEY,
            schedule_id INTEGER REFERENCES train_schedules(id),
            timestamp VARCHAR(20),
            platform_track VARCHAR(50),
            delay_minutes INTEGER
        )
    ''')

def fetch_and_save_data():
    if not DB_URL:
        raise ValueError("Kritická chyba: Chybí proměnná prostředí DATABASE_URL")

    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    init_db(cursor)

    url = "https://www.cd.cz/stanice/5433295/getopt"
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cd.cz/stanice/brno-hl-n-/5433295"
    }

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Spouštím API Českých drah...")

    try:
        # 1. Stažení odjezdů
        resp_dep = requests.post(url, headers=headers, data="language=cs&isDeep=true&toHistory=false")
        deps = resp_dep.json().get('Trains', [])
        
        # 2. Stažení příjezdů
        resp_arr = requests.post(url, headers=headers, data="language=cs&isDeep=false&toHistory=false")
        arrs = resp_arr.json().get('Trains', [])
    except Exception as e:
        print(f"Chyba při stahování dat z API: {e}")
        return

    # 3. Filtrace: Sloučení seznamů a vyřazení průjezdných vlaků z příjezdů
    dep_numbers = {str(t.get('TrainNumber', '')) for t in deps}
    live_data = deps.copy()
    
    for t in arrs:
        if str(t.get('TrainNumber', '')) not in dep_numbers:
            live_data.append(t)

    today_date = datetime.now().strftime('%Y-%m-%d')
    current_time = datetime.now().strftime('%H:%M')
    processed_count = 0

    # 4. Zpracování a uložení
    for train in live_data:
        t_type = train.get('Type', '')
        t_num = str(train.get('TrainNumber', ''))
        t_time = train.get('DT', '')
        t_delay = train.get('Delay', 0)
        
        # Očištění nástupiště z formátu ČD ("Nást. 3" -> "3")
        platform_raw = train.get('StandAndTrackBox', '')
        platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''

        # Založení vlaku do rozpisu (pokud tam pro dnešek ještě není)
        cursor.execute('''
            INSERT INTO train_schedules (date, train_type, train_number, planned_time)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (date, train_type, train_number) DO NOTHING
            RETURNING id
        ''', (today_date, t_type, t_num, t_time))
        
        result = cursor.fetchone()
        if result:
            sched_id = result[0]
        else:
            cursor.execute('''
                SELECT id FROM train_schedules 
                WHERE date = %s AND train_type = %s AND train_number = %s
            ''', (today_date, t_type, t_num))
            sched_id = cursor.fetchone()[0]

        # Delta logika - uložíme změnu nástupiště nebo zpoždění jen tehdy, když se liší od posledního logu
        cursor.execute('''
            SELECT platform_track, delay_minutes FROM platform_logs
            WHERE schedule_id = %s
            ORDER BY id DESC LIMIT 1
        ''', (sched_id,))
        
        last_log = cursor.fetchone()
        
        if not last_log or last_log[0] != platform or last_log[1] != t_delay:
            cursor.execute('''
                INSERT INTO platform_logs (schedule_id, timestamp, platform_track, delay_minutes)
                VALUES (%s, %s, %s, %s)
            ''', (sched_id, current_time, platform, t_delay))
            processed_count += 1

    conn.commit()
    conn.close()
    print(f"Hotovo. Úspěšně zaznamenáno {processed_count} nových změn (včetně končících vlaků).")

if __name__ == "__main__":
    try:
        fetch_and_save_data()
    except Exception as e:
        print(f"Kritická chyba: {e}")
        sys.exit(1)
