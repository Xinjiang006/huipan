
import os
import json
import time
import random
import requests
from typing import Optional




def sleep(sec=1):
    time.sleep(sec)

def get_round(mi, mx, decimal_places=2):
    return round(random.uniform(float(mi), float(mx)), int(decimal_places))

def save_json(data: dict, out_file: str, out_dir: str = "static/data"):
    """写入 hk_movers.json，失败时保留旧文件"""
    if not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    path = os.path.join(out_dir, out_file)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  💾 已写入 {path}")


def fetch_url(endpoint: str, headers: dict, params: dict={}, timeout: int = 10) -> Optional[str]:
    for _ in range(5):
        sleep(get_round(1,2))
        try:
            resp = requests.get(endpoint, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
        except requests.exceptions.Timeout:
            print(f"  ❌ {endpoint} → 超时")
        except requests.exceptions.ConnectionError:
            print(f"  ❌ {endpoint} → 连接失败")
        except Exception as e:
            print(f"  ❌ {endpoint} → {type(e).__name__}: {e}")
    return
