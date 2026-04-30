import os
import asyncio
import subprocess
import logging
import time
import math

logger = logging.getLogger(__name__)

# ─── Auto CRF Settings ───────────────────────────────────────────────────────
# Proses lebih cepat = CRF lebih tinggi (file lebih kecil)
# Episode sedikit  → kualitas lebih bagus (CRF rendah)
# Episode banyak   → proses lebih cepat (CRF tinggi, max 27)
CRF_MIN = 20   # Kualitas terbaik (episode ≤5)
CRF_MAX = 27   # Proses tercepat (episode banyak)

def auto_crf(total_episodes: int) -> int:
    """
    Hitung CRF otomatis berdasarkan jumlah episode.
    ≤ 5 ep  → CRF 20 (sangat bagus)
    6–10 ep → CRF 22 (seimbang)
    11–20ep → CRF 24
    > 20 ep → CRF 27 (tercepat, max)
    """
    if total_episodes <= 5:
        crf = 20
    elif total_episodes <= 10:
        crf = 22
    elif total_episodes <= 20:
        crf = 24
    else:
        crf = 27
    crf = min(crf, CRF_MAX)  # Pastikan tidak melebihi max
    logger.info(f"🎛️ Auto CRF: {crf} (untuk {total_episodes} episode)")
    return crf

async def get_video_dimensions(video_path):
    """
    Mengambil lebar dan tinggi video menggunakan ffprobe.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", video_path
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            w_h = stdout.decode().strip().split('x')
            if len(w_h) >= 2:
                return int(w_h[0]), int(w_h[1])
    except Exception as e:
        logger.warning(f"Gagal deteksi resolusi {video_path}: {e}")
    return None, None

def create_progress_bar(percentage):
    blocks = int(percentage / 10)
    bar = "■" * blocks + "□" * (10 - blocks)
    return f"|{bar}| {percentage}%"

async def merge_and_hardsub(video_dir: str, output_path: str, progress_callback=None, title=""):
    """
    Merges all episodes and burns subtitles into each before merging.
    Menggunakan adaptive auto CRF berdasarkan jumlah episode (max CRF 27).
    """
    start_time = time.time()
    try:
        # Get all video files
        videos = [f for f in os.listdir(video_dir) if f.endswith(".mp4") and "ep_" in f]
        videos.sort()
        
        total_videos = len(videos)
        processed_videos = []

        # ── Tentukan CRF otomatis ──────────────────────────────────────────
        crf = auto_crf(total_videos)
        preset = "medium"   # medium = kualitas lebih baik, masih cukup cepat
        # Jika CRF ≥ 25, pakai faster agar proses lebih singkat
        if crf >= 25:
            preset = "veryfast"
        logger.info(f"⚙️ FFmpeg encoding: preset={preset}, crf={crf}")
        # ──────────────────────────────────────────────────────────────────

        for i, video_file in enumerate(videos, 1):
            if progress_callback:
                percentage = int((i / (total_videos + 1)) * 100)
                elapsed = time.time() - start_time
                avg_time_per_ep = elapsed / i if i > 0 else 0
                remaining_eps = total_videos - i + 1
                est_remaining = avg_time_per_ep * remaining_eps
                
                est_min = int(est_remaining // 60)
                est_sec = int(est_remaining % 60)
                
                status_text = (
                    f"🎬 **{title}**\n"
                    f"🔥 **Status: Burning Hardsub...**\n"
                    f"🎞 Episode {i}/{total_videos} | CRF: {crf} | Preset: {preset}\n"
                    f"{create_progress_bar(percentage)}\n"
                    f"⏳ Estimasi Selesai: {est_min}m {est_sec}s"
                )
                await progress_callback(status_text)
                
            ep_str = video_file.replace("ep_", "").replace(".mp4", "")
            sub_file = f"ep_{ep_str}.srt"
            sub_path = os.path.join(video_dir, sub_file)
            input_path = os.path.join(video_dir, video_file)
            temp_output = os.path.join(video_dir, f"hard_{video_file}")
            
            # ── Burn subtitle jika ada ────────────────────────────────────
            if os.path.exists(sub_path):
                sub_path_fixed = sub_path.replace("\\", "/").replace(":", "\\:")
                
                # Cek resolusi video: Jika bukan 9:16, gunakan style khusus
                width, height = await get_video_dimensions(input_path)
                is_9_16 = False
                if width and height:
                    # 9/16 = 0.5625. Beri toleransi.
                    ratio = width / height
                    if 0.5 <= ratio <= 0.6:
                        is_9_16 = True
                
                if not is_9_16:
                    # Nimbus Sans Narrow, putih, size 24, offset 8
                    style = "Fontname=Nimbus Sans Narrow,Fontsize=24,PrimaryColour=&H00FFFFFF,Bold=1,Outline=1,OutlineColour=&H000000,MarginV=8"
                    logger.info(f"📐 Video bukan 9:16 ({width}x{height}) -> Menggunakan style Nimbus Sans")
                else:
                    # Default (biasanya untuk video vertikal 9:16)
                    style = "Fontname=Standard Symbols PS,Fontsize=10,PrimaryColour=&H00FFFFFF,Bold=1,Outline=1,OutlineColour=&H000000,MarginV=90"
                
                command = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-vf", f"subtitles='{sub_path_fixed}':force_style='{style}'",
                    "-c:v", "libx264",
                    "-preset", preset,   # auto preset
                    "-crf", str(crf),    # auto CRF
                    "-threads", "0",
                    "-c:a", "copy",      # audio tidak diubah
                    temp_output
                ]
            else:
                # Tidak ada subtitle — encode langsung
                command = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-c:v", "libx264",
                    "-preset", preset,
                    "-crf", str(crf),
                    "-threads", "0",
                    "-c:a", "copy",
                    temp_output
                ]
                
            logger.info(f"Burning subtitles for {video_file} (crf={crf}, preset={preset})...")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"FFmpeg burning failed for {video_file}:\n{stderr.decode()}")
                return False
            
            processed_videos.append(f"hard_{video_file}")
            
        # ── Concat semua episode ──────────────────────────────────────────
        if progress_callback:
            await progress_callback(f"🔗 **Menggabungkan {total_videos} episode...**\n{create_progress_bar(95)}")
            
        list_file_path = os.path.join(video_dir, "list.txt")
        with open(list_file_path, "w") as f:
            for file in processed_videos:
                f.write(f"file '{file}'\n")

        concat_command = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_file_path,
            "-c", "copy",
            output_path
        ]
        
        logger.info(f"Concatenating {len(processed_videos)} episodes...")
        process = await asyncio.create_subprocess_exec(
            *concat_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"FFmpeg concat failed:\n{stderr.decode()}")
            return False
            
        if progress_callback:
            await progress_callback(f"✅ **Selesai Menggabungkan!** (100%)\n{create_progress_bar(100)}")
            
        logger.info(f"Successfully processed hardsubs and merged into {output_path}")
        return True
    except Exception as e:
        logger.error(f"Error during hardsub/merge: {e}")
        return False

async def split_video(video_path: str, max_size_gb: float = 1.99) -> list[str]:
    """
    Splits a video into parts if it exceeds max_size_gb.
    Returns a list of paths to the split parts.
    """
    file_size = os.path.getsize(video_path)
    max_size_bytes = max_size_gb * 1024 * 1024 * 1024
    
    if file_size <= max_size_bytes:
        return [video_path]
    
    logger.info(f"📏 File size ({file_size} bytes) exceeds {max_size_gb} GB. Splitting...")
    
    # Get total duration
    cmd_duration = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd_duration,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"Failed to get duration for splitting: {stderr.decode()}")
        return [video_path]
    
    total_duration = float(stdout.decode().strip())
    
    # Estimate segment time
    # Time = (Max Size / Total Size) * Total Duration
    # We use a 5% safety margin to ensure it's under the limit
    segment_time = (max_size_bytes / file_size) * total_duration * 0.95
    
    video_dir = os.path.dirname(video_path)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    output_pattern = os.path.join(video_dir, f"{base_name}_part%02d.mp4")
    
    split_command = [
        "ffmpeg", "-y", "-i", video_path,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        output_pattern
    ]
    
    logger.info(f"✂️ Splitting video into parts with segment_time={segment_time}s...")
    process = await asyncio.create_subprocess_exec(
        *split_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        logger.error(f"FFmpeg splitting failed:\n{stderr.decode()}")
        return [video_path]
    
    # Find all created parts
    parts = []
    i = 0
    while True:
        part_name = f"{base_name}_part{i:02d}.mp4"
        part_path = os.path.join(video_dir, part_name)
        if os.path.exists(part_path):
            parts.append(part_path)
            i += 1
        else:
            break
            
    if not parts:
        return [video_path]
        
    logger.info(f"✅ Video split into {len(parts)} parts.")
    return parts
