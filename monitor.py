import subprocess
import time
import platform
import csv
import os
import re              # The stencil kit
from datetime import datetime
import json            # For the stats file
import random
import smtplib
from email.message import EmailMessage
import logging
import config          # add email credentials to separate config file to keep data from main stack/gitup 

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    filename='system_errors.log',
    level=logging.ERROR, # Only record Errors and above
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- CONFIGURATION ---
TARGETS = ["8.8.8.8", "1.1.1.1"]                                        # Google, CloudFlair
GATEWAY_IP = "192.168.1.1"
NORMAL_INTERVAL = 5
BURST_INTERVAL = 1
PING_FLAG = "-n" if platform.system().lower() == "windows" else "-c"    # Config for platform detection
LATENCY_THRESHOLD = 50.0                                                # Mark as LAGGING if > 50ms
COOLDOWN_LIMIT = 15                                                     # Stay in Burst Mode for 10 pings after an issue
DASHBOARD_INTERVAL = 300                                                # 5 minutes in seconds
STATS_FILE = "network_stats.json"
DASHBOARD_FILE = "Dashboard.md"
RETENTION_DAYS = 5                                                      # How many days of logs to keep
LOG_DIR = "logs"
ACTIVE_LOG = "Network_log_ACTIVE.csv"
                                          

# --- EMAIL CONFIGURATION ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
EMAIL_SENDER = config.EMAIL_SENDER
EMAIL_RECEIVER = config.EMAIL_RECEIVER
EMAIL_PASSWORD = config.EMAIL_PASSWORD

# --- STATE VARIABLES ---
last_dashboard_update = 0
current_log_file = ""
last_status = "UP"
event_start_time = None
burst_counter = 0                                                       # Keeps track of our "sticky" mode
last_cleanup_date = ""
trigger_diagnostics = []
status = "UP"
active_fault_location = "N/A"

# --- SESSION SCRATCHPAD ---
session_pings = 0
session_success = 0
session_total_latency = 0.0

# Create the logs folder if it doesn't exist
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)


def rotate_log_if_needed(current_status):
    global current_log_file
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    period = "Morning" if now.hour < 12 else "Afternoon"
    archive_name = f"Network_log_{date_str}_{period}.csv"
    archive_path = os.path.join(LOG_DIR, archive_name)
    
    # --- THE RESTART GUARD ---
    # If this is the very first check (memory is empty)
    if current_log_file == "":
        if os.path.exists(ACTIVE_LOG):
            # Check when the active log was last modified
            file_time = datetime.fromtimestamp(os.path.getmtime(ACTIVE_LOG))
            file_period = "Morning" if file_time.hour < 12 else "Afternoon"
            file_date = file_time.strftime("%Y-%m-%d")
            
            # If the file on disk is from the same date AND period, don't rotate!
            if file_date == date_str and file_period == period:
                current_log_file = archive_name
                print(f"--- Resuming existing {period} session ---")
                return # Exit the function early; no rotation needed

    if current_status == "UP" and archive_name != current_log_file:
        if os.path.exists(ACTIVE_LOG):
            # COLLISION GUARD: If the archive already exists, add a timestamp
            if os.path.exists(archive_path):
                collision_time = now.strftime("%H%M%S")
                archive_path = os.path.join(LOG_DIR, f"Network_log_{date_str}_{period}_{collision_time}.csv")
            
            os.rename(ACTIVE_LOG, archive_path)
            logging.info(f"Archived: {ACTIVE_LOG} -> {archive_path}")
        
        # Start fresh ACTIVE file
        random.shuffle(TARGETS)
        with open(ACTIVE_LOG, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([f"--- New Session: {now} ---"])
            writer.writerow(["Timestamp", "Status", "Latency_ms", "Fault_Location"]) # <--- Added 4th column
        
        current_log_file = archive_name

def update_dashboard():
    global session_pings, session_success, session_total_latency
    
    # 1. Load existing stats
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            data = json.load(f)
    else:
        data = {"daily_stats": {}, "events": []}

    # 2. Update Today's Tally
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in data["daily_stats"]:
        data["daily_stats"][today] = {"pings": 0, "success": 0, "latency_sum": 0.0}
    
    data["daily_stats"][today]["pings"] += session_pings
    data["daily_stats"][today]["success"] += session_success
    data["daily_stats"][today]["latency_sum"] += session_total_latency

    # 3. Calculate Rolling 10-Day Stats
    all_days = sorted(data["daily_stats"].keys(), reverse=True)[:10]
    total_pings = sum(data["daily_stats"][d]["pings"] for d in all_days)
    total_success = sum(data["daily_stats"][d]["success"] for d in all_days)
    total_lat_sum = sum(data["daily_stats"][d]["latency_sum"] for d in all_days)
    
    uptime_pct = (total_success / total_pings * 100) if total_pings > 0 else 0
    avg_lat = (total_lat_sum / total_success) if total_success > 0 else 0

    # 4. Write JSON (The Brain)
    with open(STATS_FILE, 'w') as f:
        json.dump(data, f, indent=4)

    # 5. Write Markdown (The Presentation)
    with open(DASHBOARD_FILE, 'w') as f:
        f.write(f"# Network Dashboard \n\n")
        f.write(f"**Uptime:** `{uptime_pct:.2f}%` | **Avg Latency:** `{avg_lat:.1f}ms` | **Last** `{len(all_days)}` Days \n\n")
        f.write(f"*Last Updated: {datetime.now().strftime('%H:%M:%S')}*\n\n")
        f.write("### Recent Events\n")
        f.write("| Start | End | Duration | Status |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        # Show last 10 events
        for e in reversed(data["events"][-10:]):
            f.write(f"| {e['start']} | {e['end']} | {e['duration']} | **{e['status']}** |\n")

    # 6. Reset session counters
    session_pings = 0
    session_success = 0
    session_total_latency = 0.0

def cleanup_old_logs():
    now = time.time()
    cutoff = now - (RETENTION_DAYS * 86400)
    
    # Now looking inside the LOG_DIR
    for filename in os.listdir(LOG_DIR):
        file_path = os.path.join(LOG_DIR, filename)
        if os.path.getmtime(file_path) < cutoff:
            try:
                os.remove(file_path)
                logging.info(f"Housekeeper: Deleted old log {filename}")
            except Exception as e:
                logging.error(f"Housekeeper Error: {e}")

def send_notification(subject, body):
    msg = EmailMessage()
    msg.set_content(body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER

    try:
        # We use SMTP_SSL for a secure connection on port 465
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
            print(f"--- Notification Sent: {subject} ---")
    except Exception as e:
        # If the internet is truly DOWN, this will fail. 
        # We just print the error so the script doesn't crash.
        print(f"--- Email Failed (Likely no internet): {e} ---")


while True:
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. LOG ROTATION: Check if swap - ONLY if status is UP
    rotate_log_if_needed(status)

    # 2. PING LOGIC (Upgraded for Granular Diagnostics)
    connection_is_up = False
    latency = 0.0
    cycle_diagnostics = [] # Temporary list for this specific 5-second check
    for target in TARGETS:
        attempt_time = datetime.now().strftime("%H:%M:%S")
        try:
            output = subprocess.check_output(["ping", PING_FLAG, "1", "-W", "2", target], 
                                             universal_newlines=True, stderr=subprocess.STDOUT)
            match = re.search(r"time=(\d+\.?\d*)", output)
            lat = float(match.group(1)) if match else 0.0
            
            cycle_diagnostics.append({"target": target, "time": attempt_time, "result": "SUCCESS", "lat": lat})
            
            # If we haven't found a success yet, mark this as the "winning" latency
            if not connection_is_up:
                latency = lat
                connection_is_up = True
            #  still break here to save time/bandwidth, 
            # BUT if we are DOWN, it will have checked ALL targets.
            break 
            
        except subprocess.CalledProcessError:
            cycle_diagnostics.append({"target": target, "time": attempt_time, "result": "FAILED", "lat": 0.0})
            continue

    # 3. STATUS & EVENT DETECTION
    status = "UP"
    fault_location = "N/A"
    if not connection_is_up:
        status = "DOWN"
        burst_counter = COOLDOWN_LIMIT

        # --- DEMARCATION TEST ---
        try:
            subprocess.check_output(["ping", PING_FLAG, "1", "-W", "2", GATEWAY_IP], 
                                     universal_newlines=True, stderr=subprocess.STDOUT)
            fault_location = "ISP"
        except subprocess.CalledProcessError:
            fault_location = "LOCAL"
        
    elif latency > LATENCY_THRESHOLD:
        status = "LAGGING"
        burst_counter = COOLDOWN_LIMIT
    else:
        if burst_counter > 0: 
            burst_counter -= 1

    # --- Accumulate data for the 5-minute dashboard ---
    session_pings += 1
    if status != "DOWN":
        session_success += 1
        session_total_latency += latency

        # --- Check if an event started or ended ---
    if status != last_status:
        # --- EVENT STARTED ---
        if status in ["DOWN", "LAGGING"]:
            event_start_time = datetime.now()
            trigger_diagnostics = cycle_diagnostics 
            active_fault_location = fault_location
            
            # Email only for total outages
            if status == "DOWN":
                send_notification(
                    f"Network Alert: {status}",
                    f"The connection shifted to {status} at {timestamp}." # <--- Address removed
                )
        
        # --- EVENT ENDED ---
        elif last_status in ["DOWN", "LAGGING"] and event_start_time:
            end_time = datetime.now()
            duration = str(end_time - event_start_time).split(".")[0]
            
            # Save the event to JSON
            try:
                if os.path.exists(STATS_FILE):
                    with open(STATS_FILE, 'r') as f: 
                        data = json.load(f)
                else:
                    data = {"daily_stats": {}, "events": []}
                
                data["events"].append({
                    "start": event_start_time.strftime("%H:%M:%S"),
                    "end": end_time.strftime("%H:%M:%S"),
                    "duration": duration,
                    "status": last_status,
                    "details": trigger_diagnostics 
                })
                
                with open(STATS_FILE, 'w') as f: 
                    json.dump(data, f, indent=4)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                logging.warning(f"Stats file corrupted: {e}. Resetting.")
                data = {"daily_stats": {}, "events": []}
            
            # Resolution email
            if last_status == "DOWN":
                time.sleep(5) 
                send_notification(
                    "Network Incident Report",
                    f"A total outage was detected.\n\n"
                    f"Started: {event_start_time.strftime('%H:%M:%S')}\n"
                    f"Restored: {end_time.strftime('%H:%M:%S')}\n"
                    f"Fault Location: {active_fault_location}\n" # <--- Added to email
                    f"Total Downtime: {duration}\n\n"
                    f"The system has resumed normal monitoring."
                )
            
            active_fault_location = "N/A"  # Reset it after sending the email
         
        last_status = status

# 4. DATA LOGGING (Wrapped in safety)
    try:
        formatted_latency = f"{latency:05.1f}"
        with open(ACTIVE_LOG, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, status, formatted_latency, fault_location]) # <--- Added 4th column
    except IOError as e:
        logging.error(f"Failed to write to CSV log: {e}")

    # 5. DASHBOARD & MAINTENANCE TIMER
    if time.time() - last_dashboard_update > DASHBOARD_INTERVAL:
        update_dashboard()
        
        # HOUSEKEEPER: Only run once per calendar day
        today_date = datetime.now().strftime("%Y-%m-%d")
        if last_cleanup_date != today_date:
            cleanup_old_logs()
            last_cleanup_date = today_date # Mark today as done
            
        last_dashboard_update = time.time()
        
    # 6. SLEEP
    sleep_time = BURST_INTERVAL if (burst_counter > 0 or status == "DOWN") else NORMAL_INTERVAL
    print(f"[{timestamp}] {status:7} - {formatted_latency}ms (Cooldown: {burst_counter})")
    time.sleep(sleep_time)