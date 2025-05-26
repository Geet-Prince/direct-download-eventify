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
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M' # Example for local time input, if used directly in sheet
DATETIME_STORAGE_FORMAT = '%Y-%m-%dT%H:%M:%SZ' # Primary format for storing UTC times in sheet
DATETIME_DISPLAY_FORMAT_USER = '%Y-%m-%d %I:%M %p' # For displaying times to user in local timezone
DATETIME_DISPLAY_SHEET_TS = '%Y-%m-%d %H:%M:%S' # Another display format, if needed
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
    if not dt_str_from_input: return None
    dt_str = str(dt_str_from_input).strip()

    # Prioritize ISO format with 'Z' for UTC
    if dt_str.endswith('Z'):
        try:
            # datetime.fromisoformat handles timezone-aware strings correctly
            dt_obj = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            return dt_obj.astimezone(pytz.utc) # Ensure it's UTC and has pytz timezone info
        except ValueError:
            # If fromisoformat fails (e.g., non-standard 'Z' usage or malformed),
            # fall through to strptime attempts.
            pass

    parsed_naive = None
    successful_fmt = None # Keep track of which format succeeded for timezone assumption
    formats_to_try = DATETIME_INPUT_FORMATS_FOR_SHEET_PARSE if is_from_sheet else DATETIME_INPUT_FORMATS_FOR_NAIVE_PARSE

    for fmt in formats_to_try:
        try:
            parsed_naive = datetime.strptime(dt_str, fmt)
            successful_fmt = fmt # Store the format that worked
            break
        except (ValueError, TypeError):
            continue

    if parsed_naive:
        # Determine the assumed timezone of the naive datetime object
        assumed_input_tz = YOUR_LOCAL_TIMEZONE # Default for form inputs or non-specific sheet formats

        if is_from_sheet and successful_fmt == DATETIME_STORAGE_FORMAT:
            # If it was parsed from sheet using DATETIME_STORAGE_FORMAT (which includes 'Z', e.g., '2023-10-27T10:00:00Z'),
            # then strptime would parse it as naive, but the 'Z' implies it was UTC.
            assumed_input_tz = pytz.utc
        
        # Now, localize the naive datetime using the determined assumed_input_tz
        try:
            # If the assumed_input_tz is already UTC and parsed_naive is naive, localize it to UTC.
            # This is for cases like DATETIME_STORAGE_FORMAT being parsed by strptime.
            if assumed_input_tz.zone == pytz.utc.zone and parsed_naive.tzinfo is None:
                return pytz.utc.localize(parsed_naive)
            
            # Otherwise, localize to the assumed_input_tz (e.g. YOUR_LOCAL_TIMEZONE) then convert to UTC.
            # is_dst=None will raise AmbiguousTimeError or NonExistentTimeError during DST transitions if applicable.
            localized_dt = assumed_input_tz.localize(parsed_naive, is_dst=None)
            return localized_dt.astimezone(pytz.utc)
        
        except (pytz.exceptions.AmbiguousTimeError, pytz.exceptions.NonExistentTimeError) as e_loc_specific:
            # Log this specific error, it's important for DST transitions.
            # Consider how to handle this robustly - e.g. ask user, try is_dst=True/False, or default.
            # Current fallback: treat as naive UTC, which might be incorrect but avoids crashing.
            print(f"Timezone localization (Ambiguous/NonExistent) error for '{dt_str}' with format '{successful_fmt}' and tz '{assumed_input_tz.zone}': {e_loc_specific}. Treating as naive UTC as fallback.")
            return pytz.utc.localize(parsed_naive) 
        except Exception as e_loc_generic:
            # Catch-all for other localization/conversion errors.
            print(f"General timezone conversion error for '{dt_str}' with format '{successful_fmt}' and tz '{assumed_input_tz.zone}': {e_loc_generic}. Treating as naive UTC as fallback.")
            return pytz.utc.localize(parsed_naive) # Fallback

    print(f"Could not parse datetime string '{dt_str}' with known formats after trying all options."); return None


def get_current_time_utc(): return datetime.now(timezone.utc) # Use timezone.utc for Python 3.9+

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
    global _cached_fests_data_all, _cache_fests_timestamp_all; now = datetime.now() # Use naive now for cache duration check
    if _cached_fests_data_all and _cache_fests_timestamp_all and (now - _cache_fests_timestamp_all < CACHE_FESTS_DURATION):
        print("Returning cached fests data."); return _cached_fests_data_all
    print("Fetching fresh fests data from sheet..."); _, _, _, fests_sheet = get_sheet_objects_cached()
    try: _cached_fests_data_all = fests_sheet.get_all_records(); _cache_fests_timestamp_all = now
    except Exception as e: print(f"ERROR fetching all fests: {e}. Returning last cache or empty."); traceback.print_exc(); return _cached_fests_data_all or []
    return _cached_fests_data_all

# --- Other Helper Functions (Assumed to be complete from your original code) ---
def generate_unique_id(): return str(uuid.uuid4().hex)[:10] # Shortened for example

def hash_password(password):
    # Replace with a strong hashing algorithm like bcrypt or Argon2 in production
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(stored_hash, provided_password):
    return stored_hash == hash_password(provided_password)

def upload_to_drive(file_stream, filename, folder_id):
    try:
        drive_service = get_drive_service_cached()
        file_metadata = {'name': filename, 'parents': [folder_id]}
        media = MediaIoBaseUpload(file_stream, mimetype='application/octet-stream', resumable=True) # Adjust mimetype if known
        file = drive_service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        print(f"Uploaded '{filename}' to Drive. File ID: {file.get('id')}, Link: {file.get('webViewLink')}")
        # Important: Ensure the file is publicly viewable or accessible as needed by your app
        # This might involve changing permissions on the file or folder.
        # For simplicity, we return webViewLink; a direct download link might be better.
        # To make it publicly viewable (anyone with the link):
        # permission = {'type': 'anyone', 'role': 'reader'}
        # drive_service.permissions().create(fileId=file.get('id'), body=permission).execute()
        # print(f"Made file '{filename}' publicly readable.")
        return file.get('webViewLink') # Or construct a direct download link if preferred
    except Exception as e:
        print(f"ERROR uploading '{filename}' to Drive: {e}"); traceback.print_exc(); return None

def share_spreadsheet_with_editor(spreadsheet_obj, email, sheet_name_for_log):
    try:
        spreadsheet_obj.share(email, perm_type='user', role='writer')
        print(f"Shared '{sheet_name_for_log}' with {email} as editor.")
    except Exception as e_share:
        print(f"WARN: Failed to share '{sheet_name_for_log}' with {email}: {e_share}")

def get_or_create_worksheet(gspread_client, spreadsheet_title_or_id, worksheet_title, headers_list):
    try:
        if isinstance(spreadsheet_title_or_id, str) and len(spreadsheet_title_or_id) > 40: # Heuristic for ID
            ss = gspread_client.open_by_key(spreadsheet_title_or_id)
        else:
            ss = gspread_client.open(spreadsheet_title_or_id) # Assumes title
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Spreadsheet '{spreadsheet_title_or_id}' not found. Cannot get/create worksheet '{worksheet_title}'.")
        raise # Or handle as per application logic, e.g., return None
    except Exception as e_open:
        print(f"Error opening spreadsheet '{spreadsheet_title_or_id}': {e_open}")
        raise

    try:
        worksheet = ss.worksheet(worksheet_title)
        print(f"Found worksheet '{worksheet_title}' in '{ss.title}'.")
        # Optionally, verify headers here if worksheet exists
        current_headers = worksheet.row_values(1) if worksheet.row_count >= 1 else []
        if not current_headers:
            print(f"Worksheet '{worksheet_title}' is empty, appending headers.")
            worksheet.append_row(headers_list)
        elif current_headers != headers_list:
            print(f"WARN: Headers mismatch in '{worksheet_title}'. Sheet: {current_headers}, Expected: {headers_list}. Consider migration or error.")
        return worksheet
    except gspread.exceptions.WorksheetNotFound:
        print(f"Worksheet '{worksheet_title}' not found in '{ss.title}'. Creating...")
        worksheet = ss.add_worksheet(title=worksheet_title, rows=1, cols=len(headers_list))
        worksheet.append_row(headers_list)
        print(f"Created worksheet '{worksheet_title}' with headers.")
        return worksheet
    except Exception as e_ws:
        print(f"Error getting/creating worksheet '{worksheet_title}': {e_ws}")
        raise

@app.context_processor
def inject_now(): return {'now': datetime.now(YOUR_LOCAL_TIMEZONE)} # Provide local 'now' to templates

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
        try: cell = clubs_sheet.find(email_form, in_column=3) # Column 3 is Email
        except gspread.exceptions.CellNotFound: print(f"DEBUG LOGIN: Email not found '{email_form}'"); flash("Invalid email or password.", "danger"); return render_template('club_login.html')
        
        if cell:
            try:
                club_data=clubs_sheet.row_values(cell.row)
                if len(club_data) < 4: flash("Login error: Incomplete club data.", "danger"); return render_template('club_login.html')
                # Assuming headers are: ClubID, ClubName, Email, PasswordHash
                stored_club_id, stored_name, stored_email, stored_hash = club_data[0].strip(), club_data[1].strip(), club_data[2].strip().lower(), club_data[3].strip()
                
                # Double check if email matches, though `find` should ensure this.
                if stored_email != email_form:
                    print(f"DEBUG LOGIN: Email mismatch. Form: '{email_form}', Sheet: '{stored_email}' at row {cell.row}. Critical data integrity issue or find logic error.")
                    flash("Internal login error. Please contact support.", "danger"); return render_template('club_login.html')

                if verify_password(stored_hash, password_form):
                    session['club_id']=stored_club_id; session['club_name']=stored_name
                    flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
                else: flash("Invalid email or password.", "danger")
            except Exception as e: print(f"ERROR LOGIN Logic: {e}"); traceback.print_exc(); flash("Login logic error.", "danger")
        else: # Should have been caught by CellNotFound, but as a fallback.
            flash("Invalid email or password.", "danger")
    return render_template('club_login.html')

@app.route('/club/logout')
def club_logout(): session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))


@app.route('/club/create_fest', methods=['GET', 'POST'])
def create_fest():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    form_data_to_pass = request.form.to_dict() if request.method == 'POST' else {}
    if request.method == 'POST':
        fest_name = request.form.get('fest_name', '').strip()
        start_time_str = request.form.get('start_time', '') # Expected in local time from datetime-local input
        end_time_str = request.form.get('end_time', '')     # Expected in local time
        registration_end_time_str = request.form.get('registration_end_time', '') # Expected in local time
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
             # Form inputs are naive local time. Parse them as such (is_from_sheet=False) to get UTC objects.
             start_dt_utc = parse_datetime_as_utc(start_time_str, is_from_sheet=False)
             end_dt_utc = parse_datetime_as_utc(end_time_str, is_from_sheet=False)
             reg_end_dt_utc = parse_datetime_as_utc(registration_end_time_str, is_from_sheet=False)

             if not all([start_dt_utc, end_dt_utc, reg_end_dt_utc]):
                 flash("Invalid date/time format. Please use YYYY-MM-DDTHH:MM.", "danger")
                 return render_template('create_fest.html', form_data=form_data_to_pass)
             
             current_time_utc = get_current_time_utc()
             if not (start_dt_utc < end_dt_utc):
                 flash("End time must be after start time.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             if not (reg_end_dt_utc <= start_dt_utc):
                 flash("Registration deadline must be on or before the start time.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             # Optionally, check if start_dt_utc is in the future
             # if start_dt_utc <= current_time_utc:
             #     flash("Fest start time must be in the future.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)

        except Exception as e_parse:
             print(f"Error parsing/validating fest times: {e_parse}"); flash(f"Invalid date/time input: {e_parse}", "danger");
             return render_template('create_fest.html', form_data=form_data_to_pass)
        
        try:
            g_client, _, _, master_fests_sheet = get_sheet_objects_cached(); fest_id=generate_unique_id();
            new_fest_row=[ fest_id, fest_name, session['club_id'], session.get('club_name','N/A'),
                           start_dt_utc.strftime(DATETIME_STORAGE_FORMAT), # Store as UTC string
                           end_dt_utc.strftime(DATETIME_STORAGE_FORMAT),   # Store as UTC string
                           reg_end_dt_utc.strftime(DATETIME_STORAGE_FORMAT),# Store as UTC string
                           fest_details, is_published,
                           fest_venue, fest_guests, fest_image_link ];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}, ImgLink:'{fest_image_link}'");
            
            # Invalidate cache
            global _cached_fests_data_all, _cache_fests_timestamp_all;
            _cached_fests_data_all = None; _cache_fests_timestamp_all = None;
            print("INFO: All fests cache invalidated after creating new fest.")

            # Create individual sheet for registrations
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; # Use master_spreadsheet_obj_global.id or title
            event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp']; # Timestamp of registration
            
            # Use the master spreadsheet object for creating new worksheets
            master_ss_obj = master_spreadsheet_obj_global # Retrieved via get_sheet_objects_cached earlier
            # Check if master_ss_obj is valid (it should be if g_client is from get_sheet_objects_cached)
            if master_ss_obj:
                 get_or_create_worksheet(g_client, master_ss_obj.id, safe_sheet_title, event_headers) # Pass ID for robustness
            else:
                # This case should ideally not be reached if initialization is correct
                print(f"CRITICAL: master_spreadsheet_obj_global is None during worksheet creation for {fest_id}. Skipping individual sheet creation.")
                flash("Fest created, but registration sheet setup failed. Contact admin.", "warning")


            flash(f"Fest '{fest_name}' created successfully!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e:
            print(f"ERROR: Create Fest (Sheet/Drive Operations): {e}"); traceback.print_exc();
            flash("Database write error or error creating event registration sheet.", "danger");
            return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})


# ***** ADDED club_dashboard ROUTE *****
@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session:
        flash("Login required to access the dashboard.", "warning")
        return redirect(url_for('club_login'))

    club_name = session.get('club_name', 'Club')
    current_club_id = session['club_id']
    
    try:
        all_fests_data = get_all_fests_cached()
        if all_fests_data is None: # Defensive check
            all_fests_data = []
    except Exception as e:
        print(f"ERROR fetching cached fests for club dashboard: {e}")
        flash("Error loading dashboard data. Please try again.", "danger")
        all_fests_data = []

    # Filter fests for the current club
    club_fests_raw = [fest for fest in all_fests_data if str(fest.get('ClubID','')) == str(current_club_id)]

    now_utc = get_current_time_utc()
    processed_club_fests = []

    for fest_data in club_fests_raw:
        fest = fest_data.copy() # Work on a copy

        # Parse sheet times (assumed UTC) into datetime objects
        start_time_utc_obj = parse_datetime_as_utc(fest.get('StartTime'), is_from_sheet=True)
        end_time_utc_obj = parse_datetime_as_utc(fest.get('EndTime'), is_from_sheet=True)
        reg_end_time_utc_obj = parse_datetime_as_utc(fest.get('RegistrationEndTime'), is_from_sheet=True)

        fest['start_time_obj_utc'] = start_time_utc_obj
        fest['end_time_obj_utc'] = end_time_utc_obj
        # fest['reg_end_time_obj_utc'] = reg_end_time_utc_obj # Store if needed for logic

        # Determine status and display class
        fest['status_class'] = 'text-muted' # default
        if start_time_utc_obj and end_time_utc_obj:
            if now_utc < start_time_utc_obj:
                fest['status_display'] = "Upcoming"
                fest['status_class'] = 'badge bg-info text-dark'
            elif now_utc <= end_time_utc_obj:
                fest['status_display'] = "Ongoing"
                fest['status_class'] = 'badge bg-success'
            else:
                fest['status_display'] = "Ended"
                fest['status_class'] = 'badge bg-danger'
        else:
            fest['status_display'] = "Unknown (Time Data Missing)"
            fest['status_class'] = 'badge bg-warning text-dark'
        
        # Format times for display in local timezone
        fest['start_time_display'] = start_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if start_time_utc_obj else "N/A"
        fest['end_time_display'] = end_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if end_time_utc_obj else "N/A"
        fest['reg_end_time_display'] = reg_end_time_utc_obj.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER) if reg_end_time_utc_obj else "N/A"
        
        fest['is_published'] = str(fest.get('Published','no')).strip().lower() == 'yes'

        processed_club_fests.append(fest)

    # Sort fests: e.g., ongoing first, then upcoming, then ended. Within each, by start time descending.
    def sort_key_dashboard(f):
        st_obj = f['start_time_obj_utc']
        # Ensure a datetime object for sorting, use min/max for N/A cases to push them down/up
        st_obj_for_sort = st_obj if st_obj else datetime.min.replace(tzinfo=pytz.utc)

        if f['status_display'] == "Ongoing":
            return (0, st_obj_for_sort) # Ongoing sorted by start time
        elif f['status_display'] == "Upcoming":
            return (1, st_obj_for_sort) # Upcoming sorted by start time
        else: # Ended or Unknown
            return (2, st_obj_for_sort) # Ended/Unknown sorted by start time (effectively reverse chronological for ended)

    processed_club_fests.sort(key=sort_key_dashboard)


    return render_template('club_dashboard.html', 
                           club_name=club_name, 
                           fests=processed_club_fests)
# ***** END club_dashboard ROUTE *****


# (Your other routes like club_history, edit_fest, end_fest, delete_fest, stats, exports would go here)
# Ensure any routes taking date/time input from forms use parse_datetime_as_utc(..., is_from_sheet=False)
# And routes reading from sheets use parse_datetime_as_utc(..., is_from_sheet=True)


# === Attendee Routes (Modified for UTC) ===
@app.route('/events')
def live_events():
    now_utc = get_current_time_utc(); available_fests=[]
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for events: {e}"); flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    
    if all_fests_data is None: all_fests_data = [] # Ensure iterable

    for fest_data in all_fests_data:
        fest = fest_data.copy() # work with a copy
        is_published=str(fest.get('Published','')).strip().lower()=='yes'
        
        reg_end_time_utc = parse_datetime_as_utc(fest.get('RegistrationEndTime',''), is_from_sheet=True)
        start_time_utc = parse_datetime_as_utc(fest.get('StartTime',''), is_from_sheet=True)
        
        if is_published and reg_end_time_utc and start_time_utc and now_utc < reg_end_time_utc and now_utc < start_time_utc :
            # For display on live_events, convert times to local
            fest['start_time_display'] = start_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER)
            fest['reg_end_time_display'] = reg_end_time_utc.astimezone(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER)
            available_fests.append(fest)
            
    available_fests.sort(key=lambda x: parse_datetime_as_utc(x.get('StartTime'), is_from_sheet=True) or datetime.max.replace(tzinfo=pytz.utc))
    return render_template('live_events.html', fests=available_fests)


@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info_dict=None; is_open_for_reg=False; now_utc = get_current_time_utc()
    try: all_fests_data = get_all_fests_cached()
    except Exception as e: print(f"ERROR getting cached fests for event_detail: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))

    if all_fests_data is None: all_fests_data = [] # Ensure iterable
    
    fest_info_dict_raw = next((f for f in all_fests_data if str(f.get('FestID',''))==str(fest_id_param)), None)
    
    if not fest_info_dict_raw: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    
    fest_info_dict = fest_info_dict_raw.copy() # Work with a copy

    is_published = str(fest_info_dict.get('Published','')).lower()=='yes'
    reg_end_time_utc = parse_datetime_as_utc(fest_info_dict.get('RegistrationEndTime', ''), is_from_sheet=True)
    start_time_utc = parse_datetime_as_utc(fest_info_dict.get('StartTime',''), is_from_sheet=True)
    end_time_utc = parse_datetime_as_utc(fest_info_dict.get('EndTime',''), is_from_sheet=True)

    if is_published and reg_end_time_utc and start_time_utc and now_utc < reg_end_time_utc and now_utc < start_time_utc :
        is_open_for_reg = True
    
    # Format times for display
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
        g_client, master_ss_obj, _, _ = get_sheet_objects_cached() # Get master_ss_obj for context
        all_fests=get_all_fests_cached()
        if all_fests is None: all_fests = []

        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==str(fest_id_param)), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        
        now_utc = get_current_time_utc()
        reg_end_time_utc = parse_datetime_as_utc(fest_info.get('RegistrationEndTime', ''), is_from_sheet=True)
        start_time_utc = parse_datetime_as_utc(fest_info.get('StartTime', ''), is_from_sheet=True)
        
        if not reg_end_time_utc or not start_time_utc or now_utc >= reg_end_time_utc or now_utc >= start_time_utc:
            flash("Registration closed or event has already started.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        
        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}";
        event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp']; # Timestamp of registration
        
        if not master_ss_obj:
            print(f"CRITICAL: master_spreadsheet_obj_global is None during join_event for fest {fest_info['FestID']}. Cannot open/create registration sheet.")
            flash("Critical server error: Cannot access event data. Please contact support.", "danger")
            return redirect(url_for('event_detail', fest_id_param=fest_id_param))

        reg_sheet = get_or_create_worksheet(g_client, master_ss_obj.id, individual_sheet_title, event_headers) # Use ID
        
        if reg_sheet.findall(email, in_column=3): # Column 3 for Email in event_headers
            flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning");
            return redirect(url_for('event_detail', fest_id_param=fest_id_param));
            
        user_id=generate_unique_id();
        # Record registration timestamp in local time for display/logging consistency if preferred for this field
        ts_registration = datetime.now(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER);
        row=[user_id, name, email, mobile, college, 'no', ts_registration]; # 'no' for Present, ts_registration for Timestamp
        reg_sheet.append_row(row); print(f"JoinEvent: Appended registration for {email} to {individual_sheet_title}")
        
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20].replace(',',';')}"; img_qr_obj=qrcode.make(qr_data);
        buf = BytesIO(); img_qr_obj.save(buf, format="PNG");
        qr_image_base64 = base64.b64encode(buf.getvalue()).decode()
        
        safe_user_name_for_file = "".join(c if c.isalnum() or c in ['_','-',' '] else "" for c in str(name)).strip().replace(' ', '_') or "user"
        safe_fest_name_for_file = "".join(c if c.isalnum() or c in ['_','-',' '] else "" for c in str(fest_info.get('FestName','Event'))).strip().replace(' ', '_') or "event"
        download_filename = f"{safe_user_name_for_file}_QR_for_{safe_fest_name_for_file}_{user_id[:4]}.png"
        
        flash(f"Successfully registered for '{fest_info.get('FestName')}'! Your QR code is below.", "success") # Simplified message slightly
        return render_template( 'join_success.html', qr_image_base64=qr_image_base64, fest_name=fest_info.get('FestName','Event'), user_name=name, download_filename=download_filename, qr_image_data_url=f"data:image/png;base64,{qr_image_base64}" )
    except gspread.exceptions.SpreadsheetNotFound: # This might be specific to master sheet or fest sheet
        print(f"ERROR JoinEvent (SpreadsheetNotFound): Fest ID {fest_id_param}. Check master sheet and event sheet creation logic.");
        flash("Registration error: Event data sheet missing or inaccessible.", "danger");
        return redirect(url_for('event_detail', fest_id_param=fest_id_param))
    except Exception as e:
        print(f"ERROR JoinEvent: {e}"); traceback.print_exc();
        flash("An unexpected registration error occurred. Please try again.", "danger");
        return redirect(url_for('event_detail', fest_id_param=fest_id_param));

# === Security Routes ===
@app.route('/security/login', methods=['GET', 'POST'])
def security_login():
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower(); event_name_password = request.form.get('password','').strip()
        if not username or not event_name_password: flash("All fields required.", "danger"); return render_template('security_login.html')
        if username == 'security': # Simple username check
            try:
                all_fests_data = get_all_fests_cached();
                if all_fests_data is None: all_fests_data = [] 
                print(f"Security Login Attempt: User='{username}', EventPass='{event_name_password}'")
                
                valid_event = None
                for fest_item in all_fests_data:
                    # Compare fest name directly for password; ensure published
                    if str(fest_item.get('FestName','')).strip() == event_name_password and \
                       str(fest_item.get('Published','')).strip().lower() == 'yes':
                        
                        # Also check if event is not long past its end time (e.g., allow access for a grace period post-event)
                        end_time_utc = parse_datetime_as_utc(fest_item.get('EndTime'), is_from_sheet=True)
                        now_utc = get_current_time_utc()
                        # Example: Allow login if event ended within last 12 hours, or is ongoing/upcoming
                        if end_time_utc and (now_utc < end_time_utc + timedelta(hours=12)):
                             valid_event = fest_item
                             break
                        elif not end_time_utc: # If no end time, consider it valid (might need stricter logic)
                            valid_event = fest_item
                            break
                
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
        # Allow check-ins up to a grace period after official end time, e.g., 1 hour
        if end_time_utc and now_utc >= (end_time_utc + timedelta(hours=1)): # Example grace period
             print(f"VerifyQR REJECTED: Scan attempt well after event end for '{event_name_msg}'")
             return jsonify({'status':'error', 'message':f"'{event_name_msg}' has ended. Check-ins are closed."}), 403
    else:
        print(f"VerifyQR WARN: Event info not found for ID {current_event_id} for time check. Proceeding with QR validation.");

    data = request.get_json(); qr_content = data.get('qr_data') if data else None
    if not qr_content: return jsonify({'status': 'error', 'message': 'No QR data received.'}), 400
    
    try:
        parsed_data={};
        for item in qr_content.split(','): # Basic CSV-like parsing
            if ':' in item: key, value = item.split(':', 1); parsed_data[key.strip()] = value.strip()
        scanned_unique_id = parsed_data.get('UniqueID'); scanned_fest_id = parsed_data.get('FestID');
        
        if not scanned_unique_id or not scanned_fest_id:
            return jsonify({'status':'error', 'message':'QR code is missing required data (UniqueID or FestID).'}), 400
        if str(scanned_fest_id) != str(session.get('security_event_id')):
            return jsonify({'status':'error', 'message':'This QR code is for a different event.'}), 400
    except Exception as e:
        print(f"ERROR parsing QR content '{qr_content}': {e}");
        return jsonify({'status':'error', 'message':'Invalid QR code format.'}), 400
    
    try:
        g_client, master_ss_obj, _, _ = get_sheet_objects_cached()
        sheet_title = session['security_event_sheet_title'];
        headers_qr = ['UniqueID','Name','Email','Mobile','College','Present','Timestamp'] # Expected headers

        if not master_ss_obj:
            print(f"CRITICAL: master_spreadsheet_obj_global is None during verify_qr. Cannot open sheet '{sheet_title}'.")
            return jsonify({'status':'error', 'message':'Server configuration error accessing event data.'}), 500

        reg_sheet = get_or_create_worksheet(g_client, master_ss_obj.id, sheet_title, headers_qr); # Use ID
        
        cell = reg_sheet.find(scanned_unique_id, in_column=1) # UniqueID is in column 1
        if not cell: return jsonify({'status':'error', 'message':'Participant not found in registration list.'}), 404
        
        row_data=reg_sheet.row_values(cell.row); sheet_headers = reg_sheet.row_values(1) # Get actual headers from sheet
        
        # Dynamically find column indices based on actual sheet headers
        try:
            p_idx = sheet_headers.index('Present')   # 0-based index
            n_idx = sheet_headers.index('Name')
            e_idx = sheet_headers.index('Email')
            m_idx = sheet_headers.index('Mobile')
            ts_idx = sheet_headers.index('Timestamp') # Timestamp of check-in
        except ValueError:
            print(f"ERROR: Header missing in sheet '{sheet_title}'. Expected: {headers_qr}, Found: {sheet_headers}")
            return jsonify({'status':'error', 'message':'Registration sheet configuration error. Key columns missing.'}), 500
        
        def get_val(idx, default='N/A'): return row_data[idx].strip() if len(row_data)>idx and row_data[idx] else default

        current_presence_status = get_val(p_idx).lower()
        participant_name = get_val(n_idx)
        participant_email = get_val(e_idx)
        participant_mobile = get_val(m_idx)
        
        # Record current scan timestamp in local time for the 'Timestamp' column update
        current_scan_timestamp_local = datetime.now(YOUR_LOCAL_TIMEZONE).strftime(DATETIME_DISPLAY_FORMAT_USER)
        
        if current_presence_status == 'yes':
            last_scan_time = get_val(ts_idx, "previously");
            return jsonify({'status':'warning',
                            'message':'ALREADY SCANNED!',
                            'name':participant_name,
                            'details':f"Email: {participant_email}, Mobile: {participant_mobile}. Last Scanned: {last_scan_time}"})
        
        # Update 'Present' to 'yes' and 'Timestamp' to current scan time
        updates_to_perform = [
            {'range': gspread.utils.rowcol_to_a1(cell.row, p_idx + 1), 'values': [['yes']]}, # +1 for 1-based A1 notation
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
        return jsonify({'status':'error', 'message':'Verification server error. Please try again.'}), 500


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
    
    # Flask's reloader runs the main module twice. `WERKZEUG_RUN_MAIN` helps run init once.
    is_flask_reloader_process = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    
    if not is_flask_reloader_process: # This is the main, first process
        if not os.environ.get('FLASK_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY') == "temp_dev_secret_key_for_flask_reloader_only_SET_IN_ENV":
            print("\n🔴 WARNING: FLASK_SECRET_KEY not securely set for the main process. This is insecure for production.\n")
        print("Flask starting up - Main process: Initializing application components...")
        initialize_application_on_startup()
        print("Flask startup - Main process: Initialization complete.")
    else: # This is the reloader's child process
        print("Flask starting up - Reloader process detected. Initialization might be repeated if state isn't shared (normal for dev).")
        # Depending on how globals are managed, some init might re-run here.
        # The get_xxx_cached functions are designed to mitigate redundant re-initialization of clients.

    port = int(os.environ.get("PORT", 10000))
    print(f"Starting Flask server (host=0.0.0.0, port={port}, debug={app.debug}, reloader={app.config.get('USE_RELOADER', True)})...")
    # For Render, debug=True is often okay as Render manages production aspects.
    # `use_reloader=True` is default with `app.run(debug=True)`.
    app.run(debug=True, host='0.0.0.0', port=port)
