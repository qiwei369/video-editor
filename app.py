import os
import subprocess
import uuid
from flask import Flask, request, render_template, send_file, jsonify
from werkzeug.utils import secure_filename
from pathlib import Path

app = Flask(__name__)

# 配置上传文件夹
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process_video():
    try:
        # 获取上传的视频文件列表
        video_files = request.files.getlist('videos')
        if not video_files or len(video_files) == 0:
            return jsonify({'error': '请至少上传一个视频文件'}), 400

        # 获取剪切参数
        trim_start = float(request.form.get('trim_start', 0))
        trim_end = float(request.form.get('trim_end', 0))

        # 检查是否启用配音替换
        enable_audio = request.form.get('enable_audio', 'false').lower() == 'true'
        audio_file = None
        if enable_audio:
            audio_file = request.files.get('audio')
            if not audio_file or audio_file.filename == '':
                return jsonify({'error': '启用配音替换但未上传音频文件'}), 400

        # 为本次任务生成唯一ID，用于存放临时文件
        task_id = str(uuid.uuid4())
        task_folder = os.path.join(UPLOAD_FOLDER, task_id)
        os.makedirs(task_folder, exist_ok=True)

        # 1. 保存上传的原始视频
        video_paths = []
        for idx, vf in enumerate(video_files):
            filename = secure_filename(vf.filename or f'video_{idx}.mp4')
            # 确保扩展名为 .mp4
            if not filename.lower().endswith('.mp4'):
                filename += '.mp4'
            save_path = os.path.join(task_folder, f'input_{idx}_{filename}')
            vf.save(save_path)
            video_paths.append(save_path)

        # 2. 裁剪视频头尾
        trimmed_paths = []
        for i, vp in enumerate(video_paths):
            output_trim = os.path.join(task_folder, f'trimmed_{i}.mp4')
            cmd_trim = ['ffmpeg', '-y', '-i', vp]
            filter_parts = []
            # 计算起始时间
            if trim_start > 0:
                filter_parts.append(f"trim=start={trim_start}")
            if trim_end > 0:
                # 获取视频时长，用于计算截掉片尾后的结束时间
                duration = get_video_duration(vp)
                if duration is not None and duration > (trim_start + trim_end):
                    end_time = duration - trim_end
                    if trim_start > 0:
                        filter_parts.append(f"trim=start={trim_start}:end={end_time}")
                    else:
                        filter_parts.append(f"trim=end={end_time}")

            if filter_parts:
                # 如果有剪切参数，使用 trim 滤镜
                if len(filter_parts) == 2:
                    # 设置开始和结束
                    filter_str = f"trim=start={trim_start}:end={end_time},setpts=PTS-STARTPTS"
                elif filter_parts[0].startswith('trim=start='):
                    filter_str = f"{filter_parts[0]},setpts=PTS-STARTPTS"
                else:
                    filter_str = f"{filter_parts[0]},setpts=PTS-STARTPTS"
                cmd_trim.extend(['-filter_complex', filter_str])
            
            cmd_trim.append(output_trim)
            subprocess.run(cmd_trim, check=True, capture_output=True)
            trimmed_paths.append(output_trim)

        # 如果只有一个视频且不需要拼接
        if len(trimmed_paths) == 1:
            processed_video = trimmed_paths[0]
        else:
            # 3. 拼接所有裁剪后的视频
            concat_file = os.path.join(task_folder, 'concat_list.txt')
            with open(concat_file, 'w') as f:
                for tp in trimmed_paths:
                    f.write(f"file '{os.path.abspath(tp)}'\n")
            
            processed_video = os.path.join(task_folder, 'concatenated.mp4')
            cmd_concat = ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file, '-c', 'copy', processed_video]
            subprocess.run(cmd_concat, check=True, capture_output=True)

        # 4. 如果需要替换音频
        final_video = os.path.join(task_folder, 'final_output.mp4')
        if enable_audio and audio_file:
            audio_filename = secure_filename(audio_file.filename or 'audio.mp3')
            if not audio_filename.lower().endswith('.mp3'):
                audio_filename += '.mp3'
            audio_path = os.path.join(task_folder, 'audio_' + audio_filename)
            audio_file.save(audio_path)

            # 获取音频时长
            audio_duration = get_video_duration(audio_path)
            if audio_duration is None:
                return jsonify({'error': '无法获取音频时长'}), 500

            # 将视频截断或填充以匹配音频长度，并替换音频
            cmd_replace = [
                'ffmpeg', '-y',
                '-i', processed_video,
                '-i', audio_path,
                '-c:v', 'libx264',   # 重新编码视频流
                '-c:a', 'aac',       # 音频编码为 AAC
                '-map', '0:v:0',     # 使用第一个输入的视频流
                '-map', '1:a:0',     # 使用第二个输入的音频流
                '-shortest',        # 以较短的流为准 (音频长度)
                '-strict', 'experimental',
                final_video
            ]
            subprocess.run(cmd_replace, check=True, capture_output=True)
        else:
            # 不需要替换音频，直接复制
            import shutil
            shutil.copy(processed_video, final_video)

        # 清理上传的临时视频 (可选)
        # 这里为了简化不删除，Render 有存储限制，正式环境可优化

        return send_file(final_video, as_attachment=True, download_name='edited_video.mp4')

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
        return jsonify({'error': f'视频处理失败: {error_msg}'}), 500
    except Exception as e:
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

def get_video_duration(file_path):
    """获取视频/音频文件的时长(秒)"""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        try:
            return float(result.stdout.strip())
        except ValueError:
            return None
    return None

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)