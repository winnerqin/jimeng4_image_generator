import os
import json
import base64
import random
import uuid
import threading
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
from openai import OpenAI
import database

# 全局变量：存储批量任务进度
batch_progress = {}
batch_progress_lock = threading.Lock()

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
    try:
        user_id = session.get('user_id')
        # 获取表单数据
        prompt = request.form.get('prompt', '').strip()
        negative_prompt = request.form.get('negative_prompt', '').strip()
        aspect_ratio = request.form.get('aspect_ratio', '1:1')
        resolution = request.form.get('resolution', '2k')
        num_images = int(request.form.get('num_images', 1))
        output_filename = request.form.get('output_filename', 'generated').strip()
        steps = int(request.form.get('steps', 28))
        seed = int(request.form.get('seed', 0))
        
        if not prompt:
            return jsonify({'error': '请输入提示词'}), 400
        
        # 获取尺寸
        if aspect_ratio in ASPECT_RATIOS and resolution in ASPECT_RATIOS[aspect_ratio]:
            width, height = ASPECT_RATIOS[aspect_ratio][resolution]
        else:
            width, height = 2048, 2048
        
        # 处理上传的图片和示例图
        image_urls = []
        
        # 先添加从OSS选择的示例图URL
        sample_image_urls = request.form.getlist('sample_image_urls')
        if sample_image_urls:
            image_urls.extend(sample_image_urls)
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
                        print(f"成功上传图片到阿里云 OSS: {oss_url}")
                    else:
                        print(f"警告：阿里云 OSS 上传失败，跳过图片 {filename}")
                else:
                    # 如果没有配置 OSS，保存文件但不添加到 image_urls
                    # 这样可以保留上传的文件，但不会导致 API 错误
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
            
            # 调用方舟大模型生成图片
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
                
                # 处理响应
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    
                    # 下载图片
                    import requests
                    img_response = requests.get(img_url)
                    if img_response.status_code == 200:
                        img_data = img_response.content
                        
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
                            print(f"保存记录失败: {db_err}")
                else:
                    print(f"API 返回错误: 无法获取图片")
            except Exception as e:
                print(f"生成第 {i+1} 张图片时出错: {e}")
                continue
        
        if not generated_images:
            return jsonify({'error': '图片生成失败，请检查参数'}), 500
        
        return jsonify({
            'success': True,
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
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

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
        data = request.json
        tasks = data.get('tasks', [])
        
        if not tasks:
            return jsonify({'success': False, 'error': '没有任务'}), 400
        
        # 生成批次ID
        batch_id = str(uuid.uuid4())
        
        # 初始化进度
        with batch_progress_lock:
            batch_progress[batch_id] = {
                'user_id': user_id,
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
                    with batch_progress_lock:
                        batch_progress[batch_id]['logs'].append({
                            'time': datetime.now().isoformat(),
                            'message': f"开始任务 {i+1}/{len(tasks)}: {task.get('prompt', '')[:30]}...",
                            'type': 'info'
                        })
                    
                    # 调用原有的批量生成逻辑
                    result = process_single_batch_task(task, batch_id, user_id)
                    
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
                    
                    print(f"批量任务进度: {i+1}/{len(tasks)}")
                except Exception as e:
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
                batch_progress[batch_id]['status'] = 'completed'
                batch_progress[batch_id]['end_time'] = datetime.now().isoformat()
                batch_progress[batch_id]['logs'].append({
                    'time': datetime.now().isoformat(),
                    'message': f"批量生成完成！成功: {batch_progress[batch_id]['completed']}, 失败: {batch_progress[batch_id]['failed']}",
                    'type': 'success'
                })
            
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
    try:
        data = request.get_json() or {}
        script = data.get('script', '').strip()
        
        if not script:
            return jsonify({'success': False, 'error': '请输入剧本文本'}), 400
        
        # 获取火山引擎 API Key
        api_key = os.environ.get('ARK_API_KEY')
        base_url = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        
        if not api_key:
            return jsonify({'success': False, 'error': 'ARK_API_KEY 未配置'}), 500
        
        # 初始化 OpenAI 兼容客户端
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        # 构建分析提示词
        analysis_prompt = f"""请分析以下剧本文本，并以JSON格式输出结果。

剧本文本：
{script}

请提取以下信息，并以JSON格式返回（不要包含markdown代码块，直接返回JSON）：
{{
  "characters": [
    {{
      "name": "人物名称",
      "description": "人物设定、性格、背景等描述"
    }}
  ],
  "scenes": [
    {{
      "location": "场景位置",
      "description": "场景描述、动作、对话等",
      "characters": ["出场人物1", "出场人物2"],
      "setting": "场景视觉设定、布景、灯光等"
    }}
  ]
}}

要求：
1. 仔细识别所有人物及其设定
2. 按照剧本顺序拆解分镜场景
3. 为每个场景提供清晰的视觉描述，便于AI图片生成
4. 返回有效的JSON格式"""
        
        # 调用火山引擎大模型
        response = client.chat.completions.create(
            model=os.environ.get('SCRIPT_ANALYSIS_MODEL', 'doubao-seed-1-8-251215'),
            messages=[
                {
                    "role": "user",
                    "content": analysis_prompt
                }
            ],
            temperature=0.7,
            top_p=0.9
        )
        
        # 提取模型返回的内容
        if response.choices and len(response.choices) > 0:
            content = response.choices[0].message.content.strip()
            
            # 尝试解析 JSON
            import json
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
                
                # 验证结构
                if 'characters' not in result:
                    result['characters'] = []
                if 'scenes' not in result:
                    result['scenes'] = []
                
                return jsonify({
                    'success': True,
                    'result': result
                })
            except json.JSONDecodeError as e:
                print(f"JSON 解析失败: {e}")
                print(f"返回内容: {content}")
                # 返回原始内容作为错误提示
                return jsonify({
                    'success': False,
                    'error': f'模型返回格式错误: {str(e)}'
                }), 500
        else:
            return jsonify({
                'success': False,
                'error': '模型未返回内容'
            }), 500
    
    except Exception as e:
        print(f"剧本分析失败: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'分析失败: {str(e)}'
        }), 500

if __name__ == '__main__':
    print("启动 Web 应用...")
    print(f"访问地址: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
