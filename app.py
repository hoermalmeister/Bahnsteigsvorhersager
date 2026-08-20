import os
from flask import Flask, render_template, jsonify, render_template_string
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
DB_URL = os.environ.get('DATABASE_URL')

STATIONS = {
    "praha": {"id": "5457076", "name": "Praha hl.n."},
    "brno": {"id": "5433295", "name": "Brno hl.n."},
    "olomouc": {"id": "5434362", "name": "Olomouc hl.n."}
}

def get_db_connection():
    return psycopg2.connect(DB_URL)

@app.route('/')
def home():
    html = """
    <!DOCTYPE html>
    <html lang="cs">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Chytrá Tabule - Výběr stanice</title>
        <style>
            body { font-family: 'Bahnschrift', sans-serif; background: #f4f7f6; color: #222; text-align: center; padding: 50px; }
            @media (prefers-color-scheme: dark) { body { background: #121212; color: #e0e0e0; } }
            .grid { display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; max-width: 800px; margin: 40px auto; }
            a.btn { text-decoration: none; background: #004a8f; color: white; padding: 20px 40px; border-radius: 8px; font-size: 1.2em; transition: 0.2s; }
            a.btn:hover { background: #003366; transform: scale(1.05); }
            @media (prefers-color-scheme: dark) { a.btn { background: #153b6b; } }
        </style>
    </head>
    <body>
        <h1>Vyberte stanici</h1>
        <div class="grid">
            {% for key, st in stations.items() %}
                <a href="/{{ key }}" class="btn">{{ st.name }}</a>
            {% endendfor %}
        </div>
    </body>
    </html>
    """
    return render_template_string(html, stations=STATIONS)

@app.route('/<station_key>')
def station_board(station_key):
    if station_key not in STATIONS:
        return "Stanice nenalezena", 404
    station_name = STATIONS[station_key]['name']
    return render_template('index.html', station_key=station_key, station_name=station_name)

@app.route('/api/board/<station_key>')
def api_board(station_key):
    if station_key not in STATIONS:
        return jsonify({"error": "Neznámá stanice"}), 404
        
    station_id = STATIONS[station_key]['id']
    table_name = f"history_{station_key}"
    
    prague_tz = ZoneInfo("Europe/Prague")
    now = datetime.now(prague_tz)
    
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": f"https://www.cd.cz/stanice/{station_id}"
    }
    url = f"https://www.cd.cz/stanice/{station_id}/getopt"
    
    try:
        resp_dep = requests.post(url, headers=headers, data="language=cs&isDeep=true&toHistory=false", timeout=5)
        
        if resp_dep.status_code != 200:
            return jsonify({"error": f"ČD API vrátilo chybu {resp_dep.status_code}"}), 500
            
        deps = resp_dep.json().get('Trains', [])[:60]
        
        resp_arr = requests.post(url, headers=headers, data="language=cs&isDeep=false&toHistory=false", timeout=5)
        arrs = resp_arr.json().get('Trains', [])[:60]
        
    except Exception as e:
        return jsonify({"error": f"Chyba komunikace s ČD: {str(e)}"}), 500

    dep_numbers = {str(t.get('TrainNumber', '')) for t in deps}
    live_data = deps.copy()
    for t in arrs:
        if str(t.get('TrainNumber', '')) not in dep_numbers:
            live_data.append(t)

    trains_to_query = []
    for t in live_data:
        trains_to_query.append((t.get('Type', ''), str(t.get('TrainNumber', ''))))

    history = {}
    if trains_to_query and DB_URL:
        try:
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            query = f"""
                SELECT train_type, train_number, final_platform, COUNT(*) as count
                FROM {table_name}
                WHERE (train_type, train_number) IN %s
                AND final_platform != ''
                GROUP BY train_type, train_number, final_platform
            """
            cursor.execute(query, (tuple(trains_to_query),))
            rows = cursor.fetchall()
            
            for row in rows:
                key = f"{row['train_type']}_{row['train_number']}"
                if key not in history:
                    history[key] = []
                history[key].append({"platform": row['final_platform'], "count": row['count']})
                
            conn.close()
        except Exception as e:
            print("DB Error:", e)

    combined_trains = []
    for train in live_data:
        t_type = train.get('Type', '')
        t_num = str(train.get('TrainNumber', ''))
        t_name = train.get('TrainName', '')
        t_time = train.get('DT', '00:00')
        t_dest = train.get('Terminus', '') or train.get('Destination', '')
        
        platform_raw = train.get('StandAndTrackBox', '')
        live_platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else None
        
        try:
            t_delay = int(train.get('Delay', 0))
        except:
            t_delay = 0

        key = f"{t_type}_{t_num}"
        hist_records = history.get(key, [])
        
        prediction = {"status": "no_data"}
        
        if hist_records:
            total = sum(r['count'] for r in hist_records)
            hist_records.sort(key=lambda x: x['count'], reverse=True)
            
            if live_platform:
                stay_count = next((r['count'] for r in hist_records if r['platform'] == live_platform), 0)
                stay_prob = int((stay_count / total) * 100) if total > 0 else 0
                
                changes = []
                for r in hist_records:
                    if r['platform'] != live_platform:
                        changes.append({
                            "platform": r['platform'],
                            "probability": int((r['count'] / total) * 100)
                        })
                
                prediction = {
                    "status": "predict_change",
                    "stay_probability": stay_prob,
                    "changes": changes[:2]
                }
            else:
                options = []
                for r in hist_records[:3]:
                    options.append({
                        "platform": r['platform'],
                        "probability": int((r['count'] / total) * 100)
                    })
                prediction = {
                    "status": "predict_new",
                    "options": options
                }

        combined_trains.append({
            "type": t_type,
            "number": t_num,
            "name": t_name,
            "time": t_time,
            "destination": t_dest,
            "delay": t_delay,
            "live_platform": live_platform,
            "prediction": prediction
        })

    def sort_key(t):
        try:
            h, m = map(int, t['time'].split(':'))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            
            # Odladěná obousměrná korekce kolem půlnoci
            if now.hour > 20 and h < 4:
                dt += timedelta(days=1)
            elif now.hour < 4 and h > 20:
                dt -= timedelta(days=1)
            
            # Řadíme VŽDY striktně podle plánovaného času (dt)
            return dt
        except Exception:
            return now

    combined_trains.sort(key=sort_key)
    return jsonify(combined_trains[:40])

if __name__ == '__main__':
    app.run(debug=True, port=5000)
