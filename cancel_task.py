# 取消/删除视频生成任务
import os
import json
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


def convert_to_dict(obj):
    """将响应对象转换为字典"""
    if isinstance(obj, dict):
        return {k: convert_to_dict(v) for k, v in obj.items()}
    elif hasattr(obj, '__dict__'):
        return {k: convert_to_dict(v) for k, v in obj.__dict__.items()}
    elif hasattr(obj, '_asdict'):  # namedtuple
        return convert_to_dict(obj._asdict())
    elif isinstance(obj, (list, tuple)):
        return [convert_to_dict(item) for item in obj]
    else:
        return obj


def main():
    # 加载 .env 文件
    print("="*60)
    print("🗑️  视频任务取消/删除工具")
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
    print("✅ 客户端初始化成功\n")
    
    # 获取用户输入的任务ID
    print("="*60)
    print("📋 请输入要取消/删除的任务ID")
    print("="*60)
    task_id = input("任务ID: ").strip()
    
    if not task_id:
        print("\n❌ 错误: 任务ID不能为空")
        return
    
    # 确认操作
    print("\n" + "="*60)
    print("⚠️  确认操作")
    print("="*60)
    print(f"任务ID: {task_id}")
    confirm = input("\n确定要取消/删除此任务吗? (y/N): ").strip().lower()
    
    if confirm not in ['y', 'yes']:
        print("\n❌ 操作已取消")
        return
    
    print("\n" + "="*60)
    print("🔄 正在取消/删除任务...")
    print("="*60)
    
    try:
        # 取消/删除任务
        resp = client.content_generation.tasks.delete(task_id=task_id)
        
        # 转换为字典格式
        resp_dict = convert_to_dict(resp)
        
        # 格式化输出为 JSON
        print("\n" + "="*60)
        print("📊 操作结果 (JSON格式)")
        print("="*60)
        print(json.dumps(resp_dict, indent=2, ensure_ascii=False))
        print("="*60)
        
        # 尝试判断操作是否成功
        if isinstance(resp_dict, dict):
            status = resp_dict.get('status', resp_dict.get('message', ''))
            if 'success' in str(status).lower() or 'cancel' in str(status).lower():
                print("\n✅ 任务取消/删除成功!")
            elif 'error' in str(status).lower() or 'fail' in str(status).lower():
                print("\n❌ 任务取消/删除失败!")
            else:
                print("\n✅ 操作完成!")
        else:
            print("\n✅ 操作完成!")
        
    except Exception as e:
        print(f"\n❌ 取消/删除任务失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
