import time
import folium
from tqdm import tqdm
from folium.plugins import HeatMap, Fullscreen, TagFilterButton
import polyline
import stravalib.exc
from flask import Flask, render_template, request, url_for, session, redirect
from flask_caching import Cache
from stravalib import unithelper, Client
from dotenv import load_dotenv
from uuid import uuid4
import os
import pandas as pd

app = Flask(__name__)
cache = Cache(app, config={'CACHE_TYPE': 'simple'})
app.secret_key = 'your_secret_key_here'  # Change this to a secure secret key
if os.path.exists("settings.env"):
    load_dotenv("settings.env")

FLASK_ENV = os.environ.get("FLASK_ENV", "dev")

# Initialize Strava client
client = Client()


if 'STRAVA_CLIENT_ID' in os.environ:
    print('Found client id')
if 'STRAVA_CLIENT_SECRET' in os.environ:
    print('Found client secret')


@app.route("/")
def index():
    try:
        authenticated = refresh()
        if authenticated:
            return redirect(url_for('personal_bests'))
        return render_template('index.html')
    except:
        return redirect(url_for('logout'))


@app.route("/strava-login")
def strava_login():
    # Create a unique state for each user session
    session['state'] = str(uuid4())

    # Redirect user to Strava authorization URL
    redirect_uri = url_for('strava_callback', _external=True)
    print(f'Here is the redirect: {redirect_uri}')
    url = client.authorization_url(
        client_id=os.getenv("STRAVA_CLIENT_ID"),  # Replace with your Strava client ID
        redirect_uri=redirect_uri,
        state=session['state'],
        approval_prompt='auto'
    )
    print(f"Here is the url: {url}")
    return redirect(url)


def refresh():
    if 'token_expires_at' in session:
        if time.time() > session['token_expires_at']:
            if 'refresh_token' not in session:
                return False

            try:
                response = client.exchange_code_for_token(
                    client_id=os.getenv("STRAVA_CLIENT_ID"),  # Replace with your Strava client ID
                    client_secret=os.getenv("STRAVA_CLIENT_SECRET"),  # Replace with your Strava client secret
                    code=session['refresh_token']
                )
            except stravalib.exc.AccessUnauthorized:
                print('Could not refresh, not authenticated')
                return False

            # Store access token in the session for the user
            session['access_token'] = response['access_token']
            session['refresh_token'] = response['refresh_token']
            session['token_expires_at'] = response['expires_at']
            return True
        else:
            print('Already authenticted')
            return True
    else:
        return False


@app.route("/logout")
def logout():
    session.clear()
    try:
        client.deauthorize()
    except stravalib.exc.AccessUnauthorized:
        pass
    return redirect(url_for("index"))


@app.route("/strava-callback")
def strava_callback():
    if request.method == 'GET':
        # Validate state to prevent CSRF attacks
        if request.args.get('state') != session.get('state'):
            return "Invalid state. Possible CSRF attack."

        code = request.args.get('code')
        response = client.exchange_code_for_token(
            client_id=os.getenv("STRAVA_CLIENT_ID"),  # Replace with your Strava client ID
            client_secret=os.getenv("STRAVA_CLIENT_SECRET"),  # Replace with your Strava client secret
            code=code
        )

        # Store access token in the session for the user
        session['access_token'] = response['access_token']
        session['refresh_token'] = response['refresh_token']
        session['token_expires_at'] = response['expires_at']
        return redirect(url_for('personal_bests'))
    else:
        return redirect(url_for('index'))


def generate_map(activities):
    print('Generating map')

    # Loop through activities and collect map data
    routes = []
    activity_types = ['Run', 'Hike', 'Ride']
    decoded_coords = [(40, -112),]  # Define initial map position (this will get overwritten)
    for activity in activities:
        if activity.type in activity_types and activity.map:
            coords = activity.map.summary_polyline
            if coords:
                # Decode the polyline data to retrieve latitude and longitude
                decoded_coords = polyline.decode(coords)
                p = folium.PolyLine(
                    smooth_factor=1,
                    opacity=0.5,
                    locations=decoded_coords,
                    color="#FC4C02",
                    tooltip=activity.name,
                    weight=5,
                    tags=[activity.type]
                )
                routes.append(p)

    # Create a base map centered on a location
    m = folium.Map(location=decoded_coords[0], zoom_start=12)

    # Customize the map controls for mobile devices
    mobile_styles = """
        @media (max-width: 768px) { /* Adjust this breakpoint according to your needs */
            /* Increase the size of map controls for mobile */
            .leaflet-control {
                font-size: 20px;
            }

            /* Make the map expand button more accessible on mobile */
            .leaflet-control-expand-full {
                width: 40px;
                height: 40px;
                line-height: 40px;
                font-size: 24px;
            }
        }
    """

    m.get_root().html.add_child(folium.Element(f"<style>{mobile_styles}</style>"))

    for route in routes:
        m.add_child(route)

    TagFilterButton(activity_types).add_to(m)

    Fullscreen(
        position="topright",
        title="Expand me",
        title_cancel="Exit me",
        force_separate_button=True,
    ).add_to(m)

    # Save map to an HTML file or render it in the template
    m.save(f'static/{session["state"]}/heatmap.html')


def fastest_segment_within_distance(df, races):
    for race in races:
        race['start_idx'] = 0
        race['fastest_time'] = None
        race['max_avg_speed'] = 0

    for end_idx in range(1, len(df)):
        for race in races:
            segment_distance = df['distance'][end_idx] - df['distance'][race['start_idx']]
            if segment_distance >= race['distance']:
                segment_time = df['time'][end_idx] - df['time'][race['start_idx']]
                avg_speed = segment_distance / segment_time

                if avg_speed > race['max_avg_speed']:
                    race['max_avg_speed'] = avg_speed
                    race['fastest_time'] = segment_time

                race['start_idx'] += 1
    return races


def calculate_personal_bests(activities, limit=10):
    types = [
        "time",
        "latlng",
        "altitude",
        "heartrate",
        "temp",
    ]

    races = [
        {'name': '1/2 mile', 'distance': 804.67},
        {'name': '1 mile', 'distance': 1609.34},
        {'name': '2 mile', 'distance': 3218.68},
        {'name': '5k', 'distance': 5000},
        {'name': '10k', 'distance': 10000}
    ]
    all_efforts = []

    count = 0
    for activity in tqdm(activities, total=limit):
        if count >= limit:
            break
        count += 1
        if activity.type == 'Run':
            best_efforts = {}
            streams = client.get_activity_streams(activity.id, types=types, resolution="medium")
            stream_data = {}
            for key, value in streams.items():
                stream_data[key] = value.data
            df = pd.DataFrame(stream_data)

            races = fastest_segment_within_distance(df, races)
            results = {}
            for race in races:
                results[race['name']] = race['fastest_time']
            all_efforts.append(results)

    best_efforts_df = pd.DataFrame(all_efforts).min()
    list_of_dicts = [{'name': index, 'time': seconds_to_time(value)} for index, value in best_efforts_df.items()]
    return list_of_dicts


def get_gear(activities):
    gear_ids = set()
    for activity in activities:
        if activity.gear_id:
            gear_ids.add(activity.gear_id)
    gear = []
    for gear_id in gear_ids:
        gear_item = client.get_gear(gear_id)
        distance = None
        if gear_item.distance:
            distance = unithelper.miles(gear_item.distance)
        gear.append({'name': gear_item.name, 'distance': distance})
    return gear


def seconds_to_time(seconds):
    if seconds is None:
        return None
    days = int(seconds // (24 * 3600))
    hours = int((seconds % (24 * 3600)) // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)

    time_string = ""

    if days > 0:
        time_string += f"{days} d "
    if hours > 0:
        time_string += f"{hours} h "
    if minutes > 0:
        time_string += f"{minutes} m "
    if seconds > 0 or time_string == "":
        time_string += f"{seconds} s"

    return time_string.strip()


@app.route("/dashboard")
@cache.cached(timeout=60)  # Cache the heatmap for 60 seconds
def dashboard():
    authenticated = refresh()
    if not authenticated:
        return redirect(url_for('index'))
    # Use the access token to fetch user data from Strava
    client.access_token = session['access_token']
    strava_athlete = client.get_athlete()
    os.makedirs(os.path.join("static", str(session['state'])), exist_ok=True)
    activities = client.get_activities()
    hike_count = 0
    hike_distance = 0.0
    for activity in activities:
        if activity.type == 'Hike':
            hike_count += 1
            hike_distance += float(unithelper.miles(activity.distance))
    stats = {
        'run': {
            'count': strava_athlete.stats.all_run_totals.count,
            'distance': unithelper.miles(strava_athlete.stats.all_run_totals.distance)
        },
        'bike': {
            'count': strava_athlete.stats.all_ride_totals.count,
            'distance': unithelper.miles(strava_athlete.stats.all_ride_totals.distance)
        },
        'hike': {
            'count': hike_count,
            'distance': hike_distance
        }
    }
    if not os.path.exists(os.path.join("static", str(session['state']), 'heatmap.html')):
        generate_map(activities)
    return render_template('dashboard.html', athlete=strava_athlete, stats=stats, state=session['state'], flask_env=FLASK_ENV)


@app.route("/personal_bests")
def personal_bests():
    authenticated = refresh()
    if not authenticated:
        return redirect(url_for('index'))
    client.access_token = session['access_token']
    strava_athlete = client.get_athlete()
    activities = client.get_activities()
    best_efforts = calculate_personal_bests(activities)
    clubs = client.get_athlete_clubs()
    gear = get_gear(activities)
    os.makedirs(os.path.join("static", str(session['state'])), exist_ok=True)
    if not os.path.exists(os.path.join("static", str(session['state']), 'heatmap.html')):
        generate_map(activities)
    return render_template('personal_best.html', athlete=strava_athlete, best_efforts=best_efforts, clubs=clubs, gear=gear, state=session['state'])


if __name__ == "__main__":
    # Use the PORT environment variable provided by Heroku
    port = int(os.environ.get('PORT', 5001))

    # Run the app
    app.run(host='0.0.0.0', port=port, debug=True)
