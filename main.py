#!/usr/bin/env python3

"""
YouTube Playlist Downloader GUI Application
Author: Peter Jan Simons
Description: Downloads YouTube playlists/videos as MP4 or MP3 with progress tracking,
             concurrent downloads support, and queue management.
"""

# Standard library imports
import os
import sys
import time
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-party imports
import yt_dlp
import FreeSimpleGUI as sg
from yt_dlp import DownloadCancelled

# Fix PATH for frozen Windows executables to include ffmpeg
if sys.platform == 'win32' and getattr(sys, 'frozen', False):
    os.environ['PATH'] = os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH']

def get_ffmpeg_path():
    """
    Locate and validate ffmpeg executable path
    Returns:
        str: Full path to ffmpeg.exe
    Raises:
        FileNotFoundError: If ffmpeg isn't found in expected location
    """
    # Determine base directory based on execution context
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Construct expected ffmpeg path
    ffmpeg_path = os.path.join(base_dir, 'bin', 'ffmpeg.exe')
    
    # Validate path exists
    if not os.path.exists(ffmpeg_path):
        raise FileNotFoundError(f"FFmpeg not found at: {ffmpeg_path}")
    
    return ffmpeg_path

# Configuration file path
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'petice_config.txt')

class ConsoleLogger:
    """
    Custom logger class for yt-dlp to capture different message types, only for dev purposes.
    """
    def debug(self, msg):
        print("[DEBUG]", msg)
    def warning(self, msg):
        print("[WARNING]", msg)
    def error(self, msg):
        print("[ERROR]", msg)

# Common options for yt-dlp downloader
COMMON_OPTS = {
    'quiet': True,          # Suppress console output
    'no_warnings': True,    # Ignore YouTube warnings
    'logger': False,        # Disable default logging
}

def sanitize_filename(name):
    """
    Clean filenames to be filesystem-safe
    Args:
        name (str): Original filename
    Returns:
        str: Sanitized filename
    """
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    name = name.strip().rstrip('.')  # Remove trailing dots
    return name

def get_playlist_info(url):
    """
    Fetch playlist/video metadata from YouTube
    Args:
        url (str): YouTube URL (video or playlist)
    Returns:
        dict: Contains title, URL, entry count, and video entries
    """
    for _ in range(3):  # Retry up to 3 times
        try:
            opts = {
                'extract_flat': True,     # Get playlist structure without full video data
                'skip_download': True,    # No media download
                'quiet': True,
                'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get('entries', []) if info else []
                return {
                    'title': sanitize_filename(info.get('title', url)),
                    'url': url,
                    'total': len(entries) or 1,  # Handle single videos
                    'entries': entries if entries else [info]
                }
        except Exception as e:
            time.sleep(1)
    return {'title': sanitize_filename(url), 'url': url, 'total': 0, 'entries': []}

def download_process(queue, playlist_info, format_choice, cancel_event, base_folder):
    """
    Sequential download process for playlist items
    Args:
        queue: Multiprocessing queue for GUI communication
        playlist_info: Playlist metadata dictionary
        format_choice: 'mp4' or 'mp3'
        cancel_event: Multiprocessing event for cancellation
        base_folder: Root download directory
    """
    try:
        queue.put(("-STATUS-", "Starting downloads"))
        entries = playlist_info.get('entries', [])
        total_files = len(entries)
        if total_files == 0:
            queue.put(("-STATUS-", "Empty content"))
            return

        # Create unique playlist folder
        playlist_folder = os.path.join(base_folder, playlist_info['title'])
        suffix = 1
        original_folder = playlist_folder
        while os.path.exists(playlist_folder):
            playlist_folder = f"{original_folder}_{suffix}"
            suffix += 1
        os.makedirs(playlist_folder)

        downloaded_count = 0
        processed_count = 0

        # Process each video in playlist
        for video in entries:
            if cancel_event.is_set():
                raise DownloadCancelled("Download cancelled")
            
            # Construct video metadata
            video_id = video.get("id", "")
            video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else video.get("url", "")
            video_title = video.get("title", "Unknown video")

            queue.put(("-STATUS-", f"Downloading:  '{video_title}'"))

            # Configure download options
            ydl_opts = {
                'ffmpeg_location': get_ffmpeg_path(),
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]' if format_choice == "mp4" else 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '256',
                }] if format_choice == "mp3" else None,
                'outtmpl': os.path.join(playlist_folder, '%(title)s.%(ext)s'),
                'merge_output_format': 'mp4' if format_choice == "mp4" else None
            }
            ydl_opts.update(COMMON_OPTS)
            if ydl_opts['postprocessors'] is None:
                del ydl_opts['postprocessors']

            # Retry logic (3 attempts)
            success = False
            for attempt in range(1, 4):
                if cancel_event.is_set():
                    raise DownloadCancelled()
                
                if attempt > 1:
                    queue.put(("-STATUS-", f"Retrying download of '{video_title}' ({attempt}/3)"))
                
                last_progress_time = 0

                def progress_hook(d):
                    """Callback function for download progress updates"""
                    nonlocal last_progress_time
                    if cancel_event.is_set():
                        raise DownloadCancelled()
                    status = d.get('status')
                    if status == 'downloading':
                        now = time.monotonic()
                        # Throttle progress updates
                        if now - last_progress_time < 0.1:
                            return
                        last_progress_time = now
                        
                        # Calculate progress percentage
                        percent = d.get('percent')
                        if percent is not None:
                            file_progress = int(percent)
                        else:
                            downloaded_bytes = d.get('downloaded_bytes', 0)
                            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 1)
                            file_progress = int((downloaded_bytes / total_bytes) * 100) if total_bytes else 0
                        
                        queue.put(("-FILE-PROGRESS-", file_progress))
                    elif status == 'finished':
                        queue.put(("-FILE-PROGRESS-", 100))

                ydl_opts['progress_hooks'] = [progress_hook]
                
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([video_url])
                    success = True
                    if attempt > 1:
                        queue.put(("-STATUS-", f"Successfully downloaded '{video_title}'"))
                        time.sleep(3)
                        queue.put(("-STATUS-", "Processing downloads"))
                    break
                except Exception as e:
                    if attempt == 3:
                        queue.put(("-STATUS-", f"Download failed '{video_title}'"))
                        time.sleep(3)
                        queue.put(("-STATUS-", "Processing downloads"))
                    continue

            # Update progress counters
            processed_count += 1
            queue.put(("-PROCESSED-PROGRESS-", 1))
            
            if success:
                downloaded_count += 1
                queue.put(("-DOWNLOADED-PROGRESS-", 1))

        queue.put(("-STATUS-", "Completed!"))
        queue.put(("-QUEUE-COMPLETE-", playlist_info))
    except Exception as e:
        queue.put(("-STATUS-", f"Error: {str(e)}"))
    finally:
        queue.put(("-THREAD-END-", None))

def download_process_concurrent(queue, playlist_info, format_choice, cancel_event, max_simultaneous, base_folder):
    """
    Concurrent download process using ThreadPoolExecutor
    Args:
        max_simultaneous: Maximum parallel downloads
        Other args same as download_process
    """
    try:
        queue.put(("-STATUS-", "Starting downloads"))
        entries = playlist_info.get('entries', [])
        total_files = len(entries)
        if total_files == 0:
            queue.put(("-STATUS-", "Empty content"))
            return

        # Create unique playlist folder (same as sequential)
        playlist_folder = os.path.join(base_folder, playlist_info['title'])
        suffix = 1
        original_folder = playlist_folder
        while os.path.exists(playlist_folder):
            playlist_folder = f"{original_folder}_{suffix}"
            suffix += 1
        os.makedirs(playlist_folder)

        # Configure base download options
        ydl_opts_template = {
            'ffmpeg_location': get_ffmpeg_path(),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]' if format_choice == "mp4" else 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '256',
            }] if format_choice == "mp3" else None,
            'outtmpl': os.path.join(playlist_folder, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4' if format_choice == "mp4" else None
        }
        ydl_opts_template.update(COMMON_OPTS)
        if ydl_opts_template['postprocessors'] is None:
            del ydl_opts_template['postprocessors']

        downloaded_count = 0
        processed_count = 0
        lock = threading.Lock()  # For thread-safe counter updates

        def download_video(video):
            """Thread worker function for individual video download"""
            nonlocal downloaded_count, processed_count
            video_title = video.get("title", "Unknown")
            video_id = video.get("id", "")
            video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else video.get("url", "")
            
            success = False
            for attempt in range(1, 4):
                try:
                    opts = ydl_opts_template.copy()
                    last_progress_time = 0

                    def progress_hook(d):
                        """Progress callback (same as sequential version)"""
                        nonlocal last_progress_time
                        if cancel_event.is_set():
                            raise DownloadCancelled()
                        status = d.get('status')
                        if status == 'downloading':
                            now = time.monotonic()
                            if now - last_progress_time < 0.1:
                                return
                            last_progress_time = now
                            
                            percent = d.get('percent')
                            if percent is not None:
                                file_progress = int(percent)
                            else:
                                downloaded_bytes = d.get('downloaded_bytes', 0)
                                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 1)
                                file_progress = int((downloaded_bytes / total_bytes) * 100) if total_bytes else 0
                            
                            queue.put(("-FILE-PROGRESS-", file_progress))
                        elif status == 'finished':
                            queue.put(("-FILE-PROGRESS-", 100))

                    opts['progress_hooks'] = [progress_hook]
                    
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([video_url])
                    success = True
                    if attempt > 1:
                        queue.put(("-STATUS-", f"Successfully downloaded '{video_title}'"))
                        time.sleep(3)
                        queue.put(("-STATUS-", "Processing downloads"))
                    break
                except Exception as e:
                    queue.put(("-STATUS-", f"Retrying download of '{video_title}' ({attempt}/3)"))
                    if attempt == 3:
                        queue.put(("-STATUS-", f"Failed to download '{video_title}'"))
                        time.sleep(3)
                        queue.put(("-STATUS-", "Processing downloads"))
                    continue

            # Update counters with thread lock
            with lock:
                processed_count += 1
                queue.put(("-PROCESSED-PROGRESS-", 1))
                if success:
                    downloaded_count += 1
                    queue.put(("-DOWNLOADED-PROGRESS-", 1))
            
            return success

        # Create thread pool and execute downloads
        with ThreadPoolExecutor(max_workers=max_simultaneous) as executor:
            futures = [executor.submit(download_video, video) for video in entries]
            for future in as_completed(futures):
                future.result()

        queue.put(("-STATUS-", "Complete!"))
        queue.put(("-QUEUE-COMPLETE-", playlist_info))
    except Exception as e:
        queue.put(("-STATUS-", f"Error: {str(e)}"))
    finally:
        queue.put(("-THREAD-END-", None))

# Base64 encoded folder icon for GUI
folder_icon = b"iVBORw0KGgoAAAANSUhEUgAAABQAAAAUCAYAAACNiR0NAAAACXBIWXMAAAsSAAALEgHS3X78AAAAAXNSR0IArs4c6QAAAWhJREFUOE+t1E1LVVEUxvGf9DJKIalvEEFfQHAq9g0aR6PESY0jE4UaCUoQNA2cSlAEok60QYOKhAZOA8VBgkhvlGYv+7ncC5fLrXvuwQ0HDues9d9rr+fZa8AJrwGcxnmcamP/xicc9rtfgDcxijNtycd4jyVs9wMNcA8XuyQdYKFU/xCfq0ID/POf4B0s4sM/YrLRBj62/vcC9iosvX6KG/iW4G7AH0hg1bXe1GG3E/gFy3iLn1VpSFtW8LUT+Kx8mMFlXGlW3we3Idzz1pGj9J2mH29juAYwVlsL8Bde4FEx+X2M9FNWR+ybAPdLz+5iCFMYrAmMkLMBvixV3ivKTpf3sZqwpG3hWoATzWPnVpyrCUzbIuh8gJfwGOM1YUnbLLdlEq8DvI4nNVRt7f+9DJYHmMNRgKu4WrO6CPEKsdq7lrFvdRlfVfmZmbFcnsbtSoVniwcvdAzYqsAM4NguojRWgCe6/gKcAEphpwhP9gAAAABJRU5ErkJggg=="

def main():
    """Main function to create and manage the GUI"""
    sg.theme("Black")

    # Initialize configuration file
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        default_base_folder = os.path.join(os.path.expanduser("~"), "Desktop", "Petice Downloads")
        default_create_subfolder = True
        with open(CONFIG_PATH, 'w') as f:
            f.write(f"{default_base_folder}\n{default_create_subfolder}\n")
    
    # Load configuration
    create_subfolder = True
    base_folder = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r') as f:
                lines = f.read().splitlines()
                if len(lines) >= 1:
                    base_folder = lines[0].strip()
                if len(lines) >= 2:
                    create_subfolder = lines[1].strip().lower() == 'true'
        except Exception as e:
            pass

    # GUI layout definition
    layout = [
        [sg.Text("Playlist URL:", size=(15,1)), 
         sg.Input(key="-URL-", disabled_readonly_background_color="#878585", tooltip="Paste YouTube video/playlist URL",expand_x=True),
         sg.Button("＋", key="-ADD-QUEUE-", tooltip="Add to download queue", size=(2,1))],
        [sg.Text("Format:", size=(28,1)),
         sg.Radio("MP4 (Video)", "FORMAT", key="-MP4-", default=True),
         sg.Radio("MP3 (Audio)", "FORMAT", key="-MP3-")],
        [sg.Text("", size=(23,1)), sg.Checkbox("Concurrent Downloads", key="-CONCURRENT-", tooltip="Enable concurrent downloads (default: 10)", enable_events=True),
         sg.Text("Max:", tooltip="Adjust based on system resources and internet speed", key="-MAX-TEXT-"), 
         sg.Input("10", key="-CONCURRENT-COUNT-", size=(5,1), tooltip="Adjust based on system resources and internet speed", disabled_readonly_background_color="#878585", enable_events=True)],
        [sg.Text("Status:", size=(15,1)), sg.Text("Waiting for start", key="-STATUS-TXT-", expand_x=True, text_color="white")],
        [sg.Text("Current Progress:", size=(15,1), key="-CURRENT-PROGRESS-LABEL-"), 
         sg.ProgressBar(100, orientation="h", size=(38,20), key="-FILE-PROGRESS-BAR-", expand_x=False),
         sg.Text("0%", key="-FILE-PROGRESS-TXT-")],
        [sg.Text("Overall Progress:", size=(15,1)), 
         sg.ProgressBar(100, orientation="h", size=(38,20), key="-OVERALL-PROGRESS-BAR-", expand_x=False),
         sg.Text("0%", key="-OVERALL-PROGRESS-TXT-")],
        [sg.Text(" " * 22 + "Downloaded Files: 0 / 0", size=(15,1), key="-SONG-COUNT-TXT-", expand_x=True, justification="center")],
        [sg.Text("Download Queue:", size=(15,1)), 
         sg.Text("Queue is empty", key="-QUEUE-TEXT-", expand_x=True, background_color="black", pad=(5,2), tooltip="Pending playlists list")],
        [sg.Text("")],
        [sg.Button("Download", size=(8, 1), key="-DOWNLOAD-"), 
         sg.Button("Cancel", key="-CANCEL-", size=(8, 1), visible=False),
         sg.Text(" " * 15),
         sg.Button("Clear Queue", size=(10, 0), key="-CLEAR-QUEUE-"),
         sg.Button("View Queue", size=(10, 0), key="-VIEW-QUEUE-"),
         sg.Button("Remove Last", size=(10, 0), key="-REMOVE-LAST-"),
         sg.Text(" " * 2), sg.Button("", image_data=folder_icon, image_size=(24,24), key="-CHOOSE-DIR-"),
         sg.Push(), 
         sg.Button("Exit", size=(8, 1), key="-EXIT-")],
    ]

    # Create main window
    window = sg.Window("Petice Downloader", layout, resizable=False, finalize=True, size=(630, 295), icon="icon.ico")
    
    # Application state variables
    download_queue = []
    initial_total = 0
    downloaded_files = 0
    processed_files = 0
    session_active = False
    current_download = None
    concurrent_mode = False
    msg_queue = multiprocessing.Queue()
    cancel_event = multiprocessing.Event()
    download_proc = None
    current_total = 0
    processed_current = 0

    def update_queue_display():
        """Update the queue display text with truncated titles"""
        titles = [item['title'][:35] + '...' if len(item['title']) > 38 else item['title'] for item in reversed(download_queue)]
        display_text = " | ".join(titles) if titles else "Queue is empty"
        window["-QUEUE-TEXT-"].update(display_text)

    def update_counters():
        """Update progress bars and counters"""
        nonlocal initial_total
        if not session_active:
            initial_total = sum(item.get('total', 0) for item in download_queue)
        
        window["-SONG-COUNT-TXT-"].update(" " * 22 + f"Downloaded Files: {downloaded_files} / {initial_total}")
        
        if initial_total > 0:
            progress = int((processed_files / initial_total) * 100)
            window["-OVERALL-PROGRESS-BAR-"].update_bar(progress)
            window["-OVERALL-PROGRESS-TXT-"].update(f"{progress}%")
        
        if session_active and concurrent_mode:
            percent = int((processed_current / current_total) * 100) if current_total > 0 else 0
            window["-FILE-PROGRESS-BAR-"].update_bar(percent)
            window["-FILE-PROGRESS-TXT-"].update(f"{percent}%")

    def add_to_queue(url):
        """Add a new URL to the download queue"""
        if not url.startswith(('http://', 'https://')):
            window["-STATUS-TXT-"].update("Invalid URL", text_color="red")
            return
            
        temp_item = {'url': url, 'title': 'Loading...', 'total': 0, 'entries': []}
        download_queue.append(temp_item)
        update_queue_display()
        window["-STATUS-TXT-"].update("Fetching info...", text_color="#5DE2E7")
        
        def fetch_title():
            """Background thread for fetching playlist metadata"""
            info = get_playlist_info(url)
            temp_item.update(info)
            
            if info['total'] == 0:
                download_queue.remove(temp_item)
                window["-STATUS-TXT-"].update("Invalid URL", text_color="red")
                update_queue_display()
                return
                
            update_queue_display()
            update_counters()
            window["-STATUS-TXT-"].update("Waiting to start", text_color="white")
        
        threading.Thread(target=fetch_title, daemon=True).start()

    def update_buttons(state):
        """Enable/disable GUI controls during downloads"""
        window["-DOWNLOAD-"].update(disabled=state)
        window["-CLEAR-QUEUE-"].update(disabled=state)
        window["-REMOVE-LAST-"].update(disabled=state)
        window["-URL-"].update(disabled=state)
        window["-CONCURRENT-"].update(disabled=state)
        window["-MP4-"].update(disabled=state)
        window["-MP3-"].update(disabled=state)
        window["-CONCURRENT-COUNT-"].update(disabled=state)

        text_color = '#878585' if state else 'white'
        window["-MAX-TEXT-"].update(text_color=text_color)

    # Main event loop
    while True:
        event, values = window.read(timeout=100)

        # Process messages from download threads
        while not msg_queue.empty():
            msg_type, msg_value = msg_queue.get()
            if msg_type == "-DOWNLOADED-PROGRESS-":
                downloaded_files += 1
            elif msg_type == "-PROCESSED-PROGRESS-":
                processed_files += 1
                if concurrent_mode:
                    processed_current += 1
            else:
                window.write_event_value(msg_type, msg_value)
            update_counters()

        # Handle window events
        if event in (sg.WIN_CLOSED, "-EXIT-"):
            break

        elif event == "-ADD-QUEUE-":
            url = values["-URL-"].strip()
            if url:
                add_to_queue(url)
                window["-URL-"].update("")
                downloaded_files = 0
                processed_files = 0
                initial_total = sum(item.get('total', 0) for item in download_queue)
                window["-FILE-PROGRESS-BAR-"].update_bar(0)
                window["-FILE-PROGRESS-TXT-"].update("0%")
                window["-OVERALL-PROGRESS-BAR-"].update_bar(0)
                window["-OVERALL-PROGRESS-TXT-"].update("0%")
                window["-SONG-COUNT-TXT-"].update(" " * 22 + f"Downloaded Files: 0 / {initial_total}")

        elif event == "-CLEAR-QUEUE-":
            download_queue = []
            downloaded_files = 0
            processed_files = 0
            update_queue_display()
            update_counters()

        elif event == "-VIEW-QUEUE-":
            # Create queue viewing window
            queue_titles = [f"{i+1}. {item['title']}" for i, item in enumerate(reversed(download_queue))]
            bin_icon = b"iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABHNCSVQICAgIfAhkiAAAAAlwSFlzAAAApgAAAKYB3X3/OAAAABl0RVh0U29mdHdhcmUAd3d3Lmlua3NjYXBlLm9yZ5vuPBoAAAGJSURBVEiJrZa7SkNBEIa/lYARCdFC0MZoKkmVPoWVL6CVbyN5EUurVIKVIILvoK2k0yKHI5gEQ8Yis7qZ7OZ6BgYyt3+Y+Te7BxEhpUADeADGgBgda6wxD8MpUFSccx3gHLgBRiZcUv+ziFylMEoB2D5QM/EL4A54SdR3gGvnXNP430WkB+BX0QL6zK5hXe0DLRFhSzs2gXJqzDWkrJh/K7oFKsBZQQ3eFHM+yUVISHIF+CwI90BEvgDsuR8wTdYTkAX2q6q3M80JawZTmKZB1ySfAu3AvlT1dltzwppuiOlPkRe7ol2gF9hjVS89zUli2AYfxq6aBlZ6mpPEWDTBHpM9pyTTnCTGogbVJRrYCVZa0ToTrLSiwieIcbCI5I05yJk+ml7GGtuMA5n8A/NIg1xjG3Hgi2M8ZCYnilEKDRHJnXNDYFtddefcEf9vxUmQXnbOHQL1wDcUkelpIw+9vY++g98/qrHYzD0Uu4sAHo29YyYuJWKx2ugENeCeyVfEsm/wSGuOLd4vPVs2cqbiWVQAAAAASUVORK5CYII="
            layout_queue = [
                [sg.Text("Download Queue", justification="center", font='_ 14', expand_x=True)],
                [sg.HorizontalSeparator()],
                [sg.Listbox(queue_titles, size=(70, 10), background_color="black", no_scrollbar=True, justification="center", font=("Arial", 12),key="-LIST-")],
                [sg.Button("",image_data=bin_icon, key="-DELQ-", image_size=(500,40))]
            ]
            
            window_queue = sg.Window("Full Queue", layout_queue, modal=True, finalize=True, size=(400, 281), icon="icon.ico")

            list_frame = window_queue["-LIST-"].Widget
            list_frame.config(borderwidth=0, highlightthickness=0)
            
            # Queue management loop
            while True:
                event_queue, values_queue = window_queue.read()
                if event_queue in (sg.WIN_CLOSED, "Close"):
                    break
                elif event_queue == "-DELQ-":
                    if values_queue and values_queue["-LIST-"]:
                        selected_title = values_queue["-LIST-"][0]
                        selected_index = queue_titles.index(selected_title)
                        if 0 <= selected_index < len(download_queue):
                            del download_queue[len(download_queue)-1-selected_index]
                            queue_titles = [f"{i+1}. {item['title']}" for i, item in enumerate(reversed(download_queue))]
                            window_queue["-LIST-"].update(queue_titles)
                            update_queue_display()
                            update_counters()
            
            window_queue.close()

        elif event == "-REMOVE-LAST-":
            if download_queue:
                download_queue.pop()
                update_queue_display()
                update_counters()

        elif event == "-CONCURRENT-":
            concurrent_mode = values["-CONCURRENT-"]

        elif event == "-CHOOSE-DIR-":
            # Directory selection dialog
            layout_folder = [
                [sg.Text("Select base directory:")],
                [sg.Input(default_text=base_folder,key="-FOLDER-"), sg.FolderBrowse()],
                [sg.Radio("Create 'Petice Downloads' subfolder", "FOLDER_OPT", default=create_subfolder, key="-CREATE-SUB-")],
                [sg.Radio("Use directory directly", "FOLDER_OPT", default=not create_subfolder, key="-USE-DIR-")],
                [sg.Button("OK"), sg.Button("Cancel")]
            ]
            
            window_folder = sg.Window("Folder Settings", layout_folder, icon="icon.ico")
            event_folder, values_folder = window_folder.read()
            
            if event_folder == "OK" and values_folder["-FOLDER-"]:
                create_subfolder = values_folder["-CREATE-SUB-"]
                selected_folder = values_folder["-FOLDER-"]
                
                # Update base folder path
                if create_subfolder:
                    if os.path.basename(selected_folder) == "Petice Downloads":
                        new_base = selected_folder
                    else:
                        new_base = os.path.join(selected_folder, "Petice Downloads")
                else:
                    new_base = selected_folder
                
                try:
                    os.makedirs(new_base, exist_ok=True)
                    base_folder = new_base
                    
                    # Save new configuration
                    with open(CONFIG_PATH, 'w') as f:
                        f.write(f"{base_folder}\n{str(create_subfolder).lower()}")
                        
                    sg.popup(f"Directory updated:\n{base_folder}\n\nThis location will be remembered.", title="Settings saved", icon="icon.ico")
                except Exception as e:
                    sg.popup_error(f"Error setting up directory:\n{str(e)}")
            
            window_folder.close()

        elif event == "-DOWNLOAD-":
            if download_queue and not current_download and initial_total > 0:
                session_active = True
                current_download = download_queue[-1]
                current_total = current_download.get('total', 0)
                processed_current = 0

                update_buttons(True)

                # Initialize UI elements
                window["-STATUS-TXT-"].update("Starting...", text_color="yellow")
                window["-FILE-PROGRESS-BAR-"].update_bar(0)
                window["-FILE-PROGRESS-TXT-"].update("0%")
                window["-EXIT-"].update(visible=False)
                window["-CANCEL-"].update(visible=True)
                cancel_event.clear()
                
                # Start download process
                format_choice = "mp4" if values["-MP4-"] else "mp3"
                
                if concurrent_mode:
                    max_workers = int(values["-CONCURRENT-COUNT-"]) if values["-CONCURRENT-COUNT-"].isdigit() else 10
                    target = download_process_concurrent
                    args = (msg_queue, current_download, format_choice, cancel_event, max_workers, base_folder)
                else:
                    target = download_process
                    args = (msg_queue, current_download, format_choice, cancel_event, base_folder)
                
                download_proc = multiprocessing.Process(target=target, args=args)
                download_proc.start()

        elif event == "-CANCEL-":
            cancel_event.set()
            if download_proc:
                download_proc.terminate()
            window["-CANCEL-"].update(visible=False)
            window["-EXIT-"].update(visible=True)
            window["-STATUS-TXT-"].update("Cancelled", text_color="red")
            update_buttons(False)
            current_download = None
            session_active = False
            downloaded_files = 0
            processed_files = 0
            update_counters()

        elif event == "-STATUS-":
            # Update status text with color coding
            status_text = values["-STATUS-"]
            color = ("#7BFF47" if "Complete" in status_text 
                    else "red" if any(x in status_text for x in ["Error", "Cancelled"]) 
                    else "white")
            window["-STATUS-TXT-"].update(status_text, text_color=color)

        elif event == "-FILE-PROGRESS-":
            # Update file progress bar
            if concurrent_mode:
                if current_total == 1:
                    window["-FILE-PROGRESS-BAR-"].update_bar(msg_value)
                    window["-FILE-PROGRESS-TXT-"].update(f"{msg_value}%")
                else:
                    percent = int((processed_current / current_total) * 100) if current_total > 0 else 0
                    window["-FILE-PROGRESS-BAR-"].update_bar(percent)
                    window["-FILE-PROGRESS-TXT-"].update(f"{percent}%")
            else:
                window["-FILE-PROGRESS-BAR-"].update_bar(msg_value)
                window["-FILE-PROGRESS-TXT-"].update(f"{msg_value}%")

        elif event == "-QUEUE-COMPLETE-":
            if download_queue and current_download == download_queue[-1]:
                download_queue.pop()
                current_download = None
                update_queue_display()
                
        elif event == "-THREAD-END-":
            # Cleanup after download completion
            window["-CANCEL-"].update(visible=False)
            window["-EXIT-"].update(visible=True)
            update_buttons(False)

            current_download = None
            session_active = False
            
            # Start next download if queue not empty
            if download_queue and not current_download:
                window.write_event_value("-DOWNLOAD-", None)

    # Cleanup before exit
    if download_proc and download_proc.is_alive():
        download_proc.terminate()
    window.close()

if __name__ == '__main__':
    multiprocessing.freeze_support()  # For pyinstaller compatibility
    main()
