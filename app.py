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

# Third-party library imports
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, flash, send_file, make_response
)
from fpdf import FPDF
import gspread
from oauth2client.service_account import ServiceAccountCredentials as GSpreadServiceAccountCredentials # Kept your original import
import pandas as pd
import qrcode
from werkzeug.utils import secure_filename # For secure filenames

# NEW: For Google Drive API
from google.oauth2.service_account import Credentials as GoogleAuthServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
if not app.secret_key:
    print("🔴 WARNING: FLASK_SECRET_KEY is not set. Using a random key for this session only. SET THIS IN YOUR ENVIRONMENT FOR PRODUCTION.")
    app.secret_key = os.urandom(24)

# --- Google Sheets Setup ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# SCOPE now includes drive for the get_gspread_client if it's shared, though drive_service uses its own scope
SCOPE_SHEETS = ['https://www.googleapis.com/auth/spreadsheets']
SCOPE_DRIVE = ['https://www.googleapis.com/auth/drive']
# If get_gspread_client ONLY needs sheets, you can use SCOPE_SHEETS there.
# For simplicity, if the same creds_dict is used, broader scope might be okay,
# but ideally, services use only the scopes they need.
# Let's assume get_gspread_client uses SCOPE_SHEETS for now.
MASTER_SHEET_NAME = 'event management'
YOUR_PERSONAL_EMAIL = os.environ.get("YOUR_PERSONAL_SHARE_EMAIL", "prince.raj.ds@gmail.com")

# NEW: Environment variable for Drive Folder ID
FEST_IMAGES_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID")

# --- Constants ---
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M'
DATETIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M'
DATETIME_INPUT_FORMATS = [
    DATETIME_SHEET_FORMAT,
    DATETIME_DISPLAY_FORMAT,
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S'
]
# NEW: Allowed image extensions
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


# --- Helper Functions ---
# NEW: Check allowed file extension
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# MODIFIED: To be a general helper
def get_google_creds_dict():
    required_env_vars = [
        "GOOGLE_TYPE", "GOOGLE_PROJECT_ID", "GOOGLE_PRIVATE_KEY_ID",
        "GOOGLE_PRIVATE_KEY", "GOOGLE_CLIENT_EMAIL", "GOOGLE_CLIENT_ID",
        "GOOGLE_AUTH_URI", "GOOGLE_TOKEN_URI", "GOOGLE_AUTH_PROVIDER_X509_CERT_URL",
        "GOOGLE_CLIENT_X509_CERT_URL"
    ]
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
        error_msg = f"Missing required Google credentials environment variables: {', '.join(missing_vars)}"
        print(f"CRITICAL ERROR: {error_msg}")
        raise ValueError(error_msg)
    return {
        "type": os.environ.get("GOOGLE_TYPE"),
        "project_id": os.environ.get("GOOGLE_PROJECT_ID"),
        "private_key_id": os.environ.get("GOOGLE_PRIVATE_KEY_ID"),
        "private_key": os.environ.get("GOOGLE_PRIVATE_KEY").replace('\\n', '\n'),
        "client_email": os.environ.get("GOOGLE_CLIENT_EMAIL"),
        "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
        "auth_uri": os.environ.get("GOOGLE_AUTH_URI"),
        "token_uri": os.environ.get("GOOGLE_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.environ.get("GOOGLE_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": os.environ.get("GOOGLE_CLIENT_X509_CERT_URL"),
        "universe_domain": os.environ.get("GOOGLE_UNIVERSE_DOMAIN", "googleapis.com")
    }

def get_gspread_client(): # Uses SCOPE_SHEETS
    print("Attempting to authorize gspread client...")
    try:
        creds_dict = get_google_creds_dict()
        creds = GSpreadServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE_SHEETS)
        client = gspread.authorize(creds)
        print("gspread client authorized successfully.")
        return client
    except Exception as e:
        print(f"CRITICAL ERROR initializing gspread client: {e}")
        traceback.print_exc()
        raise

# NEW: Google Drive Service function
def get_drive_service(): # Uses SCOPE_DRIVE
    print("Attempting to authorize Google Drive service...")
    try:
        creds_dict = get_google_creds_dict()
        creds = GoogleAuthServiceAccountCredentials.from_service_account_info(creds_dict, scopes=SCOPE_DRIVE)
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        print("Google Drive service authorized successfully.")
        return service
    except Exception as e:
        print(f"CRITICAL ERROR initializing Google Drive service: {e}")
        traceback.print_exc()
        raise

# NEW: Upload to Google Drive Function
def upload_to_drive(file_stream, filename, target_folder_id):
    if not target_folder_id:
        print("ERROR (upload_to_drive): Google Drive folder ID not provided. Cannot upload image.")
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
        return None

# MODIFIED: get_master_sheet_tabs to include FestImageLink
def get_master_sheet_tabs():
    client = get_gspread_client(); spreadsheet = None
    try: print(f"Opening master SS: '{MASTER_SHEET_NAME}'"); spreadsheet = client.open(MASTER_SHEET_NAME); print(f"Opened master SS: '{spreadsheet.title}' (ID: {spreadsheet.id})")
    except gspread.exceptions.SpreadsheetNotFound: print(f"Master SS '{MASTER_SHEET_NAME}' not found. Creating..."); spreadsheet = client.create(MASTER_SHEET_NAME); print(f"Created master SS '{MASTER_SHEET_NAME}' (ID: {spreadsheet.id})."); share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"CRITICAL ERROR opening/creating master SS: {e}"); raise
    if not spreadsheet: raise Exception("Failed master SS handle.")

    clubs_headers=['ClubID','ClubName','Email','PasswordHash']
    fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests', 'FestImageLink'] # FestImageLink ADDED

    try: clubs_sheet = spreadsheet.worksheet("Clubs"); print("Found 'Clubs' ws.")
    except gspread.exceptions.WorksheetNotFound: print("'Clubs' ws not found. Creating..."); clubs_sheet = spreadsheet.add_worksheet(title="Clubs",rows=1, cols=len(clubs_headers)); clubs_sheet.append_row(clubs_headers); clubs_sheet.resize(rows=100); print("'Clubs' ws created.")
    
    try:
        fests_sheet = spreadsheet.worksheet("Fests"); print("Found 'Fests' ws.")
        current_headers = []
        if fests_sheet.row_count >= 1:
            try: current_headers = fests_sheet.row_values(1)
            except Exception as e_get_row: print(f"Note: Could not get header row from Fests sheet: {e_get_row}")
        
        if current_headers != fests_headers:
            print(f"INFO: 'Fests' sheet headers need update/creation. Current: {current_headers}, Expected: {fests_headers}")
            if not current_headers:
                 print("Fests sheet empty/headerless, appending new expected headers...");
                 if fests_sheet.row_count > 0: fests_sheet.clear()
                 fests_sheet.append_row(fests_headers)
                 print("New headers appended to Fests sheet.")
            elif 'FestImageLink' not in current_headers and len(current_headers) == len(fests_headers) -1 : # If FestImageLink is the only missing one at the end
                print("Attempting to add 'FestImageLink' header to existing Fests sheet...")
                try:
                    fests_sheet.update_cell(1, len(fests_headers) , 'FestImageLink') # Add to the last expected column position
                    print(f"'FestImageLink' header added to column {len(fests_headers)}")
                except Exception as he: print(f"ERROR adding 'FestImageLink' header: {he}.")
            else:
                print("WARN: Fests sheet headers are significantly different or FestImageLink is not simply the last missing. Manual check advised.")
    except gspread.exceptions.WorksheetNotFound:
        print("'Fests' ws not found. Creating...");
        fests_sheet = spreadsheet.add_worksheet(title="Fests",rows=1,cols=len(fests_headers));
        fests_sheet.append_row(fests_headers);
        fests_sheet.resize(rows=100); print("'Fests' ws created.")
    except Exception as e: print(f"Error access/updating 'Fests' ws: {e}")
    return client, spreadsheet, clubs_sheet, fests_sheet

# (share_spreadsheet_with_editor and get_or_create_worksheet functions from your provided code - assumed correct)
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
    spreadsheet=None; worksheet=None; headers=headers or []; ws_created_now = False
    try: print(f"Opening/Creating individual SS: '{spreadsheet_title}'"); spreadsheet = client.open(spreadsheet_title); print(f"Opened SS: '{spreadsheet.title}'")
    except gspread.exceptions.SpreadsheetNotFound: print(f"Individual SS '{spreadsheet_title}' not found. Creating..."); spreadsheet = client.create(spreadsheet_title); print(f"Created SS '{spreadsheet.title}'."); share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, spreadsheet.title);
    except Exception as e: print(f"ERROR getting SS '{spreadsheet_title}': {e}"); raise
    if not spreadsheet: raise Exception("Failed SS handle.")
    try: worksheet = spreadsheet.worksheet(worksheet_title); print(f"Found WS '{worksheet_title}'.")
    except gspread.exceptions.WorksheetNotFound: print(f"WS '{worksheet_title}' not found. Creating..."); ws_cols=len(headers) if headers else 10; worksheet = spreadsheet.add_worksheet(title=worksheet_title,rows=1,cols=ws_cols); ws_created_now = True; print(f"WS '{worksheet_title}' created.")
    except Exception as e: print(f"ERROR getting WS '{worksheet_title}': {e}"); raise
    if not worksheet: raise Exception("Failed WS handle.")
    try:
        first_row = []; count = worksheet.row_count
        if not ws_created_now and count >= 1:
             try: first_row = worksheet.row_values(1)
             except Exception as api_e: print(f"Note: API error get row 1 for '{worksheet_title}': {api_e}")
        if headers and (ws_created_now or not first_row): print(f"Appending headers to '{worksheet_title}'..."); worksheet.append_row(headers); print("Headers appended."); worksheet.resize(rows=500);
        elif headers and first_row != headers: print(f"WARN: Headers mismatch WS '{worksheet_title}'! Sheet: {first_row}, Expected: {headers}")
        else: print(f"Headers OK/Not Needed for WS '{worksheet_title}'.")
    except Exception as hdr_e: print(f"ERROR header logic WS '{worksheet_title}': {hdr_e}")
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

# === Club Routes (club_register, club_login, club_logout are from your provided code) ===
@app.route('/club/register', methods=['GET', 'POST'])
def club_register():
    # ... (Your existing club_register code) ...
    if request.method == 'POST':
        club_name=request.form.get('club_name','').strip();email=request.form.get('email','').strip().lower();password=request.form.get('password','');confirm_password=request.form.get('confirm_password','')
        if not all([club_name,email,password,confirm_password]): flash("All fields required.", "danger"); return render_template('club_register.html')
        if password != confirm_password: flash("Passwords do not match.", "danger"); return render_template('club_register.html')
        if "@" not in email or "." not in email: flash("Invalid email.", "danger"); return render_template('club_register.html')
        try: _,_,clubs_sheet,_ = get_master_sheet_tabs();
        except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_register.html')
        try:
            if clubs_sheet.findall(email, in_column=3): flash("Email already registered.", "warning"); return redirect(url_for('club_login'))
            club_id=generate_unique_id(); hashed_pass=hash_password(password); print(f"ClubReg: Appending {club_id}")
            clubs_sheet.append_row([club_id, club_name, email, hashed_pass]); print("ClubReg: Append OK.")
            flash("Club registered!", "success"); return redirect(url_for('club_login'))
        except Exception as e: print(f"ERROR: ClubReg Op: {e}"); traceback.print_exc(); flash("Registration error.", "danger")
    return render_template('club_register.html')


@app.route('/club/login', methods=['GET', 'POST'])
def club_login():
    # ... (Your existing email-based club_login code with debugs) ...
    if request.method == 'POST':
        email_form = request.form.get('email','').strip().lower(); password_form = request.form.get('password','')
        print(f"DEBUG LOGIN: Attempt. Email: '{email_form}', Pass: '{password_form}'")
        if not email_form or not password_form: flash("Email/pass required.", "danger"); return render_template('club_login.html')
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
                if stored_email != email_form: flash("Internal login error.", "danger"); return render_template('club_login.html') # Should not happen if find worked
                if verify_password(stored_hash, password_form):
                    session['club_id']=stored_club_id; session['club_name']=name
                    flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
                else: print("DEBUG LOGIN: Password verify failed."); flash("Invalid email or password.", "danger")
            except Exception as e: print(f"ERROR LOGIN Logic: {e}"); traceback.print_exc(); flash("Login logic error.", "danger")
        else: flash("Invalid email or password.", "danger") # Should be caught by CellNotFound
    return render_template('club_login.html')


@app.route('/club/logout')
def club_logout(): session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))

# MODIFIED: create_fest for image upload
@app.route('/club/create_fest', methods=['GET', 'POST'])
def create_fest():
    if 'club_id' not in session:
        flash("Login required.", "warning"); return redirect(url_for('club_login'))
    form_data_to_pass = request.form.to_dict() if request.method == 'POST' else {}

    if request.method == 'POST':
        fest_name = request.form.get('fest_name', '').strip()
        start_time_str = request.form.get('start_time', '')
        end_time_str = request.form.get('end_time', '')
        registration_end_time_str = request.form.get('registration_end_time', '')
        fest_details = request.form.get('fest_details', '').strip()
        fest_venue = request.form.get('fest_venue', '').strip()
        fest_guests = request.form.get('fest_guests', '').strip()
        is_published = 'yes' if request.form.get('publish_fest') == 'yes' else 'no'
        fest_image_link = ""

        if 'fest_image' in request.files:
            file = request.files['fest_image']
            if file and file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                file_stream = BytesIO(); file.save(file_stream); file_stream.seek(0)
                if not FEST_IMAGES_DRIVE_FOLDER_ID:
                    flash("Image upload config error (server).", "danger"); print("ERROR: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID not set.")
                else:
                    uploaded_url = upload_to_drive(file_stream, unique_filename, FEST_IMAGES_DRIVE_FOLDER_ID)
                    if uploaded_url: fest_image_link = uploaded_url
                    else: flash("Failed to upload fest image.", "warning")
            elif file and file.filename != '' and not allowed_file(file.filename):
                flash(f"Invalid image file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}", "warning")

        required = {'Fest Name': fest_name, 'Start Time': start_time_str, 'End Time': end_time_str,
                    'Registration Deadline': registration_end_time_str, 'Details': fest_details}
        missing = [name for name, val in required.items() if not val]
        if missing: flash(f"Missing: {', '.join(missing)}", "danger"); return render_template('create_fest.html',form_data=form_data_to_pass)
        try:
             start_dt, end_dt, reg_end_dt = parse_datetime(start_time_str), parse_datetime(end_time_str), parse_datetime(registration_end_time_str)
             if not all([start_dt, end_dt, reg_end_dt]): flash("Invalid date/time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
             if not (start_dt < end_dt and reg_end_dt <= start_dt): flash("Invalid times.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        except ValueError: flash("Invalid time format.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
        try:
            client,_,_,master_fests_sheet=get_master_sheet_tabs(); fest_id=generate_unique_id();
            new_fest_row=[
                fest_id, fest_name, session['club_id'], session.get('club_name','N/A'),
                start_dt.strftime(DATETIME_SHEET_FORMAT), end_dt.strftime(DATETIME_SHEET_FORMAT),
                reg_end_dt.strftime(DATETIME_SHEET_FORMAT), fest_details, is_published,
                fest_venue, fest_guests, fest_image_link # Added image link
            ];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}, ImgLink:'{fest_image_link}'");
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip() or "fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
            get_or_create_worksheet(client, safe_sheet_title, "Registrations", event_headers);
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e: print(f"ERROR: Create Fest write: {e}"); traceback.print_exc(); flash("DB write error.", "danger"); return render_template('create_fest.html', form_data=form_data_to_pass)
    return render_template('create_fest.html', form_data={})


# --- ALL OTHER ROUTES from your last provided code (club_dashboard, club_history, edit_fest, etc.) ---
# --- Ensure they are present here. For brevity, I am only including examples of how ---
# --- data fetching would include the FestImageLink for display. ---

@app.route('/club/dashboard')
def club_dashboard(): # Assumed from your last provided code
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now=datetime.now(); upcoming,ongoing = [],[]; club_fests_all=[]
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs()
        all_fests_data=fests_sheet.get_all_records() # Will include 'FestImageLink'
    except Exception as e: print(f"ERROR Sheet Access dashboard: {e}"); flash("DB Error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name'), upcoming_fests=[], ongoing_fests=[])
    try: club_fests_all=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
    except Exception as e: print(f"ERROR filtering fests dashboard: {e}")
    for fest in club_fests_all:
        try:
            start_time, end_time = parse_datetime(fest.get('StartTime')), parse_datetime(fest.get('EndTime'))
            if not (start_time and end_time): continue
            if now < start_time: upcoming.append(fest)
            elif start_time <= now < end_time: ongoing.append(fest)
        except Exception as e: print(f"Error processing fest '{fest.get('FestName')}' times: {e}")
    upcoming.sort(key=lambda x: parse_datetime(x.get('StartTime')) or datetime.max)
    ongoing.sort(key=lambda x: parse_datetime(x.get('StartTime')) or datetime.min)
    return render_template('club_dashboard.html',club_name=session.get('club_name'), upcoming_fests=upcoming, ongoing_fests=ongoing)

@app.route('/club/history')
def club_history(): # Assumed from your last provided code
     if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
     now=datetime.now(); past_fests_all=[]
     try: _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records()
     except Exception as e: print(f"ERROR Sheet Access history: {e}"); flash("DB Error.", "danger"); return render_template('club_history.html', club_name=session.get('club_name'), past_fests=[])
     try: club_fests_for_history=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]
     except Exception as e: print(f"ERROR filtering fests history: {e}")
     for fest in club_fests_for_history:
        try:
            end_time=parse_datetime(fest.get('EndTime', ''))
            if not end_time: continue
            if now>=end_time: past_fests_all.append(fest)
        except Exception as e: print(f"Error processing fest '{fest.get('FestName')}' end time: {e}")
     past_fests_all.sort(key=lambda x: parse_datetime(x.get('EndTime')) or datetime.min, reverse=True)
     return render_template('club_history.html',club_name=session.get('club_name'), past_fests=past_fests_all)


# (edit_fest, end_fest, delete_fest, fest_stats, export_excel, export_pdf from your last provided code)
# (live_events, event_detail, join_event from your last provided code)
# (security routes from your last provided code)

# --- I will paste the live_events and event_detail to ensure they are correctly fetching data that includes FestImageLink ---
@app.route('/events')
def live_events():
    now=datetime.now(); available_fests=[]
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs()
        all_fests_data=fests_sheet.get_all_records() # FestImageLink will be in this data
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
        all_fests_data=fests_sheet.get_all_records() # FestImageLink will be in this data
    except Exception as e: print(f"ERROR Sheet Access event_detail: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))
    fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id_param), None)
    if not fest_info: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    is_published = str(fest_info.get('Published','')).lower()=='yes'
    reg_end_time = parse_datetime(fest_info.get('RegistrationEndTime', ''))
    if is_published and reg_end_time and datetime.now() < reg_end_time:
        is_open_for_reg=True
    return render_template('event_detail.html', fest=fest_info, registration_open=is_open_for_reg)

# (Ensure ALL other routes - edit_fest, end_fest, delete_fest, stats, exports, join_event, security - are copied from your last full version)

# --- Initialization Function ---
def initialize_master_sheets_and_tabs():
    print("\n----- Initializing Master Sheets & Tabs -----")
    try:
        client, spreadsheet, clubs_sheet, fests_sheet = get_master_sheet_tabs()
        print(f"Init Check PASSED: Master SS '{MASTER_SHEET_NAME}' ready.")
    except Exception as e:
        print(f"CRITICAL INIT ERROR getting sheets: {e}"); traceback.print_exc(); return
    try:
        if YOUR_PERSONAL_EMAIL:
            share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"WARN during sharing: {e}")
    print("----- Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    if not FEST_IMAGES_DRIVE_FOLDER_ID:
        print("\n🔴 WARNING: GOOGLE_DRIVE_FEST_IMAGES_FOLDER_ID environment variable is NOT SET.")
        print("🔴 Image uploads for fests will NOT function correctly. Please set this variable.\n")

    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
         print("Flask starting up - Main process: Initializing...")
         initialize_master_sheets_and_tabs()
         print("Flask startup - Main process: Initialization complete.")
    else: print("Flask starting up - Reloader process detected.")

    print("Starting Flask development server (host=0.0.0.0, port=5000)...")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=True)
