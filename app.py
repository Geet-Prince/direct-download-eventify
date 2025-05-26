# -*- coding: utf-8 -*-
# Standard library imports
import base64
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO # Still used for QR code and exports
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
# from werkzeug.utils import secure_filename # No longer needed

# from dotenv import load_dotenv
# load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    print("🔴 FATAL: FLASK_SECRET_KEY is not set. Using a temporary key for local dev, but this WILL FAIL in production or if app.debug is False.")
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        raise ValueError("FLASK_SECRET_KEY is not set in the environment. This is required for production.")
    app.secret_key = "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV"


# --- Google Setup ---
SCOPE_SHEETS = ['https://www.googleapis.com/auth/spreadsheets']
# SCOPE_DRIVE is removed as Drive API is not used directly by this app version
SCOPE_GSPREAD_CLIENT_DEFAULT = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file'] # For gspread to find/create sheets

MASTER_SHEET_NAME = os.environ.get("MASTER_SHEET_NAME", 'event management')
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID")
YOUR_PERSONAL_EMAIL = os.environ.get("YOUR_PERSONAL_SHARE_EMAIL")
# FEST_IMAGES_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID") # REMOVED

# --- Constants ---
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M'
DATETIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M'
DATETIME_INPUT_FORMATS = [ DATETIME_SHEET_FORMAT, DATETIME_DISPLAY_FORMAT, '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S' ]
# ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'} # REMOVED

# --- Global Variables for Google Services ---
gspread_client_global = None
# drive_service_global = None # REMOVED
master_spreadsheet_obj_global = None
clubs_sheet_obj_global = None
fests_sheet_obj_global = None

_cached_fests_data_all = None
_cache_fests_timestamp_all = None
CACHE_FESTS_DURATION = timedelta(minutes=5)

# --- Helper Functions ---
# allowed_file function REMOVED

def get_google_creds_dict_from_env():
    # (This function remains the same - it's used for gspread auth)
    expected_keys_map = { "type": "GOOGLE_TYPE", "project_id": "GOOGLE_PROJECT_ID", "private_key_id": "GOOGLE_PRIVATE_KEY_ID", "private_key": "GOOGLE_PRIVATE_KEY", "client_email": "GOOGLE_CLIENT_EMAIL", "client_id": "GOOGLE_CLIENT_ID", "auth_uri": "GOOGLE_AUTH_URI", "token_uri": "GOOGLE_TOKEN_URI", "auth_provider_x509_cert_url": "GOOGLE_AUTH_PROVIDER_X509_CERT_URL", "client_x509_cert_url": "GOOGLE_CLIENT_X509_CERT_URL" }
    creds_dict = {}
    missing_vars = [env_var for _, env_var in expected_keys_map.items() if not os.environ.get(env_var)]
    if missing_vars: raise ValueError(f"Missing Google credentials environment variables: {', '.join(missing_vars)}")
    for key, env_var_name in expected_keys_map.items(): creds_dict[key] = os.environ.get(env_var_name)
    creds_dict['private_key'] = creds_dict['private_key'].replace('\\n', '\n')
    creds_dict['universe_domain'] = os.environ.get("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com") # Used by google-auth, oauth2client ignores
    return creds_dict

def _initialize_gspread_client_internal():
    global gspread_client_global
    if gspread_client_global: return gspread_client_global
    print("Initializing gspread client from environment variables (one-time per worker)...")
    try:
        creds_dict = get_google_creds_dict_from_env()
        # If MASTER_SHEET_ID is set, SCOPE_SHEETS is enough. Otherwise, need drive.file to find by name.
        current_scope_for_gspread = SCOPE_SHEETS if MASTER_SHEET_ID else SCOPE_GSPREAD_CLIENT_DEFAULT
        creds = GSpreadServiceAccountCredentials.from_json_keyfile_dict(creds_dict, current_scope_for_gspread)
        gspread_client_global = gspread.authorize(creds)
        print(f"gspread client initialized successfully with scope: {current_scope_for_gspread}")
        return gspread_client_global
    except Exception as e: print(f"CRITICAL ERROR initializing gspread client: {e}"); traceback.print_exc(); raise

# _initialize_drive_service_internal function REMOVED
# get_drive_service_cached function REMOVED
# upload_to_drive function REMOVED

def get_gspread_client_cached(): return _initialize_gspread_client_internal()

def _initialize_master_sheets_internal():
    global master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global
    if master_spreadsheet_obj_global and clubs_sheet_obj_global and fests_sheet_obj_global:
        return get_gspread_client_cached(), master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global

    print("Initializing master sheet objects (one-time per worker)...")
    client = get_gspread_client_cached()
    spreadsheet = None
    if MASTER_SHEET_ID:
        try:
            print(f"Opening master SS by ID (key): '{MASTER_SHEET_ID}'"); spreadsheet = client.open_by_key(MASTER_SHEET_ID)
            print(f"Opened master SS: '{spreadsheet.title}' (ID: {spreadsheet.id})")
        except Exception as e_id: print(f"WARN: Could not open master SS by ID '{MASTER_SHEET_ID}': {e_id}. Will try by name."); spreadsheet = None
    
    if not spreadsheet:
        try:
            print(f"Attempting to open master SS by name: '{MASTER_SHEET_NAME}'"); spreadsheet = client.open(MASTER_SHEET_NAME)
            print(f"Opened master SS by name: '{spreadsheet.title}' (ID: {spreadsheet.id})")
        except gspread.exceptions.SpreadsheetNotFound:
            print(f"Master SS '{MASTER_SHEET_NAME}' not found by name. Creating...");
            try:
                spreadsheet = client.create(MASTER_SHEET_NAME); print(f"Created master SS '{MASTER_SHEET_NAME}'.")
                if YOUR_PERSONAL_EMAIL: share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
            except Exception as e_create: print(f"CRITICAL ERROR creating master SS: {e_create}"); traceback.print_exc(); raise
        except Exception as e_name: print(f"CRITICAL ERROR opening master SS by name '{MASTER_SHEET_NAME}': {e_name}"); traceback.print_exc(); raise
    
    if not spreadsheet: raise Exception("FATAL: Failed to open or create master spreadsheet.")
    master_spreadsheet_obj_global = spreadsheet

    clubs_headers=['ClubID','ClubName','Email','PasswordHash']
    fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests'] # FestImageLink REMOVED
    
    try: clubs_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Clubs")
    except gspread.exceptions.WorksheetNotFound: clubs_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Clubs",rows=1,cols=len(clubs_headers)); clubs_sheet_obj_global.append_row(clubs_headers); clubs_sheet_obj_global.resize(rows=100)
    if (clubs_sheet_obj_global.row_values(1) if clubs_sheet_obj_global.row_count >=1 else []) != clubs_headers: print("WARN: Clubs headers mismatch!")

    try: fests_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Fests")
    except gspread.exceptions.WorksheetNotFound: fests_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Fests",rows=1,cols=len(fests_headers)); fests_sheet_obj_global.append_row(fests_headers); fests_sheet_obj_global.resize(rows=100)
    current_fests_headers = fests_sheet_obj_global.row_values(1) if fests_sheet_obj_global.row_count >= 1 else []
    if not current_fests_headers:
        if fests_sheet_obj_global.col_count < len(fests_headers): fests_sheet_obj_global.add_cols(len(fests_headers) - fests_sheet_obj_global.col_count)
        if fests_sheet_obj_global.row_count > 0 and fests_sheet_obj_global.get_all_values(): fests_sheet_obj_global.clear()
        fests_sheet_obj_global.append_row(fests_headers); print("Appended headers to Fests sheet.")
    elif current_fests_headers != fests_headers : print(f"WARN: Fests headers differ. Sheet: {current_fests_headers}, Expected: {fests_headers}. Manual review advised.")
    else: print("Fests sheet headers appear correct.")
    
    print("Master sheets initialized globally.")
    return client, master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global

def get_sheet_objects_cached(): return _initialize_master_sheets_internal()

def get_all_fests_cached():
    global _cached_fests_data_all, _cache_fests_timestamp_all; now = datetime.now()
    if _cached_fests_data_all and _cache_fests_timestamp_all and (now - _cache_fests_timestamp_all < CACHE_FESTS_DURATION):
        print("Returning cached fests data."); return _cached_fests_data_all
    print("Fetching fresh fests data from sheet..."); _, _, _, fests_sheet = get_sheet_objects_cached()
    try: _cached_fests_data_all = fests_sheet.get_all_records(); _cache_fests_timestamp_all = now
    except Exception as e: print(f"ERROR fetching all fests: {e}. Returning last cache or empty."); traceback.print_exc(); return _cached_fests_data_all or []
    return _cached_fests_data_all

# (share_spreadsheet_with_editor - keep as is)
def share_spreadsheet_with_editor(spreadsheet, email_address, sheet_title):
    if not email_address or "@" not in email_address: print(f"Skipping sharing '{sheet_title}': Invalid email '{email_address}'."); return False
    if not hasattr(spreadsheet, 'list_permissions') or not hasattr(spreadsheet, 'share'): print(f"WARNING: Invalid SS object for sharing '{sheet_title}'."); return False
    try:
        print(f"Sharing SS '{sheet_title}' with {email_address}..."); perms = spreadsheet.list_permissions(); shared = False
        for p in perms:
            if p.get('type')=='user' and p.get('emailAddress')==email_address:
                if p.get('role') in ['owner', 'writer']: shared = True; print(f"'{sheet_title}' already shared correctly with {email_address}."); break
                else: print(f"Updating role for {email_address} on '{sheet_title}' to 'writer'."); spreadsheet.share(email_address, perm_type='user', role='writer', notify=False); shared = True; break
        if not shared: print(f"Sharing '{sheet_title}' new permission for {email_address}..."); spreadsheet.share(email_address, perm_type='user', role='writer', notify=False)
        print(f"Sharing ensured for '{sheet_title}' with {email_address}."); return True
    except Exception as share_e: print(f"\nWARN: Share error for '{sheet_title}' with {email_address}: {share_e}\n"); return False

# (get_or_create_worksheet - keep as is, it uses the passed client)
def get_or_create_worksheet(client_param, spreadsheet_title_or_obj, worksheet_title, headers=None):
    spreadsheet_obj = None; worksheet = None; headers = headers or []; ws_created_now = False
    try:
        if isinstance(spreadsheet_title_or_obj, gspread.Spreadsheet): spreadsheet_obj = spreadsheet_title_or_obj
        else: spreadsheet_obj = client_param.open(spreadsheet_title_or_obj)
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Individual SS '{spreadsheet_title_or_obj}' not found. Creating..."); spreadsheet_obj = client_param.create(spreadsheet_title_or_obj)
        print(f"Created SS '{spreadsheet_obj.title}'.");
        if YOUR_PERSONAL_EMAIL: share_spreadsheet_with_editor(spreadsheet_obj, YOUR_PERSONAL_EMAIL, spreadsheet_obj.title)
    except Exception as e: print(f"ERROR getting SS '{spreadsheet_title_or_obj}': {e}"); traceback.print_exc(); raise
    if not spreadsheet_obj: raise Exception(f"Failed to get spreadsheet handle for '{spreadsheet_title_or_obj}'.")
    try: worksheet = spreadsheet_obj.worksheet(worksheet_title)
    except gspread.exceptions.WorksheetNotFound:
        ws_cols = len(headers) if headers else 10; worksheet = spreadsheet_obj.add_worksheet(title=worksheet_title, rows=1, cols=ws_cols); ws_created_now = True
    except Exception as e: print(f"ERROR getting WS '{worksheet_title}': {e}"); traceback.print_exc(); raise
    if not worksheet: raise Exception(f"Failed to get worksheet handle for '{worksheet_title}'.")
    try:
        first_row = worksheet.row_values(1) if not ws_created_now and worksheet.row_count >= 1 else []
        if headers and (ws_created_now or not first_row): worksheet.append_row(headers); worksheet.resize(rows=500);
        elif headers and first_row != headers: print(f"WARN: Headers mismatch WS '{worksheet_title}'! Sheet: {first_row}, Expected: {headers}")
    except Exception as hdr_e: print(f"ERROR header logic WS '{worksheet_title}': {hdr_e}"); traceback.print_exc()
    return worksheet


def generate_unique_id(): return str(uuid.uuid4().hex)[:10]
def hash_password(password): print(f"DEBUG HASH: Placeholder for '{password}'"); return password
def verify_password(hashed, provided): print(f"DEBUG VERIFY: Stored:'{hashed}', Prov:'{provided}', Match:{hashed==provided}"); return hashed == provided
def parse_datetime(dt_str):
    if not dt_str: return None
    for fmt in DATETIME_INPUT_FORMATS:
        try: return datetime.strptime(str(dt_str).strip(), fmt)
        except: continue
    return None

@app.context_processor
def inject_now(): return {'now': datetime.now()}

@app.route('/')
def index(): return render_template('index.html')

# === Club Routes ===
@app.route('/club/register', methods=['GET', 'POST'])
def club_register():
    if request.method == 'POST':
        club_name=request.form.get('club_name','').strip();email=request.form.get('email','').strip().lower();password=request.form.get('password','');confirm_password=request.form.get('confirm_password','')
        if not all([club_name,email,password,confirm_password]): flash("All fields required.", "danger"); return render_template('club_register.html')
        if password != confirm_password: flash("Passwords do not match.", "danger"); return render_template('club_register.html')
        if "@" not in email or "." not in email.split('@')[-1]: flash("Invalid email.", "danger"); return render_template('club_register.html')
        try: _, _, clubs_sheet, _ = get_sheet_objects_cached()
        except Exception as e: print(f"ERROR Sheet Access on register: {e}"); flash("DB Error.", "danger"); return render_template('club_register.html')
        try:
            if clubs_sheet.findall(email, in_column=3): flash("Email already registered.", "warning"); return redirect(url_for('club_login'))
            club_id=generate_unique_id(); hashed_pass=hash_password(password)
            clubs_sheet.append_row([club_id, club_name, email, hashed_pass]); print(f"ClubReg: Appended {club_id}")
            flash("Club registered successfully! Please login.", "success"); return redirect(url_for('club_login'))
        except Exception as e: print(f"ERROR: ClubReg Op: {e}"); traceback.print_exc(); flash("Registration error.", "danger")
    return render_template('club_register.html')

@app.route('/club/login', methods=['GET', 'POST'])
def club_login():
    if request.method == 'POST':
        email_form = request.form.get('email','').strip().lower(); password_form = request.form.get('password','')
        print(f"DEBUG LOGIN: Attempt. Email: '{email_form}', Pass: '{password_form}'")
        if not email_form or not password_form: flash("Email/pass required.", "danger"); return render_template('club_login.html')
        if "@" not in email_form or "." not in email_form.split('@')[-1]: flash("Invalid email.", "danger"); return render_template('club_login.html')
        try: _, _, clubs_sheet, _ = get_sheet_objects_cached()
        except Exception as e: print(f"ERROR LOGIN Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_login.html')
        try: cell = clubs_sheet.find(email_form, in_column=3)
        except gspread.exceptions.CellNotFound: print(f"DEBUG LOGIN: Email not found '{email_form}'"); flash("Invalid email or password.", "danger"); return render_template('club_login.html')
        if cell:
            try:
                club_data=clubs_sheet.row_values(cell.row)
                if len(club_data) < 4: flash("Login error: Incomplete data.", "danger"); return render_template('club_login.html')
                stored_club_id, name, stored_email, stored_hash = club_data[0].strip(), club_data[1].strip(), club_data[2].strip().lower(), club_data[3].strip()
                if stored_email != email_form: flash("Internal login error.", "danger"); return render_template('club_login.html')
                if verify_password(stored_hash, password_form):
                    session['club_id']=stored_club_id; session['club_name']=name
                    flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
                else: flash("Invalid email or password.", "danger")
            except Exception as e: print(f"ERROR LOGIN Logic: {e}"); traceback.print_exc(); flash("Login logic error.", "danger")
        else: flash("Invalid email or password.", "danger")
    return render_template('club_login.html')

@app.route('/club/logout')
def club_logout(): session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))

@app.route('/club/create_fest', methods=['GET', 'POST'])
def create_fest(): # MODIFIED: No image upload logic
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    form_data_to_pass = request.form.to_dict() if request.method == 'POST' else {}
    if request.method == 'POST':
        fest_name = request.form.get('fest_name', '').strip()
        start_time_str, end_time_str, reg_end_time_str = request.form.get('start_time', ''), request.form.get('end_time', ''), request.form.get('registration_end_time', '')
        fest_details, fest_venue, fest_guests = request.form.get('fest_details', '').strip(), request.form.get('fest_venue', '').strip(), request.form.get('fest_guests', '').strip()
        is_published = 'yes' if request.form.get('publish_fest') == 'yes' else 'no'
        # fest_image_link = "" # REMOVED

        required = {'Fest Name': fest_name, 'Start Time': start_time_str, 'End Time': end_time_str, 'Registration Deadline': reg_end_time_str, 'Details': fest_details}
        missing = [name for name, val in required.items() if not val]
        if missing: flash(f"Missing: {', '.join(missing)}", "danger"); return render_template('create_fest.html',form_data=form_data_to_pass)
        try:
             start_dt, end_dt, reg_end_dt = parse_datetime(start_time_str), parse_datetime(end_time_str), parse_datetime(reg_end_time_str)
             if not all([start_dt, end_dt, reg_end_dt]): flash("Invalid date/time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             if not (start_dt < end_dt and reg_end_dt <= start_dt): flash("Invalid times.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        except ValueError: flash("Invalid time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        try:
            g_client, _, _, master_fests_sheet = get_sheet_objects_cached(); fest_id=generate_unique_id();
            # MODIFIED: new_fest_row does not include fest_image_link
            new_fest_row=[ fest_id, fest_name, session['club_id'], session.get('club_name','N/A'), start_dt.strftime(DATETIME_SHEET_FORMAT), end_dt.strftime(DATETIME_SHEET_FORMAT), reg_end_dt.strftime(DATETIME_SHEET_FORMAT), fest_details, is_published, fest_venue, fest_guests ];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}");
            global _cached_fests_data_all, _cache_fests_timestamp_all; _cached_fests_data_all = None; _cache_fests_timestamp_all = None; print("INFO: All fests cache invalidated.")
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
            get_or_create_worksheet(g_client, safe_sheet_title, "Registrations", event_headers);
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e: print(f"ERROR: Create Fest write: {e}"); traceback.print_exc(); flash("DB write error.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})

@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now=datetime.now(); upcoming,ongoing = [],[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests dashboard: {e}"); flash("DB Error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name'), upcoming_fests=[], ongoing_fests=[])
    club_fests_all=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
    for fest in club_fests_all: # fest object will NOT have FestImageLink
        try:
            start_time, end_time = parse_datetime(fest.get('StartTime')), parse_datetime(fest.get('EndTime'))
            if not (start_time and end_time): continue
            if now < start_time: upcoming.append(fest)
            elif start_time <= now < end_time: ongoing.append(fest)
        except Exception as e: print(f"Error processing fest '{fest.get('FestName')}' for dashboard: {e}")
    upcoming.sort(key=lambda x: parse_datetime(x.get('StartTime')) or datetime.max)
    ongoing.sort(key=lambda x: parse_datetime(x.get('StartTime')) or datetime.min)
    return render_template('club_dashboard.html',club_name=session.get('club_name'), upcoming_fests=upcoming, ongoing_fests=ongoing)

@app.route('/club/history')
def club_history():
     if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
     now=datetime.now(); past_fests_all=[]
     try: all_fests_data = get_all_fests_cached()
     except Exception as e: print(f"ERROR getting cached fests history: {e}"); flash("DB Error.", "danger"); return render_template('club_history.html', club_name=session.get('club_name'), past_fests=[])
     club_fests_for_history=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
     for fest in club_fests_for_history:
        try:
            end_time=parse_datetime(fest.get('EndTime', ''))
            if not end_time: continue
            if now>=end_time: past_fests_all.append(fest)
        except Exception as e: print(f"Error processing fest '{fest.get('FestName')}' for history: {e}")
     past_fests_all.sort(key=lambda x: parse_datetime(x.get('EndTime')) or datetime.min, reverse=True)
     return render_template('club_history.html',club_name=session.get('club_name'), past_fests=past_fests_all)

@app.route('/club/fest/<fest_id>/edit', methods=['GET'])
def edit_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    try:
        all_fests_data = get_all_fests_cached()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None);
        if not fest_info: flash("Fest not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Permission denied.", "danger"); return redirect(url_for('club_dashboard'))
        return render_template('edit_options.html', fest=fest_info)
    except Exception as e: print(f"ERROR getting edit options FestID {fest_id}: {e}"); traceback.print_exc(); flash("Error getting event options.", "danger"); return redirect(url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/end', methods=['POST'])
def end_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    try:
        _, _, _, fests_sheet = get_sheet_objects_cached()
        all_fests_data = get_all_fests_cached()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID', '')) == fest_id), None)
        if not fest_info: flash("Fest to end not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID', '')) != session['club_id']: flash("Permission denied.", "danger"); return redirect(url_for('club_dashboard'))
        fest_cell = fests_sheet.find(fest_id, in_column=1)
        if not fest_cell: flash("Fest to end not found in sheet (cell).", "danger"); return redirect(url_for('club_dashboard'))
        fest_row_index = fest_cell.row; header_row = fests_sheet.row_values(1)
        end_time_col_idx = header_row.index('EndTime') + 1; published_col_idx = header_row.index('Published') + 1
        now_str = datetime.now().strftime(DATETIME_SHEET_FORMAT)
        updates = [{'range': gspread.utils.rowcol_to_a1(fest_row_index, end_time_col_idx), 'values': [[now_str]]},
                   {'range': gspread.utils.rowcol_to_a1(fest_row_index, published_col_idx), 'values': [['no']]}]
        fests_sheet.batch_update(updates)
        global _cached_fests_data_all, _cache_fests_timestamp_all; _cached_fests_data_all = None; _cache_fests_timestamp_all = None
        flash(f"Fest '{fest_info.get('FestName', fest_id)}' ended & unpublished.", "success")
    except Exception as e: print(f"ERROR ending fest {fest_id}: {e}"); traceback.print_exc(); flash("Error ending event.", "danger")
    return redirect(url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/delete', methods=['POST'])
def delete_fest(fest_id): # MODIFIED: No Drive image deletion
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    redirect_url = request.referrer or url_for('club_dashboard')
    try:
        _, _, _, fests_sheet = get_sheet_objects_cached()
        all_fests_data = get_all_fests_cached()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id), None)
        if not fest_info: flash("Fest to delete not found.", "danger"); return redirect(redirect_url)
        if str(fest_info.get('ClubID',''))!=session['club_id']: flash("Permission denied.", "danger"); return redirect(redirect_url)
        fest_name_to_delete = fest_info.get('FestName', f"Fest (ID: {fest_id})")
        fest_cell = fests_sheet.find(fest_id, in_column=1)
        if not fest_cell: flash("Fest to delete not found in sheet (cell).", "danger"); return redirect(redirect_url)
        fests_sheet.delete_rows(fest_cell.row)
        print(f"Fest row for '{fest_name_to_delete}' deleted from sheet.")
        global _cached_fests_data_all, _cache_fests_timestamp_all; _cached_fests_data_all = None; _cache_fests_timestamp_all = None
        flash(f"Fest '{fest_name_to_delete}' deleted.", "success")
    except Exception as e: print(f"ERROR deleting fest {fest_id}: {e}"); traceback.print_exc(); flash("Error deleting event.", "danger")
    return redirect(redirect_url)

@app.route('/club/fest/<fest_id>/stats')
def fest_stats(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    try:
        g_client, _, _, _ = get_sheet_objects_cached()
        all_fests_data = get_all_fests_cached()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Permission denied for stats.", "danger"); return redirect(url_for('club_dashboard'))
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        sheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        stats = {'total_registered': 0, 'total_present': 0, 'total_absent': 0, 'attendees_present': [], 'attendees_absent': [], 'college_stats': defaultdict(int), 'hourly_distribution': defaultdict(lambda: 0), 'checkin_times': [], 'attendance_rate': 0}
        try:
            spreadsheet = g_client.open(sheet_title); registrations_sheet = spreadsheet.worksheet("Registrations"); registrations_data = registrations_sheet.get_all_records()
            stats['total_registered'] = len(registrations_data)
            for record in registrations_data:
                is_present = str(record.get('Present', 'no')).strip().lower() == 'yes'
                college = record.get('College', 'Unknown').strip() or 'Unknown'
                attendee = {'UniqueID': record.get('UniqueID', ''), 'Name': record.get('Name', ''), 'Email': record.get('Email', ''), 'Mobile': record.get('Mobile', ''), 'College': college, 'Timestamp': record.get('Timestamp', '')}
                if is_present:
                    stats['total_present'] += 1; stats['attendees_present'].append(attendee); stats['college_stats'][college] += 1
                    dt = parse_datetime(record.get('Timestamp'));
                    if dt: stats['checkin_times'].append(dt); hour = dt.hour; stats['hourly_distribution'][f"{hour:02d}:00-{hour+1:02d}:00"] += 1
                else: stats['total_absent'] += 1; stats['attendees_absent'].append(attendee)
            if stats['total_registered'] > 0: stats['attendance_rate'] = round((stats['total_present'] / stats['total_registered']) * 100, 2)
            stats['college_stats'] = dict(sorted(stats['college_stats'].items(), key=itemgetter(1), reverse=True))
            stats['hourly_distribution'] = dict(sorted(stats['hourly_distribution'].items()))
            stats['colleges_chart_data'] = {'labels': list(stats['college_stats'].keys())[:10], 'data': list(stats['college_stats'].values())[:10]}
            stats['attendance_chart_data'] = {'labels': ['Present', 'Absent'], 'data': [stats['total_present'], stats['total_absent']]}
            stats['hourly_chart_data'] = {'labels': list(stats['hourly_distribution'].keys()), 'data': list(stats['hourly_distribution'].values())}
        except gspread.exceptions.SpreadsheetNotFound: flash(f"Registration data for '{fest_info.get('FestName')}' not found.", "info")
        except Exception as e: print(f"Error accessing stats data for {sheet_title}: {e}"); traceback.print_exc(); flash("Error loading detailed statistics.", "warning")
        return render_template('fest_stats.html', fest=fest_info, stats=stats)
    except Exception as e: print(f"Error in fest_stats: {e}"); traceback.print_exc(); flash("Error loading statistics.", "danger"); return redirect(url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/export/excel')
def export_excel(fest_id):
    if 'club_id' not in session: flash("Login required for export.", "warning"); return jsonify({"error": "Unauthorized"}), 401
    try:
        g_client, _, _, _ = get_sheet_objects_cached(); all_fests_data = get_all_fests_cached()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Unauthorized export.", "danger"); return redirect(url_for('club_dashboard'))
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        spreadsheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        try:
            spreadsheet = g_client.open(spreadsheet_title); registrations_sheet = spreadsheet.worksheet("Registrations"); registrations_data = registrations_sheet.get_all_records()
        except gspread.exceptions.SpreadsheetNotFound: flash(f"Reg sheet for '{fest_info.get('FestName')}' not found.", "warning"); return redirect(url_for('fest_stats', fest_id=fest_id))
        except Exception as e_sheet: print(f"Sheet access error for Excel: {e_sheet}"); flash("Error accessing data.", "danger"); return redirect(url_for('fest_stats', fest_id=fest_id))
        df = pd.DataFrame(registrations_data); output = BytesIO() # Using standard BytesIO
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='Registrations')
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{safe_name}_registrations_{datetime.now().strftime('%Y%m%d')}.xlsx", mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e: print(f"Excel Export Error: {e}"); traceback.print_exc(); flash(f"Excel export error: {e}", "danger"); return redirect(request.referrer or url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/export/pdf')
def export_pdf(fest_id):
    if 'club_id' not in session: flash("Login required for PDF export.", "warning"); return redirect(url_for('club_login'))
    try:
        g_client, _, _, _ = get_sheet_objects_cached(); all_fests_data = get_all_fests_cached()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Event not found for PDF.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Unauthorized PDF export.", "danger"); return redirect(url_for('club_dashboard'))
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        spreadsheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        try:
            spreadsheet = g_client.open(spreadsheet_title); registrations_sheet = spreadsheet.worksheet("Registrations"); registrations_data = registrations_sheet.get_all_records()
        except gspread.exceptions.SpreadsheetNotFound: flash(f"Reg sheet for '{fest_info.get('FestName')}' not found for PDF.", "warning"); return redirect(url_for('fest_stats', fest_id=fest_id))
        except Exception as e_sheet: print(f"Sheet access error for PDF: {e_sheet}"); flash("Error accessing PDF data.", "danger"); return redirect(url_for('fest_stats', fest_id=fest_id))
        if not registrations_data: flash(f"No data for '{fest_info.get('FestName')}' to PDF.", "info"); return redirect(url_for('fest_stats', fest_id=fest_id))
        
        pdf = FPDF(orientation='L', unit='mm', format='A4'); pdf.add_page(); pdf.set_font("Arial", 'B', size=16)
        pdf.cell(0, 10, txt=f"Event Report: {fest_info.get('FestName','')}", ln=1, align='C'); pdf.set_font("Arial", size=10)
        pdf.cell(0, 7, txt=f"Date: {datetime.now().strftime(DATETIME_DISPLAY_FORMAT)}", ln=1, align='C'); pdf.ln(5)
        pdf.set_font("Arial", 'B', size=9)
        col_widths = {'UniqueID': 25, 'Name': 45, 'Email': 60, 'Mobile': 25, 'College': 45, 'Present': 20, 'Timestamp': 30}
        headers_pdf = ['UniqueID', 'Name', 'Email', 'Mobile', 'College', 'Present', 'Timestamp']
        display_headers_pdf = {'UniqueID': 'ID', 'Name': 'Name', 'Email': 'Email', 'Mobile': 'Mobile', 'College': 'College', 'Present': 'Status', 'Timestamp': 'Timestamp'}
        for header_key in headers_pdf: pdf.cell(col_widths.get(header_key, 30), 7, display_headers_pdf.get(header_key, header_key), border=1, align='C')
        pdf.ln(); pdf.set_font("Arial", size=8)
        for row in registrations_data:
            for header_key in headers_pdf:
                val = str(row.get(header_key, 'N/A'))
                if header_key == 'Present': val = "Present" if val.lower() == 'yes' else "Absent"
                elif header_key == 'Timestamp':
                    parsed_ts = parse_datetime(val); val = parsed_ts.strftime(DATETIME_DISPLAY_FORMAT) if parsed_ts else (val if val != 'N/A' else '')
                max_len_heuristic = int(col_widths.get(header_key, 30) / 1.8) 
                if len(val) > max_len_heuristic: val = val[:max_len_heuristic-3] + "..."
                pdf.cell(col_widths.get(header_key, 30), 6, val, border=1, align='L' if header_key in ['Name', 'Email', 'College'] else 'C')
            pdf.ln()
        pdf_output_bytes = pdf.output(dest='S').encode('latin-1'); response = make_response(pdf_output_bytes)
        response.headers['Content-Type'] = 'application/pdf'; response.headers['Content-Disposition'] = f'attachment; filename={safe_name}_report_{datetime.now().strftime("%Y%m%d")}.pdf'
        return response
    except Exception as e: print(f"PDF Export Error: {e}"); traceback.print_exc(); flash("PDF export error.", "danger"); return redirect(request.referrer or url_for('club_dashboard'))

# === Attendee Routes ===
@app.route('/events')
def live_events():
    now=datetime.now(); available_fests=[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for events: {e}"); flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    for fest in all_fests_data: # fest object will NOT have FestImageLink
        is_published=str(fest.get('Published','')).strip().lower()=='yes'
        reg_end_time = parse_datetime(fest.get('RegistrationEndTime',''))
        start_time = parse_datetime(fest.get('StartTime',''))
        if is_published and reg_end_time and start_time and now < reg_end_time and now < start_time :
            available_fests.append(fest)
    available_fests.sort(key=lambda x: parse_datetime(x.get('StartTime')) or datetime.max)
    return render_template('live_events.html', fests=available_fests)

@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info=None; is_open_for_reg=False
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for event_detail: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))
    fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id_param), None)
    if not fest_info: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    is_published = str(fest_info.get('Published','')).lower()=='yes'
    reg_end_time = parse_datetime(fest_info.get('RegistrationEndTime', ''))
    start_time = parse_datetime(fest_info.get('StartTime',''))
    if is_published and reg_end_time and start_time and datetime.now() < reg_end_time and datetime.now() < start_time : 
        is_open_for_reg=True
    return render_template('event_detail.html', fest=fest_info, registration_open=is_open_for_reg)

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param): # MODIFIED for direct QR download
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); mobile=request.form.get('mobile','').strip(); college=request.form.get('college','').strip();
    if not all([name,email,mobile,college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    if "@" not in email or "." not in email.split('@')[-1]: flash("Invalid email.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    try:
        g_client, _, _, _ = get_sheet_objects_cached()
        all_fests=get_all_fests_cached()
        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==fest_id_param), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        reg_end_time = parse_datetime(fest_info.get('RegistrationEndTime', '')); start_time = parse_datetime(fest_info.get('StartTime', ''))
        if not reg_end_time or not start_time or datetime.now() >= reg_end_time or datetime.now() >= start_time:
            flash("Registration closed or event has already started.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        
        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
        reg_sheet=get_or_create_worksheet(g_client, individual_sheet_title,"Registrations",event_headers);
        if reg_sheet.findall(email, in_column=3): flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        
        user_id=generate_unique_id(); ts=datetime.now().strftime(DATETIME_DISPLAY_FORMAT); row=[user_id, name, email, mobile, college, 'no', ts];
        reg_sheet.append_row(row); print(f"JoinEvent: Appended registration for {email} to {individual_sheet_title}")
        
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20].replace(',',';')}"; img_qr_obj=qrcode.make(qr_data);
        buf = BytesIO(); img_qr_obj.save(buf, format="PNG"); buf.seek(0)
        safe_fest_name_for_file = "".join(c if c.isalnum() or c in ['_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "event"
        download_filename = f"{safe_fest_name_for_file}_QR_{user_id[:6]}.png"
        
        flash(f"Successfully joined '{fest_info.get('FestName')}'! Your QR code is downloading. Check your downloads folder.", "success")
        return send_file( buf, mimetype='image/png', as_attachment=True, download_name=download_filename )

    except gspread.exceptions.SpreadsheetNotFound: flash("Registration error: Event data sheet missing.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param))
    except Exception as e: print(f"ERROR JoinEvent: {e}"); traceback.print_exc(); flash("Registration error.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));

# === Security Routes ===
@app.route('/security/login', methods=['GET', 'POST'])
def security_login():
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower(); event_name_password = request.form.get('password','').strip()
        if not username or not event_name_password: flash("All fields required.", "danger"); return render_template('security_login.html')
        if username == 'security':
            try:
                all_fests_data = get_all_fests_cached()
                if all_fests_data is None: all_fests_data = [] 
                now = datetime.now(); valid_event = None
                for f in all_fests_data:
                    if str(f.get('FestName','')).strip() == event_name_password and str(f.get('Published','')).strip().lower() == 'yes':
                        start_time, end_time = parse_datetime(f.get('StartTime','')), parse_datetime(f.get('EndTime',''))
                        if start_time and end_time and start_time <= now <= (end_time + timedelta(hours=2)): valid_event = f; break
                        elif start_time and not end_time and start_time <= now: valid_event = f; break
                if valid_event:
                    session['security_event_name'] = valid_event.get('FestName','N/A'); session['security_event_id'] = valid_event.get('FestID','N/A');
                    safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(valid_event.get('FestName','Event'))).strip() or "fest_event";
                    session['security_event_sheet_title']=f"{safe_base[:80]}_{valid_event.get('FestID','')}";
                    flash(f"Security access for: {session['security_event_name']}", "success"); return redirect(url_for('security_scanner'));
                else: flash("Invalid event password or event inactive/unpublished.", "danger")                        
            except Exception as e: print(f"ERROR: Security login: {e}"); traceback.print_exc(); flash("Security login error.", "danger")
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
    data = request.get_json(); qr_content = data.get('qr_data') if data else None
    if not qr_content: return jsonify({'status': 'error', 'message': 'No QR data.'}), 400
    try:
        parsed_data={};
        for item in qr_content.split(','):
            if ':' in item: key, value = item.split(':', 1); parsed_data[key.strip()] = value.strip()
        scanned_unique_id = parsed_data.get('UniqueID'); scanned_fest_id = parsed_data.get('FestID');
        if not scanned_unique_id or not scanned_fest_id: return jsonify({'status':'error', 'message':'QR missing data.'}), 400
        if scanned_fest_id != session.get('security_event_id'): return jsonify({'status':'error', 'message':'QR for wrong event.'}), 400
    except Exception as e: print(f"ERROR parsing QR: {e}"); return jsonify({'status':'error', 'message':'Invalid QR format.'}), 400
    try:
        g_client, _, _, _ = get_sheet_objects_cached()
        sheet_title = session['security_event_sheet_title']; headers_qr = ['UniqueID','Name','Email','Mobile','College','Present','Timestamp']
        reg_sheet = get_or_create_worksheet(g_client, sheet_title, "Registrations", headers_qr);
        cell = reg_sheet.find(scanned_unique_id, in_column=1)
        if not cell: return jsonify({'status':'error', 'message':'Participant not found.'}), 404
        row_data=reg_sheet.row_values(cell.row); sheet_headers = reg_sheet.row_values(1) # Get actual headers from sheet
        try: p_idx = sheet_headers.index('Present'); n_idx = sheet_headers.index('Name'); e_idx = sheet_headers.index('Email'); m_idx = sheet_headers.index('Mobile'); ts_idx = sheet_headers.index('Timestamp')
        except ValueError: return jsonify({'status':'error', 'message':'Reg sheet config error.'}), 500
        def get_val(idx, default=''): return row_data[idx] if len(row_data)>idx else default
        status = get_val(p_idx).strip().lower(); name = get_val(n_idx); email = get_val(e_idx); mobile = get_val(m_idx)
        current_scan_timestamp = datetime.now().strftime(DATETIME_DISPLAY_FORMAT)
        if status == 'yes': last_scan_time = get_val(ts_idx, "previously"); return jsonify({'status':'warning','message':'ALREADY SCANNED!', 'name':name,'details':f"{email}, {mobile}. Scanned: {last_scan_time}"})
        updates_to_perform = [ {'range': gspread.utils.rowcol_to_a1(cell.row, p_idx + 1), 'values': [['yes']]}, {'range': gspread.utils.rowcol_to_a1(cell.row, ts_idx + 1), 'values': [[current_scan_timestamp]]} ]
        reg_sheet.batch_update(updates_to_perform)
        return jsonify({'status':'success','message':'Access Granted!','name':name,'details':f"{email}, {mobile}. Checked-in: {current_scan_timestamp}"});
    except gspread.exceptions.SpreadsheetNotFound: return jsonify({'status':'error', 'message':f"Event reg data ('{session['security_event_sheet_title']}') not found."}), 404
    except Exception as e: print(f"ERROR: Verify QR op: {e}"); traceback.print_exc(); return jsonify({'status':'error', 'message':'Verification server error.'}), 500

# --- Initialization Function ---
def initialize_application_on_startup():
    print("\n----- Initializing Application on Startup -----")
    try: get_sheet_objects_cached(); print("Initial check/load of Google services complete.")
    except ValueError as ve: print(f"🔴🔴🔴 FATAL STARTUP ERROR (Credentials): {ve}"); exit(1)
    except Exception as e: print(f"CRITICAL INIT ERROR: {e}"); traceback.print_exc(); exit(1)
    print("----- Application Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    # FEST_IMAGES_DRIVE_FOLDER_ID check removed as image upload is removed
    if not MASTER_SHEET_ID: print("\n🔴 WARNING: MASTER_SHEET_ID not set. Opening master sheet relies on name search.\n")
    
    is_main_process = os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if is_main_process:
        if not os.environ.get('FLASK_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY') == "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV":
            print("\n🔴 WARNING: FLASK_SECRET_KEY not securely set for main process.\n")
        print("Flask starting up - Main process: Initializing...")
        initialize_application_on_startup()
        print("Flask startup - Main process: Initialization complete.")
    else: print("Flask starting up - Reloader process detected.")
    
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask server (host=0.0.0.0, port={port})...")
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=True)
