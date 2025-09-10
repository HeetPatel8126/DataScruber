import os
import sys
import argparse
import secrets
import signal
import time
import json
import shutil
import stat

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

def is_system_directory(dir_path):
    """Check if a directory is a system directory that should be skipped."""
    system_dirs = [
        # Windows system directories
        'System Volume Information',
        '$RECYCLE.BIN',
        'Recovery',
        'Windows',
        'Program Files',
        'Program Files (x86)',
        'ProgramData',
        'Users',
        'hiberfil.sys',
        'pagefile.sys',
        'swapfile.sys',
        # Linux system directories
        '.Trash',
        'lost+found',
        'proc',
        'sys',
        'dev',
        'boot',
        'etc',
        'lib',
        'lib64',
        'sbin',
        'bin',
        'usr',
        'var',
        'tmp',
        'opt',
        'run',
        'mnt',
        'media',
        # Mounted Windows drives in Linux commonly have these
        '$RECYCLE.BIN',
        'System Volume Information'
    ]
    
    dir_name = os.path.basename(dir_path)
    
    # Check for exact matches (case-insensitive)
    for sys_dir in system_dirs:
        if dir_name.lower() == sys_dir.lower():
            return True
    
    # Check for Windows system file patterns
    if dir_name.lower().startswith('$'):  # Windows system files often start with $
        return True
        
    # Check if it's a hidden system directory (starts with .)
    if dir_name.startswith('.') and len(dir_name) > 1:
        # Allow common user directories like .config, .local but skip system ones
        allowed_user_dirs = ['.config', '.local', '.cache', '.ssh', '.gnupg']
        if dir_name.lower() not in allowed_user_dirs:
            return True
    
    return False

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


# --- Deletion Helpers ---

def make_writable(path: str):
    """Best-effort: make a file/dir writable so it can be deleted (Windows + POSIX)."""
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    except Exception:
        pass

def final_sweep_delete_everything(target_path: str):
    """Best-effort pass to remove any remaining files and then empty directories.
    Returns (files_deleted, dirs_deleted).
    """
    files_deleted_total = 0
    dirs_deleted_total = 0

    # Try multiple passes to catch directories that become empty after earlier deletions
    for _ in range(3):
        files_deleted_this_pass = 0
        dirs_deleted_this_pass = 0

        for root, dirs, files in os.walk(target_path, topdown=False):
            # Delete any lingering files first
            for name in files:
                fpath = os.path.join(root, name)
                try:
                    make_writable(fpath)
                    os.remove(fpath)
                    files_deleted_this_pass += 1
                except Exception:
                    continue

            # Then try to delete directories
            for d in dirs:
                dpath = os.path.join(root, d)
                try:
                    # Make writable and attempt removal if empty
                    make_writable(dpath)
                    os.rmdir(dpath)
                    dirs_deleted_this_pass += 1
                except Exception:
                    continue

        files_deleted_total += files_deleted_this_pass
        dirs_deleted_total += dirs_deleted_this_pass

        if files_deleted_this_pass == 0 and dirs_deleted_this_pass == 0:
            break

    return files_deleted_total, dirs_deleted_total


# --- Core Wiping Logic ---

def phase_0_calculate_work(target_path, passes):
    """Phase 0: Calculate the total amount of data to be written."""
    status_update("status", "Calculating total work size...")
    total_bytes = 0
    file_paths = []
    
    # Calculate size of all files to be overwritten
    for root, dirs, files in os.walk(target_path):
        # Skip system directories that typically require elevated permissions
        # This modifies dirs in-place to prevent os.walk from descending into them
        original_dirs = dirs[:]
        dirs[:] = [d for d in dirs if not is_system_directory(os.path.join(root, d))]
        
        # Log skipped directories for user awareness
        skipped_dirs = set(original_dirs) - set(dirs)
        for skipped_dir in skipped_dirs:
            status_update("status", f"Skipping system directory: {os.path.join(root, skipped_dir)}")
        
        for file in files:
            file_path = os.path.join(root, file)
            try:
                # Skip hidden system files on Windows drives
                file_name = os.path.basename(file_path)
                if file_name.lower() in ['hiberfil.sys', 'pagefile.sys', 'swapfile.sys']:
                    status_update("status", f"Skipping Windows system file: {file_path}")
                    continue
                
                # Check if we can access the file before adding it
                if os.access(file_path, os.R_OK | os.W_OK):
                    file_size = os.path.getsize(file_path)
                    # Skip zero-byte files or files we can't read size of
                    if file_size > 0:
                        total_bytes += file_size
                        file_paths.append(file_path)
                    else:
                        status_update("status", f"Skipping empty/special file: {file_path}")
                else:
                    status_update("status", f"Skipping protected file: {file_path}")
            except (OSError, PermissionError) as e:
                status_update("status", f"Skipping inaccessible file: {file_path} - {e}")
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
        # Check permissions before attempting to open
        if not os.access(file_path, os.R_OK | os.W_OK):
            status_update("status", f"Skipping protected file: {file_path}")
            return processed_bytes
            
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

    except (IOError, OSError, PermissionError) as e:
        status_update("status", f"Warning: Could not overwrite {file_path}. Reason: {e}")
    
    return processed_bytes

def fill_free_space_and_report(target_path, start_time, total_work, processed_bytes, speed_tracker):
    """Fills free space and reports progress.
    Returns (junk_file_paths, target_path, created_count) where created_count counts
    only successfully written files; junk_file_paths includes any partially created
    files for cleanup.
    """
    junk_file_paths = []
    
    status_update("status", f"Starting free space fill phase in: {target_path}")
    
    # Check free space before starting
    try:
        _, _, free_space = shutil.disk_usage(target_path)
        status_update("status", f"Available free space: {format_size(free_space)}")
        if free_space < 1024 * 1024:  # Less than 1MB
            status_update("status", "Very little free space available, skipping fill phase")
            return [], target_path, 0
    except Exception as e:
        status_update("status", f"Could not check free space: {e}")
    
    status_update("status", f"Creating temp files directly in: {target_path}")
    
    try:
        count = 0  # number of successfully written files
        status_update("status", "Starting temp file creation to fill free space...")
        
        while not is_cancelled:
            file_index = count  # next file index to try creating
            junk_file_path = os.path.join(target_path, f'junk_fill_{file_index}.tmp')
            
            try:
                chunk_size = 1024 * 1024 * 32 # 32MB chunks
                # Pre-generate random data
                random_data = secrets.token_bytes(chunk_size)
                
                # Measure only actual write operation time
                chunk_start_time = time.perf_counter()
                with open(junk_file_path, 'wb') as f:
                    # Append to cleanup list once file handle is opened (file now exists)
                    junk_file_paths.append(junk_file_path)
                    f.write(random_data)
                    f.flush()  # Force write to disk
                chunk_duration = time.perf_counter() - chunk_start_time
                processed_bytes += chunk_size
                count += 1  # success
                
                # Log every 10 files created (use zero-based index for message parity)
                if (count - 1) % 10 == 0:
                    status_update("status", f"Created temp file #{count - 1}: {format_size(chunk_size)} - Total: {format_size(processed_bytes)}")

                # --- Improved Progress Calculation & Reporting ---
                elapsed_time = time.time() - start_time
                
                # Update speed tracker (only for meaningful measurements)
                if chunk_duration > 0.01:  # Ignore very fast chunks (likely cached)
                    current_speed = chunk_size / chunk_duration
                    speed_tracker.append(current_speed)
                    if len(speed_tracker) > 20:
                        speed_tracker.pop(0)
                
                # For free space filling, estimate ETA based on remaining free space and smoothed speed
                if len(speed_tracker) >= 5 and elapsed_time > 5:  # Need sufficient samples
                    sorted_speeds = sorted(speed_tracker)
                    mid = len(sorted_speeds) // 2
                    if len(sorted_speeds) % 2 == 0:
                        smoothed_speed = (sorted_speeds[mid-1] + sorted_speeds[mid]) / 2
                    else:
                        smoothed_speed = sorted_speeds[mid]
                    # Estimate remaining free space
                    try:
                        _, _, free = shutil.disk_usage(target_path)
                        remaining_work = free
                        eta = remaining_work / smoothed_speed if smoothed_speed > 0 else float('inf')
                        eta_display = format_eta(eta) if eta != float('inf') else "Calculating..."
                    except Exception:
                        eta_display = "Calculating..."
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
            except (IOError, OSError) as e:
                # Disk is likely full or write error
                # Only add to cleanup if file was already added during successful open
                # (the file gets added to junk_file_paths when we open it successfully)
                status_update("status", f"Disk full or write error after {count} temp files: {e}")
                break
        
        # Use 'count' (successful writes) for creation summary, not total tracked files
        # chunk_size defined in loop; if loop never ran, default to 0
        try:
            total_written_size = count * chunk_size
        except UnboundLocalError:
            total_written_size = 0
        status_update("status", f"Free space fill completed. Created {count} temp files totaling {format_size(total_written_size)}")
        # After filling, return the list of temp files, the target path, and the successful count
        return junk_file_paths, target_path, count
    except (IOError, OSError) as e:
        status_update("status", f"Disk space is full. Stopping junk file creation. Error: {e}")
        # Return what we have so far; zero successful files if we didn't track count
        return junk_file_paths, target_path, 0

def main():
    parser = argparse.ArgumentParser(description="Securely wipe a directory.")
    parser.add_argument('-p', '--path', required=True, help="The absolute path to the directory to wipe.")
    parser.add_argument('-m', '--mode', required=True, choices=['quick', 'secure', 'paranoid'], help="The wiping mode.")
    args = parser.parse_args()

    if not os.path.exists(args.path):
        status_update("error", f"Target path does not exist: {args.path}")
        sys.exit(1)
    
    if not os.access(args.path, os.R_OK):
        status_update("error", f"No read permission for target path: {args.path}")
        if os.name == 'posix':  # Linux/Unix
            status_update("status", "Try running with sudo: sudo python3 main.py ...")
            if args.path.startswith('/mnt/') or args.path.startswith('/media/'):
                status_update("status", "For mounted drives, you may need to remount with user permissions")
        else:  # Windows
            status_update("status", "Try running as Administrator")
        sys.exit(1)
    
    # Additional check for mounted drives on Linux
    if os.name == 'posix' and (args.path.startswith('/mnt/') or args.path.startswith('/media/')):
        try:
            # Try to create a test file to verify write permissions
            test_file = os.path.join(args.path, '.permission_test_temp')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except (OSError, PermissionError):
            status_update("error", f"No write permission for mounted drive: {args.path}")
            status_update("status", "Mounted Windows drives may require special mount options for write access")
            status_update("status", "Try: sudo mount -o remount,uid=$(id -u),gid=$(id -g) {mount_point}")
            sys.exit(1)

    junk_files_created = []
    temp_location = None

    try:
        if args.mode == 'quick':
            # Quick mode does not get an ETA as it's just deleting files.
            status_update("status", "Mode: Quick Wipe (File Deletion Only)")
            # Re-implement quick delete logic here for simplicity
            files_deleted = 0
            dirs_deleted = 0
            errors_encountered = 0
            
            for root, dirs, files in os.walk(args.path, topdown=False):
                # Skip system directories
                dirs[:] = [d for d in dirs if not is_system_directory(os.path.join(root, d))]
                
                # Delete files first
                for name in files:
                    file_path = os.path.join(root, name)
                    try:
                        if os.access(file_path, os.W_OK):
                            os.remove(file_path)
                            files_deleted += 1
                        else:
                            status_update("status", f"Skipping protected file: {file_path}")
                            errors_encountered += 1
                    except (OSError, PermissionError) as e:
                        status_update("status", f"Could not delete file {file_path}: {e}")
                        errors_encountered += 1
                
                # Then delete directories (they should be empty now)
                for name in dirs:
                    dir_path = os.path.join(root, name)
                    try:
                        if os.access(dir_path, os.W_OK):
                            os.rmdir(dir_path)
                            dirs_deleted += 1
                        else:
                            status_update("status", f"Skipping protected directory: {dir_path}")
                            errors_encountered += 1
                    except (OSError, PermissionError) as e:
                        status_update("status", f"Could not delete directory {dir_path}: {e}")
                        errors_encountered += 1
            
            # Final sweep to ensure no leftovers (handles read-only and nested cases)
            fs_files, fs_dirs = final_sweep_delete_everything(args.path)
            files_deleted += fs_files
            dirs_deleted += fs_dirs

            status_update("status", f"Quick wipe completed: {files_deleted} files and {dirs_deleted} directories deleted")
            if errors_encountered > 0:
                status_update("status", f"Note: {errors_encountered} items could not be deleted due to permissions")
            
            # No temp files created in quick mode
            junk_files_created = []
            temp_location = None

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
            
            # --- Phase 2: Delete Files and Folders ---
            if not is_cancelled:
                status_update("status", "Deleting overwritten files...")
                for file_path in file_paths:
                    try: os.remove(file_path)
                    except OSError: pass
                
                status_update("status", "Deleting empty directories...")
                # Delete all directories (including nested ones) - multiple passes to handle nested structure
                deleted_dirs = 0
                max_attempts = 5  # Prevent infinite loop
                attempt = 0
                
                while attempt < max_attempts:
                    attempt += 1
                    dirs_deleted_this_pass = 0
                    
                    # Walk through all directories bottom-up
                    for root, dirs, files in os.walk(args.path, topdown=False):
                        # Skip system directories
                        dirs[:] = [d for d in dirs if not is_system_directory(os.path.join(root, d))]
                        
                        for d in dirs:
                            dir_path = os.path.join(root, d)
                            try:
                                # Try to delete directory (will only work if empty)
                                os.rmdir(dir_path)
                                dirs_deleted_this_pass += 1
                                deleted_dirs += 1
                            except OSError:
                                # Directory not empty or permission denied
                                pass
                    
                    # If no directories were deleted in this pass, we're done
                    if dirs_deleted_this_pass == 0:
                        break
                
                # Final sweep to ensure no leftovers (handles read-only and nested cases)
                fs_files, fs_dirs = final_sweep_delete_everything(args.path)
                deleted_dirs += fs_dirs
                status_update("status", f"Deleted {deleted_dirs} directories (final sweep removed {fs_dirs} more; {fs_files} stray files removed)")

            # --- Phase 3: Fill Free Space ---
            if not is_cancelled:
                status_update("status", "Phase 3: Starting free space fill phase...")
                junk_files_created, temp_location, created_count = fill_free_space_and_report(args.path, start_time, total_work, processed_bytes, speed_tracker)
                status_update("status", f"Phase 3 completed. {created_count} temp files created.")
        
        # --- Phase 4: Cleanup ---
        status_update("status", f"Cleaning up {len(junk_files_created)} temporary files...")
        deleted_count = 0
        for f in junk_files_created:
            try: 
                os.remove(f)
                deleted_count += 1
            except OSError as e:
                status_update("status", f"Could not delete temp file {f}: {e}")
        
        status_update("status", f"Deleted {deleted_count} out of {len(junk_files_created)} temp files")

        if is_cancelled:
            sys.exit(130)
        else:
            status_update("status", "All operations completed.")

    except PermissionError as e:
        status_update("error", f"Permission denied: {e}")
        status_update("status", "This typically happens when trying to access system files.")
        status_update("status", "Try running with elevated permissions or choose a different target path.")
        # Still try to clean up
        for f in junk_files_created:
            try: os.remove(f)
            except OSError: pass
        sys.exit(1)
    except Exception as e:
        status_update("error", f"An unexpected error occurred: {e}")
        # Still try to clean up
        for f in junk_files_created:
            try: os.remove(f)
            except OSError: pass
        sys.exit(1)

if __name__ == '__main__':
    main()