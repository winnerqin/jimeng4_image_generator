import os
import json
import base64
import random
import uuid
import threading
import logging
import time
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
from openai import OpenAI
import database

# 配置日志
log_dir = Path('logs')
log_dir.mkdir(exist_ok=True)

# 创建日志格式
log_format = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 文件日志处理器
file_handler = logging.FileHandler(
    log_dir / f'app_{datetime.now().strftime("%Y%m%d")}.log',
    encoding='utf-8'
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(log_format)

# 控制台日志处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_format)

# 配置根日志器
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 应用日志器
app_logger = logging.getLogger('app')
app_logger.setLevel(logging.INFO)

# 全局变量：存储批量任务进度
batch_progress = {}
batch_progress_lock = threading.Lock()

# 全局变量：存储单图生成任务状态
single_generation_tasks = {}
single_generation_lock = threading.Lock()

# 阿里云 OSS 上传支持
def upload_to_aliyun_oss(file_path, user_id=None, is_sample=False):
    """
    上传文件到阿里云 OSS（对象存储服务）
    需要配置以下环境变量：
    - OSS_ENDPOINT: OSS 端点（如：oss-cn-wulanchabu.aliyuncs.com）
    - OSS_BUCKET: 存储桶名称（从 endpoint 中提取）
    - OSS_ACCESS_KEY_ID: 阿里云 AccessKey ID
    - OSS_ACCESS_KEY_SECRET: 阿里云 AccessKey Secret
    
    Args:
        file_path: 本地文件路径
        user_id: 用户ID，用于隔离用户文件
        is_sample: 是否为示例图（示例图保存到sample/user_{user_id}/目录）
    """
    try:
        import oss2
        
        # 从环境变量获取配置
        oss_endpoint_full = os.environ.get('OSS_ENDPOINT', 'shor-file.oss-cn-wulanchabu.aliyuncs.com')
        access_key_id = os.environ.get('OSS_ACCESS_KEY_ID')
        access_key_secret = os.environ.get('OSS_ACCESS_KEY_SECRET')
        
        if not all([oss_endpoint_full, access_key_id, access_key_secret]):
            print("警告：OSS 配置不完整，请检查 .env 文件")
            return None
        
        # 从完整的 endpoint 中提取 bucket 和实际 endpoint
        # 格式: bucket-name.oss-region.aliyuncs.com
        parts = oss_endpoint_full.split('.', 1)
        if len(parts) == 2:
            bucket_name = parts[0]
            oss_endpoint = parts[1]
        else:
            print(f"警告：OSS_ENDPOINT 格式不正确: {oss_endpoint_full}")
            return None
        
        # 初始化 OSS 客户端
        auth = oss2.Auth(access_key_id, access_key_secret)
        bucket = oss2.Bucket(auth, f"https://{oss_endpoint}", bucket_name)
        
        # 生成对象键（文件名）- 根据类型和用户分类
        filename = os.path.basename(file_path)
        timestamp = datetime.now().strftime('%Y%m%d')
        
        if is_sample and user_id:
            # 示例图按用户隔离：sample/user_{user_id}/filename
            object_key = f"sample/user_{user_id}/{filename}"
        else:
            # 生成的图片
            object_key = f"ai-images/{timestamp}/{filename}"
        
        # 上传文件
        with open(file_path, 'rb') as f:
            result = bucket.put_object(object_key, f)
        
        # 返回公网访问 URL
        # 格式: https://bucket-name.oss-region.aliyuncs.com/object-key
        public_url = f"https://{oss_endpoint_full}/{object_key}"
        return public_url
        
    except ImportError:
        print("提示：未安装 oss2 SDK，无法使用阿里云 OSS 上传功能。")
        print("安装命令: pip install oss2")
        return None
    except Exception as e:
        print(f"阿里云 OSS 上传失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_oss_bucket():
    """
    获取已配置的 OSS Bucket 对象
    """
    try:
        import oss2
        
        oss_endpoint_full = os.environ.get('OSS_ENDPOINT', 'shor-file.oss-cn-wulanchabu.aliyuncs.com')
        access_key_id = os.environ.get('OSS_ACCESS_KEY_ID')
        access_key_secret = os.environ.get('OSS_ACCESS_KEY_SECRET')
        
        if not all([oss_endpoint_full, access_key_id, access_key_secret]):
            return None, None
        
        # 从完整的 endpoint 中提取 bucket 和实际 endpoint
        parts = oss_endpoint_full.split('.', 1)
        if len(parts) == 2:
            bucket_name = parts[0]
            oss_endpoint = parts[1]
        else:
            return None, None
        
        auth = oss2.Auth(access_key_id, access_key_secret)
        bucket = oss2.Bucket(auth, f"https://{oss_endpoint}", bucket_name)
        
        return bucket, oss_endpoint_full
    except:
        return None, None

def list_sample_images_from_oss(user_id=None):
    """
    列出阿里云 OSS 中的示例图（按用户隔离）
    返回格式: [{'url': 'http://...', 'filename': 'xxx.jpg', 'size': 12345}, ...]
    """
    try:
        import oss2
        
        if not user_id:
            return []
        
        bucket, endpoint_full = get_oss_bucket()
        if not bucket:
            return []
        
        # 列出 sample/{category}/user_{user_id}/ 目录下的所有文件（同时列出人物和场景）
        sample_images = []
        prefixes = [f'sample/person/user_{user_id}/', f'sample/scene/user_{user_id}/']
        for prefix in prefixes:
            for obj in oss2.ObjectIterator(bucket, prefix=prefix):
                if obj.key.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    # 生成公网访问URL
                    url = f"https://{endpoint_full}/{obj.key}"
                    filename = os.path.basename(obj.key)
                    # 推断类别
                    category = 'person' if '/person/' in obj.key else 'scene'
                    sample_images.append({
                        'url': url,
                        'filename': filename,
                        'size': obj.size,
                        'key': obj.key,
                        'category': category
                    })

        return sample_images
    
    except Exception as e:
        print(f"读取 OSS 示例图失败: {e}")
        import traceback
        traceback.print_exc()
        return []

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'output'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production-' + str(uuid.uuid4()))

# 确保文件夹存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)
os.makedirs('static', exist_ok=True)

# 初始化数据库
database.init_database()

# 加载 .env 文件
def find_dotenv(start_dir=None):
    cur = Path(start_dir or os.getcwd()).resolve()
    root = cur.anchor
    while True:
        candidate = cur / '.env'
        if candidate.exists() and candidate.is_file():
            return str(candidate)
        if str(cur) == root:
            return None
        cur = cur.parent

def load_dotenv_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.lower().startswith('export '):
                    line = line[7:].strip()
                if '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip()
                if v.startswith(('"', "'")) and v.endswith(('"', "'")) and len(v) >= 2:
                    v = v[1:-1]
                if os.environ.get(k) is None:
                    os.environ[k] = v
    except Exception:
        pass

# 启动时加载环境变量
dotenv_path = find_dotenv()
if dotenv_path:
    print(f'Loading .env from: {dotenv_path}')
    load_dotenv_file(dotenv_path)

# 尺寸比例到像素的映射
ASPECT_RATIOS = {
    '1:1': {'1k': (1024, 1024), '2k': (2048, 2048), '4k': (4096, 4096)},
    '2:3': {'1k': (683, 1024), '2k': (1365, 2048), '4k': (2731, 4096)},
    '3:2': {'1k': (1024, 683), '2k': (2048, 1365), '4k': (4096, 2731)},
    '3:4': {'1k': (768, 1024), '2k': (1536, 2048), '4k': (3072, 4096)},
    '4:3': {'1k': (1024, 768), '2k': (2048, 1536), '4k': (4096, 3072)},
    '16:9': {'1k': (1024, 576), '2k': (2048, 1152), '4k': (4096, 2304)},
    '9:16': {'1k': (576, 1024), '2k': (1152, 2048), '4k': (2304, 4096)},
}

# ==================== 登录验证装饰器 ====================
def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """获取当前登录用户信息"""
    if 'user_id' in session:
        return database.get_user_by_id(session['user_id'])
    return None

def get_user_upload_folder(user_id):
    """获取用户专属上传目录"""
    folder = os.path.join(app.config['UPLOAD_FOLDER'], str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder

def get_user_output_folder(user_id):
    """获取用户专属输出目录"""
    folder = os.path.join(app.config['OUTPUT_FOLDER'], str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder

# ==================== 登录/注册路由 ====================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return render_template('login.html', error='请输入用户名和密码')
        
        user = database.verify_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='用户名或密码错误')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # 注册功能已禁用，请联系管理员创建账号
    return render_template('login.html', error='注册功能已关闭，请联系管理员获取账号')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ==================== 统计页面路由（仅系统管理员） ====================
@app.route('/stats')
@login_required
def stats_page():
    """统计页面 - 仅系统管理员可访问"""
    user = get_current_user()
    # 检查是否为系统管理员
    if user['username'] != 'system_admin':
        return "访问被拒绝：此页面仅系统管理员可访问", 403
    return render_template('stats.html', user=user)

@app.route('/api/stats')
@login_required
def api_stats():
    """统计API - 仅系统管理员可访问"""
    user = get_current_user()
    # 检查是否为系统管理员
    if user['username'] != 'system_admin':
        return jsonify({'success': False, 'error': '权限不足'}), 403
    
    try:
        # 获取日期筛选参数
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        # 获取统计概览
        overview = database.get_stats_overview()
        
        # 获取用户统计
        user_stats = database.get_user_stats(start_date, end_date)
        
        # 获取每日统计
        daily_stats = database.get_daily_stats(days=7)
        
        return jsonify({
            'success': True,
            'total_users': overview['total_users'],
            'total_images': overview['total_images'],
            'today_images': overview['today_images'],
            'week_images': overview['week_images'],
            'user_stats': user_stats,
            'daily_stats': daily_stats
        })
    except Exception as e:
        print(f"获取统计数据失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== 主页路由 ====================
@app.route('/')
@login_required
def index():
    return render_template('index.html', user=get_current_user())

@app.route('/generate', methods=['POST'])
@login_required
def generate():
    task_id = str(uuid.uuid4())
    user_id = session.get('user_id')
    username = session.get('username', 'unknown')
    
    try:
        # 获取表单数据
        prompt = request.form.get('prompt', '').strip()
        negative_prompt = request.form.get('negative_prompt', '').strip()
        aspect_ratio = request.form.get('aspect_ratio', '1:1')
        resolution = request.form.get('resolution', '2k')
        num_images = int(request.form.get('num_images', 1))
        output_filename = request.form.get('output_filename', 'generated').strip()
        steps = int(request.form.get('steps', 28))
        seed = int(request.form.get('seed', 0))
        
        # 记录任务开始
        with single_generation_lock:
            single_generation_tasks[task_id] = {
                'user_id': user_id,
                'username': username,
                'status': 'generating',
                'prompt': prompt,
                'num_images': num_images,
                'start_time': datetime.now().isoformat(),
                'progress': 0,
                'total': num_images
            }
        
        # 先获取示例图URL（需要在记录参数前获取）
        sample_image_urls = request.form.getlist('sample_image_urls')
        
        # 记录详细的请求参数
        request_params = {
            'prompt': prompt,
            'negative_prompt': negative_prompt,
            'aspect_ratio': aspect_ratio,
            'resolution': resolution,
            'num_images': num_images,
            'output_filename': output_filename,
            'steps': steps,
            'seed': seed,
            'sample_images_count': len(sample_image_urls) if sample_image_urls else 0
        }
        app_logger.info(f"[用户:{username}] [任务:{task_id}] ========== 开始单图生成 ==========")
        app_logger.info(f"[用户:{username}] [任务:{task_id}] 请求参数: {json.dumps(request_params, ensure_ascii=False, indent=2)}")
        
        if not prompt:
            with single_generation_lock:
                if task_id in single_generation_tasks:
                    single_generation_tasks[task_id]['status'] = 'failed'
                    single_generation_tasks[task_id]['error'] = '请输入提示词'
            app_logger.warning(f"[用户:{username}] [任务:{task_id}] 生成失败 - 缺少提示词")
            return jsonify({'error': '请输入提示词', 'task_id': task_id}), 400
        
        # 获取尺寸
        if aspect_ratio in ASPECT_RATIOS and resolution in ASPECT_RATIOS[aspect_ratio]:
            width, height = ASPECT_RATIOS[aspect_ratio][resolution]
        else:
            width, height = 2048, 2048
        
        # 处理上传的图片和示例图
        image_urls = []
        
        # 添加从OSS选择的示例图URL（已在上面获取）
        if sample_image_urls:
            image_urls.extend(sample_image_urls)
            app_logger.info(f"[用户:{username}] [任务:{task_id}] 使用 {len(sample_image_urls)} 张示例图: {sample_image_urls}")
            print(f"使用 {len(sample_image_urls)} 张示例图: {sample_image_urls}")
        
        uploaded_files = request.files.getlist('images')
        
        # 检查是否配置了 OSS 上传（可选功能）
        oss_enabled = os.environ.get('OSS_ENABLED', 'false').lower() == 'true'
        
        # 使用用户专属上传目录
        user_upload_folder = get_user_upload_folder(user_id)
        
        for file in uploaded_files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                # 保留原始文件名
                filepath = os.path.join(user_upload_folder, filename)
                file.save(filepath)
                
                if oss_enabled:
                    # 尝试上传到阿里云 OSS
                    oss_url = upload_to_aliyun_oss(filepath)
                    if oss_url:
                        image_urls.append(oss_url)
                        app_logger.info(f"[用户:{username}] [任务:{task_id}] 成功上传图片到阿里云 OSS: {oss_url}")
                        print(f"成功上传图片到阿里云 OSS: {oss_url}")
                    else:
                        app_logger.warning(f"[用户:{username}] [任务:{task_id}] 阿里云 OSS 上传失败，跳过图片 {filename}")
                        print(f"警告：阿里云 OSS 上传失败，跳过图片 {filename}")
                else:
                    # 如果没有配置 OSS，保存文件但不添加到 image_urls
                    # 这样可以保留上传的文件，但不会导致 API 错误
                    app_logger.info(f"[用户:{username}] [任务:{task_id}] 上传的图片已保存到 {filepath}，但未启用 OSS，将仅使用文字生成图片")
                    print(f"提示：上传的图片已保存到 {filepath}，但未启用 OSS，将仅使用文字生成图片")
        
        # 如果用户上传了图片但没有配置 OSS，给出提示
        if uploaded_files and any(f.filename for f in uploaded_files) and not image_urls:
            print("注意：检测到图片上传，但未配置 OSS。当前仅支持文字生成图片模式。")
            print("如需使用参考图片功能，请在 .env 中配置：")
            print("  OSS_ENABLED=true")
            print("  OSS_ENDPOINT=shor-file.oss-cn-wulanchabu.aliyuncs.com")
            print("  OSS_ACCESS_KEY_ID=你的AccessKeyId")
            print("  OSS_ACCESS_KEY_SECRET=你的AccessKeySecret")
        
        # 获取方舟大模型 API Key
        api_key = os.environ.get('ARK_API_KEY')
        base_url = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        
        if not api_key:
            return jsonify({'error': 'ARK_API_KEY 未配置'}), 500
        
        # 初始化 OpenAI 客户端（兼容方舟大模型）
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        # 生成图片
        generated_images = []
        total_needed = num_images
        
        for i in range(total_needed):
            # 更新任务进度
            with single_generation_lock:
                if task_id in single_generation_tasks:
                    single_generation_tasks[task_id]['progress'] = i + 1
                    single_generation_tasks[task_id]['current_image'] = i + 1
            app_logger.info(f"[用户:{username}] [任务:{task_id}] 生成进度: {i+1}/{total_needed}")
            # 计算种子（方舟大模型 API 限制：最大 99999999）
            if seed and seed != 0:
                per_seed = seed + i
                # 确保不超过最大值
                if per_seed > 99999999:
                    per_seed = (per_seed % 99999999) + 1
            else:
                per_seed = random.randint(1, 99999999)
            
            # 构建提示词
            full_prompt = prompt
            if negative_prompt:
                full_prompt = f"{prompt}\n负面词: {negative_prompt}"
            
            # 根据分辨率映射到方舟大模型支持的size格式
            size_map = {
                '1024x1024': '1K',
                '2048x2048': '2K',
                '1536x1536': '2K',
            }
            ark_size = size_map.get(f"{width}x{height}", "2K")
            
            # 记录API请求详情
            api_request = {
                'model': 'doubao-seedream-4-5-251128',
                'prompt': full_prompt,
                'size': ark_size,
                'width': width,
                'height': height,
                'seed': per_seed,
                'image_count': i + 1,
                'total': total_needed,
                'sample_images': len(image_urls)
            }
            app_logger.info(f"[用户:{username}] [任务:{task_id}] [图片 {i+1}/{total_needed}] 调用API生成图片")
            app_logger.info(f"[用户:{username}] [任务:{task_id}] [图片 {i+1}/{total_needed}] API请求参数: {json.dumps(api_request, ensure_ascii=False, indent=2)}")
            
            # 调用方舟大模型生成图片
            try:
                api_start_time = time.time()
                response = client.images.generate(
                    model="doubao-seedream-4-5-251128",
                    prompt=full_prompt,
                    size=ark_size,
                    response_format="url",
                    extra_body={
                        "watermark": False,
                    }
                )
                api_duration = time.time() - api_start_time
                app_logger.info(f"[用户:{username}] [任务:{task_id}] [图片 {i+1}/{total_needed}] API响应时间: {api_duration:.2f}秒")
                
                # 处理响应
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    app_logger.info(f"[用户:{username}] [任务:{task_id}] [图片 {i+1}/{total_needed}] API返回成功，图片URL: {img_url}")
                    
                    # 下载图片
                    import requests
                    download_start_time = time.time()
                    img_response = requests.get(img_url)
                    download_duration = time.time() - download_start_time
                    app_logger.info(f"[用户:{username}] [任务:{task_id}] [图片 {i+1}/{total_needed}] 图片下载时间: {download_duration:.2f}秒，状态码: {img_response.status_code}")
                    
                    if img_response.status_code == 200:
                        img_data = img_response.content
                        img_size = len(img_data)
                        app_logger.info(f"[用户:{username}] [任务:{task_id}] [图片 {i+1}/{total_needed}] 图片大小: {img_size / 1024:.2f} KB")
                        
                        # 生成文件名
                        if num_images > 1:
                            filename = f"{output_filename}_{i+1}.jpg"
                        else:
                            filename = f"{output_filename}.jpg"
                        
                        # 使用用户专属输出目录
                        user_output_folder = get_user_output_folder(user_id)
                        output_path = os.path.join(user_output_folder, filename)
                        with open(output_path, 'wb') as f:
                            f.write(img_data)
                        
                        generated_images.append({
                            'filename': filename,
                            'url': f'/output/{user_id}/{filename}',
                            'seed': per_seed
                        })
                        
                        # 保存记录到数据库
                        try:
                            sample_images_list = [{'url': url, 'filename': os.path.basename(url)} for url in image_urls]
                            database.save_generation_record({
                                'user_id': user_id,
                                'prompt': prompt,
                                'negative_prompt': negative_prompt,
                                'aspect_ratio': aspect_ratio,
                                'resolution': resolution,
                                'width': width,
                                'height': height,
                                'num_images': 1,
                                'seed': per_seed,
                                'steps': steps,
                                'sample_images': sample_images_list,
                                'image_path': f'/output/{user_id}/{filename}',
                                'filename': filename,
                                'status': 'success'
                            })
                        except Exception as db_err:
                            app_logger.error(f"[用户:{username}] [任务:{task_id}] 保存记录失败: {db_err}")
                            print(f"保存记录失败: {db_err}")
                else:
                    app_logger.warning(f"[用户:{username}] [任务:{task_id}] API 返回错误: 无法获取图片")
                    print(f"API 返回错误: 无法获取图片")
            except Exception as e:
                app_logger.error(f"[用户:{username}] [任务:{task_id}] 生成第 {i+1} 张图片时出错: {e}")
                print(f"生成第 {i+1} 张图片时出错: {e}")
                continue
        
        if not generated_images:
            with single_generation_lock:
                if task_id in single_generation_tasks:
                    single_generation_tasks[task_id]['status'] = 'failed'
                    single_generation_tasks[task_id]['error'] = '图片生成失败，请检查参数'
            app_logger.error(f"[用户:{username}] [任务:{task_id}] 生成失败 - 所有图片生成失败")
            return jsonify({'error': '图片生成失败，请检查参数', 'task_id': task_id}), 500
        
        # 更新任务状态为完成
        with single_generation_lock:
            if task_id in single_generation_tasks:
                single_generation_tasks[task_id]['status'] = 'completed'
                single_generation_tasks[task_id]['end_time'] = datetime.now().isoformat()
                single_generation_tasks[task_id]['result'] = {
                    'images_count': len(generated_images),
                    'filenames': [img['filename'] for img in generated_images]
                }
        
        # 记录最终结果
        result_summary = {
            'total_requested': num_images,
            'total_generated': len(generated_images),
            'filenames': [img['filename'] for img in generated_images],
            'seeds': [img['seed'] for img in generated_images]
        }
        app_logger.info(f"[用户:{username}] [任务:{task_id}] ========== 生成完成 ==========")
        app_logger.info(f"[用户:{username}] [任务:{task_id}] 生成结果: {json.dumps(result_summary, ensure_ascii=False, indent=2)}")
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'images': generated_images,
            'params': {
                'prompt': prompt,
                'negative_prompt': negative_prompt,
                'aspect_ratio': aspect_ratio,
                'resolution': resolution,
                'width': width,
                'height': height,
                'num_images': num_images
            }
        })
    
    except Exception as e:
        with single_generation_lock:
            if task_id in single_generation_tasks:
                single_generation_tasks[task_id]['status'] = 'failed'
                single_generation_tasks[task_id]['error'] = str(e)
        app_logger.error(f"[用户:{username}] [任务:{task_id}] 服务器错误: {e}", exc_info=True)
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'服务器错误: {str(e)}', 'task_id': task_id}), 500

@app.route('/api/sample-images')
@login_required
def get_sample_images():
    """获取 OSS 中的示例图列表（用户隔离）"""
    try:
        user_id = session.get('user_id')
        category = request.args.get('category')
        # 先从 OSS 列表中读取（如果配置了 OSS）
        sample_images = list_sample_images_from_oss(user_id)

        # 再从数据库读取人物/场景库中的条目（包含本地保存的备份路径）
        # 为避免同一文件既存在于 OSS 又存在于数据库中导致重复显示，按 URL 去重
        try:
            existing_urls = set([s.get('url') for s in sample_images if s.get('url')])

            person_assets = database.get_person_assets(user_id)
            for a in person_assets:
                a_url = a.get('url')
                if a_url and a_url in existing_urls:
                    # 已由 OSS 列表包含，跳过添加 DB 条目以避免重复
                    continue
                sample_images.append({
                    'url': a_url,
                    'filename': a.get('filename'),
                    'size': None,
                    'key': f"db_person_{a.get('id')}",
                    'category': 'person'
                })

            # 更新已存在 URL 集合
            existing_urls.update([a.get('url') for a in person_assets if a.get('url')])
        except Exception:
            pass

        try:
            scene_assets = database.get_scene_assets(user_id)
            for a in scene_assets:
                a_url = a.get('url')
                if a_url and a_url in existing_urls:
                    continue
                sample_images.append({
                    'url': a_url,
                    'filename': a.get('filename'),
                    'size': None,
                    'key': f"db_scene_{a.get('id')}",
                    'category': 'scene'
                })
        except Exception:
            pass

        # 如果请求了特定类别，则过滤
        if category in ('person', 'scene'):
            sample_images = [s for s in sample_images if s.get('category') == category]

        return jsonify({
            'success': True,
            'images': sample_images
        })
    except Exception as e:
        print(f"获取示例图失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'images': []
        })

@app.route('/batch')
@login_required
def batch():
    """批量生成页面"""
    return render_template('batch.html', user=get_current_user())

@app.route('/records')
@login_required
def records():
    """生成记录页面"""
    return render_template('records.html', user=get_current_user())

@app.route('/manage-samples')
@login_required
def manage_samples():
    """素材管理页面"""
    return render_template('manage_samples.html', user=get_current_user())

@app.route('/script-analysis')
@login_required
def script_analysis():
    """剧本分析页面"""
    return render_template('script_analysis.html', user=get_current_user())

@app.route('/api/batch-generate', methods=['POST'])
@login_required
def batch_generate():
    """批量生成API"""
    try:
        user_id = session.get('user_id')
        data = request.json
        batch_id = str(uuid.uuid4())
        
        # 获取参数
        prompt = data.get('prompt', '').strip()
        negative_prompt = data.get('negative_prompt', '').strip()
        aspect_ratio = data.get('aspect_ratio', '1:1')
        resolution = data.get('resolution', '2k')
        sample_images_data = data.get('sample_images', [])
        num_images = int(data.get('num_images', 1))
        filename_base = data.get('filename', 'batch')
        
        if not prompt:
            return jsonify({'success': False, 'error': '请输入提示词'}), 400
        
        # 获取尺寸
        if aspect_ratio in ASPECT_RATIOS and resolution in ASPECT_RATIOS[aspect_ratio]:
            width, height = ASPECT_RATIOS[aspect_ratio][resolution]
        else:
            width, height = 2048, 2048
        
        # 准备示例图 URL
        image_urls = [img['url'] for img in sample_images_data if 'url' in img]
        
        # 获取方舟大模型 API Key
        api_key = os.environ.get('ARK_API_KEY')
        base_url = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        
        if not api_key:
            return jsonify({'success': False, 'error': 'ARK_API_KEY 未配置'}), 500
        
        # 初始化 OpenAI 客户端
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        # 生成图片
        generated_images = []
        
        for i in range(num_images):
            per_seed = random.randint(1, 99999999)
            
            # 构建提示词
            full_prompt = prompt
            if negative_prompt:
                full_prompt = f"{prompt}\n负面词: {negative_prompt}"
            
            # 根据分辨率映射到方舟大模型支持的size格式
            size_map = {
                '1024x1024': '1K',
                '2048x2048': '2K',
                '1536x1536': '2K',
            }
            ark_size = size_map.get(f"{width}x{height}", "2K")
            
            try:
                response = client.images.generate(
                    model="doubao-seedream-4-5-251128",
                    prompt=full_prompt,
                    size=ark_size,
                    response_format="url",
                    extra_body={
                        "watermark": False,
                    }
                )
                
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    
                    # 下载图片
                    import requests
                    img_response = requests.get(img_url)
                    if img_response.status_code == 200:
                        img_data = img_response.content
                        
                        if num_images > 1:
                            filename = f"{filename_base}_{i+1}.jpg"
                        else:
                            filename = f"{filename_base}.jpg"
                        
                        # 使用用户专属输出目录
                        user_output_folder = get_user_output_folder(user_id)
                        output_path = os.path.join(user_output_folder, filename)
                        with open(output_path, 'wb') as f:
                            f.write(img_data)
                        
                        generated_images.append({
                            'filename': filename,
                            'url': f'/output/{user_id}/{filename}',
                            'seed': per_seed
                        })
                        
                        # 保存记录
                        try:
                            database.save_generation_record({
                                'user_id': user_id,
                                'prompt': prompt,
                                'negative_prompt': negative_prompt,
                                'aspect_ratio': aspect_ratio,
                                'resolution': resolution,
                                'width': width,
                                'height': height,
                                'num_images': 1,
                                'seed': per_seed,
                                'steps': 28,
                                'sample_images': sample_images_data,
                                'image_path': f'/output/{user_id}/{filename}',
                                'filename': filename,
                                'batch_id': batch_id,
                                'status': 'success'
                            })
                        except Exception as db_err:
                            print(f"保存记录失败: {db_err}")
            except Exception as e:
                print(f"生成第 {i+1} 张图片时出错: {e}")
                continue
        
        if not generated_images:
            return jsonify({'success': False, 'error': '图片生成失败'}), 500
        
        return jsonify({
            'success': True,
            'images': generated_images,
            'batch_id': batch_id
        })
    
    except Exception as e:
        print(f"批量生成错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/records')
@login_required
def get_records():
    """获取生成记录"""
    try:
        user_id = session.get('user_id')
        limit = int(request.args.get('limit', 20))
        offset = int(request.args.get('offset', 0))
        search = request.args.get('search', '')
        
        records = database.get_all_records(user_id, limit, offset)
        
        # 如果有搜索条件，过滤结果
        if search:
            records = [r for r in records if search.lower() in r['prompt'].lower()]
        
        total = database.get_total_count(user_id)
        
        return jsonify({
            'success': True,
            'records': records,
            'total': total
        })
    except Exception as e:
        print(f"获取记录失败: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'records': [],
            'total': 0
        })

@app.route('/api/records/<int:record_id>', methods=['DELETE'])
@login_required
def delete_record(record_id):
    """删除记录"""
    try:
        database.delete_record(record_id)
        return jsonify({'success': True})
    except Exception as e:
        print(f"删除记录失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/batch-delete', methods=['POST'])
@login_required
def batch_delete_records():
    """批量删除记录"""
    try:
        data = request.get_json()
        record_ids = data.get('ids', [])
        
        if not record_ids:
            return jsonify({'success': False, 'message': '未选择要删除的记录'})
        
        deleted_count = 0
        failed_count = 0
        
        for record_id in record_ids:
            try:
                database.delete_record(record_id)
                deleted_count += 1
            except Exception as e:
                print(f"删除记录 {record_id} 失败: {e}")
                failed_count += 1
        
        return jsonify({
            'success': True,
            'deleted': deleted_count,
            'failed': failed_count
        })
    except Exception as e:
        print(f"批量删除记录失败: {e}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/upload-sample-image', methods=['POST'])
@login_required
def upload_sample_image():
    """上传示例图到 OSS（用户隔离）"""
    try:
        user_id = session.get('user_id')
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': '文件名为空'}), 400
        
        # 验证文件类型
        allowed_extensions = {'jpg', 'jpeg', 'png', 'webp'}
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if file_ext not in allowed_extensions:
            return jsonify({'success': False, 'error': f'不支持的文件格式，仅支持: {", ".join(allowed_extensions)}'}), 400
        
        # 获取 OSS 配置
        bucket, endpoint_full = get_oss_bucket()
        if not bucket:
            return jsonify({'success': False, 'error': 'OSS 配置不完整'}), 500
        
        # 生成对象键 - 按用户与类别保存到 sample/{category}/user_{user_id}/ 目录
        filename = secure_filename(file.filename)
        category = request.form.get('category', 'person')
        if category not in ('person', 'scene'):
            category = 'person'
        object_key = f"sample/{category}/user_{user_id}/{filename}"
        
        # 上传文件到 OSS
        import oss2
        file.seek(0)
        bucket.put_object(object_key, file.read())
        
        # 生成公网访问 URL
        url = f"https://{endpoint_full}/{object_key}"
        
        return jsonify({
            'success': True,
            'url': url,
            'filename': filename,
            'key': object_key
        })
    
    except Exception as e:
        print(f"上传示例图失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/delete-sample-image', methods=['POST'])
@login_required
def delete_sample_image():
    """从 OSS 删除示例图（验证用户权限）"""
    try:
        user_id = session.get('user_id')
        data = request.json
        key = data.get('key')
        
        if not key:
            return jsonify({'success': False, 'error': '缺少文件 key'}), 400
        
        # 验证 key 是否属于当前用户（支持 person/scene 两类）
        allowed_prefixes = [f'sample/person/user_{user_id}/', f'sample/scene/user_{user_id}/']
        if not any(key.startswith(p) for p in allowed_prefixes):
            return jsonify({'success': False, 'error': '无权删除此文件'}), 403
        
        # 获取 OSS 配置
        bucket, endpoint_full = get_oss_bucket()
        if not bucket:
            return jsonify({'success': False, 'error': 'OSS 配置不完整'}), 500
        
        # 删除文件
        bucket.delete_object(key)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"删除示例图失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delete-library-asset', methods=['POST'])
@login_required
def delete_library_asset():
    """删除数据库中人物/场景库的条目（key 格式: db_person_<id> 或 db_scene_<id>）"""
    try:
        data = request.get_json() or {}
        key = data.get('key')
        user_id = session.get('user_id')

        if not key:
            return jsonify({'success': False, 'error': '缺少 key'}), 400

        if key.startswith('db_person_'):
            aid = int(key.split('_')[-1])
            # 删除数据库记录
            try:
                # 若存在本地文件路径，尝试删除
                conn_asset = database.get_person_assets(user_id)
            except Exception:
                conn_asset = []

            database.delete_person_asset(aid)
            return jsonify({'success': True})
        elif key.startswith('db_scene_'):
            aid = int(key.split('_')[-1])
            try:
                conn_asset = database.get_scene_assets(user_id)
            except Exception:
                conn_asset = []
            database.delete_scene_asset(aid)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': '不支持的 key 类型'}), 400
    except Exception as e:
        print(f"删除库条目失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/add-to-person-library', methods=['POST'])
@login_required
def add_to_person_library():
    """将指定图片保存到人物库（OSS 或本地备份）并写入数据库"""
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}
        url = data.get('url')
        filename = secure_filename(data.get('filename') or os.path.basename(url or ''))

        if not url:
            return jsonify({'success': False, 'error': '缺少 url'}), 400

        # 尝试将文件上传到 OSS（如果配置了 OSS）
        bucket, endpoint_full = get_oss_bucket()
        target_key = f'sample/person/user_{user_id}/{filename}'
        public_url = None

        # 如果是本地输出路径（/output/...），直接读取并上传
        if url.startswith('/output/') and os.path.exists(url.lstrip('/')):
            local_path = url.lstrip('/')
            if bucket:
                with open(local_path, 'rb') as fh:
                    bucket.put_object(target_key, fh.read())
                public_url = f'https://{endpoint_full}/{target_key}'
            else:
                # 保存到本地 uploads 目录作为备份
                dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'sample', 'person', f'user_{user_id}')
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, filename)
                import shutil
                shutil.copy(local_path, dest_path)
                public_url = '/' + dest_path.replace('\\', '/')
        else:
            # 若为远程 URL，尝试下载再上传
            import requests
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                if bucket:
                    bucket.put_object(target_key, resp.content)
                    public_url = f'https://{endpoint_full}/{target_key}'
                else:
                    dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'sample', 'person', f'user_{user_id}')
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = os.path.join(dest_dir, filename)
                    with open(dest_path, 'wb') as fh:
                        fh.write(resp.content)
                    public_url = '/' + dest_path.replace('\\', '/')
            else:
                return jsonify({'success': False, 'error': '无法下载远程图片'}), 400

        # 写入数据库记录
        try:
            database.save_person_asset(user_id, filename, public_url, meta={'source_url': url})
        except Exception as e:
            print(f"保存人物库记录失败: {e}")

        return jsonify({'success': True, 'url': public_url, 'filename': filename})
    except Exception as e:
        print(f"添加到人物库失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/add-to-scene-library', methods=['POST'])
@login_required
def add_to_scene_library():
    """将指定图片保存到场景库（OSS 或本地备份）并写入数据库"""
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}
        url = data.get('url')
        filename = secure_filename(data.get('filename') or os.path.basename(url or ''))

        if not url:
            return jsonify({'success': False, 'error': '缺少 url'}), 400

        bucket, endpoint_full = get_oss_bucket()
        target_key = f'sample/scene/user_{user_id}/{filename}'
        public_url = None

        if url.startswith('/output/') and os.path.exists(url.lstrip('/')):
            local_path = url.lstrip('/')
            if bucket:
                with open(local_path, 'rb') as fh:
                    bucket.put_object(target_key, fh.read())
                public_url = f'https://{endpoint_full}/{target_key}'
            else:
                dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'sample', 'scene', f'user_{user_id}')
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, filename)
                import shutil
                shutil.copy(local_path, dest_path)
                public_url = '/' + dest_path.replace('\\', '/')
        else:
            import requests
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                if bucket:
                    bucket.put_object(target_key, resp.content)
                    public_url = f'https://{endpoint_full}/{target_key}'
                else:
                    dest_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'sample', 'scene', f'user_{user_id}')
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_path = os.path.join(dest_dir, filename)
                    with open(dest_path, 'wb') as fh:
                        fh.write(resp.content)
                    public_url = '/' + dest_path.replace('\\', '/')
            else:
                return jsonify({'success': False, 'error': '无法下载远程图片'}), 400

        try:
            database.save_scene_asset(user_id, filename, public_url, meta={'source_url': url})
        except Exception as e:
            print(f"保存场景库记录失败: {e}")

        return jsonify({'success': True, 'url': public_url, 'filename': filename})
    except Exception as e:
        print(f"添加到场景库失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/batch-generate-all', methods=['POST'])
@login_required
def batch_generate_all():
    """处理整个批量生成任务（后端处理）"""
    try:
        user_id = session.get('user_id')
        username = session.get('username', 'unknown')
        data = request.json
        tasks = data.get('tasks', [])
        
        if not tasks:
            app_logger.warning(f"[用户:{username}] 批量生成失败 - 没有任务")
            return jsonify({'success': False, 'error': '没有任务'}), 400
        
        # 生成批次ID
        batch_id = str(uuid.uuid4())
        
        app_logger.info(f"[用户:{username}] [批次:{batch_id}] ========== 开始批量生成 ==========")
        app_logger.info(f"[用户:{username}] [批次:{batch_id}] 总任务数: {len(tasks)}")
        # 记录所有任务的详细信息（只记录前3个任务的完整信息，避免日志过长）
        for idx, task in enumerate(tasks[:3]):
            task_info = {
                'task_index': idx + 1,
                'prompt': task.get('prompt', '')[:100],  # 只记录前100个字符
                'aspect_ratio': task.get('aspect_ratio', '1:1'),
                'resolution': task.get('resolution', '2k'),
                'num_images': task.get('num_images', 1),
                'sample_images_count': len(task.get('sample_images', []))
            }
            app_logger.info(f"[用户:{username}] [批次:{batch_id}] 任务 {idx + 1} 详情: {json.dumps(task_info, ensure_ascii=False)}")
        if len(tasks) > 3:
            app_logger.info(f"[用户:{username}] [批次:{batch_id}] ... 还有 {len(tasks) - 3} 个任务（详情略）")
        
        # 初始化进度
        with batch_progress_lock:
            batch_progress[batch_id] = {
                'user_id': user_id,
                'username': username,
                'total': len(tasks),
                'completed': 0,
                'failed': 0,
                'status': 'running',
                'start_time': datetime.now().isoformat(),
                'logs': []
            }
        
        # 创建一个函数在后台线程中执行批量生成
        def process_batch():
            for i, task in enumerate(tasks):
                try:
                    # 更新进度
                    task_start_time = datetime.now()
                    task_info = {
                        'prompt': task.get('prompt', '')[:100],
                        'aspect_ratio': task.get('aspect_ratio', '1:1'),
                        'resolution': task.get('resolution', '2k'),
                        'num_images': task.get('num_images', 1),
                        'sample_images_count': len(task.get('sample_images', []))
                    }
                    app_logger.info(f"[用户:{username}] [批次:{batch_id}] [任务 {i+1}/{len(tasks)}] 开始处理")
                    app_logger.info(f"[用户:{username}] [批次:{batch_id}] [任务 {i+1}/{len(tasks)}] 任务参数: {json.dumps(task_info, ensure_ascii=False)}")
                    
                    with batch_progress_lock:
                        batch_progress[batch_id]['logs'].append({
                            'time': datetime.now().isoformat(),
                            'message': f"开始任务 {i+1}/{len(tasks)}: {task.get('prompt', '')[:30]}...",
                            'type': 'info'
                        })
                    
                    # 调用原有的批量生成逻辑
                    result = process_single_batch_task(task, batch_id, user_id)
                    
                    task_duration = (datetime.now() - task_start_time).total_seconds()
                    app_logger.info(f"[用户:{username}] [批次:{batch_id}] [任务 {i+1}/{len(tasks)}] 处理完成，耗时: {task_duration:.2f}秒")
                    
                    with batch_progress_lock:
                        if result.get('success'):
                            batch_progress[batch_id]['completed'] += 1
                            batch_progress[batch_id]['logs'].append({
                                'time': datetime.now().isoformat(),
                                'message': f"✓ 任务 {i+1} 完成",
                                'type': 'success'
                            })
                        else:
                            batch_progress[batch_id]['failed'] += 1
                            batch_progress[batch_id]['logs'].append({
                                'time': datetime.now().isoformat(),
                                'message': f"✗ 任务 {i+1} 失败: {result.get('error', '未知错误')}",
                                'type': 'error'
                            })
                    
                    app_logger.info(f"[用户:{batch_progress[batch_id].get('username', 'unknown')}] [批次:{batch_id}] 任务进度: {i+1}/{len(tasks)}")
                    print(f"批量任务进度: {i+1}/{len(tasks)}")
                except Exception as e:
                    app_logger.error(f"[用户:{batch_progress[batch_id].get('username', 'unknown')}] [批次:{batch_id}] 任务 {i+1} 失败: {e}")
                    print(f"批量任务 {i+1} 失败: {e}")
                    with batch_progress_lock:
                        batch_progress[batch_id]['failed'] += 1
                        batch_progress[batch_id]['logs'].append({
                            'time': datetime.now().isoformat(),
                            'message': f"✗ 任务 {i+1} 失败: {str(e)}",
                            'type': 'error'
                        })
            
            # 标记完成
            with batch_progress_lock:
                completed_count = batch_progress[batch_id]['completed']
                failed_count = batch_progress[batch_id]['failed']
                batch_progress[batch_id]['status'] = 'completed'
                batch_progress[batch_id]['end_time'] = datetime.now().isoformat()
                batch_progress[batch_id]['logs'].append({
                    'time': datetime.now().isoformat(),
                    'message': f"批量生成完成！成功: {completed_count}, 失败: {failed_count}",
                    'type': 'success'
                })
            
            app_logger.info(f"[用户:{batch_progress[batch_id].get('username', 'unknown')}] [批次:{batch_id}] 批量生成完成 - 成功: {completed_count}, 失败: {failed_count}")
            print(f"批量生成完成，批次ID: {batch_id}")
        
        # 在后台线程启动处理
        thread = threading.Thread(target=process_batch, daemon=True)
        thread.start()
        
        return jsonify({
            'success': True,
            'message': '批量任务已在后台启动',
            'batch_id': batch_id,
            'total_tasks': len(tasks)
        })
    
    except Exception as e:
        username = session.get('username', 'unknown')
        app_logger.error(f"[用户:{username}] 批量生成启动失败: {e}", exc_info=True)
        print(f"批量生成启动失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

def process_single_batch_task(task, batch_id, user_id):
    """处理单个批量任务"""
    try:
        prompt = task.get('prompt', '').strip()
        negative_prompt = task.get('negative_prompt', '').strip()
        aspect_ratio = task.get('aspect_ratio', '1:1')
        resolution = task.get('resolution', '2k')
        sample_images_data = task.get('sample_images', [])
        num_images = int(task.get('num_images', 1))
        filename_base = task.get('filename', 'batch')
        
        if not prompt:
            return {'success': False, 'error': '缺少提示词'}
        
        # 获取尺寸
        if aspect_ratio in ASPECT_RATIOS and resolution in ASPECT_RATIOS[aspect_ratio]:
            width, height = ASPECT_RATIOS[aspect_ratio][resolution]
        else:
            width, height = 2048, 2048
        
        # 准备示例图 URL
        image_urls = [img['url'] for img in sample_images_data if 'url' in img]
        
        # 获取方舟大模型 API Key
        api_key = os.environ.get('ARK_API_KEY')
        base_url = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        
        if not api_key:
            return {'success': False, 'error': 'ARK_API_KEY 未配置'}
        
        # 初始化 OpenAI 客户端
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        # 生成图片
        for i in range(num_images):
            per_seed = random.randint(1, 99999999)
            
            # 构建提示词
            full_prompt = prompt
            if negative_prompt:
                full_prompt = f"{prompt}\n负面词: {negative_prompt}"
            
            # 根据分辨率映射到方舟大模型支持的size格式
            size_map = {
                '1024x1024': '1K',
                '2048x2048': '2K',
                '1536x1536': '2K',
            }
            ark_size = size_map.get(f"{width}x{height}", "2K")
            
            try:
                response = client.images.generate(
                    model="doubao-seedream-4-5-251128",
                    prompt=full_prompt,
                    size=ark_size,
                    response_format="url",
                    extra_body={
                        "watermark": False,
                    }
                )
                
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    
                    # 下载图片
                    import requests
                    img_response = requests.get(img_url)
                    if img_response.status_code == 200:
                        img_data = img_response.content
                    
                        if num_images > 1:
                            filename = f"{filename_base}_{i+1}.jpg"
                        else:
                            filename = f"{filename_base}.jpg"
                        
                        # 使用用户专属输出目录
                        user_output_folder = os.path.join('output', str(user_id))
                        os.makedirs(user_output_folder, exist_ok=True)
                        filepath = os.path.join(user_output_folder, filename)
                        with open(filepath, 'wb') as f:
                            f.write(img_data)
                        
                        # 上传到 OSS
                        oss_url = upload_to_aliyun_oss(filepath)
                        
                        # 保存记录
                        if oss_url:
                            database.save_generation_record({
                                'user_id': user_id,
                                'prompt': prompt,
                                'negative_prompt': negative_prompt,
                                'aspect_ratio': aspect_ratio,
                                'resolution': resolution,
                                'width': width,
                                'height': height,
                                'num_images': 1,
                                'seed': per_seed,
                                'steps': 28,
                                'sample_images': sample_images_data,
                                'image_path': oss_url,
                                'filename': filename,
                                'batch_id': batch_id,
                                'status': 'success'
                            })
            except Exception as e:
                print(f"生成第 {i+1} 张图片时出错: {e}")
                continue
        
        return {'success': True}
    
    except Exception as e:
        print(f"处理单个任务失败: {e}")
        return {'success': False, 'error': str(e)}

@app.route('/api/single-generation-status/<task_id>', methods=['GET'])
@login_required
def get_single_generation_status(task_id):
    """查询单图生成任务状态"""
    user_id = session.get('user_id')
    username = session.get('username', 'unknown')
    
    with single_generation_lock:
        if task_id in single_generation_tasks:
            # 任务在内存中
            # 验证任务属于当前用户
            if single_generation_tasks[task_id].get('user_id') != user_id:
                app_logger.warning(f"[用户:{username}] 尝试访问其他用户的任务: {task_id}")
                return jsonify({'success': False, 'error': '无权访问此任务'}), 403
            
            task = single_generation_tasks[task_id].copy()
            task_status = task.get('status')
            app_logger.debug(f"[用户:{username}] 查询任务状态: {task_id} - {task_status}")
            
            # 如果任务状态是generating，但已经过了较长时间（超过30秒），检查数据库是否有记录
            if task_status == 'generating':
                start_time_str = task.get('start_time')
                if start_time_str:
                    try:
                        if 'T' in start_time_str:
                            start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                        else:
                            start_time = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
                        
                        elapsed = (datetime.now() - start_time.replace(tzinfo=None)).total_seconds()
                        
                        # 如果任务开始超过30秒，检查数据库是否有记录（图片生成通常很快）
                        if elapsed > 30:  # 30秒
                            records = database.get_all_records(user_id, limit=10, offset=0)
                            if records:
                                # 检查是否有在任务开始时间之后创建的记录
                                for record in records:
                                    try:
                                        if 'T' in record['created_at']:
                                            record_time = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
                                        else:
                                            record_time = datetime.strptime(record['created_at'], '%Y-%m-%d %H:%M:%S')
                                        
                                        # 如果记录时间在任务开始时间之后，且时间差合理（不超过10分钟），认为任务已完成
                                        record_elapsed = (record_time.replace(tzinfo=None) - start_time.replace(tzinfo=None)).total_seconds()
                                        if record_elapsed >= 0 and record_elapsed < 600:  # 在任务开始后10分钟内
                                            app_logger.info(f"[用户:{username}] 任务 {task_id} 状态为generating但检测到数据库记录（{int(record_elapsed)}秒后），更新为已完成")
                                            # 更新内存中的任务状态
                                            task['status'] = 'completed'
                                            task['end_time'] = record['created_at']
                                            task['result'] = {
                                                'images_count': 1,
                                                'filenames': [record['filename']]
                                            }
                                            single_generation_tasks[task_id] = task
                                            # 更新task变量，确保返回最新状态
                                            task = task.copy()
                                            break
                                    except Exception as e:
                                        app_logger.error(f"[用户:{username}] 解析记录时间失败: {e}")
                                        continue
                    except Exception as e:
                        app_logger.error(f"[用户:{username}] 检查任务完成状态失败: {e}", exc_info=True)
            
            # 确保返回更新后的任务状态
            final_task = single_generation_tasks.get(task_id, task)
            app_logger.debug(f"[用户:{username}] 返回任务状态: {task_id} - {final_task.get('status')}")
            return jsonify({
                'success': True,
                'task': final_task.copy() if isinstance(final_task, dict) else final_task
            })
        else:
            # 任务不在内存中，可能是服务重启或任务已过期
            # 检查数据库中是否有最近生成的记录（10分钟内）
            try:
                from datetime import timedelta
                records = database.get_all_records(user_id, limit=10, offset=0)
                
                # 检查是否有最近生成的记录
                if records:
                    latest_record = records[0]
                    # 处理不同的时间格式
                    try:
                        if 'T' in latest_record['created_at']:
                            # ISO格式: 2024-01-01T12:00:00
                            record_time = datetime.fromisoformat(latest_record['created_at'].replace('Z', '+00:00'))
                        else:
                            # SQLite格式: 2024-01-01 12:00:00
                            record_time = datetime.strptime(latest_record['created_at'], '%Y-%m-%d %H:%M:%S')
                    except:
                        # 如果解析失败，尝试其他格式
                        try:
                            record_time = datetime.strptime(latest_record['created_at'], '%Y-%m-%d %H:%M:%S.%f')
                        except:
                            record_time = datetime.now()
                    
                    time_diff = (datetime.now() - record_time.replace(tzinfo=None)).total_seconds()
                    
                    # 如果记录在10分钟内，认为任务可能已完成
                    if time_diff < 600:  # 10分钟
                        app_logger.info(f"[用户:{username}] 任务 {task_id} 不在内存中，但检测到最近有生成记录（{int(time_diff)}秒前），标记为已完成")
                        # 返回一个完成状态的任务对象
                        return jsonify({
                            'success': True,
                            'task': {
                                'user_id': user_id,
                                'username': username,
                                'status': 'completed',
                                'start_time': latest_record['created_at'],
                                'end_time': latest_record['created_at'],
                                'result': {
                                    'images_count': 1,
                                    'filenames': [latest_record['filename']]
                                }
                            }
                        })
            except Exception as e:
                app_logger.error(f"[用户:{username}] 检查数据库记录失败: {e}", exc_info=True)
            
            # 如果找不到相关记录，返回任务不存在
            app_logger.warning(f"[用户:{username}] 任务不存在: {task_id}")
            return jsonify({'success': False, 'error': '任务ID不存在或已过期'}), 404

@app.route('/api/batch-progress/<batch_id>', methods=['GET'])
@login_required
def get_batch_progress(batch_id):
    """查询批量任务进度"""
    user_id = session.get('user_id')
    with batch_progress_lock:
        if batch_id not in batch_progress:
            return jsonify({'success': False, 'error': '批次ID不存在'}), 404
        
        # 验证批次属于当前用户
        if batch_progress[batch_id].get('user_id') != user_id:
            return jsonify({'success': False, 'error': '无权访问此批次'}), 403
        
        progress = batch_progress[batch_id].copy()
        # 只返回最近100条日志
        if len(progress['logs']) > 100:
            progress['logs'] = progress['logs'][-100:]
        
        return jsonify({
            'success': True,
            'progress': progress
        })

@app.route('/output/<int:user_id>/<filename>')
@login_required
def output_file(user_id, filename):
    # 确保用户只能访问自己的文件
    if session.get('user_id') != user_id:
        return '403 Forbidden', 403
    user_output_folder = get_user_output_folder(user_id)
    return send_from_directory(user_output_folder, filename)

@app.route('/favicon.ico')
def favicon():
    return '', 204  # 返回空响应，避免 404

@app.route('/api/analyze-script', methods=['POST'])
@login_required
def analyze_script():
    """使用火山引擎大模型分析剧本，拆解人物和分镜场景"""
    user_id = session.get('user_id')
    username = session.get('username', 'unknown')
    
    try:
        data = request.get_json() or {}
        script = data.get('script', '').strip()
        
        app_logger.info(f"[用户:{username}] ========== 开始剧本分析 ==========")
        app_logger.info(f"[用户:{username}] 输入剧本长度: {len(script)} 字符")
        app_logger.info(f"[用户:{username}] 输入剧本内容（前500字符）: {script[:500]}")
        
        if not script:
            app_logger.warning(f"[用户:{username}] 剧本分析失败 - 缺少输入文本")
            return jsonify({'success': False, 'error': '请输入剧本文本'}), 400
        
        # 获取火山引擎 API Key
        api_key = os.environ.get('ARK_API_KEY')
        base_url = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        model = os.environ.get('SCRIPT_ANALYSIS_MODEL', 'doubao-seed-1-8-251215')
        
        if not api_key:
            app_logger.error(f"[用户:{username}] 剧本分析失败 - ARK_API_KEY 未配置")
            return jsonify({'success': False, 'error': 'ARK_API_KEY 未配置'}), 500
        
        app_logger.info(f"[用户:{username}] API配置 - Base URL: {base_url}, Model: {model}")
        
        # 初始化 OpenAI 兼容客户端
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        # 构建分析提示词
        analysis_prompt = f"""假如你是一位知名导演，现需要拍摄一部极具吸引力的短片，具体要求如下：

一、主角设定：

根据用户描述全方位精细塑造主角IP形象，包括姓名、年龄、性别、外貌特征、服饰风格及细节，力求打造独特且极具魅力的角色。

二、台词独白：创作全新台词独白，要有感染力，简洁直白，符合人物特征，字数控制在100字左右

三、分镜场景：依据创作的台词独白智能设计分镜场景，确保台词独白的每个字都能在分镜场景中得到展示。每个场景必需生成一套完整的提示词，符合{{}}提示词结构描述，描述要求详细，使用中文输出。

1. 场景台词：明确每个场景对应的台词
2. 提示词结构：包含拍摄景别、人物景别、视角、构图、核心主体、情绪动作、环境场景、艺术风格、氛围光线、色调等，内容描述要细致到能让AI绘图准确捕捉画面细节，产出高质量图像。
3. 运镜语言：具体说明推、拉、摇、移、跟等运镜方式在每个场景中的运用时机和预期效果
4. 音效：包括环境音（街道嘈杂声、咖啡馆的背景音乐等）、背景音乐的淡入淡出等音效设计。

四、风格色调：突出用户描述的独特风格，从参考风格（给出3条参考风格关键词）、画面质感、色调氛围等方面，用中文详细深入描述。

五、背景音乐：推荐与视频风格高度适配的背景音乐，并阐述选择理由，以更好的烘托视频氛围。

用户描述：
{script}

请以JSON格式返回结果（不要包含markdown代码块，直接返回JSON）：
{{
  "protagonist": {{
    "name": "主角姓名",
    "age": "年龄",
    "gender": "性别",
    "appearance": "外貌特征详细描述",
    "clothing": "服饰风格及细节描述"
  }},
  "monologue": "创作的台词独白（100字左右）",
  "scenes": [
    {{
      "scene_number": 1,
      "dialogue": "该场景对应的台词",
      "prompt_structure": {{
        "shot_type": "拍摄景别",
        "character_shot": "人物景别",
        "perspective": "视角",
        "composition": "构图",
        "core_subject": "核心主体",
        "emotion_action": "情绪动作",
        "environment": "环境场景",
        "art_style": "艺术风格",
        "atmosphere_lighting": "氛围光线",
        "color_tone": "色调"
      }},
      "camera_movement": "运镜语言（推、拉、摇、移、跟等）",
      "sound_effects": "音效设计（环境音、背景音乐等）"
    }}
  ],
  "style_tone": {{
    "reference_styles": ["参考风格关键词1", "参考风格关键词2", "参考风格关键词3"],
    "visual_texture": "画面质感描述",
    "color_atmosphere": "色调氛围详细描述"
  }},
  "background_music": {{
    "recommendation": "推荐的背景音乐",
    "reason": "选择理由"
  }}
}}

要求：
1. 仔细分析用户描述，塑造独特的主角IP形象
2. 创作符合人物特征的台词独白，控制在100字左右
3. 按照台词独白智能设计分镜场景，确保每个字都能在场景中得到展示
4. 每个场景的提示词结构要详细完整，便于AI绘图生成高质量图像
5. 详细描述运镜语言和音效设计
6. 提供3条参考风格关键词和详细的风格色调描述
7. 推荐合适的背景音乐并说明理由
8. 返回有效的JSON格式"""
        
        # 记录API请求
        api_request_info = {
            'model': model,
            'prompt_length': len(analysis_prompt),
            'temperature': 0.7,
            'top_p': 0.9
        }
        app_logger.info(f"[用户:{username}] 调用API进行剧本分析")
        app_logger.info(f"[用户:{username}] API请求参数: {json.dumps(api_request_info, ensure_ascii=False)}")
        app_logger.info(f"[用户:{username}] 提示词内容（前1000字符）: {analysis_prompt[:1000]}")
        
        # 调用火山引擎大模型
        api_start_time = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": analysis_prompt
                }
            ],
            temperature=0.7,
            top_p=0.9
        )
        api_duration = time.time() - api_start_time
        app_logger.info(f"[用户:{username}] API响应时间: {api_duration:.2f}秒")
        
        # 提取模型返回的内容
        if response.choices and len(response.choices) > 0:
            content = response.choices[0].message.content.strip()
            
            # 尝试解析 JSON
            try:
                # 移除可能的 markdown 代码块包装
                if content.startswith('```'):
                    content = content.split('```')[1]
                    if content.startswith('json'):
                        content = content[4:]
                    content = content.strip()
                if content.endswith('```'):
                    content = content[:-3].strip()
                
                result = json.loads(content)
                
                # 记录解析结果摘要
                result_summary = {
                    'has_protagonist': 'protagonist' in result and bool(result.get('protagonist')),
                    'has_monologue': 'monologue' in result and bool(result.get('monologue')),
                    'scenes_count': len(result.get('scenes', [])),
                    'has_style_tone': 'style_tone' in result and bool(result.get('style_tone')),
                    'has_background_music': 'background_music' in result and bool(result.get('background_music'))
                }
                app_logger.info(f"[用户:{username}] JSON解析成功")
                app_logger.info(f"[用户:{username}] 解析结果摘要: {json.dumps(result_summary, ensure_ascii=False)}")
                
                # 记录详细结果（只记录关键信息，避免日志过长）
                if result.get('protagonist'):
                    prot = result['protagonist']
                    app_logger.info(f"[用户:{username}] 主角设定: {prot.get('name', '未命名')} - {prot.get('age', '')}岁 {prot.get('gender', '')}")
                
                if result.get('monologue'):
                    monologue_preview = result['monologue'][:100]
                    app_logger.info(f"[用户:{username}] 台词独白（前100字符）: {monologue_preview}")
                
                if result.get('scenes'):
                    app_logger.info(f"[用户:{username}] 分镜场景数量: {len(result['scenes'])}")
                    for idx, scene in enumerate(result['scenes'][:3]):  # 只记录前3个场景
                        app_logger.info(f"[用户:{username}] 场景 {idx + 1}: {scene.get('dialogue', '')[:50]}")
                
                # 验证结构
                if 'protagonist' not in result:
                    result['protagonist'] = {}
                if 'monologue' not in result:
                    result['monologue'] = ''
                if 'scenes' not in result:
                    result['scenes'] = []
                if 'style_tone' not in result:
                    result['style_tone'] = {}
                if 'background_music' not in result:
                    result['background_music'] = {}
                
                app_logger.info(f"[用户:{username}] ========== 剧本分析完成 ==========")
                
                return jsonify({
                    'success': True,
                    'result': result
                })
            except json.JSONDecodeError as e:
                app_logger.error(f"[用户:{username}] JSON 解析失败: {e}")
                app_logger.error(f"[用户:{username}] 返回内容（前1000字符）: {content[:1000]}")
                print(f"JSON 解析失败: {e}")
                print(f"返回内容: {content}")
                # 返回原始内容作为错误提示
                return jsonify({
                    'success': False,
                    'error': f'模型返回格式错误: {str(e)}'
                }), 500
        else:
            app_logger.error(f"[用户:{username}] 模型未返回内容")
            return jsonify({
                'success': False,
                'error': '模型未返回内容'
            }), 500
    
    except Exception as e:
        app_logger.error(f"[用户:{username}] 剧本分析失败: {e}", exc_info=True)
        print(f"剧本分析失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'分析失败: {str(e)}'
        }), 500

if __name__ == '__main__':
    print("启动 Web 应用...")
    print(f"访问地址: http://localhost:5050")
    app.run(debug=True, host='0.0.0.0', port=5050)
