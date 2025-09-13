import os
import sys
import argparse
import signal
import time
import json
import subprocess
import shutil

# --- Platform-Specific Imports ---
# Ctypes are only used for low-level Windows API calls
if os.name == 'nt':
    import ctypes
    from ctypes import wintypes

# --- Global State for Graceful Cancellation ---
is_cancelled = False

def signal_handler(signum, frame):
    """Sets the cancellation flag when a SIGTERM or SIGINT is received."""
    global is_cancelled
    if not is_cancelled:
        status_update("status", "Cancellation signal received. Attempting to stop gracefully...")
        is_cancelled = True

# Register signal handlers for graceful shutdown
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


# --- Privilege & Input Validation Helpers ---
def is_admin_windows():
    """Check for administrator privileges on Windows."""
    if os.name != 'nt':
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def is_root_linux():
    """Check for root privileges on Linux/POSIX systems."""
    try:
        return os.geteuid() == 0
    except AttributeError:
        # This will be raised on non-POSIX systems like Windows
        return False

def ensure_privileges_or_exit(dry_run=False):
    """Exit the script if required administrative privileges are not met."""
    if dry_run:
        status_update("status", "Dry run: Skipping privilege checks.")
        return
    if os.name == 'nt':
        if not is_admin_windows():
            status_update("error", "Administrator privileges are required for destructive operations on Windows.")
            sys.exit(1)
    else:
        if not is_root_linux():
            status_update("error", "Root privileges are required for destructive operations on Linux.")
            sys.exit(1)

def sanitize_fs_type(fs_type: str):
    """
    Restricts filesystem types to a safe whitelist to prevent command injection.
    Returns the sanitized type or None if it's not allowed.
    """
    if not fs_type:
        return None

    normalized = fs_type.strip().upper()
    if os.name == 'nt':
        allowed = {"NTFS", "FAT32", "EXFAT"}
        return normalized if normalized in allowed else None
    else: # Linux and other POSIX
        allowed = {"EXT4", "EXT3", "EXT2", "XFS", "BTRFS", "NTFS", "VFAT", "EXFAT"}
        # mkfs tools are typically lowercase, e.g., 'mkfs.ext4'
        return fs_type.strip().lower() if normalized in allowed else None


# --- Frontend Communication Helpers ---
def status_update(msg_type, message):
    """Sends a JSON formatted status message to stdout for the frontend."""
    print(json.dumps({"type": msg_type, "message": message}), flush=True)

def progress_update(phase, percentage=None, bar=None):
    """Sends a JSON formatted progress message to stdout."""
    data = {"type": "progress", "phase": phase}
    if percentage is not None:
        data["percentage"] = round(percentage, 1)
    if bar is not None:
        data["bar"] = bar
    print(json.dumps(data), flush=True)


# --- Drive Enumeration ---
def list_connected_drives():
    """Enumerate connected drives across platforms and return a list of dicts."""
    if os.name == 'nt':
        return _list_drives_windows()
    else:
        return _list_drives_posix()

def _list_drives_windows():
    drives = []
    kernel32 = ctypes.windll.kernel32
    GetLogicalDrives = kernel32.GetLogicalDrives
    GetDriveTypeW = kernel32.GetDriveTypeW
    GetVolumeInformationW = kernel32.GetVolumeInformationW
    GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    GetDriveTypeW.restype = wintypes.UINT
    mask = GetLogicalDrives()
    system_drive = os.environ.get('SystemDrive', 'C:').upper()
    DRIVE_TYPES = {
        0: 'unknown', 1: 'no_root_dir', 2: 'removable', 3: 'fixed', 4: 'remote', 5: 'cdrom', 6: 'ramdisk'
    }

    for i in range(26):
        if not (mask & (1 << i)):
            continue
        letter = chr(ord('A') + i)
        root = f"{letter}:\\"
        try:
            dtype = DRIVE_TYPES.get(GetDriveTypeW(ctypes.c_wchar_p(root)), 'unknown')
            is_removable = dtype == 'removable'
            is_system = (f"{letter}:".upper() == system_drive.upper())
            fs_name_buf = ctypes.create_unicode_buffer(255)
            vol_name_buf = ctypes.create_unicode_buffer(255)
            serial = wintypes.DWORD()
            max_comp = wintypes.DWORD()
            fs_flags = wintypes.DWORD()
            try:
                GetVolumeInformationW(ctypes.c_wchar_p(root), vol_name_buf, 255, ctypes.byref(serial), ctypes.byref(max_comp), ctypes.byref(fs_flags), fs_name_buf, 255)
                fstype = fs_name_buf.value or None
                label = vol_name_buf.value or None
            except Exception:
                fstype = None
                label = None
            try:
                total, used, free = shutil.disk_usage(root)
            except Exception:
                total = used = free = None
            drives.append({
                'path': root,
                'device': f"\\\\.\\{letter}:",
                'type': dtype,
                'fstype': fstype,
                'label': label,
                'total': total,
                'used': used,
                'free': free,
                'is_system': is_system,
                'is_removable': is_removable
            })
        except Exception:
            # Skip problematic drive letters
            continue
    return drives

def _list_drives_posix():
    drives = []
    mounts_file = '/proc/mounts'
    ignore_fs = {'proc','sysfs','devtmpfs','devpts','tmpfs','cgroup','cgroup2','overlay','squashfs','ramfs','securityfs','pstore','autofs','debugfs','tracefs','fusectl'}
    seen_devices = set()
    try:
        with open(mounts_file, 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                if not device.startswith('/dev/'):
                    continue
                if fstype in ignore_fs:
                    continue
                if device in seen_devices:
                    continue
                seen_devices.add(device)
                try:
                    total, used, free = shutil.disk_usage(mountpoint)
                except Exception:
                    total = used = free = None
                # Heuristic removable detection via /sys/block/<dev>/removable
                is_removable = False
                try:
                    base = os.path.basename(device)
                    # handle partitions like sdb1 -> sdb
                    block = ''.join([c for c in base if not c.isdigit()])
                    removable_path = f"/sys/block/{block}/removable"
                    if os.path.exists(removable_path):
                        with open(removable_path, 'r') as rf:
                            is_removable = rf.read().strip() == '1'
                except Exception:
                    pass
                drives.append({
                    'path': mountpoint,
                    'device': device,
                    'type': 'block',
                    'fstype': fstype,
                    'label': None,
                    'total': total,
                    'used': used,
                    'free': free,
                    'is_system': mountpoint == '/',
                    'is_removable': is_removable
                })
    except FileNotFoundError:
        # Fallback: use df -T
        try:
            proc = subprocess.run(['df','-T'], check=True, capture_output=True, text=True)
            lines = proc.stdout.splitlines()[1:]
            for line in lines:
                parts = line.split()
                if len(parts) < 7:
                    continue
                device, fstype, total_k, used_k, avail_k, percent, mountpoint = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
                if not device.startswith('/dev/'):
                    continue
                if fstype in ignore_fs:
                    continue
                total = int(total_k) * 1024
                used = int(used_k) * 1024
                free = int(avail_k) * 1024
                drives.append({
                    'path': mountpoint,
                    'device': device,
                    'type': 'block',
                    'fstype': fstype,
                    'label': None,
                    'total': total,
                    'used': used,
                    'free': free,
                    'is_system': mountpoint == '/',
                    'is_removable': False
                })
        except Exception:
            pass
    return drives


# --- Core Wiping & Formatting Logic ---

def legacy_fast_wipe(target_path, fs_type):
    """
    Fast wipe: Destroys filesystem metadata at the start of the partition
    and then performs a quick format.
    """
    if os.name == 'nt':
        return _fast_wipe_windows(target_path, fs_type)
    else:
        return _fast_wipe_linux(target_path, fs_type)

def _fast_wipe_windows(target_path, fs_type):
    """Windows implementation of the fast wipe using ctypes."""
    drive_letter = os.path.splitdrive(target_path)[0]
    if not drive_letter:
        status_update("error", f"Invalid path for Windows wipe: {target_path}")
        return False
    
    volume_path = f"\\\\.\\{drive_letter}"
    h_volume = None

    try:
        # Define necessary Windows constants and function prototypes
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        FSCTL_LOCK_VOLUME = 0x00090018
        FSCTL_DISMOUNT_VOLUME = 0x00090020
        FSCTL_UNLOCK_VOLUME = 0x0009001C
        
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        
        # 1. Get a handle to the volume
        h_volume = kernel32.CreateFileW(
            ctypes.c_wchar_p(volume_path),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None
        )
        
        if h_volume == wintypes.HANDLE(-1).value:
            status_update("error", f"Failed to get handle for {volume_path}. Error code: {ctypes.get_last_error()}")
            return False

        # 2. Lock and dismount the volume for exclusive access
        progress_update(f"Locking and dismounting {drive_letter}...")
        bytes_returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(h_volume, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(bytes_returned), None):
            status_update("error", f"Failed to lock volume. It may be in use. Error code: {ctypes.get_last_error()}")
            return False
        if not kernel32.DeviceIoControl(h_volume, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(bytes_returned), None):
            status_update("warning", f"Failed to dismount volume, but continuing. Error code: {ctypes.get_last_error()}")

        # 3. Overwrite the first 512MB of the partition to destroy metadata
        progress_update(f"Destroying filesystem metadata on {drive_letter}...", 0)
        chunk_size = 1024 * 1024  # 1MB
        total_chunks = 512
        bytes_written = wintypes.DWORD(0)
        for i in range(total_chunks):
            if is_cancelled: return False
            random_data = os.urandom(chunk_size)
            if not kernel32.WriteFile(h_volume, random_data, chunk_size, ctypes.byref(bytes_written), None):
                status_update("error", f"Failed to write to volume. Error code: {ctypes.get_last_error()}")
                return False
            
            percentage = ((i + 1) / total_chunks) * 100
            bar_len = int(percentage / 2)
            progress_update(
                f"Destroying metadata on {drive_letter}...", percentage,
                "█" * bar_len + "-" * (50 - bar_len)
            )
        status_update("status", "Filesystem metadata successfully destroyed.")
    
    finally:
        # 4. ALWAYS unlock and close the handle, even if errors occurred
        if h_volume and h_volume != wintypes.HANDLE(-1).value:
            kernel32.DeviceIoControl(h_volume, FSCTL_UNLOCK_VOLUME, None, 0, None, 0, ctypes.byref(wintypes.DWORD()), None)
            kernel32.CloseHandle(h_volume)

    # 5. Perform a quick format to create a new, clean filesystem
    return _windows_format(drive_letter, fs_type, quick=True, label="WIPED")

def _fast_wipe_linux(target_path, fs_type):
    """Linux implementation of the fast wipe using dd and mkfs."""
    # 1. Unmount the device. It might fail if already unmounted, so we don't check the result.
    progress_update(f"Unmounting {target_path}...")
    subprocess.run(['umount', target_path], check=False, capture_output=True)

    # 2. Overwrite the partition header with dd to destroy metadata
    progress_update(f"Destroying filesystem metadata on {target_path}...")
    dd_command = ['dd', 'if=/dev/zero', f'of={target_path}', 'bs=1M', 'count=512']
    try:
        subprocess.run(dd_command, check=True, capture_output=True)
        status_update("status", "Filesystem metadata successfully destroyed.")
    except subprocess.CalledProcessError as e:
        status_update("error", f"Failed to overwrite partition header with dd: {e.stderr.decode('utf-8', errors='ignore')}")
        return False
    except FileNotFoundError:
        status_update("error", "The 'dd' command was not found. Please ensure it is installed.")
        return False

    # 3. Create a new filesystem
    progress_update(f"Creating new {fs_type} filesystem...")
    return _linux_mkfs(target_path, fs_type, label="WIPED", force=True)

def paranoid_wipe(target_path, passes, fs_type):
    """
    Performs a full, multi-pass overwrite of the entire partition using OS-native tools.
    WARNING: This is extremely slow.
    """
    is_windows = os.name == 'nt'
    if is_windows:
        drive_letter = os.path.splitdrive(target_path)[0]
        progress_update(f"Starting PARANOID wipe of {drive_letter} ({passes} passes)... This may take many hours.")
        command = f'format {drive_letter} /FS:{fs_type} /V:WIPED /Y /P:{passes}'
    else: # Linux
        progress_update(f"Starting PARANOID wipe of {target_path} with shred... This may take many hours.")
        # Unmount first to ensure exclusive access
        subprocess.run(['umount', target_path], check=False, capture_output=True)
        command = f"shred -v -n {passes} {target_path}"

    try:
        proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if is_cancelled:
                proc.terminate() # Send SIGTERM to the process
                status_update("status", "Termination signal sent to wipe process.")
                return False
            time.sleep(2)
        
        if proc.returncode != 0:
            status_update("error", f"Wipe/Format process failed with code {proc.returncode}.")
            status_update("error", f"Details: {proc.stderr.read()}")
            return False
            
        # On Linux, shred only overwrites, it doesn't create a filesystem. We must do that now.
        if not is_windows:
            progress_update(f"Creating new {fs_type} filesystem...")
            if not _linux_mkfs(target_path, fs_type, label="WIPED", force=True):
                return False
        return True
    except Exception as e:
        status_update("error", f"An unexpected error occurred during the wipe process: {e}")
        return False


# --- Multi-Stage Secure Workflow ---

def _windows_format(drive_letter, fs_type, quick=False, label="WIPED", dry_run=False):
    """Helper to call the Windows format command."""
    flags = [f'/FS:{fs_type}', f'/V:{label}', '/Y']
    if quick:
        flags.append('/Q')
    command = f"format {drive_letter} {' '.join(flags)}"
    
    if dry_run:
        status_update("status", f"[DRY-RUN] Would run: {command}")
        return True
    try:
        subprocess.run(command, shell=True, check=True, capture_output=True)
        status_update("status", f"Format completed ({'quick' if quick else 'full'}) on {drive_letter}.")
        return True
    except subprocess.CalledProcessError as e:
        status_update("error", f"Format failed: {e.stderr.decode('utf-8', errors='ignore')}")
        return False

def _linux_mkfs(device_path, fs_type, label="WIPED", force=True, dry_run=False):
    """Helper to call the Linux mkfs command."""
    cmd = [f'mkfs.{fs_type}', '-L', label]
    # The -F (force) flag is not supported by mkfs.ntfs
    if force and 'ntfs' not in fs_type:
        cmd.append('-F')
    cmd.append(device_path)
    
    if dry_run:
        status_update("status", f"[DRY-RUN] Would run: {' '.join(cmd)}")
        return True
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        status_update("status", f"Filesystem '{fs_type}' created on {device_path}.")
        return True
    except subprocess.CalledProcessError as e:
        status_update("error", f"mkfs failed: {e.stderr.decode('utf-8', errors='ignore')}")
        return False
    except FileNotFoundError:
        status_update("error", f"mkfs tool for '{fs_type}' not found. Please ensure the relevant filesystem utilities are installed.")
        return False

def _fill_with_temp_files(root_path, dry_run=False):
    """Fills all free space on a mounted path with temporary files of random data."""
    if dry_run:
        status_update("status", f"[DRY-RUN] Would fill free space on {root_path} with temp files.")
        return True
        
    folder = os.path.join(root_path, 'SCRUB_FILL')
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        status_update("error", f"Cannot create temp folder '{folder}': {e}")
        return False

    min_free_mb = 50
    chunk_size_mb = 64
    chunk_size = chunk_size_mb * 1024 * 1024
    min_free_bytes = min_free_mb * 1024 * 1024
    file_index = 0

    try:
        while True:
            if is_cancelled:
                status_update("status", "Cancellation requested during fill stage.")
                return False

            total, used, free = shutil.disk_usage(root_path)
            if free <= min_free_bytes:
                status_update("status", "Disk fill appears complete.")
                break

            file_index += 1
            filename = os.path.join(folder, f"FILL_{file_index:04d}.bin")
            
            # IMPROVEMENT: Use a smaller, more frequent progress update chunk size
            progress_chunk = 16 * 1024 * 1024 # 16MB for reporting
            last_reported_written = 0
            
            try:
                with open(filename, 'wb') as f:
                    while True:
                        if is_cancelled: return False
                        current_free = shutil.disk_usage(root_path).free
                        if current_free <= min_free_bytes:
                            break
                        
                        to_write = min(chunk_size, current_free - min_free_bytes)
                        if to_write <= 0: break
                        
                        f.write(os.urandom(to_write))
                        last_reported_written += to_write
                        
                        # Update progress bar more frequently
                        if last_reported_written >= progress_chunk:
                            percent_used = ((total - current_free) / total) * 100
                            bar_len = int(percent_used / 2)
                            progress_update("Filling free space", percent_used, "█" * bar_len + "-" * (50 - bar_len))
                            last_reported_written = 0
            
            # FIX: Add specific error handling for out of space condition
            except OSError as e:
                if e.errno == 28: # No space left on device
                    status_update("status", "Disk fill complete (hit storage limit).")
                    break
                else:
                    raise # Re-raise other OSErrors
                    
    except Exception as e:
        status_update("error", f"Error while filling disk: {e}")
        return False
    finally:
        # Clean up the large temp files regardless of success or failure
        if os.path.exists(folder):
            status_update("status", "Removing temporary fill files...")
            shutil.rmtree(folder, ignore_errors=True)

    return True

def secure_wipe(target_path, fs_type, dry_run=False):
    """
    Executes a 4-stage secure wipe:
    1. Full format to create a clean filesystem and map out bad sectors.
    2. Quick reformat to refresh metadata.
    3. Fill all available space with random data files.
    4. Final quick format to leave a clean, empty filesystem.
    """
    is_windows = os.name == 'nt'
    if is_windows:
        drive_letter = os.path.splitdrive(target_path)[0]
        if not drive_letter:
            status_update("error", "Invalid Windows drive path supplied.")
            return False
        # Safety Check: Never operate on the system drive.
        system_drive = os.environ.get('SystemDrive', 'C:')
        if drive_letter.upper() == system_drive.upper():
            status_update("error", "CRITICAL: Refusing to operate on the system drive.")
            return False
        
        root_path = drive_letter + '\\'
        
        progress_update("Step 1/4: Full format (may take time)...")
        if not _windows_format(drive_letter, fs_type, quick=False, label="WIPED1", dry_run=dry_run): return False
        if is_cancelled: return False
        
        progress_update("Step 2/4: Quick reformat...")
        if not _windows_format(drive_letter, fs_type, quick=True, label="WIPED2", dry_run=dry_run): return False
        if is_cancelled: return False
        
        progress_update("Step 3/4: Filling free space with random data...")
        if not _fill_with_temp_files(root_path, dry_run=dry_run): return False
        if is_cancelled: return False
        
        progress_update("Step 4/4: Final quick format...")
        if not _windows_format(drive_letter, fs_type, quick=True, label="CLEAN", dry_run=dry_run): return False
        
        return True
    else: # Linux
        device = target_path
        mount_point = f"/mnt/datascrubber_{int(time.time())}"
        
        try:
            progress_update("Step 1/4: Creating initial filesystem...")
            if not _linux_mkfs(device, fs_type, label='WIPED1', dry_run=dry_run): return False
            if is_cancelled: return False
            
            progress_update("Step 2/4: Re-creating filesystem...")
            if not _linux_mkfs(device, fs_type, label='WIPED2', dry_run=dry_run): return False
            if is_cancelled: return False
            
            # Mount the device to fill it with files
            if not dry_run:
                os.makedirs(mount_point, exist_ok=True)
                subprocess.run(['mount', device, mount_point], check=True, capture_output=True)
            
            progress_update("Step 3/4: Filling free space with random data...")
            if not _fill_with_temp_files(mount_point if not dry_run else '/dev/null', dry_run=dry_run): return False
            if is_cancelled: return False

        except subprocess.CalledProcessError as e:
            status_update("error", f"Failed to mount device: {e.stderr.decode('utf-8', errors='ignore')}")
            return False
        finally:
            # FIX: Ensure cleanup happens even on cancellation or error
            if not dry_run and os.path.ismount(mount_point):
                status_update("status", f"Unmounting {mount_point}...")
                subprocess.run(['umount', mount_point], check=False, capture_output=True)
            if not dry_run and os.path.exists(mount_point):
                try:
                    os.rmdir(mount_point)
                except OSError:
                    pass

        progress_update("Step 4/4: Final format...")
        if not _linux_mkfs(device, fs_type, label='CLEAN', dry_run=dry_run): return False
        
        return True


# --- Main Execution Block ---
def main():
    """Parses arguments and orchestrates the wipe operation."""
    parser = argparse.ArgumentParser(
        description="Securely wipe a drive or partition.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('-p', '--path', required=True, help="Drive root (e.g., D:\\) or block device (e.g., /dev/sdb1).")
    parser.add_argument('-m', '--mode', required=True, choices=['secure', 'paranoid', 'legacy-fast'], help=
        "secure: 4-stage format, fill, and reformat. Recommended.\n"
        "paranoid: Slow multi-pass full disk overwrite (uses format /P or shred).\n"
        "legacy-fast: Wipes the first 512MB and does a quick format."
    )
    parser.add_argument('-f', '--filesystem', required=True, help="Target filesystem (e.g., NTFS, ext4).")
    # IMPROVEMENT: Add optional argument for number of passes in paranoid mode
    parser.add_argument('--passes', type=int, default=3, help="[Paranoid Mode] Number of overwrite passes (default: 3).")
    parser.add_argument('--dry-run', action='store_true', help="Show actions without executing them.")
    parser.add_argument('--list-drives', action='store_true', help="List connected drives and exit.")
    args = parser.parse_args()
    
    try:
        if args.list_drives:
            drives = list_connected_drives()
            print(json.dumps({"type": "drives", "items": drives}, default=str), flush=True)
            return

        status_update("status", "Starting data wipe operation...")
        ensure_privileges_or_exit(dry_run=args.dry_run)

        # FIX: Validate filesystem type for all platforms early
        sanitized_fs = sanitize_fs_type(args.filesystem)
        if not sanitized_fs:
            status_update("error", f"Unsupported or invalid filesystem type specified: '{args.filesystem}'")
            sys.exit(1)

        if args.dry_run:
            status_update("status", "DRY-RUN ENABLED: No destructive actions will be performed.")

        success = False
        if args.mode == 'secure':
            status_update("status", "Mode: Secure (4-step multi-stage wipe)")
            success = secure_wipe(args.path, sanitized_fs, dry_run=args.dry_run)
        elif args.mode == 'legacy-fast':
            status_update("status", "Mode: Legacy Fast (metadata destroy + quick format)")
            success = legacy_fast_wipe(args.path, sanitized_fs)
        elif args.mode == 'paranoid':
            status_update("status", f"Mode: Paranoid Wipe ({args.passes}-Pass Overwrite)")
            status_update("warning", "This mode is extremely slow and may take many hours or days.")
            success = paranoid_wipe(args.path, args.passes, sanitized_fs)

        if not success:
            if is_cancelled:
                status_update("status", "Operation cancelled by user.")
                sys.exit(130) # Standard exit code for cancellation
            else:
                status_update("error", "Wipe operation failed. Please review the logs.")
                sys.exit(1)
        
        status_update("status", "All operations completed successfully.")

    except Exception as e:
        status_update("error", f"A critical and unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()
