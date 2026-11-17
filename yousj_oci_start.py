#!/usr/bin/env python3
"""
Yousj OCI Start — 甲骨文云自动抢机工具（自研版）

原理：用 OCI 官方 API 循环尝试创建实例，遇到「Out of host capacity」
就换可用域重试，直到开机成功。只调官方 API，凭据只读本机
~/.oci/config，不往任何第三方服务器发送。

用法：
    python yousj_oci_start.py --job job.json [--dry-run]

环境变量（可选，抢到后 Telegram 通知）：
    OCI_START_TG_TOKEN   Bot token
    OCI_START_TG_CHAT    接收通知的 chat id
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime

import oci
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.exceptions import ServiceError

# 值得重试的错误（容量不足 / 服务端抖动 / 限流）
RETRY_CODES = {
    "OutOfHostCapacity",
    "OutOfCapacity",
    "InternalError",
    "ServiceUnavailable",
    "TooManyRequests",
}
RETRY_STATUS = {429, 500, 502, 503, 504}
# 明确不该重试的错误（配额、参数、权限类，重试也不会变好）
STOP_CODES = {
    "LimitExceeded",
    "QuotaExceeded",
    "InvalidParameter",
    "NotAuthorizedOrNotFound",
    "NotAuthenticated",
    "Unauthorized",
    "CompartmentNotFound",
}


def now():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)


def notify_tg(text):
    token = os.environ.get("OCI_START_TG_TOKEN")
    chat = os.environ.get("OCI_START_TG_CHAT")
    if not token or not chat:
        return
    try:
        data = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # 通知失败不影响主流程
        log(f"(Telegram 通知发送失败: {e})")


def classify_error(err: ServiceError):
    """返回 'retry' 或 'stop'"""
    if err.code in STOP_CODES or err.status in (400, 401, 403, 404):
        return "stop"
    if err.code in RETRY_CODES or err.status in RETRY_STATUS:
        return "retry"
    return "stop"


def pick_image(compute, compartment_id, shape):
    """没指定镜像时，自动选该机型可用的最新 Oracle Linux 镜像"""
    resp = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id,
        shape=shape,
        operating_system="Oracle Linux",
        sort_by="TIMECREATED",
        sort_order="DESC",
    )
    if not resp.data:
        raise RuntimeError(f"找不到 shape={shape} 可用的 Oracle Linux 镜像")
    return resp.data[0]


def pick_subnet(vcn, compartment_id):
    resp = oci.pagination.list_call_get_all_results(
        vcn.list_subnets, compartment_id
    )
    if not resp.data:
        raise RuntimeError("该区间里没有子网：请先在控制台建 VCN/子网，或在 job 里指定 subnet_id")
    return resp.data[0]


def build_details(job, target, ad_name, image_id, subnet_id, ssh_key, seq):
    prefix = job.get("display_name_prefix", "yousj")
    shape_cfg = None
    if target.get("ocpus") or target.get("memory_gbs"):
        shape_cfg = LaunchInstanceShapeConfigDetails(
            ocpus=target.get("ocpus"),
            memory_in_gbs=target.get("memory_gbs"),
        )
    return LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=job["_compartment_id"],
        shape=target["shape"],
        display_name=f"{prefix}-{target['shape'].split('.')[-2] if '.' in target['shape'] else 'vm'}-{seq}",
        shape_config=shape_cfg,
        source_details=InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=target.get("boot_volume_gbs", 50),
        ),
        create_vnic_details=CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_key},
    )


def snipe_one(compute, job, target, ads, image_id, subnet_id, ssh_key, seq,
              interval, max_attempts, create_fn=None):
    """抢一台：轮换可用域重试，直到成功 / 命中不可重试错误 / 用完次数"""
    create = create_fn or compute.launch_instance
    attempt = 0
    while True:
        attempt += 1
        ad = ads[(attempt - 1) % len(ads)]
        details = build_details(job, target, ad, image_id, subnet_id, ssh_key, seq)
        try:
            resp = create(details)
            inst = resp.data
            log(f"🎉 抢到了！{inst.display_name}  状态={inst.lifecycle_state}  OCID={inst.id}")
            return inst
        except ServiceError as e:
            verdict = classify_error(e)
            log(f"第 {attempt} 次尝试失败（{ad}）：[{e.code}] {e.message}")
            if verdict == "stop":
                raise RuntimeError(f"不可重试的错误，停：[{e.code}] {e.message}") from e
            if max_attempts and attempt >= max_attempts:
                raise RuntimeError(f"已达最大尝试次数 {max_attempts}，放弃") from e
            wait = interval + random.uniform(0, interval * 0.2)
            if e.status == 429:
                wait *= 3
            log(f"   {wait:.0f} 秒后重试…")
            time.sleep(wait)


def main():
    ap = argparse.ArgumentParser(description="Yousj OCI Start — 甲骨文自动抢机")
    ap.add_argument("--job", required=True, help="任务配置 JSON 路径")
    ap.add_argument("--oci-config", default="~/.oci/config", help="OCI 配置文件路径")
    ap.add_argument("--profile", default="DEFAULT", help="OCI 配置 profile 名")
    ap.add_argument("--dry-run", action="store_true", help="只验证配置、列出可用域/镜像，不真开机")
    args = ap.parse_args()

    with open(os.path.expanduser(args.job), encoding="utf-8") as f:
        job = json.load(f)

    config = oci.config.from_file(os.path.expanduser(args.oci_config), args.profile)
    if job.get("region"):
        config["region"] = job["region"]
    oci.config.validate_config(config)

    tenancy = config["tenancy"]
    compartment_id = job.get("compartment_id") or tenancy
    job["_compartment_id"] = compartment_id

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    vcn = oci.core.VirtualNetworkClient(config)

    ads = [ad.name for ad in identity.list_availability_domains(tenancy).data]
    if not ads:
        raise RuntimeError("拿不到可用域列表，检查凭据/区域")
    log(f"区域 {config['region']}，可用域：{', '.join(ads)}")

    ssh_key_path = os.path.expanduser(job["ssh_key_file"])
    with open(ssh_key_path, encoding="utf-8") as f:
        ssh_key = f.read().strip()

    subnet_id = job.get("subnet_id") or pick_subnet(vcn, compartment_id).id
    interval = int(job.get("interval_seconds", 60))
    max_attempts = int(job.get("max_attempts", 0))

    made = []
    for target in job["targets"]:
        image_id = job.get("image_id") or pick_image(compute, compartment_id, target["shape"]).id
        log(f"目标：{target['shape']} × {target.get('count', 1)}"
            f"（{target.get('ocpus', '-')} OCPU / {target.get('memory_gbs', '-')} GB），镜像 {image_id}")
        if args.dry_run:
            continue
        for i in range(target.get("count", 1)):
            inst = snipe_one(compute, job, target, ads, image_id, subnet_id,
                             ssh_key, len(made) + 1, interval, max_attempts)
            made.append(inst)

    if args.dry_run:
        log("dry-run 完成：配置可用，未真正开机")
    else:
        summary = f"✅ Yousj OCI Start 抢机完成，共 {len(made)} 台：" + \
                  "；".join(f"{i.display_name}（{config['region']}）" for i in made)
        log(summary)
        notify_tg(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
