import os
import asyncio
import subprocess
import logging
import time

logger = logging.getLogger(__name__)

def create_progress_bar(percentage):
    blocks = int(percentage / 10)
    bar = "■" * blocks + "□" * (10 - blocks)
    return f"|{bar}| {percentage}%"

async def merge_and_hardsub(video_dir: str, output_path: str, progress_callback=None, title=""):
    """
    Merges all episodes and burns subtitles into each before merging.
    """
    start_time = time.time()
    try:
        # Get all video files
        videos = [f for f in os.listdir(video_dir) if f.endswith(".mp4") and "ep_" in f]
        videos.sort()
        
        total_videos = len(videos)
        processed_videos = []
        
        for i, video_file in enumerate(videos, 1):
            if progress_callback:
                percentage = int((i / (total_videos + 1)) * 100)
                elapsed = time.time() - start_time
                # Estimate remaining time
                avg_time_per_ep = elapsed / i if i > 0 else 0
                remaining_eps = total_videos - i + 1
                est_remaining = avg_time_per_ep * remaining_eps
                
                est_min = int(est_remaining // 60)
                est_sec = int(est_remaining % 60)
                
                status_text = (
                    f"🎬 **{title}**\n"
                    f"🔥 **Status: Burning Hardsub...**\n"
                    f"🎞 Episode {i}/{total_videos}\n"
                    f"{create_progress_bar(percentage)}\n"
                    f"⏳ Estimasi Selesai: {est_min}m {est_sec}s"
                )
                await progress_callback(status_text)
                
            ep_str = video_file.replace("ep_", "").replace(".mp4", "")
            sub_file = f"ep_{ep_str}.srt"
            sub_path = os.path.join(video_dir, sub_file)
            input_path = os.path.join(video_dir, video_file)
            temp_output = os.path.join(video_dir, f"hard_{video_file}")
            
            # If subtitle exists, burn it
            if os.path.exists(sub_path):
                sub_path_fixed = sub_path.replace("\\", "/").replace(":", "\\:")
                style = f"Fontname=Standard Symbols PS,Fontsize=10,PrimaryColour=&H00FFFFFF,Bold=1,Outline=1,OutlineColour=&H000000,MarginV=90"
                
                command = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-vf", f"subtitles='{sub_path_fixed}':force_style='{style}'",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "copy",
                    temp_output
                ]
            else:
                command = [
                    "ffmpeg", "-y", "-i", input_path,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "copy",
                    temp_output
                ]
                
            logger.info(f"Burning subtitles for {video_file}...")
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
            
        # Now concat the hard-subbed videos
        if progress_callback:
            await progress_callback(f"🔗 **Menggabungkan {total_videos} episode...**\n{create_progress_bar(95)}")
            
        list_file_path = os.path.join(video_dir, "list.txt")
        with open(list_file_path, "w") as f:
            for file in processed_videos:
                f.write(f"file '{file}'\n")

        # Concat command
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
