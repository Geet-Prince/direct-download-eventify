# -*- coding: utf-8 -*-
# Standard library imports
import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
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
from werkzeug.utils import secure_filename
import pytz

# For Google Drive API
from google.oauth2.service_account import Credentials as GoogleAuthServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# from dotenv import load_dotenv # Optional for local .env files
# load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    print("🔴 FATAL: FLASK_SECRET_KEY is not set. Using a temporary key for local dev, but this WILL FAIL in production or if app.debug is False.")
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug: # More robust check
        raise ValueError("FLASK_SECRET_KEY is not set in the environment. This is required for production.")
    app.secret_key = "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV" # Not for production


# --- Google Setup ---
SCOPE_SHEETS = ['https://www.googleapis.com/auth/spreadsheets']
SCOPE_DRIVE = ['https://www.googleapis.com/auth/drive']
SCOPE_GSPREAD_CLIENT_DEFAULT = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file']

MASTER_SHEET_NAME = os.environ.get("MASTER_SHEET_NAME", 'event management')
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID")
YOUR_PERSONAL_EMAIL = os.environ.get("YOUR_PERSONAL_SHARE_EMAIL")
FEST_IMAGES_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID")

# --- Timezone Setup ---
YOUR_LOCAL_TIMEZONE_STR = os.environ.get("LOCAL_TIMEZONE", "Asia/Kolkata")
try:
    YOUR_LOCAL_TIMEZONE = pytz.timezone(YOUR_LOCAL_TIMEZONE_STR)
except pytz.exceptions.UnknownTimeZoneError:
    print(f"🔴 FATAL: Invalid LOCAL_TIMEZONE '{YOUR_LOCAL_TIMEZONE_STR}'. Using UTC as fallback. Please set correctly.")
    YOUR_LOCAL_TIMEZONE = pytz.utc

# --- Constants ---
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M' # Input format from datetime-local
DATETIME_STORAGE_FORMAT = '%Y-%m-%dT%H:%M:%SZ' # ISO 8601 with Z for UTC storage in sheet
DATETIME_DISPLAY_FORMAT_USER = '%Y-%m-%d %I:%M %p' # For displaying in local time to user
DATETIME_DISPLAY_SHEET_TS = '%Y-%m-%d %H:%M:%S' # For general timestamps stored in sheets (like registration or scan time)

# For parsing naive datetime strings from sheet (if they weren't stored as UTC 'Z')
DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE = [
    DATETIME_SHEET_FORMAT,
    '%Y-%m-%d %H:%M',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S'
]
# For parsing from sheet, prioritize the format we store in
DATETIME_INPUT_FORMATS_FOR_SHEET_PARSE = [DATETIME_STORAGE_FORMAT] + DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# --- Global Variables for Google Services ---
gspread_client_global = None; drive_service_global = None; master_spreadsheet_obj_global = None
clubs_sheet_obj_global = None; fests_sheet_obj_global = None
_cached_fests_data_all = None; _cache_fests_timestamp_all = None
CACHE_FESTS_DURATION = timedelta(minutes=2)

# --- Helper Functions ---
def allowed_file(filename): return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_google_creds_dict_from_env():
    expected_keys_map = { "type": "GOOGLE_TYPE", "project_id": "GOOGLE_PROJECT_ID", "private_key_id": "GOOGLE_PRIVATE_KEY_ID", "private_key": "GOOGLE_PRIVATE_KEY", "client_email": "GOOGLE_CLIENT_EMAIL", "client_id": "GOOGLE_CLIENT_ID", "auth_uri": "GOOGLE_AUTH_URI", "token_uri": "GOOGLE_TOKEN_URI", "auth_provider_x509_cert_url": "GOOGLE_AUTH_PROVIDER_X509_CERT_URL", "client_x509_cert_url": "GOOGLE_CLIENT_X509_CERT_URL" }
    creds_dict = {}
    missing_vars = [env_var for _, env_var in expected_keys_map.items() if not os.environ.get(env_var)]
    if missing_vars: raise ValueError(f"Missing Google credentials environment variables: {', '.join(missing_vars)}")
    for key, env_var_name in expected_keys_map.items(): creds_dict[key] = os.environ.get(env_var_name)
    creds_dict['private_key'] = creds_dict['private_key'].replace('\\n', '\n')
    creds_dict['universe_domain'] = os.environ.get("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com")
    return creds_dict

def _initialize_gspread_client_internal():
    global gspread_client_global
    if gspread_client_global: return gspread_client_global
    print("Initializing gspread client (env vars)...")
    try:
        creds_dict = get_google_creds_dict_from_env()
        creds = GSpreadServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE_GSPREAD_CLIENT_DEFAULT)
        gspread_client_global = gspread.authorize(creds)
        print(f"gspread client initialized with scope: {SCOPE_GSPREAD_CLIENT_DEFAULT}")
        return gspread_client_global
    except Exception as e: print(f"CRITICAL ERROR initializing gspread client: {e}"); traceback.print_exc(); raise

def _initialize_drive_service_internal():
    global drive_service_global
    if drive_service_global: return drive_service_global
    print("Initializing Google Drive service (env vars)...")
    try:
        creds_dict = get_google_creds_dict_from_env()
        creds = GoogleAuthServiceAccountCredentials.from_service_account_info(creds_dict, scopes=SCOPE_DRIVE)
        drive_service_global = build('drive', 'v3', credentials=creds, cache_discovery=False)
        print("Google Drive service initialized.")
        return drive_service_global
    except Exception as e: print(f"CRITICAL ERROR initializing Google Drive service: {e}"); traceback.print_exc(); raise

def get_gspread_client_cached(): return _initialize_gspread_client_internal()
def get_drive_service_cached(): return _initialize_drive_service_internal()

def parse_datetime_as_utc(dt_str_from_input, input_is_naive_local=False):
    """
    Parses a datetime string and returns a UTC-aware datetime object.
    - If input_is_naive_local is True, assumes dt_str_from_input is naive and in YOUR_LOCAL_TIMEZONE.
    - Otherwise, assumes dt_str_from_input is from the sheet (likely UTC 'Z' format or older naive local).
    """
    if not dt_str_from_input: return None
    dt_str = str(dt_str_from_input).strip()

    if input_is_naive_local: # Typically for form inputs like datetime-local
        try:
            parsed_naive = datetime.strptime(dt_str, DATETIME_SHEET_FORMAT) # datetime-local format
            localized_dt = YOUR_LOCAL_TIMEZONE.localize(parsed_naive, is_dst=None)
            return localized_dt.astimezone(pytz.utc)
        except (ValueError, TypeError) as e:
            print(f"Error parsing form input '{dt_str}' as local time: {e}")
            return None # Or try other DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE if form input varies

    # If from sheet, try ISO 'Z' format first
    if dt_str.endswith('Z'):
        try:
            dt_obj = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            return dt_obj.astimezone(pytz.utc)
        except ValueError: pass

    # Fallback for other formats from sheet (e.g., old data that was naive local)
    parsed_naive = None
    for fmt in DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE: # Use this list for potentially naive sheet data
        try: parsed_naive = datetime.strptime(dt_str, fmt); break
        except (ValueError, TypeError): continue
    
    if parsed_naive:
        print(f"WARN: DateTime string '{dt_str}' from sheet parsed as naive. Assuming it was {YOUR_LOCAL_TIMEZONE.zone} and converting to UTC.")
        try: localized_dt = YOUR_LOCAL_TIMEZONE.localize(parsed_naive, is_dst=None); return localized_dt.astimezone(pytz.utc)
        except Exception as e_loc: print(f"Error localizing naive sheet time '{parsed_naive}': {e_loc}. Treating as naive UTC."); return pytz.utc.localize(parsed_naive)
             
    print(f"Could not parse datetime string '{dt_str}' from sheet with known formats.")
    return None

def get_current_time_utc(): return datetime.now(timezone.utc)

def _initialize_master_sheets_internal():
    global master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global
    if master_spreadsheet_obj_global and clubs_sheet_obj_global and fests_sheet_obj_global:
        return get_gspread_client_cached(), master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global
    print("Initializing master sheet objects (one-time per worker)..."); client = get_gspread_client_cached()
    spreadsheet = None
    if MASTER_SHEET_ID:
        try: print(f"Opening master SS by ID(key):'{MASTER_SHEET_ID}'"); spreadsheet = client.open_by_key(MASTER_SHEET_ID); print(f"Opened: '{spreadsheet.title}'")
        except Exception as e: print(f"WARN: Open master by ID failed: {e}. Try by name."); spreadsheet = None
    if not spreadsheet:
        try: print(f"Attempting to open master SS by name:'{MASTER_SHEET_NAME}'"); spreadsheet = client.open(MASTER_SHEET_NAME); print(f"Opened: '{spreadsheet.title}'")
        except gspread.exceptions.SpreadsheetNotFound: print(f"Master SS '{MASTER_SHEET_NAME}' not found. Creating..."); spreadsheet = client.create(MASTER_SHEET_NAME); print(f"Created: '{MASTER_SHEET_NAME}'.");
        if YOUR_PERSONAL_EMAIL and spreadsheet: share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
        except Exception as e: print(f"CRITICAL ERROR opening/creating master SS by name: {e}"); traceback.print_exc(); raise
    if not spreadsheet: raise Exception("FATAL: Failed to open or create master spreadsheet.")
    master_spreadsheet_obj_global = spreadsheet
    clubs_headers=['ClubID','ClubName','Email','PasswordHash']
    fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests','FestImageLink']
    try: clubs_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Clubs")
    except gspread.exceptions.WorksheetNotFound: clubs_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Clubs",rows=1,cols=len(clubs_headers)); clubs_sheet_obj_global.append_row(clubs_headers); clubs_sheet_obj_global.resize(rows=100)
    if (clubs_sheet_obj_global.row_values(1) if clubs_sheet_obj_global.row_count >=1 else []) != clubs_headers: print("WARN: Clubs headers mismatch!")
    try: fests_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Fests")
    except gspread.exceptions.WorksheetNotFound: fests_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Fests",rows=1,cols=len(fests_headers)); fests_sheet_obj_global.append_row(fests_headers); fests_sheet_obj_global.resize(rows=100)
    current_fests_headers = fests_sheet_obj_global.row_values(1) if fests_sheet_obj_global.row_count >= 1 else []
    expected_num_cols = len(fests_headers); current_cols = fests_sheet_obj_global.col_count
    if not current_fests_headers:
        if current_cols < expected_num_cols: fests_sheet_obj_global.add_cols(expected_num_cols - current_cols)
        if fests_sheet_obj_global.row_count > 0 and fests_sheet_obj_global.get_all_values(): fests_sheet_obj_global.clear()
        fests_sheet_obj_global.append_row(fests_headers); print("Appended headers to Fests.")
    elif current_fests_headers != fests_headers and 'FestImageLink' not in current_fests_headers and current_fests_headers == fests_headers[:-1]:
        if current_cols < expected_num_cols: fests_sheet_obj_global.add_cols(1)
        try: fests_sheet_obj_global.update_cell(1, expected_num_cols, 'FestImageLink'); print("Added 'FestImageLink' header.")
        except Exception as he: print(f"ERROR adding 'FestImageLink' header: {he}.")
    elif current_fests_headers != fests_headers: print("WARN: Fests headers differ significantly.")
    print("Master sheets initialized globally."); return client, master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global

def get_sheet_objects_cached(): return _initialize_master_sheets_internal()
def get_all_fests_cached():
    global _cached_fests_data_all, _cache_fests_timestamp_all; now = datetime.now()
    if _cached_fests_data_all and _cache_fests_timestamp_all and (now - _cache_fests_timestamp_all < CACHE_FESTS_DURATION):
        print("Returning cached fests data."); return _cached_fests_data_all
    print("Fetching fresh fests data from sheet..."); _, _, _, fests_sheet = get_sheet_objects_cached()
    try: _cached_fests_data_all = fests_sheet.get_all_records(); _cache_fests_timestamp_all = now
    except Exception as e: print(f"ERROR fetching all fests: {e}. Returning last cache or empty."); traceback.print_exc(); return _cached_fests_data_all or []
    return _cached_fests_data_all

# (All other helper functions: upload_to_drive, share_spreadsheet_with_editor, get_or_create_worksheet, generate_unique_id, hash_password, verify_password - keep as is)
# ... (These are identical to the last full code version)

@app.context_processor
def inject_now(): return {'now': datetime.now()}

@app.route('/')
def index(): return render_template('index.html')

# === Club Routes ===
# (Club Registration is REMOVED as per your request)
# @app.route('/club/register', methods=['GET', 'POST'])
# def club_register():
#    pass 

@app.route('/club/login', methods=['GET', 'POST']) # Same as before
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

@app.route('/club/logout') # Same as before
def club_logout(): session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))

@app.route('/club/create_fest', methods=['GET', 'POST']) # Timezone logic updated
def create_fest():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    form_data_to_pass = request.form.to_dict() if request.method == 'POST' else {}
    if request.method == 'POST':
        fest_name = request.form.get('fest_name', '').strip()
        start_time_str = request.form.get('start_time', '')
        end_time_str = request.form.get('end_time', '')
        registration_end_time_str = request.form.get('registration_end_time', '')
        fest_details, fest_venue, fest_guests = request.form.get('fest_details', '').strip(), request.form.get('fest_venue', '').strip(), request.form.get('fest_guests', '').strip()
        is_published = 'yes' if request.form.get('publish_fest') == 'yes' else 'no'
        fest_image_link = ""
        if 'fest_image' in request.files:
            file = request.files['fest_image']
            if file and file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename); unique_filename = f"{uuid.uuid4().hex}_{filename}"
                file_stream = BytesIO(); file.save(file_stream); file_stream.seek(0)
                if not FEST_IMAGES_DRIVE_FOLDER_ID: flash("Image upload server config error.", "danger"); print("ERROR: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID not set.")
                else:
                    uploaded_url = upload_to_drive(file_stream, unique_filename, FEST_IMAGES_DRIVE_FOLDER_ID)
                    if uploaded_url: fest_image_link = uploaded_url
                    else: flash("Failed to upload fest image.", "warning")
            elif file and file.filename != '' and not allowed_file(file.filename): flash(f"Invalid image type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}", "warning")
        required = {'Fest Name': fest_name, 'Start Time': start_time_str, 'End Time': end_time_str, 'Registration Deadline': registration_end_time_str, 'Details': fest_details}
        missing = [name for name, val in required.items() if not val]
        if missing: flash(f"Missing: {', '.join(missing)}", "danger"); return render_template('create_fest.html',form_data=form_data_to_pass)
        try:
             start_dt_utc = parse_datetime_as_utc(start_time_str, input_is_naive_local=True)
             end_dt_utc = parse_datetime_as_utc(end_time_str, input_is_naive_local=True)
             reg_end_dt_utc = parse_datetime_as_utc(registration_end_time_str, input_is_naive_local=True)
             if not all([start_dt_utc, end_dt_utc, reg_end_dt_utc]): flash("Invalid date/time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             if not (start_dt_utc < end_dt_utc and reg_end_dt_utc <= start_dt_utc):
                 flash("Time validation error: Start < End, and Reg Deadline <= Start.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        except Exception as e_parse: print(f"Error parsing/validating fest times: {e_parse}"); flash("Invalid date/time input or logic error.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        try:
            g_client, _, _, master_fests_sheet = get_sheet_objects_cached(); fest_id=generate_unique_id();
            new_fest_row=[ fest_id, fest_name, session['club_id'], session.get('club_name','N/A'),
                           start_dt_utc.strftime(DATETIME_STORAGE_FORMAT), end_dt_utc.strftime(DATETIME_STORAGE_FORMAT),
                           reg_end_dt_utc.strftime(DATETIME_STORAGE_FORMAT), fest_details, is_published,
                           fest_venue, fest_guests, fest_image_link ];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}, ImgLink:'{fest_image_link}'");
            global _cached_fests_data_all, _cache_fests_timestamp_all; _cached_fests_data_all = None; _cache_fests_timestamp_all = None; print("INFO: All fests cache invalidated.")
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
            get_or_create_worksheet(g_client, safe_sheet_title, "Registrations", event_headers);
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e: print(f"ERROR: Create Fest write: {e}"); traceback.print_exc(); flash("DB write error.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})

# (club_dashboard, club_history, edit_fest, end_fest, delete_fest, fest_stats, export_excel, export_pdf routes - same as last full code, but ensure UTC time logic)
# ... I will re-paste club_dashboard with UTC logic as an example ...
@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now_utc = get_current_time_utc(); upcoming,ongoing = [],[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests dashboard: {e}"); flash("DB Error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name'), upcoming_fests=[], ongoing_fests=[])
    club_fests_all=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
    for fest in club_fests_all:
        try:
            start_time_utc = parse_datetime_as_utc(fest.get('StartTime'), is_from_sheet=True)
            end_time_utc = parse_datetime_as_utc(fest.get('EndTime'), is_from_sheet=True)
            if not (start_time_utc and end_time_utc): continue
            if now_utc < start_time_utc: upcoming.append(fest)
            elif start_time_utc <= now_utc < end_time_utc: ongoing.append(fest)
        except Exception as e: print(f"Error processing fest '{fest.get('FestName')}' for dashboard: {e}")
    upcoming.sort(key=lambda x: parse_datetime_as_utc(x.get('StartTime'), is_from_sheet=True) or datetime.max.replace(tzinfo=pytz.utc))
    ongoing.sort(key=lambda x: parse_datetime_as_utc(x.get('StartTime'), is_from_sheet=True) or datetime.min.replace(tzinfo=pytz.utc))
    return render_template('club_dashboard.html',club_name=session.get('club_name'), upcoming_fests=upcoming, ongoing_fests=ongoing)

# (Paste the rest of your club_history, edit_fest, end_fest, delete_fest, fest_stats, export_excel, export_pdf routes here,
# ensuring any time comparisons use now_utc and parse_datetime_as_utc(..., is_from_sheet=True) for sheet times)

# === Attendee Routes ===
@app.route('/events')
def live_events():
    now_utc = get_current_time_utc(); available_fests=[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for events: {e}"); flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    for fest in all_fests_data:
        is_published=str(fest.get('Published','')).strip().lower()=='yes'
        reg_end_time_utc = parse_datetime_as_utc(fest.get('RegistrationEndTime',''), is_from_sheet=True)
        start_time_utc = parse_datetime_as_utc(fest.get('StartTime',''), is_from_sheet=True)
        if is_published and reg_end_time_utc and start_time_utc and now_utc < reg_end_time_utc and now_utc < start_time_utc :
            available_fests.append(fest)
    available_fests.sort(key=lambda x: parse_datetime_as_utc(x.get('StartTime'), is_from_sheet=True) or datetime.max.replace(tzinfo=pytz.utc))
    return render_template('live_events.html', fests=available_fests)

@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info_dict=None; is_open_for_reg=False; now_utc = get_current_time_utc()
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for event_detail: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))
    fest_info_dict = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id_param), None)
    if not fest_info_dict: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    is_published = str(fest_info_dict.get('Published','')).lower()=='yes'
    reg_end_time_utc = parse_datetime_as_utc(fest_info_dict.get('RegistrationEndTime', ''), is_from_sheet=True)
    start_time_utc = parse_datetime_as_utc(fest_info_dict.get('StartTime',''), is_from_sheet=True)
    if is_published and reg_end_time_utc and start_time_utc and now_utc < reg_end_time_utc and now_utc < start_time_utc : is_open_for_reg = True
    return render_template('event_detail.html', fest=fest_info_dict, registration_open=is_open_for_reg)

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param):
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); mobile=request.form.get('mobile','').strip(); college=request.form.get('college','').strip();
    if not all([name,email,mobile,college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    if "@" not in email or "." not in email.split('@')[-1]: flash("Invalid email.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    try:
        g_client, _, _, _ = get_sheet_objects_cached(); all_fests=get_all_fests_cached()
        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==fest_id_param), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        now_utc = get_current_time_utc()
        reg_end_time_utc = parse_datetime_as_utc(fest_info.get('RegistrationEndTime', ''), is_from_sheet=True)
        start_time_utc = parse_datetime_as_utc(fest_info.get('StartTime', ''), is_from_sheet=True)
        if not reg_end_time_utc or not start_time_utc or now_utc >= reg_end_time_utc or now_utc >= start_time_utc:
            flash("Registration closed or event has already started.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
        reg_sheet=get_or_create_worksheet(g_client, individual_sheet_title,"Registrations",event_headers);
        if reg_sheet.findall(email, in_column=3): flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        user_id=generate_unique_id(); ts=datetime.now(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER); row=[user_id, name, email, mobile, college, 'no', ts];
        reg_sheet.append_row(row); print(f"JoinEvent: Appended registration for {email} to {individual_sheet_title}")
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20].replace(',',';')}"; img_qr_obj=qrcode.make(qr_data);
        buf = BytesIO(); img_qr_obj.save(buf, format="PNG");
        qr_image_base64 = base64.b64encode(buf.getvalue()).decode()
        safe_user_name_for_file = "".join(c if c.isalnum() or c in ['_','-',' '] else "" for c in str(name)).strip().replace(' ', '_') or "user"
        safe_fest_name_for_file = "".join(c if c.isalnum() or c in ['_','-',' '] else "" for c in str(fest_info.get('FestName','Event'))).strip().replace(' ', '_') or "event"
        download_filename = f"{safe_user_name_for_file}_QR_for_{safe_fest_name_for_file}_{user_id[:4]}.png"
        flash(f"Successfully registered for '{fest_info.get('FestName')}'! Your QR code is below and should download automatically as '{download_filename}'. If not, use the download link.", "success")
        return render_template( 'join_success.html', qr_image_base64=qr_image_base64, fest_name=fest_info.get('FestName','Event'), user_name=name, download_filename=download_filename, qr_image_data_url=f"data:image/png;base64,{qr_image_base64}" )
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
                all_fests_data = get_all_fests_cached();
                if all_fests_data is None: all_fests_data = [] 
                print(f"Security Login Attempt: User='{username}', EventPass='{event_name_password}'")
                valid_event = next((f for f in all_fests_data if
                                    str(f.get('FestName','')).strip() == event_name_password and
                                    str(f.get('Published','')).strip().lower() == 'yes'), None)
                if valid_event:
                    session['security_event_name'] = valid_event.get('FestName','N/A'); session['security_event_id'] = valid_event.get('FestID','N/A');
                    safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(valid_event.get('FestName','Event'))).strip() or "fest_event";
                    session['security_event_sheet_title']=f"{safe_base[:80]}_{valid_event.get('FestID','')}";
                    flash(f"Security access for: {session['security_event_name']}", "success"); print(f"Security Login SUCCESS for event: {session['security_event_name']}")
                    return redirect(url_for('security_scanner'));
                else: flash("Invalid event password or event inactive/unpublished.", "danger"); print(f"Security Login FAILED for EventPass='{event_name_password}'")
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
    now_utc = get_current_time_utc(); all_fests_data = get_all_fests_cached()
    current_event_id = session.get('security_event_id')
    event_info = next((f for f in all_fests_data if str(f.get('FestID','')) == current_event_id), None)
    if event_info:
        event_name_msg = event_info.get('FestName', 'The event')
        end_time_utc = parse_datetime_as_utc(event_info.get('EndTime'), is_from_sheet=True)
        if end_time_utc and now_utc >= end_time_utc:
             print(f"VerifyQR REJECTED: Scan attempt after event end for '{event_name_msg}'")
             return jsonify({'status':'error', 'message':f"'{event_name_msg}' has ended. No more check-ins."}), 403
    else: print(f"VerifyQR WARN: Event info not found for ID {current_event_id} for time check.");
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
        row_data=reg_sheet.row_values(cell.row); sheet_headers = reg_sheet.row_values(1)
        try: p_idx = sheet_headers.index('Present'); n_idx = sheet_headers.index('Name'); e_idx = sheet_headers.index('Email'); m_idx = sheet_headers.index('Mobile'); ts_idx = sheet_headers.index('Timestamp')
        except ValueError: return jsonify({'status':'error', 'message':'Reg sheet config error.'}), 500
        def get_val(idx, default=''): return row_data[idx] if len(row_data)>idx else default
        status = get_val(p_idx).strip().lower(); name = get_val(n_idx); email = get_val(e_idx); mobile = get_val(m_idx)
        current_scan_timestamp = datetime.now(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER)
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
    print(f"INFO: Input timezone for event times assumed to be: {YOUR_LOCAL_TIMEZONE_STR} (UTC{YOUR_LOCAL_TIMEZONE.utcoffset(datetime.now())})")
    print("----- Application Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    if not FEST_IMAGES_DRIVE_FOLDER_ID: print("\n🔴 WARNING: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID not set. Image uploads will fail.\n")
    if not MASTER_SHEET_ID: print("\n🔴 WARNING: MASTER_SHEET_ID not set. Opening master sheet relies on name search.\n")
    
    is_main_process = os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    if is_main_process:
        if not os.environ.get('FLASK_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY') == "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV":
            print("\n🔴 WARNING: FLASK_SECRET_KEY not securely set for main process.\n")
        print("Flask starting up - Main process: Initializing...")
        initialize_application_on_startup()
        print("Flask startup - Main process: Initialization complete.")
    else: print("Flask starting up - Reloader process detected.")
    
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting Flask server (host=0.0.0.0, port={port})...")
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=True)
