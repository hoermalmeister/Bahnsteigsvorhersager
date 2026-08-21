import os
import re
from flask import Flask, render_template, jsonify, redirect
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

# 1. Čistá doména nyní automaticky přesměruje na Brno
@app.route('/')
def home():
    return redirect('/brno')

# 2. Generování tabule s výpočtem další stanice v pořadí (POUZE JEDNOU!)
@app.route('/<station_key>')
def station_board(station_key):
    if station_key not in STATIONS:
        return "Stanice nenalezena", 404
        
    station_name = STATIONS[station_key]['name']
    
    # Zjistíme, jaká stanice následuje pro klikací rotaci v nadpisu
    keys = list(STATIONS.keys())
    current_index = keys.index(station_key)
    next_station_key = keys[(current_index + 1) % len(keys)]
    
    return render_template('index.html', 
                           station_key=station_key, 
                           station_name=station_name,
                           next_station_key=next_station_key)

# 3. API pro konkrétní stanici
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
    live_data = []
    
    # Zpracování odjezdů (a projíždějících vlaků)
    for t in deps:
        t['_is_pure_arrival'] = False
        live_data.append(t)
        
    # Zpracování čistých příjezdů (vlaky, co zde končí)
    for t in arrs:
        if str(t.get('TrainNumber', '')) not in dep_numbers:
            t['_is_pure_arrival'] = True
            live_data.append(t)

    trains_to_query = []
    for t in live_data:
        trains_to_query.append((t.get('Type', ''), str(t.get('TrainNumber', ''))))

    history = {}
    if trains_to_query and DB_URL:
        try:
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            
            # Změna SQL: Už neseskupujeme do COUNT, potřebujeme surová data kvůli datumu a initial_platform
            query = f"""
                SELECT train_type, train_number, date, day_of_week, final_platform, initial_platform
                FROM {table_name}
                WHERE (train_type, train_number) IN %s
            """
            cursor.execute(query, (tuple(trains_to_query),))
            rows = cursor.fetchall()
            
            for row in rows:
                key = f"{row['train_type']}_{row['train_number']}"
                if key not in history:
                    history[key] = []
                history[key].append(row)
                
            conn.close()
        except Exception as e:
            print("DB Error:", e)

    # Pomocná funkce pro extrakci přesného data vlaku z URL (pro statistiku i chronologické řazení)
    def get_train_datetime(t, prague_now):
        try:
            h, m = map(int, t.get('DT', '00:00').split(':'))
            match = re.search(r'/(\d{1,2}\.\d{1,2}\.\d{4})', t.get('URL', ''))
            if match:
                date_str = match.group(1)
                train_date = datetime.strptime(date_str, "%d.%m.%Y").date()
                dt = prague_now.replace(year=train_date.year, month=train_date.month, day=train_date.day, hour=h, minute=m, second=0, microsecond=0)
                return dt, train_date
            
            # Záložní korekce (pokud URL datum nemá)
            dt = prague_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if prague_now.hour >= 18 and h <= 12:
                dt += timedelta(days=1)
            elif prague_now.hour <= 6 and h >= 18:
                dt -= timedelta(days=1)
            return dt, dt.date()
        except Exception:
            return prague_now, prague_now.date()

    combined_trains = []
    for train in live_data:
        t_type = train.get('Type', '')
        t_num = str(train.get('TrainNumber', ''))
        t_name = train.get('TrainName', '')
        t_time = train.get('DT', '00:00')
        raw_dest = train.get('Terminus', '') or train.get('Destination', '')
        
        # Pokud je název celý velkými písmeny, převedeme ho na normální tvar
        if raw_dest.isupper():
            raw_dest = raw_dest.title().replace("Hl.N.", "hl.n.").replace(" Hl. N.", " hl.n.")
        
        if train.get('_is_pure_arrival'):
            t_dest = f"Ze směru: {raw_dest}" if raw_dest else "Příjezd"
        else:
            t_dest = raw_dest
        
        platform_raw = train.get('StandAndTrackBox', '')
        live_platform = platform_raw.replace('Nást.', '').replace('kol.', '').replace(' ', '') if platform_raw else None
        
        try:
            t_delay = int(train.get('Delay', 0))
        except:
            t_delay = 0

        # Zjistíme přesné datum vlaku pro logiku filtrace
        dt_obj, t_date = get_train_datetime(train, now)
        t_date_str = t_date.strftime('%Y-%m-%d')
        is_weekend = t_date.weekday() >= 5 # 5 = Sobota, 6 = Neděle

        key = f"{t_type}_{t_num}"
        raw_hist = history.get(key, [])
        
        # PRAVIDLO 1: Nikdy nezahrnovat stejné datum, jako je datum odjezdu!
        valid_hist = [r for r in raw_hist if r['date'] != t_date_str]
        
        prediction = {"status": "no_data"}
        
        if live_platform:
            # PRAVIDLO 2: Vlak MÁ nástupiště -> final_platform vs initial_platform
            stay_count = 0
            changes_dict = {}
            
            for r in valid_hist:
                if r['final_platform'] == live_platform:
                    stay_count += 1
                elif r['initial_platform'] == live_platform and r['final_platform'] != live_platform and r['final_platform'] != '':
                    alt = r['final_platform']
                    changes_dict[alt] = changes_dict.get(alt, 0) + 1
            
            changed_count = sum(changes_dict.values())
            total_cases = stay_count + changed_count
            
            if total_cases > 0:
                # Omezení stropu na 99 %
                stay_prob = min(99, int((stay_count / total_cases) * 100))
                
                changes_list = []
                for alt_plat, count in sorted(changes_dict.items(), key=lambda x: x[1], reverse=True):
                    changes_list.append({
                        "platform": alt_plat,
                        "probability": min(99, int((count / total_cases) * 100))
                    })
                
                prediction = {
                    "status": "predict_change",
                    "stay_probability": stay_prob,
                    "changes": changes_list[:2]
                }
        else:
            # PRAVIDLO 3: Vlak NEMÁ nástupiště -> hledáme čistě podle final_platform o víkendu/v týdnu
            matched_hist = [r for r in valid_hist if (r['day_of_week'] >= 5) == is_weekend and r['final_platform'] != '']
            
            if matched_hist:
                freq = {}
                for r in matched_hist:
                    p = r['final_platform']
                    freq[p] = freq.get(p, 0) + 1
                
                total = len(matched_hist)
                options = []
                for p, count in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:3]:
                    options.append({
                        "platform": p,
                        "probability": min(99, int((count / total) * 100))
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
            "prediction": prediction,
            "planned_datetime": dt_obj  # Uložíme si do slovníku správný čas pro řazení
        })

    # Řadíme VŽDY chronologicky podle plánovaného času (dt_obj z předchozí funkce)
    def sort_key(t):
        return t['planned_datetime']

    combined_trains.sort(key=sort_key)
    
    # Odstraníme pomocný datetime objekt před odesláním do prohlížeče
    for t in combined_trains:
        del t['planned_datetime']
        
    return jsonify(combined_trains[:40])

if __name__ == '__main__':
    app.run(debug=True, port=5000)
