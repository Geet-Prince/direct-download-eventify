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
# from fpdf import FPDF # Removed as it was unused
# import pandas as pd # Removed as it was unused
import gspread
from oauth2client.service_account import ServiceAccountCredentials as GSpreadServiceAccountCredentials
import qrcode
from werkzeug.utils import secure_filename
import pytz

# For Google Drive API
from google.oauth2.service_account import Credentials as GoogleAuthServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# from dotenv import load_dotenv
# load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    print("🔴 FATAL: FLASK_SECRET_KEY is not set. Using a temporary key for local dev, but this WILL FAIL in production or if app.debug is False.")
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug: # Check if it's the main Flask process or not in debug
        raise ValueError("FLASK_SECRET_KEY is not set in the environment. This is required for production.")
    app.secret_key = "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV"


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
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M' 
DATETIME_STORAGE_FORMAT = '%Y-%m-%dT%H:%M:%SZ' 
DATETIME_DISPLAY_FORMAT_USER = '%Y-%m-%d %I:%M %p' 
DATETIME_DISPLAY_SHEET_TS = '%Y-%m-%d %H:%M:%S' 
DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE = [ DATETIME_SHEET_FORMAT, '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S' ]
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

def parse_datetime_as_utc(dt_str_from_input, is_from_sheet=True):
    if not dt_str_from_input:
        print(f"DEBUG parse_datetime_as_utc: Received empty input.")
        return None
    dt_str = str(dt_str_from_input).strip()
    print(f"DEBUG parse_datetime_as_utc: Attempting to parse '{dt_str}', is_from_sheet={is_from_sheet}")

    if dt_str.endswith('Z'):
        try:
            dt_obj = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            print(f"DEBUG parse_datetime_as_utc: Parsed '{dt_str}' with fromisoformat (Z suffix) to {dt_obj}")
            return dt_obj.astimezone(pytz.utc)
        except ValueError as e_iso:
            print(f"DEBUG parse_datetime_as_utc: fromisoformat failed for '{dt_str}': {e_iso}. Falling through.")
            pass

    parsed_naive = None
    successful_fmt = None
    formats_to_try = DATETIME_INPUT_FORMATS_FOR_SHEET_PARSE if is_from_sheet else DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE

    for fmt_idx, fmt in enumerate(formats_to_try):
        try:
            parsed_naive = datetime.strptime(dt_str, fmt)
            successful_fmt = fmt
            print(f"DEBUG parse_datetime_as_utc: Parsed '{dt_str}' with format '{fmt}' (index {fmt_idx}) to naive {parsed_naive}")
            break
        except (ValueError, TypeError):
            # print(f"DEBUG parse_datetime_as_utc: Format '{fmt}' failed for '{dt_str}'") # Can be very verbose
            continue
    
    if not parsed_naive:
        print(f"WARN parse_datetime_as_utc: Could not parse datetime string '{dt_str}' with any known formats.")
        return None

    assumed_input_tz = YOUR_LOCAL_TIMEZONE
    if is_from_sheet and successful_fmt == DATETIME_STORAGE_FORMAT:
        assumed_input_tz = pytz.utc
        print(f"DEBUG parse_datetime_as_utc: Assuming UTC for sheet input with DATETIME_STORAGE_FORMAT ('{successful_fmt}')")
    elif not is_from_sheet:
         print(f"DEBUG parse_datetime_as_utc: Assuming YOUR_LOCAL_TIMEZONE ('{YOUR_LOCAL_TIMEZONE.zone}') for form input ('{successful_fmt}')")
    else: # is_from_sheet but not DATETIME_STORAGE_FORMAT
         print(f"DEBUG parse_datetime_as_utc: Assuming YOUR_LOCAL_TIMEZONE ('{YOUR_LOCAL_TIMEZONE.zone}') for sheet input with format '{successful_fmt}'")


    try:
        if parsed_naive.tzinfo is not None: # Already timezone-aware (shouldn't happen with strptime)
            print(f"DEBUG parse_datetime_as_utc: Naive object {parsed_naive} surprisingly has tzinfo. Converting to UTC.")
            return parsed_naive.astimezone(pytz.utc)
        
        localized_dt = assumed_input_tz.localize(parsed_naive, is_dst=None)
        utc_dt = localized_dt.astimezone(pytz.utc)
        print(f"DEBUG parse_datetime_as_utc: Localized {parsed_naive} to {localized_dt} ({assumed_input_tz.zone}), then converted to UTC: {utc_dt}")
        return utc_dt
    
    except (pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError) as e_loc_specific:
        print(f"ERROR parse_datetime_as_utc: Timezone localization (Ambiguous/NonExistent) for '{dt_str}' (parsed as {parsed_naive}) with tz '{assumed_input_tz.zone}': {e_loc_specific}. Fallback to naive UTC.")
        return pytz.utc.localize(parsed_naive) 
    except Exception as e_loc_generic:
        print(f"ERROR parse_datetime_as_utc: General timezone conversion error for '{dt_str}' (parsed as {parsed_naive}) with tz '{assumed_input_tz.zone}': {e_loc_generic}. Fallback to naive UTC.")
        return pytz.utc.localize(parsed_naive)


def get_current_time_utc(): return datetime.now(timezone.utc)

def _initialize_master_sheets_internal():
    global master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global
    if master_spreadsheet_obj_global and clubs_sheet_obj_global and fests_sheet_obj_global:
        return get_gspread_client_cached(), master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global
    print("Initializing master sheet objects (one-time per worker)...")
    client = get_gspread_client_cached()
    spreadsheet = None
    if MASTER_SHEET_ID:
        try:
            print(f"Opening master SS by ID (key): '{MASTER_SHEET_ID}'")
            spreadsheet = client.open_by_key(MASTER_SHEET_ID)
            print(f"Opened master SS: '{spreadsheet.title}' (ID: {spreadsheet.id})")
        except gspread.exceptions.SpreadsheetNotFound:
            print(f"WARN: Master SS with ID '{MASTER_SHEET_ID}' not found. Will try by name/create.")
            spreadsheet = None
        except Exception as e_id:
            print(f"WARN: Could not open master SS by ID '{MASTER_SHEET_ID}': {e_id}")
            traceback.print_exc(); spreadsheet = None
    if not spreadsheet:
        try:
            print(f"Attempting to open master SS by name: '{MASTER_SHEET_NAME}'")
            spreadsheet = client.open(MASTER_SHEET_NAME)
            print(f"Opened master SS by name: '{spreadsheet.title}' (ID: {spreadsheet.id})")
        except gspread.exceptions.SpreadsheetNotFound:
            print(f"Master SS '{MASTER_SHEET_NAME}' not found by name. Creating...")
            try:
                spreadsheet = client.create(MASTER_SHEET_NAME)
                print(f"Created master SS '{MASTER_SHEET_NAME}' (ID: {spreadsheet.id}).")
                if YOUR_PERSONAL_EMAIL and spreadsheet:
                    share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
            except Exception as e_create:
                print(f"CRITICAL ERROR creating master SS: {e_create}"); traceback.print_exc(); raise
        except Exception as e_name:
            print(f"CRITICAL ERROR opening master SS by name '{MASTER_SHEET_NAME}': {e_name}"); traceback.print_exc(); raise
    if not spreadsheet: raise Exception("FATAL: Failed to open or create master spreadsheet after all attempts.")
    master_spreadsheet_obj_global = spreadsheet
    clubs_headers=['ClubID','ClubName','Email','PasswordHash']
    fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests','FestImageLink']
    try:
        clubs_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Clubs")
        print("Found 'Clubs' ws.")
        current_club_headers = clubs_sheet_obj_global.row_values(1) if clubs_sheet_obj_global.row_count >=1 else []
        if not current_club_headers: print("Clubs sheet empty/headerless, appending headers..."); clubs_sheet_obj_global.append_row(clubs_headers)
        elif current_club_headers != clubs_headers: print(f"WARN: 'Clubs' sheet headers mismatch! Sheet: {current_club_headers}, Expected: {clubs_headers}")
    except gspread.exceptions.WorksheetNotFound:
        print("'Clubs' ws not found. Creating..."); clubs_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Clubs",rows=1, cols=len(clubs_headers))
        clubs_sheet_obj_global.append_row(clubs_headers); clubs_sheet_obj_global.resize(rows=100); print("'Clubs' ws created.")
    except Exception as e_club_ws: print(f"ERROR during 'Clubs' worksheet setup: {e_club_ws}"); traceback.print_exc(); raise
    try:
        fests_sheet_obj_global = master_spreadsheet_obj_global.worksheet("Fests")
        print("Found 'Fests' ws.")
        current_fests_headers = fests_sheet_obj_global.row_values(1) if fests_sheet_obj_global.row_count >= 1 else []
        expected_num_cols_fests = len(fests_headers); current_num_cols_in_fests_sheet = fests_sheet_obj_global.col_count
        if not current_fests_headers:
            print("Fests sheet empty/headerless, ensuring columns and appending expected headers...")
            if current_num_cols_in_fests_sheet < expected_num_cols_fests: fests_sheet_obj_global.add_cols(expected_num_cols_fests - current_num_cols_in_fests_sheet); print(f"Added columns to Fests sheet.")
            if fests_sheet_obj_global.row_count > 0 and fests_sheet_obj_global.get_all_values(): fests_sheet_obj_global.clear()
            fests_sheet_obj_global.append_row(fests_headers); print("Appended new headers to Fests sheet.")
        elif current_fests_headers != fests_headers:
            print(f"INFO: 'Fests' sheet headers differ. Current:{current_fests_headers}, Expected:{fests_headers}")
            if 'FestImageLink' not in current_fests_headers and current_fests_headers == fests_headers[:-1]:
                print("Attempting to add 'FestImageLink' header...")
                if current_num_cols_in_fests_sheet < expected_num_cols_fests: fests_sheet_obj_global.add_cols(1); print("Added 1 column for FestImageLink.")
                try: fests_sheet_obj_global.update_cell(1, len(fests_headers), 'FestImageLink'); print(f"'FestImageLink' header added/updated in column {len(fests_headers)}")
                except Exception as he: print(f"ERROR adding/updating 'FestImageLink' header: {he}. Manual check advised.")
            else: print("WARN: Fests sheet headers differ in other ways or FestImageLink is not simply the last missing. Manual review needed.")
        else:
            print("Fests sheet headers appear correct.")
            if current_num_cols_in_fests_sheet < expected_num_cols_fests: fests_sheet_obj_global.add_cols(expected_num_cols_fests - current_num_cols_in_fests_sheet); print(f"Ensured Fests sheet has enough columns.")
    except gspread.exceptions.WorksheetNotFound:
        print("'Fests' ws not found. Creating with all headers..."); fests_sheet_obj_global = master_spreadsheet_obj_global.add_worksheet(title="Fests",rows=1,cols=len(fests_headers));
        fests_sheet_obj_global.append_row(fests_headers); fests_sheet_obj_global.resize(rows=100); print("'Fests' ws created.")
    except Exception as e_fests_ws: print(f"ERROR during 'Fests' worksheet setup: {e_fests_ws}"); traceback.print_exc(); raise
    print("Master sheets and tabs initialized globally."); return client, master_spreadsheet_obj_global, clubs_sheet_obj_global, fests_sheet_obj_global

def get_sheet_objects_cached(): return _initialize_master_sheets_internal()

def get_all_fests_cached():
    global _cached_fests_data_all, _cache_fests_timestamp_all; now = datetime.now()
    if _cached_fests_data_all and _cache_fests_timestamp_all and (now - _cache_fests_timestamp_all < CACHE_FESTS_DURATION):
        print("Returning cached fests data."); return _cached_fests_data_all
    print("Fetching fresh fests data from sheet..."); _, _, _, fests_sheet = get_sheet_objects_cached()
    try: _cached_fests_data_all = fests_sheet.get_all_records(); _cache_fests_timestamp_all = now
    except Exception as e: print(f"ERROR fetching all fests: {e}. Returning last cache or empty."); traceback.print_exc(); return _cached_fests_data_all or []
    return _cached_fests_data_all

def generate_unique_id(): return str(uuid.uuid4().hex)[:10]

def hash_password(password):
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(stored_hash, provided_password):
    return stored_hash == hash_password(provided_password)

def upload_to_drive(file_stream, filename, folder_id):
    try:
        drive_service = get_drive_service_cached()
        file_metadata = {'name': filename, 'parents': [folder_id]}
        media = MediaIoBaseUpload(file_stream, mimetype='application/octet-stream', resumable=True)
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        print(f"Uploaded '{filename}' to Drive. File ID: {file.get('id')}, Link: {file.get('webViewLink')}")
        return file.get('webViewLink')
    except Exception as e:
        print(f"ERROR uploading '{filename}' to Drive: {e}"); traceback.print_exc(); return None

def share_spreadsheet_with_editor(spreadsheet_obj, email, sheet_name_for_log):
    try:
        spreadsheet_obj.share(email, perm_type='user', role='writer')
        print(f"Shared '{sheet_name_for_log}' with {email} as editor.")
    except Exception as e_share:
        print(f"WARN: Failed to share '{sheet_name_for_log}' with {email}: {e_share}")

def get_or_create_worksheet(gspread_client_or_spreadsheet_obj, spreadsheet_title_or_id_or_ss_obj, worksheet_title, headers_list):
    ss = None
    try:
        if isinstance(spreadsheet_title_or_id_or_ss_obj, gspread.Spreadsheet):
            ss = spreadsheet_title_or_id_or_ss_obj
            # print(f"DEBUG get_or_create_worksheet: Using provided gspread.Spreadsheet object for '{ss.title}'")
        elif isinstance(spreadsheet_title_or_id_or_ss_obj, str):
            client_to_use = gspread_client_or_spreadsheet_obj if isinstance(gspread_client_or_spreadsheet_obj, gspread.Client) else get_gspread_client_cached()
            if len(spreadsheet_title_or_id_or_ss_obj) > 40: # Heuristic for ID
                # print(f"DEBUG get_or_create_worksheet: Opening spreadsheet by ID '{spreadsheet_title_or_id_or_ss_obj}'")
                ss = client_to_use.open_by_key(spreadsheet_title_or_id_or_ss_obj)
            else:
                # print(f"DEBUG get_or_create_worksheet: Opening spreadsheet by title '{spreadsheet_title_or_id_or_ss_obj}'")
                ss = client_to_use.open(spreadsheet_title_or_id_or_ss_obj)
        else:
            raise ValueError("First argument to get_or_create_worksheet must be a gspread Client or Spreadsheet object, and second must be title, ID, or Spreadsheet object.")
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"ERROR get_or_create_worksheet: Spreadsheet '{spreadsheet_title_or_id_or_ss_obj}' not found.")
        raise
    except Exception as e_open:
        print(f"ERROR get_or_create_worksheet: Error opening spreadsheet '{spreadsheet_title_or_id_or_ss_obj}': {e_open}")
        raise
    
    if not ss:
        print(f"CRITICAL get_or_create_worksheet: Spreadsheet object could not be obtained for '{spreadsheet_title_or_id_or_ss_obj}'.")
        raise gspread.exceptions.SpreadsheetNotFound(f"Spreadsheet object invalid for {spreadsheet_title_or_id_or_ss_obj}")


    try:
        worksheet = ss.worksheet(worksheet_title)
        # print(f"DEBUG get_or_create_worksheet: Found worksheet '{worksheet_title}' in '{ss.title}'.")
        current_headers = worksheet.row_values(1) if worksheet.row_count >= 1 else []
        if not current_headers and headers_list:
            print(f"INFO get_or_create_worksheet: Worksheet '{worksheet_title}' is empty, appending headers: {headers_list}")
            worksheet.append_row(headers_list)
        elif current_headers != headers_list and headers_list:
            print(f"WARN get_or_create_worksheet: Headers mismatch in '{worksheet_title}'. Sheet: {current_headers}, Expected: {headers_list}.")
        return worksheet
    except gspread.exceptions.WorksheetNotFound:
        print(f"INFO get_or_create_worksheet: Worksheet '{worksheet_title}' not found in '{ss.title}'. Creating...")
        if not headers_list:
            print(f"WARN get_or_create_worksheet: No headers provided for new worksheet '{worksheet_title}'. Creating empty.")
            worksheet = ss.add_worksheet(title=worksheet_title, rows=1, cols=1) # Create minimal
        else:
            worksheet = ss.add_worksheet(title=worksheet_title, rows=1, cols=len(headers_list))
            worksheet.append_row(headers_list)
            print(f"INFO get_or_create_worksheet: Created worksheet '{worksheet_title}' with headers: {headers_list}")
        return worksheet
    except Exception as e_ws:
        print(f"ERROR get_or_create_worksheet: Error getting/creating worksheet '{worksheet_title}': {e_ws}")
        raise

@app.context_processor
def inject_now_local(): return {'now_local': datetime.now(YOUR_LOCAL_TIMEZONE)}

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
        print(f"DEBUG LOGIN: Attempt. Email: '{email_form}', Pass: '{'******' if password_form else ''}'")
        if not email_form or not password_form: flash("Email/pass required.", "danger"); return render_template('club_login.html')
        if "@" not in email_form or "." not in email_form.split('@')[-1]: flash("Invalid email.", "danger"); return render_template('club_login.html')
        try: _, _, clubs_sheet, _ = get_sheet_objects_cached()
        except Exception as e: print(f"ERROR LOGIN Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_login.html')
        try: cell = clubs_sheet.find(email_form, in_column=3)
        except gspread.exceptions.CellNotFound: print(f"DEBUG LOGIN: Email not found '{email_form}'"); flash("Invalid email or password.", "danger"); return render_template('club_login.html')
        
        if cell:
            try:
                club_data=clubs_sheet.row_values(cell.row)
                if len(club_data) < 4: flash("Login error: Incomplete club data.", "danger"); return render_template('club_login.html')
                stored_club_id, stored_name, stored_email, stored_hash = club_data[0].strip(), club_data[1].strip(), club_data[2].strip().lower(), club_data[3].strip()
                
                if stored_email != email_form:
                    print(f"DEBUG LOGIN: Email mismatch. Form: '{email_form}', Sheet: '{stored_email}' at row {cell.row}.")
                    flash("Internal login error.", "danger"); return render_template('club_login.html')

                if verify_password(stored_hash, password_form):
                    session['club_id']=stored_club_id; session['club_name']=stored_name
                    flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
                else: flash("Invalid email or password.", "danger")
            except Exception as e: print(f"ERROR LOGIN Logic: {e}"); traceback.print_exc(); flash("Login logic error.", "danger")
        else: flash("Invalid email or password.", "danger")
    return render_template('club_login.html')

@app.route('/club/logout')
def club_logout(): session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))


@app.route('/club/create_fest', methods=['GET', 'POST'])
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
        
        start_dt_utc, end_dt_utc, reg_end_dt_utc = None, None, None
        try:
             start_dt_utc = parse_datetime_as_utc(start_time_str, is_from_sheet=False)
             end_dt_utc = parse_datetime_as_utc(end_time_str, is_from_sheet=False)
             reg_end_dt_utc = parse_datetime_as_utc(registration_end_time_str, is_from_sheet=False)

             if not all([start_dt_utc, end_dt_utc, reg_end_dt_utc]):
                 error_fields = []
                 if not start_dt_utc: error_fields.append("Start Time")
                 if not end_dt_utc: error_fields.append("End Time")
                 if not reg_end_dt_utc: error_fields.append("Registration Deadline")
                 flash(f"Invalid date/time format for: {', '.join(error_fields)}. Please use YYYY-MM-DDTHH:MM.", "danger")
                 return render_template('create_fest.html', form_data=form_data_to_pass)
             
             if not (start_dt_utc < end_dt_utc):
                 flash("End time must be after start time.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             if not (reg_end_dt_utc <= start_dt_utc):
                 flash("Registration deadline must be on or before the start time.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        except Exception as e_parse:
             print(f"Error parsing/validating fest times: {e_parse}"); flash(f"Invalid date/time input: {e_parse}", "danger");
             return render_template('create_fest.html', form_data=form_data_to_pass)
        
        try:
            g_client, master_ss_obj, _, master_fests_sheet = get_sheet_objects_cached() # Get master_ss_obj
            fest_id=generate_unique_id();
            new_fest_row=[ fest_id, fest_name, session['club_id'], session.get('club_name','N/A'),
                           start_dt_utc.strftime(DATETIME_STORAGE_FORMAT), 
                           end_dt_utc.strftime(DATETIME_STORAGE_FORMAT),  
                           reg_end_dt_utc.strftime(DATETIME_STORAGE_FORMAT),
                           fest_details, is_published,
                           fest_venue, fest_guests, fest_image_link ];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}, ImgLink:'{fest_image_link}'");
            
            global _cached_fests_data_all, _cache_fests_timestamp_all;
            _cached_fests_data_all = None; _cache_fests_timestamp_all = None;
            print("INFO: All fests cache invalidated after creating new fest.")

            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; 
            event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp']; 
            
            if not master_ss_obj:
                 print(f"CRITICAL: master_spreadsheet_obj_global is None during worksheet creation for {fest_id}. Skipping individual sheet creation.")
                 flash("Fest created, but registration sheet setup failed. Contact admin.", "warning")
            else:
                 get_or_create_worksheet(master_ss_obj, master_ss_obj.id, safe_sheet_title, event_headers)


            flash(f"Fest '{fest_name}' created successfully!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e:
            print(f"ERROR: Create Fest (Sheet/Drive Operations): {e}"); traceback.print_exc();
            flash("Database write error or error creating event registration sheet.", "danger");
            return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})


@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session:
        flash("Login required to access the dashboard.", "warning")
        return redirect(url_for('club_login'))

    club_name = session.get('club_name', 'Club')
    current_club_id = session['club_id']
    
    try:
        all_fests_data_raw = get_all_fests_cached()
        if all_fests_data_raw is None: all_fests_data_raw = []
    except Exception as e:
        print(f"ERROR fetching cached fests for club dashboard: {e}")
        flash("Error loading dashboard data. Please try again.", "danger")
        all_fests_data_raw = []

    club_fests_raw = [fest for fest in all_fests_data_raw if str(fest.get('ClubID','')) == str(current_club_id)]
    now_utc = get_current_time_utc()
    processed_club_fests = []

    for fest_data in club_fests_raw:
        fest = fest_data.copy() 

        start_time_utc_obj = parse_datetime_as_utc(fest.get('StartTime'), is_from_sheet=True)
        end_time_utc_obj = parse_datetime_as_utc(fest.get('EndTime'), is_from_sheet=True)
        reg_end_time_utc_obj = parse_datetime_as_utc(fest.get('RegistrationEndTime'), is_from_sheet=True)

        fest['start_time_obj_utc'] = start_time_utc_obj
        fest['end_time_obj_utc'] = end_time_utc_obj
        
        fest['status_class'] = 'text-muted' 
        if start_time_utc_obj and end_time_utc_obj: # Both must be valid
            if now_utc < start_time_utc_obj:
                fest['status_display'] = "Upcoming"
                fest['status_class'] = 'badge bg-info text-dark'
            elif now_utc <= end_time_utc_obj: # now is between start and end
                fest['status_display'] = "Ongoing"
                fest['status_class'] = 'badge bg-success'
            else: # now is after end
                fest['status_display'] = "Ended"
                fest['status_class'] = 'badge bg-secondary' # Changed from danger for less alarm
        elif start_time_utc_obj and not end_time_utc_obj: # Start valid, end not
            if now_utc < start_time_utc_obj:
                 fest['status_display'] = "Upcoming (End Time Missing)"
                 fest['status_class'] = 'badge bg-info text-dark'
            else: # Event has started, but no end time
                 fest['status_display'] = "Ongoing (End Time Missing)"
                 fest['status_class'] = 'badge bg-success'
        else: # Start time missing or both missing
            fest['status_display'] = "Unknown (Time Data Invalid)"
            fest['status_class'] = 'badge bg-warning text-dark'
        
        fest['start_time_display'] = start_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if start_time_utc_obj else "N/A"
        fest['end_time_display'] = end_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if end_time_utc_obj else "N/A"
        fest['reg_end_time_display'] = reg_end_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if reg_end_time_utc_obj else "N/A"
        
        fest['is_published'] = str(fest.get('Published','no')).strip().lower() == 'yes'
        processed_club_fests.append(fest)

    def sort_key_dashboard(f):
        st_obj = f.get('start_time_obj_utc')
        st_obj_for_sort = st_obj if st_obj else datetime.min.replace(tzinfo=pytz.utc) # Fallback for sorting
        status_order = {"Ongoing": 0, "Upcoming": 1, "Ended": 2, "Unknown": 3} # Default for others
        current_status_display = f.get('status_display', "Unknown")
        # Handle cases where status_display might contain more text like "(End Time Missing)"
        main_status = current_status_display.split(" (")[0]
        return (status_order.get(main_status, 99), st_obj_for_sort)

    processed_club_fests.sort(key=sort_key_dashboard)
    return render_template('club_dashboard.html', 
                           club_name=club_name, 
                           fests=processed_club_fests)

# ***** ADDED club_history ROUTE *****
@app.route('/club/history')
def club_history():
    if 'club_id' not in session:
        flash("Login required to access history.", "warning")
        return redirect(url_for('club_login'))

    club_name = session.get('club_name', 'Club')
    current_club_id = session['club_id']
    
    try:
        all_fests_data_raw = get_all_fests_cached()
        if all_fests_data_raw is None: all_fests_data_raw = []
    except Exception as e:
        print(f"ERROR fetching cached fests for club history: {e}")
        flash("Error loading history data. Please try again.", "danger")
        all_fests_data_raw = []

    club_fests_raw = [fest for fest in all_fests_data_raw if str(fest.get('ClubID','')) == str(current_club_id)]
    now_utc = get_current_time_utc()
    processed_club_fests_history = []

    for fest_data in club_fests_raw:
        fest = fest_data.copy() 

        start_time_utc_obj = parse_datetime_as_utc(fest.get('StartTime'), is_from_sheet=True)
        end_time_utc_obj = parse_datetime_as_utc(fest.get('EndTime'), is_from_sheet=True)
        # Reg end time might not be directly relevant for history view status, but good to parse
        reg_end_time_utc_obj = parse_datetime_as_utc(fest.get('RegistrationEndTime'), is_from_sheet=True)

        fest['start_time_obj_utc'] = start_time_utc_obj
        fest['end_time_obj_utc'] = end_time_utc_obj
        
        # Status logic for history (could be simplified if only showing "Ended")
        fest['status_class'] = 'text-muted' 
        if start_time_utc_obj and end_time_utc_obj:
            if now_utc < start_time_utc_obj:
                fest['status_display'] = "Upcoming"
                fest['status_class'] = 'badge bg-info text-dark'
            elif now_utc <= end_time_utc_obj:
                fest['status_display'] = "Ongoing"
                fest['status_class'] = 'badge bg-success'
            else:
                fest['status_display'] = "Ended"
                fest['status_class'] = 'badge bg-secondary' 
        elif start_time_utc_obj and not end_time_utc_obj:
            if now_utc < start_time_utc_obj: fest['status_display'] = "Upcoming (End Time Missing)"
            else: fest['status_display'] = "Ongoing (End Time Missing)"
            fest['status_class'] = 'badge bg-warning text-dark'
        else:
            fest['status_display'] = "Unknown (Time Data Invalid)"
            fest['status_class'] = 'badge bg-danger text-dark' # More prominent for invalid data
        
        fest['start_time_display'] = start_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if start_time_utc_obj else "N/A"
        fest['end_time_display'] = end_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if end_time_utc_obj else "N/A"
        fest['reg_end_time_display'] = reg_end_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if reg_end_time_utc_obj else "N/A"
        
        fest['is_published'] = str(fest.get('Published','no')).strip().lower() == 'yes'
        processed_club_fests_history.append(fest)

    # Sort for history: primarily Ended events first, then by end time (most recent ended first)
    def sort_key_history(f):
        et_obj = f.get('end_time_obj_utc')
        # For sorting, use a very early date for ongoing/upcoming so they appear after ended if not filtered out
        et_obj_for_sort = et_obj if et_obj else datetime.min.replace(tzinfo=pytz.utc) 
        
        status_order = {"Ended": 0, "Ongoing": 1, "Upcoming": 2, "Unknown": 3}
        main_status = f.get('status_display', "Unknown").split(" (")[0]
        
        # If it's an "Ended" event, sort by its actual end time (descending for most recent first)
        # Otherwise, use a fixed order for other statuses.
        sort_val = -et_obj_for_sort.timestamp() if main_status == "Ended" and et_obj else 0

        return (status_order.get(main_status, 99), sort_val)

    processed_club_fests_history.sort(key=sort_key_history)
    
    # You'll need to create 'club_history.html' template
    return render_template('club_history.html', 
                           club_name=club_name, 
                           fests=processed_club_fests_history)
# ***** END club_history ROUTE *****


# === Attendee Routes ===
@app.route('/events')
def live_events():
    now_utc = get_current_time_utc(); available_fests=[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for events: {e}"); flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    
    if all_fests_data is None: all_fests_data = [] 

    for fest_data in all_fests_data:
        fest = fest_data.copy() 
        is_published=str(fest.get('Published','')).strip().lower()=='yes'
        
        reg_end_time_utc = parse_datetime_as_utc(fest.get('RegistrationEndTime',''), is_from_sheet=True)
        start_time_utc = parse_datetime_as_utc(fest.get('StartTime',''), is_from_sheet=True)
        
        if is_published and reg_end_time_utc and start_time_utc and now_utc < reg_end_time_utc and now_utc < start_time_utc :
            fest['start_time_display'] = start_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if start_time_utc else "N/A"
            fest['reg_end_time_display'] = reg_end_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if reg_end_time_utc else "N/A"
            available_fests.append(fest)
            
    available_fests.sort(key=lambda x: (parse_datetime_as_utc(x.get('StartTime'), is_from_sheet=True) or datetime.max.replace(tzinfo=pytz.utc)))
    return render_template('live_events.html', fests=available_fests)


@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info_dict=None; is_open_for_reg=False; now_utc = get_current_time_utc()
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for event_detail: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))

    if all_fests_data is None: all_fests_data = [] 
    
    fest_info_dict_raw = next((f for f in all_fests_data if str(f.get('FestID',''))==str(fest_id_param)), None)
    
    if not fest_info_dict_raw: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    
    fest_info_dict = fest_info_dict_raw.copy()

    is_published = str(fest_info_dict.get('Published','')).lower()=='yes'
    reg_end_time_utc = parse_datetime_as_utc(fest_info_dict.get('RegistrationEndTime', ''), is_from_sheet=True)
    start_time_utc = parse_datetime_as_utc(fest_info_dict.get('StartTime',''), is_from_sheet=True)
    end_time_utc = parse_datetime_as_utc(fest_info_dict.get('EndTime',''), is_from_sheet=True)

    if is_published and reg_end_time_utc and start_time_utc and \
       now_utc < reg_end_time_utc and now_utc < start_time_utc :
        is_open_for_reg = True
    
    fest_info_dict['start_time_display'] = start_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if start_time_utc else "N/A"
    fest_info_dict['end_time_display'] = end_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if end_time_utc else "N/A"
    fest_info_dict['reg_end_time_display'] = reg_end_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if reg_end_time_utc else "N/A"
    
    return render_template('event_detail.html', fest=fest_info_dict, registration_open=is_open_for_reg)

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param):
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); mobile=request.form.get('mobile','').strip(); college=request.form.get('college','').strip();
    if not all([name,email,mobile,college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    if "@" not in email or "." not in email.split('@')[-1]: flash("Invalid email.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    
    try:
        g_client, master_ss_obj, _, _ = get_sheet_objects_cached() 
        all_fests=get_all_fests_cached()
        if all_fests is None: all_fests = []

        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==str(fest_id_param)), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        
        now_utc = get_current_time_utc()
        reg_end_time_utc = parse_datetime_as_utc(fest_info.get('RegistrationEndTime', ''), is_from_sheet=True)
        start_time_utc = parse_datetime_as_utc(fest_info.get('StartTime', ''), is_from_sheet=True)
        
        if not (reg_end_time_utc and start_time_utc and \
                now_utc < reg_end_time_utc and now_utc < start_time_utc):
            flash("Registration closed or event has already started.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        
        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}";
        event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp']; 
        
        if not master_ss_obj:
            print(f"CRITICAL: master_spreadsheet_obj_global is None during join_event for fest {fest_info['FestID']}.")
            flash("Critical server error: Cannot access event data.", "danger")
            return redirect(url_for('event_detail', fest_id_param=fest_id_param))

        reg_sheet = get_or_create_worksheet(master_ss_obj, master_ss_obj.id, individual_sheet_title, event_headers)
        
        if reg_sheet.findall(email, in_column=3): 
            flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning");
            return redirect(url_for('event_detail', fest_id_param=fest_id_param));
            
        user_id=generate_unique_id();
        ts_registration = datetime.now(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER);
        row=[user_id, name, email, mobile, college, 'no', ts_registration];
        reg_sheet.append_row(row); print(f"JoinEvent: Appended registration for {email} to {individual_sheet_title}")
        
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20].replace(',',';')}"; img_qr_obj=qrcode.make(qr_data);
        buf = BytesIO(); img_qr_obj.save(buf, format="PNG");
        qr_image_base64 = base64.b64encode(buf.getvalue()).decode()
        
        safe_user_name_for_file = "".join(c if c.isalnum() or c in ['_','-',' '] else "" for c in str(name)).strip().replace(' ', '_') or "user"
        safe_fest_name_for_file = "".join(c if c.isalnum() or c in ['_','-',' '] else "" for c in str(fest_info.get('FestName','Event'))).strip().replace(' ', '_') or "event"
        download_filename = f"{safe_user_name_for_file}_QR_for_{safe_fest_name_for_file}_{user_id[:4]}.png"
        
        flash(f"Successfully registered for '{fest_info.get('FestName')}'! Your QR code is below.", "success")
        return render_template( 'join_success.html', qr_image_base64=qr_image_base64, fest_name=fest_info.get('FestName','Event'), user_name=name, download_filename=download_filename, qr_image_data_url=f"data:image/png;base64,{qr_image_base64}" )
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"ERROR JoinEvent (SpreadsheetNotFound): Fest ID {fest_id_param}.");
        flash("Registration error: Event data sheet missing or inaccessible.", "danger");
        return redirect(url_for('event_detail', fest_id_param=fest_id_param))
    except Exception as e:
        print(f"ERROR JoinEvent: {e}"); traceback.print_exc();
        flash("An unexpected registration error occurred.", "danger");
        return redirect(url_for('event_detail', fest_id_param=fest_id_param));

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
                
                valid_event = None
                for fest_item in all_fests_data:
                    if str(fest_item.get('FestName','')).strip() == event_name_password and \
                       str(fest_item.get('Published','')).strip().lower() == 'yes':
                        
                        end_time_utc = parse_datetime_as_utc(fest_item.get('EndTime'), is_from_sheet=True)
                        now_utc = get_current_time_utc()
                        
                        if end_time_utc is None: # If no end time, consider it valid (or handle as error)
                            valid_event = fest_item
                            print(f"Security login: Event '{fest_item.get('FestName')}' has no end time, allowing access.")
                            break
                        # Allow login if event not ended or ended within grace period (e.g., 12 hours)
                        elif now_utc < (end_time_utc + timedelta(hours=12)):
                             valid_event = fest_item
                             break
                        else:
                            print(f"Security login: Event '{fest_item.get('FestName')}' ended at {end_time_utc}, too long ago.")
                
                if valid_event:
                    session['security_event_name'] = valid_event.get('FestName','N/A');
                    session['security_event_id'] = valid_event.get('FestID','N/A');
                    safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(valid_event.get('FestName','Event'))).strip() or "fest_event";
                    session['security_event_sheet_title']=f"{safe_base[:80]}_{valid_event.get('FestID','')}";
                    flash(f"Security access granted for: {session['security_event_name']}", "success");
                    print(f"Security Login SUCCESS for event: {session['security_event_name']}")
                    return redirect(url_for('security_scanner'));
                else:
                    flash("Invalid event password, event not published, or event has ended too long ago.", "danger");
                    print(f"Security Login FAILED for EventPass='{event_name_password}'")
            except Exception as e:
                print(f"ERROR: Security login failed: {e}"); traceback.print_exc();
                flash("Security login system error.", "danger")
        else:
            flash("Invalid security username.", "danger")
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
    if 'security_event_sheet_title' not in session or 'security_event_id' not in session:
        return jsonify({'status': 'error', 'message': 'Security session invalid. Please login again.'}), 401
    
    now_utc = get_current_time_utc()
    all_fests_data = get_all_fests_cached()
    if all_fests_data is None: all_fests_data = []
    
    current_event_id = session.get('security_event_id')
    event_info = next((f for f in all_fests_data if str(f.get('FestID','')) == str(current_event_id)), None)
    
    if event_info:
        event_name_msg = event_info.get('FestName', 'The event')
        end_time_utc = parse_datetime_as_utc(event_info.get('EndTime'), is_from_sheet=True)
        if end_time_utc and now_utc >= (end_time_utc + timedelta(hours=1)): 
             print(f"VerifyQR REJECTED: Scan attempt well after event end for '{event_name_msg}'")
             return jsonify({'status':'error', 'message':f"'{event_name_msg}' has ended. Check-ins are closed."}), 403
        elif end_time_utc is None:
             print(f"VerifyQR WARN: Event '{event_name_msg}' has no end time defined. Allowing check-in.")
    else:
        print(f"VerifyQR WARN: Event info not found for ID {current_event_id} for time check. Proceeding.");

    data = request.get_json(); qr_content = data.get('qr_data') if data else None
    if not qr_content: return jsonify({'status': 'error', 'message': 'No QR data received.'}), 400
    
    try:
        parsed_data={};
        for item in qr_content.split(','): 
            if ':' in item: key, value = item.split(':', 1); parsed_data[key.strip()] = value.strip()
        scanned_unique_id = parsed_data.get('UniqueID'); scanned_fest_id = parsed_data.get('FestID');
        
        if not scanned_unique_id or not scanned_fest_id:
            return jsonify({'status':'error', 'message':'QR code is missing required data.'}), 400
        if str(scanned_fest_id) != str(session.get('security_event_id')):
            return jsonify({'status':'error', 'message':'This QR code is for a different event.'}), 400
    except Exception as e:
        print(f"ERROR parsing QR content '{qr_content}': {e}");
        return jsonify({'status':'error', 'message':'Invalid QR code format.'}), 400
    
    try:
        g_client, master_ss_obj, _, _ = get_sheet_objects_cached()
        sheet_title = session['security_event_sheet_title'];
        headers_qr = ['UniqueID','Name','Email','Mobile','College','Present','Timestamp'] 

        if not master_ss_obj:
            print(f"CRITICAL: master_spreadsheet_obj_global is None during verify_qr.")
            return jsonify({'status':'error', 'message':'Server configuration error accessing event data.'}), 500

        reg_sheet = get_or_create_worksheet(master_ss_obj, master_ss_obj.id, sheet_title, headers_qr);
        
        cell = reg_sheet.find(scanned_unique_id, in_column=1) 
        if not cell: return jsonify({'status':'error', 'message':'Participant not found in registration list.'}), 404
        
        row_data=reg_sheet.row_values(cell.row); sheet_headers = reg_sheet.row_values(1)
        
        try:
            p_idx = sheet_headers.index('Present')  
            n_idx = sheet_headers.index('Name')
            e_idx = sheet_headers.index('Email')
            m_idx = sheet_headers.index('Mobile')
            ts_idx = sheet_headers.index('Timestamp')
        except ValueError as ve:
            print(f"ERROR: Header missing in sheet '{sheet_title}'. Expected: {headers_qr}, Found: {sheet_headers}. Error: {ve}")
            return jsonify({'status':'error', 'message':'Registration sheet configuration error (missing columns).'}), 500
        
        def get_val(idx, default='N/A'): return row_data[idx].strip() if len(row_data)>idx and row_data[idx] else default

        current_presence_status = get_val(p_idx).lower()
        participant_name = get_val(n_idx)
        participant_email = get_val(e_idx)
        participant_mobile = get_val(m_idx)
        
        current_scan_timestamp_local = datetime.now(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER)
        
        if current_presence_status == 'yes':
            last_scan_time = get_val(ts_idx, "previously");
            return jsonify({'status':'warning',
                            'message':'ALREADY SCANNED!',
                            'name':participant_name,
                            'details':f"Email: {participant_email}, Mobile: {participant_mobile}. Last Scanned: {last_scan_time}"})
        
        updates_to_perform = [
            {'range': gspread.utils.rowcol_to_a1(cell.row, p_idx + 1), 'values': [['yes']]},
            {'range': gspread.utils.rowcol_to_a1(cell.row, ts_idx + 1), 'values': [[current_scan_timestamp_local]]}
        ]
        reg_sheet.batch_update(updates_to_perform)
        
        return jsonify({'status':'success',
                        'message':'Access Granted!',
                        'name':participant_name,
                        'details':f"Email: {participant_email}, Mobile: {participant_mobile}. Checked-in: {current_scan_timestamp_local}"});
    except gspread.exceptions.WorksheetNotFound:
        return jsonify({'status':'error', 'message':f"Event registration data ('{session.get('security_event_sheet_title', 'sheet')}') not found."}), 404
    except Exception as e:
        print(f"ERROR: Verify QR operation: {e}"); traceback.print_exc();
        return jsonify({'status':'error', 'message':'Verification server error.'}), 500


# --- Initialization Function ---
def initialize_application_on_startup():
    print("\n----- Initializing Application on Startup -----")
    try: get_sheet_objects_cached(); print("Initial check/load of Google services complete.")
    except ValueError as ve: print(f"🔴🔴🔴 FATAL STARTUP ERROR (Credentials): {ve}"); exit(1)
    except Exception as e: print(f"CRITICAL INIT ERROR: {e}"); traceback.print_exc(); exit(1)
    print(f"INFO: Input timezone for event times assumed to be: {YOUR_LOCAL_TIMEZONE_STR} (UTC{YOUR_LOCAL_TIMEZONE.utcoffset(datetime.now())})")
    print(f"INFO: Times will be stored in sheets in UTC ({DATETIME_STORAGE_FORMAT}).")
    print(f"INFO: Times will be displayed to users in {YOUR_LOCAL_TIMEZONE_STR} ({DATETIME_DISPLAY_FORMAT_USER}).")
    print("----- Application Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    if not FEST_IMAGES_DRIVE_FOLDER_ID: print("\n🔴 WARNING: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID not set. Image uploads for fests will fail.\n")
    if not MASTER_SHEET_ID: print("\n🔴 WARNING: MASTER_SHEET_ID not set. Opening master sheet relies on name search, which is less robust.\n")
    
    is_flask_reloader_process = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    
    if not is_flask_reloader_process: 
        if not os.environ.get('FLASK_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY') == "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV":
            print("\n🔴 WARNING: FLASK_SECRET_KEY not securely set for the main process. This is insecure for production.\n")
        print("Flask starting up - Main process: Initializing application components...")
        initialize_application_on_startup()
        print("Flask startup - Main process: Initialization complete.")
    else: 
        print("Flask starting up - Reloader process detected. Initialization might be repeated (normal for dev).")

    port = int(os.environ.get("PORT", 10000))
    # When running with Gunicorn (as per your logs "Running 'gunicorn app:app'"), 
    # Gunicorn handles the serving, and app.run() is not typically used in production.
    # However, for local development (if you run `python app.py`), this block is still relevant.
    # Gunicorn will use the `app` object directly from `app.py`.
    
    # If Gunicorn is managing the app, this `app.run` is only for `python app.py` execution.
    # The debug and reloader settings here won't affect Gunicorn.
    # Gunicorn has its own settings for workers, debug mode (usually off in prod), and reloading.
    print(f"Flask app object created. If run directly via 'python app.py', will listen on port {port}.")
    # app.run(debug=True, host='0.0.0.0', port=port) # Comment out if Gunicorn is always used this is my main code 
