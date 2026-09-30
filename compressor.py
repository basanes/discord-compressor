import os
import subprocess
import json

def get_video_duration(file_path):
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = json.loads(result.stdout)
    return float(data['format']['duration'])

def compress_video(input_path, output_path, target_size_mb=20):
    """Compress video to a target file size in MB."""
    duration = get_video_duration(input_path)
    
    # Target size in bits
    target_size_bits = target_size_mb * 1024 * 1024 * 8
    target_bitrate = (target_size_bits / duration) * 0.90
    
    audio_bitrate = 128 * 1000 
    video_bitrate = target_bitrate - audio_bitrate
    
    if video_bitrate < 100000:
        print(f"Warning: {os.path.basename(input_path)} is too long to fit nicely into {target_size_mb}MB without quality loss.")
        video_bitrate = 100000
        
    print(f"Compressing {os.path.basename(input_path)} (Duration: {duration:.1f}s)...")

    # Pass 1
    pass1_cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", str(int(video_bitrate)),
        "-pass", "1", "-an", "-f", "null", "NUL" if os.name == "nt" else "/dev/null"
    ]
    subprocess.run(pass1_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Pass 2
    pass2_cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264", "-b:v", str(int(video_bitrate)),
        "-pass", "2", "-c:a", "aac", "-b:a", "128k",
        output_path
    ]
    subprocess.run(pass2_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Cleanup pass logs
    for ext in [".log", ".log.mbtree"]:
        if os.path.exists(f"ffmpeg2pass-0.log{ext}"):
            os.remove(f"ffmpeg2pass-0.log{ext}")
            
    print(f"Done! Saved to {output_path}\n")

if __name__ == "__main__":
    # Use the folder where the script itself is currently running (Relative Path)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    input_folder = os.path.join(current_dir, "input_videos")
    output_folder = os.path.join(current_dir, "compressed_videos")
    
    os.makedirs(input_folder, exist_ok=True)
    os.makedirs(output_folder, exist_ok=True)
    
    supported_extensions = (".mp4", ".mov", ".avi", ".mkv", ".webm")
    
    files = [f for f in os.listdir(input_folder) if f.lower().endswith(supported_extensions)]
    
    if not files:
        print(f"No videos found! Please put your videos inside: {input_folder}")
    else:
        for filename in files:
            in_file = os.path.join(input_folder, filename)
            out_file = os.path.join(output_folder, f"compressed_{filename}")
            try:
                compress_video(in_file, out_file, target_size_mb=20)
            except Exception as e:
                print(f"Error processing {filename}: {e}")
        print("All batch processing complete!")