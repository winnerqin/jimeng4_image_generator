import os
import json
import base64
import random
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from volcengine.visual.VisualService import VisualService
import database

# 阿里云 OSS 上传支持
def upload_to_aliyun_oss(file_path):
    """
    上传文件到阿里云 OSS（对象存储服务）
    需要配置以下环境变量：
    - OSS_ENDPOINT: OSS 端点（如：oss-cn-wulanchabu.aliyuncs.com）
    - OSS_BUCKET: 存储桶名称（从 endpoint 中提取）
    - OSS_ACCESS_KEY_ID: 阿里云 AccessKey ID
    - OSS_ACCESS_KEY_SECRET: 阿里云 AccessKey Secret
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
        
        # 生成对象键（文件名）- 保留原始文件名
        filename = os.path.basename(file_path)
        timestamp = datetime.now().strftime('%Y%m%d')
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

def list_sample_images_from_oss():
    """
    从 OSS 的 ai-images/sample 目录获取示例图列表
    """
    try:
        import oss2
        
        bucket, endpoint_full = get_oss_bucket()
        if not bucket:
            return []
        
        sample_images = []
        prefix = 'ai-images/sample/'
        
        # 列出所有文件
        for obj in oss2.ObjectIterator(bucket, prefix=prefix):
            # 只获取图片文件
            if obj.key.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                # 生成公网访问 URL
                url = f"https://{endpoint_full}/{obj.key}"
                filename = os.path.basename(obj.key)
                sample_images.append({
                    'filename': filename,
                    'url': url,
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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate():
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
        
        for file in uploaded_files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                # 保留原始文件名
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
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
                        
                        output_path = os.path.join(app.config['OUTPUT_FOLDER'], filename)
                        with open(output_path, 'wb') as f:
                            f.write(img_data)
                        
                        generated_images.append({
                            'filename': filename,
                            'url': f'/output/{filename}',
                            'seed': per_seed
                        })
                        
                        # 保存记录到数据库
                        try:
                            sample_images_list = [{'url': url, 'filename': os.path.basename(url)} for url in image_urls]
                            database.save_generation_record({
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
                                'image_path': f'/output/{filename}',
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
def get_sample_images():
    """获取 OSS 中的示例图列表"""
    try:
        sample_images = list_sample_images_from_oss()
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
def batch():
    """批量生成页面"""
    return render_template('batch.html')

@app.route('/records')
def records():
    """生成记录页面"""
    return render_template('records.html')

@app.route('/api/batch-generate', methods=['POST'])
def batch_generate():
    """批量生成API"""
    try:
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
                        
                        output_path = os.path.join(app.config['OUTPUT_FOLDER'], filename)
                        with open(output_path, 'wb') as f:
                            f.write(img_data)
                        
                        generated_images.append({
                            'filename': filename,
                            'url': f'/output/{filename}',
                            'seed': per_seed
                        })
                        
                        # 保存记录
                        try:
                            database.save_generation_record({
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
                                'image_path': f'/output/{filename}',
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
def get_records():
    """获取生成记录"""
    try:
        limit = int(request.args.get('limit', 20))
        offset = int(request.args.get('offset', 0))
        search = request.args.get('search', '')
        
        records = database.get_all_records(limit, offset)
        
        # 如果有搜索条件，过滤结果
        if search:
            records = [r for r in records if search.lower() in r['prompt'].lower()]
        
        total = database.get_total_count()
        
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
def delete_record(record_id):
    """删除记录"""
    try:
        database.delete_record(record_id)
        return jsonify({'success': True})
    except Exception as e:
        print(f"删除记录失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/output/<filename>')
def output_file(filename):
    return send_from_directory(app.config['OUTPUT_FOLDER'], filename)

@app.route('/favicon.ico')
def favicon():
    return '', 204  # 返回空响应，避免 404

if __name__ == '__main__':
    print("启动 Web 应用...")
    print(f"访问地址: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)
