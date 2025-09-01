import os
import sys
import argparse
import secrets
import signal
import time
import json
import shutil

# --- Signal Handling for Graceful Cancellation ---
is_cancelled = False

def signal_handler(signum, frame):
    """Sets the cancellation flag when a signal is received."""
    global is_cancelled
    if not is_cancelled:
        # We send a JSON message so the Node.js frontend can display it cleanly
        status_update("status", "Cancellation signal received. Cleaning up...")
        is_cancelled = True

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


# --- Helper Functions ---

def status_update(msg_type, message):
    """Sends a simple status message to the Node.js frontend."""
    print(json.dumps({"type": msg_type, "message": message}), flush=True)

def format_eta(seconds):
    """Formats seconds into a HH:MM:SS string."""
    if seconds is None or seconds < 0:
        return "--:--:--"
    return time.strftime('%H:%M:%S', time.gmtime(seconds))

def format_size(byte_count):
    """Formats bytes into a human-readable string (KB, MB, GB)."""
    if byte_count is None: return "0 B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while byte_count >= power and n < len(power_labels) -1 :
        byte_count /= power
        n += 1
    return f"{byte_count:.2f} {power_labels[n]}B"


# --- Core Wiping Logic ---

def phase_0_calculate_work(target_path, passes):
    """Phase 0: Calculate the total amount of data to be written."""
    status_update("status", "Calculating total work size...")
    total_bytes = 0
    file_paths = []
    
    # Calculate size of all files to be overwritten
    for root, dirs, files in os.walk(target_path):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                total_bytes += os.path.getsize(file_path)
                file_paths.append(file_path)
            except OSError:
                pass # Ignore files we can't access
    
    total_bytes *= passes

    # Add free space for secure/paranoid modes
    try:
        _, _, free = shutil.disk_usage(target_path)
        total_bytes += free
    except OSError as e:
        status_update("status", f"Warning: Could not get free space. ETA may be inaccurate. Reason: {e}")

    return total_bytes, file_paths


def overwrite_and_report(file_path, passes, start_time, total_work, processed_bytes, speed_tracker):
    """Overwrites a single file and reports progress."""
    try:
        file_size = os.path.getsize(file_path)
        with open(file_path, 'rb+') as f:
            for i in range(passes):
                if is_cancelled: return processed_bytes
                
                f.seek(0)
                chunk_size = 1024 * 1024 # 1MB chunks
                remaining_bytes = file_size
                
                while remaining_bytes > 0:
                    if is_cancelled: return processed_bytes
                    
                    bytes_to_write = min(chunk_size, remaining_bytes)
                    
                    # Pre-generate random data to exclude generation time from write speed
                    random_data = secrets.token_bytes(bytes_to_write)
                    
                    # Measure only actual write operation time
                    chunk_start_time = time.perf_counter()  # More precise than time.time()
                    f.write(random_data)
                    f.flush()  # Force write to disk (bypass OS buffers)
                    chunk_duration = time.perf_counter() - chunk_start_time
                    
                    remaining_bytes -= bytes_to_write
                    processed_bytes += bytes_to_write
                    
                    # --- Improved Progress Calculation & Reporting ---
                    elapsed_time = time.time() - start_time
                    
                    # Update speed tracker with recent chunk performance
                    if chunk_duration > 0.001:  # Ignore very fast chunks (likely cached)
                        current_speed = bytes_to_write / chunk_duration
                        speed_tracker.append(current_speed)
                        # Keep only last 20 speed measurements for better smoothing
                        if len(speed_tracker) > 20:
                            speed_tracker.pop(0)
                    
                    # Calculate smoothed speed (average of recent measurements)
                    if len(speed_tracker) >= 5 and elapsed_time > 3:  # Need at least 5 samples and 3 seconds
                        # Use median instead of average to filter out outliers
                        sorted_speeds = sorted(speed_tracker)
                        mid = len(sorted_speeds) // 2
                        if len(sorted_speeds) % 2 == 0:
                            smoothed_speed = (sorted_speeds[mid-1] + sorted_speeds[mid]) / 2
                        else:
                            smoothed_speed = sorted_speeds[mid]
                        
                        remaining_work = total_work - processed_bytes
                        eta = remaining_work / smoothed_speed if smoothed_speed > 0 else float('inf')
                        eta_display = format_eta(eta) if eta != float('inf') else "Calculating..."
                    else:
                        # Fallback to total average for initial period
                        smoothed_speed = processed_bytes / elapsed_time if elapsed_time > 0 else 0
                        eta_display = "Calculating..."
                    
                    percentage = (processed_bytes / total_work) * 100 if total_work > 0 else 100
                    
                    # Only report progress every 5MB or every 5 seconds to reduce spam
                    if (processed_bytes % (5 * 1024 * 1024) < bytes_to_write) or (time.time() - getattr(overwrite_and_report, 'last_report_time', 0) > 5):
                        overwrite_and_report.last_report_time = time.time()
                        progress_data = {
                            "type": "progress",
                            "phase": f"Overwrite (Pass {i+1}/{passes})",
                            "percentage": round(percentage, 2),
                            "speed": f"{format_size(smoothed_speed)}/s",
                            "eta": eta_display
                        }
                        print(json.dumps(progress_data), flush=True)

    except (IOError, OSError) as e:
        status_update("status", f"Warning: Could not overwrite {file_path}. Reason: {e}")
    
    return processed_bytes

def fill_free_space_and_report(target_path, start_time, total_work, processed_bytes, speed_tracker):
    """Fills free space and reports progress."""
    junk_file_paths = []
    try:
        count = 0
        while not is_cancelled:
            junk_file_path = os.path.join(target_path, f'junk_fill_{count}.tmp')
            junk_file_paths.append(junk_file_path)
            
            try:
                chunk_size = 1024 * 1024 * 32 # 32MB chunks
                # Pre-generate random data
                random_data = secrets.token_bytes(chunk_size)
                
                # Measure only actual write operation time
                chunk_start_time = time.perf_counter()
                with open(junk_file_path, 'wb') as f:
                    f.write(random_data)
                    f.flush()  # Force write to disk
                chunk_duration = time.perf_counter() - chunk_start_time
                processed_bytes += chunk_size

                # --- Improved Progress Calculation & Reporting ---
                elapsed_time = time.time() - start_time
                
                # Update speed tracker (only for meaningful measurements)
                if chunk_duration > 0.01:  # Ignore very fast chunks (likely cached)
                    current_speed = chunk_size / chunk_duration
                    speed_tracker.append(current_speed)
                    if len(speed_tracker) > 20:
                        speed_tracker.pop(0)
                
                # For free space filling, we can't know exact endpoint, so be conservative
                if len(speed_tracker) >= 5 and elapsed_time > 5:  # Need sufficient samples
                    # Use median for more stable speed measurement
                    sorted_speeds = sorted(speed_tracker)
                    mid = len(sorted_speeds) // 2
                    if len(sorted_speeds) % 2 == 0:
                        smoothed_speed = (sorted_speeds[mid-1] + sorted_speeds[mid]) / 2
                    else:
                        smoothed_speed = sorted_speeds[mid]
                    eta_display = "Unknown (filling until full)"
                else:
                    smoothed_speed = processed_bytes / elapsed_time if elapsed_time > 0 else 0
                    eta_display = "Calculating..."
                
                # Cap percentage to prevent going over 100%
                percentage = min((processed_bytes / total_work) * 100 if total_work > 0 else 100, 99.9)
                
                # Report progress less frequently to reduce spam
                if count % 5 == 0 or (time.time() - getattr(fill_free_space_and_report, 'last_report_time', 0) > 3):
                    fill_free_space_and_report.last_report_time = time.time()
                    progress_data = {
                        "type": "progress",
                        "phase": "Fill Free Space",
                        "percentage": round(percentage, 2),
                        "speed": f"{format_size(smoothed_speed)}/s",
                        "eta": eta_display
                    }
                    print(json.dumps(progress_data), flush=True)
                count += 1
            except (IOError, OSError):
                # Disk is likely full, break the loop
                break
    except (IOError, OSError):
        status_update("status", "Disk space is full. Stopping junk file creation.")
    
    return junk_file_paths

def main():
    parser = argparse.ArgumentParser(description="Securely wipe a directory.")
    parser.add_argument('-p', '--path', required=True, help="The absolute path to the directory to wipe.")
    parser.add_argument('-m', '--mode', required=True, choices=['quick', 'secure', 'paranoid'], help="The wiping mode.")
    args = parser.parse_args()

    junk_files_created = []

    try:
        if args.mode == 'quick':
            # Quick mode does not get an ETA as it's just deleting files.
            status_update("status", "Mode: Quick Wipe (File Deletion Only)")
            # Re-implement quick delete logic here for simplicity
            for root, dirs, files in os.walk(args.path, topdown=False):
                for name in files: os.remove(os.path.join(root, name))
                for name in dirs: os.rmdir(os.path.join(root, name))

        else: # secure or paranoid
            passes = 3 if args.mode == 'paranoid' else 1
            total_work, file_paths = phase_0_calculate_work(args.path, passes)
            
            processed_bytes = 0
            start_time = time.time()
            speed_tracker = []  # Track recent speed measurements for smoothing

            # --- Phase 1: Overwrite ---
            for file_path in file_paths:
                if is_cancelled: break
                processed_bytes = overwrite_and_report(file_path, passes, start_time, total_work, processed_bytes, speed_tracker)
            
            # --- Phase 2: Delete ---
            if not is_cancelled:
                status_update("status", "Deleting overwritten files...")
                for file_path in file_paths:
                    try: os.remove(file_path)
                    except OSError: pass
                # Delete empty directories
                for root, dirs, files in os.walk(args.path, topdown=False):
                    for d in dirs:
                        try: os.rmdir(os.path.join(root, d))
                        except OSError: pass

            # --- Phase 3: Fill Free Space ---
            if not is_cancelled:
                junk_files_created = fill_free_space_and_report(args.path, start_time, total_work, processed_bytes, speed_tracker)
        
        # --- Phase 4: Cleanup ---
        status_update("status", "Cleaning up temporary files...")
        for f in junk_files_created:
            try: os.remove(f)
            except OSError: pass

        if is_cancelled:
            sys.exit(130)
        else:
            status_update("status", "All operations completed.")

    except Exception as e:
        status_update("error", f"An unexpected error occurred: {e}")
        # Still try to clean up
        for f in junk_files_created:
            try: os.remove(f)
            except OSError: pass
        sys.exit(1)

if __name__ == '__main__':
    main()
