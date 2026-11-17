#!/usr/bin/env python3
"""
Yousj OCI Start 图形版 — 打开窗口，粘贴 API 信息，点「开始抢机」就行。

API 信息从哪来：甲骨文控制台右上角头像 → 我的概要信息 → API 密钥 →
添加 API 密钥 → 生成后页面会显示四项（用户 OCID、指纹、租户 OCID、区域）
和一段私钥，全部复制到本窗口对应位置即可。
私钥只保存在本机 ~/.oci/ 目录，不上传任何地方。
"""

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import oci

import yousj_oci_start as core

REGIONS = {
    "东京": "ap-tokyo-1",
    "大阪": "ap-osaka-1",
    "新加坡": "ap-singapore-1",
    "春川": "ap-chuncheon-1",
    "法兰克福": "eu-frankfurt-1",
    "凤凰城": "us-phoenix-1",
    "圣何塞": "us-sanjose-1",
    "阿什本": "us-ashburn-1",
    "伦敦": "uk-london-1",
    "悉尼": "ap-sydney-1",
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "gui_settings.json")
OCI_DIR = os.path.expanduser("~/.oci")
API_KEY_PATH = os.path.join(OCI_DIR, "yousj_api_key.pem")
SSH_PRIV_PATH = os.path.join(OCI_DIR, "yousj_ssh_key")
SSH_PUB_PATH = SSH_PRIV_PATH + ".pub"


def build_oci_config(region_key, tenancy, user, fingerprint, key_content):
    """把界面上填的四项 + 私钥组装成 OCI 配置（纯函数，可测）"""
    cfg = {
        "user": user.strip(),
        "fingerprint": fingerprint.strip(),
        "tenancy": tenancy.strip(),
        "region": region_key,
        "key_content": key_content.strip(),
    }
    missing = [k for k in ("user", "fingerprint", "tenancy", "key_content") if not cfg[k]]
    if missing:
        raise ValueError("还没填：" + "、".join(missing))
    oci.config.validate_config(cfg)
    return cfg


def ensure_ssh_keypair():
    """没有 SSH 密钥就自动生成一对（登录抢到的机器用），返回公钥文本"""
    if os.path.exists(SSH_PUB_PATH):
        with open(SSH_PUB_PATH, encoding="utf-8") as f:
            return f.read().strip()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    os.makedirs(OCI_DIR, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    with open(SSH_PRIV_PATH, "wb") as f:
        f.write(priv)
    try:
        os.chmod(SSH_PRIV_PATH, 0o600)
    except OSError:
        pass
    with open(SSH_PUB_PATH, "wb") as f:
        f.write(pub + b" yousj-oci-start\n")
    return pub.decode().strip() + " yousj-oci-start"


class StopFlag(Exception):
    pass


class App:
    def __init__(self, root):
        self.root = root
        self.log_q = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        root.title("Yousj OCI Start — 甲骨文抢机")
        root.geometry("640x720")

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="区域").grid(row=0, column=0, sticky="e", pady=3)
        self.region = ttk.Combobox(frm, values=list(REGIONS.keys()), state="readonly", width=18)
        self.region.set("东京")
        self.region.grid(row=0, column=1, sticky="w", pady=3)

        self.tenancy = self._entry(frm, 1, "租户 OCID")
        self.user = self._entry(frm, 2, "用户 OCID")
        self.fingerprint = self._entry(frm, 3, "指纹")

        ttk.Label(frm, text="API 私钥").grid(row=4, column=0, sticky="ne", pady=3)
        self.key_text = tk.Text(frm, height=6, width=56)
        self.key_text.grid(row=4, column=1, sticky="w", pady=3)
        ttk.Button(frm, text="从 .pem 文件选", command=self._pick_key).grid(
            row=5, column=1, sticky="w", pady=2)

        ttk.Label(frm, text="重试间隔(秒)").grid(row=6, column=0, sticky="e", pady=3)
        self.interval = ttk.Entry(frm, width=8)
        self.interval.insert(0, "60")
        self.interval.grid(row=6, column=1, sticky="w", pady=3)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, pady=8)
        self.start_btn = ttk.Button(btns, text="开始抢机", command=self.start)
        self.start_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(btns, text="保存填写内容", command=self.save).pack(side="left", padx=5)

        ttk.Label(frm, text="目标：ARM 2核12G ×1 + AMD 小鸡 ×2（免费额度内），"
                            "SSH 密钥自动生成在本机 ~/.oci/").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=2)

        self.log_box = tk.Text(frm, height=16, width=76, state="disabled")
        self.log_box.grid(row=9, column=0, columnspan=2, pady=5)

        self._load()
        self.root.after(150, self._drain_log)

    def _entry(self, frm, row, label):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="e", pady=3)
        e = ttk.Entry(frm, width=56)
        e.grid(row=row, column=1, sticky="w", pady=3)
        return e

    def _pick_key(self):
        path = filedialog.askopenfilename(filetypes=[("PEM 私钥", "*.pem"), ("所有文件", "*.*")])
        if path:
            with open(path, encoding="utf-8") as f:
                self.key_text.delete("1.0", "end")
                self.key_text.insert("1.0", f.read())

    def _settings(self):
        return {
            "region_name": self.region.get(),
            "tenancy": self.tenancy.get(),
            "user": self.user.get(),
            "fingerprint": self.fingerprint.get(),
            "interval": self.interval.get(),
        }

    def save(self):
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(self._settings(), f, ensure_ascii=False, indent=2)
        key = self.key_text.get("1.0", "end").strip()
        if key:
            os.makedirs(OCI_DIR, exist_ok=True)
            with open(API_KEY_PATH, "w", encoding="utf-8") as f:
                f.write(key + "\n")
            try:
                os.chmod(API_KEY_PATH, 0o600)
            except OSError:
                pass
        messagebox.showinfo("已保存", "填写内容已保存在本机，下次打开自动带出")

    def _load(self):
        if os.path.exists(SETTINGS_PATH):
            try:
                s = json.load(open(SETTINGS_PATH, encoding="utf-8"))
                self.region.set(s.get("region_name", "东京"))
                self.tenancy.insert(0, s.get("tenancy", ""))
                self.user.insert(0, s.get("user", ""))
                self.fingerprint.insert(0, s.get("fingerprint", ""))
                self.interval.delete(0, "end")
                self.interval.insert(0, s.get("interval", "60"))
            except Exception:
                pass
        if os.path.exists(API_KEY_PATH):
            self.key_text.insert("1.0", open(API_KEY_PATH, encoding="utf-8").read())

    def _log(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        while not self.log_q.empty():
            msg = self.log_q.get()
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(150, self._drain_log)

    def start(self):
        try:
            cfg = build_oci_config(
                REGIONS[self.region.get()],
                self.tenancy.get(), self.user.get(),
                self.fingerprint.get(), self.key_text.get("1.0", "end"),
            )
            interval = max(10, int(self.interval.get() or 60))
        except Exception as e:
            messagebox.showerror("配置有问题", str(e))
            return
        self.save()
        self.stop_flag.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.worker = threading.Thread(target=self._run, args=(cfg, interval), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag.set()
        self._log("正在停止…（当前这次尝试结束后停下）")

    def _run(self, cfg, interval):
        core.log = lambda m: self._log(m)  # 核心模块的日志转到窗口
        real_sleep = core.time.sleep

        def guarded_sleep(sec):
            if self.stop_flag.is_set():
                raise StopFlag()
            real_sleep(min(sec, 2))
            if self.stop_flag.is_set():
                raise StopFlag()

        core.time.sleep = guarded_sleep
        try:
            tenancy = cfg["tenancy"]
            identity = oci.identity.IdentityClient(cfg)
            compute = oci.core.ComputeClient(cfg)
            vcn = oci.core.VirtualNetworkClient(cfg)
            ads = [a.name for a in identity.list_availability_domains(tenancy).data]
            self._log(f"已连上甲骨文，区域 {cfg['region']}，可用域 {len(ads)} 个")
            ssh_pub = ensure_ssh_keypair()
            self._log(f"SSH 公钥就绪（私钥在 {SSH_PRIV_PATH}，登录机器时用它）")
            job = {"_compartment_id": tenancy, "display_name_prefix": "yousj"}
            subnet_id = core.pick_subnet(vcn, tenancy).id
            targets = [
                {"shape": "VM.Standard.A1.Flex", "ocpus": 2, "memory_gbs": 12,
                 "boot_volume_gbs": 100, "count": 1},
                {"shape": "VM.Standard.E2.1.Micro", "boot_volume_gbs": 50, "count": 2},
            ]
            made = 0
            for t in targets:
                image_id = core.pick_image(compute, tenancy, t["shape"]).id
                for _ in range(t["count"]):
                    made += 1
                    core.snipe_one(compute, job, t, ads, image_id, subnet_id,
                                   ssh_pub, made, interval, 0)
            self._log("✅ 全部抢完！窗口可以关了")
            core.notify_tg("✅ Yousj OCI Start（图形版）抢机完成")
        except StopFlag:
            self._log("已停止")
        except Exception as e:
            self._log(f"❌ 出错停下：{e}")
        finally:
            core.time.sleep = real_sleep
            self.root.after(0, lambda: (
                self.start_btn.configure(state="normal"),
                self.stop_btn.configure(state="disabled"),
            ))


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
