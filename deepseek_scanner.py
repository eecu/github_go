#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描 DeepSeek API 密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
使用官方余额接口验证密钥有效性
使用 GITHUB_SESSION 进行搜索
"""

import os
import re
import time
import threading
import concurrent.futures
from queue import Queue
import requests
from datetime import datetime
import json
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

# DeepSeek API 配置
DEEPSEEK_API_URL = "https://api.deepseek.com/user/balance"

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1    # API请求间隔（秒）

# 存储结果的线程安全队列
live_keys_queue = Queue()
processed_keys = set()
lock = threading.Lock()
file_lock = threading.Lock()

# 实时保存文件名
live_keys_filename = None
simple_keys_filename = None

# --- GitHub Session Search 相关函数 ---

def get_headers():
    if not GITHUB_SESSION:
        raise ValueError("Missing GITHUB_SESSION")
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

def search_github(query, max_pages=5):
    links = set()
    print(f"📡 搜索: {query}")
    
    for page in range(1, max_pages + 1):
        try:
            url = "https://github.com/search"
            params = {'q': query, 'type': 'code', 'p': page, 'o': 'desc', 's': 'indexed'}
            
            resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
            
            if resp.status_code == 429:
                print("   ⚠️ Rate Limited (429). Waiting 60s...")
                time.sleep(60)
                continue
            elif resp.status_code != 200:
                print(f"   ❌ Error {resp.status_code}")
                break
                
            # 提取链接
            page_links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
            
            if not page_links:
                break
                
            for link in page_links:
                full = "https://github.com" + link.split('#')[0]
                links.add(full)
                
            print(f"   Page {page}: found {len(page_links)} links")
            time.sleep(random.uniform(2, 4))
            
        except Exception as e:
            print(f"   Search Error: {e}")
            break
            
    return list(links)

# --- DeepSeek API 相关函数 ---

def save_live_key_immediately(key_info):
    """实时保存存活密钥到文件"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'deepseek_live_keys_{timestamp}.txt'
            simple_keys_filename = f'deepseek_keys_simple_{timestamp}.txt'
            
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== DeepSeek API 密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
            
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== DeepSeek API 密钥列表 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
        
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            
            balance_info = key_info.get('balance_info', {})
            if balance_info:
                f.write(f"   是否可用: {balance_info.get('is_available', 'Unknown')}\n")
                f.write(f"   余额信息: {balance_info.get('balance_infos', 'N/A')}\n")
            
            f.write(f"   仓库: {key_info['repo']}\n")
            f.write(f"   文件: {key_info['file_path']}\n")
            f.write(f"   链接: {key_info['url']}\n")
            f.write(f"   发现时间: {key_info['discovered_time']}\n")
            f.write("-" * 60 + "\n")
        
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
        
        print(f"💾 已实时保存密钥到: {live_keys_filename}")

def check_deepseek_key(key):
    """验证 DeepSeek API 密钥"""
    if not key.startswith("sk-"):
        return {"status": "无效格式", "valid": False}
    
    try:
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {key}'
        }
        
        response = requests.get(DEEPSEEK_API_URL, headers=headers, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            # 假设返回格式包含 is_available 和 balance_infos
            # 官方文档或示例返回: {"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"...","granted_balance":"...","topped_up_balance":"..."}]}
            
            is_available = data.get("is_available", False)
            balance_infos = data.get("balance_infos", [])
            
            status_str = "🟢 存活 (Live)" if is_available else "🟡 存活但不可用 (Live but Unavailable)"
            
            return {
                "status": status_str,
                "valid": True,
                "balance_info": data
            }
        elif response.status_code == 401:
            return {"status": "🔴 无效密钥 (Invalid)", "valid": False}
        else:
            return {"status": f"⚪ 未知状态 (HTTP {response.status_code})", "valid": False}
            
    except Exception as e:
        return {"status": f"⚪ 验证出错: {str(e)}", "valid": False}

def process_content_file(file_url, worker_id):
    """处理文件内容"""
    try:
        print(f"[Worker-{worker_id}] 正在检查: {file_url}")
        
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        file_content = resp.text
        
        # 提取 sk- 开头的密钥，假设长度至少 30 位
        pattern = r'(sk-[A-Za-z0-9]{30,})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            result = check_deepseek_key(key)
            print(f"[Worker-{worker_id}] 🔑 {key[:15]}... | {result['status']}")
            
            if result['valid']:
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"
                
                key_info = {
                    'type': 'DeepSeek API Key',
                    'key': key,
                    'status': result['status'],
                    'balance_info': result.get('balance_info', {}),
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': file_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                
                save_live_key_immediately(key_info)
                live_keys_queue.put(key_info)
            
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 处理出错: {e}")

def concurrent_search_and_validate():
    """并发搜索和验证"""
    print(f"🚀 开始并发搜索 DeepSeek 密钥 (并发数: {MAX_WORKERS})")
    print("=" * 60)
    
    search_queries = [
        '/sk-[a-zA-Z0-9]{32}/ api.deepseek.com',
        '/sk-[a-zA-Z0-9]{32}/ deepseek-chat',
        '/sk-[a-zA-Z0-9]{32}/ deepseek-reasoner'
    ]
    
    all_file_urls = set()
    
    for query in search_queries:
        urls = search_github(query, max_pages=5)
        all_file_urls.update(urls)
        time.sleep(2)
    
    file_list = list(all_file_urls)
    print(f"🗂️ 共找到 {len(file_list)} 个文件")
    
    if file_list:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_content_file, url, i % MAX_WORKERS + 1) for i, url in enumerate(file_list)]
            concurrent.futures.wait(futures)
    
    print("\n✅ 扫描完成")

def main():
    if not GITHUB_SESSION:
        print("❌ 错误: 请配置 GITHUB_SESSION")
        return

    print("🔍 DeepSeek API Key Scanner")
    concurrent_search_and_validate()

if __name__ == '__main__':
    main()
