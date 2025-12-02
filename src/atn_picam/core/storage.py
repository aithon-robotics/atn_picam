import os

# Storage management constants
MIN_FREE_SPACE_GB = 10
MIN_FREE_SPACE_BYTES = MIN_FREE_SPACE_GB * 1024 * 1024 * 1024

def check_and_cleanup_storage(recordings_dir):
    """
    Check available storage space and delete oldest recordings if below threshold.
    Maintains at least 10GB of free space.
    """
    try:
        # Get disk usage statistics
        stat = os.statvfs(recordings_dir)
        free_space_bytes = stat.f_bavail * stat.f_frsize
        free_space_gb = free_space_bytes / (1024 * 1024 * 1024)
        
        if free_space_bytes >= MIN_FREE_SPACE_BYTES:
            print(f"[Storage] Available space: {free_space_gb:.2f} GB (OK)")
            return
        
        print(f"\n⚠️  WARNING: Low storage space detected!")
        print(f"[Storage] Available: {free_space_gb:.2f} GB (threshold: {MIN_FREE_SPACE_GB} GB)")
        print(f"[Storage] Starting cleanup of oldest recordings...")
        
        # Get all recording files sorted by modification time (oldest first)
        recordings = []
        if os.path.exists(recordings_dir):
            for filename in os.listdir(recordings_dir):
                filepath = os.path.join(recordings_dir, filename)
                if os.path.isfile(filepath) and (filename.endswith('.mp4') or filename.endswith('.h264')):
                    recordings.append((filepath, os.path.getmtime(filepath), os.path.getsize(filepath)))
        
        recordings.sort(key=lambda x: x[1])  # Sort by modification time
        
        # Delete oldest files until we have enough space
        deleted_count = 0
        deleted_size_mb = 0
        
        for filepath, mtime, size in recordings:
            if free_space_bytes >= MIN_FREE_SPACE_BYTES:
                break
            
            try:
                os.remove(filepath)
                deleted_count += 1
                deleted_size_mb += size / (1024 * 1024)
                free_space_bytes += size
                print(f"[Storage] Deleted: {os.path.basename(filepath)} ({size/(1024*1024):.1f} MB)")
            except OSError as e:
                print(f"[Storage] Failed to delete {filepath}: {e}")
        
        free_space_gb = free_space_bytes / (1024 * 1024 * 1024)
        print(f"[Storage] Cleanup complete: Deleted {deleted_count} file(s) ({deleted_size_mb:.1f} MB)")
        print(f"[Storage] Available space now: {free_space_gb:.2f} GB\n")
        
    except Exception as e:
        print(f"[Storage] Error checking/cleaning storage: {e}")
