import os
import sys
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta

DB_URL = os.environ.get('DATABASE_URL')
STATIONS = {
    "praha": "Praha hl.n.",
    "brno": "Brno hl.n.",
    "olomouc": "Olomouc hl.n."
}

def clean_platform(plat_str):
    """Ponechá pouze čísla a lomítko (odstraní sektory, mezery a písmena)."""
    if not plat_str:
        return ""
    return re.sub(r'[^0-9/]', '', str(plat_str))

def update_predictions(run_all=False):
    if not DB_URL:
        print("Chyba: Chybí DATABASE_URL")
        return

    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    for station_key, station_name in STATIONS.items():
        table_name = f"history_{station_key}"
        
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS prediction_correct BOOLEAN;")
        conn.commit()

        if run_all:
            cursor.execute(f"SELECT DISTINCT date FROM {table_name} ORDER BY date")
            dates_to_process = [row['date'] for row in cursor.fetchall()]
            print(f"\n[{station_name}] Spouštím kompletní historii ({len(dates_to_process)} dní)...")
        else:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            dates_to_process = [yesterday]
            print(f"\n[{station_name}] Zpracovávám pouze včerejšek: {yesterday}")

        for target_date_str in dates_to_process:
            target_date_obj = datetime.strptime(target_date_str, "%Y-%m-%d").date()
            is_weekend = target_date_obj.weekday() >= 5

            cursor.execute(f"""
                SELECT train_type, train_number, final_platform, delay_minutes 
                FROM {table_name} 
                WHERE date = %s AND final_platform != ''
            """, (target_date_str,))
            day_trains = cursor.fetchall()
            
            if not day_trains:
                continue

            correct_count = 0
            total_evaluated = 0

            for pt in day_trains:
                cursor.execute(f"""
                    SELECT date, day_of_week, final_platform, delay_minutes 
                    FROM {table_name} 
                    WHERE train_type = %s AND train_number = %s AND date < %s AND final_platform != ''
                """, (pt['train_type'], pt['train_number'], target_date_str))
                
                history = cursor.fetchall()
                if not history:
                    continue 

                delay_hist = [r for r in history if abs((r.get('delay_minutes') or 0) - (pt['delay_minutes'] or 0)) <= 5]
                working_hist = delay_hist if len(delay_hist) >= 2 else history
                matched_hist = [r for r in working_hist if (r['day_of_week'] >= 5) == is_weekend]
                
                if matched_hist:
                    freq = {}
                    for r in matched_hist:
                        p = r['final_platform']
                        freq[p] = freq.get(p, 0) + 1
                    
                    predicted_platform = sorted(freq.items(), key=lambda x: x[1], reverse=True)[0][0]
                    
                    # Očištění o sektory před finálním porovnáním
                    pred_clean = clean_platform(predicted_platform)
                    real_clean = clean_platform(pt['final_platform'])
                    
                    # Vyhodnocení správnosti na základě holých čísel/lomítek
                    is_correct = (pred_clean == real_clean and real_clean != "")
                    
                    if is_correct:
                        correct_count += 1
                    total_evaluated += 1

                    cursor.execute(f"""
                        UPDATE {table_name} 
                        SET prediction_correct = %s 
                        WHERE train_type = %s AND train_number = %s AND date = %s
                    """, (is_correct, pt['train_type'], pt['train_number'], target_date_str))

            if not run_all and total_evaluated > 0:
                accuracy = (correct_count / total_evaluated) * 100
                print(f"[{station_name}] {target_date_str}: Úspěšnost {accuracy:.1f} % ({correct_count}/{total_evaluated})")
        
        conn.commit()
        if run_all:
            print(f"[{station_name}] Dávka dokončena. Data jsou zapsána.")

    conn.close()

if __name__ == '__main__':
    run_full_history = "--full" in sys.argv
    update_predictions(run_full_history)
