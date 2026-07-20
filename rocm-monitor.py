#!/usr/bin/env python3
"""ROCm GPU Monitor — 显示 VRAM 使用量（GB）、GPU 利用率和进程详情。"""

import subprocess
import re
import sys
import json


def get_gpu_info():
    """解析 rocm-smi 输出，返回每个 GPU 的 VRAM 信息。"""
    result = subprocess.run(
        ["rocm-smi", "--showmeminfo", "all"],
        capture_output=True, text=True,
    )

    gpus = {}
    for line in result.stdout.split("\n"):
        if "GPU[" not in line or "VRAM" not in line:
            continue

        gpu_match = re.search(r"GPU\[(\d+)\]", line)
        if not gpu_match:
            continue

        gpu_id = gpu_match.group(1)
        if gpu_id not in gpus:
            gpus[gpu_id] = {}

        # 只匹配 VRAM（排除 VIS_VRAM 和 GTT）
        if "VRAM Total Memory (B):" in line and "Used" not in line and "VIS_VRAM" not in line:
            gpus[gpu_id]["total"] = int(line.split(": ")[-1])
        elif "VRAM Total Used Memory (B):" in line and "VIS_VRAM" not in line:
            gpus[gpu_id]["used"] = int(line.split(": ")[-1])

    return gpus


def get_process_name(pid):
    """通过 PID 获取进程短名。"""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2,
        )
        name = result.stdout.strip()
        return name if name else "unknown"
    except Exception:
        return "unknown"


def parse_bytes(value):
    """把 ROCm/AMD-SMI 返回的显存字段转换为 int bytes。"""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def get_vram_from_amd_smi_process(info):
    """从 amd-smi process --json 的 process_info 中提取 VRAM bytes。"""
    memory_usage = info.get("memory_usage") or {}
    vram_mem = memory_usage.get("vram_mem") or {}
    if isinstance(vram_mem, dict):
        value = parse_bytes(vram_mem.get("value"))
        if value:
            return value

    mem_usage = info.get("mem_usage") or {}
    if isinstance(mem_usage, dict):
        return parse_bytes(mem_usage.get("value"))

    return 0


def get_gpu_processes_from_amd_smi():
    """优先使用 amd-smi 的 JSON 输出，它按真实 GPU 聚合进程。"""
    try:
        result = subprocess.run(
            ["amd-smi", "process", "--json"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    processes = []
    for gpu_entry in data if isinstance(data, list) else []:
        gpu_id = str(gpu_entry.get("gpu", "")).strip()
        if not gpu_id.isdigit():
            continue

        process_list = gpu_entry.get("process_list") or []
        for item in process_list:
            info = item.get("process_info") if isinstance(item, dict) else None
            if not isinstance(info, dict):
                continue

            pid = str(info.get("pid", "")).strip()
            if not pid.isdigit():
                continue

            name = str(info.get("name") or "").strip()
            if not name or name == "N/A":
                name = get_process_name(pid)

            processes.append({
                "pid": pid,
                "name": name,
                "gpus": gpu_id,
                "vram_bytes": get_vram_from_amd_smi_process(info),
                "source": "amd-smi",
            })

    return processes


def get_pid_gpu_map_from_rocm_smi():
    """解析 rocm-smi --showpidgpus，用更准确的 DRM/GPU 映射修正 --showpids。"""
    try:
        result = subprocess.run(
            ["rocm-smi", "--showpidgpus"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {}

    pid_to_gpus = {}
    current_pid = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        pid_match = re.match(r"PID\s+(\d+)\s+is using\s+(\d+)\s+DRM device", line)
        if pid_match:
            current_pid = pid_match.group(1)
            if int(pid_match.group(2)) == 0:
                pid_to_gpus[current_pid] = []
                current_pid = None
            else:
                pid_to_gpus.setdefault(current_pid, [])
            continue

        if current_pid:
            gpus = re.findall(r"\d+", line)
            if gpus:
                pid_to_gpus[current_pid].extend(gpus)
                current_pid = None

    return {
        pid: sorted(set(gpus), key=int)
        for pid, gpus in pid_to_gpus.items()
        if gpus
    }


def get_gpu_processes_from_rocm_smi():
    """解析 rocm-smi --showpids，并用 --showpidgpus 修正 GPU 列。

    返回格式: [{"pid": str, "name": str, "gpus": str, "vram_bytes": int}, ...]
    """
    result = subprocess.run(
        ["rocm-smi", "--showpids"],
        capture_output=True, text=True,
    )

    processes = []
    in_table = False
    for line in result.stdout.split("\n"):
        # 跳过分隔线和空行
        if line.startswith("===") or not line.strip():
            if in_table:
                in_table = False
            continue

        # 跳过标题行（包含 "KFD process" 或列头 "PID"）
        if "KFD process" in line:
            continue
        if line.strip().startswith("PID"):
            in_table = True
            continue

        if not in_table:
            continue

        # 解析表格行:
        # PID   	PROCESS NAME   	GPU(s)	VRAM USED  	SDMA USED    	CU OCCUPANCY
        # 465362	ray::WorkerDict	1     	14629593088	2798958970101	11
        parts = line.split("\t")
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 4:
            continue

        try:
            pid = parts[0]
            if not pid.isdigit():
                continue
            processes.append({
                "pid": pid,
                "name": parts[1],
                "gpus": parts[2],
                "vram_bytes": int(parts[3]),
                "source": "rocm-smi",
            })
        except (ValueError, IndexError):
            continue

    pid_gpu_map = get_pid_gpu_map_from_rocm_smi()
    for proc in processes:
        mapped_gpus = pid_gpu_map.get(proc["pid"])
        if mapped_gpus:
            proc["gpus"] = ",".join(mapped_gpus)
            proc["source"] = "rocm-smi+pidgpus"

    return processes


def get_gpu_processes():
    """返回 GPU 进程列表，优先选择能提供准确 per-GPU 映射的数据源。"""
    processes = get_gpu_processes_from_amd_smi()
    if processes:
        return processes
    return get_gpu_processes_from_rocm_smi()


def build_gpu_pid_map(processes):
    """从进程列表构建 gpu_id -> [pid] 的映射（用于 GPU 概览中显示进程）。

    注意: rocm-smi --showpids 的 GPU(s) 列可能不精确反映多卡分配，
    对于 Ray 等分布式框架，一个进程可能实际使用多张卡但只显示一个 GPU ID。
    这里尽量做合理映射。
    """
    gpu_pids = {}
    for proc in processes:
        gpu_str = proc["gpus"]
        # GPU(s) 列可能是 "0", "1", "0,1,2" 等
        for gid in gpu_str.split(","):
            gid = gid.strip()
            if gid.isdigit():
                gpu_pids.setdefault(gid, [])
                if proc["pid"] not in gpu_pids[gid]:
                    gpu_pids[gid].append(proc["pid"])
    return gpu_pids


def get_process_detail(pid):
    """通过 PID 获取进程详细信息：用户、CPU%、内存RSS、运行时长、完整命令。"""
    try:
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "user=,pcpu=,rss=,etime=,args="],
            capture_output=True, text=True, timeout=2,
        )
        line = result.stdout.strip()
        if not line:
            return None

        parts = line.split(None, 4)
        if len(parts) < 5:
            parts += [""] * (5 - len(parts))

        rss_kb = int(parts[2]) if parts[2].isdigit() else 0
        return {
            "pid": pid,
            "user": parts[0],
            "cpu": parts[1],
            "rss_gb": rss_kb / 1048576,  # KB -> GB
            "elapsed": parts[3],
            "cmd": parts[4],
        }
    except Exception:
        return None


def truncate(s, maxlen):
    """截断字符串，超长加省略号。"""
    return s if len(s) <= maxlen else s[: maxlen - 3] + "..."


def main():
    gpus = get_gpu_info()
    gpu_processes = get_gpu_processes()
    gpu_pids = build_gpu_pid_map(gpu_processes)

    if not gpus:
        print("No GPUs found. Is ROCm installed?")
        sys.exit(1)

    B_TO_GB = 1073741824

    # ============ GPU 概览 ============
    print("=== ROCm GPU Memory (GB) ===")
    print(f"{'GPU':<6} {'Used':>10} {'Total':>10} {'Usage':>8}  {'Process'}")
    print("-" * 56)

    total_used = 0
    total_vram = 0

    for gpu_id in sorted(gpus.keys(), key=int):
        info = gpus[gpu_id]
        if "total" not in info or "used" not in info:
            continue

        total_gb = info["total"] / B_TO_GB
        used_gb = info["used"] / B_TO_GB
        pct = used_gb / total_gb * 100 if total_gb > 0 else 0

        total_used += used_gb
        total_vram += total_gb

        # 进程信息
        pids = gpu_pids.get(gpu_id, [])
        if pids:
            proc_names = [f"{get_process_name(p)}({p})" for p in pids[:3]]
            proc_str = ", ".join(proc_names)
            if len(pids) > 3:
                proc_str += f" +{len(pids) - 3} more"
        elif used_gb > 0.5:
            proc_str = "(in use)"
        else:
            proc_str = "-"

        # 使用量进度条
        bar_len = int(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)

        print(f"  {gpu_id:<4} {used_gb:>8.2f}GB {total_gb:>8.2f}GB {pct:>6.1f}%  {proc_str}")
        print(f"       [{bar}]")

    print("-" * 56)
    total_pct = total_used / total_vram * 100 if total_vram > 0 else 0
    print(f"  ALL  {total_used:>8.2f}GB {total_vram:>8.2f}GB {total_pct:>6.1f}%")

    # ============ 进程详情 ============
    if not gpu_processes:
        return

    print()
    print("=== GPU Process Details ===")
    print(f"  {'PID':<8} {'Name':<20} {'GPU':>4} {'VRAM':>10} {'User':<10} {'CPU%':>6} {'RSS':>8} {'Elapsed':>12}  {'Command'}")
    print("  " + "-" * 100)

    for proc in sorted(gpu_processes, key=lambda p: int(p["pid"])):
        vram_gb = proc["vram_bytes"] / B_TO_GB
        detail = get_process_detail(proc["pid"])

        if detail:
            cmd_display = truncate(detail["cmd"], 36)
            print(
                f"  {proc['pid']:<8} {truncate(proc['name'], 18):<20} "
                f"{proc['gpus']:>4} {vram_gb:>8.2f}GB "
                f"{detail['user']:<10} {detail['cpu']:>5}% {detail['rss_gb']:>6.1f}GB "
                f"{detail['elapsed']:>12}  {cmd_display}"
            )
        else:
            print(
                f"  {proc['pid']:<8} {truncate(proc['name'], 18):<20} "
                f"{proc['gpus']:>4} {vram_gb:>8.2f}GB"
            )

    # 汇总
    total_proc_vram = sum(p["vram_bytes"] for p in gpu_processes) / B_TO_GB
    print("  " + "-" * 100)
    print(f"  {len(gpu_processes)} processes, total VRAM: {total_proc_vram:.2f}GB")
    print()


if __name__ == "__main__":
    main()
