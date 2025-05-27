# -*- coding: utf-8 -*-
# Standard library imports
import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone # Added timezone for UTC
try:
    from zoneinfo import ZoneInfo # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo # Fallback for older Python

from io import BytesIO as PythonBytesIO # Aliased
from operator import itemgetter
import os
import traceback
import uuid

# Third-party library imports
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, flash, send_file, make_response
)
from fpdf import FPDF
import gspread
from oauth2client.service_account import ServiceAccountCredentials as GSpreadServiceAccountCredentials
import pandas as pd
import qrcode

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    print("🔴 FATAL: FLASK_SECRET_KEY is not set. Using a temporary key for local dev, but this WILL FAIL in production or if app.debug is False.")
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        raise ValueError("FLASK_SECRET_KEY is not set in the environment. This is required for production.")
    app.secret_key = "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV"

# --- Timezone Definitions ---
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")

# --- Google Setup ---
SCOPE_GSPREAD_CLIENT_DEFAULT = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file']
MASTER_SHEET_NAME = os.environ.get("MASTER_SHEET_NAME", 'event management')
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID")
YOUR_PERSONAL_EMAIL = os.environ.get("YOUR_PERSONAL_SHARE_EMAIL")

# --- Constants ---
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M:%S%z'
DATETIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M'
DATETIME_INPUT_FORMATS = ['%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']

# --- Global Variables for Google Services ---
gspread_client_global = None
master_spreadsheet_obj_global = None
clubs_sheet_obj_global = None # This will now be for pre-defined admin logins
fests_sheet_obj_global = None

_cached_fests_data_all = None
_cache_fests_timestamp_all = None
CACHE_FESTS_DURATION = timedelta(minutes=5)

# --- Helper Functions ---
def get_current_utc_time():
    return datetime.now(UTC)

def parse_datetime_from_form_to_utc(dt_str):
    if not dt_str: return None
    for fmt in DATETIME_INPUT_FORMATS:
        try:
            naive_dt = datetime.strptime(str(dt_str).strip(), fmt)
            aware_ist_dt = naive_dt.astimezone(IST) if naive_dt.tzinfo else IST.localize(naive_dt)
            return aware_ist_dt.astimezone(UTC)
        except (ValueError, TypeError):
            continue
    print(f"Warning: Could not parse form datetime string '{dt_str}' to UTC with IST assumption.")
    return None

def parse_datetime_from_sheet(dt_str_utc):
    if not dt_str_utc: return None
    try:
        if '%z' in DATETIME_SHEET_FORMAT:
            dt = datetime.strptime(str(dt_str_utc).strip(), DATETIME_SHEET_FORMAT)
            return dt.astimezone(UTC)
    except ValueError:
        pass
    for fmt in DATETIME_INPUT_FORMATS + [DATETIME_SHEET_FORMAT.replace('%z','')]:
        try:
            naive_dt = datetime.strptime(str(dt_str_utc).strip(), fmt)
            return naive_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
    print(f"Warning: Could not parse datetime string from sheet: '{dt_str_utc}'")
    return None

def get_google_creds_dict_from_env():
    expected_keys_map = { "type": "GOOGLE_TYPE", "project_id": "GOOGLE_PROJECT_ID", "private_key_id": "GOOGLE_PRIVATE_KEY_ID", "private_key": "GOOGLE_PRIVATE_KEY", "client_email": "GOOGLE_CLIENT_EMAIL", "client_id": "GOOGLE_CLIENT_ID", "auth_uri": "GOOGLE_AUTH_URI", "token_uri": "GOOGLE_TOKEN_URI", "auth_provider_x509_cert_url": "GOOGLE_AUTH_PROVIDER_X509_CERT_URL", "client_x509_cert_url": "GOOGLE_CLIENT_X509_CERT_URL" }
    creds_dict = {}
    missing_vars = [env_var for _, env_var in expected_keys_map.items() if not os.environ.get(env_var)]
    if missing_vars: raise ValueError(f"Missing Google credentials environment variables: {', '.join(missing_vars)}")
    for key, env_var_name in expected_keys_map.items(): creds_dict[key] = os.environ.get(env_var_name)
    creds_dict['private_key'] = creds_dict['private_key'].replace('\\n', '\n')
    return creds_dict

def _initialize_gspread_client_internal():
    global gspread_client_global
    if gspread_client_global: return gspread_client_global
    print("Initializing gspread client from environment variables (one-time per worker)...")
    try:
        creds_dict = get_google_creds_dict_from_env()
        creds = GSpreadServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE_GSPREAD_CLIENT_DEFAULT)
        gspread_client_global = gspread.authorize(creds)
        return gspread_client_global
    except Exception as e: print(f"CRITICAL ERROR initializing gspread client: {e}"); traceback.print_exc(); raise

def get_gspread_client_cached(): return _initialize_gspread_client_internal()

def _initialize_master_sheets_internal():
    global master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global
    if master_spreadsheet_obj_global and clubs_sheet_obj_global and fests_sheet_obj_global:
        return get_gspread_client_cached(), master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global

    print("Initializing master sheet objects (one-time per worker)...")
    client = get_gspread_client_cached()
    spreadsheet = None
    # ... (logic to open or create master_spreadsheet_obj_global as before) ...
    if MASTER_SHEET_ID:
        try:
            print(f"Opening master SS by ID (key): '{MASTER_SHEET_ID}'"); spreadsheet = client.open_by_key(MASTER_SHEET_ID)
        except Exception as e_id: print(f"WARN: Could not open master SS by ID '{MASTER_SHEET_ID}': {e_id}. Will try by name."); spreadsheet = None
    if not spreadsheet:
        try:
            print(f"Attempting to open master SS by name: '{MASTER_SHEET_NAME}'"); spreadsheet = client.open(MASTER_SHEET_NAME)
        except gspread.exceptions.SpreadsheetNotFound:
            print(f"Master SS '{MASTER_SHEET_NAME}' not found by name. Creating...");
            try:
                spreadsheet = client.create(MASTER_SHEET_NAME);
                if YOUR_PERSONAL_EMAIL: share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
            except Exception as e_create: print(f"CRITICAL ERROR creating master SS: {e_create}"); traceback.print_exc(); raise
        except Exception as e_name: print(f"CRITICAL ERROR opening master SS by name '{MASTER_SHEET_NAME}': {e_name}"); traceback.print_exc(); raise
    if not spreadsheet: raise Exception("FATAL: Failed to open or create master spreadsheet.")
    master_spreadsheet_obj_global = spreadsheet


    # "Clubs" sheet is now for predefined admin logins
    clubs_headers=['ClubID','ClubName','Email','PasswordHash'] # ClubName can be admin's display name
    fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests', 'FestImageLink']
    
    try: clubs_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Clubs")
    except gspread.exceptions.WorksheetNotFound: 
        clubs_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Clubs",rows=1,cols=len(clubs_headers)); 
        clubs_sheet_obj_global.append_row(clubs_headers); 
        clubs_sheet_obj_global.resize(rows=100) # Or a smaller default size
        print("Created 'Clubs' sheet for admin logins. Please populate manually.")
    if (clubs_sheet_obj_global.row_values(1) if clubs_sheet_obj_global.row_count >=1 else []) != clubs_headers: 
        print("WARN: 'Clubs' sheet headers mismatch! Expected:", clubs_headers)

    try: fests_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Fests")
    except gspread.exceptions.WorksheetNotFound: 
        fests_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Fests",rows=1,cols=len(fests_headers)); 
        fests_sheet_obj_global.append_row(fests_headers); 
        fests_sheet_obj_global.resize(rows=100)
    current_fests_headers = fests_sheet_obj_global.row_values(1) if fests_sheet_obj_global.row_count >= 1 else []
    if not current_fests_headers:
        if fests_sheet_obj_global.col_count < len(fests_headers): fests_sheet_obj_global.add_cols(len(fests_headers) - fests_sheet_obj_global.col_count)
        if fests_sheet_obj_global.row_count > 0 and fests_sheet_obj_global.get_all_values(): fests_sheet_obj_global.clear()
        fests_sheet_obj_global.append_row(fests_headers); print("Appended headers to Fests sheet.")
    elif current_fests_headers != fests_headers : 
        print(f"WARN: Fests headers differ. Current in Sheet: {current_fests_headers}, Expected in Code: {fests_headers}.")
    
    print("Master sheets initialized globally.")
    return client, master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global


# ... (get_all_fests_cached, share_spreadsheet_with_editor, get_or_create_worksheet as before)
# ... (generate_unique_id, hash_password, verify_password as before)
def get_all_fests_cached(): # Duplicated from above, ensure it's defined once
    global _cached_fests_data_all, _cache_fests_timestamp_all; now_utc_cache = get_current_utc_time()
    if _cached_fests_data_all and _cache_fests_timestamp_all and (now_utc_cache - _cache_fests_timestamp_all < CACHE_FESTS_DURATION):
        return _cached_fests_data_all
    try:
        _, _, _, fests_sheet = get_sheet_objects_cached()
        _cached_fests_data_all = fests_sheet.get_all_records()
        _cache_fests_timestamp_all = now_utc_cache
    except Exception as e:
        print(f"ERROR fetching all fests: {e}. Returning last cache or empty list.");
        return _cached_fests_data_all if _cached_fests_data_all is not None else []
    return _cached_fests_data_all

def share_spreadsheet_with_editor(spreadsheet, email_address, sheet_title):
    if not email_address or "@" not in email_address: return False
    if not hasattr(spreadsheet, 'list_permissions') or not hasattr(spreadsheet, 'share'): return False
    try:
        perms = spreadsheet.list_permissions(); shared = False
        for p in perms:
            if p.get('type')=='user' and p.get('emailAddress')==email_address:
                if p.get('role') in ['owner', 'writer']: shared = True; break
                else: spreadsheet.share(email_address, perm_type='user', role='writer', notify=False); shared = True; break
        if not shared: spreadsheet.share(email_address, perm_type='user', role='writer', notify=False)
        print(f"Sharing ensured for '{sheet_title}' with {email_address}.")
        return True
    except Exception as share_e: print(f"WARN: Share error for '{sheet_title}' with {email_address}: {share_e}"); return False

def get_or_create_worksheet(client_param, spreadsheet_title_or_obj, worksheet_title, headers=None):
    spreadsheet_obj = None; worksheet = None; headers = headers or []; ws_created_now = False
    try:
        if isinstance(spreadsheet_title_or_obj, gspread.Spreadsheet): spreadsheet_obj = spreadsheet_title_or_obj
        else: spreadsheet_obj = client_param.open(spreadsheet_title_or_obj)
    except gspread.exceptions.SpreadsheetNotFound:
        spreadsheet_obj = client_param.create(spreadsheet_title_or_obj)
        print(f"Created SS '{spreadsheet_obj.title}'.");
        if YOUR_PERSONAL_EMAIL: share_spreadsheet_with_editor(spreadsheet_obj, YOUR_PERSONAL_EMAIL, spreadsheet_obj.title)
    except Exception as e: print(f"ERROR getting SS '{spreadsheet_title_or_obj}': {e}"); raise
    if not spreadsheet_obj: raise Exception(f"Failed to get spreadsheet handle for '{spreadsheet_title_or_obj}'.")
    try: worksheet = spreadsheet_obj.worksheet(worksheet_title)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet_obj.add_worksheet(title=worksheet_title, rows=1, cols=(len(headers) if headers else 10)); ws_created_now = True
    except Exception as e: print(f"ERROR getting WS '{worksheet_title}': {e}"); raise
    if not worksheet: raise Exception(f"Failed to get worksheet handle for '{worksheet_title}'.")
    try:
        first_row = worksheet.row_values(1) if not ws_created_now and worksheet.row_count >= 1 else []
        if headers and (ws_created_now or not first_row): worksheet.append_row(headers); worksheet.resize(rows=500);
        elif headers and first_row != headers: print(f"WARN: Headers mismatch for '{worksheet_title}'. Sheet: {first_row}, Expected: {headers}")
    except Exception as hdr_e: print(f"ERROR header logic for '{worksheet_title}': {hdr_e}")
    return worksheet

def generate_unique_id(): return str(uuid.uuid4().hex)[:10]
def hash_password(password): return password
def verify_password(hashed, provided): return hashed == provided

@app.context_processor
def inject_template_helpers():
    now_utc = get_current_utc_time()
    now_ist = now_utc.astimezone(IST)
    return {'now_utc': now_utc, 'now_ist': now_ist, 'IST': IST, 'UTC': UTC, 'DATETIME_DISPLAY_FORMAT': DATETIME_DISPLAY_FORMAT}

@app.route('/')
def index(): return render_template('index.html')

# === Admin/Club Routes ===
# NO /club/register route anymore

@app.route('/club/login', methods=['GET', 'POST']) # Renamed from club_login for clarity if it's general admin
def admin_login(): # Or keep as club_login if "Club" concept remains for logged-in users
    if request.method == 'POST':
        email_form = request.form.get('email','').strip().lower(); password_form = request.form.get('password','')
        if not email_form or not password_form: flash("Email/password required.", "danger"); return render_template('club_login.html') # Or admin_login.html
        if "@" not in email_form or "." not in email_form.split('@')[-1]: flash("Invalid email.", "danger"); return render_template('club_login.html')
        try: _, _, clubs_sheet, _ = get_sheet_objects_cached() # This sheet now contains admin users
        except Exception as e: print(f"ERROR LOGIN Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_login.html')
        try: cell = clubs_sheet.find(email_form, in_column=3) # Find by email
        except gspread.exceptions.CellNotFound: flash("Invalid email or password.", "danger"); return render_template('club_login.html')
        if cell:
            try:
                admin_data=clubs_sheet.row_values(cell.row)
                if len(admin_data) < 4: flash("Login error: Incomplete data.", "danger"); return render_template('club_login.html')
                stored_admin_id, admin_name, stored_email, stored_hash = admin_data[0].strip(), admin_data[1].strip(), admin_data[2].strip().lower(), admin_data[3].strip()
                # Here, ClubID and ClubName from the sheet are treated as AdminID and AdminName
                if stored_email != email_form: flash("Internal login error.", "danger"); return render_template('club_login.html')
                if verify_password(stored_hash, password_form):
                    session['club_id']=stored_admin_id # Still use 'club_id' in session for consistency with other routes
                    session['club_name']=admin_name   # Or rename session keys to 'admin_id', 'admin_name' if you refactor everywhere
                    flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
                else: flash("Invalid email or password.", "danger")
            except Exception as e: print(f"ERROR LOGIN Logic: {e}"); traceback.print_exc(); flash("Login logic error.", "danger")
        else: flash("Invalid email or password.", "danger")
    return render_template('club_login.html') # Ensure this template exists and doesn't link to register

@app.route('/club/logout') # Or admin_logout
def admin_logout():
    session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))

# All routes below this that use `session['club_id']` and `session['club_name']`
# will continue to work, assuming the "Clubs" sheet is populated with valid admin users.
# The concept of "Club" effectively becomes "Admin User" or "Event Manager".

# ... (create_fest, club_dashboard, club_history, edit_fest, end_fest, delete_fest, fest_stats, export_excel, export_pdf as in the previous full code)
# Ensure these routes use the correct session keys ('club_id', 'club_name')
# and that their logic is appropriate for a general admin rather than a specific "club" if that distinction matters.
# For now, keeping them as /club/... and using session['club_id'] is fine for minimal changes.

# --- create_fest (no changes needed in its internal logic other than using session['club_id'] as admin's ID) ---
@app.route('/club/create_fest', methods=['GET', 'POST'])
def create_fest():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('admin_login')) # Changed to admin_login
    form_data_to_pass = request.form.to_dict() if request.method == 'POST' else {}
    if request.method == 'POST':
        fest_name = request.form.get('fest_name', '').strip()
        start_time_str = request.form.get('start_time', '')
        end_time_str = request.form.get('end_time', '')
        reg_end_time_str = request.form.get('registration_end_time', '')
        fest_details, fest_venue, fest_guests = request.form.get('fest_details', '').strip(), request.form.get('fest_venue', '').strip(), request.form.get('fest_guests', '').strip()
        fest_image_link = request.form.get('fest_image_link', '').strip()
        is_published = 'yes' if request.form.get('publish_fest') == 'yes' else 'no'
        
        required = {'Fest Name': fest_name, 'Start Time': start_time_str, 'End Time': end_time_str, 'Registration Deadline': reg_end_time_str, 'Details': fest_details}
        missing = [name for name, val in required.items() if not val]
        if missing: flash(f"Missing: {', '.join(missing)}", "danger"); return render_template('create_fest.html',form_data=form_data_to_pass)
        
        start_dt_utc = parse_datetime_from_form_to_utc(start_time_str)
        end_dt_utc = parse_datetime_from_form_to_utc(end_time_str)
        reg_end_dt_utc = parse_datetime_from_form_to_utc(reg_end_time_str)

        if not all([start_dt_utc, end_dt_utc, reg_end_dt_utc]): 
            flash("Invalid date/time format for one or more times.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        if not (start_dt_utc < end_dt_utc and reg_end_dt_utc <= start_dt_utc): 
            flash("Invalid times: Start must be before End, and Reg Deadline before or at Start (all times considered in UTC).", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        
        try:
            g_client, _, _, master_fests_sheet = get_sheet_objects_cached(); fest_id=generate_unique_id();
            new_fest_row=[ fest_id, fest_name, session['club_id'], session.get('club_name','N/A'), 
                           start_dt_utc.strftime(DATETIME_SHEET_FORMAT), 
                           end_dt_utc.strftime(DATETIME_SHEET_FORMAT), 
                           reg_end_dt_utc.strftime(DATETIME_SHEET_FORMAT), 
                           fest_details, is_published, fest_venue, fest_guests, fest_image_link ];
            master_fests_sheet.append_row(new_fest_row);
            _cached_fests_data_all = None; _cache_fests_timestamp_all = None;
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
            get_or_create_worksheet(g_client, safe_sheet_title, "Registrations", event_headers);
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e: print(f"ERROR: Create Fest write: {e}"); traceback.print_exc(); flash("DB write error.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})

# --- club_dashboard, club_history, and other /club/fest/* routes remain largely the same, ---
# --- just ensure they redirect to `admin_login` if session is invalid. ---
@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('admin_login'))
    # ... (rest of the dashboard logic from previous full code, using parse_datetime_from_sheet)
    now_utc_dash = get_current_utc_time(); upcoming,ongoing = [],[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: flash("DB Error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name'), upcoming_fests=[], ongoing_fests=[])
    club_fests_all=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']] # ClubID here is AdminID
    for fest_dict in club_fests_all:
        try:
            start_time_utc = parse_datetime_from_sheet(fest_dict.get('StartTime'))
            end_time_utc = parse_datetime_from_sheet(fest_dict.get('EndTime'))
            fest_dict_copy = fest_dict.copy()
            fest_dict_copy['StartTimeParsed'] = start_time_utc
            fest_dict_copy['EndTimeParsed'] = end_time_utc
            if not (start_time_utc and end_time_utc): continue
            if now_utc_dash < start_time_utc: upcoming.append(fest_dict_copy)
            elif start_time_utc <= now_utc_dash < end_time_utc: ongoing.append(fest_dict_copy)
        except Exception as e: print(f"Error processing fest '{fest_dict.get('FestName')}' for dashboard: {e}")
    upcoming.sort(key=lambda x: x.get('StartTimeParsed') or datetime.max.replace(tzinfo=UTC))
    ongoing.sort(key=lambda x: x.get('StartTimeParsed') or datetime.min.replace(tzinfo=UTC))
    return render_template('club_dashboard.html',club_name=session.get('club_name'), upcoming_fests=upcoming, ongoing_fests=ongoing)

@app.route('/club/history')
def club_history():
     if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('admin_login'))
     # ... (rest of the history logic from previous full code, using parse_datetime_from_sheet)
     now_utc_hist = get_current_utc_time(); past_fests_all=[]
     try: all_fests_data = get_all_fests_cached()
     except Exception as e: flash("DB Error.", "danger"); return render_template('club_history.html', club_name=session.get('club_name'), past_fests=[])
     club_fests_for_history=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
     for fest_dict in club_fests_for_history:
        try:
            end_time_utc = parse_datetime_from_sheet(fest_dict.get('EndTime', ''))
            fest_dict_copy = fest_dict.copy()
            fest_dict_copy['EndTimeParsed'] = end_time_utc
            if not end_time_utc: continue
            if now_utc_hist >= end_time_utc: past_fests_all.append(fest_dict_copy)
        except Exception as e: print(f"Error processing fest '{fest_dict.get('FestName')}' for history: {e}")
     past_fests_all.sort(key=lambda x: x.get('EndTimeParsed') or datetime.min.replace(tzinfo=UTC), reverse=True)
     return render_template('club_history.html',club_name=session.get('club_name'), past_fests=past_fests_all)

# ... (edit_fest, end_fest, delete_fest, fest_stats, export_excel, export_pdf as in the previous full code,
# ensuring they redirect to 'admin_login' on session failure and use timezone-aware datetimes)

# For example, end_fest:
@app.route('/club/fest/<fest_id>/end', methods=['POST'])
def end_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('admin_login'))
    # ... (rest of the function remains same as your last full code, using DATETIME_SHEET_FORMAT for now_utc_str)
    try:
        _, _, _, fests_sheet = get_sheet_objects_cached()
        all_fests_data = get_all_fests_cached() 
        fest_info = next((f for f in all_fests_data if str(f.get('FestID', '')) == fest_id), None)
        if not fest_info: flash("Fest to end not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID', '')) != session['club_id']: flash("Permission denied.", "danger"); return redirect(url_for('club_dashboard'))
        fest_cell = fests_sheet.find(fest_id, in_column=1)
        if not fest_cell: flash("Fest to end not found in sheet (cell).", "danger"); return redirect(url_for('club_dashboard'))
        fest_row_index = fest_cell.row; header_row = fests_sheet.row_values(1) 
        try:
            end_time_col_idx = header_row.index('EndTime') + 1
            published_col_idx = header_row.index('Published') + 1
        except ValueError: flash("Sheet structure error.", "danger"); return redirect(url_for('club_dashboard'))
        now_utc_str = get_current_utc_time().strftime(DATETIME_SHEET_FORMAT)
        updates = [{'range': gspread.utils.rowcol_to_a1(fest_row_index, end_time_col_idx), 'values': [[now_utc_str]]},
                   {'range': gspread.utils.rowcol_to_a1(fest_row_index, published_col_idx), 'values': [['no']]}]
        fests_sheet.batch_update(updates)
        _cached_fests_data_all = None; _cache_fests_timestamp_all = None
        flash(f"Fest '{fest_info.get('FestName', fest_id)}' ended & unpublished.", "success")
    except Exception as e: print(f"ERROR ending fest {fest_id}: {e}"); traceback.print_exc(); flash("Error ending event.", "danger")
    return redirect(url_for('club_dashboard'))


# === Attendee Routes (No changes needed here due to this modification) ===
# ... (live_events, event_detail, join_event as in the previous full code) ...
@app.route('/events')
def live_events():
    now_utc_live = get_current_utc_time(); available_fests=[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    for fest_dict in all_fests_data:
        is_published=str(fest_dict.get('Published','')).strip().lower()=='yes'
        reg_end_time_utc = parse_datetime_from_sheet(fest_dict.get('RegistrationEndTime',''))
        start_time_utc = parse_datetime_from_sheet(fest_dict.get('StartTime',''))
        fest_dict_copy = fest_dict.copy()
        fest_dict_copy['StartTimeParsed'] = start_time_utc
        if is_published and reg_end_time_utc and start_time_utc and now_utc_live < reg_end_time_utc and now_utc_live < start_time_utc :
            available_fests.append(fest_dict_copy)
    available_fests.sort(key=lambda x: x.get('StartTimeParsed') or datetime.max.replace(tzinfo=UTC))
    return render_template('live_events.html', fests=available_fests)

@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info_dict=None; is_open_for_reg=False; now_utc_detail = get_current_utc_time()
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: flash("DB Error.", "danger"); return redirect(url_for('live_events'))
    fest_info_raw = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id_param), None)
    if not fest_info_raw: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    fest_info_dict = fest_info_raw.copy()
    fest_info_dict['StartTimeParsed'] = parse_datetime_from_sheet(fest_info_dict.get('StartTime'))
    fest_info_dict['EndTimeParsed'] = parse_datetime_from_sheet(fest_info_dict.get('EndTime'))
    fest_info_dict['RegistrationEndTimeParsed'] = parse_datetime_from_sheet(fest_info_dict.get('RegistrationEndTime'))
    is_published = str(fest_info_dict.get('Published','')).lower()=='yes'
    reg_end_time_utc = fest_info_dict['RegistrationEndTimeParsed']
    start_time_utc = fest_info_dict['StartTimeParsed']
    if is_published and reg_end_time_utc and start_time_utc and now_utc_detail < reg_end_time_utc and now_utc_detail < start_time_utc : 
        is_open_for_reg=True
    return render_template('event_detail.html', fest=fest_info_dict, registration_open=is_open_for_reg)

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param):
    # ... (Same as previous full code, uses parse_datetime_from_sheet and get_current_utc_time)
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); mobile=request.form.get('mobile','').strip(); college=request.form.get('college','').strip();
    if not all([name,email,mobile,college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    if "@" not in email or "." not in email.split('@')[-1]: flash("Invalid email.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    now_utc_join = get_current_utc_time()
    try:
        g_client, _, _, _ = get_sheet_objects_cached(); all_fests=get_all_fests_cached()
        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==fest_id_param), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        reg_end_time_utc = parse_datetime_from_sheet(fest_info.get('RegistrationEndTime', ''))
        start_time_utc = parse_datetime_from_sheet(fest_info.get('StartTime', ''))
        if not reg_end_time_utc or not start_time_utc or now_utc_join >= reg_end_time_utc or now_utc_join >= start_time_utc:
            flash("Registration closed or event has already started.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
        reg_sheet=get_or_create_worksheet(g_client, individual_sheet_title,"Registrations",event_headers);
        if reg_sheet.findall(email, in_column=3):
            flash(f"You are already registered for '{fest_info.get('FestName')}' with this email.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        user_id=generate_unique_id(); ts_for_sheet = get_current_utc_time().strftime(DATETIME_SHEET_FORMAT);
        row=[user_id, name, email, mobile, college, 'no', ts_for_sheet]; reg_sheet.append_row(row);
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20].replace(',',';')}"; img_qr_obj=qrcode.make(qr_data);
        qr_image_io = PythonBytesIO(); img_qr_obj.save(qr_image_io, format="PNG"); qr_image_io.seek(0);
        qr_image_base64 = base64.b64encode(qr_image_io.getvalue()).decode('utf-8'); qr_image_data_url = f"data:image/png;base64,{qr_image_base64}";
        fest_name_for_display = fest_info.get('FestName', 'Event'); safe_fest_name_file = "".join(c if c.isalnum() else "_" for c in fest_name_for_display);
        download_filename = f"{safe_fest_name_file}_QR_{user_id}.png";
        flash(f"Successfully registered for '{fest_name_for_display}'! Your QR code is shown below and should download automatically.", "success")
        return render_template('join_success.html', fest_name=fest_name_for_display, user_name=name, qr_image_data_url=qr_image_data_url, download_filename=download_filename)
    except gspread.exceptions.SpreadsheetNotFound: flash("Registration error: Event data sheet missing.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))
    except Exception as e: print(f"ERROR JoinEvent: {e}"); traceback.print_exc(); flash("An unexpected registration error occurred.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));

# === Security Routes (No changes needed here due to this modification, but ensure admin_login is used if they get logged out) ===
# ... (security_login, security_logout, security_scanner, security_verify_qr as in the previous full code) ...
# Make sure that security_verify_qr uses parse_datetime_from_sheet and get_current_utc_time correctly
# and formats response timestamps to IST.
@app.route('/security/login', methods=['GET', 'POST'])
def security_login():
    # ... (Same as previous full code, uses parse_datetime_from_sheet and get_current_utc_time)
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower(); event_name_password = request.form.get('password','').strip()
        if not username or not event_name_password: flash("All fields required.", "danger"); return render_template('security_login.html')
        if username == 'security':
            try:
                all_fests_data = get_all_fests_cached();
                if all_fests_data is None: all_fests_data = [] 
                valid_event = next((f for f in all_fests_data if str(f.get('FestName','')).strip() == event_name_password and str(f.get('Published','')).strip().lower() == 'yes'), None)
                if valid_event:
                    event_end_dt_utc = parse_datetime_from_sheet(valid_event.get('EndTime')); now_utc_sec = get_current_utc_time()
                    if event_end_dt_utc and now_utc_sec >= event_end_dt_utc:
                        flash(f"Event '{valid_event.get('FestName')}' ended at {event_end_dt_utc.astimezone(IST).strftime(DATETIME_DISPLAY_FORMAT + ' %Z')}.", "warning"); return render_template('security_login.html') 
                    session['security_event_name'] = valid_event.get('FestName','N/A'); session['security_event_id'] = valid_event.get('FestID','N/A')
                    safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(valid_event.get('FestName','Event'))).strip();
                    if not safe_base: safe_base="fest_event"
                    session['security_event_sheet_title']=f"{safe_base[:80]}_{valid_event.get('FestID','')}"
                    flash(f"Security access for: {session['security_event_name']}", "success"); return redirect(url_for('security_scanner'))
                else: flash("Invalid event password, event inactive/unpublished, or event has ended.", "danger")
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
    # ... (Same as previous full code, uses parse_datetime_from_sheet and get_current_utc_time, formats response to IST)
    if 'security_event_sheet_title' not in session or 'security_event_id' not in session: return jsonify({'status': 'error', 'message': 'Security session invalid.'}), 401
    now_utc_verify = get_current_utc_time()
    try:
        all_fests_data = get_all_fests_cached(); current_event_id = session.get('security_event_id')
        event_info = next((f for f in all_fests_data if str(f.get('FestID','')) == current_event_id), None)
        if not event_info: return jsonify({'status': 'error', 'message': 'Event data error. Please re-login.'}), 403
        event_end_dt_utc = parse_datetime_from_sheet(event_info.get('EndTime'))
        if event_end_dt_utc and now_utc_verify >= event_end_dt_utc:
            return jsonify({'status': 'error', 'message': f"{event_info.get('FestName', 'The event')} ended. Scanning closed."}), 403
    except Exception as e_time_check: return jsonify({'status': 'error', 'message': 'Server error during event time verification.'}), 500
    data = request.get_json(); qr_content = data.get('qr_data')
    try: 
        parsed_data={};
        for item in qr_content.split(','):
            if ':' in item: key, value = item.split(':', 1); parsed_data[key.strip()] = value.strip()
        scanned_unique_id = parsed_data.get('UniqueID'); scanned_fest_id = parsed_data.get('FestID')
        if not scanned_unique_id or not scanned_fest_id: return jsonify({'status':'error', 'message':'QR missing essential data.'}), 400
        if scanned_fest_id != session.get('security_event_id'): return jsonify({'status':'error', 'message':'QR code is for a different event.'}), 400
    except Exception as e: return jsonify({'status':'error', 'message':'Invalid QR code format.'}), 400
    try:
        client = get_gspread_client_cached(); sheet_title_from_session = session['security_event_sheet_title']
        event_headers_template = ['UniqueID','Name','Email','Mobile','College','Present','Timestamp']
        reg_sheet = get_or_create_worksheet(client, sheet_title_from_session, "Registrations", event_headers_template)
        try: cell = reg_sheet.find(scanned_unique_id, in_column=1)
        except gspread.exceptions.CellNotFound: return jsonify({'status':'error', 'message':'Participant not found.'}), 404
        if not cell: return jsonify({'status':'error','message':'Participant lookup error.'}), 404;
        row_data = reg_sheet.row_values(cell.row); sheet_headers = reg_sheet.row_values(1)
        try:
            p_idx = sheet_headers.index('Present'); n_idx = sheet_headers.index('Name')
            e_idx = sheet_headers.index('Email'); m_idx = sheet_headers.index('Mobile')
            ts_idx = sheet_headers.index('Timestamp')
        except ValueError as ve: return jsonify({'status':'error', 'message':'Sheet config error.'}), 500
        def get_val(idx, default_val=''): return row_data[idx] if len(row_data) > idx else default_val
        status = get_val(p_idx).strip().lower(); name = get_val(n_idx); email = get_val(e_idx); mobile = get_val(m_idx)
        if status == 'yes':
            last_scan_time_str_utc = get_val(ts_idx, "previously")
            last_scan_dt_aware_utc = parse_datetime_from_sheet(last_scan_time_str_utc)
            last_scan_display = last_scan_dt_aware_utc.astimezone(IST).strftime(DATETIME_DISPLAY_FORMAT + " %Z") if last_scan_dt_aware_utc else "previously"
            return jsonify({'status':'warning','message':'ALREADY SCANNED!', 'name':name,'details':f"{email}, {mobile}. Scanned: {last_scan_display}"})
        current_scan_time_utc = get_current_utc_time(); scan_ts_for_sheet = current_scan_time_utc.strftime(DATETIME_SHEET_FORMAT)
        scan_ts_for_response = current_scan_time_utc.astimezone(IST).strftime(DATETIME_DISPLAY_FORMAT + " %Z")
        updates_to_perform = [
            {'range': gspread.utils.rowcol_to_a1(cell.row, p_idx + 1), 'values': [['yes']]},
            {'range': gspread.utils.rowcol_to_a1(cell.row, ts_idx + 1), 'values': [[scan_ts_for_sheet]]}
        ]
        reg_sheet.batch_update(updates_to_perform)
        return jsonify({'status':'success','message':'Access Granted!','name':name,'details':f"{email}, {mobile}. Checked-in: {scan_ts_for_response}"});
    except gspread.exceptions.SpreadsheetNotFound: return jsonify({'status':'error', 'message':f"Event registration data not found."}), 404
    except Exception as e: print(f"ERROR: Verify QR op failed: {e}"); traceback.print_exc(); return jsonify({'status':'error', 'message':'Verification server error.'}), 500


# --- Initialization Function ---
def initialize_application_on_startup():
    print("\n----- Initializing Application on Startup -----")
    try:
        client, spreadsheet, clubs_sheet, fests_sheet = get_sheet_objects_cached()
        print(f"Init Check PASSED: Master SS '{spreadsheet.title}' and its tabs ('{clubs_sheet.title}', '{fests_sheet.title}') are ready.")
        if YOUR_PERSONAL_EMAIL:
            share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, spreadsheet.title)
    except ValueError as ve_creds:
        print(f"🔴🔴🔴 FATAL STARTUP ERROR (Credentials Missing or Invalid): {ve_creds}"); traceback.print_exc()
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug: exit(1)
    except Exception as e_init:
        print(f"CRITICAL INIT ERROR during application startup: {e_init}"); traceback.print_exc()
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug: exit(1)
    print("ℹ️ INFO: Timestamps are UTC-based internally, localized to IST for display where applicable.")
    print("----- Application Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    if not MASTER_SHEET_ID: print("\n🔴 WARNING: MASTER_SHEET_ID not set. Opening master sheet will rely on name search.\n")
    if (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true") and \
       (not os.environ.get('FLASK_SECRET_KEY') or \
        os.environ.get('FLASK_SECRET_KEY') == "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV"):
        print("\n🔴 SECURITY WARNING: FLASK_SECRET_KEY is not securely set for a production-like environment.\n")
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        initialize_application_on_startup()
    else:
        print("Flask starting up - Reloader process detected. Skipping one-time initialization in this process.")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
