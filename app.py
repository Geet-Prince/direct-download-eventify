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
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import qrcode

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
DATETIME_SHEET_FORMAT = '%Y-%m-%dT%H:%M' 

# --- Core Google Sheets Functions (Assume Correct from Previous Version) ---
def get_gspread_client():
    print("Attempting to authorize gspread client...")
    try:
        creds_dict = {
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
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
        client = gspread.authorize(creds)
        print("gspread client authorized successfully.")
        return client
    except Exception as e: 
        print(f"CRITICAL ERROR initializing gspread client: {e}")
        raise

def share_spreadsheet_with_editor(spreadsheet, email_address, sheet_title):
    if not email_address or "@" not in email_address: print(f"Skipping sharing '{sheet_title}': Invalid email."); return False
    if not hasattr(spreadsheet, 'list_permissions') or not hasattr(spreadsheet, 'share'): print(f"WARNING: Invalid SS object for sharing '{sheet_title}'."); return False
    try:
        print(f"Sharing SS '{sheet_title}' with {email_address}..."); perms = spreadsheet.list_permissions(); shared = False
        for p in perms:
            if p.get('type')=='user' and p.get('emailAddress')==email_address:
                if p.get('role') in ['owner', 'writer']: shared = True; print(f"'{sheet_title}' already shared correctly."); break
                else: print(f"Updating role for {email_address} on '{sheet_title}' to 'writer'."); spreadsheet.share(email_address, perm_type='user', role='writer', notify=False); shared = True; break
        if not shared: print(f"Sharing '{sheet_title}' new permission for {email_address}..."); spreadsheet.share(email_address, perm_type='user', role='writer', notify=False)
        print(f"Sharing ensured for '{sheet_title}'."); return True
    except Exception as share_e: print(f"\nWARN: Share error for '{sheet_title}': {share_e}\n"); return False

def get_master_sheet_tabs():
    client = get_gspread_client(); spreadsheet = None
    try: print(f"Opening master SS: '{MASTER_SHEET_NAME}'"); spreadsheet = client.open(MASTER_SHEET_NAME); print(f"Opened master SS: '{spreadsheet.title}' (ID: {spreadsheet.id})")
    except gspread.exceptions.SpreadsheetNotFound: print(f"Master SS '{MASTER_SHEET_NAME}' not found. Creating..."); spreadsheet = client.create(MASTER_SHEET_NAME); print(f"Created master SS '{MASTER_SHEET_NAME}' (ID: {spreadsheet.id})."); share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"CRITICAL ERROR opening/creating master SS: {e}"); raise
    if not spreadsheet: raise Exception("Failed master SS handle.")
    clubs_headers=['ClubID','ClubName','Email','PasswordHash']; fests_headers=['FestID','FestName','ClubID','ClubName','StartTime','EndTime','RegistrationEndTime','Details','Published','Venue','Guests']
    try: clubs_sheet = spreadsheet.worksheet("Clubs"); print("Found 'Clubs' ws.")
    except gspread.exceptions.WorksheetNotFound: print("'Clubs' ws not found. Creating..."); clubs_sheet = spreadsheet.add_worksheet(title="Clubs",rows=1, cols=len(clubs_headers)); clubs_sheet.append_row(clubs_headers); clubs_sheet.resize(rows=100); print("'Clubs' ws created.")
    try: fests_sheet = spreadsheet.worksheet("Fests"); print("Found 'Fests' ws."); current_headers=fests_sheet.row_values(1) if fests_sheet.row_count>=1 else [];
    except gspread.exceptions.WorksheetNotFound: print("'Fests' ws not found. Creating..."); fests_sheet = spreadsheet.add_worksheet(title="Fests",rows=1,cols=len(fests_headers)); fests_sheet.append_row(fests_headers); fests_sheet.resize(rows=100); print("'Fests' ws created.")
    except Exception as e: print(f"Error access 'Fests' ws: {e}") 
    return client, spreadsheet, clubs_sheet, fests_sheet

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
             except Exception as api_e: print(f"Note: API error get row 1 {api_e}") 
        if headers and (ws_created_now or not first_row): print(f"Appending headers to '{worksheet_title}'..."); worksheet.append_row(headers); print("Headers appended."); worksheet.resize(rows=500);
        elif headers and first_row != headers: print(f"WARN: Headers mismatch WS '{worksheet_title}'!")
        else: print(f"Headers OK/Not Needed for WS '{worksheet_title}'.")
    except Exception as hdr_e: print(f"ERROR header logic WS '{worksheet_title}': {hdr_e}")
    return worksheet

# --- Helper Functions ---
def generate_unique_id(): return str(uuid.uuid4().hex)[:10]
def hash_password(password): print("WARN: Placeholder Hash"); return password 
def verify_password(hashed_password, provided_password): return hashed_password == provided_password

# === Context Processor ===
@app.context_processor
def inject_now(): return {'now': datetime.now()} # Provides 'now' to all templates

# --- Routes ---
@app.route('/')
def index(): return render_template('index.html')

# === Club Routes === (Condensed - using correct logic from previous steps)
@app.route('/club/register', methods=['GET', 'POST'])
def club_register():
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
    if request.method == 'POST':
        email=request.form.get('email','').strip().lower(); password=request.form.get('password','')
        if not email or not password: flash("Email/pass required.", "danger"); return render_template('club_login.html')
        try: _,_,clubs_sheet,_ = get_master_sheet_tabs()
        except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_login.html')
        try: cell = clubs_sheet.find(email, in_column=3)
        except gspread.exceptions.CellNotFound: flash("Invalid email or password.", "danger"); return render_template('club_login.html')
        try:
            if cell: club_data=clubs_sheet.row_values(cell.row);
            else: flash("Invalid email or password.", "danger"); return render_template('club_login.html')
            if verify_password(club_data[3], password): session['club_id']=club_data[0]; session['club_name']=club_data[1]; flash(f"Welcome, {session['club_name']}!", "success"); return redirect(url_for('club_dashboard'))
            flash("Invalid email or password.", "danger")
        except Exception as e: print(f"ERROR: Club login logic: {e}"); traceback.print_exc(); flash("Login logic error.", "danger")
    return render_template('club_login.html')

@app.route('/club/logout')
def club_logout(): session.clear(); flash("Logged out.", "info"); return redirect(url_for('index'))

@app.route('/club/dashboard')
def club_dashboard():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    now=datetime.now(); upcoming,ongoing = [],[]; club_fests=[]
    try: _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records()
    except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_dashboard.html', club_name=session.get('club_name'), upcoming_fests=[], ongoing_fests=[])
    try: club_fests=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]; print(f"Dashboard: Club {session['club_id']} has {len(club_fests)} fests.")
    except Exception as e: print(f"ERROR filtering fests: {e}") 
    for fest in club_fests:
        try:
            start_str,end_str=fest.get('StartTime',''), fest.get('EndTime','')
            if start_str and end_str: start_time=datetime.strptime(start_str,DATETIME_SHEET_FORMAT); end_time=datetime.strptime(end_str,DATETIME_SHEET_FORMAT)
            else: print(f" skipping {fest.get('FestName')} - missing times"); continue
            if now<start_time: upcoming.append(fest); print(f" - Upcoming: {fest.get('FestName')}")
            elif start_time <= now < end_time: ongoing.append(fest); print(f" - Ongoing: {fest.get('FestName')}")
        except Exception as e: print(f" skipping {fest.get('FestName')} - bad time format {e}")
    upcoming.sort(key=lambda x: datetime.strptime(x.get('StartTime','2100-01-01T00:00'), DATETIME_SHEET_FORMAT))
    ongoing.sort(key=lambda x: datetime.strptime(x.get('StartTime','1900-01-01T00:00'), DATETIME_SHEET_FORMAT))
    return render_template('club_dashboard.html',club_name=session.get('club_name'), upcoming_fests=upcoming, ongoing_fests=ongoing)

@app.route('/club/history')
def club_history():
     if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
     now=datetime.now(); past_fests=[]
     try: _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records()
     except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('club_history.html', club_name=session.get('club_name'), past_fests=[])
     try: club_fests=[f for f in all_fests_data if str(f.get('ClubID','')) == session['club_id']]; print(f"History: Checking {len(club_fests)} fests.")
     except Exception as e: print(f"ERROR filtering fests: {e}") 
     for fest in club_fests:
        try: end_str = fest.get('EndTime', '');
        except Exception as e: print(f" skipping {fest.get('FestName')} - error accessing time {e}"); continue
        if end_str:
             try: end_time=datetime.strptime(end_str,DATETIME_SHEET_FORMAT)
             except Exception as e: print(f" skipping {fest.get('FestName')} - bad time format {e}"); continue
             if now>=end_time: past_fests.append(fest); print(f" - Past: {fest.get('FestName')}")
        else: print(f" skipping {fest.get('FestName')} - no end time")
     past_fests.sort(key=lambda x: datetime.strptime(x.get('EndTime','1900-01-01T00:00'),DATETIME_SHEET_FORMAT), reverse=True)
     return render_template('club_history.html',club_name=session.get('club_name'), past_fests=past_fests)

@app.route('/club/create_fest', methods=['GET', 'POST'])
def create_fest():
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    if request.method == 'POST':
        fest_name=request.form.get('fest_name','').strip(); start_time_str=request.form.get('start_time',''); end_time_str=request.form.get('end_time',''); registration_end_time_str=request.form.get('registration_end_time',''); fest_details=request.form.get('fest_details','').strip(); fest_venue=request.form.get('fest_venue', '').strip(); fest_guests=request.form.get('fest_guests','').strip(); is_published='yes' if request.form.get('publish_fest')=='yes' else 'no'
        required={'Fest Name':fest_name,'Start Time':start_time_str,'End Time':end_time_str,'Registration Deadline':registration_end_time_str,'Details':fest_details}
        missing=[name for name, val in required.items() if not val];
        if missing: flash(f"Missing: {', '.join(missing)}", "danger"); return render_template('create_fest.html',form_data=request.form)
        try: 
             start_dt=datetime.strptime(start_time_str, DATETIME_SHEET_FORMAT); end_dt=datetime.strptime(end_time_str, DATETIME_SHEET_FORMAT); reg_end_dt=datetime.strptime(registration_end_time_str, DATETIME_SHEET_FORMAT);
             if not (start_dt < end_dt and reg_end_dt <= start_dt): flash("Invalid times.", "danger"); return render_template('create_fest.html', form_data=request.form)
        except ValueError: flash("Invalid time format.", "danger"); return render_template('create_fest.html', form_data=request.form)
        try:
            client,_,_,master_fests_sheet=get_master_sheet_tabs(); fest_id=generate_unique_id();
            new_fest_row=[fest_id, fest_name, session['club_id'], session.get('club_name','N/A'), start_time_str, end_time_str, registration_end_time_str, fest_details, is_published, fest_venue, fest_guests];
            master_fests_sheet.append_row(new_fest_row); print(f"CreateFest: Appended ID:{fest_id}");
            safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_name)).strip();
            if not safe_base: safe_base="fest_event";
            safe_sheet_title=f"{safe_base[:80]}_{fest_id}"; event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
            get_or_create_worksheet(client, safe_sheet_title, "Registrations", event_headers);
            flash(f"Fest '{fest_name}' created!", "success"); return redirect(url_for('club_dashboard'));
        except Exception as e: print(f"ERROR: Create Fest write: {e}"); traceback.print_exc(); flash("DB write error.", "danger"); return render_template('create_fest.html', form_data=request.form)
    return render_template('create_fest.html') 

# Route to SHOW options page
@app.route('/club/fest/<fest_id>/edit', methods=['GET']) 
def edit_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    print(f"EditFest GET: Request options for FestID: {fest_id}")
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records();
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None);
        if not fest_info: flash("Fest not found.", "danger"); return redirect(url_for('club_dashboard')) 
        if str(fest_info.get('ClubID','')) != session['club_id']: flash("Permission denied.", "danger"); return redirect(url_for('club_dashboard'))
        return render_template('edit_options.html', fest=fest_info) 
    except Exception as e: print(f"ERROR getting edit options FestID {fest_id}: {e}"); traceback.print_exc(); flash("Error getting event options.", "danger"); return redirect(url_for('club_dashboard'))

# Route to PERFORM end action
@app.route('/club/fest/<fest_id>/end', methods=['POST'])
def end_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    print(f"EndFest POST: FestID: {fest_id}")
    try:
        _,_,_,fests_sheet = get_master_sheet_tabs()
        try: fest_cell = fests_sheet.find(fest_id, in_column=1)
        except gspread.exceptions.CellNotFound: flash("Fest to end not found.", "danger"); return redirect(url_for('club_dashboard'))
        if not fest_cell: flash("Fest to end not found.", "danger"); return redirect(url_for('club_dashboard'))
        fest_row_index = fest_cell.row; all_fests_data = fests_sheet.get_all_records(); fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id), None);
        if not fest_info: flash("Fest data mismatch.", "danger"); return redirect(url_for('club_dashboard'))
        if str(fest_info.get('ClubID',''))!=session['club_id']: flash("Permission denied.", "danger"); return redirect(url_for('club_dashboard'))
        try: header_row = fests_sheet.row_values(1); end_time_col_index = header_row.index('EndTime') + 1;
        except Exception as header_e: print(f"ERROR finding EndTime header: {header_e}"); flash("Sheet config error.", "danger"); return redirect(url_for('club_dashboard'));
        now_str = datetime.now().strftime(DATETIME_SHEET_FORMAT); print(f"EndFest: Updating Row {fest_row_index} EndTime to {now_str}")
        fests_sheet.update_cell(fest_row_index, end_time_col_index, now_str); flash(f"Fest '{fest_info.get('FestName', fest_id)}' marked ended.", "success")
    except Exception as e: print(f"ERROR ending fest {fest_id}: {e}"); traceback.print_exc(); flash("Error ending event.", "danger")
    return redirect(url_for('club_dashboard')) 

# Route to PERFORM delete action
@app.route('/club/fest/<fest_id>/delete', methods=['POST'])
def delete_fest(fest_id):
    if 'club_id' not in session: flash("Login required.", "warning"); return redirect(url_for('club_login'))
    print(f"DeleteFest POST: FestID: {fest_id}"); fest_name_to_delete = f"Fest (ID: {fest_id})"; redirect_url = request.referrer or url_for('club_dashboard');
    try:
        client, _, _, fests_sheet = get_master_sheet_tabs()
        try: fest_cell = fests_sheet.find(fest_id, in_column=1)
        except gspread.exceptions.CellNotFound: flash("Fest to delete not found.", "danger"); return redirect(redirect_url) 
        if not fest_cell: flash("Fest to delete not found.", "danger"); return redirect(redirect_url)
        fest_row_index = fest_cell.row; all_fests_data = fests_sheet.get_all_records(); fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id), None);
        if not fest_info: flash("Fest data mismatch.", "danger"); return redirect(redirect_url)
        if str(fest_info.get('ClubID',''))!=session['club_id']: flash("Permission denied.", "danger"); return redirect(redirect_url)
        fest_name_to_delete = fest_info.get('FestName', fest_name_to_delete); print(f"DeleteFest: Deleting row {fest_row_index} ('{fest_name_to_delete}')")
        fests_sheet.delete_rows(fest_row_index); print("DeleteFest: Row deleted.");
        flash(f"Fest '{fest_name_to_delete}' deleted.", "success")
    except Exception as e: print(f"ERROR deleting fest {fest_id}: {e}"); traceback.print_exc(); flash("Error deleting event.", "danger")
    return redirect(redirect_url)

# Fully Implemented Stats Route
@app.route('/club/fest/<fest_id>/stats')

def fest_stats(fest_id):
    if 'club_id' not in session:
        flash("Please login to view event statistics.", "warning")
        return redirect(url_for('club_login'))

    try:
        client, _, _, master_fests_sheet = get_master_sheet_tabs()
        all_fests_data = master_fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)

        if not fest_info:
            flash("Event not found.", "danger")
            return redirect(url_for('club_dashboard'))

        if str(fest_info.get('ClubID','')) != session['club_id']:
            flash("You don't have permission to view these statistics.", "danger")
            return redirect(url_for('club_dashboard'))

        # Create sheet name
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        sheet_title = f"{safe_name[:80]}_{fest_info.get('FestID','')}"
        
        stats = {
            'total_registered': 0,
            'total_present': 0,
            'total_absent': 0,
            'attendees_present': [],
            'attendees_absent': [],
            'college_stats': defaultdict(int),
            'hourly_distribution': defaultdict(int),
            'checkin_times': [],
            'attendance_rate': 0
        }

        try:
            spreadsheet = client.open(sheet_title)
            registrations_sheet = spreadsheet.worksheet("Registrations")
            registrations_data = registrations_sheet.get_all_records()
            
            stats['total_registered'] = len(registrations_data)
            
            for record in registrations_data:
                present_val = str(record.get('Present', 'no')).strip().lower()
                is_present = present_val == 'yes'
                college = record.get('College', 'Unknown')
                
                attendee = {
                    'UniqueID': record.get('UniqueID', ''),
                    'Name': record.get('Name', ''),
                    'Email': record.get('Email', ''),
                    'Mobile': record.get('Mobile', ''),
                    'College': college,
                    'Timestamp': record.get('Timestamp', '')
                }

                if is_present:
                    stats['total_present'] += 1
                    stats['attendees_present'].append(attendee)
                    stats['college_stats'][college] += 1
                    
                    # Process check-in time
                    ts = record.get('Timestamp')
                    if ts:
                        try:
                            dt = parse_datetime(ts)
                            stats['checkin_times'].append(dt)
                            hour = dt.hour
                            stats['hourly_distribution'][f"{hour:02d}:00-{hour+1:02d}:00"] += 1
                        except:
                            pass
                else:
                    stats['total_absent'] += 1
                    stats['attendees_absent'].append(attendee)

            # Calculate attendance rate
            if stats['total_registered'] > 0:
                stats['attendance_rate'] = (stats['total_present'] / stats['total_registered']) * 100

            # Sort colleges by attendance
            stats['college_stats'] = dict(sorted(
                stats['college_stats'].items(),
                key=itemgetter(1),
                reverse=True
            ))

            # Sort hourly distribution
            stats['hourly_distribution'] = dict(sorted(
                stats['hourly_distribution'].items()
            ))

            # Prepare data for charts
            stats['colleges_chart_data'] = {
                'labels': list(stats['college_stats'].keys())[:10],  # Top 10 colleges
                'data': list(stats['college_stats'].values())[:10]
            }

            stats['attendance_chart_data'] = {
                'labels': ['Present', 'Absent'],
                'data': [stats['total_present'], stats['total_absent']]
            }

            stats['hourly_chart_data'] = {
                'labels': list(stats['hourly_distribution'].keys()),
                'data': list(stats['hourly_distribution'].values())
            }

        except Exception as e:
            print(f"Error accessing registration data: {e}")
            # Don't flash error - might be new event with no registrations

        return render_template('fest_stats.html', fest=fest_info, stats=stats)

    except Exception as e:
        print(f"Error in fest_stats: {e}")
        traceback.print_exc()
        flash("Error loading statistics", "danger")
        return redirect(url_for('club_dashboard'))
@app.route('/club/fest/<fest_id>/export/excel')
def export_excel(fest_id):
    try:
        # 1. Verify authentication
        if 'club_id' not in session:
            return "Unauthorized", 401

        # 2. Get fest info
        client, _, _, master_fests_sheet = get_master_sheet_tabs()
        all_fests_data = master_fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)

        if not fest_info:
            return "Event not found", 404

        if str(fest_info.get('ClubID','')) != session['club_id']:
            return "Forbidden", 403

        # 3. Get data
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" 
                          for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        spreadsheet = client.open(f"{safe_name[:80]}_{fest_info.get('FestID','')}")
        registrations_sheet = spreadsheet.worksheet("Registrations")
        registrations_data = registrations_sheet.get_all_records()

        # 4. Create Excel file
        df = pd.DataFrame(registrations_data)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        output.seek(0)

        # 5. Send file
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{safe_name}_registrations.xlsx",
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        print(f"Excel Export Error: {str(e)}")
        return f"Error generating Excel: {str(e)}", 500
@app.route('/club/fest/<fest_id>/export/pdf')
def export_pdf(fest_id):
    if 'club_id' not in session:
        flash("Please login to export data.", "warning")
        return redirect(url_for('club_login'))

    try:
        # Get fest info (similar to your excel export)
        client, _, _, master_fests_sheet = get_master_sheet_tabs()
        all_fests_data = master_fests_sheet.get_all_records()
        fest_info = next((f for f in all_fests_data if str(f.get('FestID','')) == fest_id), None)

        if not fest_info:
            flash("Event not found.", "danger")
            return redirect(url_for('club_dashboard'))

        if str(fest_info.get('ClubID','')) != session['club_id']:
            flash("Unauthorized access.", "danger")
            return redirect(url_for('club_dashboard'))

        # Get data
        safe_name = "".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip() or "fest_event"
        spreadsheet = client.open(f"{safe_name[:80]}_{fest_info.get('FestID','')}")
        registrations_sheet = spreadsheet.worksheet("Registrations")
        registrations_data = registrations_sheet.get_all_records()

        # Create PDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        
        # Add title
        pdf.cell(200, 10, txt=f"Event Report: {fest_info.get('FestName','')}", ln=1, align='C')
        pdf.ln(10)
        
        # Add table headers
        pdf.cell(40, 10, "Name", border=1)
        pdf.cell(60, 10, "Email", border=1)
        pdf.cell(30, 10, "College", border=1)
        pdf.cell(30, 10, "Status", border=1)
        pdf.ln()
        
        # Add data rows
        for row in registrations_data:
            pdf.cell(40, 10, row.get('Name',''), border=1)
            pdf.cell(60, 10, row.get('Email',''), border=1)
            pdf.cell(30, 10, row.get('College',''), border=1)
            pdf.cell(30, 10, "Present" if str(row.get('Present','')).lower() == 'yes' else "Absent", border=1)
            pdf.ln()

        # Generate response
        pdf_output_bytes = pdf.output(dest='S')
        response = make_response(pdf_output_bytes)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename={safe_name}_report.pdf'
        return response

    except Exception as e:
        print(f"Error exporting PDF: {e}")
        flash("Failed to export PDF report.", "danger")
        return redirect(url_for('club_dashboard'))

# === Attendee Routes === (Assuming Correct from previous versions)
@app.route('/events')
def live_events():
    now=datetime.now(); available_fests=[]
    try: _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records()
    except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return render_template('live_events.html', fests=[])
    for fest in all_fests_data:
        is_published=str(fest.get('Published','')).strip().lower()=='yes'; reg_end_str=fest.get('RegistrationEndTime','')
        if is_published and reg_end_str:
             try:
                  if now<datetime.strptime(reg_end_str,DATETIME_SHEET_FORMAT): available_fests.append(fest)
             except Exception as e: print(f"WARN: Bad reg end time {e} for {fest.get('FestName')}")
    print(f"LiveEvents: {len(available_fests)} available.")
    available_fests.sort(key=lambda x: datetime.strptime(x.get('StartTime','2100-01-01T00:00'), DATETIME_SHEET_FORMAT)) # Sort by start time
    return render_template('live_events.html', fests=available_fests)

@app.route('/event/<fest_id_param>')
def event_detail(fest_id_param):
    fest_info=None; is_open=False
    try: _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records()
    except Exception as e: print(f"ERROR Sheet Access: {e}"); flash("DB Error.", "danger"); return redirect(url_for('live_events'))
    try: fest_info = next((f for f in all_fests_data if str(f.get('FestID',''))==fest_id_param), None);
    except Exception as e: print(f"ERROR finding fest: {e}");
    if not fest_info: flash("Event not found.", "warning"); return redirect(url_for('live_events'));
    try: reg_end_str = fest_info.get('RegistrationEndTime', ''); pub = str(fest_info.get('Published','')).lower()=='yes';
    except Exception as e: print(f"Error reading fest details: {e}"); pub = False; reg_end_str = None;
    if pub and reg_end_str:
        try: is_open=datetime.now()<datetime.strptime(reg_end_str,DATETIME_SHEET_FORMAT)
        except Exception as e: print(f"WARN: Bad reg end time check {e}"); is_open=False
    return render_template('event_detail.html', fest=fest_info, registration_open=is_open)

@app.route('/event/<fest_id_param>/join', methods=['POST'])
def join_event(fest_id_param):
     # (Code from previous versions - Assume Correct)
    name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); mobile=request.form.get('mobile','').strip(); college=request.form.get('college','').strip();
    print(f"JoinEvent POST: Fest={fest_id_param}, Email='{email}'");
    if not all([name,email,mobile,college]): flash("All fields required.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    if "@" not in email or "." not in email: flash("Invalid email.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
    individual_sheet_title="N/A"; fest_info = None
    try:
        client,_,_,master_fests_sheet = get_master_sheet_tabs(); all_fests=master_fests_sheet.get_all_records();
        fest_info=next((f for f in all_fests if str(f.get('FestID',''))==fest_id_param), None);
        if not fest_info: flash("Event not found.", "danger"); return redirect(url_for('live_events'));
        if str(fest_info.get('Published','')).lower()!='yes': flash("Event not published.", "warning"); return redirect(url_for('event_detail',fest_id_param=fest_id_param));
        reg_end_str = fest_info.get('RegistrationEndTime', '');
        try:
            if not reg_end_str or datetime.now() >= datetime.strptime(reg_end_str, DATETIME_SHEET_FORMAT): flash("Registration closed.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        except Exception as e: print(f"ERROR checking reg end time {e}"); flash("Event time config error.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));

        safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(fest_info.get('FestName','Event'))).strip();
        if not safe_base: safe_base="fest_event";
        individual_sheet_title=f"{safe_base[:80]}_{fest_info['FestID']}";
        print(f"JoinEvent: Accessing SS '{individual_sheet_title}'."); event_headers=['UniqueID','Name','Email','Mobile','College','Present','Timestamp'];
        reg_sheet=get_or_create_worksheet(client,individual_sheet_title,"Registrations",event_headers);
        if not isinstance(reg_sheet, gspread.worksheet.Worksheet): raise Exception("Reg sheet unavailable.");
        
        print(f"JoinEvent: Checking exist: '{email}'...");
        if reg_sheet.findall(email, in_column=3): flash(f"Already registered for '{fest_info.get('FestName')}' with this email.", "warning"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));
        
        user_id=generate_unique_id(); ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"); row=[user_id, name, email, mobile, college, 'no', ts];
        print(f"JoinEvent: Appending: {row}"); reg_sheet.append_row(row); print("JoinEvent: Append successful.");
        
        qr_data=f"UniqueID:{user_id},FestID:{fest_info['FestID']},Name:{name[:20]}"; img=qrcode.make(qr_data); buf=BytesIO(); img.save(buf,format="PNG"); img_str=base64.b64encode(buf.getvalue()).decode();
        flash(f"Joined '{fest_info.get('FestName')}'!", "success"); return render_template('join_success.html', qr_image=img_str, fest_name=fest_info.get('FestName','Event'), user_name=name);
    except Exception as e: print(f"ERROR JoinEvent: {e}"); traceback.print_exc(); flash("Registration error.", "danger"); return redirect(url_for('event_detail', fest_id_param=fest_id_param));

# === Security Routes === (Assuming Correct from previous versions)
@app.route('/security/login', methods=['GET', 'POST'])
def security_login():
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower(); event_name_password = request.form.get('password','').strip()
        if not username or not event_name_password: flash("All fields required.", "danger"); return render_template('security_login.html')
        if username == 'security':
            try:
                _,_,_,fests_sheet = get_master_sheet_tabs(); all_fests_data=fests_sheet.get_all_records();
                valid_event = next((f for f in all_fests_data if str(f.get('FestName',''))==event_name_password and str(f.get('Published','')).strip().lower()=='yes'), None);
                if valid_event:
                    session['security_event_name'] = valid_event.get('FestName','N/A'); session['security_event_id'] = valid_event.get('FestID','N/A');
                    safe_base="".join(c if c.isalnum() or c in [' ','_','-'] else "" for c in str(valid_event.get('FestName','Event'))).strip();
                    if not safe_base: safe_base="fest_event";
                    session['security_event_sheet_title']=f"{safe_base[:80]}_{valid_event.get('FestID','')}";
                    flash(f"Security access for: {session['security_event_name']}", "success"); return redirect(url_for('security_scanner'));
                else: flash("Invalid event password or event inactive.", "danger")
            except Exception as e: print(f"ERROR: Security login failed: {e}"); traceback.print_exc(); flash("Security login error.", "danger")
        else: flash("Invalid security username.", "danger")
    return render_template('security_login.html')

@app.route('/security/logout')
def security_logout(): session.clear(); flash("Security session ended.", "info"); return redirect(url_for('security_login'))

@app.route('/security/scanner')
def security_scanner():
    if 'security_event_sheet_title' not in session: flash("Please login as security.", "warning"); return redirect(url_for('security_login'))
    return render_template('security_scanner.html', event_name=session.get('security_event_name',"Event"))

@app.route('/security/verify_qr', methods=['POST'])
def verify_qr():
    if 'security_event_sheet_title' not in session or 'security_event_id' not in session: return jsonify({'status': 'error', 'message': 'Security session invalid.'}), 401
    data = request.get_json();
    if not data or 'qr_data' not in data: return jsonify({'status': 'error', 'message': 'No QR data.'}), 400
    qr_content = data.get('qr_data'); print(f"VerifyQR POST: QR={qr_content}")
    try: # Parse QR
        parsed_data={}; scanned_unique_id=None; scanned_fest_id=None
        for item in qr_content.split(','):
            if ':' in item: key, value = item.split(':', 1); parsed_data[key.strip()] = value.strip()
        scanned_unique_id = parsed_data.get('UniqueID'); scanned_fest_id = parsed_data.get('FestID');
        if not scanned_unique_id or not scanned_fest_id: return jsonify({'status':'error', 'message':'QR missing data.'}), 400
        if scanned_fest_id != session.get('security_event_id'): return jsonify({'status':'error', 'message':'QR for wrong event.'}), 400
    except Exception as e: print(f"ERROR parsing QR: {e}"); return jsonify({'status':'error', 'message':'Invalid QR format.'}), 400
    
    try: # Verify against sheet
        client = get_gspread_client(); sheet_title = session['security_event_sheet_title']; headers = ['UniqueID','Name','Email','Mobile','College','Present','Timestamp']
        print(f"VerifyQR: Checking SS '{sheet_title}' for UID '{scanned_unique_id}'")
        reg_sheet = get_or_create_worksheet(client, sheet_title, "Registrations", headers);
        try: cell = reg_sheet.find(scanned_unique_id, in_column=1)
        except gspread.exceptions.CellNotFound: print(f"VerifyQR ERROR: UID '{scanned_unique_id}' not found."); return jsonify({'status':'error', 'message':'Participant not found.'}), 404
        if cell:
             row_data=reg_sheet.row_values(cell.row); p_idx,n_idx,e_idx,m_idx = 5,1,2,3; 
             def get_val(idx): return row_data[idx] if len(row_data)>idx else '';
             status,name,email,mobile = get_val(p_idx),get_val(n_idx),get_val(e_idx),get_val(m_idx)
             if str(status).strip().lower() == 'yes':
                 print(f"VerifyQR WARN: Already present: {name}"); return jsonify({'status':'warning','message':'ALREADY SCANNED!', 'name':name,'details':f"{email}, {mobile}"})
             print(f"VerifyQR: Marking present: {name}"); reg_sheet.update_cell(cell.row, p_idx+1, 'yes');
             return jsonify({'status':'success','message':'Access Granted!','name':name,'details':f"{email}, {mobile}"});
        else: # Fallback, though CellNotFound should catch it
             print(f"VerifyQR ERROR: UID '{scanned_unique_id}' not found (fallback)."); return jsonify({'status':'error','message':'Participant not found.'}), 404;
    except Exception as e: print(f"ERROR: Verify QR failed: {e}"); traceback.print_exc(); return jsonify({'status':'error', 'message':'Verification server error.'}), 500


# --- Initialization Function ---
def initialize_master_sheets_and_tabs():
    print("\n----- Initializing Master Sheets & Tabs -----")
    try: client, spreadsheet, _, _ = get_master_sheet_tabs(); print(f"Init Check PASSED: Master SS '{MASTER_SHEET_NAME}' ready.")
    except Exception as e: print(f"CRITICAL INIT ERROR getting sheets: {e}"); traceback.print_exc(); print("--- Aborting further init steps ---"); return
    try: share_spreadsheet_with_editor(spreadsheet, YOUR_PERSONAL_EMAIL, MASTER_SHEET_NAME)
    except Exception as e: print(f"WARN during sharing: {e}") 
    print("----- Initialization Complete -----\n")

# --- Main Execution Block ---
if __name__ == '__main__':
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
         print("Flask starting up - Main process: Initializing...")
         initialize_master_sheets_and_tabs()
         print("Flask startup - Main process: Initialization complete.")
    else: print("Flask starting up - Reloader process detected.")
         
    print("Starting Flask development server (host=0.0.0.0, port=5000)...")
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=True) 
