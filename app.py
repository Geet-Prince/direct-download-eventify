# -*- coding: utf-8 -*-
# Standard library imports
import base64
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO
from operator import itemgetter
import os
import traceback
import uuid
# import json # Not needed as we're not loading a JSON file for creds

# Third-party library imports
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, flash, send_file, make_response
)
from fpdf import FPDF
import gspread
# Using oauth2client for gspread as per existing structure if that's what gspread version needs
# for from_json_keyfile_dict equivalent.
from oauth2client.service_account import ServiceAccountCredentials as GSpreadServiceAccountCredentials
import pandas as pd
import qrcode
from werkzeug.utils import secure_filename

# For Google Drive API - uses google-auth's Credentials
from google.oauth2.service_account import Credentials as GoogleAuthServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# from dotenv import load_dotenv # Optional: for local .env files
# load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    print("🔴 FATAL: FLASK_SECRET_KEY is not set in the environment. Application cannot start securely.")
    if app.debug:
        print("🔴 WARNING: Using a temporary FLASK_SECRET_KEY for debug mode. SET IT IN YOUR ENVIRONMENT.")
        app.secret_key = os.urandom(24)
    else:
        raise ValueError("FLASK_SECRET_KEY is not set. This is required for production.")


# --- Google Setup ---
SCOPE_SHEETS = ['https://www.googleapis.com/auth/spreadsheets']
SCOPE_DRIVE = ['https://www.googleapis.com/auth/drive']

MASTER_SHEET_NAME = os.environ.get("MASTER_SHEET_NAME", 'event management')
YOUR_PERSONAL_EMAIL = os.environ.get("YOUR_PERSONAL_SHARE_EMAIL")
FEST_IMAGES_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID")

# --- Constants ---
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M'
DATETIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M'
DATETIME_INPUT_FORMATS = [
    DATETIME_SHEET_FORMAT, DATETIME_DISPLAY_FORMAT,
    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'
]
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_google_creds_dict_from_env():
    """
    Constructs the credentials dictionary from environment variables.
    This dictionary format is suitable for from_service_account_info (google-auth)
    and from_json_keyfile_dict (oauth2client).
    """
    # Keys expected by from_service_account_info (google-auth)
    # and also by from_json_keyfile_dict (oauth2client)
    expected_keys_map = {
        "type": "GOOGLE_TYPE",
        "project_id": "GOOGLE_PROJECT_ID",
        "private_key_id": "GOOGLE_PRIVATE_KEY_ID",
        "private_key": "GOOGLE_PRIVATE_KEY",
        "client_email": "GOOGLE_CLIENT_EMAIL",
        "client_id": "GOOGLE_CLIENT_ID",
        "auth_uri": "GOOGLE_AUTH_URI",
        "token_uri": "GOOGLE_TOKEN_URI",
        "auth_provider_x509_cert_url": "GOOGLE_AUTH_PROVIDER_X509_CERT_URL",
        "client_x509_cert_url": "GOOGLE_CLIENT_X509_CERT_URL"
        # "universe_domain" is optional for google-auth, not typically used by oauth2client directly this way
    }
    creds_dict = {}
    missing_vars = []
    for key, env_var_name in expected_keys_map.items():
        value = os.environ.get(env_var_name)
        if not value:
            missing_vars.append(env_var_name)
        else:
            creds_dict[key] = value
    
    if missing_vars:
        error_msg = f"Missing Google credentials environment variables: {', '.join(missing_vars)}"
        print(f"CRITICAL ERROR: {error_msg}")
        raise ValueError(error_msg)

    creds_dict['private_key'] = creds_dict['private_key'].replace('\\n', '\n')
    # Add universe_domain if present in env, otherwise google-auth will use default
    creds_dict['universe_domain'] = os.environ.get("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com")
    return creds_dict

def get_gspread_client():
    print("Attempting to authorize gspread client from environment variables...")
    try:
        creds_dict = get_google_creds_dict_from_env()
        creds = GSpreadServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE_SHEETS)
        client = gspread.authorize(creds)
        print("gspread client authorized successfully.")
        return client
    except Exception as e:
        print(f"CRITICAL ERROR initializing gspread client: {e}")
        traceback.print_exc()
        raise

def get_drive_service():
    print("Attempting to authorize Google Drive service from environment variables...")
    try:
        creds_dict = get_google_creds_dict_from_env()
        # from_service_account_info takes the dictionary directly
        creds = GoogleAuthServiceAccountCredentials.from_service_account_info(creds_dict, scopes=SCOPE_DRIVE)
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        print("Google Drive service authorized successfully.")
        return service
    except Exception as e:
        print(f"CRITICAL ERROR initializing Google Drive service: {e}")
        traceback.print_exc()
        raise

def upload_to_drive(file_stream, filename, target_folder_id):
    if not target_folder_id:
        print("ERROR (upload_to_drive): Google Drive folder ID not configured. Cannot upload image.")
        flash("Image upload configuration error on server. Please contact support.", "error")
        return None
    try:
        drive_service = get_drive_service()
        file_metadata = {'name': filename, 'parents': [target_folder_id]}
        media = MediaIoBaseUpload(file_stream, mimetype='application/octet-stream', resumable=True)
        created_file = drive_service.files().create(
            body=file_metadata, media_body=media, fields='id, webViewLink'
        ).execute()
        file_id = created_file.get('id')
        print(f"Drive Upload: File ID: {file_id}, Name: {filename}")
        permission_request_body = {'type': 'anyone', 'role': 'reader'}
        drive_service.permissions().create(fileId=file_id, body=permission_request_body).execute()
        print(f"Drive Upload: Set public read permission for {filename}")
        image_url = f"https://drive.google.com/uc?export=view&id={file_id}"
        return image_url
    except Exception as e:
        print(f"ERROR uploading '{filename}' to Drive folder '{target_folder_id}': {e}")
        traceback.print_exc()
        flash(f"Could not upload image to Google Drive: {e}", "error")
        return None

def get_master_sheet_tabs():
    client = get_gspread_client(); spreadsheet = None
    try:
        print(f"Opening master SS: '{MASTER_SHEET_NAME}'")
        spreadsheet = client.open(MASTER_SHEET_NAME)
        print(f"Opened master SS: '{spreadsheet.title}' (ID: {spreadsheet.id})")
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Master SS '{MASTER_SHEET_NAME}' not found. Creating...")
        spreadsheet = client.create(MASTER_SHEET_NAME)
        print(f"Created master SS '{MASTER_SHEET_NAME}' (ID: {spreadsheet.id}).")
        if YOUR_PERSONAL_EMAIL: share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"CRITICAL ERROR opening/creating master SS: {e}"); traceback.print_exc(); raise
    if not spreadsheet: raise Exception("Failed master SS handle.")

    clubs_headers=['ClubID','ClubName','Email','PasswordHash']
    fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests', 'FestImageLink']

    try: clubs_sheet = spreadsheet.worksheet("Clubs"); print("Found 'Clubs' ws.")
    except gspread.exceptions.WorksheetNotFound: print("'Clubs' ws not found. Creating..."); clubs_sheet = spreadsheet.add_worksheet(title="Clubs",rows=1, cols=len(clubs_headers)); clubs_sheet.append_row(clubs_headers); clubs_sheet.resize(rows=100); print("'Clubs' ws created.")
    
    try:
        fests_sheet = spreadsheet.worksheet("Fests"); print("Found 'Fests' ws.")
        current_headers = []
        if fests_sheet.row_count >= 1:
            try: current_headers = fests_sheet.row_values(1)
            except Exception as e_get_row: print(f"Note: Could not get header row from Fests sheet: {e_get_row}")
        if not current_headers:
            print("Fests sheet empty/headerless, appending expected headers...");
            if fests_sheet.row_count > 0: fests_sheet.clear()
            fests_sheet.append_row(fests_headers)
        elif current_headers != fests_headers:
            print(f"INFO: 'Fests' sheet headers differ. Current: {current_headers}, Expected: {fests_headers}")
            if 'FestImageLink' not in current_headers and len(current_headers) == len(fests_headers) -1 :
                try:
                    fests_sheet.update_cell(1, len(fests_headers) , 'FestImageLink')
                    print(f"'FestImageLink' header added to column {len(fests_headers)}")
                except Exception as he: print(f"ERROR adding 'FestImageLink' header: {he}.")
            else: print("WARN: Fests sheet headers require manual review or migration.")
    except gspread.exceptions.WorksheetNotFound:
        print("'Fests' ws not found. Creating...");
        fests_sheet = spreadsheet.add_worksheet(title="Fests",rows=1,cols=len(fests_headers));
        fests_sheet.append_row(fests_headers);
        fests_sheet.resize(rows=100); print("'Fests' ws created.")
    except Exception as e: print(f"Error access/updating 'Fests' ws: {e}")
    return client, spreadsheet, clubs_sheet, fests_sheet

# (share_spreadsheet_with_editor and get_or_create_worksheet - assumed correct from previous versions)
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

def get_or_create_worksheet(client, spreadsheet_title, worksheet_title, headers=None):
    spreadsheet = None
    worksheet = None
    headers = headers or []
    ws_created_now = False
    try:
        print(f"Opening/Creating individual SS: '{spreadsheet_title}'")
        spreadsheet = client.open(spreadsheet_title)
        print(f"Opened SS: '{spreadsheet.title}'")
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Individual SS '{spreadsheet_title}' not found. Creating...")
        spreadsheet = client.create(spreadsheet_title)
        print(f"Created SS '{spreadsheet.title}'.")
        # Share it if an email is provided
        if YOUR_PERSONAL_EMAIL: # This 'if' needs to be on its own line or part of a properly structured block
            share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, spreadsheet.title)
    except Exception as e:
        print(f"ERROR getting SS '{spreadsheet_title}': {e}")
        raise
    
    if not spreadsheet:
        raise Exception(f"Failed to get or create spreadsheet handle for '{spreadsheet_title}'.") # More informative
    
    try:
        worksheet = spreadsheet.worksheet(worksheet_title)
        print(f"Found WS '{worksheet_title}'.")
    except gspread.exceptions.WorksheetNotFound:
        print(f"WS '{worksheet_title}' not found. Creating...")
        ws_cols = len(headers) if headers else 10
        worksheet = spreadsheet.add_worksheet(title=worksheet_title, rows=1, cols=ws_cols)
        ws_created_now = True
        print(f"WS '{worksheet_title}' created.")
    except Exception as e:
        print(f"ERROR getting WS '{worksheet_title}': {e}")
        raise
        
    if not worksheet:
        raise Exception(f"Failed to get or create worksheet handle for '{worksheet_title}'.") # More informative

    try:
        first_row = []
        # Only try to get row_values if the worksheet has rows and wasn't just created
        if not ws_created_now and worksheet.row_count >= 1:
             try:
                 first_row = worksheet.row_values(1)
             except Exception as api_e: # Catch potential API errors during row_values
                 print(f"Note: API error getting row 1 for '{worksheet_title}': {api_e}")
        
        if headers and (ws_created_now or not first_row):
            print(f"Appending headers to '{worksheet_title}'...")
            worksheet.append_row(headers)
            print("Headers appended.")
            worksheet.resize(rows=500) # Default resize
        elif headers and first_row != headers:
            print(f"WARN: Headers mismatch in WS '{worksheet_title}'!")
            print(f"Sheet Headers: {first_row}")
            print(f"Expected Headers: {headers}")
        else:
            print(f"Headers OK or Not Needed for WS '{worksheet_title}'.")
    except Exception as hdr_e:
        print(f"ERROR during header logic for WS '{worksheet_title}': {hdr_e}")
        # Decide if this should raise or just warn
    return worksheet


def generate_unique_id(): return str(uuid.uuid4().hex)[:10]
def hash_password(password): print(f"DEBUG HASH: Placeholder Hash for '{password}'"); return password
def verify_password(hashed_password, provided_password): print(f"DEBUG VERIFY: Stored:'{hashed_password}', Provided:'{provided_password}', Match:{hashed_password == provided_password}"); return hashed_password == provided_password
def parse_datetime(dt_str):
    if not dt_str: return None
    for fmt in DATETIME_INPUT_FORMATS:
        try: return datetime.strptime(str(dt_str).strip(), fmt)
        except (ValueError, TypeError): continue
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
        try: _,_,clubs_sheet,_ = get_master_sheet_tabs();
        except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_register.html')
        try:
            if clubs_sheet.findall(email, in_column=3): flash("Email already registered.", "warning"); return redirect(url_for('club_login'))
            club_id=generate_unique_id(); hashed_pass=hash_password(password); print(f"ClubReg: Appending {club_id}")
            clubs_sheet.append_row([club_id, club_name, email, hashed_pass]); print("ClubReg: Append OK.")
            flash("Club registered successfully! Please login.", "success"); return redirect(url_for('club_login'))
        except Exception as e: print(f"ERROR: ClubReg Op: {e}"); traceback.print_exc(); flash("Registration error.", "danger")
    return render_template('club_register.html')

@app.route('/club/login', methods=['GET', 'POST'])
def club_login():
    if request.method == 'POST':
        email_form = request.form.get('email','').strip().lower(); password_form = request.form.get('password','')
        print(f"DEBUG LOGIN: Attempt. Email: '{email_form}', Pass: '{password_form}'")
        if not email_form or not password_form: flash("Email/pass required.", "danger"); return render_template('club_login.html')
        if "@" not in email_form or "." not in email_form.split('@')[-1]: flash("Invalid email address format.", "danger"); return render_template('club_login.html')
        try: _,_,clubs_sheet,_ = get_master_sheet_tabs()
        except Exception as e: print(f"ERROR LOGIN Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_login.html')
        try: cell = clubs_sheet.find(email_form, in_column=3)
        except gspread.exceptions.CellNotFound: print(f"DEBUG LOGIN: Email not found '{email_form}'"); flash("Invalid email or password.", "danger"); return render_template('club_login.html')
        if cell:
            try:
                club_data=clubs_sheet.row_values(cell.row)
                if len(club_data) < 4: flash("Login error: Incomplete data.", "danger"); return render_template('club_login.html')
                stored_club_id, name, stored_email, stored_hash = club_data[0].strip(), club_data[1].strip(), club_data[2].strip().lower(), club_data[3].strip()
                print(f"DEBUG LOGIN: Sheet Data - ID:'{stored_club_id}', Name:'{name}', Email:'{stored_email}', Hash:'{stored_hash}'")
                if stored_email != email_form: flash("Internal login error.", "danger"); return render_template('club_login.html')
                if verify_password(stored_hash, password_form):
                    session['club_id']=stored_club_id; session['club_name']=name
                    flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
                else: print("DEBUG LOGIN: Password verify failed."); flash("Invalid email or password.", "danger")
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
        start_time_str, end_time_str, reg_end_time_str = request.form.get('start_time', ''), request.form.get('end_time', ''), request.form.get('registration_end_time', '')
        fest_details, fest_venue, fest_guests = request.form.get('fest_details', '').strip(), request.form.get('fest_venue', '').strip(), request.form.get('fest_guests', '').strip()
        is_published = 'yes' if request.form.get('publish_fest') == 'yes' else 'no'
        fest_image_link = ""

        if 'fest_image' in request.files:
            file = request.files['fest_image']
            if file and file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename); unique_filename = f"{uuid.uuid4().hex}_{filename}"
                file_stream = BytesIO(); file.save(file_stream); file_stream.seek(0)
                if not FEST_IMAGES_DRIVE_FOLDER_ID: flash("Image upload server config error: Drive Folder ID not set.", "danger"); print("ERROR: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID not set.")
                else:
                    uploaded_url = upload_to_drive(file_stream, unique_filename, FEST_IMAGES_DRIVE_FOLDER_ID)
                    if uploaded_url: fest_image_link = uploaded_url
                    else: flash("Failed to upload fest image. Please ensure Drive API is enabled and folder is shared.", "warning")
            elif file and file.filename != '' and not allowed_file(file.filename): flash(f"Invalid image type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}", "warning")

        required = {'Fest Name': fest_name, 'Start Time': start_time_str, 'End Time': end_time_str, 'Registration Deadline': reg_end_time_str, 'Details': fest_details}
        missing = [name for name, val in required.items() if not val]
        if missing: flash(f"Missing: {', '.join(missing)}", "danger"); return render_template('create_fest.html',form_data=form_data_to_pass)
        try:
             start_dt, end_dt, reg_end_dt = parse_datetime(start_time_str), parse_datetime(end_time_str), parse_datetime(registration_end_time_str)
             if not all([start_dt, end_dt, reg_end_dt]): flash("Invalid date/time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             if not (start_dt < end_dt and reg_end_dt <= start_dt): flash("Invalid times.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        except ValueError: flash("Invalid time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        try:
            client,_,_,master_fests_sheet=get_master_sheet_tabs(); fest_id=generate_unique_id();
            new_fest_row=[ fest_id, fest_name, session['club_id'], session.get('club_name','N/A'), start_dt.strftime(DATETIME_SHEET_FORMAT), end_dt.strftime(DATETIME_SHEET_FORMAT), reg_end_dt.strftime(DATETIME_SHEET_FORMAT), fest_details, is_published, fest_venue, fest_guests, fest_image_link ];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}, ImgLink:'{fest_image_link}'");
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
            get_or_create_worksheet(client, safe_sheet_title, "Registrations", event_headers);
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e: print(f"ERROR: Create Fest write: {e}"); traceback.print_exc(); flash("DB write error.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})


# --- ALL OTHER ROUTES (dashboard, history, edit_fest, end_fest, delete_fest, fest_stats, exports, attendee routes, security routes) ---
@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now=datetime.now(); upcoming,ongoing = [],[]
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs()
        all_fests_data=fests_sheet.get_all_records() # FestImageLink will be here
    except Exception as e: print(f"ERROR Sheet Access dashboard: {e}"); flash("DB Error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name'), upcoming_fests=[], ongoing_fests=[])
    
    club_fests_all=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
    for fest in club_fests_all:
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
     try: _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records()
     except Exception as e: print(f"ERROR Sheet Access history: {e}"); flash("DB Error.", "danger"); return render_template('club_history.html', club_name=session.get('club_name'), past_fests=[])
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
    # (This route is fine as is, it doesn't directly deal with image display/upload)
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records();
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None);
        if not fest_info: flash("Fest not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Permission denied.", "danger"); return redirect(url_for('club_dashboard'))
        # If you add an "edit image" feature, you'd handle it in a POST to this or a new route
        return render_template('edit_options.html', fest=fest_info)
    except Exception as e: print(f"ERROR getting edit options FestID {fest_id}: {e}"); traceback.print_exc(); flash("Error getting event options.", "danger"); return redirect(url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/end', methods=['POST'])
def end_fest(fest_id):
    # (This route is fine as is)
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    try:
        _, _, _, fests_sheet = get_master_sheet_tabs()
        all_fests_data = fests_sheet.get_all_records()
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
        flash(f"Fest '{fest_info.get('FestName', fest_id)}' ended & unpublished.", "success")
    except Exception as e: print(f"ERROR ending fest {fest_id}: {e}"); traceback.print_exc(); flash("Error ending event.", "danger")
    return redirect(url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/delete', methods=['POST'])
def delete_fest(fest_id):
    # (This route is fine as is regarding image link. If you want to delete from Drive, add logic here)
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    redirect_url = request.referrer or url_for('club_dashboard')
    try:
        client, _, _, fests_sheet = get_master_sheet_tabs() # client is needed if deleting Drive file
        all_fests_data = fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id), None)
        if not fest_info: flash("Fest to delete not found.", "danger"); return redirect(redirect_url)
        if str(fest_info.get('ClubID',''))!=session['club_id']: flash("Permission denied.", "danger"); return redirect(redirect_url)
        
        fest_name_to_delete = fest_info.get('FestName', f"Fest (ID: {fest_id})")
        image_link_to_delete = fest_info.get('FestImageLink')

        fest_cell = fests_sheet.find(fest_id, in_column=1)
        if not fest_cell: flash("Fest to delete not found in sheet (cell).", "danger"); return redirect(redirect_url)
        fests_sheet.delete_rows(fest_cell.row)
        print(f"Fest row for '{fest_name_to_delete}' deleted from sheet.")

        # Optional: Delete image from Google Drive
        if image_link_to_delete and 'drive.google.com' in image_link_to_delete and 'id=' in image_link_to_delete:
            try:
                drive_file_id = image_link_to_delete.split('id=')[-1].split('&')[0]
                if drive_file_id:
                    drive_service = get_drive_service()
                    drive_service.files().delete(fileId=drive_file_id).execute()
                    print(f"Fest image '{drive_file_id}' deleted from Google Drive.")
            except Exception as drive_del_e:
                print(f"WARN: Could not delete fest image from Drive (ID: {drive_file_id if 'drive_file_id' in locals() else 'unknown'}): {drive_del_e}")
        
        flash(f"Fest '{fest_name_to_delete}' deleted.", "success")
    except Exception as e: print(f"ERROR deleting fest {fest_id}: {e}"); traceback.print_exc(); flash("Error deleting event.", "danger")
    return redirect(redirect_url)

@app.route('/club/fest/<fest_id>/stats')
def fest_stats(fest_id):
    # (This route is fine as is)
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    try:
        client, _, _, master_fests_sheet = get_master_sheet_tabs(); all_fests_data = master_fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Permission denied for stats.", "danger"); return redirect(url_for('club_dashboard'))
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        sheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        stats = {'total_registered': 0, 'total_present': 0, 'total_absent': 0, 'attendees_present': [], 'attendees_absent': [], 'college_stats': defaultdict(int), 'hourly_distribution': defaultdict(lambda: 0), 'checkin_times': [], 'attendance_rate': 0}
        try:
            spreadsheet = client.open(sheet_title); registrations_sheet = spreadsheet.worksheet("Registrations"); registrations_data = registrations_sheet.get_all_records()
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
    # (This route is fine as is)
    if 'club_id' not in session: flash("Login required for export.", "warning"); return jsonify({"error": "Unauthorized"}), 401
    try:
        client, _, _, master_fests_sheet = get_master_sheet_tabs(); all_fests_data = master_fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Unauthorized export.", "danger"); return redirect(url_for('club_dashboard'))
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        spreadsheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        try:
            spreadsheet = client.open(spreadsheet_title); registrations_sheet = spreadsheet.worksheet("Registrations"); registrations_data = registrations_sheet.get_all_records()
        except gspread.exceptions.SpreadsheetNotFound: flash(f"Reg sheet for '{fest_info.get('FestName')}' not found.", "warning"); return redirect(url_for('fest_stats', fest_id=fest_id))
        except Exception as e_sheet: print(f"Sheet access error for Excel: {e_sheet}"); flash("Error accessing data.", "danger"); return redirect(url_for('fest_stats', fest_id=fest_id))
        df = pd.DataFrame(registrations_data); output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='Registrations')
        output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"{safe_name}_registrations_{datetime.now().strftime('%Y%m%d')}.xlsx", mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e: print(f"Excel Export Error: {e}"); traceback.print_exc(); flash(f"Excel export error: {e}", "danger"); return redirect(request.referrer or url_for('club_dashboard'))

@app.route('/club/fest/<fest_id>/export/pdf')
def export_pdf(fest_id):
    # (This route is fine as is)
    if 'club_id' not in session: flash("Login required for PDF export.", "warning"); return redirect(url_for('club_login'))
    try:
        client, _, _, master_fests_sheet = get_master_sheet_tabs(); all_fests_data = master_fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)
        if not fest_info: flash("Event not found for PDF.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Unauthorized PDF export.", "danger"); return redirect(url_for('club_dashboard'))
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        spreadsheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        try:
            spreadsheet = client.open(spreadsheet_title); registrations_sheet = spreadsheet.worksheet("Registrations"); registrations_data = registrations_sheet.get_all_records()
        except gspread.exceptions.SpreadsheetNotFound: flash(f"Reg sheet for '{fest_info.get('FestName')}' not found for PDF.", "warning"); return redirect(url_for('fest_stats', fest_id=fest_id))
        except Exception as e_sheet: print(f"Sheet access error for PDF: {e_sheet}"); flash("Error accessing PDF data.", "danger"); return redirect(url_for('fest_stats', fest_id=fest_id))
        if not registrations_data: flash(f"No data for '{fest_info.get('FestName')}' to PDF.", "info"); return redirect(url_for('fest_stats', fest_id=fest_id))

        pdf = FPDF(orientation='L', unit='mm', format='A4'); pdf.add_page(); pdf.set_font("Arial", 'B', size=16)
        pdf.cell(0, 10, txt=f"Event Report: {fest_info.get('FestName','')}", ln=1, align='C'); pdf.set_font("Arial", size=10)
        pdf.cell(0, 7, txt=f"Date: {datetime.now().strftime(DATETIME_DISPLAY_FORMAT)}", ln=1, align='C'); pdf.ln(5)
        pdf.set_font("Arial", 'B', size=9)
        col_widths = {'ID': 25, 'Name': 50, 'Email': 65, 'Mobile': 30, 'College': 50, 'Status': 25, 'Timestamp': 30}
        headers_pdf = ['UniqueID', 'Name', 'Email', 'College', 'Present', 'Timestamp']
        display_headers_pdf = {'UniqueID': 'Unique ID', 'Name': 'Name', 'Email': 'Email', 'College': 'College', 'Present': 'Status', 'Timestamp': 'Timestamp'}

        for header_key in headers_pdf: pdf.cell(col_widths.get(header_key, 30), 7, display_headers_pdf.get(header_key, header_key), border=1, align='C')
        pdf.ln(); pdf.set_font("Arial", size=8)

        for row in registrations_data:
            for header_key in headers_pdf:
                val = str(row.get(header_key, 'N/A'))
                if header_key == 'Present': val = "Present" if val.lower() == 'yes' else "Absent"
                elif header_key == 'Timestamp':
                    parsed_ts = parse_datetime(val); val = parsed_ts.strftime(DATETIME_DISPLAY_FORMAT) if parsed_ts else val
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
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs()
        all_fests_data=fests_sheet.get_all_records() # FestImageLink will be here
    except Exception as e: print(f"ERROR Sheet Access events: {e}"); flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    for fest in all_fests_data:
        is_published=str(fest.get('Published','')).strip().lower()=='yes'
        reg_end_time = parse_datetime(fest.get('RegistrationEndTime',''))
        if is_published and reg_end_time and now < reg_end_time:
            available_fests.append(fest)
    available_fests.sort(key=lambda x: parse_datetime(x.get('StartTime')) or datetime.max)
    return render_template('live_events.html', fests=available_fests)

@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info=None; is_open_for_reg=False
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs()
        all_fests_data=fests_sheet.get_all_records() # FestImageLink will be here
    except Exception as e: print(f"ERROR Sheet Access event_detail: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))
    fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id_param), None)
    if not fest_info: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    is_published = str(fest_info.get('Published','')).lower()=='yes'
    reg_end_time = parse_datetime(fest_info.get('RegistrationEndTime', ''))
    if is_published and reg_end_time and datetime.now() < reg_end_time:
        is_open_for_reg=True
    return render_template('event_detail.html', fest=fest_info, registration_open=is_open_for_reg)

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param):
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); mobile=request.form.get('mobile','').strip(); college=request.form.get('college','').strip();
    if not all([name,email,mobile,college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    if "@" not in email or "." not in email.split('@')[-1]: flash("Invalid email.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    try:
        client,_,_,master_fests_sheet = get_master_sheet_tabs(); all_fests=master_fests_sheet.get_all_records();
        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==fest_id_param), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        reg_end_time = parse_datetime(fest_info.get('RegistrationEndTime', ''))
        if not reg_end_time or datetime.now() >= reg_end_time: flash("Registration closed.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));

        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
        reg_sheet=get_or_create_worksheet(client,individual_sheet_title,"Registrations",event_headers);
        if reg_sheet.findall(email, in_column=3): flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        user_id=generate_unique_id(); ts=datetime.now().strftime(DATETIME_DISPLAY_FORMAT); row=[user_id, name, email, mobile, college, 'no', ts];
        reg_sheet.append_row(row);
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20].replace(',',';')}"; img=qrcode.make(qr_data); buf=BytesIO(); img.save(buf,format="PNG"); img_str=base64.b64encode(buf.getvalue()).decode();
        flash(f"Joined '{fest_info.get('FestName')}'!", "success"); return render_template('join_success.html', qr_image=img_str, fest_name=fest_info.get('FestName','Event'), user_name=name);
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
                _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records();
                now = datetime.now(); valid_event = None
                for f in all_fests_data:
                    if str(f.get('FestName',''))==event_name_password and str(f.get('Published','')).strip().lower()=='yes':
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
        client = get_gspread_client(); sheet_title = session['security_event_sheet_title']; headers_qr = ['UniqueID','Name','Email','Mobile','College','Present','Timestamp']
        reg_sheet = get_or_create_worksheet(client, sheet_title, "Registrations", headers_qr);
        cell = reg_sheet.find(scanned_unique_id, in_column=1)
        if not cell: return jsonify({'status':'error', 'message':'Participant not found.'}), 404
        row_data=reg_sheet.row_values(cell.row);
        p_idx, n_idx, e_idx = headers_qr.index('Present'), headers_qr.index('Name'), headers_qr.index('Email')
        def get_val(idx, default=''): return row_data[idx] if len(row_data)>idx else default
        if get_val(p_idx).strip().lower() == 'yes': return jsonify({'status':'warning','message':'ALREADY SCANNED!', 'name':get_val(n_idx),'details':f"Email: {get_val(e_idx)}"})
        reg_sheet.update_cell(cell.row, p_idx+1, 'yes');
        return jsonify({'status':'success','message':'Access Granted!','name':get_val(n_idx),'details':f"Email: {get_val(e_idx)}"});
    except gspread.exceptions.CellNotFound: return jsonify({'status':'error', 'message':'Participant not found (QR UID not in sheet).'}), 404
    except Exception as e: print(f"ERROR: Verify QR sheet op: {e}"); traceback.print_exc(); return jsonify({'status':'error', 'message':'Verification server error.'}), 500


# --- Initialization Function ---
def initialize_master_sheets_and_tabs():
    print("\n----- Initializing Master Sheets & Tabs -----")
    try:
        # This will also check/add FestImageLink header if missing
        client, spreadsheet, clubs_sheet, fests_sheet = get_master_sheet_tabs()
        print(f"Init Check PASSED: Master SS '{MASTER_SHEET_NAME}' ready.")
    except FileNotFoundError: # Specific catch for missing creds file
        print("🔴🔴🔴 CRITICAL ERROR: Credentials file for Google Sheets (e.g., 'google_creds.json') NOT FOUND.")
        print("🔴🔴🔴 Ensure credentials are set up correctly (via file or environment variables as configured).")
        print("🔴🔴🔴 Application cannot connect to Google Sheets/Drive without proper authentication.")
        # Optionally, re-raise or exit if this is a fatal startup condition
        # raise SystemExit("Missing credentials, application cannot start.")
        return # Stop further initialization
    except Exception as e:
        print(f"CRITICAL INIT ERROR getting sheets: {e}"); traceback.print_exc(); return
    try:
        if YOUR_PERSONAL_EMAIL and spreadsheet: # Check if spreadsheet object exists
            share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"WARN during sharing: {e}")
    print("----- Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    # Check if critical environment variables are set
    if not FEST_IMAGES_DRIVE_FOLDER_ID:
        print("\n🔴 WARNING: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID environment variable is NOT SET.")
        print("🔴 Image uploads for fests will NOT function correctly. Please set this variable.\n")
    
    # Check if FLASK_SECRET_KEY is insecure only once, not on reloads
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        if not os.environ.get('FLASK_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY') == "temp_dev_secret_key_replace_me":
            print("\n🔴 WARNING: FLASK_SECRET_KEY is not securely set via environment variable.")
            print("🔴 Using a temporary/default key. SET a strong FLASK_SECRET_KEY in your environment for production.\n")
        
        print("Flask starting up - Main process: Initializing...")
        try:
            initialize_master_sheets_and_tabs()
        except (FileNotFoundError, ValueError) as cred_error: # Catch errors from cred loading
            print(f"🔴🔴🔴 FATAL STARTUP ERROR: {cred_error}")
            print("🔴🔴🔴 Application will not start properly. Please check your Google credentials setup.")
            exit(1) # Exit if credentials are fundamentally missing/misconfigured for Sheets/Drive
        except Exception as init_e:
            print(f"🔴🔴🔴 FATAL STARTUP ERROR during initialization: {init_e}")
            traceback.print_exc()
            exit(1)
        print("Flask startup - Main process: Initialization complete.")
    else:
        print("Flask starting up - Reloader process detected.")

    print("Starting Flask development server (host=0.0.0.0, port=5000)...")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=True)
