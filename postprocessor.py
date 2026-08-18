import os
import psycopg2
import json
from datetime import datetime, timedelta

DB_URL = os.environ.get('DATABASE_URL')

def run_postprocessing():
    if not DB_URL:
        raise ValueError("Chybí proměnná prostředí DATABASE_URL")
        
    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    
    # 1. Vytvoření historické tabulky (pokud neexistuje)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS train_history (
            id SERIAL PRIMARY KEY,
            date DATE,
            day_of_week INTEGER,
            train_type VARCHAR(50),
            train_number VARCHAR(50),
            planned_time VARCHAR(20),
            final_platform VARCHAR(20),
            final_delay INTEGER,
            platform_sequence JSONB
        )
    ''')
    
    # Určíme včerejší datum (abychom zpracovávali jen vlaky, které už jistě dojely)
    yesterday = (datetime.now() - timedelta(days=0)).strftime('%Y-%m-%d')
    print(f"Spouštím post-processing pro datum: {yesterday}")

    # 2. Načtení všech včerejších spojů ze surové tabulky
    cursor.execute('''
        SELECT id, date, train_type, train_number, planned_time 
        FROM train_schedules 
        WHERE date = %s
    ''', (yesterday,))
    
    schedules = cursor.fetchall()
    processed_count = 0

    for schedule in schedules:
        sched_id, s_date, s_type, s_num, s_time = schedule
        
        # Zjištění dne v týdnu pro ML model (0 = pondělí, 6 = neděle)
        day_of_week = datetime.strptime(s_date, '%Y-%m-%d').weekday()

        # 3. Načtení všech logů pro daný vlak, seřazených chronologicky
        cursor.execute('''
            SELECT timestamp, platform_track, delay_minutes 
            FROM platform_logs 
            WHERE schedule_id = %s 
            ORDER BY timestamp ASC
        ''', (sched_id,))
        logs = cursor.fetchall()
        
        if not logs:
            continue

        # Proměnné pro finální stav a historii
        platform_sequence = []
        last_seen_platform = None
        final_platform = ""
        final_delay = 0

        # 4. Projdeme logy a sestavíme historii změn nástupišť
        for log in logs:
            timestamp, platform, delay = log
            final_delay = delay
            final_platform = platform
            
            # Pokud je nástupiště vyplněné a liší se od naposledy zapsaného, přidáme ho do sekvence
            if platform and platform != last_seen_platform:
                platform_sequence.append({
                    "time": timestamp,
                    "platform": platform
                })
                last_seen_platform = platform

        # Převedeme Python slovník na JSON řetězec pro databázi
        sequence_json = json.dumps(platform_sequence)

        # 5. Uložení očištěného záznamu do train_history
        cursor.execute('''
            INSERT INTO train_history 
            (date, day_of_week, train_type, train_number, planned_time, final_platform, final_delay, platform_sequence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (s_date, day_of_week, s_type, s_num, s_time, final_platform, final_delay, sequence_json))

        # 6. Smazání zpracovaných surových dat (čištění databáze)
        cursor.execute('DELETE FROM platform_logs WHERE schedule_id = %s', (sched_id,))
        cursor.execute('DELETE FROM train_schedules WHERE id = %s', (sched_id,))
        
        processed_count += 1

    conn.commit()
    conn.close()
    print(f"Hotovo. Úspěšně zpracováno, zkonsolidováno a vyčištěno {processed_count} vlaků.")

if __name__ == "__main__":
    try:
        run_postprocessing()
    except Exception as e:
        print(f"Kritická chyba při post-processingu: {e}")
