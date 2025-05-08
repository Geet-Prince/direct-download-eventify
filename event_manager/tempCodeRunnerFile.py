# -*- coding: utf-8 -*- 
import os
import uuid
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import qrcode
from io import BytesIO
import base64
from datetime import datetime, timedelta # Added timedelta for potential future use
import traceback 

# Uncomment for real password hashing
# from werkzeug.security import generate_password_hash, check_password_hash 

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))

# --- Google Sheets Setup ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(BASE_DIR, 'google_creds.json')
SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file']
MASTER_SHEET_NAME = 'event management' 
YOUR_PERSONAL_EMAIL = "prince.raj.ds@gmail.com" # <-- SET YOUR EMAIL OR None

# --- Constants ---
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M' # Format used by datetime-local input and for storing in sheets

# --- Core Google Sheets Functions (Assumed Mostly Correct from previous versions) ---
# Includes get_gspread_client, share_spreadsheet_with_editor... (Keeping the previous robust versions)

def get_gspread_client():
    """Authorizes gspread client using service account credentials."""
    print("Attempting to authorize gspread client...")
    try:
        if not os.path.exists(CREDS_FILE):
             print(f"CRITICAL ERROR: Credentials file not found at '{CREDS_FILE}'")
             raise FileNotFoundError(f"Credentials file not found at '{CREDS_FILE}'")
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
        client = gspread.authorize(creds)
        print("gspread client authorized successfully.")
        return client
    except Exception as e:
        print(f"CRITICAL ERROR initializing gspread client: {e}")
        print(f"Ensure '{CREDS_FILE}' is correct, has read permissions, and required APIs are enabled in Google Cloud.")
        raise

def share_spreadsheet_with_editor(spreadsheet, email_address, sheet_title):
     """Shares a gspread Spreadsheet object with a specified email as Editor."""
     if not email_address or "@" not in email_address:
          print(f"Skipping sharing '{sheet_title}': Invalid or missing email '{email_address}'.")
          return False
     try:
          print(f"Sharing spreadsheet '{sheet_title}' with {email_address}...")
          permissions = spreadsheet.list_permissions()
          already_shared_correctly = False
          for p in permissions:
               if p.get('type') == 'user' and p.get('emailAddress') == email_address:
                    if p.get('role') in ['owner', 'writer']: # writer == editor
                         already_shared_correctly = True
                         print(f"'{sheet_title}' already shared correctly with {email_address}.")
                         break
                    else:
                         print(f"Updating permissions for {email_address} on '{sheet_title}' to 'writer' (editor)...")
                         spreadsheet.share(email_address, perm_type='user', role='writer', notify=False)
                         already_shared_correctly = True
                         break
          if not already_shared_correctly:
               print(f"Sharing '{sheet_title}' as new permission for {email_address} (role: writer/editor)...")
               spreadsheet.share(email_address, perm_type='user', role='writer', notify=False)
          print(f"Successfully ensured sharing for '{sheet_title}' with {email_address}.")
          return True
     except Exception as share_e:
          print(f"\nWARNING: Could not share spreadsheet '{sheet_title}' with {email_address}. Error: {share_e}\n")
          return False


def get_master_sheet_tabs():
    """Opens the master spreadsheet and ensures 'Clubs' & 'Fests' tabs exist with correct headers."""
    client = get_gspread_client()
    spreadsheet = None
    # --- Try opening the master sheet ---
    try:
        print(f"Opening master spreadsheet: '{MASTER_SHEET_NAME}'")
        spreadsheet = client.open(MASTER_SHEET_NAME)
        print(f"Opened master spreadsheet: '{spreadsheet.title}' (ID: {spreadsheet.id})")
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Master sheet '{MASTER_SHEET_NAME}' not found. Creating...")
        try:
            spreadsheet = client.create(MASTER_SHEET_NAME)
            print(f"Master sheet '{MASTER_SHEET_NAME}' created by service account (ID: {spreadsheet.id}).")
            share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
        except Exception as create_e:
            print(f"CRITICAL ERROR creating master sheet '{MASTER_SHEET_NAME}': {create_e}")
            raise
    except Exception as open_e:
        print(f"CRITICAL ERROR opening master sheet '{MASTER_SHEET_NAME}': {open_e}")
        raise
    if not spreadsheet: raise Exception(f"Failed to get handle for master sheet '{MASTER_SHEET_NAME}'.")

    # --- Ensure 'Clubs' worksheet ---
    clubs_headers = ['ClubID', 'ClubName', 'Email', 'PasswordHash']
    try:
        clubs_sheet = spreadsheet.worksheet("Clubs")
        print("Found 'Clubs' worksheet.")
    except gspread.exceptions.WorksheetNotFound:
        print("'Clubs' worksheet not found. Creating...")
        try:
             clubs_sheet = spreadsheet.add_worksheet(title="Clubs", rows="100", cols=len(clubs_headers))
             clubs_sheet.append_row(clubs_headers)
             print("'Clubs' worksheet created with headers.")
        except Exception as create_ws_e: print(f"ERROR creating 'Clubs' ws: {create_ws_e}"); raise

    # --- Ensure 'Fests' worksheet ---
    # *** THESE HEADERS MUST MATCH THE ORDER IN YOUR SHEET ***
    fests_headers = ['FestID', 'FestName', 'ClubID', 'ClubName', 'StartTime', 'EndTime', 'RegistrationEndTime', 'Details', 'Published', 'Venue', 'Guests']
    try:
        fests_sheet = spreadsheet.worksheet("Fests")
        print("Found 'Fests' worksheet.")
        # Basic header check (optional but good)
        current_headers = fests_sheet.row_values(1) if fests_sheet.row_count > 0 else []
        if current_headers != fests_headers:
            print("WARNING: 'Fests' headers differ! Found:", current_headers, "Expected:", fests_headers)
            # Consider adding logic to update headers if needed, but might overwrite data.
    except gspread.exceptions.WorksheetNotFound:
        print("'Fests' worksheet not found. Creating...")
        try:
            fests_sheet = spreadsheet.add_worksheet(title="Fests", rows="100", cols=len(fests_headers))
            fests_sheet.append_row(fests_headers) # Append correct headers
            print("'Fests' worksheet created with headers.")
        except Exception as create_ws_e: print(f"ERROR creating 'Fests' ws: {create_ws_e}"); raise

    return client, spreadsheet, clubs_sheet, fests_sheet

def get_or_create_worksheet(client, spreadsheet_title, worksheet_title, headers=None):
    """Opens/Creates an individual spreadsheet and a worksheet within it."""
    spreadsheet = None
    worksheet = None
    headers = headers or []
    try:
        print(f"Opening/Creating individual SS: '{spreadsheet_title}'")
        spreadsheet = client.open(spreadsheet_title)
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Individual SS '{spreadsheet_title}' not found. Creating...")
        try:
            spreadsheet = client.create(spreadsheet_title)
            print(f"Created individual SS '{spreadsheet.title}' (ID: {spreadsheet.id}).")
            share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, spreadsheet.title)
        except Exception as create_e: print(f"ERROR creating SS '{spreadsheet_title}': {create_e}"); raise
    except Exception as open_e: print(f"ERROR opening SS '{spreadsheet_title}': {open_e}"); raise
    if not spreadsheet: raise Exception(f"Failed get/create SS '{spreadsheet_title}'.")

    try:
        worksheet = spreadsheet.worksheet(worksheet_title)
        print(f"Found WS '{worksheet_title}' in '{spreadsheet.title}'.")
    except gspread.exceptions.WorksheetNotFound:
        print(f"WS '{worksheet_title}' not found in '{spreadsheet.title}'. Creating...")
        try:
            ws_cols = len(headers) if headers else 10
            worksheet = spreadsheet.add_worksheet(title=worksheet_title, rows="500", cols=ws_cols)
            if headers: worksheet.append_row(headers)
            print(f"WS '{worksheet_title}' created {'with' if headers else 'without'} headers.")
        except Exception as create_ws_e: print(f"ERROR creating WS '{worksheet_title}': {create_ws_e}"); raise
    except Exception as get_ws_e: print(f"ERROR getting WS '{worksheet_title}': {get_ws_e}"); raise
    if not worksheet: raise Exception(f"Failed get/create WS '{worksheet_title}'.")
    return worksheet

# --- Helper Functions ---
def generate_unique_id(): return str(uuid.uuid4().hex)[:10]
def hash_password(password): return password # Placeholder
def verify_password(hashed_password, provided_password): return hashed_password == provided_password # Placeholder

# --- Routes ---
@app.route('/')
def index(): return render_template('index.html')

# === Club Routes ===
@app.route('/club/register', methods=['GET', 'POST'])
def club_register():
    if request.method == 'POST':
        # ... (Get form data as before) ...
        club_name = request.form.get('club_name','').strip()
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        confirm_password = request.form.get('confirm_password','')
        if not all([club_name, email, password, confirm_password]): flash("All fields required.", "danger"); return render_template('club_register.html')
        if password != confirm_password: flash("Passwords do not match.", "danger"); return render_template('club_register.html')
        if "@" not in email or "." not in email: flash("Invalid email format.", "danger"); return render_template('club_register.html')
        try:
            _ , _, clubs_sheet, _ = get_master_sheet_tabs()
            if clubs_sheet.findall(email, in_column=3): flash("Email already registered.", "warning"); return redirect(url_for('club_login'))
            club_id = generate_unique_id(); hashed_pass = hash_password(password)
            print(f"ClubReg: Appending: {club_id}, {club_name}")
            clubs_sheet.append_row([club_id, club_name, email, hashed_pass])
            flash("Club registered! Please login.", "success"); return redirect(url_for('club_login'))
        except Exception as e: print(f"ERROR: Club registration: {e}"); traceback.print_exc(); flash("Registration error.", "danger")
    return render_template('club_register.html')

@app.route('/club/login', methods=['GET', 'POST'])
def club_login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower(); password = request.form.get('password','')
        if not email or not password: flash("Email/password required.", "danger"); return render_template('club_login.html')
        try:
            _ , _, clubs_sheet, _ = get_master_sheet_tabs()
            cell = clubs_sheet.find(email, in_column=3)
            if cell and verify_password(clubs_sheet.cell(cell.row, 4).value, password):
                session['club_id'] = clubs_sheet.cell(cell.row, 1).value
                session['club_name'] = clubs_sheet.cell(cell.row, 2).value
                flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
            flash("Invalid email or password.", "danger")
        except gspread.exceptions.CellNotFound: flash("Invalid email or password.", "danger")
        except Exception as e: print(f"ERROR: Club login: {e}"); traceback.print_exc(); flash("Login error.", "danger")
    return render_template('club_login.html')

@app.route('/club/logout')
def club_logout():
    session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))

@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now = datetime.now()
    upcoming, ongoing = [], []
    try:
        _ , _, _, fests_sheet = get_master_sheet_tabs()
        all_fests_data = fests_sheet.get_all_records()
        club_fests = [f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
        print(f"Dashboard: Club {session['club_id']} has {len(club_fests)} total fests. Now: {now.strftime(DATETIME_SHEET_FORMAT)}")
        for fest in club_fests:
            try:
                start_str, end_str = fest.get('StartTime', ''), fest.get('EndTime', '')
                if start_str and end_str:
                    start_time = datetime.strptime(start_str, DATETIME_SHEET_FORMAT)
                    end_time = datetime.strptime(end_str, DATETIME_SHEET_FORMAT)
                    if now < start_time: upcoming.append(fest); print(f" - Upcoming: {fest.get('FestName')}")
                    elif start_time <= now < end_time: ongoing.append(fest); print(f" - Ongoing: {fest.get('FestName')}")
                    # else: Past - will be handled by history
                else: print(f" - Skipping categorize (missing times): {fest.get('FestName')}")
            except (ValueError, TypeError) as e: print(f" - Skipping categorize (bad time format: {e}): {fest.get('FestName')}")
        print(f"Dashboard: Upcoming: {len(upcoming)}, Ongoing: {len(ongoing)}")
        return render_template('club_dashboard.html', club_name=session.get('club_name', 'Club'), upcoming_fests=upcoming, ongoing_fests=ongoing)
    except Exception as e: print(f"ERROR: Dashboard data failed: {e}"); traceback.print_exc(); flash("Load dashboard error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name', 'Club'), upcoming_fests=[], ongoing_fests=[])

# *** NEW HISTORY ROUTE ***
@app.route('/club/history')
def club_history():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now = datetime.now(); past_fests = []
    try:
        _ , _, _, fests_sheet = get_master_sheet_tabs()
        all_fests_data = fests_sheet.get_all_records()
        club_fests = [f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
        print(f"History: Checking {len(club_fests)} total fests for club {session['club_id']}.")
        for fest in club_fests:
            try:
                end_str = fest.get('EndTime', '')
                if end_str:
                    end_time = datetime.strptime(end_str, DATETIME_SHEET_FORMAT)
                    if now >= end_time: past_fests.append(fest); print(f" - Past: {fest.get('FestName')} (Ended: {end_str})")
                else: print(f" - Skipping history (missing EndTime): {fest.get('FestName')}")
            except (ValueError, TypeError) as e: print(f" - Skipping history (bad time format: {e}): {fest.get('FestName')}")
        print(f"History: Found {len(past_fests)} past fests.")
        # Sort newest ended first
        past_fests.sort(key=lambda x: datetime.strptime(x.get('EndTime','1900-01-01T00:00'), DATETIME_SHEET_FORMAT), reverse=True)
        return render_template('club_history.html', club_name=session.get('club_name', 'Club'), past_fests=past_fests)
    except Exception as e: print(f"ERROR: Fetching history failed: {e}"); traceback.print_exc(); flash("Load history error.", "danger"); return render_template('club_history.html', club_name=session.get('club_name', 'Club'), past_fests=[])


@app.route('/club/create_fest', methods=['GET', 'POST'])
def create_fest():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    if request.method == 'POST':
        # Get form data
        fest_name=request.form.get('fest_name','').strip(); start_time_str=request.form.get('start_time', ''); end_time_str=request.form.get('end_time', ''); registration_end_time_str=request.form.get('registration_end_time', ''); fest_details=request.form.get('fest_details','').strip(); fest_venue=request.form.get('fest_venue', '').strip(); fest_guests=request.form.get('fest_guests', '').strip(); is_published='yes' if request.form.get('publish_fest') == 'yes' else 'no'
        print(f"CreateFest POST: Start='{start_time_str}', End='{end_time_str}', RegEnd='{registration_end_time_str}'")
        # Basic validation - check required fields
        required = {'Fest Name': fest_name, 'Start Time': start_time_str, 'End Time': end_time_str, 'Registration Deadline': registration_end_time_str, 'Details': fest_details}
        missing = [name for name, val in required.items() if not val]
        if missing: flash(f"Missing required fields: {', '.join(missing)}", "danger"); return render_template('create_fest.html', form_data=request.form)
        # Optional: Add validation to check if times are logical (end > start > reg_end)
        try:
            # Basic time comparison check (can be enhanced)
            start_dt = datetime.strptime(start_time_str, DATETIME_SHEET_FORMAT)
            end_dt = datetime.strptime(end_time_str, DATETIME_SHEET_FORMAT)
            reg_end_dt = datetime.strptime(registration_end_time_str, DATETIME_SHEET_FORMAT)
            if not (start_dt < end_dt and reg_end_dt <= start_dt):
                 flash("Invalid time logic: Ensure Start < End and Registration Deadline is not after Start.", "danger")
                 return render_template('create_fest.html', form_data=request.form)
        except ValueError:
             flash("Invalid date/time format submitted.", "danger")
             return render_template('create_fest.html', form_data=request.form)
        # Append to sheet
        try:
            client, _, _, master_fests_sheet = get_master_sheet_tabs()
            fest_id = generate_unique_id()
            print(f"CreateFest: Appending fest '{fest_name}' ID:{fest_id}.")
            # Data order must match fests_headers defined in get_master_sheet_tabs
            new_fest_row = [fest_id, fest_name, session['club_id'], session.get('club_name', 'N/A'), start_time_str, end_time_str, registration_end_time_str, fest_details, is_published, fest_venue, fest_guests]
            master_fests_sheet.append_row(new_fest_row)
            print("CreateFest: Appended to master sheet.")
            # Create individual sheet
            safe_base = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip()
            if not safe_base: safe_base = "fest_event"
            safe_sheet_title = f"{safe_base[:80]}_{fest_id}"
            event_sheet_headers = ['UniqueID', 'Name', 'Email', 'Mobile', 'College', 'Present', 'Timestamp']
            get_or_create_worksheet(client, safe_sheet_title, "Registrations", event_sheet_headers)
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'))
        except Exception as e: print(f"ERROR: Create Fest DB error: {e}"); traceback.print_exc(); flash("Error creating fest.", "danger")
    return render_template('create_fest.html') # Handles GET request


# *** NEW FEST STATS ROUTE ***
@app.route('/club/fest/<fest_id>/stats')
def fest_stats(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    print(f"FestStats: Request for FestID: {fest_id}")
    fest_info = None; individual_sheet_title = "N/A"
    stats = {'total_registered': 0, 'total_present': 0, 'total_absent': 0, 'attendees_present': [], 'attendees_absent': []}
    try:
        # 1. Get fest info & verify ownership
        client, _, _, fests_master_sheet = get_master_sheet_tabs()
        all_fests_data = fests_master_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Fest not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Access denied for this fest.", "danger"); return redirect(url_for('club_dashboard'))

        # 2. Determine individual sheet title
        safe_base = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip()
        if not safe_base: safe_base = "fest_event"
        individual_sheet_title = f"{safe_base[:80]}_{fest_info.get('FestID', '')}"
        print(f"FestStats: Accessing SS '{individual_sheet_title}'")

        # 3. Get registrations worksheet (handle if not found)
        try:
             individual_spreadsheet = client.open(individual_sheet_title)
             registrations_sheet = individual_spreadsheet.worksheet("Registrations")
        except (gspread.exceptions.SpreadsheetNotFound, gspread.exceptions.WorksheetNotFound) as sheet_err:
             print(f"FestStats WARNING: Registrations sheet/tab not found for '{individual_sheet_title}': {sheet_err}")
             flash("Registration data sheet/tab not found for this event. Cannot calculate stats.", "warning")
             # Still render stats page but show data is missing
             return render_template('fest_stats.html', fest=fest_info, stats=stats) 
             
        # 4. Read data & 5. Calculate stats
        registrations_data = registrations_sheet.get_all_records()
        stats['total_registered'] = len(registrations_data)
        event_sheet_headers = ['UniqueID', 'Name', 'Email', 'Mobile', 'College', 'Present', 'Timestamp'] # For reference
        present_col_name = 'Present'
        for record in registrations_data:
            is_present = str(record.get(present_col_name, 'no')).strip().lower() == 'yes'
            attendee_details = { k: record.get(k, '') for k in event_sheet_headers }
            if is_present: stats['total_present'] += 1; stats['attendees_present'].append(attendee_details)
            else: stats['attendees_absent'].append(attendee_details)
        stats['total_absent'] = stats['total_registered'] - stats['total_present']
    except Exception as e: print(f"ERROR: Stats generation failed (FestID {fest_id}): {e}"); traceback.print_exc(); flash("Error generating stats.", "danger")
    
    print(f"FestStats: Rendering - Total:{stats['total_registered']}, Present:{stats['total_present']}")
    return render_template('fest_stats.html', fest=fest_info, stats=stats)


# --- Edit Fest Route Placeholder ---
@app.route('/club/fest/<fest_id>/edit', methods=['GET', 'POST'])
def edit_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    # TODO: Implement editing logic
    flash(f"Edit fest (ID: {fest_id}) - Not Implemented", "info"); return redirect(url_for('club_dashboard'))

# === Attendee Routes ===
@app.route('/events')
def live_events():
    now = datetime.now(); available_fests = []
    try:
        _ , _, _, fests_sheet = get_master_sheet_tabs()
        all_fests_data = fests_sheet.get_all_records()
        print(f"LiveEvents: Checking {len(all_fests_data)} total fests. Now: {now.strftime(DATETIME_SHEET_FORMAT)}")
        for fest in all_fests_data:
            is_published = str(fest.get('Published', '')).strip().lower() == 'yes'
            reg_end_str = fest.get('RegistrationEndTime', '')
            if is_published and reg_end_str:
                try:
                    reg_end_time = datetime.strptime(reg_end_str, DATETIME_SHEET_FORMAT)
                    if now < reg_end_time: available_fests.append(fest); print(f" - Available: {fest.get('FestName')}")
                    else: print(f" - Reg closed: {fest.get('FestName')}")
                except (ValueError, TypeError) as e: print(f" - Skipping (bad reg end time: {e}): {fest.get('FestName')}")
            else: print(f" - Skipping (not pub/no reg end): {fest.get('FestName')}")
        print(f"LiveEvents: Found {len(available_fests)} open for registration.")
        return render_template('live_events.html', fests=available_fests)
    except Exception as e: print(f"ERROR: Fetching live events: {e}"); traceback.print_exc(); flash("Load events error.", "danger"); return render_template('live_events.html', fests=[])

@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    try:
        _ , _, _, fests_sheet = get_master_sheet_tabs()
        all_fests_data = fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id_param), None) # Find by ID regardless of time/published status for direct link access
        if not fest_info: flash("Event not found.", "warning"); return redirect(url_for('live_events'))
        
        # Add check: Is registration still open?
        is_open = False
        reg_end_str = fest_info.get('RegistrationEndTime', '')
        is_published = str(fest_info.get('Published', '')).strip().lower() == 'yes'
        if is_published and reg_end_str:
            try: is_open = datetime.now() < datetime.strptime(reg_end_str, DATETIME_SHEET_FORMAT)
            except (ValueError, TypeError): is_open = False # Treat invalid format as closed
            
        return render_template('event_detail.html', fest=fest_info, registration_open=is_open)
    except Exception as e: print(f"ERROR: Event detail FestID {fest_id_param}: {e}"); traceback.print_exc(); flash("Load event details error.", "danger"); return redirect(url_for('live_events'))

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param):
    name = request.form.get('name','').strip(); email = request.form.get('email','').strip().lower(); mobile = request.form.get('mobile','').strip(); college = request.form.get('college','').strip()
    print(f"JoinEvent POST: FestID={fest_id_param}, Email='{email}'")
    if not all([name, email, mobile, college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))
    if "@" not in email or "." not in email: flash("Invalid email format.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))
    
    individual_sheet_title = "N/A"
    try:
        # Check if event exists and registration is still open BEFORE attempting sheet ops
        client, _, _, fests_master_sheet = get_master_sheet_tabs()
        all_fests_data = fests_master_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id_param), None)
        if not fest_info: flash("Cannot join: Event not found.", "danger"); return redirect(url_for('live_events'))
        if str(fest_info.get('Published','')).strip().lower() != 'yes': flash("Cannot join: Event not published.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))
        reg_end_str = fest_info.get('RegistrationEndTime', '')
        try:
            if not reg_end_str or datetime.now() >= datetime.strptime(reg_end_str, DATETIME_SHEET_FORMAT):
                flash("Sorry, registration for this event has closed.", "warning")
                return redirect(url_for('event_detail', fest_id_param=fest_id_param))
        except (ValueError, TypeError):
             flash("Cannot join due to event time configuration error.", "danger")
             return redirect(url_for('event_detail', fest_id_param=fest_id_param))

        # If registration open, proceed with sheet operations
        safe_base = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip()
        if not safe_base: safe_base = "fest_event"
        individual_sheet_title = f"{safe_base[:80]}_{fest_info['FestID']}"
        print(f"JoinEvent: Accessing SS '{individual_sheet_title}'.")
        event_sheet_headers = ['UniqueID', 'Name', 'Email', 'Mobile', 'College', 'Present', 'Timestamp']
        registrations_sheet = get_or_create_worksheet(client, individual_sheet_title, "Registrations", event_sheet_headers)
        if not isinstance(registrations_sheet, gspread.worksheet.Worksheet): raise Exception("Reg sheet unavailable.")

        print(f"JoinEvent: Checking existing: '{email}'...")
        if registrations_sheet.findall(email, in_column=3): # Email Col C=3
            flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))
            
        user_unique_id=generate_unique_id(); timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_row = [user_unique_id, name, email, mobile, college, 'no', timestamp]
        print(f"JoinEvent: Appending: {new_row}")
        registrations_sheet.append_row(new_row); print("JoinEvent: Append successful.")

        qr_data=f"UniqueID:{user_unique_id},FestID:{fest_info['FestID']},Name:{name[:20]}"
        img=qrcode.make(qr_data); buffered=BytesIO(); img.save(buffered, format="PNG"); img_str=base64.b64encode(buffered.getvalue()).decode()
        flash(f"Successfully joined '{fest_info.get('FestName')}'!", "success"); return render_template('join_success.html', qr_image=img_str, fest_name=fest_info.get('FestName','Event'), user_name=name)
    except Exception as e: print(f"ERROR: Join event failed (FestID {fest_id_param}): {e}"); traceback.print_exc(); flash("Error during registration.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))

# === Security Routes === (Largely unchanged, ensure sheet titles are correct)
@app.route('/security/login', methods=['GET', 'POST'])
def security_login():
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower(); event_name_password = request.form.get('password','').strip()
        if not username or not event_name_password: flash("All fields required.", "danger"); return render_template('security_login.html')
        if username == 'security':
            try:
                _ , _, _, fests_sheet = get_master_sheet_tabs()
                all_fests_data = fests_sheet.get_all_records()
                valid_event = next((f for f in all_fests_data if str(f.get('FestName','')) == event_name_password and str(f.get('Published','')).strip().lower() == 'yes'), None)
                if valid_event:
                    session['security_event_name'] = valid_event.get('FestName', 'N/A'); session['security_event_id'] = valid_event.get('FestID', 'N/A')
                    safe_base = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(valid_event.get('FestName','Event'))).strip()
                    if not safe_base: safe_base = "fest_event"
                    session['security_event_sheet_title'] = f"{safe_base[:80]}_{valid_event.get('FestID', '')}"
                    flash(f"Security access for: {session['security_event_name']}", "success"); return redirect(url_for('security_scanner'))
                else: flash("Invalid event password or event inactive.", "danger")
            except Exception as e: print(f"ERROR: Security login failed: {e}"); traceback.print_exc(); flash("Security login error.", "danger")
        else: flash("Invalid security username.", "danger")
    return render_template('security_login.html')

@app.route('/security/logout')
def security_logout():
    session.pop('security_event_name', None); session.pop('security_event_id', None); session.pop('security_event_sheet_title', None)
    flash("Security session ended.", "info"); return redirect(url_for('security_login'))

@app.route('/security/scanner')
def security_scanner():
    if 'security_event_sheet_title' not in session: flash("Please login as security.", "warning"); return redirect(url_for('security_login'))
    return render_template('security_scanner.html', event_name=session.get('security_event_name',"Event"))

@app.route('/security/verify_qr', methods=['POST'])
def verify_qr():
    if 'security_event_sheet_title' not in session or 'security_event_id' not in session: return jsonify({'status': 'error', 'message': 'Security session invalid.'}), 401
    data = request.get_json()
    if not data or 'qr_data' not in data: return jsonify({'status': 'error', 'message': 'No QR data.'}), 400
    qr_content = data.get('qr_data'); print(f"SecurityVerify POST: QR={qr_content}")
    try:
        parsed_data={}; scanned_unique_id=None; scanned_fest_id=None
        for item in qr_content.split(','):
            if ':' in item: key, value = item.split(':', 1); parsed_data[key.strip()] = value.strip()
        scanned_unique_id = parsed_data.get('UniqueID'); scanned_fest_id = parsed_data.get('FestID')
    except Exception as e: print(f"ERROR parsing QR '{qr_content}': {e}"); return jsonify({'status': 'error', 'message': 'Invalid QR format.'}), 400
    if not scanned_unique_id or not scanned_fest_id: return jsonify({'status': 'error', 'message': 'QR missing data.'}), 400
    if scanned_fest_id != session.get('security_event_id'): return jsonify({'status': 'error', 'message': 'QR for wrong event.'}), 400

    try:
        client = get_gspread_client(); individual_sheet_title = session['security_event_sheet_title']
        print(f"SecurityVerify: Checking SS '{individual_sheet_title}' for UID '{scanned_unique_id}'")
        event_sheet_headers = ['UniqueID', 'Name', 'Email', 'Mobile', 'College', 'Present', 'Timestamp']
        registrations_sheet = get_or_create_worksheet(client, individual_sheet_title, "Registrations", event_sheet_headers)
        cell = registrations_sheet.find(scanned_unique_id, in_column=1)
        if cell:
            user_details_row = registrations_sheet.row_values(cell.row)
            p_idx, n_idx, e_idx, m_idx = 5, 1, 2, 3
            def get_val(idx): return user_details_row[idx] if len(user_details_row)>idx else ''
            status, name, email, mobile = get_val(p_idx), get_val(n_idx), get_val(e_idx), get_val(m_idx)
            if str(status).strip().lower() == 'yes':
                print(f"SecurityVerify: Already present: {name}"); return jsonify({'status': 'warning', 'message': 'ALREADY SCANNED!', 'name': name,'details': f"{email}, {mobile}"})
            print(f"SecurityVerify: Marking present: {name}"); registrations_sheet.update_cell(cell.row, p_idx + 1, 'yes')
            return jsonify({'status': 'success', 'message': 'Access Granted!', 'name': name, 'details': f"{email}, {mobile}"})
        else: print(f"SecurityVerify ERROR: UID '{scanned_unique_id}' not found."); return jsonify({'status': 'error', 'message': 'Participant not found.'}), 404
    except Exception as e: print(f"ERROR during QR verification: {e}"); traceback.print_exc(); return jsonify({'status': 'error', 'message': 'Verification error.'}), 500

# --- Initialization Function ---
def initialize_master_sheets_and_tabs():
    print("\n----- Initializing Master Sheets & Tabs -----")
    try:
        client, spreadsheet, _, _ = get_master_sheet_tabs()
        print(f"Check PASSED: Master SS '{MASTER_SHEET_NAME}' ready.")
        share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"CRITICAL INIT ERROR: {e}"); traceback.print_exc()
    print("----- Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
         print("Flask starting up - Main process.")
         # Initialize only in the main process, not the reloader.
         initialize_master_sheets_and_tabs() 
         
    print("Starting Flask development server...")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=True) # Enable reloader explicitly if needed