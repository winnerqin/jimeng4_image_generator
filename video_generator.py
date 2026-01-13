# 视频生成任务
import os
import time
from volcenginesdkarkruntime import Ark


def load_dotenv_file(path):
    """加载 .env 文件"""
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
    except Exception as e:
        print(f"加载 .env 文件失败: {e}")


def find_dotenv(start_dir=None):
    """查找 .env 文件"""
    import pathlib
    cur = pathlib.Path(start_dir or os.getcwd()).resolve()
    root = cur.anchor
    while True:
        candidate = cur / '.env'
        if candidate.exists() and candidate.is_file():
            return str(candidate)
        if str(cur) == root:
            return None
        cur = cur.parent


def main():
    # 加载 .env 文件
    print("="*60)
    print("🔧 初始化视频生成工具")
    print("="*60)
    
    dotenv_path = find_dotenv()
    if dotenv_path:
        print(f"📁 找到 .env 文件: {dotenv_path}")
        load_dotenv_file(dotenv_path)
    else:
        print("⚠️  未找到 .env 文件，将使用环境变量")
    
    # 获取 API Key
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        print("\n❌ 错误: ARK_API_KEY 未配置")
        print("   请在 .env 文件中设置 ARK_API_KEY=your_api_key")
        return
    
    print(f"✅ API Key 已加载 (长度: {len(api_key)})")
    
    # 初始化客户端
    client = Ark(api_key=api_key)
    print("✅ 客户端初始化成功")
    
    # 获取用户输入的提示词
    print("\n" + "="*60)
    print("📝 请输入视频生成提示词")
    print("="*60)
    prompt = input("提示词: ").strip()
    
    if not prompt:
        print("\n❌ 错误: 提示词不能为空")
        return
    
    # 获取用户输入的参数
    print("\n" + "="*60)
    print("📐 请输入视频参数 (直接回车使用默认值)")
    print("="*60)
    
    # 宽高比
    ratio = input("宽高比 (默认: 16:9，例如: 16:9, 9:16, 1:1): ").strip()
    if not ratio:
        ratio = "16:9"
    
    # 分辨率
    resolution = input("分辨率 (默认: 720p，例如: 720p, 1080p): ").strip()
    if not resolution:
        resolution = "720p"
    
    # 时长（秒）
    duration_input = input("时长/秒 (默认: 5，例如: 5, 10): ").strip()
    duration = int(duration_input) if duration_input.isdigit() else 5
    
    # 种子值（可选）
    seed_input = input("种子值 (可选，直接回车跳过): ").strip()
    seed = int(seed_input) if seed_input.isdigit() else None
    
    # 相机是否固定（可选）
    camera_fixed_input = input("相机固定 (默认: false，输入 true/false): ").strip().lower()
    camera_fixed = camera_fixed_input == 'true' if camera_fixed_input else False
    
    # 水印（可选）
    watermark_input = input("添加水印 (默认: false，输入 true/false): ").strip().lower()
    watermark = watermark_input == 'true' if watermark_input else False
    
    print("\n" + "="*60)
    print("🚀 正在提交视频生成任务...")
    print("="*60)
    print(f"  📌 模型: doubao-seedance-1-5-pro-251215")
    print(f"  📝 提示词: {prompt}")
    print(f"  📐 宽高比: {ratio}")
    print(f"  🎬 分辨率: {resolution}")
    print(f"  ⏱️  时长: {duration} 秒")
    if seed is not None:
        print(f"  🌱 种子值: {seed}")
    print(f"  📷 相机固定: {camera_fixed}")
    print(f"  💧 水印: {watermark}")
    print("="*60)
    
    try:
        # 构建API调用参数（使用新的参数方式）
        create_params = {
            "model": "doubao-seedance-1-5-pro-251215",
            "content": [{"type": "text", "text": prompt}],
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration,
            "camera_fixed": camera_fixed,
            "watermark": watermark
        }
        
        # 如果提供了种子值，添加到参数中
        if seed is not None:
            create_params["seed"] = seed
        
        # 创建视频生成任务
        resp = client.content_generation.tasks.create(**create_params)
        
        print("\n" + "="*60)
        print("✅ 任务提交成功!")
        print("="*60)
        print(f"📊 响应内容:")
        print(f"  {resp}")
        print("-"*60)
        
        # 获取任务ID - 尝试多种方式
        task_id = None
        if hasattr(resp, 'task_id'):
            task_id = resp.task_id
        elif hasattr(resp, 'id'):
            task_id = resp.id
        elif isinstance(resp, dict):
            task_id = resp.get('task_id') or resp.get('id')
        elif hasattr(resp, '__dict__'):
            # 尝试从对象的字典属性中获取
            resp_dict = resp.__dict__
            task_id = resp_dict.get('task_id') or resp_dict.get('id')
        
        if not task_id:
            print("\n❌ 错误: 无法从响应中获取任务ID")
            print(f"  响应对象类型: {type(resp)}")
            print(f"  响应对象属性: {dir(resp)}")
            return
        
        print(f"📋 任务ID: {task_id}")
        print("\n" + "="*60)
        print("🔄 开始查询任务状态 (每10秒查询一次)...")
        print("="*60)
        
        # 循环查询任务状态
        query_count = 0
        while True:
            query_count += 1
            current_time = time.strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n{'='*60}")
            print(f"📡 [查询 #{query_count}] {current_time}")
            print("-"*60)
            
            try:
                status_resp = client.content_generation.tasks.get(task_id=task_id)
                
                # 获取任务状态 - 尝试多种方式
                status = None
                if hasattr(status_resp, 'status'):
                    status = status_resp.status
                elif hasattr(status_resp, 'task'):
                    task = status_resp.task
                    if isinstance(task, dict):
                        status = task.get('status')
                    elif hasattr(task, 'status'):
                        status = task.status
                elif isinstance(status_resp, dict):
                    status = status_resp.get('status') or status_resp.get('task', {}).get('status')
                
                if status is None:
                    status = 'unknown'
                    print("⚠️  警告: 无法从响应中获取状态")
                    print(f"📋 完整响应: {status_resp}")
                else:
                    # 格式化状态显示
                    status_icon = "⏳" if str(status).lower() in ['queued', 'running'] else "✅"
                    print(f"{status_icon} 任务状态: {status}")
                
                # 检查状态
                status_lower = str(status).lower()
                if status_lower in ['queued', 'running']:
                    print(f"⏱️  等待中... 10秒后继续查询")
                    print("-"*60)
                    time.sleep(10)
                else:
                    # 任务完成或失败
                    print("\n" + "="*60)
                    if status_lower in ['completed', 'success', 'succeeded']:
                        print("✅ 任务成功完成!")
                        print(f"   最终状态: {status}")
                    elif status_lower in ['failed', 'error']:
                        print("❌ 任务失败!")
                        print(f"   最终状态: {status}")
                    else:
                        print("🏁 任务结束!")
                        print(f"   最终状态: {status}")
                    print("="*60)
                    
                    # 尝试获取视频URL或其他结果信息
                    output_info = None
                    if hasattr(status_resp, 'output'):
                        output_info = status_resp.output
                    elif hasattr(status_resp, 'task'):
                        task = status_resp.task
                        if isinstance(task, dict):
                            output_info = task.get('output')
                        elif hasattr(task, 'output'):
                            output_info = task.output
                    elif isinstance(status_resp, dict):
                        output_info = status_resp.get('output') or status_resp.get('task', {}).get('output')
                    
                    if output_info:
                        print("\n📹 输出信息:")
                        print("-"*60)
                        if isinstance(output_info, dict):
                            for key, value in output_info.items():
                                print(f"   {key}: {value}")
                        else:
                            print(f"   {output_info}")
                    
                    print("\n" + "="*60)
                    break
                    
            except Exception as e:
                print(f"❌ 查询任务状态失败: {e}")
                import traceback
                traceback.print_exc()
                print("⏱️  10秒后重试...")
                print("-"*60)
                time.sleep(10)
        
        print("🎉 任务处理完成!")
        print("="*60)
        
    except Exception as e:
        print(f"\n❌ 创建任务失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
