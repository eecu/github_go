#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Azure OpenAI 批量模型可用性验证器 V2
功能:
1. 读取扫描器生成的 azure_keys_simple_*.txt 文件
2. 批量对每个 (Endpoint, Key) 进行深度模型可用性检测
3. 生成详细的汇总报告

V2 改进:
- 更新 API 版本到最新的 2024-12-01-preview
- 扩展推理模型判断逻辑，包含 GPT-5/5.1 系列
- 添加多层回退策略处理各种参数错误
- 增强错误处理和诊断信息
"""

import os
import csv
import time
import glob
import json
import requests
from datetime import datetime
from typing import List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================
# 核心检测逻辑 (V2 增强版)
# ==========================

TARGET_MODELS = [
    # GPT-3.5 系列
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-0125",
    "gpt-3.5-turbo-1106",
    # GPT-4 系列
    "gpt-4",
    "gpt-4-0125-preview",
    "gpt-4-0613",
    "gpt-4-1106-preview",
    "gpt-4-turbo",
    "gpt-4-turbo-2024-04-09",
    # GPT-4.1 系列
    "gpt-4.1",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano",
    "gpt-4.1-nano-2025-04-14",
    # GPT-4o 系列
    "gpt-4o",
    "gpt-4o-2024-05-13",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    # GPT-5 系列
    "gpt-5",
    "gpt-5-2025-08-07",
    "gpt-5-chat-latest",
    "gpt-5-mini",
    "gpt-5-mini-2025-08-07",
    "gpt-5-nano",
    "gpt-5-nano-2025-08-07",
    # GPT-5.1 系列
    "gpt-5.1",
    "gpt-5.1-2025-11-13",
    # 其他模型
    "grok-4-latest",
    "gpt-oss-120b",
    "gpt-oss-20b",
    # O1 系列 (推理模型)
    "o1",
    "o1-2024-12-17",
    "o1-mini",
    "o1-mini-2024-09-12",
    # O3 系列 (推理模型)
    "o3",
    "o3-2025-04-16",
    "o3-mini",
    "o3-mini-2025-01-31",
    # O4 系列 (推理模型)
    "o4-mini",
    "o4-mini-2025-04-16"
]

# 推理模型特征关键词 (这些模型使用不同的参数)
REASONING_MODEL_PATTERNS = [
    'o1', 'o3', 'o4',  # O系列推理模型
    'gpt-5',          # GPT-5 系列可能是推理模型
    'gpt-5.1',        # GPT-5.1 系列可能是推理模型
]

class AzureOpenAIAvailabilityCheckerV2:
    """Azure OpenAI 模型可用性检测器 V2"""
    
    # 支持多个 API 版本，按优先级排列
    API_VERSIONS = [
        "2025-01-01-preview",  # 最新预览版
        "2024-12-01-preview",  # 较新预览版
        "2024-10-01-preview",  # 中期预览版
        "2024-08-01-preview",  # 稳定预览版
        "2024-06-01",          # 稳定版
        "2024-02-01",          # 旧稳定版
    ]
    
    def __init__(self, endpoint: str, api_key: str, api_version: str = None):
        # 清理端点格式
        if ':' in endpoint and not endpoint.startswith('http'):
            endpoint = endpoint.split(':')[-1]
        if not endpoint.startswith('http'):
            endpoint = 'https://' + endpoint
            
        self.endpoint = endpoint.rstrip('/')
        self.api_key = api_key
        self.api_version = api_version or self.API_VERSIONS[0]  # 默认使用最新版本
        
        # 设置请求头
        self.headers = {
            'api-key': self.api_key,
            'Content-Type': 'application/json'
        }
    
    def _is_reasoning_model(self, model_name: str) -> bool:
        """判断是否为推理模型 (需要特殊参数处理)"""
        model_lower = model_name.lower()
        for pattern in REASONING_MODEL_PATTERNS:
            if pattern in model_lower:
                return True
        return False
    
    def get_deployed_models(self) -> Tuple[List[str], List[Dict]]:
        """获取已部署的模型列表"""
        # 尝试多个 API 版本
        for api_ver in self.API_VERSIONS:
            try:
                url = f"{self.endpoint}/openai/deployments?api-version={api_ver}"
                response = requests.get(url, headers=self.headers, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    deployments = data.get('data', [])
                    
                    deployed_models = []
                    for deployment in deployments:
                        model_name = deployment.get('model', '')
                        deployment_id = deployment.get('id', '')
                        if model_name:
                            deployed_models.append(model_name)
                        if deployment_id and deployment_id != model_name:
                            deployed_models.append(deployment_id)
                    
                    return deployed_models, deployments
            except:
                continue
        
        return [], []
    
    def _build_payloads(self, model_name: str) -> List[Dict]:
        """
        为模型构建多种测试 payload
        返回按优先级排列的 payload 列表
        """
        payloads = []
        is_reasoning = self._is_reasoning_model(model_name)
        
        if is_reasoning:
            # 推理模型优先尝试 max_completion_tokens，不设置 temperature
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 1
            })
            # 备用：最小化参数
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}]
            })
            # 再备用：尝试标准参数 (以防判断错误)
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1
            })
        else:
            # 标准模型优先使用 max_tokens + temperature
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1,
                "temperature": 0
            })
            # 备用：只用 max_tokens
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 1
            })
            # 备用：尝试 max_completion_tokens (以防是推理模型)
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 1
            })
            # 最后备用：最小化参数
            payloads.append({
                "messages": [{"role": "user", "content": "Hi"}]
            })
        
        return payloads
    
    def test_model_availability(self, model_name: str) -> Dict[str, Any]:
        """测试特定模型的可用性 (V2 增强版，多 API 版本 + 多 Payload 回退)"""
        
        payloads = self._build_payloads(model_name)
        last_error = None
        
        # 尝试多个 API 版本
        for api_ver in self.API_VERSIONS:
            url = f"{self.endpoint}/openai/deployments/{model_name}/chat/completions?api-version={api_ver}"
            
            # 对每个 API 版本，尝试多种 payload
            for payload in payloads:
                try:
                    start_time = time.time()
                    response = requests.post(url, headers=self.headers, json=payload, timeout=20)
                    response_time = time.time() - start_time
                    
                    if response.status_code == 200:
                        return {
                            "model": model_name,
                            "status": "available",
                            "response_time": response_time,
                            "api_version": api_ver,
                            "payload_type": "reasoning" if "max_completion_tokens" in payload else "standard"
                        }
                    elif response.status_code == 404:
                        # 模型未部署，不需要尝试其他 payload
                        return {"model": model_name, "status": "not_deployed"}
                    elif response.status_code == 429:
                        # 限流说明模型存在
                        return {
                            "model": model_name,
                            "status": "rate_limited",
                            "api_version": api_ver
                        }
                    elif response.status_code == 400:
                        # 参数错误，尝试下一个 payload
                        error_text = response.text
                        last_error = f"HTTP 400: {error_text[:200]}"
                        continue
                    elif response.status_code == 401:
                        # Key 无效，直接返回
                        return {"model": model_name, "status": "invalid_key"}
                    else:
                        # 其他错误，记录并尝试下一个
                        error_msg = response.text
                        try:
                            error_json = response.json()
                            if "error" in error_json and "message" in error_json["error"]:
                                error_msg = error_json["error"]["message"]
                        except:
                            pass
                        last_error = f"HTTP {response.status_code}: {error_msg[:200]}"
                        
                except requests.Timeout:
                    last_error = "Request timeout"
                    continue
                except Exception as e:
                    last_error = str(e)
                    continue
        
        # 所有尝试都失败
        return {
            "model": model_name,
            "status": "error",
            "message": last_error or "All attempts failed"
        }
    
    def check(self, max_workers: int = 5) -> Dict[str, Any]:
        """执行完整检测"""
        deployed_models, deployments = self.get_deployed_models()
        
        results = {
            "endpoint": self.endpoint,
            "api_versions_tried": self.API_VERSIONS,
            "target_models": TARGET_MODELS,
            "deployed_list": [d.get('id') for d in deployments],
            "available": [],
            "rate_limited": [],
            "errors": []
        }
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_model = {executor.submit(self.test_model_availability, model): model for model in TARGET_MODELS}
            
            for future in as_completed(future_to_model):
                res = future.result()
                if res["status"] == "available":
                    results["available"].append(res)
                elif res["status"] == "rate_limited":
                    results["rate_limited"].append(res)
                elif res["status"] == "error":
                    results["errors"].append(res)
                    
        return results

# ==========================
# 批量处理逻辑
# ==========================

def select_file():
    """列出并选择 .txt 文件"""
    files = glob.glob("*.txt")
    # 优先显示 azure_keys_simple_*
    priority_files = [f for f in files if f.startswith("azure_keys_simple_")]
    other_files = [f for f in files if not f.startswith("azure_keys_simple_")]
    
    sorted_files = sorted(priority_files, reverse=True) + sorted(other_files)
    
    if not sorted_files:
        print("❌ 当前目录下没有找到 .txt 文件")
        return None
    
    print("\n📂 可用的文件列表:")
    for i, f in enumerate(sorted_files):
        print(f"   [{i+1}] {f}")
        
    while True:
        choice = input("\n请选择文件编号 (输入 q 退出): ").strip()
        if choice.lower() == 'q':
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(sorted_files):
                return sorted_files[idx]
            else:
                print("❌ 无效的编号")
        except ValueError:
            print("❌ 请输入数字")

def main():
    print("🚀 Azure OpenAI 批量模型验证器 V2")
    print("=" * 50)
    print("✨ V2 改进:")
    print("   - 支持多 API 版本回退 (2024-12-01-preview 等)")
    print("   - 增强 GPT-5/5.1 推理模型检测")
    print("   - 多层 Payload 回退策略")
    print("=" * 50)
    
    input_file = select_file()
    if not input_file:
        return
        
    print(f"\n✅ 已选择: {input_file}")
    
    # 读取配置
    configs = []
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('='):
                    continue
                if "Endpoint,Key" in line:  # 跳过 Header
                    continue
                    
                parts = line.split(',')
                if len(parts) >= 2:
                    configs.append((parts[0].strip(), parts[1].strip()))
    except Exception as e:
        print(f"❌ 读取文件失败: {e}")
        return
        
    if not configs:
        print("❌ 文件中未找到有效的 Endpoint,Key 配置")
        return
        
    print(f"📊 找到 {len(configs)} 个待检测配置")
    print(f"🎯 目标模型数： {len(TARGET_MODELS)}")
    confirm = input("是否开始批量检测? (y/n): ").strip().lower()
    if confirm != 'y':
        return
        
    # 初始化结果文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = f"azure_batch_report_v2_{timestamp}.txt"
    
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write("=== Azure OpenAI 批量深度检测报告 V2 ===\n")
        f.write(f"源文件: {input_file}\n")
        f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"目标模型数： {len(TARGET_MODELS)}\n")
        f.write("=" * 60 + "\n\n")
    
    print("\n🏁 开始批量处理...\n")
    
    valid_count = 0
    
    for i, (endpoint, key) in enumerate(configs):
        print(f"[{i+1}/{len(configs)}] 正在检测: {endpoint} ...")
        
        checker = AzureOpenAIAvailabilityCheckerV2(endpoint, key)
        result = checker.check(max_workers=10)
        
        # 只有当至少有一个可用模型或者有限流模型时，才记录
        if result['available'] or result['rate_limited']:
            valid_count += 1
            
            # 在控制台显示简报
            avail_models = [m['model'] for m in result['available']]
            rate_limited_models = [m['model'] for m in result['rate_limited']]
            
            print(f"   🟢 发现 {len(avail_models)} 个可用模型")
            if avail_models:
                print(f"   📝 可用: {', '.join(avail_models)}")
            if rate_limited_models:
                print(f"   ⚠️ 限流: {', '.join(rate_limited_models)}")
            
            # 写入报告
            with open(report_file, 'a', encoding='utf-8') as f:
                f.write(f"🔗 Endpoint: {endpoint}\n")
                f.write(f"🔑 Key: {key}\n")
                if result['deployed_list']:
                    f.write(f"📋 Deployment IDs (List): {', '.join(result['deployed_list'])}\n")
                
                if result['available']:
                    f.write("✅ Available Models:\n")
                    for m in result['available']:
                        api_ver = m.get('api_version', 'unknown')
                        payload_type = m.get('payload_type', 'unknown')
                        resp_time = m.get('response_time', 0)
                        f.write(f"   - {m['model']} ({resp_time:.2f}s, API: {api_ver}, Type: {payload_type})\n")
                
                if result['rate_limited']:
                    f.write("⚠️ Rate Limited Models:\n")
                    for m in result['rate_limited']:
                        f.write(f"   - {m['model']}\n")
                
                f.write("-" * 60 + "\n")
        else:
            print("   ⚪ 无可用模型 (或许是 Key 无效或部署名不匹配)")
            
    print("\n" + "=" * 60)
    print("🎉 批量检测完成!")
    print(f"📄 报告已保存至： {report_file}")
    print(f"📊 有效端点数： {valid_count}/{len(configs)}")

if __name__ == "__main__":
    main()