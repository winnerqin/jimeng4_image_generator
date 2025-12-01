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
from volcengine.visual.VisualService import VisualService
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
        
        # 列出 sample/user_{user_id}/ 目录下的所有文件
        sample_images = []
        prefix = f'sample/user_{user_id}/'
        for obj in oss2.ObjectIterator(bucket, prefix=prefix):
            if obj.key.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                # 生成公网访问URL
                url = f"https://{endpoint_full}/{obj.key}"
                filename = os.path.basename(obj.key)
                
                sample_images.append({
                    'url': url,
                    'filename': filename,
                    'size': obj.size,
                    'key': obj.key
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
        
        # 获取 AK/SK
        ak = os.environ.get('VOLCENGINE_AK') or os.environ.get('VOLCENGINE_ACCESS_KEY')
        sk = os.environ.get('VOLCENGINE_SK') or os.environ.get('VOLCENGINE_SECRET_KEY')
        
        if not (ak and sk):
            return jsonify({'error': 'VOLCENGINE_AK 和 VOLCENGINE_SK 未配置'}), 500
        
        # 初始化服务
        svc = VisualService()
        svc.set_ak(ak)
        svc.set_sk(sk)
        
        # 生成图片
        generated_images = []
        total_needed = num_images
        
        for i in range(total_needed):
            # 计算种子（火山引擎 API 限制：最大 99999999）
            if seed and seed != 0:
                per_seed = seed + i
                # 确保不超过最大值
                if per_seed > 99999999:
                    per_seed = (per_seed % 99999999) + 1
            else:
                per_seed = random.randint(1, 99999999)
            
            # 构建请求体
            body = {
                'prompt': prompt,
                'negative_prompt': negative_prompt,
                'width': width,
                'height': height,
                'steps': steps,
                'seed': per_seed,
                'model_version': 'v1',
                'req_key': 'jimeng_t2i_v40',
                'num_images': 1,
                'n': 1,
                'image_count': 1,
            }
            
            if image_urls:
                body['image_urls'] = image_urls
            
            # 调用 SDK
            try:
                resp = svc.text2img_xl_sft(body)
                
                # 检查响应
                if isinstance(resp, dict) and resp.get('code') == 10000:
                    data = resp.get('data', {})
                    b64_list = data.get('binary_data_base64', [])
                    
                    if b64_list:
                        # 解码并保存图片
                        img_b64 = b64_list[0]
                        img_data = base64.b64decode(img_b64)
                        
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
                    print(f"API 返回错误: {resp}")
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
        sample_images = list_sample_images_from_oss(user_id)
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
    """示例图管理页面"""
    return render_template('manage_samples.html', user=get_current_user())

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
        
        # 获取 AK/SK
        ak = os.environ.get('VOLCENGINE_AK') or os.environ.get('VOLCENGINE_ACCESS_KEY')
        sk = os.environ.get('VOLCENGINE_SK') or os.environ.get('VOLCENGINE_SECRET_KEY')
        
        if not (ak and sk):
            return jsonify({'success': False, 'error': 'VOLCENGINE_AK 和 VOLCENGINE_SK 未配置'}), 500
        
        # 初始化服务
        svc = VisualService()
        svc.set_ak(ak)
        svc.set_sk(sk)
        
        # 生成图片
        generated_images = []
        
        for i in range(num_images):
            per_seed = random.randint(1, 99999999)
            
            body = {
                'prompt': prompt,
                'negative_prompt': negative_prompt,
                'width': width,
                'height': height,
                'steps': 28,
                'seed': per_seed,
                'model_version': 'v1',
                'req_key': 'jimeng_t2i_v40',
                'num_images': 1,
                'n': 1,
                'image_count': 1,
            }
            
            if image_urls:
                body['image_urls'] = image_urls
            
            try:
                resp = svc.text2img_xl_sft(body)
                
                if isinstance(resp, dict) and resp.get('code') == 10000:
                    data_resp = resp.get('data', {})
                    b64_list = data_resp.get('binary_data_base64', [])
                    
                    if b64_list:
                        img_b64 = b64_list[0]
                        img_data = base64.b64decode(img_b64)
                        
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
        
        # 生成对象键 - 按用户隔离保存到 sample/user_{user_id}/ 目录
        filename = secure_filename(file.filename)
        object_key = f"sample/user_{user_id}/{filename}"
        
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
        
        # 验证 key 是否属于当前用户
        expected_prefix = f'sample/user_{user_id}/'
        if not key.startswith(expected_prefix):
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
        
        # 获取 AK/SK
        ak = os.environ.get('VOLCENGINE_AK') or os.environ.get('VOLCENGINE_ACCESS_KEY')
        sk = os.environ.get('VOLCENGINE_SK') or os.environ.get('VOLCENGINE_SECRET_KEY')
        
        if not (ak and sk):
            return {'success': False, 'error': 'VOLCENGINE_AK 和 VOLCENGINE_SK 未配置'}
        
        # 初始化服务
        svc = VisualService()
        svc.set_ak(ak)
        svc.set_sk(sk)
        
        # 生成图片
        for i in range(num_images):
            per_seed = random.randint(1, 99999999)
            
            body = {
                'prompt': prompt,
                'negative_prompt': negative_prompt,
                'width': width,
                'height': height,
                'steps': 28,
                'seed': per_seed,
                'model_version': 'v1',
                'req_key': 'jimeng_t2i_v40',
                'num_images': 1,
            }
            
            if image_urls:
                body['image_urls'] = image_urls
            
            resp = svc.text2img_xl_sft(body)
            
            if isinstance(resp, dict) and resp.get('code') == 10000:
                data_resp = resp.get('data', {})
                b64_list = data_resp.get('binary_data_base64', [])
                
                if b64_list:
                    img_b64 = b64_list[0]
                    img_data = base64.b64decode(img_b64)
                    
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

if __name__ == '__main__':
    print("启动 Web 应用...")
    print(f"访问地址: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
