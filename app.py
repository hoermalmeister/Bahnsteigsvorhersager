import os
import re
import requests
import psycopg2
from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta

app = Flask(__name__)
DB_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DB_URL)

def calculate_prediction(cursor, train_number, current_platform, today_str, is_weekend):
    """
    Nová vylepšená predikční logika s oddělením víkendů/prac. dnů 
    a sledováním dispečerských změn (initial vs final).
    """
    if not current_platform:
        # 1. NENÍ NÁSTUPIŠTĚ: Pracovní dny vs. víkendy (bez dneška)
        # 0 = pondělí ... 6 = neděle
        days_tuple = (5, 6) if is_weekend else (0, 1, 2, 3, 4)
        
        cursor.execute('''
            SELECT final_platform, COUNT(*) FROM train_history 
            WHERE train_number = %s 
              AND day_of_week IN %s 
              AND date != %s 
              AND final_platform != ''
            GROUP BY final_platform
        ''', (train_number, days_tuple, today_str))
        
        history = cursor.fetchall()
        if not history:
            return {"status": "no_data"}

        total_records = sum(count for _, count in history)
        predictions = []
        for platform, count in history:
            prob = int((count / total_records) * 100)
            prob = min(prob, 99) 
            predictions.append({"platform": platform, "probability": prob})

        predictions.sort(key=lambda x: x['probability'], reverse=True)
        return {"status": "predict_new", "options": predictions}

    else:
        # 2. JE NÁSTUPIŠTĚ: Analyzujeme stabilitu aktuálně hlášeného nástupiště (bez dneška, nehledě na dny v týdnu)
        
        # A) ÚSPĚCH: Kolikrát se z tohoto nástupiště nakonec reálně odjelo (final_platform)
        cursor.execute('''
            SELECT COUNT(*) FROM train_history 
            WHERE train_number = %s 
              AND final_platform = %s 
              AND date != %s
        ''', (train_number, current_platform, today_str))
        success_count = cursor.fetchone()[0]

        # B) SELHÁNÍ (Změna): Kolikrát dispečer zahlásil toto nástupiště jako první (initial), ale nakonec se odjelo jinam
        cursor.execute('''
            SELECT final_platform, COUNT(*) FROM train_history 
            WHERE train_number = %s 
              AND initial_platform = %s 
              AND final_platform != %s 
              AND final_platform != '' 
              AND date != %s
            GROUP BY final_platform
        ''', (train_number, current_platform, current_platform, today_str))
        changes = cursor.fetchall()

        total_records = success_count + sum(count for _, count in changes)
        
        if total_records == 0:
            return {"status": "no_data"}

        stay_prob = min(int((success_count / total_records) * 100), 99)
        
        change_preds = []
        for new_plat, count in changes:
            prob = min(int((count / total_records) * 100), 99)
            change_preds.append({"platform": new_plat, "probability": prob})
            
        change_preds.sort(key=lambda x: x['probability'], reverse=True)

        return {
            "status": "predict_change",
            "current": current_platform,
            "stay_probability": stay_prob,
            "changes": change_preds
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
        resp_dep = requests.post(url, headers=headers, data="language=cs&isDeep=true&toHistory=false")
        deps = resp_dep.json().get('Trains', [])[:30] # Vezmeme trochu víc, ať máme rezervu po filtraci
        
        resp_arr = requests.post(url, headers=headers, data="language=cs&isDeep=false&toHistory=false")
        arrs = resp_arr.json().get('Trains', [])[:30]
    except Exception as e:
        return jsonify({"error": "Nelze načíst živá data"}), 500

    # 1. Filtrace končících příjezdů
    dep_numbers = {str(t.get('TrainNumber', '')) for t in deps}
    combined_trains = deps.copy()
    
    for t in arrs:
        t_num = str(t.get('TrainNumber', ''))
        if t_num not in dep_numbers:
            t['Destination'] = f"Ze směru: {t.get('Origin', t.get('StartStation', t.get('Destination', '')))}"
            combined_trains.append(t)
            
    # 2. Řazení ČISTĚ podle plánovaného času (zpoždění ignorujeme, jak jsi požadoval)
    def sort_key(train):
        t_url = train.get('URL', '')
        time_str = train.get('DT', '00:00')
        
        match = re.search(r'/(\d{1,2}\.\d{1,2}\.\d{4})/', t_url)
        if match:
            date_str = match.group(1)
            try:
                # Žádné přičítání zpoždění – vloží se tam, kam patří podle grafikonu
                return datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
            except ValueError:
                pass
        return datetime.max 

    combined_trains.sort(key=sort_key)
    
    # 3. Odříznutí na max 20 vlaků, ať je tabule čistá
    combined_trains = combined_trains[:20]

    # Zjištění dneška a víkendu (přidáváme 2 hodiny pro simulaci pražského času na UTC serveru)
    prague_now = datetime.utcnow() + timedelta(hours=2)
    today_str = prague_now.strftime('%Y-%m-%d')
    is_weekend = prague_now.weekday() >= 5
    
    board = []
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        for train in combined_trains:
            t_num = str(train.get('TrainNumber', ''))
            t_type = train.get('Type', '')
            platform_raw = train.get('StandAndTrackBox', '')
            platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else ''
            
            # Zavolání nové statistiky
            prediction = calculate_prediction(cursor, t_num, platform, today_str, is_weekend)
            
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
    app.run(debug=True, port=5000)
