"""
数据库模型 - 存储图片生成记录
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = 'generation_records.db'

def init_database():
    """初始化数据库"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 创建生成记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS generation_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            prompt TEXT NOT NULL,
            negative_prompt TEXT,
            aspect_ratio TEXT,
            resolution TEXT,
            width INTEGER,
            height INTEGER,
            num_images INTEGER,
            seed INTEGER,
            steps INTEGER,
            sample_images TEXT,
            image_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            batch_id TEXT,
            status TEXT DEFAULT 'success'
        )
    ''')
    
    # 创建索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_created_at ON generation_records(created_at DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_batch_id ON generation_records(batch_id)')
    
    conn.commit()
    conn.close()
    print(f"数据库初始化完成: {DB_PATH}")

def save_generation_record(data):
    """
    保存生成记录
    
    Args:
        data: dict with keys:
            - prompt, negative_prompt, aspect_ratio, resolution
            - width, height, num_images, seed, steps
            - sample_images (list), image_path, filename
            - batch_id (optional)
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    sample_images_json = json.dumps(data.get('sample_images', []))
    
    cursor.execute('''
        INSERT INTO generation_records 
        (prompt, negative_prompt, aspect_ratio, resolution, width, height, 
         num_images, seed, steps, sample_images, image_path, filename, batch_id, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('prompt'),
        data.get('negative_prompt', ''),
        data.get('aspect_ratio'),
        data.get('resolution'),
        data.get('width'),
        data.get('height'),
        data.get('num_images', 1),
        data.get('seed', 0),
        data.get('steps', 28),
        sample_images_json,
        data.get('image_path'),
        data.get('filename'),
        data.get('batch_id'),
        data.get('status', 'success')
    ))
    
    record_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return record_id

def get_all_records(limit=100, offset=0):
    """获取所有记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM generation_records 
        ORDER BY created_at DESC 
        LIMIT ? OFFSET ?
    ''', (limit, offset))
    
    rows = cursor.fetchall()
    records = []
    
    for row in rows:
        record = dict(row)
        # 解析 JSON 字段
        if record['sample_images']:
            record['sample_images'] = json.loads(record['sample_images'])
        records.append(record)
    
    conn.close()
    return records

def get_records_by_batch(batch_id):
    """获取指定批次的记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM generation_records 
        WHERE batch_id = ?
        ORDER BY created_at DESC
    ''', (batch_id,))
    
    rows = cursor.fetchall()
    records = []
    
    for row in rows:
        record = dict(row)
        if record['sample_images']:
            record['sample_images'] = json.loads(record['sample_images'])
        records.append(record)
    
    conn.close()
    return records

def get_record_by_id(record_id):
    """获取单条记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM generation_records WHERE id = ?', (record_id,))
    row = cursor.fetchone()
    
    if row:
        record = dict(row)
        if record['sample_images']:
            record['sample_images'] = json.loads(record['sample_images'])
        conn.close()
        return record
    
    conn.close()
    return None

def delete_record(record_id):
    """删除记录"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM generation_records WHERE id = ?', (record_id,))
    conn.commit()
    conn.close()

def get_total_count():
    """获取总记录数"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM generation_records')
    count = cursor.fetchone()[0]
    conn.close()
    return count

if __name__ == '__main__':
    # 测试数据库
    init_database()
    print("✅ 数据库表创建成功")
