import os
import json
import base64
import random
import uuid
import threading
import logging
import string
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for, flash
from werkzeug.utils import secure_filename
from openai import OpenAI
import database

def generate_random_filename(length=8):
    """生成指定长度的随机文件名（仅包含字母和数字）"""
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

# ==================== 日志工具 ====================
# 配置日志记录
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def log_operation(operation, details=None, level='INFO'):
    """记录操作日志"""
    msg = f"[操作] {operation}"
    if details:
        msg += f" | {details}"
    if level == 'ERROR':
        logger.error(msg)
    elif level == 'WARNING':
        logger.warning(msg)
    else:
        logger.info(msg)

def log_request(method, endpoint, user_id=None, params=None):
    """记录请求"""
    msg = f"[请求] {method} {endpoint}"
    if user_id:
        msg += f" | 用户: {user_id}"
    if params:
        msg += f" | 参数: {params}"
    logger.info(msg)

def log_response(endpoint, status, message=None):
    """记录响应"""
    msg = f"[响应] {endpoint} | 状态: {status}"
    if message:
        msg += f" | {message}"
    logger.info(msg)

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

# 尺寸比例到像素的映射（根据方舟大模型2K/4K标准）
ASPECT_RATIOS = {
    '1:1': {'2k': (2048, 2048), '4k': (4096, 4096)},
    '4:3': {'2k': (2560, 1920), '4k': (3840, 2880)},
    '3:4': {'2k': (1920, 2560), '4k': (2880, 3840)},
    '16:9': {'2k': (2560, 1920), '4k': (3840, 2160)},
    '9:16': {'2k': (1440, 2560), '4k': (2160, 3840)},
    '3:2': {'2k': (2560, 1706), '4k': (3840, 2560)},
    '2:3': {'2k': (1706, 2560), '4k': (2560, 3840)},
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
        
        log_request('POST', '/login', params=f'username={username}')
        
        if not username or not password:
            log_operation('登录失败', '缺少用户名或密码', 'WARNING')
            return render_template('login.html', error='请输入用户名和密码')
        
        user = database.verify_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            log_operation('用户登录成功', f'用户名: {username}, 用户ID: {user["id"]}')
            return redirect(url_for('index'))
        else:
            log_operation('登录失败', f'用户名: {username}, 密码错误', 'WARNING')
            return render_template('login.html', error='用户名或密码错误')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # 注册功能已禁用，请联系管理员创建账号
    log_operation('注册尝试被拒绝', '注册功能已关闭', 'WARNING')
    return render_template('login.html', error='注册功能已关闭，请联系管理员获取账号')

@app.route('/logout')
def logout():
    user = get_current_user()
    user_info = f"用户ID: {session.get('user_id')}, 用户名: {session.get('username')}" if user else "未知用户"
    log_operation('用户登出', user_info)
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
        aspect_ratio = request.form.get('aspect_ratio', '1:1')
        resolution = request.form.get('resolution', '2k')
        num_images = int(request.form.get('num_images', 1))
        sequential_count = int(request.form.get('sequential_count', 1))
        image_style = request.form.get('image_style', '').strip()
        seed = int(request.form.get('seed', 0))
        
        log_request('POST', '/generate', user_id, 
                   f'提示词长度={len(prompt)}, 数量={num_images}, 尺寸={aspect_ratio}/{resolution}')
        
        if not prompt:
            log_operation('生成失败', f'用户ID: {user_id}, 原因: 缺少提示词', 'WARNING')
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
        # 如果使用组图生成，只调用一次API；否则按num_images循环
        total_needed = 1 if sequential_count > 1 else num_images
        
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
            
            # 如果选择了风格，将风格的prompt合并到提示词中
            if image_style:
                try:
                    styles_file = os.path.join('static', 'styles.json')
                    if os.path.exists(styles_file):
                        with open(styles_file, 'r', encoding='utf-8') as f:
                            styles_data = json.load(f)
                            style_obj = next((s for s in styles_data.get('styles', []) if s.get('id') == image_style), None)
                            if style_obj:
                                style_prompt = style_obj.get('prompt', '')
                                if style_prompt:
                                    # 将风格prompt合并到用户提示词中
                                    full_prompt = f"{full_prompt}, {style_prompt}"
                                    print(f"[风格合并] 原始提示词: {prompt}")
                                    print(f"[风格合并] 风格: {style_obj.get('name', image_style)}")
                                    print(f"[风格合并] 风格prompt: {style_prompt}")
                                    print(f"[风格合并] 合并后提示词: {full_prompt}")
                except Exception as e:
                    print(f"[风格合并] 加载风格失败: {e}")
                    import traceback
                    traceback.print_exc()
            
            # 调用方舟大模型生成图片
            # 使用 size 参数，格式为 "widthxheight"，如 "1440x2560"
            # 支持使用参考图片（OSS示例图或用户上传的图片）
            try:
                # 构建 size 参数
                size_str = f"{width}x{height}"
                
                # 构建 extra_body，包含参考图片
                extra_body_params = {
                    "watermark": False,  # 默认不添加水印
                }
                
                # 添加组图生成参数（仅在第一次生成时添加，因为组图功能会一次性生成多张）
                if i == 0 and sequential_count > 1:
                    extra_body_params["sequential_image_generation"] = "auto"
                    extra_body_params["sequential_image_generation_options"] = {
                        "max_images": sequential_count
                    }
                
                # 添加参考图片参数
                if image_urls:
                    if len(image_urls) == 1:
                        # 单张图片使用 image 参数
                        extra_body_params["image"] = image_urls[0]
                    else:
                        # 多张图片使用 image_urls 参数（如果API支持）
                        extra_body_params["image_urls"] = image_urls
                        # 或者使用第一张图片作为主要参考图
                        extra_body_params["image"] = image_urls[0]
                
                # ========== 记录输入内容 ==========
                input_details = {
                    'model': 'doubao-seedream-4-5-251128',
                    'prompt': full_prompt[:200] + '...' if len(full_prompt) > 200 else full_prompt,
                    'size': size_str,
                    'width': width,
                    'height': height,
                    'aspect_ratio': aspect_ratio,
                    'resolution': resolution,
                    'seed': per_seed,
                    'image_style': image_style,
                    'num_images': 1,
                    'sequential_count': sequential_count,
                    'reference_images_count': len(image_urls),
                    'reference_images': image_urls[:3] if len(image_urls) > 3 else image_urls  # 只记录前3张
                }
                
                log_operation('API调用输入', f'用户ID={user_id}, 第{i+1}张 | {json.dumps(input_details, ensure_ascii=False, indent=2)}')
                print("=" * 80)
                print(f"[输入] 第 {i+1}/{total_needed} 张图片生成请求:")
                print(f"  模型: {input_details['model']}")
                print(f"  原始提示词: {prompt}")
                print(f"  合并后提示词: {full_prompt}")
                print(f"  尺寸: {size_str} ({width}x{height})")
                print(f"  宽高比: {aspect_ratio}, 分辨率: {resolution}")
                print(f"  种子值: {per_seed}, 风格: {image_style if image_style else '无'}")
                if sequential_count > 1:
                    print(f"  组图数量: {sequential_count}")
                print(f"  参考图数量: {len(image_urls)}")
                if image_urls:
                    print(f"  参考图URL: {image_urls}")
                print("=" * 80)
                
                response = client.images.generate(
                    model="doubao-seedream-4-5-251128",
                    prompt=full_prompt,
                    size=size_str,
                    response_format="url",
                    extra_body=extra_body_params
                )
                
                # ========== 记录输出内容 ==========
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    
                    output_details = {
                        'status': 'success',
                        'generated_image_url': img_url,
                        'response_data_count': len(response.data),
                        'model': 'doubao-seedream-4-5-251128'
                    }
                    
                    log_operation('API调用输出', f'用户ID={user_id}, 第{i+1}张 | {json.dumps(output_details, ensure_ascii=False, indent=2)}')
                    print("=" * 80)
                    print(f"[输出] 第 {i+1}/{total_needed} 张图片生成响应:")
                    print(f"  状态: 成功")
                    print(f"  生成图片URL: {img_url}")
                    print(f"  响应数据数量: {len(response.data)}")
                    print("=" * 80)
                else:
                    log_operation('API调用输出', f'用户ID={user_id}, 第{i+1}张 | 状态=失败, 响应数据为空', 'WARNING')
                    print(f"[输出] 第 {i+1}/{total_needed} 张图片生成响应: 失败 - 响应数据为空")
                
                # 处理响应（组图生成可能返回多张图片）
                if response.data and len(response.data) > 0:
                    # 如果使用组图生成，处理所有返回的图片
                    images_to_process = response.data if (i == 0 and sequential_count > 1) else [response.data[0]]
                    
                    for img_idx, img_data_obj in enumerate(images_to_process):
                        img_url = img_data_obj.url
                        
                        # 下载图片
                        import requests
                        img_response = requests.get(img_url)
                        if img_response.status_code == 200:
                            img_data = img_response.content
                            
                            # 生成8位随机文件名，避免重复
                            random_name = generate_random_filename(8)
                            if sequential_count > 1:
                                filename = f"{random_name}_{img_idx+1}.jpg"
                            elif num_images > 1:
                                filename = f"{random_name}_{i+1}.jpg"
                            else:
                                filename = f"{random_name}.jpg"
                        
                        # 使用用户专属输出目录
                        user_output_folder = get_user_output_folder(user_id)
                        output_path = os.path.join(user_output_folder, filename)
                        with open(output_path, 'wb') as f:
                            f.write(img_data)
                        
                        # 如果配置了OSS，上传到OSS并获取公共URL（用于作为参考图）
                        image_url = f'/output/{user_id}/{filename}'  # 默认使用本地URL
                        if oss_enabled:
                            oss_url = upload_to_aliyun_oss(output_path, user_id=user_id)
                            if oss_url:
                                image_url = oss_url
                                log_operation('OSS上传成功', f'用户ID={user_id}, 文件={filename}, OSS_URL={oss_url}')
                                print(f"[OSS] 成功上传生成的图片到OSS: {oss_url}")
                            else:
                                log_operation('OSS上传失败', f'用户ID={user_id}, 文件={filename}', 'WARNING')
                                print(f"[OSS] 警告：生成的图片上传到OSS失败，使用本地URL")
                        
                            # 记录最终输出结果
                            final_output = {
                                'filename': filename,
                                'local_path': output_path,
                                'final_url': image_url,
                                'file_size': len(img_data),
                                'seed': per_seed
                            }
                            img_index = img_idx + 1 if sequential_count > 1 else i + 1
                            log_operation('图片处理完成', f'用户ID={user_id}, 第{img_index}张 | {json.dumps(final_output, ensure_ascii=False)}')
                            print(f"[完成] 第 {img_index} 张图片处理完成:")
                            print(f"  文件名: {filename}")
                            print(f"  本地路径: {output_path}")
                            print(f"  最终URL: {image_url}")
                            print(f"  文件大小: {len(img_data)} bytes")
                            print(f"  种子值: {per_seed}")
                        
                            generated_images.append({
                                'filename': filename,
                                'url': image_url,
                                'seed': per_seed
                            })
                            
                            # 保存记录到数据库
                            try:
                                sample_images_list = [{'url': url, 'filename': os.path.basename(url)} for url in image_urls]
                                database.save_generation_record({
                                    'user_id': user_id,
                                    'prompt': prompt,
                                    'aspect_ratio': aspect_ratio,
                                    'resolution': resolution,
                                    'width': width,
                                    'height': height,
                                'num_images': 1,
                                'seed': per_seed,
                                'image_style': image_style,
                                'sample_images': sample_images_list,
                                'image_path': image_url,  # 使用image_url（可能是OSS URL或本地URL）
                                    'filename': filename,
                                    'status': 'success'
                                })
                            except Exception as db_err:
                                log_operation('保存记录失败', f'用户ID={user_id}, 文件={filename}, 错误={str(db_err)}', 'WARNING')
                                print(f"[警告] 保存记录失败: {db_err}")
                        else:
                            log_operation('下载图片失败', f'用户ID={user_id}, URL={img_url}, 状态码={img_response.status_code}', 'WARNING')
                            print(f"[警告] 下载图片失败: HTTP {img_response.status_code}")
                else:
                    log_operation('API返回错误', f'用户ID={user_id}, 第{i+1}张 | 无法获取图片', 'WARNING')
                    print(f"[警告] API 返回错误: 无法获取图片")
            except Exception as e:
                error_details = {
                    'error': str(e),
                    'error_type': type(e).__name__,
                    'image_index': i+1,
                    'total': total_needed
                }
                log_operation('生成图片失败', f'用户ID={user_id}, 第{i+1}张 | {json.dumps(error_details, ensure_ascii=False)}', 'ERROR')
                print("=" * 80)
                print(f"[错误] 第 {i+1}/{total_needed} 张图片生成失败:")
                print(f"  错误类型: {type(e).__name__}")
                print(f"  错误信息: {str(e)}")
                import traceback
                print(f"  错误详情:\n{traceback.format_exc()}")
                print("=" * 80)
                continue
        
        if not generated_images:
            log_operation('生成失败', f'用户ID: {user_id}, 原因: 无法生成图片', 'ERROR')
            print("=" * 80)
            print(f"[最终结果] 生成失败 - 没有成功生成任何图片")
            print("=" * 80)
            return jsonify({'error': '图片生成失败，请检查参数'}), 500
        
        # 记录最终生成结果
        final_result = {
            'user_id': user_id,
            'total_requested': num_images,
            'total_generated': len(generated_images),
            'prompt_preview': prompt[:100] + '...' if len(prompt) > 100 else prompt,
            'generated_files': [{'filename': img['filename'], 'url': img['url']} for img in generated_images]
        }
        log_operation('图片生成成功', f'用户ID={user_id} | {json.dumps(final_result, ensure_ascii=False, indent=2)}')
        print("=" * 80)
        print(f"[最终结果] 生成成功:")
        print(f"  请求数量: {num_images}")
        print(f"  成功生成: {len(generated_images)}")
        print(f"  提示词预览: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
        print(f"  生成的文件:")
        for img in generated_images:
            print(f"    - {img['filename']}: {img['url']}")
        print("=" * 80)
        return jsonify({
            'success': True,
            'images': generated_images,
            'params': {
                'prompt': prompt,
                'aspect_ratio': aspect_ratio,
                'resolution': resolution,
                'width': width,
                'height': height,
                'num_images': num_images,
                'image_style': image_style
            }
        })
    
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('生成异常', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        log_request('GET', '/api/sample-images', user_id, f'类别: {category or "全部"}')
        
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

        log_operation('获取示例图', f'用户ID: {user_id}, 类别: {category or "全部"}, 数量: {len(sample_images)}')
        
        return jsonify({
            'success': True,
            'images': sample_images
        })
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('获取示例图失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
        return jsonify({
            'success': False,
            'error': str(e),
            'images': []
        })

@app.route('/api/image-styles')
@login_required
def get_image_styles():
    """获取图片风格列表"""
    try:
        styles_file = os.path.join('static', 'styles.json')
        if os.path.exists(styles_file):
            with open(styles_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return jsonify({'success': True, 'styles': data.get('styles', [])})
        else:
            return jsonify({'success': False, 'error': '风格文件不存在'}), 404
    except Exception as e:
        log_operation('获取风格列表失败', f'错误: {str(e)}', 'ERROR')
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/recent-images')
@login_required
def get_recent_images():
    """获取最近生成的图片列表（用作参考图）"""
    try:
        user_id = session.get('user_id')
        limit = int(request.args.get('limit', 50))  # 默认获取最近50张
        
        log_request('GET', '/api/recent-images', user_id, f'数量: {limit}')
        
        # 从数据库获取最近生成的图片记录
        records = database.get_all_records(user_id, limit=limit, offset=0)
        
        # 提取图片信息，只返回OSS URL的图片（API可以访问的）
        recent_images = []
        for record in records:
            image_path = record.get('image_path', '')
            if image_path and image_path.startswith('https://'):
                # 只包含OSS URL的图片
                recent_images.append({
                    'url': image_path,
                    'filename': record.get('filename', 'generated.jpg'),
                    'created_at': record.get('created_at', ''),
                    'prompt': record.get('prompt', '')[:50] + '...' if len(record.get('prompt', '')) > 50 else record.get('prompt', ''),
                    'key': f"recent_{record.get('id')}",
                    'category': 'recent'
                })
        
        log_operation('获取最近生成图片', f'用户ID: {user_id}, 数量: {len(recent_images)}')
        return jsonify({'success': True, 'images': recent_images})
    
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('获取最近生成图片失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

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
        
        # 获取风格设置
        image_style = data.get('image_style', '').strip()
        
        for i in range(num_images):
            per_seed = random.randint(1, 99999999)
            
            # 构建提示词
            full_prompt = prompt
            
            # 如果选择了风格，将风格添加到提示词中
            if image_style:
                try:
                    styles_file = os.path.join('static', 'styles.json')
                    if os.path.exists(styles_file):
                        with open(styles_file, 'r', encoding='utf-8') as f:
                            styles_data = json.load(f)
                            style_obj = next((s for s in styles_data.get('styles', []) if s.get('id') == image_style), None)
                            if style_obj:
                                style_prompt = style_obj.get('prompt', '')
                                if style_prompt:
                                    full_prompt = f"{full_prompt}, {style_prompt}"
                except Exception as e:
                    print(f"加载风格失败: {e}")
            
            # 调用方舟大模型生成图片
            # 使用 size 参数，格式为 "widthxheight"，如 "1440x2560"
            # 支持使用参考图片（OSS示例图或用户上传的图片）
            try:
                # 构建 size 参数
                size_str = f"{width}x{height}"
                
                # 构建 extra_body，包含参考图片
                extra_body_params = {
                    "watermark": False,  # 默认不添加水印
                }
                
                # 添加参考图片参数
                if image_urls:
                    if len(image_urls) == 1:
                        # 单张图片使用 image 参数
                        extra_body_params["image"] = image_urls[0]
                    else:
                        # 多张图片使用 image_urls 参数（如果API支持）
                        extra_body_params["image_urls"] = image_urls
                        # 或者使用第一张图片作为主要参考图
                        extra_body_params["image"] = image_urls[0]
                
                # ========== 记录输入内容 ==========
                input_details = {
                    'model': 'doubao-seedream-4-5-251128',
                    'prompt': full_prompt[:200] + '...' if len(full_prompt) > 200 else full_prompt,
                    'size': size_str,
                    'width': width,
                    'height': height,
                    'aspect_ratio': aspect_ratio,
                    'resolution': resolution,
                    'seed': per_seed,
                    'image_style': image_style,
                    'reference_images_count': len(image_urls),
                    'reference_images': image_urls[:3] if len(image_urls) > 3 else image_urls
                }
                log_operation('批量生成API调用输入', f'用户ID={user_id}, 批次={batch_id}, 第{i+1}张 | {json.dumps(input_details, ensure_ascii=False, indent=2)}')
                print("=" * 80)
                print(f"[批量生成-输入] 批次: {batch_id}, 第 {i+1}/{num_images} 张:")
                print(f"  原始提示词: {prompt}")
                print(f"  合并后提示词: {full_prompt}")
                print(f"  尺寸: {size_str} ({width}x{height})")
                print(f"  风格: {image_style if image_style else '无'}")
                print(f"  参考图数量: {len(image_urls)}")
                print("=" * 80)
                
                response = client.images.generate(
                    model="doubao-seedream-4-5-251128",
                    prompt=full_prompt,
                    size=size_str,
                    response_format="url",
                    extra_body=extra_body_params
                )
                
                # ========== 记录输出内容 ==========
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    output_details = {
                        'status': 'success',
                        'generated_image_url': img_url,
                        'response_data_count': len(response.data)
                    }
                    log_operation('批量生成API调用输出', f'用户ID={user_id}, 批次={batch_id}, 第{i+1}张 | {json.dumps(output_details, ensure_ascii=False)}')
                    print(f"[批量生成-输出] 批次: {batch_id}, 第 {i+1}/{num_images} 张: 成功, URL={img_url}")
                else:
                    log_operation('批量生成API调用输出', f'用户ID={user_id}, 批次={batch_id}, 第{i+1}张 | 状态=失败, 响应数据为空', 'WARNING')
                    print(f"[批量生成-输出] 批次: {batch_id}, 第 {i+1}/{num_images} 张: 失败 - 响应数据为空")
                
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    
                    # 下载图片
                    import requests
                    img_response = requests.get(img_url)
                    if img_response.status_code == 200:
                        img_data = img_response.content
                        
                        # 生成8位随机文件名，避免重复
                        random_name = generate_random_filename(8)
                        if num_images > 1:
                            filename = f"{random_name}_{i+1}.jpg"
                        else:
                            filename = f"{random_name}.jpg"
                        
                        # 使用用户专属输出目录
                        user_output_folder = get_user_output_folder(user_id)
                        output_path = os.path.join(user_output_folder, filename)
                        with open(output_path, 'wb') as f:
                            f.write(img_data)
                        
                        # 如果配置了OSS，上传到OSS并获取公共URL（用于作为参考图）
                        image_url = f'/output/{user_id}/{filename}'  # 默认使用本地URL
                        oss_enabled = os.environ.get('OSS_ENABLED', 'false').lower() == 'true'
                        if oss_enabled:
                            oss_url = upload_to_aliyun_oss(output_path, user_id=user_id)
                            if oss_url:
                                image_url = oss_url
                                log_operation('批量生成OSS上传成功', f'用户ID={user_id}, 批次={batch_id}, 文件={filename}, OSS_URL={oss_url}')
                                print(f"[批量生成-OSS] 成功上传生成的图片到OSS: {oss_url}")
                        
                        # 记录最终输出结果
                        final_output = {
                            'filename': filename,
                            'local_path': output_path,
                            'final_url': image_url,
                            'file_size': len(img_data),
                            'seed': per_seed
                        }
                        log_operation('批量生成图片处理完成', f'用户ID={user_id}, 批次={batch_id}, 第{i+1}张 | {json.dumps(final_output, ensure_ascii=False)}')
                        print(f"[批量生成-完成] 批次: {batch_id}, 第 {i+1}/{num_images} 张: 文件名={filename}, URL={image_url}")
                        
                        generated_images.append({
                            'filename': filename,
                            'url': image_url,
                            'seed': per_seed
                        })
                        
                        # 保存记录
                        try:
                            database.save_generation_record({
                                'user_id': user_id,
                                'prompt': prompt,
                                'aspect_ratio': aspect_ratio,
                                'resolution': resolution,
                                'width': width,
                                'height': height,
                                'num_images': 1,
                                'seed': per_seed,
                                'image_style': image_style,
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
        
        log_request('GET', '/api/records', user_id, f'limit={limit}, offset={offset}, search={search[:20]}' if search else f'limit={limit}, offset={offset}')
        
        records = database.get_all_records(user_id, limit, offset)
        
        # 如果有搜索条件，过滤结果
        if search:
            records = [r for r in records if search.lower() in r['prompt'].lower()]
        
        total = database.get_total_count(user_id)
        
        log_operation('获取记录', f'用户ID: {user_id}, 记录数: {len(records)}/{total}')
        
        return jsonify({
            'success': True,
            'records': records,
            'total': total
        })
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('获取记录失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        user_id = session.get('user_id')
        log_request('DELETE', f'/api/records/{record_id}', user_id)
        database.delete_record(record_id)
        log_operation('删除记录', f'用户ID: {user_id}, 记录ID: {record_id}')
        return jsonify({'success': True})
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('删除记录失败', f'用户ID: {user_id}, 记录ID: {record_id}, 错误: {str(e)}', 'ERROR')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/batch-delete', methods=['POST'])
@login_required
def batch_delete_records():
    """批量删除记录"""
    try:
        user_id = session.get('user_id')
        data = request.get_json()
        record_ids = data.get('ids', [])
        log_request('POST', '/api/batch-delete', user_id, f'记录数: {len(record_ids)}')
        
        if not record_ids:
            log_operation('批量删除记录', f'用户ID: {user_id}, 未选择记录', 'WARNING')
            return jsonify({'success': False, 'message': '未选择要删除的记录'})
        
        deleted_count = 0
        failed_count = 0
        
        for record_id in record_ids:
            try:
                database.delete_record(record_id)
                deleted_count += 1
            except Exception as e:
                log_operation('删除单条记录失败', f'用户ID: {user_id}, 记录ID: {record_id}, 错误: {str(e)}', 'WARNING')
                failed_count += 1
        
        log_operation('批量删除记录', f'用户ID: {user_id}, 成功: {deleted_count}, 失败: {failed_count}')
        return jsonify({
            'success': True,
            'deleted': deleted_count,
            'failed': failed_count
        })
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('批量删除记录失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/upload-sample-image', methods=['POST'])
@login_required
def upload_sample_image():
    """上传示例图到 OSS（用户隔离）"""
    try:
        user_id = session.get('user_id')
        log_request('POST', '/api/upload-sample-image', user_id)
        
        if 'file' not in request.files:
            log_operation('上传样本图失败', f'用户ID: {user_id}, 错误: 没有上传文件', 'WARNING')
            return jsonify({'success': False, 'error': '没有上传文件'}), 400
        
        file = request.files['file']
        if file.filename == '':
            log_operation('上传样本图失败', f'用户ID: {user_id}, 错误: 文件名为空', 'WARNING')
            return jsonify({'success': False, 'error': '文件名为空'}), 400
        
        # 验证文件类型
        allowed_extensions = {'jpg', 'jpeg', 'png', 'webp'}
        file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        if file_ext not in allowed_extensions:
            log_operation('上传样本图失败', f'用户ID: {user_id}, 错误: 不支持的文件格式 {file_ext}', 'WARNING')
            return jsonify({'success': False, 'error': f'不支持的文件格式，仅支持: {", ".join(allowed_extensions)}'}), 400
        
        # 获取 OSS 配置
        bucket, endpoint_full = get_oss_bucket()
        if not bucket:
            log_operation('上传样本图失败', f'用户ID: {user_id}, 错误: OSS配置不完整', 'ERROR')
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
        
        log_operation('上传样本图', f'用户ID: {user_id}, 文件: {filename}, 类别: {category}, 大小: {len(file.read())} bytes')
        
        return jsonify({
            'success': True,
            'url': url,
            'filename': filename,
            'key': object_key
        })
    
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('上传样本图失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        log_request('POST', '/api/delete-sample-image', user_id, f'key: {key}')
        
        if not key:
            log_operation('删除样本图失败', f'用户ID: {user_id}, 错误: 缺少文件key', 'WARNING')
            return jsonify({'success': False, 'error': '缺少文件 key'}), 400
        
        # 验证 key 是否属于当前用户（支持 person/scene 两类）
        allowed_prefixes = [f'sample/person/user_{user_id}/', f'sample/scene/user_{user_id}/']
        if not any(key.startswith(p) for p in allowed_prefixes):
            log_operation('删除样本图失败', f'用户ID: {user_id}, 错误: 无权删除此文件 {key}', 'WARNING')
            return jsonify({'success': False, 'error': '无权删除此文件'}), 403
        
        # 获取 OSS 配置
        bucket, endpoint_full = get_oss_bucket()
        if not bucket:
            log_operation('删除样本图失败', f'用户ID: {user_id}, 错误: OSS配置不完整', 'ERROR')
            return jsonify({'success': False, 'error': 'OSS 配置不完整'}), 500
        
        # 删除文件
        bucket.delete_object(key)
        log_operation('删除样本图', f'用户ID: {user_id}, 文件: {key.split("/")[-1]}')
        
        return jsonify({'success': True})
    
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('删除样本图失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        log_request('POST', '/api/delete-library-asset', user_id, f'key: {key}')

        if not key:
            log_operation('删除库资源失败', f'用户ID: {user_id}, 错误: 缺少key', 'WARNING')
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
            log_operation('删除人物库资源', f'用户ID: {user_id}, 资源ID: {aid}')
            return jsonify({'success': True})
        elif key.startswith('db_scene_'):
            aid = int(key.split('_')[-1])
            try:
                conn_asset = database.get_scene_assets(user_id)
            except Exception:
                conn_asset = []
            database.delete_scene_asset(aid)
            log_operation('删除场景库资源', f'用户ID: {user_id}, 资源ID: {aid}')
            return jsonify({'success': True})
        else:
            log_operation('删除库资源失败', f'用户ID: {user_id}, 错误: 不支持的key类型 {key}', 'WARNING')
            return jsonify({'success': False, 'error': '不支持的 key 类型'}), 400
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('删除库资源失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        log_request('POST', '/api/add-to-person-library', user_id, f'文件: {filename}')

        if not url:
            log_operation('添加人物库资源失败', f'用户ID: {user_id}, 错误: 缺少url', 'WARNING')
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
                log_operation('添加人物库资源失败', f'用户ID: {user_id}, 错误: 无法下载远程图片', 'WARNING')
                return jsonify({'success': False, 'error': '无法下载远程图片'}), 400

        # 写入数据库记录
        try:
            database.save_person_asset(user_id, filename, public_url, meta={'source_url': url})
            log_operation('添加人物库资源', f'用户ID: {user_id}, 文件: {filename}')
        except Exception as e:
            log_operation('保存人物库记录失败', f'用户ID: {user_id}, 错误: {str(e)}', 'WARNING')

        return jsonify({'success': True, 'url': public_url, 'filename': filename})
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('添加人物库资源失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        log_request('POST', '/api/add-to-scene-library', user_id, f'文件: {filename}')

        if not url:
            log_operation('添加场景库资源失败', f'用户ID: {user_id}, 错误: 缺少url', 'WARNING')
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
                log_operation('添加场景库资源失败', f'用户ID: {user_id}, 错误: 无法下载远程图片', 'WARNING')
                return jsonify({'success': False, 'error': '无法下载远程图片'}), 400

        try:
            database.save_scene_asset(user_id, filename, public_url, meta={'source_url': url})
            log_operation('添加场景库资源', f'用户ID: {user_id}, 文件: {filename}')
        except Exception as e:
            log_operation('保存场景库记录失败', f'用户ID: {user_id}, 错误: {str(e)}', 'WARNING')

        return jsonify({'success': True, 'url': public_url, 'filename': filename})
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('添加场景库资源失败', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
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
        
        log_request('POST', '/api/batch-generate-all', user_id, f'任务数量: {len(tasks)}')
        
        if not tasks:
            log_operation('批量生成失败', f'用户ID: {user_id}, 原因: 没有任务', 'WARNING')
            return jsonify({'success': False, 'error': '没有任务'}), 400
        
        # 生成批次ID
        batch_id = str(uuid.uuid4())
        
        log_operation('批量生成启动', f'用户ID: {user_id}, 批次ID: {batch_id}, 任务数: {len(tasks)}')
        
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
        aspect_ratio = task.get('aspect_ratio', '1:1')
        image_style = task.get('image_style', '').strip()
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
            
            # 如果选择了风格，将风格的prompt合并到提示词中
            if image_style:
                try:
                    styles_file = os.path.join('static', 'styles.json')
                    if os.path.exists(styles_file):
                        with open(styles_file, 'r', encoding='utf-8') as f:
                            styles_data = json.load(f)
                            style_obj = next((s for s in styles_data.get('styles', []) if s.get('id') == image_style), None)
                            if style_obj:
                                style_prompt = style_obj.get('prompt', '')
                                if style_prompt:
                                    # 将风格prompt合并到用户提示词中
                                    full_prompt = f"{full_prompt}, {style_prompt}"
                                    print(f"[风格合并] 原始提示词: {prompt}")
                                    print(f"[风格合并] 风格: {style_obj.get('name', image_style)}")
                                    print(f"[风格合并] 风格prompt: {style_prompt}")
                                    print(f"[风格合并] 合并后提示词: {full_prompt}")
                except Exception as e:
                    print(f"[风格合并] 加载风格失败: {e}")
                    import traceback
                    traceback.print_exc()
            
            # 调用方舟大模型生成图片
            # 使用 size 参数，格式为 "widthxheight"，如 "1440x2560"
            # 支持使用参考图片（OSS示例图或用户上传的图片）
            try:
                # 构建 size 参数
                size_str = f"{width}x{height}"
                
                # 构建 extra_body，包含参考图片
                extra_body_params = {
                    "watermark": False,  # 默认不添加水印
                }
                
                # 添加参考图片参数
                if image_urls:
                    if len(image_urls) == 1:
                        # 单张图片使用 image 参数
                        extra_body_params["image"] = image_urls[0]
                    else:
                        # 多张图片使用 image_urls 参数（如果API支持）
                        extra_body_params["image_urls"] = image_urls
                        # 或者使用第一张图片作为主要参考图
                        extra_body_params["image"] = image_urls[0]
                
                response = client.images.generate(
                    model="doubao-seedream-4-5-251128",
                    prompt=full_prompt,
                    size=size_str,
                    response_format="url",
                    extra_body=extra_body_params
                )
                
                if response.data and len(response.data) > 0:
                    img_url = response.data[0].url
                    
                    # 下载图片
                    import requests
                    img_response = requests.get(img_url)
                    if img_response.status_code == 200:
                        img_data = img_response.content
                    
                        # 生成8位随机文件名，避免重复
                        random_name = generate_random_filename(8)
                        if num_images > 1:
                            filename = f"{random_name}_{i+1}.jpg"
                        else:
                            filename = f"{random_name}.jpg"
                        
                        # 使用用户专属输出目录
                        user_output_folder = os.path.join('output', str(user_id))
                        os.makedirs(user_output_folder, exist_ok=True)
                        filepath = os.path.join(user_output_folder, filename)
                        with open(filepath, 'wb') as f:
                            f.write(img_data)
                        
                        # 上传到 OSS（如果配置了OSS）
                        image_url = f'/output/{user_id}/{filename}'  # 默认使用本地URL
                        oss_enabled = os.environ.get('OSS_ENABLED', 'false').lower() == 'true'
                        if oss_enabled:
                            oss_url = upload_to_aliyun_oss(filepath, user_id=user_id)
                            if oss_url:
                                image_url = oss_url
                                print(f"成功上传生成的图片到OSS: {oss_url}")
                        
                        # 保存记录（无论是否上传到OSS都保存）
                        database.save_generation_record({
                            'user_id': user_id,
                            'prompt': prompt,
                            'aspect_ratio': aspect_ratio,
                            'resolution': resolution,
                            'width': width,
                            'height': height,
                            'num_images': 1,
                            'seed': per_seed,
                            'image_style': image_style,
                            'sample_images': sample_images_data,
                            'image_path': image_url,  # 使用image_url（可能是OSS URL或本地URL）
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
    log_request('GET', f'/api/batch-progress/{batch_id}', user_id)
    
    with batch_progress_lock:
        if batch_id not in batch_progress:
            log_operation('查询批量进度失败', f'用户ID: {user_id}, 批次ID: {batch_id}, 错误: 批次不存在', 'WARNING')
            return jsonify({'success': False, 'error': '批次ID不存在'}), 404
        
        # 验证批次属于当前用户
        if batch_progress[batch_id].get('user_id') != user_id:
            log_operation('查询批量进度失败', f'用户ID: {user_id}, 批次ID: {batch_id}, 错误: 无权访问', 'WARNING')
            return jsonify({'success': False, 'error': '无权访问此批次'}), 403
        
        progress = batch_progress[batch_id].copy()
        # 只返回最近100条日志
        if len(progress['logs']) > 100:
            progress['logs'] = progress['logs'][-100:]
        
        log_operation('查询批量进度', f'用户ID: {user_id}, 批次ID: {batch_id}, 进度: {progress.get("completed", 0)}/{progress.get("total", 0)}')
        
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
        user_id = session.get('user_id')
        data = request.get_json() or {}
        script = data.get('script', '').strip()
        
        log_request('POST', '/api/analyze-script', user_id, f'脚本长度: {len(script)}')
        
        if not script:
            log_operation('剧本分析失败', f'用户ID: {user_id}, 原因: 缺少脚本', 'WARNING')
            return jsonify({'success': False, 'error': '请输入剧本文本'}), 400
        
        # 获取火山引擎 API Key
        api_key = os.environ.get('ARK_API_KEY')
        base_url = os.environ.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        
        if not api_key:
            log_operation('剧本分析失败', 'API Key未配置', 'ERROR')
            return jsonify({'success': False, 'error': 'ARK_API_KEY 未配置'}), 500
        
        # 初始化 OpenAI 兼容客户端
        client = OpenAI(api_key=api_key, base_url=base_url)
        
        # 构建分析提示词
        analysis_prompt = f"""你是一位知名导演，现需要拍摄一部极具吸引力的短片。用户提供了创意需求描述，请按照以下结构进行全面的创意分析和设计。

用户创意需求：
{script}

请以JSON格式返回完整的创意设计方案（直接返回JSON，不要包含markdown代码块）：
{{
  "character": {{
    "name": "人物姓名",
    "age": "年龄",
    "gender": "性别",
    "appearance": "外貌特征描述",
    "costume": "服饰风格及细节描述",
    "personality": "性格特征描述",
    "charm_points": "独特魅力点"
  }},
  "monologue": {{
    "content": "创作的台词独白（100字左右，简洁直白，符合人物特征）",
    "character_traits": "台词体现的人物特征说明"
  }},
  "scenes": [
    {{
      "scene_number": 1,
      "location": "场景位置描述",
      "monologue_content": "该场景对应的台词内容",
      "shot_type": "拍摄景别（全景、中景、近景、特写等）",
      "character_shot": "人物景别",
      "angle": "视角描述",
      "composition": "构图方式",
      "core_subject": "核心主体",
      "emotion_action": "情绪和动作描述",
      "environment": "环境场景细节",
      "art_style": "艺术风格",
      "lighting_mood": "氛围光线描述",
      "color_tone": "色调描述",
      "camera_movement": "运镜语言（推、拉、摇、移、跟等及其效果）",
      "sound_effect": "音效设计（环境音、背景音乐等）",
      "prompt_for_ai": "完整的AI绘图提示词（详细中文描述，让AI能准确捕捉画面细节）"
    }}
  ],
  "style_tone": {{
    "reference_keywords": ["参考风格关键词1", "参考风格关键词2", "参考风格关键词3"],
    "texture_description": "画面质感详细描述",
    "color_atmosphere": "色调氛围详细描述",
    "overall_description": "整体风格深入描述"
  }},
  "background_music": {{
    "recommendation": "推荐的背景音乐名称或风格",
    "reason": "选择理由及其如何烘托视频氛围的说明"
  }}
}}

创意设计要求：
一、主角设定：全方位精细塑造主角IP形象，包括姓名、年龄、性别、外貌特征、服饰风格及细节，打造独特且极具魅力的角色。

二、台词独白：创作全新台词独白，要有张力，简洁直白，符合人物特征，字数控制在100字左右。

三、分镜场景：依据创作的台词独白智能设计分镜场景，确保台词独白的每个字都能在分镜场景中得到展示。每个场景必需生成一套完整的提示词。
  1. 场景台词：明确每个场景对应的台词
  2. 提示词结构：包含拍摄景别、人物景别、视角、构图、核心主体、情绪动作、环境场景、艺术风格、氛围光线、色调等，描述要细致到能让AI绘图准确捕捉画面细节。
  3. 运镜语言：具体说明推、拉、摇、移、跟等运镜方式在每个场景中的运用时机和预期效果。
  4. 音效：包括环境音、背景音乐的淡入淡出等音效设计。

四、风格色调：突出用户描述的独特风格，从参考风格关键词、画面质感、色调氛围等方面，用中文详细深入描述。

五、背景音乐：推荐与视频风格高度适配的背景音乐，并阐述选择理由，以更好地烘托视频氛围。"""
        
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
                    # 处理 ```json ... ``` 的情况
                    parts = content.split('```')
                    if len(parts) >= 2:
                        content = parts[1]
                        if content.startswith('json'):
                            content = content[4:]
                        content = content.strip()
                
                result = json.loads(content)
                
                # 验证和规范化新结构
                # 验证 character 字段
                if 'character' not in result or not isinstance(result['character'], dict):
                    result['character'] = {
                        'name': '主角',
                        'age': '未知',
                        'gender': '未知',
                        'appearance': '',
                        'costume': '',
                        'personality': '',
                        'charm_points': ''
                    }
                else:
                    char = result['character']
                    # 确保所有字段都存在
                    defaults = {
                        'name': '主角',
                        'age': '未知',
                        'gender': '未知',
                        'appearance': '',
                        'costume': '',
                        'personality': '',
                        'charm_points': ''
                    }
                    for key, default in defaults.items():
                        if key not in char:
                            char[key] = default
                        char[key] = str(char.get(key, default))
                
                # 验证 monologue 字段
                if 'monologue' not in result or not isinstance(result['monologue'], dict):
                    result['monologue'] = {
                        'content': '',
                        'character_traits': ''
                    }
                else:
                    mono = result['monologue']
                    if 'content' not in mono:
                        mono['content'] = ''
                    if 'character_traits' not in mono:
                        mono['character_traits'] = ''
                    mono['content'] = str(mono.get('content', ''))
                    mono['character_traits'] = str(mono.get('character_traits', ''))
                
                # 验证 scenes 字段
                if 'scenes' not in result or not isinstance(result['scenes'], list):
                    result['scenes'] = []
                else:
                    for scene in result['scenes']:
                        if not isinstance(scene, dict):
                            continue
                        # 确保场景有所有必要字段
                        scene_defaults = {
                            'scene_number': 1,
                            'location': '未知场景',
                            'monologue_content': '',
                            'shot_type': '',
                            'character_shot': '',
                            'angle': '',
                            'composition': '',
                            'core_subject': '',
                            'emotion_action': '',
                            'environment': '',
                            'art_style': '',
                            'lighting_mood': '',
                            'color_tone': '',
                            'camera_movement': '',
                            'sound_effect': '',
                            'prompt_for_ai': ''
                        }
                        for key, default in scene_defaults.items():
                            if key not in scene:
                                scene[key] = default
                            scene[key] = str(scene.get(key, default))
                
                # 验证 style_tone 字段
                if 'style_tone' not in result or not isinstance(result['style_tone'], dict):
                    result['style_tone'] = {
                        'reference_keywords': [],
                        'texture_description': '',
                        'color_atmosphere': '',
                        'overall_description': ''
                    }
                else:
                    style = result['style_tone']
                    if 'reference_keywords' not in style:
                        style['reference_keywords'] = []
                    if 'texture_description' not in style:
                        style['texture_description'] = ''
                    if 'color_atmosphere' not in style:
                        style['color_atmosphere'] = ''
                    if 'overall_description' not in style:
                        style['overall_description'] = ''
                    
                    # 确保 reference_keywords 是列表
                    if not isinstance(style['reference_keywords'], list):
                        keywords = style.get('reference_keywords', '')
                        if isinstance(keywords, str):
                            style['reference_keywords'] = [k.strip() for k in keywords.split('、') if k.strip()]
                        else:
                            style['reference_keywords'] = []
                
                # 验证 background_music 字段
                if 'background_music' not in result or not isinstance(result['background_music'], dict):
                    result['background_music'] = {
                        'recommendation': '',
                        'reason': ''
                    }
                else:
                    music = result['background_music']
                    if 'recommendation' not in music:
                        music['recommendation'] = ''
                    if 'reason' not in music:
                        music['reason'] = ''
                    music['recommendation'] = str(music.get('recommendation', ''))
                    music['reason'] = str(music.get('reason', ''))
                
                log_operation('剧本分析成功', f'用户ID: {user_id}, 拆解场景数: {len(result.get("scenes", []))}')
                return jsonify({
                    'success': True,
                    'result': result
                })
            except json.JSONDecodeError as e:
                log_operation('剧本分析失败', f'用户ID: {user_id}, JSON解析错误: {str(e)}', 'ERROR')
                return jsonify({
                    'success': False,
                    'error': f'模型返回格式错误，请重试'
                }), 500
        else:
            log_operation('剧本分析失败', f'用户ID: {user_id}, 模型未返回内容', 'ERROR')
            return jsonify({
                'success': False,
                'error': '模型未返回内容'
            }), 500
    
    except Exception as e:
        user_id = session.get('user_id')
        log_operation('剧本分析异常', f'用户ID: {user_id}, 错误: {str(e)}', 'ERROR')
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': f'分析失败: {str(e)}'
        }), 500

if __name__ == '__main__':
    # 应用启动日志
    log_operation('应用启动', f'版本: 1.0 | 环境: {"开发" if os.environ.get("DEBUG") else "生产"} | 监听地址: 0.0.0.0:5000')
    
    # 记录数据库和存储配置
    try:
        db_info = database.get_database_info() if hasattr(database, 'get_database_info') else '已初始化'
        log_operation('数据库初始化', f'状态: {db_info}')
    except Exception as e:
        log_operation('数据库初始化失败', f'错误: {str(e)}', 'WARNING')
    
    # 检查 OSS 配置
    oss_endpoint = os.environ.get('OSS_ENDPOINT')
    if oss_endpoint:
        log_operation('OSS配置', f'端点: {oss_endpoint}')
    else:
        log_operation('OSS配置未设置', '将使用本地文件存储', 'WARNING')
    
    # 检查上传文件夹
    if not os.path.exists('uploads'):
        os.makedirs('uploads')
        log_operation('创建上传目录', 'uploads/')
    
    print("启动 Web 应用...")
    print(f"访问地址: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
