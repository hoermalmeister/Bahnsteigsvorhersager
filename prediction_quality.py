import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta

DB_URL = os.environ.get('DATABASE_URL')
STATIONS = {
    "praha": "Praha hl.n.",
    "brno": "Brno hl.n.",
    "olomouc": "Olomouc hl.n."
}

def evaluate_accuracy():
    if not DB_URL:
        print("Chyba: Chybí DATABASE_URL")
        return

    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # Vyhodnocujeme vždy včerejšek (uzavřený den)
    target_date = (datetime.now() - timedelta(days=1)).date()
    target_date_str = target_date.strftime("%Y-%m-%d")
    is_weekend = target_date.weekday() >= 5

    print(f"=== Zpětné vyhodnocení přesnosti predikcí za: {target_date_str} ===")

    for station_key, station_name in STATIONS.items():
        table_name = f"history_{station_key}"
        
        try:
            # Načteme všechny včerejší vlaky, které reálně někam přijely
            cursor.execute(f"""
                SELECT train_type, train_number, final_platform, delay_minutes 
                FROM {table_name} 
                WHERE date = %s AND final_platform != ''
            """, (target_date_str,))
            day_trains = cursor.fetchall()
            
            if not day_trains:
                print(f"[{station_name}] Žádná data pro tento den.")
                continue

            correct_predictions = 0
            total_evaluated = 0

            for pt in day_trains:
                # Pro každý vlak načteme historii STRICTNĚ STARŠÍ než včerejšek
                cursor.execute(f"""
                    SELECT date, day_of_week, final_platform, delay_minutes 
                    FROM {table_name} 
                    WHERE train_type = %s AND train_number = %s AND date < %s AND final_platform != ''
                """, (pt['train_type'], pt['train_number'], target_date_str))
                
                history = cursor.fetchall()
                if not history:
                    continue # Neměli jsme historii, nepředpovídali jsme

                # Aplikujeme naši logiku se zpožděním
                delay_hist = [r for r in history if abs((r.get('delay_minutes') or 0) - (pt['delay_minutes'] or 0)) <= 5]
                working_hist = delay_hist if len(delay_hist) >= 2 else history

                # Aplikujeme naši logiku pro pracovní den vs víkend
                matched_hist = [r for r in working_hist if (r['day_of_week'] >= 5) == is_weekend]
                
                if matched_hist:
                    freq = {}
                    for r in matched_hist:
                        p = r['final_platform']
                        freq[p] = freq.get(p, 0) + 1
                    
                    # Vybereme nástupiště, které mělo nejvyšší procento
                    predicted_platform = sorted(freq.items(), key=lambda x: x[1], reverse=True)[0][0]
                    
                    total_evaluated += 1
                    # Pokud se náš hlavní tip trefil do reálné final_platform z databáze
                    if predicted_platform == pt['final_platform']:
                        correct_predictions += 1

            if total_evaluated > 0:
                accuracy = (correct_predictions / total_evaluated) * 100
                print(f"[{station_name}] Úspěšnost: {accuracy:.1f} % ({correct_predictions} správně z {total_evaluated} předpovídaných)")
            else:
                print(f"[{station_name}] Nelze vyhodnotit (chybí starší historie)")
                
        except Exception as e:
            print(f"[{station_name}] Chyba databáze: {e}")

    conn.close()

if __name__ == '__main__':
    evaluate_accuracy()
