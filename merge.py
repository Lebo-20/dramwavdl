import os
import asyncio
import subprocess
import logging
import time

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
            preset = "faster"
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
