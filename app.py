import os
import requests
import psycopg2
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify

app = Flask(__name__)
DB_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DB_URL)

def calculate_prediction(cursor, train_number, current_platform, current_dow):
    """
    Vypočítá pravděpodobnosti nástupišť na základě historických dat.
    Maximální pravděpodobnost je zastropována na 99 %.
    """
    if not current_platform:
        # NENÍ NÁSTUPIŠTĚ: Porovnáváme pouze stejný den v týdnu
        cursor.execute('''
            SELECT final_platform, COUNT(*) FROM train_history 
            WHERE train_number = %s AND day_of_week = %s AND final_platform != ''
            GROUP BY final_platform
        ''', (train_number, current_dow))
    else:
        # JE NÁSTUPIŠTĚ: Porovnáváme jakýkoliv den v týdnu
        cursor.execute('''
            SELECT final_platform, COUNT(*) FROM train_history 
            WHERE train_number = %s AND final_platform != ''
            GROUP BY final_platform
        ''', (train_number,))

    history = cursor.fetchall()
    if not history:
        return {"status": "no_data"}

    total_records = sum(count for _, count in history)
    predictions = []

    for platform, count in history:
        # Výpočet s maximálním stropem 99 %
        prob = int((count / total_records) * 100)
        prob = min(prob, 99) 
        predictions.append({"platform": platform, "probability": prob})

    # Seřazení od největší pravděpodobnosti po nejmenší
    predictions.sort(key=lambda x: x['probability'], reverse=True)

    if not current_platform:
        return {"status": "predict_new", "options": predictions}
    else:
        # Rozdělení na to, zda na ohlášeném nástupišti zůstane, nebo se změní
        stay_prob = next((p['probability'] for p in predictions if p['platform'] == current_platform), 0)
        changes = [p for p in predictions if p['platform'] != current_platform]
        return {
            "status": "predict_change",
            "current": current_platform,
            "stay_probability": stay_prob,
            "changes": changes
        }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/board')
def get_board():
    url = "https://www.cd.cz/stanice/5433295/getopt"
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.cd.cz/stanice/brno-hl-n-/5433295"
    }
    
    try:
        # 1. Stažení odjezdů
        resp_dep = requests.post(url, headers=headers, data="language=cs&isDeep=true&toHistory=false")
        deps = resp_dep.json().get('Trains', [])
        
        # 2. Stažení příjezdů
        resp_arr = requests.post(url, headers=headers, data="language=cs&isDeep=false&toHistory=false")
        arrs = resp_arr.json().get('Trains', [])
    except Exception as e:
        return jsonify({"error": "Nelze načíst živá data"}), 500

    # 3. Filtrace: Vytvoříme si množinu čísel vlaků na odjezdu pro bleskové vyhledávání
    dep_numbers = {str(t.get('TrainNumber', '')) for t in deps}
    combined_trains = deps.copy()
    
    # Projdeme příjezdy a přidáme jen ty končící
    for t in arrs:
        t_num = str(t.get('TrainNumber', ''))
        if t_num not in dep_numbers:
            # Přepíšeme cíl na "Ze směru", protože ČD v Destination posílá výchozí stanici
            t['Destination'] = f"Ze směru: {t.get('Destination', '')}"
            combined_trains.append(t)
            
    # 4. Sloučení obou seznamů s ohledem na absolutní čas a aktuální zpoždění
    def sort_key(train):
        url = train.get('URL', '')
        time_str = train.get('DT', '00:00')
        
        # Bezpečné načtení zpoždění (pokud chybí, je 0)
        try:
            delay_mins = int(train.get('Delay', 0))
        except (ValueError, TypeError):
            delay_mins = 0
            
        # Vytáhneme datum přímo z URL
        match = re.search(r'/(\d{1,2}\.\d{1,2}\.\d{4})/', url)
        if match:
            date_str = match.group(1)
            try:
                # 1. Vytvoříme přesný plánovaný čas
                planned_time = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
                
                # 2. Přičteme zpoždění a získáme reálný čas příjezdu/odjezdu
                actual_time = planned_time + timedelta(minutes=delay_mins)
                
                return actual_time
            except ValueError:
                pass
                
        # Fallback
        return datetime.max 

    combined_trains.sort(key=sort_key)

    current_dow = datetime.now().weekday()
    board = []
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        for train in combined_trains:
            t_num = str(train.get('TrainNumber', ''))
            t_type = train.get('Type', '')
            platform_raw = train.get('StandAndTrackBox', '')
            platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''
            
            # Získání predikce
            prediction = calculate_prediction(cursor, t_num, platform, current_dow)
            
            board.append({
                "type": t_type,
                "number": t_num,
                "destination": train.get('Destination', ''),
                "time": train.get('DT', ''),
                "delay": train.get('Delay', 0),
                "live_platform": platform,
                "prediction": prediction
            })
            
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(board)

if __name__ == '__main__':
    # V produkci se spouští jinak, toto je pro lokální testování
    app.run(debug=True, port=5000)
